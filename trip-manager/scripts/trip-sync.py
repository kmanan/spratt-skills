#!/usr/bin/env python3
"""
trip-sync.py — Extract structured trip data from manifests -> upsert tables.

Uses Claude Haiku to read the manifest body and extract structured JSON.
No frontmatter needed. The LLM writes naturally, this script extracts.

Two modes:
  1. Single file:    trip-sync.py <manifest-path>
  2. Directory scan:  trip-sync.py --scan
     Scans TRIPS_DIR for .md files modified since last sync.
     Triggered automatically by launchd WatchPaths on any file change.

Exit codes:
  0 = success (or no changes in scan mode)
  1 = error (logged, no DB changes for the failed manifest)
"""

import sys
import os
import re
import json
import sqlite3
import logging
import urllib.request
import urllib.error
from logging.handlers import RotatingFileHandler
from datetime import datetime, date, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

# --- Config ---

TRIPS_DB = os.path.expanduser("~/.config/spratt/trips/trips.sqlite")
TRIPS_DIR = os.path.expanduser("~/.config/spratt/memory/trips")
LAST_SYNC_FILE = os.path.expanduser("~/.config/spratt/trips/.last-sync")
LOG_FILE = os.path.expanduser("~/Library/Logs/spratt/trip-sync.log")

# Claude Haiku API — reliable structured extraction with guaranteed valid JSON
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

KNOWN_TIMEZONES = {
    "india": "Asia/Kolkata",
    "mumbai": "Asia/Kolkata",
    "delhi": "Asia/Kolkata",
    "bangalore": "Asia/Kolkata",
    "seattle": "America/Los_Angeles",
    "redmond": "America/Los_Angeles",
    "portland": "America/Los_Angeles",
    "chicago": "America/Chicago",
    "new york": "America/New_York",
    "nyc": "America/New_York",
    "new jersey": "America/New_York",
    "jersey city": "America/New_York",
    "dc": "America/New_York",
    "washington": "America/New_York",
    "washington dc": "America/New_York",
}

EXTRACTION_PROMPT = """Extract trip data as JSON. Only extract what's explicitly stated — use null for missing fields. Timezone = IANA name from destination city. Flight times = ISO 8601 with UTC offset (ET in April = -04:00, PT = -07:00). Reservation times in 24h format (18:45).

{"trip_id":"str","name":"str|null","travelers":"str|null","destination":"str|null","timezone":"IANA|null","start_date":"YYYY-MM-DD|null","end_date":"YYYY-MM-DD|null","flights":[{"traveler":"str|null","flight_number":"str","route":"ABC -> DEF","departs":"ISO8601|null","arrives":"ISO8601|null"}],"hotels":[{"name":"str|null","address":"str|null","check_in":"YYYY-MM-DD|null","check_out":"YYYY-MM-DD|null"}],"reservations":[{"type":"dinner|brunch|activity|tour","name":"str","date":"YYYY-MM-DD","time":"HH:MM","address":"str|null"}]}

MANIFEST:
"""

# --- Logging ---

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [trip-sync] %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=2),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# --- LLM Extraction ---

# Claude structured outputs limits union types to 16. Use plain strings with empty = missing.
EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "travelers": {"type": "string"},
        "destination": {"type": "string"},
        "timezone": {"type": "string"},
        "start_date": {"type": "string"},
        "end_date": {"type": "string"},
        "flights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "traveler": {"type": "string"},
                    "flight_number": {"type": "string"},
                    "route": {"type": "string"},
                    "departs": {"type": "string"},
                    "arrives": {"type": "string"},
                },
                "required": ["traveler", "flight_number", "route", "departs", "arrives"],
                "additionalProperties": False,
            },
        },
        "hotels": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "address": {"type": "string"},
                    "check_in": {"type": "string"},
                    "check_out": {"type": "string"},
                },
                "required": ["name", "address", "check_in", "check_out"],
                "additionalProperties": False,
            },
        },
        "reservations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "name": {"type": "string"},
                    "date": {"type": "string"},
                    "time": {"type": "string"},
                    "address": {"type": "string"},
                },
                "required": ["type", "name", "date", "time", "address"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["name", "travelers", "destination", "timezone", "start_date", "end_date", "flights", "hotels", "reservations"],
    "additionalProperties": False,
}


