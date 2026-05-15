#!/bin/bash
set -euo pipefail
source /Users/solmaz/Investment_Tools/automations/common.sh

LOG_FILE="$ROOT/logs/morning_intel.log"
setup_automation "$LOG_FILE"

run_logged "$LOG_FILE" "$PYTHON_BIN" morning_intelligence.py
notify_user "Morning Intelligence" "Morning briefing ready"
