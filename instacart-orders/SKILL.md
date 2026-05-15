---
name: instacart-orders
description: Read Instacart order history (canonical item IDs, retailer, dates) from the printing-press CLI's local SQLite DB. Trigger ad-hoc re-imports when the nightly history scrape is stale or missed an order.
version: 2.0.0
---

# Instacart Order History

Replaces the old browser-snapshot scraper (retired 2026-05-13) with a
deterministic CLI-driven pipeline. Order history now lives in the
`instacart-pp-cli` SQLite DB at
`~/Library/Application Support/instacart/instacart.db` and is populated
nightly by `~/.config/spratt/infrastructure/instacart/history-scrape.py`
under launchd job `com.spratt.instacart-history-scrape`.

## When to use this skill

- "What was in the last Instacart order?" — query the CLI DB.
- "When did I last buy X on Instacart?" — `history search X --json`.
- "How often do I buy X?" — `~/.config/spratt/infrastructure/instacart/cadence.py --due-only`.
- A nightly scrape missed a specific order and you want to import it now.

For *building* a cart (add items, place order) use the **instacart-api**
skill instead.

## Read-only queries

```bash
# Most-recent orders
instacart-pp-cli history list --json --limit 20

# All items at a single retailer, by purchase frequency
instacart-pp-cli history list --store costco --json

# Free-text search across purchased items
instacart-pp-cli history search "honey roasted" --json

# Order + item totals per retailer
instacart-pp-cli history stats --json
```

The DB exposes canonical Instacart IDs:

- `order_id` — 17–18 digit numeric (the Apollo cache id, not the URL id).
- `item_id` — format `items_<retailerLocationId>-<legacyProductId>` (use this with `instacart-pp-cli add --item-id ...` to bypass autosuggest).
- `product_id` — numeric, useful for cross-referencing.

## Ad-hoc re-import (rare)

If you know the nightly scrape missed a specific order — for example
because the order is older than the "Load more" boundary at scrape time
— trigger a manual import.

```bash
# Sweep everything visible on the order-history page (~3 hours wall
# clock for 200+ orders). Idempotent — already-imported orders are
# skipped by `history import`.
python3 ~/.config/spratt/infrastructure/instacart/history-scrape.py --backfill

# Or process only N new (un-tracked) orders. Useful for catch-up after
# the launchd job has been off for a few days.
python3 ~/.config/spratt/infrastructure/instacart/history-scrape.py --max-orders 30
```

The script writes status to
`~/.config/spratt/infrastructure/launchd-status/instacart-history-scrape.json`
and emits an outbox alert on uncaught failure.

## Limits

- Some legacy orders return `cache_key_missing` during extraction (the
  URL `order_id` doesn't line up with the Apollo cache key on a small
  number of older orders). Those rows are skipped, not retried, and the
  script records them under `errors` in the status JSON.
- The CLI's own `history sync` command is documented as broken upstream
  ("Instacart has no clean GraphQL op for order history"). Don't use it.
  Use `history-scrape.py` above — it drives the CLI's own bundled
  `docs/extract-one.js` (per-order Apollo cache reader) and pipes results
  back through `instacart-pp-cli history import -`.
- Auth: relies on the Chrome cookies that `instacart-pp-cli auth login`
  reads. If `auth status --json` returns `logged_in:false`, see the
  **instacart-api** skill for the recovery flow.

## What lives where now

| Old (pre-2026-05-13) | New |
|---|---|
| `orders.sqlite.orders` rows with `source='instacart'` | `instacart.db` `orders` + `order_items` tables |
| Email-scan fabricated `email-XXX` order_ids when receipts had no number | Canonical numeric `order_id` from Apollo |
| Nightly OpenClaw cron `Instacart Order Scraper` (agentTurn, Flash) | launchd `com.spratt.instacart-history-scrape` (deterministic Python) |
| Purchase cadence: `purchase-cadence.py` against `orders.sqlite` for both Amazon and Instacart | `purchase-cadence.py` for Amazon only; `instacart/cadence.py` for Instacart |
| Cart-build via browser autosuggest (`reorder-cart-build.py`) | `cart-build.py` via CLI `add --item-id` (canonical IDs) |

The historic `orders.sqlite` rows stay where they are; nothing actively
queries them anymore. The only `orders.sqlite` table still in use is
`reorder_notifications`, which `reorder-nudge.py` reads + writes to dedup
Wed/Sat nudges across runs.
