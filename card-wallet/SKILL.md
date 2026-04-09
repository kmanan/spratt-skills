---
name: card-wallet
description: Track credit card benefits (use-it-or-lose-it credits) and optimize which card to use per purchase category. Manages both household cardholders.
---

# Card Wallet

Two functions in one skill:
1. **Benefit tracking** — expiring credits, Saturday cron reminders, mark used/skipped
2. **Purchase optimizer** — "which card for groceries?" based on reward rates, caps, and network acceptance

## Database

`~/.config/spratt/cards/cards.sqlite`

Tables: `cards`, `benefits`, `usage`, `benefit_changes`, `reward_rates`, `quarterly_categories`, `spending_estimates`

---

## Part 1: Benefit Tracking

### Check what's pending for someone

```bash
sqlite3 ~/.config/spratt/cards/cards.sqlite "
  SELECT b.name, b.amount, b.cycle, u.period_key, c.card_name
  FROM usage u
  JOIN benefits b ON u.benefit_id = b.id
  JOIN cards c ON b.card_id = c.id
  WHERE u.status = 'pending' AND c.holder = 'manan'
  ORDER BY u.period_key
"
```

### Mark a benefit as used

When someone says "used the Uber credit" or "used Saks", match the benefit by name and mark the current period:

```bash
sqlite3 ~/.config/spratt/cards/cards.sqlite "
  UPDATE usage SET status = 'used', acknowledged_at = datetime('now')
  WHERE benefit_id = (
    SELECT b.id FROM benefits b JOIN cards c ON b.card_id = c.id
    WHERE b.name LIKE '%Uber%' AND c.holder = 'manan' AND b.active = 1
  )
  AND period_key = '2026-04'
  AND status = 'pending'
"
```

Confirm back: "Marked AMEX Uber Cash as used for April."

### Mark a benefit as skipped

```bash
sqlite3 ~/.config/spratt/cards/cards.sqlite "
  UPDATE usage SET status = 'skipped', acknowledged_at = datetime('now')
  WHERE benefit_id = ? AND period_key = ? AND status = 'pending'
"
```

### Deactivate a benefit

```bash
sqlite3 ~/.config/spratt/cards/cards.sqlite "
  UPDATE benefits SET active = 0, updated_at = datetime('now') WHERE id = ?
"
```

### Matching user intent

Users will say things loosely. Match by keyword against benefit name and card name:
- "used uber" → Uber Cash benefit on AMEX Platinum
- "used the saks credit" → Saks Fifth Avenue on AMEX Platinum
- "used doordash" → could be Chase Sapphire Reserve ($5/mo) or Chase Freedom ($10/qtr) — ask which
- "activated chase freedom" → 5% Rotating Categories, mark as used for current quarter
- "skip hotel credit" → Hotel Credit (FHR/THC), mark as skipped

Always compute the correct `period_key` from today's date:
- Monthly: `YYYY-MM` (e.g., `2026-04`)
- Quarterly: `YYYY-Q#` (e.g., `2026-Q2`)
- Semi-annual: `YYYY-H#` (e.g., `2026-H1`)
- Annual: `YYYY` (e.g., `2026`)

---

## Part 2: Purchase Optimizer

### "Which card for X?"

When someone asks which card to use for a purchase:

1. **Map to category.** Match the merchant/purchase to a reward category:
   - Groceries/supermarket → `groceries` (not tracked yet — use Apple Pay 2% fallback)
   - Restaurant/dining/eating out → `dining`
   - Gas/fuel → `gas`
   - Amazon/Whole Foods → `amazon`
   - Travel/flights/hotels → `travel`, `flights_direct`, `hotels_amex_travel`, etc.
   - Apple Store/subscriptions → `apple`
   - Drug store/pharmacy → `drugstores`
   - Streaming services → may be a quarterly rotating category

2. **Query reward rates:**
   ```sql
   SELECT c.card_name, c.network, r.rate, r.cap_amount, r.rate_after_cap,
          c.point_valuation_cpp, c.holder
   FROM reward_rates r
   JOIN cards c ON r.card_id = c.id
   WHERE r.category = :category AND c.active = 1 AND c.holder = :holder
   ORDER BY (r.rate * COALESCE(c.point_valuation_cpp, 1.0) / 100.0) DESC
   ```

3. **Check quarterly categories** for Chase Freedom Flex:
   ```sql
   SELECT categories, activated FROM quarterly_categories
   WHERE card_id = 3 AND year = :year AND quarter = :quarter
   ```
   If the purchase category matches this quarter's rotating categories AND the user has activated, Chase Freedom earns 5%.

4. **Cap awareness.** If the top card has a cap (`cap_amount` is not null), note it. If near cap exhaustion, recommend the next best card.

