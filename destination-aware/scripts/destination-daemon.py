#!/usr/bin/env python3
"""Destination-aware reminders daemon.

Connects to Home Assistant's WebSocket API, subscribes to a state trigger
for the Tesla destination sensor, and fires the context pipeline when a
destination is set.

Liveness is enforced by TWO independent signals, both of which will force
a reconnect on failure:

1. App-layer ping/pong every PING_INTERVAL seconds. HA must answer within
   PONG_TIMEOUT or the connection is treated as wedged. This is the signal
   that was missing from the old SSE implementation — SSE pings kept the
   TCP pipe alive but proved nothing about HA actually routing events to us.

2. Periodic REST sanity check: poll /api/states/<entity> every SANITY_INTERVAL
   and compare last_changed against the last event we received over WS. If
   REST advanced but WS didn't see it, the subscription is broken — log,
   alert Manan via outbox, and reconnect.

launchd KeepAlive restarts the process on any unhandled exit.
"""

import json
import logging
import os
import subprocess
import time
import urllib.request
from threading import Event, Lock

import websocket

HA_CONFIG = os.path.expanduser("~/.config/home-assistant/config.json")
CONTEXT_SCRIPT = os.path.expanduser(
    "~/.config/spratt/infrastructure/destination/destination-context.py"
)
OUTBOX_CLI = os.path.expanduser(
    "~/.config/spratt/infrastructure/outbox/outbox.py"
)
MANAN = "Manan"  # resolved by outbox.py via contacts.sqlite (infrastructure/contacts)
ENTITY_ID = "sensor.maha_tesla_destination"
STATE_FILE = os.path.expanduser(
    "~/.config/spratt/infrastructure/destination/last-handled.json"
)
HEARTBEAT_FILE = os.path.expanduser(
    "~/.config/spratt/infrastructure/destination/.heartbeat"
)
KNOWN_DESTINATIONS_FILE = os.path.expanduser(
    "~/.config/spratt/infrastructure/destination/known-destinations.json"
)

PING_INTERVAL = 30       # send app-layer ping every N seconds
PONG_TIMEOUT = 10        # require pong within N seconds
SANITY_INTERVAL = 300    # REST sanity check every N seconds
RECV_TIMEOUT = 1         # WS recv() timeout — drives the event loop tick
BACKOFF_START = 5
BACKOFF_MAX = 60

LOG_DIR = os.path.expanduser("~/Library/Logs/spratt")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [destination] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "destination-daemon.log")),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# --- HA config ---

def load_ha_config():
    with open(HA_CONFIG) as f:
        cfg = json.load(f)
    return cfg["url"], cfg["token"]


def ws_url(http_url: str) -> str:
    if http_url.startswith("https://"):
        return "wss://" + http_url[len("https://"):].rstrip("/") + "/api/websocket"
    if http_url.startswith("http://"):
        return "ws://" + http_url[len("http://"):].rstrip("/") + "/api/websocket"
    raise ValueError(f"unexpected HA url scheme: {http_url}")


