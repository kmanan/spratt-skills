![image (2) (Small)](https://github.com/user-attachments/assets/1c4afea6-c572-48ff-9bbd-d1a3768f3fe1)

# Spratt Skills — Household Automation Add-ons for OpenClaw

A collection of infrastructure add-ons for [OpenClaw](https://github.com/openclaw/openclaw) that turn it into a household operating system. Built for a real family, running in production on a Mac Mini M4.

These aren't typical OpenClaw skills (SKILL.md files that teach the LLM what to do). Most are **infrastructure** — daemons, databases, pipelines, and automation that run alongside OpenClaw, handling the things an LLM shouldn't be trusted to do reliably. Some (Apple Reminders, Tool Routing) are pure skill definitions that teach the LLM correct tool selection and usage patterns.

## The Core Idea: LLM Plans, Code Delivers

Every component follows the same principle: the LLM decides what to do, deterministic code executes it. No LLM is involved at message delivery time, flight polling time, or database write time. The LLM's job is language and reasoning. Code handles reliability.

---

## Components

### 1. [Outbox](./outbox/) — Scheduled Message Delivery

A SQLite message queue with a polling daemon. The LLM writes messages to a table with a scheduled send time. A Python daemon delivers them via iMessage (BlueBubbles). No LLM at delivery time.

**Why it exists:** OpenClaw crons re-invoke the LLM at delivery time. The LLM reinterpreted instructions differently each run — trip dinners went as individual DMs instead of group chat, a flight monitor ran 22 polls and delivered zero notifications. The outbox pattern eliminates this failure mode entirely.

| | |
|---|---|
| **What you get** | outbox.py (CLI + Python API), sender.py (daemon), SQLite schema |
| **Dependencies** | Python 3, SQLite, iMessage via [BlueBubbles](https://bluebubbles.app/) + imsg CLI |
| **Schedule** | Persistent daemon (60s polling loop). Messages delivered within ~1 minute of scheduled time. |
| **macOS-specific** | launchd plist for the daemon (KeepAlive). Adaptable to systemd. |
| **Setup time** | ~15 minutes |

### 2. [Trip Manager](./trip-manager/) — Database-First Trip Automation

The LLM writes trip data directly to SQLite through a CLI (`trip-db.py add-flight`, `add-hotel`, etc.). Outbox messages are auto-generated for flight reminders, hotel check-ins, and dinner notifications. Flight monitor state is auto-derived. Update any record, regenerate downstream — only the changed items, no full regeneration.

**Setup automation:** Creating a trip automatically texts the household manager asking "solo or group?" A solo reply triggers `setup-solo` (resolves the traveler from contacts, sets their phone as the notification recipient). A group reply triggers adding travelers + `find-group-chat` (scans recent iMessage chats to discover the group GUID). No manual GUID lookups.

**Why it exists:** Family travel has dozens of moving parts — flights, hotels, restaurants, Uber links, group chat notifications — spread across confirmation emails and text threads. Without structure, details get lost and nobody gets reminded. The trip manager gives the LLM a single database to write to, and deterministic scripts handle all the downstream notification scheduling and flight tracking setup.

| | |
|---|---|
| **What you get** | trip-db.py (CLI with 13 subcommands including `setup-solo` and `find-group-chat`), trip-outbox-gen.py, trip-status.py, SQLite schema (5 tables) |
| **Dependencies** | Python 3, SQLite, Outbox (above), contacts.sqlite (for name→phone resolution) |
| **Schedule** | N/A — CLI tools invoked on demand by the LLM or by email scanning cron. |
| **macOS-specific** | No (all scripts are standalone CLI tools) |
| **Setup time** | ~15 minutes (after Outbox is set up) |

### 3. [Flight Monitor](./flight-monitor/) — Real-Time Flight Tracking Daemon

A persistent daemon that polls FlightAware AeroAPI, detects events (landing, delay, gate change, diversion), and sends notifications through the outbox. Adaptive polling — 3 minutes during active window, 30 minutes when idle. No LLM in the polling loop.

**Why it exists:** An LLM-mediated flight cron used browser scraping, couldn't interpret "not found," and self-deleted after "completing" with zero notifications sent. The original implementation used `FlightRadarAPI` — a community scraper of FlightRadar24's public data, not an official API — which returned inconsistent results for valid flights. Migrated to FlightAware AeroAPI for stable, authenticated access.

| | |
|---|---|
| **What you get** | flight_monitor.py, track_flight.py, state derivation from trips DB |
| **Dependencies** | Python 3, FlightAware AeroAPI key (~$5/mo), Outbox (above) |
| **Schedule** | Persistent daemon. Polls every 3 min during active flight window, every 30 min when idle. |
| **macOS-specific** | launchd plist (KeepAlive + PathState). Adaptable to systemd. |
| **Setup time** | ~15 minutes (after Outbox is set up) |

### 4. [Email-to-Orders](./email-to-orders/) — Order & Shipment Tracking from Email

An email scanning cron extracts grocery/shopping order details from email confirmations and inserts them into SQLite. Supports Instacart, Amazon Fresh, Whole Foods, DoorDash, and more. Also tracks shipping status — when a tracking number or delivery update arrives by email, it's stored on the order and the household gets notified for significant events (out for delivery, delivered).

**Why it exists:** "Did we already buy cilantro this week?" and "Where's that Amazon package?" are real questions in a household. The orders database makes them queryable — search by item name, check delivery status, all from one place.

| | |
|---|---|
| **What you get** | order-ingest.py (CLI with insert, update-items, update-tracking), SQLite schema with tracking columns, email scan cron prompt template |
| **Dependencies** | Python 3, SQLite, an email scanning setup (Gmail via gog CLI, Outlook via Microsoft Graph, or any IMAP) |
| **Schedule** | Cron, 3x/day. Scans emails from the last 8 hours. |
| **macOS-specific** | No |
| **Setup time** | ~10 minutes |

### 5. [Instacart Orders](./instacart-orders/) — CLI-Driven Order History Pipeline

A CLI-driven order-history pipeline. `instacart-pp-cli` owns the local SQLite DB, the canonical IDs, and the `history import` intake. The only browser involvement is a one-shot Apollo-cache read per order-detail page using `extract-one.js` — a snippet shipped inside the CLI's own `docs/` directory. Instacart has no clean GraphQL op for order history (the CLI's `history sync` command is documented as broken upstream for exactly that reason), so the CLI author prescribed this read-once-from-cache approach and we drive it from Python: launchd fires nightly, the orchestrator walks every order page, runs `extract-one.js` to capture the Apollo record, and pipes each JSON record to `instacart-pp-cli history import -` for durable storage.

Per-order durability: the orchestrator extracts → imports → persists processed state after every order, so a mid-run crash never loses data. Each successful order is written to `~/Library/Application Support/instacart/instacart.db` (orders + order_items) with **canonical Instacart IDs** — `order_id`, `item_id` (`items_<retailerId>-<legacyId>`), `product_id`, retailer slug, delivered_at, and per-item quantity/quantity_type. Those canonical IDs are what makes deterministic cart-building (next sections) possible.

**Why it exists:** Email confirmations don't carry itemized order details, and the older LLM-driven nightly scraper that read receipt pages was non-deterministic (mismatched orders, missed items, hallucinated prices). Replacing it with a deterministic CLI-owned import that reads from Instacart's own client-side Apollo cache eliminates that whole class of failure.

| | |
|---|---|
| **What you get** | `history-scrape.py` (per-order extract→import via the CLI's bundled `extract-one.js` + CLI stdin), `cadence.py` (median day-gap per `(item_id, retailer_slug)` from the CLI DB), SKILL.md, launchd plist example |
| **Dependencies** | `instacart-pp-cli` (from [printing-press-library](https://github.com/mvanhorn/printing-press-library)) which ships `docs/extract-one.js`, OpenClaw browser tool (only to load the order-detail page and run the CLI's extractor), an active Instacart session (`auth login` or `auth paste`), Outbox (above) for failure alerts |
| **Schedule** | launchd `com.spratt.instacart-history-scrape` nightly at 9pm local. Incremental by default; `--backfill` walks every visible order. |
| **macOS-specific** | launchd plist is macOS-specific; underlying script is portable to any cron. |
| **Setup time** | ~15 minutes (once `instacart-pp-cli` is installed and logged in) |

### 6. [Smart Reorder](./smart-reorder/) — Purchase Cadence + Dual-Source Reorder Nudge

Predicts when each household item is due to be reordered based on median days between purchases. Two cadence sources, unioned by `canonical_key`:

- **Amazon (legacy path):** `purchase-cadence.py` against `orders.sqlite`. An LLM (Flash) classifies receipt item names into canonical products — so "Bounty Paper Towels 12pk" and "Bounty Select-a-Size 12 Mega Rolls" are recognized as the same thing, while a different brand stays separate.
- **Instacart (CLI path):** `cadence.py` against the printing-press CLI's SQLite DB (populated by [Instacart Orders](./instacart-orders/) above). No LLM classification needed — the CLI's canonical `item_id` IS the join key. "Same item" is determined by Instacart, not a heuristic.

`reorder-nudge.py` queries both sources and ships one iMessage on Wed + Sat 8am PT. If `cart-build.py` (next section) ran 15 minutes earlier and staged anything, the message reframes from "due for reorder" to "🛒 Staged in Instacart cart — review and check out" and links to the checkout URL. Amazon items always appear under "📦 Amazon — buy manually". A `reorder_notifications` table tracks last-notified state per canonical key so the same item doesn't re-announce until it's actually purchased again.

**Why it exists:** "We're out of milk again" shouldn't require remembering. The system knows you buy milk every 7 days, your last order was 8 days ago, and at 7:45am Wednesday morning it already filled a cart for you — at 8am it pings your phone telling you to open the Instacart app and check out.

| | |
|---|---|
| **What you get** | `purchase-cadence.py` (Amazon-side cadence over orders.sqlite, supports `--sources`), `item-classify.py` (alias management CLI), `reorder-nudge.py` (dual-source nudge + cart-status-aware framing), SQLite schema (item_aliases + reorder_notifications tables) |
| **Dependencies** | Python 3, SQLite, [Instacart Orders](./instacart-orders/) for the CLI cadence side, Email-to-Orders for the Amazon side, Outbox (above) for delivery |
| **Schedule** | Item classification runs nightly. Cadence analysis is on-demand. Reorder-nudge fires Wed + Sat 8am PT via launchctl — no LLM at notification time, just SQL → text → outbox. |
| **macOS-specific** | The reorder-nudge plist is macOS-specific; underlying script is portable. |
| **Setup time** | ~5 minutes (after Instacart Orders is set up) |

### 7. [Instacart API](./instacart-api/) — CLI-Driven Cart Building

Builds grocery carts on Instacart by calling `instacart-pp-cli add <retailer> --item-id <id>` against the canonical IDs captured by [Instacart Orders](./instacart-orders/) above. No browser, no DOM scraping, no autosuggest, no fuzzy matching. Direct GraphQL to Instacart's backend through the CLI's session. Warm calls are sub-second.

The auto-replenishment loop (`cart-build.py`) runs on launchd Wed + Sat 7:45am PT — 15 minutes before the reorder nudge. It calls `cadence.py --due-only --format json` to get items due for reorder, then stages each one with `instacart-pp-cli add <retailer> --item-id <id> --qty N --yes`. It writes a status JSON; the reorder nudge reads it within a 45-minute window and changes its message from "due for reorder" to "🛒 Staged in Instacart cart". One human action: open the Instacart app, tap Place Order.

The bot builds the cart. Manan places the order. The CLI has no `place`/`checkout` action — that's an intentional design constraint.

**Why it exists:** The previous browser cart-building path (DOM-driven URL search + click-Add) was 11/14 wrong on a typical run — Instacart's autosuggest matches "organic milk" with a non-organic shop's top result, the wrong unit, or last week's promotion. Once `instacart-pp-cli history import` has the canonical IDs locally, `--item-id` bypasses search entirely and adds the exact item that was bought before. Accuracy went to 100% with one tap to ship.

| | |
|---|---|
| **What you get** | `cart-build.py` (deterministic cadence → cart staging via CLI), SKILL.md (full command surface, auth recovery flow, programmatic invocation contract for downstream skills), launchd plist example |
| **Dependencies** | `instacart-pp-cli` (from [printing-press-library](https://github.com/mvanhorn/printing-press-library)), [Instacart Orders](./instacart-orders/) for the canonical IDs the CLI cadence reads, Outbox (above) for failure alerts |
| **Schedule** | launchd `com.spratt.instacart-cart-build` Wed + Sat 7:45am PT. Also invokable interactively for ad-hoc carts. |
| **macOS-specific** | launchd plist is macOS-specific; underlying script is portable. |
| **Setup time** | ~10 minutes (`instacart-pp-cli auth login` once, then drop the plist) |

### 8. [Discovery Butler](./discovery-butler/) — Casual Coffee / Quick-Bite Nudges

A friend who knows the area and occasionally texts you a fun spot. Daily 2:00pm launchd cron, mostly silent. Fires Thursday (Friday fallback) for two principals at home, and day 1 (or 2/3 fallback) of any active trip into the trip's iMessage chat. One per person per ISO week at home; one per trip total on trips.

**Why it exists:** `places.sqlite` tracks places you've already saved, but there was no surface for "what's new and worth trying." Browsing-when-hungry produces decision fatigue picks; a once-a-week nudge with a single walk-in suggestion solves a real ergonomic gap without becoming another notification to mute. Most cron firings produce nothing — that silence is the point.

| | |
|---|---|
| **What you get** | `scripts/nudge.py` (~280 lines — discovery, dedup vs places.sqlite, compose with LLM gateway, outbox-routed delivery, fires-DB dedup), SKILL.md, launchd plist example |
| **Dependencies** | [`wanderlust-goat-pp-cli`](https://github.com/mvanhorn/printing-press-library) (needs `GOOGLE_PLACES_API_KEY`), OpenClaw with gateway access to `openai-codex/gpt-5.5` (or any compose model — falls back to Flash, then deterministic template), Spratt's outbox CLI, Home Assistant `person.*` + Places integration (optional — falls back to a hardcoded home anchor), `trips.sqlite` with `trips` + `travelers` tables (optional — at-home flow works without it) |
| **Schedule** | launchd `com.spratt.discovery-butler` daily 2:00pm. Mostly silent. Smoke-test path: `python3 nudge.py --smoke` (compose + print, no outbox/DB writes). |
| **macOS-specific** | launchd plist is macOS-specific; underlying script is portable. |
| **Setup time** | ~10 minutes once `wanderlust-goat-pp-cli` is installed and `GOOGLE_PLACES_API_KEY` is in env. |

### 9. [Outlook Graph](./outlook-graph/) — Outlook Email & Calendar via Microsoft Graph

Shell scripts for managing Outlook/Hotmail email and calendar through Microsoft Graph API. Multi-account OAuth2 with auto-refreshing tokens. Calendar events support descriptions, attendees, and multi-calendar targeting — create a family appointment on the "For Family" calendar with attendees and notes in one command.

**Why it exists:** The ClawHub outlook-plus skill only had basic event CRUD — no description/body field, no attendees, no multi-calendar targeting. For a household assistant that needs to create shared calendar events with notes ("doctor appointment — symptoms to discuss: ...") and invite family members, those are table-stakes features.

| | |
|---|---|
| **What you get** | outlook-calendar.sh, outlook-mail.sh, outlook-setup.sh, outlook-token.sh |
| **Dependencies** | bash, curl, jq. Azure CLI for initial setup only. |
| **Schedule** | N/A — shell scripts invoked on demand by the LLM or by email scanning cron. |
| **macOS-specific** | No |
| **Setup time** | ~10 minutes |

### 10. [Places](./places/) — Save & Search Restaurants, Activities, Attractions

A SQLite database for places you want to remember — restaurants, bars, activities, attractions. Share an Instagram post, Google Maps link, Yelp page, or just say "remember that Thai place on Queen West" and it gets saved with category, cuisine, location, tags, and notes. Query by vibe ("date night spots"), location, cuisine, or who saved it. Track visits and ratings.

**Why it exists:** Interesting places come from Instagram stories, friend recommendations, and articles — then get forgotten. This gives the LLM a structured place to save them and a way to surface them when you ask "where should we go for dinner?"

| | |
|---|---|
| **What you get** | SKILL.md (OpenClaw skill definition), SQLite schema, setup script |
| **Dependencies** | SQLite, OpenClaw browser tool (for Instagram/Facebook/TikTok URL extraction) |
| **Schedule** | N/A — interactive skill, invoked on demand when user shares a place. |
| **macOS-specific** | No |
| **Setup time** | ~5 minutes |

### 11. [Destination-Aware Reminders](./destination-aware/) — Tesla Nav → Context Surfacing

When you set a destination in your Tesla, this daemon detects it via Home Assistant's WebSocket `subscribe_trigger` and surfaces relevant context before you arrive — shopping lists for grocery stores, appointment notes for doctors, pickup reminders for daycare. No zones, no polling, no HA automations. The Tesla tells HA where you're going, the daemon identifies what's there via Google Places, and sends a text with what you need to know. Every category (grocery, daycare, pharmacy, medical, home, work, restaurant) runs through Haiku with a category-specific prompt so unrelated todos don't get dumped into the message. Now includes full instructions for **creating destination-aware reminders** — recurring (weekly day-specific), one-time, and permanent — with rules for how the temporal gate and LLM filter interact so reminders fire on the right day and for the right destination.

**Why it exists:** "Bring diapers to daycare" sitting in a reminder list doesn't help if you only see it at 7am and forget by 5pm pickup. The reminder should surface when you're actually heading there. And when the LLM creates those reminders, it needs to understand the daemon's gating mechanism — a non-recurring reminder for a weekly task goes overdue and fires on every matching trip regardless of day.

| | |
|---|---|
| **What you get** | destination-daemon.py (persistent WebSocket client with triple liveness), destination-context.py (place resolver + context gatherer), SKILL.md |
| **Dependencies** | Python 3, `websocket-client` pip package, Home Assistant with Tesla integration, [goplaces](https://github.com/openclaw/goplaces) CLI (Google Places API), Outbox (above), `ANTHROPIC_API_KEY` in the daemon's plist EnvironmentVariables for per-category Haiku LLM filtering |
| **Schedule** | Persistent daemon. Reacts instantly when Tesla nav destination is set. Zero polling. |
| **macOS-specific** | launchd plist (KeepAlive). Adaptable to systemd. |
| **Setup time** | ~10 minutes (after Outbox is set up). See [destination-aware/README.md](./destination-aware/README.md) for deployment gotchas. |

### 12. [Delivery Watcher](./delivery-watcher/) — On-Porch Package Alerts

KeepAlive daemon that texts both principals when an Amazon or Instacart package is **delivered** and the front-door contact sensor hasn't changed state in the 5 minutes since. The trigger is a sender+subject hook inside the existing email-scan job (no LLM, no body parsing, no mailbox polling in this daemon) that writes a row into `delivery_signals.sqlite`; the daemon polls that local table every 30s. The Ring door sensor in Home Assistant is the "did you already grab it" gate. No camera, no vision, no OCR — the carrier already tells us *which* order arrived.

**Why it exists:** A delivery notification from the Amazon app says "delivered." It doesn't say "...and it's still sitting on your porch in the rain." This skill closes that loop — if you opened the front door within 5 minutes of the delivery, it stays silent. Otherwise both you and your partner get a one-line text so somebody grabs it before the porch pirate or the weather does.

| | |
|---|---|
| **What you get** | `scripts/watch.py` (single-file daemon — polls `delivery_signals.sqlite`, queries HA, writes outbox), `scripts/gather-emails.hook.py` (drop-in hook for your existing inbox-scan job), SKILL.md, launchd plist example |
| **Dependencies** | Any existing inbox-scan job that fetches Outlook/Gmail metadata on a schedule (this repo's email-to-orders is one such), Home Assistant with a door contact sensor (`device_class=door`), Outbox CLI |
| **Schedule** | launchd `com.spratt.delivery-watcher`, KeepAlive, 30s poll loop. First-run backfill silently marks existing delivered rows as already-notified so the daemon doesn't spam on startup. |
| **macOS-specific** | launchd plist (KeepAlive). Adaptable to systemd. |
| **Setup time** | ~5 minutes once email-scan + HA + outbox are wired |

### 13. [Card Wallet](./card-wallet/) — Credit Card Benefits + Purchase Optimization

Merged skill that tracks both **"use it or lose it" credit card benefits** (monthly credits, quarterly categories, semi-annual windows) and **per-purchase reward optimization** ("which card for groceries?"). A weekly cron checks expiring benefits and notifies each cardholder with a tiered-brevity format — urgent and this-period items show full detail, while 30+ day items collapse into a single summary line to keep the message scannable. A monthly LLM-powered refresh searches the web for benefit and reward rate changes. Interactive queries recommend the optimal card per spending category with cap awareness and network acceptance warnings (Amex fallbacks).

**Why it exists:** AMEX Platinum alone has 7+ expiring credits across monthly, semi-annual, and annual cycles. Nobody remembers them all. And nobody does the math on "Chase Freedom 5% on rotating categories this quarter vs Apple Pay 2% vs Sapphire Reserve 3x dining" in their head. Spratt does both. Evolved from the standalone card-perks tracker by merging with the [card-optimizer](https://clawhub.com/skills/card-optimizer) skill from ClawHub (by scottfo).

| | |
|---|---|
| **What you get** | card-wallet-check.py (weekly cron), card-wallet-refresh.py (monthly helper), SKILL.md (interactive benefit + purchase queries), SQLite schema (7 tables: cards, benefits, usage, benefit_changes, reward_rates, quarterly_categories, spending_estimates) |
| **Dependencies** | Python 3, SQLite, Outbox (above), Apple Reminders via remindctl, Claude Haiku API (for monthly refresh, ~$0.03/mo) |
| **Schedule** | Weekly cron (Saturdays) for expiration checks. Monthly cron for benefit + reward rate refresh. Quarterly cron (Jan/Apr/Jul/Oct) for rotating category lookups. |
| **macOS-specific** | Apple Reminders via remindctl (optional — remove reminder creation for Linux) |
| **Setup time** | ~10 minutes (after Outbox is set up) |

### 14. [Meal Planner](./meal-planner/) — Weekly Meal Planning with Instacart Integration

Weekly meal planning that reads from your recipe database, checks pantry inventory, and generates shopping lists that feed directly into the Instacart API pipeline. Handles dietary restrictions, household coordination (adults vs kids), batch cooking, and budget tracking. Based on the [meal-planner](https://clawhub.com/skills/meal-planner) skill from ClawHub (by clawic), adapted to use SQLite-backed recipes and Instacart API CLI cart-building instead of static lists.

**Why it exists:** Meal planning involves recipes you've saved, groceries you need to buy, and what's already in the pantry. Without integration, you're copying ingredient lists from one app to another. This connects the recipe database to the grocery pipeline so "plan this week's meals" ends with "cart built on Instacart, ready to place."

| | |
|---|---|
| **What you get** | SKILL.md (planning instructions integrated with recipes.sqlite + Instacart pipeline), setup.md, shopping-guide.md, meal-prep.md, budget-tips.md, memory-template.md |
| **Dependencies** | recipes.sqlite (from recipe-instacart skill), orders.sqlite + instacart.db (for purchase history), Instacart API (for cart building) |
| **Schedule** | N/A — interactive skill, invoked on demand when user wants to plan meals. |
| **macOS-specific** | No |
| **Setup time** | ~5 minutes + first-use household onboarding conversation |

### 15. [Apple Reminders](./apple-reminders/) — Full Reminders Management + Recurring Support

Full Apple Reminders management via the `remindctl` CLI (view, add, edit, complete, delete, list routing) plus a compiled Swift binary for recurring reminders via EventKit. The `remindctl` CLI doesn't support recurrence natively, so the EventKit binary fills that gap.

**Why it exists:** "Remind me every Monday to bring diapers to daycare" should create **one recurring reminder**, not 12 individual copies. And the LLM needs clear routing rules for which reminder list to use (per-person lists, Shared, Shopping) and how to create destination-aware reminders that interact correctly with the destination daemon's temporal gate.

| | |
|---|---|
| **What you get** | SKILL.md (full remindctl usage + list routing + destination-aware cross-reference), create-recurring-reminder (Swift binary + source) |
| **Dependencies** | macOS, Swift compiler (Xcode Command Line Tools), [remindctl](https://github.com/steipete/remindctl) |
| **Schedule** | N/A — invoked on demand when user requests a reminder. |
| **macOS-specific** | Yes (EventKit is Apple-only) |
| **Setup time** | ~2 minutes (compile + grant permissions) |

### 16. [Email PDF Attachment](./email-pdf-attachment/) — Native PDF Extraction from Email

Skill instructions for finding Outlook/Gmail emails with PDF attachments, downloading them to an OpenClaw-allowed media path, and reading them through OpenClaw's native PDF capability. The scheduled Spratt email scan uses the same principle: PDF attachments are processed through OpenClaw's bundled `document-extract` PDF extractor before LLM structured extraction.

**Why it exists:** Email attachments are a common source of travel bookings, invoices, medical reports, and other structured data. The LLM should not ask the user to paste PDF contents or invent a local parser path. It should use the native PDF tool/extractor and then write deterministic outputs through the right system.

| | |
|---|---|
| **What you get** | SKILL.md with Outlook/Gmail attachment lookup, download allowlist, native `pdf` tool usage, scheduled-script `document-extract` path, and downstream action mapping |
| **Dependencies** | OpenClaw `pdf` skill/tool, OpenClaw bundled `document-extract` plugin, Outlook Graph scripts or `gog` Gmail CLI |
| **Schedule** | N/A — invoked by interactive workflows and mirrored by scheduled email scan implementation |
| **macOS-specific** | No for extraction; downstream actions may use macOS Reminders/Calendar depending on the workflow |
| **Setup time** | ~2 minutes if email auth is already configured |

### 17. [Tool Routing](./tool-routing/) — Intent-to-Tool Mapping

A routing table that maps user intents to the correct tool or skill. Covers messaging (live `message` vs scheduled `outbox`), productivity tools, web/browser, trips, email attachments, the "what are our plans?" multi-source check, forwarded-email semantics, and the cron-vs-outbox hard boundary. Also includes TaskFlow guidance for multi-step interactive workflows (trip planning, Instacart cart building, Resy bookings) that span multiple turns and need durable state tracking.

**Why it exists:** With 13+ skills and multiple messaging pathways, the LLM needs a single reference for "which tool do I use for this?" Without it, common mistakes recur — using crons for message delivery instead of the outbox, sending scheduled messages via `message` instead of the outbox, or missing data sources when asked about plans.

| | |
|---|---|
| **What you get** | SKILL.md (routing table, messaging rules, TaskFlow patterns, cron boundary) |
| **Dependencies** | None (pure routing instructions) |
| **Schedule** | N/A — read on demand when the LLM needs to pick a tool. |
| **macOS-specific** | No |
| **Setup time** | ~1 minute (copy to skills directory) |

---

## Architecture

```
              All databases in one directory: db/
              (trips, outbox, orders, recipes, places, contacts, cards)

Human → LLM → trip-db.py add-trip
                    ↓
              trips.sqlite → outbox "Solo or group?" text to manager
                    ↓
              Manager replies → setup-solo (contacts lookup → phone)
                              → or add-travelers + find-group-chat (imsg scan → GUID)
                    ↓
              trip-db.py add-flight, add-hotel, add-reservation, etc.
                    ↓
              trip-outbox-gen.py (deterministic templates)
                    ↓
              outbox.sqlite (scheduled messages)
                    ↓
              sender.py daemon (60s polling) → imsg CLI → iMessage

              flight_monitor.py daemon (3 min polling) → FlightAware AeroAPI
                    ↓ (reads trips.sqlite directly, no sidecar state)
              outbox.sqlite → sender.py → iMessage

Human → LLM → places.sqlite (save place from URL or description)
              LLM ← places.sqlite (query: "date night spots we haven't tried")

Email → email scan cron (Flash triage → extract)
                    ↓
              order-ingest.py → orders.sqlite (metadata, items, tracking)
              trip-db.py → trips.sqlite
              outlook-calendar.sh → Outlook calendar (with attendees + notes)

              launchd com.spratt.instacart-history-scrape (nightly 9pm)
                    ↓
              history-scrape.py walks order-history page via OpenClaw browser
              (Instacart has no GraphQL op for history; CLI's `history sync` is broken upstream)
                    ↓
              instacart-pp-cli ships docs/extract-one.js — one-shot Apollo cache reader, per order
                    ↓
              pipes each record to `instacart-pp-cli history import -` (stdin, per-order durable)
                    ↓
              ~/Library/Application Support/instacart/instacart.db (orders + order_items, canonical IDs)

              launchd com.spratt.instacart-cart-build (Wed + Sat 7:45am)
                    ↓
              cart-build.py → cadence.py --due-only (median day-gap per item_id, retailer)
                    ↓
              `instacart-pp-cli add <retailer> --item-id <id> --qty N --yes` per due item
                    ↓
              Instacart cart staged (CLI has no `place` — Manan opens app, taps Place Order)
                    ↓
              launchd-status/instacart-cart-build.json (consumed by reorder-nudge below)

              launchd com.spratt.reorder-nudge (Wed + Sat 8am, 15 min after cart-build)
                    ↓
              fetch Amazon cadence (purchase-cadence.py → orders.sqlite, Flash item-classify)
              fetch Instacart cadence (cadence.py → instacart.db, canonical item_id join)
                    ↓
              if cart-build status ≤45 min old: "🛒 Staged in Instacart cart — review & checkout"
              else: "🛒 Instacart — due for reorder" (manual)
              + "📦 Amazon — buy manually"
                    ↓
              outbox.sqlite → sender.py → iMessage

launchd com.spratt.discovery-butler (daily 2pm, mostly silent)
                    ↓
              nudge.py — at home: Thu/Fri only; on trip: day 1–3 only
                    ↓
              wanderlust-goat-pp-cli goat <anchor> --criteria "third-wave coffee" --agent
                    ↓
              dedup vs places.sqlite (already-saved names) + business_status=OPERATIONAL
                    ↓
              openclaw infer model run --gateway --model openai-codex/gpt-5.5 (Flash fallback)
                    ↓
              outbox.sqlite → at-home: Manan + Harshita (separate); trip: trip.group_chat_guid
                    ↓
              discovery_fires.sqlite marks (person, kind, key) so it can't re-fire

Human → "plan meals this week"
                    ↓
              meal-planner → reads recipes.sqlite + pantry inventory
                    ↓
              weekly plan + ingredient list
                    ↓
              Instacart API (CLI) → cart staged → Manan taps Place Order in app

Tesla nav destination set → sensor.maha_tesla_destination changes
                    ↓
              destination-daemon.py (WebSocket subscriber, instant)
                    ↓
              goplaces resolve → identify place (grocery? daycare? doctor?)
                    ↓
              remindctl + icalBuddy → candidate context
                    ↓
              compose filter (Haiku per category):
                grocery → "pick only grocery-cart items" (no "drop off at X", no work todos)
                daycare → "pick only daycare-relevant items" (kid supplies, forms, teacher convos)
                pharmacy/medical/home/work/restaurant → category-specific prompt
                no match from any category → stay silent
                    ↓
              outbox.sqlite → sender.py → "🛒 Heading to QFC — cilantro, milk, paper towels"

email-scan hook (gather-emails.hook.py) records delivery signals
              into delivery_signals.sqlite (Amazon "delivered" emails,
              Instacart "receipt" emails, etc.) — no orders.sqlite write
                    ↓
              delivery-watcher.py daemon (KeepAlive, 30s poll)
                    ↓
              deliveries.sqlite — record + dedup on signal id
                    ↓
              T+5min: GET /api/states/binary_sensor.<front_door>
                    ↓
              door last_changed >= delivery_ts ?
                yes → silent (mark skipped='door changed at <iso>')
                no  → outbox.sqlite → both principals (separate messages)
                       "📦 Amazon delivered: <item> + N more. Still on porch."

Saturday cron → card-wallet-check.py (deterministic)
                    ↓
              cards.sqlite (benefits, reward_rates, usage tracking)
                    ↓
              outbox.sqlite + Apple Reminders (if expiring within 10 days)

Monthly cron → card-wallet-refresh.py dump → Haiku (web search + diff)
                    ↓
              cards.sqlite (benefit + rate updates) → outbox notification if changed

Human → "which card for dining?"
                    ↓
              reward_rates + quarterly_categories → best card recommendation
```

---

## Reliability Patterns

A few hard-won patterns that every component follows. These exist because we
hit the specific failure mode each prevents, and you almost certainly will too
if you don't adopt them.

### 1. One database directory — no scattered paths

**Problem:** databases were scattered across `infrastructure/outbox/`,
`trips/`, `orders/`, `cards/`, `infrastructure/contacts/` — no pattern. When the
LLM agent couldn't find a database, it silently created a 0-byte file at whatever
path it guessed, causing split-brain reads and ghost tables. This happened three
times in two weeks.

**Pattern:** all databases live in one flat directory (`db/`). Every script uses
a hardcoded absolute path to `~/.config/spratt/db/<name>.sqlite` — no
self-relative resolution (`__file__`-based), no symlinks, no searching. The
canonical path table is in `TOOLS.md` (loaded on every LLM turn) and repeated
in each skill's `SKILL.md`.

### 2. Strict file-exists guard — no silent DB auto-create

**Problem:** `sqlite3.connect(path)` silently creates an empty new database file
if the path doesn't exist. Combined with scattered paths (now fixed by pattern 1),
a typo or config error can cause the app to create a brand new empty DB at the
wrong path and start reading/writing to it. The real data sits untouched
elsewhere. Split-brain, no error.

**Pattern:** every script that opens a SQLite DB checks the file exists first;
if not, it prints a clear `FATAL` error pointing at the expected path and exits
with code 1. The one explicit path that *can* create a DB is an `init`
subcommand used only for first-time setup.

```python
def require_db_file(path, name):
    """Fail loudly if a SQLite DB file doesn't exist where expected."""
    if not os.path.exists(path):
        sys.stderr.write(
            f"\nFATAL: {name} database not found at:\n    {path}\n\n"
            f"Refusing to auto-create (prevents silent data loss if the path is wrong).\n\n"
        )
        sys.exit(1)
```

Applied in `outbox.py` (`OutboxDB.__init__(allow_create=False)`), and in every
trip-manager, card-wallet, and orders script that connects to a DB.

### 2. Schema-as-code — full canonical schema in source

**Problem:** Tables created once via manual `sqlite3` CLI or an init script
that's since been lost can't be reproduced from code. Disaster recovery or
setup on a new machine hits a wall where scripts reference tables that don't
exist in a fresh DB.

Worse: partial drift. `outbox.py`'s embedded SCHEMA had fallen behind the live
DB — missing a `trip_id` column added later via `ALTER TABLE`, and using a
different timestamp default format. Runtime was fine because `CREATE TABLE IF
NOT EXISTS` is a no-op when the table exists. But if the file were ever lost,
a recreated DB would be subtly wrong and every `schedule()` call with
`trip_id` would fail cryptically.

**Pattern:** every database has its complete canonical schema in the primary
module, as a `SCHEMA` constant using `CREATE TABLE IF NOT EXISTS` (idempotent).
Every column, every index, every default — no hidden migrations, no ALTER-only
columns.

- `outbox/scripts/outbox.py` → `SCHEMA` covers the `messages` table
- `trip-manager/scripts/trip-db.py` → `TRIPS_SCHEMA` covers 5 tables
- `card-wallet/scripts/card-wallet-check.py` → `CARDS_SCHEMA` covers 7 tables
- `smart-reorder/scripts/order-ingest.py` → `ORDERS_SCHEMA` covers 2 tables

Verified by applying each source schema to a fresh empty DB and comparing
column-by-column against the live DB.

### 3. Cancel-by-specific-ID — never bulk DELETE

**Problem:** A single `DELETE FROM messages` without a WHERE clause wiped the
entire outbox history once. Bulk operations have unbounded blast radius and
are one typo away from permanent loss.

**Pattern:** all cleanup uses `UPDATE status='cancelled' WHERE id IN (...)`
with specific row IDs. No `DELETE`, no prefix matching, no time-based bulk.
Cancelled rows stay in the table as an audit trail. If you really need to
reclaim space, do it separately and deliberately — not as part of normal flow.

This is enforced by convention and by a CRITICAL rule in CLAUDE.md.

### 4. Cancel old outbox row before NULLing its pointer

**Problem:** When `trip-sync.py` detected that a flight/hotel/reservation had
changed, it cleared the row's `outbox_msg_id` to NULL so `trip-outbox-gen.py`
would regenerate a new outbox message. But the old outbox row the pointer
previously referenced was left pending. Every edit added one new pending
message and orphaned the old one — three edits to the same flight produced
three pending messages, all firing within minutes of each other.

**Pattern:** `trip-sync.py` has a `cancel_outbox_by_ids()` helper called on
every path that orphans an outbox pointer: data-changed update, removed-from-
manifest cleanup, hotel DELETE+INSERT, and the new reservation removal path.
Old messages are cancelled by specific ID *before* the trip-side pointer is
cleared.

### 5. LLM plans, code delivers

The foundational principle of this repo. Restated because every other pattern
here exists to enforce it:

- **LLM side:** extracting structured data from unstructured input (email,
  messages, user questions), composing message bodies, deciding what to do.
- **Code side:** polling, delivery, database writes, scheduling, state
  transitions, dedup, cleanup.

Every place we violated this — cron LLMs polling flight status, LLMs writing
messages directly to iMessage, LLMs deciding when crons were "done" — broke in
production. The outbox, the flight monitor daemon, the trip database CLI all
exist because an LLM was previously doing that job and doing it badly.

---

## ClawHub Credits

Several components were built on top of skills from the [ClawHub](https://clawhub.com) marketplace:

- **Instacart API** (CLI-driven cart-build) and **Instacart Orders** (CLI-driven history scrape) both ride on [`instacart-pp-cli`](https://github.com/mvanhorn/printing-press-library) by mvanhorn — a Go CLI that talks directly to Instacart's GraphQL endpoint. The CLI also ships `docs/extract-one.js`, a one-shot Apollo-cache reader for order history (the one operation Instacart's GraphQL doesn't expose). The history scraper wraps that CLI-bundled extractor with OpenClaw browser driving + per-order durable import — there is no non-browser path for history today, and this is the path the CLI's own docs prescribe. The previous browser-search cart skill ([instacart-skill](https://clawhub.com/skills/instacart-skill) by bigdaddyluke, forked) was deprecated 2026-05-13 and removed from this repo when Instacart API hit ~100% accuracy.
- **Smart Reorder** feeds into Instacart API (above) for cart building. We added SQL-backed purchase cadence analysis (one cadence script per data source) and LLM item classification on the Amazon side.
- **Card Wallet** merges our card-perks tracker with the [card-optimizer](https://clawhub.com/skills/card-optimizer) by scottfo. We unified the data store into SQLite (replacing the JSON file), added multi-holder support, and integrated quarterly management.
- **Meal Planner** is based on the [meal-planner](https://clawhub.com/skills/meal-planner) by clawic. We rewired it to read from recipes.sqlite instead of markdown files and feed shopping lists into the Instacart API CLI pipeline instead of generating static lists.

---

## Prerequisites

- macOS (for launchd and iMessage via BlueBubbles) — Linux adaptable with systemd + alternative messaging
- Python 3.9+
- SQLite 3
- [OpenClaw](https://github.com/openclaw/openclaw) installed and running
- [BlueBubbles](https://bluebubbles.app/) + imsg CLI for iMessage delivery (or adapt sender.py for your messaging platform)

---

## Quick Start

```bash
# Clone
git clone https://github.com/kmanan/spratt-skills.git
cd spratt-skills

# Set up environment
cp shared/env/env.example.sh shared/env/env.sh
# Edit env.sh with your API keys.
#
# CRITICAL: every launchd plist in this repo sources env.sh via a wrapper
# (/bin/zsh -c "source .../env.sh && /usr/bin/python3 ...") because launchd
# does NOT source ~/.zshrc or ~/.zshenv. Any key you export interactively
# will silently fail under cron unless it's also in env.sh — the wrapped
# CLI exits nonzero, subprocess.run swallows the stderr, and your script
# sees "no results" indistinguishable from a legitimate empty response.
# See discovery-butler/SKILL.md "Failure behavior" for the canonical writeup.

# Create the consolidated database directory
mkdir -p ~/.config/spratt/db

# 1. Start with the Outbox (everything depends on it)
cd outbox
cat schemas/outbox.sql | sqlite3 ~/.config/spratt/db/outbox.sqlite
# Edit the sender.py IMSG_BIN path for your setup
# Install the launchd plist (see outbox/README.md)

# 2. Add Trip Manager
cd ../trip-manager
cat schemas/trips.sql | sqlite3 ~/.config/spratt/db/trips.sqlite
# The LLM uses trip-db.py CLI to write trip data — no daemon needed

# 3. Add Flight Monitor
cd ../flight-monitor
# Set FLIGHTAWARE_API_KEY in env.sh
# Install launchd plist (see flight-monitor/README.md)

# 4. Add Email-to-Orders (now with shipment tracking)
cd ../email-to-orders
cat schemas/orders.sql | sqlite3 ~/.config/spratt/db/orders.sqlite
# Add the email scan cron prompt to your OpenClaw cron jobs

# 5. Add Instacart Orders (CLI-driven history scrape)
cd ../instacart-orders
# Install instacart-pp-cli from https://github.com/mvanhorn/printing-press-library
# Authenticate: `instacart-pp-cli auth login` (Chrome must be quit), or
#               `instacart-pp-cli auth paste` (paste Cookie header from DevTools).
# Drop scripts/history-scrape.py + scripts/cadence.py into your infrastructure dir.
# Install shared/launchd/com.spratt.instacart-history-scrape.plist.example (edit paths).
# First-run backfill: `python3 history-scrape.py --backfill --max-orders 250`
# Copy SKILL.md to your OpenClaw skills directory.

# 6. Add Smart Reorder (dual-source cadence + cart-aware nudge)
cd ../smart-reorder
# purchase-cadence.py + item-classify.py go in your orders infrastructure dir (Amazon side).
# Drop scripts/reorder-nudge.py into your orders infrastructure dir.
# Install shared/launchd/com.spratt.reorder-nudge.plist.example (Wed + Sat 8am).
# The nudge auto-detects cart-build status if you set up Instacart API (next step).

# 7. Add Instacart API (CLI-driven cart building)
cd ../instacart-api
# Requires instacart-pp-cli + an active session (step 5 already did this).
# Drop scripts/cart-build.py into your infrastructure dir.
# Install shared/launchd/com.spratt.instacart-cart-build.plist.example (Wed + Sat 7:45am,
# 15 min before reorder-nudge so the nudge reads its status).
# Copy SKILL.md to your OpenClaw skills directory.

# 8. Add Discovery Butler (casual coffee / quick-bite nudges)
cd ../discovery-butler
# Install wanderlust-goat-pp-cli from printing-press-library; export GOOGLE_PLACES_API_KEY.
# Edit MANAN_PHONE / HARSHITA_PHONE + HA entity ids at the top of scripts/nudge.py.
# Smoke-test: `python3 scripts/nudge.py --smoke` (compose + print, no outbox).
# Drop scripts/nudge.py into your infrastructure dir.
# Install shared/launchd/com.spratt.discovery-butler.plist.example (daily 2pm local).
# Copy SKILL.md to your OpenClaw skills directory.

# 9. Add Places
cd ../places
bash examples/setup.sh
# Copy SKILL.md to your OpenClaw skills directory

# 10. Add Destination-Aware Reminders
cd ../destination-aware
# Configure HA_URL and HA_TOKEN in ~/.config/home-assistant/config.json
# Set GOOGLE_PLACES_API_KEY for goplaces
# Install launchd plist (see shared/launchd/)

# 11. Add Delivery Watcher (on-porch package alerts)
cd ../delivery-watcher
# Requires an email-scan job that records to delivery_signals.sqlite via
# the gather-emails.hook.py (from email-to-orders, step 4) + HA contact
# sensor on the front door + Outbox.
# Find your door entity:
#   curl -H "Authorization: Bearer $HA_TOKEN" $HA_URL/api/states \
#     | jq -r '.[] | select(.attributes.device_class=="door") | .entity_id'
# Edit DOOR_ENTITY + MANAN_PHONE + HARSHITA_PHONE at the top of scripts/watch.py.
# Drop scripts/watch.py into your infrastructure dir.
# Install shared/launchd/com.spratt.delivery-watcher.plist.example (KeepAlive, 30s poll).
# First run silently backfills existing delivery signals — no spam on startup.
# Copy SKILL.md to your OpenClaw skills directory.

# 12. Add Card Wallet (benefits + purchase optimizer)
cd ../card-wallet
cat schemas/cards.sql | sqlite3 ~/.config/spratt/db/cards.sqlite
# Seed your cards, benefits, and reward rates
# Configure HOLDER_RECIPIENTS in card-wallet-check.py
# Add Saturday + monthly + quarterly cron jobs to OpenClaw

# 13. Add Meal Planner
cd ../meal-planner
# Copy SKILL.md + reference docs to your OpenClaw skills directory
# Requires recipes.sqlite (from recipe-instacart skill) and Instacart API (step 7)
# First use triggers household onboarding conversation
```

---

## How This Differs from ClawHub Skills

ClawHub skills are SKILL.md files — instructions that teach the LLM what to do. The LLM runs the skill's steps using OpenClaw's built-in tools.

These add-ons are **infrastructure that runs outside of OpenClaw**:
- Daemons that poll, deliver, and monitor (launchd/systemd)
- Databases that the LLM writes to and briefings read from
- CLIs that validate input, handle timezone math, and enforce schema
- Deterministic scripts that generate messages from templates

The LLM interacts with this infrastructure through CLIs (`outbox.py schedule`, `trip-db.py add-flight`) and SQL queries. But the infrastructure itself runs without the LLM.

Where we do use LLMs in the pipeline: Flash classifies grocery item names into canonical products (semantic matching that regex can't do), and Haiku searches the web monthly for credit card benefit changes. These are genuinely interpretive tasks — not shell commands dressed up as agentTurns.

---

## Production Status

This system has been running in production for a household of 4 (2 adults, 2 kids) since late March 2026. It has managed:
- A 5-day DC trip with 2 travelers, 4 flights, 5 restaurants, daily briefings, and real-time flight tracking
- A solo corporate retreat trip with automated flight tracking and hotel/event notifications
- Daily morning briefings and evening digests for 2 adults
- Email scanning across 4 accounts (2 Gmail, 2 Outlook)
- Smart home control via Home Assistant
- Grocery order tracking with purchase cadence analysis across 5+ weekly Instacart orders + a one-time backfill of 139 historical orders (1,111 items, 14 retailers) into the printing-press CLI's local SQLite — the cadence engine now joins on canonical Instacart `item_id` rather than fuzzy-matched receipt names
- Destination-aware reminders via Tesla navigation + Google Places
- Credit card benefit tracking across 6 cards, 14 benefits, and 24 reward rate categories

All 7 databases are consolidated into a single `db/` directory — no scattered paths, no self-relative resolution. The system handles ~20-30 messages/day through the outbox, costs ~$0.10-0.20/day in API calls, and has had zero missed message deliveries since the outbox pattern was implemented.

---

## Blog Post

Full writeup on the design, failures, and lessons learned: [beingmanan.com](https://beingmanan.com)

## License

MIT
