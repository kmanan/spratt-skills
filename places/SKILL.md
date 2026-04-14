---
name: places
description: Save a place (restaurant, bar, activity, attraction) from a URL or description to the places database. Use when someone shares a place link, asks to save a spot, asks about saved places, or wants recommendations from saved places.
metadata: {"clawdbot":{"emoji":"📍","requires":{"bins":["sqlite3"]}}}
---

# Places → SQLite

## Saving a place from a URL

### Step 1: Fetch and extract place info

**Instagram URLs (`instagram.com/reel/<ID>/`, `instagram.com/p/<ID>/`):**

1. **Extract the post ID from the URL first.** The ID is the path segment after `/reel/` or `/p/`. Remember it — you'll use it to verify you got the right content.

2. **Try oEmbed first.** It's scoped to the exact URL — no feed-scroll mistake possible:
   ```bash
   curl -sf "https://api.instagram.com/oembed/?url=$URL" | jq '{title, author_name, author_url, thumbnail_url}'
   ```
   Returns caption (`title`), creator handle (`author_name`), and profile URL (`author_url`). If this works, use it directly. Skip to Step 2 of the overall flow.

3. **Verify the oEmbed match.** The `author_url` must contain the handle from the input URL's path (e.g., URL was `instagram.com/<handle>/reel/<ID>/` or the post links back to a profile consistent with the reel). If the handles diverge, discard oEmbed's result and fall through to the browser path.

4. **Browser fallback** (oEmbed returned an error, auth wall, rate limit, or mismatched handle):
   - `openclaw browser navigate "URL"` — uses the default `openclaw` browser profile (logged in). **NEVER pass `profile: "user"`** — it will fail.
   - Wait for page load.
   - `openclaw browser snapshot --format ai` to extract text.
   - **Verify the snapshot matches the URL.** The post ID you extracted in Step 1 must appear in the snapshot output (URL bar, meta, or embedded link). If it doesn't, Instagram's feed scrolled and you're looking at a different reel — navigate to the URL again and re-snapshot. Do NOT proceed with content from a different post.
   - Click "See more" if needed to expand the full description.

5. Extract: place name, location, what kind of place it is, any pricing info.

6. **Do NOT save anything** if you couldn't verify the extracted content against the post ID from Step 1. Reply asking what the place is instead of guessing.

**Facebook / TikTok URLs:** Use the browser fallback path above (Steps 4-6). oEmbed is Instagram-specific here.

7. Do NOT use `web_fetch` for social media — it returns a login wall with no content.

**Google Maps URLs (maps.google.com, goo.gl/maps, maps.app.goo.gl):**
- Use web_fetch or browser tool
- Extract: name, address, category, price range, rating from the page

**Review sites (yelp.com, tripadvisor.com, eater.com, infatuation.com):**
- Use web_fetch to get the page
- Extract: name, location, cuisine/type, price range, highlights

**All other URLs:**
- Use web_fetch to get the page
- Extract whatever place info is available

### Step 2: Save to places database

```bash
sqlite3 ~/places/places.sqlite "INSERT INTO places (name, category, source_url, source_platform, location, cuisine_or_type, tags, price_range, notes, saved_by) VALUES (
  'Place Name',
  'restaurant',
  'https://original-url.com',
  'instagram',
  'Queen West, Toronto',
  'Italian',
  '[\"date-night\", \"outdoor-patio\", \"pasta\"]',
  '\$\$',
  'Wife saw this on Instagram. Known for handmade pasta.',
  'wife'
);"
```

Adjust the database path to wherever you store your places.sqlite.

**Field notes:**
- `category`: restaurant, cafe, bar, brewery, activity, park, attraction, shop, hotel, bakery
- `tags`: JSON array — include vibe, occasion, features (e.g., "date-night", "kid-friendly", "outdoor", "brunch", "group-friendly")
- `source_platform`: instagram, google, yelp, tiktok, website, tripadvisor, eater, manual
- `cuisine_or_type`: for restaurants use cuisine ("Italian", "Japanese"); for activities use type ("climbing gym", "hiking trail", "escape room")
- `location`: neighborhood + city, or full address if available
- `price_range`: $, $$, $$$, $$$$ (leave NULL if unknown)
- `saved_by`: who shared/requested it (manan, wife, etc.)
- `notes`: context about why it was saved, who recommended it, what it's known for

