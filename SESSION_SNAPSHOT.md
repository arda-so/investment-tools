# SESSION SNAPSHOT (Saved)
Date: 2026-02-16 00:32:28 GMT
Project: /Users/solmaz/Investment_Tools

## Current State
- New modular app (v2) is running on: http://127.0.0.1:8766
- Legacy app remains on: http://127.0.0.1:8765
- Goal achieved: migrated major pages into new architecture while restoring old-style dashboard feel.

## Migrated to v2
1. Dashboard/Home (`/dashboard`, `/`)
- Old-style simple layout restored natively (no mirror).
- Live moving news ticker added.
- Right floating minimizable panel: Quick Note + Ask AI.
- Quick Capture + Ask AI endpoints are wired and inline.
- Market Pulse / Regulatory Audit / Portfolio / Watchlist / Library / Smart Feed are on dashboard.

2. Organizer (`/organizer`)
- Daily Note save + End Day + Recall.
- HTMX partial updates for major actions.

3. Company Files (`/company_file`)
- Search/filter/sort/pagination.
- Company detail includes moat, competitors, notes, tasks, reminders with inline actions.

4. My Companies (`/my_companies`)
- Dedicated page with clean separation:
  - My Portfolio
  - My Watchlist

5. Reports (`/reports`)
- Native reports listing and filter page in v2.
- Report viewer: `/reports/view?name=...`

## Key Navigation (v2)
- Home: `/dashboard`
- Reports: `/reports`
- Organizer: `/organizer`
- Company Files: `/company_file`
- My Companies: `/my_companies`

## Main Files Added/Changed (v2)
- `app/main.py`
- `app/routers/dashboard.py`
- `app/routers/organizer.py`
- `app/routers/company_file.py`
- `app/routers/reports.py`
- `app/services/dashboard_service.py`
- `app/services/organizer_service.py`
- `app/services/company_file_service.py`
- `app/services/reports_service.py`
- `app/templates/dashboard.html`
- `app/templates/components/dashboard_body.html`
- `app/templates/organizer.html`
- `app/templates/components/organizer_body.html`
- `app/templates/company_file.html`
- `app/templates/company_detail.html`
- `app/templates/components/company_detail_body.html`
- `app/templates/reports.html`
- `app/templates/report_view.html`
- `app/templates/my_companies.html`
- `app/templates/base.html`

## Open Items for Next Session
1. Pixel-parity polish vs old home (`:8765`) section-by-section.
2. Validate each dashboard section data quality vs legacy outputs.
3. Migrate any remaining legacy endpoints not yet moved.
4. Optional: close old background sessions/processes to reduce open process count warnings.

## Addendum (2026-02-16 Late Session)
- SEC company workflow in v2 is now:
  - Company file: `/company_file?t=TICKER`
  - SEC page: `/company_file/sec?t=TICKER`
  - Filing viewer: `/filing?path=...` and raw file `/filing_raw?path=...`
- Filing coverage fix:
  - v2 now shows full filing set per ticker (high cap query, no dropping non-local rows).
  - Rows open local file when available; otherwise open SEC URL if present.
- Filing organization:
  - Grouped by category with counts for fast reading:
    - Annual, Quarterly, Proxy/Shareholder, Current, Ownership/Beneficial, Other.
- Automation architecture now v2-only:
  - Old launch agents removed.
  - New launch agents active:
    - `com.solmaz.v2.morning`
    - `com.solmaz.v2.earnings`
    - `com.solmaz.v2.signals`
    - `com.solmaz.v2.daily`
    - `com.solmaz.v2.insider.deep`
  - Installer/refresh command:
    - `./bin/install_v2_launchd`
