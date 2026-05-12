# API Skills — Printing Press CLI Integration Plan

**Status:** Planning. Not yet implemented.
**Source catalog:** https://github.com/mvanhorn/printing-press-library (82 CLIs across 16 categories)
**Last updated:** 2026-05-11

## Goal

Enhance specific Spratt subsystems with CLIs from the Printing Press Library. Every entry below is tied to a named subsystem, with an explicit "what changes" and "what does NOT change." This is not a wishlist.

## Hard constraints

- **DO NOT TOUCH `trip-manager` or `flight-monitor`.** The travel concierge works perfectly. Award/cash redemption logic lives in **`card-wallet`**, not in trip planning. No CLI in this plan integrates into `trips.sqlite` or `flight-monitor`.
- **DO NOT TOUCH `recipe-instacart` / IG-FB-TikTok → recipe pipeline.** It uses a logged-in browser profile by design. No catalog CLI replaces it.
- Every CLI must integrate with an existing Spratt subsystem and follow the outbox-first observability rule.

---

## Selected CLIs (6)

### 1. `instacart` — REPLACE fragile browser ordering (revert-ready, no fallback)

**Current Instacart constellation (eleven components):**

| # | Component | Type | Role today |
|---|---|---|---|
| 1 | `skills/instacart-skill` | Skill (browser) | Interactive ordering — `openclaw browser` + `gog` 2FA |
| 2 | `skills/instacart-orders` | Skill (browser) | Nightly scraper — fills missing items into `orders.sqlite` |
| 3 | `skills/orders` | Skill | Read-only Q&A over `orders.sqlite` |
| 4 | `skills/smart-reorder` | Skill | Cadence-driven reorder suggestions |
| 5 | `skills/email-to-orders` | Skill | Documentation for email-scan ingestion contract |
| 6 | `infra/orders/order-ingest.py` | Script | Deterministic SQLite writer for `orders.sqlite` |
| 7 | `infra/orders/item-classify.py` | Script | Canonical household name aliasing |
| 8 | `infra/orders/purchase-cadence.py` | Script | Per-item reorder cadence analysis |
| 9 | `infra/orders/reorder-nudge.py` | Script | iMessage nudges for items due to reorder |
| 10 | Cron `Instacart Order Scraper` (21:00 daily, ENABLED) | Cron | Drives `instacart-orders` scraper skill |
| 11 | Cron `Instacart Backfill (one-time)` (DISABLED) | Cron | One-time historical backfill (already off) |
| (also) | Email-scan Instacart branch | Cron prompt | Inserts order shell with `--items '[]'` on receipt arrival |

**Today's flow (two-stage ingestion):**

```
Stage 1 (real-time): Instacart receipt email → email-scan → order-ingest.py 
                     --items '[]' --delivery-status STATUS  (item list EMPTY)

Stage 2 (21:00 nightly): scraper cron → instacart-orders skill (browser) 
                         → fetches receipt page → order-ingest.py update-items
                         → item-classify.py canonicalizes new names
```

**What changes vs what stays:**

| Component | Change |
|---|---|
| `instacart-skill` (ordering, browser) | **Deprecated** — replaced by new `instacart-api` skill wrapping the new CLI |
| `instacart-orders` (scraper, browser) | **Deprecated entirely** — new CLI gives items at email-receipt time, no scraping needed |
| Cron `Instacart Order Scraper` | **Disabled (not deleted)** — set `enabled: false` in jobs.json |
| Email-scan Instacart branch | **Upgraded** — calls new `instacart` CLI inline to fetch items at email-receipt time, inserts FULL itemized order via existing `order-ingest.py` |
| `order-ingest.py` | **No change** — still the deterministic SQLite writer; new path calls it the same way |
| `item-classify.py` | **No change** — still canonicalizes; new items flow through same way |
| `purchase-cadence.py`, `reorder-nudge.py` | **No change** — downstream of `orders.sqlite` |
| `skills/orders` (read-only Q&A) | **No change** |
| `skills/smart-reorder` | **No change** |
| `skills/email-to-orders` | **No change** — same `order-ingest.py` contract |

**The new flow (single-stage ingestion):**

```
Real-time: Instacart receipt email → email-scan extracts order_id 
           → calls `instacart` CLI to fetch full items for that order_id
           → order-ingest.py with full items + total + store
           → item-classify.py runs as today

Interactive ordering: user → instacart-api skill → instacart CLI → order placed
                      (no browser, no 2FA dance, no openclaw browser dependency)
```

