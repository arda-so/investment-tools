from __future__ import annotations

import datetime as dt

from app.core.ticker import normalize_ticker
from app.services.portfolio_state_service import read_watchlist_rows_state, write_watchlist_rows_state


WATCHLIST_PATH = None


def read_watchlist_rows() -> list[dict[str, str]]:
    return read_watchlist_rows_state()


def read_watchlist_tickers() -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for r in read_watchlist_rows():
        tk = normalize_ticker(str(r.get("ticker") or ""))
        if not tk or tk in seen:
            continue
        seen.add(tk)
        out.append(tk)
    return out


def write_watchlist_rows(rows: list[dict[str, str]]) -> None:
    cleaned: list[dict[str, str]] = []
    for r in rows:
        t = normalize_ticker(r.get("ticker", ""))
        if not t:
            continue
        added_at = str(r.get("added_at", "")).strip() or dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        reason = str(r.get("reason", "")).replace("\n", " ").strip()
        category = str(r.get("category", "")).replace("\n", " ").strip()
        cleaned.append({"ticker": t, "added_at": added_at, "reason": reason, "category": category})
    write_watchlist_rows_state(cleaned)
