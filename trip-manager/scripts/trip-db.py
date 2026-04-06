#!/usr/bin/env python3
"""
trip-db.py — Safe CLI for LLM-driven trip database operations.

Thin wrapper around trips.sqlite that handles timezone conversion,
input validation, parameterized SQL, and post-write verification.
The LLM (Spratt) calls these subcommands instead of composing raw SQL.

Subcommands:
  add-trip          Create a new trip (only id required)
  add-flight        Add a flight to a trip (local time + timezone -> UTC)
  add-hotel         Add a hotel to a trip
  add-reservation   Add a reservation (type auto-inferred if omitted)
  add-traveler      Add a traveler with phone number
  update-trip       Update trip fields
  update-flight     Update flight fields (clears outbox for regeneration)
  update-reservation Update reservation fields (clears outbox for regeneration)
  cancel-reservation Cancel a reservation by name (sets status, doesn't delete)
  view              Show full trip context (separate queries, no cartesian products)
  list-trips        List all trips by status

Exit codes:
  0 = success
  1 = validation error (bad input, missing trip, etc.)
  2 = database error

Design:
  - All times provided as LOCAL time + IANA timezone; script converts to UTC
  - trip_id validated on every add-* call (must exist)
  - Parameterized SQL only (no string interpolation)
  - Every write prints verification output so the LLM sees what landed
  - Updates automatically null outbox_msg_id to trigger regeneration
"""

import sys
import os
import re
import sqlite3
import argparse
from datetime import datetime, date, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

# --- Config ---

TRIPS_DB = os.path.expanduser("~/.config/spratt/trips/trips.sqlite")


def _sync_flight_state(trip_id):
    """Auto-update flight monitor state.json after flight changes."""
    try:
        import importlib
        _trip_dir = os.path.dirname(os.path.abspath(__file__))
        if _trip_dir not in sys.path:
            sys.path.insert(0, _trip_dir)
        mod = importlib.import_module("trip-flight-state")
        a, u, r = mod.sync_trip_flights(trip_id)
        if a > 0 or u > 0 or r > 0:
            print(f"  monitor:     state.json updated ({a} added, {u} updated, {r} removed)")
    except Exception as e:
        print(f"  monitor:     state.json sync failed (non-fatal): {e}", file=sys.stderr)

KNOWN_TIMEZONES = {
    "india": "Asia/Kolkata",
    "mumbai": "Asia/Kolkata",
    "delhi": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata",
    "seattle": "America/Los_Angeles",
    "redmond": "America/Los_Angeles",
    "portland": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles",
    "los angeles": "America/Los_Angeles",
    "chicago": "America/Chicago",
    "new york": "America/New_York",
    "nyc": "America/New_York",
    "new jersey": "America/New_York",
    "jersey city": "America/New_York",
    "dc": "America/New_York",
    "washington": "America/New_York",
    "washington dc": "America/New_York",
    "boston": "America/New_York",
    "miami": "America/New_York",
    "denver": "America/Denver",
    "phoenix": "America/Phoenix",
    "honolulu": "Pacific/Honolulu",
    "london": "Europe/London",
    "paris": "Europe/Paris",
    "tokyo": "Asia/Tokyo",
}

VALID_RESERVATION_TYPES = {"dinner", "brunch", "lunch", "activity", "tour", "show", "event"}


# --- Helpers ---

