#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.entity_resolution_service import (
    ensure_entity_resolution_schema,
    process_entity_resolution_judge_queue,
    run_deterministic_canonicalization,
)


def main() -> int:
    p = argparse.ArgumentParser(description="Deterministic-first entity canonicalization + optional LLM-judge queue processing.")
    p.add_argument("--scan-limit", type=int, default=5000, help="Company rows to scan for deterministic pairing.")
    p.add_argument("--queue-limit", type=int, default=2000, help="Max deterministic candidate pairs to evaluate/enqueue.")
    p.add_argument("--judge-limit", type=int, default=200, help="Pending queue rows for judge processing.")
    p.add_argument("--apply", action="store_true", help="Apply merges and queue status updates.")
    p.add_argument("--process-judge", action="store_true", help="Process pending queue rows via deterministic/LLM judge.")
    args = p.parse_args()

    out: dict[str, object] = {
        "schema": ensure_entity_resolution_schema(),
        "deterministic": run_deterministic_canonicalization(
            apply=bool(args.apply),
            scan_limit=max(100, int(args.scan_limit or 5000)),
            queue_limit=max(50, int(args.queue_limit or 2000)),
        ),
    }
    if bool(args.process_judge):
        out["judge"] = process_entity_resolution_judge_queue(
            apply=bool(args.apply),
            limit=max(1, int(args.judge_limit or 200)),
        )

    ok = bool((out.get("schema") or {}).get("ok")) and bool((out.get("deterministic") or {}).get("ok"))
    if bool(args.process_judge):
        ok = ok and bool((out.get("judge") or {}).get("ok"))
    print(json.dumps(out, ensure_ascii=True, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
