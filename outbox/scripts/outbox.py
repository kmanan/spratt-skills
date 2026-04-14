#!/usr/bin/env python3
"""
Spratt Message Outbox — CLI + Python API

Single source of truth for all outbound messages. Every iMessage from Spratt
or any background process goes through this outbox. The sender.py daemon is
the only thing that actually calls imsg.

CLI Usage:
  outbox.py schedule --to "9" --body "Dinner at 7:30" --at "2026-04-02T20:00:00Z" --source "trip:dc"
  outbox.py schedule --to "Manan" --body "Landed!" --at now --source "flight:AS3" --priority 10
  outbox.py cancel --source "trip:dc"
  outbox.py cancel --id 42
  outbox.py update --id 42 --body "New text"
  outbox.py update --id 42 --at "2026-04-02T21:00:00Z"
  outbox.py list
  outbox.py list --source "trip:dc" --status pending
  outbox.py list --status failed
  outbox.py list --status delivered --since 24h
  outbox.py status

Python API:
  from outbox import OutboxDB
  db = OutboxDB()
  db.schedule(recipient="9", body="Dinner at 7:30", send_at="2026-04-02T20:00:00Z", source="trip:dc")
  db.cancel(source="trip:dc")
  db.list_messages(status="pending")
"""

import sqlite3
import argparse
import json
import sys
import os
from datetime import datetime, timezone, timedelta

DB_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), "outbox.sqlite")


def require_db_file(path, name):
    """Fail loudly if a SQLite DB file doesn't exist where expected.

    Prevents silent split-brain from path resolution bugs (stale symlinks,
    moved files) that would otherwise cause sqlite3.connect() to create an
    empty new DB at the wrong path — exactly the April 2026 SSD incident.

    This function exits the process instead of raising so the error message
    is visible in stderr/logs without a Python traceback burying it.
    """
    if not os.path.exists(path):
        sys.stderr.write(
            f"\nFATAL: {name} database not found at:\n    {path}\n\n"
            f"Refusing to auto-create (prevents silent data loss if the path is wrong).\n"
            f"If this is intentional first-time setup, run explicit init first.\n\n"
        )
        sys.exit(1)


def _is_handle(recipient):
    """True iff `recipient` is already a deliverable iMessage handle (no name lookup needed)."""
    if not recipient or not isinstance(recipient, str):
        return False
    r = recipient.strip()
    if r.startswith("chat_guid:") and len(r) > len("chat_guid:"):
        return True
    if r.startswith("+") and r[1:].isdigit() and 8 <= len(r[1:]) <= 15:
        return True
    if r.isdigit():
        return True
    if "@" in r:
        local, _, domain = r.partition("@")
        if local and "." in domain:
            return True
    return False


def _resolve_recipient(recipient):
    """Turn a name/alias like 'Manan' or 'family chat' into a deliverable handle.

    If `recipient` already looks like a phone, chat_guid, email, or numeric
    chat-id, it is returned untouched. Otherwise we try the household
    contacts lookup (infrastructure/contacts/contacts.sqlite, populated
    nightly by sync-contacts.py from Apple Contacts + CONTACTS.md group
    chats). If the lookup returns a handle, we use that. If not, we return
    the original string so the validator produces the actionable error.
    """
    if _is_handle(recipient):
        return recipient
    try:
        contacts_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "contacts")
        if contacts_dir not in sys.path:
            sys.path.insert(0, contacts_dir)
        import contacts as _contacts  # type: ignore
        resolved = _contacts.resolve(recipient)
        if resolved:
            return resolved
    except Exception:
        # contacts module missing or DB unreachable — fall through to validator,
        # which will produce a clear error for the caller.
        pass
    return recipient


def _validate_recipient(recipient):
    """Raise ValueError unless recipient is a deliverable handle.

    sender.py hands the recipient string to `imsg`; imsg will happily accept
    anything and ask iMessage to resolve it. If the string is a contact NAME
    like "Manan" instead of a phone, iMessage routes to whatever Contacts
    resolves that name to — typically the user's own card or nothing — and
    the message silently fails delivery (red "Not Delivered" in the UI) while
    the outbox marks it "delivered" because imsg's immediate return was OK.

    Accept only formats that imsg + iMessage can actually route:
      - "+<E.164>"         phone, e.g. "+15551234567"
      - "chat_guid:<GUID>" iMessage group-chat GUID
      - "<digits>"         legacy numeric chat-id (imsg --chat-id)
      - "<email>"          Apple ID email handle (must contain '@' + '.')
    """
    if recipient is None or not isinstance(recipient, str):
        raise ValueError(f"recipient must be a non-empty string, got {recipient!r}")
    r = recipient.strip()
    if not r:
        raise ValueError("recipient is empty")
    if _is_handle(r):
        return
    raise ValueError(
        f"invalid recipient {r!r} — not a deliverable handle and not a known "
        f"contact alias. Use a phone '+<E.164>' (e.g. '+15551234567'), "
        f"'chat_guid:<GUID>', an email handle, or a name/alias that's in "
        f"contacts.sqlite (run `contacts list` to see what's registered, or "
        f"add the person to Apple Contacts and run `python3 "
        f"~/.config/spratt/skills/sync-contacts.py`)."
    )

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient   TEXT NOT NULL,
    channel     TEXT NOT NULL DEFAULT 'imessage',
    body        TEXT NOT NULL,
    send_at     TEXT NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    source      TEXT,
    created_by  TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    delivered_at TEXT,
    failed_at   TEXT,
    error       TEXT,
    trip_id     TEXT
);