def rest_state(ha_url: str, ha_token: str, entity: str):
    """Return (state, last_changed_iso) or (None, None) on failure."""
    req = urllib.request.Request(
        f"{ha_url}/api/states/{entity}",
        headers={"Authorization": f"Bearer {ha_token}"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        return data.get("state", "unknown"), data.get("last_changed")
    except Exception as e:
        log.error(f"REST state fetch failed for {entity}: {e}")
        return None, None


def rest_destination_coords(ha_url: str, ha_token: str):
    try:
        req = urllib.request.Request(
            f"{ha_url}/api/states/device_tracker.maha_tesla_route",
            headers={"Authorization": f"Bearer {ha_token}"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        attrs = data.get("attributes") or {}
        lat, lng = attrs.get("latitude"), attrs.get("longitude")
        if lat and lng:
            return float(lat), float(lng)
    except Exception as e:
        log.warning(f"coords fetch failed: {e}")
    return None, None


# --- Context pipeline ---

def lookup_known(destination):
    """Match destination against known-destinations.json.

    Case-insensitive substring match, longest key wins so that
    'Bright Horizons at Woodinville' matches 'bright horizons' rather
    than accidentally hitting a shorter key.

    Returns {"name": str, "categories": [str, ...]} or None.
    """
    try:
        with open(KNOWN_DESTINATIONS_FILE) as f:
            table = json.load(f).get("destinations", {})
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.warning(f"known-destinations.json unavailable: {e}")
        return None

    dest_lower = (destination or "").lower()
    best_key = None
    for key in table:
        if key in dest_lower and (best_key is None or len(key) > len(best_key)):
            best_key = key
    if best_key is None:
        return None
    entry = table[best_key]
    return {"name": entry.get("name", best_key.title()), "categories": entry.get("categories", [])}


def gather_context(destination, lat=None, lng=None, known=None):
    try:
        cmd = ["/usr/bin/python3", CONTEXT_SCRIPT, "--destination", destination]
        if known:
            cmd.extend([
                "--known-name", known["name"],
                "--known-categories", ",".join(known["categories"]),
            ])
        elif lat and lng:
            cmd.extend(["--lat", str(lat), "--lng", str(lng)])
        env = os.environ.copy()
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
        if r.returncode != 0:
            log.error(f"Context script failed: {r.stderr}")
            return None
        return json.loads(r.stdout)
    except Exception as e:
        log.error(f"Context gather failed: {e}")
        return None


def _parse_open_reminders(reminders_text):
    """Extract open reminder text (stripping '[ ]' and trailing '[tags]')."""
    out = []
    for line in (reminders_text or "").split("\n"):
        if "[ ]" not in line:
            continue
        parts = line.split("[ ] ", 1)
        if len(parts) > 1:
            out.append(parts[1].split(" [")[0].strip())
    return out


def llm_filter_grocery(items, place):
    """Ask Haiku which items are grocery-relevant.

    Returns the filtered subset, or None if the LLM call failed / key missing.
    Callers should stay silent on None — better silence than dumping noise.
    """
    if not items:
        return []
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("ANTHROPIC_API_KEY not set; skipping grocery LLM filter")
        return None

    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(items))
    user_content = (
        f"Destination: {place} (a grocery store).\n"
        f"Reminders (open):\n{numbered}\n\n"
        f"Pick ONLY reminders that are general grocery-shopping items to add to the cart — "
        f"food, drinks, household consumables bought during a routine shop. "
        f"EXCLUDE items that reference another destination, person, or delivery target "
        f'(e.g. "bring X for Sriram", "drop off at daycare", "take to work"), '
        f"even if the item itself is sold at the store. "
        f"EXCLUDE work/project todos, research tasks, setup tasks, and anything that isn't "
        f"something you physically pick up off a grocery shelf. "
        f'Return ONLY JSON: {{"indices": [<1-based ints>]}}. '
        f'If none match, return {{"indices": []}}.'
    )
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 256,
        "messages": [{"role": "user", "content": user_content}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read())
        text = result["content"][0]["text"].strip()
        if text.startswith("```"):
            text = "\n".join(l for l in text.split("\n") if not l.strip().startswith("```")).strip()
        # Tolerate prose around the JSON — pull the first balanced {...}
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            text = text[start:end + 1]
        parsed = json.loads(text)
        indices = parsed.get("indices", [])
        picked = [items[i - 1] for i in indices if isinstance(i, int) and 1 <= i <= len(items)]
        return picked
    except Exception as e:
        log.warning(f"Grocery LLM filter failed: {e}")
        return None


def _filter_by_keywords(reminders_text, keywords):
    """Return open-reminder texts that contain any keyword (case-insensitive)."""
    kws = [k.lower().strip() for k in keywords if k and k.strip()]
    if not kws:
        return []
    out = []
    for item in _parse_open_reminders(reminders_text):
        low = item.lower()
        if any(kw in low for kw in kws):
            out.append(item)
    return out


# Uniform rule across every branch: if no relevant reminder matches, stay silent.
# Destination -> corresponding reminder -> text. No match -> nothing.
BRANCHES = [
    # (category, emoji, keyword list, include place name as keyword)
    ("daycare",    "🏫", ["sriram", "daycare", "preschool", "bright horizons",
                          "pickup", "drop off", "drop-off", "dropoff",
                          "diaper", "bottle", "blanket", "nap", "formula",
                          "snack", "lunch box", "lunchbox", "tuition",
                          "permission slip", "sign-in", "sign in"], True),
    ("pharmacy",   "💊", ["pharmacy", "prescription", "refill", "rx", "medication"], True),
    ("medical",    "🏥", ["doctor", "appointment", "checkup", "consultation", "ask about"], True),
    ("home",       "🏠", ["home"], True),
    ("work",       "💼", ["work", "office"], True),
    ("restaurant", "🍽", [], True),  # match only on place name — generic restaurant keywords are too broad
]


def compose_message(context):
    place = context.get("place_name", "Unknown")
    categories = context.get("categories", [])
    reminders = context.get("reminders", "")

    # Grocery gets its own LLM-filter path (more permissive than keyword match).
    if "grocery" in categories:
        open_items = _parse_open_reminders(reminders)
        picked = llm_filter_grocery(open_items, place)
        if picked:
            return f"🛒 Heading to {place}\nShopping list: {', '.join(picked)}"
        # None (LLM failed) or [] (nothing grocery-relevant) → silent
        return None

    # Every other branch: keyword-filter reminders, stay silent if nothing matches.
    for category, emoji, keywords, include_place in BRANCHES:
        if category not in categories:
            continue
        kws = list(keywords)
        if include_place and place and place.lower() != "unknown":
            kws.append(place.lower())
        matches = _filter_by_keywords(reminders, kws)
        if matches:
            return f"{emoji} Heading to {place}\nDon't forget: {', '.join(matches[:5])}"
        return None  # category matched but no reminder matched — silent

    return None  # no category matched — silent


def send_outbox(body: str, source: str):
    try:
        subprocess.run(
            [
                "/usr/bin/python3", OUTBOX_CLI, "schedule",
                "--to", MANAN,
                "--body", body,
                "--at", "now",
                "--source", source,
                "--created-by", "destination-daemon",
            ],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        log.error(f"outbox send failed: {e}")


def handle_destination(destination, ha_url, ha_token):
    log.info(f"Destination set: {destination}")
    known = lookup_known(destination)
    if known:
        log.info(f"Matched known destination: {known['name']} ({', '.join(known['categories'])}) — skipping goplaces")
        context = gather_context(destination, known=known)
    else:
        lat, lng = rest_destination_coords(ha_url, ha_token)
        if lat:
            log.info(f"Destination coords: {lat}, {lng}")
        context = gather_context(destination, lat=lat, lng=lng)
    if not context:
        log.info("No context gathered, skipping")
        return
    categories = context.get("categories", [])
    place_name = context.get("place_name", "Unknown")
    log.info(f"Resolved: {place_name} ({', '.join(categories) or 'uncategorized'})")
    message = compose_message(context)
    if not message:
        log.info(f"No relevant context for {place_name}, staying silent")
        return
    log.info(f"Sending notification for {place_name}")
    send_outbox(message, "destination-aware")


def load_last_handled():
    try:
        with open(STATE_FILE) as f:
            return json.load(f).get("destination")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_last_handled(destination):
    with open(STATE_FILE, "w") as f:
        json.dump({"destination": destination}, f)


def clear_last_handled():
    try:
        os.remove(STATE_FILE)
    except FileNotFoundError:
        pass


# --- WebSocket session ---

class WedgedConnection(Exception):
    pass


class Session:
    """One WebSocket lifecycle: auth → subscribe → recv loop → close."""

    def __init__(self, ha_url: str, ha_token: str):
        self.ha_url = ha_url
        self.ha_token = ha_token
        self.ws = None
        self._id = 1
        self._id_lock = Lock()
        self._pending_ping_id = None
        self._pending_ping_sent_at = 0.0
        self._last_event_at = time.time()
        self._last_rest_last_changed = None  # str or None — last_changed we observed via WS or REST startup

    def next_id(self) -> int:
        with self._id_lock:
            v = self._id
            self._id += 1
            return v

    def send(self, obj: dict) -> None:
        self.ws.send(json.dumps(obj))

    def recv_json(self, timeout: float):
        self.ws.settimeout(timeout)
        try:
            raw = self.ws.recv()
        except websocket.WebSocketTimeoutException:
            return None
        if raw is None or raw == "":
            raise WedgedConnection("empty frame")
        return json.loads(raw)

    def connect_and_auth(self) -> None:
        url = ws_url(self.ha_url)
        log.info(f"Connecting to {url}")
        self.ws = websocket.create_connection(url, timeout=15)
        # 1) auth_required
        msg = self.recv_json(10)
        if not msg or msg.get("type") != "auth_required":
            raise WedgedConnection(f"expected auth_required, got {msg}")
        # 2) auth
        self.send({"type": "auth", "access_token": self.ha_token})
        msg = self.recv_json(10)
        if not msg or msg.get("type") != "auth_ok":
            raise WedgedConnection(f"auth failed: {msg}")
        log.info(f"Authenticated (ha_version={msg.get('ha_version')})")

    def subscribe(self) -> int:
        sub_id = self.next_id()
        self.send({
            "id": sub_id,
            "type": "subscribe_trigger",
            "trigger": {"platform": "state", "entity_id": ENTITY_ID},
        })
        # Wait for result ack (may race with an early event; handle inline).
        deadline = time.time() + 10
        while time.time() < deadline:
            msg = self.recv_json(2)
            if msg is None:
                continue
            if msg.get("type") == "result" and msg.get("id") == sub_id:
                if not msg.get("success", False):
                    raise WedgedConnection(f"subscribe failed: {msg}")
                log.info(f"Subscribed to {ENTITY_ID} (trigger id={sub_id})")
                return sub_id
            # Unlikely but possible: handle stray event
            self._dispatch(msg)
        raise WedgedConnection("subscribe ack timeout")

    def send_ping(self) -> None:
        if self._pending_ping_id is not None:
            return  # already waiting on one
        pid = self.next_id()
        self.send({"id": pid, "type": "ping"})
        self._pending_ping_id = pid
        self._pending_ping_sent_at = time.time()

    def check_pong_overdue(self) -> None:
        if self._pending_ping_id is None:
            return
        age = time.time() - self._pending_ping_sent_at
        if age > PONG_TIMEOUT:
            raise WedgedConnection(f"pong overdue: {int(age)}s (pending id={self._pending_ping_id})")

    def _dispatch(self, msg: dict) -> None:
        """Handle one server message."""
        t = msg.get("type")
        if t == "pong":
            if msg.get("id") == self._pending_ping_id:
                self._pending_ping_id = None
            return
        if t == "event":
            self._last_event_at = time.time()
            variables = (((msg.get("event") or {}).get("variables") or {}).get("trigger") or {})
            to_state = variables.get("to_state") or {}
            from_state = variables.get("from_state") or {}
            new_state = to_state.get("state", "unknown")
            old_state = from_state.get("state", "unknown")
            last_changed = to_state.get("last_changed")
            if last_changed:
                self._last_rest_last_changed = last_changed
            self._on_state_change(old_state, new_state)
            return
        if t == "result":
            # Subscription result or late ack — ignore beyond logging failures
            if not msg.get("success", True):
                log.warning(f"result error: {msg}")
            return
        log.debug(f"unhandled message type: {t}")

    def _on_state_change(self, old_state: str, new_state: str) -> None:
        last_handled = load_last_handled()
        if new_state not in ("unknown", "unavailable", "") and new_state != old_state:
            handle_destination(new_state, self.ha_url, self.ha_token)
            save_last_handled(new_state)
        elif new_state in ("unknown", "unavailable") and old_state not in ("unknown", "unavailable", ""):
            log.info("Navigation ended, destination cleared")
            clear_last_handled()

    def rest_sanity_check(self) -> None:
        """Compare REST last_changed to what WS has seen. Mismatch => wedged."""
        state, last_changed = rest_state(self.ha_url, self.ha_token, ENTITY_ID)
        if state is None:
            return  # REST itself failed; don't blame WS
        if self._last_rest_last_changed is None:
            # First check — just prime the value.
            self._last_rest_last_changed = last_changed
            return
        if last_changed and last_changed > self._last_rest_last_changed:
            # REST saw a change we didn't. Subscription is broken.
            msg = (
                f"destination-daemon: WS missed a state change on {ENTITY_ID}. "
                f"REST last_changed={last_changed} but WS last seen={self._last_rest_last_changed}. "
                f"Reconnecting."
            )
            log.error(msg)
            send_outbox(msg, "destination-aware-health")
            raise WedgedConnection("REST sanity check detected missed event")

    def run(self) -> None:
        """Main loop for this session. Raises WedgedConnection on any failure."""
        self.connect_and_auth()

        # Prime REST baseline before subscribing so sanity check has a reference.
        _, self._last_rest_last_changed = rest_state(self.ha_url, self.ha_token, ENTITY_ID)

        self.subscribe()

        # Startup: if there's already an active destination we haven't handled,
        # process it once so restarts don't miss in-flight trips.
        current_state, _ = rest_state(self.ha_url, self.ha_token, ENTITY_ID)
        last_handled = load_last_handled()
        if current_state and current_state not in ("unknown", "unavailable", "") and current_state != last_handled:
            log.info(f"Destination already active on startup, processing: {current_state}")
            handle_destination(current_state, self.ha_url, self.ha_token)
            save_last_handled(current_state)
        elif current_state == last_handled:
            log.info(f"Destination {current_state} already handled, skipping")

        next_ping_at = time.time() + PING_INTERVAL
        next_sanity_at = time.time() + SANITY_INTERVAL

        while True:
            msg = self.recv_json(RECV_TIMEOUT)
            if msg is not None:
                self._dispatch(msg)

            now = time.time()
            if now >= next_ping_at:
                self.send_ping()
                next_ping_at = now + PING_INTERVAL
            self.check_pong_overdue()

            # Heartbeat for spratt-health: refresh mtime every tick. A stale
            # mtime means the loop itself is wedged (different from WS wedged).
            try:
                with open(HEARTBEAT_FILE, "w") as f:
                    f.write(str(now))
            except Exception:
                pass

            if now >= next_sanity_at:
                self.rest_sanity_check()
                next_sanity_at = now + SANITY_INTERVAL

    def close(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None


def main():
    ha_url, ha_token = load_ha_config()
    log.info(f"Starting destination daemon (WebSocket), watching {ENTITY_ID}")

    backoff = BACKOFF_START
    while True:
        sess = Session(ha_url, ha_token)
        try:
            sess.run()
        except WedgedConnection as e:
            log.error(f"wedged: {e}")
        except KeyboardInterrupt:
            log.info("Shutting down")
            sess.close()
            return 0
        except Exception as e:
            log.error(f"session error: {e.__class__.__name__}: {e}")
        finally:
            sess.close()

        log.info(f"Reconnecting in {backoff}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, BACKOFF_MAX)
        # Reset backoff after a long enough wait — if we survive a full cycle
        # the next failure should start from the bottom.
        if backoff >= BACKOFF_MAX:
            backoff = BACKOFF_START


if __name__ == "__main__":
    main()
