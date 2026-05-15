# Agent Continuation Handoff

Date: 2026-02-21
Owner: Solmaz / Investor Tools
Purpose: Single source of truth so another AI/dev can continue without re-discovery.

## 1) Current State (Authoritative)
- Core backend runtime target: `postgres`
- Queue backend runtime target: `postgres` (redis optional)
- Strict mode target: enabled in non-dev
- Admin migration/bootstrap endpoints: locked in non-dev by default
- Health endpoints implemented:
  - `GET /health/live`
  - `GET /health/ready`

## 2) Verified Live Checks (most recent)
Executed and verified:

1. `curl -sS http://127.0.0.1:8766/health/live`
- Output:
`{"ok":true,"service":"investor_app","version":"2.0.0"}`

2. `curl -sS http://127.0.0.1:8766/health/ready`
- Output:
`{"ok":true,"service":"investor_app","core_db_backend":"postgres","queue_backend":"postgres","db_ok":true,"db_error":""}`

3. `curl -sS -X POST http://127.0.0.1:8766/ai/migration/bootstrap`
- Output in non-dev with admin off:
`{"detail":"admin_api_disabled"}`

## 3) What Was Fixed

### A) Postgres strict startup / cutover logic
File: `app/services/postgres_core_service.py`
- Added non-dev behavior: no auto downgrade to sqlite in `guard_core_backend_cutover()`.
- Added `verify_postgres_core_ready()` that validates Postgres directly (connectivity + required `_core` tables).
- Updated `enforce_strict_postgres_ready()` to use Postgres readiness, not sqlite parity.
- Improved strict error detail:
  - now raises: `strict_postgres_verify_failed:<reason>`
- Added `pg_connect()` self-heal retry:
  - reset/rebuild pool once if stale.

### B) Health endpoints
File: `app/main.py`
- Added:
  - `/health/live`
  - `/health/ready`

### C) Admin endpoint lockdown
File: `app/routers/ai.py`
- Added `_admin_guard(request)` and enforced it on:
  - `/ai/phase2/bootstrap`
  - `/ai/migration/status`
  - `/ai/migration/bootstrap`
  - `/ai/migration/sync-core`
  - `/ai/migration/verify-core`
  - `/ai/migration/guard-core`
  - `/ai/events/bootstrap`
  - `/ai/learning/run`
  - `/ai/learning/promote`

### D) Cloud-safe env behavior
File: `app/core/config.py`
- `.env` loading in non-dev now controlled:
  - `ALLOW_LOCAL_ENV_FILE` (default off in non-dev).

### E) Queue backend accessor
File: `app/services/ai_job_queue_service.py`
- Added public `queue_backend()` helper for readiness response.

### F) Container/runtime defaults
Files:
- `docker-compose.yml`
- `.env.example`
- `CLOUD_READINESS_CHECKLIST.md`

Highlights:
- Added app healthcheck using `/health/ready`.
- Added restart policies (`unless-stopped`) for app/worker.
- Added cloud-safe env template values and checklist.

## 4) Core Migration Status
- Runtime core-domain SQL paths were moved to Postgres core tables.
- Strict scans were run to remove non-core runtime SQL for:
  - `action_proposals`, `portfolio_transactions`, `watchlist_thesis`,
  - `investor_style_memory`, `report_facts`, `todos`, `investor_notes`,
  - `workspace_journal`, `company_reminders`, `company_profile_cache`, `filings`
  (excluding migration utility logic in `postgres_core_service.py`).

## 5) Known Operational Notes
- `bin/run_v2_app` has preflight behavior and may mask startup errors.
- For debugging strict startup, run uvicorn directly with explicit env (see runbook below).
- If strict startup fails, check error now includes exact cause.

## 6) Runbook (exact)

### Strict non-dev launch (recommended test path)
```bash
cd ~/Investment_Tools
export APP_ENV=production
export ENVIRONMENT=production
export CORE_DB_BACKEND=postgres
export CORE_DB_STRICT_POSTGRES=1
export ALLOW_LOCAL_ENV_FILE=0
export AI_QUEUE_BACKEND=postgres
export AI_QUEUE_STRICT_PROD=1
export AI_ADMIN_API_ENABLED=0
export POSTGRES_DSN='postgresql://investor:investor_dev@127.0.0.1:5432/investor_os'
export AI_REDIS_URL='redis://127.0.0.1:6379/0'

pkill -f "uvicorn app.main:app" 2>/dev/null || true
lsof -ti tcp:8766 | xargs kill -9 2>/dev/null || true

./.venv-memory/bin/uvicorn app.main:app --host 127.0.0.1 --port 8766
```

### Validation
```bash
curl -sS http://127.0.0.1:8766/health/live
curl -sS http://127.0.0.1:8766/health/ready
curl -sS -X POST http://127.0.0.1:8766/ai/migration/bootstrap
```

