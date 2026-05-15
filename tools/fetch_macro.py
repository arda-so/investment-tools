#!/usr/bin/env python3
"""
fetch_macro.py — Pull market macro data from OFFICIAL sources only.

Sources used:
  1. FRED (Federal Reserve Economic Data) — free API, key required
     https://fred.stlouisfed.org/docs/api/api_key.html
  2. Treasury.gov — Daily Treasury Par Yield Curve (no key needed)

FRED series used:
  DGS10         10-Year Treasury Constant Maturity Rate
  DGS2          2-Year Treasury Constant Maturity Rate
  DFII10        10-Year Real Yield (TIPS)
  T10YIE        10-Year Breakeven Inflation Rate
  BAMLH0A0HYM2  ICE BofA US High Yield OAS
  BAMLC0A0CM    ICE BofA US Corporate (IG) OAS
  VIXCLS        CBOE VIX Close
  DTWEXBGS      Trade-Weighted USD Index (broad)
  DCOILWTICO    WTI Crude Oil
  DCOILBRENTEU  Brent Crude Oil

Usage:
  export FRED_API_KEY="your_key_here"   # get free at fred.stlouisfed.org
  python3 tools/fetch_macro.py [--weeks 1] [--end-date 2026-02-07]

Output:
  outputs/weekly/macro_data.json   — raw JSON for programmatic use
  Prints a formatted table to stdout
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
import requests

# ── FRED series we pull ──────────────────────────────────────────────────────
FRED_SERIES = {
    "DGS10":          "10Y Treasury yield",
    "DGS2":           "2Y Treasury yield",
    "DFII10":         "10Y real yield (TIPS)",
    "T10YIE":         "10Y breakeven inflation",
    "BAMLH0A0HYM2":   "HY OAS (ICE BofA)",
    "BAMLC0A0CM":     "IG OAS (ICE BofA)",
    "VIXCLS":         "VIX",
    "DTWEXBGS":       "USD index (trade-weighted)",
    "DCOILWTICO":     "WTI crude oil",
    "DCOILBRENTEU":   "Brent crude oil",
}

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "outputs" / "weekly"


def get_fred_key():
    key = os.environ.get("FRED_API_KEY", "")
    if not key:
        print("ERROR: Set FRED_API_KEY environment variable.", file=sys.stderr)
        print("  Get a free key at: https://fred.stlouisfed.org/docs/api/api_key.html", file=sys.stderr)
        sys.exit(1)
    return key


def fetch_fred_series(series_id, api_key, start_date, end_date):
    """Fetch one FRED series. Returns list of {date, value} dicts."""
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start_date,
        "observation_end": end_date,
        "sort_order": "desc",
    }
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    observations = []
    for obs in data.get("observations", []):
        val = obs["value"]
        if val == ".":  # FRED uses "." for missing
            continue
        observations.append({
            "date": obs["date"],
            "value": float(val),
        })
    return observations


def fetch_treasury_yields(end_date_str):
    """Fetch daily Treasury par yield curve from Treasury.gov XML feed.
    This is a backup / cross-check for FRED Treasury data."""
    # Treasury.gov provides XML/CSV; we'll use FRED as primary since it's cleaner
    # This function is here as a verification source if needed
    pass


def compute_weekly_change(observations):
    """Given observations sorted desc by date, return (latest_value, latest_date, change, prior_date)."""
    if len(observations) < 2:
        if len(observations) == 1:
            return observations[0]["value"], observations[0]["date"], None, None
        return None, None, None, None

    latest = observations[0]
    prior = observations[1]

    # Try to find observation ~5 trading days back for proper weekly change
    for obs in observations[1:]:
        days_diff = (datetime.strptime(latest["date"], "%Y-%m-%d") -
                     datetime.strptime(obs["date"], "%Y-%m-%d")).days
        if days_diff >= 5:
            prior = obs
            break

    change = latest["value"] - prior["value"]
    return latest["value"], latest["date"], change, prior["date"]


def classify_signal(series_id, change):
    """Simple signal classification based on weekly change."""
    if change is None:
        return "N/A"

    # Rates: big moves matter
    if series_id in ("DGS10", "DGS2", "DFII10", "T10YIE"):
        if abs(change) < 0.05:
            return "neutral"
        elif change > 0.10:
            return "hawkish"
        elif change < -0.10:
            return "dovish"
        else:
            return "neutral"

    # Spreads: widening = stress
    if series_id in ("BAMLH0A0HYM2", "BAMLC0A0CM"):
        if change > 0.10:
            return "caution"
        elif change < -0.10:
            return "bullish"
        return "neutral"

    # VIX
    if series_id == "VIXCLS":
        if change > 2:
            return "caution"
        elif change < -2:
            return "bullish"
        return "neutral"

    # USD
    if series_id == "DTWEXBGS":
        if change > 0.5:
            return "USD strong"
        elif change < -0.5:
            return "USD weak"
        return "neutral"

    # Oil
    if series_id in ("DCOILWTICO", "DCOILBRENTEU"):
        pct = (change / (change + 1)) * 100 if change else 0
        if abs(pct) < 2:
            return "neutral"
        return "up" if change > 0 else "down"

    return "neutral"


def format_value(series_id, value):
    """Format value with appropriate units."""
    if series_id in ("DGS10", "DGS2", "DFII10", "T10YIE",
                      "BAMLH0A0HYM2", "BAMLC0A0CM"):
        return f"{value:.2f}%"
    if series_id == "VIXCLS":
        return f"{value:.2f}"
    if series_id == "DTWEXBGS":
        return f"{value:.2f}"
    if series_id in ("DCOILWTICO", "DCOILBRENTEU"):
        return f"${value:.2f}"
    return f"{value:.2f}"


def format_change(series_id, change):
    """Format weekly change with appropriate units."""
    if change is None:
        return "N/A"
    if series_id in ("DGS10", "DGS2", "DFII10", "T10YIE",
                      "BAMLH0A0HYM2", "BAMLC0A0CM"):
        bps = change * 100
        sign = "+" if bps >= 0 else ""
        return f"{sign}{bps:.0f} bps"
    if series_id == "VIXCLS":
        sign = "+" if change >= 0 else ""
        return f"{sign}{change:.2f}"
    if series_id == "DTWEXBGS":
        sign = "+" if change >= 0 else ""
        return f"{sign}{change:.2f}"
    if series_id in ("DCOILWTICO", "DCOILBRENTEU"):
        sign = "+" if change >= 0 else ""
        return f"{sign}${change:.2f}"
    sign = "+" if change >= 0 else ""
    return f"{sign}{change:.2f}"


def main():
    parser = argparse.ArgumentParser(description="Fetch macro data from FRED (official source)")
    parser.add_argument("--weeks", type=int, default=1, help="Number of weeks of data (default: 1)")
    parser.add_argument("--end-date", type=str, default=None,
                        help="End date YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    api_key = get_fred_key()

    if args.end_date:
        end_dt = datetime.strptime(args.end_date, "%Y-%m-%d")
    else:
        end_dt = datetime.now()

    # Fetch 3 weeks of data to ensure we have enough for weekly change calc
    start_dt = end_dt - timedelta(days=21 * args.weeks)

    end_date = end_dt.strftime("%Y-%m-%d")
    start_date = start_dt.strftime("%Y-%m-%d")

    print(f"Fetching FRED data: {start_date} to {end_date}")
    print(f"Source: Federal Reserve Economic Data (FRED)")
    print(f"{'=' * 70}")

    results = {}

    for series_id, label in FRED_SERIES.items():
        try:
            obs = fetch_fred_series(series_id, api_key, start_date, end_date)
            value, date, change, prior_date = compute_weekly_change(obs)
            signal = classify_signal(series_id, change)

            results[series_id] = {
                "label": label,
                "value": value,
                "date": date,
                "change": change,
                "prior_date": prior_date,
                "signal": signal,
                "observations": obs[:10],  # keep last 10 for reference
            }

            val_str = format_value(series_id, value) if value else "N/A"
            chg_str = format_change(series_id, change)
            print(f"  {label:<30s}  {val_str:>10s}  {chg_str:>12s}  [{signal}]  (as of {date})")

        except Exception as e:
            print(f"  {label:<30s}  ERROR: {e}", file=sys.stderr)
            results[series_id] = {
                "label": label,
                "value": None,
                "error": str(e),
            }

    # Compute derived metrics
    dgs10 = results.get("DGS10", {}).get("value")
    dgs2 = results.get("DGS2", {}).get("value")
    if dgs10 is not None and dgs2 is not None:
        spread_2s10s = dgs10 - dgs2
        # Get prior values for change
        dgs10_obs = results.get("DGS10", {}).get("observations", [])
        dgs2_obs = results.get("DGS2", {}).get("observations", [])
        prior_spread = None
        if len(dgs10_obs) > 1 and len(dgs2_obs) > 1:
            # Find matching prior dates
            for i in range(1, min(len(dgs10_obs), len(dgs2_obs))):
                days_10 = (datetime.strptime(dgs10_obs[0]["date"], "%Y-%m-%d") -
                          datetime.strptime(dgs10_obs[i]["date"], "%Y-%m-%d")).days
                if days_10 >= 5:
                    prior_spread = dgs10_obs[i]["value"] - dgs2_obs[min(i, len(dgs2_obs)-1)]["value"]
                    break

        spread_change = (spread_2s10s - prior_spread) if prior_spread else None
        results["2s10s_spread"] = {
            "label": "2s10s spread",
            "value": spread_2s10s,
            "change": spread_change,
            "signal": "steepening" if spread_change and spread_change > 0 else
                      "flattening" if spread_change and spread_change < 0 else "neutral",
        }
        chg_str = f"{spread_change*100:+.0f} bps" if spread_change else "N/A"
        print(f"  {'2s10s spread':<30s}  {spread_2s10s*100:>+8.0f} bps  {chg_str:>12s}  [{results['2s10s_spread']['signal']}]")

    print(f"{'=' * 70}")

    # Save raw JSON
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "macro_data.json"
    # Convert for JSON serialization
    json_results = {}
    for k, v in results.items():
        json_results[k] = {sk: sv for sk, sv in v.items()}
    with open(out_path, "w") as f:
        json.dump({
            "fetched_at": datetime.now().isoformat(),
            "source": "FRED (Federal Reserve Economic Data)",
            "source_url": "https://fred.stlouisfed.org",
            "period": {"start": start_date, "end": end_date},
            "data": json_results,
        }, f, indent=2, default=str)

    print(f"\nSaved: {out_path}")
    print(f"Source: FRED (fred.stlouisfed.org) — official Federal Reserve data")
    print(f"\nNOTE: FRED does not publish MOVE index or DXY (Dollar Index).")
    print(f"  DTWEXBGS (Trade-Weighted USD) is the closest official substitute for DXY.")
    print(f"  For MOVE index, no official free API exists.")


if __name__ == "__main__":
    main()
