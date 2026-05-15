#!/usr/bin/env python3
"""
securities_monitor.py — Download recent SEC filings for S&P 100

Usage:
    python securities_monitor.py                     # Last 7 days, full S&P 100
    python securities_monitor.py --days 14           # Last 14 days
    python securities_monitor.py --ticker AAPL       # Single ticker
    python securities_monitor.py --ticker AAPL,MSFT  # Multiple tickers
    python securities_monitor.py --forms 10-K,8-K    # Only specific forms

Output:
    filing_docs/TICKER_FORM_YYYY-MM-DD.txt
"""

import argparse
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Shared SEC utilities
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from sec_client import (
    sec_get,
    html_to_text,
    load_ticker_cik_map,
    download_filing_text,
    sanitize_form_name,
    DEFAULT_FORMS,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FILING_DIR = REPO_ROOT / "filing_docs"
DATA_DIR = REPO_ROOT / "data"
UNIVERSE_FILE = DATA_DIR / "sp100_universe.txt"

# ---------------------------------------------------------------------------
# Recent filings (date-filtered, form-filtered)
# ---------------------------------------------------------------------------

def get_recent_filings(cik, forms, cutoff_date):
    """Get filings for a CIK since cutoff_date. Returns list of dicts."""
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    resp = sec_get(url)
    data = resp.json()

    recent = data.get("filings", {}).get("recent", data.get("recent", {}))
    if not recent:
        return []

    results = []
    acc_numbers = recent.get("accessionNumber", [])
    forms_list = recent.get("form", [])
    dates = recent.get("filingDate", [])
    primary_docs = recent.get("primaryDocument", [])

    for i in range(len(acc_numbers)):
        form = forms_list[i] if i < len(forms_list) else ""
        date_str = dates[i] if i < len(dates) else ""
        primary_doc = primary_docs[i] if i < len(primary_docs) else ""

        if form not in forms:
            continue

        try:
            filing_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue

        if filing_date < cutoff_date:
            continue

        accession = acc_numbers[i].replace("-", "")
        cik_trimmed = cik.lstrip("0")
        doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_trimmed}/{accession}/{primary_doc}"

        results.append({
            "accession": acc_numbers[i],
            "form": form,
            "date": date_str,
            "primary_doc": primary_doc,
            "doc_url": doc_url,
            "cik_trimmed": cik_trimmed,
            "accession_flat": accession,
        })

    return results


def get_filing_exhibits(cik_trimmed, accession_flat):
    """Fetch the filing index and return exhibit documents.

    Returns list of dicts: [{name, type, url}, ...]
    Only returns EX-99.x exhibits (press releases, supplements).
    """
    index_url = (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik_trimmed}/{accession_flat}/index.json"
    )
    try:
        resp = sec_get(index_url)
        data = resp.json()
    except Exception:
        return []

    exhibits = []
    items = data.get("directory", {}).get("item", [])
    for item in items:
        name = item.get("name", "")
        doc_type = item.get("type", "")
        if re.match(r"EX-99\.\d+", doc_type, re.IGNORECASE):
            exhibit_url = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{cik_trimmed}/{accession_flat}/{name}"
            )
            exhibits.append({
                "name": name,
                "type": doc_type,
                "url": exhibit_url,
            })
    return exhibits


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

