#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f ".venv-memory/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source ".venv-memory/bin/activate"
fi

set -a
if [[ -f ".env" ]]; then
  # shellcheck disable=SC1091
  source ".env"
fi
set +a

python tools/refresh_relationship_decay_scores.py --limit 500000 --apply