Expected:
- live: `ok:true`
- ready: `core_db_backend:"postgres"`, `db_ok:true`
- migration bootstrap: `admin_api_disabled` when admin disabled

## 7) Remaining Work (Next Agent)
1. Add fail-fast startup validator for required env in non-dev/cloud:
   - require `POSTGRES_DSN` when `CORE_DB_BACKEND=postgres`
   - require queue env consistency
2. Cloud secret wiring:
   - Secret Manager integration + no plaintext local secrets in non-dev
3. Deployment manifests:
   - Cloud Run/GKE service split for app + worker
4. Observability:
   - alerting on readiness failures, queue depth, worker heartbeat gaps
5. Backup/restore drill in cloud target.

## 8) File Index for Fast Navigation
- Runtime entry:
  - `app/main.py`
- Core DB:
  - `app/services/postgres_core_service.py`
- AI/admin routes:
  - `app/routers/ai.py`
- Queue:
  - `app/services/ai_job_queue_service.py`
- Config/env:
  - `app/core/config.py`
  - `.env.example`
  - `docker-compose.yml`
  - `CLOUD_READINESS_CHECKLIST.md`

---

If disconnected: start from this file first, then run Section 6 validation commands before making new changes.

## 9) Current Known Issues (2026-02-22)
- Cloud preflight currently fails unless `.env` includes required cloud vars:
  - `AI_REDIS_URL` missing was observed.
- Deep analysis can still be slow by model latency; transport layer was hardened, but long LLM jobs remain variable.
- External provider/API instability (news/market feeds, model provider) can still impact freshness and response times.

## 10) Immediate Next Actions (Priority Order)
1. Cloud env completion:
   - Copy `.env.cloud.example` -> `.env`
   - Set real `POSTGRES_DSN`, `AI_REDIS_URL`, model keys
   - Run `./bin/cloud_preflight` until all checks pass
2. Cloud deployment dry run:
   - `docker compose -f docker-compose.cloud.yml up -d --build`
   - Verify:
     - `/health/live`
     - `/health/ready`
     - `/ai/worker/health`
3. AI latency/reliability validation:
   - Test quick chat (`hi`, `what else?`) -> immediate
   - Test deep query -> queued + partial status + final result
   - Confirm no repeated `Transport issue` for normal load
4. Dashboard proposals UX validation:
   - `Dismiss` should delete card immediately
   - `Feedback -> Submit` should delete card immediately
   - No duplicated `52-Week & All-Time Lows` blocks
5. Production hardening:
   - Keep admin APIs disabled by default (`AI_ADMIN_API_ENABLED=0`)
   - Enable centralized logs/alerts for queue depth, worker heartbeat, readiness failures


## 2026-02-22 Refresh
- Handoff now includes known issues + immediate next actions reflecting 2026-02-22 runtime fixes.

## 2026-02-24 Refresh (Latest)
- Applied strict runtime cutover updates:
  1. `app/services/company_lookup_service.py`
     - `market_cap_map()` no longer reads SQLite `cache.db`.
  2. `app/services/events_service.py`
     - Runtime logic is Postgres-only (SQLite branches removed).
  3. `app/routers/ai.py`
     - `/ai/command` timeout path fixed to avoid blocking shutdown waits.
     - Timeout now consistently returns queued payload with `job_id`.

- Validation completed after change:
  - `py_compile` on changed files passed.
  - `/health/live` passed.
  - `/health/ready` passed (`postgres/postgres`, `db_ok:true`).
  - `/dashboard` passed.
  - `/ai/command` no longer hangs; returns queued fallback under budget timeout.
  - `/ai/command/async` queues successfully.

- Known note for future agents:
  - If behavior seems unchanged after code patch, ensure app process was restarted from project root (`~/Investment_Tools`) and confirm active listener PID matches new launch.
  - Tooling warning about max unified exec processes is harness-side and not an app defect.

## 2026-03-02 Refresh B — Workspace OS Day Page Rebuild

### What Was Built
Full ClickUp-style personal investment workspace at `/day`:
- 3-panel layout (sidebar + main + AI panel)
- 6 spaces: Today, Inbox, Portfolio, Watchlist, Newsletter, Projects
- 4 views per space: List, Board (Kanban drag-and-drop), Calendar, Activity feed
- Quick Capture modal (Ctrl+K), emoji picker, dark mode, AI chat panel
- New DB columns: `url`, `emoji`, `project_space` on `investment_records_core`
- New endpoints: `GET /workspace/activity`, `GET /workspace/calendar-data`

### Files Changed
- `app/services/workspace_os_service.py` — new fields, `get_activity_feed()`, `get_calendar_data()`
- `app/routers/workspace_os.py` — new form params + 2 new endpoints
- `app/templates/workspace_os_day.html` — full rebuild (~1100 lines)

### Files NOT Changed
- `base.html`, `dashboard.py`, all other templates and services — untouched

