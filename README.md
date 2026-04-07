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
| **macOS-specific** | launchd plist for the daemon (KeepAlive). Adaptable to systemd. |
| **Setup time** | ~15 minutes |

### 2. [Trip Manager](./trip-manager/) — Database-First Trip Automation

The LLM writes trip data directly to SQLite through a CLI (`trip-db.py add-flight`, `add-hotel`, etc.). Outbox messages are auto-generated for flight reminders, hotel check-ins, and dinner notifications. Flight monitor state is auto-derived. Update any record, regenerate downstream — only the changed items, no full regeneration.

**Why it exists:** Family travel has dozens of moving parts — flights, hotels, restaurants, Uber links, group chat notifications — spread across confirmation emails and text threads. Without structure, details get lost and nobody gets reminded. The trip manager gives the LLM a single database to write to, and deterministic scripts handle all the downstream notification scheduling and flight tracking setup.

| | |
|---|---|
| **What you get** | trip-db.py (CLI with 11 subcommands), trip-outbox-gen.py, trip-status.py, trip-flight-state.py, SQLite schema (5 tables) |
| **Dependencies** | Python 3, SQLite, Outbox (above) |
| **macOS-specific** | No (all scripts are standalone CLI tools) |
| **Setup time** | ~15 minutes (after Outbox is set up) |

### 3. [Flight Monitor](./flight-monitor/) — Real-Time Flight Tracking Daemon

A persistent daemon that polls FlightAware AeroAPI, detects events (landing, delay, gate change, diversion), and sends notifications through the outbox. Adaptive polling — 3 minutes during active window, 30 minutes when idle. No LLM in the polling loop.

**Why it exists:** An LLM-mediated flight cron used browser scraping, couldn't interpret "not found," and self-deleted after "completing" with zero notifications sent. The original implementation used `FlightRadarAPI` — a community scraper of FlightRadar24's public data, not an official API — which returned inconsistent results for valid flights. Migrated to FlightAware AeroAPI for stable, authenticated access.

| | |
|---|---|
| **What you get** | flight_monitor.py, track_flight.py, state derivation from trips DB |
| **Dependencies** | Python 3, FlightAware AeroAPI key (~$5/mo), Outbox (above) |
| **macOS-specific** | launchd plist (KeepAlive + PathState). Adaptable to systemd. |
| **Setup time** | ~15 minutes (after Outbox is set up) |

### 4. [Email-to-Orders](./email-to-orders/) — Automatic Order Tracking from Email

An email scanning cron extracts grocery/shopping order details from email confirmations and inserts them into SQLite. Supports Instacart, Amazon Fresh, Whole Foods, DoorDash, and more. No manual entry — orders are ingested automatically from email.

**Why it exists:** "Did we already buy cilantro this week?" is a real question in a household. The orders database makes it queryable.

| | |
|---|---|
| **What you get** | order-ingest.py (CLI), SQLite schema, email scan cron prompt template |
| **Dependencies** | Python 3, SQLite, an email scanning setup (Gmail via gog CLI, Outlook via Microsoft Graph, or any IMAP) |
| **macOS-specific** | No |
| **Setup time** | ~10 minutes |

### 5. [Outlook Graph](./outlook-graph/) — Outlook Email & Calendar via Microsoft Graph

Shell scripts for managing Outlook/Hotmail email and calendar through Microsoft Graph API. Multi-account OAuth2 with auto-refreshing tokens. Calendar events support descriptions, attendees, and multi-calendar targeting — create a family appointment on the "For Family" calendar with attendees and notes in one command.

**Why it exists:** The ClawHub outlook-plus skill only had basic event CRUD — no description/body field, no attendees, no multi-calendar targeting. For a household assistant that needs to create shared calendar events with notes ("doctor appointment — symptoms to discuss: ...") and invite family members, those are table-stakes features.

| | |
|---|---|
| **What you get** | outlook-calendar.sh, outlook-mail.sh, outlook-setup.sh, outlook-token.sh |
| **Dependencies** | bash, curl, jq. Azure CLI for initial setup only. |
| **macOS-specific** | No |
| **Setup time** | ~10 minutes |

