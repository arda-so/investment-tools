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
DB_INSTANCE="${DB_INSTANCE:-investor-os-pg}"
JOB_NAME="${JOB_NAME:-investor-tools-worker-job}"
APP_SERVICE="${APP_SERVICE:-investor-tools-app}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-investor-tools-runtime@${PROJECT_ID}.iam.gserviceaccount.com}"
SCHEDULER_JOB="${SCHEDULER_JOB:-investor-tools-worker-cron}"
SCHEDULER_LOCATION="${SCHEDULER_LOCATION:-europe-west1}"
SCHEDULE="${SCHEDULE:-*/2 * * * *}"
TASK_TIMEOUT="${TASK_TIMEOUT:-900s}"
MAX_RETRIES="${MAX_RETRIES:-0}"
TASKS="${TASKS:-1}"
ENABLE_SCHEDULER="${ENABLE_SCHEDULER:-1}"
INCLUDE_GEMINI_SECRET="${INCLUDE_GEMINI_SECRET:-1}"

if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
  echo "PROJECT_ID is required. Set PROJECT_ID or run: gcloud config set project <id>" >&2
  exit 1
fi

IMAGE="${IMAGE:-}"
if [ -z "$IMAGE" ]; then
  IMAGE="$(gcloud run services describe "$APP_SERVICE" --region="$REGION" --project="$PROJECT_ID" --format='value(spec.template.spec.containers[0].image)')"
fi

if [ -z "$IMAGE" ]; then
  echo "Could not resolve image from app service ${APP_SERVICE}. Set IMAGE explicitly." >&2
  exit 1
fi

SECRETS="POSTGRES_DSN=POSTGRES_DSN:latest"
if [ "$INCLUDE_GEMINI_SECRET" = "1" ]; then
  SECRETS="${SECRETS},GEMINI_API_KEY=GEMINI_API_KEY:latest"
fi

ENV_VARS="APP_ENV=cloud,CORE_DB_BACKEND=postgres,CORE_DB_GUARD_ENFORCE=1,CORE_DB_STRICT_POSTGRES=1,PHASE2_POSTGRES_ENABLED=1,AI_QUEUE_BACKEND=postgres,AI_QUEUE_STRICT_PROD=1,AI_WORKER_ONCE=1"
if [ -n "${CLOUD_FILES_BUCKET:-}" ]; then
  ENV_VARS="${ENV_VARS},CLOUD_FILES_BUCKET=${CLOUD_FILES_BUCKET}"
fi
if [ -n "${CLOUD_FILES_PREFIX:-}" ]; then
  ENV_VARS="${ENV_VARS},CLOUD_FILES_PREFIX=${CLOUD_FILES_PREFIX}"
fi

CONN_NAME="${PROJECT_ID}:${REGION}:${DB_INSTANCE}"
echo "Deploying worker job ${JOB_NAME} using image:"
echo "  ${IMAGE}"

gcloud run jobs deploy "$JOB_NAME" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --image="$IMAGE" \
  --service-account="$SERVICE_ACCOUNT" \
  --set-cloudsql-instances="$CONN_NAME" \
  --set-env-vars="$ENV_VARS" \
  --set-secrets="$SECRETS" \
  --command="/app/bin/run_ai_worker" \
  --tasks="$TASKS" \
  --max-retries="$MAX_RETRIES" \
  --task-timeout="$TASK_TIMEOUT"

echo "Running worker job once now..."
gcloud run jobs execute "$JOB_NAME" --project="$PROJECT_ID" --region="$REGION" --wait

# ---------------------------------------------------------------------------
# Agent worker job (investor-agent-worker)
# Runs after the nightly SEC sync; analyzes new filings for held tickers.
# ---------------------------------------------------------------------------
AGENT_JOB_NAME="${AGENT_JOB_NAME:-investor-agent-worker}"
AGENT_SCHEDULER_JOB="${AGENT_SCHEDULER_JOB:-investor-agent-worker-cron}"
AGENT_SCHEDULE="${AGENT_SCHEDULE:-30 2 * * *}"   # 02:30 UTC nightly
AGENT_TASK_TIMEOUT="${AGENT_TASK_TIMEOUT:-1800s}" # 30 min max
AGENT_ENV_VARS="${ENV_VARS}"  # AI_WORKER_ONCE=1 already present in ENV_VARS

echo ""
echo "Deploying agent worker job ${AGENT_JOB_NAME} …"

gcloud run jobs deploy "$AGENT_JOB_NAME" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --image="$IMAGE" \
  --service-account="$SERVICE_ACCOUNT" \
  --set-cloudsql-instances="$CONN_NAME" \
  --set-env-vars="$AGENT_ENV_VARS" \
  --set-secrets="$SECRETS" \
  --command="/app/bin/run_agent_worker" \
  --args="--watch-all,--days,1" \
  --tasks=1 \
  --max-retries=0 \
  --task-timeout="$AGENT_TASK_TIMEOUT"

if [ "$ENABLE_SCHEDULER" = "1" ]; then
  AGENT_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${AGENT_JOB_NAME}:run"

  if gcloud scheduler jobs describe "$AGENT_SCHEDULER_JOB" --project="$PROJECT_ID" --location="$SCHEDULER_LOCATION" >/dev/null 2>&1; then
    gcloud scheduler jobs update http "$AGENT_SCHEDULER_JOB" \
      --project="$PROJECT_ID" \
      --location="$SCHEDULER_LOCATION" \
      --schedule="$AGENT_SCHEDULE" \
      --uri="$AGENT_URI" \
      --http-method=POST \
      --oauth-service-account-email="$SERVICE_ACCOUNT" \
      --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform"
  else
    gcloud scheduler jobs create http "$AGENT_SCHEDULER_JOB" \
      --project="$PROJECT_ID" \
      --location="$SCHEDULER_LOCATION" \
      --schedule="$AGENT_SCHEDULE" \
      --uri="$AGENT_URI" \
      --http-method=POST \
      --oauth-service-account-email="$SERVICE_ACCOUNT" \
      --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform"
  fi
  echo "Agent scheduler job ${AGENT_SCHEDULER_JOB} active (${AGENT_SCHEDULE})."
fi

if [ "$ENABLE_SCHEDULER" = "1" ]; then
  URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${JOB_NAME}:run"
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SERVICE_ACCOUNT}" \
    --role="roles/run.invoker" >/dev/null

  if gcloud scheduler jobs describe "$SCHEDULER_JOB" --project="$PROJECT_ID" --location="$SCHEDULER_LOCATION" >/dev/null 2>&1; then
    gcloud scheduler jobs update http "$SCHEDULER_JOB" \
      --project="$PROJECT_ID" \
      --location="$SCHEDULER_LOCATION" \
      --schedule="$SCHEDULE" \
      --uri="$URI" \
      --http-method=POST \
      --oauth-service-account-email="$SERVICE_ACCOUNT" \
      --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform"
  else
    gcloud scheduler jobs create http "$SCHEDULER_JOB" \
      --project="$PROJECT_ID" \
      --location="$SCHEDULER_LOCATION" \
      --schedule="$SCHEDULE" \
      --uri="$URI" \
      --http-method=POST \
      --oauth-service-account-email="$SERVICE_ACCOUNT" \
      --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform"
  fi
  echo "Scheduler job ${SCHEDULER_JOB} active with schedule: ${SCHEDULE}"
fi

echo "Done."
