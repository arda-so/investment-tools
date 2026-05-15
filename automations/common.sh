#!/bin/bash
# Shared helpers for automation scripts.

set -euo pipefail

ROOT="/Users/solmaz/Investment_Tools"
PYTHON_BIN="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"

setup_automation() {
  local log_file="$1"
  export PATH="/Library/Frameworks/Python.framework/Versions/3.14/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
  cd "$ROOT"
  mkdir -p logs reports
  export AI_CONNECTOR_SILENT=1
  # Load local secrets if present (for manual runs and portability).
  if [ -f "$ROOT/.env" ]; then
    # shellcheck disable=SC1091
    set -a
    . "$ROOT/.env"
    set +a
  fi
  if [ -f "$HOME/.env" ]; then
    # shellcheck disable=SC1090
    set -a
    . "$HOME/.env"
    set +a
  fi
  # Battery-friendly defaults (can still be overridden in .env).
  : "${ONYX_FAST_MODE:=1}"
  : "${ONYX_PREWARM_ON_START:=0}"
  : "${ONYX_PREWARM_TICKERS:=4}"
  : "${ONYX_FAST_FEED_ON:=1}"
  : "${ONYX_FAST_FEED_INTERVAL:=1800}"
  : "${ONYX_FAST_FEED_AI_TICKERS:=1}"
  : "${ONYX_INTEL_FEED_INTERVAL:=43200}"
  : "${ONYX_INTEL24_INTERVAL:=7200}"
  : "${ONYX_INTEL24_ON:=0}"
  : "${ONYX_SEC_RISK_ON:=0}"
  : "${ONYX_UNIFIED_SCHED_ON:=1}"
  : "${OLLAMA_NUM_PARALLEL:=1}"
  : "${OLLAMA_MAX_LOADED_MODELS:=1}"
  : "${OLLAMA_KEEP_ALIVE:=5m}"
  export ONYX_FAST_MODE ONYX_PREWARM_ON_START ONYX_PREWARM_TICKERS
  export ONYX_FAST_FEED_ON ONYX_FAST_FEED_INTERVAL ONYX_FAST_FEED_AI_TICKERS ONYX_INTEL_FEED_INTERVAL ONYX_INTEL24_INTERVAL ONYX_INTEL24_ON ONYX_SEC_RISK_ON ONYX_UNIFIED_SCHED_ON
  export OLLAMA_NUM_PARALLEL OLLAMA_MAX_LOADED_MODELS OLLAMA_KEEP_ALIVE
  {
    echo ""
    echo "=== $(basename "$0") :: $(date) ==="
  } >> "$log_file"

  if [ -z "${OPENAI_API_KEY:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    echo "Error: neither OPENAI_API_KEY nor ANTHROPIC_API_KEY is set." >> "$log_file"
    echo "Set one of them in $ROOT/.env before running automations." >> "$log_file"
    return 2
  fi
}

run_logged() {
  local log_file="$1"
  shift
  "$@" >> "$log_file" 2>&1
}

notify_user() {
  local title="$1"
  local message="$2"
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display notification \"$message\" with title \"$title\"" || true
  fi
}
