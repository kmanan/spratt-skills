---
name: destination-aware
description: Surface relevant reminders, calendar context, and notes when Tesla navigation is set to a destination. Triggered by HA webhook when sensor.maha_tesla_destination changes.
version: 1.0.0
---

# Destination-Aware Reminders

When Manan sets a destination in the Tesla, this skill surfaces relevant context before he arrives.

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
  --to "Manan" \
  --body "MESSAGE" \
  --at now \
  --source "destination-aware" \
  --created-by "destination-aware"
```

## Important rules

- **NEVER send empty or low-value messages.** If there's nothing useful to surface, stay silent.
- **One message per destination.** The trigger only fires once per nav input.
- **Keep it short.** This is a text you read while parking. Max 3-4 lines.
- **Calendar match:** Compare the destination address against calendar event locations. Partial match is fine (street name match = same place).
- **Memory search:** For medical appointments, search daily logs for the patient name + "doctor" or "pediatrician" to find past visit notes.
