---
name: destination-aware
description: Surface relevant reminders, calendar context, and notes when Tesla navigation is set to a destination. Triggered by Home Assistant when sensor.maha_tesla_destination changes.
version: 2.0.0
---

# Destination-Aware Reminders

When the owner sets a destination in the Tesla, this skill surfaces relevant context (open reminders, calendar events) before they arrive.

## What changed in v2

- **Reminders carry destination categories as native Apple `#tag` hashtags in the title.** Trip-time matching is a deterministic set intersection between the reminder's tags and the destination's categories — no per-trip LLM judgment for tagged reminders.
- **Lead-time cadence ramp** replaces the binary "due today or skip" gate. Reminders with future deadlines still surface, with cooldowns that shorten as the due date approaches.
- **Tags are assigned at creation by the email-scan classifier** using a closed enum loaded from `known-destinations.json`. Low-confidence picks are surfaced via iMessage so the owner can correct them.
- **Untagged reminders are not an error.** They fall through to the legacy LLM filter — the same code path that ran in v1. The system never goes worse than v1; it only gets better as tagging coverage grows.

## Pipeline

```
Tesla nav set
  → Home Assistant WebSocket event (sensor.maha_tesla_destination)
  → destination-daemon receives the new destination
  → resolve to a known destination (case-insensitive match in known-destinations.json)
    or call Google Places via goplaces
  → load destination's categories (e.g. ["grocery", "pharmacy"] for QFC)
  → fetch open reminders from configured lists via remindctl
  → apply lead-time cadence gate (drops items in cooldown for their tier)
  → for each remaining reminder:
        tags_on_title ∩ destination.categories
        non-empty → surface (deterministic)
        empty + reminder has no tags → fall back to LLM filter per category
        empty + reminder has tags that don't match → drop silently
  → if anything to surface, compose one message and write to outbox
  → record each fire in reminder_fires.sqlite for the cadence gate
```

## Files in this skill

