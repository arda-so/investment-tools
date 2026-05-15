# Investment Tools Ontology Maturity Report
Date: 2026-03-05
Scope: Current state, architecture, and concrete improvement plan for ontology-driven AI reasoning.

## 1) Executive Summary
Your system already has a real ontology layer and graph-based reasoning in active runtime paths. The primary bottleneck is not missing architecture; it is graph quality control (entity precision, relationship trust scoring, and source-of-truth consistency).

Key conclusion: move from "ontology exists" to "ontology is governed."

## 2) Verified Current State
### 2.1 Runtime posture
- `.env` indicates Postgres-first operation (`CORE_DB_BACKEND=postgres`, strict mode enabled).
- SQLite remains present as fallback/legacy for selected flows.

### 2.2 Live data footprint (verified)
- Postgres:
  - `watchlist_thesis_core`: 15
  - `decision_log_core`: 1260
  - `memory_compact_core`: 28
  - `report_facts_core`: 2111
  - `learning_events_core`: 294
  - `portfolio_transactions_core`: 1244
  - `investor_style_memory_core`: 8
  - `entities_core`: 94
  - `relationships_core`: 306
- SQLite:
  - `data/core.db` includes memory/decision tables (`watchlist_thesis`, `decision_log`, `memory_compact`, `report_facts`, `learning_events`, etc.)
  - `onyx_brain.db`:
    - `entities`: 428
    - `relationships`: 2164
    - `holdings`: 10
    - `evidence`: 13
    - `signals`: 164
    - `signal_audit`: 184

### 2.3 Ontology implementation already present
- Ontology schema + setup:
  - `app/services/proactive_ai_service.py` (SQLite graph tables + ontology ingest state)
  - `app/services/sec_ingest_pipeline_service.py` (Postgres `entities_core`, `relationships_core`)
- Ontology ingest:
  - `ingest_ontology_from_report_facts(...)` in `app/services/proactive_ai_service.py`
- SEC relationship extraction to graph:
  - `app/services/sec_ingest_pipeline_service.py`
- Graph simulation:
  - `simulate_macro_shock(...)` in `app/services/proactive_ai_service.py`
  - Recursive traversal over `relationships_core` for scenario propagation

## 3) Architecture Behind (As Implemented)
### 3.1 Layered flow
1. Ingestion Layer
- Reports/filings produce structured facts (`report_facts_core`).
- SEC pipeline extracts entity/relationship candidates from filings.

2. Ontology Layer
- Entity tables (`entities_core` / `entities`) and edge tables (`relationships_core` / `relationships`).
- Supported relation types: `EXPOSED_TO`, `COMPETES_WITH`, `SUPPLIER_TO`, `CUSTOMER_OF`, `SIGNALS_MACRO`.

3. Reasoning Layer
- Event mapping to seed nodes.
- Graph traversal with decay/confidence weighting.
- Portfolio/watchlist scoping before impact output.

4. Decision + Memory Layer
- Decisions: `decision_log_core`
- Thesis: `watchlist_thesis_core`
- Compact memory + style memory: `memory_compact_core`, `investor_style_memory_core`
- Learning events: `learning_events_core`

### 3.2 What this means
You are already running a digital-twin pattern:
- real-world objects (companies/themes),
- explicit relationships,
- scenario propagation over those links,
- action history and memory tied to investment workflow.

## 4) Gaps and Risks
### 4.1 Entity quality noise (highest priority)
Observed graph includes non-company tokens classified as `COMPANY` (examples in data include words like `THE`, `OUR`, `IN`, `GROUP`, `ITS`).  
Risk: contaminates traversal, peer logic, and downstream action confidence.

### 4.2 Dual-store ontology drift
- Postgres is runtime-first.
- SQLite graph contains larger but noisier graph history.
Risk: inconsistent outcomes depending on execution path and tool.

### 4.3 Fallback ingest defect
- In `ingest_ontology_from_report_facts(...)`, SQLite branch initializes `rows = []`, resulting in no ingest there.
Risk: silent mismatch in behavior when fallback path is used.

### 4.4 Schema ownership fragmentation
- Graph core schema is created in SEC ingest schema function, not centralized with broader core schema ownership.
Risk: migration and bootstrap order fragility.

## 5) Improvement Plan
## Phase 1: Graph Quality Hardening (immediate ROI)
1. Entity validation gate
- Reject stopwords/common function words for `COMPANY`.
- Require ticker validation or known company alias match for `COMPANY` inserts.
- Route unknown uppercase tokens to quarantine instead of graph.

2. Relationship quality gate
- Minimum confidence threshold by relation type.
- Require source citation class + minimum evidence length.
- Add "pending_review" status for uncertain edges.

3. Canonicalization pass
- Merge equivalent entities (ticker/name aliases).
- Backfill stable IDs (`id_text`) and normalize naming.
- Remove obvious noisy entities/edges with audit trail.

Success metrics:
- noisy COMPANY rate < 2%
- edge rejection explainability 100%
- duplicate/canonical conflicts reduced by >80%

## Phase 2: Ontology Governance
1. Type constraints
- Enforce allowed source/target type matrix per relationship type.
2. Provenance scoring
- Weighted trust by source type (SEC > curated report > heuristic extraction).
3. Health dashboards
- Track graph density, orphan nodes, low-confidence edge share, top noisy tokens.

Success metrics:
- orphan node ratio stable/downward
- low-confidence edge share below policy threshold
- weekly graph health report available to operator

## Phase 3: Runtime Consolidation
1. Postgres as canonical ontology store
- Keep SQLite graph as explicit read-only fallback or deprecate.
2. Centralize schema lifecycle
- Move ontology core table ownership to core schema bootstrap path.
3. Deterministic boot checks
- Startup validator asserts required ontology tables/indexes.

Success metrics:
- zero schema drift incidents
- zero path-dependent ontology mismatch in smoke tests

## Phase 4: Decision Explainability Upgrade
1. Action traceability
- Link each action proposal to explicit ontology paths used.
2. Hypothesis graph objects
- Formalize `Hypothesis`, `Signal`, `Evidence`, `Action` linkage records.
3. Pre-trade guardrails
- Mandatory graph impact checks for high-conviction actions.

Success metrics:
- every recommendation includes path + evidence provenance
- measurable reduction in ungrounded action proposals

## 6) Target Architecture (Next-State)
1. Source ingestion
- Reports, SEC filings, market data, operator notes.
2. Entity/edge extraction pipeline
- Deterministic validators + optional LLM reflection pass.
3. Canonical ontology store (Postgres)
- `entities_core`, `relationships_core`, ingest state, quality metadata.
4. Reasoning services
- scenario simulation, contagion/cascade alerts, thesis breach checks.
5. Decision services
- proposal queue, risk veto, execution logging.
6. Feedback loop
- accepted/rejected decisions feed learning events and extraction policy tuning.

## 7) Recommended Implementation Order
1. Phase 1 quality gates + cleanup job
2. Fallback ingest bug fix (`rows=[]` path)
3. Phase 2 governance constraints
4. Phase 3 schema/runtime consolidation
5. Phase 4 explainability enrichment

## 8) Practical Outcome for Your AI
After these upgrades, your system should move from broad-but-noisy graph reasoning to high-trust, auditable ontology reasoning that materially improves:
- recommendation precision,
- scenario reliability,
- and operator confidence in AI-generated actions.

