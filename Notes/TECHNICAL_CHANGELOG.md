# Technical Changelog

---

## 2026-03-09 — Phase 0 Structural Guardrails (Safety-Only)

### Added Non-Blocking Architecture Checks
- Added import-cycle scanner:
  - `bin/check_import_cycles.py`
- Added structure scanner (file/function size drift):
  - `bin/check_code_structure.py`
- Added baseline config for architecture drift tracking:
  - `config/architecture_baseline.json`

### Quality Gate Integration
- Updated `bin/quality_gate.sh` to run both new checks in warn mode (`|| true`).
- Fixed fallback Python executable assignment in `quality_gate.sh` (`python3` fallback).

### Documentation
- Added audit:
  - `Notes/SPAGHETTI_AUDIT_2026-03-09.md`
- Added phased safe refactor backlog:
  - `Notes/SPAGHETTI_REFACTOR_TICKETS_2026-03-09.md`
- Updated canonical index:
  - `Notes/NOTES_INDEX.md`

## 2026-03-07 — Notes Refresh + Current-State Snapshot

### Documentation
- Added `Notes/APP_STATUS_2026-03-07.md` as the latest app status snapshot.
- Updated canonical index date and pointers in `Notes/NOTES_INDEX.md`.
- Added explicit current-state routing/service/template map for `/today` and `/day`.
- Archived stale planning docs to `Notes/archive/`:
  - `NEXT_7_DAYS_PLAN.md`
  - `PHASE3_PHASE4_EXECUTION_PLAN_LOCAL_FIRST.md`

### `/today` Implementation Snapshot
- Route mapping confirmed:
  - `GET /today` and `GET /day` -> `day_view()` in `app/routers/workspace_os.py`
- Service mapping confirmed:
  - `get_day_view()` in `app/services/workspace_os_service.py`
  - merges `investor_annotations_core`, `action_proposals_core`, `investment_records_core`
- Template mapping confirmed:
  - `app/templates/workspace_os_day.html` (full workspace template, `themeDarkV1` key)

### Recovery + Backup Traceability
- Captured recovery backup bundle under `backups/recovery_20260307_124041/`.
- Recorded cloud export location for recovery artifacts:
  - `gs://onyx-terminal-487613-runtime-files/backups/recovery_20260307_124041/`

## 2026-03-06 — AI Brain Upgrade + Cascade Reasoning + Graph Cleanup

### AI Brain Improvements (8 features)
- Fact-check layer: LLM output validated against injected financial data.
- QA verifier temperature: `1.0 -> 0.2` (deterministic quality gate).
- Debate context expansion: `500 -> 1500` chars for judge, `250-350` words for advocates.
- Earnings transcript: two-pass compression (opening + Q&A), `8K -> 12K` char budget.
- Financial context budget: `3600 -> 5000` chars.
- Reflexion rules: time-decay weighting (60-day half-life).
- Data freshness annotations on macro/portfolio context.
- Structured `[AI_CALL]` logging: provider, model, mode, latency, cache hit/miss.

### Cascade Reasoning Overhaul
- Graph-augmented cascade prompts: LLM now sees pre-computed graph paths before reasoning.
- Chain-of-thought: forced STEP 1 -> 2 -> 3 -> magnitude filter in cascade prompts.
- Criticality-weighted propagation: relationships scored `0.05-1.0` by supply-chain importance.
- Lag-memory feedback loop: `cascade_lag_stats_core` tracks historical timing patterns.
- Recursive depth control: `AI_CASCADE_RECURSIVE_MAX_DEPTH` env var (default `3`).
- Risk-theme supernode dampening: `ONTOLOGY_RISK_THEME_HOP_FACTOR` reduces noise from shared themes.
- Direction-aware traversal: supply constraints trace downstream, demand shocks trace upstream.
- Ticker alias mapping: `TSMC->TSM`, `FB->META`, `GOOG->GOOGL` in seed resolution.

### Entity Graph Cleanup
- Purged 41 noise entities (English words misclassified as companies).
- Added 13 missing tickers (TSM, AMZN, META, GOOGL, ASML, etc.).
- Seeded 32 real supply chain / competitive / risk theme relationships.
- Backfilled criticality scores on all existing relationships.
- Merged ADOBE duplicate into ADBE.

