#!/usr/bin/env python3
"""
Deterministic flight monitor daemon.

Polls track_flight.py on a fixed interval, detects state changes
(landed, delayed, diverted, gate change), and sends iMessage notifications
via the outbox. No LLM in the loop.

State is persisted to a JSON file so the monitor survives restarts.

Usage:
  flight_monitor.py                          # Run with default state file
  flight_monitor.py /path/to/state.json      # Run with custom state file
  flight_monitor.py --once                   # Single poll cycle (for testing)
"""

import sys
import os
import json
import time
import logging
from logging.handlers import RotatingFileHandler
import urllib.parse
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

# Airport -> IANA timezone mapping
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


def format_local_time(utc_str, dest_iata):
    """Convert UTC time string to local time at destination airport."""
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
from pathlib import Path

# Import track_flight from the same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from track_flight import track_flight

# Import outbox for message delivery
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "outbox"))
from outbox import OutboxDB

# --- Config ---

DEFAULT_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
POLL_ACTIVE_SECONDS = 900       # 15 minutes when any flight is within monitoring window
POLL_IDLE_SECONDS = 1800        # 30 minutes when all flights are far out
POLL_ARRIVAL_SECONDS = 300      # 5 minutes when a flight is approaching landing (< 45 min out)
MONITORING_WINDOW_HOURS = 3     # Start active polling this many hours before departure
UBER_BASE = "https://m.uber.com/ul/?action=setPickup&pickup=my_location"
LOG_FILE = os.path.expanduser("~/Library/Logs/spratt/flight-monitor.log")
OWNER_PHONE = "+1XXXXXXXXXX"  # Replace with your phone number

# Known airport rideshare pickup instructions
AIRPORT_PICKUP = {
    "DCA": "Follow signs to Ground Transportation. Rideshare pickup is at the garage level between Terminals A and B.",
    "SEA": "Follow signs to the parking garage. Rideshare pickup is on the 3rd floor of the garage, accessible from any terminal.",
    "EWR": "Follow signs to Ground Transportation. Rideshare pickup varies by terminal — check the Uber app for your exact pin.",
    "JFK": "Follow signs to Ground Transportation. Rideshare pickup is at the terminal arrivals level curb.",
    "IAD": "Take the AeroTrain to the parking garage. Rideshare pickup is at the Ground Transportation area.",
    "LGA": "Follow signs to Ground Transportation. Rideshare pickup is at the arrivals level curb.",
}

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [flight-monitor] %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# --- Message Delivery via Outbox ---

_outbox = None

def get_outbox():
    global _outbox
    if _outbox is None:
        _outbox = OutboxDB()
    return _outbox


# --- Notification Templates ---

def uber_link(address=None, lat=None, lng=None):
    """Build Uber deep link. With address/coords -> specific destination. Without -> just open Uber."""
    if address and lat and lng:
        encoded = urllib.parse.quote(address)
        return f"{UBER_BASE}&dropoff[formatted_address]={encoded}&dropoff[latitude]={lat}&dropoff[longitude]={lng}"
    # No destination — just open Uber with pickup at current location
    return UBER_BASE


def make_landing_message(flight_id, flight, result):
    """Build landing notification text."""
    dest = result.get("destination", {}) if result else {}
    terminal = dest.get("terminal") or "?"
    gate = dest.get("gate") or "?"
    baggage = dest.get("baggage")
    dest_iata = dest.get("iata") or "?"
    label = flight.get("label", flight_id)

    lines = [f"{label} has landed at {dest_iata}!"]
    lines.append(f"Terminal {terminal}, Gate {gate}")
    if baggage:
        lines.append(f"Baggage: belt {baggage}")

    # Rideshare pickup directions
    pickup_info = AIRPORT_PICKUP.get(dest_iata)
    if pickup_info:
        lines.append(f"Rideshare pickup: {pickup_info}")

    # Uber link — with destination if known, otherwise just open Uber
    hotel = flight.get("hotel_address")
    if hotel and flight.get("hotel_lat") and flight.get("hotel_lng"):
        lines.append(f"Uber to {hotel.split(',')[0]}: {uber_link(hotel, flight['hotel_lat'], flight['hotel_lng'])}")
    else:
        lines.append(f"Open Uber: {uber_link()}")

    return "\n".join(lines)


