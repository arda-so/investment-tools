# Terminal Report Structure

This project now has an additive consolidated "Terminal" layer.
Legacy reports and automations are preserved.

## Consolidated Reports

- `reports/terminal_daily_brief_YYYYMMDD.md`
  - Combines:
    - `morning_intelligence_*.md`
    - `daily_brief_*.md`
    - `red_flag_alert_*.txt`

- `reports/terminal_appendix_YYYYMMDD.md`
  - Combines:
    - `earnings_radar_*.md`
    - `signal_tracker_*.md`

- `reports/terminal_weekly_outlook_YYYYMMDD.md`
  - Combines:
    - `weekly_report_*.md`
    - `quarterly_report_*_brief.md`
    - `thesis_check_*.md`
    - `valuation_monitor_*.md`

- `reports/terminal_monthly_ic_memo_YYYYMMDD.md`
  - Combines:
    - `monthly_report_*.md`
    - `quarterly_report_*.md`
    - `valuation_monitor_*.md`
    - `thesis_check_*.md`

## New Run Commands

- `./bin/run_terminal_daily`
- `./bin/run_terminal_weekly`
- `./bin/run_terminal_monthly`

## Notes

- No legacy report was removed or renamed.
- `bin/investor` latest pack now surfaces Terminal reports first.
