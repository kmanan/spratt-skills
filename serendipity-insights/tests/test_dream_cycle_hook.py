#!/usr/bin/env python3
"""Smoke tests for the OpenClaw dream-cycle hook."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import tempfile
from pathlib import Path

ROOT = Path(os.path.expanduser("~/.config/spratt"))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


cycle = load_module("run_dream_cycle", ROOT / "infrastructure/dreaming/run-dream-cycle.py")
build_pack = load_module("build_dream_input_pack", ROOT / "infrastructure/dreaming/build-dream-input-pack.py")
record_obs = load_module("record_dream_observations", ROOT / "infrastructure/dreaming/record-dream-observations.py")


def seed_insights(db_path: Path):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE insights (
              id TEXT PRIMARY KEY,
              stable_key TEXT,
              decision_id TEXT,
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
              classification TEXT,
              context_refs_json TEXT NOT NULL DEFAULT '[]',
              capabilities_json TEXT NOT NULL DEFAULT '{}',
              actions_json TEXT NOT NULL DEFAULT '[]',
              outcome TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO insights (
              id, kind, owner, title, summary, suggested_action, source,
              source_ref, evidence_json, confidence, created_at, updated_at,
              status, reconciliation_state, surface_policy, classification
            )
            VALUES ('i1', 'food', 'manan', 'Dinner idea', 'Vegetarian dinner',
                    'Suggest concrete places', 'email', 'email-1', '{}', 0.8,
                    '2026-06-12T00:00:00Z', '2026-06-12T00:00:00Z',
                    'candidate', 'optional_residue', 'optional', 'optional_residue')
            """
        )
        conn.commit()


def test_prompt_and_mock_recording():
    with tempfile.TemporaryDirectory() as td:
        temp = Path(td)
        build_pack.INSIGHTS_DB = temp / "insights.sqlite"
        build_pack.OUTBOX_DB = temp / "outbox.sqlite"
        build_pack.PACK_DIR = temp / "input-packs"
        record_obs.LEDGER = temp / "dream-observations.jsonl"
        seed_insights(build_pack.INSIGHTS_DB)

        cycle.BUILD_PACK = ROOT / "infrastructure/dreaming/build-dream-input-pack.py"
        cycle.RECORDER = ROOT / "infrastructure/dreaming/record-dream-observations.py"
        pack = build_pack.build_pack(14, 10)
        prompt = cycle.build_prompt(pack)
        assert "OUTPUT" not in prompt[:20]
        assert "observations" in prompt

        payload = {
            "observations": [{
                "observation": "Repeated dinner ideas may indicate a useful planning preference.",
                "input_refs": [{"type": "insight", "id": "i1"}],
                "classification": "possible_workflow_learning",
                "recommended_action": "Consider surfacing concrete candidates.",
                "evidence_summary": "One seeded insight.",
                "confidence": 0.6,
                "promotion_target": "ops_history",
                "why_not_directly_actionable": "Requires review.",
            }]
        }
        rows = [
            record_obs.validate_and_normalize(obs, dream_stage="manual", input_pack="test")
            for obs in record_obs.normalize_observations(payload)
        ]
        assert record_obs.append_rows(rows) == 1
        assert record_obs.LEDGER.exists()


if __name__ == "__main__":
    test_prompt_and_mock_recording()
    print("dream cycle hook tests passed")
