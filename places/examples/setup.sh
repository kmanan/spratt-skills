#!/bin/bash
# Quick setup for the places database

PLACES_DIR="${PLACES_DIR:-$HOME/.config/spratt/places}"

mkdir -p "$PLACES_DIR"

if [ -f "$PLACES_DIR/places.sqlite" ]; then
    echo "places.sqlite already exists at $PLACES_DIR/places.sqlite"
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
sqlite3 "$PLACES_DIR/places.sqlite" < "$SCRIPT_DIR/schemas/places.sql"
echo "Created places.sqlite at $PLACES_DIR/places.sqlite"
