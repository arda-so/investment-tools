#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.ontology_quality_service import process_anti_reverify_queue


def main() -> int:
    p = argparse.ArgumentParser(description="Process anti-relationship re-verification queue.")
    p.add_argument("--limit", type=int, default=250, help="Max pending rows to process.")
    p.add_argument("--apply", action="store_true", help="Apply status/relationship updates.")
    args = p.parse_args()
    out = process_anti_reverify_queue(apply=bool(args.apply), limit=max(1, int(args.limit or 250)))
    print(json.dumps(out, ensure_ascii=True, indent=2))
    return 0 if bool(out.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
