# Cloud-Ready Local Parity (App + Worker + Postgres + Redis)

This project now supports a containerized runtime that mirrors the planned cloud shape.

## What was added

- `Dockerfile`
- `docker-compose.yml`
- `.dockerignore` (to keep build context lean)
- Startup scripts now use dynamic project root and env-based host/port:
  - `bin/run_v2_app`
  - `bin/run_ai_worker`

## Start the full stack

```bash
cd ~/Investment_Tools
docker compose up --build -d
```

## Verify

```bash
curl -sS http://127.0.0.1:8766/ai/worker/health
curl -sS http://127.0.0.1:8766/ai/phase2/status
```

## Stop

```bash
docker compose down
```

## Notes

- App runs on `http://127.0.0.1:8766`.
- Container runtime uses env vars only (no hardcoded absolute host paths).
- Queue and core DB are configured for Postgres-backed operation by default.


## 2026-02-22 Refresh
- Added cloud profile compose file: `docker-compose.cloud.yml` for app+worker with env-based managed service wiring.\n- Added `.env.cloud.example` as production-safe baseline template.

## 2026-02-24 Refresh
- Runtime update: active company market-cap lookup and events runtime paths are Postgres-only.
- AI command reliability: `/ai/command` timeout path now returns queued fallback quickly (no blocking hang).
- Health posture unchanged and valid in current run:
  - `/health/live` ok
  - `/health/ready` => `core_db_backend=postgres`, `queue_backend=postgres`, `db_ok=true`
- Continuation policy: keep async-first AI flow and avoid reintroducing SQLite fallback in active runtime request paths.
