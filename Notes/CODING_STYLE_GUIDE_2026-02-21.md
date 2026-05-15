# Coding Style Guide (Investor OS)
As of: 2026-02-21

## 1) Core Principles

- Keep logic explicit, testable, and reversible.
- Prefer reliability over cleverness.
- Keep AI reasoning dynamic; keep system behavior deterministic.
- No hidden side-effects in router handlers.

## 2) Python / Backend

- Use type hints on service and router functions.
- Keep router thin; push business logic into `app/services/*`.
- Return structured dict contracts for AI paths (`status`, `intent`, `message`, `traces`).
- Use small, composable helpers over long monolithic functions.
- Catch exceptions at integration boundaries (network, DB, model call), not everywhere.

## 3) Data / Persistence

- Queue + concurrency paths: Postgres.
- Legacy/domain paths: SQLite (until migrated).
- Never hard delete critical user memory without explicit intent.
- Include timestamps (`created_at`, `updated_at`) and stable IDs for artifacts.

## 4) Frontend Templates + JS

- Keep one primary CTA per workflow.
- Progressive disclosure for advanced controls.
- Avoid mixing technical diagnostics into investor-facing panels.
- For async UI flows:
  - SSE first
  - deterministic polling fallback
  - clear user status labels

## 5) AI System Style

- No hardcoded investment conclusions.
- No forced static thesis/risk categories in final answer content.
- Use dynamic context injection (`portfolio`, `thesis`, `style memory`, latest analysis artifact).
- Keep safety/routing constraints deterministic (timeouts, gating, queue rules).

## 6) Observability Requirements

- Every AI answer should include traceability (`traces`, and when available `analysis_id`).
- Track worker health, queue depth, and recent error counts.
- Surface operational panels in observability pages, not main investment dashboard.

## 7) Testing Rules

- Add tests for:
  - routing/intents
  - async queue state transitions
  - fallback behavior
  - regression cases from real user transcripts
- Any bug fixed from production transcript should add a regression test.


## 2026-02-22 Refresh
- For UI mutation endpoints used by HTMX, return minimal payload (`204` where appropriate) and avoid full-page template render in hot paths.\n- Add regression tests for async transport recoverability and in-card HTMX swap correctness.

## 2026-02-24 Refresh
- Runtime update: active company market-cap lookup and events runtime paths are Postgres-only.
- AI command reliability: `/ai/command` timeout path now returns queued fallback quickly (no blocking hang).
- Health posture unchanged and valid in current run:
  - `/health/live` ok
  - `/health/ready` => `core_db_backend=postgres`, `queue_backend=postgres`, `db_ok=true`
- Continuation policy: keep async-first AI flow and avoid reintroducing SQLite fallback in active runtime request paths.
