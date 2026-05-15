#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

# Ensure repo root is on sys.path when running as a script.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import app_env  # noqa: F401  # ensures .env loader side effects
from app.services.ontology_quality_service import cleanup_noisy_company_entities


def main() -> int:
    p = argparse.ArgumentParser(
        description="Cleanup noisy COMPANY entities and attached relationships (Postgres-only)."
    )
    p.add_argument("--limit", type=int, default=20000, help="Max noisy entities to evaluate.")
    p.add_argument(
        "--apply",
        action="store_true",
        help="Apply deletion. Without this flag the command runs in dry-run mode.",
    )
    args = p.parse_args()
    out = cleanup_noisy_company_entities(apply=bool(args.apply), limit=max(1, int(args.limit or 20000)))
    print(json.dumps(out, ensure_ascii=True, indent=2))
    return 0 if bool(out.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
