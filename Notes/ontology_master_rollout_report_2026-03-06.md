# Ontology Master Rollout Report (Consolidated)

Date: 2026-03-06  
Scope: Local + Cloud (`onyx-terminal-487613`, `europe-west1`)

## Executive Summary
- The ontology upgrade program is implemented and deployed across core phases:
  - quality hardening
  - temporal/decay
  - canonicalization
  - signed propagation + anti-relationships
  - stale anti-edge re-verification
  - AGE pilot scaffolding
  - SQL traversal acceleration + index optimization
- Cloud runtime remains Postgres/SQL as canonical engine.
- Apache AGE is blocked on current Cloud SQL runtime (`age` extension unavailable), so graph acceleration is currently delivered through SQL-path optimization.

## What Is Fully Done
1. Entity Quality Gates + Cleanup
- Noisy entity filtering and cleanup flow implemented.
- Governance-style quality checks operational.

2. Temporal + Decay Model
- `valid_from`, `valid_to`, `last_verified_at`, `decay_half_life_days`, `effective_confidence`, `status`, `signed_weight` integrated.
- Backfill and refresh jobs implemented and deployed.

3. Canonicalization Pipeline
- Deterministic-first canonicalization implemented.
- LLM-judge queue path added as controlled extension.
- Cloud canonicalization job deployed and scheduled.

4. Anti-Relationships + Signed Propagation
- Added anti-relationship semantics (`HEDGES_AGAINST`, `IMMUNE_TO`).
- Shock simulation propagates signed effects (contagion vs mitigating).

5. Stale Anti-Relationship Re-Verification
- Queue + processor implemented.
- Enqueue + worker jobs deployed and scheduled in cloud.

6. AGE Pilot (Safe Scaffolding)
- AGE status/preflight/sync scaffolding implemented.
- Cloud safety wiring complete (flags + guarded jobs).
- Gate result: AGE not installable on current managed Postgres runtime.

7. SQL Traversal Acceleration (No-AGE Path)
- Added traversal result cache + controls.
- Batched evidence query replaced N+1 lookup.
- Added traversal-focused indexes and cloud verification job.
- Added robust benchmark runner (p50/p95/p99, warmup, retry, trimmed metrics).

## Database Changes (Core)
- Main ontology tables:
  - `entities_core`
  - `relationships_core`
- Added/used temporal/decay columns on `relationships_core`.
- Added operational tables:
  - `anti_relationship_reverify_queue_core`
  - canonicalization/queue support tables
- Added traversal performance indexes:
  - `idx_rel_core_active_source_conf`
  - `idx_rel_core_active_target_conf`
  - `idx_rel_core_status_valid_to`
  - `idx_rel_core_status_effective_conf`

## Cloud Architecture Changes
- App and worker deployments now carry ontology feature flags and hard safety defaults.
- Cloud Run jobs are digest-pinned for immutable execution.
- Added/updated scheduled jobs for ontology maintenance and governance cycles.
- Worker service remains job-driven (not HTTP service driven).

## Performance and Benchmark Status
- No-cache stable benchmark (20 runs/scenario) indicates improved tail behavior after SQL optimization/indexing.
- Cache-hit anomaly was fixed (early-return path now cached).
- Latest robust cached benchmark (`investor-sql-benchmark-job-ggbdc`) shows:
  - cache hits: `20/20` per scenario
  - `overall_p50_ms = 803.61`
  - `overall_p95_ms = 904.62`
  - `overall_filtered_avg_ms = 804.94`
  - `overall_filtered_p95_ms = 902.91`

## AGE Decision (Current)
- Cloud AGE installation check result:
  - `extension "age" is not available`
  - missing `age.control` on DB runtime
- Decision:
  - keep `ONTOLOGY_TRAVERSAL_ENGINE=sql`
  - keep AGE flags disabled in production
  - continue SQL optimization track until runtime support changes.

## Safety Posture
- Rollouts are additive and idempotent.
- Feature flags default to safest modes.
- Manual + scheduled validation runs are documented in phase reports.
- Core health checks remain green with Postgres strict mode.

