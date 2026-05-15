#!/bin/bash
set -euo pipefail
source /Users/solmaz/Investment_Tools/automations/common.sh

LOG_FILE="$ROOT/logs/macro_watchdog.log"
setup_automation "$LOG_FILE"

# 1) Refresh CPI/macro watchdog packet (official BLS sources).
run_logged "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/macro_watchdog.py" --write

# 2) Rebuild terminal daily/appendix so event-day macro context is visible.
run_logged "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/build_terminal_reports.py" --daily --appendix

notify_user "Macro Watchdog" "Macro watchdog refreshed and terminal reports rebuilt"

