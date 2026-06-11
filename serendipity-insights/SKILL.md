---
name: serendipity-insights
description: Use when Spratt notices a potentially useful opportunity, pattern, recommendation, or "worth surfacing" idea that is not a direct user command. Routes all such candidates through the shared insights queue instead of memory, profiles, dreaming, heartbeat, or one-off tables.
---

# Serendipity Insights

This skill is the routing contract for Spratt's insight layer. It does not replace
the runtime code. It tells agents to use the one runtime layer that already
exists.

## Runtime

- Shared API: `~/.config/spratt/infrastructure/lib/insights.py`
- Runtime DB: `~/.config/spratt/db/insights.sqlite`
- Packaged helper: `scripts/insights.py`
- Packaged schema: `schemas/insights.sql`
- Profile source: `~/.config/spratt/memory/profiles.json`
- Main readers: briefing/digest gather via `fetch_surfaceable`
- Current producers: email-scan opportunities, briefing opportunity refresh,
  discovery-butler surfaced picks

## What Belongs Here

Use `upsert_insight` when Spratt notices something potentially useful but not
commanded directly:

- A travel planning gap, such as an upcoming trip with no hotel or anchor meal.
- A saved place that matches an upcoming trip or current location.
- A card benefit, expiring credit, or purchase optimization worth mentioning.
- A calendar/reminder conflict or opportunity that is not urgent enough for an alert.
- A discovery recommendation that was surfaced and should not repeat soon.
- A cross-source pattern that could help a briefing or digest.

The insight should be compact: title, summary, suggested action, source,
source_ref, evidence JSON, confidence, status, reconciliation_state, and
surface_policy.

## What Does Not Belong Here

- Human todos: Apple Reminders is the source of truth.
- Scheduled messages: outbox is the source of truth.
- Trips: `trips.sqlite` and `memory/trips/*.md` are the source of truth.
- Durable personal preferences: `memory/profiles.json` is the source of truth.
- Spratt commitments: `memory/commitments.md` is the source of truth.
- Infrastructure logs, heartbeat, cron status, stack traces, or health output:
  `state/ops-history/*.jsonl`, not memory or insights.

Do not write candidate opportunities into `MEMORY.md`, person profile Markdown,
`memory/daily`, `DREAMS.md`, heartbeat output, or a new SQLite table.

## Status Rules

- `candidate`: noticed but not fully reconciled.
- `reconciled`: checked against deterministic source-of-truth stores and still useful.
- `surfaced`: already sent or shown; keep for cooldown/dedup history.
- `stale`: contradicted, expired, or already solved elsewhere.

Before surfacing an insight, reconcile against the relevant deterministic stores:
calendar, reminders, trips, saved places, orders, cards, outbox, and current
profile data.

## Surface Rules

- Briefings/digests may include only a small number of high-confidence,
  non-expired insights.
- Insights suggest action; they do not book, order, schedule, or create
  reminders without the normal workflow and confirmation rules.
- Discovery-butler can send a casual nudge, then record the surfaced item as
  `status="surfaced"` for cooldown and history.
- Heartbeat must not carry this content.
- Dreaming must not be used as the production queue.

## Inspection

```bash
sqlite3 ~/.config/spratt/db/insights.sqlite \
  "SELECT status, kind, owner, title, source, source_ref, updated_at FROM insights ORDER BY updated_at DESC LIMIT 20;"
```

## Minimal Write Pattern

```python
from infrastructure.lib.insights import upsert_insight

upsert_insight(
    kind="travel_planning",
    owner="manan",
    title="Plan one anchor meal for Lisle",
    summary="Upcoming trip has no meal or activity anchor yet.",
    suggested_action="Pick one vegetarian-friendly dinner near the hotel.",
    source="briefing",
    source_ref="trip-2026-06-17-lisle:anchor-meal",
    evidence={"trip_id": "trip-2026-06-17-lisle"},
    confidence=0.78,
    status="candidate",
    reconciliation_state="unreconciled",
    surface_policy="optional",
)
```

## Failure Mode To Avoid

Do not create another "intelligence layer." If a new producer notices something,
wire it into `db/insights.sqlite`. If a new surface wants ideas, read from
`fetch_surfaceable`. The skill exists to prevent parallel queues and memory
pollution.