## Canonical Phase Reports
- `archive/ontology_phase_cloud_rollout_status_2026-03-05.md`
- `archive/ontology_phase_canonicalization_rollout_status_2026-03-05.md`
- `archive/ontology_signed_propagation_rollout_status_2026-03-05.md`
- `archive/ontology_anti_reverify_rollout_status_2026-03-05.md`
- `archive/ontology_age_phase_a_rollout_status_2026-03-05.md`
- `archive/ontology_sql_traversal_acceleration_rollout_2026-03-06.md`
- `archive/ontology_sql_traversal_indexes_rollout_2026-03-06.md`

## Current Overall Status
- Program status: **Implemented and operational on SQL-first cloud architecture**
- Next practical optimization axis:
  - continue SQL query/index tuning
  - harden benchmark governance and regression gates
  - evaluate alternate graph backend only if SQL path no longer meets latency targets.

## Post-Review Fixes (2026-03-06)
- BUG FIX: Consolidated 3 duplicate decay implementations into `app/core/ontology_math.py`.
  `_decay_confidence()` in ontology_quality_service was ignoring ONTOLOGY_DECAY_ENABLED flag.
- Consolidated `_signed_weight_for_rel()` (2 duplicates) into same shared module.
- Added 2 missing env vars to deploy scripts (`ONTOLOGY_CANONICAL_DETERMINISTIC_THRESHOLD`,
  `ONTOLOGY_CANONICAL_LLM_MIN_CONFIDENCE`).
- Removed dead `ONTOLOGY_AGE_SHADOW_MODE` env var from deploy scripts.

## Simplification Plan (Agreed)
- Merge 6 ontology cron jobs -> 1 nightly maintenance job.
- Remove AGE runtime code, jobs, and env vars.
- Reduce ontology env vars from 22 -> ~6 core knobs.
- Batch INSERT/UPDATE loops with `executemany()`.

## Simplification Execution (2026-03-06)
- Completed: merged ontology maintenance into one Cloud Run job:
  - `investor-ontology-maintenance-job`
  - runner: `bin/run_ontology_maintenance_job`
  - orchestration: `tools/run_ontology_maintenance.py`
- Completed: removed AGE runtime and job surface:
  - deleted `app/services/age_graph_service.py`
  - deleted AGE tools/jobs (`tools/age_*`, `bin/run_age_*`)
  - removed AGE wiring from app readiness and deploy scripts
  - removed legacy cloud AGE jobs (`investor-age-enable-job`, `investor-age-preflight-job`)
- Completed: reduced ontology deploy knobs to core set in cloud scripts and env examples:
  - `ONTOLOGY_TEMPORAL_ENABLED`
  - `ONTOLOGY_DECAY_ENABLED`
  - `ONTOLOGY_DECAY_HALF_LIFE_DAYS`
  - `ONTOLOGY_CANONICAL_LLM_ENABLED`
  - `ONTOLOGY_CANONICAL_DETERMINISTIC_THRESHOLD`
  - `ONTOLOGY_CANONICAL_LLM_MIN_CONFIDENCE`
  - `ONTOLOGY_SIGNED_PROPAGATION_ENABLED`
- Completed: batched ontology DB write loops via `executemany()`:
  - `enqueue_stale_anti_relationships`
  - `backfill_relationship_temporal_decay`
  - `refresh_relationship_decay_scores`
  - deterministic canonicalization queue writes

## Closeout Update (2026-03-06)
- Notes cleanup completed:
  - individual ontology phase/audit docs moved to `Notes/archive/`
  - AGE planning docs moved to `Notes/archive/`
  - `Notes/NOTES_INDEX.md` updated to current date and canonical pointers
- DB cleanup completed:
  - local Postgres: `to_regclass('public.ontology_age_sync_state_core') = NULL`
  - cloud Postgres: `to_regclass('public.ontology_age_sync_state_core') = NULL`
  - `DROP TABLE IF EXISTS ontology_age_sync_state_core` executed safely in cloud (no-op; table already absent)

## Feedback Implementation Addendum (2026-03-06)
- Implemented graph-augmented cascade reasoning in `app/services/proactive_ai_service.py`:
  - added SQL graph-chain context injection for cascade analysis prompts
  - upgraded prompts to explicit first/second/third-order reasoning steps with magnitude filtering
  - applied to both:
    - `analyze_portfolio_cascades(...)`
    - `analyze_universe_signal_for_portfolio(...)`
