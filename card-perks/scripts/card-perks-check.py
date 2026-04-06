#!/usr/bin/env python3
"""
Card Perks Saturday Check — deterministic, no LLM.

Runs weekly via exec cron. For each active, non-auto-applied benefit: ensures
a usage row exists for the current period, then checks if any pending benefits
expire within 10 days. If so, creates Apple Reminders and schedules an outbox
message (one per holder). Sets notified_at to prevent duplicate notifications.

Configuration:
    Set CARDS_DB, OUTBOX_CLI, HOLDER_RECIPIENTS, and HOLDER_REMINDER_LIST
    below to match your setup.
"""

import sqlite3
import subprocess
import sys
import os
from datetime import date, datetime, timedelta
from calendar import monthrange

# ── Configuration ────────────────────────────────────────────────────────────
# Adjust these paths and mappings for your setup.

CARDS_DB = os.environ.get(
    "CARDS_DB",
    os.path.expanduser("~/.config/spratt/cards/cards.sqlite"),
)
OUTBOX_CLI = os.environ.get(
    "OUTBOX_CLI",
    os.path.expanduser("~/.config/spratt/infrastructure/outbox/outbox.py"),
)

# Map holder names (as stored in cards.holder) to phone numbers or chat GUIDs
HOLDER_RECIPIENTS = {
    "manan": os.environ.get("RECIPIENT_MANAN", "+1XXXXXXXXXX"),
    "harshita": os.environ.get("RECIPIENT_HARSHITA", "+1XXXXXXXXXX"),
}

# Map holder names to Apple Reminders list names
HOLDER_REMINDER_LIST = {
    "manan": "Manan",
    "harshita": "Harshita",
}

# ── Database ─────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(CARDS_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


# ── Period Key Computation ───────────────────────────────────────────────────

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


# ── Expiration Logic ─────────────────────────────────────────────────────────

def is_expiring_soon(benefit, today, days=10):
    """Check if a benefit expires within `days` days."""
    cycle = benefit["cycle"]
    period_rule = benefit["period_rule"]

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
    reset = end + timedelta(days=1)
    return reset.strftime("%b %-d")


# ── Pending Rows ─────────────────────────────────────────────────────────────

def ensure_pending_rows(conn, today):
    """For each active, non-auto-applied benefit, ensure a usage row exists."""
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


# ── Message Formatting ───────────────────────────────────────────────────────

def format_benefit_line(b, today):
    """Format a single benefit for the outbox message."""
    days = days_until_expiry(b, today)
    exp_str = expiry_date_str(b, today)
    card_short = b["card_name"].replace("Chase ", "").replace("AMEX ", "")

    if b["period_rule"] == "chase-freedom" and b["requires_activation"]:
        cats = b["notes"] or "check chase.com"
        return f"  \u26a1 {b['card_name']} 5% \u2014 activate by {exp_str} ({cats})"

    return f"  \U0001f4b8 {card_short} {b['name']} \u2014 ${b['amount']:.0f} unclaimed ({days} days until {exp_str})"


def build_message(holder, benefits, today):
    """Build the full notification message for a holder."""
    n = len(benefits)
    # Customize the address — adapt this to your household
    address = "sir" if holder == "manan" else "ma'am"

    if n == 1:
        opener = f"\U0001f9e4 If I may, {address} \u2014 one card benefit requires your attention before it vanishes into the ether:"
    else:
        opener = f"\U0001f9e4 A gentle reminder, {address} \u2014 {n} card benefits are expiring shortly and remain unclaimed:"

    lines = [opener, ""]
    for b in benefits:
        lines.append(format_benefit_line(b, today))
    lines.append("")
    lines.append('A simple "used [name]" will do. I shall attend to the rest.')
    return "\n".join(lines)


# ── Apple Reminders ──────────────────────────────────────────────────────────

def create_reminder(benefit, today, holder):
    """Create an Apple Reminder for an expiring benefit."""
    list_name = HOLDER_REMINDER_LIST.get(holder, "Reminders")

    if benefit["period_rule"] == "chase-freedom" and benefit["requires_activation"]:
        deadline = chase_freedom_activation_deadline(today)
        title = f"Activate Chase Freedom 5% \u2014 deadline {deadline.strftime('%b %-d')}"
        due = deadline.strftime("%Y-%m-%d")
    else:
        end = period_end_date(benefit["cycle"], benefit["period_rule"], today)
        if end is None:
            return
        title = f"Use {benefit['name']} (${benefit['amount']:.0f}) \u2014 expires {(end + timedelta(days=1)).strftime('%b %-d')}"
        due = end.strftime("%Y-%m-%d")

    try:
        subprocess.run(
            ["remindctl", "add", title, "--list", list_name, "--due", due],
            capture_output=True, text=True, timeout=10
        )
    except Exception as e:
        print(f"Warning: failed to create reminder: {e}", file=sys.stderr)


# ── Outbox ───────────────────────────────────────────────────────────────────

def schedule_outbox(holder, message):
    """Schedule an outbox message for a holder."""
    recipient = HOLDER_RECIPIENTS.get(holder)
    if not recipient or recipient == "+1XXXXXXXXXX":
        print(f"Warning: no recipient configured for holder '{holder}'", file=sys.stderr)
        return

    try:
        subprocess.run(
            [
                sys.executable, OUTBOX_CLI,
                "schedule",
                "--to", recipient,
                "--body", message,
                "--at", "now",
                "--source", "card-perks:saturday",
                "--created-by", "card-perks-check",
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


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    today = date.today()
    conn = get_db()

    # Step 1: ensure pending usage rows exist
    ensure_pending_rows(conn, today)

    # Step 2: find expiring benefits (pending, not yet notified)
    expiring = get_expiring_benefits(conn, today)

    if not expiring:
        return

    # Step 3: for each holder, create reminders + outbox message
    for holder, benefits in expiring.items():
        message = build_message(holder, benefits, today)
        schedule_outbox(holder, message)

        for b in benefits:
            create_reminder(b, today, holder)

        usage_ids = [b["usage_id"] for b in benefits]
        mark_notified(conn, usage_ids)

    conn.close()


if __name__ == "__main__":
    main()