### 6. [Places](./places/) — Save & Search Restaurants, Activities, Attractions

A SQLite database for places you want to remember — restaurants, bars, activities, attractions. Share an Instagram post, Google Maps link, Yelp page, or just say "remember that Thai place on Queen West" and it gets saved with category, cuisine, location, tags, and notes. Query by vibe ("date night spots"), location, cuisine, or who saved it. Track visits and ratings.

**Why it exists:** Interesting places come from Instagram stories, friend recommendations, and articles — then get forgotten. This gives the LLM a structured place to save them and a way to surface them when you ask "where should we go for dinner?"

| | |
|---|---|
| **What you get** | SKILL.md (OpenClaw skill definition), SQLite schema, setup script |
| **Dependencies** | SQLite, OpenClaw browser tool (for Instagram/Facebook/TikTok URL extraction) |
| **macOS-specific** | No |
| **Setup time** | ~5 minutes |

### 7. [Card Perks](./card-perks/) — Credit Card Benefits Tracker

Tracks "use it or lose it" credit card benefits — monthly credits, quarterly categories, semi-annual windows. A weekly cron checks what's expiring soon and notifies each cardholder via outbox + Apple Reminders. A monthly LLM-powered refresh searches the web for benefit changes so the database stays current without manual maintenance.

**Why it exists:** AMEX Platinum alone has 7+ expiring credits across monthly, semi-annual, and annual cycles. Nobody remembers them all. Spratt does.

| | |
|---|---|
| **What you get** | card-perks-check.py (weekly cron), card-perks-refresh.py (monthly helper), SQLite schema (4 tables), SKILL.md for interactive acknowledgment |
| **Dependencies** | Python 3, SQLite, Outbox (above), Apple Reminders via remindctl, Claude Haiku API (for monthly refresh, ~$0.03/mo) |
| **macOS-specific** | Apple Reminders via remindctl (optional — remove reminder creation for Linux) |
| **Setup time** | ~10 minutes (after Outbox is set up) |

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
              order-ingest.py → orders.sqlite
              trip-db.py → trips.sqlite
              outlook-calendar.sh → Outlook calendar (with attendees + notes)

Saturday cron → card-perks-check.py (deterministic)
                    ↓
              cards.sqlite (benefits, usage tracking)
                    ↓
              outbox.sqlite + Apple Reminders (if expiring within 10 days)

Monthly cron → card-perks-refresh.py dump → Haiku (web search + diff)
                    ↓
              cards.sqlite (benefit updates) → outbox notification if changed
```

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

# 4. Add Email-to-Orders
cd ../email-to-orders
cat schemas/orders.sql | sqlite3 orders.sqlite
# Add the cron prompt to your OpenClaw cron jobs

# 5. Add Places
cd ../places
bash examples/setup.sh
# Copy SKILL.md to your OpenClaw skills directory

# 6. Add Card Perks Tracker
cd ../card-perks
cat schemas/cards.sql | sqlite3 cards.sqlite
# Seed your cards and benefits (see card-perks/schemas/cards.sql for schema)
# Configure HOLDER_RECIPIENTS in card-perks-check.py or set env vars
# Add Saturday + monthly cron jobs to OpenClaw
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

---

## Production Status

This system has been running in production for a household of 4 (2 adults, 2 kids) since late March 2026. It has managed:
- A 5-day DC trip with 2 travelers, 4 flights, 5 restaurants, daily briefings, and real-time flight tracking
- Daily morning briefings and evening digests for 2 adults
- Email scanning across 4 accounts (2 Gmail, 2 Outlook)
- Smart home control via Home Assistant
- Grocery order tracking from Instacart and Amazon

The system handles ~20-30 messages/day through the outbox, costs ~$0.10-0.20/day in API calls, and has had zero missed message deliveries since the outbox pattern was implemented.

---

## Blog Post

Full writeup on the design, failures, and lessons learned: [beingmanan.com](https://beingmanan.com)

## License

MIT
