---
name: instacart-api
description: Build Instacart carts via the printing-press CLI. Search products and add to multi-retailer carts directly through Instacart's GraphQL API — no browser automation. Manan places the order in the Instacart app (one tap).
homepage: https://www.instacart.com
metadata: {"clawdbot":{"emoji":"🛒","requires":{"bins":["instacart-pp-cli"]}}}
---

# Instacart API (cart-build)

CLI-driven replacement for browser cart-building. The CLI rides on the Chrome session and talks directly to Instacart's GraphQL endpoint — no Playwright, no DOM scraping, no 2FA dance.

**Status:** Live in production as of 2026-05-13. The auto cart-build pipeline runs Wed+Sat 7:45am PT via `~/.config/spratt/infrastructure/instacart/cart-build.py` (launchd `com.spratt.instacart-cart-build`) and stages items by exact `--item-id`. The `--item-id` path bypasses the autosuggest/search chain entirely and works against canonical IDs persisted by `history import` from the nightly scraper. The earlier upstream bug in `ShopCollectionScoped` only affected `search` and the natural-language `add <query>` path — those still hit `ShopCollectionScoped returned no shops` until the CLI ships the missing `$retailerSlug` / `$coordinates` vars. For agent-driven interactive use, prefer `--item-id` if you already have the canonical id from `history list`. `carts`, `cart show`, and `cart remove` all work.

