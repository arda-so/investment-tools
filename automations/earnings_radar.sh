#!/bin/bash
set -euo pipefail
source /Users/solmaz/Investment_Tools/automations/common.sh

LOG_FILE="$ROOT/logs/earnings_radar.log"
setup_automation "$LOG_FILE"

# Optional: load user-level secrets as fallback.
if [ -f "$HOME/.env" ]; then
  # shellcheck disable=SC1090
  set -a
  . "$HOME/.env"
  set +a
fi

run_logged "$LOG_FILE" "$PYTHON_BIN" market_scanner.py --earnings
notify_user "Earnings Radar" "Earnings Radar ready"
