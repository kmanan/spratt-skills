#!/usr/bin/env python3
"""
Deterministic order ingestion — called by email scanning agent or instacart scraper.
Inserts order into orders.sqlite and optionally notifies via outbox.

Usage (insert):
    order-ingest.py --source instacart --order-id ORD-123 --date 2026-04-05 \
        --items '[{"name":"Whole Milk","qty":2,"price":4.99}]' --total 14.97 \
        --email-id MSG-ABC --account outlook \
        [--notify] [--delivery-status "arriving today 3-5pm"]

Usage (update items on existing order):
    order-ingest.py update-items --source instacart --order-id ORD-123 \
        --items '[{"name":"Whole Milk","qty":2,"price":4.99}]' --total 14.97
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime

ORDERS_DB = os.path.expanduser("~/.config/spratt/orders/orders.sqlite")
OUTBOX_CLI = os.path.expanduser("~/.config/spratt/infrastructure/outbox/outbox.py")
OWNER_PHONE = "+1XXXXXXXXXX"  # Replace with your phone number


def validate_items(items_str):
    """Parse and validate a JSON items array. Returns (items_list, items_json)."""
    try:
        items = json.loads(items_str)
        if not isinstance(items, list):
            print(f"ERROR: --items must be a JSON array, got {type(items).__name__}", file=sys.stderr)
            sys.exit(1)
        return items, json.dumps(items)
    except json.JSONDecodeError as e:
        print(f"ERROR: Invalid JSON in --items: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_insert(args):
    """Insert a new order."""
    items, items_json = validate_items(args.items)

    conn = sqlite3.connect(ORDERS_DB)
    if args.order_id:
        existing = conn.execute(
            "SELECT id FROM orders WHERE order_id = ? AND source = ?",
            (args.order_id, args.source)
        ).fetchone()
        if existing:
            print(f"SKIP: order {args.order_id} from {args.source} already exists (id={existing[0]})")
            conn.close()
            sys.exit(0)

    conn.execute(
        "INSERT INTO orders (source, order_id, order_date, items, total, source_email_id, source_account) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (args.source, args.order_id, args.date, items_json, args.total, args.email_id, args.account)
    )
    conn.commit()
    order_db_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    item_count = len(items)
    item_names = ", ".join(i.get("name", "?") for i in items[:5])
    if len(items) > 5:
        item_names += f" (+{len(items) - 5} more)"
    total_str = f"${args.total:.2f}" if args.total else "unknown total"

    print(f"OK: inserted order {order_db_id} from {args.source} — {item_count} items, {total_str}")

    if args.notify:
        status = args.delivery_status or "order received"
        body = f"Order from {args.source.title()} — {status}. {item_count} items, {total_str}."
        if item_count <= 5:
            body += f"\n{item_names}"

        try:
            subprocess.run(
                [
                    sys.executable, OUTBOX_CLI,
                    "schedule",
                    "--to", OWNER_PHONE,
                    "--body", body,
                    "--at", "now",
                    "--source", f"email-scan:{args.source}",
                    "--created-by", "order-ingest",
                ],
                capture_output=True, text=True, timeout=10
            )
            print(f"NOTIFIED: scheduled outbox message")
        except Exception as e:
            print(f"WARNING: notification failed: {e}", file=sys.stderr)


def cmd_update_items(args):
    """Update items and total on an existing order by order_id + source."""
    items, items_json = validate_items(args.items)

    conn = sqlite3.connect(ORDERS_DB)
    existing = conn.execute(
        "SELECT id, items FROM orders WHERE order_id = ? AND source = ?",
        (args.order_id, args.source)
    ).fetchone()

    if not existing:
        print(f"ERROR: no order found for order_id={args.order_id} source={args.source}", file=sys.stderr)
        conn.close()
        sys.exit(1)

    updates = ["items = ?"]
    params = [items_json]
    if args.total is not None:
        updates.append("total = ?")
        params.append(args.total)
    params.append(existing[0])

    conn.execute(f"UPDATE orders SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    conn.close()

    print(f"OK: updated order {existing[0]} ({args.source}/{args.order_id}) — now {len(items)} items")


def main():
    parser = argparse.ArgumentParser(description="Ingest an order into orders.sqlite")
    subparsers = parser.add_subparsers(dest="command")

    # Default (no subcommand) = insert (backwards compatible)
    parser.add_argument("--source", required=False, help="e.g. instacart, amazon, doordash")
    parser.add_argument("--order-id", default=None, help="Vendor order ID")
    parser.add_argument("--date", default=None, help="Order date (YYYY-MM-DD or ISO)")
    parser.add_argument("--items", default=None, help="JSON array of items")
    parser.add_argument("--total", type=float, default=None, help="Order total")
    parser.add_argument("--email-id", default=None, help="Source email message ID")
    parser.add_argument("--account", default=None, help="Email account name")
    parser.add_argument("--notify", action="store_true", help="Send delivery notification")
    parser.add_argument("--delivery-status", default=None, help="e.g. 'delivered', 'arriving today'")

    # update-items subcommand
    update_parser = subparsers.add_parser("update-items", help="Update items on an existing order")
    update_parser.add_argument("--source", required=True, help="e.g. instacart")
    update_parser.add_argument("--order-id", required=True, help="Vendor order ID")
    update_parser.add_argument("--items", required=True, help="JSON array of items")
    update_parser.add_argument("--total", type=float, default=None, help="Order total")

    args = parser.parse_args()

    if args.command == "update-items":
        cmd_update_items(args)
    else:
        # Backwards-compatible insert mode
        if not args.source or not args.date or not args.items:
            parser.error("--source, --date, and --items are required for insert")
        cmd_insert(args)


if __name__ == "__main__":
    main()
