# Ontology + AGE Execution Plan (Cloud + Local, Safe Rollout)
Date: 2026-03-05
Owner: Investment Tools
Mode: Safety-first, reversible, feature-flagged
Policy: Postgres-only for ontology/graph execution (no SQLite fallback paths).

## 1) Goals
1. Improve reasoning correctness and explainability.
2. Improve multi-hop graph traversal performance.
3. Deploy safely across local and cloud without breaking existing behavior.

## 2) Guardrails (Non-Negotiable)
1. Default behavior remains current SQL traversal.
2. All new logic is behind flags.
3. No destructive migration; additive schema only.
4. Shadow mode first, then partial rollout, then full rollout.
5. One-command rollback per phase.

## 3) Upgrade Scope
1. AGE traversal path (optional, feature-flagged).
2. Entity quality gates + cleanup pipeline.
3. Temporal edges + confidence decay.
4. Canonicalization (deterministic first, LLM judge async queue).
5. Anti-relationships + signed shock propagation.

## 4) Flags (Cloud and Local)
1. `ONTOLOGY_TRAVERSAL_ENGINE=sql|age` (default `sql`)
2. `ONTOLOGY_AGE_ENABLED=0|1` (default `0`)
3. `ONTOLOGY_AGE_SHADOW_MODE=0|1` (default `0`)
4. `ONTOLOGY_TEMPORAL_ENABLED=0|1` (default `0`)
5. `ONTOLOGY_DECAY_ENABLED=0|1` (default `0`)
6. `ONTOLOGY_CANONICAL_LLM_ENABLED=0|1` (default `0`)
7. `ONTOLOGY_SIGNED_PROPAGATION_ENABLED=0|1` (default `0`)

## 5) Execution Phases
## Phase A (Started): Planning + Safe Scaffolding
1. Add architecture/rollout documentation.
2. Add AGE service adapter with hard fallback to SQL (Postgres path only).
3. Add env flags in cloud/local examples.
4. Keep runtime default unchanged.

Exit criteria:
1. App behavior unchanged with default flags.
2. Tests/smoke checks pass.

Rollback:
1. Keep flags at defaults (`sql`, AGE disabled).

## Phase B: Quality First (Before Performance Switch)
1. Implement company entity gate to reject stopword-like pseudo entities.
2. Add cleanup job for noisy historical entities/edges.
3. Remove non-Postgres ontology execution paths from active rollout scope.

Exit criteria:
1. Noisy COMPANY ratio drops to target threshold.
2. No regression in ingest throughput.

Rollback:
1. Disable cleanup execution job and keep gate in observe-only mode.

## Phase C: Temporal + Decay
1. Add `valid_from`, `valid_to`, `last_verified_at`, `decay_half_life_days`, `effective_confidence`.
2. Add scheduled decay refresh job.
3. Keep old confidence fields for compatibility during migration.

Exit criteria:
1. Temporal filtering available in read path.
2. Confidence decay updates are deterministic and auditable.

Rollback:
1. Disable temporal/decay flags; old confidence path remains active.

## Phase D: Canonicalization Pipeline
1. Deterministic alias/ticker merge rules.
2. Ambiguous candidates into queue.
3. Optional LLM judge worker for queue resolution.

Exit criteria:
1. Canonical ID consistency increased.
2. Duplicate entity rate materially reduced.

Rollback:
1. Freeze queue processor; keep deterministic merges only.

## Phase E: Signed Propagation + Anti-Relationships
1. Add `HEDGES_AGAINST` and `IMMUNE_TO`.
2. Signed path scoring in macro shock simulation.
3. Maintain current unsigned mode fallback.

Exit criteria:
1. Better shock-path realism.
2. No destabilizing increase in false positives.

Rollback:
1. Disable signed propagation flag.

## Phase F: AGE Pilot (After Quality Gates)
1. Enable AGE in staging first.
2. Shadow mode compare: SQL vs AGE results.
3. 10% rollout, then 50%, then 100% if parity + latency pass.

Exit criteria:
1. p95 latency improvement target met.
2. Result parity threshold met.

Rollback:
1. Flip `ONTOLOGY_TRAVERSAL_ENGINE=sql`.

## 6) Local vs Cloud Rollout Order
1. Local dev: implement + verify first.
2. Cloud staging: shadow mode.
3. Cloud production: canary rollout.
4. Local production mirror: keep same flags for parity testing.

## 7) Immediate Next Actions (Now)
1. Complete Phase A scaffolding (in progress).
2. Start Phase B implementation (entity quality gate + Postgres-only ontology hardening).
3. Prepare additive schema migration scripts for Phase C.

## 8) Safety Checklist Before Enabling Any Flag
1. Database backup completed.
2. Smoke test for `/api/simulate_macro_shock` passed.
3. Observability check for errors/timeouts.
4. One-step rollback command verified.
