#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PATTERN='(from[[:space:]]+app\.services\.ai_async_service[[:space:]]+import|import[[:space:]]+app\.services\.ai_async_service|ai_async_service)'
EXCLUDE='app/services/ai_async_service.py'

if command -v rg >/dev/null 2>&1; then
  HITS="$(rg -n --glob '!app/services/ai_async_service.py' "$PATTERN" app tools bin || true)"
else
  HITS="$(grep -RInE "$PATTERN" app tools bin 2>/dev/null | grep -v "^$EXCLUDE:" || true)"
fi

if [[ -n "${HITS}" ]]; then
  echo "ERROR: legacy ai_async_service references found:"
  echo "$HITS"
  exit 1
fi

echo "OK: no legacy ai_async_service references outside shim."
