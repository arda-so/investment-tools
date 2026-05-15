#!/usr/bin/env python3
"""
extract_deep_excerpts.py — Extract key sections from downloaded filings for deep comparison

Reads files from outputs/deep/{TICKER}/sources/ and produces
compact excerpts in outputs/deep/{TICKER}/extracts/excerpts.jsonl

Usage:
    python tools/extract_deep_excerpts.py AAPL

Output:
    outputs/deep/{TICKER}/extracts/excerpts.jsonl
"""

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

MAX_EXCERPT_CHARS = 10000

# Filename pattern: TICKER_FORM_YYYY-MM-DD_ACCESSION.txt (or without accession suffix)
FILENAME_RE = re.compile(r"^([A-Z0-9.]+)_(.+?)_(\d{4}-\d{2}-\d{2})(?:_[0-9A-Za-z-]+)?\.txt$")

# Section extraction patterns with labels
SECTION_PATTERNS = [
    {
        "label": "Business Overview (Item 1)",
        "patterns": [
            r"(?i)item\s+1[\.\s\-:]+business\b",
            r"(?i)item\s+1[\.\s\-:]+general\b",
            r"(?i)overview\s+of\s+(?:the\s+)?(?:company|business)",
        ],
        "forms": {"10-K", "20-F"},
        "chars_after": 8000,
    },
    {
        "label": "Risk Factors (Item 1A)",
        "patterns": [
            r"(?i)item\s+1a[\.\s\-:]+risk\s+factors",
            r"(?i)risk\s+factors",
        ],
        "forms": {"10-K", "10-Q", "20-F"},
        "chars_after": 8000,
    },
    {
        "label": "MD&A (Item 7)",
        "patterns": [
            r"(?i)item\s+7[\.\s\-:]+management.s\s+discussion",
            r"(?i)item\s+2[\.\s\-:]+management.s\s+discussion",  # 10-Q
            r"(?i)management.s\s+discussion\s+and\s+analysis",
        ],
        "forms": {"10-K", "10-Q", "20-F"},
        "chars_after": 8000,
    },
    {
        "label": "Segments",
        "patterns": [
            r"(?i)segment\s+(?:information|reporting|results|data)",
            r"(?i)(?:reportable|operating)\s+segments",
            r"(?i)information\s+about\s+segments",
        ],
        "forms": {"10-K", "10-Q", "20-F"},
        "chars_after": 6000,
    },
    {
        "label": "Share Count / Equity / Buybacks",
        "patterns": [
            r"(?i)(?:share|stock)\s+repurchase",
            r"(?i)(?:treasury\s+stock|common\s+(?:stock|shares)\s+outstanding)",
            r"(?i)buyback",
        ],
        "forms": {"10-K", "10-Q", "20-F", "8-K"},
        "chars_after": 4000,
    },
    {
        "label": "Stock-Based Compensation",
        "patterns": [
            r"(?i)stock.based\s+compensation",
            r"(?i)share.based\s+(?:compensation|payment)",
            r"(?i)equity\s+compensation",
        ],
        "forms": {"10-K", "10-Q", "20-F"},
        "chars_after": 4000,
    },
    {
        "label": "Debt / Liquidity / Covenants",
        "patterns": [
            r"(?i)(?:long.term|short.term)\s+debt",
            r"(?i)credit\s+(?:facility|agreement|line)",
            r"(?i)liquidity\s+and\s+capital",
            r"(?i)debt\s+covenant",
            r"(?i)borrowing",
        ],
        "forms": {"10-K", "10-Q", "20-F", "8-K"},
        "chars_after": 5000,
    },
    {
        "label": "Revenue Recognition",
        "patterns": [
            r"(?i)revenue\s+recognition",
            r"(?i)(?:new\s+)?accounting\s+(?:standard|pronouncement)",
        ],
        "forms": {"10-K", "10-Q", "20-F"},
        "chars_after": 4000,
    },
    {
        "label": "Proxy — Compensation",
        "patterns": [
            r"(?i)(?:executive|named)\s+(?:officer\s+)?compensation",
            r"(?i)compensation\s+discussion\s+and\s+analysis",
            r"(?i)summary\s+compensation\s+table",
            r"(?i)performance.based\s+(?:award|unit|stock\s+unit|RSU|PSU)",
        ],
        "forms": {"DEF_14A"},
        "chars_after": 6000,
    },
    {
        "label": "Proxy — Governance",
        "patterns": [
            r"(?i)corporate\s+governance",
            r"(?i)related.party\s+transaction",
            r"(?i)related\s+person\s+transaction",
        ],
        "forms": {"DEF_14A"},
        "chars_after": 4000,
    },
    {
        "label": "8-K Material Events",
        "patterns": [
            r"(?i)item\s+[12578]\.\d{2}",
            r"(?i)forward.looking\s+statement",
            r"(?i)guidance",
            r"(?i)outlook",
        ],
        "forms": {"8-K"},
        "chars_after": 6000,
    },
]


