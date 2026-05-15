#!/bin/bash
set -euo pipefail
source /Users/solmaz/Investment_Tools/automations/common.sh

LOG_FILE="$ROOT/logs/long_term_weekly.log"
setup_automation "$LOG_FILE"
REPORT_DIR="$ROOT/reports"
DATE_TAG="$(date +%Y%m%d)"
MODE="$("$PYTHON_BIN" research_agent.py mode | head -n 1 | awk '{print $3}')"

echo "Source mode: $MODE" >> "$LOG_FILE"

run_logged "$LOG_FILE" "$PYTHON_BIN" research_agent.py update --days 7

export REPORT_PATH="$REPORT_DIR/thesis_check_${DATE_TAG}.md"
run_logged "$LOG_FILE" "$PYTHON_BIN" research_agent.py thesis check --all --limit 40

export REPORT_PATH="$REPORT_DIR/quarterly_longterm_${DATE_TAG}.md"
run_logged "$LOG_FILE" "$PYTHON_BIN" research_agent.py quarterly --all --lookback 4

export REPORT_PATH="$REPORT_DIR/valuation_monitor_${DATE_TAG}.md"
if [ "$MODE" = "bloomberg_like" ]; then
  run_logged "$LOG_FILE" "$PYTHON_BIN" research_agent.py valuation --all --allow-broader-sources
else
  run_logged "$LOG_FILE" "$PYTHON_BIN" research_agent.py valuation --all
fi

unset REPORT_PATH

notify_user "Research Agent" "Long-term weekly package ready"
