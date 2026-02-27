from __future__ import annotations

import datetime as dt
from typing import Any

from app.core import cloud_files
from app.core.ticker import normalize_ticker
from app.services.postgres_core_service import core_backend, pg_connect, strict_postgres_mode


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _ensure_state_schema_pg() -> bool:
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_positions_core (
                ticker TEXT PRIMARY KEY,
                shares DOUBLE PRECISION NOT NULL DEFAULT 0,
                cost DOUBLE PRECISION NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cash_balances_core (
                currency TEXT PRIMARY KEY,
                amount DOUBLE PRECISION NOT NULL DEFAULT 0,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist_core (
                ticker TEXT PRIMARY KEY,
                added_at TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def _read_local_portfolio_rows() -> list[dict[str, str]]:
    txt = cloud_files.read_text("data/portfolio.csv")
    if not txt:
        return []
    out: list[dict[str, str]] = []
    for ln in str(txt).splitlines():
        parts = [x.strip() for x in ln.split(",")]
        tk = normalize_ticker(parts[0] if parts else "")
        if not tk:
            continue
        out.append(
            {
                "ticker": tk,
                "shares": parts[1] if len(parts) >= 2 else "",
                "cost": parts[2] if len(parts) >= 3 else "",
                "note": parts[3] if len(parts) >= 4 else "",
            }
        )
    return out


def _read_local_cash_rows() -> list[dict[str, str]]:
    txt = cloud_files.read_text("data/cash_balances.csv")
    if not txt:
        return []
    out: list[dict[str, str]] = []
    for i, ln in enumerate(str(txt).splitlines()):
        s = str(ln or "").strip()
        if not s:
            continue
        if i == 0 and "currency" in s.lower() and "amount" in s.lower():
            continue
        parts = [x.strip() for x in s.split(",")]
        ccy = str(parts[0] if parts else "USD").strip().upper() or "USD"
        out.append({"currency": ccy, "amount": str(_to_float(parts[1] if len(parts) >= 2 else 0.0, 0.0))})
    return out


def _read_local_watchlist_rows() -> list[dict[str, str]]:
    txt = cloud_files.read_text("data/my_watchlist.txt")
    if not txt:
        return []
    out: list[dict[str, str]] = []
    for ln in str(txt).splitlines():
        s = str(ln or "").strip()
        if not s or s.startswith("#"):
            continue
        parts = [x.strip() for x in s.split(",")]
        tk = normalize_ticker(parts[0] if parts else "")
        if not tk:
            continue
        out.append(
            {
                "ticker": tk,
                "added_at": parts[1] if len(parts) >= 2 else "",
                "reason": parts[2] if len(parts) >= 3 else "",
            }
        )
    return out


def _mirror_portfolio_to_file(rows: list[dict[str, str]]) -> None:
    lines: list[str] = []
    for r in rows:
        tk = normalize_ticker(str(r.get("ticker") or ""))
        if not tk:
            continue
        sh = _to_float(r.get("shares"), 0.0)
        cost = _to_float(r.get("cost"), 0.0)
        note = str(r.get("note") or "").replace("\n", " ").strip()
        lines.append(",".join([tk, f"{sh:g}", f"{cost:g}", note]))
    cloud_files.write_text("data/portfolio.csv", "\n".join(lines) + ("\n" if lines else ""))


def _mirror_cash_to_file(rows: list[dict[str, str]]) -> None:
    lines = ["currency,amount"]
    for r in rows:
        ccy = str(r.get("currency") or "USD").strip().upper() or "USD"
        amt = _to_float(r.get("amount"), 0.0)
        lines.append(f"{ccy},{amt:g}")
    cloud_files.write_text("data/cash_balances.csv", "\n".join(lines) + "\n")


def _mirror_watchlist_to_file(rows: list[dict[str, str]]) -> None:
    lines = ["# TICKER,ADDED_AT,REASON"]
    for r in rows:
        tk = normalize_ticker(str(r.get("ticker") or ""))
        if not tk:
            continue
        added = str(r.get("added_at") or "").strip() or dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        reason = str(r.get("reason") or "").replace("\n", " ").strip()
        lines.append(",".join([tk, added, reason]))
    cloud_files.write_text("data/my_watchlist.txt", "\n".join(lines) + "\n")


def read_portfolio_rows_state() -> list[dict[str, str]]:
    if core_backend() != "postgres":
        return _read_local_portfolio_rows()
    if not _ensure_state_schema_pg():
        if strict_postgres_mode():
            return []
        return _read_local_portfolio_rows()
    con = pg_connect()
    if con is None:
        if strict_postgres_mode():
            return []
        return _read_local_portfolio_rows()
    try:
        cur = con.cursor()
        cur.execute("SELECT ticker, shares, cost, note FROM portfolio_positions_core ORDER BY ticker ASC")
        rows = cur.fetchall() or []
        out = [
            {
                "ticker": normalize_ticker(str(r[0] or "")),
                "shares": f"{_to_float(r[1], 0.0):g}",
                "cost": f"{_to_float(r[2], 0.0):g}",
                "note": str(r[3] or ""),
            }
            for r in rows
            if normalize_ticker(str(r[0] or ""))
        ]
        if out:
            return out
        if strict_postgres_mode():
            return []
        return _read_local_portfolio_rows()
    except Exception:
        if strict_postgres_mode():
            return []
        return _read_local_portfolio_rows()
    finally:
        con.close()


def write_portfolio_rows_state(rows: list[dict[str, str]]) -> None:
    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    for r in rows:
        tk = normalize_ticker(str(r.get("ticker") or ""))
        if not tk or tk in seen:
            continue
        seen.add(tk)
        cleaned.append(
            {
                "ticker": tk,
                "shares": f"{_to_float(r.get('shares'), 0.0):g}",
                "cost": f"{_to_float(r.get('cost'), 0.0):g}",
                "note": str(r.get("note") or "").replace("\n", " ").strip(),
            }
        )
    if not strict_postgres_mode():
        _mirror_portfolio_to_file(cleaned)
    if core_backend() != "postgres":
        return
    if not _ensure_state_schema_pg():
        return
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM portfolio_positions_core")
        for r in cleaned:
            cur.execute(
                """
                INSERT INTO portfolio_positions_core (ticker, shares, cost, note, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                """,
                (r["ticker"], _to_float(r["shares"], 0.0), _to_float(r["cost"], 0.0), r["note"]),
            )
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


def read_cash_rows_state() -> list[dict[str, str]]:
    if core_backend() != "postgres":
        return _read_local_cash_rows()
    if not _ensure_state_schema_pg():
        if strict_postgres_mode():
            return []
        return _read_local_cash_rows()
    con = pg_connect()
    if con is None:
        if strict_postgres_mode():
            return []
        return _read_local_cash_rows()
    try:
        cur = con.cursor()
        cur.execute("SELECT currency, amount FROM cash_balances_core ORDER BY currency ASC")
        rows = cur.fetchall() or []
        out = [{"currency": str(r[0] or "USD").upper(), "amount": f"{_to_float(r[1], 0.0):g}"} for r in rows]
        if out:
            return out
        if strict_postgres_mode():
            return []
        return _read_local_cash_rows()
    except Exception:
        if strict_postgres_mode():
            return []
        return _read_local_cash_rows()
    finally:
        con.close()


def write_cash_rows_state(rows: list[dict[str, str]]) -> None:
    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    for r in rows:
        ccy = str(r.get("currency") or "USD").strip().upper() or "USD"
        if ccy in seen:
            continue
        seen.add(ccy)
        cleaned.append({"currency": ccy, "amount": f"{_to_float(r.get('amount'), 0.0):g}"})
    if not strict_postgres_mode():
        _mirror_cash_to_file(cleaned)
    if core_backend() != "postgres":
        return
    if not _ensure_state_schema_pg():
        return
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM cash_balances_core")
        for r in cleaned:
            cur.execute(
                "INSERT INTO cash_balances_core (currency, amount, updated_at) VALUES (%s, %s, NOW())",
                (r["currency"], _to_float(r["amount"], 0.0)),
            )
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


def read_watchlist_rows_state() -> list[dict[str, str]]:
    if core_backend() != "postgres":
        return _read_local_watchlist_rows()
    if not _ensure_state_schema_pg():
        if strict_postgres_mode():
            return []
        return _read_local_watchlist_rows()
    con = pg_connect()
    if con is None:
        if strict_postgres_mode():
            return []
        return _read_local_watchlist_rows()
    try:
        cur = con.cursor()
        cur.execute("SELECT ticker, added_at, reason FROM watchlist_core ORDER BY ticker ASC")
        rows = cur.fetchall() or []
        out = []
        for r in rows:
            tk = normalize_ticker(str(r[0] or ""))
            if not tk:
                continue
            out.append({"ticker": tk, "added_at": str(r[1] or ""), "reason": str(r[2] or "")})
        if out:
            return out
        if strict_postgres_mode():
            return []
        return _read_local_watchlist_rows()
    except Exception:
        if strict_postgres_mode():
            return []
        return _read_local_watchlist_rows()
    finally:
        con.close()


def write_watchlist_rows_state(rows: list[dict[str, str]]) -> None:
    cleaned: list[dict[str, str]] = []
    seen: set[str] = set()
    for r in rows:
        tk = normalize_ticker(str(r.get("ticker") or ""))
        if not tk or tk in seen:
            continue
        seen.add(tk)
        cleaned.append(
            {
                "ticker": tk,
                "added_at": str(r.get("added_at") or "").strip() or dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "reason": str(r.get("reason") or "").replace("\n", " ").strip(),
            }
        )
    if not strict_postgres_mode():
        _mirror_watchlist_to_file(cleaned)
    if core_backend() != "postgres":
        return
    if not _ensure_state_schema_pg():
        return
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM watchlist_core")
        for r in cleaned:
            cur.execute(
                "INSERT INTO watchlist_core (ticker, added_at, reason, updated_at) VALUES (%s, %s, %s, NOW())",
                (r["ticker"], r["added_at"], r["reason"]),
            )
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()