### New Env Vars
- `AI_QA_VERIFIER_TEMPERATURE`, `AI_FACTCHECK_MAX_FLAGS`, `AI_FINANCIAL_CTX_MAX_CHARS`,
  `AI_REFLEXION_DECAY_HALF_LIFE_DAYS`, `AI_REFLEXION_MIN_SCORE`, `AI_REFLEXION_MIN_CONFIDENCE`,
  `AI_CALL_LOG_ENABLED`, `AI_CASCADE_RECURSIVE_MAX_DEPTH`, `ONTOLOGY_RISK_THEME_HOP_FACTOR`,
  `DEBATE_SIGNAL_CHARS`, `DEBATE_REASONING_CHARS`, `DEBATE_ADVOCATE_CHARS`,
  `DEBATE_JUDGE_SIGNAL_CHARS`, `DEBATE_ADVOCATE_WORDS`, `EARNINGS_ANALYSIS_MAX_CHARS`,
  `EARNINGS_OPENING_RATIO`.

### Dark Mode (app-wide)
- Unified localStorage key (`themeDarkV1`) across all pages.
- Visible toggle button in global nav.
- CSS variable coverage extended to dashboard and company detail pages.

## 2026-03-06 — Ontology Simplification Closeout

### Scope
Finalized post-review ontology simplification and documentation cleanup across local + cloud.

### Runtime / Jobs
- Consolidated ontology maintenance into one Cloud Run job:
  - `investor-ontology-maintenance-job`
  - runner: `bin/run_ontology_maintenance_job`
  - orchestrator: `tools/run_ontology_maintenance.py`
- Removed legacy ontology cron jobs and AGE jobs from cloud scheduler/job inventory.

### Codebase Cleanup
- Removed AGE runtime path/files (`app/services/age_graph_service.py`, `tools/age_*`, `bin/run_age_*`).
- Confirmed proactive simulation path is SQL-only in `app/services/proactive_ai_service.py`.
- Batched ontology writes with `executemany()` in:
  - `app/services/ontology_quality_service.py`
  - `app/services/entity_resolution_service.py`
- Removed dead one-time tool:
  - `tools/backfill_relationship_temporal_decay.py`
- Implemented graph-augmented cascade reasoning upgrades in:
  - `app/services/proactive_ai_service.py`
  - injected SQL-derived chain context into cascade prompts
  - enforced explicit first/second/third-order reasoning + magnitude filtering in cascade prompts
- Fixed legacy ontology column usage in peer lookup:
  - `_get_sector_peers()` now uses `relationships_core.source_id/target_id` (not `source_entity_id/target_entity_id`)
- Finalized maintenance rowcount accuracy after batched writes/commit in:
  - `app/services/ontology_quality_service.py`
- Added higher-order cascade upgrades:
  - `relationships_core.criticality_score` schema + ingestion defaults
  - lag-memory table `cascade_lag_stats_core`
  - recursive fan-out depth knob `AI_CASCADE_RECURSIVE_MAX_DEPTH`
  - SQL traversal now includes criticality weighting and lag hints in cascade prompts

### Cloud Deployment
- Deployed app revision `investor-tools-app-00223-h7q` in `europe-west1`.
- Deployed refreshed worker/jobs via `bin/deploy_worker_job.sh`.

### Async Criticality Reviewer
- Added asynchronous criticality review queue and processor:
  - `criticality_review_queue_core`
  - `enqueue_criticality_review_candidates(...)`
  - `process_criticality_review_queue(...)`
- Integrated into `tools/run_ontology_maintenance.py` so review occurs in nightly background maintenance.
- Added new ontology criticality env knobs in examples and deploy scripts.

### Cloud Refresh (Post-addition)
- Deployed app revision `investor-tools-app-00225-scx` in `europe-west1`.
- Re-deployed Cloud Run jobs to new image digest and executed:
  - `investor-ontology-maintenance-job-hrcgx` (successful).

### Notes / Reporting
- Archived obsolete phase/audit docs and AGE planning docs to `Notes/archive/`.
- Updated canonical index `Notes/NOTES_INDEX.md` (date + pointers).
- Updated `Notes/ontology_master_rollout_report_2026-03-06.md` to reflect simplification closeout.

### DB Verification (Item 4 close)
- Local Postgres: `to_regclass('public.ontology_age_sync_state_core') = NULL`
- Cloud Postgres: `to_regclass('public.ontology_age_sync_state_core') = NULL`
- Executed `DROP TABLE IF EXISTS ontology_age_sync_state_core` in cloud (safe no-op; table already absent).

---

## 2026-03-02 — Workspace OS Day Page Rebuild

