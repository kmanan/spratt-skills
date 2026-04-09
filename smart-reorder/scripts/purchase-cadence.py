#!/usr/bin/env python3
"""
Purchase cadence analysis — reads orders.sqlite to find items due for reorder.

Calculates median days between purchases per item. Items purchased 2+ times
with days_since_last >= median_cadence are flagged as due.

Usage:
    purchase-cadence.py [--store qfc] [--min-purchases 2] [--format json|text]
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, date
from statistics import median

ORDERS_DB = os.path.expanduser("~/.config/spratt/orders/orders.sqlite")


def normalize_name(name):
    """Normalize product name for matching across orders."""
    n = name.lower().strip()
    # Remove size/variant info in parentheses
    n = re.sub(r'\s*\(.*?\)', '', n)
    # Remove trailing size specs (e.g., "16 oz", "1 lb", "half gallon")
    n = re.sub(r'\s+\d+\s*(oz|lb|lbs|ml|l|g|kg|ct|pk|pack)\b.*$', '', n, flags=re.IGNORECASE)
    # Remove trailing container/size words
    n = re.sub(r'\s+(half\s+gallon|gallon|quart|pint|bottle|bottles|can|cans|bag|bags|box|boxes|bunch|bundle|package|count|sticks?)\s*$', '', n, flags=re.IGNORECASE)
    # Collapse whitespace, strip punctuation except hyphens
    n = re.sub(r'[,.]', '', n)
    n = re.sub(r'\s+', ' ', n).strip()
    return n


def load_aliases(db_path):
    """Load the item_aliases table into a dict: raw_name -> canonical_name."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT raw_name, canonical_name FROM item_aliases").fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def get_item_history(db_path, store=None):
    """Extract all instacart items with their order dates from the DB."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    query = """
        SELECT order_date, items, store
        FROM orders
        WHERE source = 'instacart'
          AND items != '[]'
          AND json_array_length(items) > 0
    """
    params = []
    if store:
        query += " AND store = ?"
        params.append(store)
    query += " ORDER BY order_date"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    # Load aliases for semantic grouping (populated by Flash)
    aliases = load_aliases(db_path)

    # Flatten: canonical_name -> list of order_dates
    history = defaultdict(list)
    name_map = {}  # canonical -> most recent original name

    for row in rows:
        order_date = row["order_date"][:10]  # YYYY-MM-DD
        try:
            items = json.loads(row["items"])
        except (json.JSONDecodeError, TypeError):
            continue
        for item in items:
            orig_name = item.get("name", "")
            if not orig_name:
                continue
            # Use alias if available, otherwise fall back to regex normalizer
            canonical = aliases.get(orig_name, normalize_name(orig_name))
            history[canonical].append(order_date)
            name_map[canonical] = orig_name  # keep most recent spelling

    return history, name_map


def compute_cadence(dates):
    """Compute median days between purchases from a list of date strings."""
    if len(dates) < 2:
        return None

    unique_dates = sorted(set(dates))
    if len(unique_dates) < 2:
        return None

    parsed = [datetime.strptime(d, "%Y-%m-%d").date() for d in unique_dates]
    gaps = [(parsed[i + 1] - parsed[i]).days for i in range(len(parsed) - 1)]
    gaps = [g for g in gaps if g > 0]  # filter same-day duplicates

    if not gaps:
        return None

    return median(gaps)


def analyze(db_path, store=None, min_purchases=2):
    """Run cadence analysis and return list of items with status."""
    history, name_map = get_item_history(db_path, store)
    today = date.today()
    results = []

    for norm_name, dates in history.items():
        unique_dates = sorted(set(dates))
        if len(unique_dates) < min_purchases:
            continue

        cadence = compute_cadence(dates)
        if cadence is None:
            continue

        last_date = datetime.strptime(unique_dates[-1], "%Y-%m-%d").date()
        days_since = (today - last_date).days

        if days_since >= cadence:
            status = "due"
        elif days_since >= cadence * 0.8:
            status = "soon"
        else:
            status = "not_due"

        results.append({
            "item": name_map[norm_name],
            "purchases": len(unique_dates),
            "cadence_days": round(cadence, 1),
            "days_since": days_since,
            "last_purchased": unique_dates[-1],
            "status": status,
        })

    # Sort: due first, then soon, then not_due; within each group by days_since desc
    status_order = {"due": 0, "soon": 1, "not_due": 2}
    results.sort(key=lambda r: (status_order[r["status"]], -r["days_since"]))

    return results


def main():
    parser = argparse.ArgumentParser(description="Analyze purchase cadence from orders.sqlite")
    parser.add_argument("--store", default=None, help="Filter by store (e.g. qfc, costco)")
    parser.add_argument("--min-purchases", type=int, default=2, help="Minimum purchases to qualify (default: 2)")
    parser.add_argument("--format", choices=["json", "text"], default="text", help="Output format")
    parser.add_argument("--due-only", action="store_true", help="Only show due and soon items")
    args = parser.parse_args()

    results = analyze(ORDERS_DB, store=args.store, min_purchases=args.min_purchases)

    if args.due_only:
        results = [r for r in results if r["status"] in ("due", "soon")]

    if not results:
        if args.format == "json":
            print("[]")
        else:
            print("No items qualify for reorder analysis yet.")
            print(f"Need at least {args.min_purchases} orders per item.")
        sys.exit(0)

    if args.format == "json":
        print(json.dumps(results, indent=2))
    else:
        due = [r for r in results if r["status"] == "due"]
        soon = [r for r in results if r["status"] == "soon"]
        not_due = [r for r in results if r["status"] == "not_due"]

        if due:
            print(f"🔴 Due for reorder ({len(due)} items):")
            for r in due:
                print(f"  {r['item']} — last bought {r['days_since']}d ago (cadence: {r['cadence_days']}d, {r['purchases']} purchases)")

        if soon:
            print(f"\n🟡 Coming up soon ({len(soon)} items):")
            for r in soon:
                print(f"  {r['item']} — last bought {r['days_since']}d ago (cadence: {r['cadence_days']}d)")

        if not_due and not args.due_only:
            print(f"\n🟢 Not due yet ({len(not_due)} items):")
            for r in not_due:
                print(f"  {r['item']} — last bought {r['days_since']}d ago (cadence: {r['cadence_days']}d)")

        print(f"\nTotal: {len(results)} items tracked, {len(due)} due, {len(soon)} soon")


if __name__ == "__main__":
    main()
