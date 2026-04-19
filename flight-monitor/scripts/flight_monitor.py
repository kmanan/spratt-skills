#!/usr/bin/env python3
"""
Flight monitor daemon (DB-native).

Reads active flights directly from ~/.config/spratt/db/trips.sqlite each poll cycle.
Writes runtime state (notified_landed, was_ever_found, last_checked, gate, etc.) back
to the same flights row. No sidecar state file.

Event messages are written to the outbox SQLite; recipients are computed at send
time by joining the trip's group_chat_guid and travelers table.

Usage:
  flight_monitor.py          # Run the daemon
  flight_monitor.py --once   # Single poll cycle (for testing)
"""

import sys
import os
import json
import time
import logging
import sqlite3
import urllib.parse
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

# Import track_flight from the same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from track_flight import track_flight

# Import outbox for message delivery
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "outbox"))
from outbox import OutboxDB

# ─── Config ───

TRIPS_DB = os.path.expanduser("~/.config/spratt/db/trips.sqlite")
LOG_FILE = os.path.expanduser("~/Library/Logs/spratt/flight-monitor.log")

POLL_ACTIVE_SECONDS = 900       # 15 min when any flight is within monitoring window
POLL_IDLE_SECONDS = 1800        # 30 min when all flights are far out
POLL_ARRIVAL_SECONDS = 300      # 5 min when a flight is approaching landing (< 45 min out)
MONITORING_WINDOW_HOURS = 3     # Start active polling this many hours before departure
EXPIRE_HOURS = 12               # Stop polling this many hours after scheduled departure
UBER_BASE = "https://m.uber.com/ul/?action=setPickup&pickup=my_location"

# Airport → IANA timezone mapping (for rendering ETAs in local time)
AIRPORT_TZ = {
    "SEA": "America/Los_Angeles", "SFO": "America/Los_Angeles", "LAX": "America/Los_Angeles",
    "PDX": "America/Los_Angeles", "SAN": "America/Los_Angeles", "SMF": "America/Los_Angeles",
    "JFK": "America/New_York", "LGA": "America/New_York", "EWR": "America/New_York",
    "DCA": "America/New_York", "IAD": "America/New_York", "BWI": "America/New_York",
    "BOS": "America/New_York", "PHL": "America/New_York", "ATL": "America/New_York",
    "MIA": "America/New_York", "FLL": "America/New_York", "MCO": "America/New_York",
    "ORD": "America/Chicago", "MDW": "America/Chicago", "DFW": "America/Chicago",
    "IAH": "America/Chicago", "AUS": "America/Chicago", "MSP": "America/Chicago",
    "DEN": "America/Denver", "PHX": "America/Phoenix", "SLC": "America/Denver",
    "HNL": "Pacific/Honolulu",
    "BOM": "Asia/Kolkata", "DEL": "Asia/Kolkata", "BLR": "Asia/Kolkata",
    "LHR": "Europe/London", "CDG": "Europe/Paris", "FRA": "Europe/Berlin",
    "NRT": "Asia/Tokyo", "HND": "Asia/Tokyo", "ICN": "Asia/Seoul",
}

# Rideshare pickup instructions by airport
AIRPORT_PICKUP = {
    "DCA": "Follow signs to Ground Transportation. Rideshare pickup is at the garage level between Terminals A and B.",
    "SEA": "Follow signs to the parking garage. Rideshare pickup is on the 3rd floor of the garage, accessible from any terminal.",
    "EWR": "Follow signs to Ground Transportation. Rideshare pickup varies by terminal — check the Uber app for your exact pin.",
    "JFK": "Follow signs to Ground Transportation. Rideshare pickup is at the terminal arrivals level curb.",
    "IAD": "Take the AeroTrain to the parking garage. Rideshare pickup is at the Ground Transportation area.",
    "LGA": "Follow signs to Ground Transportation. Rideshare pickup is at the arrivals level curb.",
}

# ─── Logging ───

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [flight-monitor] %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ─── DB helpers ───


