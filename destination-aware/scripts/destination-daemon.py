#!/usr/bin/env python3
"""Destination-aware reminders daemon.

Subscribes to Home Assistant SSE stream, watches for Tesla destination
changes, and fires the context pipeline when a destination is set.

Usage:
    python3 destination-daemon.py

Runs as a persistent daemon (launchd KeepAlive).
"""

import json
import logging
import os
import subprocess
import sys
import time
import urllib.request

HA_CONFIG = os.path.expanduser("~/.config/home-assistant/config.json")
CONTEXT_SCRIPT = os.path.expanduser(
    "~/.config/spratt/infrastructure/destination/destination-context.py"
)
OUTBOX_CLI = os.path.expanduser(
    "~/.config/spratt/infrastructure/outbox/outbox.py"
)
MANAN = "+1XXXXXXXXXX"  # Replace with your phone number
ENTITY_ID = "sensor.maha_tesla_destination"

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


def load_ha_config():
    with open(HA_CONFIG) as f:
        cfg = json.load(f)
    return cfg["url"], cfg["token"]


def get_current_state(ha_url, ha_token):
    """Get current state of the destination sensor."""
    req = urllib.request.Request(
        f"{ha_url}/api/states/{ENTITY_ID}",
        headers={"Authorization": f"Bearer {ha_token}"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        return data.get("state", "unknown")
    except Exception as e:
        log.error(f"Failed to get current state: {e}")
        return "unknown"


def subscribe_sse(ha_url, ha_token):
    """Subscribe to HA event stream, yield state_changed events for our entity.

    HA sends ping events every ~30s. If no data arrives for 120s,
    the connection is considered dead and we raise to trigger reconnect.
    """
    import socket
    import select

    req = urllib.request.Request(
        f"{ha_url}/api/stream",
        headers={
            "Authorization": f"Bearer {ha_token}",
            "Accept": "text/event-stream",
        },
    )
    resp = urllib.request.urlopen(req, timeout=30)
    sock = resp.fp.raw._sock if hasattr(resp.fp, 'raw') else None
    if sock:
        sock.setblocking(False)

    buffer = ""
    event_type = None
    last_data_time = time.time()

    while True:
        # Check for stale connection (no data in 120s)
        if time.time() - last_data_time > 120:
            raise ConnectionError("SSE stream stale — no data for 120s")

        # Use select for timeout-aware reading
        if sock:
            ready, _, _ = select.select([sock], [], [], 30)
            if not ready:
                continue

        try:
            raw_line = resp.readline()
        except (socket.error, OSError):
            raise ConnectionError("SSE read error")

        if not raw_line:
            raise ConnectionError("SSE stream closed by server")

        last_data_time = time.time()
        line = raw_line.decode("utf-8", errors="replace").rstrip("\n")

        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            buffer += line[5:].strip()
        elif line == "":
            if event_type == "state_changed" and buffer:
                try:
                    data = json.loads(buffer)
                    entity = data.get("data", {}).get("entity_id", "")
                    if entity == ENTITY_ID:
                        new_state = data["data"].get("new_state", {}).get("state", "unknown")
                        old_state = data["data"].get("old_state", {}).get("state", "unknown")
                        yield old_state, new_state
                except (json.JSONDecodeError, KeyError):
                    pass
            buffer = ""
            event_type = None


def gather_context(destination, lat=None, lng=None):
    """Run destination-context.py and return parsed JSON."""
    try:
        cmd = ["/usr/bin/python3", CONTEXT_SCRIPT, "--destination", destination]
        if lat and lng:
            cmd.extend(["--lat", str(lat), "--lng", str(lng)])
        env = os.environ.copy()
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        if r.returncode != 0:
            log.error(f"Context script failed: {r.stderr}")
            return None
        return json.loads(r.stdout)
    except Exception as e:
        log.error(f"Context gather failed: {e}")
        return None


def compose_message(context):
    """Compose a concise text from gathered context."""
    place = context.get("place_name", "Unknown")
    categories = context.get("categories", [])
    reminders = context.get("reminders", "")
    calendar = context.get("calendar_today", "")

    lines = []

    if "grocery" in categories:
        # Extract just the Shopping list items if present
        shopping_items = []
        if "Shopping list:" in reminders:
            shopping_section = reminders.split("Shopping list:\n")[1].split("\n\n")[0]
            for line in shopping_section.strip().split("\n"):
                # Parse reminder lines like "[1] [ ] Item name [List] — date"
                if "[ ]" in line:
                    # Extract item name between "[ ] " and " ["
                    parts = line.split("[ ] ", 1)
                    if len(parts) > 1:
                        item = parts[1].split(" [")[0].strip()
                        shopping_items.append(item)
            if shopping_items:
                lines.append(f"🛒 Heading to {place}")
                lines.append(f"Shopping list: {', '.join(shopping_items)}")

        if not shopping_items:
            # No shopping items, check other reminders
            open_reminders = []
            for line in reminders.split("\n"):
                if "[ ]" in line:
                    parts = line.split("[ ] ", 1)
                    if len(parts) > 1:
                        item = parts[1].split(" [")[0].strip()
                        open_reminders.append(item)
            if open_reminders:
                lines.append(f"🛒 Heading to {place}")
                lines.append(f"Reminders: {', '.join(open_reminders[:5])}")

    elif "daycare" in categories:
        lines.append(f"🏫 Heading to {place}")
        # Surface any kid-related reminders
        open_reminders = []
        for line in reminders.split("\n"):
            if "[ ]" in line:
                parts = line.split("[ ] ", 1)
                if len(parts) > 1:
                    item = parts[1].split(" [")[0].strip()
                    open_reminders.append(item)
        if open_reminders:
            lines.append(f"Don't forget: {', '.join(open_reminders[:5])}")

    elif "medical" in categories:
        lines.append(f"🏥 Heading to {place}")
        # Surface calendar event notes for this location
        if calendar:
            lines.append(f"Today's calendar:\n{calendar[:200]}")

    elif "restaurant" in categories:
        lines.append(f"🍽 Heading to {place}")
        if calendar:
            lines.append(f"Today's calendar:\n{calendar[:200]}")

    if not lines:
        return None

    return "\n".join(lines)


def send_notification(message):
    """Send via outbox."""
    try:
        subprocess.run(
            [
                "/usr/bin/python3",
                OUTBOX_CLI,
                "schedule",
                "--to", MANAN,
                "--body", message,
                "--at", "now",
                "--source", "destination-aware",
                "--created-by", "destination-daemon",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        log.info("Notification sent via outbox")
    except Exception as e:
        log.error(f"Notification failed: {e}")


def get_destination_coords(ha_url, ha_token):
    """Get destination coordinates from Tesla route tracker."""
    try:
        req = urllib.request.Request(
            f"{ha_url}/api/states/device_tracker.maha_tesla_route",
            headers={"Authorization": f"Bearer {ha_token}"},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        attrs = data.get("attributes", {})
        lat = attrs.get("latitude")
        lng = attrs.get("longitude")
        if lat and lng:
            return float(lat), float(lng)
    except Exception as e:
        log.warning(f"Could not get destination coords: {e}")
    return None, None


def handle_destination(destination, ha_url=None, ha_token=None):
    """Full pipeline: gather context → compose → send."""
    log.info(f"Destination set: {destination}")

    # Get destination coordinates for location-biased search
    lat, lng = None, None
    if ha_url and ha_token:
        lat, lng = get_destination_coords(ha_url, ha_token)
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
    send_notification(message)


def main():
    ha_url, ha_token = load_ha_config()
    log.info(f"Starting destination daemon, watching {ENTITY_ID}")

    # Check current state on startup — if a destination is already active, handle it
    current = get_current_state(ha_url, ha_token)
    log.info(f"Current destination state: {current}")
    if current not in ("unknown", "unavailable", ""):
        log.info(f"Destination already active on startup, processing: {current}")
        handle_destination(current, ha_url=ha_url, ha_token=ha_token)

    while True:
        try:
            log.info("Connecting to HA event stream...")
            for old_state, new_state in subscribe_sse(ha_url, ha_token):
                if old_state == "unknown" and new_state not in ("unknown", "unavailable", ""):
                    handle_destination(new_state, ha_url=ha_url, ha_token=ha_token)
                elif new_state == "unknown" and old_state not in ("unknown", "unavailable", ""):
                    log.info("Navigation ended, destination cleared")

        except KeyboardInterrupt:
            log.info("Shutting down")
            break
        except Exception as e:
            log.error(f"SSE connection lost: {e}")
            log.info("Reconnecting in 30s...")
            time.sleep(30)


if __name__ == "__main__":
    main()
