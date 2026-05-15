#!/usr/bin/env python3
"""
download_history.py — Download 10+ years of SEC filings for a single ticker

Usage:
    python tools/download_history.py AAPL
    python tools/download_history.py AAPL --years 15
    python tools/download_history.py AAPL --forms 10-K,DEF_14A

Output:
    outputs/deep/{TICKER}/sources/TICKER_FORM_YYYY-MM-DD.txt
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from sec_client import (
    sec_get,
    html_to_text,
    load_ticker_cik_map,
    get_all_filings,
    sanitize_form_name,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def select_filings(filings, forms_wanted, cutoff_year, ticker):
    """Select which filings to download, respecting priorities from CLAUDE.md."""
    selected = []

    # Group by form type
    by_form = {}
    for f in filings:
        form = f["form"]
        if form not in forms_wanted:
            continue
        try:
            year = int(f["date"][:4])
        except (ValueError, TypeError):
            continue
        if year < cutoff_year:
            continue
        if form not in by_form:
            by_form[form] = []
        by_form[form].append(f)

    # Sort each group by date descending
    for form in by_form:
        by_form[form].sort(key=lambda x: x["date"], reverse=True)

    # Priority 1: ALL annual filings (10-K or 20-F)
    for form in ("10-K", "20-F"):
        if form in by_form:
            selected.extend(by_form[form])

    # Priority 2: Latest 2 proxies
    if "DEF 14A" in by_form:
        selected.extend(by_form["DEF 14A"][:2])

    # Priority 3: Top 12 most recent 8-Ks
    if "8-K" in by_form:
        selected.extend(by_form["8-K"][:12])

    # Priority 4: Recent 10-Qs (last 4)
    if "10-Q" in by_form:
        selected.extend(by_form["10-Q"][:4])

    # Priority 5: Form 4 insider (last 10)
    if "4" in by_form:
        selected.extend(by_form["4"][:10])

    # Priority 6: 6-K for foreign issuers
    if "6-K" in by_form:
        selected.extend(by_form["6-K"][:8])

    # Deduplicate by accession number
    seen = set()
    deduped = []
    for f in selected:
        if f["accession"] not in seen:
            seen.add(f["accession"])
            deduped.append(f)

    return deduped


def main():
    parser = argparse.ArgumentParser(description="Download filing history for one ticker")
    parser.add_argument("ticker", help="Ticker symbol (e.g. AAPL)")
    parser.add_argument("--years", type=int, default=12, help="Years of history (default: 12)")
    parser.add_argument("--forms", help="Comma-separated forms to download")
    args = parser.parse_args()

    ticker = args.ticker.upper()

    if args.forms:
        forms_wanted = set(f.strip() for f in args.forms.split(","))
    else:
        forms_wanted = {"10-K", "10-Q", "8-K", "20-F", "6-K", "DEF 14A", "4"}

    cutoff_year = datetime.now().year - args.years

    print(f"=== Download History: {ticker} ===")
    print(f"  Period: {cutoff_year}–{datetime.now().year}")
    print(f"  Forms: {', '.join(sorted(forms_wanted))}")

    ticker_cik = load_ticker_cik_map()
    cik = ticker_cik.get(ticker) or ticker_cik.get(ticker.replace(".", ""))
    if not cik:
        print(f"ERROR: CIK not found for {ticker}")
        sys.exit(1)

    cik_raw = cik.lstrip("0")
    print(f"  CIK: {cik}")

    company_name, entity_type, sic, tickers_list, all_filings = get_all_filings(cik)
    print(f"  Company: {company_name}")
    print(f"  Total filings on EDGAR: {len(all_filings)}")

    selected = select_filings(all_filings, forms_wanted, cutoff_year, ticker)
    print(f"  Selected for download: {len(selected)}")
    print()

    # Create output directory
    output_dir = REPO_ROOT / "outputs" / "deep" / ticker / "sources"
    output_dir.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    skipped = 0
    errors = []

    for i, filing in enumerate(selected, 1):
        form_safe = sanitize_form_name(filing["form"])
        acc_suffix = filing["accession"][-8:].replace("-", "")
        filename = f"{ticker}_{form_safe}_{filing['date']}_{acc_suffix}.txt"
        filepath = output_dir / filename

        if filepath.exists():
            skipped += 1
            continue

        accession_clean = filing["accession"].replace("-", "")
        primary_doc = filing["primaryDocument"]
        if not primary_doc:
            errors.append(f"{filing['form']} {filing['date']}: no primary document")
            continue

        doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_raw}/{accession_clean}/{primary_doc}"
        print(f"  [{i}/{len(selected)}] {filename}...", end=" ", flush=True)

        try:
            resp = sec_get(doc_url, timeout=60)
            content_type = resp.headers.get("Content-Type", "")
            text = resp.text

            if "html" in content_type.lower() or text.strip().startswith(("<", "<!DOCTYPE")):
                text = html_to_text(text)

            if len(text.strip()) < 100:
                print("SKIP (too short)")
                continue

            with open(filepath, "w", encoding="utf-8") as f:
                f.write(text)

            downloaded += 1
            size_kb = len(text) / 1024
            print(f"OK ({size_kb:.0f} KB)")
        except Exception as e:
            errors.append(f"{filename}: {e}")
            print(f"ERROR: {e}")

    print()
    print(f"=== Done ===")
    print(f"  Downloaded: {downloaded}")
    print(f"  Skipped (already exist): {skipped}")
    print(f"  Output: {output_dir}")
    if errors:
        print(f"  Errors: {len(errors)}")
        for err in errors:
            print(f"    - {err}")


if __name__ == "__main__":
    main()
