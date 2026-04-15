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

REMINDER_LISTS = ["Manan", "Harshita", "Shared"]


def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "timeout"
    except Exception as e:
        return 1, "", str(e)


def resolve_destination(destination, lat=None, lng=None):
    """Use goplaces to identify what's at the destination.

    If lat/lng are provided (from Tesla route tracker), uses location-biased
    search first for best results. Falls back to resolve and "place at" trick.
    """
    ADDRESS_ONLY = {"premise", "street_address", "route", "subpremise", "geocode"}
    ALL_CATEGORY_TYPES = GROCERY_TYPES | MEDICAL_TYPES | SCHOOL_TYPES | RESTAURANT_TYPES

    # Step 0: If we have coordinates, search near them (most reliable)
    if lat and lng:
        code, out, _ = run(
            f'/opt/homebrew/bin/goplaces search "{destination}" --json --limit 3 '
            f'--lat={lat} --lng={lng} --radius-m=500'
        )
        if code == 0 and out:
            try:
                # Handle next_page_token appended after JSON array
                json_str = out.split("\nnext_page_token:")[0] if "\nnext_page_token:" in out else out
                places = json.loads(json_str)
                for place in places:
                    types = set(place.get("types", []))
                    if types & ALL_CATEGORY_TYPES:
                        return place
            except json.JSONDecodeError:
                pass

    # Step 1: resolve directly (works when Tesla sends a place name)
    code, out, _ = run(f'/opt/homebrew/bin/goplaces resolve "{destination}" --json --limit 3')
    if code == 0 and out:
        try:
            places = json.loads(out)
            if places:
                for place in places:
                    types = set(place.get("types", []))
                    if types & ALL_CATEGORY_TYPES:
                        return place

                # No category match — check if results are address-only
                first_types = set(places[0].get("types", []))
                if first_types.issubset(ADDRESS_ONLY):
                    # Step 2: "place at" prefix trick for raw addresses
                    code2, out2, _ = run(
                        f'/opt/homebrew/bin/goplaces resolve "place at {destination}" --json --limit 5'
                    )
                    if code2 == 0 and out2:
                        try:
                            places2 = json.loads(out2)
                            for place in places2:
                                types = set(place.get("types", []))
                                if types & ALL_CATEGORY_TYPES:
                                    return place
                        except json.JSONDecodeError:
                            pass

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
        # Grocery: only Shared list (household shopping items)
        lists_to_check = ["Shared"]
    elif "daycare" in categories:
        # Daycare: all lists (could be kid-related items anywhere)
        lists_to_check = REMINDER_LISTS
    else:
        # Everything else: all lists
        lists_to_check = REMINDER_LISTS

    for lst in lists_to_check:
        code, out, _ = run(f"/opt/homebrew/bin/remindctl show all --list {lst}")
        if code == 0 and out and out != "(none)" and "No reminders found" not in out:
            parts.append(f"{lst} list:\n{out}")
    return "\n\n".join(parts) if parts else None


def get_calendar_today():
    """Fetch today's calendar events with locations."""
    code, out, _ = run(
        '/opt/homebrew/bin/icalBuddy -ea -nrd -eed -eep "notes,url,uid" '
        '-ic "manankakkar@gmail.com,Kakkar\\, Manan K,Calendar,For Family,Family" '
        'eventsToday'
    )
    return out if code == 0 and out and out != "(none)" else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", required=True, help="Destination address from Tesla nav")
    parser.add_argument("--lat", type=float, default=None, help="Destination latitude")
    parser.add_argument("--lng", type=float, default=None, help="Destination longitude")
    parser.add_argument("--known-name", default=None, help="If set with --known-categories, skip goplaces and use these values")
    parser.add_argument("--known-categories", default=None, help="Comma-separated category list (e.g. grocery,pharmacy)")
    args = parser.parse_args()

    destination = args.destination

    if args.known_name and args.known_categories:
        # Known destination — skip goplaces entirely.
        place_name = args.known_name
        place_address = destination
        place_types = []
        categories = [c.strip() for c in args.known_categories.split(",") if c.strip()]
    else:
        place = resolve_destination(destination, lat=args.lat, lng=args.lng)
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
