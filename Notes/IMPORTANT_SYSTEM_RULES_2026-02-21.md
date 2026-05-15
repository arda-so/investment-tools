# Important System Rules
As of: 2026-02-21

## Product-Facing Rules

1. Main dashboard must stay investor-focused.
- Technical/operator panels belong to observability/admin views.

2. One primary action per user workflow.
- Avoid competing primary buttons in same panel.

3. Follow-up continuity matters.
- “That report/it” follow-ups must resolve against latest analysis artifact.

## AI Behavior Rules

1. No hardcoded investment conclusions.
- The model decides content dynamically from context and evidence.

2. Deterministic infrastructure, dynamic reasoning.
- Deterministic: routing, queueing, fallback, timeouts, safety gates.
- Dynamic: analysis labels, insights, synthesis text.

3. Relevance before analysis.
- If signal is noise/unrelated, abort gracefully.

4. Strict anti-hallucination.
- Do not invent user rules/limits not present in stored memory/context.

## Reliability Rules

1. Never rely on SSE alone.
- Always keep polling fallback endpoint active.

2. Worker timeouts must fail fast.
- No hanging `running` jobs that never resolve.

3. Health endpoints must reflect real state.
- Queue depth, active workers, recent errors.

## Data Rules

1. Preserve lineage for every analysis.
- Keep stable IDs (`analysis_id` / reducer job id).

2. Hybrid migration discipline.
- Postgres for concurrency-critical paths.
- SQLite paths migrated incrementally with verification.

3. No silent schema assumptions.
- Ensure required tables on startup/use-path.

## Operational Rules

1. App and worker are separate processes.
- App handles HTTP/UI.
- Worker handles queued deep jobs.

2. Keep logs and docs current after architecture changes.
- Update architecture docs with each major queue/routing/data change.


## 2026-02-22 Refresh
- Never block UI actions on full dashboard rerender when card-level mutation is sufficient.\n- Treat SSE transport errors as recoverable states first (retry/recover), not immediate user-failure.

## 2026-02-24 Refresh
- Runtime update: active company market-cap lookup and events runtime paths are Postgres-only.
- AI command reliability: `/ai/command` timeout path now returns queued fallback quickly (no blocking hang).
- Health posture unchanged and valid in current run:
  - `/health/live` ok
  - `/health/ready` => `core_db_backend=postgres`, `queue_backend=postgres`, `db_ok=true`
- Continuation policy: keep async-first AI flow and avoid reintroducing SQLite fallback in active runtime request paths.
