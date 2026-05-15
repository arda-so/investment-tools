# Postgres Migration Punch-List (Strict)

Last updated: 2026-02-21

## Goal
Eliminate remaining runtime dependency on `data/core.db` for core app behavior.  
Keep SQLite only for explicitly non-core stores (if intentionally retained), otherwise migrate.

## Current State
- Completed recently:
  - `company_profile_cache_core` migration and dashboard crash guards.
  - Company File domain tables migrated and wired:
    - `companies_core`
    - `company_lists_core`
    - `company_list_items_core`
    - `company_moat_tags_core`
    - `company_sec_competitors_core`
  - Proactive runtime tables migrated and wired:
    - `blue_chips_core`
    - `proactive_monitor_state_core`
    - `agent_runs_core`
    - `reflexion_notes_core`
    - `failure_patterns_core`
    - `reflexion_policy_versions_core`

## Priority 0 (Hot-path still hybrid)

### 1) `app/routers/dashboard.py`
- SQLite direct calls still present for blue chips and related helpers.
- Migrate these handlers/helpers to Postgres-backed service calls:
  - `_ensure_blue_chips_schema`
  - `_list_blue_chips`
  - `_upsert_blue_chip`
  - `_remove_blue_chip`
  - My Universe blue-chip add/remove endpoints.

### 2) `app/services/dashboard_service.py`
- Uses `_conn()` / `connect_sqlite(CORE_DB_PATH)` for:
  - `intel24_snapshot`
  - `intel_feed`
  - `changes`
- Decision required:
  1. Migrate these tables to Postgres core and rewire reads, or
  2. Reclassify as non-core telemetry DB and isolate clearly (not `core.db`).

## Priority 1 (Core workflows)

### 3) `app/services/organizer_service.py`
- Large SQLite surface (`daily_notes`, `daily_note_tags`, tasks/notes helpers).
- Migrate to Postgres adapters and remove SQLite branches in runtime path.

### 4) `app/routers/organizer.py`
- Contains direct SQLite reads for daily history/tags.
- Replace with service-layer Postgres functions only.

### 5) `app/services/portfolio_memory_service.py`
- Still heavily SQLite (`memory_compact`, interview queue/state, rules, overrides, report ingest state).
- Needs full adapter migration:
  - queue/state tables
  - rule tables
  - overrides/profile answer tables
  - memory compaction paths

### 6) `app/services/daily_operator.py`
- SQLite for state + dynamic gap persistence.
- Migrate:
  - `daily_operator_state`
  - style/thesis upserts
  - audit retrieval

## Priority 2 (AI control plane and support services)

### 7) `app/services/ai_orchestrator.py`
- Still core SQLite usage (risk veto config, action logs, blue chips, company lookups).
- Move all runtime mutable data to Postgres tables.

### 8) `app/services/user_preferences_service.py`
- SQLite `user_preferences`.
- Migrate to Postgres to align with proactive/reflexion writes.

### 9) `app/services/observability_service.py`
- SQLite system events.
- Migrate or intentionally isolate as non-core sink.

### 10) `app/services/sec_ingest_pipeline_service.py`
- Mixed mode remains (`_conn_core`, blue chip reads; onyx graph remains SQLite).
- Migrate core-side tables and keep onyx explicitly separate if desired.

### 11) `app/services/events_service.py`
- Hybrid branch remains for SQLite backend.
- In strict Postgres mode, remove/disable SQLite branch paths.

## Optional / Explicitly Separate Stores
- `onyx_brain.db`: graph/ontology store; keep only if intentional and documented.
- `cache.db`: market/cache entries; keep if intentional.

## Verification Gates (after each phase)

1. Compile
```bash
./.venv-memory/bin/python -m py_compile app/services/*.py app/routers/*.py
```

2. Migration sync/verify
```bash
curl -sS -X POST http://127.0.0.1:8766/ai/migration/bootstrap
curl -sS -X POST http://127.0.0.1:8766/ai/migration/sync-core
curl -sS http://127.0.0.1:8766/ai/migration/verify-core
```

3. Smoke tests
```bash
curl -sS http://127.0.0.1:8766/health/live
curl -sS http://127.0.0.1:8766/health/ready
```

4. Strict grep check (target: no `CORE_DB_PATH` usage in migrated files)
```bash
rg -n "CORE_DB_PATH|connect_sqlite|_conn\\(" app/services app/routers
```

## Definition of Done
- No runtime read/write path for core features touches `data/core.db`.
- `core_backend=postgres` with strict mode enabled works without fallback.
- `/dashboard`, `/company_file`, `/organizer`, `/ai/command`, `/ai/proactive/audit` all return 200.
- Migration verify shows table count parity for all migrated tables.


## 2026-02-22 Refresh
- Runtime incident fix validated: `/dashboard` 500 resolved after proposal service accumulator initialization.\n- Additional migration hygiene remains: complete cloud managed secret/env wiring and preflight pass before production cutover.

## 2026-02-24 Refresh
- Runtime update: active company market-cap lookup and events runtime paths are Postgres-only.
- AI command reliability: `/ai/command` timeout path now returns queued fallback quickly (no blocking hang).
- Health posture unchanged and valid in current run:
  - `/health/live` ok
  - `/health/ready` => `core_db_backend=postgres`, `queue_backend=postgres`, `db_ok=true`
- Continuation policy: keep async-first AI flow and avoid reintroducing SQLite fallback in active runtime request paths.
