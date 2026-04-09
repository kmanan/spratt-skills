#!/usr/bin/env python3
"""
Card Wallet Saturday Check — deterministic, no LLM.

Runs every Saturday at 9am via exec cron. For each active, non-auto-applied
benefit: ensures a usage row exists for the current period, then checks if
any pending benefits expire within 10 days. If so, creates Apple Reminders
and schedules an outbox message (one per holder). Sets notified_at to prevent
duplicate notifications on consecutive Saturdays.
"""

import sqlite3
import subprocess
import sys
import os
from datetime import date, datetime, timedelta
from calendar import monthrange
from pathlib import Path

CARDS_DB = os.path.expanduser("~/.config/spratt/cards/cards.sqlite")
OUTBOX_CLI = os.path.expanduser("~/.config/spratt/infrastructure/outbox/outbox.py")

# Holder → recipient for outbox messages
HOLDER_RECIPIENTS = {
    "holder1": "+1XXXXXXXXXX",  # Replace with your phone numbers
    "holder2": "+1XXXXXXXXXX",
}

# Holder → Apple Reminders list name
HOLDER_REMINDER_LIST = {
    "manan": "Manan",
    "harshita": "Harshita",
}


def get_db():
    conn = sqlite3.connect(CARDS_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def current_period_key(cycle, period_rule, today):
    """Return the period key for a benefit given today's date."""
    if cycle == "monthly":
        if period_rule == "december-only":
            if today.month == 12:
                return f"{today.year}-12"
            return None  # not active outside December
        return f"{today.year}-{today.month:02d}"

    if cycle == "quarterly":
        q = (today.month - 1) // 3 + 1
        return f"{today.year}-Q{q}"

    if cycle == "semi-annual":
        h = 1 if today.month <= 6 else 2
        return f"{today.year}-H{h}"

    if cycle == "annual":
        return str(today.year)

    return None


def period_end_date(cycle, period_rule, today):
    """Return the last day of the current period."""
    if cycle == "monthly":
        if period_rule == "december-only":
            if today.month != 12:
                return None
            return date(today.year, 12, 31)
        _, last_day = monthrange(today.year, today.month)
        return date(today.year, today.month, last_day)

    if cycle == "quarterly":
        q = (today.month - 1) // 3 + 1
        end_month = q * 3
        _, last_day = monthrange(today.year, end_month)
        return date(today.year, end_month, last_day)

    if cycle == "semi-annual":
        if today.month <= 6:
            return date(today.year, 6, 30)
        return date(today.year, 12, 31)

    if cycle == "annual":
        return date(today.year, 12, 31)

    return None


def chase_freedom_activation_deadline(today):
    """Chase Freedom activation deadline: 14th of 3rd month of quarter."""
    q = (today.month - 1) // 3 + 1
    third_month = q * 3
    return date(today.year, third_month, 14)


def is_expiring_soon(benefit, today, days=10):
    """Check if a benefit expires within `days` days."""
    cycle = benefit["cycle"]
    period_rule = benefit["period_rule"]

    # Chase Freedom activation has its own deadline
    if period_rule == "chase-freedom" and benefit["requires_activation"]:
        deadline = chase_freedom_activation_deadline(today)
        days_left = (deadline - today).days
        return 0 <= days_left <= days

    end = period_end_date(cycle, period_rule, today)
    if end is None:
        return False

    days_left = (end - today).days
    return 0 <= days_left <= days


def days_until_expiry(benefit, today):
    """Return days until expiry (or activation deadline for Chase Freedom)."""
    if benefit["period_rule"] == "chase-freedom" and benefit["requires_activation"]:
        deadline = chase_freedom_activation_deadline(today)
        return (deadline - today).days

    end = period_end_date(benefit["cycle"], benefit["period_rule"], today)
    if end is None:
        return 999
    return (end - today).days


def expiry_date_str(benefit, today):
    """Human-readable expiry/deadline date."""
    if benefit["period_rule"] == "chase-freedom" and benefit["requires_activation"]:
        deadline = chase_freedom_activation_deadline(today)
        return deadline.strftime("%b %-d")

    end = period_end_date(benefit["cycle"], benefit["period_rule"], today)
    if end is None:
        return "?"
    # Show "resets" as the day after period end
    reset = end + timedelta(days=1)
    return reset.strftime("%b %-d")


def ensure_pending_rows(conn, today):
    """For each active, non-auto-applied benefit, ensure a usage row exists for the current period."""
    benefits = conn.execute("""
        SELECT b.id, b.cycle, b.period_rule
        FROM benefits b
        JOIN cards c ON b.card_id = c.id
        WHERE b.active = 1 AND b.auto_applied = 0 AND c.active = 1
    """).fetchall()

    for b in benefits:
        pk = current_period_key(b["cycle"], b["period_rule"], today)
        if pk is None:
            continue
        conn.execute("""
            INSERT OR IGNORE INTO usage (benefit_id, period_key)
            VALUES (?, ?)
        """, (b["id"], pk))

    conn.commit()


def get_expiring_benefits(conn, today):
    """Return pending, not-yet-notified benefits expiring within 10 days, grouped by holder."""
    rows = conn.execute("""
        SELECT
            u.id AS usage_id,
            u.period_key,
            b.id AS benefit_id,
            b.name,
            b.merchant,
            b.amount,
            b.cycle,
            b.period_rule,
            b.requires_activation,
            b.notes,
            c.holder,
            c.card_name
        FROM usage u
        JOIN benefits b ON u.benefit_id = b.id
        JOIN cards c ON b.card_id = c.id
        WHERE u.status = 'pending'
          AND u.notified_at IS NULL
          AND b.active = 1
          AND b.auto_applied = 0
          AND c.active = 1
    """).fetchall()

    expiring = {}
    for row in rows:
        if is_expiring_soon(row, today):
            holder = row["holder"]
            if holder not in expiring:
                expiring[holder] = []
            expiring[holder].append(row)

    return expiring


def format_benefit_line(b, today):
    """Format a single benefit for the outbox message."""
    days = days_until_expiry(b, today)
    exp_str = expiry_date_str(b, today)
    card_short = b["card_name"].replace("Chase ", "").replace("AMEX ", "")

    if b["period_rule"] == "chase-freedom" and b["requires_activation"]:
        cats = b["notes"] or "check chase.com"
        return f"  ⚡ {b['card_name']} 5% — activate by {exp_str} ({cats})"

    return f"  💸 {card_short} {b['name']} — ${b['amount']:.0f} unclaimed ({days} days until {exp_str})"


def create_reminder(benefit, today, holder):
    """Create an Apple Reminder for an expiring benefit."""
    list_name = HOLDER_REMINDER_LIST.get(holder, "Reminders")

    if benefit["period_rule"] == "chase-freedom" and benefit["requires_activation"]:
        deadline = chase_freedom_activation_deadline(today)
        title = f"Activate Chase Freedom 5% — deadline {deadline.strftime('%b %-d')}"
        due = deadline.strftime("%Y-%m-%d")
    else:
        end = period_end_date(benefit["cycle"], benefit["period_rule"], today)
        if end is None:
            return
        title = f"Use {benefit['name']} (${benefit['amount']:.0f}) — expires {(end + timedelta(days=1)).strftime('%b %-d')}"
        due = end.strftime("%Y-%m-%d")

    try:
        subprocess.run(
            ["remindctl", "add", title, "--list", list_name, "--due", due],
            capture_output=True, text=True, timeout=10
        )
    except Exception as e:
        print(f"Warning: failed to create reminder: {e}", file=sys.stderr)


def schedule_outbox(holder, message):
    """Schedule an outbox message for a holder."""
    recipient = HOLDER_RECIPIENTS.get(holder)
    if not recipient:
        print(f"Warning: no recipient for holder '{holder}'", file=sys.stderr)
        return

    try:
        subprocess.run(
            [
                sys.executable, OUTBOX_CLI,
                "schedule",
                "--to", recipient,
                "--body", message,
                "--at", "now",
                "--source", "card-wallet:saturday",
                "--created-by", "card-wallet-check",
            ],
            capture_output=True, text=True, timeout=10
        )
    except Exception as e:
        print(f"Warning: failed to schedule outbox message: {e}", file=sys.stderr)


def mark_notified(conn, usage_ids):
    """Set notified_at on usage rows to prevent duplicate notifications."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    for uid in usage_ids:
        conn.execute(
            "UPDATE usage SET notified_at = ? WHERE id = ?",
            (now, uid)
        )
    conn.commit()


def main():
    today = date.today()
    conn = get_db()

    # Step 1: ensure pending usage rows exist
    ensure_pending_rows(conn, today)

    # Step 2: find expiring benefits (pending, not yet notified)
    expiring = get_expiring_benefits(conn, today)

    if not expiring:
        # Silent Saturday — nothing to do
        return

    # Step 3: for each holder, create reminders + outbox message
    for holder, benefits in expiring.items():
        # Build message — Spratt's voice
        n = len(benefits)
        address = "sir" if holder == "manan" else "ma'am"
        if n == 1:
            opener = f"🧤 If I may, {address} — one card benefit requires your attention before it vanishes into the ether:"
        else:
            opener = f"🧤 A gentle reminder, {address} — {n} card benefits are expiring shortly and remain unclaimed:"
        lines = [opener, ""]
        for b in benefits:
            lines.append(format_benefit_line(b, today))
        lines.append("")
        lines.append("A simple \"used [name]\" will do. I shall attend to the rest.")
        message = "\n".join(lines)

        # Schedule outbox
        schedule_outbox(holder, message)

        # Create Apple Reminders
        for b in benefits:
            create_reminder(b, today, holder)

        # Mark as notified
        usage_ids = [b["usage_id"] for b in benefits]
        mark_notified(conn, usage_ids)

    conn.close()


if __name__ == "__main__":
    main()
