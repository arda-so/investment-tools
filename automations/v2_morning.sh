#!/bin/bash
set -euo pipefail
source /Users/solmaz/Investment_Tools/automations/common_v2.sh

LOG_FILE="$ROOT/logs/v2_morning.log"
setup_v2_automation "$LOG_FILE"

# Morning refresh packet for v2 dashboard.
run_logged_v2 "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/macro_watchdog.py" --write
run_logged_v2 "$LOG_FILE" "$PYTHON_BIN" "$ROOT/market_scanner.py" --earnings --data
run_logged_v2 "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/run_morning_brief.py"
# Proactive premarket scan (forced) to prepare action proposals.
run_logged_v2 "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/run_proactive_monitor.py" --force

notify_user_v2 "Investor OS v2" "Morning refresh complete"