**Revert-not-fallback principle:**

- "Revert" means: if the new CLI breaks or behaves wrong, the old path is dormant on disk and can be re-enabled in minutes by flipping the cron back on and restoring skill names. **At no point do both paths run simultaneously** (no try-new-fall-back-to-old logic). One path is active; the other is dormant.
- All deprecated components stay **on disk in place**. Nothing is deleted.
- Deprecation mechanism: rename `SKILL.md` → `SKILL.md.disabled` in the deprecated skill directory. OpenClaw's loader looks for the exact filename `SKILL.md`, so this stops loading the skill into the agent's context without removing the file. Reverting = rename back.
- Crons: set `enabled: false` in `~/.openclaw/cron/jobs.json`. Reverting = flip to `true`.

**Phased rollout:**

| Sub-phase | Action | Reversibility | Old path state |
|---|---|---|---|
| 6a | Install `instacart` CLI. Validate auth + read-only fetch parity against last 10 orders in `orders.sqlite`. No writes. | n/a — read-only test | Active, unchanged |
| 6b | Build new `instacart-api` skill (interactive ordering wrapper). Do NOT yet swap email-scan path. Verify on 1–2 manual test orders. | Don't enable until 6c | Active, unchanged |
| 6c | Cutover ingestion: update email-scan Instacart branch to call new CLI inline at receipt time. **Simultaneously** disable scraper cron (`enabled: false`). | Re-enable cron + revert email-scan prompt | Disabled |
| 6d | Cutover ordering: rename `instacart-skill/SKILL.md` → `SKILL.md.disabled`. New `instacart-api` is sole interactive path. | Rename back | Disabled |
| 6e | After 14 days clean: rename `instacart-orders/SKILL.md` → `SKILL.md.disabled` too (it has no remaining consumers once scraper cron is off). | Rename back | Disabled |

**Why this avoids duplication:**

- At any moment in any sub-phase, exactly ONE path is wired for ingestion (either email-scan inserts empty + scraper fills, OR email-scan inserts full). Never both.
- At any moment, exactly ONE skill name `instacart` (interactive ordering) is loaded by the agent — either browser-based or API-based, never both. The deprecated one's `SKILL.md` is renamed out.
- The crons are mutually exclusive by enabled state.

**Revert procedure (single emergency action):**

```bash
# Revert ingestion path (sub-phase 6c)
python3 -c "import json,sys;j=json.load(open('/Users/spratt/.openclaw/cron/jobs.json'));[setattr(__import__('builtins'), '_', __import__('builtins').dict.update(job, {'enabled': True})) for job in j['jobs'] if job.get('name')=='Instacart Order Scraper'];json.dump(j, open('/Users/spratt/.openclaw/cron/jobs.json','w'), indent=2)"
# (and revert email-scan prompt — the diff is small)

# Revert ordering path (sub-phase 6d)
mv ~/.config/spratt/skills/instacart-skill/SKILL.md.disabled \
   ~/.config/spratt/skills/instacart-skill/SKILL.md

# Revert scraper deprecation (sub-phase 6e)
mv ~/.config/spratt/skills/instacart-orders/SKILL.md.disabled \
   ~/.config/spratt/skills/instacart-orders/SKILL.md
```

Each sub-phase is independently reversible — no need to roll back all of 6 to undo any one of 6a–6e.

**Subsystems touched:** `instacart-skill` (deprecate), `instacart-orders` (deprecate), `cron-jobs.json` (disable one job), `email-scan/extract-email-actions.py` and the Email Scan cron prompt (call new CLI). New skill: `instacart-api`.
**Subsystems NOT touched:** `orders/order-ingest.py`, `orders/item-classify.py`, `orders/purchase-cadence.py`, `orders/reorder-nudge.py`, `skills/orders`, `skills/smart-reorder`, `skills/email-to-orders`, `meal-planner`, `recipe-instacart`, `orders.sqlite` schema.

**Open verification questions (settle in sub-phase 6a):**

- Does the new CLI handle 2FA the same way (gog email watcher)? If different, document the new auth pattern.
- Does the new CLI return items with prices and quantities matching what Instacart shows on the web receipt? Diff against scraped data from last 30 days.
- Does the email-receipt arrival give us a fetchable `order_id` immediately, or is there a window where Instacart's API doesn't yet return order details for a just-placed order? If there's a lag, email-scan needs a retry with backoff.

---

### 2. `seats-aero` + `flight-goat` — ADD points-redemption decisions to `card-wallet`

