#!/bin/bash
set -euo pipefail
source /Users/solmaz/Investment_Tools/automations/common.sh

LOG_FILE="$ROOT/logs/terminal_monthly.log"
setup_automation "$LOG_FILE"
DATE_TAG="$(date +%Y%m%d)"
TMP_DIR="$ROOT/reports/.terminal_inputs"
mkdir -p "$TMP_DIR"

export REPORT_PATH="$TMP_DIR/monthly_report_${DATE_TAG}.md"
run_logged "$LOG_FILE" "$PYTHON_BIN" "$ROOT/research_agent.py" ask "Answer ALL of the following questions based on the filing database:

MONTHLY DEEP DIVE:
1. Sector-by-sector summary: for each sector, what are the dominant themes in the latest filings — growth, contraction, risk, opportunity?
2. Which companies mentioned tariffs, trade restrictions, or geopolitical risk for the first time in the last 90 days?
3. Rank all companies by risk: who has the most concerning combination of rising debt, declining margins, new litigation, and management turnover?
4. Which companies have the cleanest balance sheets — low debt, high cash, consistent buybacks, no restructuring charges?
5. Compare executive compensation vs company performance across the top 50 companies — who is overpaying management?
6. Track AI and artificial intelligence mentions across all filings — who mentions it most, who started recently, who doesn't mention it at all?
7. Which companies have been steadily growing their dividend based on proxy and 8-K filings over the last 5 years?
8. Identify companies where MD&A tone shifted from optimistic to cautious compared to the same quarter last year.
9. Which companies changed auditors, restated financials, or reported material weaknesses in the last 12 months?
10. Give me the top 20 companies to research deeper — unusual changes, opportunity signals, or warning signs.

Be comprehensive. This is the monthly review."

run_logged "$LOG_FILE" "$PYTHON_BIN" "$ROOT/research_agent.py" update --days 7
export REPORT_PATH="$TMP_DIR/thesis_check_${DATE_TAG}.md"
run_logged "$LOG_FILE" "$PYTHON_BIN" "$ROOT/research_agent.py" thesis check --all --limit 40
export REPORT_PATH="$TMP_DIR/valuation_monitor_${DATE_TAG}.md"
MODE="$("$PYTHON_BIN" "$ROOT/research_agent.py" mode | head -n 1 | awk '{print $3}')"
if [ "$MODE" = "bloomberg_like" ]; then
  run_logged "$LOG_FILE" "$PYTHON_BIN" "$ROOT/research_agent.py" valuation --all --allow-broader-sources
else
  run_logged "$LOG_FILE" "$PYTHON_BIN" "$ROOT/research_agent.py" valuation --all
fi
unset REPORT_PATH

for f in \
  "$ROOT/reports/monthly_report_${DATE_TAG}.md" \
  "$ROOT/reports/thesis_check_${DATE_TAG}.md" \
  "$ROOT/reports/valuation_monitor_${DATE_TAG}.md"; do
  [ -f "$f" ] && mv "$f" "$TMP_DIR/"
done

run_logged "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/build_terminal_reports.py" --monthly
notify_user "Terminal Monthly" "Terminal monthly IC memo ready"
