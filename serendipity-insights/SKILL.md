---
name: serendipity-insights
description: Use when Spratt notices a potentially useful pattern, recommendation, missing context, or "worth surfacing" idea that is not a direct user command. Producers emit signals into the central reconciliation runtime; they do not write user-facing opportunities directly.
---

# Serendipity Insights

This skill documents Spratt's current serendipity capability as built in
production on 2026-06-12.

Serendipity is not a separate chat bot, generic opportunity inbox, heartbeat
feature, profile note, or dreaming diary. It is a central reconciliation runtime:

```text
producer signal
  -> retrieve available context
  -> reconcile against deterministic source-of-truth stores
  -> suppress stale/redundant/noisy signals
  -> write only unresolved residue to db/insights.sqlite
  -> surface candidates through briefings/digests/discovery surfaces
```

## Runtime Files

Production paths:

- Central runtime: `~/.config/spratt/infrastructure/lib/serendipity.py`
- Insight storage helper: `~/.config/spratt/infrastructure/lib/insights.py`
- Runtime DB: `~/.config/spratt/db/insights.sqlite`
- Profile source: `~/.config/spratt/memory/profiles.json`
- Dream input/ledger state: `~/.config/spratt/state/dream-ledger/`

Packaged repo files:

- `scripts/serendipity.py`
- `scripts/insights.py`
- `schemas/insights.sql`
- `scripts/dreaming/build-dream-input-pack.py`
- `scripts/dreaming/record-dream-observations.py`
- `scripts/dreaming/review-dream-observations.py`
- `scripts/dreaming/run-dream-cycle.py`
- `tests/test_serendipity_runtime.py`
- `tests/test_dreaming_phase4.py`
- `tests/test_dream_cycle_hook.py`

## Built Producers And Readers

Current producers emit signals through `reconcile_signal()`:

- email-scan saved opportunities
- briefing opportunity refresh
- discovery-butler surfaced picks

Current readers:

- briefing/digest gather reads surfaceable rows from `db/insights.sqlite`
- discovery-butler records surfaced outcomes for cooldown/history
- dream input pack reads recent insight decisions and outcomes

Old `opportunities.sqlite` is no longer a briefing surface source for this path.

## Signal Contract

Producers should call:

```python
from infrastructure.lib.serendipity import reconcile_signal

decision = reconcile_signal({
    "signal_id": "source-stable-id",
    "source": "email|briefing|discovery|cards|orders|reminders|trips|places|calendar|memory|dreaming|user",
    "actor": "manan|harshita|both|unknown",
    "domain_hints": ["travel", "food", "cards"],
    "raw_context": {
        "kind": "travel_planning",
        "title": "Short human-readable title",
        "summary": "What was noticed",
        "suggested_action": "Concrete next action, if any",
        "score": 0.78
    },
    "claims": [],
    "source_refs": [],
    "created_at": "ISO-8601"
})
```

Do not call `upsert_insight()` from producers. In current production,
`upsert_insight()` is an internal storage helper used by
`infrastructure.lib.serendipity`.

## What The Runtime Does Today

Built behavior:

- computes stable decision IDs and stable insight keys
- records context references and capability status in each decision
- checks profile availability through `memory/profiles.json`
- checks available tables in trips, cards, orders, and places stores
- checks OpenClaw memory-search status and records one of the runtime statuses
  such as `fts_ok`, `disabled`, `unavailable`, `local_ok`, or `remote_ok`
- suppresses confirmed duplicate lodging-style signals when `trips.sqlite`
  already has matching hotel state
- suppresses low-confidence signals as noise
- treats dreaming signals as review-only
- writes unresolved residue to `db/insights.sqlite`
- exposes `surface_candidates(owner, channel, limit)`
- records outcomes with `record_outcome(insight_id, outcome, note="")`

Not built yet:

- full domain-specific adapters for every source-of-truth table
- deterministic auto-writes for missing hard facts beyond existing
  producer-specific logic
- automatic promotion of dream observations into production behavior

