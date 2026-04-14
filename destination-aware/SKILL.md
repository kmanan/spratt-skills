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
- `categories` — grocery, medical, daycare, restaurant, or empty
- `reminders` — relevant reminder items (Shopping list for grocery, all lists otherwise)
- `calendar_today` — today's calendar events (check if any match the destination location)

### Step 2: Compose a message

Based on the gathered context, compose a concise text. Rules:

**Grocery store** (category = grocery):
- Lead with the Shopping list items
- Format: "🛒 Heading to [STORE] — [item1, item2, item3]"
- If no Shopping items, check other lists for grocery-related reminders

**Daycare** (category = daycare):
- Check reminders for anything about kids, diapers, forms, pickup
- Check calendar for pickup/dropoff times
- Format: "🏫 Heading to daycare — [relevant reminders]. Pickup at [time]."

**Medical** (category = medical):
- Check calendar for the appointment — include event notes/description
- Search for any recent notes about the patient or symptoms
- Format: "🏥 [Appointment name] — [event notes]. [Any relevant context from memory]."

**Restaurant** (category = restaurant):
- Check calendar for a dinner/lunch reservation at this location
- Include reservation time, party size, any notes
- Format: "🍽 [Restaurant] — Reservation at [time], party of [N]. [notes]"

**No category match / no relevant context:**
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
