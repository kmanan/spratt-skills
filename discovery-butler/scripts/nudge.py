#!/usr/bin/env python3
"""Discovery-butler — casual location-aware coffee / quick-bite nudges.

Runs daily at 2pm PT. Most days emits nothing.

At home (no active trip):
  - Manan + Harshita each get one message on Thursday (Friday fallback).
  - One per person per ISO week.

Active trip:
  - One message per trip on day 1 (days 2-3 as fallback).
  - Sent to trips.group_chat_guid if set; otherwise to the first traveler's phone.
  - One per trip, total.

Composition:
  - wanderlust-goat-pp-cli returns ranked picks (deterministic, no LLM).
  - Top pick is dedup'd vs places.sqlite (already-saved spots) and
    today's calendar.
  - OpenClaw infer gateway (openai-codex/gpt-5.5; Flash fallback) wraps
    the pick in one casual butler-voice line. Final fallback is a
    deterministic template — never silent.

Outbox-routed. No --deliver. Errors fire an alert to Manan via outbox.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.expanduser("~/.config/spratt")
DB_DIR = os.path.join(ROOT, "db")
FIRES_DB = os.path.join(DB_DIR, "discovery_fires.sqlite")
PLACES_DB = os.path.join(DB_DIR, "places.sqlite")
TRIPS_DB = os.path.join(DB_DIR, "trips.sqlite")
OUTBOX_CLI = os.path.join(ROOT, "infrastructure/outbox/outbox.py")
WANDERLUST = "/Users/spratt/go/bin/wanderlust-goat-pp-cli"

MANAN_PHONE = "+13157082088"
HARSHITA_PHONE = "+13129330988"  # locked: Wife handle (api_skills.md:480)

HA_PERSON_ENTITY = {
    "manan": "person.manan",
    "harshita": "person.harshita",
}
HA_GEOCODE_ENTITY = {
    "manan": "sensor.geocode_places",
    "harshita": "sensor.harshita_s_geocode",
}
HOME_LAT, HOME_LON, HOME_LABEL = 47.674, -122.122, "Redmond, WA"
STALE_LOCATION_HOURS = 6

GATEWAY_MODEL = "openai-codex/gpt-5.5"
GATEWAY_FALLBACK = "google/gemini-2.5-flash"
GATEWAY_TIMEOUT_S = 30


def ensure_fires_db():
    os.makedirs(DB_DIR, exist_ok=True)
    with sqlite3.connect(FIRES_DB) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fires (
                person TEXT NOT NULL,
                kind TEXT NOT NULL,
                key TEXT NOT NULL,
                sent_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (person, kind, key)
            )
        """)


def already_sent(person, kind, key):
    with sqlite3.connect(FIRES_DB) as conn:
        row = conn.execute(
            "SELECT 1 FROM fires WHERE person=? AND kind=? AND key=?",
            (person, kind, key),
        ).fetchone()
    return row is not None


def mark_sent(person, kind, key):
    with sqlite3.connect(FIRES_DB) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO fires (person, kind, key) VALUES (?, ?, ?)",
            (person, kind, key),
        )
        conn.commit()


def ha_location(person):
    """Return (lat, lon, label). Falls back to home on any error."""
    entity = HA_PERSON_ENTITY.get(person)
    ha_url = os.environ.get("HA_URL", "http://homeassistant.local:8123")
    token = os.environ.get("HA_TOKEN")
    if not entity or not token:
        return HOME_LAT, HOME_LON, HOME_LABEL
    try:
        req = urllib.request.Request(
            f"{ha_url}/api/states/{entity}",
            headers={"Authorization": f"Bearer {token}"},
        )
        data = json.loads(urllib.request.urlopen(req, timeout=5).read())
    except Exception:
        return HOME_LAT, HOME_LON, HOME_LABEL
    state = data.get("state", "")
    attrs = data.get("attributes", {})
    lat, lon = attrs.get("latitude"), attrs.get("longitude")
    if state == "home" or not lat or not lon:
        return HOME_LAT, HOME_LON, HOME_LABEL
    last_updated = data.get("last_updated", "")
    if last_updated:
        try:
            lu = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
            if (datetime.now(lu.tzinfo) - lu).total_seconds() / 3600 > STALE_LOCATION_HOURS:
                return HOME_LAT, HOME_LON, HOME_LABEL
        except Exception:
            pass
    label = HOME_LABEL
    geocode = HA_GEOCODE_ENTITY.get(person)
    if geocode:
        try:
            greq = urllib.request.Request(
                f"{ha_url}/api/states/{geocode}",
                headers={"Authorization": f"Bearer {token}"},
            )
            gdata = json.loads(urllib.request.urlopen(greq, timeout=5).read())
            gstate = (gdata.get("state") or "").strip()
            if gstate and gstate.lower() not in ("home", "unknown", "unavailable"):
                label = gstate
        except Exception:
            pass
    return lat, lon, label


