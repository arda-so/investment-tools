#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Use venv python if available (has all project deps incl. sqlalchemy, pydantic)
if [ -x "$ROOT_DIR/.venv-memory/bin/python" ]; then
  PY="$ROOT_DIR/.venv-memory/bin/python"
else
  PY=""$PY""
fi

echo "[quality-gate] Syntax check (py_compile)..."
"$PY" -m py_compile $(find app -type f -name "*.py" | sort)

echo "[quality-gate] Ruff hard-error lint..."
ruff check app --select E9,F63,F7,F82

echo "[quality-gate] DRY audit..."
"$PY" bin/check_dry.py

echo "[quality-gate] AI data-policy audit..."
"$PY" bin/check_ai_data_policy.py

echo "[quality-gate] Data-first policy eval..."
"$PY" tools/eval_data_first_policy.py

echo "[quality-gate] Replay smoke tests..."
CORE_DB_BACKEND=postgres "$PY" tools/run_replay_tests.py --smoke

echo "[quality-gate] PASS"
