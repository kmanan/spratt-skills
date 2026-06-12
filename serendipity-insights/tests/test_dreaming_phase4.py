#!/usr/bin/env python3
"""Smoke tests for Phase 4 dreaming ledger scripts."""

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


build_pack = load_module("build_dream_input_pack", ROOT / "infrastructure/dreaming/build-dream-input-pack.py")
record_obs = load_module("record_dream_observations", ROOT / "infrastructure/dreaming/record-dream-observations.py")
review_obs = load_module("review_dream_observations", ROOT / "infrastructure/dreaming/review-dream-observations.py")


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
              surfaced_at TEXT,
              dismissed_at TEXT,
              accepted_at TEXT,
              classification TEXT,
              context_refs_json TEXT NOT NULL DEFAULT '[]',
              capabilities_json TEXT NOT NULL DEFAULT '{}',
              actions_json TEXT NOT NULL DEFAULT '[]',
              surface_channel TEXT,
              last_surfaced_at TEXT,
              cooldown_until TEXT,
              outcome TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO insights (
              id, stable_key, decision_id, kind, owner, title, summary,
              suggested_action, source, source_ref, evidence_json, confidence,
              created_at, updated_at, status, reconciliation_state,
              surface_policy, classification
            )
            VALUES (
              'i1', 'sk1', 'd1', 'food', 'manan', 'Dinner idea',
              'Vegetarian anchor dinner', 'Suggest concrete places', 'email',
              'email-1', '{}', 0.8, '2026-06-12T00:00:00Z',
              '2026-06-12T00:00:00Z', 'candidate', 'optional_residue',
              'optional', 'optional_residue'
            )
            """
        )
        conn.commit()


def test_pack_record_review():
    with tempfile.TemporaryDirectory() as td:
        temp = Path(td)
        insights_db = temp / "insights.sqlite"
        outbox_db = temp / "outbox.sqlite"
        ledger = temp / "dream-observations.jsonl"
        memory_candidates = temp / "memory-candidates.jsonl"
        ops_history = temp / "dreaming.jsonl"
        seed_insights(insights_db)

        build_pack.INSIGHTS_DB = insights_db
        build_pack.OUTBOX_DB = outbox_db
        build_pack.PACK_DIR = temp / "input-packs"
        pack = build_pack.build_pack(days=14, limit=10)
        assert pack["insights"]["recent"]
        assert pack["do_not_promote_rules"]

        record_obs.LEDGER = ledger
        row = record_obs.validate_and_normalize(
            {
                "observation": "Manan benefits from concrete vegetarian dinner candidates on trips.",
                "input_refs": [{"type": "insight", "id": "i1"}],
                "classification": "possible_profile_learning",
                "recommended_action": "Create a memory candidate.",
                "evidence_summary": "One accepted-looking insight.",
                "confidence": 0.7,
                "promotion_target": "memory_profile",
                "why_not_directly_actionable": "Needs review.",
            },
            dream_stage="manual",
            input_pack="test-pack",
        )
        assert record_obs.append_rows([row]) == 1

        review_obs.LEDGER = ledger
        review_obs.MEMORY_CANDIDATES = memory_candidates
        review_obs.OPS_HISTORY = ops_history
        rows = review_obs.load_rows()
        promoted_to = review_obs.promote(rows[0], reviewer="test", note="ok")
        review_obs.mark_reviewed(rows[0], reviewer="test", decision="promoted", note="ok", promoted_to=promoted_to)
        review_obs.write_rows(rows)
        assert memory_candidates.exists()
        reviewed = review_obs.load_rows()[0]
        assert reviewed["status"] == "reviewed"
        assert reviewed["review_decision"] == "promoted"


if __name__ == "__main__":
    test_pack_record_review()
    print("dreaming phase4 tests passed")