def active_trips(today_iso):
    """Return active trips with day_index in (1, 2, 3) — i.e. eligible for nudge."""
    if not os.path.exists(TRIPS_DB):
        return []
    with sqlite3.connect(TRIPS_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT id, name, destination, start_date, end_date, group_chat_guid,
                   CAST((julianday(?, 'localtime') - julianday(start_date)) + 1 AS INTEGER) AS day_index
            FROM trips
            WHERE status = 'active'
              AND start_date <= ?
              AND end_date   >= ?
        """, (today_iso, today_iso, today_iso)).fetchall()
    return [dict(r) for r in rows if r["day_index"] in (1, 2, 3)]


def travelers_for_trip(trip_id):
    with sqlite3.connect(TRIPS_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT name, phone FROM travelers WHERE trip_id = ? AND phone IS NOT NULL AND phone != ''",
            (trip_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def saved_place_names():
    """Lowercase set of place names already saved — used for dedup."""
    if not os.path.exists(PLACES_DB):
        return set()
    with sqlite3.connect(PLACES_DB) as conn:
        rows = conn.execute("SELECT lower(name) FROM places WHERE name IS NOT NULL").fetchall()
    return {r[0] for r in rows}


def fetch_picks(anchor, criteria="third-wave coffee", minutes=12, top=5):
    """Call wanderlust-goat. Returns list of pick dicts."""
    cmd = [
        WANDERLUST, "goat", anchor,
        "--criteria", criteria,
        "--minutes", str(minutes),
        "--top", str(top),
        "--agent",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        return []
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    return data.get("results") or []


def pick_one(results, exclude_names):
    """Return first result whose name isn't in exclude_names, with business_status OPERATIONAL."""
    for r in results:
        if (r.get("business_status") or "").upper() != "OPERATIONAL":
            continue
        if (r.get("name") or "").strip().lower() in exclude_names:
            continue
        return r
    return None


def compose_line(pick, framing, location_label):
    """Wrap the pick in a casual butler-voice one-liner.

    framing: 'weekend' or 'trip'.
    Tries openai-codex/gpt-5.5 via OpenClaw gateway, falls back to Flash,
    final fallback is a deterministic template — never silent.
    """
    name = pick.get("name", "a spot")
    address = pick.get("address", "")
    walking = pick.get("walking_minutes")
    why = pick.get("why", "")
    walk_str = f"{round(walking)} min walk" if isinstance(walking, (int, float)) else ""

    if framing == "trip":
        opening = f"☕ {location_label} pick"
    else:
        opening = "☕ Weekend coffee idea"

    fallback = f"{opening}: {name}"
    extras = ", ".join(x for x in (walk_str, why) if x)
    if extras:
        fallback += f" — {extras}"
    fallback += ". Walk-in."

    prompt = (
        f"You are Spratt, a household butler. Write ONE casual line "
        f"recommending a {'coffee shop / quick bite while traveling' if framing == 'trip' else 'walk-in coffee shop for the weekend'}.\n"
        f"Pick: {name} — {address}. {why}. {walk_str}.\n"
        f"Open with '{opening}:'. Keep it under 22 words. No emojis other than the leading ☕. "
        f"No reservations talk — this is walk-in. Mention 'walk-in' or 'no reservation needed' once.\n"
        f"Output only the line, no preamble."
    )

    for model in (GATEWAY_MODEL, GATEWAY_FALLBACK):
        try:
            proc = subprocess.run(
                ["openclaw", "infer", "model", "run", "--gateway",
                 "--model", model, "--prompt", prompt, "--json"],
                capture_output=True, text=True, timeout=GATEWAY_TIMEOUT_S,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                continue
            try:
                payload = json.loads(proc.stdout)
                text = (payload.get("text") or payload.get("output")
                        or payload.get("content") or "").strip()
            except json.JSONDecodeError:
                text = proc.stdout.strip()
            if text:
                return text.splitlines()[0].strip()
        except subprocess.TimeoutExpired:
            continue
    return fallback


def outbox_send(to, body, trip_id=None):
    cmd = [
        "python3", OUTBOX_CLI, "schedule",
        "--to", to, "--body", body, "--at", "now",
        "--source", "discovery-butler",
        "--created-by", "discovery-butler",
    ]
    if trip_id:
        cmd += ["--trip-id", trip_id]
    subprocess.run(cmd, check=True, timeout=15)


def alert_manan(reason):
    """Surface failures to Manan's phone — outbox-first observability."""
    try:
        outbox_send(MANAN_PHONE, f"⚠️ discovery-butler: {reason}")
    except Exception:
        pass


def iso_week_key(dt):
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def run_at_home(person, recipient, weekday, week_key, force=False):
    """Returns True iff a nudge was sent."""
    THU, FRI = 3, 4
    if not force and weekday not in (THU, FRI):
        return False
    if not force and already_sent(person, "weekend", week_key):
        return False

    lat, lon, label = ha_location(person)
    anchor = f"{lat},{lon}"
    picks = fetch_picks(anchor)
    if not picks:
        alert_manan(f"no picks for {person} at {label}")
        return False
    chosen = pick_one(picks, saved_place_names())
    if not chosen:
        return False
    line = compose_line(chosen, framing="weekend", location_label=label)
    outbox_send(recipient, line)
    mark_sent(person, "weekend", week_key)
    print(f"sent weekend nudge to {person} ({recipient}): {line}", file=sys.stderr)
    return True


def run_trip(trip, force=False):
    """Returns True iff a nudge was sent for this trip."""
    trip_id = trip["id"]
    chat_target = trip.get("group_chat_guid")
    if not chat_target:
        trav = travelers_for_trip(trip_id)
        if not trav:
            return False
        chat_target = trav[0]["phone"]
    if not force and already_sent(chat_target, "trip", trip_id):
        return False

    destination = trip.get("destination") or trip.get("name")
    if not destination:
        return False
    picks = fetch_picks(destination, criteria="third-wave coffee", minutes=15)
    if not picks:
        alert_manan(f"no picks for trip {trip_id} ({destination})")
        return False
    chosen = pick_one(picks, saved_place_names())
    if not chosen:
        return False
    line = compose_line(chosen, framing="trip", location_label=destination)
    outbox_send(chat_target, line, trip_id=trip_id)
    mark_sent(chat_target, "trip", trip_id)
    print(f"sent trip nudge for {trip_id} → {chat_target}: {line}", file=sys.stderr)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="Bypass weekday and dedup gates (for ad-hoc / testing)")
    ap.add_argument("--person", choices=("manan", "harshita"),
                    help="Force-run for one person (still requires --force to bypass dedup)")
    ap.add_argument("--smoke", action="store_true",
                    help="Compose + print, do NOT touch outbox or fires DB")
    args = ap.parse_args()

    ensure_fires_db()
    now = datetime.now()
    today_iso = now.date().isoformat()
    weekday = now.weekday()
    week_key = iso_week_key(now)

    if args.smoke:
        # Smoke path: hit wanderlust + compose for Manan at home, print only.
        lat, lon, label = ha_location("manan")
        picks = fetch_picks(f"{lat},{lon}")
        chosen = pick_one(picks, saved_place_names()) if picks else None
        if not chosen:
            print("smoke: no usable pick", file=sys.stderr)
            sys.exit(1)
        print(compose_line(chosen, framing="weekend", location_label=label))
        sys.exit(0)

    sent_any = False
    trips_today = active_trips(today_iso)
    travelers_on_trip = set()
    for t in trips_today:
        for trav in travelers_for_trip(t["id"]):
            travelers_on_trip.add((trav.get("phone") or "").strip())
        try:
            sent_any |= run_trip(t, force=args.force)
        except Exception as e:
            alert_manan(f"trip {t.get('id')} failed: {e}")

    pairs = (
        ("manan", MANAN_PHONE),
        ("harshita", HARSHITA_PHONE),
    )
    for person, phone in pairs:
        if args.person and args.person != person:
            continue
        if phone in travelers_on_trip:
            continue
        try:
            sent_any |= run_at_home(person, phone, weekday, week_key, force=args.force)
        except Exception as e:
            alert_manan(f"at-home {person} failed: {e}")

    if not sent_any:
        print("no nudges fired this run", file=sys.stderr)


if __name__ == "__main__":
    main()