- `scripts/destination-daemon.py` — long-running WebSocket subscriber + matcher. Run by launchd.
- `scripts/destination-context.py` — single-shot context gatherer (resolves destination, fetches reminders, fetches today's calendar). Called by the daemon.
- `scripts/destination-summary.py` — ad-hoc human/JSON summary of open reminders and recent fires. Useful for spot checks and for composing weekly digest content.
- `scripts/backfill-tags.py` — one-time classifier pass over existing untagged reminders. Dry-run by default; `--apply` commits via `remindctl edit`.
- `scripts/known-destinations.json.example` — destination table + canonical category enum. Owner customizes per household.

## The category vocabulary

Single source of truth: the top-level `categories` array in `known-destinations.json`.

```json
{
  "categories": ["home", "work", "grocery", "pharmacy", "daycare", "medical", "restaurant"],
  "destinations": {
    "bright horizons": {"categories": ["daycare"], "name": "Bright Horizons"},
    "qfc":             {"categories": ["grocery", "pharmacy"], "name": "QFC"},
    ...
  }
}
```

Every layer reads this list at runtime — destination resolution, the daemon's tag parser, the email-scan classifier's enum, the spratt-health check, the summary script. Adding a new category (e.g. `gym`) is one JSON edit; all consumers pick it up automatically.

## Trip-time matching (`compose_message`)

1. Pull open reminders from configured lists (`remindctl show all --list <L> --json`).
2. Apply the lead-time cadence gate — see "Cadence tiers" below.
3. Partition eligible reminders into:
   - **Tagged:** title contains at least one `#<tag>` where `<tag>` is in the enum.
   - **Untagged:** no hashtags from the enum.
4. For **tagged** reminders, compute `parse_tags(title) ∩ destination.categories`. Non-empty → surface under the destination's highest-priority matching category. Tagged-but-non-matching is dropped silently — this is the deterministic fix for the cross-destination spam failure mode.
5. If no tagged reminder matched, run the legacy per-category LLM filter against **untagged** reminders only. Header gets a `?` suffix to indicate the picks are LLM-inferred rather than deterministic.
6. Compose one message. Send via the outbox. Record each fire in `reminder_fires.sqlite`.

Message header format: `<emoji> Heading to <Place> [<category>]` (or `[<category>?]` for LLM-fallback picks).

## Cadence tiers

Replaces the v1 binary "due today or skip" gate. Cooldown is tracked per-reminder in `reminder_fires.sqlite`.

| Time to due | Cooldown |
|---|---|
| Overdue / today / no due date | 0 (every matching trip) |
| ≤ 7 days | 0 (every matching trip) |
| 8–30 days | 1 day |
| > 30 days | 7 days |

A reminder created today with a due date 120 days out will fire about 18 times before its due date — once a week until ~30 days out, daily until ~7 days out, then every trip. No more "the reminder went silent for 140 days and reappeared the day before."

## Creating destination-aware reminders

When the owner asks for a reminder tied to a destination, follow these rules so the daemon surfaces it correctly.

### Tag the title

Append `#<category>` for each applicable category to the reminder title. Categories must be in the enum.

- `Take Sriram's blanket to Bright Horizons #daycare`
- `Pick up amoxicillin at QFC #pharmacy`
- `Get Sriram's allergy meds at Costco #pharmacy #grocery`
- `Provide updated employment authorization to HR #work`

`remindctl add "<title with tags>"` works directly — Apple Reminders renders the hashtags as styled tags, and the daemon's `parse_tags` extracts them.

### Recurring day-specific reminders

Use the apple-reminders skill's `create-recurring-reminder` helper. The tag goes in the title:

```bash
create-recurring-reminder "Take Sriram's blanket to Bright Horizons #daycare" monday 07:30 Shared
```

The user completes the reminder each week on their phone; Apple auto-advances the due date. If they don't complete it, it stays overdue and fires on subsequent matching trips (intended — nag behavior).

### One-time destination reminders

```bash
remindctl add --title "Get ginger at QFC #grocery" --list Shared
```

- Specific future date: add `--due <date>`. The cadence ramp determines firing frequency.
- "Next time I go to X": omit `--due`. Treated as overdue → fires every matching trip until completed.

### Permanent destination reminders

`remindctl add` with no due date and the appropriate tag. Fires on every matching trip until completed.

### What NOT to do

- **Don't put a destination reminder on a list the daemon doesn't check.** The list set is configured in `destination-context.py` (`REMINDER_LISTS`).
- **Don't create duplicate reminders.** Tagged-but-non-matching is silent, so two copies with different tags will both compete for the surface slot — confusing.
- **Don't tag with words outside the enum.** They're silently ignored by the matcher (set intersection with the enum filters them out), but they clutter the title.

## Automatic tagging at creation (email-scan path)

When email-scan creates a reminder from an inbound email (Outlook/Gmail), it runs a closed-enum classifier (`categorize_action`) with few-shot examples and a 0.7 confidence threshold:

- **≥ 0.7 confidence:** tag is appended to the title automatically; iMessage notification shows `(#<tag>)`.
- **< 0.7 confidence:** tag is NOT applied. iMessage notification reads `(untagged — best guess #foo at 55% confidence; add #tag manually if you want destination alerts)`.
- **No tag (classifier returned ""):** iMessage notification reads `(no destination tag — won't fire on Tesla nav)`.

This surfaces every untagged or low-confidence creation at the moment it happens, while the email context is fresh.

## Backfill of existing reminders

`scripts/backfill-tags.py` runs the same classifier over open reminders already in Apple Reminders.

```bash
# Dry-run: print proposals
python3 backfill-tags.py

# Apply high-confidence (≥0.7) suggestions, send iMessage summary
python3 backfill-tags.py --apply --notify

# Restrict to one list, override threshold
python3 backfill-tags.py --list Shared --threshold 0.8 --apply
```

The script never deletes or completes anything. It only edits titles via `remindctl edit --title "<new title with tag>"`.

## Observability

### `spratt-health` check

`check_destination_aware` in `system-health/check.py` reports:

- `open_total`, `tagged`, `untagged_pct` — coverage metrics across all configured lists.
- `by_category` — count of open reminders per tag.
- `untagged_sample` — up to 5 example untagged titles for diagnosis.
- `fires_7d`, `fires_by_category_7d` — activity in `reminder_fires.sqlite`.
- `fires_db_reachable` — whether the daemon has fired anything yet (file is created on first fire).
- `status` returns `WARNING` when:
  - ≥ 5 open reminders and > 50% are untagged (categorization gap), or
  - ≥ 3 tagged reminders exist but no fires in the last 7d (possible plumbing break).

The check returns to `ok` automatically once metrics recover. It never returns `CRITICAL` — this is observability, not delivery.

### Summary script

`destination-summary.py` prints the same information in human-readable form for ad-hoc checks. JSON output via `--json` for downstream composition.

### iMessage-first

Per the system's "observability lives in the outbox" rule, every degraded state has a path to the owner's phone:

- Wrong-tag firing on wrong destination → header in the fire message itself (`[grocery]` in the bracket)
- Reminder created untagged or low-confidence → iMessage at creation time (from email-scan)
- Coverage regressing systemwide → `spratt-health` WARNING → Grandmumma's periodic inspection alerts via outbox
- Daemon down / plumbing broken → `check_destination_daemon` returns CRITICAL → Grandmumma alerts immediately

## Important rules

- **NEVER send empty or low-value messages.** If nothing eligible passes the gate, stay silent.
- **One message per destination event.** The trigger fires once per nav input.
- **Keep messages short.** Read in 5 seconds while parking. Max 5 items.
- **Tagged-but-non-matching is silent.** A `#daycare` reminder at a grocery destination produces NO output. This is the entire point of v2.
- **Untagged reminders still work.** They fall through to the legacy LLM filter, exactly as they did in v1.
