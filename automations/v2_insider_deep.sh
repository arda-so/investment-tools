#!/bin/bash
set -euo pipefail
source /Users/solmaz/Investment_Tools/automations/common_v2.sh

LOG_FILE="$ROOT/logs/v2_insider_deep.log"
setup_v2_automation "$LOG_FILE"

# Weekly deep refresh for full insider history coverage.
run_logged_v2 "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/insider_refresh.py" --all --months 18

notify_user_v2 "Investor OS v2" "Weekly insider deep refresh complete"

