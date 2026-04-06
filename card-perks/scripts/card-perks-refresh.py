#!/usr/bin/env python3
"""
Card Perks Refresh Helper — dumps current benefits for the monthly agentTurn.

Called by the monthly cron's agentTurn prompt to get a formatted snapshot of
what's currently in the database. The LLM then compares this against web
search results to detect changes.

Usage:
    python3 card-perks-refresh.py dump        # Print current benefits as text
    python3 card-perks-refresh.py dump-json   # Print as JSON
"""

import sqlite3
import json
import sys
import os

CARDS_DB = os.environ.get(
    "CARDS_DB",
    os.path.expanduser("~/.config/spratt/cards/cards.sqlite"),
)


def get_db():
    conn = sqlite3.connect(CARDS_DB)
    conn.row_factory = sqlite3.Row
    return conn


def dump_text():
    conn = get_db()
    rows = conn.execute("""
        SELECT
            c.holder,
            c.card_name,
            c.issuer,
            b.name,
            b.merchant,
            b.amount,
            b.cycle,
            b.period_rule,
            b.requires_activation,
            b.auto_applied,
            b.notes,
            b.active
        FROM benefits b
        JOIN cards c ON b.card_id = c.id
        WHERE c.active = 1
        ORDER BY c.holder, c.card_name, b.id
    """).fetchall()

    current_card = None
    for r in rows:
        card_key = f"{r['holder']} — {r['card_name']}"
        if card_key != current_card:
            if current_card is not None:
                print()
            print(f"### {card_key} ({r['issuer']})")
            current_card = card_key

        status = "ACTIVE" if r["active"] else "INACTIVE"
        flags = []
        if r["auto_applied"]:
            flags.append("auto-applied")
        if r["requires_activation"]:
            flags.append("requires activation")
        flag_str = f" [{', '.join(flags)}]" if flags else ""

        notes_str = f" — {r['notes']}" if r["notes"] else ""
        print(f"  [{status}] {r['name']}: ${r['amount']:.2f}/{r['cycle']} "
              f"({r['period_rule']}) @ {r['merchant']}{flag_str}{notes_str}")

    conn.close()


def dump_json():
    conn = get_db()
    rows = conn.execute("""
        SELECT
            c.holder,
            c.card_name,
            c.issuer,
            b.id AS benefit_id,
            b.name,
            b.merchant,
            b.amount,
            b.cycle,
            b.period_rule,
            b.requires_activation,
            b.auto_applied,
            b.notes,
            b.active
        FROM benefits b
        JOIN cards c ON b.card_id = c.id
        WHERE c.active = 1
        ORDER BY c.holder, c.card_name, b.id
    """).fetchall()

    data = [dict(r) for r in rows]
    print(json.dumps(data, indent=2))
    conn.close()


def main():
    if len(sys.argv) < 2:
        print("Usage: card-perks-refresh.py [dump|dump-json]", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "dump":
        dump_text()
    elif cmd == "dump-json":
        dump_json()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
