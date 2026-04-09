# Destination-Aware Reminders

When you set a destination in your Tesla, a daemon detects it instantly via Home Assistant's SSE stream, identifies the place type via Google Places, and texts you relevant reminders before you arrive.

**No HA automations. No zones. No polling. No webhooks.** The daemon subscribes directly to HA's event stream and reacts when `sensor.maha_tesla_destination` changes.

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
destination-daemon.py (SSE listener) detects state change
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
| **SSE stream goes stale** | Daemon stops receiving events after hours | 120s heartbeat check — reconnects if no data |
| **Short place names** | "QFC" without location returns wrong/no result | Fetch destination coords from `device_tracker.maha_tesla_route` for location-biased search |
| **Raw addresses** | Google returns "premise" type, not business | "place at ADDRESS" prefix trick resolves to the business at that address |
| **All reminders dumped** | Grocery trip shows "set up Resy" and other irrelevant todos | Grocery destinations only check Shared list, not personal lists |

## Files

| File | Purpose |
|------|---------|
| `scripts/destination-daemon.py` | Persistent daemon — SSE listener, orchestrator |
| `scripts/destination-context.py` | Place resolver + context gatherer (reminders, calendar) |
| `SKILL.md` | OpenClaw skill definition for interactive use |
