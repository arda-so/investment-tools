#!/usr/bin/env python3
"""
insider_refresh.py — Backfill and refresh SEC Form 4 insider filings.

Usage:
  python3 tools/insider_refresh.py --ticker HUBS --months 18
  python3 tools/insider_refresh.py --all --months 18
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
FILINGS_DIR = REPO_ROOT / "filings"
DB_PATH = DATA_DIR / "research.db"

sys.path.insert(0, str(REPO_ROOT / "tools"))
from sec_client import get_all_filings, load_ticker_cik_map, download_filing_text  # noqa: E402


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def read_watchlist() -> list[str]:
    p = DATA_DIR / "my_watchlist.txt"
    out: list[str] = []
    if not p.exists():
        return out
    for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        t = s.split(",", 1)[0].strip().upper()
        if t:
            out.append(t)
    return sorted(set(out))


def read_portfolio() -> list[str]:
    p = DATA_DIR / "portfolio.csv"
    out: list[str] = []
    if not p.exists():
        return out
    for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        t = s.split(",", 1)[0].strip().upper()
        if t:
            out.append(t)
    return sorted(set(out))


def ensure_company_row(conn: sqlite3.Connection, ticker: str, cik: str) -> None:
    row = conn.execute("SELECT ticker FROM companies WHERE ticker = ?", (ticker,)).fetchone()
    if row:
        return
    conn.execute(
        "INSERT OR IGNORE INTO companies (ticker, name, cik, added_date) VALUES (?, ?, ?, ?)",
        (ticker, ticker, cik, datetime.now().isoformat()),
    )


def refresh_ticker(conn: sqlite3.Connection, ticker: str, cik: str, cutoff_date: str) -> tuple[int, int]:
    cik_raw = cik.lstrip("0")
    _, _, _, _, all_filings = get_all_filings(cik)

    form4 = []
    for f in all_filings:
        if f.get("form") != "4":
            continue
        d = (f.get("date") or "").strip()
        if not d or d < cutoff_date:
            continue
        form4.append(f)
    form4.sort(key=lambda x: x.get("date", ""), reverse=True)

    downloaded = 0
    scanned = 0
    FILINGS_DIR.mkdir(parents=True, exist_ok=True)
    for filing in form4:
        accession = (filing.get("accession") or "").strip()
        if not accession:
            continue
        scanned += 1
        ex = conn.execute(
            "SELECT id FROM filings WHERE ticker = ? AND accession = ? LIMIT 1",
            (ticker, accession),
        ).fetchone()
        if ex:
            continue

        accession_flat = accession.replace("-", "")
        primary_doc = (filing.get("primaryDocument") or "").strip()
        if not primary_doc:
            continue
        doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_raw}/{accession_flat}/{primary_doc}"
        try:
            text = download_filing_text(doc_url)
        except Exception:
            continue
        if len(text.strip()) < 120:
            continue

        acc_suffix = accession[-8:].replace("-", "")
        file_name = f"{ticker}_4_{filing['date']}_{acc_suffix}.txt"
        path = FILINGS_DIR / file_name
        path.write_text(text, encoding="utf-8")

        conn.execute(
            """INSERT INTO filings (ticker, form, date, accession, doc_url, path, downloaded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                ticker,
                "4",
                filing["date"],
                accession,
                doc_url,
                str(path),
                datetime.now().isoformat(),
            ),
        )
        downloaded += 1
    return downloaded, scanned


def main() -> None:
    p = argparse.ArgumentParser(description="Refresh SEC Form 4 insider history")
    p.add_argument("--ticker", help="Single ticker, e.g. HUBS")
    p.add_argument("--all", action="store_true", help="Use portfolio + watchlist")
    p.add_argument("--months", type=int, default=18, help="Lookback months (default 18)")
    args = p.parse_args()

    tickers: list[str] = []
    if args.ticker:
        tickers = [t.strip().upper() for t in args.ticker.split(",") if t.strip()]
    elif args.all:
        tickers = sorted(set(read_watchlist() + read_portfolio()))
    else:
        raise SystemExit("Provide --ticker or --all")

    if not tickers:
        print("No tickers to refresh.")
        return

    cutoff = (datetime.now() - timedelta(days=max(30, args.months * 30))).strftime("%Y-%m-%d")
    ticker_cik = load_ticker_cik_map()

    conn = db()
    total_dl = 0
    total_scanned = 0
    try:
        for i, t in enumerate(tickers, 1):
            cik = ticker_cik.get(t) or ticker_cik.get(t.replace(".", ""))
            if not cik:
                print(f"[{i}/{len(tickers)}] {t}: CIK not found")
                continue
            ensure_company_row(conn, t, cik)
            dl, scanned = refresh_ticker(conn, t, cik, cutoff)
            total_dl += dl
            total_scanned += scanned
            print(f"[{i}/{len(tickers)}] {t}: scanned={scanned} inserted={dl} (since {cutoff})")
        conn.commit()
    finally:
        conn.close()

    print(f"Done. tickers={len(tickers)} scanned={total_scanned} inserted={total_dl}")


if __name__ == "__main__":
    main()