5. **Network acceptance.** If top card is AMEX (`network = 'amex'`), warn about acceptance and provide the best Visa/Mastercard fallback:
   - "Use AMEX Platinum for 5x on flights. If Amex isn't accepted, fall back to Chase Sapphire Reserve for 3x."
   - Costco: Visa only — never recommend AMEX for Costco.

6. **Apple Pay fallback.** For any purchase where the merchant accepts Apple Pay and no card earns more than 2%, the Apple Card at 2% via Apple Pay is the best default.

### Category matching

Map fuzzy user language to categories:
- "groceries", "grocery store", "supermarket", "Kroger", "QFC", "Safeway" → groceries
- "food", "restaurant", "eating out", "dinner" → dining
- "gas", "fuel", "charging", "EV" → gas
- "Amazon", "Whole Foods" → amazon
- "pharmacy", "CVS", "Walgreens", "Rite Aid" → drugstores
- "Costco" → special: Visa only, no AMEX. Check if quarterly rotating includes warehouse.
- "Apple Store", "iCloud", "Apple subscription" → apple
- "travel", "flight", "hotel", "Airbnb", "car rental" → travel (then check specific sub-categories)

### Add a new card

```bash
sqlite3 ~/.config/spratt/cards/cards.sqlite "
  INSERT INTO cards (holder, card_name, issuer, network, annual_fee, reward_type, point_valuation_cpp)
  VALUES ('manan', 'New Card', 'issuer', 'visa', 0, 'cashback', NULL)
"
```
Then add reward_rates for each category the card earns on.

### Add reward rates

```bash
sqlite3 ~/.config/spratt/cards/cards.sqlite "
  INSERT INTO reward_rates (card_id, category, rate, cap_amount, cap_period, rate_after_cap)
  VALUES (?, 'groceries', 4.0, 25000, 'yearly', 1.0)
"
```

---

## Part 3: Quarterly Management

The `quarterly_categories` table tracks Chase Freedom Flex (and any future rotating-category cards) per quarter.

### Check current quarter

```sql
SELECT categories, activated FROM quarterly_categories
WHERE card_id = 3 AND year = 2026 AND quarter = 2
```

### Activate a quarter

When user says "activated chase freedom":
```sql
UPDATE quarterly_categories SET activated = 1, activated_at = datetime('now')
WHERE card_id = 3 AND year = 2026 AND quarter = 2
```
Also mark the benefit as used in the usage table (same as before).

### Add next quarter's categories

The quarterly cron (1st of Jan/Apr/Jul/Oct) searches the web and inserts:
```sql
INSERT INTO quarterly_categories (card_id, year, quarter, categories)
VALUES (3, 2026, 3, '["gas", "ev_charging", "select_streaming"]')
```

---

## Part 4: Annual Fee ROI (on request)

Only when user asks "is this card worth keeping?" or "card ROI":

1. Read `spending_estimates` for monthly spend per category.
2. For the target card, calculate annual bonus rewards vs. a 2% flat baseline:
   - For each category: `(card_rate - 2%) × annual_spend × point_value_cpp / 100`
   - Sum all categories = bonus value
   - Net value = bonus value - annual_fee
3. If net value < 0, suggest downgrading.

Populate spending estimates only when user provides them:
```sql
INSERT OR REPLACE INTO spending_estimates (category, monthly_amount) VALUES ('dining', 500);
```

---

## Tone

You are Spratt. These are the household's finances — handle them with quiet competence and your usual dry wit. Examples:

- Marking used: "Very good, sir. The Saks credit is accounted for — $50 well spent, one hopes. 🧾"
- Marking skipped: "The hotel credit shall go unclaimed this year. A pity, but duly noted. 📝"
- Checking status: "Your current obligations, sir:" followed by a tidy list (💸 pending, ✅ used, ⏭️ skipped)
- Purchase recommendation: "For groceries, sir: Apple Card via Apple Pay at 2%. None of your cards offer a dedicated grocery rate, I'm afraid — though if the Freedom's quarterly categories include groceries, that changes the calculus considerably."
- Ambiguous match: "You have DoorDash credits on two cards — the Sapphire Reserve ($5/mo) and the Freedom ($10/qtr). Which shall I mark, sir?"

Use emojis sparingly. Never gush.

## Important

- NEVER DELETE rows from usage. Only UPDATE status.
- If no pending row exists for the current period, INSERT one first, then update it.
- The `auto_applied` benefits (Walmart+, CLEAR) don't get usage rows from the Saturday check. If someone asks about them, they're auto-applied — just confirm.
- When adding a new card, always add both benefit rows AND reward_rate rows.
- Multi-holder: always check `c.holder` when querying. Manan has 5 cards, Harshita has 1.
