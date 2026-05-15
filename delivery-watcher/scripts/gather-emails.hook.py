"""
Drop-in hook for your existing inbox-scan job.

Call `record_delivery_signals(emails)` once per scan, passing the list of
emails the scan already fetches. Each email must be a dict with at least:
  - id        (provider's message ID — used as PK)
  - provider  ("outlook" | "gmail")
  - account
  - from      (sender email or RFC-2822 "Name <addr>")
  - subject
  - date      (ISO 8601 like "2026-05-14T21:01:16Z", or RFC-2822, or epoch ms)

The hook is idempotent: re-calling with the same emails is harmless
(INSERT OR IGNORE on message_id PK). Failures are caught and logged but
do not raise — your scan job's main work is unaffected.

Sender + subject patterns are listed below; add more carriers by extending
the if/elif chain in `record_delivery_signals`.
"""

import email.utils
import os
import sqlite3
import sys
import time
from datetime import datetime


DELIVERY_SIGNALS_DB = os.path.expanduser(
    "~/.config/spratt/db/delivery_signals.sqlite"
)


def parse_email_date(s):
    if not s:
        return 0
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        pass
    if s.isdigit():
        n = int(s)
        return n // 1000 if len(s) > 10 else n
    try:
        return int(email.utils.parsedate_to_datetime(s).timestamp())
    except (ValueError, TypeError):
        return 0


def record_delivery_signals(emails):
    """Match delivery-shaped emails by sender+subject and write to
    delivery_signals.sqlite for delivery-watcher. Idempotent — INSERT OR IGNORE
    on message_id PK. Failures are logged but never break the caller."""
    sigs = []
    for e in emails:
        sender = (e.get("from") or "").lower()
        subject = e.get("subject") or ""
        source = None
        if "orders@instacart.com" in sender and "receipt" in subject.lower():
            source = "instacart"
        elif "order-update@amazon.com" in sender and subject.startswith("Delivered:"):
            source = "amazon"
        # Add more carriers here. Examples to extend later:
        #   elif "noreply@doordash.com" in sender and "Delivered" in subject:
        #       source = "doordash"
        if not source:
            continue
        arrived_at = parse_email_date(e.get("date") or "")
        if not arrived_at:
            continue
        sigs.append((
            e.get("id") or "",
            e.get("provider") or "",
            e.get("account") or "",
            source,
            e.get("from") or "",
            subject,
            arrived_at,
            int(time.time()),
        ))
    if not sigs:
        return 0
    try:
        os.makedirs(os.path.dirname(DELIVERY_SIGNALS_DB), exist_ok=True)
        with sqlite3.connect(DELIVERY_SIGNALS_DB) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS delivery_signals (
                    message_id  TEXT PRIMARY KEY,
                    provider    TEXT NOT NULL,
                    account     TEXT NOT NULL,
                    source      TEXT NOT NULL,
                    sender      TEXT NOT NULL,
                    subject     TEXT NOT NULL,
                    arrived_at  INTEGER NOT NULL,
                    detected_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_arrived ON delivery_signals(arrived_at);
            """)
            conn.executemany(
                "INSERT OR IGNORE INTO delivery_signals "
                "(message_id, provider, account, source, sender, subject, arrived_at, detected_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                sigs,
            )
            inserted = conn.total_changes
        print(f"  delivery_signals: matched {len(sigs)}, inserted {inserted}", file=sys.stderr)
        return inserted
    except Exception as exc:
        print(f"  delivery_signals: WARNING — write failed: {exc}", file=sys.stderr)
        return 0
