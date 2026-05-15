#!/bin/bash
set -euo pipefail
source /Users/solmaz/Investment_Tools/automations/common.sh

LOG_FILE="$ROOT/logs/monthly.log"
setup_automation "$LOG_FILE"

export REPORT_PATH="$ROOT/reports/monthly_report_$(date +%Y%m%d).md"
run_logged "$LOG_FILE" "$PYTHON_BIN" research_agent.py ask "Answer ALL of the following questions based on the filing database:

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

notify_user "Research Agent" "Monthly report ready"
