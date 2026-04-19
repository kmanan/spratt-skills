#!/usr/bin/env python3
"""
trip-outbox-gen.py — Generate outbox messages from trips.sqlite rows.

Reads directly from the database (no manifest files). Uses trips.group_chat_guid
and the travelers table for routing. Only generates messages for rows where
outbox_msg_id IS NULL (new or updated items that need messages).

Usage:
  trip-outbox-gen.py <trip-id>         Generate messages for one trip
  trip-outbox-gen.py --all             Generate for all upcoming/active trips
  trip-outbox-gen.py --dry-run <id>    Show what would be generated without writing

Exit codes:
  0 = success
  1 = error (trip not found, DB error, etc.)
"""

import sys
import os
import sqlite3
import logging
import urllib.parse
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, timezone

# Import outbox for message delivery (same pattern as flight_monitor.py)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "outbox"))
from outbox import OutboxDB

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

# ─── Config ───

TRIPS_DB = os.path.expanduser("~/.config/spratt/db/trips.sqlite")
OUTBOX_DB = os.path.expanduser("~/.config/spratt/db/outbox.sqlite")
LOG_FILE = os.path.expanduser("~/Library/Logs/spratt/trip-outbox-gen.log")
UBER_BASE = "https://m.uber.com/ul/?action=setPickup&pickup=my_location"
MANAN_PHONE = "+13157082088"


def require_db_file(path, name):
    """Fail loudly if a SQLite DB file doesn't exist where expected.
    Prevents silent split-brain from path resolution bugs that would otherwise
    cause sqlite3.connect() to create an empty new DB at the wrong path.
    """
    if not os.path.exists(path):
        sys.stderr.write(
            f"\nFATAL: {name} database not found at:\n    {path}\n\n"
            f"Refusing to auto-create (prevents silent data loss if the path is wrong).\n\n"
        )
        sys.exit(1)

# IATA airport code → IANA timezone (for rendering departure times in local time).
# Covers every airport referenced in current trips. Extend as needed.
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


def format_departure(departs_utc, origin_iata):
    """Render a departs_utc ISO string as human-readable local time at the origin airport.

    Example: "10:55 PM PT · Sun, Apr 19" for '2026-04-19T22:55:00-07:00' at SEA.
    Falls back to raw ISO only if the timestamp can't be parsed (shouldn't happen).
    """
    if not departs_utc:
        return "time TBD"
    try:
        dt = datetime.fromisoformat(departs_utc.replace("Z", "+00:00"))
    except Exception:
        return departs_utc  # unparseable → fall through rather than drop

    # Re-express in the origin airport's local timezone when known.
    tz_name = AIRPORT_TZ.get((origin_iata or "").upper())
    if tz_name:
        try:
            dt = dt.astimezone(ZoneInfo(tz_name))
            tz_abbr = dt.strftime("%Z") or ""
            # %Z can return "PDT"/"PST" etc; collapse to "PT"/"ET" for conversational feel
            short = {
                "PDT": "PT", "PST": "PT",
                "EDT": "ET", "EST": "ET",
                "CDT": "CT", "CST": "CT",
                "MDT": "MT", "MST": "MT",
                "HDT": "HT", "HST": "HT",
            }.get(tz_abbr, tz_abbr)
            time_part = dt.strftime("%-I:%M %p").lstrip("0")
            date_part = dt.strftime("%a, %b %-d")
            return f"{time_part} {short} · {date_part}".strip().rstrip("·").strip()
        except Exception:
            pass

    # No tz map match — render in whatever offset the ISO carried.
    return dt.strftime("%-I:%M %p · %a, %b %-d")

# ─── Logging ───

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [trip-outbox-gen] %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=2),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ─── Helpers ───

def uber_link(address):
    """Generate Uber deep link for an address."""
    if not address:
        return ""
    encoded = urllib.parse.quote(address)
    return f"{UBER_BASE}&dropoff[formatted_address]={encoded}"


