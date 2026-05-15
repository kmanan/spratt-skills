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

# launchd Weekday: Sun=0, Mon=1, ..., Sat=6. Python weekday(): Mon=0..Sun=6.
# Cart-build runs Wed (launchd 3 == Python 2) and Sat (launchd 6 == Python 5).
CART_BUILD_DAYS_PY = {2, 5}

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
CART_BUILD_SCRIPT = f"{HOME}/.config/spratt/infrastructure/instacart/cart-build.py"
# 12 items × ~60s CLI timeout each is the worst case; pad to 6 min for the
# self-heal subprocess so a hung CLI can't block the user-facing nudge forever.
CART_BUILD_SELF_HEAL_TIMEOUT_SEC = 360


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
    failed_entries = []
    # Reject dry-run/phantom-staged entries — they represent cart-build runs
    # that did not actually mutate the cart. Treating them as staged would
    # mislead Manan ("Staged in cart" message with nothing in the cart).
    if (
        cart_status
        and cart_status.get("status") == "ok"
        and not cart_status.get("dry_run")
    ):
        for s in cart_status.get("staged", []):
            if "dry-run" in (s.get("mutation_status") or ""):
                continue
            staged_by_retailer.setdefault(s["retailer_slug"], []).append(s)
        # Surface cart-build's per-item failures in the user-facing message
        # so partial failures don't silently disappear. The cart-build alert
        # only names one example; this adds the full list.
        for e in cart_status.get("errors", []) or []:
            failed_entries.append(e)

    instacart_items = [i for i in new_items if i.get("origin") == "instacart"]
    amazon_items = [i for i in new_items if i.get("origin") == "amazon"]

    lines = []

    if instacart_items:
        if staged_by_retailer:
            lines.append("🛒 Staged in Instacart cart:")
            for retailer, rows in staged_by_retailer.items():
                pretty = ", ".join(f"{r['name']} ×{r['quantity']}" for r in rows)
                lines.append(f"• {retailer.title()} ({len(rows)}): {pretty}")
            if failed_entries:
                lines.append("")
                lines.append(f"⚠️ Failed to stage ({len(failed_entries)}) — buy manually:")
                for e in failed_entries:
                    lines.append(
                        f"• [{e.get('retailer_slug','?')}] {e.get('name','?')}"
                    )
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


def self_heal_cart_build():
    """Run cart-build.py inline when its scheduled cron didn't fire.

    Returns (succeeded, detail) where succeeded is True iff cart-build exited 0
    AND wrote a fresh status file. detail is a human-readable string for the
    alert in either case.
    """
    started = time.time()
    try:
        r = subprocess.run(
            ["python3", CART_BUILD_SCRIPT],
            capture_output=True, text=True,
            timeout=CART_BUILD_SELF_HEAL_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {CART_BUILD_SELF_HEAL_TIMEOUT_SEC}s"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

    elapsed = int(time.time() - started)
    if r.returncode != 0:
        return False, f"exit={r.returncode} after {elapsed}s, stderr={r.stderr.strip()[:200]}"
    # cart-build's own error path also exits non-zero, so reaching here means
    # cart-build wrote a fresh "ok" status. The compose() filters still apply.
    return True, f"cart-build self-heal succeeded in {elapsed}s"


def cart_build_missed_fire(cart_status, instacart_due_count):
    """On scheduled cart-build days, detect when cart-build didn't run.
    Returns a short alert string or None. Triggers only when there's
    actually something for cart-build to have staged (instacart items
    due) — otherwise a missed cron is indistinguishable from a no-op."""
    if datetime.now().weekday() not in CART_BUILD_DAYS_PY:
        return None
    if instacart_due_count <= 0:
        return None
    if cart_status is None:
        return (
            "⚠️ Cart-build didn't run today (Wed/Sat 7:45am PT) AND inline "
            "self-heal also failed — no fresh status file at "
            f"{CART_BUILD_STATUS}. {instacart_due_count} Instacart item(s) due. "
            "Falling back to legacy 'buy manually' framing. "
            "Check launchctl print gui/$(id -u)/com.spratt.instacart-cart-build."
        )
    if cart_status.get("status") == "ERROR":
        return (
            f"⚠️ Cart-build ran but errored: {cart_status.get('error','?')}. "
            f"{instacart_due_count} Instacart item(s) still due. Falling back."
        )
    return None


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
            instacart_due_count = sum(
                1 for i in new_due if i.get("origin") == "instacart"
            )
            # Self-heal: if today is Wed/Sat (cart-build day), instacart items
            # are due, and cart-build hasn't left a fresh status file, run it
            # inline before composing. Closes the "cron silently didn't fire"
            # gap with a same-process recovery.
            if (
                datetime.now().weekday() in CART_BUILD_DAYS_PY
                and instacart_due_count > 0
                and cart_status is None
            ):
                print(f"[{datetime.now().isoformat()}] no fresh cart-build status; "
                      f"running cart-build inline (self-heal)")
                ok, detail = self_heal_cart_build()
                print(f"[{datetime.now().isoformat()}] self-heal: ok={ok} {detail}")
                if not ok:
                    try:
                        send_outbox(
                            f"⚠️ Cart-build cron missed; inline self-heal also failed: {detail}. "
                            f"{instacart_due_count} Instacart item(s) due. "
                            f"Reorder nudge will use legacy framing.",
                            f"{SOURCE_TAG}:self-heal-failed",
                        )
                    except Exception:
                        print(f"[{datetime.now().isoformat()}] "
                              f"failed to send self-heal-failed alert")
                # Reload status regardless — cart-build may have partially run
                # and written an ERROR or partial-ok status file.
                cart_status = load_recent_cart_status()
            missed_alert = cart_build_missed_fire(cart_status, instacart_due_count)
            if missed_alert:
                try:
                    send_outbox(missed_alert, f"{SOURCE_TAG}:cart-build-missed-fire")
                except Exception:
                    print(f"[{datetime.now().isoformat()}] "
                          f"failed to send cart-build-missed-fire alert")
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
