---
name: delivery-watcher
description: Texts both principals when an Amazon or Instacart package is delivered and the front door hasn't opened/closed in the 5 minutes since. Carrier delivery confirmation is the trigger; the Ring contact sensor is the "did you already grab it" gate.
---

# delivery-watcher

A daemon that tells you the package is on the porch — and stays quiet
if you already grabbed it.

## What it does

KeepAlive launchd daemon, 30s poll loop. Per loop:

1. Polls `orders.sqlite` for rows where `tracking_status='delivered'` and
   `source IN ('amazon','instacart')`.
2. Records each new delivery in `deliveries.sqlite` (dedup on `order_pk`).
3. For deliveries where 5 minutes have elapsed since `tracking_updated_at`:
   - Queries Home Assistant for the front-door contact sensor's `last_changed`.
   - If the door changed state at-or-after the delivery time → assume
     picked up, mark `skipped`, silent.
   - Otherwise → outbox-send to both principals (separate messages).

That's the whole skill. Carrier "delivered" emails are the trigger source
(no computer vision needed — they already tell us *which* order it was).
The door contact sensor is the pickup gate.

## Sample output

```
📦 Amazon delivered: Slice Auto-Retractable Box Cutter + 2 more. Still on porch.
📦 Instacart delivered — QFC, 12 items, $87.50. Still on porch.
```

One message per package — no batching. iMessage threading handles visual
grouping when several land at once.

## Architecture

```
Carrier (Amazon, Instacart) → email
                              ↓
                      email-scan cron (parses delivery confirmation)
                              ↓
                      orders.sqlite (source, tracking_status='delivered',
                                     tracking_updated_at=<ISO>)
                              ↓
                      delivery-watcher.py daemon (30s poll)
                              ↓
                      deliveries.sqlite — record + dedup
                              ↓
              T+5min: GET /api/states/binary_sensor.<door>
                              ↓
              last_changed >= delivery_ts ?
                 yes → silent (mark skipped='door changed at <iso>')
                 no  → outbox text to BOTH principals (separate)
                              ↓
                      mark notified=1 (one shot per order_pk)
```

## Why it works without vision

A Ring camera *could* detect a package via its AI, but then we'd still
have to figure out *which* order arrived (Amazon? Instacart? UPS for what
order specifically?). The carriers already tell us in email. That signal
is more reliable and free.

## Wiring

| | |
|---|---|
| **What you get** | `scripts/watch.py` (~240 lines — single file, no abstractions), launchd plist example, SKILL.md |
| **Dependencies** | `orders.sqlite` with `tracking_status` populated by an email-scan pipeline (this repo's `email-to-orders` skill does this for Amazon + Instacart). Home Assistant with a front-door contact sensor (device_class=`door`). Outbox CLI. |
| **Schedule** | launchd `com.spratt.delivery-watcher`, KeepAlive, polls every 30s |
| **macOS-specific** | launchd plist is macOS-specific; daemon is portable to any host that can run Python 3 + curl Home Assistant. |
| **Setup time** | ~5 minutes once email-scan + orders.sqlite + HA + outbox are already wired |

## Configuration

Hardcoded near the top of `watch.py`:

```python
DOOR_ENTITY     = "binary_sensor.front_door_2"   # your HA door contact sensor
MANAN_PHONE     = "+13157082088"                  # principal 1
HARSHITA_PHONE  = "+13129330988"                  # principal 2
GATE_SECONDS    = 5 * 60                          # how long to wait before alerting
POLL_SECONDS    = 30                              # daemon poll cadence
FIRST_RUN_GRACE = 60 * 60                         # silently backfill rows older than this on first run
```

Find your door entity via:
```
curl -H "Authorization: Bearer $HA_TOKEN" $HA_URL/api/states \
  | jq -r '.[] | select(.attributes.device_class == "door") | .entity_id'
```

## First-run safety

On the very first daemon start, `deliveries.sqlite` is empty. Without
guardrails this would text every historical "delivered" row in
`orders.sqlite`. The first-run code inserts all existing delivered rows
with `notified=1, skipped=1, skip_reason='first-run backfill'`. From that
moment on, only *new* delivered rows can trigger a message.

## Dedup

PK on `deliveries(order_pk)`. One alert per delivered order. If the same
row in `orders.sqlite` updates again later (e.g. status correction), the
watcher does **not** re-fire.

## Edge cases worth knowing

- **Email-scan lag.** Email-scan runs every N minutes. If it ingests a
  "delivered" email 30 min after the actual delivery, and you already
  grabbed the package during that 30 min, the door's `last_changed` will
  be *before* `delivery_ts` and the watcher will incorrectly text "still
  on porch." Acceptable v1 failure — email-scan typically runs every 30
  min, so the worst case is bounded.
- **Costco delivery via Instacart.** Costco grocery delivery uses
  Instacart as the fulfillment provider. It shows up in `orders.sqlite`
  as `source='instacart', store='Costco'`, and the watcher catches it
  through the Instacart branch.
- **Stale rows in `out_for_delivery`.** Some Amazon orders sit at
  `out_for_delivery` and never transition (email-scan missed the
  "delivered" notification). These never fire — accurate behavior.

## Failure modes

| Failure | Behavior |
|---|---|
| HA unreachable | `get_door_last_changed()` returns None → defer (don't mark notified). Retry next 30s. No spam. |
| Outbox send fails | Leave un-notified, retry next loop, AND outbox an alert to principal 1 describing the failure. |
| Loop body crashes | Caught at the top of `main()`, alerts via outbox, sleep, continue. |
| Outbox itself broken | We can't alert via outbox. KeepAlive launchd respawn + a daemon-down health check picks it up. |

Observability is iMessage-first per the spratt design — no log-tailing.

## What's intentionally NOT in v1

- **Camera snapshot in the message.** The outbox CLI is text-only. Adding
  attachment support would touch the outbox sender, which is load-bearing.
  Filed as a separate task.
- **Carriers beyond Amazon / Instacart.** DoorDash, USPS, Whole Foods,
  Walmart all have different email shapes. Add only when needed.
- **Porch-pirate "picked up by stranger" detection.** Different problem.
- **Vision/OCR of shipping labels.** Carriers already tell us the answer.

## Testing

The script has no `--smoke` flag in v1 — the daemon path is simple enough
that the building blocks can be exercised inline:

```bash
python3 -c "
import sys; sys.path.insert(0, '/path/to/delivery-watcher')
import watch
watch.db_init()
print('HA door:', watch.get_door_last_changed())
print('new rows:', len(watch.poll_new_deliveries()))
"
```

For end-to-end live testing, wait for a real delivery. (You will not
need to fake one — they happen.)
