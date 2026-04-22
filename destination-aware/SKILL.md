---
name: destination-aware
description: Surface relevant reminders, calendar context, and notes when Tesla navigation is set to a destination. Triggered by HA webhook when sensor.maha_tesla_destination changes.
version: 1.0.0
---

# Destination-Aware Reminders

When the owner sets a destination in the Tesla, this skill surfaces relevant context before they arrive.

## Trigger

Home Assistant webhook fires when `sensor.maha_tesla_destination` changes from `unknown` to an address. The webhook payload contains the destination address.

## How to handle

### Step 1: Gather context

```bash
python3 ~/.config/spratt/infrastructure/destination/destination-context.py \
  --destination "DESTINATION_ADDRESS"
```

This returns JSON with:
- `place_name` — what goplaces identified (e.g., "QFC", "Bright Horizons")
- `categories` — grocery, medical, daycare, pharmacy, home, work, restaurant, or empty
- `reminders` — structured list of dicts `{title, dueDate, listName, isCompleted}` from all reminder lists (fetched via `remindctl --json`)
- `calendar_today` — today's calendar events (check if any match the destination location)

### Step 2: Compose a message

The daemon handles compose automatically. It applies a **temporal gate** (drops completed and future-dated reminders), then runs **Haiku LLM filtering** per category with a tightly scoped prompt. Each category has its own instruction telling Haiku exactly what to keep and what to exclude.

**Message formats by category:**
- 🛒 Grocery: "Heading to [STORE] — Shopping list: [items]"
- 🏫 Daycare: "Heading to [PLACE] — Don't forget: [items]"
- 💊 Pharmacy: "Heading to [PLACE] — Don't forget: [items]"
- 🏥 Medical: "Heading to [PLACE] — Don't forget: [items]"
- 🏠 Home: "Heading to Home — Don't forget: [items]"
- 💼 Work: "Heading to [PLACE] — Don't forget: [items]"
- 🍽 Restaurant: "Heading to [PLACE] — Don't forget: [items]"

**No category match / no relevant reminders after filtering:**
- Do NOT send a message. Stay silent.

### Step 3: Send via outbox

Only if there is relevant context to share:

```bash
python3 ~/.config/spratt/infrastructure/outbox/outbox.py schedule \
  --to "OWNER" \
  --body "MESSAGE" \
  --at now \
  --source "destination-aware" \
  --created-by "destination-aware"
```

## Creating destination-aware reminders

When someone asks for a reminder tied to a destination ("remind me X when I go to Y", "every Monday at daycare remind me to bring Z"), you must create the reminder so the daemon surfaces it correctly.

### How the daemon gates reminders

The daemon runs two filters on every destination event:
1. **Temporal gate** (`_eligible_titles`): only reminders due today/overdue or undated pass. Future-dated reminders are dropped.
2. **LLM filter**: remaining reminders are matched to the destination category (grocery, daycare, home, etc.) by title.

**The dueDate controls WHICH DAYS the reminder fires. The title controls WHICH DESTINATIONS it matches.**

### Recurring day-specific ("every Monday at daycare", "every Friday coming home")

Use `create-recurring-reminder`:
```bash
~/.config/spratt/skills/apple-reminders/create-recurring-reminder "<title>" <day-of-week> <HH:MM> <list> [notes]
```

Rules:
- **Title must be matchable to the destination category.** Include destination context:
  - Daycare drop-off: "Take X to Bright Horizons" / "Bring X to daycare"
  - Home from daycare: "Take X home from daycare" / "Bring X back from daycare"
  - Grocery: "Get X at Costco" / "Buy X"
- **Day and time must match the actual trip pattern.** Monday 07:30 for morning drop-off, Friday 16:00 for evening pickup. Ask the user if unclear.
- **List:** Use per-person list names for individual reminders, `Shared` for shared household items.
- **One recurring reminder per day+destination combination.** Never create individual copies per week.
- **The user completes the reminder each week** on their phone. Apple auto-advances the dueDate to the next occurrence. If they don't complete it, it stays overdue and fires on subsequent matching trips (intended — it nags).

### One-time destination ("next time I'm at QFC get ginger", "at the doctor Thursday ask about the rash")

Use `remindctl add`:
```bash
remindctl add --title "<title>" --list <list> [--due <date>]
```

- **Specific future date** (e.g., doctor on Thursday): use `--due <date>`. Won't fire until that day.
- **"Next time I go to X"** with no date: omit `--due`. Undated reminders pass the temporal gate every day, firing on the next matching trip. Complete after.

### Permanent destination ("always remind me to check the cubby at daycare")

Use `remindctl add` with no due date. Fires on every matching destination trip until completed.

### What NOT to do

- **NEVER create a non-recurring reminder for a weekly task.** It goes overdue after one week and fires on every matching trip regardless of day — this is the #1 cause of wrong-day destination spam.
- **NEVER put destination reminders on a list the daemon doesn't check.** The daemon checks the lists configured in `get_reminders()` (default: per-person lists + `Shared`).
- **NEVER create duplicate reminders with the same purpose.** One reminder per day+destination. Duplicates across lists cause double messages.

## Important rules

- **NEVER send empty or low-value messages.** If there's nothing useful to surface, stay silent.
- **One message per destination.** The trigger only fires once per nav input.
- **Keep it short.** This is a text you read while parking. Max 3-4 lines.
- **Calendar match:** Compare the destination address against calendar event locations. Partial match is fine (street name match = same place).
- **Memory search:** For medical appointments, search daily logs for the patient name + "doctor" or "pediatrician" to find past visit notes.
