#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys
import argparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from onyx_db import run_thesis_check_all_active


def main() -> None:
    ap = argparse.ArgumentParser(description="Run Onyx thesis checks for active holdings.")
    ap.add_argument("--force", action="store_true", help="Ignore cooldown and run checks now.")
    ap.add_argument("--interval", type=int, default=21600, help="Cooldown in seconds (default: 21600).")
    args = ap.parse_args()
    interval = 0 if args.force else max(0, int(args.interval))
    rows = run_thesis_check_all_active(min_interval_seconds=interval)
    stamp = Path("reports")
    stamp.mkdir(parents=True, exist_ok=True)
    out = stamp / "onyx_thesis_checks_latest.json"
    out.write_text(json.dumps(rows, ensure_ascii=True, indent=2), encoding="utf-8")
    print(f"checked={len(rows)}")
    print(str(out.resolve()))


if __name__ == "__main__":
    main()
