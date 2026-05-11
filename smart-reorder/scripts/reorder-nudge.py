#!/usr/bin/env python3
"""
Reorder nudge — iMessages when household items are due for reorder.

Runs purchase-cadence.py to compute due items across configured sources,
dedupes against the reorder_notifications table so the same item isn't
re-announced until it's purchased again, and ships a single iMessage via
the outbox.

Fires from launchctl (com.spratt.reorder-nudge.plist) on Wed + Sat 8am PT.
Silent when nothing newly due. On unhandled error, posts the failure to
the outbox so the signal reaches Manan's phone.
"""

import json
import os
import sqlite3
import subprocess
import sys
import traceback
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
ORDERS_DB = f"{HOME}/.config/spratt/db/orders.sqlite"
CADENCE = f"{HOME}/.config/spratt/infrastructure/orders/purchase-cadence.py"
OUTBOX = f"{HOME}/.config/spratt/infrastructure/outbox/outbox.py"

RECIPIENT = "+13157082088"
SOURCES = "instacart,amazon"
SOURCE_TAG = "reorder-nudge"


def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reorder_notifications (
            canonical_key TEXT PRIMARY KEY,
            notified_at TEXT NOT NULL,
            last_purchased_at_notify TEXT NOT NULL
        )
    """)
    conn.commit()


def fetch_due_items():
    result = subprocess.run(
        ["python3", CADENCE,
         "--sources", SOURCES,
         "--due-only", "--format", "json"],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout or "[]")


def filter_new_due(items, conn):
    """Keep only items that are status=due AND either never-notified-before
    or have been purchased again since the last notification."""
    new = []
    for item in items:
        if item.get("status") != "due":
            continue
        key = item["canonical_key"]
        last_purchased = item["last_purchased"]
        row = conn.execute(
            "SELECT last_purchased_at_notify FROM reorder_notifications "
            "WHERE canonical_key = ?",
            (key,),
        ).fetchone()
        if row is None or row[0] < last_purchased:
            new.append(item)
    return new


def record_notifications(items, conn):
    now = datetime.now(timezone.utc).isoformat()
    for item in items:
        conn.execute(
            """INSERT INTO reorder_notifications
                 (canonical_key, notified_at, last_purchased_at_notify)
               VALUES (?, ?, ?)
               ON CONFLICT(canonical_key) DO UPDATE SET
                   notified_at = excluded.notified_at,
                   last_purchased_at_notify = excluded.last_purchased_at_notify""",
            (item["canonical_key"], now, item["last_purchased"]),
        )
    conn.commit()


def compose(items):
    lines = ["🛒 Time to buy:"]
    for it in items:
        lines.append(
            f"• {it['item']} — {it['days_since']}d since last "
            f"(cadence {it['cadence_days']}d)"
        )
    return "\n".join(lines)


def send_outbox(body, source):
    subprocess.run(
        ["python3", OUTBOX, "schedule",
         "--to", RECIPIENT,
         "--body", body,
         "--at", "now",
         "--source", source,
         "--created-by", SOURCE_TAG],
        check=True,
    )


def main():
    try:
        items = fetch_due_items()
        conn = sqlite3.connect(ORDERS_DB)
        try:
            ensure_schema(conn)
            new_due = filter_new_due(items, conn)
            if not new_due:
                print(f"[{datetime.now().isoformat()}] no new due items "
                      f"({len(items)} due total, all already notified)")
                return
            body = compose(new_due)
            send_outbox(body, SOURCE_TAG)
            record_notifications(new_due, conn)
            print(f"[{datetime.now().isoformat()}] notified {len(new_due)} item(s): "
                  f"{', '.join(i['canonical_key'] for i in new_due)}")
        finally:
            conn.close()
    except Exception as e:
        err_body = (
            f"⚠️ reorder-nudge failed: {type(e).__name__}: {e}\n\n"
            f"{traceback.format_exc()[:800]}"
        )
        try:
            send_outbox(err_body, f"{SOURCE_TAG}:error")
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