def compute_send_time_from_utc(utc_time_str, hours_before):
    """Compute UTC send time: event_time (UTC) minus hours_before."""
    try:
        if not utc_time_str:
            return None
        dt = datetime.fromisoformat(utc_time_str.replace("Z", "+00:00"))
        send_dt = dt - timedelta(hours=hours_before)
        return send_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def compute_send_time_local(date_str, time_str, hours_before, tz_name):
    """Compute UTC send time from local date + time."""
    try:
        if not date_str or not time_str or not tz_name:
            return None
        tz = ZoneInfo(tz_name)
        local_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        send_dt = local_dt - timedelta(hours=hours_before)
        return send_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def get_recipient(trips_conn, trip_id):
    """Determine message recipient for a trip.

    Priority: group_chat_guid > first traveler phone > Manan fallback.
    """
    trip = trips_conn.execute(
        "SELECT group_chat_guid FROM trips WHERE id = ?", (trip_id,)
    ).fetchone()
    if trip and trip["group_chat_guid"]:
        return trip["group_chat_guid"]

    # Solo trip — find first traveler phone
    traveler = trips_conn.execute(
        "SELECT phone FROM travelers WHERE trip_id = ? AND phone IS NOT NULL ORDER BY id LIMIT 1",
        (trip_id,),
    ).fetchone()
    if traveler and traveler["phone"]:
        return traveler["phone"]

    return MANAN_PHONE


_outbox = None

def get_outbox():
    global _outbox
    if _outbox is None:
        _outbox = OutboxDB()
    return _outbox

def create_outbox_message(recipient, body, send_at_utc, source, trip_id, dry_run=False):
    """Write a message to the outbox via OutboxDB.schedule() which resolves and validates recipients."""
    if dry_run:
        log.info(f"[DRY RUN] Would create: to={recipient}, at={send_at_utc}, source={source}")
        log.info(f"  body: {body[:120]}...")
        return -1

    return get_outbox().schedule(
        recipient=recipient,
        body=body,
        send_at=send_at_utc,
        source=source,
        created_by="trip-outbox-gen",
        trip_id=trip_id,
    )


# ─── Generation ───

