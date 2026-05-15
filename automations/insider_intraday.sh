#!/bin/bash
set -euo pipefail
source /Users/solmaz/Investment_Tools/automations/common.sh

LOG_FILE="$ROOT/logs/insider_intraday.log"
setup_automation "$LOG_FILE"

run_logged "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/insider_refresh.py" --all --months 18
notify_user "Insider Refresh" "Intraday insider scan complete"