def make_delay_message(flight_id, flight, result, delay_mins):
    label = flight.get("label", flight_id)
    eta_raw = result.get("times", {}).get("estimated_arrival") or "unknown"
    dest_iata = result.get("destination", {}).get("iata", "")
    eta = format_local_time(eta_raw, dest_iata)
    return f"\u2708\ufe0f {label} is delayed ~{delay_mins} min. New ETA: {eta}"


def make_gate_change_message(flight_id, flight, result, old_gate, new_gate):
    label = flight.get("label", flight_id)
    dest_iata = result.get("destination", {}).get("iata", "?")
    return f"{label}: gate changed at {dest_iata} from {old_gate} to {new_gate}"


def make_diversion_message(flight_id, flight, result):
    label = flight.get("label", flight_id)
    status = result.get("status", "unknown") if result else "unknown"
    return f"ALERT: {label} \u2014 {status}. Check immediately."


# --- State Management ---

def load_state(state_file, retries=3, delay=2):
    for attempt in range(retries):
        try:
            with open(state_file) as f:
                return json.load(f)
        except FileNotFoundError:
            log.warning(f"State file not found: {state_file}")
            return None
        except (PermissionError, OSError) as e:
            if attempt < retries - 1:
                log.warning(f"State file access error (attempt {attempt+1}/{retries}): {e}")
                time.sleep(delay)
            else:
                log.error(f"State file access failed after {retries} attempts: {e}")
                return None


def save_state(state_file, state, retries=3, delay=2):
    tmp = state_file + ".tmp"
    for attempt in range(retries):
        try:
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, state_file)
            return
        except (PermissionError, OSError) as e:
            if attempt < retries - 1:
                log.warning(f"State file write error (attempt {attempt+1}/{retries}): {e}")
                time.sleep(delay)
            else:
                log.error(f"State file write failed after {retries} attempts: {e}")
                raise


# --- Notification Dispatch ---

def notify(flight_id, flight, text):
    """Write notification to the outbox for delivery by sender.py."""
    db = get_outbox()
    chat = flight.get("notify_chat")
    also = flight.get("notify_also", [])
    source = f"flight:{flight_id}"

    if chat:
        # Group chat exists — send there only, no individual messages
        db.schedule(recipient=chat, body=text, send_at="now", source=source, created_by="flight-monitor", priority=10)
        log.info(f"Queued to outbox: {source} -> chat {chat}")
    else:
        # No group chat — send to individual recipients
        for target in (also or []):
            db.schedule(recipient=target, body=text, send_at="now", source=source, created_by="flight-monitor", priority=10)
            log.info(f"Queued to outbox: {source} -> {target}")


# --- Departure Window Check ---

EXPIRE_HOURS = 12  # Stop polling this many hours after scheduled departure

def is_in_monitoring_window(flight):
    """Check if a flight is within the active monitoring window.
    Returns True if within MONITORING_WINDOW_HOURS before departure.
    Returns True if was_ever_found and not yet expired (in the air).
    Returns False if expired (departure + EXPIRE_HOURS has passed).
    Returns True if depart_after is not set (always monitor).
    """
    depart_after = flight.get("depart_after")
    if not depart_after:
        return True

    try:
        dep_time = datetime.fromisoformat(depart_after.replace("Z", "+00:00"))
        if dep_time.tzinfo is None:
            dep_time = dep_time.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)

        # Expired — stop polling regardless
        if now > dep_time + timedelta(hours=EXPIRE_HOURS):
            return False

        # Was found (in the air) — keep polling until expired
        if flight.get("was_ever_found"):
            return True

        # Not yet found — only poll within the monitoring window
        window_start = dep_time - timedelta(hours=MONITORING_WINDOW_HOURS)
        return now >= window_start
    except Exception:
        return True


# --- Core Poll Logic ---

