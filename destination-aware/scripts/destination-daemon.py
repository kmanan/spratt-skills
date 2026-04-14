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
MANAN = "Manan"  # resolved by outbox.py via contacts.sqlite; use your alias here
ENTITY_ID = "sensor.maha_tesla_destination"
STATE_FILE = os.path.expanduser(
    "~/.config/spratt/infrastructure/destination/last-handled.json"
)
HEARTBEAT_FILE = os.path.expanduser(
    "~/.config/spratt/infrastructure/destination/.heartbeat"
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


# --- Context pipeline (unchanged from previous daemon) ---

def gather_context(destination, lat=None, lng=None):
    try:
        cmd = ["/usr/bin/python3", CONTEXT_SCRIPT, "--destination", destination]
        if lat and lng:
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


def compose_message(context):
    place = context.get("place_name", "Unknown")
    categories = context.get("categories", [])
    reminders = context.get("reminders", "")
    calendar = context.get("calendar_today", "")
    lines = []

    if "grocery" in categories:
        shopping_items = []
        if "Shopping list:" in reminders:
            shopping_section = reminders.split("Shopping list:\n")[1].split("\n\n")[0]
            for line in shopping_section.strip().split("\n"):
                if "[ ]" in line:
                    parts = line.split("[ ] ", 1)
                    if len(parts) > 1:
                        shopping_items.append(parts[1].split(" [")[0].strip())
            if shopping_items:
                lines.append(f"🛒 Heading to {place}")
                lines.append(f"Shopping list: {', '.join(shopping_items)}")
        if not shopping_items:
            open_reminders = []
            for line in reminders.split("\n"):
                if "[ ]" in line:
                    parts = line.split("[ ] ", 1)
                    if len(parts) > 1:
                        open_reminders.append(parts[1].split(" [")[0].strip())
            if open_reminders:
                lines.append(f"🛒 Heading to {place}")
                lines.append(f"Reminders: {', '.join(open_reminders[:5])}")

    elif "daycare" in categories:
        lines.append(f"🏫 Heading to {place}")
        open_reminders = []
        for line in reminders.split("\n"):
            if "[ ]" in line:
                parts = line.split("[ ] ", 1)
                if len(parts) > 1:
                    open_reminders.append(parts[1].split(" [")[0].strip())
        if open_reminders:
            lines.append(f"Don't forget: {', '.join(open_reminders[:5])}")

    elif "medical" in categories:
        lines.append(f"🏥 Heading to {place}")
        if calendar:
            lines.append(f"Today's calendar:\n{calendar[:200]}")

    elif "restaurant" in categories:
        lines.append(f"🍽 Heading to {place}")
        if calendar:
            lines.append(f"Today's calendar:\n{calendar[:200]}")

    return "\n".join(lines) if lines else None


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
