#!/usr/bin/env python3
"""
Instacart purchase cadence — reads instacart-pp-cli's SQLite history DB.

Originally based on the smart-replenishment idea in the `instacart-skill` by
bigdaddyluke on ClawHub (https://clawhub.com/skills/instacart-skill). His
skill was an LLM-driven browser cart-builder; this is the SQL-backed
descendant that runs over canonical Instacart item_ids supplied by
mvanhorn's `instacart-pp-cli`.

Replaces the Instacart half of purchase-cadence.py (which reads
orders.sqlite with regex-normalized item names). Groups by canonical
(item_id, retailer_slug) which is stable across orders, computes median
days between purchases, classifies each as due / soon / not_due.

Output schema is a superset of purchase-cadence.py so reorder-nudge.py
can union both sources by `canonical_key`. Adds retailer_slug, item_id,
product_id, quantity, quantity_type — the fields cart-build.py needs to
call `instacart add --item-id`.
"""

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from statistics import median

HOME = os.path.expanduser("~")
CLI_DB = f"{HOME}/Library/Application Support/instacart/instacart.db"


def fetch_history(db_path, store=None):
    """Return dict keyed by (item_id, retailer_slug) → list of (placed_at_unix,
    name, quantity, quantity_type, product_id). Most recent last."""
    if not os.path.exists(db_path):
        sys.stderr.write(f"FATAL: instacart CLI DB not found at {db_path}\n")
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    q = """
        SELECT o.placed_at, o.retailer_slug,
               oi.item_id, oi.product_id, oi.name, oi.quantity, oi.quantity_type
        FROM order_items oi
        JOIN orders o ON o.order_id = oi.order_id
    """
    params = []
    if store:
        q += " WHERE o.retailer_slug = ?"
        params.append(store)
    q += " ORDER BY o.placed_at"

    rows = conn.execute(q, params).fetchall()
    conn.close()

    history = defaultdict(list)
    for r in rows:
        key = (r["item_id"], r["retailer_slug"])
        history[key].append((
            r["placed_at"],
            r["name"] or "",
            r["quantity"] or 1,
            r["quantity_type"] or "each",
            r["product_id"] or "",
        ))
    return history


def compute_cadence_days(unix_timestamps):
    if len(unix_timestamps) < 2:
        return None
    dates = sorted({
        datetime.fromtimestamp(ts, tz=timezone.utc).date()
        for ts in unix_timestamps
    })
    if len(dates) < 2:
        return None
    gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return None
    return median(gaps)


def analyze(db_path, store=None, min_purchases=2):
    history = fetch_history(db_path, store=store)
    today = date.today()
    results = []

    for (item_id, retailer_slug), events in history.items():
        unix_dates = sorted({
            datetime.fromtimestamp(e[0], tz=timezone.utc).date().isoformat()
            for e in events
        })
        if len(unix_dates) < min_purchases:
            continue

        cadence = compute_cadence_days([e[0] for e in events])
        if cadence is None:
            continue

        last_iso = unix_dates[-1]
        last_date = datetime.strptime(last_iso, "%Y-%m-%d").date()
        days_since = (today - last_date).days

        if days_since >= cadence:
            status = "due"
        elif days_since >= cadence * 0.8:
            status = "soon"
        else:
            status = "not_due"

        latest = events[-1]
        results.append({
            "item": latest[1],
            "canonical_key": item_id,
            "purchases": len(unix_dates),
            "cadence_days": round(cadence, 1),
            "days_since": days_since,
            "last_purchased": last_iso,
            "status": status,
            "retailer_slug": retailer_slug,
            "item_id": item_id,
            "product_id": latest[4],
            "quantity": latest[2],
            "quantity_type": latest[3],
        })

    status_order = {"due": 0, "soon": 1, "not_due": 2}
    results.sort(key=lambda r: (status_order[r["status"]], -r["days_since"]))
    return results


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--store", help="Filter by retailer_slug (e.g. costco, qfc)")
    p.add_argument("--min-purchases", type=int, default=2)
    p.add_argument("--format", choices=["json", "text"], default="text")
    p.add_argument("--due-only", action="store_true",
                   help="Only emit items with status due or soon")
    args = p.parse_args()

    results = analyze(CLI_DB, store=args.store, min_purchases=args.min_purchases)
    if args.due_only:
        results = [r for r in results if r["status"] in ("due", "soon")]

    if args.format == "json":
        print(json.dumps(results, indent=2))
        return

    if not results:
        print("No items qualify yet "
              f"(need ≥{args.min_purchases} purchases per item).")
        return

    by_status = defaultdict(list)
    for r in results:
        by_status[r["status"]].append(r)

    for label, status, emoji in [("Due", "due", "🔴"),
                                  ("Soon", "soon", "🟡"),
                                  ("Not due", "not_due", "🟢")]:
        bucket = by_status.get(status, [])
        if not bucket or (args.due_only and status == "not_due"):
            continue
        print(f"{emoji} {label} ({len(bucket)}):")
        for r in bucket:
            print(f"  [{r['retailer_slug']}] {r['item']} — "
                  f"{r['days_since']}d since (cadence {r['cadence_days']}d, "
                  f"×{r['purchases']})")
        print()

    print(f"Total: {len(results)} tracked.")


if __name__ == "__main__":
    main()