### Current Status
- All py_compile checks pass
- All endpoints verified live on port 8766
- Record create/read with new fields works end-to-end

### Next Agent: Workspace Enhancements (if any)
If adding features to `/day`:
- `project_space='general'` = Today, `'portfolio'`, `'watchlist'`, `'newsletter'`, `'projects'`
- AI panel uses POST `/ai/command` with `q` body param
- Board drag-drop POSTs to `/workspace/record/{id}/status`
- Calendar dot-data fetched from `/workspace/calendar-data`

---

## 2026-03-02 Refresh (Cloud-Primary Handoff)

### Operational Truth (must follow)
- Cloud Postgres is primary source of truth.
- Local should be used for development/testing only.
- For cloud UI access: app is private and must be accessed through authenticated proxy or approved IAM principal.

### Key User-Visible Issues Observed
1. Workspace `All Activity` intermittently empty while side context showed non-zero proposals/alerts.
2. Proposal cards were repetitive/noisy and some actions did not navigate as expected.
3. Morning brief content appeared in classic view but channel/feed parity was inconsistent.
4. Transcript refresh UX confusion: queue accepted but no immediate visible transcript for some tickers (SEC source sparsity).

## 2026-03-02 Refresh C (Latest Runtime + Workspace Save/Delete)

### Cloud Deployment State
- App deployed to Cloud Run revision: `investor-tools-app-00162-fnp` (100% traffic).
- Cloud app URL remains private:
  - `https://investor-tools-app-gq4yi3pgoa-ew.a.run.app`

### Verified Cloud Behavior (direct authenticated API checks)
1. Workspace record save works:
   - `POST /workspace/record` returned `{"ok": true, "id": ...}`.
   - `GET /workspace/records` returned the saved record in `investment_records_core`.
2. Workspace record delete route fixed:
   - `POST /workspace/record/{id}/delete` now returns `404` when record does not exist.
   - For existing records, delete returns `{"ok": true}` and record is removed from `/workspace/records`.

### Workspace OS Fixes Applied
Files:
- `app/services/workspace_os_service.py`
- `app/routers/workspace_os.py`
- `app/templates/workspace_os_day.html`

Changes:
- Added schema-safety calls in workspace record/timeline/day view service paths.
- Added service logging for workspace record create/read/update/delete failures.
- Fixed day view bug where `created_at` for records was incorrectly sourced from `due_date`.
- `/workspace/record` now returns explicit error payload on failed save (no silent no-op).
- `/day` quick-add UI now surfaces save errors with alert instead of silent failure.
- `delete_record()` now returns success only when rowcount > 0.
- `/workspace/record/{id}/delete` returns 404 + error if not found.

### Critical Operational Note
- “Notes gone” reports were frequently caused by unstable/duplicate local cloud-proxy ports (not DB loss).
- Cloud data persisted correctly in `investment_records_core`.
- Standardize on one proxy entrypoint and one port at a time.

### New/Restored Utility
- `bin/open_cloud_app` restored for one-command cloud proxy open flow (kills stale process on chosen port before start).
- In restricted harness environments the proxy binary may still exit; direct authenticated cloud API checks are authoritative.

### Root Cause (All Activity blank)
- `load_workspace_feed()` had a broad failure path where one section query/schema mismatch could effectively blank feed output.
- Schema drift risk existed for optional columns (examples: `execute_route`, `brief_json`, `finished_at`, `duration_ms`, `accession`) across environments/revisions.

### Fixes Applied
File: `app/services/workspace_feed_service.py`
- Added column guards via `_column_exists(...)` for optional fields.
- Hardened section-level query blocks so one section failure does not kill full feed assembly.
- Added feed error classification/logging:
  - `_is_schema_error(exc)`
  - `_log_feed_error(section, exc)`
  - structured warning format: `workspace_feed section=<...> error_type=<schema|runtime> error=<...>`
- Removed outer behavior that returned fully empty feed on any single uncaught section error; now fail-soft with partial data.

### Current Status
- Code-level hardening is in place in repo.
- If UI still shows stale behavior, active process/revision is likely old and needs restart/redeploy.

### Next Agent Immediate Checklist
1. Ensure deployed revision includes latest `workspace_feed_service.py`.
2. Verify:
   - `GET /dashboard/feed?channel=all&limit=50` returns mixed items when data exists.
   - `GET /dashboard/feed?channel=portfolio&limit=50` and `channel=alerts` both return expected rows.
3. Tail logs and confirm no repeated `workspace_feed section=... error_type=schema`.
4. Add explicit migration for optional feed columns so runtime fallbacks remain safety-only.

### Transcript Pipeline Clarification
- Current free path is SEC-first ingestion.
- Background worker queue path exists; refresh action enqueues work.
- Missing transcripts for a ticker may be real source absence, not queue failure.
- Do not claim transcript completeness without source-level coverage checks.
