#!/bin/bash
set -euo pipefail
source /Users/solmaz/Investment_Tools/automations/common.sh

LOG_FILE="$ROOT/logs/quarterly.log"
setup_automation "$LOG_FILE"

export REPORT_PATH="$ROOT/reports/quarterly_report_$(date +%Y%m%d).md"
# Refresh recent filing context before deep quarterly analysis.
run_logged "$LOG_FILE" "$PYTHON_BIN" research_agent.py update --days 45 --forms 10-Q,10-K,8-K

# Build a structured, filing-delta quarterly report for full watchlist coverage.
run_logged "$LOG_FILE" "$PYTHON_BIN" research_agent.py quarterly --all --lookback 8

notify_user "Research Agent" "Quarterly report ready"
