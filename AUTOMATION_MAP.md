# Automation Map (Investor OS v2)

Last updated: 2026-02-17

This is the current automation map for the new v2 architecture.

## Active Launchd Jobs

These are loaded by `bin/install_v2_launchd`.

1. `com.solmaz.v2.morning`
- Schedule: Mon-Fri 07:50
- Script: `automations/v2_morning.sh`
- Runs:
  - `tools/macro_watchdog.py --write`
  - `market_scanner.py --earnings --data`

2. `com.solmaz.v2.earnings`
- Schedule: Mon-Fri 08:20
- Script: `automations/v2_earnings.sh`
- Runs:
  - `market_scanner.py --earnings --data`

3. `com.solmaz.v2.daily`
- Schedule: Mon-Fri 08:55 (primary) + 09:12 (fallback retry)
- Script: `automations/v2_daily.sh`
- Runs:
  - `tools/macro_watchdog.py --write`
  - `tools/insider_refresh.py --all --months 3`
  - `market_scanner.py --earnings --data`
  - `market_scanner.py --signals --data`
  - `tools/build_terminal_reports.py --daily --appendix`
- Purpose:
  - Keep the daily briefing (`/reports`) and core dashboard intelligence fresh.

4. `com.solmaz.v2.signals`
- Schedule: Mon-Fri 12:20
- Script: `automations/v2_signals.sh`
- Runs:
  - `market_scanner.py --signals --data`

5. `com.solmaz.v2.sec.nightly`
- Schedule: Mon-Fri 21:35
- Script: `automations/v2_sec_nightly.sh`
- Runs:
  - `tools/sec_sync_my_companies.py --days 7 --full-empty-limit 6`

6. `com.solmaz.v2.insider.deep`
- Schedule: Sunday 08:30
- Script: `automations/v2_insider_deep.sh`
- Runs:
  - `tools/insider_refresh.py --all --months 18`

## Primary Outputs

### Reports
- `reports/terminal_daily_brief_YYYYMMDD.md`
- `reports/terminal_appendix_YYYYMMDD.md`
- `reports/l2_digest_YYYYMMDD_HHMM.md`
- plus weekly/monthly/quarterly artifacts when run

## Report Coverage (What Each One Contains)

1. `terminal_daily_brief_YYYYMMDD.md`
- Daily top-line intelligence summary.
- Key SEC filing changes and material flags.
- Risk highlights (for example debt/liquidity/covenant/guidance/default language).
- Earnings context and what changed vs recent runs.
- Action-oriented high-level takeaways.

2. `terminal_appendix_YYYYMMDD.md`
- Supporting details behind the daily brief.
- Earnings calendars and must-watch lists.
- Priority ticker coverage sections.
- Insider/scanner diagnostics and signal tables.
- Additional detailed context used for deeper reading.

3. `l2_digest_YYYYMMDD_HHMM.md`
- Compact tactical digest.
- Fast “what changed” summary.
- Short action queue / urgency cues.

4. `weekly_report_YYYYMMDD.md`
- Weekly recap and major themes.
- Sector/market context and key developments.
- Medium-horizon watch items.

5. `terminal_weekly_outlook_YYYYMMDD.md` (when run)
- Forward-looking week setup.
- Event/earnings focus and risk windows.
- Expected volatility and monitoring priorities.

6. `terminal_monthly_ic_memo_YYYYMMDD.md` (when run)
- Monthly investment-committee style summary.
- Thesis/risk framing at portfolio level.
- Strategic context and medium/long-term signals.

7. `quarterly_report_YYYYMMDD.md` (when run)
- Quarterly deep review.
- Longer-horizon structural trends.
- Regime/signal shifts across filings/earnings context.

### Data / Intelligence Refresh
- `data/filings.db` (SEC sync-related updates)
- `data/core.db` intelligence snapshots and app state

## Logs

### Automation logs
- `logs/v2_morning.log`
- `logs/v2_earnings.log`
- `logs/v2_daily.log`
- `logs/v2_signals.log`
- `logs/v2_sec_nightly.log`
- `logs/v2_insider_deep.log`

### Launchd logs
- `logs/launchd_v2_morning.log` (+ `_err.log`)
- `logs/launchd_v2_earnings.log` (+ `_err.log`)
- `logs/launchd_v2_daily.log` (+ `_err.log`)
- `logs/launchd_v2_signals.log` (+ `_err.log`)
- `logs/launchd_v2_sec_nightly.log` (+ `_err.log`)
- `logs/launchd_v2_insider_deep.log` (+ `_err.log`)

## Useful Commands

1. Reinstall/reload all v2 launch agents
```bash
./bin/install_v2_launchd
```

2. Check loaded jobs
```bash
launchctl list | rg "com\\.solmaz\\.v2\\." -N
```

3. Manual daily run
```bash
./bin/run_daily
```

4. Manual reports build only
```bash
./.venv-memory/bin/python tools/build_terminal_reports.py --daily --appendix
```

## Notes

- The v2 daily job now includes report building so `/reports` stays current.
- If daily report generation is delayed by heavy scanner/AI steps, fallback run at 09:12 helps recovery.
- App URL: `http://127.0.0.1:8766`
