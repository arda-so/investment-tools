#!/bin/bash
set -euo pipefail
source /Users/solmaz/Investment_Tools/automations/common_v2.sh

LOG_FILE="$ROOT/logs/v2_sec_nightly.log"
setup_v2_automation "$LOG_FILE"

# Keep SEC filing coverage fresh for portfolio + watchlist.
run_logged_v2 "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/sec_sync_my_companies.py" --days 7 --full-empty-limit 6
# Proactive post-close scan (forced) for filing/report-driven proposals.
run_logged_v2 "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/run_proactive_monitor.py" --force
# Autonomous agent is cloud-primary now.
# To re-enable local nightly agent run, set:
#   export LOCAL_AGENT_WORKER_NIGHTLY=1
if [ "${LOCAL_AGENT_WORKER_NIGHTLY:-0}" = "1" ]; then
  run_logged_v2 "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/agent_worker.py" --watch --days 1
fi
# Universe watch: scan watchlist (non-held) filings for signals affecting held positions.
# Runs locally always; cloud runs it via the agent worker job (--universe-watch appended there).
run_logged_v2 "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/agent_worker.py" --universe-watch --days 1
# Measure T+7/30/90 outcomes for prior agent analyses (runs regardless of agent mode).
run_logged_v2 "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/run_outcome_measurement.py"

notify_user_v2 "Investor OS v2" "Nightly SEC sync complete"
