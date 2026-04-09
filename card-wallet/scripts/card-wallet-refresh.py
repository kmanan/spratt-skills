#!/usr/bin/env python3
"""
Card Wallet Refresh Helper — dumps current benefits and reward rates for the monthly agentTurn.

Called by the monthly cron's agentTurn prompt to get a formatted snapshot of
what's currently in the database. The LLM then compares this against web
search results to detect changes.

Usage:
    python3 card-wallet-refresh.py dump        # Print current benefits + reward rates as text
    python3 card-wallet-refresh.py dump-json   # Print as JSON (for programmatic use)
"""

import sqlite3
import json
import sys
import os

CARDS_DB = os.path.expanduser("~/.config/spratt/cards/cards.sqlite")


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

    # Also dump reward rates
    print("\n## Reward Rates")
    rates = conn.execute("""
        SELECT c.holder, c.card_name, r.category, r.rate, r.cap_amount, r.cap_period, r.notes
        FROM reward_rates r
        JOIN cards c ON r.card_id = c.id
        WHERE c.active = 1
        ORDER BY c.holder, c.card_name, r.rate DESC
    """).fetchall()

    current_card = None
    for r in rates:
        card_key = f"{r['holder']} — {r['card_name']}"
        if card_key != current_card:
            print(f"\n### {card_key}")
            current_card = card_key
        cap_str = f" (cap: ${r['cap_amount']:.0f}/{r['cap_period']})" if r["cap_amount"] else ""
        notes_str = f" — {r['notes']}" if r["notes"] else ""
        print(f"  {r['category']}: {r['rate']}%{cap_str}{notes_str}")

    # Dump quarterly categories if any
    quarters = conn.execute("""
        SELECT c.card_name, q.year, q.quarter, q.categories, q.activated
        FROM quarterly_categories q
        JOIN cards c ON q.card_id = c.id
        ORDER BY q.year, q.quarter
    """).fetchall()

    if quarters:
        print("\n## Quarterly Categories")
        for q in quarters:
            act = "✅" if q["activated"] else "❌"
            print(f"  {q['card_name']} {q['year']}-Q{q['quarter']}: {q['categories']} {act}")

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
