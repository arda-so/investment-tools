from __future__ import annotations

import datetime as dt

from app.core.db import core_conn as _conn, sqlite_retry
from app.core.ticker import normalize_ticker
from app.services.postgres_core_service import (
    core_backend,
    list_blue_chips_pg,
    remove_blue_chip_pg,
    upsert_blue_chip_pg,
)

def _ensure_blue_chips_schema_sqlite() -> None:
    def _write() -> None:
        con = _conn()
        try:
            con.execute(
                """CREATE TABLE IF NOT EXISTS blue_chips (
                    ticker TEXT PRIMARY KEY,
                    added_at TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT ''
                )"""
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_blue_chips_added ON blue_chips(added_at DESC)")
            con.commit()
        finally:
            con.close()

    sqlite_retry(_write)


def list_blue_chips_rows(limit: int = 120) -> list[dict[str, str]]:
    lim = max(1, min(5000, int(limit or 120)))
    out: list[dict[str, str]] = []
    if core_backend() == "postgres":
        for r in list_blue_chips_pg(limit=lim):
            tk = normalize_ticker(str(r.get("ticker") or ""))
            if not tk:
                continue
            reason = str(r.get("reason") or "").strip()
            added_at = str(r.get("added_at") or "")
            out.append(
                {
                    "ticker": tk,
                    "reason": reason,
                    "added_at": added_at,
                    "name": reason,
                }
            )
        return out

    _ensure_blue_chips_schema_sqlite()
    con = _conn()
    try:
        rows = con.execute(
            "SELECT ticker, reason, added_at FROM blue_chips ORDER BY added_at DESC, ticker ASC LIMIT ?",
            (lim,),
        ).fetchall()
        for r in rows:
            tk = normalize_ticker(str(r["ticker"] or ""))
            if not tk:
                continue
            reason = str(r["reason"] or "").strip()
            added_at = str(r["added_at"] or "")
            out.append(
                {
                    "ticker": tk,
                    "reason": reason,
                    "added_at": added_at,
                    "name": reason,
                }
            )
    except Exception:
        return []
    finally:
        con.close()
    return out


def upsert_blue_chip(ticker: str, reason: str = "") -> bool:
    tk = normalize_ticker(ticker)
    if not tk:
        return False
    rsn = str(reason or "").strip()
    if core_backend() == "postgres":
        return bool(upsert_blue_chip_pg(tk, reason=rsn))

    _ensure_blue_chips_schema_sqlite()

    def _write() -> None:
        con = _conn()
        try:
            con.execute(
                """INSERT INTO blue_chips(ticker, added_at, reason)
                   VALUES (?, ?, ?)
                   ON CONFLICT(ticker) DO UPDATE SET
                     added_at=excluded.added_at,
                     reason=excluded.reason""",
                (tk, dt.datetime.now().isoformat(timespec="seconds"), rsn),
            )
            con.commit()
        finally:
            con.close()

    sqlite_retry(_write)
    return True


def remove_blue_chip(ticker: str) -> bool:
    tk = normalize_ticker(ticker)
    if not tk:
        return False
    if core_backend() == "postgres":
        return bool(remove_blue_chip_pg(tk))

    _ensure_blue_chips_schema_sqlite()
    removed = {"ok": False}

    def _write() -> None:
        con = _conn()
        try:
            cur = con.execute("DELETE FROM blue_chips WHERE ticker=?", (tk,))
            con.commit()
            removed["ok"] = int(cur.rowcount or 0) > 0
        finally:
            con.close()

    sqlite_retry(_write)
    return bool(removed["ok"])
