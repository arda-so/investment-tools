#!/usr/bin/env bash
set -euo pipefail

ROOT="${INVESTOR_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing command: $1" >&2; exit 1; }
}

need_cmd gcloud
need_cmd curl

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="${REGION:-europe-west1}"
REPO="${REPO:-investor-tools}"
APP_SERVICE="${APP_SERVICE:-investor-tools-app}"
WORKER_SERVICE="${WORKER_SERVICE:-investor-tools-worker}"
DB_INSTANCE="${DB_INSTANCE:-investor-os-pg}"
DEPLOY_WORKER="${DEPLOY_WORKER:-1}"
ALLOW_PUBLIC="${ALLOW_PUBLIC:-0}"
INCLUDE_GEMINI_SECRET="${INCLUDE_GEMINI_SECRET:-0}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-}"

if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
  echo "PROJECT_ID is required. Set PROJECT_ID or run: gcloud config set project <id>" >&2
  exit 1
fi

if ! gcloud artifacts repositories describe "$REPO" --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "Artifact Registry repo not found: ${REPO} in ${REGION}" >&2
  exit 1
fi
gcloud secrets describe POSTGRES_DSN --project="$PROJECT_ID" >/dev/null

secret_exists() {
  gcloud secrets describe "$1" --project="$PROJECT_ID" >/dev/null 2>&1
}

SA_OPT=""
if [ -n "$SERVICE_ACCOUNT" ]; then
  SA_OPT="--service-account=$SERVICE_ACCOUNT"
fi

if [ -n "${IMAGE_TAG:-}" ]; then
  TAG="$IMAGE_TAG"
else
  if command -v git >/dev/null 2>&1; then
    TAG="$(git rev-parse --short HEAD 2>/dev/null || true)"
  fi
  TAG="${TAG:-latest}"
fi
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/app:${TAG}"

echo "Building image: $IMAGE"
if [ "${USE_CLOUD_BUILD:-0}" = "1" ] || ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "Using Cloud Build (no local Docker required)..."
  gcloud builds submit \
    --project="$PROJECT_ID" \
    --tag="$IMAGE" \
    --machine-type=e2-highcpu-8 \
    .
else
  docker build -t "$IMAGE" .
  docker push "$IMAGE"
fi

SECRETS="POSTGRES_DSN=POSTGRES_DSN:latest"
if [ "$INCLUDE_GEMINI_SECRET" = "1" ] && secret_exists GEMINI_API_KEY; then
  SECRETS="${SECRETS},GEMINI_API_KEY=GEMINI_API_KEY:latest"
fi
if secret_exists OPENAI_API_KEY; then
  SECRETS="${SECRETS},OPENAI_API_KEY=OPENAI_API_KEY:latest"
fi
if secret_exists GEMINI_API_KEY; then
  SECRETS="${SECRETS},GEMINI_API_KEY=GEMINI_API_KEY:latest"
fi
if secret_exists ANTHROPIC_API_KEY; then
  SECRETS="${SECRETS},ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest"
fi
if secret_exists GROQ_API_KEY; then
  SECRETS="${SECRETS},GROQ_API_KEY=GROQ_API_KEY:latest"
fi

AUTH_FLAG="--no-allow-unauthenticated"
if [ "$ALLOW_PUBLIC" = "1" ]; then
  AUTH_FLAG="--allow-unauthenticated"
fi

