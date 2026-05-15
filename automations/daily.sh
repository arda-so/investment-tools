#!/bin/bash
set -euo pipefail
source /Users/solmaz/Investment_Tools/automations/common.sh

LOG_FILE="$ROOT/logs/daily.log"
setup_automation "$LOG_FILE"

run_logged "$LOG_FILE" "$PYTHON_BIN" research_agent.py update --days 3

export REPORT_PATH="$ROOT/reports/daily_brief_$(date +%Y%m%d).md"
run_logged "$LOG_FILE" "$PYTHON_BIN" research_agent.py ask "Answer ALL of the following questions based on the latest filings:

DAILY BRIEFING (covering yesterday's full US business day and today so far):
1. What 8-Ks were filed yesterday and today? Summarize each — earnings, acquisitions, leadership changes, material events.
2. Did any company file an 8-K mentioning dividend cut, suspension, or reduction?
3. Any company mention supply chain disruption, force majeure, or production halt in new filings?
4. Which companies filed new proxy statements? Any unusual shareholder proposals or executive pay changes?
5. Flag anything with litigation, impairment, restatement, going concern, material weakness, restructuring, covenant violations, or auditor changes.
6. Any new 10-K or 10-Q filings? Highlight key changes in risk factors, MD&A, or debt sections compared to the previous filing.

Keep it concise. Only report what actually happened — skip sections with nothing to report."

# Keep Report Studio in sync with today's daily artifacts.
run_logged "$LOG_FILE" "$PYTHON_BIN" tools/build_terminal_reports.py --daily --appendix

notify_user "Research Agent" "Daily brief ready"
