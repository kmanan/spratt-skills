# API Skills — Printing Press CLI Integration Plan

**Status:** Planning. Not yet implemented.
**Source catalog:** https://github.com/mvanhorn/printing-press-library (82 CLIs across 16 categories)
**Last updated:** 2026-05-11

## Goal

Enhance specific Spratt subsystems with CLIs from the Printing Press Library. Every entry below is tied to a named subsystem, with an explicit "what changes" and "what does NOT change." This is not a wishlist.

## Hard constraints

- **No order is ever placed without Manan explicitly saying the exact phrase "place the order".** This is a global rule across every order-placing path in this plan: food-butler (Dominos, future JJ), instacart-api, smart-reorder reply handlers, meal-planner, recipe-instacart "shop this," destination-aware bridges. "yes," "do it," "send it," "go ahead," "ok," and any LLM-paraphrased equivalent do NOT trigger placement. The verb is fixed — exact-string match on `"place the order"` (case-insensitive, leading/trailing whitespace allowed). Nudges present and stage; only the explicit phrase commits.
- **DO NOT TOUCH `trip-manager` or `flight-monitor`.** The travel concierge works perfectly. Award/cash redemption logic lives in **`card-wallet`**, not in trip planning. No CLI in this plan integrates into `trips.sqlite` or `flight-monitor`.
- **DO NOT TOUCH `recipe-instacart` / IG-FB-TikTok → recipe pipeline.** It uses a logged-in browser profile by design. No catalog CLI replaces it.
- **No fallback — revert where needed.** One path active, the other dormant on disk. Never run both in parallel.
- **No duplicates.** New code must not stack on top of existing code doing the same thing.
- **LLM does the thinking, scripts do the data.** Deterministic gates and DB writes in Python; voice/judgment/recommendation in LLM. This is a household butler, not a chatbot.
- **Outbox-first observability.** Every failure path must reach Manan's phone.

---

## At-a-glance: integrations → skills