def db_conn():
    """Short-lived connection to trips.sqlite with sensible concurrency defaults."""
    if not os.path.exists(TRIPS_DB):
        raise RuntimeError(f"trips DB missing: {TRIPS_DB}")
    conn = sqlite3.connect(TRIPS_DB, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def get_active_flights():
    """Return flights that should be considered for monitoring.

    Filter: trip is upcoming/active, flight is scheduled, landing not yet notified.
    """
    with db_conn() as conn:
        rows = conn.execute(
            """SELECT f.*, t.group_chat_guid, t.name AS trip_name, t.status AS trip_status
               FROM flights f
               JOIN trips t ON f.trip_id = t.id
               WHERE t.status IN ('upcoming', 'active')
                 AND f.status = 'scheduled'
                 AND COALESCE(f.notified_landed, 0) = 0
               ORDER BY f.departs_utc"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_hotel_address_for_trip(trip_id):
    with db_conn() as conn:
        row = conn.execute(
            "SELECT address FROM hotels WHERE trip_id = ? LIMIT 1",
            (trip_id,),
        ).fetchone()
    return row["address"] if row and row["address"] else None


def get_primary_recipient(trip_id, group_chat_guid):
    """Return the primary recipient string for a trip.

    Group chat GUID on the trip takes precedence. Otherwise the first traveler's
    phone (solo-trip convention: trip person gets the alert). Returns None if
    neither is available (caller logs and skips).
    """
    if group_chat_guid:
        return group_chat_guid
    with db_conn() as conn:
        row = conn.execute(
            """SELECT phone FROM travelers
               WHERE trip_id = ? AND phone IS NOT NULL
               ORDER BY id LIMIT 1""",
            (trip_id,),
        ).fetchone()
    return row["phone"] if row and row["phone"] else None


def update_flight(flight_id, **fields):
    """UPDATE flights row with runtime state. flight_id is the numeric PK."""
    if not fields:
        return
    cols_sql = ", ".join(f"{k} = ?" for k in fields.keys())
    vals = list(fields.values()) + [flight_id]
    with db_conn() as conn:
        conn.execute(
            f"UPDATE flights SET {cols_sql}, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            vals,
        )
        conn.commit()


# ─── Outbox helper ───

_outbox = None


def get_outbox():
    global _outbox
    if _outbox is None:
        _outbox = OutboxDB()
    return _outbox


# ─── Formatting utils ───


def format_local_time(utc_str, dest_iata):
    """Convert UTC ISO string to local time at destination airport."""
    if not utc_str or utc_str == "unknown":
        return "unknown"
    try:
        dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
        tz_name = AIRPORT_TZ.get(dest_iata)
        if tz_name:
            local = dt.astimezone(ZoneInfo(tz_name))
            tz_abbr = local.strftime("%Z")
            return local.strftime(f"%-I:%M %p {tz_abbr}")
        return dt.strftime("%-I:%M %p UTC")
    except Exception:
        return utc_str


def uber_link(address=None):
    if address:
        encoded = urllib.parse.quote(address)
        return f"{UBER_BASE}&dropoff[formatted_address]={encoded}"
    return UBER_BASE


def flight_label(flight):
    """Build 'Leo EWR to SEA' style label from flight row."""
    traveler = flight.get("traveler") or "Flight"
    route = flight.get("route") or ""
    if "→" in route:
        parts = route.split("→")
        return f"{traveler} {parts[0].strip()} to {parts[1].strip()}"
    return f"{traveler} {route}".strip()


# ─── Message templates ───


def _pick_destination(flight, result):
    """Return the destination dict, preferring live result, falling back to cached blob."""
    dest = (result or {}).get("destination") if result else None
    if dest:
        return dest
    blob = flight.get("last_result_json")
    if blob:
        try:
            return (json.loads(blob) or {}).get("destination", {})
        except Exception:
            return {}
    return {}


def make_landing_message(flight, result, hotel_address):
    dest = _pick_destination(flight, result)
    terminal = dest.get("terminal") or "?"
    gate = dest.get("gate") or flight.get("gate") or "?"
    baggage = dest.get("baggage")
    dest_iata = dest.get("iata") or "?"
    label = flight_label(flight)

    lines = [f"{label} has landed at {dest_iata}!"]
    lines.append(f"Terminal {terminal}, Gate {gate}")
    if baggage:
        lines.append(f"Baggage: belt {baggage}")

    pickup_info = AIRPORT_PICKUP.get(dest_iata)
    if pickup_info:
        lines.append(f"Rideshare pickup: {pickup_info}")

    if hotel_address:
        lines.append(f"Uber to {hotel_address.split(',')[0]}: {uber_link(hotel_address)}")
    else:
        lines.append(f"Open Uber: {uber_link()}")

    return "\n".join(lines)


def make_delay_message(flight, result, delay_mins):
    label = flight_label(flight)
    eta_raw = (result.get("times") or {}).get("estimated_arrival") or "unknown"
    dest_iata = (result.get("destination") or {}).get("iata", "")
    eta = format_local_time(eta_raw, dest_iata)
    return f"✈️ {label} is delayed ~{delay_mins} min. New ETA: {eta}"


def make_gate_change_message(flight, result, old_gate, new_gate):
    label = flight_label(flight)
    dest_iata = (result.get("destination") or {}).get("iata", "?")
    return f"{label}: gate changed at {dest_iata} from {old_gate} to {new_gate}"


def make_diversion_message(flight, result):
    label = flight_label(flight)
    status = (result or {}).get("status", "unknown")
    return f"ALERT: {label} — {status}. Check immediately."


# ─── Notify: write event message to outbox ───


def notify(flight, text):
    """Queue a flight event message to the outbox for the trip's primary recipient."""
    recipient = get_primary_recipient(flight["trip_id"], flight.get("group_chat_guid"))
    source = f"flight:{flight['flight_number']}"
    if not recipient:
        log.error(f"{flight['flight_number']}: no recipient for trip {flight['trip_id']} — message dropped")
        get_outbox().schedule(
            recipient="+13157082088",
            body=f"ALERT: Flight {flight['flight_number']} (trip {flight['trip_id']}) has no notification recipient configured. Flight alerts are being dropped. Fix with: trip-db.py update-trip --id {flight['trip_id']} --group-chat <recipient>",
            send_at="now",
            source=f"system:no-recipient:{flight['flight_number']}",
            created_by="flight-monitor",
            priority=20,
        )
        return
    get_outbox().schedule(
        recipient=recipient,
        body=text,
        send_at="now",
        source=source,
        created_by="flight-monitor",
        priority=10,
        trip_id=flight["trip_id"],
    )
    log.info(f"Queued to outbox: {source} → {recipient}")


def system_alert(flight, text, tag):
    """Send an operational alert to Manan directly (not routed via trip recipients)."""
    get_outbox().schedule(
        recipient="+13157082088",
        body=text,
        send_at="now",
        source=f"system:{tag}:{flight['flight_number']}",
        created_by="flight-monitor",
        priority=20,
    )
    log.info(f"System alert sent to Manan for {flight['flight_number']} ({tag})")


# ─── Monitoring window ───


def is_in_monitoring_window(flight):
    """Should this flight be polled right now?"""
    depart_after = flight.get("departs_utc")
    if not depart_after:
        log.warning(f"{flight['flight_number']}: no departs_utc — refusing to poll (would track wrong flight)")
        return False
    try:
        dep_time = datetime.fromisoformat(depart_after.replace("Z", "+00:00"))
        if dep_time.tzinfo is None:
            dep_time = dep_time.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)

        if now > dep_time + timedelta(hours=EXPIRE_HOURS):
            return False
        if flight.get("was_ever_found"):
            return True
        window_start = dep_time - timedelta(hours=MONITORING_WINDOW_HOURS)
        return now >= window_start
    except Exception:
        return False


# ─── Poll logic ───


def estimate_delay_minutes(result):
    """Arrival delay preferred, departure delay as fallback. Zero if neither > 0."""
    delay = result.get("delay") or {}
    arr = delay.get("arrival_minutes")
    if arr is not None and arr > 0:
        return int(arr)
    dep = delay.get("departure_minutes")
    if dep is not None and dep > 0:
        return int(dep)
    return 0


def poll_flight(flight, approaching_map):
    """Poll one flight. Returns (updates_dict, events_list).

    updates_dict: fields to UPDATE on the flights row.
    events_list: (event_type, message) tuples to dispatch.
    """
    updates = {"last_checked": datetime.now(timezone.utc).isoformat()}
    events = []
    flight_num = flight["flight_number"]
    trip_id = flight["trip_id"]

    result = track_flight(flight_num)

    # ─── Not found ───
    if result.get("error") == "not_found":
        consecutive = (flight.get("consecutive_not_found") or 0) + 1
        updates["consecutive_not_found"] = consecutive
        log.info(f"{flight_num}: not found (count={consecutive}, was_found={flight.get('was_ever_found')})")

        # Inferred landing: was airborne, now disappeared from radar
        if flight.get("was_ever_found") and consecutive >= 2 and not flight.get("notified_landed"):
            updates["notified_landed"] = 1
            updates["last_status"] = "landed (inferred)"
            hotel_address = get_hotel_address_for_trip(trip_id)
            msg = make_landing_message(flight, None, hotel_address)
            events.append(("landed", msg))
            log.info(f"{flight_num}: LANDED (inferred from disappearance)")

        # Never seen + past departure + several failed polls → operational alert
        depart_after = flight.get("departs_utc")
        if (not flight.get("was_ever_found") and not flight.get("notified_not_found")
                and depart_after and consecutive >= 5):
            try:
                dep_time = datetime.fromisoformat(depart_after.replace("Z", "+00:00"))
                if dep_time.tzinfo is None:
                    dep_time = dep_time.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > dep_time + timedelta(hours=1):
                    updates["notified_not_found"] = 1
                    label = flight_label(flight)
                    msg = (
                        f"⚠️ Flight {flight_num} ({label}) not found on FlightAware — "
                        f"scheduled departure was {depart_after}. Gate and landing alerts will "
                        f"not work. Check manually."
                    )
                    events.append(("not_found_alert", msg))
                    log.warning(f"{flight_num}: NOT FOUND ALERT")
            except Exception:
                pass

        return updates, events

    # ─── API error ───
    if result.get("error"):
        log.warning(f"{flight_num}: API error: {result['error']}")
        return updates, events

    # ─── Flight found ───
    updates["consecutive_not_found"] = 0
    updates["was_ever_found"] = 1
    updates["last_result_json"] = json.dumps(result)

    status_text = (result.get("status") or "").lower()
    position = result.get("position") or {}
    dest = result.get("destination") or {}

    # Landed detection (explicit)
    if not flight.get("notified_landed"):
        landed = False
        if any(kw in status_text for kw in ["landed", "arrived"]):
            landed = True
        if position.get("on_ground") and (position.get("altitude") or 99999) < 500:
            if flight.get("last_status") in ("airborne", "descending", "en route"):
                landed = True
        if landed:
            updates["notified_landed"] = 1
            updates["last_status"] = "landed"
            hotel_address = get_hotel_address_for_trip(trip_id)
            msg = make_landing_message(flight, result, hotel_address)
            events.append(("landed", msg))
            log.info(f"{flight_num}: LANDED")

    # Delay detection
    delay_mins = estimate_delay_minutes(result)
    prev_delay = flight.get("delay_minutes_notified") or 0
    if delay_mins >= 15 and delay_mins > prev_delay + 10:
        updates["delay_minutes_notified"] = delay_mins
        msg = make_delay_message(flight, result, delay_mins)
        events.append(("delay", msg))
        log.info(f"{flight_num}: DELAYED {delay_mins}min")

    # Diversion / cancellation
    if any(kw in status_text for kw in ["diverted", "cancelled", "canceled"]):
        if not flight.get("notified_diversion"):
            updates["notified_diversion"] = 1
            msg = make_diversion_message(flight, result)
            events.append(("diversion", msg))
            log.info(f"{flight_num}: DIVERTED/CANCELLED")

    # Gate change
    new_gate = dest.get("gate")
    old_gate = flight.get("gate")
    if new_gate and old_gate and new_gate != old_gate:
        msg = make_gate_change_message(flight, result, old_gate, new_gate)
        events.append(("gate_change", msg))
        log.info(f"{flight_num}: Gate changed {old_gate} -> {new_gate}")
    if new_gate:
        updates["gate"] = new_gate

    # Status progression (not landed)
    if not updates.get("notified_landed"):
        alt = position.get("altitude") or 0
        if alt > 5000 or "en route" in status_text:
            updates["last_status"] = "airborne"
        elif alt > 500:
            updates["last_status"] = "descending"
        elif "scheduled" in status_text or "estimated" in status_text:
            updates["last_status"] = "pre-departure"
        else:
            updates["last_status"] = (status_text[:30] or "unknown")

    # Approaching flag (transient, in-memory only)
    try:
        eta = (result.get("times") or {}).get("estimated_arrival")
        if eta:
            eta_dt = datetime.fromisoformat(eta.replace("Z", "+00:00"))
            if eta_dt.tzinfo is None:
                eta_dt = eta_dt.replace(tzinfo=timezone.utc)
            mins_to_arrival = (eta_dt - datetime.now(timezone.utc)).total_seconds() / 60
            if mins_to_arrival <= 45:
                approaching_map[flight_num] = True
    except Exception:
        pass

    return updates, events


# ─── Main poll cycle ───


def run_once(approaching_map):
    """Single poll cycle. Returns (any_active, any_in_window, any_approaching)."""
    try:
        flights = get_active_flights()
    except Exception as e:
        log.error(f"Failed to query active flights: {e}", exc_info=True)
        return False, False, False

    any_active = bool(flights)
    any_in_window = False

    for flight in flights:
        flight_num = flight["flight_number"]

        if not is_in_monitoring_window(flight):
            continue
        any_in_window = True

        # Skip mid-cruise polls to save API calls: if airborne with cached ETA > 45 min
        # away and we haven't seen it approach yet, keep cruising.
        if (flight.get("last_status") == "airborne"
                and flight.get("last_result_json")
                and not approaching_map.get(flight_num)):
            try:
                cached = json.loads(flight["last_result_json"])
                eta = (cached.get("times") or {}).get("estimated_arrival")
                if eta:
                    eta_dt = datetime.fromisoformat(eta.replace("Z", "+00:00"))
                    if eta_dt.tzinfo is None:
                        eta_dt = eta_dt.replace(tzinfo=timezone.utc)
                    mins_to_arrival = (eta_dt - datetime.now(timezone.utc)).total_seconds() / 60
                    if mins_to_arrival > 45:
                        log.info(f"{flight_num}: cruising, {int(mins_to_arrival)} min to arrival — skipping poll")
                        continue
                    else:
                        approaching_map[flight_num] = True
            except Exception:
                pass

        try:
            updates, events = poll_flight(flight, approaching_map)
            # Persist runtime updates before dispatching messages (so a crash
            # between message and update doesn't double-fire next cycle).
            update_flight(flight["id"], **updates)
            # Reflect updates into the in-memory dict so message builders see fresh values.
            merged = {**flight, **updates}
            for event_type, message in events:
                if event_type == "not_found_alert":
                    system_alert(merged, message, "flight-not-found")
                else:
                    notify(merged, message)
        except Exception as e:
            log.error(f"{flight_num}: poll error: {e}", exc_info=True)

    any_approaching = any(approaching_map.get(f["flight_number"]) for f in flights)
    return any_active, any_in_window, any_approaching


def main():
    single_run = "--once" in sys.argv
    log.info(f"Starting flight monitor (DB-native). TRIPS_DB={TRIPS_DB}, single_run={single_run}")

    approaching_map = {}  # transient: flight_number → True once < 45 min to arrival

    if single_run:
        any_active, _, _ = run_once(approaching_map)
        log.info(f"Single run complete. Active flights: {any_active}")
        sys.exit(0 if any_active else 2)

    while True:
        any_active, any_in_window, any_approaching = run_once(approaching_map)

        # Never exit: trips appear/disappear over time; the daemon stays up.
        if not any_active:
            log.info("No active flights in DB — next check in idle interval.")
            time.sleep(POLL_IDLE_SECONDS)
            continue

        if any_approaching:
            sleep_time = POLL_ARRIVAL_SECONDS
            log.info(f"Flight approaching — next poll in {sleep_time}s")
        elif any_in_window:
            sleep_time = POLL_ACTIVE_SECONDS
            log.info(f"Active window — next poll in {sleep_time}s")
        else:
            sleep_time = POLL_IDLE_SECONDS
            log.info(f"All flights outside monitoring window — next poll in {sleep_time}s")

        time.sleep(sleep_time)


if __name__ == "__main__":
    main()
