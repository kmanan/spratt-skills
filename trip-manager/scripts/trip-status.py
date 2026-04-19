#!/usr/bin/env python3
"""
trip-status.py — Daily trip status transitions + outbox cleanup.

Runs via exec cron at midnight. No LLM.

Transitions:
  upcoming → active   (when start_date <= today <= end_date)
  active → completed  (when end_date < today)

On completion: cancels pending outbox messages for that trip.

Exit codes:
  0 = success (even if no transitions)
  1 = error (logged)
"""

import sys
import os
import sqlite3
import json
import logging
import subprocess
import urllib.request
from logging.handlers import RotatingFileHandler
from datetime import date

# ─── Config ───

TRIPS_DB = os.path.expanduser("~/.config/spratt/db/trips.sqlite")
OUTBOX_DB = os.path.expanduser("~/.config/spratt/db/outbox.sqlite")
LOG_FILE = os.path.expanduser("~/Library/Logs/spratt/trip-status.log")


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
    format="%(asctime)s [trip-status] %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=2),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


OUTBOX_CLI = os.path.expanduser("~/.config/spratt/infrastructure/outbox/outbox.py")
MANAN = "Manan"  # resolved by outbox.py via contacts.sqlite


def generate_trip_summary(trip_id):
    """Query trip data from DB and schedule a summary message via Haiku."""
    require_db_file(TRIPS_DB, "trips")
    conn = sqlite3.connect(TRIPS_DB)
    conn.row_factory = sqlite3.Row

    trip = conn.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()
    if not trip:
        conn.close()
        return

    flights = conn.execute(
        "SELECT traveler, flight_number, route, departs_utc, arrives_utc, status "
        "FROM flights WHERE trip_id = ? ORDER BY departs_utc", (trip_id,)
    ).fetchall()

    hotels = conn.execute(
        "SELECT name, address, check_in, check_out FROM hotels WHERE trip_id = ?", (trip_id,)
    ).fetchall()

    reservations = conn.execute(
        "SELECT type, name, date, time, address FROM reservations WHERE trip_id = ? ORDER BY date, time",
        (trip_id,)
    ).fetchall()

    conn.close()

    # Build data summary for Haiku
    data = {
        "trip": dict(trip),
        "flights": [dict(f) for f in flights],
        "hotels": [dict(h) for h in hotels],
        "reservations": [dict(r) for r in reservations],
    }

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log.warning(f"ANTHROPIC_API_KEY not set, skipping summary for {trip_id}")
        return

    prompt = (
        f"Compose a short, warm trip recap message for a group chat. "
        f"Use emojis as visual markers. Keep it to 5-8 lines max. "
        f"Include highlights: where they went, what they did, flights taken. "
        f"End with a warm sign-off from Spratt.\n\n"
        f"Trip data:\n{json.dumps(data, indent=2, default=str)}"
    )

    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 512,
        "messages": [{"role": "user", "content": prompt}],
        "system": "You are Spratt, a household butler. Compose a trip recap message. Return ONLY the message text, no JSON wrapping.",
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

    resp = urllib.request.urlopen(req, timeout=30)
    result = json.loads(resp.read())
    summary = result["content"][0]["text"].strip()

    # Use group chat from trip DB, fall back to Manan
    group_chat = trip["group_chat_guid"]
    recipient = group_chat if group_chat else MANAN

    # Schedule via outbox
    subprocess.run(
        [
            sys.executable, OUTBOX_CLI,
            "schedule",
            "--to", recipient,
            "--body", summary,
            "--at", "now",
            "--source", f"trip:{trip_id}:summary",
            "--created-by", "trip-status",
        ],
        capture_output=True, text=True, timeout=10,
    )
    log.info(f"trip summary scheduled for {trip_id} to {recipient}")


def run():
    # ─── Open trips database ───
    if not os.path.exists(TRIPS_DB):
        log.error(f"cannot open trips.sqlite: file not found at {TRIPS_DB}")
        return 1

    try:
        trips_conn = sqlite3.connect(TRIPS_DB)
        trips_conn.row_factory = sqlite3.Row
    except Exception as e:
        log.error(f"cannot open trips.sqlite: {e}")
        return 1

    today = date.today().isoformat()
    transitions = 0
    completed_trips = []

    try:
        # ─── upcoming → active ───
        upcoming = trips_conn.execute(
            "SELECT id, status FROM trips WHERE status = 'upcoming' AND start_date <= ? AND end_date >= ?",
            (today, today),
        ).fetchall()

        for row in upcoming:
            trips_conn.execute(
                "UPDATE trips SET status = 'active', updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                (row["id"],),
            )
            log.info(f"{row['id']}: upcoming → active")
            transitions += 1

        # ─── active → completed ───
        active = trips_conn.execute(
            "SELECT id, status FROM trips WHERE status = 'active' AND end_date < ?",
            (today,),
        ).fetchall()

        for row in active:
            trips_conn.execute(
                "UPDATE trips SET status = 'completed', updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                (row["id"],),
            )
            log.info(f"{row['id']}: active → completed")
            transitions += 1
            completed_trips.append(row["id"])

        trips_conn.commit()
    except Exception as e:
        trips_conn.rollback()
        log.error(f"SQL failed on trips.sqlite: {e}")
        trips_conn.close()
        return 1

    trips_conn.close()

    # ─── Generate trip summaries for completed trips ───
    for trip_id in completed_trips:
        try:
            generate_trip_summary(trip_id)
        except Exception as e:
            log.error(f"summary generation failed for {trip_id}: {e}")

    # ─── Cancel pending outbox messages for completed trips ───
    if completed_trips:
        if not os.path.exists(OUTBOX_DB):
            log.error(f"cannot open outbox.sqlite: file not found at {OUTBOX_DB}")
            return 1

        try:
            outbox_conn = sqlite3.connect(OUTBOX_DB)
            for trip_id in completed_trips:
                cur = outbox_conn.execute(
                    "UPDATE messages SET status = 'cancelled', updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
                    "WHERE trip_id = ? AND status = 'pending'",
                    (trip_id,),
                )
                if cur.rowcount > 0:
                    log.info(f"cancelled {cur.rowcount} pending outbox messages for completed trip {trip_id}")
            outbox_conn.commit()
            outbox_conn.close()
        except Exception as e:
            log.error(f"SQL failed on outbox.sqlite: {e}")
            return 1

    if transitions > 0:
        log.info(f"OK: {transitions} trips transitioned")
    else:
        log.info("OK: no status changes")

    return 0


def main():
    sys.exit(run())


if __name__ == "__main__":
    main()