COMMON_ENV="APP_ENV=cloud,APP_PORT=8080,APP_HOST=0.0.0.0,CORE_DB_BACKEND=postgres,CORE_DB_GUARD_ENFORCE=1,CORE_DB_STRICT_POSTGRES=1,PHASE2_POSTGRES_ENABLED=1,AI_QUEUE_BACKEND=postgres,AI_QUEUE_STRICT_PROD=1,AI_TIMEOUT_SECONDS=${AI_TIMEOUT_SECONDS:-45},AI_MAX_TOKENS=${AI_MAX_TOKENS:-1200},AI_ENABLE_RESPONSE_CACHE=${AI_ENABLE_RESPONSE_CACHE:-1},AI_CACHE_TTL_SEC=${AI_CACHE_TTL_SEC:-300},AI_CACHE_MAX_ENTRIES=${AI_CACHE_MAX_ENTRIES:-256},AI_CHAT_READONLY=${AI_CHAT_READONLY:-1},AI_COMMAND_SYNC_BUDGET_MS=${AI_COMMAND_SYNC_BUDGET_MS:-38000},ENABLE_IR_AUDIO_TRANSCRIBE=${ENABLE_IR_AUDIO_TRANSCRIBE:-1}"
if [ -n "${CLOUD_FILES_BUCKET:-}" ]; then
  COMMON_ENV="${COMMON_ENV},CLOUD_FILES_BUCKET=${CLOUD_FILES_BUCKET}"
fi
if [ -n "${CLOUD_FILES_PREFIX:-}" ]; then
  COMMON_ENV="${COMMON_ENV},CLOUD_FILES_PREFIX=${CLOUD_FILES_PREFIX}"
fi

echo "Deploying app service: ${APP_SERVICE}"
gcloud run deploy "$APP_SERVICE" \
  --image="$IMAGE" \
  --region="$REGION" \
  --platform=managed \
  "$AUTH_FLAG" \
  $SA_OPT \
  --add-cloudsql-instances="${PROJECT_ID}:${REGION}:${DB_INSTANCE}" \
  --set-env-vars="$COMMON_ENV" \
  --set-secrets="$SECRETS"

if [ "$DEPLOY_WORKER" = "1" ]; then
  echo "Deploying worker service: ${WORKER_SERVICE}"
  WORKER_ENV="APP_ENV=cloud,CORE_DB_BACKEND=postgres,CORE_DB_GUARD_ENFORCE=1,CORE_DB_STRICT_POSTGRES=1,PHASE2_POSTGRES_ENABLED=1,AI_QUEUE_BACKEND=postgres,AI_QUEUE_STRICT_PROD=1"
  if [ -n "${CLOUD_FILES_BUCKET:-}" ]; then
    WORKER_ENV="${WORKER_ENV},CLOUD_FILES_BUCKET=${CLOUD_FILES_BUCKET}"
  fi
  if [ -n "${CLOUD_FILES_PREFIX:-}" ]; then
    WORKER_ENV="${WORKER_ENV},CLOUD_FILES_PREFIX=${CLOUD_FILES_PREFIX}"
  fi
  gcloud run deploy "$WORKER_SERVICE" \
    --image="$IMAGE" \
    --region="$REGION" \
    --platform=managed \
    --no-allow-unauthenticated \
    $SA_OPT \
    --command="/app/bin/run_ai_worker" \
    --add-cloudsql-instances="${PROJECT_ID}:${REGION}:${DB_INSTANCE}" \
    --set-env-vars="$WORKER_ENV" \
    --set-secrets="$SECRETS"
fi

APP_URL="$(gcloud run services describe "$APP_SERVICE" --region "$REGION" --project "$PROJECT_ID" --format='value(status.url)')"
echo "App URL: $APP_URL"

echo "Health checks:"
curl -fsS "${APP_URL}/health/live" && echo
curl -fsS "${APP_URL}/health/ready" && echo
if [ "$DEPLOY_WORKER" = "1" ]; then
  curl -fsS "${APP_URL}/ai/worker/health" && echo
fi

echo "Deploy complete."
echo "Rollback command example:"
echo "ROLLBACK_TAG=<previous_tag> PROJECT_ID=$PROJECT_ID REGION=$REGION REPO=$REPO APP_SERVICE=$APP_SERVICE WORKER_SERVICE=$WORKER_SERVICE DB_INSTANCE=$DB_INSTANCE ./bin/rollback_cloud_run.sh"
