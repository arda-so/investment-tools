# Phase 3/4/5 Execution Plan (Intelligence-First, Local-First)

## Objective
Build deep intelligence loops first (reasoning, cause-effect, outcome tracking), THEN scale data sources and infra. Intelligence depth before data breadth.

## Strategy: Two-Lane Execution

### Lane A: Delivery (Feature Progress)
- Ships intelligence capabilities (outcome tracking, thesis breach, cross-portfolio reasoning, debate, backtesting).

### Lane B: Quality (Always Parallel)
- Enforces reliability and correctness for every feature step:
  - strict JSON schema validation
  - replay tests
  - anti-hallucination/relevance scoring
  - latency and failure budgets
  - observability and trace completeness

Rule: **No milestone closes unless both lanes pass.**

## Principles
- Intelligence-first: build reasoning depth before adding more data sources.
- No hardcoded reasoning rules in AI analysis path (ENFORCED — see 2026-02-24 session).
- All learning loops must be closed: observe → learn → apply → measure → improve.
- The AI does the analysis — never tells the user to "check" something.
- Local-first runtime, cloud-first architecture.
- Stable interfaces before infra changes.
- All AI outputs auditable, replayable, and evaluable.

---

## Completed Work (Pre-Phase 3)

### 2026-02-24 Session — Self-Learning + Financial Intelligence Overhaul

**Files changed:** `ai_orchestrator.py`, `proactive_ai_service.py`, `ai_insight_service.py`

**1. Quality Event Pipeline Fixed (ROOT CAUSE)**
- `_log_quality_event` was dead in Postgres mode (`if strict_postgres_mode(): return`). Fixed — now writes to `ai_quality_log_core`.
- `get_ai_quality_report` returned empty in Postgres mode. Fixed — reads from Postgres.
- `_recent_quality_rows` in ai_insight_service only read SQLite. Fixed — reads from `ai_quality_log_core`.

**2. All Hardcoded Learning Replaced with LLM-Driven Reasoning**
- `_build_reflexion_rule`: Was 4 hardcoded if/else rules → now LLM analyzes each failure and generates contextual rules with reasoning. Fallback to timestamped generic rule if LLM unavailable.
- `_build_missing_tool_suggestions`: Was 4 keyword clusters (options/greeks, blue chip, tax loss, supply chain) → now LLM analyzes blocked query patterns to identify ANY missing capability.
- `_build_behavioral_friction_suggestions`: Was 2 keyword patterns (blue chip, watchlist) → now LLM detects ANY friction pattern and suggests UX fixes.
- `_build_portfolio_blindspot_suggestions`: Was hardcoded keyword lists + thresholds (50%/55% concentration) → now LLM reasons over holdings + filing data for concentration, correlation, supply chain, regulatory, macro, and secondary effect risks.
- Reflexion event filter widened: added `risk_veto_blocked` and `risk_veto_review` (were silently dropped).

**3. Insight Generation Enriched with Real Financial Data**
- New `_build_financial_context(ticker)` function feeds actual numbers into insight LLM:
  - 5-year revenue, margins, FCF, debt/equity from `mini_statements_service`
  - Revenue segments (product + geography) from XBRL via `company_intel_service`
  - Buyback quarterly data from XBRL
  - Insider trade patterns from Form 4
- Pre-computes: YoY growth rates, gross margin percentages, debt/equity ratios, net cash position
- Upgraded `_evaluate_signal_reasoning` prompt: requires specific numbers in every insight, cause-effect chains, and clear conclusions (not "check the margin" but "margin dropped from 37.8% to 34.2% because...")

---

## Phase 3 Scope — Build Intelligence Loops

### 3.1) Event-Driven Zero-Click Analysis
Goal: auto-ingest new signals and create proposals without manual trigger.

Delivery:
- `events_core` table with dedupe keys.
- Source adapters:
  - `sec_edgar_poller` (local poller now)
  - `news_ingest_adapter`
- Event processor:
  - `process_event(event_id)` async path
  - idempotent retries with backoff
- Chain:
  - event -> map stage -> reduce stage -> proposal write-back

Quality:
- Dedupe correctness test (duplicate source items produce one event).
- Replay test (`event_id`) deterministic output check.
- Event latency SLO (ingest->proposal).
- Error classification dashboard (parse/model/write/network).

Acceptance:
- New SEC filing triggers one proposal set, no duplicates, replay-safe.

### 3.2) Outcome Tracking (Closing the Feedback Loop)
Goal: track whether AI proposals were RIGHT. Feed results back into learning.

