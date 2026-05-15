---
name: tool-routing
description: Maps user intents to the correct tool or skill (smart home, iMessage send vs schedule, calendar writes, reminders, destination-tied reminders, browser, grocery, recipes, meal-planning, saved places, addresses, restaurant reservations, card choice, flight search, trip writes and watchers, email handling, message history, forwarded-email semantics, creating-todos-for-others etiquette, cron-vs-outbox boundaries, exec-vs-agentTurn for crons). Read when you need to pick a tool for a task and the mapping isn't obvious from the skill name, or when deciding between `message` (live) vs `outbox` (scheduled), or when asked "what are our plans?".
---

# Tool Routing

Intent -> tool/skill. Read this before composing a response that involves doing something, not just talking.

## Messaging

- **Send iMessage (live conversation reply)** -> `message` tool with `channel: "bluebubbles"`, `action: "send"`. For group chats use `target: "chat_guid:GUID"` from CONTACTS.md. For DMs use E.164 phone number. **Never use `outbox.py schedule --at now` for live replies** -- it adds up to 60s of latency (sender poll interval) for zero benefit.
- **Schedule iMessage (pre-composed, future or background)** -> **outbox** skill. Messages where the content is known at scheduling time go through the SQLite outbox. No OpenClaw crons for message delivery. No direct imsg calls. See outbox SKILL.md.
- **Dynamic iMessage (content assembled at delivery time)** -> `message` tool directly. Morning briefings, digests, inspection reports -- content depends on live data, so LLM assembles and sends in the same turn.
- **Briefing/digest on demand** ("give me my briefing", "what's my evening digest") -> **briefing** skill. Cron-fired briefings are separate.
- **Message history** -> `imsg history --chat-id N --limit 10 --json` (see TOOLS.md for chat IDs).
- **"Remind me" / "text someone at a time"** -> compose the message, then `outbox.py schedule --to {recipient} --body {text} --at {time} --source "reminder:{desc}"`. For personal reminders (shows on iPhone): `remindctl add "title" --due "YYYY-MM-DD HH:MM"`.

## Productivity

