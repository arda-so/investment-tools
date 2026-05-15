#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.portfolio_memory_service import save_morning_brief_snapshot


def main() -> int:
    brief = save_morning_brief_snapshot(limit_holdings=5, source="scheduler")
    print(json.dumps(brief, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
