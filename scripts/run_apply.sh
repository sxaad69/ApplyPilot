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

# Count prepared jobs (tailored + cover-lettered + not applied). Only launch
# the browser and run apply when there is actual work — don't open Chrome
# (or run the pipeline) on every loop pass.
count_prepared() {
  "$APP_DIR/.venv/bin/python" -c "
from applypilot.db import JobDatabase
db = JobDatabase()
n = db._conn.execute(\"\"\"
  SELECT COUNT(*) FROM jobs
  WHERE tailored_resume_path IS NOT NULL
    AND (cover_letter_path IS NOT NULL OR cover_letter_path != '')
    AND (apply_status IS NULL OR apply_status = '')
\"\"\").fetchone()[0]
print(n)
"
}

echo "[$(date)] apply: starting continuous loop"
while true; do
  READY=$(count_prepared)
  echo "[$(date)] apply: ${READY} prepared job(s) ready"
  if [ "${READY:-0}" -gt 0 ]; then
    # Chrome is managed by the com.applypilot.chrome launchd agent (KeepAlive),
    # so it's always on :9222 with the logged-in profile. Just run the apply.
    "$APP_DIR/.venv/bin/applypilot" apply --limit 5 \
      >> "$LOG_DIR/apply.log" 2>&1 || true
  else
    echo "[$(date)] apply: no work, skipping pass"
  fi
  echo "[$(date)] apply: pass done, sleeping 180s"
  sleep 180
done
