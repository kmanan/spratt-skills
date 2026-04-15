# Destination-Aware Reminders

When you set a destination in your Tesla, a daemon detects it instantly via Home Assistant's WebSocket API, identifies the place type via Google Places, and texts you relevant reminders before you arrive.

**No HA automations. No zones. No polling. No webhooks.** The daemon holds a persistent WebSocket to HA and gets a server-push on every state change of `sensor.maha_tesla_destination`.

## Examples

- **Nav to QFC** → "🛒 Heading to QFC — Dave's Killer Bread, cilantro, paper towels"
- **Nav to daycare** → "🏫 Heading to Bright Horizons — Don't forget: diapers for Sriram"
- **Nav to Work**, reminder "Bring laptop charger to work" → "💼 Heading to Work — Don't forget: Bring laptop charger to work"
- **Nav to Home**, no matching reminder → *(silence)*
- **Nav to friend's house** → *(silence — nothing relevant to surface)*

## The uniform rule

**Destination → matching reminder → text. No match → silence.** Every category branch (grocery, daycare, pharmacy, medical, home, work, restaurant) applies the same rule: if the filter returns nothing, the daemon stays silent. "Silent" is an outcome, never a tag.

## How It Works

```
Tesla nav destination set
        ↓
HA updates sensor.maha_tesla_destination (automatic, Tesla integration)
        ↓
destination-daemon.py (WebSocket subscribe_trigger) detects state change
        ↓
Lookup against known-destinations.json (case-insensitive, longest-key-wins)
  - Hit? Use the local category/name directly; skip goplaces entirely.
    Examples: "Work" → work, "QFC Woodinville" → grocery+pharmacy,
              "Bright Horizons at Woodinville" → daycare.
  - Miss? Fall through ↓
        ↓
Fetches destination coordinates from device_tracker.maha_tesla_route
        ↓
goplaces search (Google Places API) → identifies place type
  - Uses coordinates for location-biased search ("QFC" near you, not across the country)
  - Falls back to "place at ADDRESS" trick for raw addresses
        ↓
Categorizes: grocery | daycare | pharmacy | medical | home | work | restaurant | uncategorized
        ↓
Compose-time relevance filter — stay silent if nothing matches:
  - Grocery → Shared reminder list → Haiku keeps only grocery-cart items
              (excludes "drop off at X", "for Sriram", work todos, etc.)
  - Daycare → All reminder lists → keyword filter (sriram, diaper, bottle,
              blanket, pickup, drop-off, etc.) + the place name
  - Pharmacy → prescription/refill/rx/medication keywords + place name
  - Medical → doctor/appointment/checkup/ask-about keywords + place name
  - Home → "home" keyword + place name
  - Work → "work"/"office" keywords + place name
  - Restaurant → place name only (generic restaurant keywords are too broad)
  - Uncategorized → silent
        ↓
Sends one text via outbox before you arrive
```

Priority: `known-destinations.json` ordering controls it. For QFC with
`["grocery", "pharmacy"]`, grocery fires first (most common). Reorder the
JSON to change precedence.

## Setup

### Prerequisites

