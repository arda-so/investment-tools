#!/usr/bin/env python3
"""Rank red-flag blocks by severity score and print top alerts."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


SEVERE_WEIGHTS = {
    "going concern": 12,
    "material weakness": 11,
    "restatement": 10,
    "adverse opinion": 10,
    "auditor resignation": 10,
    "disagreement with auditor": 10,
    "sec investigation": 9,
    "doj": 9,
    "wells notice": 9,
    "grand jury": 9,
    "subpoena": 8,
    "class action": 8,
    "consent decree": 8,
    "cease and desist": 8,
    "cross-default": 8,
    "acceleration of debt": 8,
    "forbearance": 8,
    "covenant waiver": 8,
    "insufficient capital": 8,
    "default": 7,
    "goodwill impairment": 7,
    "inventory write-down": 7,
    "impairment": 6,
    "internal investigation": 6,
    "whistleblower": 6,
    "cfo resignation": 6,
    "ineffective disclosure controls": 6,
    "unregistered securities": 6,
    "related party transaction": 5,
    "late filing": 5,
    "debt covenant": 5,
    "liquidity constraint": 5,
    "drawdown": 4,
    "restructuring": 4,
    "severance costs": 4,
    "loss of significant customer": 4,
    "customer concentration": 3,
    "backlog decline": 3,
}

FORM_BASE = {"8-K": 4, "10-Q": 3, "10-K": 2}


def split_blocks(text: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    cur: list[str] = []
    for ln in text.splitlines():
        if ln.lstrip().startswith("[~]"):
            if cur:
                blocks.append(cur)
            cur = [ln]
        elif cur:
            cur.append(ln)
    if cur:
        blocks.append(cur)
    return blocks


def parse_header(line: str) -> tuple[str, str, str]:
    # Expected shape: [~] TICKER | FORM YYYY-MM-DD | SECTION
    clean = line.strip()
    clean = re.sub(r"^\[\~\]\s*", "", clean)
    parts = [p.strip() for p in clean.split("|")]
    ticker = parts[0] if len(parts) > 0 else "UNKNOWN"
    form_date = parts[1] if len(parts) > 1 else "-"
    section = parts[2] if len(parts) > 2 else "-"
    return ticker, form_date, section


def score_block(block: list[str]) -> tuple[int, list[str], str, str, str]:
    ticker, form_date, section = parse_header(block[0])
    txt = "\n".join(block).lower()

    drivers = []
    score = 0
    # phrase hits
    for k, w in sorted(SEVERE_WEIGHTS.items(), key=lambda x: -len(x[0])):
        if k in txt:
            score += w
            drivers.append(k)

    # form base score
    form = form_date.split()[0] if form_date and form_date != "-" else ""
    score += FORM_BASE.get(form, 1)

    # section heuristic
    sec = section.lower()
    if "debt" in sec or "liquidity" in sec:
        score += 2
    if "material events" in sec:
        score += 2

    # dedupe drivers but keep order
    seen = set()
    uniq = []
    for d in drivers:
        if d in seen:
            continue
        seen.add(d)
        uniq.append(d)

    return score, uniq, ticker, form_date, section


def bucket(score: int) -> str:
    if score >= 20:
        return "HIGH"
    if score >= 10:
        return "MED"
    return "LOW"


def main() -> None:
    p = argparse.ArgumentParser(description="Rank red-flag blocks by severity.")
    p.add_argument("--file", required=True)
    p.add_argument("--limit", type=int, default=5)
    args = p.parse_args()

    path = Path(args.file)
    if not path.exists() or not path.read_text(encoding="utf-8", errors="ignore").strip():
        return

    blocks = split_blocks(path.read_text(encoding="utf-8", errors="ignore"))
    ranked = []
    for b in blocks:
        ranked.append(score_block(b))
    ranked.sort(key=lambda x: x[0], reverse=True)

    for score, drivers, ticker, form_date, section in ranked[: args.limit]:
        b = bucket(score)
        if drivers:
            why = ", ".join(drivers[:3])
        else:
            why = "filing delta"
        print(f"- {b}({score}) {ticker} | {form_date} | {section} | drivers: {why}")


if __name__ == "__main__":
    main()
