# Trip Watchers — Additive Flight-Event Recipients

A **watcher** is someone who receives a copy of every flight event for a trip *without* being a traveler on that trip — a parent following their kid's flight home, a partner CC'd on a business trip, an assistant looped in on a family member's travel.

This is an additive layer on top of the existing trip-manager + flight-monitor stack. **Nothing changes for trips that have no watchers.** Group chats still receive the primary message. Solo travelers still get their personal phone alerts. Watchers are nullable; everything works when the watcher list is empty (which is the default for every trip).

## The model

Watchers live in the existing `travelers` table with `role='watcher'`. No new table. The `role` column already existed for the `primary`/`companion` distinction; `watcher` is just another value.

```sql
travelers (
  id, trip_id, name, phone, role
)
```

Why reuse `travelers` instead of a new `trip_watchers` table:

- Same name → phone resolution via `contacts.sqlite` already implemented.
- Same trip-id FK and indexing.
- Same delete-cascade story when a trip is removed.
- A new table would have introduced a parallel schema for one extra column (`role`).

The primary-recipient logic (group chat for group trips, first traveler for solo) reads `travelers` *with* an explicit `role != 'watcher'` filter, so watchers cannot accidentally become the primary recipient. Symmetrically, `update_travelers_display` excludes watchers, so the `trips.travelers` display column keeps showing the actual flying party.

## Adding a watcher

```
trip-db.py add-watcher --trip <trip_id> --name "Manan Kakkar"
```

Phone is resolved from `contacts.sqlite` by name. If the name isn't in contacts (or you want to override), pass `--phone +13157082088` directly.

Validation:

- Trip must exist.
- Explicit `--phone` must be E.164 (starts with `+`).
- Contacts-resolved handle must be a phone (E.164), not a group-chat handle. Watchers are individuals; flight events fan out per-person.
- Idempotent on (trip, phone) — re-running the same `add-watcher` is a no-op, not an error.

Every failure path queues an outbox alert to the household manager via `_notify_manan`:

| Failure | Source tag |
|---|---|
| Trip doesn't exist | `trip-db:add-watcher:no-trip` |
| `--phone` not E.164 | `trip-db:add-watcher:bad-phone` |
| Name not in contacts, no `--phone` | `trip-db:add-watcher:no-contact` |
| Contacts returned a group-chat handle | `trip-db:add-watcher:group-handle` |
| DB insert error | `trip-db:add-watcher:db-error` |

## Fanout in the flight monitor

When `flight_monitor.notify()` queues an outbox row for the primary recipient, it then calls `get_watchers(trip_id)` and queues one additional outbox row per watcher with source `flight:<flight_number>:watcher`. The primary delivery is unchanged whether watchers exist or not.

Failure isolation:

- The watcher block is wrapped in its own try/except so a watcher-query exception cannot affect the primary send (which already happened by then).
- Per-watcher try/except — one bad phone or outbox error doesn't block subsequent watchers.
- Watchers with `NULL` phone are kept in the query result (not silently dropped) so the loop can emit a `watcher-bad-phone` system alert.
- A watcher whose phone equals the primary recipient is skipped — no double-message.

System alerts surfaced via `system_alert(flight, msg, code)`:

| Failure | Code |
|---|---|
| `get_watchers()` raised | `watcher-query-failed` |
| Watcher row had `NULL` or empty phone | `watcher-bad-phone` |
| Outbox enqueue raised | `watcher-fanout-failed` |

## LLM ingestion path

The LLM can add watchers from natural-language requests received over iMessage or email. The trigger phrases (documented in the production `trip-manager` skill) include:

- "track this flight and text me" / "send me updates on this flight"
- "add Manan as a watcher on trip X" / "CC Manan on flight updates"
- "I want to be alerted when their flight lands"

These map to `add-watcher --trip <id> --name <person>`. The LLM picks the right trip from `trip-db.py list` and the right name from the message or its memory.

## Why a dedicated subcommand (not `add-traveler --role watcher`)

`add-traveler` predates watchers and supports a fundamentally different shape of input:

- Travelers can be added without a phone (just a name on a manifest).
- Travelers can be group-chat handles in some edge flows.
- Adding a traveler also nudges trip-status downstream pipelines.

Watchers need stricter validation (E.164 phone required, no group handles, contacts-first resolution) and zero downstream side effects beyond inserting one row. Building both shapes into `add-traveler` would have made its behavior conditional on `role` — a footgun. A separate subcommand keeps `add-traveler` exactly as it was and gives watchers their own validated entry point.