def estimate_delay_minutes(result):
    """Get delay in minutes. Uses API delay field, falls back to time comparison."""
    delay = result.get("delay", {})
    arr_delay = delay.get("arrival_minutes")
    if arr_delay is not None and arr_delay > 0:
        return int(arr_delay)
    dep_delay = delay.get("departure_minutes")
    if dep_delay is not None and dep_delay > 0:
        return int(dep_delay)
    return 0


def poll_flight(flight_id, flight):
    """Poll one flight and return list of (event_type, message) tuples."""
    events = []
    result = track_flight(flight_id)
    now = datetime.now(timezone.utc).isoformat()
    flight["last_checked"] = now

    # --- Not found ---
    if result.get("error") == "not_found":
        flight["consecutive_not_found"] = flight.get("consecutive_not_found", 0) + 1
        log.info(f"{flight_id}: not found (count={flight['consecutive_not_found']}, was_found={flight.get('was_ever_found')})")

        # Inferred landing: was airborne, now disappeared
        if flight.get("was_ever_found") and flight["consecutive_not_found"] >= 2 and not flight.get("notified_landed"):
            flight["notified_landed"] = True
            flight["last_status"] = "landed (inferred)"
            msg = make_landing_message(flight_id, flight, flight.get("last_result"))
            events.append(("landed", msg))
            log.info(f"{flight_id}: LANDED (inferred from disappearance)")

        # Alert: flight not found 1 hour after scheduled departure
        depart_after = flight.get("depart_after")
        if (not flight.get("was_ever_found") and not flight.get("notified_not_found")
                and depart_after and flight["consecutive_not_found"] >= 5):
            try:
                dep_time = datetime.fromisoformat(depart_after.replace("Z", "+00:00"))
                if dep_time.tzinfo is None:
                    dep_time = dep_time.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > dep_time + timedelta(hours=1):
                    flight["notified_not_found"] = True
                    label = flight.get("label", flight_id)
                    msg = f"Warning: Flight {flight_id} ({label}) not found on FlightAware — scheduled departure was {depart_after}. Gate and landing alerts will not work. Check manually."
                    events.append(("not_found_alert", msg))
                    log.warning(f"{flight_id}: NOT FOUND ALERT — past departure + 1h, never seen")
            except Exception:
                pass

        return events

    # --- API error ---
    if result.get("error"):
        log.warning(f"{flight_id}: API error: {result['error']}")
        return events

    # Flight found — reset not-found counter, mark as seen
    flight["consecutive_not_found"] = 0
    flight["was_ever_found"] = True
    flight["last_result"] = result

    status_text = (result.get("status") or "").lower()
    position = result.get("position", {})
    dest = result.get("destination", {})

    # --- Landed detection ---
    if not flight.get("notified_landed"):
        landed = False

        if any(kw in status_text for kw in ["landed", "arrived"]):
            landed = True

        if position.get("on_ground") and position.get("altitude", 99999) < 500:
            if flight.get("last_status") in ("airborne", "descending", "en route"):
                landed = True

        if landed:
            flight["notified_landed"] = True
            flight["last_status"] = "landed"
            msg = make_landing_message(flight_id, flight, result)
            events.append(("landed", msg))
            log.info(f"{flight_id}: LANDED")

    # --- Delay detection ---
    delay_mins = estimate_delay_minutes(result)
    prev_delay = flight.get("delay_minutes_notified", 0)
    if delay_mins >= 15 and delay_mins > prev_delay + 10:
        flight["delay_minutes_notified"] = delay_mins
        msg = make_delay_message(flight_id, flight, result, delay_mins)
        events.append(("delay", msg))
        log.info(f"{flight_id}: DELAYED {delay_mins}min")

    # --- Diversion / cancellation ---
    if any(kw in status_text for kw in ["diverted", "cancelled", "canceled"]):
        if not flight.get("notified_diversion"):
            flight["notified_diversion"] = True
            msg = make_diversion_message(flight_id, flight, result)
            events.append(("diversion", msg))
            log.info(f"{flight_id}: DIVERTED/CANCELLED")

    # --- Gate change ---
    new_gate = dest.get("gate")
    old_gate = flight.get("last_gate")
    if new_gate and old_gate and new_gate != old_gate:
        msg = make_gate_change_message(flight_id, flight, result, old_gate, new_gate)
        events.append(("gate_change", msg))
        log.info(f"{flight_id}: Gate changed {old_gate} -> {new_gate}")
    if new_gate:
        flight["last_gate"] = new_gate

    # --- Update status tracking ---
    if not flight.get("notified_landed"):
        if position.get("altitude", 0) > 5000 or "en route" in status_text:
            flight["last_status"] = "airborne"
        elif position.get("altitude", 0) > 500:
            flight["last_status"] = "descending"
        elif "scheduled" in status_text or "estimated" in status_text:
            flight["last_status"] = "pre-departure"
        else:
            flight["last_status"] = status_text[:30] or "unknown"

    return events


