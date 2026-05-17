#!/usr/bin/env python3
"""For each active/upcoming trip, send weather + 3 wanderlust picks to the trip's iMessage chat.

Deterministic — no LLM. Runs daily via launchd. Output rows are tagged
`wanderlust:<trip-id>:<YYYY-MM-DD>` so the outbox dedupes if the job runs twice.

Routing: uses `trips.group_chat_guid` from trips.sqlite. That column accepts
either a real iMessage chat GUID (`chat_guid:<GUID>`) or a phone number — the
outbox already handles both. If the column is empty, the trip is skipped and
a one-line warning is sent to Manan so he knows to add it.
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime

WEATHER_GOAT = os.path.expanduser("~/go/bin/weather-goat-pp-cli")
WANDERLUST = os.path.expanduser("~/go/bin/wanderlust-goat-pp-cli")
OUTBOX = os.path.expanduser("~/.config/spratt/infrastructure/outbox/outbox.py")
TRIPS_DB = os.path.expanduser("~/.config/spratt/db/trips.sqlite")
SKIP_FILE = os.path.expanduser("~/.config/spratt/skills/wanderlust/state/skip-trips.json")

WMO_CODE_DESC = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow",
    80: "rain showers", 81: "rain showers", 82: "heavy showers",
    95: "thunderstorm", 96: "thunderstorm w/ hail", 99: "thunderstorm w/ hail",
}

DOW_CRITERIA = {
    0: "coffee",        # Mon
    1: "market",        # Tue
    2: "museum",        # Wed
    3: "street food",   # Thu
    4: "bookstore",     # Fri
    5: "viewpoint",     # Sat
    6: "quiet park",    # Sun
}


def geocode(destination):
    """Geocode via weather-goat's working internal geocoder. Returns (lat, lon, label) or None."""
    try:
        r = subprocess.run(
            [WEATHER_GOAT, "config", "set-location", destination, "--dry-run"],
            capture_output=True, text=True, timeout=15,
        )
        m = re.search(
            r"Location saved:\s*(.+?)\s*\(([-\d.]+),\s*([-\d.]+)\)",
            r.stdout or "",
        )
        if m:
            return float(m.group(2)), float(m.group(3)), m.group(1).strip()
    except Exception:
        pass
    return None


def fetch_weather_line(lat, lon, label):
    try:
        r = subprocess.run(
            [WEATHER_GOAT, "forecast",
             "--latitude", str(lat), "--longitude", str(lon),
             "--forecast-days", "1", "--agent"],
            capture_output=True, text=True, timeout=20,
        )
        data = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}
    except Exception:
        return f"{label}: (weather unavailable)"
    res = data.get("results") or {}
    cur = res.get("current", {}) or {}
    daily = res.get("daily", {}) or {}
    temp = cur.get("temperature_2m")
    code = cur.get("weather_code")
    cond = WMO_CODE_DESC.get(code, f"code {code}") if code is not None else "—"
    try:
        hi = daily.get("temperature_2m_max", [None])[0]
        lo = daily.get("temperature_2m_min", [None])[0]
        pop = daily.get("precipitation_probability_max", [None])[0]
    except Exception:
        hi = lo = pop = None
    parts = [cond]
    if temp is not None:
        parts.append(f"{round(temp)}°F")
    if hi is not None and lo is not None:
        parts.append(f"hi {round(hi)} / lo {round(lo)}")
    if pop is not None and pop >= 20:
        parts.append(f"{pop}% precip")
    return f"{label}: " + ", ".join(parts)


def fetch_picks(lat, lon, criteria, top=3):
    try:
        r = subprocess.run(
            [WANDERLUST, "goat", f"{lat},{lon}",
             "--criteria", criteria, "--minutes", "15", "--top", str(top), "--agent"],
            capture_output=True, text=True, timeout=45,
        )
        data = json.loads(r.stdout) if r.returncode == 0 and r.stdout.strip() else {}
    except Exception:
        return []
    out = []
    for item in (data.get("results") or [])[:top]:
        name = item.get("name") or "?"
        walk = item.get("walking_minutes")
        why = item.get("why") or ""
        line = f"- {name}"
        if walk is not None:
            line += f" ({round(walk)} min walk)"
        if why:
            line += f" — {why}"
        out.append(line)
    return out


def schedule_outbox(recipient, body, source, trip_id, dry_run=False):
    if dry_run:
        print(f"--- DRY RUN ---\nTO: {recipient}\nSOURCE: {source}\nTRIP: {trip_id}\n{body}\n")
        return
    subprocess.run(
        ["python3", OUTBOX, "schedule",
         "--to", recipient, "--body", body, "--at", "now",
         "--source", source, "--trip-id", trip_id],
        check=False, timeout=30,
    )


def load_skip_set():
    try:
        with open(SKIP_FILE) as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()
    except Exception:
        return set()


def active_trips():
    conn = sqlite3.connect(TRIPS_DB)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, destination, group_chat_guid, travelers "
        "FROM trips WHERE status IN ('active','upcoming') ORDER BY start_date"
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print what would be sent; don't queue.")
    args = parser.parse_args()

    today = date.today()
    criteria = DOW_CRITERIA[today.weekday()]
    iso_today = today.isoformat()

    skip_ids = load_skip_set()
    trips = active_trips()
    if not trips:
        return

    for trip_id, name, destination, chat_target, travelers in trips:
        if not destination:
            continue

        if not chat_target:
            continue

        if trip_id in skip_ids:
            continue

        geo = geocode(destination)
        if not geo:
            schedule_outbox(
                "Manan",
                f"Wanderlust digest: couldn't geocode '{destination}' for trip '{name}'.",
                f"wanderlust:warning:{trip_id}:{iso_today}",
                trip_id,
                dry_run=args.dry_run,
            )
            continue
        lat, lon, label = geo

        weather = fetch_weather_line(lat, lon, label)
        picks = fetch_picks(lat, lon, criteria)

        lines = [f"🌤 {weather}"]
        if picks:
            lines.append(f"📍 Picks for today ({criteria}, 15-min walk):")
            lines.extend(picks)
        body = "\n".join(lines)

        schedule_outbox(
            chat_target,
            body,
            f"wanderlust:{trip_id}:{iso_today}",
            trip_id,
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"trip-city-digest error: {e}", file=sys.stderr)
        sys.exit(1)
