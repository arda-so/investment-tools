#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

HOST="${SMOKE_HOST:-127.0.0.1}"
PORT="${SMOKE_PORT:-8766}"
BASE="http://${HOST}:${PORT}"
LOG="${SMOKE_LOG:-/tmp/investor_os_smoke.log}"
START_APP="${SMOKE_START_APP:-1}"

cleanup() {
  if [[ -n "${APP_PID:-}" ]]; then
    kill "${APP_PID}" >/dev/null 2>&1 || true
    wait "${APP_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

if [[ "$START_APP" == "1" ]]; then
  ./bin/run_v2_app >"$LOG" 2>&1 &
  APP_PID=$!
  for _ in $(seq 1 40); do
    if curl -fsS "${BASE}/health/live" >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
  done
fi

curl -fsS "${BASE}/health/live" >/dev/null
curl -fsS "${BASE}/health/ready" >/dev/null
curl -fsS "${BASE}/dashboard" >/dev/null
curl -fsS "${BASE}/organizer" >/dev/null
curl -fsS "${BASE}/company_file?t=CRM" >/dev/null

ASYNC_JSON="$(curl -fsS -X POST "${BASE}/ai/command/async" \
  -H 'Content-Type: application/json' \
  -d '{"query":"hi","context":{"session_id":"ci-smoke"}}')"

echo "$ASYNC_JSON" | grep -Eq '"ok"\s*:\s*true'
echo "$ASYNC_JSON" | grep -Eq '"status"\s*:\s*"(queued|done)"'

echo "[smoke-runtime] PASS"
