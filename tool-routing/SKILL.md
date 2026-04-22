---
name: tool-routing
description: Maps user intents to the correct tool or skill (smart home, iMessage send vs schedule, calendar writes, reminders, browser, grocery, addresses, email handling, message history, forwarded-email semantics, creating-todos-for-others etiquette, cron-vs-outbox boundaries). Read when you need to pick a tool for a task and the mapping isn't obvious from the skill name, or when deciding between `message` (live) vs `outbox` (scheduled), or when asked "what are our plans?".
---

# Tool Routing

Intent -> tool/skill. Read this before composing a response that involves doing something, not just talking.

## Messaging

- **Send iMessage (live conversation)** -> `message` tool with `channel: "bluebubbles"`, `action: "send"`. For group chats use `target: "chat_guid:GUID"` from CONTACTS.md. For DMs use E.164 phone number.
- **Schedule iMessage (pre-composed, future or background)** -> **outbox** skill. Messages where the content is known at scheduling time go through the SQLite outbox. No OpenClaw crons for message delivery. No direct imsg calls. See outbox SKILL.md.
- **Dynamic iMessage (content assembled at delivery time)** -> `message` tool directly. Morning briefings, digests, inspection reports -- content depends on live data, so LLM assembles and sends in the same turn.
- **Message history** -> `imsg history --chat-id N --limit 10 --json` (see TOOLS.md for chat IDs).
- **"Remind me" / "text someone at a time"** -> compose the message, then `outbox.py schedule --to {recipient} --body {text} --at {time} --source "reminder:{desc}"`. For personal reminders (shows on iPhone): `remindctl add "title" --due "YYYY-MM-DD HH:MM"`.

## Productivity

- **Smart home** -> home-assistant skill.
- **Reminder for self** -> apple-reminders skill.
- **Note** -> apple-notes skill.
- **Weather** -> weather skill.
- **Grocery/shopping** -> browser tool -> instacart.com.
- **Addresses/venues** -> goplaces skill, always include Uber deep links (see TOOLS.md).
- **Calendar (WRITING)** -> see TOOLS.md for per-account routing.
- **Cost monitoring** -> model-usage skill (codexbar) or cost-monitor.sh.

## Web and content

- **Any URL** -> browser tool immediately (logged-in Chrome session).
- **Recipe from URL** -> browser tool to extract -> apple-notes skill to save -> browser to add to Instacart.

## Trips

- **Trip questions** (who's traveling, what flights, hotel, dates, status) -> query `~/.config/spratt/db/trips.sqlite` tables: `trips`, `flights`, `hotels`. Only read the manifest for itinerary details (daily plans, restaurant specifics).

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

**Applies to:** trip planning, Instacart cart building, Resy booking flows, recipe-to-cart, meal planning, any multi-step browser workflow. **Does NOT apply to:** single-turn lookups, outbox scheduling, simple reads/writes, cron pipelines that complete in one tick.

Carry the latest `revision` forward after every mutation -- stale revision = conflict error.

## Crons vs outbox (hard boundary)

- **DO NOT create OpenClaw crons for message delivery.** Use the outbox for all scheduled/timed messages.
- Crons are ONLY for: system maintenance (backups, sync, inspections), monitoring tasks that need LLM reasoning (delivery tracking, email triage), and recurring LLM tasks (digests, briefings). Timed message delivery uses the outbox, not crons. Personal reminders use Apple Reminders. Flight tracking uses the deterministic flight monitor daemon, not crons.
