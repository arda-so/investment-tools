# Cloud Go-Live Checklist

This is the strict pass/fail checklist for production cloud deployment.

Detailed execution runbook:
- `docs/GCP_CUTOVER_RUNBOOK.md`
- Copy/paste execution version:
- `docs/GCP_CUTOVER_RUNBOOK_FILLED.md`

## Current Status

- `Postgres runtime cutover`: done
- `Async app/worker split`: done
- `SSE for async results`: done
- `Cloud env template`: done (`.env.cloud.example`)
- `Cloud compose profile`: done (`docker-compose.cloud.yml`)
- `Preflight checker`: done (`bin/cloud_preflight`)

## Must Pass Before Go-Live

1. Runtime mode
- Set:
  - `CORE_DB_BACKEND=postgres`
  - `CORE_DB_GUARD_ENFORCE=1`
  - `CORE_DB_STRICT_POSTGRES=1`
  - `APP_ENV=production`
- Result: app refuses startup if Postgres is not available.

2. Queue backend
- Set:
  - `AI_QUEUE_BACKEND=postgres` (or managed redis flow if explicitly chosen)
  - `AI_QUEUE_STRICT_PROD=1`
- Result: queue cannot silently fall back to sqlite in production.

3. Secrets/config
- Do not hardcode credentials.
- Use cloud secret manager / env injection for:
  - `POSTGRES_DSN`
  - `AI_REDIS_URL`
  - `GEMINI_API_KEY`
  - `AI_ADMIN_API_KEY` (if admin API enabled)

4. Health probes
- Liveness: `GET /health/live`
- Readiness: `GET /health/ready`
- Worker: `GET /ai/worker/health`
- Must be wired into orchestrator health checks.

5. Process supervision
- App restart policy required.
- Worker restart policy required.
- No manual shell-only lifecycle in production.

6. Observability
- App logs centralized.
- Worker logs centralized.
- Alert on:
  - repeated queue errors
  - worker offline
  - readiness failures
  - sustained high latency

7. Backup + recovery
- Postgres automated backups enabled.
- Restore test performed and documented.
- RPO/RTO defined.

8. Security
- Admin endpoints disabled by default (`AI_ADMIN_API_ENABLED=0`).
- HTTPS/TLS termination enforced at ingress.
- Network policy restricts DB/cache to service network.

## Preflight Commands

Run before deploying:

```bash
cd ~/Investment_Tools
cp .env.cloud.example .env
# edit .env with real values
./bin/cloud_preflight
PROJECT_ID="your-project-id" REGION="europe-west1" REPO="investor-tools" DB_INSTANCE="investor-os-pg" ./bin/cloud_preflight --gcp
```

## One-Command Deploy (Recommended)

```bash
# One-time IAM bootstrap
PROJECT_ID="your-project-id" REGION="europe-west1" ./bin/cloud_bootstrap_iam

# Deploy app + worker
PROJECT_ID="your-project-id" REGION="europe-west1" DB_INSTANCE="investor-os-pg" \
SERVICE_ACCOUNT="investor-tools-runtime@your-project-id.iam.gserviceaccount.com" \
INCLUDE_GEMINI_SECRET=1 ./bin/deploy_cloud_run.sh
```

## Cloud Runbook (Minimal)

1. Build image:
```bash
docker build -t investor-tools:latest .
```

2. Start app+worker with cloud profile:
```bash
docker compose -f docker-compose.cloud.yml up -d --build
```

3. Verify:
```bash
curl -sS http://127.0.0.1:8766/health/live
curl -sS http://127.0.0.1:8766/health/ready
curl -sS http://127.0.0.1:8766/ai/worker/health
```

4. Verify async path:
```bash
curl -sS -X POST http://127.0.0.1:8766/ai/command/async \
  -H 'Content-Type: application/json' \
  -d '{"query":"summarize my portfolio in one paragraph","context":{"session_id":"go-live-check"}}'
```

## Known Gap (still external to repo)

- Managed cloud infra resources (hosted Postgres, hosted Redis, secret manager, ingress TLS, dashboards/alerts) must be provisioned in your cloud account.
- This repository now contains the application-side readiness artifacts, but cloud account provisioning is still required.
