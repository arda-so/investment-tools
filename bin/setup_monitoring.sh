#!/usr/bin/env bash
# Sets up Cloud Monitoring alert policies + email notification channel for InvestorOS.
# Idempotent: can be re-run; will skip channel creation if one with the same email exists.
#
# Usage:
#   bin/setup_monitoring.sh
#   ALERT_EMAIL=you@example.com bin/setup_monitoring.sh
#   DRY_RUN=1 bin/setup_monitoring.sh   # print what would be created without sending requests
set -euo pipefail

ROOT="${INVESTOR_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MONITORING_DIR="${ROOT}/bin/monitoring"

PROJECT="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
ALERT_EMAIL="${ALERT_EMAIL:-$(gcloud config get-value account 2>/dev/null)}"
DRY_RUN="${DRY_RUN:-0}"
MONITORING_API="https://monitoring.googleapis.com/v3/projects/${PROJECT}"

if [ -z "$PROJECT" ] || [ "$PROJECT" = "(unset)" ]; then
  echo "ERROR: PROJECT_ID is not set. Run: gcloud config set project <id>" >&2
  exit 1
fi

if [ -z "$ALERT_EMAIL" ]; then
  echo "ERROR: ALERT_EMAIL is not set (or gcloud account not configured)." >&2
  exit 1
fi

echo "=== InvestorOS Cloud Monitoring Setup ==="
echo "  Project : $PROJECT"
echo "  Email   : $ALERT_EMAIL"
echo "  DRY_RUN : $DRY_RUN"
echo ""

TOKEN=$(gcloud auth print-access-token)

_api() {
  local method="$1"
  local path="$2"
  local body="${3:-}"
  if [ "$DRY_RUN" = "1" ]; then
    echo "[DRY_RUN] $method ${MONITORING_API}${path}" >&2
    if [ -n "$body" ]; then echo "  Body: $body" >&2; fi
    # Return minimal valid JSON for each API shape so callers can parse it
    if [ "$method" = "GET" ] && echo "$path" | grep -q "notificationChannels"; then
      echo '{"notificationChannels":[]}'
    elif [ "$method" = "GET" ] && echo "$path" | grep -q "alertPolicies"; then
      echo '{"alertPolicies":[]}'
    else
      echo "{\"name\":\"projects/${PROJECT}/dryrun/$(date +%s)\"}"
    fi
    return 0
  fi
  if [ -n "$body" ]; then
    curl -s -X "$method" \
      "${MONITORING_API}${path}" \
      -H "Authorization: Bearer ${TOKEN}" \
      -H "Content-Type: application/json" \
      -d "$body"
  else
    curl -s -X "$method" \
      "${MONITORING_API}${path}" \
      -H "Authorization: Bearer ${TOKEN}" \
      -H "Content-Type: application/json"
  fi
}

# ── Step 1: Notification channel ──────────────────────────────────────────────
echo "── Step 1: Email notification channel"

# Check if email channel already exists
EXISTING_CHANNEL=""
CHANNELS_RESP=$(_api GET "/notificationChannels?filter=type%3D%22email%22")
EXISTING_CHANNEL=$(echo "$CHANNELS_RESP" | python3 -c "
import sys, json
data = json.load(sys.stdin)
channels = data.get('notificationChannels', [])
for c in channels:
    if c.get('labels', {}).get('email_address', '').lower() == '${ALERT_EMAIL}'.lower():
        print(c['name'])
        break
" 2>/dev/null || true)

if [ -n "$EXISTING_CHANNEL" ]; then
  echo "  Found existing channel: $EXISTING_CHANNEL"
  CHANNEL_NAME="$EXISTING_CHANNEL"
else
  echo "  Creating email channel for: $ALERT_EMAIL"
  CHANNEL_RESP=$(_api POST "/notificationChannels" "{
    \"type\": \"email\",
    \"displayName\": \"InvestorOS Alerts → ${ALERT_EMAIL}\",
    \"labels\": {\"email_address\": \"${ALERT_EMAIL}\"},
    \"userLabels\": {\"managed_by\": \"setup_monitoring_sh\"}
  }")
  CHANNEL_NAME=$(echo "$CHANNEL_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('name','ERROR'))" 2>/dev/null || true)
  if [ -z "$CHANNEL_NAME" ] || [ "$CHANNEL_NAME" = "ERROR" ]; then
    echo "ERROR: Failed to create notification channel. Response:" >&2
    echo "$CHANNEL_RESP" >&2
    exit 1
  fi
  echo "  Created channel: $CHANNEL_NAME"
fi

echo ""

# ── Step 2: Alert policies ─────────────────────────────────────────────────────
echo "── Step 2: Alert policies"

_create_policy() {
  local policy_file="$1"
  local policy_name
  policy_name=$(python3 -c "import json; d=json.load(open('$policy_file')); print(d['displayName'])" 2>/dev/null)

  echo ""
  echo "  Policy: $policy_name"

  # Check if policy already exists (by displayName)
  EXISTING_POLICIES=$(_api GET "/alertPolicies")
  EXISTING_POLICY_NAME=$(echo "$EXISTING_POLICIES" | python3 -c "
import sys, json
data = json.load(sys.stdin)
policies = data.get('alertPolicies', [])
target = open('$policy_file').read()
target_name = json.loads(target)['displayName']
for p in policies:
    if p.get('displayName','') == target_name:
        print(p['name'])
        break
" 2>/dev/null || true)

  if [ -n "$EXISTING_POLICY_NAME" ]; then
    echo "  → Already exists: $EXISTING_POLICY_NAME (skipping)"
    return 0
  fi

  # Inject channel name into policy JSON
  POLICY_JSON=$(python3 -c "
import json, sys
policy = json.load(open('$policy_file'))
policy['notificationChannels'] = ['${CHANNEL_NAME}']
print(json.dumps(policy))
")

  POLICY_RESP=$(_api POST "/alertPolicies" "$POLICY_JSON")
  CREATED_NAME=$(echo "$POLICY_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('name','ERROR'))" 2>/dev/null || true)

  if [ -z "$CREATED_NAME" ] || [ "$CREATED_NAME" = "ERROR" ]; then
    echo "  ERROR: Failed to create policy. Response:" >&2
    echo "$POLICY_RESP" >&2
    # Don't exit — continue with other policies
    return 1
  fi

  echo "  → Created: $CREATED_NAME"
}

_create_policy "${MONITORING_DIR}/policy_app_latency_p95.json"
_create_policy "${MONITORING_DIR}/policy_job_failures.json"
_create_policy "${MONITORING_DIR}/policy_stuck_runs.json"

echo ""
echo "=== Done ==="
echo ""
echo "View policies:"
echo "  https://console.cloud.google.com/monitoring/alerting?project=${PROJECT}"
echo ""
echo "To delete all InvestorOS policies (if needed for re-create):"
echo "  gcloud monitoring policies list --project=${PROJECT} --format='value(name)' | xargs -I{} gcloud monitoring policies delete {} --project=${PROJECT} --quiet"
