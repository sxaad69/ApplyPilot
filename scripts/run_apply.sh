#!/bin/bash
# Apply service: continuously apply to prepared jobs via Hermes+Playwright.
# Runs forever (launchd KeepAlive). Fully automatic. Polls every 3 min.
set -euo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH"
APP_DIR="/Users/user/applypilot/ApplyPilot"
LOG_DIR="$HOME/.applypilot/logs"
mkdir -p "$LOG_DIR"

source "$APP_DIR/scripts/lib.sh"
load_env

# Ensure the logged-in CDP Chrome is running for the apply browser session.
if ! curl -s http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
  mkdir -p "$HOME/.hermes/chrome-debug"
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    --remote-debugging-port=9222 \
    --user-data-dir="$HOME/.hermes/chrome-debug" \
    --no-first-run --no-default-browser-check >/dev/null 2>&1 &
  sleep 5
fi

echo "[$(date)] apply: starting continuous loop"
while true; do
  echo "[$(date)] apply: pass start"
  "$APP_DIR/.venv/bin/applypilot" apply --limit 5 \
    >> "$LOG_DIR/apply.log" 2>&1
  echo "[$(date)] apply: pass done, sleeping 180s"
  sleep 180
done