### Step 3: Saving a place from description (no URL)

When someone says "remember that sushi place in Kensington Market" or "save Blue Barn as a brunch spot":
- Ask for any missing critical info (name and rough location at minimum)
- Set `source_platform` to `manual`
- Set `source_url` to NULL

## Querying places

```bash
# Recent saves
sqlite3 ~/places/places.sqlite "SELECT id, name, category, location, cuisine_or_type, created_at FROM places ORDER BY created_at DESC LIMIT 10;"

# Search by name
sqlite3 ~/places/places.sqlite "SELECT id, name, category, location, cuisine_or_type, price_range FROM places WHERE name LIKE '%blue%';"

# Search by category
sqlite3 ~/places/places.sqlite "SELECT id, name, location, cuisine_or_type, price_range FROM places WHERE category = 'restaurant' ORDER BY created_at DESC;"

# Search by cuisine or type
sqlite3 ~/places/places.sqlite "SELECT id, name, location, price_range, tags FROM places WHERE cuisine_or_type LIKE '%italian%';"

# Search by tag
sqlite3 ~/places/places.sqlite "SELECT id, name, category, location FROM places WHERE tags LIKE '%date-night%';"

# Search by location
sqlite3 ~/places/places.sqlite "SELECT id, name, category, cuisine_or_type FROM places WHERE location LIKE '%seattle%';"

# Places we haven't visited
sqlite3 ~/places/places.sqlite "SELECT id, name, category, location, notes FROM places WHERE visited_at IS NULL ORDER BY created_at DESC;"

# Places we've visited and rated
sqlite3 ~/places/places.sqlite "SELECT name, category, location, rating, visited_at FROM places WHERE visited_at IS NOT NULL ORDER BY rating DESC;"

# Get full place details by ID
sqlite3 ~/places/places.sqlite "SELECT * FROM places WHERE id = 5;"
```

## Marking a place as visited

```bash
sqlite3 ~/places/places.sqlite "UPDATE places SET visited_at = datetime('now') WHERE id = ID;"
```

## Rating a place after visiting

```bash
sqlite3 ~/places/places.sqlite "UPDATE places SET rating = 4, visited_at = COALESCE(visited_at, datetime('now')), notes = COALESCE(notes || ' | ', '') || 'Great pasta, noisy on weekends.' WHERE id = ID;"
```

## Recommendation queries

```bash
# Date night restaurants we haven't tried
sqlite3 ~/places/places.sqlite "SELECT name, location, cuisine_or_type, price_range, notes FROM places WHERE tags LIKE '%date-night%' AND category = 'restaurant' AND visited_at IS NULL;"

# Highly rated places to revisit
sqlite3 ~/places/places.sqlite "SELECT name, category, location, rating, notes FROM places WHERE rating >= 4 ORDER BY rating DESC;"

# Weekend activity ideas
sqlite3 ~/places/places.sqlite "SELECT name, cuisine_or_type, location, notes FROM places WHERE category IN ('activity', 'park', 'attraction') AND visited_at IS NULL;"
```

## Schema reference

```sql
places (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  category TEXT,              -- restaurant, cafe, bar, activity, park, attraction, shop
  source_url TEXT,
  source_platform TEXT,       -- instagram, google, yelp, tiktok, website, manual
  location TEXT,              -- neighborhood/city or address
  cuisine_or_type TEXT,       -- cuisine for restaurants, type for activities
  tags TEXT,                  -- JSON array
  price_range TEXT,           -- $, $$, $$$, $$$$
  notes TEXT,
  saved_by TEXT,
  visited_at DATETIME,
  rating INTEGER,             -- 1-5 after visiting
  created_at DATETIME
)
```

## Trigger phrases
- "save this place"
- "remember this restaurant"
- "add this to my places list"
- "where should we go for dinner?"
- "what places has wife saved?"
- "show me date night spots"
- "we went to [place] last night" (mark visited)
- "rate [place] 4 stars"
- "places we haven't been to"
- Any restaurant/place URL shared with a request to save