Delivery:
- `proposal_outcomes_core` table:
  - `proposal_id`, `ticker`, `outcome_type(price|thesis|event)`, `measured_at`, `baseline_price`, `outcome_price`, `return_pct`, `thesis_confirmed(bool)`, `notes`
- Outcome measurement job:
  - After proposal accepted/executed: snapshot baseline price + thesis state
  - At T+7d, T+30d, T+90d: measure actual outcome (price change, thesis validity)
  - Score: was the AI right? (directional accuracy, magnitude accuracy)
- Feedback integration:
  - Feed outcome scores back into reflexion system
  - High-accuracy proposals → reinforce the reasoning pattern
  - Low-accuracy proposals → trigger reflexion rule to improve
- Dashboard widget: "AI Accuracy" showing hit rate over time

Quality:
- Outcome measurement deterministic (same proposal → same measurement).
- No look-ahead bias (baseline captured at proposal time, not retroactively).
- Score calibration: proposals marked "high confidence" should have higher hit rate.

Acceptance:
- Executed proposals automatically measured at T+7/30/90.
- AI accuracy visible on dashboard.
- Reflexion system receives outcome feedback and generates rules from misses.

### 3.3) Thesis Breach Detector
Goal: auto-compare new data against stored thesis invalidation criteria. Alert immediately.

Delivery:
- `thesis_breach_alerts_core` table:
  - `ticker`, `thesis_id`, `breach_type`, `breach_detail`, `severity`, `detected_at`, `financial_context`, `status(open|acknowledged|dismissed)`
- Breach detection engine:
  - On new filing/report ingest: load thesis + invalidation criteria for that ticker
  - Call `_build_financial_context(ticker)` to get current numbers
  - LLM compares actual financial data vs thesis expectations and invalidation criteria
  - If breach detected: create alert with specific numbers showing the breach
- Integration:
  - Runs automatically after SEC ingest pipeline processes a new filing
  - Surfaces breach alerts on dashboard (separate from proposals)
  - Breach can trigger a proposal for action (TRIM/EXIT recommendation)

Quality:
- False positive rate < 20% (breaches should be real, not noise).
- Every breach must cite specific numbers (not vague).
- Breaches include the original thesis text + what changed.

Acceptance:
- New 10-K with margin below thesis threshold triggers automatic breach alert.
- Alert shows: "Your thesis expected gross margin > 35%. Latest 10-K shows 33.2%, down from 36.1% YoY."

### 3.4) Cross-Portfolio Reasoning (Secondary Effects)
Goal: when a signal hits one holding, analyze cascading effects across the portfolio.

Delivery:
- `portfolio_cascade_alerts_core` table:
  - `trigger_ticker`, `trigger_signal`, `affected_ticker`, `effect_type`, `effect_summary`, `confidence`, `detected_at`
- Cascade analysis engine:
  - On significant signal (proposal created or breach detected):
    - Load full portfolio holdings with `_build_financial_context` for each
    - Load entity graph relationships (`COMPETES_WITH`, `SUPPLIER_TO`, `CUSTOMER_OF`, `EXPOSED_TO`)
    - LLM reasons: "Signal X hit ticker A. Given the portfolio, what are the secondary effects on tickers B, C, D?"
  - Effect types: `shared_supplier`, `same_sector_exposure`, `customer_dependency`, `regulatory_contagion`, `macro_correlation`
- Integration:
  - Cascade alerts appear alongside the triggering proposal
  - Cross-links between affected holdings

Quality:
- Cascades must cite the relationship (e.g., "AAPL and QCOM share TSMC as supplier").
- Confidence scoring per cascade (LLM estimates impact probability).
- No circular reasoning (A affects B which affects A).

Acceptance:
- Supply chain signal on AAPL triggers cascade check on other holdings sharing TSMC exposure.
- Alert: "QCOM also sources 60% of chips from TSMC. If TSMC capacity constrained, both positions at risk."

---

## Phase 4 Scope — Make It Rigorous

### 4.1) Multi-Agent Debate Framework
Goal: adversarial reasoning before proposal appears in UI.

Delivery:
- Debate stages:
  - `bull_thesis`, `bear_rebuttal`, `risk_review`, `judge_decision`
- `proposal_debate_artifacts` table.
- Gate policy:
  - relevance + anti-hallucination + risk review must pass

Quality:
- Stage completeness check (all required artifacts present).
- Contradiction detection across agent outputs.
- Judge consistency checks on repeated runs.

Acceptance:
- Every surfaced proposal has full debate trace and pass/fail rationale.

### 4.2) Backtesting Engine
Goal: measure proposal quality on historical windows.

