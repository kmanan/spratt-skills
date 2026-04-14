#!/usr/bin/env python3
"""
trip-flight-state.py — Derive flight monitor state.json from trips.sqlite.

Reads flights table + travelers table + trips table to build state.json entries.
Preserves runtime state (was_ever_found, notified_*, consecutive_not_found, etc.)
for flights that haven't changed. Only updates/adds/removes entries based on
current DB state.

Usage:
  trip-flight-state.py <trip-id>       Sync state.json for one trip's flights
  trip-flight-state.py --all           Sync for all upcoming/active trips
  trip-flight-state.py --dry-run <id>  Show what would change without writing

Exit codes:
  0 = success
  1 = error
"""

import sys
import os
import json
import sqlite3
import logging
from logging.handlers import RotatingFileHandler

# ─── Config ───

TRIPS_DB = os.path.expanduser("~/.config/spratt/trips/trips.sqlite")
STATE_FILE = os.path.expanduser("~/.config/spratt/infrastructure/flight-monitor/state.json")
LOG_FILE = os.path.expanduser("~/Library/Logs/spratt/trip-flight-state.log")
MANAN_PHONE = "Manan"  # resolved by outbox.py via contacts.sqlite


def require_db_file(path, name):
    """Fail loudly if a SQLite DB file doesn't exist where expected.
    Prevents silent split-brain from path resolution bugs.
    """
    if not os.path.exists(path):
        sys.stderr.write(
            f"\nFATAL: {name} database not found at:\n    {path}\n\n"
            f"Refusing to auto-create (prevents silent data loss if the path is wrong).\n\n"
        )
        sys.exit(1)

# ─── Logging ───

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [trip-flight-state] %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=2),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# Runtime fields that must be preserved across updates
RUNTIME_FIELDS = {
    "was_ever_found": False,
    "consecutive_not_found": 0,
    "notified_landed": False,
    "notified_delay": False,
    "notified_diversion": False,
    "delay_minutes_notified": 0,
    "last_status": "unknown",
    "last_gate": None,
    "last_checked": None,
    "last_result": None,
}


def load_state():
    """Load current state.json. Returns dict with 'flights' key."""
    if not os.path.exists(STATE_FILE):
        return {"flights": {}}
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        if "flights" not in data:
            data["flights"] = {}
        return data
    except (json.JSONDecodeError, IOError) as e:
        log.error(f"Failed to read state.json: {e}")
        return {"flights": {}}


def save_state(state, dry_run=False):
    """Write state.json atomically."""
    if dry_run:
        log.info("[DRY RUN] Would write state.json")
        return

    tmp_path = STATE_FILE + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, STATE_FILE)
    except IOError as e:
        log.error(f"Failed to write state.json: {e}")
        # Clean up temp file
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def build_flight_entry(conn, flight_row, trip_row, existing_entry):
    """Build a state.json flight entry from DB data.

    Preserves runtime fields from existing_entry if the flight hasn't changed.
    """
    flight_number = flight_row["flight_number"]
    traveler = flight_row["traveler"] or "Unknown"
    route = flight_row["route"] or ""

    # Build label: "Dad DCA to SEA"
    if "→" in route:
        parts = route.split("→")
        label = f"{traveler} {parts[0].strip()} to {parts[1].strip()}"
    else:
        label = f"{traveler} {route}"

    # Determine notify_chat: group chat GUID if set, else first traveler phone
    notify_chat = None
    if trip_row["group_chat_guid"]:
        # Use chat_guid: prefix for sender.py compatibility
        notify_chat = f"chat_guid:{trip_row['group_chat_guid']}"
    else:
        # Solo trip — use traveler's phone
        trav = conn.execute(
            "SELECT phone FROM travelers WHERE trip_id = ? AND name = ? AND phone IS NOT NULL",
            (flight_row["trip_id"], traveler),
        ).fetchone()
        if trav:
            notify_chat = trav["phone"]

    # notify_also: all traveler phones for this trip (always include Manan)
    travelers = conn.execute(
        "SELECT phone FROM travelers WHERE trip_id = ? AND phone IS NOT NULL",
        (flight_row["trip_id"],),
    ).fetchall()
    notify_also = list({t["phone"] for t in travelers})
    if MANAN_PHONE not in notify_also:
        notify_also.append(MANAN_PHONE)

    # Hotel info
    hotel = conn.execute(
        "SELECT address FROM hotels WHERE trip_id = ? LIMIT 1",
        (flight_row["trip_id"],),
    ).fetchone()
    hotel_address = hotel["address"] if hotel else None

    entry = {
        "label": label,
        "depart_after": flight_row["departs_utc"],
        "notify_chat": notify_chat,
        "notify_also": notify_also,
        "hotel_address": hotel_address,
        "hotel_lat": None,
        "hotel_lng": None,
    }

    # Preserve runtime state from existing entry if flight hasn't materially changed
    if existing_entry:
        data_changed = (
            existing_entry.get("depart_after") != flight_row["departs_utc"]
            or existing_entry.get("label", "").split(" ")[0] != traveler
        )
        if data_changed:
            log.info(f"Flight {flight_number} data changed — resetting runtime state")
            for field, default in RUNTIME_FIELDS.items():
                entry[field] = default
        else:
            for field, default in RUNTIME_FIELDS.items():
                entry[field] = existing_entry.get(field, default)
    else:
        # New flight — initialize runtime state
        for field, default in RUNTIME_FIELDS.items():
            entry[field] = default

    return entry


