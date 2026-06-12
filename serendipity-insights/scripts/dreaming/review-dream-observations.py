#!/usr/bin/env python3
"""Review and promote pending dream observations."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.expanduser("~/.config/spratt"))
from infrastructure.lib.serendipity import reconcile_signal

ROOT = Path(os.path.expanduser("~/.config/spratt"))
LEDGER = ROOT / "state" / "dream-ledger" / "dream-observations.jsonl"
MEMORY_CANDIDATES = ROOT / "state" / "dream-ledger" / "memory-candidates.jsonl"
OPS_HISTORY = ROOT / "state" / "ops-history" / "dreaming.jsonl"


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_rows() -> list[dict[str, Any]]:
    if not LEDGER.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def write_rows(rows: list[dict[str, Any]]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def find_row(rows: list[dict[str, Any]], observation_id: str) -> dict[str, Any]:
    for row in rows:
        if row.get("id") == observation_id:
            return row
    raise SystemExit(f"observation not found: {observation_id}")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def mark_reviewed(row: dict[str, Any], *, reviewer: str, decision: str, note: str, promoted_to: str = "") -> None:
    row["status"] = "reviewed"
    row["reviewed_at"] = now_utc()
    row["reviewer"] = reviewer
    row["review_decision"] = decision
    row["review_note"] = note
    row["promoted_to"] = promoted_to


def promote(row: dict[str, Any], *, reviewer: str, note: str) -> str:
    target = row.get("promotion_target") or "none"
    if target in {"memory_profile", "memory_lesson"}:
        candidate = {
            "created_at": now_utc(),
            "source": "dreaming",
            "dream_observation_id": row["id"],
            "target": target,
            "observation": row.get("observation"),
            "recommended_action": row.get("recommended_action"),
            "evidence_summary": row.get("evidence_summary"),
            "input_refs": row.get("input_refs") or [],
            "reviewer": reviewer,
            "note": note,
            "status": "memory_candidate",
        }
        append_jsonl(MEMORY_CANDIDATES, candidate)
        return str(MEMORY_CANDIDATES)
    if target == "insight_candidate":
        decision = reconcile_signal({
            "signal_id": row["id"],
            "source": "dreaming",
            "actor": "spratt",
            "domain_hints": ["dreaming"],
            "raw_context": {
                "kind": "dreaming",
                "title": row.get("observation") or "Dream observation",
                "summary": row.get("evidence_summary") or "",
                "suggested_action": row.get("recommended_action") or "",
                "score": row.get("confidence") or 0.0,
            },
            "claims": [{
                "type": "dreaming",
                "value": row,
                "confidence": row.get("confidence") or 0.0,
                "evidence_refs": [ref.get("id") or ref.get("source_ref") or row["id"] for ref in row.get("input_refs") or [] if isinstance(ref, dict)],
            }],
            "source_refs": [row["id"]],
            "created_at": now_utc(),
        })
        return f"insights.sqlite:{decision['decision_id']}"
    if target == "ops_history":
        event = {
            "created_at": now_utc(),
            "source": "dreaming",
            "dream_observation_id": row["id"],
            "classification": row.get("classification"),
            "observation": row.get("observation"),
            "recommended_action": row.get("recommended_action"),
            "evidence_summary": row.get("evidence_summary"),
            "reviewer": reviewer,
            "note": note,
        }
        append_jsonl(OPS_HISTORY, event)
        return str(OPS_HISTORY)
    return "none"


def cmd_list(args: argparse.Namespace) -> int:
    rows = load_rows()
    if args.status:
        rows = [row for row in rows if row.get("status") == args.status]
    for row in rows[: args.limit]:
        print(
            f"{row.get('id')} | {row.get('status')} | {row.get('classification')} | "
            f"{row.get('promotion_target')} | {row.get('confidence')} | {row.get('observation')}"
        )
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    rows = load_rows()
    row = find_row(rows, args.id)
    mark_reviewed(row, reviewer=args.reviewer, decision="rejected", note=args.note)
    write_rows(rows)
    print(f"rejected {args.id}")
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    rows = load_rows()
    row = find_row(rows, args.id)
    promoted_to = promote(row, reviewer=args.reviewer, note=args.note)
    mark_reviewed(row, reviewer=args.reviewer, decision="promoted", note=args.note, promoted_to=promoted_to)
    write_rows(rows)
    print(f"promoted {args.id} -> {promoted_to}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--status", default="pending_review")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.set_defaults(func=cmd_list)

    p_reject = sub.add_parser("reject")
    p_reject.add_argument("id")
    p_reject.add_argument("--reviewer", default="manan")
    p_reject.add_argument("--note", default="")
    p_reject.set_defaults(func=cmd_reject)

    p_promote = sub.add_parser("promote")
    p_promote.add_argument("id")
    p_promote.add_argument("--reviewer", default="manan")
    p_promote.add_argument("--note", default="")
    p_promote.set_defaults(func=cmd_promote)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
