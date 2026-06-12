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

Serendipity is notice + reconcile + act on structured truth + surface only the
residue. Use `upsert_insight` only after the relevant source-of-truth store has
been checked and updated when possible.

The insight queue is for unresolved residue:

- A travel planning gap, such as an upcoming trip with no hotel or anchor meal.
- A saved place that matches an upcoming trip or current location.
- A card benefit, expiring credit, or purchase optimization worth mentioning.
- A calendar/reminder conflict or opportunity that is not urgent enough for an alert.
- A discovery recommendation that was surfaced and should not repeat soon.
- A cross-source pattern that could help a briefing or digest.

Do not store a fact as an insight when it belongs in a deterministic store. If
an email or weak signal mentions concrete flights, hotels, reservations,
orders, cards, reminders, or calendar facts, reconcile that against the source
store first. Update the store through the domain tool when the fact is missing
or corrected. Mark the weak signal stale or redundant when the fact is already
confirmed.

The insight should be compact: title, summary, suggested action, source,
source_ref, evidence JSON, confidence, status, reconciliation_state, and
surface_policy.

## Reconciliation Contract

Every producer must follow this order:

1. Identify the domain and source-of-truth store.
2. Extract hard facts from the source.
3. Check the current store for matching, missing, contradictory, or superseded
   facts.
4. If the hard fact is missing or corrected, update the store through the domain
   tool, not raw SQL.
5. Regenerate deterministic downstream artifacts when that domain requires it.
6. Only write an insight for the remaining unresolved optional action.

Examples:

- Travel flights/hotels/reservations -> `trip-db.py` / `trips.sqlite` first,
  then regenerate trip outbox rows. Only create an insight for a remaining gap
  such as "pick one anchor dinner."
- A weak email asking "did you rebook with Delta?" is not an opportunity if
  confirmation emails or `trips.sqlite` already show the Delta flights. It is
  redundant evidence and should be stale or ignored.
- A saved restaurant near an upcoming trip can become an insight only after
  checking the trip dates, existing reservations, and profile constraints.
- An expiring card benefit can become an insight only after checking card usage
  and whether the benefit has already been consumed.

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
- `redundant`: weak signal matched a fact already confirmed in the source store.

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
