#!/bin/bash
set -euo pipefail
source /Users/solmaz/Investment_Tools/automations/common.sh

LOG_FILE="$ROOT/logs/weekly.log"
setup_automation "$LOG_FILE"

export REPORT_PATH="$ROOT/reports/weekly_report_$(date +%Y%m%d).md"
run_logged "$LOG_FILE" "$PYTHON_BIN" research_agent.py ask "Answer ALL of the following questions based on the filing database:

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

notify_user "Research Agent" "Weekly report ready"
