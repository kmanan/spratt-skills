#!/usr/bin/env python3
"""Destination-aware reminder activity summary.

Reads reminder_fires.sqlite + Apple Reminders state and prints a short
human-readable digest. Intended for ad-hoc spot checks and as a building
block for any future weekly roll-up.

Usage:
    destination-summary.py                  # last 7 days, text
    destination-summary.py --days 30        # last 30 days
    destination-summary.py --json           # structured output
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

KNOWN_FILE = os.path.expanduser(
    "~/.config/spratt/infrastructure/destination/known-destinations.json"
)
FIRES_DB = os.path.expanduser("~/.config/spratt/db/reminder_fires.sqlite")
REMINDER_LISTS = ("Manan", "Harshita", "Shared")
TAG_RE = re.compile(r"#(\w+)")


def load_allowed():
    try:
        with open(KNOWN_FILE) as f:
            return set(json.load(f).get("categories", []))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def list_open_reminders():
    allowed = load_allowed()
    open_total = 0
    tagged = 0
    by_category = {}
    untagged_examples = []
    for lst in REMINDER_LISTS:
        r = subprocess.run(
            ["/opt/homebrew/bin/remindctl", "show", "all", "--list", lst, "--json"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            continue
        try:
            items = json.loads(r.stdout)
        except json.JSONDecodeError:
            continue
        for it in items:
            if it.get("isCompleted"):
                continue
            title = (it.get("title") or "").strip()
            if not title:
                continue
            open_total += 1
            tags = {m.lower() for m in TAG_RE.findall(title)} & allowed
            if tags:
                tagged += 1
                for t in tags:
                    by_category[t] = by_category.get(t, 0) + 1
            else:
                if len(untagged_examples) < 5:
                    untagged_examples.append(title[:70])
    return {
        "open_total": open_total,
        "tagged": tagged,
        "untagged": open_total - tagged,
        "untagged_pct": round(100 * (open_total - tagged) / open_total, 1) if open_total else 0.0,
        "by_category": by_category,
        "untagged_examples": untagged_examples,
    }


def fires_window(days):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    total = 0
    by_cat = {}
    by_dest = {}
    if not os.path.exists(FIRES_DB):
        return {"total": 0, "by_category": {}, "by_destination": {}, "db_present": False}
    try:
        conn = sqlite3.connect(FIRES_DB, timeout=5.0)
        row = conn.execute(
            "SELECT COUNT(*) FROM reminder_fires WHERE fired_at >= ?", (cutoff,),
        ).fetchone()
        total = row[0] if row else 0
        for cat, cnt in conn.execute(
            "SELECT category, COUNT(*) FROM reminder_fires WHERE fired_at >= ? GROUP BY category",
            (cutoff,),
        ):
            by_cat[cat] = cnt
        for dest, cnt in conn.execute(
            "SELECT destination, COUNT(*) FROM reminder_fires WHERE fired_at >= ? GROUP BY destination "
            "ORDER BY COUNT(*) DESC LIMIT 10",
            (cutoff,),
        ):
            by_dest[dest] = cnt
        conn.close()
    except sqlite3.Error as e:
        return {"total": 0, "by_category": {}, "by_destination": {}, "db_present": True, "error": str(e)}
    return {"total": total, "by_category": by_cat, "by_destination": by_dest, "db_present": True}


def render_text(reminders, fires, days):
    lines = [f"Destination-aware summary (last {days}d)", "=" * 40]
    lines.append(f"Open reminders: {reminders['open_total']} total, "
                 f"{reminders['tagged']} tagged, {reminders['untagged']} untagged "
                 f"({reminders['untagged_pct']}% untagged)")
    if reminders["by_category"]:
        cat_str = ", ".join(f"{k}={v}" for k, v in sorted(reminders["by_category"].items()))
        lines.append(f"  Tagged by category: {cat_str}")
    if reminders["untagged_examples"]:
        lines.append("  Untagged examples:")
        for t in reminders["untagged_examples"]:
            lines.append(f"    - {t}")
    lines.append("")
    if not fires["db_present"]:
        lines.append("Fire log: reminder_fires.sqlite does not exist yet (no fires recorded)")
    else:
        lines.append(f"Fires in last {days}d: {fires['total']}")
        if fires["by_category"]:
            lines.append("  By category: " + ", ".join(f"{k}={v}" for k, v in sorted(fires["by_category"].items())))
        if fires["by_destination"]:
            lines.append("  Top destinations:")
            for d, cnt in fires["by_destination"].items():
                lines.append(f"    {cnt}x  {d}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    reminders = list_open_reminders()
    fires = fires_window(args.days)

    if args.json:
        print(json.dumps({"days": args.days, "reminders": reminders, "fires": fires}, indent=2))
    else:
        print(render_text(reminders, fires, args.days))


if __name__ == "__main__":
    main()
