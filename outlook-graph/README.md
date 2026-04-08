# outlook-graph — Outlook/Hotmail via Microsoft Graph API

Shell scripts for managing Outlook email and calendar through Microsoft Graph API. Multi-account OAuth2 with auto-refreshing tokens. No Node dependencies, no Python — just bash, curl, and jq.

## What's Included

| Script | Purpose |
|--------|---------|
| `outlook-setup.sh` | Automated Azure App Registration + OAuth2 setup |
| `outlook-mail.sh` | Read, search, send, manage emails |
| `outlook-calendar.sh` | Create, read, update, delete calendar events with attendees and descriptions |
| `outlook-token.sh` | Token refresh, test, list accounts |

## Calendar Features

```bash
# Create event with description and attendees
outlook-calendar.sh --account personal --calendar "For Family" \
    create "Doctor Appointment" "2026-04-06T15:00" "2026-04-06T16:00" \
    --location "123 Main St, Kirkland WA" \
    --body "Symptoms to discuss: dry lips, knee pain, reduced appetite" \
    --attendees "alice@outlook.com,bob@gmail.com"

# Update event body (e.g., add notes later)
outlook-calendar.sh --account personal update <id> --body "Updated notes here"

# Add attendees without replacing existing ones
outlook-calendar.sh --account personal update <id> --add-attendees "new@email.com"

# Multi-field update
outlook-calendar.sh --account personal update <id> \
    --body "New notes" --location "New Location" --add-attendees "extra@email.com"

# Target a specific calendar by name (case-insensitive)
outlook-calendar.sh --account personal --calendar "for family" events

# View, list, check availability
outlook-calendar.sh today
outlook-calendar.sh week
outlook-calendar.sh read <id>
outlook-calendar.sh free "2026-04-06T09:00" "2026-04-06T17:00"
```

## Email Features

The `inbox` and `unread` commands are scoped to the Inbox folder via `/mailFolders/Inbox/messages`. This means Outlook's spam/junk filtering is respected — emails that Outlook moves to Junk will not be returned. This is important when using LLM-driven email scanning, as phishing emails in the Junk folder could otherwise be processed as legitimate mail.

```bash
# Read inbox (Inbox folder only — Junk/Spam excluded)
outlook-mail.sh --account outlook inbox 10
outlook-mail.sh --account outlook unread

# Search (KQL syntax)
outlook-mail.sh --account outlook search "subject:instacart OR subject:tracking"

# Advanced queries — time-based filtering (recommended for automated scanning)
CUTOFF=$(date -u -v-8H '+%Y-%m-%dT%H:%M:%SZ')
outlook-mail.sh --account outlook query --after "$CUTOFF" --folder Inbox --count 30
outlook-mail.sh --account outlook query --after 2026-04-01 --from boss@work.com --has-attachments

# Manage
outlook-mail.sh mark-read <id>
outlook-mail.sh send "to@email.com" "Subject" "Body text"
outlook-mail.sh reply <id> "Reply body"
```

**Tip for email scanning crons:** Use `query --after` with a time window instead of `unread`. Filtering by unread status means emails the user has already opened get skipped, causing missed orders and notifications.

## Setup

```bash
# Requires: Azure CLI (az), jq
./scripts/outlook-setup.sh                    # Default account
./scripts/outlook-setup.sh --account work     # Additional account
./scripts/outlook-setup.sh --account personal # Another account
```

The setup script handles Azure App Registration, API permissions (Mail.ReadWrite, Mail.Send, Calendars.ReadWrite), and OAuth2 token exchange automatically.

## Multi-Account

```bash
# Use --account flag or OUTLOOK_ACCOUNT env var
outlook-calendar.sh --account personal today
outlook-mail.sh --account work inbox
export OUTLOOK_ACCOUNT=personal

# List configured accounts
outlook-token.sh list
```

Credentials stored per-account in `~/.outlook-mcp/{account}/`.

## Dependencies

- bash, curl, jq
- Azure CLI (`az`) for initial setup only
- A Microsoft account (personal or work/school)

## Why Not the ClawHub outlook-plus Skill?

This started as a fork of [cristiandan/outlook-skill](https://github.com/cristiandan/outlook-skill) but has been significantly extended:
- Multi-calendar targeting (`--calendar` flag with case-insensitive name resolution)
- Event descriptions/body (`--body` on create and update)
- Attendee management (`--attendees`, `--add-attendees`)
- Multi-field updates in a single call
- Auto-refresh tokens on every call (no manual token management)

## License

MIT
