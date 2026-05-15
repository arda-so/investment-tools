# Recent Architecture (System + AI)

As of: 2026-02-21  
Scope: full runtime architecture of Investor OS (web app, AI command path, worker queue, dashboard, phase2 map/reduce, storage).

## 1) Topology

- HTTP app: FastAPI (`./bin/run_v2_app`) on `127.0.0.1:8766`
- AI worker: background processor (`./bin/run_ai_worker`)
- Queue backend: Postgres by default (`AI_QUEUE_BACKEND=postgres`)
- UI push channel: SSE (`/ai/command/sse/{job_id}`)
- Local render stack: server-side templates + JS command panel (`app/templates/base.html`)

High level split:

- Fast path: deterministic DB/tool responses (sub-second when possible)
- Deep path: queued async AI reasoning

## 2) Request/Response Paths

### 2.1 Synchronous AI

- Endpoint: `POST /ai/command`
- Router: `app/routers/ai.py::_ai_command_sync`
- Behavior:
  - injects runtime context + memory
  - runs orchestrator (`run_ai_command`)
  - appends user/assistant messages to chat memory
  - returns structured response (`intent`, `status`, `message`, `traces`, `action`, `ui`)

### 2.2 Asynchronous AI

- Queue submit: `POST /ai/command/async`
- Status stream: `GET /ai/command/sse/{job_id}`
- Poll fallback: `GET /ai/command/result/{job_id}`
- Queue implementation: `app/services/ai_job_queue_service.py`

Flow:

1. UI submits async payload.
2. If fast deterministic route applies, returns done immediately.
3. Else enqueue job (`ai_command_jobs`) and return `job_id`.
4. Worker claims and executes.
5. UI receives result via SSE; if SSE drops, UI uses `/ai/command/result/{job_id}` fallback.

## 3) AI Queue + Worker

### 3.1 Queue Schema

Primary table (Postgres):

- `ai_command_jobs`
- statuses: `queued | running | done | error`
- heartbeat fields used for stale job reclaim

Auxiliary:

- `ai_worker_heartbeats`
- `ai_command_cache` (query/session hash cache)

### 3.2 Worker Runtime

File: `tools/run_ai_worker.py`

- Claims jobs via `claim_next_job`
- Maintains heartbeat while processing
- Handles job types:
  - default chat command
  - `map_task`
  - `reduce_task`
- Timeout budget:
  - `AI_WORKER_JOB_TIMEOUT_SEC` (current default path raised to 90s)
- Timeout handling fix:
  - executor now uses non-blocking shutdown (`wait=False`) so timed-out jobs fail cleanly instead of hanging

## 4) Phase 2 Map/Reduce

### 4.1 Endpoints

- `POST /ai/phase2/map-reduce/sector`
- `GET /ai/phase2/map-reduce/{reducer_job_id}`
- `GET /ai/phase2/map-reduce/report/{reducer_job_id}`
- `GET /ai/phase2/status`

### 4.2 Storage

Postgres table:

- `phase2_sector_reports`
  - `reducer_job_id` (primary key)
  - `session_id`
  - `tickers_json`
  - `question`
  - `result_json`
  - `summary_text`

### 4.3 UI Integration

Dashboard command panel launches map/reduce through same `Send` action (single primary CTA).

Reducer completion:

- pushes result to chat
- stores latest reducer id client-side as analysis artifact

## 5) Analysis Threading (No Hardcoded Investment Logic)

### 5.1 Problem solved

Follow-up prompts like “did you learn anything from that report?” sometimes drifted to generic report search instead of the just-finished sector analysis.

### 5.2 Current solution

- `analysis_id` threading:
  - reducer job id is persisted as latest analysis artifact in client context
  - next chat sends `context.latest_analysis_id`
- backend resolves artifact by id (`get_map_reduce_report`)
- resolved artifact is injected as `ctx.latest_analysis`
- follow-up resolver answers from artifact context when router intent drifts

### 5.3 Observability

Chat message metadata now includes `analysis_id` when present.
Traces include `analysis.id`.

## 6) Dashboard Data Architecture

Primary dashboard composition:

- template: `app/templates/components/dashboard_body.html`
- service: `app/services/dashboard_service.py`
- router: `app/routers/dashboard.py`

News wire strategy:

1. Try live vendor feeds first (Finnhub general + company)
2. fallback: Yahoo ticker news
3. fallback: local feed/report facts
4. de-dupe + relevance filter + recency cutoff

Recent additions:

- Live/Fallback mode badge
- provider health banner (down/partial)
- wire freshness age filter (`NEWS_WIRE_MAX_AGE_HOURS`)

## 7) Data Stores

