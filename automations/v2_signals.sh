#!/bin/bash
set -euo pipefail
source /Users/solmaz/Investment_Tools/automations/common_v2.sh

LOG_FILE="$ROOT/logs/v2_signals.log"
setup_v2_automation "$LOG_FILE"

run_logged_v2 "$LOG_FILE" "$PYTHON_BIN" "$ROOT/market_scanner.py" --signals --data

notify_user_v2 "Investor OS v2" "Signals refresh complete"
