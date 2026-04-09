---
name: instacart-orders
description: Scrape Instacart order history via browser to get full itemized details (names, quantities, prices). Use when an Instacart order has missing items, or when asked to backfill order history.
version: 1.0.0
---

# Instacart Order Scraping

Extracts full itemized order details from Instacart via browser automation.
The email scanner detects Instacart orders but emails rarely contain item lists — this skill fills them in by scraping the Instacart account.

## When to use

- After email scanner ingests an Instacart order with 0 or few items
- When asked to backfill past Instacart orders
- When asked "what was in the last Instacart order?"

## Step 1: Find orders needing items

Check for Instacart orders with empty or missing items:

```bash
sqlite3 ~/.config/spratt/orders/orders.sqlite \
  "SELECT id, order_id, order_date, items, total FROM orders WHERE source = 'instacart' AND (items = '[]' OR items = '' OR json_array_length(items) = 0) ORDER BY order_date DESC;"
```

If no orders need filling, stop here.

## Step 2: Get order list from Instacart

```
openclaw browser navigate "https://www.instacart.com/store/account/orders"
openclaw browser snapshot --format ai
```

This returns a list of past orders with:
- Delivery date
- Item count and total
- **"View order detail" link** — URL like `/store/orders/ORDER_ID`
- Store name (QFC, Costco, Safeway, etc.)

Match orders from the database (by date + approximate total) to find the right detail URL.

## Step 3: Get receipt with full item details

From the order detail page, find the **"Receipt"** link. Navigate to it:

```
openclaw browser navigate "RECEIPT_URL"
openclaw browser snapshot --format ai
```

The receipt page contains the full itemized breakdown organized by category:
- Item name with size/variant (e.g., "Cilantro Bunch (1 bunch)")
- Quantity (e.g., "2 x $4.99")
- Final price after any loyalty savings

**If you can't find the receipt link**, fall back to the order detail page — item names are in img alt text (no prices/quantities, but names are enough for search).

## Step 4: Parse items into JSON

Build a JSON array from the receipt. Each item:

```json
[
  {"name": "Cilantro Bunch", "qty": 1, "price": 1.49},
  {"name": "Simply Pulp Free Orange Juice Bottles", "qty": 1, "price": 7.99},
  {"name": "LaCroix Sparkling Water, Orange", "qty": 2, "price": 8.58}
]
```

**Rules:**
- `name`: product name without size/variant in parentheses — keep it searchable
- `qty`: number of units ordered (the "2 x" part)
- `price`: final item price after loyalty savings (not original price)
- Get the order total from the "Order Totals" section

## Step 5: Update the order in the database

```bash
python3 ~/.config/spratt/infrastructure/orders/order-ingest.py update-items \
  --source instacart \
  --order-id "ORDER_ID" \
  --items 'JSON_ARRAY' \
  --total TOTAL \
  --store STORE_NAME
```

**Always include `--store`** (e.g. `qfc`, `costco`, `safeway`). The store name is visible on the order list and detail pages. This powers the purchase cadence analysis for smart reordering.

If the order doesn't exist yet (backfill), use insert mode:

```bash
python3 ~/.config/spratt/infrastructure/orders/order-ingest.py \
  --source instacart \
  --order-id "ORDER_ID" \
  --date "YYYY-MM-DD" \
  --items 'JSON_ARRAY' \
  --total TOTAL \
  --store STORE_NAME
```

## Step 6: Confirm

After updating, verify:

```bash
sqlite3 ~/.config/spratt/orders/orders.sqlite \
  "SELECT id, order_date, json_array_length(items) as item_count, total FROM orders WHERE source = 'instacart' ORDER BY order_date DESC LIMIT 5;"
```

## Notes

- **NEVER use `profile: "user"` with the browser** — always use the default `openclaw` profile
- The receipt page is preferred over the order detail page because it has quantities and prices
- Receipt URLs contain auth tokens and may expire — always navigate from the order detail page to get a fresh link
- If "Load more orders" button appears on the orders list, click it for older orders
- Order IDs on Instacart's site may not exactly match the order_id stored from email extraction — match by date + store instead
