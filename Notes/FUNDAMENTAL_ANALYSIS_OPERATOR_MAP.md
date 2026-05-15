# Fundamental Analysis Operator Map

Date: 2026-02-24  
Scope: Dashboard Fundamental Analysis pipeline, AI architecture paths, automations, runtime operations.

## 1) What "Fundamental Analysis" on Dashboard Is
- UI component: `app/templates/components/dashboard_body.html`
- Data source: `action_proposals` rendered server-side.
- API/router integration: `app/routers/dashboard.py`
- Backing service: `app/services/proactive_ai_service.py`
- Storage source of truth: Postgres `action_proposals_core`

This is a structured proposal system (not free-form chat output).

## 2) Core Data Model (Postgres)
Table: `action_proposals_core`  
Defined in: `app/services/postgres_core_service.py`

Key fields:
- `status`, `kind`, `ticker`, `title`
- `thesis_json` (bullets)
- `insights_json` (insight cards)
- `reasoning_json` (structured rationale)
- `confidence`, `priority_score`
- `execute_route`, `execute_payload_json`
- `source_event_key`
- `dismissed_reason`, `rejection_reason`, `rejected_at`, `executed_at`

## 3) Proposal Generation Pipeline
Entry point:
- `run_event_driven_monitor(force=...)` in `app/services/proactive_ai_service.py`

Signal inputs:
- `report_facts_core` (high-importance facts)
- `filings_core` (10-Q, 10-K, 8-K, 20-F, 40-F, 6-K)
- ontology graph relationships (`entities_core`, `relationships_core`) for peer contagion

Proposal kinds:
- `thesis_trigger`
- `filing_update`
- `contagion_peer_alert`

Quality/normalization pipeline:
- `app/core/proposal_pipeline.py`
  - normalize reasoning
  - normalize citations
  - insight extraction
  - quality gate
  - score normalization

## 4) AI Reasoning Contract for Fundamental Signals
Function:
- `_evaluate_signal_reasoning(...)` in `app/services/proactive_ai_service.py`

Behavior:
- relevance gate first (drop noise/non-events)
- compare new signal vs thesis + investor style memory
- produce strict JSON reasoning fields and insight cards
- no markdown/no unstructured spillover in write path

## 5) Dashboard Operations (Human Actions)
Endpoints in `app/routers/dashboard.py`:

1. Refresh insights
- `POST /dashboard/proposals/scan`

2. Execute/open analysis
- `POST /dashboard/proposals/{proposal_id}/execute`

3. Dismiss insight
- `POST /dashboard/proposals/{proposal_id}/dismiss`

4. Reject insight (with learning)
- `POST /dashboard/proposals/{proposal_id}/reject`
- API variant: `POST /api/proposals/{proposal_id}/reject`

## 6) Execute/Reject Lifecycle
Execute:
- `execute_action_proposal(...)` in `app/services/proactive_ai_service.py`
- effects:
  - marks row executed
  - seeds workspace note/task/reminder
  - redirects to execution route

Reject:
- `reject_action_proposal(...)`
- `learn_from_rejection(...)`
- effects:
  - marks proposal rejected
  - writes preference/memory delta to reduce repeated low-quality suggestions

## 7) AI Command Architecture (Chat + Queue)
Router: `app/routers/ai.py`

1. Sync command
- `POST /ai/command`
- flow:
  - deterministic fast route
  - cached route
  - bounded sync budget
  - timeout/exception => queue fallback with `job_id`

2. Async command
- `POST /ai/command/async`
- queue-first (still supports deterministic instant replies)

3. Streaming
- `GET /ai/command/sse/{job_id}`
- emits: status, partial, result, error events

## 8) Automations Behind Fundamental Analysis
Primary scripts:

1. Morning
- `automations/v2_morning.sh`
- runs:
  - macro watchdog
  - earnings/data refresh
  - morning brief
  - proactive monitor forced run

2. Night SEC
- `automations/v2_sec_nightly.sh`
- runs:
  - SEC sync for watched names
  - proactive monitor forced run

3. Filing-triggered monitor
- `process_new_filings_pipeline(...)` in `app/services/sec_ingest_pipeline_service.py`
- after filing processing, it invokes monitor (`run_event_driven_monitor(force=True)`)

## 9) Manual Operator Commands
From repo root (`~/Investment_Tools`):

```bash
# Refresh dashboard fundamental insights
curl -sS -X POST http://127.0.0.1:8766/dashboard/proposals/scan

# Run proactive monitor directly
python3 tools/run_proactive_monitor.py --force

# SEC sync + ingest + monitor path
python3 tools/sec_sync_my_companies.py --days 7 --full-empty-limit 6

# AI async command example
curl -sS -X POST http://127.0.0.1:8766/ai/command/async \
  -H 'Content-Type: application/json' \
  -d '{"query":"summarize my portfolio in one paragraph","context":{"session_id":"ops"}}'
```

## 10) Runtime Health Checks
```bash
curl -sS http://127.0.0.1:8766/health/live
curl -sS http://127.0.0.1:8766/health/ready
curl -sS http://127.0.0.1:8766/ai/worker/health
curl -sS http://127.0.0.1:8766/ai/phase2/status
```

Expected readiness:
- `core_db_backend: "postgres"`
- `queue_backend: "postgres"` (or configured backend)
- `db_ok: true`

## 11) Current Architecture Guarantees (as of 2026-02-24)
- Active fundamental proposal runtime is Postgres-backed.
- Proposal quality passes unified pipeline normalization and gating.
- Sync AI command path has non-blocking timeout fallback to async queue.
- Event-driven monitor supports repeated forced scans without proposal storms due to cooldown/open checks.

## 12) Fast Troubleshooting
1. No insights shown:
- run `/dashboard/proposals/scan`
- verify monitor output (`created`, `reason`)

2. Chat appears slow:
- use `/ai/command/async` + SSE
- check `/ai/worker/health`

3. Health ready fails:
- check Postgres DSN and backend mode
- verify `/health/ready` `db_error`

4. Repeated noisy proposals:
- use reject path with reason to feed learning loop