## Insight Storage

`db/insights.sqlite` stores the residue and decision audit trail, not the source
of truth. It has the original insight columns plus:

- `stable_key`
- `decision_id`
- `classification`
- `context_refs_json`
- `capabilities_json`
- `actions_json`
- `surface_channel`
- `last_surfaced_at`
- `cooldown_until`
- `outcome`

Indexes:

- `idx_insights_surface`
- `idx_insights_source_ref`
- `idx_insights_stable_key`
- `idx_insights_decision`

Valid statuses/classes used today include:

- `candidate`
- `reconciled`
- `surfaced`
- `stale`
- `suppressed`
- `already_true`
- `optional_residue`
- `noise`
- `needs_review`

## Source-Of-Truth Boundary

Hard facts belong in deterministic stores, not insights:

- trips/flights/hotels/reservations: `trips.sqlite` through trip-manager tools
- reminders: Apple Reminders/remindctl
- scheduled messages: outbox
- saved places: places store
- orders/carts: order and Instacart stores
- cards/benefits: card wallet store
- durable preferences: profile/memory workflow, not insight rows
- infrastructure logs, heartbeat, cron status, stack traces: ops history, not
  memory, dreaming, or insights

An insight is only the unresolved residue after the source-of-truth check.

## Dreaming Integration

Dreaming is wired as a review loop, not as production authority.

Built files:

- `build-dream-input-pack.py` reads recent insight decisions, profile context,
  and recent outbox outcomes, then writes
  `state/dream-ledger/input-packs/YYYY-MM-DD.json`
- `run-dream-cycle.py` builds the pack, asks OpenClaw for strict JSON
  observations, and records them
- `record-dream-observations.py` validates structured observations and appends
  pending rows to `state/dream-ledger/dream-observations.jsonl`
- `review-dream-observations.py` lists, rejects, or promotes reviewed
  observations

Hard rule:

- Dreaming may not write memory, profiles, reminders, trips, outbox, or surfaced
  insights directly.
- Dreaming output starts as `pending_review`.
- Promotion happens only through the review command.

Current OpenClaw schedule:

- Job id: `0fe5eb3b-0cfe-4849-a66e-bde549959903`
- Name: `Serendipity Dream Cycle`
- Schedule: `17 4 * * 0` in `America/Los_Angeles`
- Payload kind: `command`
- Command:
  `/usr/bin/python3 /Users/spratt/.config/spratt/infrastructure/dreaming/run-dream-cycle.py --days 14 --limit 10 --dream-stage rem --model openai/gpt-5.5`
- Delivery: none
- Failure alert: after 1 failed run to Manan over iMessage
- Repo mirror: `~/.config/spratt/infrastructure/cron-jobs.json`

## Inspection

Recent decisions:

```bash
sqlite3 ~/.config/spratt/db/insights.sqlite \
  "SELECT status, classification, kind, owner, title, source, source_ref, updated_at FROM insights ORDER BY updated_at DESC LIMIT 20;"
```

Surfaceable rows:

```python
from infrastructure.lib.serendipity import surface_candidates

items = surface_candidates("manan", channel="briefing", limit=3)
```

Pending dream observations:

```bash
python3 ~/.config/spratt/infrastructure/dreaming/review-dream-observations.py list --status pending_review --limit 20
```

Dry-run the dream hook without a model call:

```bash
python3 ~/.config/spratt/infrastructure/dreaming/run-dream-cycle.py --dry-run --days 14 --limit 10
```

## Failure Modes To Avoid

- Do not create another intelligence layer.
- Do not add heartbeat content for serendipity.
- Do not write active todos into profiles or memory.
- Do not write ops/log/cron/heartbeat status into memory or dreaming.
- Do not ask the user a generic "pick an anchor" question when Spratt has
  profile, trip, location, place, or reservation tools that can produce concrete
  candidates.
- Do not surface a weak signal when deterministic stores already confirm the
  fact. Mark it stale/redundant or suppress it.
