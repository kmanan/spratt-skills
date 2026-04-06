#!/usr/bin/env python3
"""
Spratt Message Outbox — CLI + Python API

Single source of truth for all outbound messages. Every iMessage from Spratt
or any background process goes through this outbox. The sender.py daemon is
the only thing that actually calls imsg.

CLI Usage:
  outbox.py schedule --to "9" --body "Dinner at 7:30" --at "2026-04-02T20:00:00Z" --source "trip:dc"
  outbox.py schedule --to "+1XXXXXXXXXX" --body "Landed!" --at now --source "flight:AS3" --priority 10
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
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    delivered_at TEXT,
    failed_at   TEXT,
    error       TEXT
);

CREATE INDEX IF NOT EXISTS idx_pending ON messages(status, send_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_source ON messages(source);
"""


class OutboxDB:
    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
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

    args = parser.parse_args()
    db = OutboxDB()

    if args.command == "schedule":
        row_id = db.schedule(
            recipient=args.to, body=args.body, send_at=args.at,
            source=args.source, created_by=args.created_by,
            priority=args.priority, max_retries=args.max_retries,
            trip_id=args.trip_id,
        )
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
