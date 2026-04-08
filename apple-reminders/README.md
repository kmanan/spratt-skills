# Apple Reminders — Recurring Reminder Support

Apple's `remindctl` CLI (v0.1.1) doesn't support recurring reminders. This component adds a compiled Swift binary that creates proper recurring reminders via EventKit.

## Why it exists

When someone says "remind me every Monday to bring diapers to daycare," the LLM should create **one recurring reminder**, not 12 individual copies. Without this tool, the LLM has no way to set recurrence and falls back to creating separate reminders for each date — which is fragile, clutters the list, and stops after the last one.

## Usage

```bash
# Compile (once)
swiftc scripts/create-recurring-reminder.swift -o create-recurring-reminder

# Create a recurring reminder
./create-recurring-reminder "Buy milk" monday 07:30
./create-recurring-reminder "Take diapers to daycare" monday 07:30 Manan "Bright Horizons Woodinville"
```

**Arguments:**
- `title` — reminder text (required)
- `day-of-week` — monday, tuesday, ... sunday (required)
- `HH:MM` — time in 24h format (required)
- `list-name` — Apple Reminders list (optional, defaults to default list)
- `notes` — additional notes (optional)

## What it does

- Creates a single `EKReminder` with an `EKRecurrenceRule` (weekly, on the specified day)
- Sets an alarm at the due time
- Syncs across all Apple devices via iCloud
- Prints the reminder ID for reference

## Dependencies

- macOS (EventKit is Apple-only)
- Swift compiler (`swiftc`, included with Xcode or Command Line Tools)
- Reminders permission granted to Terminal

## Integration with remindctl

Use `remindctl` for everything except recurrence — viewing, completing, deleting, editing. When `remindctl` adds recurrence support (PR #40), this binary becomes unnecessary.