CREATE INDEX IF NOT EXISTS idx_pending ON messages(status, send_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_source ON messages(source);
"""


class OutboxDB:
    def __init__(self, db_path=None, allow_create=False):
        """Open the outbox DB. Refuses to auto-create unless allow_create=True.

        allow_create=True is only for explicit init paths. Normal operational
        code (sender daemon, briefings, trip outbox gen) must NOT pass it —
        missing DB should be a loud failure, not a silent empty-DB creation.
        """
        self.db_path = db_path or DB_PATH
        if not allow_create:
            require_db_file(self.db_path, "outbox")
        else:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        # SCHEMA uses CREATE TABLE IF NOT EXISTS — safe no-op when table exists,
        # and on a freshly-initialized DB (allow_create=True) it lays down the
        # correct current schema.
        self.conn.executescript(SCHEMA)

    def _now(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def _parse_send_at(self, send_at):
        if send_at == "now":
            return self._now()
        # Normalize to SQLite-compatible format: "YYYY-MM-DD HH:MM:SS"
        s = send_at.replace("Z", "+00:00") if send_at.endswith("Z") else send_at
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo:
                dt = dt.astimezone(timezone.utc)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            # If parsing fails, strip T and Z for basic compatibility
            return send_at.replace("T", " ").replace("Z", "")

    def schedule(self, recipient, body, send_at="now", source=None, created_by=None, priority=0, max_retries=3, trip_id=None):
        # Resolve names/aliases ("Manan", "family chat") to handles before validating,
        # so callers can pass either a phone number or a household alias.
        recipient = _resolve_recipient(recipient)
        _validate_recipient(recipient)
        send_at = self._parse_send_at(send_at)
        cur = self.conn.execute(
            """INSERT INTO messages (recipient, body, send_at, priority, source, created_by, max_retries, trip_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (recipient, body, send_at, priority, source, created_by, max_retries, trip_id),
        )
        self.conn.commit()
        return cur.lastrowid

    def cancel(self, id=None, source=None):
        if id:
            self.conn.execute(
                "UPDATE messages SET status='cancelled', updated_at=? WHERE id=? AND status='pending'",
                (self._now(), id),
            )
        elif source:
            self.conn.execute(
                "UPDATE messages SET status='cancelled', updated_at=? WHERE source LIKE ? AND status='pending'",
                (self._now(), source + "%"),
            )
        self.conn.commit()
        return self.conn.total_changes

    def update(self, id, body=None, send_at=None):
        updates = []
        params = []
        if body is not None:
            updates.append("body=?")
            params.append(body)
        if send_at is not None:
            updates.append("send_at=?")
            params.append(self._parse_send_at(send_at))
        if not updates:
            return 0
        updates.append("updated_at=?")
        params.append(self._now())
        params.append(id)
        self.conn.execute(
            f"UPDATE messages SET {', '.join(updates)} WHERE id=? AND status='pending'",
            params,
        )
        self.conn.commit()
        return self.conn.total_changes

    def mark_delivered(self, id):
        now = self._now()
        self.conn.execute(
            "UPDATE messages SET status='delivered', delivered_at=?, updated_at=? WHERE id=?",
            (now, now, id),
        )
        self.conn.commit()

    def mark_failed(self, id, error=None):
        now = self._now()
        self.conn.execute(
            "UPDATE messages SET status='failed', failed_at=?, error=?, updated_at=? WHERE id=?",
            (now, error, now, id),
        )
        self.conn.commit()

    def increment_retry(self, id):
        self.conn.execute(
            "UPDATE messages SET retry_count = retry_count + 1, updated_at=? WHERE id=?",
            (self._now(), id),
        )
        self.conn.commit()

    def get_pending(self):
        return self.conn.execute(
            """SELECT * FROM messages
               WHERE status='pending' AND send_at <= datetime('now')
               ORDER BY priority DESC, send_at ASC"""
        ).fetchall()

    def list_messages(self, status=None, source=None, since=None):
        conditions = []
        params = []
        if status:
            conditions.append("status=?")
            params.append(status)
        if source:
            conditions.append("source LIKE ?")
            params.append(source + "%")
        if since:
            conditions.append("created_at >= ?")
            params.append(since)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        return self.conn.execute(
            f"SELECT * FROM messages{where} ORDER BY send_at DESC LIMIT 50", params
        ).fetchall()

    def get_overdue(self, minutes=5):
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
        return self.conn.execute(
            "SELECT * FROM messages WHERE status='pending' AND send_at <= ? ORDER BY send_at",
            (cutoff,),
        ).fetchall()

    def status_counts(self):
        rows = self.conn.execute(
            "SELECT status, COUNT(*) as cnt FROM messages GROUP BY status"
        ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    def close(self):
        self.conn.close()


def format_row(r):
    status_icon = {"pending": "⏳", "delivered": "✅", "failed": "❌", "cancelled": "🚫"}.get(r["status"], "?")
    return f'{status_icon} [{r["id"]}] {r["status"]:10s} | to={r["recipient"]:20s} | at={r["send_at"]} | src={r["source"] or "-":30s} | {r["body"][:60]}'


def main():
    parser = argparse.ArgumentParser(prog="outbox", description="Spratt Message Outbox")
    sub = parser.add_subparsers(dest="command")

    # schedule
    p = sub.add_parser("schedule")
    p.add_argument("--to", required=True, help="Recipient: chat-id or phone number")
    p.add_argument("--body", required=True, help="Message text")
    p.add_argument("--at", default="now", help="Send time (ISO 8601 UTC or 'now')")
    p.add_argument("--source", help="Source tag for grouping")
    p.add_argument("--created-by", default="manual", help="Who created this")
    p.add_argument("--priority", type=int, default=0, help="Priority (higher = first)")
    p.add_argument("--max-retries", type=int, default=3)
    p.add_argument("--trip-id", default=None, help="Trip ID for cross-referencing trips table")

    # cancel
    p = sub.add_parser("cancel")
    p.add_argument("--id", type=int, help="Message ID")
    p.add_argument("--source", help="Cancel all with this source prefix")

    # update
    p = sub.add_parser("update")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--body", help="New message text")
    p.add_argument("--at", help="New send time")

    # list
    p = sub.add_parser("list")
    p.add_argument("--status", help="Filter by status")
    p.add_argument("--source", help="Filter by source prefix")
    p.add_argument("--since", help="Filter by created_at (e.g., '24h' or ISO date)")
    p.add_argument("--json", action="store_true", help="JSON output")

    # status
    sub.add_parser("status")

    # overdue
    sub.add_parser("overdue")

    # init — explicit first-time setup (creates empty outbox.sqlite)
    sub.add_parser("init", help="Create a new empty outbox DB at the expected path")

    args = parser.parse_args()

    # init is the only path that's allowed to create the DB file.
    if args.command == "init":
        if os.path.exists(DB_PATH):
            print(f"Outbox DB already exists at {DB_PATH} — nothing to do.")
            sys.exit(0)
        OutboxDB(allow_create=True).close()
        print(f"Initialized new outbox DB at {DB_PATH}")
        sys.exit(0)

    db = OutboxDB()

    if args.command == "schedule":
        try:
            row_id = db.schedule(
                recipient=args.to, body=args.body, send_at=args.at,
                source=args.source, created_by=args.created_by,
                priority=args.priority, max_retries=args.max_retries,
                trip_id=args.trip_id,
            )
        except ValueError as e:
            # Print to stderr and exit 1 so callers (cron prompts, shell scripts)
            # see a clear, single-line error instead of a Python traceback.
            print(f"outbox: refusing to schedule — {e}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"id": row_id, "status": "scheduled"}))

    elif args.command == "cancel":
        if not args.id and not args.source:
            print("Error: --id or --source required", file=sys.stderr)
            sys.exit(1)
        count = db.cancel(id=args.id, source=args.source)
        print(json.dumps({"cancelled": count}))

    elif args.command == "update":
        count = db.update(id=args.id, body=args.body, send_at=args.at)
        print(json.dumps({"updated": count}))

    elif args.command == "list":
        since = None
        if args.since:
            if args.since.endswith("h"):
                hours = int(args.since[:-1])
                since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                since = args.since

        rows = db.list_messages(status=args.status, source=args.source, since=since)
        if hasattr(args, "json") and args.json:
            print(json.dumps([dict(r) for r in rows], indent=2))
        else:
            if not rows:
                print("No messages found.")
            for r in rows:
                print(format_row(r))

    elif args.command == "status":
        counts = db.status_counts()
        total = sum(counts.values())
        print(f"Outbox: {total} total")
        for status, cnt in sorted(counts.items()):
            icon = {"pending": "⏳", "delivered": "✅", "failed": "❌", "cancelled": "🚫"}.get(status, "?")
            print(f"  {icon} {status}: {cnt}")

    elif args.command == "overdue":
        rows = db.get_overdue()
        if not rows:
            print("No overdue messages.")
        for r in rows:
            print(format_row(r))

    else:
        parser.print_help()

    db.close()


if __name__ == "__main__":
    main()