### 7.1 Postgres (concurrency-critical / phase2)

- `ai_command_jobs`
- `ai_worker_heartbeats`
- `ai_command_cache`
- `phase2_sector_reports`
- phase2 vector-ready tables:
  - `sec_signal_chunks` (with `vector` column)
  - `action_proposals_mirror`

### 7.2 SQLite (legacy + app domain data)

Core domain still resides largely in `data/core.db`:

- portfolio, watchlist, notes, tasks, thesis, report facts, proposals, chat memory, etc.

Current state is hybrid:

- queue + phase2 are Postgres-backed
- much of product-domain read/write still SQLite-backed

## 8) AI/Reasoning Layer

Primary orchestrator:

- `app/services/ai_orchestrator.py`

Characteristics:

- manager + worker registry
- route classification + policy gates
- react/tool paths for deterministic intents
- LLM fallback when needed
- quality logs and traces

Design rule in effect:

- no hardcoded investment conclusions
- hard constraints only for transport, routing, reliability, and safety gates

## 9) Current Operational Endpoints

- `GET /ai/worker/health`  
  queue depth, counts, active workers, recent errors

- `GET /ai/phase2/status`  
  postgres readiness + pgvector readiness

- `GET /ai/command/result/{job_id}`  
  non-SSE fallback retrieval

## 10) Known Limits / Remaining Work

1. Full domain migration to Postgres is not complete (hybrid state).
2. Some long-running deep reasoning jobs can still approach timeout under load.
3. Follow-up resolver is context-driven but still depends on artifact presence and healthy worker execution.
4. Process supervision is manual; app/worker lifecycles can still be disrupted by local shell/session churn.

## 11) Recommended Next Steps

1. Complete core domain migration (proposals, transactions, memory tables) to Postgres adapters.
2. Add `analysis_id` server persistence in chat rows directly for stronger replayability.
3. Introduce supervisor/service manager for app + worker with restart policy.
4. Add latency/error SLO dashboard:
   - queue wait
   - running duration
   - timeout/error rate by intent
5. Add explicit UI “linked analysis” chip click-through to `/ai/phase2/map-reduce/report/{id}`.

---

This document is intended to be the current operational architecture reference.


## 2026-02-22 Refresh
- Added dynamic short-query quick/deep routing in `app/routers/ai.py`.\n- Added SSE resilience and backend-driven partial progress messaging.\n- Proposal reject learning is backgrounded; HTMX actions return 204 in-card deletion flow.\n- Removed duplicated panel injection risk by using `hx-swap="delete"` for proposal dismiss/feedback submit.

## 2026-02-23 Refresh

### Data-First AI Contract (Global)
- Runtime now enforces: no SEC quantitative extraction without structured data context.
- Fallback is deterministic: `Data not available in structured filings.`
- Enforcement location:
  - `app/core/ai_data_policy.py`
  - `tools/llm_engine.py` (central call path)

### SEC Pipeline Split of Responsibilities
- Structured services provide numbers:
  - `app/services/mini_statements_service.py`
  - `app/services/company_intel_service.py`
- LLM role in filing flow is synthesis of qualitative content (MD&A / Risk), not numeric extraction.

### Company Intel Auto-Refresh
- Filing ingest now triggers guarded intel refresh for relevant forms:
  - `10-K`, `10-Q`, `20-F`, `40-F`
- Refresh condition: stale/missing intel or newer filing date.

### Coverage/Freshness Architecture
- Company detail now computes and surfaces:
  - `quant_ready | qual_only | missing`
  - stale flags and SLA day checks
  - schema/provenance metadata

### New Operational Endpoints
- `GET /api/company/financial-deltas?ticker=...`
- `GET /ai/runtime/metrics`

### CI Quality Expansion
- `bin/check_ai_data_policy.py` added to quality gate.
- `tools/eval_data_first_policy.py` added as policy regression test.

### Integrity Operations
- Structured financial integrity checker added:
  - `tools/integrity_financial_data.py`
- Scheduled in:
  - `automations/v2_daily.sh`
  - `automations/terminal_weekly.sh`

## 2026-02-24 Refresh

### Runtime Postgres Enforcement (Incremental)
- Removed active SQLite fallback from company market-cap lookup path.
- Removed SQLite runtime branches from events processing service.
- Active runtime path now expects Postgres for:
  - event schema/bootstrap
  - event create/read/list/update
  - company profile market-cap lookup

### AI Sync Path Reliability
- `/ai/command` timeout path was adjusted to avoid executor shutdown blocking.
- On budget timeout, response now returns queued payload immediately with `job_id`.
- Deep analysis remains async/SSE-first.
