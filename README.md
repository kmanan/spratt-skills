![image (2) (Small)](https://github.com/user-attachments/assets/1c4afea6-c572-48ff-9bbd-d1a3768f3fe1)

# Spratt Skills — Household Automation Add-ons for OpenClaw

A collection of infrastructure add-ons for [OpenClaw](https://github.com/openclaw/openclaw) that turn it into a household operating system. Built for a real family, running in production on a Mac Mini M4.

These aren't typical OpenClaw skills (SKILL.md files that teach the LLM what to do). They're **infrastructure** — daemons, databases, pipelines, and automation that run alongside OpenClaw, handling the things an LLM shouldn't be trusted to do reliably.

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

**Why it exists:** Family travel has dozens of moving parts — flights, hotels, restaurants, Uber links, group chat notifications — spread across confirmation emails and text threads. Without structure, details get lost and nobody gets reminded. The trip manager gives the LLM a single database to write to, and deterministic scripts handle all the downstream notification scheduling and flight tracking setup.

| | |
|---|---|
| **What you get** | trip-db.py (CLI with 11 subcommands), trip-outbox-gen.py, trip-status.py, trip-flight-state.py, SQLite schema (5 tables) |
| **Dependencies** | Python 3, SQLite, Outbox (above) |
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

### 5. [Instacart Orders](./instacart-orders/) — Browser-Based Order Scraping

Instacart confirmation emails don't contain itemized order details — they just link to the Instacart site. This skill uses OpenClaw's browser automation to navigate to Instacart's receipt page and extract full item lists with names, quantities, and prices. Now also captures **store names** to power per-store purchase cadence analysis.

**Why it exists:** The email scanner ingests Instacart orders with empty items because the email body doesn't have them. This skill closes the loop by scraping the data from the source.

| | |
|---|---|
| **What you get** | SKILL.md (browser scraping instructions), cron job template |
| **Dependencies** | OpenClaw browser tool, Email-to-Orders (above), an active Instacart account logged in via the browser's `openclaw` profile |
| **Schedule** | Cron, daily at 9pm. Finds orders with empty items, backfills them, and classifies new item names via Flash. |
| **macOS-specific** | No |
| **Setup time** | ~5 minutes (after Email-to-Orders is set up) |

### 6. [Smart Reorder](./smart-reorder/) — Purchase Cadence Analysis + LLM Item Classification

Analyzes your grocery purchase history to predict when you'll need to reorder each item. Calculates median days between purchases per product, flags items that are due or coming up soon. An LLM (Flash) classifies receipt item names into canonical products — so "QFC Vitamin D Whole Milk Half Gallon" and "QFC Vitamin D Whole Milk" are recognized as the same thing, while "Organic Valley Whole Milk" stays separate (different brand).

Feeds into the [Instacart Skill](./instacart-skill/) (below) for cart building.

**Why it exists:** "We're out of milk again" shouldn't require remembering. The system knows you buy milk every 7 days and your last order was 8 days ago.

| | |
|---|---|
| **What you get** | purchase-cadence.py (cadence analysis CLI), item-classify.py (alias management CLI), SQLite schema (item_aliases table) |
| **Dependencies** | Python 3, SQLite, Email-to-Orders + Instacart Orders (above) for data. Flash (via nightly cron) for item classification. |
| **Schedule** | Item classification runs as part of the nightly Instacart scraper cron. Cadence analysis is on-demand (called by the Instacart ordering skill or interactively). |
| **macOS-specific** | No |
| **Setup time** | ~5 minutes (after Instacart Orders is set up) |

### 7. [Instacart Skill](./instacart-skill/) — Browser-Based Grocery Cart Building

Drives a browser to build grocery carts on Instacart. The LLM searches products, adds items, handles quantity adjustments, and presents a cart summary — but never places the order. Originally from [instacart-skill](https://clawhub.com/skills/instacart-skill) on ClawHub (by bigdaddyluke), heavily customized with URL-based search (Instacart's search input doesn't work with Playwright), snapshot-first browser interaction rules, smart lookback integration via purchase cadence analysis, and automated browser crash recovery.

**Why it exists:** Typing grocery lists into Instacart is tedious. "We need groceries" should result in a pre-built cart based on what you usually buy and what's due for reorder. The skill bridges the gap between the smart reorder analysis and the actual Instacart cart.

| | |
|---|---|
| **What you get** | SKILL.md (browser automation instructions with URL-based search, login flow, cart building, smart replenishment mode) |
| **Dependencies** | OpenClaw browser tool, an active Instacart account, Smart Reorder (above) for purchase cadence data, `gog` CLI for login verification codes |
| **Schedule** | N/A — interactive skill, invoked on demand or by smart replenishment automation. |
| **macOS-specific** | No |
| **Setup time** | ~5 minutes (after Smart Reorder is set up). Log into Instacart once in the `openclaw` browser profile. |

### 8. [Outlook Graph](./outlook-graph/) — Outlook Email & Calendar via Microsoft Graph

Shell scripts for managing Outlook/Hotmail email and calendar through Microsoft Graph API. Multi-account OAuth2 with auto-refreshing tokens. Calendar events support descriptions, attendees, and multi-calendar targeting — create a family appointment on the "For Family" calendar with attendees and notes in one command.

**Why it exists:** The ClawHub outlook-plus skill only had basic event CRUD — no description/body field, no attendees, no multi-calendar targeting. For a household assistant that needs to create shared calendar events with notes ("doctor appointment — symptoms to discuss: ...") and invite family members, those are table-stakes features.

| | |
|---|---|
| **What you get** | outlook-calendar.sh, outlook-mail.sh, outlook-setup.sh, outlook-token.sh |
| **Dependencies** | bash, curl, jq. Azure CLI for initial setup only. |
| **Schedule** | N/A — shell scripts invoked on demand by the LLM or by email scanning cron. |
| **macOS-specific** | No |
| **Setup time** | ~10 minutes |

### 9. [Places](./places/) — Save & Search Restaurants, Activities, Attractions

A SQLite database for places you want to remember — restaurants, bars, activities, attractions. Share an Instagram post, Google Maps link, Yelp page, or just say "remember that Thai place on Queen West" and it gets saved with category, cuisine, location, tags, and notes. Query by vibe ("date night spots"), location, cuisine, or who saved it. Track visits and ratings.

**Why it exists:** Interesting places come from Instagram stories, friend recommendations, and articles — then get forgotten. This gives the LLM a structured place to save them and a way to surface them when you ask "where should we go for dinner?"

| | |
|---|---|
| **What you get** | SKILL.md (OpenClaw skill definition), SQLite schema, setup script |
| **Dependencies** | SQLite, OpenClaw browser tool (for Instagram/Facebook/TikTok URL extraction) |
| **Schedule** | N/A — interactive skill, invoked on demand when user shares a place. |
| **macOS-specific** | No |
| **Setup time** | ~5 minutes |

### 10. [Destination-Aware Reminders](./destination-aware/) — Tesla Nav → Context Surfacing

When you set a destination in your Tesla, this daemon detects it via Home Assistant's SSE stream and surfaces relevant context before you arrive — shopping lists for grocery stores, appointment notes for doctors, pickup reminders for daycare. No zones, no polling, no HA automations. The Tesla tells HA where you're going, the daemon identifies what's there via Google Places, and sends a text with what you need to know.

**Why it exists:** "Bring diapers to daycare" sitting in a reminder list doesn't help if you only see it at 7am and forget by 5pm pickup. The reminder should surface when you're actually heading there.

| | |
|---|---|
| **What you get** | destination-daemon.py (persistent SSE listener), destination-context.py (place resolver + context gatherer), SKILL.md |
| **Dependencies** | Python 3, Home Assistant with Tesla integration, [goplaces](https://github.com/openclaw/goplaces) CLI (Google Places API), Outbox (above) |
| **Schedule** | Persistent daemon. Reacts instantly when Tesla nav destination is set. Zero polling. |
| **macOS-specific** | launchd plist (KeepAlive). Adaptable to systemd. |
| **Setup time** | ~10 minutes (after Outbox is set up). See [destination-aware/README.md](./destination-aware/README.md) for deployment gotchas. |

### 11. [Card Wallet](./card-wallet/) — Credit Card Benefits + Purchase Optimization

Merged skill that tracks both **"use it or lose it" credit card benefits** (monthly credits, quarterly categories, semi-annual windows) and **per-purchase reward optimization** ("which card for groceries?"). A weekly cron checks expiring benefits and notifies each cardholder. A monthly LLM-powered refresh searches the web for benefit and reward rate changes. Interactive queries recommend the optimal card per spending category with cap awareness and network acceptance warnings (Amex fallbacks).

**Why it exists:** AMEX Platinum alone has 7+ expiring credits across monthly, semi-annual, and annual cycles. Nobody remembers them all. And nobody does the math on "Chase Freedom 5% on rotating categories this quarter vs Apple Pay 2% vs Sapphire Reserve 3x dining" in their head. Spratt does both. Evolved from the standalone card-perks tracker by merging with the [card-optimizer](https://clawhub.com/skills/card-optimizer) skill from ClawHub (by scottfo).

| | |
|---|---|
| **What you get** | card-wallet-check.py (weekly cron), card-wallet-refresh.py (monthly helper), SKILL.md (interactive benefit + purchase queries), SQLite schema (7 tables: cards, benefits, usage, benefit_changes, reward_rates, quarterly_categories, spending_estimates) |
| **Dependencies** | Python 3, SQLite, Outbox (above), Apple Reminders via remindctl, Claude Haiku API (for monthly refresh, ~$0.03/mo) |
| **Schedule** | Weekly cron (Saturdays) for expiration checks. Monthly cron for benefit + reward rate refresh. Quarterly cron (Jan/Apr/Jul/Oct) for rotating category lookups. |
| **macOS-specific** | Apple Reminders via remindctl (optional — remove reminder creation for Linux) |
| **Setup time** | ~10 minutes (after Outbox is set up) |

### 12. [Meal Planner](./meal-planner/) — Weekly Meal Planning with Instacart Integration

Weekly meal planning that reads from your recipe database, checks pantry inventory, and generates shopping lists that feed directly into the Instacart ordering pipeline. Handles dietary restrictions, household coordination (adults vs kids), batch cooking, and budget tracking. Based on the [meal-planner](https://clawhub.com/skills/meal-planner) skill from ClawHub (by clawic), adapted to use SQLite-backed recipes and the Instacart skill instead of standalone markdown files.

**Why it exists:** Meal planning involves recipes you've saved, groceries you need to buy, and what's already in the pantry. Without integration, you're copying ingredient lists from one app to another. This connects the recipe database to the grocery pipeline so "plan this week's meals" ends with "cart built on Instacart, ready to place."

| | |
|---|---|
| **What you get** | SKILL.md (planning instructions integrated with recipes.sqlite + Instacart pipeline), setup.md, shopping-guide.md, meal-prep.md, budget-tips.md, memory-template.md |
| **Dependencies** | recipes.sqlite (from recipe-instacart skill), orders.sqlite (for purchase history), Instacart ordering skill (for cart building) |
| **Schedule** | N/A — interactive skill, invoked on demand when user wants to plan meals. |
| **macOS-specific** | No |
| **Setup time** | ~5 minutes + first-use household onboarding conversation |

### 13. [Apple Reminders — Recurring](./apple-reminders/) — Recurring Reminder Support

A compiled Swift binary that creates proper recurring Apple Reminders via EventKit. The `remindctl` CLI doesn't support recurrence, so without this, the LLM creates individual copies for each occurrence — fragile and cluttered.

**Why it exists:** "Remind me every Monday to bring diapers to daycare" should create one recurring reminder, not 12 individual ones.

| | |
|---|---|
| **What you get** | create-recurring-reminder (Swift binary + source) |
| **Dependencies** | macOS, Swift compiler (Xcode Command Line Tools) |
| **Schedule** | N/A — invoked on demand when user requests a recurring reminder. |
| **macOS-specific** | Yes (EventKit is Apple-only) |
| **Setup time** | ~2 minutes (compile + grant permissions) |

---

## Architecture

```
Human → LLM → trip-db.py CLI (add-trip, add-flight, add-hotel, etc.)
                    ↓
              trips.sqlite (trips, flights, hotels, reservations, travelers)
                    ↓
              trip-outbox-gen.py (deterministic templates)
                    ↓
              outbox.sqlite (scheduled messages)
                    ↓
              sender.py daemon (60s polling) → imsg CLI → iMessage

              trip-flight-state.py → state.json
                    ↓
              flight_monitor.py daemon (3 min polling) → FlightAware AeroAPI
                    ↓ (on events)
              outbox.sqlite → sender.py → iMessage

Human → LLM → places.sqlite (save place from URL or description)
              LLM ← places.sqlite (query: "date night spots we haven't tried")

Email → email scan cron (Flash triage → extract)
                    ↓
              order-ingest.py → orders.sqlite (metadata, items, tracking)
              trip-db.py → trips.sqlite
              outlook-calendar.sh → Outlook calendar (with attendees + notes)

              Instacart scraper cron (daily, browser automation)
                    ↓
              instacart.com/orders → receipt page → parse items
                    ↓
              order-ingest.py update-items → orders.sqlite (fills in items + store)
                    ↓
              item-classify.py → Flash classifies product names → item_aliases table

Human → "we need groceries"
                    ↓
              purchase-cadence.py → median days between purchases per item
                    ↓
              due items → Instacart skill (browser) → cart built → user places order

Human → "plan meals this week"
                    ↓
              meal-planner → reads recipes.sqlite + pantry inventory
                    ↓
              weekly plan + ingredient list
                    ↓
              Instacart skill (browser) → cart built → user places order

Tesla nav destination set → sensor.maha_tesla_destination changes
                    ↓
              destination-daemon.py (SSE listener, instant)
                    ↓
              goplaces resolve → identify place (grocery? daycare? doctor?)
                    ↓
              remindctl + icalBuddy → relevant context
                    ↓
              outbox.sqlite → sender.py → "🛒 Heading to QFC — cilantro, milk, paper towels"

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

## ClawHub Credits

Several components were built on top of skills from the [ClawHub](https://clawhub.com) marketplace:

- **Instacart Skill** is forked from [instacart-skill](https://clawhub.com/skills/instacart-skill) by bigdaddyluke. We replaced search-box typing with URL-based search (Instacart's search input doesn't work with Playwright), added snapshot-first browser interaction rules, integrated smart lookback via purchase cadence analysis, and added browser crash self-recovery. Auto-checkout is disabled.
- **Smart Reorder** feeds into the Instacart Skill (above) for cart building. We added SQL-backed purchase cadence analysis and LLM item classification.
- **Card Wallet** merges our card-perks tracker with the [card-optimizer](https://clawhub.com/skills/card-optimizer) by scottfo. We unified the data store into SQLite (replacing the JSON file), added multi-holder support, and integrated quarterly management.
- **Meal Planner** is based on the [meal-planner](https://clawhub.com/skills/meal-planner) by clawic. We rewired it to read from recipes.sqlite instead of markdown files and feed shopping lists into the Instacart pipeline instead of generating static lists.

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
# Edit env.sh with your API keys

# 1. Start with the Outbox (everything depends on it)
cd outbox
cat schemas/outbox.sql | sqlite3 outbox.sqlite
# Edit the sender.py IMSG_BIN path for your setup
# Install the launchd plist (see outbox/README.md)

# 2. Add Trip Manager
cd ../trip-manager
cat schemas/trips.sql | sqlite3 trips.sqlite
# The LLM uses trip-db.py CLI to write trip data — no daemon needed

# 3. Add Flight Monitor
cd ../flight-monitor
# Set FLIGHTAWARE_API_KEY in env.sh
# Install launchd plist (see flight-monitor/README.md)

# 4. Add Email-to-Orders (now with shipment tracking)
cd ../email-to-orders
cat schemas/orders.sql | sqlite3 orders.sqlite
# Add the email scan cron prompt to your OpenClaw cron jobs

# 5. Add Instacart Orders (fills in items the email scanner can't get)
cd ../instacart-orders
# Copy SKILL.md to your OpenClaw skills directory
# Add the daily scraper cron job (see SKILL.md for cron template)
# Make sure the openclaw browser profile is logged into Instacart

# 6. Add Smart Reorder (purchase cadence + item classification)
cd ../smart-reorder
# purchase-cadence.py and item-classify.py go in your orders infrastructure dir
# The nightly scraper cron handles item classification automatically
# Run a one-time backfill of Instacart order history for initial data

# 7. Add Instacart Skill (browser-based cart building)
cd ../instacart-skill
# Copy SKILL.md to your OpenClaw skills directory
# Log into Instacart once in the openclaw browser profile
# Create memory/instacart-storefronts.json with your store slug mappings
# Set INSTACART_URL, INSTACART_EMAIL in your agent's env file

# 8. Add Places
cd ../places
bash examples/setup.sh
# Copy SKILL.md to your OpenClaw skills directory

# 9. Add Destination-Aware Reminders
cd ../destination-aware
# Configure HA_URL and HA_TOKEN in ~/.config/home-assistant/config.json
# Set GOOGLE_PLACES_API_KEY for goplaces
# Install launchd plist (see shared/launchd/)

# 10. Add Card Wallet (benefits + purchase optimizer)
cd ../card-wallet
cat schemas/cards.sql | sqlite3 cards.sqlite
# Seed your cards, benefits, and reward rates
# Configure HOLDER_RECIPIENTS in card-wallet-check.py
# Add Saturday + monthly + quarterly cron jobs to OpenClaw

# 11. Add Meal Planner
cd ../meal-planner
# Copy SKILL.md + reference docs to your OpenClaw skills directory
# Requires recipes.sqlite (from recipe-instacart skill) and Instacart skill
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
- Daily morning briefings and evening digests for 2 adults
- Email scanning across 4 accounts (2 Gmail, 2 Outlook)
- Smart home control via Home Assistant
- Grocery order tracking with purchase cadence analysis across 5+ weekly Instacart orders
- Destination-aware reminders via Tesla navigation + Google Places
- Credit card benefit tracking across 6 cards, 14 benefits, and 24 reward rate categories

The system handles ~20-30 messages/day through the outbox, costs ~$0.10-0.20/day in API calls, and has had zero missed message deliveries since the outbox pattern was implemented.

---

## Blog Post

Full writeup on the design, failures, and lessons learned: [beingmanan.com](https://beingmanan.com)

## License

MIT
