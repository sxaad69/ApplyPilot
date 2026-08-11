#!/bin/bash
# Discovery service: run discovery for up to DISCOVERY_DURATION_MINUTES (default 30),
# then exit. Throttled via DISCOVERY_QUERY_DELAY. Called by launchd every 8h.
set -euo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"
export DISCOVERY_DURATION_MINUTES="${DISCOVERY_DURATION_MINUTES:-30}"
export DISCOVERY_QUERY_DELAY="${DISCOVERY_QUERY_DELAY:-12}"

APP_DIR="/Users/user/applypilot/ApplyPilot"
LOG_DIR="$HOME/.applypilot/logs"
mkdir -p "$LOG_DIR"

# Load .env safely (handles unquoted spaces in SEARCH_KEYWORDS etc.)
source "$APP_DIR/scripts/lib.sh"
load_env

echo "[$(date)] discovery: starting (max ${DISCOVERY_DURATION_MINUTES} min, delay ${DISCOVERY_QUERY_DELAY}s)"
"$APP_DIR/.venv/bin/applypilot" run discover -w 2 \
  >> "$LOG_DIR/discovery.log" 2>&1 &
DISCOVERY_PID=$!

# Watchdog: kill discovery after the duration window.
( sleep "$((DISCOVERY_DURATION_MINUTES * 60))"; kill "$DISCOVERY_PID" 2>/dev/null || true; echo "[$(date)] discovery: duration window ended, stopped" >> "$LOG_DIR/discovery.log" ) &
WATCHDOG_PID=$!
wait "$DISCOVERY_PID"
kill "$WATCHDOG_PID" 2>/dev/null || true
echo "[$(date)] discovery: done" >> "$LOG_DIR/discovery.log"