def extract_from_manifest(manifest_path):
    """Read manifest, send to Haiku with structured outputs, return structured dict."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    with open(manifest_path, "r") as f:
        content = f.read()

    # Strip frontmatter if present (we're moving away from it but don't break on it)
    content = re.sub(r"^---\s*\n.*?\n---\s*\n", "", content, count=1, flags=re.DOTALL)

    if len(content.strip()) < 20:
        return None  # Empty or trivial file, skip

    # trip_id is ALWAYS the filename without extension — deterministic, not LLM-generated
    trip_id = os.path.splitext(os.path.basename(manifest_path))[0]

    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 8192,
        "messages": [
            {
                "role": "user",
                "content": EXTRACTION_PROMPT + content,
            }
        ],
        "system": "Extract trip data from the manifest. Only extract what is explicitly stated — use null for missing fields.",
        "output_config": {
            "format": {
                "type": "json_schema",
                "schema": EXTRACTION_SCHEMA,
            }
        },
    }

    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        raise RuntimeError(f"Haiku API call failed: {e}")

    # Check stop reason — if max_tokens, the output was truncated
    stop_reason = result.get("stop_reason", "")
    if stop_reason == "max_tokens":
        raise RuntimeError(
            f"Haiku response truncated (max_tokens reached). "
            f"Manifest may be too complex. Input: {len(content)} chars"
        )

    # Extract text from response
    try:
        text = result["content"][0]["text"]
    except (KeyError, IndexError):
        raise RuntimeError(f"Unexpected Haiku response: {json.dumps(result)[:300]}")

    try:
        data = json.loads(text)
        # Override trip_id with deterministic filename-based ID
        data["trip_id"] = trip_id
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Haiku returned invalid JSON: {e}\nResponse: {text[:300]}")

    return data


# --- Timezone Resolution ---

def resolve_timezone(tz_raw, destination):
    """Resolve timezone. Returns IANA name or None."""
    if tz_raw:
        try:
            ZoneInfo(tz_raw)
            return tz_raw
        except (KeyError, Exception):
            pass

    search = (destination or "").lower().strip()
    for keyword, tz in KNOWN_TIMEZONES.items():
        if keyword in search:
            return tz

    if tz_raw:
        for keyword, tz in KNOWN_TIMEZONES.items():
            if keyword in tz_raw.lower().strip():
                return tz

    return None


# --- Validation ---

def validate_extracted(data, manifest_path):
    """Validate extracted data. Returns (trip_dict, flights_list, hotels_list, reservations_list, error)."""
    if not data or not isinstance(data, dict):
        return None, None, None, None, "extraction returned empty or non-dict"

    trip_id = data.get("trip_id")
    if not trip_id:
        return None, None, None, None, "no trip_id extracted"

    # Resolve timezone
    tz_raw = data.get("timezone")
    destination = data.get("destination")
    tz_name = resolve_timezone(tz_raw, destination)

    # Validate dates if present
    start_str = data.get("start_date")
    end_str = data.get("end_date")
    start = None
    end = None

    if start_str:
        try:
            start = date.fromisoformat(str(start_str))
            start_str = str(start_str)
        except ValueError:
            start_str = None
    if end_str:
        try:
            end = date.fromisoformat(str(end_str))
            end_str = str(end_str)
        except ValueError:
            end_str = None

    # Compute status
    status = "upcoming"
    today = date.today()
    if start and end:
        if start <= today <= end:
            status = "active"
        elif today < start:
            status = "upcoming"
        else:
            status = "completed"
    elif start and not end:
        status = "active" if start <= today else "upcoming"

    # Compute UTC offset
    tz_utc_offset = None
    if tz_name and start:
        try:
            tz = ZoneInfo(tz_name)
            dt = datetime(start.year, start.month, start.day, 12, 0, 0, tzinfo=tz)
            offset = dt.strftime("%z")
            tz_utc_offset = f"{offset[:3]}:{offset[3:]}"
        except Exception:
            pass

    trip = {
        "id": str(trip_id),
        "name": str(data["name"]) if data.get("name") else None,
        "travelers": str(data["travelers"]) if data.get("travelers") else None,
        "destination": str(destination) if destination else None,
        "timezone": tz_name,
        "tz_utc_offset": tz_utc_offset,
        "start_date": start_str,
        "end_date": end_str,
        "status": status,
        "manifest_path": manifest_path,
    }

    # Flights
    flights = []
    for f in data.get("flights") or []:
        if not isinstance(f, dict):
            continue
        fn = f.get("flight_number")
        if not fn:
            continue
        flights.append({
            "trip_id": str(trip_id),
            "traveler": str(f["traveler"]) if f.get("traveler") else None,
            "flight_number": str(fn),
            "route": str(f["route"]) if f.get("route") else None,
            "departs_utc": str(f["departs"]) if f.get("departs") else None,
            "arrives_utc": str(f["arrives"]) if f.get("arrives") else None,
        })

    # Hotels
    hotels = []
    for h in data.get("hotels") or []:
        if not isinstance(h, dict):
            continue
        hotels.append({
            "trip_id": str(trip_id),
            "name": str(h["name"]) if h.get("name") else None,
            "address": str(h["address"]) if h.get("address") else None,
            "check_in": str(h["check_in"]) if h.get("check_in") else None,
            "check_out": str(h["check_out"]) if h.get("check_out") else None,
        })

    # Reservations
    reservations = []
    for r in data.get("reservations") or []:
        if not isinstance(r, dict):
            continue
        if not r.get("name"):
            continue
        reservations.append({
            "trip_id": str(trip_id),
            "type": str(r.get("type", "dinner")) if r.get("type") else "dinner",
            "name": str(r["name"]),
            "date": str(r["date"]) if r.get("date") else None,
            "time": str(r["time"]) if r.get("time") else None,
            "address": str(r["address"]) if r.get("address") else None,
        })

    return trip, flights, hotels, reservations, None


# --- Database ---

def upsert_trip(conn, trip):
    """Upsert a trip row."""
    existing = conn.execute("SELECT status FROM trips WHERE id = ?", (trip["id"],)).fetchone()
    if existing and existing[0] == "cancelled":
        log.info(f"trip {trip['id']} is cancelled — skipping upsert (manual override respected)")
        return False

    conn.execute("""
        INSERT INTO trips (id, name, travelers, destination, timezone, tz_utc_offset,
                          start_date, end_date, status, manifest_path, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        ON CONFLICT(id) DO UPDATE SET
            name = COALESCE(excluded.name, trips.name),
            travelers = COALESCE(excluded.travelers, trips.travelers),
            destination = COALESCE(excluded.destination, trips.destination),
            timezone = COALESCE(excluded.timezone, trips.timezone),
            tz_utc_offset = COALESCE(excluded.tz_utc_offset, trips.tz_utc_offset),
            start_date = COALESCE(excluded.start_date, trips.start_date),
            end_date = COALESCE(excluded.end_date, trips.end_date),
            status = excluded.status,
            manifest_path = excluded.manifest_path,
            updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
    """, (
        trip["id"], trip["name"], trip["travelers"], trip["destination"],
        trip["timezone"], trip["tz_utc_offset"], trip["start_date"],
        trip["end_date"], trip["status"], trip["manifest_path"],
    ))
    return True


def sync_flights(conn, trip_id, flights):
    """Sync flights. Detect changes, clear outbox_msg_id on data change."""
    if not flights:
        return 0

    existing = {}
    for row in conn.execute(
        "SELECT flight_number, departs_utc, arrives_utc, traveler, route FROM flights WHERE trip_id = ?",
        (trip_id,),
    ).fetchall():
        existing[row[0]] = {"departs_utc": row[1], "arrives_utc": row[2], "traveler": row[3], "route": row[4]}

    manifest_flight_numbers = set()
    count = 0

    for f in flights:
        manifest_flight_numbers.add(f["flight_number"])
        old = existing.get(f["flight_number"])

        if old:
            # Check if data changed
            changed = (
                old["departs_utc"] != f["departs_utc"]
                or old["arrives_utc"] != f["arrives_utc"]
                or old["traveler"] != f["traveler"]
                or old["route"] != f["route"]
            )
            if changed:
                conn.execute("""
                    UPDATE flights SET
                        traveler = ?, route = ?, departs_utc = ?, arrives_utc = ?,
                        status = CASE WHEN status = 'cancelled' THEN 'scheduled' ELSE status END,
                        outbox_msg_id = NULL, outbox_generated_at = NULL,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                    WHERE trip_id = ? AND flight_number = ?
                """, (f["traveler"], f["route"], f["departs_utc"], f["arrives_utc"], trip_id, f["flight_number"]))
                log.info(f"flight {f['flight_number']} data changed — outbox will regenerate")
            # If not changed, don't touch the row at all
        else:
            conn.execute("""
                INSERT INTO flights (trip_id, traveler, flight_number, route, departs_utc, arrives_utc)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (f["trip_id"], f["traveler"], f["flight_number"], f["route"], f["departs_utc"], f["arrives_utc"]))
            log.info(f"flight {f['flight_number']} added")
        count += 1

    removed = set(existing.keys()) - manifest_flight_numbers
    for fn in removed:
        conn.execute(
            "UPDATE flights SET status = 'cancelled', updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE trip_id = ? AND flight_number = ?", (trip_id, fn))
        log.info(f"flight {fn} removed from manifest — marked cancelled")

    return count


