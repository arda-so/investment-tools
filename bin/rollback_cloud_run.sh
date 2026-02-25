#!/usr/bin/env bash
set -euo pipefail

ROOT="${INVESTOR_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing command: $1" >&2; exit 1; }
}

need_cmd gcloud

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="${REGION:-europe-west1}"
REPO="${REPO:-investor-tools}"
APP_SERVICE="${APP_SERVICE:-investor-tools-app}"
WORKER_SERVICE="${WORKER_SERVICE:-investor-tools-worker}"
DB_INSTANCE="${DB_INSTANCE:-investor-os-pg}"
DEPLOY_WORKER="${DEPLOY_WORKER:-1}"
INCLUDE_GEMINI_SECRET="${INCLUDE_GEMINI_SECRET:-0}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-}"
ROLLBACK_TAG="${ROLLBACK_TAG:-}"

if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
  echo "PROJECT_ID is required." >&2
  exit 1
fi
if [ -z "$ROLLBACK_TAG" ]; then
  echo "ROLLBACK_TAG is required. Example: ROLLBACK_TAG=stable ./bin/rollback_cloud_run.sh" >&2
  exit 1
fi

if [ -n "$SERVICE_ACCOUNT" ]; then
  SA_FLAG=(--service-account="$SERVICE_ACCOUNT")
else
  SA_FLAG=()
fi

IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/app:${ROLLBACK_TAG}"
SECRETS="POSTGRES_DSN=POSTGRES_DSN:latest"
if [ "$INCLUDE_GEMINI_SECRET" = "1" ]; then
  SECRETS="${SECRETS},GEMINI_API_KEY=GEMINI_API_KEY:latest"
fi

echo "Rolling back app to image: $IMAGE"
gcloud run deploy "$APP_SERVICE" \
  --image="$IMAGE" \
  --region="$REGION" \
  --platform=managed \
  --no-allow-unauthenticated \
  --add-cloudsql-instances="${PROJECT_ID}:${REGION}:${DB_INSTANCE}" \
  --set-env-vars="APP_ENV=cloud,APP_PORT=8080,APP_HOST=0.0.0.0,CORE_DB_BACKEND=postgres,CORE_DB_GUARD_ENFORCE=1,CORE_DB_STRICT_POSTGRES=1,PHASE2_POSTGRES_ENABLED=1,AI_QUEUE_BACKEND=postgres,AI_QUEUE_STRICT_PROD=1" \
  --set-secrets="$SECRETS" \
  "${SA_FLAG[@]}"

if [ "$DEPLOY_WORKER" = "1" ]; then
  echo "Rolling back worker to image: $IMAGE"
  gcloud run deploy "$WORKER_SERVICE" \
    --image="$IMAGE" \
    --region="$REGION" \
    --platform=managed \
    --no-allow-unauthenticated \
    --command="/app/bin/run_ai_worker" \
    --add-cloudsql-instances="${PROJECT_ID}:${REGION}:${DB_INSTANCE}" \
    --set-env-vars="APP_ENV=cloud,CORE_DB_BACKEND=postgres,CORE_DB_GUARD_ENFORCE=1,CORE_DB_STRICT_POSTGRES=1,PHASE2_POSTGRES_ENABLED=1,AI_QUEUE_BACKEND=postgres,AI_QUEUE_STRICT_PROD=1" \
    --set-secrets="$SECRETS" \
    "${SA_FLAG[@]}"
fi

APP_URL="$(gcloud run services describe "$APP_SERVICE" --region "$REGION" --project "$PROJECT_ID" --format='value(status.url)')"
echo "Rollback complete: ${APP_URL}"
