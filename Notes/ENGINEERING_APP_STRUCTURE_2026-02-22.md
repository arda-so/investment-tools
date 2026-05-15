# Engineering App Structure Report

Last updated: 2026-02-22
Audience: engineers onboarding to Investor OS v2

## 1) High-Level Architecture

- Runtime: FastAPI app (`app/main.py`) + background AI worker (`tools/run_ai_worker.py`, launched by `bin/run_ai_worker`).
- Rendering: server-side Jinja templates in `app/templates/`.
- DB mode: Postgres is enforced at app startup (`core_backend == postgres`).
- Queue mode: AI queue backend currently reports as Postgres on readiness (`/health/ready`).
- Core routers registered in `app/main.py`:
  - `dashboard_router`
  - `reports_router`
  - `organizer_router`
  - `observability_router`
  - `company_file_router`
  - `ai_router`

## 2) Main User-Facing Applications (Pages)

### A. Dashboard / Home
- Routes: `/`, `/dashboard`
- Router: `app/routers/dashboard.py`
- Templates:
  - Page shell: `app/templates/dashboard.html`
  - HTMX body: `app/templates/components/dashboard_body.html`
- Purpose:
  - Daily market briefing
  - Action proposals
  - Earnings calendar
  - Lows/risk/rates panels
  - Quick AI command and desk actions

### B. Company Files
- Route: `/company_file`
- Router: `app/routers/company_file.py`
- Templates:
  - Index/list: `app/templates/company_file.html`
  - Detail page: `app/templates/company_detail.html`
  - HTMX detail body: `app/templates/components/company_detail_body.html`
  - SEC panel: `app/templates/company_sec.html`
- Purpose:
  - Per-company workspace (thesis/moats/notes/tasks/reminders/competitors/SEC links)
  - IR email actions
  - SEC sync actions

### C. My Companies / My Universe
- Routes:
  - `/my_companies`
  - `/my_universe`
- Router: `app/routers/dashboard.py`
- Template:
  - `app/templates/my_universe.html`
- Purpose:
  - Universe management and portfolio/watchlist/blue-chip membership operations
  - Cash line management for portfolio context

### D. Organizer
- Route: `/organizer`
- Router: `app/routers/organizer.py`
- Templates:
  - `app/templates/organizer.html`
  - `app/templates/components/organizer_body.html`
  - file views (`organizer_*_file.html` variants)
- Purpose:
  - Daily notes
  - Todos/tasks
  - Memory review/cleanup/approvals
  - Google integration actions

### E. Reports
- Routes: `/reports`, `/report`, `/reports/view`
- Router: `app/routers/reports.py`
- Templates:
  - `app/templates/reports.html`
  - `app/templates/report_view.html`
- Purpose:
  - Report browsing, grouping, read tracking, and report intelligence extraction

### F. Observability
- Route: `/observability`
- Router: `app/routers/observability.py`
- Template:
  - `app/templates/observability.html`
- Purpose:
  - Runtime/system visibility and operational diagnostics

## 3) Core Data Domains

## Portfolio domain
- Holdings, transactions, cash lines, quick capture, portfolio intelligence.
- Key services:
  - `app/services/portfolio_memory_service.py`
  - `app/services/dashboard_service.py`

## Watchlist / Universe domain
- Watchlist adds/removes, blue-chip membership, thesis linkage.
- Key services:
  - `app/services/portfolio_memory_service.py`
  - `app/services/company_file_service.py`
  - `app/services/postgres_core_service.py`

## Company Files domain ("my companies")
- Company profile, moat tags, conviction box, notes, tasks, reminders, competitors, SEC history links.
- Key services:
  - `app/services/company_file_service.py`
  - `app/services/sec_sync_state.py`

## Research / Filings / Facts domain
- SEC filing ingest, extraction, relationship graph, report facts.
- Key services:
  - `app/services/sec_ingest_pipeline_service.py`
  - `app/services/proactive_ai_service.py`
  - `app/services/reports_service.py`

## AI Command + Queue domain
- Synchronous command endpoint + async queued jobs + SSE result stream.
- Key services:
  - `app/services/ai_orchestrator.py`
  - `app/services/ai_job_queue_service.py`
  - `app/services/ai_react_service.py`
  - `app/services/ai_async_service.py` (compat shim only)
- Key endpoints:
  - `/ai/command`
  - `/ai/command/async`
  - `/ai/command/result/{job_id}`
  - `/ai/command/sse/{job_id}`
  - `/ai/worker/health`

