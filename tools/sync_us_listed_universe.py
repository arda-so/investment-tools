#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import ssl
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CORE_DB = ROOT / "data" / "core.db"
SEC_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
USER_AGENT = "Investment_Tools_Bot contact@example.com"

US_EXCHANGES = {
    "NASDAQ",
    "NASDAQ CAPITAL MARKET",
    "NASDAQ GLOBAL MARKET",
    "NASDAQ GLOBAL SELECT",
    "NYSE",
    "NYSE AMERICAN",
    "NYSE ARCA",
    "NYSE MKT",
    "CBOE BZX",
    "CBOE BYX",
    "IEX",
}


def _norm_ticker(raw: str) -> str:
    t = str(raw or "").strip().upper()
    if not t:
        return ""
    t = re.sub(r"[^A-Z0-9.\-]", "", t)
    return t


def _fetch_sec_rows() -> list[dict[str, str]]:
    req = urllib.request.Request(
        SEC_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    ctx = None
    try:
        import certifi  # type: ignore

        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = None
    try:
        with urllib.request.urlopen(req, timeout=12, context=ctx) as resp:
            raw = resp.read()
    except Exception as exc:
        msg = str(exc).upper()
        if "CERTIFICATE_VERIFY_FAILED" not in msg:
            raise
        # Local macOS trust-store fallback: allow one insecure retry to unblock bootstrap.
        insecure_ctx = ssl._create_unverified_context()  # noqa: S323
        with urllib.request.urlopen(req, timeout=12, context=insecure_ctx) as resp:
            raw = resp.read()
    payload = json.loads(raw.decode("utf-8", errors="ignore")) if raw else {}

    rows: list[dict[str, str]] = []
    if isinstance(payload, dict) and isinstance(payload.get("fields"), list) and isinstance(payload.get("data"), list):
        fields = [str(x or "").strip() for x in payload.get("fields") or []]
        for row in payload.get("data") or []:
            if not isinstance(row, list):
                continue
            obj = {fields[i]: row[i] for i in range(min(len(fields), len(row)))}
            tk = _norm_ticker(str(obj.get("ticker") or ""))
            if not tk:
                continue
            rows.append(
                {
                    "ticker": tk,
                    "name": str(obj.get("name") or "").strip(),
                    "cik": str(obj.get("cik") or "").strip(),
                    "exchange": str(obj.get("exchange") or "").strip(),
                }
            )
        return rows

    # Fallback legacy SEC structure: {"0":{"cik_str":...,"ticker":"...","title":"..."}}
    if isinstance(payload, dict):
        for v in payload.values():
            if not isinstance(v, dict):
                continue
            tk = _norm_ticker(str(v.get("ticker") or ""))
            if not tk:
                continue
            rows.append(
                {
                    "ticker": tk,
                    "name": str(v.get("title") or v.get("name") or "").strip(),
                    "cik": str(v.get("cik_str") or v.get("cik") or "").strip(),
                    "exchange": "",
                }
            )
    return rows


def sync_universe() -> dict[str, object]:
    rows = _fetch_sec_rows()
    if not rows:
        return {"ok": False, "error": "no_sec_rows_fetched"}

    now = dt.datetime.now().isoformat()
    con = sqlite3.connect(str(CORE_DB))
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
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS companies (
                ticker TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                cik TEXT NOT NULL DEFAULT '',
                added_date TEXT NOT NULL DEFAULT ''
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS company_profile_cache (
                ticker TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL DEFAULT '',
                industry TEXT NOT NULL DEFAULT '',
                sector TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )

        total = 0
        us_listed = 0
        otc = 0
        for r in rows:
            tk = _norm_ticker(r.get("ticker") or "")
            if not tk:
                continue
            nm = str(r.get("name") or "").strip()
            cik = str(r.get("cik") or "").strip()
            ex = str(r.get("exchange") or "").strip()
            ex_u = ex.upper()
            is_us = 1 if ex_u in US_EXCHANGES else 0
            is_otc = 1 if ("OTC" in ex_u or "PINK" in ex_u) else 0
            if is_us:
                us_listed += 1
            if is_otc:
                otc += 1
            total += 1

            con.execute(
                """
                INSERT INTO universe_registry
                    (ticker, name, cik, exchange, is_us_listed, is_otc, source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'sec_company_tickers_exchange', ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    name=excluded.name,
                    cik=excluded.cik,
                    exchange=excluded.exchange,
                    is_us_listed=excluded.is_us_listed,
                    is_otc=excluded.is_otc,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (tk, nm, cik, ex, is_us, is_otc, now),
            )

            con.execute(
                """
                INSERT INTO companies (ticker, name, cik, added_date)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    name=CASE WHEN trim(companies.name)='' THEN excluded.name ELSE companies.name END,
                    cik=CASE WHEN trim(companies.cik)='' THEN excluded.cik ELSE companies.cik END
                """,
                (tk, nm, cik, now),
            )

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

        con.commit()
        cur = con.execute("SELECT COUNT(*) FROM universe_registry")
        reg_count = int(cur.fetchone()[0] or 0)
        cur = con.execute("SELECT COUNT(*) FROM company_profile_cache")
        prof_count = int(cur.fetchone()[0] or 0)
        cur = con.execute("SELECT COUNT(*) FROM companies")
        cmp_count = int(cur.fetchone()[0] or 0)
    finally:
        con.close()

    out = {
        "fetched_rows": total,
        "fetched_us_listed": us_listed,
        "fetched_otc_like": otc,
        "universe_registry_total": reg_count,
        "company_profile_cache_total": prof_count,
        "companies_total": cmp_count,
    }

    dsn = str(os.getenv("POSTGRES_DSN", "")).strip()
    if dsn:
        try:
            import psycopg2  # type: ignore
            from app.services.postgres_core_service import ensure_postgres_core_schema  # type: ignore

            try:
                ensure_postgres_core_schema()
            except Exception:
                pass

            con_pg = psycopg2.connect(dsn)
            try:
                cur = con_pg.cursor()
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS universe_registry_core (
                        ticker TEXT PRIMARY KEY,
                        name TEXT NOT NULL DEFAULT '',
                        cik TEXT NOT NULL DEFAULT '',
                        exchange TEXT NOT NULL DEFAULT '',
                        is_us_listed BOOLEAN NOT NULL DEFAULT FALSE,
                        is_otc BOOLEAN NOT NULL DEFAULT FALSE,
                        source TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_universe_registry_core_exchange ON universe_registry_core(exchange)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_universe_registry_core_us ON universe_registry_core(is_us_listed)"
                )
                for r in rows:
                    tk = _norm_ticker(r.get("ticker") or "")
                    if not tk:
                        continue
                    nm = str(r.get("name") or "").strip()
                    cik = str(r.get("cik") or "").strip()
                    cur.execute(
                        """
                        INSERT INTO companies_core (ticker, name, cik, added_date)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (ticker) DO UPDATE SET
                          name = CASE WHEN trim(companies_core.name) = '' THEN EXCLUDED.name ELSE companies_core.name END,
                          cik = CASE WHEN trim(companies_core.cik) = '' THEN EXCLUDED.cik ELSE companies_core.cik END
                        """,
                        (tk, nm, cik, now),
                    )
                    cur.execute(
                        """
                        INSERT INTO universe_registry_core
                          (ticker, name, cik, exchange, is_us_listed, is_otc, source, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (ticker) DO UPDATE SET
                          name = EXCLUDED.name,
                          cik = EXCLUDED.cik,
                          exchange = EXCLUDED.exchange,
                          is_us_listed = EXCLUDED.is_us_listed,
                          is_otc = EXCLUDED.is_otc,
                          source = EXCLUDED.source,
                          updated_at = EXCLUDED.updated_at
                        """,
                        (
                            tk,
                            nm,
                            cik,
                            str(r.get("exchange") or "").strip(),
                            (str(r.get("exchange") or "").strip().upper() in US_EXCHANGES),
                            ("OTC" in str(r.get("exchange") or "").strip().upper() or "PINK" in str(r.get("exchange") or "").strip().upper()),
                            "sec_company_tickers_exchange",
                            now,
                        ),
                    )
                    cur.execute(
                        """
                        INSERT INTO company_profile_cache_core (ticker, name, country, industry, sector, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (ticker) DO UPDATE SET
                          name = CASE WHEN trim(company_profile_cache_core.name) = '' THEN EXCLUDED.name ELSE company_profile_cache_core.name END,
                          updated_at = EXCLUDED.updated_at
                        """,
                        (tk, nm, "", "", "", now),
                    )
                con_pg.commit()
                cur.execute("SELECT COUNT(*) FROM company_profile_cache_core")
                out["company_profile_cache_core_total"] = int((cur.fetchone() or [0])[0] or 0)
                cur.execute("SELECT COUNT(*) FROM companies_core")
                out["companies_core_total"] = int((cur.fetchone() or [0])[0] or 0)
                cur.execute("SELECT COUNT(*) FROM universe_registry_core")
                out["universe_registry_core_total"] = int((cur.fetchone() or [0])[0] or 0)
            finally:
                con_pg.close()
        except Exception as exc:
            out["postgres_sync_error"] = str(exc)

    return out


def main() -> int:
    out = sync_universe()
    print(json.dumps(out, ensure_ascii=True))
    return 0 if bool(out.get("fetched_rows")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
