# Cloud Readiness Checklist

## Baseline
- `CORE_DB_BACKEND=postgres`
- `CORE_DB_STRICT_POSTGRES=1`
- `AI_QUEUE_BACKEND=postgres`
- `AI_QUEUE_STRICT_PROD=1`
- `POSTGRES_DSN` set via env/secret manager
- `ALLOW_LOCAL_ENV_FILE=0` in cloud
- `AI_ADMIN_API_ENABLED=0` by default in non-dev
- Optional admin key gate: `AI_ADMIN_API_KEY` + `x-admin-key` header

## Health Probes
- Liveness: `GET /health/live`
- Readiness: `GET /health/ready` (checks Postgres connectivity and active queue backend)
- Worker health: `GET /ai/worker/health`

## Container Runtime
- `app` has restart policy and readiness healthcheck.
- `worker` has restart policy.
- `postgres` and `redis` have service healthchecks.

## Pre-Deploy Validation
1. `curl -sS http://127.0.0.1:8766/health/live`
2. `curl -sS http://127.0.0.1:8766/health/ready`
3. `curl -sS http://127.0.0.1:8766/ai/worker/health`
4. Queue test:
   - `POST /ai/command/async`
   - `GET /ai/command/sse/{job_id}`
5. Admin lock test (in non-dev):
   - `POST /ai/migration/bootstrap` without key -> `403`
   - with `x-admin-key` when configured -> allowed
6. Cloud preflight:
   - `./bin/cloud_preflight`
   - must pass env/dependency/runtime checks before deployment.

## Cloud Migration Path (next)
1. Move secrets to Secret Manager.
2. Point `POSTGRES_DSN` to managed Postgres.
3. Point `AI_REDIS_URL` to managed Redis.
4. Deploy app + worker as separate services/jobs.
5. Add centralized logs/alerts on:
   - readiness failures
   - queue depth growth
   - worker heartbeat gaps

## New Cloud Artifacts (2026-02-22)
- `.env.cloud.example` (production-safe env template)
- `docker-compose.cloud.yml` (app+worker cloud profile without local DB/cache containers)
- `docs/CLOUD_GO_LIVE_CHECKLIST.md` (strict go-live pass/fail checklist)
- `bin/cloud_preflight` (one-command pre-deploy validation)


## 2026-02-22 Refresh
- Added strict preflight command: `./bin/cloud_preflight`.\n- Current known failing condition (if present): missing `AI_REDIS_URL` in `.env`.

## 2026-02-23 Refresh
- Added cloud execution runbooks:
  - `docs/GCP_CUTOVER_RUNBOOK.md`
  - `docs/GCP_CUTOVER_RUNBOOK_FILLED.md`
- Added root index:
  - `START_HERE.md`
- Added data-quality operational readiness checks:
  - `tools/integrity_financial_data.py`
  - `automations/v2_financial_integrity.sh`
  - `bin/run_financial_integrity`
- New cloud-observable endpoints:
  - `GET /ai/runtime/metrics`
  - `GET /api/company/financial-deltas?ticker=...`

## 2026-02-24 Refresh
- Runtime update: active company market-cap lookup and events runtime paths are Postgres-only.
- AI command reliability: `/ai/command` timeout path now returns queued fallback quickly (no blocking hang).
- Health posture unchanged and valid in current run:
  - `/health/live` ok
  - `/health/ready` => `core_db_backend=postgres`, `queue_backend=postgres`, `db_ok=true`
- Continuation policy: keep async-first AI flow and avoid reintroducing SQLite fallback in active runtime request paths.
