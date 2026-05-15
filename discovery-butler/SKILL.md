---
name: discovery-butler
description: Casual location-aware coffee / quick-bite nudges. Fires Thu 2pm (Fri fallback) for two principals at home, and day 1 (or 2/3 fallback) of any active trip into the trip chat. One per person per week, one per trip total.
---

# discovery-butler

A friend who knows the area and occasionally texts you a fun spot. Not a daily nag. Most cron firings produce nothing — that silence is the goal.

## What it does

Daily 2:00pm local-time cron. Most days, exits silently. When the conditions match, sends a single one-line iMessage:

```
☕ Weekend coffee idea: Sound & Fog — 6 min walk, third-wave, opened March. Walk-in.
☕ NYC pick: Devoción on Atlantic Ave — 10 min from your hotel. Walk-in.
☕ Seattle pick: Sey Coffee — 12 min from where you're staying. Walk-in.
```

## Firing rules

| Context | When | Who receives | Anchor |
|---|---|---|---|
| At home, no active trip | Thursday 2pm (default), Friday as fallback | Two principals (`MANAN_PHONE`, `HARSHITA_PHONE`) — separate messages | HA `person.<name>` entity → lat/lon, with `sensor.<name>_geocode` for the human-readable label |
| Active trip, day 1–3 | Day 1 (default), day 2 or 3 as fallback | `trips.group_chat_guid` if set; otherwise first traveler's phone from `travelers` table. **One message per trip, not per person.** | `trips.destination` (free-text — wanderlust-goat geocodes it) |

Dedup: `~/.config/spratt/db/discovery_fires.sqlite`, PK `(person, kind, key)`. Weekend kind keys on ISO week (e.g. `2026-W19`); trip kind keys on `trip_id`. A traveler who's already on an active trip is excluded from the at-home loop for that day.

## How it works

1. **Discovery** — `wanderlust-goat-pp-cli goat <anchor> --criteria "third-wave coffee" --minutes 12 --top 5 --agent` returns ranked picks (deterministic, no LLM). Fields used: `name`, `address`, `lat`, `lng`, `walking_minutes`, `score.total`, `why`, `google_maps_uri`, `business_status` (filter to `OPERATIONAL`).
2. **Dedup** — exclude picks whose lowercased name appears in `places.sqlite` (already-saved spots). First remaining pick wins.
3. **Compose** — `openclaw infer model run --gateway --model openai-codex/gpt-5.5 --prompt <...> --json` wraps the pick in a casual butler-voice line under 22 words. Flash fallback (`google/gemini-2.5-flash`). Final fallback is a deterministic template — never silent.
4. **Send** — `outbox.py schedule --to <recipient> --body <line> --at now --source discovery-butler --created-by discovery-butler`. Trip nudges include `--trip-id <trip_id>` for cross-referencing.
5. **Mark sent** — insert into `fires` so the same person/trip can't be re-nudged.

## Wiring

| | |
|---|---|
| **What you get** | `scripts/nudge.py` (~280 lines), `SKILL.md`, launchd plist example |
| **Dependencies** | `wanderlust-goat-pp-cli` (from [printing-press-library](https://github.com/mvanhorn/printing-press-library) — needs `GOOGLE_PLACES_API_KEY`), OpenClaw with gateway access to `openai-codex/gpt-5.5` (or any other compose model — see below), an outbox CLI on the same host (this skill expects Spratt's `~/.config/spratt/infrastructure/outbox/outbox.py`), Home Assistant `person.*` + Places integration for the at-home location lookup (optional — falls back to a hardcoded `HOME_LAT`/`HOME_LON`), `trips.sqlite` with `trips` + `travelers` tables for trip routing (optional — at-home flow works without it) |
| **Schedule** | launchd `com.spratt.discovery-butler` daily 2:00pm. Mostly silent — only fires on Thursday/Friday at home or on day 1–3 of an active trip. |
| **macOS-specific** | launchd plist is macOS-specific; underlying script is portable to any cron. |
| **Setup time** | ~10 minutes once `wanderlust-goat-pp-cli` is installed and `GOOGLE_PLACES_API_KEY` is in env. |

## Ad-hoc Q&A

`"Hey Spratt, what should we try in Boston?"` — the skill's compose path is reusable. Just call:
```bash
wanderlust-goat-pp-cli goat "Boston, MA" --criteria "coffee" --minutes 12 --top 3 --agent
```
and feed the top result through the compose step. The fires DB is NOT updated for ad-hoc — those don't count against the weekly/per-trip quota.

For testing the script's compose path without delivering:
```bash
python3 scripts/nudge.py --smoke
```
Prints the composed line and exits. No outbox write, no fires-DB write.

For forced delivery (bypasses weekday + dedup gates):
```bash
python3 scripts/nudge.py --force --person manan
```

## Configuration

The script hardcodes two phone numbers and Home Assistant entity IDs at the top — adjust for your principals:

```python
MANAN_PHONE    = "+13157082088"
HARSHITA_PHONE = "+13129330988"

HA_PERSON_ENTITY = {
    "manan":    "person.manan",
    "harshita": "person.harshita",
}
HA_GEOCODE_ENTITY = {
    "manan":    "sensor.geocode_places",
    "harshita": "sensor.harshita_s_geocode",
}
HOME_LAT, HOME_LON, HOME_LABEL = 47.674, -122.122, "Redmond, WA"
```

Compose model: edit `GATEWAY_MODEL` / `GATEWAY_FALLBACK`. If you don't use OpenClaw's gateway, the deterministic template kicks in and the script is still usable — just less voice-y.

## What's intentionally NOT in v1

- Taste profile from `instacart.db` / `orders.sqlite` — coffee shops are low-stakes.
- `table-reservation-goat` integration — these are walk-in suggestions by design.
- `card-wallet` integration — coffee shops have no meaningful card-credit nudges.

Add later only if the bare version proves too thin.

## Failure behavior

Every uncaught failure inside the at-home or trip loop fires an outbox alert to `MANAN_PHONE` with the reason — observability lives on the user's phone, not in logs (Spratt is iMessage-first). "No picks returned" also alerts.

### "No picks" — the env-var trap

The most common cause of "no picks" is not quota exhaustion, it's `GOOGLE_PLACES_API_KEY` being set in your interactive shell (e.g. `~/.zshrc`) but NOT in `shared/env/env.sh`. launchd does not source `~/.zshrc` or `~/.zshenv`, so wanderlust-goat-pp-cli exits with rc=4 and stderr `"Error: GOOGLE_PLACES_API_KEY is not set"` — which `subprocess.run(..., capture_output=True)` swallows, returning the same empty list it would for "no operational coffee shops nearby." Manual `nudge.py --smoke` works, the cron fails.

To diagnose, log `proc.returncode`, `len(proc.stdout)`, and the first line of `proc.stderr` inside `fetch_picks`. The fix is one line: add `export GOOGLE_PLACES_API_KEY="..."` to `shared/env/env.sh`. The plist already sources it via the wrapper pattern (`/bin/zsh -c "source ... && /usr/bin/python3 ..."`).

Other causes (in rough order of frequency): Google Places quota exhausted, `GOOGLE_PLACES_API_KEY` rotated/revoked, network failure, wanderlust-goat-pp-cli not on `PATH` for the launchd job.
