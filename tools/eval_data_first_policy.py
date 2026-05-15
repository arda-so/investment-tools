#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.ai_data_policy import enforce_data_first_policy, DATA_UNAVAILABLE_MSG


def main() -> int:
    cases = [
        {
            "name": "sec_quant_without_structured",
            "prompt": "Read this 10-K and extract exact revenue breakdown by geography and margin trends.",
            "context": "You are analyst.",
            "json_mode": False,
            "expect_block": True,
        },
        {
            "name": "sec_quant_with_structured",
            "prompt": "Analyze margin trends for this 10-Q.",
            "context": "STRUCTURED_FINANCIAL_DATA_JSON: {\"revenue_segments\":{}}",
            "json_mode": False,
            "expect_block": False,
        },
        {
            "name": "non_quant_sec_qual_only",
            "prompt": "Summarize management tone from Item 1A risk factors in latest 10-K.",
            "context": "qualitative only",
            "json_mode": False,
            "expect_block": False,
        },
    ]
    fails = []
    for c in cases:
        blocked, out = enforce_data_first_policy(c["prompt"], c["context"], json_mode=bool(c["json_mode"]))
        if bool(blocked) != bool(c["expect_block"]):
            fails.append(f"{c['name']}: expected blocked={c['expect_block']} got {blocked}")
            continue
        if blocked and DATA_UNAVAILABLE_MSG not in str(out):
            fails.append(f"{c['name']}: missing fallback message")
    if fails:
        print("data-first policy eval failed:")
        for f in fails:
            print("-", f)
        return 1
    print("data-first policy eval passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