### Scope
Full rebuild of `/day` as a ClickUp-style personal investment workspace.
Touches: `workspace_os_service.py`, `workspace_os.py`, `workspace_os_day.html`, `investment_records_core` table.
Does NOT touch: `base.html`, `dashboard.py`, any other template or service.

### DB Migration (additive only)
`app/services/workspace_os_service.py` → `ensure_workspace_os_schema()`
- Added `ALTER TABLE IF NOT EXISTS` for 3 new columns:
  - `url TEXT NOT NULL DEFAULT ''`
  - `emoji TEXT NOT NULL DEFAULT ''`
  - `project_space TEXT NOT NULL DEFAULT 'general'`

### Service Changes
`app/services/workspace_os_service.py`
- `create_record()` now accepts and persists `url`, `emoji`, `project_space`
- `update_record()` allowed-set extended with `url`, `emoji`, `project_space`
- Added `get_activity_feed(limit)` — reads proposals, cascade alerts, thesis alerts, filings, agent runs, records; returns sorted chronological feed with emoji/action metadata
- Added `get_calendar_data(project_space, year, month)` — reads `investment_records_core` (due_date filtered) + `earnings_calendar_snapshot_core`

### Router Changes
`app/routers/workspace_os.py`
- `api_create_record` accepts `url`, `emoji`, `project_space` Form params
- Added `GET /workspace/activity` → calls `get_activity_feed()`
- Added `GET /workspace/calendar-data` → calls `get_calendar_data()`

### Template: Full Rebuild (~1100 lines)
`app/templates/workspace_os_day.html`
- 3-panel layout: collapsible sidebar (220px/52px) + main content + AI panel (0/320px)
- 6 spaces: Today 🏠, Inbox 📬, Portfolio 📈, Watchlist 👁, Newsletter ✍️, Projects 🎯
- 4 views: List, Board (Kanban drag-and-drop), Calendar (month grid), Activity feed
- Today space: server-rendered via Jinja (`focus`, `agent_flagged`, `backlog`); other spaces: client-side fetched
- Board drag: HTML5 drag API, POSTs status to `/workspace/record/{id}/status`
- Calendar: fetches `/workspace/calendar-data`, renders 7-col month grid with dots
- Activity: fetches `/workspace/activity`, renders chronological feed
- AI panel: chat via `/ai/command` POST endpoint
- Quick Capture modal: `Ctrl+K`, supports emoji picker, project_space, url, priority
- Dark mode: toggles `body.theme-dark` class (inherits CSS variables from `base.html`)
- localStorage: persists space, view-per-space, sidebar state, AI panel state, dark mode
- Keyboard shortcuts: `Ctrl+K`=Quick Capture, `Escape`=close modal/AI

### Verified Live (2026-03-02)
- `GET /day` → 200, all 12 layout/JS landmarks present
- `GET /workspace/activity` → returns feed items (5 in test)
- `GET /workspace/calendar-data` → year:2026, month:3
- `GET /api/sidebar-data` → portfolio (4), watchlist (19)
- `POST /workspace/record` with emoji/url/project_space → id:1 saved correctly

---

Date: 2026-02-21
Scope: Dashboard/AI architecture, Postgres cutover hardening, cloud-readiness baseline

---

Date: 2026-02-22
Scope: AI chat latency/reliability and dashboard proposal UX stability

## AI Command Routing / Latency
- `app/routers/ai.py`
  - Added dynamic quick/deep short-query router (`_fast_dynamic_chat_reply`) with strict JSON contract.
  - Kept deterministic fast data routes for DB-backed queries.
  - Removed fixed greeting keyword dependence from fast-path behavior; short conversational fallback is generic.
  - SSE partial timing made configurable via `AI_SSE_PARTIAL_AFTER_SEC` (default 2s).

## Chat Transport Recovery
- `app/templates/base.html`
  - Improved SSE robustness with reconnect attempts and immediate job-result recovery polling.
  - Increased recovery polling window for long-running jobs.
  - Deep queued jobs now show an in-progress assistant row whose text is sourced from backend `partial` event payloads.

## Dashboard Proposal Actions (Performance + Bug Fix)
- `app/routers/dashboard.py`
  - HTMX dismiss/reject now return `204` in HX flow (no full page render).
  - Rejection learning moved to `BackgroundTasks` (non-blocking).
- `app/templates/components/dashboard_body.html`
  - Action labels simplified: `Dismiss`, `Feedback`, `Submit`.
  - `Dismiss` and `Feedback Submit` use `hx-swap=\"delete\"` on closest proposal card.
  - Prevents accidental HTML injection/duplication bug (duplicate `52-Week & All-Time Lows` block).