**Today:** `card-wallet` tracks 3 points cards (AMEX Platinum, CSR ×2 holders) at 2.0 cpp baseline. Zero ability to look up award space or compare to cash. Points sit idle.

**With both CLIs in `card-wallet`:** Spratt answers three new questions, all standalone iMessage Q&A:

1. **"What's X points worth flying to Y?"** — flight-goat returns cash anchor, seats-aero returns award space across MR/UR transfer partners, card-wallet computes cpp at your valuation. Break-even recommendation.
2. **"I have 200k MR expiring — best use?"** — seats-aero scans MR transfer partners (ANA, Aeroplan, Virgin, Avianca, etc.) for next-90-day sweet spots. Top 3 by cpp.
3. **"Book through Chase Travel portal?"** — flight-goat cash price vs CSR portal redemption (1.5¢ in portal) vs transfer-partner redemption (your 2.0¢ baseline), with pending Travel Credit factored in.

**Integration:**
- New section in `~/.config/spratt/skills/card-wallet/SKILL.md`: "Points redemption decisions"
- Two helpers in `~/.config/spratt/infrastructure/card-wallet/`:
  - `flight-cash.py` — wraps `flight-goat` for cash anchor
  - `award-search.py` — wraps `seats-aero` for award space + transfer partner enumeration
- Reuses existing `cards.point_valuation_cpp` column. No schema change.

**Subsystems touched:** `card-wallet` skill, `card-wallet/` infra.
**Subsystems NOT touched:** `trip-manager`, `trips.sqlite`, `flight-monitor`. No trip is ever created from this flow. No data is written to `trips.sqlite`. This is purely point-decision Q&A — same shape as today's "which card for X?" optimizer.

---

### 3. `table-reservation-goat` — AUGMENT `resy-booking` skill

**Today:** `resy-booking` skill is Resy-API only. OpenTable and Tock restaurants are invisible to Spratt.

**With `table-reservation-goat`:** Single query searches all three networks. Cancellation watches surface drop-ins for booked-solid spots.

**Integration:**
- Augment `resy-booking` skill's search step. Resy-only is the fallback if `table-reservation-goat` is unavailable.
- Cancellation watch fires via outbox → iMessage on availability.

**Subsystems touched:** `resy-booking` skill.
**Subsystems NOT touched:** trips.sqlite (reservations associated with trips still flow through `trip-db.py add-reservation` as today).

---

### 4. `weather-goat` — AUGMENT briefings with AQI, severe alerts, extended forecast

**Today:** `gather-briefing-data.py` calls `wttr.in/{lat,lon}?format=j1`. Gives temp + condition + 3-day forecast. No AQI. No severe weather alerts. No extended forecast.

**With `weather-goat`:** Three precise data adds:

| Field | Where it's added | Why |
|---|---|---|
| `aqi` | Both morning briefs (Manan + Harshita) | Seattle wildfire smoke; matters for outdoor plans with Sriram |
| `severe_alerts` | Both morning briefs | NWS warnings not on wttr |
| `extended_forecast` (5–10 day) | Future use: trip day briefs only | wttr's 3-day is fine for daily briefs |

**What I'm explicitly NOT taking from weather-goat:** Activity verdicts (GO/CAUTION/STOP). Haiku already synthesizes "rain after 3pm, postpone the walk" from wttr hourly data; the verdict is opinionated wrapping, not new info.

**Integration:**
- Replace wttr.in call in `gather-briefing-data.py` with weather-goat, exposing `aqi` and `severe_alerts` in the brief context.
- Compose step (Haiku) gets one new line in the prompt: "If AQI is poor or severe alert present, surface it in 1 short sentence."

**Subsystems touched:** `flows/gather-briefing-data.py`, `flows/compose-briefing.py` (prompt only).
**Subsystems NOT touched:** trip-manager, flight-monitor, anything else.

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

