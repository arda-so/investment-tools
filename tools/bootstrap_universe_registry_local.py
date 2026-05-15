#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE_DB = ROOT / "data" / "core.db"
INDEX_DIR = ROOT / "data" / "index_lists"

US_INDEX_FILES = {"sp500.txt", "russell1000.txt", "russell2000.txt"}


def _norm_ticker(raw: str) -> str:
    s = str(raw or "").strip().upper()
    if not s:
        return ""
    out = []
    for ch in s:
        if ch.isalnum() or ch in {".", "-"}:
            out.append(ch)
    return "".join(out)


def _read_index_tickers() -> tuple[set[str], dict[str, set[str]]]:
    all_tickers: set[str] = set()
    by_file: dict[str, set[str]] = {}
    for p in sorted(INDEX_DIR.glob("*.txt")):
        vals: set[str] = set()
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            tk = _norm_ticker(ln)
            if tk:
                vals.add(tk)
                all_tickers.add(tk)
        by_file[p.name] = vals
    return all_tickers, by_file


def main() -> int:
    all_idx, by_file = _read_index_tickers()
    us_listed = set()
    for fn in US_INDEX_FILES:
        us_listed |= by_file.get(fn, set())

    con = sqlite3.connect(str(CORE_DB))
    con.row_factory = sqlite3.Row
    now = dt.datetime.now().isoformat()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS universe_registry (
                ticker TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                cik TEXT NOT NULL DEFAULT '',
                exchange TEXT NOT NULL DEFAULT '',
                is_us_listed INTEGER NOT NULL DEFAULT 0,
                is_otc INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_universe_registry_exchange ON universe_registry(exchange)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_universe_registry_us ON universe_registry(is_us_listed)")

        name_map: dict[str, str] = {}
        for r in con.execute("SELECT ticker, name FROM company_profile_cache").fetchall():
            tk = _norm_ticker(r["ticker"])
            nm = str(r["name"] or "").strip()
            if tk and nm and tk not in name_map:
                name_map[tk] = nm
        for r in con.execute("SELECT ticker, name FROM companies").fetchall():
            tk = _norm_ticker(r["ticker"])
            nm = str(r["name"] or "").strip()
            if tk and nm and tk not in name_map:
                name_map[tk] = nm

        inserted = 0
        for tk in sorted(all_idx):
            nm = name_map.get(tk, "")
            is_us = 1 if tk in us_listed else 0
            ex = "US_INDEX" if is_us else "GLOBAL_INDEX"
            con.execute(
                """
                INSERT INTO universe_registry
                    (ticker, name, cik, exchange, is_us_listed, is_otc, source, updated_at)
                VALUES (?, ?, '', ?, ?, 0, 'local_index_lists', ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    name=CASE WHEN trim(universe_registry.name)='' THEN excluded.name ELSE universe_registry.name END,
                    exchange=CASE WHEN trim(universe_registry.exchange)='' THEN excluded.exchange ELSE universe_registry.exchange END,
                    is_us_listed=CASE WHEN universe_registry.is_us_listed=1 THEN 1 ELSE excluded.is_us_listed END,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (tk, nm, ex, is_us, now),
            )
            inserted += 1

            con.execute(
                """
                INSERT INTO company_profile_cache (ticker, name, country, industry, sector, updated_at)
                VALUES (?, ?, '', '', '', ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    name=CASE WHEN trim(company_profile_cache.name)='' THEN excluded.name ELSE company_profile_cache.name END,
                    updated_at=excluded.updated_at
                """,
                (tk, nm, now),
            )
            con.execute(
                """
                INSERT INTO companies (ticker, name, cik, added_date)
                VALUES (?, ?, '', ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    name=CASE WHEN trim(companies.name)='' THEN excluded.name ELSE companies.name END
                """,
                (tk, nm, now),
            )

        con.commit()
        uni_us = int(con.execute("SELECT COUNT(*) FROM universe_registry WHERE is_us_listed=1").fetchone()[0] or 0)
        uni_all = int(con.execute("SELECT COUNT(*) FROM universe_registry").fetchone()[0] or 0)
        prof = int(con.execute("SELECT COUNT(*) FROM company_profile_cache").fetchone()[0] or 0)
        comp = int(con.execute("SELECT COUNT(*) FROM companies").fetchone()[0] or 0)
    finally:
        con.close()

    print(json.dumps(
        {
            "seeded_rows": inserted,
            "universe_registry_total": uni_all,
            "universe_registry_us_listed": uni_us,
            "company_profile_cache_total": prof,
            "companies_total": comp,
        },
        ensure_ascii=True,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

