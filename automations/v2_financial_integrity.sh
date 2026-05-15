#!/bin/bash
set -euo pipefail
source /Users/solmaz/Investment_Tools/automations/common_v2.sh

LOG_FILE="$ROOT/logs/v2_financial_integrity.log"
setup_v2_automation "$LOG_FILE"

# Coverage/staleness integrity check for structured financial pipeline.
run_logged_v2 "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/integrity_financial_data.py"

notify_user_v2 "Investor OS v2" "Financial integrity check complete"