```python
# discovery-butler/nudge.py — daily 2pm cron, mostly silent
for person in (manan, harshita, plus active_trip_travelers):
    trip = active_trip_for(person)  # from trips.sqlite, read-only
    if trip:
        if trip.day_index in (1, 2, 3) and not already_sent_for_trip(person, trip.id):
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
- Source: `wanderlust-goat` for editorial freshness near the target location.
- Excludes: anything in `places.sqlite` (already saved), anything in today/tomorrow's calendar or `trips.sqlite` reservations (don't suggest what's already booked).
- One casual line composed by Haiku.

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
- New cron: daily 2pm — but most days the script exits silently after the rules say "don't fire"
- Output via outbox

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
- **Reply verbs** — what user replies count as "go" ("yes", "send it", "do it")

**Per-vendor crons:**

| Vendor | Cron | Anti-nag | Status |
|---|---|---|---|
| `dominos` | Friday 4:00pm | last Dominos order > 10 days | Phase 5 build |
| `jimmy-johns` | Weekday lunch (cadence TBD when adding) | last JJ order > 7 days | Planned, not in this phase — install when JJ CLI is wired |

Adding a future vendor (e.g., another sandwich/burger chain): drop a new `vendors/<name>.md`, add one cron, point both at the shared compose + reply-handling helpers. No skill rebuild.

**Universal gates (apply to every vendor):**
- Active/upcoming trip covering tonight in `trips.sqlite` → skip
- Dinner reservation tonight (trips.sqlite reservations OR icalBuddy) → skip
- Vendor-specific anti-nag (per table above) → skip
- Sriram bedtime / school-night logic if it ever matters — flagged for future, not now

**Universal pipeline (per vendor):**
1. Hook fires, gates evaluated, vendor module supplies order template + voice.
2. Vendor CLI (`dominos`, eventually `jimmy-johns`, …) computes tonight's deal/availability + price.
3. card-wallet's purchase-optimizer returns the best card (almost always CSR 3x dining; rare card-specific deals can override).
4. Compose step (Haiku) emits ONE iMessage with inline order, price, and reply verbs.
5. Reply lands in interactive context. Spratt parses verb, calls vendor CLI to place order, routes confirmation + delivery tracking through outbox.

**Sample output (Dominos, Friday 4pm):**
```
Pizza tonight, sir? Domino's BOGO is live.
Your usual: 2 large pepperoni + cheesy bread, $28.40 with the deal.
Use CSR (3x dining). ETA 35 min from order.
Reply "yes" to send it.
```

**Sample output (Jimmy John's, weekday lunch — planned shape):**
```
Lunch, sir? JJ #9 Italian Night Club, $9.50 with chips.
Delivery 12 min to home. CSR 3x dining.
Reply "yes" to send it.
```

**Reply handling (shared):**

Inline everything in the outbox message — order, price, card. Reply context arrives carrying enough state that Spratt doesn't need perfect memory recall to act. The reply handler is in the skill itself, not per-vendor, so adding a vendor doesn't touch reply parsing.

**Implementation phasing inside Phase 5:**
- **5a** — Build `food-butler` skill scaffold + Dominos tenant + Dominos cron. Ship.
- **5b** — Once stable (4+ Friday cycles), add `jimmy-johns` tenant + JJ cron in a single PR. Tests the multi-tenant assumption.
- Future vendors follow the 5b pattern.

**Subsystems touched:** New `food-butler` skill + 1 new cron initially (Dominos), more crons as vendors are added. `orders.sqlite` (read for anti-nag, write for new orders via existing `order-ingest.py` — same contract).
**Subsystems NOT touched:** trip-manager, flight-monitor, destination-aware, recipe-instacart, instacart pipeline.

---

## Phasing

| Phase | What | Reversibility |
|---|---|---|
| 1 | `weather-goat` into briefings | Trivial — swap one HTTP call back to wttr |
| 2 | `seats-aero` + `flight-goat` into card-wallet (new redemption section) | Fully additive — new skill section, new helpers, no existing path changed |
| 3 | `table-reservation-goat` aug for resy skill | Resy-only is the fallback; safe parallel |
| 4 | `wanderlust-goat` weekend-butler brief (Thursday cron) | Standalone new cron — disable to revert |
| 5 | `food-butler` skill + `dominos` first tenant (Friday check-in cron); `jimmy-johns` added in 5b when JJ CLI is wired | Standalone new skill + per-vendor crons — disable each to revert independently |
| 6 | `instacart` replace browser path | 2-week parallel run before disabling scraper cron |

Phases 1–3 first (lowest risk, smallest blast radius). Phases 4–5 depend on phase 3 (`table-reservation-goat` is the bookability layer the weekend brief uses; also gives confidence on the dominos reply-and-act pattern). Phase 6 last because biggest blast radius — actively-running browser scraper to retire.

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

## Open items

- **Weekend-butler reply handling**: needs verification at phase 4 build that interactive iMessage reply ("book Boat Bar") carries enough context for Spratt to act. Inline-everything in the outbox message is the chosen design.
- **Instacart phase 6a auth verification**: open until the new CLI's 2FA pattern is validated against `gog` email watcher (see phase 6 detail).