def get_db():
    """Connect to trips.sqlite with WAL mode."""
    if not os.path.exists(TRIPS_DB):
        print(f"ERROR: Database not found at {TRIPS_DB}", file=sys.stderr)
        sys.exit(2)
    conn = sqlite3.connect(TRIPS_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def ensure_trip_exists(conn, trip_id):
    """Validate that trip_id exists. Exit 1 if not."""
    row = conn.execute("SELECT id, name, status FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if not row:
        existing = conn.execute("SELECT id, name FROM trips ORDER BY created_at DESC LIMIT 5").fetchall()
        print(f"ERROR: Trip '{trip_id}' does not exist.", file=sys.stderr)
        if existing:
            print("Existing trips:", file=sys.stderr)
            for r in existing:
                print(f"  {r['id']} — {r['name'] or '(unnamed)'}", file=sys.stderr)
        print("Create it first with: trip-db.py add-trip --id <trip-id>", file=sys.stderr)
        sys.exit(1)
    if row["status"] == "cancelled":
        print(f"WARNING: Trip '{trip_id}' is cancelled. Proceeding anyway.", file=sys.stderr)
    return row


def resolve_tz(tz_input):
    """Resolve a timezone input to IANA name. Accepts IANA names or city keywords."""
    if not tz_input:
        return None
    # Try as IANA name first
    try:
        ZoneInfo(tz_input)
        return tz_input
    except (KeyError, Exception):
        pass
    # Try keyword lookup
    search = tz_input.lower().strip()
    for keyword, tz in KNOWN_TIMEZONES.items():
        if keyword in search or search in keyword:
            return tz
    print(f"ERROR: Cannot resolve timezone '{tz_input}'. Use IANA name (e.g., America/New_York) or city name.", file=sys.stderr)
    sys.exit(1)


def local_to_utc(date_str, time_str, tz_name):
    """Convert local date + time + timezone to UTC ISO string.

    Args:
        date_str: "YYYY-MM-DD"
        time_str: "HH:MM" (24h)
        tz_name: IANA timezone name

    Returns: "YYYY-MM-DDTHH:MM:SSZ" in UTC
    """
    if not date_str or not time_str or not tz_name:
        return None
    try:
        tz = ZoneInfo(tz_name)
        local_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        local_dt = local_dt.replace(tzinfo=tz)
        utc_dt = local_dt.astimezone(ZoneInfo("UTC"))
        return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception as e:
        print(f"ERROR: Cannot convert '{date_str} {time_str}' in {tz_name} to UTC: {e}", file=sys.stderr)
        sys.exit(1)


def parse_departs(departs_str, tz_name):
    """Parse a departure time string. Accepts 'YYYY-MM-DD HH:MM' (local) or ISO 8601 with offset.

    If the string already has a UTC offset (e.g., 2026-04-01T15:00:00Z or 2026-04-01T08:00:00-07:00),
    it's stored as-is. Otherwise, local time + tz_name is converted to UTC.
    """
    if not departs_str:
        return None

    # Check if already has offset/Z
    if "T" in departs_str and (departs_str.endswith("Z") or "+" in departs_str[10:] or departs_str.count("-") > 2):
        # Already ISO with offset — normalize to UTC
        try:
            dt = datetime.fromisoformat(departs_str.replace("Z", "+00:00"))
            return dt.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception as e:
            print(f"ERROR: Cannot parse ISO time '{departs_str}': {e}", file=sys.stderr)
            sys.exit(1)

    # Local time — needs timezone
    if not tz_name:
        print(f"ERROR: Departure time '{departs_str}' has no timezone offset. Provide --tz.", file=sys.stderr)
        sys.exit(1)

    # Parse "YYYY-MM-DD HH:MM"
    parts = departs_str.strip().split(" ")
    if len(parts) == 2:
        return local_to_utc(parts[0], parts[1], tz_name)

    print(f"ERROR: Cannot parse departure time '{departs_str}'. Use 'YYYY-MM-DD HH:MM' format.", file=sys.stderr)
    sys.exit(1)


def infer_reservation_type(name, time_str):
    """Infer reservation type from name and time if not explicitly provided."""
    if time_str:
        try:
            hour = int(time_str.split(":")[0])
            if hour >= 17:
                return "dinner"
            elif hour < 11:
                return "brunch"
            elif 11 <= hour < 15:
                return "lunch"
        except (ValueError, IndexError):
            pass
    # Keyword-based inference from name
    name_lower = (name or "").lower()
    restaurant_keywords = {"restaurant", "grill", "bistro", "cafe", "kitchen", "bar", "tavern", "steakhouse", "sushi", "pizza"}
    activity_keywords = {"museum", "park", "zoo", "theater", "theatre", "gallery", "library", "monument", "memorial", "tour", "hike", "walk"}
    if any(kw in name_lower for kw in restaurant_keywords):
        return "dinner"
    if any(kw in name_lower for kw in activity_keywords):
        return "activity"
    return "activity"  # Safe default


def validate_date(date_str):
    """Validate a date string is ISO format. Returns the string or exits."""
    if not date_str:
        return None
    try:
        date.fromisoformat(date_str)
        return date_str
    except ValueError:
        print(f"ERROR: Invalid date '{date_str}'. Use YYYY-MM-DD format.", file=sys.stderr)
        sys.exit(1)


def validate_time(time_str):
    """Validate a time string is HH:MM 24h format. Returns the string or exits."""
    if not time_str:
        return None
    if not re.match(r"^\d{1,2}:\d{2}$", time_str):
        print(f"ERROR: Invalid time '{time_str}'. Use HH:MM 24h format (e.g., 19:30).", file=sys.stderr)
        sys.exit(1)
    parts = time_str.split(":")
    h, m = int(parts[0]), int(parts[1])
    if h > 23 or m > 59:
        print(f"ERROR: Invalid time '{time_str}'. Hours 0-23, minutes 0-59.", file=sys.stderr)
        sys.exit(1)
    # Normalize to zero-padded
    return f"{h:02d}:{m:02d}"


def validate_trip_id(trip_id):
    """Validate trip_id format: lowercase, hyphens, no spaces."""
    if not trip_id:
        print("ERROR: --id is required.", file=sys.stderr)
        sys.exit(1)
    if not re.match(r"^[a-z0-9][a-z0-9-]*$", trip_id):
        print(f"ERROR: trip_id '{trip_id}' must be lowercase alphanumeric with hyphens (e.g., 2026-04-dc).", file=sys.stderr)
        sys.exit(1)
    return trip_id


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def update_travelers_display(conn, trip_id):
    """Update trips.travelers display column from travelers table."""
    rows = conn.execute(
        "SELECT name FROM travelers WHERE trip_id = ? ORDER BY id", (trip_id,)
    ).fetchall()
    if rows:
        display = ", ".join(r["name"] for r in rows)
        conn.execute(
            "UPDATE trips SET travelers = ?, updated_at = ? WHERE id = ?",
            (display, now_utc(), trip_id),
        )


# --- Schema Migration ---

def ensure_schema(conn):
    """Ensure the schema has all required tables and columns for the new architecture."""
    # Add group_chat_guid to trips if missing
    columns = {row[1] for row in conn.execute("PRAGMA table_info(trips)").fetchall()}
    if "group_chat_guid" not in columns:
        conn.execute("ALTER TABLE trips ADD COLUMN group_chat_guid TEXT")

    # Create travelers table if missing
    conn.execute("""
        CREATE TABLE IF NOT EXISTS travelers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id TEXT NOT NULL,
            name TEXT NOT NULL,
            phone TEXT,
            role TEXT,
            created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_travelers_trip ON travelers(trip_id)")
    conn.commit()


# --- Subcommands ---

def cmd_add_trip(args):
    """Create a new trip. Only --id is required; everything else is optional."""
    trip_id = validate_trip_id(args.id)
    conn = get_db()
    ensure_schema(conn)

    # Check for duplicate
    existing = conn.execute("SELECT id FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if existing:
        print(f"ERROR: Trip '{trip_id}' already exists. Use update-trip to modify it.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    start_date = validate_date(args.start_date)
    end_date = validate_date(args.end_date)

    # Resolve timezone from explicit --tz or infer from destination
    tz_name = None
    if args.tz:
        tz_name = resolve_tz(args.tz)
    elif args.destination:
        # Try to infer from destination
        search = args.destination.lower()
        for keyword, tz in KNOWN_TIMEZONES.items():
            if keyword in search:
                tz_name = tz
                break

    # Compute UTC offset if we have timezone and start date
    tz_utc_offset = None
    if tz_name and start_date:
        try:
            tz = ZoneInfo(tz_name)
            dt = datetime(int(start_date[:4]), int(start_date[5:7]), int(start_date[8:10]), 12, 0, 0, tzinfo=tz)
            offset = dt.strftime("%z")
            tz_utc_offset = f"{offset[:3]}:{offset[3:]}"
        except Exception:
            pass

    # Compute initial status
    status = "upcoming"
    today = date.today()
    if start_date and end_date:
        s = date.fromisoformat(start_date)
        e = date.fromisoformat(end_date)
        if s <= today <= e:
            status = "active"
        elif today > e:
            status = "completed"
    elif start_date:
        s = date.fromisoformat(start_date)
        if s <= today:
            status = "active"

    conn.execute(
        """INSERT INTO trips (id, name, destination, timezone, tz_utc_offset,
           start_date, end_date, status, group_chat_guid, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (trip_id, args.name, args.destination, tz_name, tz_utc_offset,
         start_date, end_date, status, args.group_chat, now_utc(), now_utc()),
    )
    conn.commit()

    # Verify
    row = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    conn.close()

    print(f"OK: Trip created")
    print(f"  id:          {row['id']}")
    print(f"  name:        {row['name'] or '(not set)'}")
    print(f"  destination: {row['destination'] or '(not set)'}")
    print(f"  dates:       {row['start_date'] or '?'} to {row['end_date'] or '?'}")
    print(f"  timezone:    {row['timezone'] or '(not set)'}")
    print(f"  status:      {row['status']}")
    if row['group_chat_guid']:
        print(f"  group_chat:  {row['group_chat_guid']}")


def cmd_add_flight(args):
    """Add a flight to an existing trip."""
    conn = get_db()
    ensure_schema(conn)
    ensure_trip_exists(conn, args.trip)

    if not args.flight:
        print("ERROR: --flight (flight number) is required.", file=sys.stderr)
        sys.exit(1)

    # Check for duplicate flight number on this trip
    dup = conn.execute(
        "SELECT id FROM flights WHERE trip_id = ? AND flight_number = ? AND status = 'scheduled'",
        (args.trip, args.flight),
    ).fetchone()
    if dup:
        print(f"ERROR: Flight {args.flight} already exists on trip {args.trip} (id={dup['id']}). Use update-flight to modify.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    # Resolve timezone — try explicit, then trip's timezone
    tz_name = None
    if args.tz:
        tz_name = resolve_tz(args.tz)
    else:
        trip_row = conn.execute("SELECT timezone FROM trips WHERE id = ?", (args.trip,)).fetchone()
        if trip_row and trip_row["timezone"]:
            tz_name = trip_row["timezone"]

    # Convert departure time
    departs_utc = parse_departs(args.departs, tz_name)
    arrives_utc = parse_departs(args.arrives, tz_name) if args.arrives else None

    conn.execute(
        """INSERT INTO flights (trip_id, traveler, flight_number, route, departs_utc, arrives_utc)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (args.trip, args.traveler, args.flight, args.route, departs_utc, arrives_utc),
    )
    conn.commit()

    # Verify
    row = conn.execute(
        "SELECT * FROM flights WHERE trip_id = ? AND flight_number = ? ORDER BY id DESC LIMIT 1",
        (args.trip, args.flight),
    ).fetchone()
    conn.close()

    print(f"OK: Flight added")
    print(f"  id:          {row['id']}")
    print(f"  trip:        {args.trip}")
    print(f"  traveler:    {row['traveler'] or '(not set)'}")
    print(f"  flight:      {row['flight_number']}")
    print(f"  route:       {row['route'] or '(not set)'}")
    print(f"  departs_utc: {row['departs_utc'] or '(not set)'}")
    print(f"  arrives_utc: {row['arrives_utc'] or '(not set)'}")
    print(f"  outbox:      not yet generated — run trip-outbox-gen.py {args.trip}")

    # Auto-update flight monitor state.json
    _sync_flight_state(args.trip)


def cmd_add_hotel(args):
    """Add a hotel to an existing trip."""
    conn = get_db()
    ensure_schema(conn)
    ensure_trip_exists(conn, args.trip)

    check_in = validate_date(args.checkin)
    check_out = validate_date(args.checkout)

    conn.execute(
        """INSERT INTO hotels (trip_id, name, address, check_in, check_out)
           VALUES (?, ?, ?, ?, ?)""",
        (args.trip, args.name, args.address, check_in, check_out),
    )
    conn.commit()

    row = conn.execute(
        "SELECT * FROM hotels WHERE trip_id = ? ORDER BY id DESC LIMIT 1",
        (args.trip,),
    ).fetchone()
    conn.close()

    print(f"OK: Hotel added")
    print(f"  id:        {row['id']}")
    print(f"  trip:      {args.trip}")
    print(f"  name:      {row['name'] or '(not set)'}")
    print(f"  address:   {row['address'] or '(not set)'}")
    print(f"  check_in:  {row['check_in'] or '(not set)'}")
    print(f"  check_out: {row['check_out'] or '(not set)'}")
    print(f"  outbox:    not yet generated — run trip-outbox-gen.py {args.trip}")


def cmd_add_reservation(args):
    """Add a reservation to an existing trip."""
    conn = get_db()
    ensure_schema(conn)
    ensure_trip_exists(conn, args.trip)

    if not args.name:
        print("ERROR: --name is required.", file=sys.stderr)
        sys.exit(1)

    res_date = validate_date(args.date)
    res_time = validate_time(args.time)

    # Type: explicit > inferred
    res_type = args.type
    if res_type:
        res_type = res_type.lower()
        if res_type not in VALID_RESERVATION_TYPES:
            print(f"ERROR: Invalid type '{res_type}'. Valid: {', '.join(sorted(VALID_RESERVATION_TYPES))}", file=sys.stderr)
            sys.exit(1)
    else:
        res_type = infer_reservation_type(args.name, res_time)
        print(f"NOTE: Type inferred as '{res_type}' from name/time. Override with --type if wrong.")

    conn.execute(
        """INSERT INTO reservations (trip_id, type, name, date, time, address, party_size, confirmation, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (args.trip, res_type, args.name, res_date, res_time,
         args.address, args.party_size, args.confirmation, args.notes),
    )
    conn.commit()

    row = conn.execute(
        "SELECT * FROM reservations WHERE trip_id = ? ORDER BY id DESC LIMIT 1",
        (args.trip,),
    ).fetchone()
    conn.close()

    print(f"OK: Reservation added")
    print(f"  id:           {row['id']}")
    print(f"  trip:         {args.trip}")
    print(f"  type:         {row['type']}")
    print(f"  name:         {row['name']}")
    print(f"  date:         {row['date'] or '(not set)'}")
    print(f"  time:         {row['time'] or '(not set)'}")
    print(f"  address:      {row['address'] or '(not set)'}")
    if row['party_size']:
        print(f"  party_size:   {row['party_size']}")
    if row['confirmation']:
        print(f"  confirmation: {row['confirmation']}")
    if row['notes']:
        print(f"  notes:        {row['notes']}")
    print(f"  outbox:       not yet generated — run trip-outbox-gen.py {args.trip}")


def cmd_add_traveler(args):
    """Add a traveler to an existing trip."""
    conn = get_db()
    ensure_schema(conn)
    ensure_trip_exists(conn, args.trip)

    if not args.name:
        print("ERROR: --name is required.", file=sys.stderr)
        sys.exit(1)

    # Check for duplicate
    dup = conn.execute(
        "SELECT id FROM travelers WHERE trip_id = ? AND name = ?",
        (args.trip, args.name),
    ).fetchone()
    if dup:
        print(f"ERROR: Traveler '{args.name}' already exists on trip {args.trip} (id={dup['id']}).", file=sys.stderr)
        conn.close()
        sys.exit(1)

    conn.execute(
        "INSERT INTO travelers (trip_id, name, phone, role) VALUES (?, ?, ?, ?)",
        (args.trip, args.name, args.phone, args.role),
    )

    # Update display column on trips table
    update_travelers_display(conn, args.trip)

    conn.commit()

    row = conn.execute(
        "SELECT * FROM travelers WHERE trip_id = ? AND name = ?",
        (args.trip, args.name),
    ).fetchone()
    conn.close()

    print(f"OK: Traveler added")
    print(f"  id:    {row['id']}")
    print(f"  trip:  {args.trip}")
    print(f"  name:  {row['name']}")
    print(f"  phone: {row['phone'] or '(not set)'}")
    if row['role']:
        print(f"  role:  {row['role']}")


def cmd_update_trip(args):
    """Update fields on an existing trip."""
    conn = get_db()
    ensure_schema(conn)
    ensure_trip_exists(conn, args.id)

    updates = []
    params = []

    if args.name is not None:
        updates.append("name = ?")
        params.append(args.name)
    if args.destination is not None:
        updates.append("destination = ?")
        params.append(args.destination)
    if args.start_date is not None:
        updates.append("start_date = ?")
        params.append(validate_date(args.start_date))
    if args.end_date is not None:
        updates.append("end_date = ?")
        params.append(validate_date(args.end_date))
    if args.tz is not None:
        tz_name = resolve_tz(args.tz)
        updates.append("timezone = ?")
        params.append(tz_name)
    if args.group_chat is not None:
        updates.append("group_chat_guid = ?")
        params.append(args.group_chat)
    if args.status is not None:
        valid_statuses = {"upcoming", "active", "completed", "cancelled"}
        if args.status not in valid_statuses:
            print(f"ERROR: Invalid status '{args.status}'. Valid: {', '.join(sorted(valid_statuses))}", file=sys.stderr)
            sys.exit(1)
        updates.append("status = ?")
        params.append(args.status)

    if not updates:
        print("ERROR: No fields to update. Provide at least one of: --name, --destination, --start-date, --end-date, --tz, --group-chat, --status", file=sys.stderr)
        sys.exit(1)

    updates.append("updated_at = ?")
    params.append(now_utc())
    params.append(args.id)

    conn.execute(f"UPDATE trips SET {', '.join(updates)} WHERE id = ?", params)

    # Recompute UTC offset if timezone or dates changed
    row = conn.execute("SELECT timezone, start_date FROM trips WHERE id = ?", (args.id,)).fetchone()
    if row["timezone"] and row["start_date"]:
        try:
            tz = ZoneInfo(row["timezone"])
            sd = row["start_date"]
            dt = datetime(int(sd[:4]), int(sd[5:7]), int(sd[8:10]), 12, 0, 0, tzinfo=tz)
            offset = dt.strftime("%z")
            tz_utc_offset = f"{offset[:3]}:{offset[3:]}"
            conn.execute("UPDATE trips SET tz_utc_offset = ? WHERE id = ?", (tz_utc_offset, args.id))
        except Exception:
            pass

    conn.commit()

    row = conn.execute("SELECT * FROM trips WHERE id = ?", (args.id,)).fetchone()
    conn.close()

    print(f"OK: Trip updated")
    print(f"  id:          {row['id']}")
    print(f"  name:        {row['name'] or '(not set)'}")
    print(f"  destination: {row['destination'] or '(not set)'}")
    print(f"  dates:       {row['start_date'] or '?'} to {row['end_date'] or '?'}")
    print(f"  timezone:    {row['timezone'] or '(not set)'}")
    print(f"  status:      {row['status']}")


def cmd_update_flight(args):
    """Update fields on an existing flight. Clears outbox_msg_id for regeneration."""
    conn = get_db()
    ensure_schema(conn)

    # Find the flight
    row = conn.execute(
        "SELECT * FROM flights WHERE trip_id = ? AND flight_number = ? AND status = 'scheduled'",
        (args.trip, args.flight),
    ).fetchone()
    if not row:
        print(f"ERROR: No scheduled flight '{args.flight}' on trip '{args.trip}'.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    # Resolve timezone
    tz_name = None
    if args.tz:
        tz_name = resolve_tz(args.tz)
    else:
        trip_row = conn.execute("SELECT timezone FROM trips WHERE id = ?", (args.trip,)).fetchone()
        if trip_row and trip_row["timezone"]:
            tz_name = trip_row["timezone"]

    updates = []
    params = []

    if args.traveler is not None:
        updates.append("traveler = ?")
        params.append(args.traveler)
    if args.route is not None:
        updates.append("route = ?")
        params.append(args.route)
    if args.departs is not None:
        updates.append("departs_utc = ?")
        params.append(parse_departs(args.departs, tz_name))
    if args.arrives is not None:
        updates.append("arrives_utc = ?")
        params.append(parse_departs(args.arrives, tz_name))
    if args.new_flight is not None:
        updates.append("flight_number = ?")
        params.append(args.new_flight)

    if not updates:
        print("ERROR: No fields to update.", file=sys.stderr)
        sys.exit(1)

    # Clear outbox for regeneration
    updates.append("outbox_msg_id = NULL")
    updates.append("outbox_generated_at = NULL")
    updates.append("updated_at = ?")
    params.append(now_utc())
    params.append(row["id"])

    conn.execute(f"UPDATE flights SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()

    updated = conn.execute("SELECT * FROM flights WHERE id = ?", (row["id"],)).fetchone()
    conn.close()

    print(f"OK: Flight updated (outbox cleared for regeneration)")
    print(f"  id:          {updated['id']}")
    print(f"  flight:      {updated['flight_number']}")
    print(f"  traveler:    {updated['traveler'] or '(not set)'}")
    print(f"  route:       {updated['route'] or '(not set)'}")
    print(f"  departs_utc: {updated['departs_utc'] or '(not set)'}")
    print(f"  arrives_utc: {updated['arrives_utc'] or '(not set)'}")
    print(f"  ACTION:      Run trip-outbox-gen.py {args.trip} to regenerate messages")

    # Auto-update flight monitor state.json
    _sync_flight_state(args.trip)


def cmd_update_reservation(args):
    """Update fields on an existing reservation. Clears outbox_msg_id for regeneration."""
    conn = get_db()
    ensure_schema(conn)

    # Find the reservation by name + trip (most recent if multiple)
    row = conn.execute(
        "SELECT * FROM reservations WHERE trip_id = ? AND name = ? ORDER BY id DESC LIMIT 1",
        (args.trip, args.name),
    ).fetchone()
    if not row:
        # Show available reservations
        avail = conn.execute(
            "SELECT name, date, time FROM reservations WHERE trip_id = ? ORDER BY date, time",
            (args.trip,),
        ).fetchall()
        print(f"ERROR: No reservation named '{args.name}' on trip '{args.trip}'.", file=sys.stderr)
        if avail:
            print("Available reservations:", file=sys.stderr)
            for r in avail:
                print(f"  {r['name']} — {r['date']} {r['time'] or ''}", file=sys.stderr)
        conn.close()
        sys.exit(1)

    updates = []
    params = []

    if args.date is not None:
        updates.append("date = ?")
        params.append(validate_date(args.date))
    if args.time is not None:
        updates.append("time = ?")
        params.append(validate_time(args.time))
    if args.address is not None:
        updates.append("address = ?")
        params.append(args.address)
    if args.type is not None:
        t = args.type.lower()
        if t not in VALID_RESERVATION_TYPES:
            print(f"ERROR: Invalid type '{t}'. Valid: {', '.join(sorted(VALID_RESERVATION_TYPES))}", file=sys.stderr)
            sys.exit(1)
        updates.append("type = ?")
        params.append(t)
    if args.party_size is not None:
        updates.append("party_size = ?")
        params.append(args.party_size)
    if args.confirmation is not None:
        updates.append("confirmation = ?")
        params.append(args.confirmation)
    if args.notes is not None:
        updates.append("notes = ?")
        params.append(args.notes)
    if args.new_name is not None:
        updates.append("name = ?")
        params.append(args.new_name)

    if not updates:
        print("ERROR: No fields to update.", file=sys.stderr)
        sys.exit(1)

    # Clear outbox for regeneration
    updates.append("outbox_msg_id = NULL")
    updates.append("outbox_generated_at = NULL")
    updates.append("updated_at = ?")
    params.append(now_utc())
    params.append(row["id"])

    conn.execute(f"UPDATE reservations SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()

    updated = conn.execute("SELECT * FROM reservations WHERE id = ?", (row["id"],)).fetchone()
    conn.close()

    print(f"OK: Reservation updated (outbox cleared for regeneration)")
    print(f"  id:           {updated['id']}")
    print(f"  name:         {updated['name']}")
    print(f"  type:         {updated['type']}")
    print(f"  date:         {updated['date'] or '(not set)'}")
    print(f"  time:         {updated['time'] or '(not set)'}")
    print(f"  address:      {updated['address'] or '(not set)'}")
    print(f"  ACTION:       Run trip-outbox-gen.py {args.trip} to regenerate messages")


def cmd_cancel_reservation(args):
    """Cancel a reservation by name. Does NOT delete — updates status field."""
    conn = get_db()
    ensure_schema(conn)

    row = conn.execute(
        "SELECT * FROM reservations WHERE trip_id = ? AND name = ? ORDER BY id DESC LIMIT 1",
        (args.trip, args.name),
    ).fetchone()
    if not row:
        print(f"ERROR: No reservation named '{args.name}' on trip '{args.trip}'.", file=sys.stderr)
        conn.close()
        sys.exit(1)

    # Cancel the outbox message if one exists
    if row["outbox_msg_id"]:
        try:
            outbox_db = os.path.expanduser("~/.config/spratt/infrastructure/outbox/outbox.sqlite")
            oconn = sqlite3.connect(outbox_db)
            oconn.execute(
                "UPDATE messages SET status = 'cancelled', updated_at = strftime('%Y-%m-%d %H:%M:%S', 'now') WHERE id = ? AND status = 'pending'",
                (row["outbox_msg_id"],),
            )
            oconn.commit()
            oconn.close()
            print(f"  Outbox message {row['outbox_msg_id']} cancelled.")
        except Exception as e:
            print(f"  WARNING: Could not cancel outbox message: {e}", file=sys.stderr)

    # Mark reservation as cancelled via notes (no status column on reservations)
    conn.execute(
        "UPDATE reservations SET notes = COALESCE(notes || ' | ', '') || 'CANCELLED', outbox_msg_id = NULL, updated_at = ? WHERE id = ?",
        (now_utc(), row["id"]),
    )
    conn.commit()
    conn.close()

    print(f"OK: Reservation '{args.name}' cancelled")
    print(f"  id:     {row['id']}")
    print(f"  date:   {row['date']}")
    print(f"  time:   {row['time']}")


def cmd_view(args):
    """Show full trip context using separate queries (no cartesian products)."""
    conn = get_db()
    ensure_schema(conn)

    trip = conn.execute("SELECT * FROM trips WHERE id = ?", (args.trip,)).fetchone()
    if not trip:
        # List available trips
        all_trips = conn.execute("SELECT id, name, status FROM trips ORDER BY start_date DESC").fetchall()
        print(f"ERROR: Trip '{args.trip}' not found.", file=sys.stderr)
        if all_trips:
            print("Available trips:", file=sys.stderr)
            for t in all_trips:
                print(f"  {t['id']} — {t['name'] or '(unnamed)'} [{t['status']}]", file=sys.stderr)
        conn.close()
        sys.exit(1)

    # Trip header
    print(f"=== {trip['name'] or trip['id']} ===")
    print(f"Destination: {trip['destination'] or '(not set)'} | Timezone: {trip['timezone'] or '(not set)'}")
    print(f"Dates: {trip['start_date'] or '?'} to {trip['end_date'] or '?'} | Status: {trip['status']}")
    if trip['group_chat_guid']:
        print(f"Group chat: {trip['group_chat_guid']}")
    print()

    # Travelers (from travelers table, not trips.travelers column)
    travelers = conn.execute(
        "SELECT name, phone, role FROM travelers WHERE trip_id = ? ORDER BY id",
        (args.trip,),
    ).fetchall()
    if travelers:
        print("=== Travelers ===")
        for t in travelers:
            parts = [t["name"]]
            if t["role"]:
                parts.append(f"({t['role']})")
            if t["phone"]:
                parts.append(f"— {t['phone']}")
            print("  ".join(parts))
        print()

    # Flights
    flights = conn.execute(
        "SELECT flight_number, traveler, route, departs_utc, arrives_utc, status, outbox_msg_id "
        "FROM flights WHERE trip_id = ? ORDER BY departs_utc",
        (args.trip,),
    ).fetchall()
    if flights:
        print("=== Flights ===")
        for f in flights:
            status_tag = f" [{f['status']}]" if f["status"] != "scheduled" else ""
            outbox_tag = " outbox:yes" if f["outbox_msg_id"] else ""
            print(f"{f['flight_number']}  {f['traveler'] or '?'}  {f['route'] or '?'}  {f['departs_utc'] or 'TBD'}{status_tag}{outbox_tag}")
        print()

    # Hotels
    hotels = conn.execute(
        "SELECT name, address, check_in, check_out, outbox_msg_id "
        "FROM hotels WHERE trip_id = ? ORDER BY check_in",
        (args.trip,),
    ).fetchall()
    if hotels:
        print("=== Hotels ===")
        for h in hotels:
            outbox_tag = " outbox:yes" if h["outbox_msg_id"] else ""
            print(f"{h['name'] or '(unnamed)'} — {h['address'] or '(no address)'} ({h['check_in'] or '?'} to {h['check_out'] or '?'}){outbox_tag}")
        print()

    # Reservations
    reservations = conn.execute(
        "SELECT name, type, date, time, address, party_size, confirmation, notes, outbox_msg_id "
        "FROM reservations WHERE trip_id = ? ORDER BY date, time",
        (args.trip,),
    ).fetchall()
    if reservations:
        print("=== Reservations ===")
        for r in reservations:
            cancelled = " [CANCELLED]" if r["notes"] and "CANCELLED" in r["notes"] else ""
            outbox_tag = " outbox:yes" if r["outbox_msg_id"] else ""
            conf = f" #{r['confirmation']}" if r["confirmation"] else ""
            party = f" (party of {r['party_size']})" if r["party_size"] else ""
            print(f"{r['date'] or '?'}  {r['time'] or '?'}  {r['type']:<10} {r['name']}{conf}{party} — {r['address'] or '(no address)'}{cancelled}{outbox_tag}")
        print()

    # Pending outbox count
    try:
        outbox_db = os.path.expanduser("~/.config/spratt/infrastructure/outbox/outbox.sqlite")
        oconn = sqlite3.connect(outbox_db)
        pending = oconn.execute(
            "SELECT COUNT(*) FROM messages WHERE trip_id = ? AND status = 'pending'",
            (args.trip,),
        ).fetchone()[0]
        delivered = oconn.execute(
            "SELECT COUNT(*) FROM messages WHERE trip_id = ? AND status = 'delivered'",
            (args.trip,),
        ).fetchone()[0]
        oconn.close()
        print(f"=== Outbox ===")
        print(f"Pending: {pending} | Delivered: {delivered}")
    except Exception:
        pass

    conn.close()


def cmd_list_trips(args):
    """List all trips, optionally filtered by status."""
    conn = get_db()
    ensure_schema(conn)

    if args.status:
        trips = conn.execute(
            "SELECT id, name, destination, start_date, end_date, status FROM trips WHERE status = ? ORDER BY start_date DESC",
            (args.status,),
        ).fetchall()
    else:
        trips = conn.execute(
            "SELECT id, name, destination, start_date, end_date, status FROM trips ORDER BY start_date DESC"
        ).fetchall()

    conn.close()

    if not trips:
        print("No trips found.")
        return

    for t in trips:
        dates = f"{t['start_date'] or '?'} to {t['end_date'] or '?'}"
        print(f"{t['id']:<30} {t['name'] or '(unnamed)':<30} {t['destination'] or '':<20} {dates:<25} [{t['status']}]")


# --- Argument Parser ---

def build_parser():
    parser = argparse.ArgumentParser(
        description="Trip database CLI — safe wrapper for LLM-driven trip management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # add-trip
    p = sub.add_parser("add-trip", help="Create a new trip")
    p.add_argument("--id", required=True, help="Trip ID (e.g., 2026-04-dc). Lowercase, hyphens only.")
    p.add_argument("--name", help="Trip name (e.g., 'DC Trip')")
    p.add_argument("--destination", help="Destination city")
    p.add_argument("--start-date", dest="start_date", help="Start date (YYYY-MM-DD)")
    p.add_argument("--end-date", dest="end_date", help="End date (YYYY-MM-DD)")
    p.add_argument("--tz", help="Timezone (IANA name or city keyword)")
    p.add_argument("--group-chat", dest="group_chat", help="Group chat GUID for messaging")

    # add-flight
    p = sub.add_parser("add-flight", help="Add a flight to a trip")
    p.add_argument("--trip", required=True, help="Trip ID")
    p.add_argument("--traveler", help="Traveler name")
    p.add_argument("--flight", required=True, help="Flight number (e.g., AS4)")
    p.add_argument("--route", help="Route (e.g., 'SEA -> DCA')")
    p.add_argument("--departs", help="Departure: 'YYYY-MM-DD HH:MM' local or ISO with offset")
    p.add_argument("--arrives", help="Arrival: 'YYYY-MM-DD HH:MM' local or ISO with offset")
    p.add_argument("--tz", help="Departure timezone (IANA or city). Falls back to trip timezone.")

    # add-hotel
    p = sub.add_parser("add-hotel", help="Add a hotel to a trip")
    p.add_argument("--trip", required=True, help="Trip ID")
    p.add_argument("--name", help="Hotel name")
    p.add_argument("--address", help="Hotel address")
    p.add_argument("--checkin", help="Check-in date (YYYY-MM-DD)")
    p.add_argument("--checkout", help="Check-out date (YYYY-MM-DD)")

    # add-reservation
    p = sub.add_parser("add-reservation", help="Add a reservation to a trip")
    p.add_argument("--trip", required=True, help="Trip ID")
    p.add_argument("--name", required=True, help="Venue/event name")
    p.add_argument("--date", help="Date (YYYY-MM-DD)")
    p.add_argument("--time", help="Time (HH:MM, 24h)")
    p.add_argument("--type", help=f"Type: {', '.join(sorted(VALID_RESERVATION_TYPES))}. Auto-inferred if omitted.")
    p.add_argument("--address", help="Venue address")
    p.add_argument("--party-size", dest="party_size", type=int, help="Party size")
    p.add_argument("--confirmation", help="Confirmation number from Resy/OpenTable")
    p.add_argument("--notes", help="Additional notes")

    # add-traveler
    p = sub.add_parser("add-traveler", help="Add a traveler to a trip")
    p.add_argument("--trip", required=True, help="Trip ID")
    p.add_argument("--name", required=True, help="Traveler name")
    p.add_argument("--phone", help="Phone number (e.g., +1XXXXXXXXXX)")
    p.add_argument("--role", help="Role (e.g., primary, companion)")

    # update-trip
    p = sub.add_parser("update-trip", help="Update trip fields")
    p.add_argument("--id", required=True, help="Trip ID")
    p.add_argument("--name", help="New name")
    p.add_argument("--destination", help="New destination")
    p.add_argument("--start-date", dest="start_date", help="New start date (YYYY-MM-DD)")
    p.add_argument("--end-date", dest="end_date", help="New end date (YYYY-MM-DD)")
    p.add_argument("--tz", help="New timezone")
    p.add_argument("--group-chat", dest="group_chat", help="New group chat GUID")
    p.add_argument("--status", help="New status (upcoming/active/completed/cancelled)")

    # update-flight
    p = sub.add_parser("update-flight", help="Update flight fields (clears outbox)")
    p.add_argument("--trip", required=True, help="Trip ID")
    p.add_argument("--flight", required=True, help="Current flight number to update")
    p.add_argument("--traveler", help="New traveler name")
    p.add_argument("--route", help="New route")
    p.add_argument("--departs", help="New departure time")
    p.add_argument("--arrives", help="New arrival time")
    p.add_argument("--tz", help="Timezone for new times")
    p.add_argument("--new-flight", dest="new_flight", help="New flight number (for rebooking)")

    # update-reservation
    p = sub.add_parser("update-reservation", help="Update reservation fields (clears outbox)")
    p.add_argument("--trip", required=True, help="Trip ID")
    p.add_argument("--name", required=True, help="Current reservation name to update")
    p.add_argument("--date", help="New date")
    p.add_argument("--time", help="New time")
    p.add_argument("--type", help="New type")
    p.add_argument("--address", help="New address")
    p.add_argument("--party-size", dest="party_size", type=int, help="New party size")
    p.add_argument("--confirmation", help="New confirmation number")
    p.add_argument("--notes", help="New notes")
    p.add_argument("--new-name", dest="new_name", help="Rename the reservation")

    # cancel-reservation
    p = sub.add_parser("cancel-reservation", help="Cancel a reservation (does not delete)")
    p.add_argument("--trip", required=True, help="Trip ID")
    p.add_argument("--name", required=True, help="Reservation name to cancel")

    # view
    p = sub.add_parser("view", help="Show full trip context")
    p.add_argument("--trip", required=True, help="Trip ID")

    # list-trips
    p = sub.add_parser("list-trips", help="List all trips")
    p.add_argument("--status", help="Filter by status (upcoming/active/completed/cancelled)")

    return parser


# --- Main ---

def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "add-trip": cmd_add_trip,
        "add-flight": cmd_add_flight,
        "add-hotel": cmd_add_hotel,
        "add-reservation": cmd_add_reservation,
        "add-traveler": cmd_add_traveler,
        "update-trip": cmd_update_trip,
        "update-flight": cmd_update_flight,
        "update-reservation": cmd_update_reservation,
        "cancel-reservation": cmd_cancel_reservation,
        "view": cmd_view,
        "list-trips": cmd_list_trips,
    }

    fn = commands.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
