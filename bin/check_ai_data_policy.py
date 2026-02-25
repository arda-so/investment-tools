#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SEARCH_DIRS = [REPO_ROOT / "app", REPO_ROOT / "tools"]


FORBIDDEN_PATTERNS = [
    r"exact revenue breakdown by product and geography",
    r"Do NOT summarize text\.\s*Answer the 4 required questions",
    r"Buffett-style filing analyst",
]

SEC_HINTS = [
    "10-k",
    "10-q",
    "8-k",
    "sec filing",
    "item 1a",
    "item 7",
]

QUANT_HINTS = [
    "revenue",
    "margin",
    "debt",
    "cash flow",
    "eps",
    "fcf",
    "segment",
    "geography",
    "buyback",
    "equity",
    "breakdown",
]

STRUCTURED_MARKERS = [
    "structured_financial_data_json",
    "mini_statements",
    "revenue_segments",
    "buyback",
    "provided with 100% accurate quantitative data in json",
    "data not available in structured filings",
]


def _contains_any(text: str, needles: list[str]) -> bool:
    low = str(text or "").lower()
    return any(n in low for n in needles)


def _iter_prompt_blocks(lines: list[str]) -> list[tuple[int, str]]:
    blocks: list[tuple[int, str]] = []
    n = len(lines)
    lhs_prompt_re = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*prompt[A-Za-z0-9_]*\s*=")
    for i, line in enumerate(lines):
        if lhs_prompt_re.search(line):
            start = i
            end = min(n, i + 140)
            chunk = "\n".join(lines[start:end])
            blocks.append((start + 1, chunk))
    return blocks


def main() -> int:
    violations: list[str] = []
    compiled = [re.compile(pat, re.I) for pat in FORBIDDEN_PATTERNS]
    for root in SEARCH_DIRS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(REPO_ROOT)
            if str(rel) in {"app/core/ai_data_policy.py", "bin/check_ai_data_policy.py"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            lines = text.splitlines()
            for i, line in enumerate(lines, start=1):
                for cp in compiled:
                    if cp.search(line):
                        violations.append(f"{rel}:{i}: legacy SEC extraction prompt phrase: {line.strip()[:200]}")
                        break
            # Semantic gate: SEC + quant prompts must include structured data marker/fallback.
            for line_no, chunk in _iter_prompt_blocks(lines):
                if not (_contains_any(chunk, SEC_HINTS) and _contains_any(chunk, QUANT_HINTS)):
                    continue
                if _contains_any(chunk, STRUCTURED_MARKERS):
                    continue
                violations.append(
                    f"{rel}:{line_no}: SEC quantitative prompt missing structured-data marker/fallback."
                )
    if violations:
        print("AI data-policy check failed:")
        for v in violations:
            print(f"- {v}")
        return 1
    print("AI data-policy check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
