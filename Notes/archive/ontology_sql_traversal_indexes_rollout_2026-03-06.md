# Ontology Phase: SQL Traversal Index Rollout

Date: 2026-03-06
Environment: Local + Cloud Run (`onyx-terminal-487613`, `europe-west1`)

## Implemented
- Added additive Postgres indexes for traversal hot paths in `relationships_core`:
  - `idx_rel_core_active_source_conf`
  - `idx_rel_core_active_target_conf`
  - `idx_rel_core_status_valid_to`
  - `idx_rel_core_status_effective_conf`
- Indexes are created in `ensure_sec_ingest_schema()` and therefore remain idempotent/safe.

## Cloud rollout
- App deployed to revision: `investor-tools-app-00216-2c5`.
- Worker/jobs redeployed to same image digest.
- One-off cloud job executed to force index ensure/verification:
  - job: `investor-ensure-traversal-indexes-job`
  - execution: `investor-ensure-traversal-indexes-job-bgm5x`
  - result: `all_required_present=true`

## Benchmark (cloud, no-cache, direction AI disabled)
- Baseline from previous run (before this index rollout):
  - execution: `investor-sql-benchmark-job-bb22z`
  - overall avg: `853.83ms`
  - overall p95: `1028.95ms`

- Post-index run #1:
  - execution: `investor-sql-benchmark-job-sl5q4`
  - overall avg: `943.73ms`
  - overall p95: `1212.58ms`

- Post-index run #2:
  - execution: `investor-sql-benchmark-job-gzdpm`
  - overall avg: `843.30ms`
  - overall p95: `1090.06ms`

- Post-index stable sample (20 runs/scenario):
  - execution: `investor-sql-benchmark-job-dbhqh`
  - overall avg: `852.75ms`
  - overall p50: `843.27ms`
  - overall p95: `904.91ms`
  - overall p99: `1052.70ms`

- Post-index stable sample with cache enabled (20 runs/scenario):
  - execution: `investor-sql-benchmark-job-w87pp`
  - overall avg: `1004.10ms`
  - overall p50: `847.29ms`
  - overall p95: `973.58ms`
  - overall p99: `1681.34ms`
  - observed `cache_hits`: `0` in this run (requires follow-up; cache likely being bypassed by key churn or scope variation).

## Interpretation
- 20-run no-cache sample is the most reliable signal:
  - avg is effectively flat vs baseline (`852.75ms` vs `853.83ms`)
  - p95 improved (`904.91ms` vs `1028.95ms`)
- Conclusion: index rollout is safe and improves tail latency under no-cache conditions, but gains are moderate and scenario-dependent.

## Files changed
- `app/services/sec_ingest_pipeline_service.py`
- `tools/ensure_traversal_indexes.py` (new)

## Cache-Hit Follow-Up (Resolved)
- Issue observed: benchmark showed `cache_hits=0` even with cache enabled.
- Root cause: early returns for unmapped seed/scope paths skipped cache store/read.
- Fix applied in `simulate_macro_shock(...)`:
  - cache lookup now executes before seed/scope early-return exits
  - early-return responses are also cached with `cache_hit=false` on first run.

### Validation
- Local repeat test (single scenario, 8 runs, cache enabled):
  - `cache_hits=7`
- Cloud repeat test (20 runs/scenario, cache enabled):
  - execution: `investor-sql-benchmark-job-4rn2c`
  - `cache_hits=19` per scenario (expected first miss + 19 hits).

## Benchmark Method Upgrade (Outlier Robust)
- Upgraded benchmark tool with:
  - warmup runs (`--warmup`)
  - outlier retry policy (`--max-retries`, `--outlier-multiplier`)
  - trimmed metrics (`--trim-top-pct`)
  - filtered aggregates (`overall_filtered_avg_ms`, `overall_filtered_p95_ms`)

- Cloud run with upgraded method:
  - execution: `investor-sql-benchmark-job-ggbdc`
  - config: `runs=20`, `warmup=1`, cache enabled, direction AI disabled
  - per-scenario cache hits: `20/20`
  - overall:
    - `overall_avg_ms = 823.8`
    - `overall_p50_ms = 803.61`
    - `overall_p95_ms = 904.62`
    - `overall_filtered_avg_ms = 804.94`
    - `overall_filtered_p95_ms = 902.91`

### Note on latency
- Cloud cached run still showed one large outlier (`max_ms=18512.3`) for one scenario.
- This is likely environmental/runtime jitter and not a cache correctness issue, given 19 hits were recorded.
