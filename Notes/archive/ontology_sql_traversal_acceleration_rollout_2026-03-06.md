# Ontology Phase: SQL Traversal Acceleration (No-AGE Path)

Date: 2026-03-06
Environment: Local + Cloud Run (`onyx-terminal-487613`, `europe-west1`)

## Why this phase
- Cloud Postgres cannot install Apache AGE on current runtime (`age.control` missing).
- Kept Postgres SQL traversal as canonical path and improved performance/safety there.

## Implemented
1. Traversal result cache (safe, bounded, TTL)
- Added in-memory cache for `simulate_macro_shock(...)` keyed by:
  - normalized scenario text
  - depth
  - seed ids
  - scope ids
  - signed propagation mode
- New env controls:
  - `ONTOLOGY_SIM_CACHE_ENABLED` (default `1`)
  - `ONTOLOGY_SIM_CACHE_TTL_SEC` (default `300`)
  - `ONTOLOGY_SIM_CACHE_MAX_ENTRIES` (default `256`)
- Response now includes `cache_hit` boolean.

2. SQL traversal query hardening
- `_traverse_impacts_sql(...)` now filters in SQL:
  - active status only
  - positive effective confidence only
  - excludes expired edges via `valid_to`.
- Citation gather query also filters:
  - active status only
  - non-empty citation links.

3. Evidence fetch optimization
- Replaced per-company N+1 query loop with one batched SQL query using:
  - `UNNEST(%s::bigint[])`
  - window ranking per impacted company (`ROW_NUMBER() ... PARTITION BY cid`)
- Keeps top evidence rows per impacted company while reducing query round trips.

4. Direction-AI guard flag
- Added `ONTOLOGY_IMPACT_DIRECTION_AI_ENABLED` (default `1`).
- When disabled, skips the optional LLM direction classification step.
- Useful for low-latency mode and deterministic benchmarks.

5. Benchmark utility
- Added `tools/benchmark_macro_shock_sql.py`:
  - supports cache on/off
  - supports direction AI on/off
  - reports avg/p95/min/max + cache-hit counts.

## Cloud wiring
- Added new env vars to:
  - `bin/deploy_cloud_run.sh`
  - `bin/deploy_worker_job.sh`
  - `.env.cloud.example`
- Deployed app + workers with new flags and defaults.

## Verification
1. Local syntax/compile checks
- `py_compile` passed for:
  - `app/services/proactive_ai_service.py`
  - `tools/benchmark_macro_shock_sql.py`
- shell syntax checks passed for deploy scripts.

2. Local benchmark (direction AI disabled)
- Command:
  - `tools/benchmark_macro_shock_sql.py --runs 3 --max-depth 3 --no-cache`
- Result snapshot:
  - overall avg ~ `90.75ms`
  - overall p95 ~ `113.76ms`

3. Cloud deploy
- App revision: `investor-tools-app-00215-5qg` (100% traffic).
- Worker jobs redeployed and one-off execution succeeded:
  - `investor-tools-worker-job-qf7jq`.
- `/health/ready` verified via authenticated proxy:
  - `db_ok=true`
  - `age_available=false` (expected, unchanged).

4. Cloud benchmark job
- One-off job:
  - `investor-sql-benchmark-job`
  - execution: `investor-sql-benchmark-job-bb22z`
- Result snapshot (no cache, direction AI disabled):
  - overall avg ~ `853.83ms`
  - overall p95 ~ `1028.95ms`

## Files changed
- `app/services/proactive_ai_service.py`
- `tools/benchmark_macro_shock_sql.py` (new)
- `bin/deploy_cloud_run.sh`
- `bin/deploy_worker_job.sh`
- `.env.cloud.example`

## Outcome
- Achieved safe performance improvements without database replatform.
- Preserved existing output contract and SQL fallback behavior.
- Added controls to trade-off latency vs enriched direction classification when needed.
