#!/usr/bin/env python3
"""
Flight tracker — uses FlightAware AeroAPI.

Accepts normal IATA booking flight numbers: AS3, UA4560, AA100.

Usage:
  track_flight.py AS3              # Track by flight number
  track_flight.py AS3 --json       # Raw JSON output
  track_flight.py AS3 UA4560       # Track multiple flights

Returns: status, times, gates, terminals, position, delays.
Returns exit code 1 if flight not found.

API key: set FLIGHTAWARE_API_KEY in environment.
"""

import sys
import os
import json
import urllib.request
import urllib.error

AEROAPI_URL = "https://aeroapi.flightaware.com/aeroapi"
API_KEY = os.environ.get("FLIGHTAWARE_API_KEY", "")


def track_flight(flight_number):
    if not API_KEY:
        return {"error": "FLIGHTAWARE_API_KEY not set", "flight": flight_number}

    # Normalize: strip spaces, uppercase
    fn = flight_number.strip().upper().replace(" ", "")

    url = f"{AEROAPI_URL}/flights/{fn}"

    try:
        req = urllib.request.Request(url, headers={
            "x-apikey": API_KEY,
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"error": "not_found", "flight": fn}
        return {"error": f"API error: HTTP {e.code}", "flight": fn}
    except Exception as e:
        return {"error": f"API error: {e}", "flight": fn}

    flights = data.get("flights", [])
    if not flights:
        return {"error": "not_found", "flight": fn}

    # Pick the most relevant flight from the list (FlightAware returns past + future).
    # Priority: 1) en-route  2) today's scheduled/delayed  3) most recently departed not yet arrived
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc)
    today_str = now.strftime("%Y-%m-%d")

    best = None

    # Pass 1: in the air right now
    for f in flights:
        status = (f.get("status") or "").lower()
        if "en route" in status or (f.get("actual_off") and not f.get("actual_on")):
            best = f
            break

    # Pass 2: scheduled or delayed today (not yet departed, not arrived)
    if not best:
        for f in flights:
            status = (f.get("status") or "").lower()
            sched = f.get("scheduled_out") or ""
            if sched.startswith(today_str) and "arrived" not in status:
                best = f
                break

    # Pass 3: closest future flight
    if not best:
        for f in flights:
            sched = f.get("scheduled_out") or ""
            try:
                sched_dt = _dt.fromisoformat(sched.replace("Z", "+00:00"))
                if sched_dt >= now:
                    best = f
                    break
            except Exception:
                continue

    # Pass 4: most recent (first in list)
    if not best:
        best = flights[0]

    r = best

    # Parse position from AeroAPI (only available via separate endpoint for in-flight)
    altitude = 0
    speed = 0
    lat = None
    lng = None
    on_ground = False

    progress = r.get("progress_percent", 0)
    if r.get("actual_on"):
        on_ground = True
    elif r.get("actual_off") and not r.get("actual_on"):
        # In the air — try to get position
        fa_id = r.get("fa_flight_id")
        if fa_id:
            try:
                pos_url = f"{AEROAPI_URL}/flights/{fa_id}/position"
                pos_req = urllib.request.Request(pos_url, headers={
                    "x-apikey": API_KEY,
                    "Accept": "application/json",
                })
                with urllib.request.urlopen(pos_req, timeout=10) as pos_resp:
                    pos_data = json.loads(pos_resp.read().decode("utf-8"))
                last = pos_data.get("last_position") or {}
                altitude = int(last.get("altitude", 0)) * 100  # FL -> feet
                speed = last.get("groundspeed", 0)
                lat = last.get("latitude")
                lng = last.get("longitude")
            except Exception:
                pass

    dep_delay_sec = r.get("departure_delay", 0) or 0
    arr_delay_sec = r.get("arrival_delay", 0) or 0
    dep_delay = dep_delay_sec // 60 if abs(dep_delay_sec) > 60 else dep_delay_sec
    arr_delay = arr_delay_sec // 60 if abs(arr_delay_sec) > 60 else arr_delay_sec

    status_text = r.get("status", "unknown")

    # Prefer the IATA ident the user searched for (codeshare-aware)
    flight_iata = r.get("ident_iata") or fn
    codeshares_iata = r.get("codeshares_iata") or []
    if flight_iata != fn and fn in codeshares_iata:
        flight_iata = fn

    return {
        "flight": flight_iata,
        "callsign": r.get("ident_icao"),
        "status": status_text,
        "origin": {
            "airport": r.get("origin", {}).get("name"),
            "iata": r.get("origin", {}).get("code_iata", "?"),
            "terminal": r.get("terminal_origin"),
            "gate": r.get("gate_origin"),
        },
        "destination": {
            "airport": r.get("destination", {}).get("name"),
            "iata": r.get("destination", {}).get("code_iata", "?"),
            "terminal": r.get("terminal_destination"),
            "gate": r.get("gate_destination"),
            "baggage": r.get("baggage_claim"),
        },
        "times": {
            "scheduled_departure": r.get("scheduled_out"),
            "actual_departure": r.get("actual_out") or r.get("estimated_out"),
            "scheduled_arrival": r.get("scheduled_in"),
            "estimated_arrival": r.get("estimated_in") or r.get("actual_in"),
        },
        "position": {
            "altitude": altitude,
            "speed": speed,
            "on_ground": on_ground,
            "latitude": lat,
            "longitude": lng,
        },
        "delay": {
            "departure_minutes": dep_delay,
            "arrival_minutes": arr_delay,
        },
        "progress_percent": progress,
        "aircraft": {
            "model": r.get("aircraft_type"),
            "registration": r.get("registration"),
        },
    }