## Stability Fix
- `app/services/proactive_ai_service.py`
  - Fixed `list_action_proposals()` crash path (`UnboundLocalError: out`) by initializing accumulator safely.
  - Resolved intermittent dashboard `500 Internal Server Error` from proposal rendering path.

## Cloud Readiness Artifacts Added
- `.env.cloud.example`
- `docker-compose.cloud.yml`
- `bin/cloud_preflight`
- `docs/CLOUD_GO_LIVE_CHECKLIST.md`

Preflight currently fails fast if required env is missing (e.g., `AI_REDIS_URL`), by design.

## Platform and Runtime
- `app/main.py`
  - Added `/health/live`
  - Added `/health/ready` (returns backend + db status + queue backend)
  - Startup still performs schema/bootstrap checks, now compatible with strict Postgres readiness.

- `app/core/config.py`
  - Added non-dev-safe local env behavior:
    - `ALLOW_LOCAL_ENV_FILE` controls `.env` file loading.
    - Default in non-dev is off.
  - Added helpers:
    - `runtime_env_name()`
    - `is_non_dev_env()`
    - `allow_local_files()`

## Queue and Worker
- `app/services/ai_job_queue_service.py`
  - Added public `queue_backend()` to expose active backend in health/readiness output.

## Postgres Core Cutover and Guarding
- `app/services/postgres_core_service.py`
  - Added `_is_non_dev_env()`.
  - Added `verify_postgres_core_ready()`:
    - Direct Postgres connection check.
    - Required `_core` table presence check.
  - Updated `guard_core_backend_cutover()`:
    - In non-dev, no silent downgrade to sqlite.
  - Updated `enforce_strict_postgres_ready()`:
    - Uses `verify_postgres_core_ready()` (not sqlite parity).
    - Emits detailed failure reason (`strict_postgres_verify_failed:<reason>`).
  - Updated `pg_connect()`:
    - pool self-heal (reset + retry once on stale pool failure).

## API/Admin Safety
- `app/routers/ai.py`
  - Added `_admin_guard(request)`.
  - Guarded endpoints:
    - `/ai/phase2/bootstrap`
    - `/ai/migration/status`
    - `/ai/migration/bootstrap`
    - `/ai/migration/sync-core`
    - `/ai/migration/verify-core`
    - `/ai/migration/guard-core`
    - `/ai/events/bootstrap`
    - `/ai/learning/run`
    - `/ai/learning/promote`
  - Guard behavior:
    - In non-dev: disabled by default unless `AI_ADMIN_API_ENABLED=1`.
    - Optional header key check when `AI_ADMIN_API_KEY` is set.

## Core Runtime SQL Migration Work
- Major runtime paths shifted to Postgres `_core` access across key services:
  - `app/services/agent_service.py`
  - `app/services/dashboard_service.py`
  - `app/services/company_file_service.py`
  - `app/services/portfolio_memory_service.py`
  - `app/services/proactive_ai_service.py`
  - `app/services/sec_ingest_pipeline_service.py`
  - `app/routers/dashboard.py`
- Legacy runtime SQLite core-table read/write paths were removed or bypassed in Postgres mode.

## Container / Compose
- `docker-compose.yml`
  - App healthcheck uses `/health/ready`.
  - `restart: unless-stopped` added for app and worker.

- `.env.example`
  - Added cloud-safe baseline vars:
    - `CORE_DB_BACKEND=postgres`
    - `CORE_DB_STRICT_POSTGRES=1`
    - `CORE_DB_GUARD_ENFORCE=1`
    - `POSTGRES_DSN=...`
    - `AI_QUEUE_BACKEND=postgres`
    - `AI_QUEUE_STRICT_PROD=1`
    - `AI_REDIS_URL=...`
    - `ALLOW_LOCAL_ENV_FILE=1` (set `0` in cloud/non-dev)
    - `AI_ADMIN_API_ENABLED=1` (set `0` in cloud/non-dev)
    - optional `AI_ADMIN_API_KEY`

## Operational Docs
- Added:
  - `CLOUD_READINESS_CHECKLIST.md`
  - `AGENT_CONTINUATION_HANDOFF.md`
  - `archive/EXECUTIVE_SUMMARY.md` (historical; active index is `NOTES_INDEX.md`)
  - `TECHNICAL_CHANGELOG.md`
  - `NEXT_7_DAYS_PLAN.md` (planned alongside this changelog)