| Phase | CLI(s) | Skill / subsystem (new or changed) | Verdict |
|---|---|---|---|
| 1 | `weather-goat` | `briefings` morning briefs (Manan + Harshita) — adds AQI + NWS severe alerts | AUGMENT |
| 2 | `seats-aero`, `flight-goat` | `card-wallet` (no trip-manager touch) — award sweet-spot finder + cash-vs-points sanity check | AUGMENT |
| 3 | `table-reservation-goat` | `skills/resy/` (internal name `resy-booking`) — adds OpenTable + Tock search (no SevenRooms support); cancellation watches | AUGMENT |
| 4 | `wanderlust-goat` | **NEW** `discovery-butler` skill — Thu/Fri weekend nudges + day 1-3 trip nudges | ADD |
| 5a | `dominos` | **NEW** `food-butler` skill (multi-tenant, `vendors/dominos.md`) — Friday "shitty carbs" check-in | ADD |
| 5b | (Jimmy John's) | `food-butler/vendors/jimmy-johns.md` — weekday-lunch tenant | ADD (planned) |
| 6 | `instacart` | **NEW** `instacart-api` skill — replaces browser cart-build (search + add). Order placement stays on Manan's phone (one tap). Ingestion via scraper unchanged. | REPLACE (cart-build only) |

**Untouched by this plan:** `trip-manager`, `flight-monitor`, `recipe-instacart` (the IG/FB reel → recipe pipeline itself — but Phase 6 wires it to the new `instacart-api` for the shop-this step), `orders.sqlite` schema, outbox, briefings pipeline beyond the weather replacement, the four briefing/digest cron jobs, the two `card-wallet` cron jobs, the **`Instacart Order Scraper` cron** (stays enabled — ingestion stays on the scraper until the CLI exposes past-order fetch), `instacart-orders` skill (drives the scraper, stays active), and the email-scan Instacart branch's three pre-existing flag bugs (tracked as an independent fix).

---

## Findings from code review (2026-05-11)

Reviewed all 6 CLIs (READMEs in `mvanhorn/printing-press-library`) and every Spratt component the plan touches. Material findings:

### CLI capability deltas

| CLI | Plan assumed | Actually true |
|---|---|---|
| `instacart` | Fetches order items by `order_id`; places orders; replaces scraper | **Cart-build only.** No `order get` (past orders documented as "known gap"). No `order place`. But auth via Chrome cookie jar is actually nicer (no `gog` 2FA dance). The CLI's strength is the bot-painful 80% (search + cart build); order placement is a 5-second human action. Past-order ingestion stays on the scraper. |
| `table-reservation-goat` | OpenTable + Tock + SevenRooms | OpenTable + Tock + Resy. **No SevenRooms.** Also: `book` is gated behind `TRG_ALLOW_BOOK=1` (defaults dry-run). |
| `wanderlust-goat` | Returns structured `neighborhood` + `editorial_blurb` | Returns ranked picks with source citations + score breakdowns. Exact JSON fields not documented in README — verify against `--help` before hardcoding field names. |
| `seats-aero` | Covers ANA / Aeroplan / Virgin / Avianca | Wraps the Seats.aero Partner API as-is. Partner coverage depends on subscription tier, not the CLI. Verify our tier before relying on a specific program. |
| `flight-goat` | Pure cash-fare search | FlightAware AeroAPI (live flight data) **+** Google Flights layer (cash prices) in one binary. We use the cash side only: `flights`, `dates`, `compare`. |
| `weather-goat` | AQI + NWS alerts + extended forecast | Confirmed. Free (no key). NWS alerts US-only. 10K req/day cap on Open-Meteo. |
| `dominos` | Order placement + deals + ETA | Confirmed end-to-end. Auth via Chrome harvest of bearer token, ~1h TTL. |

### Spratt component deltas

| Plan claim | Reality |
|---|---|
| `smart-reorder` and `email-to-orders` skill dirs exist (UNCHANGED) | **Both absent from disk.** Drop from component lists. |
| Email-scan inserts shell with `--items '[]'` today | **Broken today** — `run-email-actions.py upsert_order()` passes three nonexistent flags (`--order-date`, `--items-json`, `--status`). The shell-insert step has been silently failing; the nightly scraper has been the only reason items end up populated. Fix this regardless of Phase 6 outcome. |
| `trips.day_index` is a stored field | **Derived** via `CAST((julianday('now','localtime') - julianday(start_date)) + 1 AS INTEGER)`. No column. |
| Travelers resolved via `contacts.sqlite` | `travelers` table on `trips.sqlite` already has a `phone` column. Use it. Contacts only needed for Manan/Harshita iMessage targets (and Harshita has two numbers in `contacts_lookup` — verify which one she wants for discovery nudges). |
| HA location helper needed | **Exists** at `gather-briefing-data.py:40–97` (`get_weather_location(person)`). Map: `manan→person.manan`, `harshita→person.harshita`. Trip travelers (Dad, Leo, …) use `trip.destination`, not HA. |
| `card-wallet` "which card for X?" optimizer is a script | Pure in-skill LLM reasoning. No CLI, no helper. New Phase 2 helpers follow the same pattern — helpers fetch JSON, LLM reasons. |
| Travel Credit usage is finely tracked | Binary in DB (`pending` / `used` / `skipped`). No partial-usage. Document the constraint. |
| Resy skill has fallback chain | `skills/resy/` (internal name `resy-booking`). Resy-direct stays dormant; `table-reservation-goat` becomes the primary search. Revert by renaming `search_resy_direct.py` back. |
| `orders.sqlite` has a `vendor` column | Vendor identity is the `source` column. `store` is Instacart-specific (grocery store within an order). Food-butler uses `source = 'dominos'`. |
| Reuse `reorder_notifications` for food-butler cooldown | Wrong semantics (purchase-triggered, not time-triggered). New table: `food_butler_cooldown (vendor TEXT PRIMARY KEY, last_fired_at TEXT)`. |
| Phase 1 is one wttr.in call | **Two** call sites in `gather-briefing-data.py` — geocode (lines 84–95) and weather (lines 289–294). Both need replacement. |
| `item-classify.py` keeps running after scraper retires | It has no other automatic caller. Plan must say how new item names get classified once the scraper cron is off — only relevant if Phase 6 ever unblocks. |

### Plan impacts

- **Phase 6 (instacart) is REVISED, not blocked.** The CLI cannot place orders or fetch past-order items — but those weren't the painful parts. The CLI replaces the bot-painful 80% (search + cart-build) and leaves order placement on Manan's phone (one tap). Past-order ingestion stays on the scraper. The real unlock is downstream: `recipe-instacart`, `smart-reorder`, and `meal-planner` all gain programmatic cart-build via `instacart-api`. Scope revised below.
- **Phase 3 (resy) loses SevenRooms.** Still valuable for the OpenTable + Tock coverage gap.
- **Phase 5 (food-butler) compose model is Flash, not Haiku.** Per CLAUDE.md, Haiku is reserved for briefings/digests. A Friday nudge is orchestration-shaped → Flash with Haiku fallback.
- **Email-scan Instacart branch bugs are independent of Phase 6** and should be fixed regardless.

---

## Selected CLIs (6)

### 1. `instacart` — REPLACE browser cart-building; keep one-tap submit on the phone

**Reframe:** The original plan asked the CLI to do both halves of the browser workflow (build the cart **and** place the order) plus replace the ingestion scraper. The CLI doesn't do all three. But the half it does do — search + cart-build — is exactly the half that's been bot-painful (`openclaw browser` + DOM scraping + `gog` 2FA dance). Order placement (one tap in the Instacart app) doesn't need a bot, and ingestion of past-order items is fine where it is (the scraper works).

So the bot/human boundary moves to the right place:

- **Bot does (via CLI):** product search across retailers, fuzzy-matched cart construction, multi-retailer carts, cart review summary in iMessage
- **You do (on your phone, 5 seconds):** open the Instacart app, tap "Place Order"

That's a strict ergonomics win — no browser launch, no DOM flakiness, no `openclaw browser` runtime, no agent-driven 2FA. The auth model also gets cleaner: you log in to Instacart in Chrome once; the CLI reads that session via `kooky` for weeks. No `gog` watcher needed. When cookies expire, `auth status` flips and Spratt alerts via outbox.

**What the CLI actually exposes:**

| Command | Use |
|---|---|
| `search <query>` | Canonical product search (replaces DOM search) |
| `add <retailer> <query>` | Search + add to cart in one step |
| `cart show / remove` | View, modify |
| `carts` | List all carts (across retailers) |
| `auth login / status / logout` | Chrome-cookie-based session |
| `capture` | Re-seed GraphQL hashes if Instacart rotates its web bundle |

**What the CLI doesn't expose — and why each is fine:**

| Gap | Why it's not a blocker |
|---|---|
| No `orders place` / checkout | One-tap action on Manan's phone. Bot doesn't need this. Cleaner accountability: the human signs off on every order. |
| No `orders get <id>` past-order fetch | Past-order ingestion stays on the existing scraper cron. The scraper works; no reason to retire it without a clean replacement. If the catalog adds the mobile GraphQL query (or we contribute it via `instacart capture`), revisit. |
| No 2FA handling | The CLI doesn't need it — Chrome handles 2FA at login, CLI rides the resulting cookie session for weeks. Better than today's `gog` email watcher. |

**Current Instacart state (10 components, not 11 — `smart-reorder` and `email-to-orders` skill dirs don't exist on disk):**

| # | Component | Change |
|---|---|---|
| 1 | `skills/instacart-skill` (browser cart-build + `gog` 2FA) | **Deprecated** — replaced by new `instacart-api` skill |
| 2 | `skills/instacart-orders` (nightly scraper) | **Unchanged** — ingestion stays on the scraper |
| 3 | `skills/orders` (read-only Q&A) | Unchanged |
| 4 | `infra/orders/order-ingest.py` | Unchanged. (3 pre-existing flag bugs in its email-scan caller — see below.) |
| 5 | `infra/orders/item-classify.py` | Unchanged. Scraper still triggers it nightly. |
| 6 | `infra/orders/purchase-cadence.py` | Unchanged |
| 7 | `infra/orders/reorder-nudge.py` | Unchanged. **Becomes actionable** once `instacart-api` can build carts in response to "yes" replies. |
| 8 | Cron `Instacart Order Scraper` (21:00 daily) | **Stays `enabled: true`** — supports ingestion |
| 9 | Cron `Instacart Backfill (one-time)` | Stays disabled |
| 10 | Email-scan Instacart branch | Unchanged behavior (still inserts shell). 3 pre-existing flag bugs to fix in a separate small change. |

**The killer downstream unlocks (the real reason to do this):**

Today these flows hit the same browser-cart-build wall:

| Flow | Today | With `instacart-api` |
|---|---|---|
| `recipe-instacart` reel → recipe → ingredients | Manual or via `instacart-skill` (browser) | Recipe ingredients → CLI search + add → cart ready in app → one tap |
| `smart-reorder` "eggs are due to reorder" nudge | Informational only | "yes" reply → CLI builds cart → "ready in your app, tap to submit" |
| `meal-planner` weekly shop | Manual | Plan → CLI builds cart → one tap |
| Destination-aware bridge ("pick up X at QFC" reminder) | Reminder only | "or add to your Costco cart for tomorrow" → CLI builds → one tap |

These are the unlocks Phase 6 is actually about. The "replace fragile browser ordering" framing was selling the wrong story.

**Phased rollout (revised):**

| Sub-phase | Action | Reversibility |
|---|---|---|
| 6a | Install `instacart` CLI. `auth login` via Chrome cookies. Verify search + `carts` + `cart show` work read-only against an existing cart. | n/a — read-only |
| 6b | Build new `instacart-api` skill: search + cart-build wrapper. Manual test: build a 5-item cart, review, you place the order on your phone. Confirm cart contents match. | Don't enable until 6c |
| 6c | Wire `recipe-instacart`'s "shop this" step to call `instacart-api` (killer integration). Verify on 1–2 recipes. | Revert by reverting `recipe-instacart`'s shop step |
| 6d | Deprecate `instacart-skill` (browser cart-build) — rename `SKILL.md` → `SKILL.md.disabled`. `instacart-api` becomes sole cart-build path. | Rename back |
| 6e | Wire `smart-reorder` + `meal-planner` reply-handlers to call `instacart-api` on the **exact phrase "place the order"** (per global hard constraint). Optional — depends on whether smart-reorder needs an action verb in v1. Even when wired, the CLI builds the cart only; Manan still taps "Place Order" in the Instacart app — so "place the order" here means "build the cart for me to tap." | Revert reply-handler edits |

**Auth health check:**

Add `instacart_auth_healthy` to `spratt-health`:
```bash
instacart auth status --json  # 0 = ok, non-zero = relog needed
```
Surface a one-line "Instacart session expired — please log in to Instacart in Chrome" via outbox when it flips, so you know before a cart-build attempt fails.

**Decoupled fix (independent of any Phase 6 work):**

`run-email-actions.py upsert_order()` has three live flag bugs against `order-ingest.py`:

| Current (wrong) | Correct |
|---|---|
| `--order-date` | `--date` |
| `--items-json <file>` | `--items-file <file>` |
| `--status <s>` | (remove — status applies to `update-tracking`, not insert) |

Also: `order_date = datetime.now()` instead of parsing the email body. This corrupts `purchase-cadence.py` cadence math. Fix in a separate small change; don't couple to Phase 6.

**Subsystems touched:** New `instacart-api` skill. `instacart-skill/SKILL.md` renamed to `.disabled` at 6d. `recipe-instacart`'s shop-this step (at 6c). Optionally `smart-reorder` + `meal-planner` reply handlers (at 6e).
**Subsystems NOT touched:** `instacart-orders`, scraper cron, `order-ingest.py`, `item-classify.py`, `purchase-cadence.py`, `reorder-nudge.py`, `orders.sqlite` schema, email-scan Instacart branch (independent fix elsewhere).

**Revisit trigger:** if the catalog adds a `orders get <id>` query (mobile GraphQL captured), the **ingestion** half of Phase 6 also unblocks — revisit retiring the scraper then.

---

### 2. `seats-aero` + `flight-goat` — ADD points-redemption decisions to `card-wallet`

**Today:** `card-wallet` tracks 3 points cards (AMEX Platinum, CSR ×2 holders) at 2.0 cpp baseline — all three confirmed in `cards.sqlite` at `point_valuation_cpp = 2.0`. Zero ability to look up award space or compare to cash. Points sit idle. The "which card for X?" optimizer is **pure in-skill LLM reasoning** — Spratt loads the skill, runs SQL against `cards.sqlite` + `reward_rates`, and reasons. No helper script. New section follows the same pattern.

**With both CLIs in `card-wallet`:** Spratt answers three new questions, all standalone iMessage Q&A:

1. **"What's X points worth flying to Y?"** — flight-goat returns cash anchor, seats-aero returns award space across MR/UR transfer partners, the LLM computes cpp at your valuation and recommends a break-even.
2. **"I have 200k MR expiring — best use?"** — seats-aero scans MR transfer partners for next-90-day sweet spots. Top 3 by cpp. (Partner coverage is subscription-tier dependent — verify our tier exposes ANA / Aeroplan / Virgin / Avianca before relying on a specific program.)
3. **"Book through Chase Travel portal?"** — flight-goat cash price vs CSR portal redemption (1.5¢) vs transfer-partner redemption (your 2.0¢ baseline), with Travel Credit status factored in.

**Travel Credit constraint (important caveat):** `usage` rows are binary — `pending` (full $300 remaining) or `used` (assumed $0 remaining). The DB does not track partial usage. The LLM must read this constraint from the skill doc and ask Manan how much remains when a sub-$300 redemption is on the table.

**Integration:**
- New section in `~/.config/spratt/skills/card-wallet/SKILL.md`: "Points redemption decisions" — describes the two helpers and the LLM reasoning pattern; also documents the Travel Credit binary-state caveat.
- Two new helpers in `~/.config/spratt/infrastructure/card-wallet/`:
  - `flight-cash.py` — wraps `flight-goat` (cash side: `flights` / `dates` / `compare`). Output JSON shape: `{route, travel_date, economy_usd, business_usd, source, retrieved_at}`.
  - `award-search.py` — wraps `seats-aero` (Partner API). Output JSON shape: `{route, cabin, programs: [{name, points, fees_usd, seats, valid_through}], retrieved_at}`. Helper reads `point_valuation_cpp` from the DB dynamically (don't hardcode 2.0 — Manan may retune it).
- Reuses existing `cards.point_valuation_cpp` column. No schema change.
- On API failure, both helpers must return a clean error JSON (not crash) so the LLM can say "award data unavailable" gracefully.

**Don't propose:** changes to the `card-wallet` Saturday Check or Monthly Refresh crons (both stay on `google/gemini-2.5-flash` per CLAUDE.md). Note that `card-wallet-refresh.py` internally calls `claude-sonnet-4-6` for research — that's the script's documented choice and is correct; CLAUDE.md's table refers to the cron wrapper, not the script's internal API call.

**Subsystems touched:** `card-wallet` skill, `card-wallet/` infra.
**Subsystems NOT touched:** `trip-manager`, `trips.sqlite`, `flight-monitor`. No trip is created from this flow. No write to `trips.sqlite`. Purely point-decision Q&A.

---

### 3. `table-reservation-goat` — AUGMENT the reservation skill

**Today:** Skill lives at `~/.config/spratt/skills/resy/` (frontmatter name `resy-booking`). Resy-API only via direct DevTools-harvested key + auth token. Scripts on disk: `search.py`, `availability.py`, `book.py`, `modify.py`, `cancel.py`, `list_reservations.py`, `waitlist.py`, `health_check.py`. OpenTable and Tock restaurants are invisible to Spratt today. **Note:** SKILL.md currently references workspace paths under `~/.openclaw/workspace/skills/resy-booking/scripts/...` that don't match the real `~/.config/spratt/skills/resy/scripts/...` location — fix as a small docs cleanup.

**With `table-reservation-goat`:** Single search across **OpenTable + Tock + Resy** (not SevenRooms — the CLI doesn't support it). Cancellation watches surface drop-ins for booked-solid spots via `watch add` + `watch tick`. Booking is gated behind `TRG_ALLOW_BOOK=1` env var (defaults to dry-run) — set this in the launchd plist for the watcher.

**Integration (revert-not-fallback):**
- Add `search_goat.py` shelling out to `table-reservation-goat` CLI, normalizing results to the same schema `search.py` returns. **New primary search path.**
- Rename `search.py` → `search_resy_direct.py` — dormant, kept for revert only. Not invoked in the active path.
- `book.py` / `cancel.py` / `modify.py` / `waitlist.py` unchanged — they operate on a Resy reservation ID post-booking and stay platform-specific. OpenTable / Tock bookings need their own `book_opentable.py` / `book_tock.py` added later if we move past Resy-as-default-book-target. Phase 3 is search-only augmentation.
- **New cancellation-watch infrastructure** (genuinely new — no existing polling daemon today):
  - `watch_cancellation.py` script (exec payload, Flash for orchestration)
  - State sidecar: `~/.config/spratt/skills/resy/watches.json` — tracks venue, date, party_size, platform, expires_at
  - launchd plist `com.spratt.resy-watch` (StartInterval, e.g. every 5 min)
  - Writes to outbox on availability open
  - Health hook: add a check to `system-health/check.py` that verifies the watcher launchd job is alive
- Auth model: OpenTable + Tock via `auth login --chrome` (cookie import from logged-in Chrome). Resy stays on its direct token path.

**Trip-db contract (unchanged):** `trip-db.py add-reservation --trip <id> --name --date YYYY-MM-DD --time HH:MM --type dinner|brunch|lunch|... --address --party-size --confirmation --notes`. The `--confirmation` field already accepts any platform's confirmation code. No schema change needed.

**Subsystems touched:** `skills/resy/` skill (add `search_goat.py`, rename existing `search.py` for revert, fix paths in SKILL.md, add cancellation-watch glue + plist).
**Subsystems NOT touched:** trips.sqlite (reservations still flow through `trip-db.py add-reservation`); existing `book.py` / `cancel.py` / `modify.py` / `waitlist.py`.

---

### 4. `weather-goat` — AUGMENT briefings with AQI + severe alerts

**Today:** `gather-briefing-data.py` calls `wttr.in` at **two** sites: a reverse-geocode call (lines 84–95, only when traveling and HA has fresh coordinates) and the main weather call (lines 289–294, `format=3` one-liner). The result is stored as a single string in `data["weather"]` and rendered verbatim under the `WEATHER:` section of the Haiku compose prompt. No AQI. No severe alerts. No structured weather sub-fields.

**With `weather-goat`:** Two precise additions, both surfaced in the same string slot:

| Field | Where | Why |
|---|---|---|
| `aqi` (current + breathe verdict) | Both morning briefs (Manan + Harshita) | Seattle wildfire smoke; matters for outdoor plans with Sriram |
| `severe_alerts` (active NWS) | Both morning briefs | Free, US-only, not on wttr |

**Explicitly not taking from weather-goat:** Activity verdicts (`go walk/bike/hike` → GO/CAUTION/STOP). Haiku already synthesizes "rain after 3pm, postpone the walk" from hourly data; the verdict is opinionated wrapping, not new info. Also skipping `extended_forecast` — wttr's 3-day shape is fine for daily briefs. (Reconsider for trip day briefs separately if/when those are scoped.)

**Implementation contract (preserves downstream):**
- `data["weather"]` stays a **string** (no schema change). Compose step needs no changes — it concatenates the section content as-is. The new string can be multi-line (e.g., `Redmond, WA: ☀️ +72°F\nAQI 28 (good)\nNo active alerts`).
- Replace **both** wttr.in call sites — not just the weather one. The geocode call needs to either be removed (weather-goat handles lat/lon directly with built-in geocoding) or rewired through weather-goat's `geocoding` subcommand.
- Match the current failure contract: on error, return a `(error: ...)` string, never raise. Existing wrap is `subprocess` with 30s timeout; weather-goat must complete inside that.
- Compose step gets **one** prompt addition: "If AQI is poor or a severe alert is present, surface it in 1 short sentence."

**Subsystems touched:** `flows/gather-briefing-data.py` (two call sites), `flows/compose-briefing.py` (one-line prompt addition).
**Subsystems NOT touched:** `send-briefing.py` (load-bearing — no changes), the four briefing/digest cron jobs (model + schedule unchanged), `.lobster` files.

---

### 5. `wanderlust-goat` — ADD `discovery-butler` skill: casual location-aware nudges

**Today:** No equivalent. Spratt has `places.sqlite` (places you've already saved) but no discovery layer for "what's new and worth trying."

**The skill — a friend who knows the area and occasionally texts you a fun spot:**

Not a daily nag. Not a structured "weekend planner." Just a casual one-line nudge — a coffee shop or quick bite worth trying — fired at most once a week per person, and only in the right context.

**Firing rules:**

| Context | When | Who | Location |
|---|---|---|---|
| At home, no active trip | Thursday 2pm (default; Friday 2pm is the alternate if Thursday is missed) | Manan + Harshita (separate messages) | HA current location |
| Active trip in `trips.sqlite` | Day 1 of trip (default; day 2 or 3 as fallback if day 1 missed) | Whoever is listed as a traveler for that trip (per `trips.sqlite`) — could be Manan, Harshita, dad, brother, etc. — phone numbers resolved via `contacts.sqlite` | Trip destination |

**One per person per week** for at-home suggestions. **One per person per trip** for trip suggestions. No double-firing.

**Pipeline (one script, one cron):**

`day_index` is **derived, not stored** — compute it as `CAST((julianday('now','localtime') - julianday(start_date)) + 1 AS INTEGER)`. The `trips.status` flag is set at write time and refreshed by the `trip-status` launchd job, so a stale `status='active'` row could still leak — double-check `start_date <= today <= end_date` even when status is active.

Travelers come from the `travelers` table on `trips.sqlite` (columns: `id`, `trip_id`, `name`, `phone`, `role`). The `phone` is already E.164 — use it directly. Don't parse the denormalized display string in `trips.travelers`. Contacts lookup is only needed for Manan/Harshita's own iMessage targets. (Harshita has two numbers in `contacts_lookup` — `+12034792084` for "Harshita Iyer" vs `+13129330988` for "Wife". Verify which Manan wants for discovery nudges before wiring.)

HA location lookup already exists at `gather-briefing-data.py:40–97` (`get_weather_location(person)`). Map: `manan→person.manan`, `harshita→person.harshita`. Trip travelers (Dad, Leo, …) have no HA entities — they always use `trip.destination`.

```python
# discovery-butler/nudge.py — daily 2pm cron, mostly silent
for person in (manan, harshita) + active_trip_travelers():
    trip = active_trip_for(person)  # query trips + travelers tables, double-check date bounds
    if trip:
        day_index = derive_day_index(trip.start_date)  # julianday arithmetic
        if day_index in (1, 2, 3) and not already_sent_for_trip(person, trip.id):
            send_suggestion(person, location=trip.destination, framing="trip")
            mark_sent(person, kind="trip", key=trip.id)
    else:
        if today.weekday() in (THU, FRI) and not already_sent_this_week(person):
            send_suggestion(person, location=ha_location(person), framing="weekend")
            mark_sent(person, kind="weekend", key=current_iso_week())
    # otherwise silent — most cron firings produce nothing
```

**Suggestion contents:**

- One pick. Coffee shop OR quick bite. Walk-in friendly (no reservations needed).
- Source: `wanderlust-goat` for editorial freshness near the target location. **Verify the actual JSON output shape with `--help` / a manual run before hardcoding field names** — the README does not document the exact schema, only that it returns ranked picks with source citations and score breakdowns.
- Excludes (de-dup): anything in `places.sqlite` (matched case-insensitive on `name` — there is no normalized address column), anything in today/tomorrow's calendar (icalBuddy) or `trips.sqlite` reservations.
- One casual line composed by the LLM.

**Sample outputs:**

```
At home, Thursday 2pm → Manan:
☕ Weekend coffee idea: Sound & Fog (Greenwood, third-wave, 
   opened March). Walk-in. Good Sat morning stop.

Trip day 1 in NYC, Wednesday 2pm → Manan + Harshita:
☕ NYC pick: Devoción on Atlantic Ave (Brooklyn, third-wave, 
   Beard noms). Walk-in. 10 min from your hotel.

Trip day 1 visit by dad + brother to Seattle → both their numbers:
☕ Seattle pick: Sey Coffee (Capitol Hill, new). Walk-in. 
   12 min from where you're staying.
```

**State tracking:**

`~/.config/spratt/db/discovery_fires.sqlite`:
```sql
CREATE TABLE fires (
  person     TEXT NOT NULL,
  kind       TEXT NOT NULL,    -- 'weekend' or 'trip'
  key        TEXT NOT NULL,    -- ISO week (e.g., '2026-W19') or trip_id
  sent_at    TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (person, kind, key)
);
```

**Ad-hoc requests:**

"Hey Spratt, what should we try in Boston?" — handled by the skill's normal Q&A surface. The same gather function is called, no cron involved, returns a pick on demand. State is not updated (ad-hoc doesn't count against the weekly/per-trip quota).

**Implementation:**

- New skill: `~/.config/spratt/skills/discovery-butler/SKILL.md`
- New script: `~/.config/spratt/infrastructure/discovery-butler/nudge.py` (~100 lines)
- New cron: daily 2pm PT. `agentTurn` payload (composes content). `lightContext: true`. Most days the script exits silently after the rules say "don't fire" — that silence is the goal.
- **Open model decision for the cron:** Discovery nudges aren't briefings/digests (Haiku-reserved per CLAUDE.md), but they do compose a one-line butler-voice pick — not orchestration. Two options: (a) Haiku, treating it as compose work like a mini-brief; (b) Flash for the whole turn. Default to **Flash** — orchestration-shaped, single-line compose, with Haiku fallback. Flag for Manan's confirmation before wiring.
- Output via outbox (`--source discovery-butler --created-by discovery-butler --at now`).
- State DB `discovery_fires.sqlite` is created on first run via `CREATE TABLE IF NOT EXISTS`.

**What's intentionally NOT in v1:**

- Taste profile from `orders.sqlite` — coffee shops + quick bites are low-stakes; over-personalization is unnecessary
- `table-reservation-goat` integration — these are walk-in suggestions. Reservation booking lives in the resy/table-reservation-goat skill, a separate flow when the user *wants* to book somewhere.
- `card-wallet` integration — coffee shops don't have meaningful card-credit nudges. Skip.

Each of these can be added later if the bare version proves too thin. Ship the bare version first.

**Subsystems touched:** New skill + infra. `places.sqlite` (read-only), `trips.sqlite` (read-only), `contacts.sqlite` (read-only).
**Subsystems NOT touched:** trip-manager (read-only `trips.sqlite` access, no writes, no compose-prompt edits), flight-monitor, destination-aware, recipe-instacart, briefings.

---

### 6. `dominos` (and future: `jimmy-johns`, …) — ADD `food-butler` skill: vendor-driven comfort-food nudges

**Today:** No equivalent. Quick-food decisions happen reactively when Manan is already hungry. By the time the craving lands, decision fatigue picks the easiest thing, not the best thing.

**The skill — `food-butler`, generalized:**

A multi-tenant comfort-food orchestrator. One skill, one outbox path, one reply-and-act pattern. Per-vendor modules slot in as we add CLIs from the Printing Press catalog.

**Architecture:**

```
~/.config/spratt/skills/food-butler/
├── SKILL.md                    # how the butler works; how to add a vendor
└── vendors/
    ├── dominos.md              # Friday-evening pizza nudge (first tenant)
    └── jimmy-johns.md          # weekday-lunch sandwich nudge (planned, not yet built)
```

Each vendor module specifies:
- **Hook** — when to consider firing (cron expression, day-of-week, time-of-day)
- **Gates** — conditions that suppress firing (trip in progress, prior reservation, anti-nag cooldown)
- **Order template** — the "usual" the user would re-up (pulled from `orders.sqlite` history or a static seed)
- **Voice** — the one-liner the compose step should produce
- **Reply verb (universal):** the exact phrase `"place the order"` (case-insensitive, surrounding whitespace stripped) is the **only** trigger that commits an order. No paraphrases. No vendor-specific verbs. See hard constraint at the top of this doc.

**Per-vendor crons:**

| Vendor | Cron | Anti-nag | Status |
|---|---|---|---|
| `dominos` | Friday 4:00pm | last Dominos order > 10 days | Phase 5 build |
| `jimmy-johns` | Weekday lunch (cadence TBD when adding) | last JJ order > 7 days | Planned, not in this phase — install when JJ CLI is wired |

Adding a future vendor (e.g., another sandwich/burger chain): drop a new `vendors/<name>.md`, add one cron, point both at the shared compose + reply-handling helpers. No skill rebuild.

**Universal gates (apply to every vendor):**
- Active/upcoming trip covering tonight in `trips.sqlite` → skip. Canonical query: `SELECT name, start_date, end_date FROM trips WHERE status IN ('upcoming','active') AND start_date <= date('now') AND end_date >= date('now')`.
- Dinner reservation tonight → check **both** `trips.sqlite` reservations (`type IN ('dinner','brunch','lunch')` with today's date) and icalBuddy (`/opt/homebrew/bin/icalBuddy -ea -nrd -eed -eep "notes,url,uid" -ic "manankakkar@gmail.com,..." eventsToday` — grep stdout for `dinner|reservation|restaurant`). Calendar parsing is heuristic; trips.sqlite is the reliable source. Use both, OR them.
- Vendor-specific anti-nag (per table above) → skip
- Sriram bedtime / school-night logic if it ever matters — flagged for future, not now

There is **no unified "is tonight blocked?" helper** in the codebase today — the gate script inlines all three checks. Don't extract to a shared helper for one consumer.

**Universal pipeline (per vendor):**
1. Hook fires, gates evaluated, vendor module supplies order template + voice.
2. Vendor CLI (`dominos`, eventually `jimmy-johns`, …) computes tonight's deal/availability + price.
3. Card recommendation lookup. The card-wallet "which card for X?" optimizer is **pure in-skill LLM reasoning** — no CLI to call. For a deterministic script context, query `reward_rates` directly: `SELECT c.card_name, r.rate FROM reward_rates r JOIN cards c ON r.card_id=c.id WHERE r.category='dining' AND c.active=1 AND c.holder='manan' ORDER BY (r.rate * COALESCE(c.point_valuation_cpp,1.0)/100.0) DESC LIMIT 1`. Almost always CSR 3x dining; the DB query is one line.
4. Compose step. The nudge cron is `exec` payload running `food-butler-nudge.py` (gates are deterministic Python — must not be `agentTurn`). The script shells out to `openclaw infer model run --model google/gemini-2.5-flash` for the one-line butler voice (same pattern as email-scan's LLM stages). Captures stdout, writes outbox. **Flash, not Haiku** — Haiku is reserved for briefings/digests per CLAUDE.md.
5. Reply lands in interactive context. Per CLAUDE.md, interactive replies use the `message` tool (iblai-router-routed iMessage session), NOT the outbox. The reply handler is the live Spratt session reading conversation context — so the nudge body **must inline everything needed to act** (vendor, store ID, items, price, card). Alternatively use TaskFlow `setWaiting` / `resume` for durable multi-turn state across a session gap (cleaner if reply arrives 30+ minutes later in a fresh session).

**Sample output (Dominos, Friday 4pm):**
```
Pizza tonight, sir? Domino's BOGO is live.
Your usual: 2 large pepperoni + cheesy bread, $28.40 with the deal.
Use CSR (3x dining). ETA 35 min from order.
Reply "place the order" to commit. Any other reply, or no reply, and I stand down.
```

**Sample output (Jimmy John's, weekday lunch — planned shape):**
```
Lunch, sir? JJ #9 Italian Night Club, $9.50 with chips.
Delivery 12 min to home. CSR 3x dining.
Reply "place the order" to commit. Any other reply, or no reply, and I stand down.
```

**Reply handling (shared):**

Inline everything in the outbox message — order, price, card. Reply context arrives carrying enough state that Spratt doesn't need perfect memory recall to act. The reply handler is in the skill itself, not per-vendor, so adding a vendor doesn't touch reply parsing.

The matcher is a strict substring check: `"place the order" in reply.lower()`. That accepts "place the order", "place the order!", "yes, place the order", "go ahead, place the order please" — but rejects "yes", "do it", "send it", "place it", "place order", "ok send", or any LLM-paraphrased equivalent. The phrase must appear verbatim. Intentional friction, by Manan's directive.

**Implementation phasing inside Phase 5:**
- **5a** — Build `food-butler` skill scaffold + Dominos tenant + Dominos cron. Ship.
- **5b** — Once stable (4+ Friday cycles), add `jimmy-johns` tenant + JJ cron in a single PR. Tests the multi-tenant assumption.
- Future vendors follow the 5b pattern.

**Anti-nag state — new table, not reused:** `reorder_notifications` (used by `reorder-nudge.py`) has purchase-triggered semantics ("notify again when a new purchase happens since last notify") — wrong for time-based vendor cooldown. Add a new table to `orders.sqlite` via `CREATE TABLE IF NOT EXISTS food_butler_cooldown (vendor TEXT PRIMARY KEY, last_fired_at TEXT)`. Gate check: `julianday('now') - julianday(last_fired_at) >= <vendor.cooldown_days>` (with NULL coalesced to "go ahead — never fired").

**Orders schema note:** there is no `vendor` column in `orders` — use `source` (existing TEXT column, accepts any string). `order-ingest.py` validates nothing on this field, so `--source dominos` works today with no code change. The `store` column is Instacart-specific (grocery store within an order) — leave NULL for Dominos / JJ.

**Day-one consideration:** the "last order > 10 days" gate will hit zero rows on first run. Treat NULL `MAX(order_date)` as "go ahead." Code `COALESCE(MAX(order_date), '2000-01-01')`.

**Subsystems touched:** New `food-butler` skill + 1 new cron initially (Dominos), more crons as vendors are added. `orders.sqlite` (read for anti-nag + write for new orders via existing `order-ingest.py` — same contract; new `food_butler_cooldown` table added on first run).
**Subsystems NOT touched:** trip-manager, flight-monitor, destination-aware, recipe-instacart, instacart pipeline, briefings.

---

## Phasing

| Phase | What | Reversibility |
|---|---|---|
| 1 | `weather-goat` into briefings (replace both wttr.in call sites) | Trivial — swap calls back to wttr |
| 2 | `seats-aero` + `flight-goat` into card-wallet (new redemption section + 2 helpers) | Fully additive — new skill section, new helpers, no existing path changed |
| 3 | `table-reservation-goat` for `resy` skill: new `search_goat.py` becomes primary; existing `search.py` renamed to `search_resy_direct.py` (dormant, revert-ready); new cancellation-watch launchd job + sidecar JSON | Rename files back, disable plist |
| 4 | `wanderlust-goat` `discovery-butler` skill: daily 2pm cron, mostly silent; Thu/Fri at home + day 1–3 of trip | Standalone new cron — disable to revert |
| 5a | `food-butler` skill scaffold + Dominos tenant + Friday 4pm cron | Disable cron + rename `vendors/dominos.md` |
| 5b | Jimmy John's tenant added under same skill once 5a stable (4+ Friday cycles) | Disable JJ cron + rename `vendors/jimmy-johns.md` |
| 6 | **REVISED.** New `instacart-api` skill replaces browser cart-build (search + add). One-tap order placement stays on Manan's phone. Scraper-driven ingestion unchanged. Downstream: `recipe-instacart`, `smart-reorder`, `meal-planner` get programmatic cart-build. | Rename `instacart-skill/SKILL.md` back, revert recipe-instacart shop step |
| Independent fix | `run-email-actions.py upsert_order()` flag bugs (`--order-date`, `--items-json`, `--status`) + `order_date` extraction. Unblocks reliable scraper-shell handoff. | Single-file change, easy to revert |

Run order: Phase 1 first (lowest blast radius, single-day delivery). Phase 2 in parallel (additive, doesn't touch any existing code path). Phase 3 next (introduces revert-ready file renames + first new launchd plist for cancellation watch). Phase 4 once Phase 3's plist pattern is validated. Phase 5a after Phase 4 — Friday nudge reply-and-act pattern benefits from the discovery-butler outbox cadence experience. Phase 5b after 4+ stable Dominos Fridays. The independent email-scan fix can go anytime — it's a defect, not a feature.

---

## Catalog items explicitly NOT picked (and why)

Comprehensive audit lives in conversation history. Short version of the rejects with reasons:

- **`recipe-goat`, `allrecipes`, `food52`** — `recipe-instacart` is reels/video → recipe, not search-best-recipe. Different problem.
- **`firecrawl`** — won't carry IG/FB logged-in auth, so doesn't replace the browser path. Public-web scraping is already handled by `web_fetch`.
- **`flight-goat` as trip-planning tool** — would touch trip-manager. Off-limits. Only used inside card-wallet for cash-anchor in points decisions.
- **`seats-aero` as trip-planning tool** — same. Only used inside card-wallet.
- **`hackernews`, `fedex`, `techmeme`, `producthunt`, `wikipedia`, `archive-is`** — owner doesn't want them.
- **`jimmy-johns`** — DEFERRED, not skipped. Planned as second tenant of `food-butler` skill in phase 5b. Weekday-lunch sandwich nudge to complement Dominos' Friday-evening pizza nudge.
- **`pagliacci`** — different problem shape than the comfort-food butler (premium local Seattle pizza, date-night ordering, not "shitty carbs tonight"). Revisit only if a date-night-food use case emerges separately.
- **`ordertogo`** — generic restaurant ordering. No clear hook today; food-butler is for known vendors with reliable order templates, not browsing.
- **All marketing CLIs** (klaviyo, customer-io, ahrefs, etc.), **seller tools** (amazon-seller, shopify, tiktok-shop), **infra ops** (render, digitalocean, etc.) — wrong domain.
- **Conditional adds** (slack, fathom, fireflies, linear, jira, notion, whoop, etc.) — pending decision on whether owner uses each. Not in scope of this plan.

---

## Resolved decisions (2026-05-11)

- **Discovery-butler cron model:** `openai/gpt-5.5` via `openclaw infer model run --gateway --model openai/gpt-5.5`. Same pattern as email-scan — uses Manan's Codex subscription, no per-token Anthropic/Gemini cost. Fallback to Flash. (Phase 4.) Phase 5 food-butler compose step uses the same gateway invocation, same model.
- **Harshita's iMessage handle for discovery nudges:** `+13129330988` (Wife).
- **Food-butler reply state:** Inline-everything in the nudge body for v1. The reply matcher is the strict substring check (`"place the order" in reply.lower()`) — see the global hard constraint at the top. Revisit TaskFlow `setWaiting`/`resume` only if reply correctness suffers when replies arrive 30+ min later in a fresh session.

## Phase 0 — install + doctor pass (executed 2026-05-11)

**Toolchain prerequisite resolved:** Go 1.26.3 installed to `~/go-sdk/go` (user-writable, no sudo/Homebrew — `spratt` user is non-admin). PATH update added to `~/.config/spratt/infrastructure/env.sh` so all future shell sessions and launchd jobs sourcing env.sh see `go`, `weather-goat-pp-cli`, `flight-goat-pp-cli`, `wanderlust-goat-pp-cli`. Binaries land in `~/go/bin/`.

**Catalog access:** `mvanhorn/printing-press-library` is actually **public** (despite the README's "private" wording). `gh` auth as `kmanan` works for fetches without env-var token setup; the installer accepts `GH_TOKEN=$(gh auth token)` inline if a future install needs it.

**Install pattern (record for next phases):**
```bash
source ~/.config/spratt/infrastructure/env.sh
GH_TOKEN=$(gh auth token) npx -y @mvanhorn/printing-press install <cli>
```

**Installed in Phase 0 (don't-need-Manan-present subset):**

| CLI | Binary name | Doctor verdict | Phase impact |
|---|---|---|---|
| `weather-goat` | `weather-goat-pp-cli` | ✅ Config + auth + API all OK on `forecast` and `alerts`. ❌ `air-quality` and `geocoding` 404 due to a base-URL bug in the CLI — both endpoints live on subdomain hosts (`air-quality-api.open-meteo.com`, `geocoding-api.open-meteo.com`) but the CLI hits `api.open-meteo.com/v1/<endpoint>`. Verified with `--dry-run` + raw curl. | **Phase 1 partial unlock.** `forecast` works (the primary wttr replacement). NWS `alerts` works. AQI needs a workaround until upstream fixes the air-quality subcommand: either call `air-quality-api.open-meteo.com` directly from a small Python helper in `gather-briefing-data.py` (keyless, same data), or file `weather-goat-pp-cli feedback` upstream and wait. We'll wrap the direct call — small, deterministic, keeps Phase 1 shippable. |
| `flight-goat` | `flight-goat-pp-cli` | ⚠️ Auth wanted env `FLIGHT_GOAT_API_KEY_AUTH`, not `FLIGHTAWARE_API_KEY`. Aliased in env.sh — both names point to the same FlightAware key. Cash side (Google Flights via native Go) is **keyless** and works end-to-end: smoke-tested `flights SEA LAX 2026-06-15 --stops non_stop` → 23 nonstop results, full leg + price data. | **Phase 2 cash anchor ready.** `flight-cash.py` helper can wrap `flight-goat-pp-cli flights <origin> <destination> <date> --agent` directly. Phase 2b (award search) still blocked on `SEATS_AERO_API_KEY`. |
| `wanderlust-goat` | `wanderlust-goat-pp-cli` | ✅ All green. Detects `GOOGLE_PLACES_API_KEY` ✅ + `ANTHROPIC_API_KEY` ✅. **Does NOT use `GOOGLE_API_KEY`** — Maps Geocoding is via Nominatim/OSM, not Google. The `GOOGLE_API_KEY` in shell env is not consumed by this CLI. Smoke test: `goat "47.674,-122.121" --criteria "third-wave coffee" --minutes 10 --top 3` returned 3 ranked Redmond picks with `walking_minutes`, `score` subscores, `why` one-liner, and `google_maps_uri` deeplink — exactly the discovery-butler payload shape. | **Phase 4 unblocked.** Use `goat` (deterministic) for the cron path; reserve `near --llm` for ad-hoc richer Q&A. Field names for the discovery-butler script: `name`, `address`, `lat`, `lng`, `walking_minutes`, `score.total`, `why`, `google_maps_uri`, `business_status` (filter to `OPERATIONAL`). |

**Notable CLI surface observations (record for skill authoring):**

- All three CLIs accept `--agent` as a global flag that turns on `--json --compact --no-input --no-color --yes` together. Use this everywhere — single contract.
- All three have `doctor`, `feedback`, `sync`, `analytics`, `profile`, `workflow` subcommands. The `workflow` surface is a candidate for composing multi-step calls later.
- `weather-goat forecast` flag shape: `--latitude` and `--longitude` are **floats, required**. Place-name resolution must happen upstream (HA already supplies lat/lon, so this is fine for Phase 1).
- `flight-goat flights` flag shape: `<origin> <destination> <date>` are **positionals**, not flags. Date is `YYYY-MM-DD`.
- `wanderlust-goat goat` accepts the anchor as either a free-text address or `"<lat>,<lng>"` positional. No `--latitude`/`--longitude` flags.

**Upstream feedback ticket to file (when adopting Phase 1):**

`weather-goat-pp-cli`: `air-quality` and `geocoding` subcommands construct URLs against `api.open-meteo.com/v1/<endpoint>` but Open-Meteo serves those on dedicated subdomains. Suggested fix: per-endpoint base URL in config (or hardcoded subdomains in the route table). Submit via `weather-goat-pp-cli feedback`.

## Phase 0 — env-var requirements (pre-build doctor pass)

Audited what Manan already has vs. what each CLI needs. Run `<cli> doctor` (or equivalent) once the CLI is installed; do not stub keys.

| CLI | Required env / auth | Already have | Action |
|---|---|---|---|
| `weather-goat` | None — Open-Meteo + NWS are keyless. Optional `WEATHER_GOAT_USER_AGENT` for polite NWS use. | n/a | Set a polite UA string in env.sh; no key needed. |
| `flight-goat` | `FLIGHTAWARE_API_KEY` for the live-flight side; **Google Flights cash side** uses a public scrape — verify with `--help` whether a key is required (some forks require `GOOGLE_API_KEY` or none). | `FLIGHTAWARE_API_KEY` ✅ (in `~/.config/spratt/infrastructure/env.sh`), `GOOGLE_API_KEY` ✅ (shell), `GOOGLE_PLACES_API_KEY` ✅ (shell) | Card-wallet only uses the cash side; FA key is incidental. Confirm at Phase 2. |
| `seats-aero` | `SEATS_AERO_API_KEY` (Partner API subscription). | **NOT set** — needs Manan to provision. | **BLOCKER for Phase 2 award-search half.** Cash-anchor half (flight-goat) can ship without it. |
| `table-reservation-goat` | `TRG_ALLOW_BOOK=1` (env gate; defaults dry-run). OpenTable + Tock via Chrome-cookie auth (`auth login --chrome` — no key). Resy stays on the existing direct token in `skills/resy/`. | n/a — auth is cookie-based | Set `TRG_ALLOW_BOOK=1` in the cancellation-watch plist's `EnvironmentVariables`, NOT in shell env (keep dry-run as the global default). |
| `wanderlust-goat` | Likely **Google Maps Geocoding / Places** for location resolution + an editorial source. Confirm with `--help` / `doctor` before wiring. | `GOOGLE_API_KEY` ✅ (Maps), `GOOGLE_PLACES_API_KEY` ✅ | Map `GOOGLE_API_KEY` → Maps Geocoding/Places usage in wanderlust-goat. If it wants a distinct `WANDERLUST_GOAT_GOOGLE_KEY`, alias to the existing key in env.sh. |
| `dominos` | No API key. Bearer token harvested from Chrome (`auth refresh`), ~1h TTL. | n/a | Document the refresh cadence in food-butler SKILL.md. Add `dominos auth status` to `spratt-health`. |
| `instacart` | No API key. Chrome cookie jar via `kooky` (`auth login`). | n/a | Add `instacart auth status` to `spratt-health` (see Phase 6 auth-health). |

**LLM model env (compose steps, not CLI-specific):**

| Use | Routing | Key |
|---|---|---|
| Discovery-butler one-line compose (Phase 4) | `openclaw infer model run --gateway --model openai/gpt-5.5` | OpenClaw gateway (no per-job key) |
| Food-butler one-line compose (Phase 5) | same | same |
| Card-wallet redemption reasoning (Phase 2) | In-skill, runs in Manan's live session | `ANTHROPIC_API_KEY` ✅ (already used) |

**The one Phase-0 blocker:** `SEATS_AERO_API_KEY`. Manan needs to provision a Seats.aero Partner API key before Phase 2's award-search helper can be built. The cash-anchor helper (Phase 2a, `flight-cash.py`) can ship independently — it only needs the existing keys.

## Open items

- **`wanderlust-goat` output schema** — README documents ranked picks with source citations + score breakdowns, but exact JSON field names aren't shown. Verify with `--help` / a manual run before hardcoding field names in `discovery-butler/nudge.py`.
- **`seats-aero` partner coverage** — depends on Manan's Seats.aero subscription tier, not the CLI. Once the key is provisioned, verify ANA / Aeroplan / Virgin / Avianca are exposed before promising those redemptions in card-wallet's SKILL.md.
- **`flight-goat` Google Flights side key requirement** — README is ambiguous on whether a key is required for the cash side. Run `flight-goat doctor` at Phase 0 and document.
- **`wanderlust-goat` Maps key shape** — confirm whether it reads `GOOGLE_API_KEY` directly or a CLI-specific name (`WANDERLUST_GOAT_GOOGLE_KEY`, etc.) at Phase 0.
- **Email-scan Instacart flag bugs** — independent of Phase 6, but worth a same-day fix. The shell-insert step has been silently failing; only the scraper has been keeping items populated. While at it, also fix `order_date = datetime.now()` to extract the real order date from the email body.
- **Phase 6 ingestion revisit trigger** — if `instacart` CLI gains an `orders get --order-id` command (or we contribute one via `instacart capture`), the ingestion half also unblocks: email-scan can fetch full items inline at receipt time, and the scraper cron can retire. Order placement stays one-tap on Manan's phone regardless — that's where it belongs per the global hard constraint.
