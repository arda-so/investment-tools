# GCP Cutover Runbook (Copy/Paste Ready)

Use this file as your execution script. Fill the `TODO` values once, then run in order.

## Fast Path (Recommended)

```bash
# 1) Bootstrap runtime IAM service account (one-time)
PROJECT_ID="TODO_YOUR_GCP_PROJECT_ID" REGION="europe-west1" ./bin/cloud_bootstrap_iam

# 2) Deploy app + worker in one command
PROJECT_ID="TODO_YOUR_GCP_PROJECT_ID" REGION="europe-west1" DB_INSTANCE="investor-os-pg" \
SERVICE_ACCOUNT="investor-tools-runtime@TODO_YOUR_GCP_PROJECT_ID.iam.gserviceaccount.com" \
INCLUDE_GEMINI_SECRET=1 ./bin/deploy_cloud_run.sh
```

Rollback:
```bash
ROLLBACK_TAG="<previous_image_tag>" PROJECT_ID="TODO_YOUR_GCP_PROJECT_ID" REGION="europe-west1" \
SERVICE_ACCOUNT="investor-tools-runtime@TODO_YOUR_GCP_PROJECT_ID.iam.gserviceaccount.com" \
./bin/rollback_cloud_run.sh
```

## Cloud File Parity (Reports/Filings/Data)

```bash
export PROJECT_ID="TODO_YOUR_GCP_PROJECT_ID"
export REGION="europe-west1"
export FILE_BUCKET="${PROJECT_ID}-runtime-files"

gcloud storage buckets create "gs://${FILE_BUCKET}" --project="${PROJECT_ID}" --location="${REGION}" --uniform-bucket-level-access
python3 tools/sync_cloud_files.py --bucket "${FILE_BUCKET}" --root .

# Deploy with cloud file envs
PROJECT_ID="${PROJECT_ID}" REGION="${REGION}" DB_INSTANCE="investor-os-pg" \
SERVICE_ACCOUNT="investor-tools-runtime@${PROJECT_ID}.iam.gserviceaccount.com" \
CLOUD_FILES_BUCKET="${FILE_BUCKET}" CLOUD_FILES_PREFIX="" INCLUDE_GEMINI_SECRET=0 \
./bin/deploy_cloud_run.sh
```

## 1) Set Variables (edit once)

```bash
export PROJECT_ID="TODO_YOUR_GCP_PROJECT_ID"
export REGION="us-central1"
export REPO="investor-tools"
export DB_INSTANCE="investor-os-pg"
export DB_NAME="investor_os"
export DB_USER="investor"

# Cloud Run service names
export APP_SERVICE="investor-os-app"
export WORKER_SERVICE="investor-os-worker"
```

## 2) Enable APIs

```bash
gcloud config set project "$PROJECT_ID"
gcloud services enable \
  run.googleapis.com \
  sqladmin.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com
```

## 3) Create Artifact Registry

```bash
gcloud artifacts repositories create "$REPO" \
  --repository-format=docker \
  --location="$REGION" \
  --project="$PROJECT_ID"
```

## 4) Create Cloud SQL (Postgres)

```bash
gcloud sql instances create "$DB_INSTANCE" \
  --database-version=POSTGRES_16 \
  --cpu=2 \
  --memory=8GiB \
  --region="$REGION" \
  --project="$PROJECT_ID"

gcloud sql databases create "$DB_NAME" \
  --instance="$DB_INSTANCE" \
  --project="$PROJECT_ID"

gcloud sql users create "$DB_USER" \
  --instance="$DB_INSTANCE" \
  --password="TODO_STRONG_DB_PASSWORD" \
  --project="$PROJECT_ID"
```

## 5) Create Secrets

