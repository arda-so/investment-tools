#!/usr/bin/env bash
set -euo pipefail

ROOT="${INVESTOR_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$ROOT"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing command: $1" >&2; exit 1; }
}

need_cmd gcloud

secret_exists() {
  gcloud secrets describe "$1" --project="$PROJECT_ID" >/dev/null 2>&1
}

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="${REGION:-europe-west1}"
DB_INSTANCE="${DB_INSTANCE:-investor-os-pg}"
JOB_NAME="${JOB_NAME:-investor-tools-worker-job}"
APP_SERVICE="${APP_SERVICE:-investor-tools-app}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-investor-tools-runtime@${PROJECT_ID}.iam.gserviceaccount.com}"
SCHEDULER_JOB="${SCHEDULER_JOB:-investor-tools-worker-cron}"
SCHEDULER_LOCATION="${SCHEDULER_LOCATION:-europe-west1}"
SCHEDULE="${SCHEDULE:-*/10 * * * *}"
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

ENV_VARS="APP_ENV=cloud,CORE_DB_BACKEND=postgres,CORE_DB_GUARD_ENFORCE=1,CORE_DB_STRICT_POSTGRES=1,PHASE2_POSTGRES_ENABLED=1,AI_QUEUE_BACKEND=postgres,AI_QUEUE_STRICT_PROD=1,AI_WORKER_ONCE=1,AI_TIMEOUT_SECONDS=${AI_TIMEOUT_SECONDS:-45},AI_MAX_TOKENS=${AI_MAX_TOKENS:-1200},AI_ENABLE_RESPONSE_CACHE=${AI_ENABLE_RESPONSE_CACHE:-1},AI_CACHE_TTL_SEC=${AI_CACHE_TTL_SEC:-300},AI_CACHE_MAX_ENTRIES=${AI_CACHE_MAX_ENTRIES:-256},ENABLE_IR_AUDIO_TRANSCRIBE=${ENABLE_IR_AUDIO_TRANSCRIBE:-1}"
if [ -n "${CLOUD_FILES_BUCKET:-}" ]; then
  ENV_VARS="${ENV_VARS},CLOUD_FILES_BUCKET=${CLOUD_FILES_BUCKET}"
fi
if [ -n "${CLOUD_FILES_PREFIX:-}" ]; then
  ENV_VARS="${ENV_VARS},CLOUD_FILES_PREFIX=${CLOUD_FILES_PREFIX}"
fi
if [ -n "${WHISPER_PYTHON_BIN:-}" ]; then
  ENV_VARS="${ENV_VARS},WHISPER_PYTHON_BIN=${WHISPER_PYTHON_BIN}"
fi
if [ -n "${WHISPER_MODEL:-}" ]; then
  ENV_VARS="${ENV_VARS},WHISPER_MODEL=${WHISPER_MODEL}"
fi
if [ -n "${WHISPER_LANGUAGE:-}" ]; then
  ENV_VARS="${ENV_VARS},WHISPER_LANGUAGE=${WHISPER_LANGUAGE}"
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

echo "Done deploying core jobs."

