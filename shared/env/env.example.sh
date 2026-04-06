#!/bin/bash
# Spratt Skills — Environment Variables
# Copy to env.sh and fill in your values. chmod 600 env.sh.
# Sourced by all launchd plists and scripts.

export ANTHROPIC_API_KEY=""        # Required for trip manifest extraction (Haiku)
export GEMINI_API_KEY=""           # Optional — for email scanning cron (Flash)
export GOOGLE_API_KEY=""           # Optional — same key, some tools use this name
export FLIGHTAWARE_API_KEY=""      # Optional — for enhanced flight data
