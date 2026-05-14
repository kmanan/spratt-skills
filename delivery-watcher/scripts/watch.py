#!/usr/bin/env python3
"""
delivery-watcher
================

KeepAlive daemon. Every 30s:
  1. Polls orders.sqlite for new (source IN amazon,instacart, tracking_status='delivered') rows.
  2. Records them in deliveries.sqlite for dedup.
  3. For deliveries where T+5min has passed, checks binary_sensor.front_door_2
     in Home Assistant. If last_changed >= delivery_ts → assume picked up, silent.
     Otherwise → outbox text to Manan + Wife (separate messages).

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

HA_CONFIG_PATH    = Path.home() / ".config/home-assistant/config.json"
DOOR_ENTITY       = "binary_sensor.front_door_2"

ORDERS_DB         = Path.home() / ".config/spratt/db/orders.sqlite"
DELIVERIES_DB     = Path.home() / ".config/spratt/db/deliveries.sqlite"
OUTBOX_CLI        = Path.home() / ".config/spratt/infrastructure/outbox/outbox.py"

MANAN_PHONE       = "+13157082088"
HARSHITA_PHONE    = "+13129330988"

GATE_SECONDS      = 5 * 60     # 5 min after delivery before texting
POLL_SECONDS      = 30
FIRST_RUN_GRACE   = 60 * 60    # rows older than 1h on first run = silently backfill

# ---------- DB setup ----------

DELIVERIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS deliveries (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    order_pk             INTEGER NOT NULL UNIQUE,
    source               TEXT NOT NULL,
    order_id             TEXT,
    delivery_ts          INTEGER NOT NULL,
    detected_at          INTEGER NOT NULL,
    notified             INTEGER NOT NULL DEFAULT 0,
    skipped              INTEGER NOT NULL DEFAULT 0,
    skip_reason          TEXT,
    door_last_changed_at TEXT,
    body                 TEXT,
    notified_at          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pending ON deliveries(notified, delivery_ts);
"""

def db_init():
    DELIVERIES_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DELIVERIES_DB) as conn:
        conn.executescript(DELIVERIES_SCHEMA)

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
    # ISO 8601 — fromisoformat handles +00:00; convert 'Z' to +00:00
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))

# ---------- orders polling ----------

def parse_ts(iso: str) -> int:
    """Parse ISO 8601 timestamp to unix seconds. Returns 0 on bad input."""
    if not iso:
        return 0
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except (ValueError, AttributeError):
        return 0

def poll_new_deliveries():
    """Returns list of (order_pk, source, order_id, delivery_ts, body) tuples for orders
    we haven't seen yet."""
    if not ORDERS_DB.exists():
        return []
    with sqlite3.connect(ORDERS_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, source, order_id, store, items, total, tracking_updated_at
              FROM orders
             WHERE tracking_status='delivered'
               AND source IN ('amazon','instacart')
               AND tracking_updated_at IS NOT NULL
        """).fetchall()
    with sqlite3.connect(DELIVERIES_DB) as conn:
        seen = {r[0] for r in conn.execute("SELECT order_pk FROM deliveries")}
    new = []
    for r in rows:
        if r["id"] in seen:
            continue
        ts = parse_ts(r["tracking_updated_at"])
        if ts == 0:
            continue
        new.append({
            "order_pk":    r["id"],
            "source":      r["source"],
            "order_id":    r["order_id"],
            "store":       r["store"],
            "items":       r["items"],
            "total":       r["total"],
            "delivery_ts": ts,
        })
    return new

def record_delivery(row, first_run_backfill=False):
    """Insert into deliveries.sqlite. If first_run_backfill, mark notified=1, skipped=1."""
    body = format_body(row)
    now = int(time.time())
    with sqlite3.connect(DELIVERIES_DB) as conn:
        conn.execute("""
            INSERT OR IGNORE INTO deliveries
                (order_pk, source, order_id, delivery_ts, detected_at,
                 notified, skipped, skip_reason, body)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row["order_pk"], row["source"], row["order_id"],
            row["delivery_ts"], now,
            1 if first_run_backfill else 0,
            1 if first_run_backfill else 0,
            "first-run backfill" if first_run_backfill else None,
            body,
        ))

# ---------- body composition ----------

def format_body(row):
    items = []
    try:
        items = json.loads(row["items"]) if row["items"] else []
    except json.JSONDecodeError:
        items = []
    item_count = len(items)
    total = row.get("total")
    total_str = f"${total:.2f}" if isinstance(total, (int, float)) else ""

    if row["source"] == "instacart":
        store = row.get("store") or "Instacart"
        parts = [store, f"{item_count} item{'s' if item_count != 1 else ''}"]
        if total_str:
            parts.append(total_str)
        return f"📦 Instacart delivered — {', '.join(parts)}. Still on porch."

    # amazon
    if items:
        first = items[0].get("name", "")[:50].strip().rstrip(".,")
        more = f" + {item_count - 1} more" if item_count > 1 else ""
        return f"📦 Amazon delivered: {first}{more}. Still on porch."
    return "📦 Amazon delivered. Still on porch."

# ---------- gate + send ----------

def find_due():
    """Rows where T+GATE_SECONDS has passed and we haven't notified."""
    cutoff = int(time.time()) - GATE_SECONDS
    with sqlite3.connect(DELIVERIES_DB) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("""
            SELECT id, order_pk, source, delivery_ts, body
              FROM deliveries
             WHERE notified=0 AND delivery_ts <= ?
        """, (cutoff,)).fetchall()

def evaluate_and_send(due_row):
    """For one due delivery: check door, send or skip, mark."""
    delivery_ts_utc = datetime.fromtimestamp(due_row["delivery_ts"], tz=timezone.utc)
    door_changed = get_door_last_changed()
    if door_changed is None:
        # HA unreachable — defer (don't mark notified, retry next loop)
        return
    skipped = door_changed >= delivery_ts_utc
    now = int(time.time())
    if skipped:
        mark_done(due_row["id"], skipped=True,
                  skip_reason=f"door changed at {door_changed.isoformat()} >= delivery",
                  door_iso=door_changed.isoformat())
        return
    # send to both
    body = due_row["body"]
    ok_a = send_outbox(MANAN_PHONE, body)
    ok_b = send_outbox(HARSHITA_PHONE, body)
    if not (ok_a and ok_b):
        # leave un-notified so we retry next loop; alert
        alert_failure(f"outbox send failed: manan={ok_a} wife={ok_b}, order_pk={due_row['order_pk']}")
        return
    mark_done(due_row["id"], skipped=False, skip_reason=None,
              door_iso=door_changed.isoformat())

def mark_done(delivery_id, skipped, skip_reason, door_iso):
    with sqlite3.connect(DELIVERIES_DB) as conn:
        conn.execute("""
            UPDATE deliveries
               SET notified=1, skipped=?, skip_reason=?,
                   door_last_changed_at=?, notified_at=?
             WHERE id=?
        """, (1 if skipped else 0, skip_reason, door_iso, int(time.time()), delivery_id))

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
    """On very first run, mark any already-delivered rows that are older than
    FIRST_RUN_GRACE as backfill (silent). Recent ones still get processed."""
    with sqlite3.connect(DELIVERIES_DB) as conn:
        already = conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0]
    if already > 0:
        return  # not first run
    cutoff = int(time.time()) - FIRST_RUN_GRACE
    for row in poll_new_deliveries():
        record_delivery(row, first_run_backfill=row["delivery_ts"] < cutoff)

def loop_once():
    # 1. discover new deliveries
    for row in poll_new_deliveries():
        record_delivery(row)
    # 2. process due ones
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