# ---------------------------------------------------------------------------
# Helper: deploy_job NAME COMMAND ARGS SCHEDULE TIMEOUT
#   NAME     — Cloud Run job name (e.g. investor-morning-job)
#   COMMAND  — container entrypoint (e.g. /app/bin/run_morning_job)
#   ARGS     — comma-separated args or "" for none
#   SCHEDULE — cron expression (e.g. "0 7 * * 1-5")
#   TIMEOUT  — task timeout (e.g. 1800s)
# ---------------------------------------------------------------------------
deploy_job() {
  local name="$1" cmd="$2" args="$3" sched="$4" timeout="$5"
  local cron_name="${name}-cron"
  local uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${name}:run"

  echo ""
  echo "── Deploying job: ${name} (${sched}) ──"

  local deploy_args=(
    gcloud run jobs deploy "$name"
    --project="$PROJECT_ID"
    --region="$REGION"
    --image="$IMAGE"
    --service-account="$SERVICE_ACCOUNT"
    --set-cloudsql-instances="$CONN_NAME"
    --set-env-vars="$ENV_VARS"
    --set-secrets="$SECRETS"
    --command="$cmd"
    --tasks=1
    --max-retries=0
    --task-timeout="$timeout"
  )
  if [ -n "$args" ]; then
    deploy_args+=(--args="$args")
  fi
  "${deploy_args[@]}"

  if [ "${ENABLE_SCHEDULER:-1}" = "1" ]; then
    if gcloud scheduler jobs describe "$cron_name" --project="$PROJECT_ID" --location="$SCHEDULER_LOCATION" >/dev/null 2>&1; then
      gcloud scheduler jobs update http "$cron_name" \
        --project="$PROJECT_ID" \
        --location="$SCHEDULER_LOCATION" \
        --schedule="$sched" \
        --uri="$uri" \
        --http-method=POST \
        --oauth-service-account-email="$SERVICE_ACCOUNT" \
        --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform"
    else
      gcloud scheduler jobs create http "$cron_name" \
        --project="$PROJECT_ID" \
        --location="$SCHEDULER_LOCATION" \
        --schedule="$sched" \
        --uri="$uri" \
        --http-method=POST \
        --oauth-service-account-email="$SERVICE_ACCOUNT" \
        --oauth-token-scope="https://www.googleapis.com/auth/cloud-platform"
    fi
    echo "  Scheduler ${cron_name}: ${sched}"
  fi
}

# ---------------------------------------------------------------------------
# Automation jobs (mirrors local launchd automations, now cloud-primary)
# ---------------------------------------------------------------------------

# SEC nightly sync: runs at 02:00 UTC so filings are fresh before agent worker (02:30)
deploy_job "investor-sec-nightly-job" \
  "/app/bin/run_sec_nightly_job" "" \
  "0 2 * * *" "1200s"

# Morning brief + proactive proposals: weekdays 07:00 UTC (07/08 AM London)
deploy_job "investor-morning-job" \
  "/app/bin/run_morning_job" "" \
  "0 7 * * 1-5" "1800s"

# Pre-earnings prep: weekdays 07:30 UTC (just after morning brief)
deploy_job "investor-earnings-job" \
  "/app/bin/run_earnings_job" "" \
  "30 7 * * 1-5" "1800s"

# Midday signals refresh: weekdays 12:00 UTC
deploy_job "investor-signals-job" \
  "/app/bin/run_signals_job" "" \
  "0 12 * * 1-5" "600s"

# End-of-day full refresh: weekdays 18:00 UTC (after US market close)
deploy_job "investor-daily-job" \
  "/app/bin/run_daily_job" "" \
  "0 18 * * 1-5" "2400s"

# Earnings transcript ingest queue drainer: every 2 minutes (cloud-only, no local terminal needed)
deploy_job "investor-earnings-ingest-job" \
  "/app/bin/run_earnings_ingest_worker" "--once" \
  "*/2 * * * *" "900s"

# Universe registry refresh: daily (SEC company tickers/exchanges to core tables)
UNIVERSE_SYNC_SCHEDULE="${UNIVERSE_SYNC_SCHEDULE:-15 3 * * *}"
deploy_job "investor-universe-sync-job" \
  "/app/bin/run_universe_sync_job" "" \
  "${UNIVERSE_SYNC_SCHEDULE}" "1800s"

# EDGAR near-real-time watch: every 15 min — polls held/watchlist tickers for new SEC filings
# and fires run_event_driven_monitor() so proposals surface within ~15 min of SEC filing.
# Timeout 600s (10 min) allows LLM calls in run_event_driven_monitor to complete.
deploy_job "investor-edgar-watch-job" \
  "/app/bin/run_edgar_watch" "" \
  "*/15 * * * *" "600s"

echo ""
echo "All jobs deployed."
