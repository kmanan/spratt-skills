#!/usr/bin/env python3
"""
Item classifier helper — manages the item_aliases table in orders.sqlite.

The actual classification is done by the LLM (Flash) in the nightly scraper cron.
This script handles the deterministic parts: finding unclassified items and writing
mappings back to the database.

Usage:
    item-classify.py list-unclassified          # Show raw names not yet in item_aliases
    item-classify.py list-all                   # Show all current aliases
    item-classify.py set --raw "name" --canonical "name"   # Write one alias
    item-classify.py set-batch --json '[{"raw": "...", "canonical": "..."}, ...]'
"""

import argparse
import json
import os
import sqlite3
import sys

ORDERS_DB = os.path.expanduser("~/.config/spratt/orders/orders.sqlite")


def get_db():
    conn = sqlite3.connect(ORDERS_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def list_unclassified():
    """Find all unique item names in orders that don't have an alias yet."""
    conn = get_db()
    # Extract all unique item names from the JSON items arrays
    rows = conn.execute("""
        SELECT DISTINCT json_extract(value, '$.name') AS item_name
        FROM orders, json_each(orders.items)
        WHERE source = 'instacart'
          AND json_extract(value, '$.name') IS NOT NULL
          AND json_extract(value, '$.name') NOT IN (
              SELECT raw_name FROM item_aliases
          )
        ORDER BY item_name
    """).fetchall()
    conn.close()
    return [r["item_name"] for r in rows]


def list_all():
    """Show all current aliases."""
    conn = get_db()
    rows = conn.execute(
        "SELECT raw_name, canonical_name FROM item_aliases ORDER BY canonical_name, raw_name"
    ).fetchall()
    conn.close()
    return [(r["raw_name"], r["canonical_name"]) for r in rows]


def set_alias(raw_name, canonical_name):
    """Write or update one alias."""
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO item_aliases (raw_name, canonical_name) VALUES (?, ?)",
        (raw_name, canonical_name)
    )
    conn.commit()
    conn.close()


def set_batch(mappings):
    """Write multiple aliases at once. mappings = [{"raw": ..., "canonical": ...}, ...]"""
    conn = get_db()
    for m in mappings:
        conn.execute(
            "INSERT OR REPLACE INTO item_aliases (raw_name, canonical_name) VALUES (?, ?)",
            (m["raw"], m["canonical"])
        )
    conn.commit()
    conn.close()
    return len(mappings)


def main():
    parser = argparse.ArgumentParser(description="Manage item_aliases in orders.sqlite")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list-unclassified", help="Show item names without aliases")
    sub.add_parser("list-all", help="Show all current aliases")

    p = sub.add_parser("set", help="Set one alias")
    p.add_argument("--raw", required=True, help="Raw item name from receipt")
    p.add_argument("--canonical", required=True, help="Canonical product name")

    p = sub.add_parser("set-batch", help="Set multiple aliases from JSON")
    p.add_argument("--json", required=True, help='JSON array: [{"raw": "...", "canonical": "..."}, ...]')

    args = parser.parse_args()

    if args.command == "list-unclassified":
        items = list_unclassified()
        if not items:
            print("All items classified.")
        else:
            print(f"{len(items)} unclassified items:")
            for name in items:
                print(f"  {name}")
            # Also output as JSON for easy LLM consumption
            print(f"\nJSON: {json.dumps(items)}")

    elif args.command == "list-all":
        aliases = list_all()
        if not aliases:
            print("No aliases yet.")
        else:
            current_canonical = None
            for raw, canonical in aliases:
                if canonical != current_canonical:
                    print(f"\n{canonical}:")
                    current_canonical = canonical
                if raw != canonical:
                    print(f"  ← {raw}")
                else:
                    print(f"  = {raw}")

    elif args.command == "set":
        set_alias(args.raw, args.canonical)
        print(f"OK: '{args.raw}' → '{args.canonical}'")

    elif args.command == "set-batch":
        try:
            mappings = json.loads(args.json)
        except json.JSONDecodeError as e:
            print(f"ERROR: Invalid JSON: {e}", file=sys.stderr)
            sys.exit(1)
        count = set_batch(mappings)
        print(f"OK: wrote {count} aliases")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