- **Smart home** -> home-assistant skill.
- **Reminder for self** -> apple-reminders skill.
- **Reminder tied to going somewhere** ("next time I'm at QFC", "when I drop the kid at daycare") -> apple-reminders skill with `#<category>` tag in the title. See **destination-aware** skill for the category enum and how the Tesla-nav daemon picks reminders up. Untagged reminders still work via LLM fallback, but tagging is the deterministic path. Cadence tiers determine firing frequency -- see destination-aware SKILL.md.
- **Note** -> apple-notes skill.
- **Weather** -> weather skill.
- **Grocery/shopping (general)** -> browser tool -> instacart.com. Household auto-reorder runs on its own schedule (Wed/Sat) via the reorder-nudge pipeline.
- **Order history / past purchases** ("what did we order last week", "when did we last buy X") -> **instacart-orders** skill (queries the printing-press CLI's local `instacart.db`).
- **Plan meals / weekly menu / shopping list** -> **meal-planner** skill (feeds into Instacart).
- **Save a recipe / cook from a saved recipe** -> **recipe-instacart** skill (handles URL extraction, recipes.sqlite, and Instacart cart staging).
- **Save a place / recall a saved place** ("save this restaurant", "where was that bar in Capitol Hill") -> **places** skill.
- **Address / venue resolution on the fly** (need an Uber link right now) -> goplaces skill, always include Uber deep links (see TOOLS.md).
- **Restaurant reservation** -> **table-reservation** skill (one CLI for OpenTable/Tock/Resy). The owner confirms the booking; Spratt never auto-books.
- **Credit card choice / track card credits and benefits** -> **card-wallet** skill.
- **Calendar (WRITING)** -> see TOOLS.md for per-account routing.
- **Cost monitoring** -> model-usage skill (codexbar) or cost-monitor.sh.

## Web and content

- **Any URL** -> browser tool immediately (logged-in Chrome session).

## Trips

- **Trip read questions** (who's traveling, what flights, hotel, dates, status) -> query `~/.config/spratt/db/trips.sqlite` tables: `trips`, `flights`, `hotels`, `travelers`. Only read the manifest for itinerary details (daily plans, restaurant specifics).
- **Trip writes** (create trip, add traveler, add flight, add hotel, update flight number, set monitoring) -> **trip-manager** skill via `trip-db.py`. Never write raw SQL to trips.sqlite. trip-sync is retired; trip-db.py is the only entry point.
- **Watch a flight without traveling on it** ("track this flight and text me", "CC me on flight updates", "add X as a watcher on trip Y", "alert me when their flight lands") -> trip-manager skill `add-watcher --trip <id> --name <person>`. Watchers receive flight-event fanout but never become the primary recipient. See trip-manager `WATCHERS.md`.
- **Search/compare flight prices** (planning, not tracking a booked flight) -> **flight-search** skill.

## Email (attachments)

- **Email attachment / PDF** (eTicket, booking confirmation, invoice, forwarded email with a PDF) -> **email-pdf-attachment** skill.

## "What are our plans?"

Check EVERYTHING. Never skip a source, never say "token expired."

1. icalBuddy (calendar) -- no tokens needed, sees everything.
2. remindctl (reminders for those dates).
3. trips.sqlite (`SELECT * FROM trips WHERE status IN ('upcoming','active')`).
4. memory/commitments.md (open commitments).
5. outbox.sqlite (pending scheduled messages for the date range).

See TOOLS.md for the exact icalBuddy invocation and remindctl list names.

## Semantics and etiquette

- **Forwarded email from the owner** -> they're telling you to handle it, not asking you to tell them about it. Read it, act on it.
- **Creating a todo for someone** -> always text that person to let them know.
- **Travel events, reservations** -> if travelers are in CONTACTS.md, create or use an existing group chat for the trip and send all reminders, Uber links, and updates THERE -- not as individual DMs. One message in the group, not duplicate DMs to each person.

## Multi-step interactive work -> TaskFlow

Any interactive workflow that (a) spans multiple turns, (b) waits on user input between steps, or (c) could be interrupted by a session timeout or browser crash should use TaskFlow for durable state tracking.

- At workflow start: `createManaged(controllerId, goal, currentStep, stateJson)` -- stateJson carries only IDs and phase, not data (the database has the real data)
- When waiting for user input: `setWaiting(waitJson: {kind, question})` -- records what you're blocked on
- When user responds: `resume(currentStep, stateJson)` -- clears the wait, updates phase
- When done: `finish(stateJson)` -- marks flow complete
- At session start: call `list()` to check for stale non-terminal flows from prior sessions -- either resume them or `fail()` them before creating new ones

**Applies to:** trip planning, Instacart cart building, table-reservation booking flows, recipe-to-cart, meal planning, any multi-step browser workflow. **Does NOT apply to:** single-turn lookups, outbox scheduling, simple reads/writes, cron pipelines that complete in one tick.

Carry the latest `revision` forward after every mutation -- stale revision = conflict error.

## Crons vs outbox (hard boundary)

- **DO NOT create OpenClaw crons for message delivery.** Use the outbox for all scheduled/timed messages.
- Crons are ONLY for: system maintenance (backups, sync, inspections), monitoring tasks that need LLM reasoning (delivery tracking, email triage, watcher fanout), and recurring LLM tasks (digests, briefings). Timed message delivery uses the outbox, not crons. Personal reminders use Apple Reminders. Flight tracking uses the deterministic flight monitor daemon, not crons.
- **Cron payload: `exec` for shell/Python scripts, `agentTurn` only for LLM work.** If a cron just runs a deterministic script (backup, sync, drift audit, health check that prints text) use `exec` -- `agentTurn` spins up an LLM session for nothing. Exception: `sessionTarget: "isolated"` requires `agentTurn`; if the job must be isolated and runs a script, wrap it in a minimal agentTurn prompt.
- **Track a delivery / poll-for-condition** -> **cron-monitor** skill (silent polling cron that writes to outbox on state change). For flight tracking, use the flight monitor daemon (see trip-manager), not cron-monitor.
