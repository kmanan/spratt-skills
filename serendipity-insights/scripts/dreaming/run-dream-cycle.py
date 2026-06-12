#!/usr/bin/env python3
"""Run the serendipity -> dreaming -> review-ledger cycle.

This script automates only the reflection and recording step. It never promotes
observations and never writes memory/profile/trip/reminder/outbox state.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(os.path.expanduser("~/.config/spratt"))
DEFAULT_MODEL = os.environ.get("SPRATT_DREAM_MODEL", "openai/gpt-5.5")
BUILD_PACK = ROOT / "infrastructure" / "dreaming" / "build-dream-input-pack.py"
RECORDER = ROOT / "infrastructure" / "dreaming" / "record-dream-observations.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load module: {path}")
    spec.loader.exec_module(module)
    return module


def parse_json_text(text: str) -> Any:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for i, char in enumerate(text):
            if char in "[{":
                try:
                    obj, _end = decoder.raw_decode(text[i:])
                    return obj
                except json.JSONDecodeError:
                    continue
        raise


def extract_gateway_text(stdout: str) -> str:
    payload = json.loads(stdout)
    if isinstance(payload, dict):
        if isinstance(payload.get("outputs"), list) and payload["outputs"]:
            first = payload["outputs"][0]
            if isinstance(first, dict):
                return str(first.get("text") or first.get("content") or "")
        for key in ("text", "output", "content"):
            if payload.get(key):
                return str(payload[key])
    return stdout


def build_prompt(pack: dict[str, Any]) -> str:
    return (
        "You are Spratt's dreaming reflection step. Read the structured input "
        "pack and emit ONLY JSON. Do not write memory, reminders, trips, outbox, "
        "or insight rows. Produce at most 5 observations. Each observation must "
        "have: observation, input_refs, classification, recommended_action, "
        "evidence_summary, confidence, promotion_target, why_not_directly_actionable. "
        "Allowed classifications: possible_profile_learning, possible_workflow_learning, "
        "possible_insight_candidate, producer_quality_issue, noise. Allowed promotion_target: "
        "none, memory_profile, memory_lesson, insight_candidate, ops_history. "
        "Every observation must cite input_refs from the pack. If there is not "
        "enough signal, return {\"observations\": []}.\n\n"
        "INPUT_PACK_JSON:\n"
        f"{json.dumps(pack, ensure_ascii=False, sort_keys=True)}"
    )


def call_openclaw(prompt: str, *, model: str, timeout: int) -> dict[str, Any]:
    cmd = [
        "openclaw",
        "infer",
        "model",
        "run",
        "--gateway",
        "--model",
        model,
        "--json",
        "--prompt",
        prompt,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "openclaw infer failed")[:2000])
    text = extract_gateway_text(proc.stdout)
    parsed = parse_json_text(text)
    if isinstance(parsed, list):
        parsed = {"observations": parsed}
    if not isinstance(parsed, dict):
        raise RuntimeError("dream output was not a JSON object")
    if "observations" not in parsed:
        parsed = {"observations": [parsed]}
    return parsed


def build_pack(days: int, limit: int) -> tuple[dict[str, Any], Path]:
    module = load_module("build_dream_input_pack", BUILD_PACK)
    pack = module.build_pack(max(1, days), max(1, limit))
    output = module.PACK_DIR / f"{pack['window_end'][:10]}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(pack, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return pack, output


def record_observations(payload: dict[str, Any], *, input_pack: Path, dream_stage: str) -> int:
    module = load_module("record_dream_observations", RECORDER)
    rows = [
        module.validate_and_normalize(obs, dream_stage=dream_stage, input_pack=str(input_pack))
        for obs in module.normalize_observations(payload)
    ]
    return module.append_rows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--dream-stage", default="manual", choices=["light", "rem", "deep", "manual"])
    parser.add_argument("--dry-run", action="store_true", help="Build pack and prompt, but do not call OpenClaw or record observations")
    parser.add_argument("--mock-output", default="", help="Path to JSON output to record instead of calling OpenClaw")
    parser.add_argument("--prompt-output", default="", help="Optional path to write the prompt for inspection")
    args = parser.parse_args()

    pack, pack_path = build_pack(args.days, args.limit)
    prompt = build_prompt(pack)
    if args.prompt_output:
        Path(args.prompt_output).write_text(prompt, encoding="utf-8")

    if args.dry_run:
        print(json.dumps({"pack": str(pack_path), "prompt_chars": len(prompt), "dry_run": True}, sort_keys=True))
        return 0

    if args.mock_output:
        payload = parse_json_text(Path(args.mock_output).read_text(encoding="utf-8"))
        if isinstance(payload, list):
            payload = {"observations": payload}
    else:
        payload = call_openclaw(prompt, model=args.model, timeout=args.timeout)

    written = record_observations(payload, input_pack=pack_path, dream_stage=args.dream_stage)
    print(json.dumps({"pack": str(pack_path), "recorded": written, "model": args.model}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