- Home Assistant with Tesla integration (`sensor.maha_tesla_destination` entity)
- [goplaces](https://github.com/openclaw/goplaces) CLI with `GOOGLE_PLACES_API_KEY`
- Outbox (for message delivery)
- `remindctl` (for Apple Reminders access)
- Python `websocket-client` library (`pip install --user websocket-client`)
- `ANTHROPIC_API_KEY` — required to filter grocery reminders to actual cart items (uses Haiku). If unset, the grocery branch stays silent instead of dumping every open reminder.
- macOS (for launchd daemon)

### Install

```bash
# 1. Create config file for HA access
cat > ~/.config/home-assistant/config.json << 'EOF'
{
  "url": "http://YOUR_HA_IP:8123",
  "token": "YOUR_LONG_LIVED_ACCESS_TOKEN"
}
EOF

# 2. Edit scripts/destination-daemon.py:
#    - Set MANAN to your phone number
#    - Set CONTEXT_SCRIPT and OUTBOX_CLI paths for your install

# 3. Create launchd plist (see shared/launchd/ for template)
#    IMPORTANT: Include these environment variables in the plist:
#    - PATH: /opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
#    - GOOGLE_PLACES_API_KEY: your key
#    - ANTHROPIC_API_KEY: your key (for the grocery LLM filter)
#      launchd does NOT inherit your shell env (~/.zshrc etc.), so the
#      key must be set inside the plist or the daemon will silently
#      stay silent on every grocery trip.

# 4. Load the daemon
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.spratt.destination-daemon.plist

# 5. First run: macOS will prompt to allow Reminders access — click Allow
```

### Verify

```bash
# Check daemon is running
launchctl list | grep destination

# Check logs
tail -f ~/Library/Logs/spratt/destination-daemon.log

# Test by setting a destination in HA Developer Tools:
# States → sensor.maha_tesla_destination → set to "QFC" → Set State
```

## Deployment Gotchas

These issues were discovered during production deployment and are already handled in the code:

| Issue | Symptom | Fix |
|-------|---------|-----|
| **launchd PATH** | `goplaces` and `remindctl` not found | Set PATH in plist EnvironmentVariables |
| **GOOGLE_PLACES_API_KEY** | goplaces returns empty results | Set API key in plist EnvironmentVariables |
| **sys.executable** | Subprocess calls route to Xcode python instead of /usr/bin/python3 | Hardcode `/usr/bin/python3` in subprocess calls |
| **Reminders authorization** | `remindctl` hangs on first run | macOS prompts for access — click Allow. Added 10s timeout as safety net |
| **Event stream silently wedges** | Process stays alive and TCP is fine, but state_changed events never arrive — a structural zombie | **Triple liveness:** (1) app-layer `{"type":"ping"}` every 30s expecting `pong` within 10s, (2) REST sanity check every 5min comparing `/api/states/<entity>.last_changed` against last WS-delivered timestamp, (3) heartbeat file touched every loop tick — `spratt-health` alerts if stale >120s. Any check failure tears down the socket and reconnects with exponential backoff. |
| **Short place names** | "QFC" without location returns wrong/no result | Fetch destination coords from `device_tracker.maha_tesla_route` for location-biased search |
| **Raw addresses** | Google returns "premise" type, not business | "place at ADDRESS" prefix trick resolves to the business at that address |
| **Grocery trip dumps unrelated todos** | "🛒 Heading to QFC — set up Resy, research AI thing, bring diapers for Sriram…" — unrelated reminders land in the grocery message | `compose_message` grocery branch used to fall back to "first 5 open items from Shared" whenever it couldn't find a dead-code `Shopping list:` section (which doesn't exist on real Reminders setups). Now: parse all open Shared items, pass them through Haiku with a tight prompt ("pick only grocery-cart items; exclude anything tagged to another destination/person or work todos"), and stay silent if the LLM fails or nothing qualifies. Requires `ANTHROPIC_API_KEY` in the plist. |
| **Daycare trip dumps unrelated todos** | Same as above but for "🏫 Heading to Bright Horizons" — daycare trips were first-5-ing every open reminder across Manan/Harshita/Shared | Daycare branch now keyword-filters against a fixed list (`sriram`, `daycare`, `bright horizons`, `diaper`, `bottle`, `blanket`, `pickup`, `drop-off`, `nap`, `formula`, `snack`, `lunch box`, `tuition`, `permission slip`, `sign-in`) plus the resolved place name. Stays silent if nothing matches — no keyword list is used as the "all grocery items" catch-all because keyword filtering for food is endless; only daycare uses it. |
| **Phone numbers hardcoded in scripts** | The daemon and related tools had literal `+1XXX…` strings and a git history that leaked them | All recipient fields route through contacts aliases (e.g. `"Manan"`, `"Wife"`) — `outbox.py::_resolve_recipient` hits `~/.config/spratt/infrastructure/contacts/contacts.sqlite` at send time. No numbers in source. |
| **Generic Tesla favorites ("Work", "Home") resolve to random nearby businesses** | Nav to "Work" produced `🏥 Heading to Any Lab Test Now` — Google Places did a coord-biased search at the work address and returned the nearest categorized business (a national lab chain ~500m away) | `known-destinations.json` is consulted before goplaces. Case-insensitive substring match, longest-key-wins. Hits skip Google entirely and use the local category. "Work"/"Home"/"Office" map to their own categories; nav fires only if a reminder mentions the destination. |
| **Medical/restaurant branches fired unconditionally** | `🏥 Heading to X` + calendar text on every medical classification, even with no relevant reminder or appointment | Every category branch now follows the uniform rule: keyword-filter reminders (or LLM-filter for grocery), stay silent if nothing matches. "Silent" is an outcome, never a destination tag. |

## Why WebSocket, not SSE

The daemon originally used HA's `/api/stream` Server-Sent Events endpoint. On 2026-04-11 at 16:20 the event loop silently stopped dispatching `state_changed` events while the TCP socket, SSE ping frames, and the Python process all stayed healthy. Nothing tripped for three days. On 2026-04-14 morning a Tesla-to-daycare navigation fired no reminder; investigation found the daemon had been a zombie the whole time.

Rewrite principles (now in the code):

1. **WebSocket `subscribe_trigger`** (`/api/websocket`) with a `{"platform":"state","entity_id":<id>}` trigger — the event stream is filtered server-side, so an idle connection is a real signal, not just low traffic.
2. **App-layer liveness**, not transport-layer. Send `{"type":"ping"}` every 30s; if `pong` doesn't arrive in 10s, tear down. TCP keepalive and WebSocket control frames lie about application health.
3. **Cross-source sanity check.** Every 5 minutes, GET `/api/states/<entity>` and compare `last_changed` against the latest timestamp the WebSocket delivered. If REST has seen a change the WebSocket hasn't, reconnect.
4. **Heartbeat file** (`.heartbeat`) touched every main-loop iteration. `spratt-health` reads its mtime; anything older than 120s is a CRITICAL health signal routed to the household via the outbox.
5. **Exponential backoff** (5s → 60s cap) on every reconnect path.

The SSE endpoint is also undocumented/legacy — HA's documented real-time integration path is the WebSocket API, and the WS schema is versioned.

## Files

| File | Purpose |
|------|---------|
| `scripts/destination-daemon.py` | Persistent daemon — WebSocket client, orchestrator |
| `scripts/destination-context.py` | Place resolver + context gatherer (reminders, calendar) |
| `scripts/known-destinations.json.example` | Template mapping of Tesla nav labels to categories. Copy to `known-destinations.json` and edit for your own favorites (Home, Work, local grocery names, daycare, etc.). |
| `SKILL.md` | OpenClaw skill definition for interactive use |