def sync_hotels(conn, trip_id, hotels):
    """Sync hotels. Compare before replacing to preserve outbox tracking."""
    if not hotels:
        return 0

    old_hotels = conn.execute(
        "SELECT name, address, check_in, check_out FROM hotels WHERE trip_id = ?", (trip_id,)
    ).fetchall()
    old_set = {(r[0], r[1], r[2], r[3]) for r in old_hotels}
    new_set = {(h["name"], h["address"], h["check_in"], h["check_out"]) for h in hotels}

    if old_set == new_set:
        return 0  # No change

    conn.execute("DELETE FROM hotels WHERE trip_id = ?", (trip_id,))
    for h in hotels:
        conn.execute("""
            INSERT INTO hotels (trip_id, name, address, check_in, check_out)
            VALUES (?, ?, ?, ?, ?)
        """, (h["trip_id"], h["name"], h["address"], h["check_in"], h["check_out"]))
    log.info(f"hotels updated for {trip_id}")
    return len(hotels)


def sync_reservations(conn, trip_id, reservations):
    """Sync reservations. Match on name+date, detect changes."""
    if not reservations:
        return 0

    existing = {}
    for row in conn.execute(
        "SELECT id, name, date, time, address, type FROM reservations WHERE trip_id = ?", (trip_id,)
    ).fetchall():
        key = (row[1], row[2])  # name + date
        existing[key] = {"id": row[0], "time": row[3], "address": row[4], "type": row[5]}

    manifest_keys = set()
    count = 0

    for r in reservations:
        key = (r["name"], r["date"])
        manifest_keys.add(key)
        old = existing.get(key)

        if old:
            changed = old["time"] != r["time"] or old["address"] != r["address"] or old["type"] != r["type"]
            if changed:
                conn.execute("""
                    UPDATE reservations SET type = ?, time = ?, address = ?,
                        outbox_msg_id = NULL, outbox_generated_at = NULL,
                        updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                    WHERE id = ?
                """, (r["type"], r["time"], r["address"], old["id"]))
                log.info(f"reservation {r['name']} on {r['date']} changed — outbox will regenerate")
        else:
            conn.execute("""
                INSERT INTO reservations (trip_id, type, name, date, time, address)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (r["trip_id"], r["type"], r["name"], r["date"], r["time"], r["address"]))
            log.info(f"reservation added: {r['name']} on {r['date']}")
        count += 1

    return count


# --- Post-Sync Outbox Generation (delegates to trip-outbox-gen.py) ---

_trip_dir = os.path.dirname(os.path.abspath(__file__))
if _trip_dir not in sys.path:
    sys.path.insert(0, _trip_dir)
import importlib
_outbox_gen = importlib.import_module("trip-outbox-gen")
_flight_state = importlib.import_module("trip-flight-state")


def generate_outbox_messages(trip_id):
    """Delegate outbox generation to trip-outbox-gen.py (single source of truth)."""
    return _outbox_gen.generate_for_trip(trip_id)


def sync_flight_monitor_state(trip_id):
    """Update flight monitor state.json so new flights get tracked automatically."""
    try:
        a, u, r = _flight_state.sync_trip_flights(trip_id)
        if a > 0 or u > 0 or r > 0:
            log.info(f"flight monitor state: {a} added, {u} updated, {r} removed")
    except Exception as e:
        log.warning(f"flight state sync failed (non-fatal): {e}")


# --- Sync One Manifest ---

def alert_failure(manifest_path, error_msg):
    """Send failure alert to owner via outbox."""
    try:
        import subprocess
        short_name = os.path.basename(manifest_path)
        body = f"[trip-sync] extraction failed for {short_name}: {str(error_msg)[:200]}"
        subprocess.run(
            [
                "python3",
                os.path.expanduser("~/.config/spratt/infrastructure/outbox/outbox.py"),
                "schedule",
                "--to", "+1XXXXXXXXXX",  # Replace with your phone number
                "--body", body,
                "--at", "now",
                "--source", "system:trip-sync-alert",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        pass  # Don't let alerting failure mask the original error


def sync_one(manifest_path):
    """Sync a single manifest file. Returns True on success, False on error."""
    try:
        data = extract_from_manifest(manifest_path)
    except RuntimeError as e:
        log.error(f"extraction failed for {manifest_path}: {e}")
        alert_failure(manifest_path, e)
        return False
    except Exception as e:
        log.error(f"unexpected error reading {manifest_path}: {e}")
        alert_failure(manifest_path, e)
        return False

    if data is None:
        # Empty/trivial file — skip silently
        return True

    trip, flights, hotels, reservations, error = validate_extracted(data, manifest_path)
    if error:
        log.error(f"manifest {manifest_path} validation failed: {error}")
        return False

    conn = sqlite3.connect(TRIPS_DB)
    try:
        trip_ok = upsert_trip(conn, trip)
        flight_count = sync_flights(conn, trip["id"], flights) if trip_ok else 0
        hotel_count = sync_hotels(conn, trip["id"], hotels) if trip_ok else 0
        resv_count = sync_reservations(conn, trip["id"], reservations) if trip_ok else 0
        conn.commit()

        # Post-sync: generate outbox messages + update flight monitor
        outbox_count = 0
        if trip_ok:
            outbox_count = generate_outbox_messages(trip["id"])
            if flight_count > 0:
                sync_flight_monitor_state(trip["id"])
    except Exception as e:
        conn.rollback()
        log.error(f"failed to write to trips.sqlite: {e}")
        conn.close()
        return False

    conn.close()

    parts = [f"upserted trip {trip['id']} — {trip.get('destination')}, {trip.get('timezone')}, {trip['status']}"]
    if flight_count > 0:
        parts.append(f"{flight_count} flights")
    if hotel_count > 0:
        parts.append(f"{hotel_count} hotels")
    if resv_count > 0:
        parts.append(f"{resv_count} reservations")
    if outbox_count > 0:
        parts.append(f"{outbox_count} outbox messages created")
    log.info(f"OK: {', '.join(parts)}")
    return True


# --- Directory Scan Mode ---

def get_last_sync_time():
    try:
        return os.path.getmtime(LAST_SYNC_FILE)
    except OSError:
        return 0


def touch_last_sync():
    os.makedirs(os.path.dirname(LAST_SYNC_FILE), exist_ok=True)
    with open(LAST_SYNC_FILE, "w") as f:
        f.write("")


def scan_directory():
    """Scan TRIPS_DIR for manifests modified since last sync. Returns exit code."""
    if not os.path.isdir(TRIPS_DIR):
        log.error(f"trips directory not found: {TRIPS_DIR}")
        return 1

    last_sync = get_last_sync_time()
    changed = []

    for fname in os.listdir(TRIPS_DIR):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(TRIPS_DIR, fname)
        if os.path.getmtime(fpath) > last_sync:
            changed.append(fpath)

    if not changed:
        return 0

    errors = 0
    for path in changed:
        if not sync_one(path):
            errors += 1

    touch_last_sync()

    if errors > 0:
        log.error(f"scan completed with {errors} error(s) out of {len(changed)} manifests")
        return 1

    log.info(f"scan: {len(changed)} manifest(s) synced")
    return 0


# --- Main ---

def main():
    if len(sys.argv) < 2:
        print("Usage: trip-sync.py <manifest-path>  OR  trip-sync.py --scan", file=sys.stderr)
        sys.exit(1)

    if sys.argv[1] == "--scan":
        sys.exit(scan_directory())
    else:
        manifest_path = sys.argv[1]
        if not os.path.exists(manifest_path):
            log.error(f"manifest not found: {manifest_path}")
            sys.exit(1)
        sys.exit(0 if sync_one(manifest_path) else 1)


if __name__ == "__main__":
    main()
