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

### 2. [Trip Manager](./trip-manager/) — Manifest-Driven Trip Automation

Write a trip in natural markdown. An LLM extracts structured data into SQLite (trips, flights, hotels, reservations, travelers). Outbox messages are auto-generated for flight reminders, hotel check-ins, and dinner notifications. Flight monitor state is auto-derived. Change the manifest, everything downstream updates — only the changed items, no full regeneration.

**Why it exists:** Trip details were hardcoded in cron prompts, state.json, and docs — three disconnected places. When a flight changed, nothing cascaded. The manifest pattern makes one file the source of truth with automatic downstream propagation.

| | |
|---|---|
| **What you get** | trip-sync.py (LLM extraction), trip-db.py (CLI), trip-outbox-gen.py, trip-status.py, trip-flight-state.py, SQLite schema (5 tables), message templates |
| **Dependencies** | Python 3, SQLite, Claude Haiku API (for extraction, ~$0.01/call), Outbox (above) |
| **macOS-specific** | launchd WatchPaths for auto-triggering on file change. Adaptable to inotifywait/fswatch on Linux. |
| **Setup time** | ~30 minutes (after Outbox is set up) |

### 3. [Flight Monitor](./flight-monitor/) — Real-Time Flight Tracking Daemon

A persistent daemon that polls FlightRadar24, detects events (landing, delay, gate change, diversion), and sends notifications through the outbox. Adaptive polling — 3 minutes during active window, 30 minutes when idle. No LLM in the polling loop.

**Why it exists:** An LLM-mediated flight cron used browser scraping, couldn't interpret "not found," and self-deleted after "completing" with zero notifications sent. The daemon is deterministic, restartable, and survives reboots.

| | |
|---|---|
| **What you get** | flight_monitor.py, track_flight.py, state derivation from trips DB |
| **Dependencies** | Python 3, FlightRadarAPI (`pip install FlightRadarAPI`), Outbox (above) |
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

### 5. [Card Perks](./card-perks/) — Credit Card Benefits Tracker

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
Human → LLM → Trip Manifest (markdown)
                    ↓
              trip-sync.py (Haiku extracts, ~$0.01)
                    ↓
              trips.sqlite (trips, flights, hotels, reservations, travelers)
                    ↓
              trip-outbox-gen.py (deterministic templates)
                    ↓
              outbox.sqlite (scheduled messages)
                    ↓
              sender.py daemon (60s polling) → imsg CLI → iMessage

              flight_monitor.py daemon (3 min polling) → FlightRadar24
                    ↓ (on events)
              outbox.sqlite → sender.py → iMessage

Email → email scan cron (Flash triage → extract)
                    ↓
              order-ingest.py → orders.sqlite
              trip-db.py → trips.sqlite

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
- Claude API key (for trip manifest extraction — Haiku tier, ~$0.01/call)

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
# Install WatchPaths plist (see trip-manager/README.md)

# 3. Add Flight Monitor
cd ../flight-monitor
pip3 install FlightRadarAPI
# Install launchd plist (see flight-monitor/README.md)

# 4. Add Email-to-Orders
cd ../email-to-orders
cat schemas/orders.sql | sqlite3 orders.sqlite
# Add the cron prompt to your OpenClaw cron jobs

# 5. Add Card Perks Tracker
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
- File watchers that trigger extraction pipelines
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