Delivery:
- `backtest_runs`, `backtest_decisions` tables.
- Time-sliced simulation runner (future-hidden decisions).
- Metrics output:
  - hit rate, drawdown, benchmark-relative alpha proxy
- Markdown + JSON report export.

Quality:
- Reproducibility test (same run config => same outputs).
- Data leakage checks (no future fields in decision step).
- Performance budget for run completion.

Acceptance:
- One historical run completes with reproducible metrics/report.

### 4.3) Temporal Pattern Matching
Goal: "Last time this happened..." reasoning from historical data.

Delivery:
- Pattern matching engine:
  - When analyzing a signal, search historical proposals + outcomes for similar patterns
  - "Last 3 times gross margin dropped 200bps for a company like this, stock declined 15% within 2 quarters"
  - Uses outcome tracking data (Phase 3.2) as ground truth
- Integration with insight generation:
  - Historical precedents added to LLM context
  - Insights include: "Historical pattern: similar margin compression in [PEER] in 2024 led to 12% decline"

Quality:
- Pattern similarity must be meaningful (same sector, similar metrics, not random matches).
- Historical precedents must have actual outcome data (not just proposals).

Acceptance:
- Insight cards include historical precedent context when available.

### 4.4) Cost Layer + Model Routing
Goal: reserve strongest model for hard tasks only.

Delivery:
- `model_routing_policy` table:
  - `task_type`, `tier`, `model_name`, `fallback_model`, `latency_budget_ms`
- Routing engine by task class:
  - extraction/classification -> low-cost tier
  - synthesis/debate -> strong tier
- Usage telemetry by task/model.

Quality:
- QoS guard (quality floor per task class).
- Cost/latency dashboards.
- Automatic fallback logic verification.

Acceptance:
- Majority low-complexity tasks routed to low-cost tier with quality retained.

---

## Phase 5 Scope — Scale Data Sources

### 5.1) Multimodal Earnings Call Intake
Goal: transcript/audio-driven analysis with consistent contracts.

Delivery:
- `earnings_call_artifacts` table:
  - `artifact_id`, `ticker`, `kind(audio|transcript|segment)`, `uri_or_path`, `ts_start`, `ts_end`, `meta_json`
- Segment pipeline:
  - transcript chunking -> per-segment insight -> reducer aggregate
- Same proposal/insight output schema as text path.

Quality:
- Segment-to-citation linkage test.
- Hallucination guard on unsupported claims.
- Output schema conformance >= 99%.

Acceptance:
- Transcript input yields structured insight cards with evidence references.

### 5.2) Fine-Tuning Corpus Pipeline
Goal: build supervised corpus from production traces (now that system produces quality data).

Delivery:
- `llm_training_corpus` table:
  - prompt version, context hash, output JSON, human outcome, quality score
- Curator job:
  - keep schema-valid, high-quality, approved samples only
- JSONL export pipeline.

Quality:
- PII/secrets scrub checks.
- Dedup and provenance integrity checks.
- Label consistency QA sample.

Acceptance:
- Clean exportable dataset snapshots with provenance.

### 5.3) Cloud Migration
- Managed queue/scheduler/worker pools.
- Cloud storage migration for large artifacts.
- Autoscaling orchestration.

---

## Milestones (Revised with Mandatory Gates)

### M1 (1-2 weeks): Event Core + Outcome Foundation
Delivery:
1. `events_core` + dedupe indexes
2. SEC poller adapter
3. `process_event(event_id)`
4. `proposal_outcomes_core` table + baseline capture

Quality Gate:
- replay deterministic pass
- dedupe pass
- schema pass
- event latency dashboard live

### M2 (2-3 weeks): Thesis Breach + Cross-Portfolio
Delivery:
1. Thesis breach detector (auto-compare vs invalidation criteria)
2. Cross-portfolio cascade engine
3. Dashboard integration for breach alerts + cascades

Quality Gate:
- breach false positive rate < 20%
- cascade alerts cite specific relationships
- all alerts include specific numbers

### M3 (2-3 weeks): Debate + Outcome Measurement
Delivery:
1. Multi-agent debate stages + artifacts
2. Outcome measurement at T+7/30/90
3. Feedback loop: outcomes → reflexion system

Quality Gate:
- debate artifact completeness
- anti-hallucination pass rate target
- outcome measurement deterministic
- AI accuracy dashboard live

### M4 (2-3 weeks): Backtesting + Temporal + Routing
Delivery:
1. Backtest runner + reports
2. Temporal pattern matching
3. Model routing policy + cost dashboards

Quality Gate:
- no future leakage in backtests
- pattern matches meaningful (same sector)
- routing QoS pass
- cost dashboards live

