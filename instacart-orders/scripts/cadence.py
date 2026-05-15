#!/usr/bin/env python3
"""
Instacart purchase cadence — reads instacart-pp-cli's SQLite history DB.

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
import math
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from statistics import median


def recency_match(days_since, cadence_days):
    if cadence_days <= 0:
        return 0.0
    ratio = days_since / cadence_days
    if ratio < 0.5:
        return 0.0
    if ratio <= 1.5:
        return 1.0
    if ratio <= 3.0:
        return (3.0 - ratio) / 1.5
    return 0.0


def confidence(purchases):
    return min(1.0, max(0.0, (purchases - 1) / 4))


def compute_score(purchases, cadence_days, days_since):
    return (math.log(purchases + 1)
            * recency_match(days_since, cadence_days)
            * confidence(purchases))

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
        med_qty = median([e[2] for e in events]) or 1
        score = compute_score(len(unix_dates), cadence, days_since)
        recency_ratio = round(days_since / cadence, 2) if cadence > 0 else 0.0
        results.append({
            "item": latest[1],
            "canonical_key": item_id,
            "purchases": len(unix_dates),
            "cadence_days": round(cadence, 1),
            "days_since": days_since,
            "last_purchased": last_iso,
            "status": status,
            "score": round(score, 3),
            "recency_ratio": recency_ratio,
            "retailer_slug": retailer_slug,
            "item_id": item_id,
            "product_id": latest[4],
            "quantity": med_qty,
            "quantity_type": latest[3],
        })

    results.sort(key=lambda r: -r["score"])
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
        results = [r for r in results
                   if r["status"] in ("due", "soon")
                   and r["purchases"] >= 4
                   and r["recency_ratio"] <= 3]

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
