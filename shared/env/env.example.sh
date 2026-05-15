#!/bin/bash
# Spratt Skills — Environment Variables
# Copy to env.sh and fill in your values. chmod 600 env.sh.
#
# This file is sourced by every launchd plist via the wrapper pattern:
#     /bin/zsh -c "source /path/to/env.sh && /usr/bin/python3 /path/to/script.py"
#
# IMPORTANT: launchd does NOT source ~/.zshrc or ~/.zshenv. Any env var your
# scripts need at cron time MUST be exported here. A key that works in your
# interactive shell will silently fail under launchd otherwise — the wrapped
# tool (e.g. wanderlust-goat-pp-cli) typically exits nonzero with stderr like
# "Error: GOOGLE_PLACES_API_KEY is not set", which Python subprocess.run
# swallows into an empty result list. You will not see this in the log unless
# your script prints stderr/rc explicitly.

export ANTHROPIC_API_KEY=""        # Required for trip manifest extraction (Haiku)
export GEMINI_API_KEY=""           # Optional — for email scanning cron (Flash)
export GOOGLE_API_KEY=""           # Optional — same key, some tools use this name
export GOOGLE_PLACES_API_KEY=""    # Required for discovery-butler + destination-aware (wanderlust-goat-pp-cli / goplaces)
export FLIGHTAWARE_API_KEY=""      # Optional — for enhanced flight data