## Verified Behavior Snapshot
- `GET /health/live` returns `ok:true`.
- `GET /health/ready` returns:
  - `core_db_backend:"postgres"`
  - `queue_backend:"postgres"`
  - `db_ok:true`
- `POST /ai/migration/bootstrap` in non-dev with admin disabled returns:
  - `{"detail":"admin_api_disabled"}`


## 2026-02-22 Refresh
- Added cloud-readiness artifacts: `.env.cloud.example`, `docker-compose.cloud.yml`, `bin/cloud_preflight`, `docs/CLOUD_GO_LIVE_CHECKLIST.md`.\n- Added AI latency/reliability updates and dashboard proposal UX fixes.\n- Fixed dashboard 500 from `UnboundLocalError` in proposal service.

---

Date: 2026-02-23
Scope: Data-first SEC policy hardening, company intel automation, cloud runbook finalization

## Data-First AI Policy (Global)
- `app/core/ai_data_policy.py`
  - Added runtime guard for SEC quantitative extraction attempts without structured data context.
  - Standard fallback message: `Data not available in structured filings.`
- `tools/llm_engine.py`
  - Guard enforced in central `ask_ai_with_meta()` path.
  - Added runtime telemetry (`calls`, `errors`, avg latency) and basic circuit breaker/cooldown.

## CI/Quality Gate Expansion
- `bin/check_ai_data_policy.py`
  - Added semantic prompt checks to block legacy SEC numeric extraction patterns.
  - Added strict prompt-block scanning (LHS prompt assignment based) for deterministic CI behavior.
- `bin/quality_gate.sh`
  - Now includes:
    - AI data-policy audit
    - `tools/eval_data_first_policy.py`

## SEC Ingestion Pipeline Hardening
- `app/services/sec_ingest_pipeline_service.py`
  - Replaced extractor-style filing prompt behavior with synthesizer policy language.
  - Added structured financial context dependency before quantitative interpretation.
  - Added strict analysis JSON normalizer `_validate_analysis_json()` prior downstream writes.
  - Added auto-refresh hook into filing processing for company intel updates.

## Company Intel + Company File
- `app/services/company_intel_service.py`
  - Added `maybe_refresh_company_intel_on_filing()`:
    - triggers only on relevant forms (`10-K`, `10-Q`, `20-F`, `40-F`)
    - refreshes only when stale/missing/newer filing.
- `app/services/company_file_service.py`
  - Added `data_coverage` model (`quant_ready|qual_only|missing`) with freshness metadata.
  - Added provenance metadata and financial delta payload.
- `app/templates/components/company_detail_body.html`
  - Added coverage/status badge in header.
  - Added YoY delta presentation and provenance line.
  - Added Revenue Segmentation / Buybacks / Insider section rendering.

## Financial APIs and Telemetry Endpoints
- `app/services/mini_statements_service.py`
  - Added schema/provenance fields to historical financial responses.
  - Added `compute_financial_deltas()`.
- `app/routers/company_file.py`
  - Added `GET /api/company/financial-deltas`.
- `app/routers/ai.py`
  - Added `GET /ai/runtime/metrics`.

## Integrity and Automation
- `tools/integrity_financial_data.py`
  - Coverage/staleness integrity checker for structured financial pipeline.
- `automations/v2_daily.sh`
  - Added daily integrity run.
- `automations/terminal_weekly.sh`
  - Added weekly integrity run.
- `automations/v2_financial_integrity.sh`
  - Dedicated integrity automation script.
- `bin/run_financial_integrity`
  - Runner for integrity automation.

## Cloud Documentation Updates
- Added:
  - `docs/GCP_CUTOVER_RUNBOOK.md`
  - `docs/GCP_CUTOVER_RUNBOOK_FILLED.md` (copy/paste deployment flow)
  - `START_HERE.md` at repository root.
- Updated:
  - `docs/CLOUD_GO_LIVE_CHECKLIST.md` links to new runbooks.

---

Date: 2026-02-24
Scope: Postgres-only runtime hardening + AI sync timeout reliability

## Postgres-Only Runtime Tightening
- `app/services/company_lookup_service.py`
  - Removed SQLite `cache.db` fallback from `market_cap_map()`.
  - Runtime lookup path is now Postgres-only.

- `app/services/events_service.py`
  - Removed SQLite runtime branches and imports.
  - `ensure_events_schema`, `create_event`, `get_event`, `list_events`, `_update_event_row` now run on Postgres only.
  - Returns explicit backend-required errors if Postgres backend is not active.

