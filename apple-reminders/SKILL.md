---
name: apple-reminders
description: Manage Apple Reminders via the `remindctl` CLI on macOS (list, add, edit, complete, delete). Supports lists, date filters, and JSON/plain output.
homepage: https://github.com/steipete/remindctl
metadata: {"clawdbot":{"emoji":"\u23f0","os":["darwin"],"requires":{"bins":["remindctl"]},"install":[{"id":"brew","kind":"brew","formula":"steipete/tap/remindctl","bins":["remindctl"],"label":"Install remindctl via Homebrew"}]}}
---

# Apple Reminders CLI (remindctl)

Use `remindctl` to manage Apple Reminders directly from the terminal. It supports list filtering, date-based views, and scripting output.

Setup
- Install (Homebrew): `brew install steipete/tap/remindctl`
- From source: `pnpm install && pnpm build` (binary at `./bin/remindctl`)
- macOS-only; grant Reminders permission when prompted.

Permissions
- Check status: `remindctl status`
- Request access: `remindctl authorize`

View Reminders
- Default (today): `remindctl`
- Today: `remindctl today`
- Tomorrow: `remindctl tomorrow`
- Week: `remindctl week`
- Overdue: `remindctl overdue`
- Upcoming: `remindctl upcoming`
- Completed: `remindctl completed`
- All: `remindctl all`
- Specific date: `remindctl 2026-01-04`

Manage Lists
- List all lists: `remindctl list`
- Show list: `remindctl list Work`
- Create list: `remindctl list Projects --create`
- Rename list: `remindctl list Work --rename Office`
- Delete list: `remindctl list Work --delete`

Create Reminders
- Quick add: `remindctl add "Buy milk"`
- With list + due: `remindctl add --title "Call mom" --list Personal --due tomorrow`

List Routing
- **Always use `--list` when creating reminders.** Route based on who it's for:
  - Owner's personal items -> `--list Owner`
  - Partner's personal items -> `--list Partner`
  - Shared household items (groceries, house tasks) -> `--list Shopping` (grocery) or `--list Shared` (other)
- If unclear who it's for, ask.

Reminder vs FYI: The Narrow-Scope Rule
- **A reminder is for items with BOTH (a) an explicit deadline or specific date AND (b) a real consequence if missed.** Everything else is an FYI text, not a reminder.
- Qualifies as a reminder:
  - "Submit form by Friday" (deadline + consequence)
  - "RSVP by 5/12"
  - "Renew passport" (deadline implied by expiry)
  - "Confirm scheduled appointment by date X"
  - User explicitly says "remind me to ..."
- Does NOT qualify — send a one-line FYI text instead:
  - Family member started/shared a cart, doc, or list ("wife started an Instacart cart")
  - Vendor invites or save-the-dates without a deadline
  - Account/order/shipping status updates
  - Points, rewards, marketing soft-CTAs ("offer ends soon")
  - General awareness items the user already knows about from the source
- Why this matters: a reminder list cluttered with non-actionable items becomes noise. The user stops trusting it, and briefings/digests over-report. When in doubt, send the FYI text.
- This rule is enforced upstream in the email-scan pipeline (decide -> extract -> run): items routed as `notify` send a one-line outbox text and mark the email read; only items with `route: reminder` AND a `due_date` create a reminder. User-curated forwards are the one passthrough exception.

Edit Reminders
- Edit title/due: `remindctl edit 1 --title "New title" --due 2026-01-04`

Complete Reminders
- Complete by id: `remindctl complete 1 2 3`

Delete Reminders
- Delete by id: `remindctl delete 4A83 --force`

Output Formats
- JSON (scripting): `remindctl today --json`
- Plain TSV: `remindctl today --plain`
- Counts only: `remindctl today --quiet`

Date Formats
Accepted by `--due` and date filters:
- `today`, `tomorrow`, `yesterday`
- `YYYY-MM-DD`
- `YYYY-MM-DD HH:mm`
- ISO 8601 (`2026-01-04T12:34:56Z`)

Recurring Reminders
- **ALWAYS use the recurring reminder tool for repeating reminders. NEVER create individual copies for each occurrence.**
- remindctl v0.1.1 does NOT support recurrence (PR #40 pending).
- Use the compiled EventKit binary instead:
  `~/.config/spratt/skills/apple-reminders/create-recurring-reminder <title> <day-of-week> <HH:MM> [list-name] [notes]`
- Example: `create-recurring-reminder "Buy milk" monday 07:30 Personal`
- Supported days: monday, tuesday, wednesday, thursday, friday, saturday, sunday
- Creates a single weekly recurring reminder with an alarm at the specified time.
- The reminder syncs across all Apple devices via iCloud.
- Source: `create-recurring-reminder.swift` in the same directory.
- **For destination-aware recurring reminders** (e.g., "every Monday at daycare remind me to bring X"): read the `destination-aware` skill first. The title, day, and time must be set correctly for the destination daemon's temporal gate and LLM filter to work. Wrong setup causes reminders to fire on the wrong days.

Notes
- macOS-only.
- If access is denied, enable Terminal/remindctl in System Settings -> Privacy & Security -> Reminders.
- If running over SSH, grant access on the Mac that runs the command.