def parse_filename(filename):
    m = FILENAME_RE.match(filename)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


def extract_section(text, pattern, chars_before=500, chars_after=8000):
    match = re.search(pattern, text)
    if not match:
        return None
    start = max(0, match.start() - chars_before)
    end = min(len(text), match.end() + chars_after)
    return text[start:end]


def process_filing(filepath, ticker, form, date_str):
    """Extract all relevant sections from one filing."""
    text = filepath.read_text(encoding="utf-8", errors="replace")
    if len(text) < 200:
        return []

    year = date_str[:4]
    records = []

    for section_def in SECTION_PATTERNS:
        # Check if this section applies to this form type
        if form not in section_def["forms"]:
            continue

        chars_after = section_def.get("chars_after", 8000)

        for pattern in section_def["patterns"]:
            excerpt = extract_section(text, pattern, chars_before=300, chars_after=chars_after)
            if excerpt and len(excerpt) > 100:
                records.append({
                    "year": year,
                    "form": form,
                    "date": date_str,
                    "section": section_def["label"],
                    "excerpt": excerpt[:MAX_EXCERPT_CHARS],
                    "source_path": str(filepath.relative_to(REPO_ROOT)),
                })
                break  # Only take first match per section definition

    # If no sections matched, take a generic excerpt from the middle
    if not records and len(text) > 1000:
        mid = len(text) // 4  # Start at 25% to skip headers
        records.append({
            "year": year,
            "form": form,
            "date": date_str,
            "section": "General (no section match)",
            "excerpt": text[mid:mid + MAX_EXCERPT_CHARS],
            "source_path": str(filepath.relative_to(REPO_ROOT)),
        })

    return records


def main():
    parser = argparse.ArgumentParser(description="Extract deep excerpts for a ticker")
    parser.add_argument("ticker", help="Ticker symbol (e.g. AAPL)")
    args = parser.parse_args()

    ticker = args.ticker.upper()
    sources_dir = REPO_ROOT / "outputs" / "deep" / ticker / "sources"

    if not sources_dir.exists() or not list(sources_dir.glob("*.txt")):
        print(f"ERROR: No source filings found in {sources_dir}")
        print(f"Run: python tools/download_history.py {ticker}")
        sys.exit(1)

    # Create output directory
    extracts_dir = REPO_ROOT / "outputs" / "deep" / ticker / "extracts"
    extracts_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(sources_dir.glob("*.txt"))
    print(f"=== Extract Deep Excerpts: {ticker} ===")
    print(f"  Source files: {len(files)}")

    all_records = []
    for filepath in files:
        parsed = parse_filename(filepath.name)
        if not parsed:
            print(f"  SKIP (bad filename): {filepath.name}")
            continue

        file_ticker, form, date_str = parsed
        print(f"  Processing: {filepath.name}...", end=" ", flush=True)

        records = process_filing(filepath, ticker, form, date_str)
        all_records.extend(records)
        print(f"{len(records)} section(s)")

    # Sort by date then section
    all_records.sort(key=lambda r: (r["date"], r["section"]))

    # Write JSONL
    output_file = extracts_dir / "excerpts.jsonl"
    with open(output_file, "w", encoding="utf-8") as f:
        for rec in all_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"\n=== Done ===")
    print(f"  Total excerpts: {len(all_records)}")
    print(f"  Output: {output_file}")

    # Summary by section
    section_counts = {}
    for r in all_records:
        section_counts[r["section"]] = section_counts.get(r["section"], 0) + 1
    print(f"\n  Sections extracted:")
    for section, count in sorted(section_counts.items(), key=lambda x: -x[1]):
        print(f"    {section}: {count}")


if __name__ == "__main__":
    main()
