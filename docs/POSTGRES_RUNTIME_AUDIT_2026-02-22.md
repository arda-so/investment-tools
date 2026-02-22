# Postgres Runtime Audit (2026-02-22)

## Scope
Strict audit of active runtime paths with `CORE_DB_BACKEND=postgres`.

## Confirmed
- App readiness reports Postgres backend and DB healthy.
- Worker health reports Postgres queue backend.
- Phase2 status reports Postgres + pgvector ready.
- Dashboard and company pages return HTTP 200.

## Hardening Applied
- Removed auto-downgrade to SQLite in core guard.
- Removed automatic SQLite bootstrap sync on app startup (now opt-in via `CORE_DB_BOOTSTRAP_FROM_SQLITE=1`).
- Enforced Postgres-only startup in app init (`postgres_required` guard).
- Migrated active ontology graph runtime in Postgres mode:
  - `sec_ingest_pipeline_service`: writes graph data to `entities_core` / `relationships_core`.
  - `proactive_ai_service`: reads/writes graph in Postgres mode for ontology ingest, contagion monitor, and macro-shock simulation.
- Replaced active dashboard SQLite intel read with Postgres core table read.

## Remaining SQLite References
These remain for compatibility/migration paths and are not active in Postgres runtime:
- Explicit `if core_backend() != "postgres"` branches.
- SQLite migration helpers (`sync_core_from_sqlite`, parity verification).
- Optional local cache fallback code paths (`cache.db`) in non-Postgres branches.

## Operational Notes
- Async AI jobs can still run long depending on model latency; worker health shows running jobs correctly.
- This is now a Postgres-first runtime with strict startup enforcement.

## Validation Commands
- `curl -sS http://127.0.0.1:8766/health/ready`
- `curl -sS http://127.0.0.1:8766/ai/worker/health`
- `curl -sS http://127.0.0.1:8766/ai/phase2/status`

