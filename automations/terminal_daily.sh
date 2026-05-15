#!/bin/bash
set -euo pipefail
source /Users/solmaz/Investment_Tools/automations/common.sh

LOG_FILE="$ROOT/logs/terminal_daily.log"
setup_automation "$LOG_FILE"
DATE_TAG="$(date +%Y%m%d)"
TMP_DIR="$ROOT/reports/.terminal_inputs"
mkdir -p "$TMP_DIR"

# 1) Macro snapshot
export REPORT_PATH="$TMP_DIR/morning_intelligence_${DATE_TAG}.md"
run_logged "$LOG_FILE" "$PYTHON_BIN" "$ROOT/morning_intelligence.py"
run_logged "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/macro_watchdog.py" --write

# 2) Filing intelligence
run_logged "$LOG_FILE" "$PYTHON_BIN" "$ROOT/research_agent.py" update --days 3
export REPORT_PATH="$TMP_DIR/daily_brief_${DATE_TAG}.md"
run_logged "$LOG_FILE" "$PYTHON_BIN" "$ROOT/research_agent.py" ask "Answer ALL of the following questions based on the latest filings:

DAILY BRIEFING (covering yesterday's full US business day and today so far):
1. What 8-Ks were filed yesterday and today? Summarize each — earnings, acquisitions, leadership changes, material events.
2. Did any company file an 8-K mentioning dividend cut, suspension, or reduction?
3. Any company mention supply chain disruption, force majeure, or production halt in new filings?
4. Which companies filed new proxy statements? Any unusual shareholder proposals or executive pay changes?
5. Flag anything with litigation, impairment, restatement, going concern, material weakness, restructuring, covenant violations, or auditor changes.
6. Any new 10-K or 10-Q filings? Highlight key changes in risk factors, MD&A, or debt sections compared to the previous filing.

Keep it concise. Only report what actually happened — skip sections with nothing to report."

# 3) Red flags
run_logged "$LOG_FILE" "$PYTHON_BIN" "$ROOT/research_agent.py" update --days 1
CHANGES="$("$PYTHON_BIN" "$ROOT/research_agent.py" changes 2>>"$LOG_FILE" || true)"
ALERTS="$(printf "%s\n" "$CHANGES" | "$PYTHON_BIN" "$ROOT/tools/red_flag_filter.py" || true)"
if [ -n "$ALERTS" ]; then
  printf "%s\n" "$ALERTS" > "$TMP_DIR/red_flag_alert_${DATE_TAG}.txt"
fi

# 3b) Insider refresh (portfolio + watchlist, 18 months backfill window)
run_logged "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/insider_refresh.py" --all --months 18

# 4) Appendix sections
export REPORT_PATH="$TMP_DIR/earnings_radar_${DATE_TAG}.md"
run_logged "$LOG_FILE" "$PYTHON_BIN" "$ROOT/market_scanner.py" --earnings
export REPORT_PATH="$TMP_DIR/signal_tracker_${DATE_TAG}.md"
run_logged "$LOG_FILE" "$PYTHON_BIN" "$ROOT/market_scanner.py" --signals

unset REPORT_PATH

# Safety net: move any same-day legacy artifacts into hidden terminal inputs.
for f in \
  "$ROOT/reports/morning_intelligence_${DATE_TAG}.md" \
  "$ROOT/reports/daily_brief_${DATE_TAG}.md" \
  "$ROOT/reports/red_flag_alert_${DATE_TAG}.txt" \
  "$ROOT/reports/earnings_radar_${DATE_TAG}.md" \
  "$ROOT/reports/signal_tracker_${DATE_TAG}.md" \
  "$ROOT/reports/market_scanner_${DATE_TAG}.md"; do
  [ -f "$f" ] && mv "$f" "$TMP_DIR/"
done

# 5) Consolidated terminal outputs
run_logged "$LOG_FILE" "$PYTHON_BIN" "$ROOT/tools/build_terminal_reports.py" --daily --appendix
notify_user "Terminal Daily" "Terminal daily brief and appendix ready"
