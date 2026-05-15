# Investor OS Architecture (Executive Summary)
As of: 2026-02-21

## What the system is
Investor OS is a local-first investment operating system with one main app and one AI worker:

- App serves the dashboard, portfolio, reports, and company research pages.
- AI worker handles heavier analysis jobs in the background.

## How it works in plain language
When you ask AI something:

1. Fast questions are answered immediately (holdings, tasks, notes, basic lookups).
2. Heavy analysis is queued and processed in the background.
3. Results stream back to the UI; if streaming drops, the app fetches the result directly.

This is why the UI can stay responsive while deeper analysis runs.

## Why this architecture is better now

- Faster UX: not every question waits for deep AI reasoning.
- More reliable: queue + worker model prevents blocking the main app.
- More transparent: analysis now carries an `analysis_id` so follow-ups can stay tied to the exact report run.
- Better scale path: Postgres-backed queue and phase2 map/reduce foundation are in place.

## Current storage strategy

- Postgres: AI queue and phase2 analysis artifacts.
- SQLite: most product/domain records still live here (portfolio, notes, tasks, many app tables).

Current state is hybrid, by design, during migration.

## What is completed

- Async AI queue with worker health endpoint.
- SSE result delivery with fallback polling endpoint.
- Phase2 sector map/reduce pipeline and report persistence.
- Analysis-threading improvements for “what did you learn from that report?” style follow-ups.
- Main dashboard cleaned of technical operator panels.

## What is still in progress

1. Complete migration of core domain tables from SQLite to Postgres.
2. Improve long-job reliability under heavy load.
3. Strengthen automatic follow-up binding to latest analysis artifact in all cases.
4. Add stronger process supervision (auto-restart policy) for app/worker.

## Business impact

- Better responsiveness and lower friction for daily use.
- Fewer “stuck” experiences from long AI runs.
- Higher trust: clearer provenance for AI follow-up answers.
- Clear path to scale to heavier parallel analysis over time.

## Canonical technical reference
For engineering-level details, see:

- `RECENT_ARCHITECTURE_2026-02-21.md`


## 2026-02-22 Refresh
- Fast/deep routing is now active in AI command path.\n- UI transport layer uses stronger SSE reconnect + recovery polling.\n- Proposal feedback/dismiss moved to non-blocking card-level operations.\n- Runtime remains Postgres strict mode.

## 2026-02-24 Refresh
- Runtime update: active company market-cap lookup and events runtime paths are Postgres-only.
- AI command reliability: `/ai/command` timeout path now returns queued fallback quickly (no blocking hang).
- Health posture unchanged and valid in current run:
  - `/health/live` ok
  - `/health/ready` => `core_db_backend=postgres`, `queue_backend=postgres`, `db_ok=true`
- Continuation policy: keep async-first AI flow and avoid reintroducing SQLite fallback in active runtime request paths.
