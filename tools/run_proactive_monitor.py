#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.proactive_ai_service import run_event_driven_monitor


def main() -> int:
    ap = argparse.ArgumentParser(description="Run proactive monitor (event-driven by default).")
    ap.add_argument("--force", action="store_true", help="force scan even if no new filings/report_facts")
    args = ap.parse_args()
    out = run_event_driven_monitor(force=bool(args.force))
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
