#!/usr/bin/env python3
"""Record structured dream observations into the review ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(os.path.expanduser("~/.config/spratt"))
LEDGER = ROOT / "state" / "dream-ledger" / "dream-observations.jsonl"

ALLOWED_CLASSIFICATIONS = {
    "possible_profile_learning",
    "possible_workflow_learning",
    "possible_insight_candidate",
    "producer_quality_issue",
    "noise",
}
ALLOWED_PROMOTION_TARGETS = {
    "none",
    "memory_profile",
    "memory_lesson",
    "insight_candidate",
    "ops_history",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def stable_id(observation: dict[str, Any]) -> str:
    raw = json.dumps({
        "observation": observation.get("observation"),
        "input_refs": observation.get("input_refs") or [],
        "classification": observation.get("classification"),
        "recommended_action": observation.get("recommended_action"),
    }, ensure_ascii=False, sort_keys=True)
    return "dream-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def load_payload(path: str) -> Any:
    if path == "-":
        text = sys.stdin.read()
    else:
        text = Path(path).read_text(encoding="utf-8")
    return json.loads(text)


def normalize_observations(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("observations"), list):
        return payload["observations"]
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return [payload]
    raise ValueError("dream output must be a JSON object, list, or {observations: [...]}")


def validate_and_normalize(observation: dict[str, Any], *, dream_stage: str, input_pack: str) -> dict[str, Any]:
    if not isinstance(observation, dict):
        raise ValueError("observation must be an object")
    text = " ".join(str(observation.get("observation") or "").split())
    if not text:
        raise ValueError("observation is required")
    input_refs = observation.get("input_refs") or []
    if not isinstance(input_refs, list) or not input_refs:
        raise ValueError("input_refs must be a non-empty list")
    classification = observation.get("classification") or "noise"
    if classification not in ALLOWED_CLASSIFICATIONS:
        raise ValueError(f"invalid classification: {classification}")
    promotion_target = observation.get("promotion_target") or "none"
    if promotion_target not in ALLOWED_PROMOTION_TARGETS:
        raise ValueError(f"invalid promotion_target: {promotion_target}")
    try:
        confidence = max(0.0, min(1.0, float(observation.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    row = {
        "id": observation.get("id") or stable_id(observation),
        "created_at": now_utc(),
        "dream_stage": dream_stage,
        "input_pack": input_pack,
        "input_refs": input_refs,
        "observation": text[:1200],
        "classification": classification,
        "recommended_action": " ".join(str(observation.get("recommended_action") or "").split())[:1200],
        "evidence_summary": " ".join(str(observation.get("evidence_summary") or "").split())[:1200],
        "confidence": confidence,
        "promotion_target": promotion_target,
        "why_not_directly_actionable": " ".join(str(observation.get("why_not_directly_actionable") or "").split())[:1200],
        "status": "pending_review",
        "reviewed_at": "",
        "reviewer": "",
        "review_decision": "",
        "promoted_to": "",
        "review_note": "",
    }
    if not row["why_not_directly_actionable"]:
        row["why_not_directly_actionable"] = "Dream observations require review before promotion."
    return row


def existing_ids() -> set[str]:
    if not LEDGER.exists():
        return set()
    ids = set()
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            ids.add(json.loads(line).get("id"))
        except Exception:
            continue
    return ids


def append_rows(rows: list[dict[str, Any]]) -> int:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    seen = existing_ids()
    written = 0
    with LEDGER.open("a", encoding="utf-8") as f:
        for row in rows:
            if row["id"] in seen:
                continue
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            seen.add(row["id"])
            written += 1
    return written


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="JSON file or - for stdin")
    parser.add_argument("--dream-stage", default="manual", choices=["light", "rem", "deep", "manual"])
    parser.add_argument("--input-pack", default="")
    args = parser.parse_args()

    payload = load_payload(args.input)
    rows = [
        validate_and_normalize(obs, dream_stage=args.dream_stage, input_pack=args.input_pack)
        for obs in normalize_observations(payload)
    ]
    written = append_rows(rows)
    print(f"recorded {written} dream observation(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
