#!/bin/bash
set -euo pipefail
source /Users/solmaz/Investment_Tools/automations/common.sh

LOG_FILE="$ROOT/logs/signal_tracker.log"
setup_automation "$LOG_FILE"

run_logged "$LOG_FILE" "$PYTHON_BIN" market_scanner.py --signals
notify_user "Signal Tracker" "Signal Tracker ready"
