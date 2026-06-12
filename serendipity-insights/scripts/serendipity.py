#!/usr/bin/env python3
"""Central reconciliation runtime for Spratt insight signals.

Producers emit signals. This module decides whether the signal is a deterministic
write, a user-facing residue candidate, or noise. Domain executors stay outside
this module; this layer records decisions and only writes unresolved residue to
the insights ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

from infrastructure.lib import insights
from infrastructure.lib.profiles import load_profiles

ROOT = os.path.expanduser("~/.config/spratt")
TRIPS_DB = os.path.join(ROOT, "db/trips.sqlite")
CARDS_DB = os.path.join(ROOT, "db/cards.sqlite")
ORDERS_DB = os.path.join(ROOT, "db/orders.sqlite")
PLACES_DB = os.path.join(ROOT, "db/places.sqlite")

_MEMORY_STATUS_CACHE: tuple[float, str] | None = None


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def stable_hash(*parts: Any) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _safe_json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True)


def _owner(value: str | None) -> str:
    owner = (value or "manan").lower()
    return owner if owner in {"manan", "harshita", "both", "unknown"} else "manan"


def _memory_search_status() -> str:
    global _MEMORY_STATUS_CACHE
    now = time.time()
    if _MEMORY_STATUS_CACHE and now - _MEMORY_STATUS_CACHE[0] < 300:
        return _MEMORY_STATUS_CACHE[1]
    try:
        proc = subprocess.run(
            ["openclaw", "memory", "status", "--deep", "--agent", "spratt", "--json"],
            text=True,
            capture_output=True,
            timeout=6,
            check=False,
        )
        if proc.returncode != 0:
            status = "unavailable"
        else:
            payload = json.loads(proc.stdout or "{}")
            custom = payload.get("custom") or {}
            provider = payload.get("provider") or payload.get("requestedProvider")
            if provider == "none" or custom.get("searchMode") == "fts-only":
                status = "fts_ok"
            elif payload.get("enabled") is False:
                status = "disabled"
            elif provider in {"local", "ollama"}:
                status = "local_ok"
            elif provider:
                status = "remote_ok"
            else:
                status = "unavailable"
    except Exception:
        status = "unavailable"
    _MEMORY_STATUS_CACHE = (now, status)
    return status


def _table_exists(db_path: str, table: str) -> bool:
    if not os.path.exists(db_path):
        return False
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            return bool(row)
    except sqlite3.Error:
        return False


def _count_rows(db_path: str, table: str, where: str = "", params: tuple[Any, ...] = ()) -> int:
    if not _table_exists(db_path, table):
        return 0
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    try:
        with sqlite3.connect(db_path) as conn:
            return int(conn.execute(sql, params).fetchone()[0] or 0)
    except sqlite3.Error:
        return 0


def _trip_has_hotel(start_date: str = "", end_date: str = "") -> bool:
    if not _table_exists(TRIPS_DB, "hotels"):
        return False
    where = ""
    params: tuple[Any, ...] = ()
    if start_date:
        where = "(date(check_in) <= date(?) AND date(check_out) >= date(?))"
        params = (end_date or start_date, start_date)
    return _count_rows(TRIPS_DB, "hotels", where, params) > 0


def _context_refs(signal: dict[str, Any], capabilities: dict[str, str]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    try:
        profile_data = load_profiles()
        refs.append({
            "type": "profiles",
            "status": "ok",
            "people": sorted((profile_data.get("people") or {}).keys()),
        })
    except Exception as exc:
        refs.append({"type": "profiles", "status": "unavailable", "error": str(exc)})
    for label, db_path, tables in (
        ("trips", TRIPS_DB, ("trips", "flights", "hotels")),
        ("cards", CARDS_DB, ("cards", "benefits", "offers")),
        ("orders", ORDERS_DB, ("orders", "order_items", "cart_items")),
        ("places", PLACES_DB, ("places", "saved_places")),
    ):
        existing = [table for table in tables if _table_exists(db_path, table)]
        refs.append({"type": label, "status": "ok" if existing else "unavailable", "tables": existing})
    refs.append({"type": "memory_search", "status": capabilities["memory_search"]})
    if signal.get("source_refs"):
        refs.append({"type": "source_refs", "refs": signal.get("source_refs")})
    return refs


def _insight_from_signal(signal: dict[str, Any]) -> dict[str, Any]:
    raw = signal.get("raw_context") or {}
    claims = signal.get("claims") or []
    first_claim = claims[0] if claims else {}
    value = first_claim.get("value") if isinstance(first_claim, dict) else {}
    if not isinstance(value, dict):
        value = {}
    title = raw.get("title") or value.get("title") or raw.get("subject") or "Opportunity"
    summary = raw.get("summary") or raw.get("reason") or value.get("summary") or ""
    suggested_action = raw.get("suggested_action") or value.get("suggested_action") or ""
    return {
        "kind": raw.get("kind") or first_claim.get("type") or "other",
        "title": " ".join(str(title).split())[:120],
        "summary": " ".join(str(summary).split())[:500],
        "suggested_action": " ".join(str(suggested_action).split())[:240],
        "expires_at": raw.get("expires_at") or value.get("expires_at") or "",
        "confidence": float(raw.get("score") or first_claim.get("confidence") or signal.get("confidence") or 0.7),
    }


def _classify(signal: dict[str, Any], candidate: dict[str, Any]) -> tuple[str, str, str]:
    raw = signal.get("raw_context") or {}
    text = " ".join(
        str(raw.get(key) or "")
        for key in ("kind", "title", "summary", "suggested_action", "evidence")
    ).lower()
    if raw.get("resolved_by_trip"):
        return "already_true", "suppressed", "resolved by existing trip store"
    if "hotel" in text and _trip_has_hotel(raw.get("due_date") or "", raw.get("expires_at") or raw.get("due_date") or ""):
        return "already_true", "suppressed", "lodging already exists in trips.sqlite"
    if candidate["confidence"] < 0.45:
        return "noise", "suppressed", "confidence below surface threshold"
    if signal.get("source") == "dreaming":
        return "optional_residue", "needs_review", "dream observations require review before promotion"
    return "optional_residue", "candidate", "unresolved residue after available context checks"


def reconcile_signal(signal: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    source = signal.get("source") or "unknown"
    source_ref = signal.get("signal_id") or signal.get("source_ref") or ""
    owner = _owner(signal.get("actor") or signal.get("owner"))
    candidate = _insight_from_signal(signal)
    capabilities = {
        "memory_search": _memory_search_status(),
        "dreaming": "pending_review_only" if source == "dreaming" else "not_used",
        "wanderlust": "not_used",
        "table_reservation": "not_used",
        "instacart": "not_used",
        "reminders": "not_used",
    }
    context_refs = _context_refs(signal, capabilities)
    classification, status, reason = _classify(signal, candidate)
    decision_id = stable_hash("decision", source, source_ref, owner, candidate["kind"])
    stable_key = stable_hash(source, source_ref, owner, candidate["kind"])
    actions = [{
        "type": "mark_stale" if status == "suppressed" else "noop",
        "tool": "none",
        "mode": "auto",
        "payload": {},
        "result": {"reason": reason},
    }]
    decision = {
        "decision_id": decision_id,
        "signal_id": source_ref,
        "owner": owner,
        "domain": (signal.get("domain_hints") or [candidate["kind"] or "other"])[0],
        "classification": classification,
        "actions": actions,
        "insight": candidate,
        "context_refs": context_refs,
        "capabilities": capabilities,
        "status": status,
        "reason": reason,
    }
    if not dry_run:
        insight_status = "stale" if status == "suppressed" else status
        if insight_status == "needs_review":
            insight_status = "candidate"
        insights.upsert_insight(
            kind=candidate["kind"],
            owner=owner,
            title=candidate["title"],
            summary=candidate["summary"],
            suggested_action=candidate["suggested_action"],
            source=source,
            source_ref=source_ref,
            evidence={
                "signal": signal,
                "decision": {
                    "classification": classification,
                    "reason": reason,
                    "context_refs": context_refs,
                    "capabilities": capabilities,
                    "actions": actions,
                },
            },
            confidence=candidate["confidence"],
            expires_at=candidate["expires_at"],
            status=insight_status,
            reconciliation_state=classification,
            surface_policy="review_only" if status == "needs_review" else "optional",
            stable_key=stable_key,
            decision_id=decision_id,
            classification=classification,
            context_refs=context_refs,
            capabilities=capabilities,
            actions=actions,
        )
    return decision


def surface_candidates(owner: str, *, channel: str, limit: int = 3) -> list[dict[str, Any]]:
    rows = insights.fetch_surfaceable(owner, limit=limit)
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["surface_channel"] = channel
        result.append(item)
    return result


def record_outcome(insight_id: str, outcome: str, *, note: str = "") -> None:
    insights.record_outcome(insight_id, outcome, note=note)
