#!/bin/bash
# Processing service: continuously score -> tailor -> cover -> pdf new jobs.
# Runs forever (launchd KeepAlive). Polls every 2 min.
set -euo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"
APP_DIR="/Users/user/applypilot/ApplyPilot"
LOG_DIR="$HOME/.applypilot/logs"
mkdir -p "$LOG_DIR"

source "$APP_DIR/scripts/lib.sh"
load_env

echo "[$(date)] processing: starting continuous loop"
while true; do
  echo "[$(date)] processing: pass start"
  "$APP_DIR/.venv/bin/applypilot" run score tailor cover pdf -w 3 \
    >> "$LOG_DIR/processing.log" 2>&1
  echo "[$(date)] processing: pass done, sleeping 120s"
  sleep 120
done