## AI Command Reliability
- `app/routers/ai.py`
  - Fixed sync endpoint timeout behavior in `/ai/command`.
  - Replaced blocking executor context-manager shutdown with non-blocking shutdown:
    - `shutdown(wait=False, cancel_futures=True)`
  - Prevents timeout path from hanging while worker thread continues.
  - Timeout now cleanly returns queued response (`analysis_queued`) with `job_id`.

## Cleanup
- `app/services/dashboard_service.py`
  - Removed dead helper `_safe_count()` and unused `sqlite3` import.

## Validation Snapshot
- `python3 -m py_compile app/services/company_lookup_service.py app/services/dashboard_service.py app/services/events_service.py app/routers/ai.py` passed.
- `GET /health/live` passed.
- `GET /health/ready` passed with:
  - `core_db_backend:"postgres"`
  - `queue_backend:"postgres"`
  - `db_ok:true`
- `GET /dashboard` passed.
- `POST /ai/command` now returns quickly via queued fallback instead of hanging.
- `POST /ai/command/async` queues correctly.

## Operational Note
- Warning `maximum number of unified exec processes` is harness/process-pool pressure, not an app runtime bug.

---

Date: 2026-03-02
Scope: Workspace feed reliability, cloud-primary UX consistency, handoff hardening

## Workspace Feed Reliability (`/dashboard/feed`)
- `app/services/workspace_feed_service.py`
  - Added `_column_exists(cur, table, column)` helper for schema-tolerant queries.
  - Added `_is_schema_error(exc)` and `_log_feed_error(section, exc)` for structured warning logs.
  - Hardened section blocks (`morning-brief`, `alerts`, `filings`, `portfolio`, `notes`, `agent-runs`) to fail-soft independently.
  - Removed outer full-wipe behavior where single exception could collapse `All Activity` into empty list.

## Schema-Drift Tolerance Added
- Optional-column handling added for:
  - `morning_briefs_core.source`
  - `morning_briefs_core.brief_json`
  - `action_proposals_core.execute_route`
  - `filings_core.accession`
  - `agent_runs_core.finished_at`
  - `agent_runs_core.duration_ms`
  - `agent_runs_core.updated_at`
  - `agent_runs_core.error_text`

## Why This Change
- User observed: `All Activity` empty while side context still showed non-zero proposals/alerts.
- Root cause: feed assembly was brittle to schema differences between environments/revisions.
- Result after patch: partial feed remains available even if one subsection query fails.

## Remaining Hardening (next step)
1. Add DB migration(s) to normalize optional feed columns.
2. Keep runtime guards as safety net, not primary compatibility layer.
3. Add feed health endpoint/check script to detect schema drift pre-UI.

---

Date: 2026-03-02 (Latest)
Scope: Workspace OS save/delete reliability + cloud verification

## Workspace OS Save/Delete Fixes
- `app/services/workspace_os_service.py`
  - Added explicit schema ensure calls in workspace record/day/timeline paths.
  - Added logging in record CRUD failure branches.
  - Fixed `get_day_view()` record mapping bug:
    - `created_at` now comes from `created_at` field (not `due_date`).
  - `delete_record()` now returns `True` only when a row is actually deleted (`rowcount > 0`).

- `app/routers/workspace_os.py`
  - `POST /workspace/record` now returns explicit error payload and HTTP 500 when save fails.
  - `POST /workspace/record/{id}/delete` now returns HTTP 404 + `"Record not found."` when no row deleted.

- `app/templates/workspace_os_day.html`
  - Quick-add now shows explicit save error alerts on failed API responses/network failures (no silent drop).

## Cloud Deployments
- Deployed revision `investor-tools-app-00161-8x6` (save visibility fixes).
- Deployed revision `investor-tools-app-00162-fnp` (delete route correctness).

## Live Verification (Cloud, authenticated direct calls)
- Create:
  - `POST /workspace/record` returned `{"ok": true, "id": ...}`
  - `GET /workspace/records` returned created record.
- Delete:
  - Non-existent id returns HTTP 404 with `{"ok": false, "error": "Record not found."}`
  - Existing id returns `{"ok": true}` and record disappears from `GET /workspace/records`.

## Operational Note
- Multiple stale local proxy instances on different ports were causing session inconsistency and the perception of “lost notes.”
- Data persistence in cloud Postgres (`investment_records_core`) is confirmed.
