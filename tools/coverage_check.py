#!/usr/bin/env python3
"""
coverage_check.py — Check SEC filing coverage for a ticker (10-12 years)

Usage:
    python tools/coverage_check.py AAPL
    python tools/coverage_check.py AAPL --years 15

Output:
    outputs/deep/{TICKER}/coverage.md
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from sec_client import load_ticker_cik_map, get_all_filings

REPO_ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description="Check SEC filing coverage for a ticker")
    parser.add_argument("ticker", help="Ticker symbol (e.g. AAPL)")
    parser.add_argument("--years", type=int, default=12, help="Years of history to check (default: 12)")
    args = parser.parse_args()

    ticker = args.ticker.upper()
    print(f"Checking coverage for {ticker}...")

    ticker_cik = load_ticker_cik_map()
    cik = ticker_cik.get(ticker) or ticker_cik.get(ticker.replace(".", ""))
    if not cik:
        print(f"ERROR: CIK not found for {ticker}")
        sys.exit(1)

    print(f"  CIK: {cik}")
    company_name, entity_type, sic, tickers, filings = get_all_filings(cik)
    print(f"  Company: {company_name}")
    print(f"  Total filings found: {len(filings)}")

    cutoff_year = datetime.now().year - args.years
    is_foreign = entity_type in ("foreign-private-issuer",) or any(
        f["form"] in ("20-F", "6-K") for f in filings[:20]
    )
    primary_annual = "20-F" if is_foreign else "10-K"

    # Count by form and year
    form_year_count = {}
    earliest_annual = None
    for f in filings:
        form = f["form"]
        date_str = f["date"]
        try:
            year = int(date_str[:4])
        except (ValueError, TypeError):
            continue
        if year < cutoff_year:
            continue
        key = form
        if key not in form_year_count:
            form_year_count[key] = {}
        form_year_count[key][year] = form_year_count[key].get(year, 0) + 1

        if form == primary_annual:
            if earliest_annual is None or date_str < earliest_annual:
                earliest_annual = date_str

    # Produce coverage.md
    output_dir = REPO_ROOT / "outputs" / "deep" / ticker
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / "coverage.md"

    current_year = datetime.now().year
    annual_count = sum(form_year_count.get(primary_annual, {}).values())
    proxy_count = sum(form_year_count.get("DEF 14A", {}).values())
    eight_k_count = sum(form_year_count.get("8-K", {}).values())

    lines = [
        f"# Coverage Report: {ticker}",
        f"",
        f"**Company:** {company_name}",
        f"**CIK:** {cik}",
        f"**Entity Type:** {entity_type}",
        f"**SIC:** {sic}",
        f"**Foreign Issuer:** {'Yes' if is_foreign else 'No'}",
        f"**Primary Annual Form:** {primary_annual}",
        f"**Earliest Annual Filing in Range:** {earliest_annual or 'None found'}",
        f"**Analysis Period:** {cutoff_year}–{current_year}",
        f"",
        f"## Filing Counts ({cutoff_year}–{current_year})",
        f"",
        f"| Form | Count |",
        f"|------|-------|",
    ]

    for form in sorted(form_year_count.keys()):
        total = sum(form_year_count[form].values())
        lines.append(f"| {form} | {total} |")

    lines.extend([
        f"",
        f"## Annual Filing ({primary_annual}) by Year",
        f"",
        f"| Year | Count |",
        f"|------|-------|",
    ])

    annual_years = form_year_count.get(primary_annual, {})
    for yr in range(cutoff_year, current_year + 1):
        cnt = annual_years.get(yr, 0)
        flag = " (MISSING)" if cnt == 0 else ""
        lines.append(f"| {yr} | {cnt}{flag} |")

    # Assessment
    lines.extend([
        f"",
        f"## Assessment",
        f"",
    ])

    if annual_count >= 8:
        lines.append(f"Coverage is **good** ({annual_count} annual filings in range).")
    elif annual_count >= 5:
        lines.append(f"Coverage is **moderate** ({annual_count} annual filings). Some years missing.")
    else:
        lines.append(f"Coverage is **weak** ({annual_count} annual filings). Deep comparison will have gaps.")

    if proxy_count > 0:
        lines.append(f"Proxy statements (DEF 14A): {proxy_count} available.")
    else:
        lines.append(f"No proxy statements (DEF 14A) found in range.")

    lines.append(f"8-K filings: {eight_k_count} available.")

    # Notes
    notes = []
    if is_foreign:
        notes.append("Foreign private issuer — uses 20-F/6-K instead of 10-K/10-Q.")
    missing_years = [yr for yr in range(cutoff_year, current_year + 1) if annual_years.get(yr, 0) == 0]
    if missing_years:
        notes.append(f"Missing annual filings for: {', '.join(str(y) for y in missing_years)}")

    if notes:
        lines.extend(["", "## Notes", ""])
        for note in notes:
            lines.append(f"- {note}")

    report = "\n".join(lines) + "\n"

    with open(output_file, "w") as f:
        f.write(report)

    print(f"\nCoverage report written to: {output_file}")
    print(f"  Annual filings ({primary_annual}): {annual_count}")
    print(f"  Proxy (DEF 14A): {proxy_count}")
    print(f"  8-K filings: {eight_k_count}")


if __name__ == "__main__":
    main()
