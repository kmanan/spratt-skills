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


# Complete canonical schema for cards.sqlite. All CREATE TABLE uses IF NOT EXISTS
# so running this against the existing live DB is a no-op. On a fresh DB this
# produces the full schema (7 tables) without relying on any external init step.
CARDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id                  INTEGER PRIMARY KEY,
    holder              TEXT NOT NULL,
    card_name           TEXT NOT NULL,
    issuer              TEXT,
    network             TEXT,
    annual_fee          REAL DEFAULT 0,
    reward_type         TEXT DEFAULT 'cashback',
    point_valuation_cpp REAL,
    active              INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(holder, card_name)
);

CREATE TABLE IF NOT EXISTS benefits (
    id                  INTEGER PRIMARY KEY,
    card_id             INTEGER NOT NULL REFERENCES cards(id),
    name                TEXT NOT NULL,
    merchant            TEXT,
    amount              REAL NOT NULL,
    cycle               TEXT NOT NULL,
    period_rule         TEXT NOT NULL,
    requires_activation INTEGER NOT NULL DEFAULT 0,
    auto_applied        INTEGER NOT NULL DEFAULT 0,
    notes               TEXT,
    active              INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS usage (
    id              INTEGER PRIMARY KEY,
    benefit_id      INTEGER NOT NULL REFERENCES benefits(id),
    period_key      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    notified_at     TEXT,
    acknowledged_at TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_benefit_period ON usage(benefit_id, period_key);

CREATE TABLE IF NOT EXISTS benefit_changes (
    id          INTEGER PRIMARY KEY,
    card_id     INTEGER NOT NULL REFERENCES cards(id),
    change_type TEXT NOT NULL,
    description TEXT NOT NULL,
    detected_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reward_rates (
    id              INTEGER PRIMARY KEY,
    card_id         INTEGER NOT NULL REFERENCES cards(id),
    category        TEXT NOT NULL,
    rate            REAL NOT NULL,
    cap_amount      REAL,
    cap_period      TEXT,
    rate_after_cap  REAL DEFAULT 1.0,
    is_quarterly    INTEGER DEFAULT 0,
    notes           TEXT,
    UNIQUE(card_id, category)
);

CREATE TABLE IF NOT EXISTS quarterly_categories (
    id              INTEGER PRIMARY KEY,
    card_id         INTEGER NOT NULL REFERENCES cards(id),
    year            INTEGER NOT NULL,
    quarter         INTEGER NOT NULL,
    categories      TEXT NOT NULL,
    activated       INTEGER DEFAULT 0,
    activated_at    TEXT,
    UNIQUE(card_id, year, quarter)
);

CREATE TABLE IF NOT EXISTS spending_estimates (
    id              INTEGER PRIMARY KEY,
    category        TEXT NOT NULL UNIQUE,
    monthly_amount  REAL NOT NULL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def ensure_cards_schema(conn):
    """Ensure all cards.sqlite tables and indexes exist. Idempotent."""
    conn.executescript(CARDS_SCHEMA)
    conn.commit()

# Holder → alias for outbox messages (resolved via contacts.sqlite at send time)
# NOTE: "Harshita" resolves to a different contact than Wife in the current
# contacts DB — using the "Wife" alias here matches the number previously
# hardcoded. Fix the contacts alias collision if needed.
HOLDER_RECIPIENTS = {
    "manan": "Manan",
    "harshita": "Wife",
}

# Holder → Apple Reminders list name
HOLDER_REMINDER_LIST = {
    "manan": "Manan",
    "harshita": "Harshita",
}


def get_db():
    if not os.path.exists(CARDS_DB):
        sys.stderr.write(
            f"\nFATAL: cards database not found at:\n    {CARDS_DB}\n\n"
            f"Refusing to auto-create (prevents silent data loss if the path is wrong).\n\n"
        )
        sys.exit(1)
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
    """Human-readable LAST-day-to-use date (period end, not reset date).

    Uses the actual period end so the text reads "19 days left, expires Apr 30"
    rather than the confusing "19 days until May 1" (which is the reset date
    and is also off-by-one from days_until_expiry's count to period end).
    """
    if benefit["period_rule"] == "chase-freedom" and benefit["requires_activation"]:
        deadline = chase_freedom_activation_deadline(today)
        return deadline.strftime("%b %-d")

    end = period_end_date(benefit["cycle"], benefit["period_rule"], today)
    if end is None:
        return "?"
    return end.strftime("%b %-d")


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


def get_all_pending_benefits(conn, today):
    """Return all pending (unclaimed) benefits in active periods, grouped by holder.

    A benefit is shown weekly until the user marks it used/skipped — that's the
    whole point of the reminder. Filtering by notified_at (the old behavior)
    would suppress the weekly nudge after the first Saturday. Status='pending'
    means the user hasn't acknowledged it; once they reply "used X" the handler
    sets status='used' and it drops out of this query.
    """
    rows = conn.execute("""
        SELECT
            u.id AS usage_id,
            u.period_key,
            u.notified_at,
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
          AND b.active = 1
          AND b.auto_applied = 0
          AND c.active = 1
    """).fetchall()

    by_holder = {}
    for row in rows:
        # Defensive: skip if the benefit's period has already ended
        # (shouldn't normally happen — ensure_pending_rows creates rows for the
        # current period — but guards against edge cases around month boundaries).
        days_left = days_until_expiry(row, today)
        if days_left < 0:
            continue
        by_holder.setdefault(row["holder"], []).append(row)

    # Sort each holder's list by soonest-expiring first.
    for holder in by_holder:
        by_holder[holder].sort(key=lambda b: days_until_expiry(b, today))

    return by_holder


def urgency_tier(days_left):
    """Classify remaining-days into an urgency tier.

    - 🔴 urgent: ≤ 10 days. Last-chance. Also gets Apple Reminder created.
    - 🟡 this-period: 11 - 30 days. Plan-to-use nudge.
    - 🟢 fyi: > 30 days. Informational — mostly annual/semi-annual credits.
    """
    if days_left <= 10:
        return "urgent"
    if days_left <= 30:
        return "mid"
    return "fyi"


def format_benefit_line(b, today):
    """Format a single benefit for the outbox message."""
    days = days_until_expiry(b, today)
    exp_str = expiry_date_str(b, today)
    card_short = b["card_name"].replace("Chase ", "").replace("AMEX ", "")

    if b["period_rule"] == "chase-freedom" and b["requires_activation"]:
        cats = b["notes"] or "check chase.com"
        return f"  ⚡ {b['card_name']} 5% — activate by {exp_str} ({cats})"

    # "5 days left, expires Apr 30" reads cleaner than the old "5 days until May 1"
    # (which implied May 1 was the deadline rather than the reset date).
    return f"  💸 {card_short} {b['name']} — ${b['amount']:.0f} unclaimed ({days} days left, expires {exp_str})"


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


def build_weekly_message(holder, benefits, today):
    """Build the weekly outbox message body for one holder's pending benefits.

    Benefits are grouped into urgency tiers (🔴 urgent, 🟡 mid, 🟢 fyi) and the
    message only surfaces tiers that have items. If everything is 🟢 (all annual
    credits far out), still sends — the user asked for weekly visibility, not
    just urgent alerts.
    """
    # "Sir" / "Ma'am" — capitalised manually because .title() mangles apostrophes
    # (produces "Ma'Am") and Spratt's voice is sensitive to that.
    address = "Sir" if holder == "manan" else "Ma'am"

    # Partition by tier
    tiers = {"urgent": [], "mid": [], "fyi": []}
    for b in benefits:
        tiers[urgency_tier(days_until_expiry(b, today))].append(b)

    total_value = sum(b["amount"] for b in benefits if b["amount"])
    n = len(benefits)

    # Opener — tone depends on whether any are urgent
    if tiers["urgent"]:
        opener = (
            f"🧤 {address}, {len(tiers['urgent'])} card benefit"
            f"{'s' if len(tiers['urgent']) != 1 else ''} "
            f"requires your attention before vanishing:"
        )
    else:
        opener = (
            f"🧤 {address}, {n} card benefit{'s' if n != 1 else ''} "
            f"await your use (${total_value:,.0f} total)."
        )

    lines = [opener, ""]

    if tiers["urgent"]:
        lines.append("🔴 EXPIRING SOON")
        for b in tiers["urgent"]:
            lines.append(format_benefit_line(b, today))
        lines.append("")

    if tiers["mid"]:
        lines.append("🟡 THIS PERIOD")
        for b in tiers["mid"]:
            lines.append(format_benefit_line(b, today))
        lines.append("")

    if tiers["fyi"]:
        fyi_total = sum(b["amount"] for b in tiers["fyi"] if b["amount"])
        fyi_count = len(tiers["fyi"])
        lines.append(
            f"🟢 {fyi_count} more benefit{'s' if fyi_count != 1 else ''} "
            f"(${fyi_total:,.0f}) still available with 30+ days left."
        )
        lines.append("")

    lines.append("A simple \"used [name]\" or \"skip [name]\" will do. I shall attend to the rest.")
    return "\n".join(lines)


def main():
    today = date.today()
    conn = get_db()

    # Step 1: ensure pending usage rows exist for the current period.
    # (This is what creates e.g. a fresh 2026-05 Uber Cash row when May rolls over.)
    ensure_pending_rows(conn, today)

    # Step 2: collect all pending benefits per holder.
    by_holder = get_all_pending_benefits(conn, today)

    if not by_holder:
        # Everything is marked used/skipped across every holder. Genuinely silent.
        return

    # Step 3: for each holder, send weekly message + Apple Reminders for urgent items.
    for holder, benefits in by_holder.items():
        message = build_weekly_message(holder, benefits, today)
        schedule_outbox(holder, message)

        # Apple Reminders: only create for the urgent tier (≤10 days).
        # Creating reminders every Saturday for annual credits would spam the
        # Reminders list with duplicates — remindctl add has no built-in dedup.
        # Urgent-tier items are the "must not forget" bucket; the rest live in
        # the weekly message only.
        for b in benefits:
            if urgency_tier(days_until_expiry(b, today)) == "urgent":
                create_reminder(b, today, holder)

        # Update notified_at on all benefits in this notification. This is now
        # a tracking field (last time we pinged the user about this benefit),
        # not a filter — unlike the old behavior which suppressed re-notification.
        usage_ids = [b["usage_id"] for b in benefits]
        mark_notified(conn, usage_ids)

    conn.close()


if __name__ == "__main__":
    main()