**ID-rot fallback (added 2026-05-16):** `cart-build.py` now self-heals when a stored `--item-id` is rejected as `notFoundBasketProduct` (stored historical IDs are RetailerID-prefixed; Instacart's add API requires LocationID-prefixed). On rejection it issues `search <name> --store <retailer> --limit 1`, takes the top hit's fresh `item_id`, and retries the add once. The status JSON records `fallback_attempted`, `fallback_to_item_id`, and `fallback_matched_name` so the path is visible. Result on the 2026-05-16 run: 11 of 12 due items staged via fallback (the one remaining failure was an unrelated `invalidQuantity` from fractional-weight historical orders). Ad-hoc agent-driven `add --item-id` calls do NOT get this fallback — search first if the stored ID is suspect.

## Boundary — read this first

- **The bot builds the cart. Manan places the order on his phone.** The CLI has no `place` / `checkout` action. After cart-build, present a summary; Manan opens the Instacart app and taps "Place Order".
- **Global hard constraint:** Order placement requires the exact phrase `"place the order"` (case-insensitive). Any equivalent ("yes", "do it", "send it", "go ahead") does NOT trigger anything. This skill never places orders anyway, but the constraint applies to any future wiring.
- **Cart-build only.** Past-order ingestion stays on the existing nightly scraper (launchd `com.spratt.instacart-history-scrape`, deterministic Python — see the instacart-orders skill). Don't touch it.

## Prerequisites

- Binary: `~/go/bin/instacart-pp-cli` (also reachable as `instacart-pp-cli` if `~/go/bin` is on PATH).
- Active session: `instacart-pp-cli auth status --json` returns `{"logged_in": true, ...}`. If `logged_in:false`, re-auth (see below).

## Command surface

All commands accept `--json` for machine-readable output and `--dry-run` to preview without firing mutations.

| Command | Purpose |
|---|---|
| `auth status` | Check session health. `logged_in:true` = ok. |
| `auth login` | Read cookies from Chrome (requires Chrome quit first). |
| `auth paste` | Paste a `Cookie` header from DevTools — fallback when `auth login` fails on encrypted-cookie decode (Chrome 146+). |
| `retailers list` | Stores that deliver to the saved address. |
| `search <query> --store <slug>` | Product search at one retailer. |
| `add <retailer> <query>` | Search + add top match to active cart. Use `--qty N`, `--yes` to skip confirmation, `--dry-run` to preview. |
| `add --item-id <id> <retailer>` | Skip search; add exact item by Instacart id. |
| `cart show <retailer>` | List items in active cart at one retailer. |
| `cart remove <retailer> <item-id>` | Remove one line. |
| `carts` | List all active carts across retailers. |

## Auth health

The CLI's session expires when Chrome rotates the cookie (weeks, not hours). Check before any cart-build run:

```bash
instacart-pp-cli auth status --json
```

If `logged_in:false`:

1. Try `instacart-pp-cli auth login` first (quit Chrome, then run). This reads cookies via `kooky`.
2. On `invalid byte ... in Cookie.Value` errors (Chrome 146+ encrypted-cookie bug), fall back to `auth paste`:
   - Manan opens Chrome → instacart.com → DevTools → Network → filter `graphql` → right-click a `www.instacart.com` request → Copy → Copy as cURL.
   - Pipe the cookie value into `instacart-pp-cli auth paste`.

**Outbox alert when stale:** If `auth status` returns `logged_in:false` during a cart-build attempt, send a one-line outbox alert: *"Instacart session expired — open Chrome, log into instacart.com, and re-run auth login."* Don't proceed with cart-build until restored.

## Workflow — interactive cart-build

1. **Check auth.** `auth status --json`. Abort + alert if stale.
2. **Resolve the retailer.** If Manan named a store, use the slug directly (`qfc`, `fred-meyer`, `costco`, `pcc-community-markets`). If unclear, `retailers list --json` to enumerate.
3. **Add items.** For each requested item:
   - Preview with `--dry-run --json` to confirm the top match looks right.
   - Add for real with `--yes`.
   - On ambiguous matches (top result name doesn't match request), use `search --limit 5 --json`, present 2–3 options, ask which.
4. **Review.** `cart show <retailer> --json` and present the summary: item count, line items with names and quantities. If multi-retailer, repeat per retailer and use `carts` for the rollup.
5. **Hand off.** Tell Manan: *"Cart ready at <retailer>. Open the Instacart app to review and place."* Never say "I've placed your order" — the CLI cannot.

## Workflow — programmatic (called from another skill)

Future callers (`recipe-instacart`, `meal-planner`) will invoke this skill with a structured list:

```json
{
  "retailer": "qfc",
  "items": [
    {"query": "whole milk gallon", "qty": 1},
    {"query": "organic eggs 12ct", "qty": 2}
  ]
}
```

For each item:

1. `instacart-pp-cli add <retailer> "<query>" --qty <n> --yes --json` → capture the returned `item_id` and matched name.
2. If `--json` returns an error (no match, ambiguous), record the failure and continue. Don't abort the whole cart on one miss.
3. At end: `cart show <retailer> --json` + return a structured summary: `{added: [...], failed: [...], cart_total: ..., review_url: ...}`.

Programmatic callers MUST end with the human-handoff line. They MUST NOT attempt order placement.

## Safety

- **Never** place orders. The CLI has no such command and this is intentional.
- **Never** modify payment method or delivery address.
- **Never** clear or empty a cart without Manan asking — `cart remove` is for specific item corrections, not bulk wipes.
- **Never** assume a retailer slug — verify with `retailers list` if uncertain.

## Speed

- Direct GraphQL, no browser. Warm calls are sub-second.
- Cache is local; `--verbose` shows the cache layer if debugging.
- Don't loop with `sleep`. The CLI is synchronous and waits for the API response.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `auth status` returns `logged_in:false` | Re-auth (see Auth health). |
| `add` returns HTTP 401 | Same — session corrupted. Re-auth. |
| `add` returns "no match" | Try `search --limit 5` to see alternatives. The query may be too specific or too vague. |
| `cart show` returns empty after `add` | Verify the retailer slug is the active cart — `carts` lists all. |
| Wrong retailer's cart was modified | `add` resolves the active cart for the retailer. Specify `--cart-id` to target a specific cart when multiple exist for one retailer. |
| GraphQL operation hash rejected | `instacart-pp-cli capture` re-seeds the local hash cache after Instacart rotates its bundle. |

## Response style

- Terse. Confirm what was added by exact product name + quantity.
- One short question at a time when ambiguous.
- End every cart-build session with: *"Cart ready at <retailer>. Tap Place Order in the Instacart app when you're ready."*
