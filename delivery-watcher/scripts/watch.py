#!/usr/bin/env python3
"""
delivery-watcher
================

KeepAlive daemon. Reads delivery_signals.sqlite (populated by email-scan when
it sees an Instacart "receipt" or Amazon "Delivered:" email). For each signal
where T+5min has passed since email arrival:
  - Checks binary_sensor.front_door_2 in Home Assistant.
  - If last_changed >= arrived_at → assume picked up, silent.
  - Otherwise → outbox text to Manan + Wife (separate messages).

No mailbox polling here. email-scan is the upstream eye on the mailbox; this
daemon just reacts to its signals. Local SQLite read every 30s is free.

Observability is iMessage-first: any uncaught failure outboxes an alert to Manan.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

# ---------- config ----------

HA_CONFIG_PATH       = Path.home() / ".config/home-assistant/config.json"
DOOR_ENTITY          = "binary_sensor.front_door_2"

DELIVERY_SIGNALS_DB  = Path.home() / ".config/spratt/db/delivery_signals.sqlite"
DELIVERIES_DB        = Path.home() / ".config/spratt/db/deliveries.sqlite"
OUTBOX_CLI           = Path.home() / ".config/spratt/infrastructure/outbox/outbox.py"

MANAN_PHONE          = "+13157082088"
HARSHITA_PHONE       = "+13129330988"

GATE_SECONDS         = 5 * 60
POLL_SECONDS         = 30
FIRST_RUN_GRACE      = 60 * 60   # rows older than 1h on first run = silent backfill

# ---------- DB setup ----------

ALERTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id           TEXT NOT NULL UNIQUE,
    source               TEXT NOT NULL,
    subject              TEXT,
    arrived_at           INTEGER NOT NULL,
    detected_at          INTEGER NOT NULL,
    notified             INTEGER NOT NULL DEFAULT 0,
    skipped              INTEGER NOT NULL DEFAULT 0,
    skip_reason          TEXT,
    door_last_changed_at TEXT,
    body                 TEXT,
    notified_at          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pending ON alerts(notified, arrived_at);
"""

def db_init():
    DELIVERIES_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DELIVERIES_DB) as conn:
        conn.executescript(ALERTS_SCHEMA)

# ---------- HA ----------

def ha_creds():
    with open(HA_CONFIG_PATH) as f:
        cfg = json.load(f)
    return cfg["url"].rstrip("/"), cfg["token"]

def get_door_last_changed():
    """Returns datetime (UTC) of the most recent state change, or None on failure."""
    url, token = ha_creds()
    req = Request(
        f"{url}/api/states/{DOOR_ENTITY}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except (URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"[ha] {DOOR_ENTITY} fetch failed: {e}", file=sys.stderr)
        return None
    iso = data.get("last_changed")
    if not iso:
        return None
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))

# ---------- signal intake ----------

def poll_new_signals():
    """Return signal rows we haven't recorded an alert for yet."""
    if not DELIVERY_SIGNALS_DB.exists():
        return []
    with sqlite3.connect(DELIVERY_SIGNALS_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT message_id, source, subject, arrived_at
              FROM delivery_signals
        """).fetchall()
    with sqlite3.connect(DELIVERIES_DB) as conn:
        seen = {r[0] for r in conn.execute("SELECT message_id FROM alerts")}
    return [dict(r) for r in rows if r["message_id"] not in seen]

def record_alert(sig, first_run_backfill=False):
    body = format_body(sig)
    now = int(time.time())
    with sqlite3.connect(DELIVERIES_DB) as conn:
        conn.execute("""
            INSERT OR IGNORE INTO alerts
                (message_id, source, subject, arrived_at, detected_at,
                 notified, skipped, skip_reason, body)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            sig["message_id"], sig["source"], sig["subject"],
            sig["arrived_at"], now,
            1 if first_run_backfill else 0,
            1 if first_run_backfill else 0,
            "first-run backfill" if first_run_backfill else None,
            body,
        ))

# ---------- body composition ----------

def parse_amazon_subject(subject):
    """Pull product name out of e.g. `Delivered: "Live Conscious Magwell..."`."""
    s = subject
    if s.startswith("Delivered:"):
        s = s[len("Delivered:"):].strip()
    if s.startswith("\"") and "\"" in s[1:]:
        end = s.index("\"", 1)
        return s[1:end].rstrip(".")
    return s.rstrip(".")

def format_body(sig):
    if sig["source"] == "instacart":
        return "📦 Instacart delivered. Still on porch."
    if sig["source"] == "amazon":
        name = parse_amazon_subject(sig.get("subject") or "")
        if name:
            return f"📦 Amazon delivered: {name}. Still on porch."
        return "📦 Amazon delivered. Still on porch."
    return "📦 Delivery — still on porch."

# ---------- gate + send ----------

def find_due():
    cutoff = int(time.time()) - GATE_SECONDS
    with sqlite3.connect(DELIVERIES_DB) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("""
            SELECT id, message_id, source, arrived_at, body
              FROM alerts
             WHERE notified=0 AND arrived_at <= ?
        """, (cutoff,)).fetchall()

def evaluate_and_send(due):
    arrived_utc = datetime.fromtimestamp(due["arrived_at"], tz=timezone.utc)
    door_changed = get_door_last_changed()
    if door_changed is None:
        return
    if door_changed >= arrived_utc:
        mark_done(due["id"], skipped=True,
                  skip_reason=f"door changed at {door_changed.isoformat()} >= arrived",
                  door_iso=door_changed.isoformat())
        return
    body = due["body"]
    ok_a = send_outbox(MANAN_PHONE, body)
    ok_b = send_outbox(HARSHITA_PHONE, body)
    if not (ok_a and ok_b):
        alert_failure(f"outbox send failed: manan={ok_a} wife={ok_b}, msg_id={due['message_id']}")
        return
    mark_done(due["id"], skipped=False, skip_reason=None,
              door_iso=door_changed.isoformat())

def mark_done(alert_id, skipped, skip_reason, door_iso):
    with sqlite3.connect(DELIVERIES_DB) as conn:
        conn.execute("""
            UPDATE alerts
               SET notified=1, skipped=?, skip_reason=?,
                   door_last_changed_at=?, notified_at=?
             WHERE id=?
        """, (1 if skipped else 0, skip_reason, door_iso, int(time.time()), alert_id))

def send_outbox(to, body):
    try:
        subprocess.run([
            "python3", str(OUTBOX_CLI), "schedule",
            "--to", to, "--body", body, "--at", "now",
            "--source", "delivery-watcher",
            "--created-by", "delivery-watcher",
        ], check=True, capture_output=True, timeout=15)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"[outbox] send to {to} failed: {e}", file=sys.stderr)
        return False

def alert_failure(reason):
    body = f"⚠️ delivery-watcher: {reason}"
    print(body, file=sys.stderr)
    send_outbox(MANAN_PHONE, body)

# ---------- main loop ----------

def first_run_backfill():
    with sqlite3.connect(DELIVERIES_DB) as conn:
        already = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    if already > 0:
        return
    cutoff = int(time.time()) - FIRST_RUN_GRACE
    for sig in poll_new_signals():
        record_alert(sig, first_run_backfill=sig["arrived_at"] < cutoff)

def loop_once():
    for sig in poll_new_signals():
        record_alert(sig)
    for due in find_due():
        evaluate_and_send(due)

def main():
    db_init()
    first_run_backfill()
    print(f"[delivery-watcher] running (poll={POLL_SECONDS}s gate={GATE_SECONDS}s)")
    while True:
        try:
            loop_once()
        except Exception as e:
            alert_failure(f"loop crashed: {e!r}")
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
