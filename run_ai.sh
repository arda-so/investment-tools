#!/usr/bin/env bash
set -euo pipefail

source /Users/solmaz/.zshrc

prompt="${1:-Hello}"
system_message="${2:-Test}"

python3 - <<PY
from tools.llm_engine import ask_ai
print(ask_ai(${prompt!r}, ${system_message!r}))
PY
