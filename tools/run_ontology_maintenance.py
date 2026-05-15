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
from app.services.ontology_quality_service import (
    backfill_relationship_temporal_decay,
    enqueue_criticality_review_candidates,
    enqueue_stale_anti_relationships,
    process_criticality_review_queue,
    process_anti_reverify_queue,
    refresh_relationship_decay_scores,
)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Run nightly ontology maintenance in one pass.",
    )
    p.add_argument("--decay-limit", type=int, default=500000)
    p.add_argument("--scan-limit", type=int, default=5000)
    p.add_argument("--queue-limit", type=int, default=2000)
    p.add_argument("--judge-limit", type=int, default=200)
    p.add_argument("--anti-limit", type=int, default=200000)
    p.add_argument("--anti-process-limit", type=int, default=250)
    p.add_argument("--criticality-limit", type=int, default=150000)
    p.add_argument("--criticality-process-limit", type=int, default=100)
    p.add_argument("--apply", action="store_true", help="Apply writes.")
    args = p.parse_args()

    apply = bool(args.apply)
    out: dict[str, object] = {}

    out["temporal_backfill"] = backfill_relationship_temporal_decay(
        apply=apply,
        limit=max(1, int(args.decay_limit or 500000)),
    )
    out["decay_refresh"] = refresh_relationship_decay_scores(
        apply=apply,
        limit=max(1, int(args.decay_limit or 500000)),
    )
    out["entity_schema"] = ensure_entity_resolution_schema()
    out["canonical_deterministic"] = run_deterministic_canonicalization(
        apply=apply,
        scan_limit=max(100, int(args.scan_limit or 5000)),
        queue_limit=max(50, int(args.queue_limit or 2000)),
    )
    out["canonical_judge"] = process_entity_resolution_judge_queue(
        apply=apply,
        limit=max(1, int(args.judge_limit or 200)),
    )
    out["anti_enqueue"] = enqueue_stale_anti_relationships(
        apply=apply,
        limit=max(1, int(args.anti_limit or 200000)),
    )
    out["anti_process"] = process_anti_reverify_queue(
        apply=apply,
        limit=max(1, int(args.anti_process_limit or 250)),
    )
    out["criticality_enqueue"] = enqueue_criticality_review_candidates(
        apply=apply,
        limit=max(1, int(args.criticality_limit or 150000)),
    )
    out["criticality_process"] = process_criticality_review_queue(
        apply=apply,
        limit=max(1, int(args.criticality_process_limit or 100)),
    )

    ok = all(
        bool((out.get(k) or {}).get("ok"))
        for k in [
            "temporal_backfill",
            "decay_refresh",
            "entity_schema",
            "canonical_deterministic",
            "canonical_judge",
            "anti_enqueue",
            "anti_process",
            "criticality_enqueue",
            "criticality_process",
        ]
    )
    out["ok"] = ok
    out["apply"] = apply
    print(json.dumps(out, ensure_ascii=True, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
