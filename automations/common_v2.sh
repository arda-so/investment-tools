#!/bin/bash
# Shared helpers for v2 automation scripts (low-load, no mandatory AI keys).

set -euo pipefail

ROOT="/Users/solmaz/Investment_Tools"
PYTHON_BIN="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"

setup_v2_automation() {
  local log_file="$1"
  export PATH="/Library/Frameworks/Python.framework/Versions/3.14/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
  cd "$ROOT"
  mkdir -p logs reports
  export AI_CONNECTOR_SILENT=1
  # Keep local model pressure low for any incidental code paths.
  : "${OLLAMA_NUM_PARALLEL:=1}"
  : "${OLLAMA_MAX_LOADED_MODELS:=1}"
  : "${OLLAMA_KEEP_ALIVE:=2m}"
  export OLLAMA_NUM_PARALLEL OLLAMA_MAX_LOADED_MODELS OLLAMA_KEEP_ALIVE
  {
    echo ""
    echo "=== $(basename "$0") :: $(date) ==="
  } >> "$log_file"
}

run_logged_v2() {
  local log_file="$1"
  shift
  "$@" >> "$log_file" 2>&1
}

notify_user_v2() {
  local title="$1"
  local message="$2"
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display notification \"$message\" with title \"$title\"" || true
  fi
}

