#!/bin/bash
# Load applypilot .env safely (handles values with spaces like SEARCH_KEYWORDS).
load_env() {
  local env_file="$HOME/.applypilot/.env"
  [ -f "$env_file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%$'\r'}"
    [ -z "$line" ] && continue
    [[ "$line" == \#* ]] && continue
    [[ "$line" != *=* ]] && continue
    export "$line"
  done < "$env_file"
}