- Corrected ontology peer lookup SQL to current schema:
  - replaced legacy `source_entity_id/target_entity_id` with `source_id/target_id` in `_get_sector_peers(...)`
- Maintenance reporting accuracy fixes finalized:
  - `enqueue_stale_anti_relationships(...)` now captures `queued` using `cur.rowcount` after commit
  - `backfill_relationship_temporal_decay(...)` now captures updated rowcount after commit
  - `refresh_relationship_decay_scores(...)` now captures updated rowcount after commit
- Verified syntax gates:
  - `py_compile` passed for:
    - `app/services/proactive_ai_service.py`
    - `app/services/debate_service.py`
    - `app/services/earnings_transcript_service.py`
    - `app/services/portfolio_memory_service.py`
    - `tools/llm_engine.py`

## Higher-Order Reasoning Upgrade (2026-03-06)
- Added edge magnitude weighting for traversal:
  - `relationships_core.criticality_score` (default `0.5`) added to schema/migrations.
  - propagation now uses: `score = prev * conf * damp * sign * criticality`.
- Added lag-memory feedback loop:
  - new table `cascade_lag_stats_core` stores `(trigger_ticker, affected_ticker)` lag stats.
  - cascade prompts now include historical lag hints when available.
- Added recursive fan-out depth control:
  - new runtime knob `AI_CASCADE_RECURSIVE_MAX_DEPTH` (default `3`).
  - graph-chain injection now respects configured recursive depth.
- Cloud rollout completed:
  - app deployed revision: `investor-tools-app-00223-h7q`
  - image digest: `sha256:fdc07b0b0fa571fc5e15474c07f14202d81b561eac1bdb8ee2ec84d542fdfd63`
  - worker/job deployment refreshed in `europe-west1`

## Async LLM Criticality Reviewer (2026-03-06)
- Implemented asynchronous `LLM-as-a-reviewer` criticality pipeline (not inline ingestion):
  - queue schema: `criticality_review_queue_core`
  - enqueue pass: `enqueue_criticality_review_candidates(...)`
  - review pass: `process_criticality_review_queue(...)`
- Wired into nightly maintenance orchestrator:
  - `tools/run_ontology_maintenance.py` now runs:
    - `criticality_enqueue`
    - `criticality_process`
- Added runtime knobs:
  - `ONTOLOGY_CRITICALITY_REVIEW_ENABLED`
  - `ONTOLOGY_CRITICALITY_LLM_ENABLED`
  - `ONTOLOGY_CRITICALITY_LOW_CONF_MAX`
  - `ONTOLOGY_CRITICALITY_MIN_IMPACT_SCORE`
  - `ONTOLOGY_CRITICALITY_LLM_MIN_CONFIDENCE`
- Cloud deployment confirmation:
  - app revision: `investor-tools-app-00225-scx`
  - executed ontology maintenance job successfully: `investor-ontology-maintenance-job-hrcgx`

## Cascade Reasoning Upgrade (2026-03-06)
- Graph-augmented prompts: cascade LLM now receives pre-computed graph paths with hop count, dampened scores, and lag hints.
- Criticality weighting: `relationships_core.criticality_score` column, heuristic estimator, backfilled on all edges.
- Lag-memory loop: `cascade_lag_stats_core` table tracks historical cascade timing.
- Risk-theme dampening: `ONTOLOGY_RISK_THEME_HOP_FACTOR` (default `0.15`) prevents shared themes from inflating scores.
- Direction-aware traversal: supply shocks prioritize downstream, demand shocks prioritize upstream.
- Ticker alias resolution in `_event_seed_entities()`: `TSMC->TSM`, `FB->META`, `GOOG->GOOGL`.

## Entity Graph Cleanup (2026-03-06)
- Purged 41 noise entities, 57 garbage relationships.
- Added 13 missing company entities (TSM, AMZN, META, GOOGL, ASML, BABA, CAT, COST, GS, IT, JPM, NOW, PLD).
- Seeded 32 verified supply chain / competitive / risk theme edges with criticality scores.
- Post-cleanup: 42 entities, 141 active relationships, 138 with custom criticality.
- Validated: `simulate_macro_shock('TSMC supply constraint')` now returns NVDA/AAPL hop 1, MSFT/GOOGL hop 2, CRM hop 3.
