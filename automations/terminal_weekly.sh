#!/bin/bash
set -euo pipefail
source /Users/solmaz/Investment_Tools/automations/common.sh

LOG_FILE="$ROOT/logs/terminal_weekly.log"
setup_automation "$LOG_FILE"
DATE_TAG="$(date +%Y%m%d)"
TMP_DIR="$ROOT/reports/.terminal_inputs"
mkdir -p "$TMP_DIR"

export REPORT_PATH="$TMP_DIR/weekly_report_${DATE_TAG}.md"
run_logged "$LOG_FILE" "$PYTHON_BIN" "$ROOT/research_agent.py" ask "Answer ALL of the following questions based on the filing database:

WEEKLY REPORT:
1. Which companies increased or decreased their debt in the latest filings compared to the previous quarter?
2. Rank the top 10 most aggressive share buyback programs based on recent filings.
3. Which companies mentioned pricing power, price increases, or margin expansion in their latest MD&A?
4. Any companies where insider ownership or compensation structure changed significantly in recent proxy filings?
5. Compare free cash flow commentary across AAPL, MSFT, GOOGL, JPM, KO — who is generating the most and what are they doing with it?
6. Which companies added new risk factors that weren't in their previous filing? What are they worried about now?
7. Any company mention goodwill impairment testing, asset writedowns, or valuation adjustments?
8. Which companies changed their revenue recognition policies or accounting estimates recently?

Be thorough but concise. Skip sections with nothing to report."

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
  "$ROOT/reports/weekly_report_${DATE_TAG}.md" \
  "$ROOT/reports/thesis_check_${DATE_TAG}.md" \
  "$ROOT/reports/valuation_monitor_${DATE_TAG}.md" \
  "$ROOT/reports/quarterly_longterm_${DATE_TAG}.md"; do
  [ -f "$f" ] && mv "$f" "$TMP_DIR/"
done

run_logged "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/build_terminal_reports.py" --weekly
# Weekly data integrity audit for structured financial coverage/freshness.
run_logged "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/integrity_financial_data.py"
notify_user "Terminal Weekly" "Terminal weekly outlook ready"