## Memory / Learning domain
- Compact memory, style memory, thesis memory, learning candidates/promotion.
- Key services:
  - `app/services/portfolio_memory_service.py`
  - `app/services/user_preferences_service.py`
  - `app/services/chat_memory_service.py`

## 4) Postgres Structure (Source of Truth)

Primary schema creation and adapters live in:
- `app/services/postgres_core_service.py`

This service initializes and serves core tables for:
- action proposals
- portfolio transactions
- watchlist thesis
- investor style memory
- filings
- report facts
- todos/tasks
- investor notes
- company reminders
- memory compact
- company profile cache
- companies / lists / list items
- moat tags / competitors
- blue chips
- monitoring and AI logs
- daily notes, queue/log, learning/rules, risk veto
- preferences, question overrides, interview queue

Operational rule:
- App startup enforces Postgres mode in `app/main.py`.

## 5) End-to-End Flow Examples

### A. Action Proposal flow
1. Signal/fact/filing enters via ingest/monitor services.
2. Proposal generated/updated in proposal domain (Postgres-backed).
3. Dashboard renders proposals on `/dashboard`.
4. User can open workspace / dismiss / reject with feedback.

### B. Company File workflow
1. User opens `/company_file?t=TICKER`.
2. Detail loaded from company profile + thesis/memory/todos/reminders + SEC stats.
3. Mutations (note/task/reminder/competitor/moat) persist to Postgres adapters.

### C. AI async workflow
1. `POST /ai/command/async` enqueues job.
2. Worker claims job and runs deep reasoning.
3. UI subscribes via `/ai/command/sse/{job_id}` for status/result.
4. Result is cached through unified queue cache path.

## 6) "My Universe" vs "Company Files" vs "Portfolio" (Practical Model)

- Portfolio:
  - Current position/exposure/cash and performance context.
  - Feeds dashboard and risk/decision logic.

- Watchlist/My Universe:
  - Candidate set and strategic universe controls (add/remove/blue-chip tags).
  - Not necessarily in live holdings.

- Company Files (My Companies):
  - Deep per-ticker workspace for thesis execution (notes/tasks/reminders/competitors/moats/SEC artifacts).
  - Can exist for both held names and watchlist names.

Recommended mental model:
- Universe selects what to study.
- Company File stores conviction and work product.
- Portfolio stores capital actually allocated.

## 7) File Map for Engineers (Start Here)

- App bootstrap: `app/main.py`
- Routers:
  - `app/routers/dashboard.py`
  - `app/routers/company_file.py`
  - `app/routers/organizer.py`
  - `app/routers/reports.py`
  - `app/routers/ai.py`
- Core services:
  - `app/services/postgres_core_service.py`
  - `app/services/ai_job_queue_service.py`
  - `app/services/ai_orchestrator.py`
  - `app/services/portfolio_memory_service.py`
  - `app/services/company_file_service.py`
  - `app/services/sec_ingest_pipeline_service.py`
- Templates:
  - `app/templates/dashboard.html`
  - `app/templates/components/dashboard_body.html`
  - `app/templates/company_file.html`
  - `app/templates/company_detail.html`
  - `app/templates/my_universe.html`

## 8) Current Operational Notes

- Postgres is required by runtime guard.
- Queue + SSE are active for deep AI paths.
- `ai_async_service.py` remains as compatibility shim only; queue/cache source of truth is `ai_job_queue_service.py`.
- Health endpoints to verify runtime:
  - `/health/live`
  - `/health/ready`

## 9) Suggested Onboarding Checklist (Engineer)

1. Read `app/main.py` startup guards and router registration.
2. Read `app/services/postgres_core_service.py` table/adapters.
3. Trace one flow each for:
   - Dashboard proposal action
   - Company file mutation
   - AI async job via SSE
4. Confirm local health (`/health/live`, `/health/ready`) before feature work.


## 2026-02-24 Refresh
- Runtime update: active company market-cap lookup and events runtime paths are Postgres-only.
- AI command reliability: `/ai/command` timeout path now returns queued fallback quickly (no blocking hang).
- Health posture unchanged and valid in current run:
  - `/health/live` ok
  - `/health/ready` => `core_db_backend=postgres`, `queue_backend=postgres`, `db_ok=true`
- Continuation policy: keep async-first AI flow and avoid reintroducing SQLite fallback in active runtime request paths.
