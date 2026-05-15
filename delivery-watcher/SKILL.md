---
name: delivery-watcher
description: Texts both principals when an Amazon or Instacart "delivered" email arrives and the front door contact sensor hasn't opened/closed in the 5 minutes since. Carrier email is the trigger; Ring door sensor is the "did you already grab it" gate.
---

# delivery-watcher

A daemon that tells you the package is on the porch — and stays quiet
if you already grabbed it. No mailbox polling in this daemon; signals
arrive via a hook in an existing mail-scan job.

## What it does

A two-process pipeline:

1. **Hook in your existing email scan** (whatever already polls your
   inbox on a schedule). When it sees an email matching:
   - `from = orders@instacart.com` AND subject contains `receipt`
   - `from = order-update@amazon.com` AND subject startswith `Delivered:`

   it writes a row to `delivery_signals.sqlite` with `message_id`,
   `arrived_at` (the email's receive timestamp), `source`, and the
   subject. Idempotent (`INSERT OR IGNORE` on `message_id`).

2. **KeepAlive launchd daemon, 30s poll loop**, reads
   `delivery_signals.sqlite` (local file — free). Per loop:
   - Records new signals into `deliveries.sqlite.alerts` (dedup on
     `message_id`).
   - For each alert where `(now - arrived_at) >= 5 min` and not yet
     notified:
     - Queries Home Assistant for the front-door contact sensor's
       `last_changed`.
     - If the door changed state at-or-after the email's `arrived_at` →
       assume picked up, mark `skipped`, silent.
     - Otherwise → outbox-send to both principals (separate messages).

The gate runs from *email arrival time*, not from when the watcher
noticed the email. So if the scan job picks up a delivery email 20 min
after it arrived, the gate has already passed and the door check fires
on the next watcher tick.

## Sample output

```
📦 Amazon delivered: Slice Auto-Retractable Box Cutter. Still on porch.
📦 Instacart delivered. Still on porch.
```

One message per package — no batching. iMessage threading handles
visual grouping when several land at once.

## Why this shape

A first design polled an `orders.sqlite` table that an upstream email
scanner populated via LLM extraction. That was wrong:

- It required the upstream extractor to find an `order_id` URL in the
  body, and the Outlook fetch tool was stripping all `<a href>` tags
  before the LLM ever saw the body. Every Instacart email was being
  skipped with "no real order_id."
- It added an LLM dependency to the delivery-alert path. Detection of
  *whether a delivery happened* is a sender+subject string match — no
  language understanding required.

So the rewrite: signal-detection is a 5-line `if sender == X and
subject startswith Y` block inside the mail-scan job. The watcher
reacts to a tiny local SQLite table. No LLM in the delivery-alert
critical path. Order_id-extraction bugs upstream stop mattering.

## Why no separate mailbox polling

Polling a remote mailbox API every 30s to catch ~3-5 events/week
across thousands of API calls is wasteful. Your existing mail-scan
job already sees every incoming email; piggybacking on it adds zero
new mailbox calls. Worst-case alert latency = scan cadence + 5 min
gate. For a 30-min scan cadence that's ~35 min, which is fine when
the value is "phone buzzes when needed" rather than seconds.

## Architecture

```
Carrier (Amazon, Instacart) → email
                              ↓
              your existing inbox-scan job (runs on a schedule)
                              ↓
              record_delivery_signals() hook
              matches sender+subject (no LLM)
                              ↓
              delivery_signals.sqlite (message_id PK, arrived_at, ...)
                              ↓
              delivery-watcher.py daemon (30s local-SQLite poll)
                              ↓
              deliveries.sqlite (table: alerts) — record + dedup
                              ↓
              T+5min from arrived_at: GET /api/states/binary_sensor.<door>
                              ↓
              last_changed >= arrived_at ?
                 yes → silent (mark skipped='door changed at <iso>')
                 no  → outbox text to BOTH principals (separate)
                              ↓
                      mark notified=1 (one shot per message_id)
```

## Wiring

| | |
|---|---|
| **What you get** | `scripts/watch.py` (~200 lines, single file), `scripts/gather-emails.hook.py` (the ~80-line hook function to drop into your mail-scan job), launchd plist example, SKILL.md |
| **Dependencies** | An existing inbox-scan job that fetches Outlook/Gmail metadata on a schedule (the bundled `gather-emails.hook.py` plugs into one). Home Assistant with a front-door contact sensor (device_class=`door`). Outbox CLI. |
| **Schedule** | launchd `com.spratt.delivery-watcher`, KeepAlive, polls local SQLite every 30s |
| **macOS-specific** | launchd plist is macOS-specific; daemon is portable to any host that can run Python 3 + curl Home Assistant. |
| **Setup time** | ~5 minutes once mail-scan + HA + outbox are already wired |

## Configuration

Hardcoded near the top of `watch.py`:

```python
DOOR_ENTITY     = "binary_sensor.front_door_2"   # your HA door contact sensor
MANAN_PHONE     = "+13157082088"                  # principal 1
HARSHITA_PHONE  = "+13129330988"                  # principal 2
GATE_SECONDS    = 5 * 60                          # how long to wait before alerting
POLL_SECONDS    = 30                              # daemon poll cadence (local DB only)
FIRST_RUN_GRACE = 60 * 60                         # silently backfill rows older than this on first run
```

Sender + subject patterns are defined in `record_delivery_signals()` —
add carriers as needed (DoorDash, USPS, etc).

Find your door entity via:
```
curl -H "Authorization: Bearer $HA_TOKEN" $HA_URL/api/states \
  | jq -r '.[] | select(.attributes.device_class == "door") | .entity_id'
```

## First-run safety

On the very first daemon start, `deliveries.sqlite.alerts` is empty.
Without guardrails this would text every historical "delivered" signal
already in `delivery_signals.sqlite`. The first-run code inserts all
existing signals with `notified=1, skipped=1, skip_reason='first-run
backfill'`. From that moment on, only *new* signals can trigger a
message.

## Dedup

PK on `alerts(message_id)` — the Outlook/Gmail message ID. One alert
per email. If the mail-scan job sees the same email again later (same
message_id), the hook's `INSERT OR IGNORE` ensures no duplicate signal,
and even if a duplicate did slip in, the watcher's `INSERT OR IGNORE`
into `alerts` would catch it.

## Edge cases worth knowing

- **Costco delivery via Instacart.** Costco grocery delivery uses
  Instacart as the fulfillment provider. The email comes from
  `orders@instacart.com` and matches the "receipt" rule automatically.
- **Amazon promo / Subscribe-and-Save.** Those come from different
  senders (e.g. `marketing@amazon.com`) and won't false-fire — only
  `order-update@amazon.com` + `Delivered:` subject triggers.
- **Mail-scan lag.** Worst-case alert latency = scan-cadence + 5min
  gate. Tune your scan cadence to your tolerance.

## Failure modes

| Failure | Behavior |
|---|---|
| Mail-scan job stops running | No signals → watcher idles. Independent monitoring of the mail-scan job catches this. |
| HA unreachable | `get_door_last_changed()` returns None → defer (don't mark notified). Retry next 30s. No spam. |
| Outbox send fails | Leave un-notified, retry next loop, AND outbox an alert to principal 1 describing the failure. |
| Loop body crashes | Caught at top of `main()`, alerts via outbox, sleep, continue. |
| `record_delivery_signals` hook crashes | Caught locally in the mail-scan job; logs warning and continues. |

Observability is iMessage-first per the spratt design — no log-tailing.

## What's intentionally NOT in v1

- **Camera snapshot in the message.** The outbox CLI is text-only.
  Adding attachment support would touch the outbox sender, which is
  load-bearing. Filed separately.
- **Carriers beyond Amazon / Instacart.** DoorDash, USPS, Whole Foods,
  Walmart all have different sender/subject patterns. Add only when
  needed.
- **Body parsing for the alert message.** Instacart's subject is
  generic ("Your Instacart family order receipt"), so the alert says
  just "Instacart delivered." Parsing the body to extract store name
  is filed as a possible enhancement.
- **Porch-pirate "picked up by stranger" detection.** Different problem.
- **Vision/OCR of shipping labels.** Carriers already tell us the answer.

## Testing

The script has no `--smoke` flag in v1 — the daemon path is simple
enough that the building blocks can be exercised inline:

```bash
# Insert a fake signal as if it just arrived
python3 -c "
import sys; sys.path.insert(0, '/path/to/your-mail-scan-job')
import importlib.util
spec = importlib.util.spec_from_file_location('gem', '/path/to/your-mail-scan-job/gather.py')
gem = importlib.util.module_from_spec(spec); spec.loader.exec_module(gem)
gem.record_delivery_signals([{
  'id': 'TEST_FAKE_ID', 'provider': 'outlook', 'account': 'outlook',
  'from': 'orders@instacart.com',
  'subject': 'Your Instacart family order receipt',
  'date': '2026-05-14T21:01:16Z',
}])
"
```

For end-to-end live testing, wait for a real delivery. (You will not
need to fake one — they happen.)