def format_flight(data):
    if "error" in data:
        if data["error"] == "not_found":
            return f"Flight {data['flight']} not found."
        return f"Error tracking {data['flight']}: {data['error']}"

    o = data['origin']
    d = data['destination']
    t = data['times']
    p = data['position']
    delay = data.get('delay', {})

    lines = []
    lines.append(f"\u2708\ufe0f {data['flight']} \u2014 {data['status']}")

    dep_delay = delay.get('departure_minutes', 0)
    arr_delay = delay.get('arrival_minutes', 0)
    if dep_delay and dep_delay > 0:
        lines.append(f"Warning: Departure delayed {dep_delay} min")
    if arr_delay and arr_delay > 0:
        lines.append(f"Warning: Arrival delayed {arr_delay} min")

    lines.append(f"Departure {o['iata']}: gate {o.get('gate') or '?'}, terminal {o.get('terminal') or '?'}")
    if t['actual_departure']:
        lines.append(f"   Departed {t['actual_departure']}")
    elif t['scheduled_departure']:
        lines.append(f"   Scheduled {t['scheduled_departure']}")

    lines.append(f"Arrival {d['iata']}: terminal {d.get('terminal') or '?'}, gate {d.get('gate') or '?'}")
    if t['estimated_arrival']:
        lines.append(f"   ETA {t['estimated_arrival']}")

    if p.get('latitude') and not p['on_ground'] and p['altitude'] > 0:
        lines.append(f"Position: {p['altitude']:,}ft, {p['speed']}kt")

    progress = data.get('progress_percent', 0)
    if progress > 0:
        lines.append(f"Progress: {progress}% complete")

    if d.get('baggage'):
        lines.append(f"Baggage: belt {d['baggage']}")

    return '\n'.join(lines)


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: track_flight.py <flight_number> [--json]", file=sys.stderr)
        sys.exit(1)

    use_json = '--json' in sys.argv
    flight_numbers = [a for a in sys.argv[1:] if not a.startswith('-')]

    any_not_found = False
    for fn in flight_numbers:
        result = track_flight(fn)
        if use_json:
            print(json.dumps(result, indent=2))
        else:
            print(format_flight(result))
            if len(flight_numbers) > 1:
                print()
        if result.get('error') == 'not_found':
            any_not_found = True

    sys.exit(1 if any_not_found else 0)
