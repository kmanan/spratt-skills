# Email Scan Cron — Prompt Template

Example cron prompt for an email scanning agent that triages recent emails and ingests orders.

**Key design decisions:**
- Scan by **time window** (last 8 hours), NOT by unread status. Users open emails to check details — that shouldn't cause the scanner to skip them.
- Use the `query --after` command for Outlook and `newer_than:` for Gmail.
- The 8-hour window provides overlap with a 3x/day scan schedule (e.g., 7:30am, 1:30pm, 6pm).
- Order deduplication is handled by `order-ingest.py` (skips if `order_id + source` already exists), so re-scanning the same email is harmless.
- Instacart orders are ingested with empty items — the `instacart-orders` skill fills them in later via browser scraping.

## Prompt

```
You are the email scanning agent. Scan email inboxes for actionable emails received in the last 8 hours.

STAGE 1: Header triage

Compute cutoff time:
CUTOFF=$(date -u -v-8H '+%Y-%m-%dT%H:%M:%SZ')

Outlook:
outlook-mail.sh --account ACCOUNT query --after "$CUTOFF" --folder Inbox --count 30

Gmail:
gog gmail search -a ACCOUNT "newer_than:8h"

Classify each email by subject and sender ONLY:
- ACTIONABLE: order confirmations, flight/hotel bookings, delivery notifications
- SKIP: newsletters, marketing, social media, promotions

STAGE 2: Extract from actionable emails

Read full body of each ACTIONABLE email and route:

Grocery/food orders:
- Instacart: ingest with empty items (email won't have item list):
  order-ingest.py --source instacart --order-id ID --date DATE --items '[]' --total 0
- All others (Amazon, DoorDash, etc.): extract full item list:
  order-ingest.py --source SOURCE --order-id ID --date DATE --items 'JSON' --total TOTAL --notify
```
