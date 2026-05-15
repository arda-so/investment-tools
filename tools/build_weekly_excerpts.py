#!/usr/bin/env python3
"""
build_weekly_excerpts.py — Batch-extract excerpts from weekly filings

Reads all filings in filing_docs/ for the current week,
extracts key sections, and writes one JSONL file for the
weekly scanner to use in PASS 1 scoring.

Usage:
    python tools/build_weekly_excerpts.py              # Last 7 days
    python tools/build_weekly_excerpts.py --days 14    # Last 14 days

Output:
    outputs/weekly/excerpts.jsonl
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FILING_DIR = REPO_ROOT / "filing_docs"
OUTPUT_DIR = REPO_ROOT / "outputs" / "weekly"

MAX_EXCERPT_CHARS = 8000

# Filename pattern: TICKER_FORM_YYYY-MM-DD_ACCESSION.txt (or without accession suffix)
FILENAME_RE = re.compile(r"^([A-Z0-9.]+)_(.+?)_(\d{4}-\d{2}-\d{2})(?:_[0-9A-Za-z-]+)?\.txt$")

# Section anchors for different form types
SECTION_ANCHORS_10K_10Q = [
    r"(?i)item\s+1a[\.\s\-:]+risk\s+factors",
    r"(?i)item\s+7[\.\s\-:]+management.s\s+discussion",
    r"(?i)item\s+2[\.\s\-:]+management.s\s+discussion",  # 10-Q uses Item 2
    r"(?i)forward.looking\s+statements",
]

SECTION_ANCHORS_8K = [
    r"(?i)item\s+2\.02",   # Results of operations
    r"(?i)item\s+1\.01",   # Entry into material agreement
    r"(?i)item\s+7\.01",   # Regulation FD
    r"(?i)item\s+8\.01",   # Other events
    r"(?i)forward.looking\s+statements",
    r"(?i)guidance",
    r"(?i)outlook",
]

KEYWORD_PATTERNS = [
    r"(?i)guidance",
    r"(?i)acqui(?:sition|red|ring)",
    r"(?i)merger",
    r"(?i)impairment",
    r"(?i)restructur",
    r"(?i)material\s+weakness",
    r"(?i)subpoena",
    r"(?i)investigation",
    r"(?i)going\s+concern",
    r"(?i)liquidity",
    r"(?i)covenant",
    r"(?i)department\s+of\s+justice|DOJ|SEC\s+enforcement",
]


def parse_filename(filename):
    """Parse TICKER_FORM_DATE.txt -> (ticker, form, date_str) or None."""
    m = FILENAME_RE.match(filename)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


def extract_around_anchor(text, pattern, chars_before=1000, chars_after=7000):
    """Find pattern in text and extract surrounding context."""
    match = re.search(pattern, text)
    if not match:
        return None
    start = max(0, match.start() - chars_before)
    end = min(len(text), match.end() + chars_after)
    return text[start:end]


def extract_excerpt(text, form):
    """Extract the most relevant excerpt from a filing based on form type."""
    if form in ("10-K", "10-Q", "20-F"):
        anchors = SECTION_ANCHORS_10K_10Q
    elif form.startswith("8-K"):
        anchors = SECTION_ANCHORS_8K
    else:
        anchors = KEYWORD_PATTERNS

    # Try each anchor, take the first hit
    for anchor in anchors:
        excerpt = extract_around_anchor(text, anchor)
        if excerpt and len(excerpt) > 200:
            return excerpt[:MAX_EXCERPT_CHARS]

    # Fallback: take the first 8000 chars after skipping the header
    # (skip first 500 chars which is usually boilerplate)
    start = min(500, len(text))
    return text[start:start + MAX_EXCERPT_CHARS]


def detect_keywords(text):
    """Return list of keywords found in text."""
    found = []
    for pat in KEYWORD_PATTERNS:
        if re.search(pat, text):
            # Extract the keyword name from the pattern
            label = re.sub(r"\(\?i\)", "", pat)
            label = re.sub(r"[\\().+?|]", "", label)
            label = label.strip().upper().replace("  ", " ")[:30]
            found.append(label)
    return found


def main():
    parser = argparse.ArgumentParser(description="Build weekly excerpts from filing_docs/")
    parser.add_argument("--days", type=int, default=7, help="Look back N days (default: 7)")
    args = parser.parse_args()

    cutoff = (datetime.now() - timedelta(days=args.days)).date()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if not FILING_DIR.exists():
        print(f"ERROR: {FILING_DIR} does not exist. Run securities_monitor.py first.")
        sys.exit(1)

    files = sorted(FILING_DIR.glob("*.txt"))
    print(f"Found {len(files)} filing files in {FILING_DIR}")

    records = []
    for filepath in files:
        parsed = parse_filename(filepath.name)
        if not parsed:
            continue

        ticker, form, date_str = parsed
        try:
            filing_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            # Fallback to file modification time
            mtime = os.path.getmtime(filepath)
            filing_date = datetime.fromtimestamp(mtime).date()

        if filing_date < cutoff:
            continue

        text = filepath.read_text(encoding="utf-8", errors="replace")
        excerpt = extract_excerpt(text, form)
        keywords = detect_keywords(excerpt)

        record = {
            "ticker": ticker,
            "form": form,
            "date": date_str,
            "path": str(filepath.relative_to(REPO_ROOT)),
            "excerpt": excerpt,
            "keywords": keywords,
            "chars": len(text),
        }
        records.append(record)

    # Write JSONL
    output_file = OUTPUT_DIR / "excerpts.jsonl"
    with open(output_file, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"Wrote {len(records)} excerpts to {output_file}")

    # Quick summary
    if records:
        print(f"\nSummary:")
        forms_count = {}
        for r in records:
            forms_count[r["form"]] = forms_count.get(r["form"], 0) + 1
        for form, count in sorted(forms_count.items(), key=lambda x: -x[1]):
            print(f"  {form}: {count}")


if __name__ == "__main__":
    main()
