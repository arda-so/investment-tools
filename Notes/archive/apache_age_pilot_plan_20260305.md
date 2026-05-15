# Apache AGE Pilot Plan (Postgres-First, Low-Churn)
Date: 2026-03-05
Owner: Investment Tools core platform
Status: Proposed pilot

## 1) Objective
Use Apache AGE to accelerate deep graph traversal for ontology reasoning while keeping current Postgres tables and app architecture intact.

Primary target:
- Replace recursive SQL traversal in shock simulation with Cypher traversal behind a feature flag.

Non-goal for pilot:
- No full database replatform.
- No migration away from `entities_core` / `relationships_core` as source of truth.

## 2) Why This Approach
Current system is Postgres-first and already populated with:
- `entities_core`
- `relationships_core`
- `report_facts_core`
- decision/memory tables tied to existing services

AGE pilot gives graph traversal acceleration with minimal disruption:
1. Keep existing ingestion and governance pipeline.
2. Add graph execution path for selected workloads (`simulate_macro_shock` first).
3. Roll back safely by disabling one feature flag.

## 3) Target Pilot Architecture
1. Canonical store remains relational:
- `entities_core` and `relationships_core` stay authoritative.

2. Graph execution layer:
- Apache AGE graph (`ontology_graph`) as a read-optimized projection/cache for traversal queries.

3. Sync layer:
- Incremental sync from relational tables into AGE graph using `last_relationship_id` watermark.

4. Runtime selection:
- Existing SQL recursive traversal remains fallback.
- AGE traversal path enabled by flag: `ONTOLOGY_TRAVERSAL_ENGINE=age|sql`.

## 4) Data Mapping
Relational to graph mapping:
1. Vertex labels
- `COMPANY` -> `:Company`
- `RISK_THEME` -> `:RiskTheme`
- `MACRO_NODE` -> `:MacroNode`

2. Edge labels
- `EXPOSED_TO`, `COMPETES_WITH`, `SUPPLIER_TO`, `CUSTOMER_OF`, `SIGNALS_MACRO`

3. Required properties on vertices
- `entity_id` (from `entities_core.id`)
- `name`
- `normalized_name`
- `type`
- `updated_at`

4. Required properties on edges
- `rel_id` (from `relationships_core.id`)
- `relationship_type`
- `confidence_score`
- `citation_link`
- `created_at`

## 5) Implementation Plan (Phased)
## Phase A: Environment + Safety
1. Verify AGE availability in target Postgres instance.
2. Add feature flags:
- `ONTOLOGY_TRAVERSAL_ENGINE=sql` default
- `AGE_GRAPH_NAME=ontology_graph`
3. Add startup capability check and health endpoint field:
- `age_available: true/false`

## Phase B: Create AGE Projection
1. Enable extension and graph namespace (one-time):
```sql
CREATE EXTENSION IF NOT EXISTS age;
LOAD 'age';
SET search_path = ag_catalog, "$user", public;
SELECT create_graph('ontology_graph');
```
2. Initial full sync job:
- Create vertices for all rows in `entities_core`.
- Create edges for all rows in `relationships_core`.
3. Create sync state table:
```sql
CREATE TABLE IF NOT EXISTS ontology_age_sync_state_core (
  state_key TEXT PRIMARY KEY,
  state_value TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);
```

## Phase C: Incremental Sync
1. Add worker script (`scripts/age_sync_graph.py`) with id watermark:
- Read `last_relationship_id`.
- Upsert missing vertices/edges into AGE graph.
- Update watermark after commit.
2. Run schedule:
- Every 5-15 minutes initially.
- Optionally trigger after SEC ingest batches.

## Phase D: Query Adapter
1. Add service module (`app/services/age_graph_service.py`) with:
- `age_is_available()`
- `age_traverse_impact(seed_ids, max_depth, min_score)`
- `age_get_citations(path_nodes)`
2. Keep same output contract as current SQL path:
- impacts, confidence, citations, missing_variables

## Phase E: Wire Pilot Endpoint
1. Update `simulate_macro_shock(...)` in `app/services/proactive_ai_service.py`:
- choose AGE traversal when flag is `age` and availability check passes
- fallback to existing SQL CTE traversal on any failure
2. Keep API response schema unchanged to avoid frontend churn.

## Phase F: Benchmark + Gate
Benchmark scenarios:
1. 1-hop, 2-hop, 3-hop, 4-hop traversals.
2. 1 seed vs multi-seed scenarios.
3. Portfolio/watchlist scope filters.

Success gates:
1. p95 traversal latency improvement >= 40% at depth 3-4.
2. Result parity >= 95% on top impacted tickers versus SQL baseline.
3. Zero production-breaking changes with fallback path forced on error.

## 6) Concrete Code Touch Points
Primary files:
1. `app/services/proactive_ai_service.py`
- Introduce traversal engine switch in `simulate_macro_shock`.

2. `app/services/sec_ingest_pipeline_service.py`
- Optional hook to enqueue/trigger incremental AGE sync after ingest batch.

3. New: `app/services/age_graph_service.py`
- AGE SQL/Cypher execution wrapper + parsing.

4. New: `scripts/age_sync_graph.py`
- Full + incremental sync logic from core tables.

5. Optional migration helpers:
- `sql/age_enable.sql`
- `sql/age_sync_state.sql`

## 7) Cypher Shape (Reference)
Representative traversal query shape:
```cypher
MATCH (s)
WHERE s.entity_id IN $seed_ids
CALL {
  WITH s
  MATCH p = (s)-[r*1..$max_depth]-(n)
  WHERE ALL(x IN r WHERE coalesce(x.confidence_score, 0.0) >= $min_conf)
  RETURN n.entity_id AS entity_id, max(reduce(score=1.0, e IN r | score * coalesce(e.confidence_score, 0.65))) AS best_score
}
RETURN entity_id, best_score
ORDER BY best_score DESC
LIMIT $limit;
```
Note: final query details should be adjusted to the exact AGE version/behavior in your environment.

## 8) Risks and Mitigations
1. AGE extension not available in managed Postgres
- Mitigation: keep SQL traversal path; pilot in self-hosted/staging first.

2. Sync drift between relational and graph projection
- Mitigation: watermark sync + nightly full reconciliation job.

3. Output mismatch with current SQL path
- Mitigation: run both paths in shadow mode and compare before enabling user-facing results.

4. Operational complexity
- Mitigation: scope pilot to one endpoint first (`simulate_macro_shock`).

## 9) Rollout Strategy
1. Week 1
- Enable AGE in staging, run full sync, validate graph counts and parity.

2. Week 2
- Ship adapter + shadow mode comparison logging in production (no user impact).

3. Week 3
- Enable AGE for 10% traffic on macro shock endpoint.

4. Week 4
- Expand to 100% if latency and parity gates pass.

## 10) Decision Criteria: Continue or Stop
Continue if:
1. Latency and parity gates pass.
2. Ops overhead remains low.
3. No correctness regressions in top impacted tickers/citations.

Stop/rollback if:
1. AGE availability is inconsistent in target environment.
2. Sync drift exceeds tolerance.
3. Query complexity introduces instability beyond current SQL path.

## 11) Recommendation
Proceed with AGE pilot now, but only as a traversal acceleration layer, not a full data-model migration.  
Keep Postgres relational ontology tables as canonical until:
1. quality governance is hardened,
2. temporal/confidence-decay upgrades are live,
3. AGE pilot demonstrates sustained wins under production load.

