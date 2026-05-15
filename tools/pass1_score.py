#!/usr/bin/env python3
"""PASS 1: Score and rank weekly filings from excerpts.jsonl"""

import csv
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
excerpts_file = REPO / "outputs/weekly/excerpts.jsonl"
out_dir = REPO / "outputs/weekly"

FORM_WEIGHT = {"8-K": 5, "10-Q": 4, "10-K": 4, "20-F": 4, "6-K": 3, "DEF_14A": 3, "4": 2}
KEYWORD_BOOSTS = {
    "guidance": 3, "acquisition": 3, "merger": 3, "impairment": 3,
    "restructuring": 3, "restructur": 3, "material weakness": 3,
    "subpoena": 3, "doj": 3, "sec enforcement": 3,
    "liquidity": 2, "covenant": 2, "investigation": 2, "going concern": 2,
    "buyback": 2, "repurchase": 2, "layoff": 2, "workforce reduction": 2,
    "goodwill": 2, "write-down": 2, "writedown": 2,
}

records = []
with open(excerpts_file) as f:
    for line in f:
        records.append(json.loads(line))

scored = []
for rec in records:
    form = rec["form"]
    score = FORM_WEIGHT.get(form, 1)
    text_lower = rec.get("excerpt", "").lower()
    keywords_found = []
    for kw, boost in KEYWORD_BOOSTS.items():
        if kw in text_lower:
            score += boost
            keywords_found.append(kw)
    for kw in rec.get("keywords", []):
        score += 1
    date = rec.get("date", "")
    if date >= "2026-02-06":
        score += 3
    elif date >= "2026-02-05":
        score += 2
    elif date >= "2026-02-04":
        score += 1
    scored.append({
        "ticker": rec["ticker"],
        "form": rec["form"],
        "date": rec["date"],
        "path": rec["path"],
        "score": score,
        "keywords": keywords_found,
        "chars": rec.get("chars", 0),
    })

scored.sort(key=lambda x: -x["score"])

with open(out_dir / "filings_index.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["ticker", "form", "date", "path", "score", "keywords"])
    w.writeheader()
    for s in scored:
        row = dict(s)
        row["keywords"] = "|".join(row["keywords"])
        del row["chars"]
        w.writerow(row)

non_form4 = [s for s in scored if s["form"] != "4"]
top = non_form4[:12]

with open(out_dir / "top_filings.txt", "w") as f:
    f.write("# Top Filings for Week — Ranked by Signal Score\n")
    f.write("# Generated: 2026-02-07\n\n")
    for i, t in enumerate(top, 1):
        kw = "|".join(t["keywords"]) or "none"
        f.write(f'{i:2d}. [{t["score"]:3d}] {t["ticker"]:6s} {t["form"]:8s} {t["date"]}  keywords: {kw}\n')
        f.write(f'     {t["path"]}\n')

print("=== PASS 1 Complete ===")
print(f"Total filings scored: {len(scored)}")
print(f"Non-Form4 filings: {len(non_form4)}")
print()
print("Top 12 for PASS 2 deep read:")
for i, t in enumerate(top, 1):
    kw = "|".join(t["keywords"]) or "none"
    print(f'  {i:2d}. [{t["score"]:3d}] {t["ticker"]:6s} {t["form"]:8s} {t["date"]}  kw: {kw}')
