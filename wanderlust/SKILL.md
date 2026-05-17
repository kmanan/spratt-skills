---
name: wanderlust
description: Place discovery within walking distance (Google Places + OSM + Wikipedia + Reddit + Atlas Obscura). Powers (1) the scheduled trip-city daily pings — weather + 3 picks to the trip's iMessage chat, and (2) ad-hoc COFFEE SHOP recommendations in conversation. For ANY message asking about coffee shops anchored to a place ("coffee shop we can walk to from the aquarium", "good coffee near Pike Place", "where can we grab coffee in Capitol Hill") — call goat BEFORE reaching for web_search / tavily / firecrawl. Other place categories (restaurants, sights, events) are NOT in conversational scope yet — use web search for those. Spratt suggests, never books.
homepage: https://github.com/mvanhorn/printing-press-library
metadata: {"clawdbot":{"emoji":"🧭","requires":{"bins":["wanderlust-goat-pp-cli"]}}}
---

# Wanderlust (trip-city discovery)

CLI-driven place discovery for travel. Same engine for ad-hoc "what's near me" queries and the scheduled trip-city digest that pings active-trip iMessage chats with weather + 3 picks each morning.

## When to use in conversation

ONE conversational trigger today: **coffee shop recommendations anchored to a place.**

Examples that MUST route here, not `web_search` / `tavily` / `firecrawl`:
- "coffee shop we can walk to from the Seattle Aquarium"
- "good coffee near Pike Place"
- "where should I grab espresso in Capitol Hill"
- "best third-wave coffee near our hotel"

Command:
`wanderlust-goat-pp-cli goat "<anchor>" --criteria "third-wave coffee" --minutes 15 --top 5 --agent`

Other place categories (restaurants, museums, photo spots, walking routes, etc.) are NOT yet in conversational scope — keep using `web_search` / `tavily` / `firecrawl` for those until Manan expands this list.

## Boundary

- **Suggest, don't book.** Wanderlust is read-only. Reservations live in the `table-reservation` skill (which has its own booking gate).
- **Trip-city messages are non-personalized.** They land in the trip's existing iMessage chat (`trips.group_chat_guid` — phone number or chat GUID), not in Manan/Harshita's per-person briefings. That keeps the briefings focused on home + today, and lets travelers see the same content together.

## Prerequisites

- Binary: `~/go/bin/wanderlust-goat-pp-cli`
- `wanderlust-goat-pp-cli doctor` → `Auth: not required`, `GOOGLE_PLACES_API_KEY: set`, `API: reachable`.
- `ANTHROPIC_API_KEY` enables `--llm` criteria matching on `near`. The `goat` subcommand is deterministic — no LLM, uses static keyword tables. Use `goat` for cron/automation; use `near` only when judgment matters.

## Command surface

All commands accept `--agent` for JSON + non-interactive mode.

| Command | Purpose |
|---|---|
| `doctor` | Config + auth + API reachability. |
| `goat <anchor> --criteria <free-text> --minutes N --top K` | Deterministic top-K picks within K-minute walk of anchor. **Use this in cron.** |
| `near <anchor> --criteria ... --llm` | Same fanout as goat with LLM criteria-match. Interactive only. |
| `crossover --anchor ...` | Food-near-culture pairings (high-trust restaurant within 200m of Wikipedia-notable site or Atlas Obscura entry). |
| `golden-hour <anchor>` | Local sunrise/sunset/blue-hour + photographer viewpoints. |
| `route-view <from> <to>` | Walking polyline + interesting things along the path. |
| `quiet-hour <anchor> --at HH:MM` | Places locals describe as quiet at the requested time. |
| `reddit-quotes <place>` | Verbatim Reddit comments mentioning a place (no LLM summarization). |
| `sync-city <city>` | Pre-cache editorial best-of, Reddit, Wikipedia, OSM POIs, Atlas Obscura for offline use. |
| `coverage <city>` | Per-tier source coverage report for a synced city. |
| `why <place-id>` | Score breakdown — which sources mentioned it, country boost, walking time, criteria match. |

## Trip-city digest (the scheduled flow)

A daily launchd job runs `~/.config/spratt/skills/wanderlust/scripts/trip-city-digest.py` for every `status IN ('active','upcoming')` trip in `trips.sqlite`. For each trip:

1. **Geocode** the `destination` text via `weather-goat-pp-cli config set-location --dry-run` (same path the briefings use — keeps geocoding logic in one place).
2. **Weather** via `weather-goat-pp-cli forecast --lat/--lon --agent` (current conditions + today's hi/lo).
3. **Top 3 picks** via `wanderlust-goat-pp-cli goat <lat>,<lng> --criteria "<seed>" --minutes 15 --top 3 --agent`. Criteria seed rotates day-of-week to keep it fresh (Mon=coffee, Tue=market, Wed=museum, Thu=street food, Fri=bookstore, Sat=viewpoint, Sun=quiet park).
4. **Compose** a 4-line plain-text message — no markdown, no emojis other than 🌤 and 📍.
5. **Skip list**: `~/.config/spratt/skills/wanderlust/state/skip-trips.json` is a JSON array of trip ids. Any trip whose id is in that list is silently skipped — used to opt a specific trip out without changing code. Currently contains `2026-05-india-dad` (Mom's solo May trip).
6. **Schedule** via `outbox.py schedule --to <recipient>`:
   - If `trips.group_chat_guid` is set → use that (works for chat GUIDs OR phone numbers; the outbox accepts both).
   - Else → skip. We don't fan out to individual travelers without an explicit chat target on the trip row.
7. **Source tag**: `wanderlust:<trip-id>:<YYYY-MM-DD>` so the outbox dedupes if the job runs twice in one day.

The job is **deterministic** — no LLM in the runtime path. `goat` uses static keyword tables, weather is a forecast API, the message template is fixed. Runs as `exec`-style launchd job.

## Workflow — interactive "what should we do near X"

1. `wanderlust-goat-pp-cli goat "<anchor>" --criteria "<what>" --minutes 15 --top 5 --agent`
2. Present top 3 by name + walking minutes + score's `why` field.
3. If Manan picks one and it's a restaurant, hand off to `table-reservation` skill via `goat <name> --location <city> --party N` to check availability.

## Safety

- **Never** book from this skill. Route booking through `table-reservation`.
- **Never** sync-city a destination automatically — that's a deliberate pre-trip step Manan triggers. Cron only runs `goat`, which works without sync (live Google Places + Nominatim).
- **Never** post to a trip chat if `group_chat_guid` is empty — skip and surface to Manan via outbox (`source: wanderlust:warning`).

## Open items

- **Sync-city automation** — run `sync-city <destination>` 48h before each trip's `start_date`. Deferred until the digest has run for a couple of trips.
- **Criteria seed tuning** — the day-of-week rotation is a guess. Adjust after a week of real data.