### M5 (2-3 weeks): Scale (Earnings + Corpus + Cloud)
Delivery:
1. Earnings transcript pipeline
2. Training corpus curator + export
3. Cloud migration prep

Quality Gate:
- citation linkage pass
- corpus quality filters pass
- schema pass >= 99%

---

## Operating Metrics (Track Weekly)
- Proposal schema success rate
- Relevance abort rate (noise suppression)
- Hallucination incident count
- **AI accuracy rate (outcome tracking hit rate)**
- **Thesis breach detection rate (true positive rate)**
- **Cross-portfolio cascade coverage**
- End-to-end event latency (p50/p95)
- Queue depth + failure retries + DLQ volume
- Cost per 1k tasks by model tier

## Definition of Done (Phase 3/4/5 Local)
- Event-to-proposal fully automated and idempotent.
- **Proposals measured for accuracy at T+7/30/90 with feedback loop closed.**
- **Thesis breaches auto-detected with specific numbers.**
- **Cross-portfolio secondary effects analyzed on every significant signal.**
- Proposal quality gates enforce relevance/anti-hallucination.
- Debate artifacts persisted and inspectable.
- Backtests reproducible with stored configs/results.
- Temporal pattern matching provides historical precedents.
- Model routing active with quality and cost telemetry.
- Every AI output strict JSON + schema versioned.

---

## Immediate Next Sprint
1. Build `proposal_outcomes_core` table + baseline capture on proposal execution.
2. Build `thesis_breach_alerts_core` table + detection engine.
3. Build `portfolio_cascade_alerts_core` table + cascade analysis.
4. Wire breach detector into SEC ingest pipeline.
5. Add dashboard widgets for breaches + cascades.

## Refresh Log

### 2026-02-25 (Pipeline Wiring Session)

**Intelligence loops wired end-to-end.**

**1. Thesis Breach → SEC Ingest Pipeline (sec_ingest_pipeline_service.py)**
- `detect_thesis_breaches(ticker)` called automatically after each filing is processed in `process_new_filings_pipeline()`.
- Collects set of `processed_tickers` during the filing loop; runs breach detection for each after the loop completes (before `run_event_driven_monitor`).
- Import added: `detect_thesis_breaches` from `proactive_ai_service`.

**2. Cascade Analysis → Proposal Creation (proactive_ai_service.py)**
- `analyze_portfolio_cascades(trigger_ticker, signal, proposal_id)` called in `_proposal_upsert()` after a new proposal is created and mirrored.
- Wrapped in try/except so cascade failure never blocks proposal creation.

**3. Outcome Measurement Background Loop (main.py)**
- `measure_proposal_outcomes()` runs every 6 hours in a dedicated daemon thread (`outcome-measurement`).
- Staggered 60s after startup to allow full server initialization.
- Thread registered with `app.state.outcome_measurement_thread`; cleaned up on shutdown.

**4. Dashboard Data Wiring (dashboard.py)**
- `list_thesis_breach_alerts`, `list_cascade_alerts`, `get_ai_accuracy_stats` imported and called in `_render()`.
- Template context updated: `thesis_breach_alerts`, `cascade_alerts`, `ai_accuracy` passed to template.
- Template already had the AI Reports Hub tab UI (built in previous session). All three panels rendering correctly.

**Current state:**
- Dashboard shows cascade alerts (live data) and AI accuracy stats.
- Thesis breach detection runs on every new SEC filing.
- Cascades analyzed on every new proposal creation.
- Outcome measurement runs every 6h automatically.

### 2026-02-22
- Keep Phase 3/4 plans gated behind stable Phase 2 UX reliability metrics.

### 2026-02-24 (Major Revision)
- **Roadmap restructured: intelligence-first, data-breadth-second.**
- Rationale: core insight quality was poor because (a) quality event pipeline was dead in Postgres, (b) all learning was hardcoded keyword matching, (c) insight LLM had no access to actual financial data. These were fixed in the pre-Phase 3 session.
- New phases:
  - Phase 3: Intelligence loops (events, outcome tracking, thesis breach, cross-portfolio)
  - Phase 4: Rigor (debate, backtesting, temporal patterns, model routing)
  - Phase 5: Scale (earnings calls, fine-tuning corpus, cloud)
- Multimodal earnings and fine-tuning corpus moved to Phase 5 (need solid intelligence before scaling data).
- Runtime: Postgres-only, async-first AI flow, no SQLite fallback in active paths.
- Health: `/health/live` ok, `/health/ready` => `core_db_backend=postgres`, `queue_backend=postgres`, `db_ok=true`
