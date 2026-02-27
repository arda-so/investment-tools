#!/bin/bash
set -euo pipefail
source /Users/solmaz/Investment_Tools/automations/common_v2.sh

LOG_FILE="$ROOT/logs/v2_catchup.log"
setup_v2_automation "$LOG_FILE"

# Catch-up pass: refresh official evidence + rebuild daily report views + compact long-term memory.
run_logged_v2 "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/build_terminal_reports.py" --daily --appendix
run_logged_v2 "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/run_daily_catchup.py"

notify_user_v2 "Investor OS v2" "Daily catch-up complete"
