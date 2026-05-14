#!/usr/bin/env python3
"""
Reorder nudge — iMessages when household items are due for reorder.

Two cadence sources, unioned by canonical_key:
- Amazon: purchase-cadence.py against orders.sqlite (legacy path).
- Instacart: cadence.py against instacart-pp-cli's SQLite DB.

If cart-build.py wrote a recent status file (≤45 min old), the message
summarizes what was staged in Instacart and what still needs manual
attention. Otherwise it falls back to the legacy "time to buy" nudge.

Dedup table `reorder_notifications` in orders.sqlite is shared across
both sources — keyed on `canonical_key` (item_id for instacart, regex
slug for amazon).

Fires from launchctl (com.spratt.reorder-nudge.plist) on Wed + Sat 8am
PT. Silent when nothing newly due. Outbox alert on uncaught failure.
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone

HOME = os.path.expanduser("~")
ORDERS_DB = f"{HOME}/.config/spratt/db/orders.sqlite"
CADENCE_LEGACY = f"{HOME}/.config/spratt/infrastructure/orders/purchase-cadence.py"
CADENCE_INSTACART = f"{HOME}/.config/spratt/infrastructure/instacart/cadence.py"
CART_BUILD_STATUS = f"{HOME}/.config/spratt/infrastructure/launchd-status/instacart-cart-build.json"
OUTBOX = f"{HOME}/.config/spratt/infrastructure/outbox/outbox.py"

RECIPIENT = "+13157082088"
LEGACY_SOURCES = "amazon"
SOURCE_TAG = "reorder-nudge"
CART_STATUS_MAX_AGE_SEC = 45 * 60


def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reorder_notifications (
            canonical_key TEXT PRIMARY KEY,
            notified_at TEXT NOT NULL,
            last_purchased_at_notify TEXT NOT NULL
        )
    """)
    conn.commit()


def run_cadence(script, extra_args):
    cmd = ["python3", script, "--due-only", "--format", "json"] + list(extra_args)
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout or "[]")


def fetch_due_items():
    amazon = run_cadence(CADENCE_LEGACY, ["--sources", LEGACY_SOURCES])
    for it in amazon:
        it["origin"] = "amazon"
    instacart = run_cadence(CADENCE_INSTACART, [])
    for it in instacart:
        it["origin"] = "instacart"
    return amazon + instacart


def filter_new_due(items, conn):
    """Keep status=due items that are either never-notified or have been
    purchased again since the last notification."""
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


def load_recent_cart_status():
    if not os.path.exists(CART_BUILD_STATUS):
        return None
    try:
        mtime = os.path.getmtime(CART_BUILD_STATUS)
    except OSError:
        return None
    if time.time() - mtime > CART_STATUS_MAX_AGE_SEC:
        return None
    try:
        with open(CART_BUILD_STATUS) as f:
            return json.load(f)
    except Exception:
        return None


def compose(new_items, cart_status):
    """Build the iMessage body. Instacart items get a 'staged' framing if
    cart-build ran recently and reported them; amazon stays manual."""
    staged_by_retailer = {}
    if cart_status and cart_status.get("status") == "ok":
        for s in cart_status.get("staged", []):
            staged_by_retailer.setdefault(s["retailer_slug"], []).append(s)

    instacart_items = [i for i in new_items if i.get("origin") == "instacart"]
    amazon_items = [i for i in new_items if i.get("origin") == "amazon"]

    lines = []

    if instacart_items:
        if staged_by_retailer:
            lines.append("🛒 Staged in Instacart cart:")
            for retailer, rows in staged_by_retailer.items():
                pretty = ", ".join(f"{r['name']} ×{r['quantity']}" for r in rows)
                lines.append(f"• {retailer.title()} ({len(rows)}): {pretty}")
            lines.append("Review & check out: https://www.instacart.com/store/checkout")
        else:
            lines.append("🛒 Instacart — due for reorder:")
            for it in instacart_items:
                lines.append(
                    f"• [{it['retailer_slug']}] {it['item']} — "
                    f"{it['days_since']}d (cadence {it['cadence_days']}d)"
                )

    if amazon_items:
        if lines:
            lines.append("")
        lines.append("📦 Amazon — buy manually:")
        for it in amazon_items:
            lines.append(
                f"• {it['item']} — {it['days_since']}d (cadence {it['cadence_days']}d)"
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
            cart_status = load_recent_cart_status()
            body = compose(new_due, cart_status)
            if not body.strip():
                print(f"[{datetime.now().isoformat()}] composed empty body, skipping")
                return
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
