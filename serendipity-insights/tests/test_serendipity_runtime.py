#!/usr/bin/env python3
"""Smoke tests for the central serendipity runtime."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.expanduser("~/.config/spratt"))

from infrastructure.lib import insights, serendipity


def test_candidate_and_stable_key():
    with tempfile.TemporaryDirectory() as td:
        insights.INSIGHTS_DB = os.path.join(td, "insights.sqlite")
        serendipity.TRIPS_DB = os.path.join(td, "trips.sqlite")
        serendipity._MEMORY_STATUS_CACHE = (9999999999, "fts_ok")
        decision = serendipity.reconcile_signal({
            "signal_id": "email-1",
            "source": "email",
            "actor": "manan",
            "domain_hints": ["food"],
            "raw_context": {
                "kind": "food",
                "title": "Anchor dinner in Chicago",
                "summary": "One strong vegetarian option would help.",
                "suggested_action": "Suggest concrete places.",
                "score": 0.82,
            },
            "claims": [{
                "type": "place",
                "value": {"city": "Chicago"},
                "confidence": 0.82,
                "evidence_refs": ["email-1"],
            }],
            "source_refs": ["email-1"],
        })
        assert decision["status"] == "candidate"
        with sqlite3.connect(insights.INSIGHTS_DB) as conn:
            row = conn.execute(
                "SELECT stable_key, decision_id, classification, capabilities_json FROM insights"
            ).fetchone()
        assert row[0]
        assert row[1]
        assert row[2] == "optional_residue"
        assert "fts_ok" in row[3]


def test_existing_hotel_suppresses_signal():
    with tempfile.TemporaryDirectory() as td:
        insights.INSIGHTS_DB = os.path.join(td, "insights.sqlite")
        serendipity.TRIPS_DB = os.path.join(td, "trips.sqlite")
        serendipity._MEMORY_STATUS_CACHE = (9999999999, "fts_ok")
        with sqlite3.connect(serendipity.TRIPS_DB) as conn:
            conn.execute("CREATE TABLE hotels (check_in TEXT, check_out TEXT)")
            conn.execute("INSERT INTO hotels VALUES ('2026-06-17', '2026-06-18')")
            conn.commit()
        decision = serendipity.reconcile_signal({
            "signal_id": "email-hotel",
            "source": "email",
            "actor": "manan",
            "domain_hints": ["travel"],
            "raw_context": {
                "kind": "travel",
                "title": "Book hotel for Lisle",
                "summary": "Need hotel",
                "suggested_action": "Book hotel",
                "due_date": "2026-06-17",
                "expires_at": "2026-06-18",
                "score": 0.9,
            },
            "claims": [{"type": "hotel", "value": {}, "confidence": 0.9}],
            "source_refs": ["email-hotel"],
        })
        assert decision["status"] == "suppressed"
        with sqlite3.connect(insights.INSIGHTS_DB) as conn:
            row = conn.execute("SELECT status, classification FROM insights").fetchone()
        assert row == ("stale", "already_true")


if __name__ == "__main__":
    test_candidate_and_stable_key()
    test_existing_hotel_suppresses_signal()
    print("serendipity runtime tests passed")
