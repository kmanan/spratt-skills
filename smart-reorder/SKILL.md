---
name: smart-reorder
description: Two-source purchase cadence (Amazon orders.sqlite + Instacart instacart.db) plus the Wed/Sat reorder-nudge iMessage. Median day-gap per canonical item, due/soon/not_due classification, cart-aware nudge framing.
version: 1.0.0
---

# Smart Reorder

> Originally based on the [instacart-skill](https://clawhub.com/skills/instacart-skill)
> by **bigdaddyluke** on ClawHub. His skill was an LLM-driven browser cart-builder
> with a smart-replenishment notion baked in; here the replenishment idea is
> extracted into a SQL-backed analyzer that feeds Spratt's deterministic cart-build
> pipeline. No LLM at notification time — just SQL → text → outbox.

## What's in this skill

| File | Purpose |
|---|---|
| `scripts/purchase-cadence.py` | Amazon-side cadence over `orders.sqlite`. Uses `item_aliases` (populated by `item-classify.py`) to merge SKU variants. |
| `scripts/item-classify.py` | Nightly Flash classification of receipt item names → canonical products. Maintains the `item_aliases` table. |
| `scripts/reorder-nudge.py` | Wed + Sat 8am PT iMessage. Unions both cadence sources by `canonical_key`. Reframes from "due for reorder" → "🛒 Staged in Instacart cart" when `cart-build.py` ran in the last 45 min. |
| `schemas/orders.sql` | `item_aliases` + `reorder_notifications` tables. |

Instacart-side cadence lives in [`instacart-orders/scripts/cadence.py`](../instacart-orders/scripts/cadence.py) — same algorithm, different DB and no LLM aliasing because Instacart's `item_id` is already canonical.

## How the two sources differ

| | Amazon (`purchase-cadence.py`) | Instacart (`cadence.py`) |
|---|---|---|
| Source DB | `orders.sqlite` | `~/Library/Application Support/instacart/instacart.db` |
| Item identity | Free-text name → Flash-classified `canonical_key` via `item_aliases` | Canonical `(item_id, retailer_slug)` from Instacart |
| LLM at extraction time | Yes (Flash) — only when a new item name appears | No |
| LLM at notification time | No | No |
| Output schema | Compatible — both emit `canonical_key`, `purchases`, `cadence_days`, `days_since`, `status`, `score`, `recency_ratio`, `quantity`, … |

`reorder-nudge.py` consumes both with a single union by `canonical_key`.

## Scoring (both sides)

```
score = log(purchases + 1) × recency_match(days_since / cadence) × confidence(purchases)
```

`recency_match` peaks at 1.0 for `ratio ∈ [0.5, 1.5]`, decays linearly to 0 by `ratio = 3`. `confidence` ramps 0 → 1 over 1 → 5 purchases. `--due-only` additionally requires `purchases ≥ 4` and `recency_ratio ≤ 3` so the nudge doesn't resurface items abandoned 3+ cadence periods ago.

## Why this isn't a fork-back to ClawHub

bigdaddyluke's upstream is an LLM-browser cart builder. This descendant is a SQL analyzer that depends on (a) the Spratt outbox for delivery, (b) mvanhorn's `instacart-pp-cli` DB for Instacart canonical IDs, and (c) `orders.sqlite` + `item_aliases` for Amazon. There's no shared code path that would merge cleanly. The lineage is honest in this attribution, not in a PR.
