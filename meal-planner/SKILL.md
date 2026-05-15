---
name: Meal Planner
slug: meal-planner
version: 1.1.0
description: Plan meals with weekly menus, shopping lists, batch cooking, budget tracking, dietary preferences. Uses recipes.sqlite and feeds shopping into the Instacart pipeline.
metadata: {"clawdbot":{"emoji":"🍽️","requires":{"bins":[]},"os":["linux","darwin","win32"]}}
---

> Based on the [meal-planner](https://clawhub.com/skills/meal-planner) skill by
> **clawic** on ClawHub. Spratt's contribution: rewired to read recipes from
> `recipes.sqlite` (instead of static markdown), feeds shopping lists directly
> into the Instacart API CLI cart-build pipeline, and pulls pantry inventory
> from the orders database for dedup.

## Setup

On first use, read `setup.md` for onboarding guidelines. Start helping naturally without technical jargon.

## When to Use

User wants to plan meals, generate shopping lists, track food budget, organize recipes, coordinate household eating, or reduce food waste.

## Architecture

### Data stores (existing — do NOT create new ones)

- **Recipes:** `~/.config/spratt/db/recipes.sqlite` — structured recipes with JSON ingredients, tags, source URLs. Managed by the `recipe-instacart` skill.
- **Grocery history:** `~/Library/Application Support/instacart/instacart.db` — canonical Instacart purchase history (orders + order_items, retailer-scoped). Owned by `instacart-pp-cli`'s history-scrape daemon. This is the food-shaped data; `orders.sqlite` is general Amazon (electronics, household goods) and is NOT relevant for meal planning.
- **Purchase cadence:** `python3 ~/.config/spratt/infrastructure/instacart/cadence.py` — analyzes reorder timing from Instacart history.

### Meal planner storage (markdown, for plans and preferences)

```
~/.config/spratt/meal-planner/
├── memory.md              # Preferences + dietary info + household
├── weeks/                 # Weekly meal plans
│   └── YYYY-WXX.md
├── inventory/             # What's in pantry/fridge
│   ├── pantry.md
│   └── fridge.md
├── templates/             # Reusable meal templates
│   └── {template-name}.md
└── archive/               # Past weeks for reference
```

**No `recipes/` or `shopping/` subdirectories.** Recipes live in SQLite. Shopping lists are generated inline and fed to the Instacart skill.

## Quick Reference

| Topic | File |
|-------|------|
| Setup process | `setup.md` |
| Memory template | `memory-template.md` |
| Shopping optimization | `shopping-guide.md` |
| Batch cooking | `meal-prep.md` |
| Budget tracking | `budget-tips.md` |

## Core Rules

### 1. Check Memory First
Before any meal planning, read `~/.config/spratt/meal-planner/memory.md` for:
- Dietary restrictions and allergies (critical for safety)
- Household composition (adults, kids, guests)
- Cooking skill level and time constraints
- Budget targets and preferences
- Cuisine preferences and dislikes

### 2. Use Saved Recipes
Before suggesting meals, check recipes.sqlite:
```bash
sqlite3 ~/.config/spratt/recipes/recipes.sqlite "
  SELECT id, name, tags, servings, prep_time, cook_time
  FROM recipes ORDER BY last_made ASC NULLS FIRST
"
```

Prefer saved recipes over inventing new ones — these are recipes the household has already vetted. Reference by DB id in weekly plans.

To find recipes by tag or ingredient:
```bash
sqlite3 ~/.config/spratt/recipes/recipes.sqlite "SELECT id, name FROM recipes WHERE tags LIKE '%dinner%'"
sqlite3 ~/.config/spratt/recipes/recipes.sqlite "SELECT id, name FROM recipes WHERE ingredients LIKE '%chicken%'"
```

### 3. Weekly Planning Rhythm
When user asks to plan meals:
- Check inventory first (avoid buying duplicates)
- Check what recipes exist in the DB
- Balance nutrition across the week
- Cluster similar ingredients (reduce waste)
- Plan leftovers strategically (cook once, eat twice)
- Leave 1-2 flex slots for spontaneity or eating out

### 4. Shopping List → Instacart Pipeline
For each shopping trip, generate the ingredient list:
- Aggregate ingredients across all meals for the week
- Subtract items already in pantry/fridge inventory
- Group by store section (produce, proteins, dairy, pantry)
- Include "linked to meals" annotations (so user knows why each item)

Then offer two paths:
- **Instacart:** "Want me to build this cart on Instacart?" → hand off to the `instacart` skill
- **Manual:** Print the list for in-store shopping

Do NOT save shopping lists as files — they're ephemeral, generated from the weekly plan.

### 5. Dietary Safety
For any dietary restrictions or allergies:
- Flag incompatible recipes BEFORE suggesting
- Check ingredient lists thoroughly
- Suggest substitutions when possible
- Never assume "a little bit is fine"
- Mark severity: preference vs. intolerance vs. allergy (life-threatening)

### 6. Household Coordination
When cooking for multiple people:
- Track individual restrictions per person
- Note kid-friendly vs. adult portions
- Plan meals everyone can eat (or easy modifications)
- Track who likes what (reduce "I don't want that" moments)

### 7. Budget Optimization
| Strategy | Typical Savings | When to Apply |
|----------|-----------------|---------------|
| Seasonal produce | 20-40% | Always check what's in season |
| Batch cooking | 30% time, 15% cost | Busy weeks |
| Protein rotation | 15-25% | Alternate expensive/cheap proteins |
| Pantry meals | 50%+ | End of budget cycle |
| Store brands | 10-30% | Most staples |

## Weekly Plan Format

```markdown
# Week YYYY-WXX

## Overview
- Budget target: $XXX
- Dietary focus: [any theme]
- Special events: [guests, holidays]

## Monday
**Breakfast:** [meal] | Prep: X min
**Lunch:** [meal] | Prep: X min
**Dinner:** [meal] | Prep: X min | Recipe: #ID (Name)

## Tuesday
...

## Batch Prep (Sunday)
- [ ] Cook rice for Mon/Tue/Wed
- [ ] Chop vegetables for week
- [ ] Marinate Thu chicken

## Ingredients Needed
[Generated from meals above, minus inventory on hand]
```

Reference recipes by SQLite ID: `Recipe: #3 (Garlic Lemon Chilli Pasta)`

## Saving New Recipes

When a meal plan includes a new recipe (not in the DB), save it using the recipe-instacart skill's flow:
```bash
sqlite3 ~/.config/spratt/recipes/recipes.sqlite "
  INSERT INTO recipes (name, ingredients, instructions, tags, servings, prep_time, cook_time, saved_by)
  VALUES (?, ?, ?, ?, ?, ?, ?, 'meal-planner')
"
```

Ingredients must be JSON: `[{"name": "chicken breast", "qty": "2 lbs"}, ...]`
Tags must be JSON: `["dinner", "quick", "indian"]`

## Inventory Management

Proactively ask about inventory updates:
- After a grocery delivery: "Your Instacart order just arrived — want to update the pantry?"
- When planning: "Checking pantry — last update was X days ago"
- For staples: track approximate quantities (full, half, low, out)

Check recent purchases to inform inventory. Grocery history is in
`instacart.db` — `orders.sqlite` is general Amazon and not food-shaped, so
ignore it for meal planning:

```bash
sqlite3 "$HOME/Library/Application Support/instacart/instacart.db" "
  SELECT o.placed_at, o.retailer_slug, oi.name, oi.quantity
  FROM order_items oi
  JOIN orders o ON o.order_id = oi.order_id
  ORDER BY o.placed_at DESC LIMIT 50
"
```

## Common Traps

- Planning without checking inventory → duplicate purchases, waste
- Overambitious meal plans → exhaustion, ordering takeout
- Ignoring prep time → not just cook time, total time matters
- Same proteins all week → meal fatigue, nutrition gaps
- No flex meals → rigid plans break under real life
- Forgetting leftovers → food waste
- Not tracking what worked → repeating failures

## Scope

This skill ONLY:
- Manages meal planning in `~/.config/spratt/meal-planner/`
- Reads recipes from `recipes.sqlite`
- Reads grocery purchase history from `instacart.db`
- Generates shopping lists for the Instacart pipeline

This skill NEVER:
- Places orders (that's the instacart skill's job)
- Provides medical nutrition advice