```bash
# Build DSN first with your Cloud SQL private/public connection plan.
printf '%s' "postgresql://$DB_USER:TODO_STRONG_DB_PASSWORD@TODO_DB_HOST:5432/$DB_NAME" | \
  gcloud secrets create POSTGRES_DSN --data-file=- --project="$PROJECT_ID"

printf '%s' "TODO_GEMINI_API_KEY" | \
  gcloud secrets create GEMINI_API_KEY --data-file=- --project="$PROJECT_ID"

# Optional
printf '%s' "TODO_OPENAI_API_KEY" | \
  gcloud secrets create OPENAI_API_KEY --data-file=- --project="$PROJECT_ID"
```

## 6) Build and Push Image

```bash
cd ~/Investment_Tools
gcloud auth configure-docker "$REGION-docker.pkg.dev"

docker build -t "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/app:latest" .
docker push "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/app:latest"
```

## 7) Deploy App (Cloud Run)

```bash
gcloud run deploy "$APP_SERVICE" \
  --image "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/app:latest" \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --set-env-vars APP_ENV=production,CORE_DB_BACKEND=postgres,CORE_DB_GUARD_ENFORCE=1,CORE_DB_STRICT_POSTGRES=1,PHASE2_POSTGRES_ENABLED=1,AI_QUEUE_BACKEND=postgres,AI_QUEUE_STRICT_PROD=1,AI_ADMIN_API_ENABLED=0 \
  --set-secrets POSTGRES_DSN=POSTGRES_DSN:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest
```

## 8) Deploy Worker (Cloud Run Service)

```bash
gcloud run deploy "$WORKER_SERVICE" \
  --image "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/app:latest" \
  --region "$REGION" \
  --platform managed \
  --no-allow-unauthenticated \
  --command ./bin/run_ai_worker \
  --set-env-vars APP_ENV=production,CORE_DB_BACKEND=postgres,CORE_DB_GUARD_ENFORCE=1,CORE_DB_STRICT_POSTGRES=1,PHASE2_POSTGRES_ENABLED=1,AI_QUEUE_BACKEND=postgres,AI_QUEUE_STRICT_PROD=1 \
  --set-secrets POSTGRES_DSN=POSTGRES_DSN:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest
```

## 9) Get App URL and Verify Health

```bash
export APP_URL="$(gcloud run services describe "$APP_SERVICE" --region "$REGION" --format='value(status.url)')"
echo "$APP_URL"

curl -sS "$APP_URL/health/live"
curl -sS "$APP_URL/health/ready"
curl -sS "$APP_URL/ai/worker/health"
curl -sS "$APP_URL/ai/runtime/metrics"
curl -sS "$APP_URL/api/company/financial-deltas?ticker=NVDA"
```

## 10) Bootstrap + Verify Core Data

```bash
curl -sS -X POST "$APP_URL/ai/migration/bootstrap"
curl -sS -X POST "$APP_URL/ai/migration/sync-core"
curl -sS "$APP_URL/ai/migration/verify-core"
```

Expected: all `match: true`.

## 11) Async AI Smoke Test

```bash
curl -sS -X POST "$APP_URL/ai/command/async" \
  -H 'Content-Type: application/json' \
  -d '{"query":"summarize my portfolio in one paragraph","context":{"session_id":"cloud-go-live-smoke"}}'
```

## 12) Scheduler Mapping (post-cutover)

Migrate these to Cloud Scheduler + Cloud Run Jobs:
- `automations/v2_daily.sh`
- `automations/terminal_weekly.sh`
- `automations/v2_financial_integrity.sh`

## 13) Go/No-Go Checklist

Go live only if:
- `./bin/quality_gate.sh` passes on release commit.
- `/health/live`, `/health/ready`, `/ai/worker/health` are healthy.
- `/ai/migration/verify-core` shows all matches.
- Async command path works.
- Integrity report does not show abnormal stale/missing spikes.

## 14) First 48h Monitoring

Monitor:
- error rate
- p95 latency
- queue depth
- worker heartbeat
- DB connectivity
- integrity report (`tools/integrity_financial_data.py`) at least 2x/day
