# Destination-Aware Reminders

When you set a destination in your Tesla, a daemon detects it instantly via Home Assistant's WebSocket API, identifies the place type via Google Places, and texts you relevant reminders before you arrive.

**No HA automations. No zones. No polling. No webhooks.** The daemon holds a persistent WebSocket to HA and gets a server-push on every state change of `sensor.maha_tesla_destination`.

## Examples

- **Nav to QFC** → "🛒 Heading to QFC — Dave's Killer Bread, cilantro, paper towels"
- **Nav to daycare** → "🏫 Heading to Bright Horizons — Don't forget: diapers for Sriram"
- **Nav to doctor** → "🏥 Sriram's checkup — Ask about: solid foods transition"
- **Nav to friend's house** → *(silence — nothing relevant to surface)*

## How It Works

```
Tesla nav destination set
        ↓
HA updates sensor.maha_tesla_destination (automatic, Tesla integration)
        ↓
destination-daemon.py (WebSocket subscribe_trigger) detects state change
        ↓
Fetches destination coordinates from device_tracker.maha_tesla_route
        ↓
goplaces search (Google Places API) → identifies place type
  - Uses coordinates for location-biased search ("QFC" near you, not across the country)
  - Falls back to "place at ADDRESS" trick for raw addresses
        ↓
Categorizes: grocery | daycare | medical | restaurant | unknown
        ↓
Fetches relevant context:
  - Grocery → Shared reminder list only (not personal todos)
  - Daycare → All reminder lists
  - Medical → Calendar events at matching location
  - Unknown → stays silent
        ↓
Sends one text via outbox before you arrive
```

## Setup

### Prerequisites

- Home Assistant with Tesla integration (`sensor.maha_tesla_destination` entity)
- [goplaces](https://github.com/openclaw/goplaces) CLI with `GOOGLE_PLACES_API_KEY`
- Outbox (for message delivery)
- `remindctl` (for Apple Reminders access)
- Python `websocket-client` library (`pip install --user websocket-client`)
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
| **All reminders dumped** | Grocery trip shows "set up Resy" and other irrelevant todos | Grocery destinations only check Shared list, not personal lists |

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
| `SKILL.md` | OpenClaw skill definition for interactive use |
