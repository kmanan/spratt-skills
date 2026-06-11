#!/usr/bin/env python3
"""Single candidate insight queue for Spratt.

Facts remain in structured stores. This table stores what Spratt has noticed,
after or before deterministic reconciliation, and gives surfaces one place to
read optional high-signal ideas.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any

ROOT = os.path.expanduser("~/.config/spratt")
INSIGHTS_DB = os.path.join(ROOT, "db/insights.sqlite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS insights (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  owner TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  suggested_action TEXT,
  source TEXT NOT NULL,
  source_ref TEXT,
  evidence_json TEXT NOT NULL,
  confidence REAL NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  expires_at TEXT,
  status TEXT NOT NULL,
  reconciliation_state TEXT NOT NULL,
  surface_policy TEXT NOT NULL,
  surfaced_at TEXT,
  dismissed_at TEXT,
  accepted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_insights_surface
ON insights(status, owner, confidence, expires_at, created_at);
CREATE INDEX IF NOT EXISTS idx_insights_source_ref
ON insights(source, source_ref);
"""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(INSIGHTS_DB), exist_ok=True)
    conn = sqlite3.connect(INSIGHTS_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def make_id(source: str, source_ref: str | None, kind: str, owner: str, title: str) -> str:
    raw = "|".join([source or "", source_ref or "", kind or "", owner or "", title or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def upsert_insight(
    *,
    kind: str,
    owner: str,
    title: str,
    summary: str = "",
    suggested_action: str = "",
    source: str,
    source_ref: str = "",
    evidence: dict[str, Any] | None = None,
    confidence: float = 0.7,
    expires_at: str = "",
    status: str = "candidate",
    reconciliation_state: str = "unreconciled",
    surface_policy: str = "optional",
    insight_id: str | None = None,
) -> str:
    confidence = max(0.0, min(1.0, float(confidence or 0.0)))
    insight_id = insight_id or make_id(source, source_ref, kind, owner, title)
    ts = now_utc()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO insights (
              id, kind, owner, title, summary, suggested_action, source,
              source_ref, evidence_json, confidence, created_at, updated_at,
              expires_at, status, reconciliation_state, surface_policy
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              kind=excluded.kind,
              owner=excluded.owner,
              title=excluded.title,
              summary=excluded.summary,
              suggested_action=excluded.suggested_action,
              source=excluded.source,
              source_ref=excluded.source_ref,
              evidence_json=excluded.evidence_json,
              confidence=excluded.confidence,
              updated_at=excluded.updated_at,
              expires_at=excluded.expires_at,
              status=excluded.status,
              reconciliation_state=excluded.reconciliation_state,
              surface_policy=excluded.surface_policy
            """,
            (
                insight_id,
                kind or "other",
                owner or "manan",
                title or "Untitled insight",
                summary or "",
                suggested_action or "",
                source,
                source_ref or "",
                json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True),
                confidence,
                ts,
                ts,
                expires_at or "",
                status,
                reconciliation_state,
                surface_policy,
            ),
        )
        conn.commit()
    return insight_id


def mark_stale_by_source_ref(source: str, source_ref: str, reason: str = "") -> None:
    if not source_ref:
        return
    with connect() as conn:
        conn.execute(
            """
            UPDATE insights
            SET status='stale',
                reconciliation_state='contradicted',
                updated_at=?,
                evidence_json=json_set(
                  CASE WHEN json_valid(evidence_json) THEN evidence_json ELSE '{}' END,
                  '$.stale_reason',
                  ?
                )
            WHERE source=? AND source_ref=? AND status IN ('candidate', 'reconciled')
            """,
            (now_utc(), reason, source, source_ref),
        )
        conn.commit()


def fetch_surfaceable(owner: str, limit: int = 3, min_confidence: float = 0.70) -> list[sqlite3.Row]:
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT title, summary, suggested_action, source_ref, expires_at,
                   kind, confidence
            FROM insights
            WHERE status IN ('candidate', 'reconciled')
              AND confidence >= ?
              AND owner IN (?, 'both', 'unknown')
              AND (expires_at IS NULL OR expires_at = '' OR date(expires_at) >= date('now'))
            ORDER BY
              CASE status WHEN 'reconciled' THEN 0 ELSE 1 END,
              confidence DESC,
              created_at DESC
            LIMIT ?
            """,
            (min_confidence, owner, limit),
        ).fetchall()
    return rows
