#!/usr/bin/env python3
"""Build a structured input pack for OpenClaw dreaming.

This is read-only with respect to production behavior: it reads structured
stores and writes a compact JSON pack under state/dream-ledger/input-packs.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import sys

sys.path.insert(0, os.path.expanduser("~/.config/spratt"))
from infrastructure.lib.profiles import load_profiles

ROOT = Path(os.path.expanduser("~/.config/spratt"))
INSIGHTS_DB = ROOT / "db" / "insights.sqlite"
OUTBOX_DB = ROOT / "db" / "outbox.sqlite"
PACK_DIR = ROOT / "state" / "dream-ledger" / "input-packs"

DO_NOT_PROMOTE_RULES = [
    "Dreaming may not write memory, profiles, reminders, trips, outbox, or surfaced insights directly.",
    "Dreaming output must become pending_review observations first.",
    "Ops, heartbeat, cron, logs, stack traces, and routine infrastructure status are not human memory.",
    "Only reviewed observations may feed back into serendipity or memory-candidate workflows.",
]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def read_json(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value) if value else fallback
    except Exception:
        return fallback


def fetch_insights(window_start: str, limit: int) -> dict[str, list[dict[str, Any]]]:
    if not INSIGHTS_DB.exists():
        return {"recent": [], "stale_or_rejected": [], "repeated_groups": []}
    with sqlite3.connect(INSIGHTS_DB) as conn:
        conn.row_factory = sqlite3.Row
        recent = conn.execute(
            """
            SELECT id, stable_key, decision_id, kind, owner, title, summary,
                   suggested_action, source, source_ref, confidence, status,
                   classification, reconciliation_state, surface_policy,
                   evidence_json, context_refs_json, capabilities_json,
                   actions_json, outcome, created_at, updated_at
            FROM insights
            WHERE datetime(updated_at) >= datetime(?)
              AND status IN ('candidate', 'reconciled', 'surfaced')
            ORDER BY updated_at DESC, confidence DESC
            LIMIT ?
            """,
            (window_start, limit),
        ).fetchall()
        stale = conn.execute(
            """
            SELECT id, stable_key, decision_id, kind, owner, title, summary,
                   suggested_action, source, source_ref, confidence, status,
                   classification, reconciliation_state, surface_policy,
                   evidence_json, context_refs_json, capabilities_json,
                   actions_json, outcome, created_at, updated_at
            FROM insights
            WHERE datetime(updated_at) >= datetime(?)
              AND (status IN ('stale', 'dismissed') OR outcome IN ('dismissed', 'rejected'))
            ORDER BY updated_at DESC, confidence DESC
            LIMIT ?
            """,
            (window_start, limit),
        ).fetchall()
        groups = conn.execute(
            """
            SELECT source, kind, owner, COUNT(*) AS count,
                   MAX(updated_at) AS latest_updated_at
            FROM insights
            WHERE datetime(updated_at) >= datetime(?)
            GROUP BY source, kind, owner
            HAVING COUNT(*) > 1
            ORDER BY count DESC, latest_updated_at DESC
            LIMIT ?
            """,
            (window_start, limit),
        ).fetchall()
    return {
        "recent": [normalize_insight(row) for row in recent],
        "stale_or_rejected": [normalize_insight(row) for row in stale],
        "repeated_groups": [dict(row) for row in groups],
    }


def normalize_insight(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for key in ("evidence_json", "context_refs_json", "capabilities_json", "actions_json"):
        item[key.removesuffix("_json")] = read_json(item.pop(key, ""), [] if key != "capabilities_json" else {})
    return item


def fetch_outbox_outcomes(window_start: str, limit: int) -> list[dict[str, Any]]:
    if not OUTBOX_DB.exists():
        return []
    try:
        with sqlite3.connect(OUTBOX_DB) as conn:
            conn.row_factory = sqlite3.Row
            columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
            if not {"id", "body"}.issubset(columns):
                return []
            ts_col = "created_at" if "created_at" in columns else "send_at" if "send_at" in columns else ""
            status_col = "status" if "status" in columns else ""
            source_col = "source" if "source" in columns else ""
            where = f"WHERE datetime({ts_col}) >= datetime(?)" if ts_col else ""
            params: tuple[Any, ...] = (window_start, limit) if ts_col else (limit,)
            rows = conn.execute(
                f"""
                SELECT id,
                       substr(body, 1, 500) AS body,
                       {ts_col if ts_col else "''"} AS created_at,
                       {status_col if status_col else "''"} AS status,
                       {source_col if source_col else "''"} AS source
                FROM messages
                {where}
                ORDER BY id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]
    except sqlite3.Error:
        return []


def build_pack(days: int, limit: int) -> dict[str, Any]:
    end = now_utc()
    start = end - timedelta(days=days)
    window_start = iso(start)
    return {
        "generated_at": iso(end),
        "window_start": window_start,
        "window_end": iso(end),
        "insights": fetch_insights(window_start, limit),
        "outcomes": {
            "recent_outbox": fetch_outbox_outcomes(window_start, limit),
        },
        "profile_context": load_profiles(),
        "do_not_promote_rules": DO_NOT_PROMOTE_RULES,
        "output_contract": {
            "required_fields": [
                "observation",
                "input_refs",
                "classification",
                "recommended_action",
                "confidence",
                "promotion_target",
                "why_not_directly_actionable",
            ],
            "allowed_classifications": [
                "possible_profile_learning",
                "possible_workflow_learning",
                "possible_insight_candidate",
                "producer_quality_issue",
                "noise",
            ],
            "allowed_promotion_targets": [
                "none",
                "memory_profile",
                "memory_lesson",
                "insight_candidate",
                "ops_history",
            ],
            "default_status": "pending_review",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    pack = build_pack(max(1, args.days), max(1, args.limit))
    PACK_DIR.mkdir(parents=True, exist_ok=True)
    output = Path(args.output) if args.output else PACK_DIR / f"{pack['window_end'][:10]}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(pack, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
