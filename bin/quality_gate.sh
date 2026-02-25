#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "[quality-gate] Syntax check (py_compile)..."
python3 -m py_compile $(find app -type f -name "*.py" | sort)

echo "[quality-gate] Ruff hard-error lint..."
ruff check app --select E9,F63,F7,F82

echo "[quality-gate] DRY audit..."
python3 bin/check_dry.py

echo "[quality-gate] AI data-policy audit..."
python3 bin/check_ai_data_policy.py

echo "[quality-gate] Data-first policy eval..."
python3 tools/eval_data_first_policy.py

echo "[quality-gate] PASS"
