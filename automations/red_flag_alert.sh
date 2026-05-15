#!/bin/bash
set -euo pipefail
source /Users/solmaz/Investment_Tools/automations/common.sh

LOG_FILE="$ROOT/logs/red_flag_alert.log"
setup_automation "$LOG_FILE"

run_logged "$LOG_FILE" "$PYTHON_BIN" research_agent.py update --days 1
CHANGES="$("$PYTHON_BIN" research_agent.py changes 2>>"$LOG_FILE" || true)"
ALERTS="$(printf "%s\n" "$CHANGES" | "$PYTHON_BIN" "$ROOT/tools/red_flag_filter.py" || true)"

if [ -n "$ALERTS" ]; then
  REPORT_PATH="$ROOT/reports/red_flag_alert_$(date +%Y%m%d).txt"
  printf "%s\n" "$ALERTS" > "$REPORT_PATH"
  printf "%s\n" "$ALERTS" >> "$LOG_FILE"
  notify_user "Research Agent Alert" "RED FLAG detected in SEC filings"
fi
