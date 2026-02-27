#!/usr/bin/env python3
"""
run_outcome_measurement.py — Measure pending T+7/30/90 proposal outcomes.

Called nightly after agent_worker completes.  Finds all proposal_outcomes_core
rows with status='pending' whose measurement window has elapsed, fetches the
current price, and marks them correct/incorrect.

Usage:
    python tools/run_outcome_measurement.py
    python tools/run_outcome_measurement.py --verbose
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.proactive_ai_service import measure_proposal_outcomes


def main() -> None:
    verbose = "--verbose" in sys.argv
    print("[outcome_measurement] Running T+7/30/90 outcome checks …")
    try:
        result = measure_proposal_outcomes()
        measured = result.get("measured", 0)
        skipped  = result.get("skipped", 0)
        errors   = result.get("errors", 0)
        print(f"[outcome_measurement] Done — measured={measured}  skipped={skipped}  errors={errors}")
        if verbose:
            import json
            print(json.dumps(result, indent=2, default=str))
    except Exception as exc:
        print(f"[outcome_measurement] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
