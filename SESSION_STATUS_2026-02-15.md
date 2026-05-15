# Session Status (2026-02-15)

## Completed Today
- Renamed main app title/header to `Investment Intelligence Platform`.
- Company navigation unified:
  - Universe ticker clicks now open `Company File` (`/company_file?t=...`).
  - Earnings Intelligence ticker clicks now open `Company File`.
  - SEC Filings remains available inside company page via button.
- Company File simplified and made company-focused:
  - Kept search box for quick company switching.
  - Added cleaner company info section.
  - Added `Company Notes`.
  - Added `Reminders` (add + toggle open/done).
- Workspace retirement progress:
  - Main visible links now point to Company File.
  - Legacy `/api/workspace/*` endpoints return retired status.
- Index/Company list UX:
  - Removed redundant `Open` actions in multiple tables.
  - Ticker/company names are clickable links directly.

## Performance Tuning Done
- Startup speed improved:
  - Heavy startup tasks moved to background thread (server starts first).
- Dashboard speed improved:
  - Added cached `get_reported_earnings_snapshot()` with signature key.
- Market cap load behavior improved:
  - Hybrid loader: some rows fetched immediately, rest backfilled async.

## Database Architecture Update
- Began split DB model:
  - `data/core.db` for core app data.
  - `data/cache.db` for cache entries.
  - `data/filings.db` for filings/research datasets.
- Added safe bootstrap logic from legacy `data/research.db`.
- Cache functions now persist to `cache.db` while still using in-memory fast path.

## Backups
- Multiple backup attempts were created.
- Important: some earlier `.tar.gz` files were still being written when checked.
- Most reliable current approach: upload a **completed, verified** backup only.

## Open Items / Next Session
1. Finalize one clean, verified backup file and upload it to Google Drive.
2. Validate market cap enrichment quality for index pages (still seeing `Unknown`/`-` in some contexts).
3. Add explicit local profile cache seeding for index constituents so company/industry don’t show `Unknown`.
4. Optional: add top bar “System Status” widget showing:
   - active model for fast intel
   - active model for deep intel
   - scheduler last run times
5. Optional hardening:
   - add small startup health endpoint and latency log.

## Quick Run Commands
- Start app:
  - `./bin/run_terminal_app`
- Verify syntax:
  - `python3 -m py_compile tools/terminal_app.py`
- Check app:
  - `curl -sS --max-time 10 http://127.0.0.1:8765/`