def generate_for_trip(trip_id, dry_run=False):
    """Generate outbox messages for a single trip. Returns count generated."""
    require_db_file(TRIPS_DB, "trips")
    conn = sqlite3.connect(TRIPS_DB)
    conn.row_factory = sqlite3.Row

    trip = conn.execute(
        "SELECT id, name, timezone, group_chat_guid, status FROM trips WHERE id = ?",
        (trip_id,),
    ).fetchone()
    if not trip:
        log.error(f"Trip '{trip_id}' not found")
        conn.close()
        return -1

    if trip["status"] in ("completed", "cancelled"):
        log.info(f"Trip '{trip_id}' is {trip['status']} — skipping")
        conn.close()
        return 0

    tz_name = trip["timezone"]
    recipient = get_recipient(conn, trip_id)
    generated = 0

    # ─── Flights without outbox messages ───
    flights = conn.execute(
        "SELECT id, traveler, flight_number, route, departs_utc, arrives_utc "
        "FROM flights WHERE trip_id = ? AND status = 'scheduled' AND outbox_msg_id IS NULL",
        (trip_id,),
    ).fetchall()

    for f in flights:
        send_at = compute_send_time_from_utc(f["departs_utc"], 3)
        if not send_at:
            log.warning(f"Flight {f['flight_number']}: no departure time, skipping outbox")
            continue

        # Departure airport from route ("EWR → SEA" → "EWR")
        airport = ""
        if f["route"] and "→" in f["route"]:
            airport = f["route"].split("→")[0].strip()

        # Human-readable departure time in the origin airport's local timezone.
        # Message fires 3h before departure, so "in ~3 hours" gives the reader
        # an immediate sense of urgency on top of the absolute local time.
        departure_str = format_departure(f["departs_utc"], airport)
        traveler = f["traveler"] or "Flight"
        flight_num = f["flight_number"]
        route = f["route"] or ""
        body = (
            f"✈️ {traveler} — {flight_num} {route}\n"
            f"Departs {departure_str} (in ~3 hours)."
        )
        if airport:
            body += f"\n🚗 Uber to {airport}: {uber_link(airport + ' Airport')}"

        source = f"trip:{trip_id}:flight:{f['flight_number']}"
        msg_id = create_outbox_message(recipient, body, send_at, source, trip_id, dry_run)

        if not dry_run:
            conn.execute(
                "UPDATE flights SET outbox_msg_id = ?, outbox_generated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                (msg_id, f["id"]),
            )
        log.info(f"outbox [{msg_id}] created for flight {f['flight_number']}")
        generated += 1

    # ─── Hotels without outbox messages ───
    hotels = conn.execute(
        "SELECT id, name, address, check_in "
        "FROM hotels WHERE trip_id = ? AND outbox_msg_id IS NULL",
        (trip_id,),
    ).fetchall()

    for h in hotels:
        if not h["check_in"]:
            continue
        send_at = compute_send_time_local(h["check_in"], "08:00", 0, tz_name)
        if not send_at:
            continue

        body = f"🏨 Checking in to {h['name'] or 'hotel'} today."
        if h["address"]:
            body += f"\n📍 {h['address']}\n🚗 Uber: {uber_link(h['address'])}"

        source = f"trip:{trip_id}:hotel:{h['name'] or 'hotel'}"
        msg_id = create_outbox_message(recipient, body, send_at, source, trip_id, dry_run)

        if not dry_run:
            conn.execute(
                "UPDATE hotels SET outbox_msg_id = ?, outbox_generated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                (msg_id, h["id"]),
            )
        log.info(f"outbox [{msg_id}] created for hotel {h['name']}")
        generated += 1

    # ─── Reservations without outbox messages ───
    reservations = conn.execute(
        "SELECT id, type, name, date, time, address, notes "
        "FROM reservations WHERE trip_id = ? AND outbox_msg_id IS NULL",
        (trip_id,),
    ).fetchall()

    for r in reservations:
        # Skip cancelled reservations
        if r["notes"] and "CANCELLED" in r["notes"]:
            continue
        if not r["date"] or not r["time"]:
            continue

        hours_before = 4 if r["type"] in ("dinner", "brunch", "lunch") else 2
        send_at = compute_send_time_local(r["date"], r["time"], hours_before, tz_name)
        if not send_at:
            continue

        emoji = {"dinner": "🍽", "brunch": "🥂", "lunch": "🍽", "activity": "🎯", "tour": "🗺", "show": "🎭", "event": "📍"}.get(r["type"], "📍")
        body = f"{emoji} {r['type'].title()} at {r['name']}, {r['time']}."
        if r["address"]:
            body += f"\n📍 {r['address']}\n🚗 Uber: {uber_link(r['address'])}"

        source = f"trip:{trip_id}:reservation:{r['name']}"
        msg_id = create_outbox_message(recipient, body, send_at, source, trip_id, dry_run)

        if not dry_run:
            conn.execute(
                "UPDATE reservations SET outbox_msg_id = ?, outbox_generated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                (msg_id, r["id"]),
            )
        log.info(f"outbox [{msg_id}] created for {r['type']} {r['name']}")
        generated += 1

    if not dry_run:
        conn.commit()
    conn.close()

    return generated


# ─── Main ───

def main():
    if len(sys.argv) < 2:
        print("Usage: trip-outbox-gen.py <trip-id> | --all | --dry-run <trip-id>", file=sys.stderr)
        sys.exit(1)

    dry_run = False
    trip_id = None

    if sys.argv[1] == "--dry-run":
        dry_run = True
        if len(sys.argv) < 3:
            print("Usage: trip-outbox-gen.py --dry-run <trip-id>", file=sys.stderr)
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

        total = 0
        for t in trips:
            count = generate_for_trip(t["id"], dry_run)
            if count > 0:
                total += count
        print(f"OK: {total} outbox messages generated across {len(trips)} trips")
        sys.exit(0)
    else:
        trip_id = sys.argv[1]

    count = generate_for_trip(trip_id, dry_run)
    if count < 0:
        sys.exit(1)

    prefix = "[DRY RUN] " if dry_run else ""
    print(f"{prefix}OK: {count} outbox messages generated for {trip_id}")


if __name__ == "__main__":
    main()
