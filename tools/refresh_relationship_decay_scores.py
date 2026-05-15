#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import app_env  # noqa: F401
from app.services.ontology_quality_service import refresh_relationship_decay_scores


def main() -> int:
    p = argparse.ArgumentParser(
        description="Refresh effective_confidence values for relationships_core (Postgres-only)."
    )
    p.add_argument("--limit", type=int, default=500000, help="Max rows to scan.")
    p.add_argument(
        "--apply",
        action="store_true",
        help="Apply updates. Without this flag runs dry-run only.",
    )
    args = p.parse_args()
    out = refresh_relationship_decay_scores(apply=bool(args.apply), limit=max(1, int(args.limit or 500000)))
    print(json.dumps(out, ensure_ascii=True, indent=2))
    return 0 if bool(out.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())