def load_universe(ticker_filter=None):
    """Load list of tickers to scan."""
    if ticker_filter:
        return [t.strip().upper() for t in ticker_filter.split(",")]

    if not UNIVERSE_FILE.exists():
        print(f"ERROR: Universe file not found: {UNIVERSE_FILE}")
        sys.exit(1)

    tickers = []
    with open(UNIVERSE_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                tickers.append(line.upper())
    return tickers


# ---------------------------------------------------------------------------
# Main download logic
# ---------------------------------------------------------------------------

def run_download(tickers, forms, days, dry_run=False, with_exhibits=True):
    """Download recent filings for given tickers."""
    FILING_DIR.mkdir(exist_ok=True)
    cutoff = (datetime.now() - timedelta(days=days)).date()

    print(f"=== Securities Monitor ===")
    print(f"  Tickers:  {len(tickers)}")
    print(f"  Forms:    {', '.join(sorted(forms))}")
    print(f"  Since:    {cutoff}")
    print(f"  Output:   {FILING_DIR}")
    print(f"  Exhibits: {'ON (8-K press releases)' if with_exhibits else 'OFF'}")
    print()

    ticker_cik = load_ticker_cik_map()

    total_downloaded = 0
    total_exhibits = 0
    total_skipped = 0
    errors = []

    for idx, ticker in enumerate(tickers, 1):
        cik = ticker_cik.get(ticker)
        if not cik:
            alt = ticker.replace(".", "")
            cik = ticker_cik.get(alt)
        if not cik:
            errors.append(f"{ticker}: CIK not found")
            continue

        print(f"[{idx}/{len(tickers)}] {ticker} (CIK {cik})...", end=" ", flush=True)

        try:
            filings = get_recent_filings(cik, forms, cutoff)
        except Exception as e:
            errors.append(f"{ticker}: {e}")
            print(f"ERROR: {e}")
            continue

        if not filings:
            print("no new filings")
            continue

        print(f"{len(filings)} filing(s)")

        for filing in filings:
            form_safe = sanitize_form_name(filing["form"])
            acc_suffix = filing["accession"][-8:]
            filename = f"{ticker}_{form_safe}_{filing['date']}_{acc_suffix}.txt"
            filepath = FILING_DIR / filename

            if filepath.exists():
                total_skipped += 1
            elif dry_run:
                print(f"    [DRY RUN] Would download: {filename}")
            else:
                try:
                    text = download_filing_text(filing["doc_url"])
                    if len(text.strip()) < 100:
                        print(f"    SKIP (too short): {filename}")
                        continue

                    with open(filepath, "w", encoding="utf-8") as f:
                        f.write(text)

                    total_downloaded += 1
                    print(f"    OK: {filename} ({len(text):,} chars)")
                except Exception as e:
                    errors.append(f"{ticker}/{filing['form']}: {e}")
                    print(f"    ERROR: {filename}: {e}")
                    continue

            # Download exhibits for 8-K filings
            if with_exhibits and filing["form"] == "8-K":
                try:
                    exhibits = get_filing_exhibits(
                        filing["cik_trimmed"], filing["accession_flat"]
                    )
                    for ex in exhibits:
                        ex_tag = ex["type"].replace("-", "").replace(".", "")
                        ex_filename = (
                            f"{ticker}_{form_safe}_{filing['date']}"
                            f"_{acc_suffix}_{ex_tag}.txt"
                        )
                        ex_filepath = FILING_DIR / ex_filename

                        if ex_filepath.exists():
                            total_skipped += 1
                            continue

                        if dry_run:
                            print(f"    [DRY RUN] Would download exhibit: {ex_filename}")
                            continue

                        try:
                            text = download_filing_text(ex["url"])
                            if len(text.strip()) < 100:
                                continue

                            with open(ex_filepath, "w", encoding="utf-8") as f:
                                f.write(text)

                            total_exhibits += 1
                            print(f"    OK: {ex_filename} ({len(text):,} chars)")
                        except Exception as e:
                            errors.append(f"{ticker}/{ex['type']}: {e}")
                            print(f"    ERROR: {ex_filename}: {e}")
                except Exception:
                    pass

    print()
    print(f"=== Done ===")
    print(f"  Downloaded: {total_downloaded} filings + {total_exhibits} exhibits")
    print(f"  Skipped (already exist): {total_skipped}")
    if errors:
        print(f"  Errors: {len(errors)}")
        for err in errors:
            print(f"    - {err}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Download recent SEC filings for S&P 100 universe"
    )
    parser.add_argument(
        "--ticker", "-t",
        help="Comma-separated tickers (default: full S&P 100)"
    )
    parser.add_argument(
        "--days", "-d",
        type=int, default=7,
        help="Look back N days (default: 7)"
    )
    parser.add_argument(
        "--forms", "-f",
        help="Comma-separated form types (default: 10-K,10-Q,8-K,20-F,6-K,DEF 14A,4)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be downloaded without downloading"
    )
    parser.add_argument(
        "--no-exhibits",
        action="store_true",
        help="Skip downloading Exhibit 99.x press releases from 8-K filings"
    )
    args = parser.parse_args()

    tickers = load_universe(args.ticker)

    if args.forms:
        forms = set(f.strip() for f in args.forms.split(","))
    else:
        forms = DEFAULT_FORMS

    run_download(
        tickers, forms, args.days,
        dry_run=args.dry_run,
        with_exhibits=not args.no_exhibits,
    )


if __name__ == "__main__":
    main()
