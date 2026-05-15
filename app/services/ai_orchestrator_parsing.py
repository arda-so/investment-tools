from __future__ import annotations

import datetime as dt
import re

from app.core.config import ROOT
from app.core.normalize import normalize_text as _norm
from app.core.ticker import safe_ticker_flexible as _safe_ticker
from app.services.ai_orchestrator_constants import WEEKDAY_TO_INT
from app.services.portfolio_memory_service import get_holdings


def extract_ticker(text: str) -> str:
    from app.services.orchestrator.command_parser import extract_ticker as _extract

    return _extract(text)


def extract_tickers_bulk(text: str) -> list[str]:
    from app.services.orchestrator.command_parser import extract_tickers_bulk as _extract_bulk

    return _extract_bulk(text)


def is_known_ticker_for_user_scope(ticker: str) -> bool:
    tk = _safe_ticker(ticker)
    if not tk:
        return False
    try:
        from app.services.postgres_core_service import pg_connect

        con_pg = pg_connect()
        if con_pg is not None:
            try:
                cur = con_pg.cursor()
                cur.execute("SELECT 1 FROM companies_core WHERE UPPER(ticker)=%s LIMIT 1", (tk,))
                if cur.fetchone():
                    return True
            finally:
                con_pg.close()
    except Exception:
        pass
    try:
        for row in get_holdings(limit=200):
            if _safe_ticker(str(row.get("ticker") or "")) == tk:
                return True
    except Exception:
        pass
    try:
        watchlist_path = ROOT / "data" / "my_watchlist.txt"
        if watchlist_path.exists():
            for line in watchlist_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                entry = str(line or "").strip()
                if not entry or entry.startswith("#"):
                    continue
                parts = [part.strip() for part in entry.split(",")]
                candidate = _safe_ticker(parts[0] if parts else "")
                if candidate == tk:
                    return True
    except Exception:
        pass
    return False


def extract_ticker_for_sec_query(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    explicit = re.search(r"\b(?:company|ticker)\s+\$?([A-Za-z]{1,5})\b", raw, flags=re.I)
    if explicit:
        return _safe_ticker(explicit.group(1))
    dollar = re.search(r"\$([A-Za-z]{1,5})\b", raw)
    if dollar:
        return _safe_ticker(dollar.group(1))
    form_re = r"(?:10[\s-]?k|10[\s-]?q|8[\s-]?k)"
    checks = [
        re.search(rf"\b([A-Za-z]{{1,5}})\s+{form_re}\b", raw, flags=re.I),
        re.search(rf"\b{form_re}\s+(?:for|of)\s+([A-Za-z]{{1,5}})\b", raw, flags=re.I),
        re.search(rf"\b(?:for|of)\s+([A-Za-z]{{1,5}})\s+{form_re}\b", raw, flags=re.I),
    ]
    for match in checks:
        if not match:
            continue
        ticker = _safe_ticker(match.group(1))
        if not ticker:
            continue
        if is_known_ticker_for_user_scope(ticker):
            return ticker
    return ""


def resolve_ticker_from_company_name(norm_text: str) -> str:
    try:
        from app.services.orchestrator.ticker_resolution import resolve_ticker_from_company_name as _resolve

        return str(_resolve(norm_text) or "").strip().upper()
    except Exception:
        return ""


def resolve_ticker_from_company_hint(text: str) -> str:
    try:
        from app.services.orchestrator.ticker_resolution import resolve_ticker_from_company_hint as _resolve

        return str(_resolve(text) or "").strip().upper()
    except Exception:
        return ""


def next_weekday(base: dt.date, target_weekday: int) -> dt.date:
    delta = (target_weekday - base.weekday()) % 7
    if delta == 0:
        delta = 7
    return base + dt.timedelta(days=delta)


def next_business_day(base: dt.date) -> dt.date:
    day = base + dt.timedelta(days=1)
    while day.weekday() >= 5:
        day = day + dt.timedelta(days=1)
    return day


def normalize_due_weekend(day: dt.date) -> dt.date:
    if day.weekday() == 5:
        return day + dt.timedelta(days=2)
    if day.weekday() == 6:
        return day + dt.timedelta(days=1)
    return day


def extract_due_date(task_text: str) -> tuple[str, str]:
    text = str(task_text or "").strip()
    low = _norm(text)
    today = dt.date.today()
    due: dt.date | None = None
    if "today" in low:
        due = normalize_due_weekend(today)
        text = re.sub(r"\btoday\b", "", text, flags=re.I).strip(" ,.-")
    elif "tomorrow" in low:
        due = next_business_day(today)
        text = re.sub(r"\btomorrow\b", "", text, flags=re.I).strip(" ,.-")
    elif "next week" in low:
        due = next_weekday(today, 0)
        text = re.sub(r"\bnext\s+week\b", "", text, flags=re.I).strip(" ,.-")
    else:
        for weekday, weekday_int in WEEKDAY_TO_INT.items():
            if re.search(rf"\b{weekday}\b", low):
                due = next_weekday(today, weekday_int)
                text = re.sub(rf"\b{weekday}\b", "", text, flags=re.I).strip(" ,.-")
                break
    return due.isoformat() if due else "", text.strip()