# --- Main Loop ---

def run_once(state, state_file):
    """Single poll cycle. Returns (any_active, any_in_window, any_approaching)."""
    flights = state.get("flights", {})
    any_active = False
    any_in_window = False

    for flight_id, flight in flights.items():
        if flight.get("notified_landed"):
            continue

        any_active = True

        if not is_in_monitoring_window(flight):
            log.debug(f"{flight_id}: outside monitoring window, skipping")
            continue

        any_in_window = True

        # Skip mid-cruise polls to save API calls.
        last_result = flight.get("last_result")
        if (flight.get("last_status") == "airborne" and last_result
                and not flight.get("_approaching")):
            eta = (last_result.get("times") or {}).get("estimated_arrival")
            if eta:
                try:
                    eta_dt = datetime.fromisoformat(eta.replace("Z", "+00:00"))
                    if eta_dt.tzinfo is None:
                        eta_dt = eta_dt.replace(tzinfo=timezone.utc)
                    mins_to_arrival = (eta_dt - datetime.now(timezone.utc)).total_seconds() / 60
                    if mins_to_arrival > 45:
                        log.info(f"{flight_id}: cruising, {int(mins_to_arrival)} min to arrival — skipping poll")
                        continue
                    else:
                        flight["_approaching"] = True
                except Exception:
                    pass

        try:
            events = poll_flight(flight_id, flight)
            for event_type, message in events:
                if event_type == "not_found_alert":
                    db = get_outbox()
                    db.schedule(recipient=OWNER_PHONE, body=message, send_at="now",
                               source=f"system:flight-not-found:{flight_id}",
                               created_by="flight-monitor", priority=20)
                    log.info(f"Not-found alert sent for {flight_id}")
                else:
                    notify(flight_id, flight, message)
        except Exception as e:
            log.error(f"{flight_id}: poll error: {e}", exc_info=True)

    # Check if any flight is approaching landing (< 45 min)
    any_approaching = any(f.get("_approaching") for f in flights.values() if not f.get("notified_landed"))

    save_state(state_file, state)
    return any_active, any_in_window, any_approaching


def main():
    single_run = "--once" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    state_file = args[0] if args else DEFAULT_STATE_FILE

    log.info(f"Starting flight monitor. State: {state_file}, single_run={single_run}")

    state = load_state(state_file)
    if not state:
        log.error("No state file found. Write a state file first, then start the monitor.")
        sys.exit(1)

    if single_run:
        any_active, _, _ = run_once(state, state_file)
        log.info(f"Single run complete. Active flights: {any_active}")
        sys.exit(0 if any_active else 2)

    while True:
        # Re-read state file each cycle to pick up flight number changes
        fresh_state = load_state(state_file)
        if fresh_state:
            for fid, fdata in fresh_state.get("flights", {}).items():
                if fid not in state.get("flights", {}):
                    state.setdefault("flights", {})[fid] = fdata
                    log.info(f"New flight added: {fid}")
            current_ids = set(fresh_state.get("flights", {}).keys())
            for fid in list(state.get("flights", {}).keys()):
                if fid not in current_ids:
                    log.info(f"Flight removed: {fid}")
                    del state["flights"][fid]

        any_active, any_in_window, any_approaching = run_once(state, state_file)
        if not any_active:
            log.info("All flights landed. Monitor exiting.")
            break

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
