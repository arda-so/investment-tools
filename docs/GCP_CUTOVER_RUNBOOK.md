# GCP Cutover Runbook

This runbook is the execution plan to move Investor OS from local runtime to GCP with minimal outage and rollback safety.

## 0) Scope and Assumptions

- App and worker are already Postgres-first in runtime.
- `./bin/quality_gate.sh` must pass before any deploy.
- Cutover target uses:
  - Cloud Run (app)
  - Cloud Run Job or Cloud Run service (worker)
  - Cloud SQL Postgres
  - Secret Manager
  - (Optional) Memorystore Redis

## 1) Freeze and Snapshot (Day 0)

1. Create release tag and freeze schema changes:
```bash
cd ~/Investment_Tools
git status
git tag cloud-cutover-prep-$(date +%Y%m%d-%H%M)
```

2. Run local quality gate:
```bash
./bin/quality_gate.sh
```

3. Run local preflight:
```bash
cp .env.cloud.example .env
# fill real values
./bin/cloud_preflight
```

## 2) Provision GCP Resources

Set environment:
```bash
export PROJECT_ID="your-gcp-project"
export REGION="us-central1"
export REPO="investor-tools"
export DB_INSTANCE="investor-os-pg"
export DB_NAME="investor_os"
export DB_USER="investor"
```

Enable APIs:
```bash
gcloud services enable run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
  artifactregistry.googleapis.com cloudbuild.googleapis.com
```

Create Artifact Registry:
```bash
gcloud artifacts repositories create "$REPO" \
  --repository-format=docker --location="$REGION" --project="$PROJECT_ID"
```

Create Cloud SQL Postgres (example sizing; tune later):
```bash
gcloud sql instances create "$DB_INSTANCE" \
  --database-version=POSTGRES_16 \
  --cpu=2 --memory=8GiB --region="$REGION" --project="$PROJECT_ID"
```

Create DB and user:
```bash
gcloud sql databases create "$DB_NAME" --instance="$DB_INSTANCE" --project="$PROJECT_ID"
gcloud sql users create "$DB_USER" --instance="$DB_INSTANCE" --password="CHANGE_ME" --project="$PROJECT_ID"
```

## 3) Secrets and Runtime Config

Store secrets:
```bash
printf '%s' "postgresql://USER:PASSWORD@HOST:5432/$DB_NAME" | \
  gcloud secrets create POSTGRES_DSN --data-file=- --project="$PROJECT_ID"
printf '%s' "your_gemini_api_key" | \
  gcloud secrets create GEMINI_API_KEY --data-file=- --project="$PROJECT_ID"
```

Minimum required runtime env:
- `CORE_DB_BACKEND=postgres`
- `CORE_DB_GUARD_ENFORCE=1`
- `CORE_DB_STRICT_POSTGRES=1`
- `PHASE2_POSTGRES_ENABLED=1`
- `AI_QUEUE_BACKEND=postgres`
- `AI_QUEUE_STRICT_PROD=1`
- `APP_ENV=production`

## 4) Build and Push Images

```bash
gcloud auth configure-docker "$REGION-docker.pkg.dev"

docker build -t "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/app:latest" .
docker push "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/app:latest"
```

If app/worker use same image with different startup command, push once and deploy with different command/args.

## 5) Deploy App (Cloud Run)

```bash
gcloud run deploy investor-os-app \
  --image "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/app:latest" \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --set-env-vars CORE_DB_BACKEND=postgres,CORE_DB_GUARD_ENFORCE=1,CORE_DB_STRICT_POSTGRES=1,PHASE2_POSTGRES_ENABLED=1,AI_QUEUE_BACKEND=postgres,AI_QUEUE_STRICT_PROD=1,APP_ENV=production \
  --set-secrets POSTGRES_DSN=POSTGRES_DSN:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest
```

## 6) Deploy Worker

Option A: Cloud Run service with always-on min instance.
Option B: Cloud Run Job triggered on schedule.

Example (service style):
```bash
gcloud run deploy investor-os-worker \
  --image "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/app:latest" \
  --region "$REGION" \
  --platform managed \
  --no-allow-unauthenticated \
  --command ./bin/run_ai_worker \
  --set-env-vars CORE_DB_BACKEND=postgres,CORE_DB_GUARD_ENFORCE=1,CORE_DB_STRICT_POSTGRES=1,PHASE2_POSTGRES_ENABLED=1,AI_QUEUE_BACKEND=postgres,AI_QUEUE_STRICT_PROD=1,APP_ENV=production \
  --set-secrets POSTGRES_DSN=POSTGRES_DSN:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest
```

## 7) Bootstrap and Verify Data in Cloud

Call admin migration endpoints from trusted environment:
```bash
curl -sS -X POST "https://<app-url>/ai/migration/bootstrap"
curl -sS -X POST "https://<app-url>/ai/migration/sync-core"
curl -sS "https://<app-url>/ai/migration/verify-core"
```

Expected: `match: true` for all core tables.

## 8) Health + Functional Checks

```bash
curl -sS "https://<app-url>/health/live"
curl -sS "https://<app-url>/health/ready"
curl -sS "https://<app-url>/ai/worker/health"
curl -sS "https://<app-url>/ai/runtime/metrics"
curl -sS "https://<app-url>/api/company/financial-deltas?ticker=NVDA"
```

Smoke async queue:
```bash
curl -sS -X POST "https://<app-url>/ai/command/async" \
  -H 'Content-Type: application/json' \
  -d '{"query":"summarize my portfolio in one paragraph","context":{"session_id":"cloud-smoke"}}'
```

## 9) Scheduler Migration

Map local automations to Cloud Scheduler:
- `automations/v2_daily.sh`
- `automations/terminal_weekly.sh`
- `automations/v2_financial_integrity.sh`

Recommended:
- Cloud Scheduler -> Cloud Run Job HTTP trigger
- Keep logs in Cloud Logging

## 10) Cutover and Rollback

Cutover:
1. Route traffic to Cloud Run URL/domain.
2. Keep local stack read-only/standby for 24-72h.

Rollback:
1. Switch DNS/traffic back.
2. Stop cloud writes if data divergence detected.
3. Reconcile using verify endpoints and database diffs.

## 11) Go/No-Go Criteria

Go only if all are true:
- Quality gate PASS.
- Live/ready/worker endpoints healthy.
- Core migration verify reports `match=true`.
- Async command + SSE path succeeds.
- Data-first policy checks pass in CI.
- Integrity job runs without stale/missing explosion.

## 12) Post-Go-Live (First 48h)

- Monitor:
  - p95 latency
  - error rate
  - queue depth
  - worker heartbeat
  - DB connection failures
- Run integrity check at least twice daily:
```bash
python3 tools/integrity_financial_data.py
```

