#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.proactive_ai_service import simulate_macro_shock


DEFAULT_SCENARIOS = [
    "Taiwan semiconductor supply shock",
    "Oil price spike and shipping disruption",
    "US rates remain higher for longer",
]


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    idx = int(round(0.95 * (len(vals) - 1)))
    return float(vals[max(0, min(len(vals) - 1, idx))])


def _px(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    p = max(0.0, min(1.0, float(pct)))
    idx = int(round(p * (len(vals) - 1)))
    return float(vals[max(0, min(len(vals) - 1, idx))])


def main() -> int:
    p = argparse.ArgumentParser(description="Benchmark SQL macro-shock traversal latency.")
    p.add_argument("--runs", type=int, default=5, help="Runs per scenario.")
    p.add_argument("--max-depth", type=int, default=3, help="Traversal depth.")
    p.add_argument("--warmup", type=int, default=1, help="Warmup calls per scenario (excluded from metrics).")
    p.add_argument("--no-cache", action="store_true", help="Disable ontology simulation cache for this benchmark.")
    p.add_argument("--with-direction-ai", action="store_true", help="Keep LLM direction classifier enabled during benchmark.")
    p.add_argument("--max-retries", type=int, default=1, help="Max retries for detected runtime outliers.")
    p.add_argument("--outlier-multiplier", type=float, default=3.0, help="Retry threshold as multiple of running median latency.")
    p.add_argument("--trim-top-pct", type=float, default=0.05, help="Filter top pct of slowest samples for robust stats.")
    p.add_argument("--scenario", action="append", default=[], help="Scenario text (repeat flag for multiple).")
    args = p.parse_args()

    if bool(args.no_cache):
        os.environ["ONTOLOGY_SIM_CACHE_ENABLED"] = "0"
    if not bool(args.with_direction_ai):
        os.environ["ONTOLOGY_IMPACT_DIRECTION_AI_ENABLED"] = "0"

    runs = max(1, min(30, int(args.runs or 5)))
    depth = max(1, min(4, int(args.max_depth or 3)))
    warmup = max(0, min(10, int(args.warmup or 1)))
    max_retries = max(0, min(3, int(args.max_retries or 1)))
    outlier_multiplier = max(1.2, min(20.0, float(args.outlier_multiplier or 3.0)))
    trim_top_pct = max(0.0, min(0.4, float(args.trim_top_pct or 0.05)))
    scenarios = [str(s or "").strip() for s in list(args.scenario or []) if str(s or "").strip()] or list(DEFAULT_SCENARIOS)

    def _filtered(values: list[float]) -> list[float]:
        if not values:
            return []
        vals = sorted(values)
        cut = int(len(vals) * trim_top_pct)
        if cut <= 0:
            return vals
        keep = max(1, len(vals) - cut)
        return vals[:keep]

    out_rows: list[dict[str, object]] = []
    all_durations: list[float] = []
    all_filtered: list[float] = []
    for sc in scenarios:
        times_ms: list[float] = []
        hits = 0
        retries_used = 0
        for _ in range(warmup):
            simulate_macro_shock(event_description=sc, max_depth=depth)
        for _ in range(runs):
            attempts = 0
            while True:
                attempts += 1
                t0 = time.perf_counter()
                res = simulate_macro_shock(event_description=sc, max_depth=depth)
                dt_ms = (time.perf_counter() - t0) * 1000.0
                med = _px(times_ms, 0.50) if times_ms else 0.0
                is_outlier = bool(times_ms) and med > 0.0 and dt_ms > (med * outlier_multiplier)
                can_retry = is_outlier and attempts <= (max_retries + 1)
                if can_retry and attempts <= max_retries:
                    retries_used += 1
                    continue
                times_ms.append(dt_ms)
                all_durations.append(dt_ms)
                if bool((res or {}).get("cache_hit")):
                    hits += 1
                break
        filt = _filtered(times_ms)
        all_filtered.extend(filt)
        out_rows.append(
            {
                "scenario": sc,
                "runs": runs,
                "warmup_runs": warmup,
                "avg_ms": round(float(statistics.mean(times_ms)), 2),
                "p50_ms": round(_px(times_ms, 0.50), 2),
                "p95_ms": round(_p95(times_ms), 2),
                "p99_ms": round(_px(times_ms, 0.99), 2),
                "filtered_avg_ms": round(float(statistics.mean(filt)), 2) if filt else 0.0,
                "filtered_p95_ms": round(_p95(filt), 2) if filt else 0.0,
                "min_ms": round(float(min(times_ms)), 2),
                "max_ms": round(float(max(times_ms)), 2),
                "cache_hits": int(hits),
                "retries_used": int(retries_used),
            }
        )

    summary = {
        "ok": True,
        "runs_per_scenario": runs,
        "warmup_per_scenario": warmup,
        "retry_policy": {"max_retries": max_retries, "outlier_multiplier": outlier_multiplier},
        "trim_top_pct": trim_top_pct,
        "scenario_count": len(scenarios),
        "overall_avg_ms": round(float(statistics.mean(all_durations)), 2) if all_durations else 0.0,
        "overall_p50_ms": round(_px(all_durations, 0.50), 2) if all_durations else 0.0,
        "overall_p95_ms": round(_p95(all_durations), 2) if all_durations else 0.0,
        "overall_p99_ms": round(_px(all_durations, 0.99), 2) if all_durations else 0.0,
        "overall_filtered_avg_ms": round(float(statistics.mean(all_filtered)), 2) if all_filtered else 0.0,
        "overall_filtered_p95_ms": round(_p95(all_filtered), 2) if all_filtered else 0.0,
        "rows": out_rows,
    }
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
