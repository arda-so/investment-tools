#!/bin/bash
set -euo pipefail
source /Users/solmaz/Investment_Tools/automations/common_v2.sh

LOG_FILE="$ROOT/logs/v2_daily.log"
setup_v2_automation "$LOG_FILE"

# Daily core refresh for v2 (no AI generation).
run_logged_v2 "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/macro_watchdog.py" --write
run_logged_v2 "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/insider_refresh.py" --all --months 3
run_logged_v2 "$LOG_FILE" "$PYTHON_BIN" "$ROOT/market_scanner.py" --earnings --data
run_logged_v2 "$LOG_FILE" "$PYTHON_BIN" "$ROOT/market_scanner.py" --signals --data
# Keep reports cockpit fresh daily.
run_logged_v2 "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/build_terminal_reports.py" --daily --appendix
# Event-driven monitor during trading day: only acts when new filings/report facts exist.
run_logged_v2 "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/run_proactive_monitor.py"
# Data integrity audit: structured financial coverage/freshness.
run_logged_v2 "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/integrity_financial_data.py"

notify_user_v2 "Investor OS v2" "Daily refresh complete"