def sync_trip_flights(trip_id, dry_run=False):
    """Sync state.json entries for one trip's flights. Returns (added, updated, removed) counts."""
    require_db_file(TRIPS_DB, "trips")
    conn = sqlite3.connect(TRIPS_DB)
    conn.row_factory = sqlite3.Row

    trip = conn.execute(
        "SELECT id, name, group_chat_guid, timezone FROM trips WHERE id = ?",
        (trip_id,),
    ).fetchone()
    if not trip:
        log.error(f"Trip '{trip_id}' not found")
        conn.close()
        return -1, -1, -1

    flights = conn.execute(
        "SELECT id, trip_id, traveler, flight_number, route, departs_utc "
        "FROM flights WHERE trip_id = ? AND status = 'scheduled'",
        (trip_id,),
    ).fetchall()

    state = load_state()
    db_flight_numbers = set()
    added = 0
    updated = 0

    for f in flights:
        fn = f["flight_number"]
        db_flight_numbers.add(fn)
        existing = state["flights"].get(fn)
        new_entry = build_flight_entry(conn, f, trip, existing)

        if existing:
            # Check if anything changed
            if (existing.get("depart_after") != new_entry["depart_after"]
                    or existing.get("label") != new_entry["label"]
                    or existing.get("notify_chat") != new_entry["notify_chat"]):
                state["flights"][fn] = new_entry
                updated += 1
                log.info(f"Updated state.json entry for {fn}")
            else:
                # No data change — but update notify_also in case travelers changed
                existing["notify_also"] = new_entry["notify_also"]
                existing["hotel_address"] = new_entry["hotel_address"]
        else:
            state["flights"][fn] = new_entry
            added += 1
            log.info(f"Added state.json entry for {fn}")

    # Remove state.json entries for flights no longer scheduled in ANY active/upcoming trip.
    # This handles cancelled flights, renamed flights, and orphaned entries.
    # We check globally because a rename changes the flight_number in-place,
    # leaving the old entry in state.json with no DB record to match.
    removed = 0
    all_scheduled = conn.execute(
        "SELECT flight_number FROM flights f JOIN trips t ON f.trip_id = t.id "
        "WHERE f.status = 'scheduled' AND t.status IN ('upcoming', 'active')"
    ).fetchall()
    all_scheduled_fn = {r["flight_number"] for r in all_scheduled}

    for fn in list(state["flights"].keys()):
        if fn not in all_scheduled_fn:
            del state["flights"][fn]
            removed += 1
            log.info(f"Removed state.json entry for flight {fn} (not scheduled in any active trip)")

    conn.close()

    if added > 0 or updated > 0 or removed > 0:
        save_state(state, dry_run)

    return added, updated, removed


def main():
    if len(sys.argv) < 2:
        print("Usage: trip-flight-state.py <trip-id> | --all | --dry-run <trip-id>", file=sys.stderr)
        sys.exit(1)

    dry_run = False
    trip_id = None

    if sys.argv[1] == "--dry-run":
        dry_run = True
        if len(sys.argv) < 3:
            print("Usage: trip-flight-state.py --dry-run <trip-id>", file=sys.stderr)
            sys.exit(1)
        trip_id = sys.argv[2]
    elif sys.argv[1] == "--all":
        require_db_file(TRIPS_DB, "trips")
        conn = sqlite3.connect(TRIPS_DB)
        conn.row_factory = sqlite3.Row
        trips = conn.execute(
            "SELECT id FROM trips WHERE status IN ('upcoming', 'active')"
        ).fetchall()
        conn.close()

        total_a, total_u, total_r = 0, 0, 0
        for t in trips:
            a, u, r = sync_trip_flights(t["id"], dry_run)
            if a >= 0:
                total_a += a
                total_u += u
                total_r += r

        prefix = "[DRY RUN] " if dry_run else ""
        print(f"{prefix}OK: {total_a} added, {total_u} updated, {total_r} removed across {len(trips)} trips")
        sys.exit(0)
    else:
        trip_id = sys.argv[1]

    a, u, r = sync_trip_flights(trip_id, dry_run)
    if a < 0:
        sys.exit(1)

    prefix = "[DRY RUN] " if dry_run else ""
    print(f"{prefix}OK: {a} added, {u} updated, {r} removed for {trip_id}")


if __name__ == "__main__":
    main()
