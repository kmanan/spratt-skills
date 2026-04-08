#!/usr/bin/env python3
"""Gather context for a Tesla navigation destination.

Called when sensor.maha_tesla_destination changes in Home Assistant.
Resolves the destination via goplaces, checks reminders, calendar, and
outputs JSON for the compose step.

Usage:
    python3 destination-context.py --destination "14811 226th Ave NE, Woodinville WA"
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime

GROCERY_TYPES = {"grocery_store", "supermarket", "market", "food_store", "warehouse_store", "health_food_store"}
MEDICAL_TYPES = {"doctor", "dentist", "hospital", "medical_lab", "pharmacy", "physiotherapist"}
SCHOOL_TYPES = {"school", "preschool", "child_care_agency", "day_care"}
RESTAURANT_TYPES = {"restaurant", "cafe", "bar", "meal_delivery", "meal_takeaway"}

REMINDER_LISTS = ["Manan", "Shared", "Shopping"]


def run(cmd):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def resolve_destination(destination):
    """Use goplaces to identify what's at the destination.

    Tries two approaches:
    1. goplaces resolve — works when Tesla sends a place name ("QFC", "Bright Horizons")
    2. goplaces search — works when Tesla sends an address, searches for the business there

    If neither finds a real business (only address types), returns the
    address-only result. The daemon will stay silent for unrecognized places.
    """
    ADDRESS_ONLY = {"premise", "street_address", "route", "subpremise", "geocode"}

    # Approach 1: resolve directly
    code, out, _ = run(f'goplaces resolve "{destination}" --json --limit 3')
    if code == 0 and out:
        try:
            places = json.loads(out)
            if places:
                # Check all results — sometimes the business is result 2 or 3
                for place in places:
                    types = set(place.get("types", []))
                    if types and not types.issubset(ADDRESS_ONLY):
                        return place

                # All results were addresses — try search as fallback
                loc = places[0].get("location", {})
                lat, lng = loc.get("lat"), loc.get("lng")
                if lat and lng:
                    # Approach 2: search with destination text near the coordinates
                    code2, out2, _ = run(
                        f'goplaces search "{destination}" --json --limit 1 '
                        f'--lat={lat} --lng={lng} --radius-m=100'
                    )
                    if code2 == 0 and out2:
                        try:
                            # goplaces search may include next_page_token after the array
                            search_results = json.loads(out2.split("\n")[0]) if "\nnext_page_token:" in out2 else json.loads(out2)
                            if search_results:
                                candidate = search_results[0]
                                ctypes = set(candidate.get("types", []))
                                if not ctypes.issubset(ADDRESS_ONLY):
                                    return candidate
                        except json.JSONDecodeError:
                            pass

                # Return address-only result as last resort
                return places[0]
        except json.JSONDecodeError:
            pass

    return None


def categorize(place_types):
    """Determine primary destination category from Google Places types.

    Returns the most specific match — grocery beats restaurant,
    medical/daycare beat everything else.
    """
    types_set = set(place_types or [])
    # Priority order: most specific first
    if types_set & SCHOOL_TYPES:
        return ["daycare"]
    if types_set & MEDICAL_TYPES:
        return ["medical"]
    if types_set & GROCERY_TYPES:
        return ["grocery"]
    if types_set & RESTAURANT_TYPES:
        return ["restaurant"]
    return []


def get_reminders(categories):
    """Fetch relevant reminders based on destination category."""
    parts = []
    if "grocery" in categories:
        # Shopping list is the primary grocery list
        code, out, _ = run("remindctl show all --list Shopping")
        if code == 0 and out and out != "(none)":
            parts.append(f"Shopping list:\n{out}")
    # Always check personal and shared lists for any relevant items
    for lst in REMINDER_LISTS:
        code, out, _ = run(f"remindctl show all --list {lst}")
        if code == 0 and out and out != "(none)":
            parts.append(f"{lst} list:\n{out}")
    return "\n\n".join(parts) if parts else None


def get_calendar_today():
    """Fetch today's calendar events with locations."""
    code, out, _ = run(
        'icalBuddy -ea -nrd -eed -eep "notes,url,uid" '
        '-ic "manankakkar@gmail.com,Kakkar\\, Manan K,Calendar,For Family,Family" '
        'eventsToday'
    )
    return out if code == 0 and out and out != "(none)" else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", required=True, help="Destination address from Tesla nav")
    args = parser.parse_args()

    destination = args.destination

    # Step 1: Resolve destination
    place = resolve_destination(destination)

    place_name = place.get("name", "Unknown") if place else "Unknown"
    place_address = place.get("address", destination) if place else destination
    place_types = place.get("types", []) if place else []
    categories = categorize(place_types)

    # Step 2: Gather context based on category
    data = {
        "destination": destination,
        "place_name": place_name,
        "place_address": place_address,
        "place_types": place_types[:5],  # top 5 for context
        "categories": categories,
        "timestamp": datetime.now().isoformat(),
    }

    # Reminders
    reminders = get_reminders(categories)
    if reminders:
        data["reminders"] = reminders

    # Calendar — check for events at matching location
    calendar = get_calendar_today()
    if calendar:
        data["calendar_today"] = calendar

    json.dump(data, sys.stdout)


if __name__ == "__main__":
    main()
