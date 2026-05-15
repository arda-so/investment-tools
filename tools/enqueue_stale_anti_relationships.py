#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.ontology_quality_service import enqueue_stale_anti_relationships


def main() -> int:
    p = argparse.ArgumentParser(description="Enqueue stale anti-relationships (HEDGES_AGAINST/IMMUNE_TO) for re-verification.")
    p.add_argument("--limit", type=int, default=200000, help="Max rows to scan.")
    p.add_argument("--apply", action="store_true", help="Apply queue writes; dry-run without this flag.")
    args = p.parse_args()
    out = enqueue_stale_anti_relationships(apply=bool(args.apply), limit=max(1, int(args.limit or 200000)))
    print(json.dumps(out, ensure_ascii=True, indent=2))
    return 0 if bool(out.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
