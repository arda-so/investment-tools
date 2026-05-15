#!/usr/bin/env python3
"""Rank important earnings from earnings radar (upcoming + weekly reported)."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
import datetime as dt


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FOCUS = ROOT / "data" / "earnings_focus_tickers.txt"


def parse_cap(s: str) -> int:
    raw = (s or "").replace("$", "").replace(",", "").strip()
    if not raw:
        return 0
    try:
        return int(float(raw))
    except Exception:
        return 0


def load_focus(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out: set[str] = set()
    for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        out.add(s.upper())
    return out


def extract_rows_upcoming(md: str) -> list[dict]:
    rows: list[dict] = []
    lines = md.splitlines()
    in_table = False
    for i, ln in enumerate(lines):
        if "#### Universe Reporting Today/Tomorrow" in ln:
            in_table = True
            continue
        if not in_table:
            continue
        if ln.startswith("### ") and i > 0:
            break
        if not ln.strip().startswith("|"):
            continue
        if "Date | Symbol | Company" in ln or "|------|" in ln:
            continue
        parts = [p.strip() for p in ln.strip().split("|")]
        # ['', Date, Symbol, Company, Time, EPS Est, Last Year EPS, Market Cap, '']
        if len(parts) < 9:
            continue
        date, symbol, company, etime, eps_est, eps_last, mcap = parts[1:8]
        if not symbol or symbol == "Symbol":
            continue
        rows.append(
            {
                "date": date,
                "symbol": symbol.upper(),
                "company": company,
                "time": etime,
                "eps_est": eps_est,
                "eps_last": eps_last,
                "mcap": mcap,
                "mcap_n": parse_cap(mcap),
            }
        )
    return rows


def extract_rows_week(md: str) -> list[dict]:
    rows: list[dict] = []
    lines = md.splitlines()
    current_date = None
    current_iso = None
    year = dt.date.today().year

    for ln in lines:
        m = re.match(r"^### ([A-Za-z]+), ([A-Za-z]+) (\d{2})", ln.strip())
        if m:
            # Example: ### Wednesday, February 11 (DONE) — 24 companies
            month_name = m.group(2)
            day = int(m.group(3))
            try:
                current_date = dt.datetime.strptime(f"{month_name} {day} {year}", "%B %d %Y").date()
                current_iso = current_date.isoformat()
            except Exception:
                current_date = None
                current_iso = None
            continue

        if not current_iso:
            continue
        if not ln.strip().startswith("|"):
            continue
        if "Symbol | Company | Time" in ln or "|--------|---------|------|" in ln:
            continue
        parts = [p.strip() for p in ln.strip().split("|")]
        # ['', Symbol, Company, Time, EPS Est, Last Year EPS, Market Cap, '']
        if len(parts) < 8:
            continue
        symbol, company, etime, eps_est, eps_last, mcap = parts[1:7]
        if not symbol or symbol == "Symbol":
            continue
        rows.append(
            {
                "date": current_iso,
                "symbol": symbol.upper(),
                "company": company,
                "time": etime,
                "eps_est": eps_est,
                "eps_last": eps_last,
                "mcap": mcap,
                "mcap_n": parse_cap(mcap),
            }
        )
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Print top important earnings rows.")
    ap.add_argument("--file", required=True)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--focus-file", default=str(DEFAULT_FOCUS))
    ap.add_argument("--scope", choices=["upcoming", "week"], default="upcoming")
    args = ap.parse_args()

    p = Path(args.file)
    if not p.exists():
        return
    txt = p.read_text(encoding="utf-8", errors="ignore")
    if args.scope == "week":
        rows = extract_rows_week(txt)
    else:
        rows = extract_rows_upcoming(txt)
    if not rows:
        return

    focus = load_focus(Path(args.focus_file))
    if focus:
        rows = [r for r in rows if r["symbol"] in focus]
    rows.sort(key=lambda r: r["mcap_n"], reverse=True)

    today = dt.date.today().isoformat()
    for r in rows[: args.limit]:
        status = "REPORTED" if r["date"] < today else "UPCOMING"
        print(
            f"- {status} | {r['symbol']} | {r['date']} {r['time']} | "
            f"mcap {r['mcap']} | EPS {r['eps_est']} vs {r['eps_last']}"
        )


if __name__ == "__main__":
    main()
