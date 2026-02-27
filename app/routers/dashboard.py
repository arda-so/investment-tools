from __future__ import annotations

import concurrent.futures
import csv
import datetime as dt
import difflib
import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.config import CORE_DB_PATH, ROOT
from app.core.date import parse_datetime_flexible
from app.core.market import finnhub_key
from app.core.num import to_float, to_float_clean as _to_float
from app.core.db import get_sqlite_conn
from app.core.ticker import normalize_ticker, yfinance_symbol
from app.core.universe_command_parser import parse_universe_command
from app.core.trade_decision_taxonomy import (
    label_for_buy_belief,
    label_for_buy_reason,
    label_for_sell_reason,
    normalize_buy_belief,
    normalize_buy_reason,
    normalize_sell_reason,
)
from app.services.ai_orchestrator import (
    get_risk_veto_config,
    list_recent_risk_veto_decisions,
    update_risk_veto_config,
)
from app.services.blue_chip_service import (
    list_blue_chips_rows,
    remove_blue_chip,
    upsert_blue_chip,
)
from app.services.company_lookup_service import company_name_map as lookup_company_name_map
from app.services.ai_insight_service import list_ai_meta_suggestions, run_ai_meta_suggestions
from app.services.company_file_service import add_company_reminder
from app.services.company_file_service import list_companies
from app.services.dashboard_service import ask_ai_local, ask_workspace_ai, dashboard_snapshot, quick_capture, portfolio_intelligence_brief
from app.services.earnings_transcript_service import list_sec_earnings_releases
from app.services.organizer_service import list_recent_notes, list_tasks
from app.services.portfolio_memory_service import (
    get_cached_morning_brief,
    query_report_facts,
    record_decision,
    record_portfolio_transaction,
    upsert_watchlist_thesis,
)
from app.services.reports_service import list_reports
from app.services.proactive_ai_service import (
    dismiss_action_proposal,
    dismiss_cascade_alert,
    dismiss_thesis_breach_alert,
    execute_action_proposal,
    get_ai_accuracy_stats,
    get_ai_audit_timeline,
    learn_from_rejection,
    list_action_proposals,
    list_cascade_alerts,
    list_recent_agent_runs,
    list_recent_reflexions,
    list_thesis_breach_alerts,
    reject_action_proposal,
    rollback_reflexion_policy,
    run_event_driven_monitor,
    simulate_macro_shock,
    simulate_macro_shock_batch,
)
from app.services.sec_ingest_pipeline_service import process_new_filings_pipeline
from app.services.postgres_core_service import (
    core_backend,
    list_earnings_calendar_snapshot_pg,
    pg_connect,
    strict_postgres_mode,
    upsert_earnings_calendar_snapshot_pg,
)
from app.services.watchlist_service import read_watchlist_rows, write_watchlist_rows
from app.services.workspace_feed_service import add_workspace_message, list_workspace_channels, load_workspace_feed
from app.services.portfolio_state_service import (
    read_cash_rows_state,
    read_portfolio_rows_state,
    write_cash_rows_state,
    write_portfolio_rows_state,
)


router = APIRouter()

PORTFOLIO_PATH = ROOT / "data" / "portfolio.csv"
CASH_BAL_PATH = ROOT / "data" / "cash_balances.csv"
MARKET_CACHE_PATH = ROOT / "data" / "cache" / "market_brief.json"
MARKET_BRIEF_CACHE_TTL_SEC = 300.0
UNIVERSE_QUOTES_CACHE_PATH = ROOT / "data" / "cache" / "universe_quotes.json"
UNIVERSE_QUOTES_TTL_SEC = 300.0
UNIVERSE_QUOTES_REFRESH_MIN_INTERVAL_SEC = 20.0
REPORTS_DIR = ROOT / "reports"
MONITOR_RENDER_TIMEOUT_SEC = 0.35
EARNINGS_ENRICH_TTL_SEC = 180.0
_EARNINGS_ENRICH_CACHE: dict[str, object] = {"ts": 0.0, "key": "", "rows": []}
_UNIVERSE_QUOTES_LOCK = threading.Lock()
_UNIVERSE_QUOTES_REFRESH_LOCK = threading.Lock()
_UNIVERSE_QUOTES_REFRESHING = False
_UNIVERSE_QUOTES_LAST_REFRESH_TS = 0.0
_NAME_FALLBACK_CACHE: dict[str, tuple[str, float]] = {}
_NAME_FALLBACK_LOCK = threading.Lock()
_NAME_FALLBACK_TTL_SEC = 24 * 60 * 60
_GLOBAL_SEARCH_LOCK = threading.Lock()
_GLOBAL_SEARCH_CACHE_TTL_SEC = 20.0
_GLOBAL_SEARCH_CACHE: dict[str, tuple[float, list[dict[str, str]]]] = {}
_SEC_FORM_ALIAS: dict[str, str] = {
    "6k": "6-K",
    "8k": "8-K",
    "10k": "10-K",
    "10q": "10-Q",
    "20f": "20-F",
    "40f": "40-F",
}
_SEC_SEARCH_WORDS: set[str] = {
    "sec",
    "filing",
    "filings",
    "earnings",
    "release",
    "releases",
    "report",
    "reports",
}
_SEC_HINT_TOKENS: set[str] = {
    "sec",
    "filing",
    "earnings",
    "10k",
    "10-k",
    "10q",
    "10-q",
    "8k",
    "8-k",
    "20f",
    "20-f",
    "40f",
    "40-f",
    "6k",
    "6-k",
}
BLUE_CHIPS_SEED_TICKERS: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "BRK.B", "JPM", "V", "UNH",
    "XOM", "JNJ", "PG", "AVGO", "MA", "HD", "COST", "KO", "PEP", "ABBV",
    "BAC", "WMT", "CRM", "ORCL", "CVX", "MRK", "CSCO", "ACN", "LIN", "TMO",
    "MCD", "NEE",
)


def _search_tokens(raw_query: str) -> tuple[str, list[str], list[str]]:
    low = str(raw_query or "").strip().lower()
    toks = [t for t in re.split(r"\s+", low) if t]
    toks_norm = [re.sub(r"[^a-z0-9]+", "", t) for t in toks if t]
    return low, toks, toks_norm


def _is_sec_like_query(low: str, toks: list[str]) -> bool:
    return any(t in _SEC_HINT_TOKENS for t in toks) or ("earnings release" in str(low or ""))


def _sec_query_parts(toks: list[str], toks_norm: list[str]) -> tuple[set[str], str]:
    requested_forms = {_SEC_FORM_ALIAS[t] for t in toks_norm if t in _SEC_FORM_ALIAS}
    company_terms = [
        t for t, tn in zip(toks, toks_norm) if tn and tn not in _SEC_SEARCH_WORDS and tn not in _SEC_FORM_ALIAS
    ]
    return requested_forms, " ".join(company_terms).strip()


def _resolve_company_tickers_for_search(company_query: str, limit: int = 8) -> list[str]:
    q = str(company_query or "").strip()
    if not q:
        return []
    out: list[str] = []
    try:
        crows = list_companies(query=q, page=1, page_size=max(1, min(24, int(limit))), scope="all", sort="mcap_desc").get("rows") or []
        for r in crows:
            tk = str(getattr(r, "ticker", "") or (r.get("ticker") if isinstance(r, dict) else "") or "").strip().upper()
            if tk and tk not in out:
                out.append(tk)
    except Exception:
        return []
    return out


def _parse_human_amount(raw: str, default: float = 0.0) -> float:
    s = str(raw or "").strip().lower().replace(",", "")
    if not s:
        return default
    mul = 1.0
    if s.endswith("k"):
        mul = 1_000.0
        s = s[:-1]
    elif s.endswith("m"):
        mul = 1_000_000.0
        s = s[:-1]
    elif s.endswith("b"):
        mul = 1_000_000_000.0
        s = s[:-1]
    try:
        return float(s) * mul
    except Exception:
        return default


_safe_ticker = normalize_ticker
_yf_symbol = yfinance_symbol


def _read_blue_chips_rows() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for r in list_blue_chips_rows(limit=5000):
        t = _safe_ticker(str(r.get("ticker") or ""))
        if not t:
            continue
        out.append(
            {
                "ticker": t,
                "added_at": str(r.get("added_at") or ""),
                "reason": str(r.get("reason") or ""),
            }
        )
    return out


def _company_name_map(tickers: list[str]) -> dict[str, str]:
    wanted = [_safe_ticker(t) for t in (tickers or []) if _safe_ticker(t)]
    out = lookup_company_name_map(wanted)
    missing = sorted({t for t in wanted if t and not str(out.get(t) or "").strip()})
    if not missing:
        return out

    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is not None:
            try:
                cur = con_pg.cursor()
                marks = ",".join("%s" for _ in missing)
                cur.execute(
                    f"SELECT UPPER(ticker), name FROM companies_core WHERE UPPER(ticker) IN ({marks})",
                    tuple(missing),
                )
                for r in cur.fetchall() or []:
                    tk = _safe_ticker(str(r[0] or ""))
                    nm = str(r[1] or "").strip()
                    if tk and nm:
                        out[tk] = nm
            except Exception:
                pass
            finally:
                con_pg.close()
    missing2 = sorted({t for t in wanted if t and not str(out.get(t) or "").strip()})
    if missing2:
        live = _live_company_name_map(missing2, limit=48)
        for tk, nm in live.items():
            if tk and nm:
                out[tk] = nm
    return out


def _cache_name_get(ticker: str) -> str:
    tk = _safe_ticker(ticker)
    if not tk:
        return ""
    now = time.time()
    with _NAME_FALLBACK_LOCK:
        row = _NAME_FALLBACK_CACHE.get(tk)
        if not row:
            return ""
        name, ts = row
        if now - float(ts) > _NAME_FALLBACK_TTL_SEC:
            _NAME_FALLBACK_CACHE.pop(tk, None)
            return ""
        return str(name or "").strip()


def _cache_name_put(ticker: str, name: str) -> None:
    tk = _safe_ticker(ticker)
    nm = str(name or "").strip()
    if not tk or not nm:
        return
    with _NAME_FALLBACK_LOCK:
        _NAME_FALLBACK_CACHE[tk] = (nm, time.time())
        if len(_NAME_FALLBACK_CACHE) > 2000:
            for k in sorted(_NAME_FALLBACK_CACHE.keys())[:200]:
                _NAME_FALLBACK_CACHE.pop(k, None)


def _fetch_company_name_live(ticker: str, key: str) -> tuple[str, str]:
    tk = _safe_ticker(ticker)
    if not tk:
        return "", ""
    cached = _cache_name_get(tk)
    if cached:
        return tk, cached

    if key:
        try:
            import certifi  # type: ignore
            import requests  # type: ignore

            r = requests.get(
                "https://finnhub.io/api/v1/stock/profile2",
                params={"symbol": tk, "token": key},
                timeout=(1.5, 2.5),
                verify=certifi.where(),
                headers={"Accept": "application/json", "User-Agent": "InvestorOS/1.0"},
            )
            if r.status_code == 200 and r.content:
                obj = r.json()
                if isinstance(obj, dict):
                    nm = str(obj.get("name") or "").strip()
                    if nm:
                        _cache_name_put(tk, nm)
                        return tk, nm
        except Exception:
            pass

    try:
        import yfinance as yf  # type: ignore

        info = yf.Ticker(_yf_symbol(tk)).info or {}
        nm = str(info.get("longName") or info.get("shortName") or "").strip()
        if nm:
            _cache_name_put(tk, nm)
            return tk, nm
    except Exception:
        pass
    return tk, ""


def _live_company_name_map(tickers: list[str], limit: int = 48) -> dict[str, str]:
    wanted = sorted({_safe_ticker(t) for t in (tickers or []) if _safe_ticker(t)})
    if not wanted:
        return {}
    lim = max(1, min(80, int(limit or 48)))
    wanted = wanted[:lim]
    out: dict[str, str] = {}
    miss: list[str] = []
    for tk in wanted:
        nm = _cache_name_get(tk)
        if nm:
            out[tk] = nm
        else:
            miss.append(tk)
    if not miss:
        return out

    key = str(finnhub_key() or "").strip()
    workers = max(1, min(8, len(miss)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_fetch_company_name_live, tk, key) for tk in miss]
        for f in concurrent.futures.as_completed(futs):
            try:
                tk, nm = f.result()
            except Exception:
                continue
            if tk and nm:
                out[tk] = nm
    return out


def _upsert_blue_chip(ticker: str, reason: str = "") -> bool:
    return upsert_blue_chip(_safe_ticker(ticker), reason=str(reason or ""))


def _remove_blue_chip(ticker: str) -> bool:
    return remove_blue_chip(_safe_ticker(ticker))


def _read_portfolio_file() -> list[dict[str, str]]:
    return read_portfolio_rows_state()


def _write_portfolio_file(rows: list[dict[str, str]]) -> None:
    write_portfolio_rows_state(rows)


def _read_watchlist_rows() -> list[dict[str, str]]:
    return read_watchlist_rows()


def _write_watchlist_file(rows: list[dict[str, str]]) -> None:
    write_watchlist_rows(rows)


def _last_quote_map(tickers: list[str]) -> dict[str, dict[str, float | None]]:
    if not tickers:
        return {}
    uniq: list[str] = []
    seen: set[str] = set()
    for t in tickers:
        tk = _safe_ticker(t)
        if tk and tk not in seen:
            seen.add(tk)
            uniq.append(tk)
    if not uniq:
        return {}

    cache = _load_universe_quotes_cache()
    out: dict[str, dict[str, float | None]] = {}
    now = time.time()
    stale_or_missing: list[str] = []
    for tk in uniq:
        rec = cache.get(tk, {})
        ts = _to_float(rec.get("ts"), 0.0)
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        px = _to_float((payload or {}).get("price"), 0.0)
        if px > 0:
            out[tk] = {
                "price": px,
                "day_pct": (
                    float((payload or {}).get("day_pct"))
                    if isinstance((payload or {}).get("day_pct"), (int, float))
                    else None
                ),
                "market_cap": (
                    float((payload or {}).get("market_cap"))
                    if isinstance((payload or {}).get("market_cap"), (int, float))
                    else None
                ),
            }
        if px <= 0 or (now - ts) > UNIVERSE_QUOTES_TTL_SEC:
            stale_or_missing.append(tk)

    # Stale-while-revalidate: serve cached values immediately, refresh in background.
    if stale_or_missing:
        if not out:
            # Cold cache bootstrap: fetch once synchronously to avoid blank first render.
            fresh_boot = _fetch_quote_map_sync(stale_or_missing)
            if fresh_boot:
                out.update(fresh_boot)
                with _UNIVERSE_QUOTES_LOCK:
                    cache2 = _load_universe_quotes_cache()
                    ts2 = time.time()
                    for tk, payload in fresh_boot.items():
                        cache2[str(tk or "").strip().upper()] = {"ts": ts2, "payload": payload}
                    _write_universe_quotes_cache(cache2)
        _refresh_universe_quotes_async(stale_or_missing)
    return out


def _fetch_quote_map_sync(tickers: list[str]) -> dict[str, dict[str, float | None]]:
    if not tickers:
        return {}
    out: dict[str, dict[str, float | None]] = {}
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return out
    uniq = []
    seen: set[str] = set()
    for t in tickers:
        tk = _safe_ticker(t)
        if tk and tk not in seen:
            seen.add(tk)
            uniq.append(tk)

    def _fetch_one(tk: str) -> tuple[str, dict[str, float | None]] | None:
        try:
            obj = yf.Ticker(_yf_symbol(tk))
            fi = (obj.fast_info or {})
            px = _to_float(fi.get("last_price"), 0.0)
            prev = _to_float(fi.get("previous_close"), 0.0)
            mcap = _to_float(fi.get("market_cap"), 0.0)
            if px <= 0:
                px = _to_float(fi.get("regular_market_price"), 0.0)
            if prev <= 0:
                prev = _to_float(fi.get("regular_market_previous_close"), 0.0)
            if px <= 0 or prev <= 0:
                info = (obj.info or {})
                if px <= 0:
                    px = _to_float(info.get("currentPrice"), 0.0)
                if px <= 0:
                    px = _to_float(info.get("regularMarketPrice"), 0.0)
                if prev <= 0:
                    prev = _to_float(info.get("previousClose"), 0.0)
                if prev <= 0:
                    prev = _to_float(info.get("regularMarketPreviousClose"), 0.0)
                if mcap <= 0:
                    mcap = _to_float(info.get("marketCap"), 0.0)
            if px <= 0 or prev <= 0:
                hist = obj.history(period="5d", interval="1d")
                if not hist.empty:
                    closes = hist["Close"].dropna()
                    if px <= 0 and not closes.empty:
                        px = _to_float(closes.iloc[-1], 0.0)
                    if prev <= 0 and len(closes) >= 2:
                        prev = _to_float(closes.iloc[-2], 0.0)
            day_pct: float | None = None
            if px > 0 and prev > 0:
                day_pct = ((px - prev) / prev) * 100.0
            if px > 0:
                return tk, {"price": px, "day_pct": day_pct, "market_cap": (mcap if mcap > 0 else None)}
        except Exception:
            return None
        return None

    max_workers = min(12, max(1, len(uniq)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_fetch_one, tk) for tk in uniq]
        for fut in concurrent.futures.as_completed(futures):
            try:
                rec = fut.result()
                if rec:
                    tk, payload = rec
                    out[tk] = payload
            except Exception:
                continue
    return out


def _load_universe_quotes_cache() -> dict[str, dict[str, object]]:
    if not UNIVERSE_QUOTES_CACHE_PATH.exists():
        return {}
    try:
        raw = json.loads(UNIVERSE_QUOTES_CACHE_PATH.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _write_universe_quotes_cache(data: dict[str, dict[str, object]]) -> None:
    try:
        UNIVERSE_QUOTES_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        UNIVERSE_QUOTES_CACHE_PATH.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
    except Exception:
        pass


def _refresh_universe_quotes_async(tickers: list[str]) -> None:
    global _UNIVERSE_QUOTES_REFRESHING, _UNIVERSE_QUOTES_LAST_REFRESH_TS
    now = time.time()
    with _UNIVERSE_QUOTES_REFRESH_LOCK:
        if _UNIVERSE_QUOTES_REFRESHING:
            return
        if (now - _UNIVERSE_QUOTES_LAST_REFRESH_TS) < UNIVERSE_QUOTES_REFRESH_MIN_INTERVAL_SEC:
            return
        _UNIVERSE_QUOTES_REFRESHING = True
        _UNIVERSE_QUOTES_LAST_REFRESH_TS = now

    def _job() -> None:
        global _UNIVERSE_QUOTES_REFRESHING
        try:
            fresh = _fetch_quote_map_sync(tickers)
            if not fresh:
                return
            with _UNIVERSE_QUOTES_LOCK:
                cache = _load_universe_quotes_cache()
                ts = time.time()
                for tk, payload in fresh.items():
                    cache[str(tk or "").strip().upper()] = {"ts": ts, "payload": payload}
                _write_universe_quotes_cache(cache)
        finally:
            with _UNIVERSE_QUOTES_REFRESH_LOCK:
                _UNIVERSE_QUOTES_REFRESHING = False

    th = threading.Thread(target=_job, daemon=True)
    th.start()


def _last_price_map(tickers: list[str]) -> dict[str, float]:
    quotes = _last_quote_map(tickers)
    out: dict[str, float] = {}
    for t, q in quotes.items():
        px = _to_float(q.get("price"), 0.0)
        if px > 0:
            out[t] = px
    return out


def _last_day_pct_map(tickers: list[str]) -> dict[str, float]:
    quotes = _last_quote_map(tickers)
    out: dict[str, float] = {}
    for t, q in quotes.items():
        d = q.get("day_pct")
        if isinstance(d, (int, float)):
            out[t] = float(d)
    return out


def _since_added_pct_map(added_at_by_ticker: dict[str, str], current_price_by_ticker: dict[str, float]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    if not added_at_by_ticker:
        return out
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return out

    def _one(tk: str, added_raw: str) -> tuple[str, float | None]:
        cur = _to_float(current_price_by_ticker.get(tk), 0.0)
        if cur <= 0:
            return tk, None
        dt_added = parse_datetime_flexible(str(added_raw or ""))
        if dt_added is None:
            return tk, None
        start = (dt_added - dt.timedelta(days=7)).date().isoformat()
        try:
            obj = yf.Ticker(_yf_symbol(tk))
            hist = obj.history(start=start, interval="1d")
            if hist is None or hist.empty:
                return tk, None
            closes = hist["Close"].dropna()
            if closes.empty:
                return tk, None
            try:
                closes = closes[closes.index >= dt_added]
            except Exception:
                pass
            if closes.empty:
                return tk, None
            base = _to_float(closes.iloc[0], 0.0)
            if base <= 0:
                return tk, None
            return tk, ((cur - base) / base) * 100.0
        except Exception:
            return tk, None

    pairs = [(str(t or "").strip().upper(), str(a or "")) for t, a in (added_at_by_ticker or {}).items() if str(t or "").strip()]
    if not pairs:
        return out
    max_workers = min(12, max(1, len(pairs)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_one, tk, ad) for tk, ad in pairs]
        for fut in concurrent.futures.as_completed(futures):
            try:
                tk, pct = fut.result()
                out[tk] = pct
            except Exception:
                continue
    return out


def _pick_day_pct(live_val: object, fallback_val: object) -> float | None:
    if isinstance(live_val, (int, float)):
        return float(live_val)
    if isinstance(fallback_val, (int, float)):
        return float(fallback_val)
    return None


def _cash_usd_total() -> tuple[float, list[str]]:
    rows = _read_cash_rows()
    if not rows:
        return 0.0, []
    lines: list[str] = []
    total = 0.0
    for row in rows:
        ccy = str(row.get("currency") or "").strip().upper()
        amt = _to_float(row.get("amount"), 0.0)
        usd = amt
        if ccy == "EUR":
            usd = amt * 1.09
        elif ccy == "GBP":
            usd = amt * 1.27
        elif ccy in {"USD", ""}:
            usd = amt
        total += usd
        lines.append(f"{ccy or 'USD'} {amt:,.2f} (USD {usd:,.2f})")
    return total, lines


def _read_cash_rows() -> list[dict[str, str]]:
    return read_cash_rows_state()


def _write_cash_rows(rows: list[dict[str, str]]) -> None:
    write_cash_rows_state(rows)


def _fmt_price(v: float | None, decimals: int = 2) -> str:
    if not isinstance(v, (int, float)):
        return "-"
    return f"{float(v):,.{int(decimals)}f}"


def _fmt_day(v: float | None) -> str:
    if not isinstance(v, (int, float)):
        return "-"
    return f"{float(v):+.2f}%"


def _load_market_cache() -> dict[str, object]:
    if not MARKET_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(MARKET_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_market_cache(payload: dict[str, object]) -> None:
    try:
        MARKET_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        MARKET_CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
    except Exception:
        return


def _market_brief(home: dict) -> dict[str, object]:
    specs = [
        {"id": "sp500", "label": "S&P 500", "symbol": "^GSPC", "decimals": 2},
        {"id": "nasdaq", "label": "Nasdaq", "symbol": "^IXIC", "decimals": 2},
        {"id": "dow", "label": "Dow", "symbol": "^DJI", "decimals": 2},
        {"id": "russell2000", "label": "Russell 2000", "symbol": "^RUT", "decimals": 2},
        {"id": "us2y", "label": "US 2Y", "symbol": "^IRX", "decimals": 2, "divide_by_10": True},
        {"id": "us5y", "label": "US 5Y", "symbol": "^FVX", "decimals": 2, "divide_by_10": True},
        {"id": "us10y", "label": "US 10Y", "symbol": "^TNX", "decimals": 2, "divide_by_10": True},
        {"id": "us30y", "label": "US 30Y", "symbol": "^TYX", "decimals": 2, "divide_by_10": True},
        {"id": "de10y", "label": "German 10Y", "symbol": "^DE10Y", "decimals": 2, "divide_by_10": True},
        {"id": "jp10y", "label": "Japan 10Y", "symbol": "^JP10Y", "decimals": 2, "divide_by_10": True},
        {"id": "vix", "label": "VIX", "symbol": "^VIX", "decimals": 2},
        {"id": "gold", "label": "Gold", "symbol": "GC=F", "decimals": 2},
        {"id": "silver", "label": "Silver", "symbol": "SI=F", "decimals": 2},
        {"id": "platinum", "label": "Platinum", "symbol": "PL=F", "decimals": 2},
        {"id": "palladium", "label": "Palladium", "symbol": "PA=F", "decimals": 2},
        {"id": "crude", "label": "Crude Oil", "symbol": "CL=F", "decimals": 2},
        {"id": "brent", "label": "Brent Oil", "symbol": "BZ=F", "decimals": 2},
        {"id": "natgas", "label": "Nat Gas", "symbol": "NG=F", "decimals": 2},
        {"id": "copper", "label": "Copper", "symbol": "HG=F", "decimals": 2},
        {"id": "aluminum", "label": "Aluminum", "symbol": "ALI=F", "decimals": 2},
        {"id": "heatoil", "label": "Heating Oil", "symbol": "HO=F", "decimals": 2},
        {"id": "gasoline", "label": "Gasoline", "symbol": "RB=F", "decimals": 2},
        {"id": "corn", "label": "Corn", "symbol": "ZC=F", "decimals": 2},
        {"id": "wheat", "label": "Wheat", "symbol": "ZW=F", "decimals": 2},
        {"id": "soybeans", "label": "Soybeans", "symbol": "ZS=F", "decimals": 2},
        {"id": "ironore", "label": "Iron Ore", "symbol": "TIO=F", "decimals": 2},
        {"id": "lumber", "label": "Lumber", "symbol": "LBS=F", "decimals": 2},
        {"id": "cotton", "label": "Cotton", "symbol": "CT=F", "decimals": 2},
        {"id": "sugar", "label": "Sugar", "symbol": "SB=F", "decimals": 2},
        {"id": "coffee", "label": "Coffee", "symbol": "KC=F", "decimals": 2},
        {"id": "cocoa", "label": "Cocoa", "symbol": "CC=F", "decimals": 2},
        {"id": "dxy", "label": "US Dollar Index", "symbol": "DX-Y.NYB", "decimals": 2},
        {"id": "eurusd", "label": "EUR/USD", "symbol": "EURUSD=X", "decimals": 4},
        {"id": "eurgbp", "label": "EUR/GBP", "symbol": "EURGBP=X", "decimals": 4},
        {"id": "usdjpy", "label": "USD/JPY", "symbol": "JPY=X", "decimals": 3},
        {"id": "gbpusd", "label": "GBP/USD", "symbol": "GBPUSD=X", "decimals": 4},
        {"id": "usdcnh", "label": "USD/CNH", "symbol": "CNH=X", "decimals": 4},
    ]

    pulse_items = ((home.get("pulse") or {}).get("items") or {}) if isinstance(home, dict) else {}
    cache = _load_market_cache()
    cache_items = cache.get("items", {}) if isinstance(cache, dict) else {}

    def from_home(label: str) -> tuple[str, str]:
        v = pulse_items.get(label, {}) if isinstance(pulse_items, dict) else {}
        return str(v.get("price") or "-"), str(v.get("day") or "-")

    items: dict[str, dict[str, object]] = {}
    for s in specs:
        hp, hd = from_home(str(s["label"]))
        cp, cd = "-", "-"
        if isinstance(cache_items, dict):
            cv = cache_items.get(str(s["id"]), {})
            if isinstance(cv, dict):
                cp = str(cv.get("price") or "-")
                cd = str(cv.get("day") or "-")
        items[str(s["id"])] = {
            "label": s["label"],
            "price": cp if cp != "-" else hp,
            "day": cd if cd != "-" else hd,
            "source": "cache/home",
        }

    cache_fresh = False
    try:
        updated_s = str(cache.get("updated_at") or "").strip() if isinstance(cache, dict) else ""
        if updated_s:
            updated_dt = dt.datetime.strptime(updated_s, "%Y-%m-%d %H:%M:%S")
            age = max(0.0, (dt.datetime.now() - updated_dt).total_seconds())
            cache_fresh = age <= float(MARKET_BRIEF_CACHE_TTL_SEC)
    except Exception:
        cache_fresh = False

    if cache_fresh:
        return {
            "as_of": str(cache.get("updated_at") or dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            "items": items,
            "live_hits": 0,
        }

    live_hits = 0
    try:
        import yfinance as yf  # type: ignore

        def _fetch_live_row(spec: dict[str, object]) -> tuple[str, str, str, str]:
            sid = str(spec["id"])
            sym = str(spec["symbol"])
            dec = int(spec.get("decimals", 2))
            div10 = bool(spec.get("divide_by_10"))
            try:
                obj = yf.Ticker(sym)
                fi = (obj.fast_info or {})
                px = _to_float(fi.get("last_price"), 0.0)
                prev = _to_float(fi.get("previous_close"), 0.0)
                if px <= 0:
                    px = _to_float(fi.get("regular_market_price"), 0.0)
                if prev <= 0:
                    prev = _to_float(fi.get("regular_market_previous_close"), 0.0)
                if px <= 0 or prev <= 0:
                    info = (obj.info or {})
                    if px <= 0:
                        px = _to_float(info.get("currentPrice"), 0.0)
                    if px <= 0:
                        px = _to_float(info.get("regularMarketPrice"), 0.0)
                    if prev <= 0:
                        prev = _to_float(info.get("previousClose"), 0.0)
                    if prev <= 0:
                        prev = _to_float(info.get("regularMarketPreviousClose"), 0.0)
                if px <= 0 or prev <= 0:
                    hist = obj.history(period="5d", interval="1d")
                    if not hist.empty:
                        closes = hist["Close"].dropna()
                        if px <= 0 and not closes.empty:
                            px = _to_float(closes.iloc[-1], 0.0)
                        if prev <= 0 and len(closes) >= 2:
                            prev = _to_float(closes.iloc[-2], 0.0)

                if div10:
                    # ^TNX can come either as 40.6 (needs /10) or 4.06 (already normalized).
                    if px > 20:
                        px = px / 10.0
                    if prev > 20:
                        prev = prev / 10.0

                if px > 0:
                    day = ((px - prev) / prev * 100.0) if prev > 0 else None
                    return sid, str(spec["label"]), _fmt_price(px, dec), _fmt_day(day)
            except Exception:
                pass
            return sid, str(spec["label"]), "-", "-"

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            for sid, label, price, day in ex.map(_fetch_live_row, specs):
                if price == "-":
                    continue
                items[sid] = {
                    "label": label,
                    "price": price,
                    "day": day if day != "-" else str((items.get(sid) or {}).get("day") or "-"),
                    "source": "live",
                }
                live_hits += 1
    except Exception:
        pass

    if live_hits > 0:
        _save_market_cache(
            {
                "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "items": {k: {"price": v.get("price"), "day": v.get("day")} for k, v in items.items()},
            }
        )

    return {
        "as_of": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "items": items,
        "live_hits": live_hits,
    }


def _latest_report_path(prefixes: tuple[str, ...]) -> Path | None:
    if not REPORTS_DIR.exists():
        return None
    cands: list[Path] = []
    for pfx in prefixes:
        cands.extend(REPORTS_DIR.glob(f"{pfx}*"))
    cands = [p for p in cands if p.is_file()]
    if not cands:
        return None
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0]


def _read_text_file(path: Path | None, max_chars: int = 300_000) -> str:
    if not path or not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
    except Exception:
        return ""


def _extract_morning_points(txt: str, limit: int = 6) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for ln in str(txt or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith(("- ", "* ")):
            s = s[2:].strip()
        elif re.match(r"^\d+\.\s+", s):
            s = re.sub(r"^\d+\.\s+", "", s)
        else:
            continue
        s = re.sub(r"^\*\*([^*]+)\*\*:\s*", r"\1: ", s)
        s = re.sub(r"\s+", " ", s).strip()
        if len(s) < 18:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s[:260])
        if len(out) >= max(1, int(limit)):
            break
    return out


def _extract_section_table_rows(txt: str, heading_re: str, row_limit: int = 12) -> list[list[str]]:
    lines = str(txt or "").splitlines()
    start = -1
    pat = re.compile(heading_re, flags=re.I)
    for i, ln in enumerate(lines):
        if pat.search(ln):
            start = i
            break
    if start < 0:
        return []
    rows: list[list[str]] = []
    in_table = False
    for ln in lines[start + 1 :]:
        s = ln.strip()
        if s.startswith("### ") and rows:
            break
        if s.startswith("|"):
            in_table = True
            if re.match(r"^\|\s*-", s):
                continue
            parts = [c.strip() for c in s.strip("|").split("|")]
            if parts and parts[0].lower() in {"ticker", "date", "symbol"}:
                continue
            if parts:
                rows.append(parts)
                if len(rows) >= max(1, int(row_limit)):
                    break
            continue
        if in_table and not s:
            break
    return rows


def _is_stale(path: Path | None, max_age_hours: int = 36) -> bool:
    if not path or not path.exists():
        return True
    try:
        age_sec = time.time() - float(path.stat().st_mtime)
        return age_sec > (max(1, int(max_age_hours)) * 3600)
    except Exception:
        return True


def _extract_earnings_week_rows(txt: str, today: dt.date, row_limit: int = 12) -> list[dict[str, str]]:
    lines = str(txt or "").splitlines()
    if not lines:
        return []
    in_earn = False
    current_day: dt.date | None = None
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    # "This Week" should mean calendar week window (Mon-Sun), not "from today onward".
    week_start = today - dt.timedelta(days=today.weekday())
    week_end = week_start + dt.timedelta(days=6)

    for ln in lines:
        s = ln.strip()
        if not in_earn:
            if re.match(r"^##\s+EARNINGS CALENDAR", s, flags=re.I):
                in_earn = True
            continue
        if s.startswith("## ") and not re.match(r"^##\s+EARNINGS CALENDAR", s, flags=re.I):
            break

        # Example: "### Wednesday, February 18 — 25 companies"
        mday = re.match(r"^###\s+[A-Za-z]+,\s+([A-Za-z]+)\s+(\d{1,2})\b", s)
        if mday:
            mon = str(mday.group(1) or "").strip()
            day_num = int(mday.group(2) or "0")
            try:
                base = dt.datetime.strptime(f"{today.year} {mon} {day_num}", "%Y %B %d").date()
                # Handle year roll boundary.
                if (base - today).days > 300:
                    base = dt.date(today.year - 1, base.month, base.day)
                elif (today - base).days > 300:
                    base = dt.date(today.year + 1, base.month, base.day)
                current_day = base
            except Exception:
                current_day = None
            continue

        if not s.startswith("|"):
            continue
        if re.match(r"^\|\s*-", s):
            continue
        parts = [c.strip() for c in s.strip("|").split("|")]
        if not parts:
            continue
        h0 = parts[0].lower()
        if h0 in {"date", "symbol", "ticker"}:
            continue

        date_s = ""
        sym = ""
        comp = ""
        tm = ""
        if re.match(r"^\d{4}-\d{2}-\d{2}$", parts[0]):
            date_s = parts[0]
            sym = parts[1] if len(parts) > 1 else ""
            comp = parts[2] if len(parts) > 2 else ""
            tm = parts[3] if len(parts) > 3 else ""
        elif current_day is not None:
            # Day subsection rows: Symbol | Company | Time | ...
            date_s = current_day.isoformat()
            sym = parts[0]
            comp = parts[1] if len(parts) > 1 else ""
            tm = parts[2] if len(parts) > 2 else ""
        else:
            continue

        sym = _safe_ticker(sym)
        if not sym or not date_s:
            continue
        try:
            dd = dt.datetime.strptime(date_s, "%Y-%m-%d").date()
        except Exception:
            continue
        if dd < week_start or dd > week_end:
            continue
        key = (date_s, sym)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "date": date_s,
                "symbol": sym,
                "company": comp or "-",
                "time": tm or "-",
            }
        )
        if len(out) >= max(1, int(row_limit)):
            break
    return out


def _fmt_eps(v: object) -> str:
    try:
        if v is None:
            return "-"
        n = float(v)
        if abs(n) < 0.000001:
            return "0.00"
        return f"{n:.2f}"
    except Exception:
        return "-"


def _enrich_earnings_with_reported_status(rows: list[dict[str, str]], today: dt.date) -> list[dict[str, str]]:
    if not rows:
        return rows
    tickers = sorted({str(r.get("symbol") or "").strip().upper() for r in rows if str(r.get("symbol") or "").strip()})
    if not tickers:
        return rows
    cache_key = json.dumps(
        {
            "tickers": tickers,
            "rows": [(str(r.get("date") or ""), str(r.get("symbol") or "")) for r in rows],
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    now_ts = time.time()
    def _cache_and_return(base_rows: list[dict[str, str]]) -> list[dict[str, str]]:
        try:
            _EARNINGS_ENRICH_CACHE["ts"] = now_ts
            _EARNINGS_ENRICH_CACHE["key"] = cache_key
            _EARNINGS_ENRICH_CACHE["rows"] = [dict(x) for x in base_rows]
        except Exception:
            pass
        return base_rows
    try:
        if (
            str(_EARNINGS_ENRICH_CACHE.get("key") or "") == cache_key
            and (now_ts - float(_EARNINGS_ENRICH_CACHE.get("ts") or 0.0)) <= EARNINGS_ENRICH_TTL_SEC
        ):
            cached_rows = list(_EARNINGS_ENRICH_CACHE.get("rows") or [])
            if cached_rows:
                return [dict(x) for x in cached_rows if isinstance(x, dict)]
    except Exception:
        pass
    key = finnhub_key()
    if not key:
        return rows
    try:
        min_d = min(dt.datetime.strptime(str(r.get("date") or ""), "%Y-%m-%d").date() for r in rows if str(r.get("date") or ""))
        max_d = max(dt.datetime.strptime(str(r.get("date") or ""), "%Y-%m-%d").date() for r in rows if str(r.get("date") or ""))
    except Exception:
        min_d = today - dt.timedelta(days=2)
        max_d = today + dt.timedelta(days=1)
    # Catch late-report windows: include one week lookback from panel min date.
    date_from = (min(min_d, today) - dt.timedelta(days=7)).isoformat()
    date_to = (max(max_d, today) + dt.timedelta(days=1)).isoformat()

    reported: dict[tuple[str, str], dict[str, object]] = {}
    try:
        import certifi  # type: ignore
        import requests  # type: ignore

        r = requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={"from": date_from, "to": date_to, "token": key},
            timeout=(2.0, 4.0),
            verify=certifi.where(),
            headers={"Accept": "application/json", "User-Agent": "InvestorOS/1.0"},
        )
        if r.status_code != 200:
            return [dict(x) for x in rows]
        cal = r.json() or {}
        events = cal.get("earningsCalendar") if isinstance(cal, dict) else []
        if not isinstance(events, list):
            events = []
        ticker_set = set(tickers)
        for ev in events:
            if not isinstance(ev, dict):
                continue
            sym = _safe_ticker(str(ev.get("symbol") or ""))
            d = str(ev.get("date") or "").strip()
            if not sym or not d or sym not in ticker_set:
                continue
            a = ev.get("epsActual")
            e = ev.get("epsEstimate")
            if a is None or e is None:
                continue
            try:
                af = float(a)
                ef = float(e)
            except Exception:
                continue
            surprise = None
            try:
                if ef != 0:
                    surprise = ((af - ef) / abs(ef)) * 100.0
            except Exception:
                surprise = None
            verdict = "BEAT" if af >= ef else "MISS"
            reported[(d, sym)] = {
                "verdict": verdict,
                "surprise": surprise,
                "eps_actual": af,
                "eps_estimate": ef,
                "source": "Finnhub Earnings Calendar",
            }
    except Exception:
        # Do not cache transport failures; allow fast retry on next request.
        return [dict(x) for x in rows]

    out: list[dict[str, str]] = []
    for row in rows:
        rr = dict(row)
        sym = _safe_ticker(str(rr.get("symbol") or ""))
        d = str(rr.get("date") or "").strip()
        rep = reported.get((d, sym))
        rr["reported"] = "0"
        rr["verdict"] = ""
        rr["surprise_txt"] = ""
        rr["eps_actual"] = "-"
        rr["eps_estimate"] = "-"
        rr["result_source"] = ""
        rr["event_status"] = "upcoming"
        if rep:
            rr["reported"] = "1"
            rr["verdict"] = str(rep.get("verdict") or "")
            sp = rep.get("surprise")
            rr["surprise_txt"] = f"{float(sp):+.1f}%" if isinstance(sp, (int, float)) else ""
            rr["eps_actual"] = _fmt_eps(rep.get("eps_actual"))
            rr["eps_estimate"] = _fmt_eps(rep.get("eps_estimate"))
            rr["result_source"] = str(rep.get("source") or "")
            rr["confidence"] = "preliminary"
            rr["event_status"] = "reported"
        else:
            # If event date is already in the past and we still have no feed result,
            # avoid showing it as "upcoming" forever.
            try:
                ed = dt.datetime.strptime(d, "%Y-%m-%d").date()
                if ed < today:
                    rr["event_status"] = "reported"
                    rr["confidence"] = "preliminary"
            except Exception:
                pass
        out.append(rr)
    final_rows = _verify_reported_earnings_with_sec(out)
    return _cache_and_return([dict(x) for x in final_rows])


def _finnhub_earnings_week_rows(today: dt.date, row_limit: int = 120) -> list[dict[str, str]]:
    key = finnhub_key()
    if not key:
        return []
    lim = max(1, min(500, int(row_limit)))
    week_start = today - dt.timedelta(days=today.weekday())
    week_end = week_start + dt.timedelta(days=6)
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    try:
        import certifi  # type: ignore
        import requests  # type: ignore

        r = requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={"from": week_start.isoformat(), "to": week_end.isoformat(), "token": key},
            timeout=(2.0, 4.0),
            verify=certifi.where(),
            headers={"Accept": "application/json", "User-Agent": "InvestorOS/1.0"},
        )
        if r.status_code != 200:
            return []
        cal = r.json() if r.content else {}
        events = cal.get("earningsCalendar") if isinstance(cal, dict) else []
        if not isinstance(events, list):
            return []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            sym = _safe_ticker(str(ev.get("symbol") or ""))
            d = str(ev.get("date") or "").strip()
            if not sym or not d:
                continue
            if (d, sym) in seen:
                continue
            seen.add((d, sym))
            tm = str(ev.get("hour") or ev.get("time") or "-").strip() or "-"
            comp = str(ev.get("company") or ev.get("name") or "-").strip() or "-"
            out.append(
                {
                    "date": d,
                    "symbol": sym,
                    "company": comp,
                    "time": tm,
                }
            )
            if len(out) >= lim:
                break
    except Exception:
        return []
    out.sort(key=lambda x: (str(x.get("date") or ""), str(x.get("symbol") or "")))
    return out


def _verify_reported_earnings_with_sec(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    if not rows:
        return rows
    # Verify reported earnings against local official SEC filings coverage.
    forms = {"8-K", "6-K", "10-Q", "10-K", "20-F", "40-F"}
    tickers = sorted({str(r.get("symbol") or "").strip().upper() for r in rows if str(r.get("symbol") or "").strip()})
    if not tickers:
        return rows
    filing_map: dict[tuple[str, str], list[tuple[dt.date, str]]] = {}
    used_pg = False
    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is None and strict_postgres_mode():
            return rows
        if con_pg is not None:
            try:
                marks = ",".join("%s" for _ in tickers)
                q = (
                    f"SELECT ticker, form, date FROM filings_core "
                    f"WHERE ticker IN ({marks}) AND date IS NOT NULL AND date != '' "
                    f"ORDER BY date DESC LIMIT 5000"
                )
                cur = con_pg.cursor()
                cur.execute(q, tuple(tickers))
                db_rows = cur.fetchall() or []
                for r in db_rows:
                    tk = _safe_ticker(str(r[0] or ""))
                    fm = str(r[1] or "").strip().upper()
                    ds = str(r[2] or "").strip()
                    if not tk or not fm or fm not in forms or not ds:
                        continue
                    try:
                        fd = dt.datetime.strptime(ds, "%Y-%m-%d").date()
                    except Exception:
                        continue
                    filing_map.setdefault((tk, fm), []).append((fd, ds))
                used_pg = True
            except Exception:
                if strict_postgres_mode():
                    return rows
            finally:
                con_pg.close()
    if not used_pg:
        return rows

    out: list[dict[str, str]] = []
    for rr in rows:
        row = dict(rr)
        if str(row.get("reported") or "0") != "1":
            out.append(row)
            continue
        tk = _safe_ticker(str(row.get("symbol") or ""))
        ds = str(row.get("date") or "").strip()
        verified = False
        form_used = ""
        date_used = ""
        try:
            ed = dt.datetime.strptime(ds, "%Y-%m-%d").date()
        except Exception:
            ed = None
        if tk and ed is not None:
            for fm in ("8-K", "6-K", "10-Q", "10-K", "20-F", "40-F"):
                rows_f = filing_map.get((tk, fm), [])
                hit = next((x for x in rows_f if abs((x[0] - ed).days) <= 3), None)
                if hit:
                    verified = True
                    form_used = fm
                    date_used = hit[1]
                    break
        row["confidence"] = "verified" if verified else "preliminary"
        row["verify_source"] = "SEC filings"
        row["verify_form"] = form_used
        row["verify_date"] = date_used
        out.append(row)
    return out


def _clean_low_ticker(raw: str) -> tuple[str, bool]:
    s = str(raw or "").strip()
    had_at_low = bool(re.search(r"\bAT\s*LOW\b", s, flags=re.I))
    s = s.replace("**", " ")
    s = re.sub(r"\bAT\s*LOW\b", " ", s, flags=re.I)
    s = re.sub(r"[^A-Za-z0-9.\- ]", " ", s)
    toks = [t for t in s.split() if t]
    ticker = ""
    for t in toks:
        tt = _safe_ticker(t)
        if tt:
            ticker = tt
            break
    if not ticker:
        ticker = _safe_ticker(raw)
    return ticker, had_at_low


def dashboard_report_panels() -> dict[str, object]:
    today = dt.date.today()

    morning_path = _latest_report_path(("terminal_daily_brief_", "morning_intelligence_", "daily_brief_"))
    appendix_path = _latest_report_path(("terminal_appendix_",))
    earnings_path = _latest_report_path(("earnings_radar_",))

    morning_txt = _read_text_file(morning_path)
    appendix_txt = _read_text_file(appendix_path)
    earnings_txt = _read_text_file(earnings_path)

    morning_points: list[str] = []
    morning_updated_fallback = "-"
    # Cloud/Postgres-first source of truth: morning_briefs_core.
    # File parsing is retained only as fallback for local/offline workflows.
    try:
        cached = get_cached_morning_brief()
        b = [str(x or "").strip() for x in list((cached or {}).get("bullets") or []) if str(x or "").strip()]
        if b:
            morning_points = b[:7]
            asof = str((cached or {}).get("asof") or "").strip()
            if asof:
                try:
                    morning_updated_fallback = dt.datetime.fromisoformat(asof).strftime("%H:%M")
                except Exception:
                    morning_updated_fallback = asof[:5] if len(asof) >= 5 else asof
    except Exception:
        pass
    if not morning_points:
        morning_points = _extract_morning_points(morning_txt, limit=7)

    both_rows = _extract_section_table_rows(
        appendix_txt,
        r"AT BOTH 52-WEEK AND (5-YEAR|ALL-TIME) LOWS",
        row_limit=10,
    )
    near_rows = _extract_section_table_rows(
        appendix_txt,
        r"NEAR 52-WEEK LOWS ONLY",
        row_limit=10,
    )

    lows_both: list[dict[str, str]] = []
    for r in both_rows:
        if len(r) < 6:
            continue
        tk, had_low = _clean_low_ticker(r[0])
        above_low = str(r[4] or "").strip()
        is_at_low = had_low or above_low in {"0", "0.0", "0.0%", "0.00", "0.00%"}
        lows_both.append(
            {
                "ticker": tk,
                "current": r[1],
                "above_low": above_low,
                "from_high": r[5],
                "is_at_low": is_at_low,
            }
        )

    lows_52: list[dict[str, str]] = []
    for r in near_rows:
        if len(r) < 6:
            continue
        tk, had_low = _clean_low_ticker(r[0])
        above_low = str(r[4] or "").strip()
        is_at_low = had_low or above_low in {"0", "0.0", "0.0%", "0.00", "0.00%"}
        lows_52.append(
            {
                "ticker": tk,
                "current": r[1],
                "above_low": above_low,
                "from_high": r[5],
                "is_at_low": is_at_low,
            }
        )

    # Enrich low-list rows with company names for better scanability in UI.
    low_tickers = [str(x.get("ticker") or "").strip().upper() for x in (lows_both + lows_52)]
    name_map = _company_name_map(low_tickers)
    for row in lows_both:
        tk = str(row.get("ticker") or "").strip().upper()
        row["company"] = str(name_map.get(tk) or "").strip()
    for row in lows_52:
        tk = str(row.get("ticker") or "").strip().upper()
        row["company"] = str(name_map.get(tk) or "").strip()

    # Prefer fresh appendix when earnings_radar is stale.
    earnings_primary = appendix_txt if _is_stale(earnings_path, max_age_hours=36) and appendix_txt else (earnings_txt or appendix_txt)
    earnings_week = _extract_earnings_week_rows(earnings_primary, today=today, row_limit=120)
    if not earnings_week and earnings_primary is not appendix_txt:
        earnings_week = _extract_earnings_week_rows(appendix_txt, today=today, row_limit=120)
    # If report parsing yields nothing, fallback to live Finnhub calendar.
    if not earnings_week:
        earnings_week = _finnhub_earnings_week_rows(today=today, row_limit=120)
    # Durable fallback from Postgres snapshot (stale-while-revalidate behavior).
    if not earnings_week and core_backend() == "postgres":
        ws = (today - dt.timedelta(days=today.weekday())).isoformat()
        we = (today - dt.timedelta(days=today.weekday()) + dt.timedelta(days=6)).isoformat()
        earnings_week = list_earnings_calendar_snapshot_pg(ws, we, limit=200)
    earnings_week = _enrich_earnings_with_reported_status(earnings_week, today=today)
    # Fill missing company names from local profile cache so UI does not show "-" for valid tickers.
    earnings_tickers = [str((r or {}).get("symbol") or "").strip().upper() for r in (earnings_week or [])]
    earnings_name_map = _company_name_map(earnings_tickers)
    for row in earnings_week:
        sym = str((row or {}).get("symbol") or "").strip().upper()
        cur_company = str((row or {}).get("company") or "").strip()
        if sym and (not cur_company or cur_company == "-"):
            nm = str(earnings_name_map.get(sym) or "").strip()
            if nm:
                row["company"] = nm
                row["name"] = nm
    def _time_rank(v: str) -> int:
        s = str(v or "").strip().lower()
        if "pre" in s or "bmo" in s:
            return 0
        if "day" in s:
            return 1
        if "post" in s or "after" in s or "amc" in s:
            return 2
        return 3
    earnings_week = sorted(
        list(earnings_week or []),
        key=lambda r: (
            str((r or {}).get("date") or "9999-99-99"),
            _time_rank(str((r or {}).get("time") or "")),
            0 if str((r or {}).get("reported") or "0") == "1" else 1,
            str((r or {}).get("symbol") or ""),
        ),
    )
    if earnings_week and core_backend() == "postgres":
        try:
            upsert_earnings_calendar_snapshot_pg(earnings_week)
        except Exception:
            pass
    reported_count = sum(1 for r in earnings_week if str((r or {}).get("reported") or "0") == "1")
    upcoming_count = max(0, len(earnings_week) - reported_count)

    def _ts(p: Path | None) -> str:
        if not p or not p.exists():
            return "-"
        try:
            return dt.datetime.fromtimestamp(p.stat().st_mtime).strftime("%H:%M")
        except Exception:
            return "-"

    return {
        "morning_source": morning_path.name if morning_path else "-",
        "appendix_source": appendix_path.name if appendix_path else "-",
        "earnings_source": earnings_path.name if earnings_path else (appendix_path.name if appendix_path else "-"),
        "morning_updated": (_ts(morning_path) if morning_path else morning_updated_fallback),
        "appendix_updated": _ts(appendix_path),
        "earnings_updated": _ts(earnings_path or appendix_path),
        "earnings_result_source": "Finnhub Earnings Calendar + SEC filing verification",
        "earnings_rows_count": len(earnings_week),
        "earnings_reported_count": reported_count,
        "earnings_upcoming_count": upcoming_count,
        "morning_points": morning_points,
        "lows_both": lows_both,
        "lows_52": lows_52,
        "earnings_week": earnings_week,
    }


def _universe_reports(home: dict, metrics: dict[str, object]) -> tuple[list[str], list[str]]:
    portfolio = list(home.get("portfolio", []) or [])
    watchlist = list(home.get("watchlist", []) or [])
    pr: list[str] = []
    wr: list[str] = []

    if portfolio:
        top = sorted((metrics.get("portfolio_rows") or []), key=lambda x: float(x.get("weight_pct") or 0.0), reverse=True)[:3]
        if top:
            pr.append(
                "Top concentration: "
                + ", ".join(
                    f"{str(r.get('ticker') or '-')} {float(r.get('weight_pct') or 0.0):.1f}%"
                    for r in top
                )
            )
        dlist = [r for r in (metrics.get("portfolio_rows") or []) if isinstance(r.get("day_pct"), (int, float))]
        if dlist:
            best = max(dlist, key=lambda x: float(x.get("day_pct") or 0.0))
            worst = min(dlist, key=lambda x: float(x.get("day_pct") or 0.0))
            pr.append(
                f"Daily move dispersion: best {best.get('ticker')} {float(best.get('day_pct') or 0.0):+.2f}% | "
                f"worst {worst.get('ticker')} {float(worst.get('day_pct') or 0.0):+.2f}%."
            )
        sec_missing = [r for r in portfolio if int(r.get("filings") or 0) == 0]
        if sec_missing:
            pr.append("SEC coverage gaps in portfolio: " + ", ".join(str(r.get("ticker") or "-") for r in sec_missing[:8]) + ".")
        if not pr:
            pr.append("Portfolio loaded. Add more positions to improve intelligence depth.")
    else:
        pr.append("No portfolio holdings yet. Add ticker + shares + average cost to start tracking AUM.")

    if watchlist:
        movers = [r for r in watchlist if isinstance(r.get("day_pct"), (int, float))]
        if movers:
            movers.sort(key=lambda x: abs(float(x.get("day_pct") or 0.0)), reverse=True)
            topm = movers[:5]
            wr.append(
                "Highest watchlist volatility: "
                + ", ".join(
                    f"{str(r.get('ticker') or '-')} {float(r.get('day_pct') or 0.0):+.2f}%"
                    for r in topm
                )
                + "."
            )
        sec_ready = [r for r in watchlist if int(r.get("filings") or 0) > 0]
        sec_missing = [r for r in watchlist if int(r.get("filings") or 0) == 0]
        wr.append(f"SEC-ready watchlist names: {len(sec_ready)}/{len(watchlist)}.")
        if sec_missing:
            wr.append("Need SEC sync: " + ", ".join(str(r.get("ticker") or "-") for r in sec_missing[:10]) + ".")
    else:
        wr.append("No watchlist names yet. Add companies to begin screening.")
    return pr, wr


def _watchlist_opportunities(home: dict, limit: int = 8) -> list[dict[str, object]]:
    rows = list(home.get("watchlist", []) or [])
    out: list[dict[str, object]] = []
    for r in rows:
        d = r.get("day_pct")
        if not isinstance(d, (int, float)):
            continue
        out.append(
            {
                "ticker": str(r.get("ticker") or "").strip().upper(),
                "name": str(r.get("name") or "").strip(),
                "day_pct": float(d or 0.0),
                "industry": str(r.get("industry") or "").strip(),
                "market_cap": str(r.get("market_cap") or "-"),
            }
        )
    out.sort(key=lambda x: abs(float(x.get("day_pct") or 0.0)), reverse=True)
    return out[: max(1, int(limit))]


def _my_companies_metrics(home: dict) -> dict[str, object]:
    portfolio = list(home.get("portfolio", []) or [])
    if not portfolio:
        return {
            "portfolio_rows": [],
            "stock_value_usd": 0.0,
            "cash_value_usd": 0.0,
            "aum_usd": 0.0,
            "cash_drag_pct": 0.0,
            "top3": [],
            "sector_exposure": [],
            "industry_exposure": [],
            "country_exposure": [],
            "quick_analysis": ["No portfolio holdings yet."],
            "intel24": [],
            "cash_lines": [],
        }

    tks = [_safe_ticker(r.get("ticker", "")) for r in portfolio]
    quote_map = _last_quote_map(tks)

    stock_value = 0.0
    stock_prev_close_value = 0.0
    stock_day_pnl = 0.0
    enriched: list[dict[str, object]] = []
    for r in portfolio:
        t = _safe_ticker(r.get("ticker", ""))
        sh = _to_float(r.get("shares"), 0.0)
        cost = _to_float(r.get("cost"), 0.0)
        q = quote_map.get(t, {})
        fallback_px = _to_float(r.get("price_live"), 0.0) or _to_float(r.get("price_now"), 0.0) or _to_float(r.get("price"), 0.0)
        live_px = _to_float(q.get("price"), 0.0) or fallback_px
        # For portfolio day P/L, use live quote day change only.
        # Do not fall back to cached/stale snapshot values.
        day_pct_live = _pick_day_pct(q.get("day_pct"), r.get("day_pct"))
        px = live_px or cost
        val = max(0.0, sh * px)
        pnl = sh * (px - cost) if sh > 0 and px > 0 and cost > 0 else 0.0
        d_pct = _to_float(day_pct_live, 0.0)
        prev_val = val / (1.0 + (d_pct / 100.0)) if abs(1.0 + (d_pct / 100.0)) > 1e-9 else val
        day_pnl = val - prev_val
        stock_value += val
        stock_prev_close_value += prev_val
        stock_day_pnl += day_pnl
        enriched.append(
            {
                **r,
                "ticker": t,
                "shares_num": sh,
                "cost_num": cost,
                "price_now": px,
                "price_live": live_px,
                "day_pct": day_pct_live,
                "value_usd": val,
                "pnl_usd": pnl,
                "day_pnl_usd": day_pnl,
                "prev_close_value_usd": prev_val,
            }
        )

    cash_usd, cash_lines = _cash_usd_total()
    aum = stock_value + cash_usd
    aum_prev_close = stock_prev_close_value + cash_usd
    for r in enriched:
        r["weight_pct"] = (float(r["value_usd"]) / aum * 100.0) if aum > 0 else 0.0

    def _exposure(key: str) -> list[dict[str, object]]:
        agg: dict[str, float] = {}
        for r in enriched:
            k = str(r.get(key) or "Unknown").strip() or "Unknown"
            agg[k] = agg.get(k, 0.0) + float(r.get("value_usd") or 0.0)
        out = [{"label": k, "pct": (v / stock_value * 100.0) if stock_value > 0 else 0.0} for k, v in agg.items()]
        out.sort(key=lambda x: float(x["pct"]), reverse=True)
        return out[:8]

    top3 = sorted(enriched, key=lambda x: float(x.get("weight_pct") or 0.0), reverse=True)[:3]

    quick: list[str] = []
    if top3:
        quick.append(f"Concentration: {top3[0]['ticker']} is {float(top3[0]['weight_pct']):.1f}% of AUM.")
    sectors = {str(r.get("industry") or "Unknown") for r in enriched}
    quick.append(f"Diversification: {len(sectors)} industry group(s) represented.")
    dlist = [r for r in enriched if isinstance(r.get("day_pct"), (int, float))]
    if dlist:
        best = sorted(dlist, key=lambda x: float(x.get("day_pct") or 0.0), reverse=True)[0]
        worst = sorted(dlist, key=lambda x: float(x.get("day_pct") or 0.0))[0]
        quick.append(
            f"Daily dispersion: best {best['ticker']} {float(best.get('day_pct') or 0.0):+.2f}%, "
            f"worst {worst['ticker']} {float(worst.get('day_pct') or 0.0):+.2f}%."
        )

    # 24h intelligence rows from intel24 snapshot.
    intel24: list[dict[str, object]] = []
    tickers = [t for t in tks if t]
    if tickers:
        if core_backend() == "postgres":
            con_pg = pg_connect()
            if con_pg is not None:
                try:
                    cur = con_pg.cursor()
                    cur.execute(
                        """
                        SELECT ticker, day_pct, insider_txt, sec_txt, happened, suggestion
                        FROM intel24_snapshot_core
                        WHERE ticker = ANY(%s)
                        ORDER BY ABS(COALESCE(day_pct,0)) DESC
                        """,
                        (tickers,),
                    )
                    for r in cur.fetchall() or []:
                        intel24.append(
                            {
                                "ticker": str(r[0] or "").strip().upper(),
                                "day_pct": float(r[1] or 0.0),
                                "insider_txt": str(r[2] or "-"),
                                "sec_txt": str(r[3] or "-"),
                                "happened": str(r[4] or "-"),
                                "suggestion": str(r[5] or "-"),
                            }
                        )
                finally:
                    con_pg.close()
        elif not strict_postgres_mode():
            con = get_sqlite_conn(str(CORE_DB_PATH), row_factory=True)
            try:
                marks = ",".join("?" for _ in tickers)
                rows = con.execute(
                    f"""
                    SELECT ticker, day_pct, insider_txt, sec_txt, happened, suggestion
                    FROM intel24_snapshot
                    WHERE ticker IN ({marks})
                    ORDER BY ABS(COALESCE(day_pct,0)) DESC
                    """,
                    tuple(tickers),
                ).fetchall()
                intel24 = [
                    {
                        "ticker": str(r["ticker"] or "").strip().upper(),
                        "day_pct": float(r["day_pct"] or 0.0),
                        "insider_txt": str(r["insider_txt"] or "-"),
                        "sec_txt": str(r["sec_txt"] or "-"),
                        "happened": str(r["happened"] or "-"),
                        "suggestion": str(r["suggestion"] or "-"),
                    }
                    for r in rows
                ]
            finally:
                con.close()

    return {
        "portfolio_rows": enriched,
        "stock_value_usd": stock_value,
        "stock_prev_close_usd": stock_prev_close_value,
        "stock_day_pnl_usd": stock_day_pnl,
        "stock_day_pct": (stock_day_pnl / stock_prev_close_value * 100.0) if stock_prev_close_value > 0 else 0.0,
        "cash_value_usd": cash_usd,
        "aum_usd": aum,
        "aum_prev_close_usd": aum_prev_close,
        "aum_day_pnl_usd": stock_day_pnl,
        "aum_day_pct": (stock_day_pnl / aum_prev_close * 100.0) if aum_prev_close > 0 else 0.0,
        "cash_drag_pct": (cash_usd / aum * 100.0) if aum > 0 else 0.0,
        "top3": top3,
        "sector_exposure": _exposure("industry"),
        "industry_exposure": _exposure("industry"),
        "country_exposure": _exposure("country"),
        "quick_analysis": quick,
        "intel24": intel24,
        "cash_lines": cash_lines,
    }


def _is_hx(request: Request) -> bool:
    return str(request.headers.get("HX-Request") or "").strip().lower() == "true"


def _safe_monitor_info() -> dict[str, object]:
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(run_event_driven_monitor, False)
        out = dict(fut.result(timeout=MONITOR_RENDER_TIMEOUT_SEC) or {})
        ex.shutdown(wait=False, cancel_futures=True)
        return out
    except concurrent.futures.TimeoutError:
        ex.shutdown(wait=False, cancel_futures=True)
        return {"ok": True, "timed_out": True, "note": "monitor deferred"}
    except Exception as exc:
        ex.shutdown(wait=False, cancel_futures=True)
        return {"ok": False, "error": str(exc)}


def _render(
    request: Request,
    message: str = "",
    ask_q: str = "",
    ask_a: str = "",
    portfolio_intel: str = "",
):
    templates = request.app.state.templates
    monitor_info = _safe_monitor_info()
    # Show newest proposals first on dashboard so fresh filing cards are visible immediately.
    proposals = list_action_proposals(status="open", limit=60)
    proposals = sorted(
        proposals,
        key=lambda p: int(p.get("id") or 0),
        reverse=True,
    )[:10]
    risk_veto = list_recent_risk_veto_decisions(limit=8)
    risk_veto_config = get_risk_veto_config()
    agent_runs = list_recent_agent_runs(limit=8)
    reflexions = list_recent_reflexions(limit=8)
    policy_versions = list((reflexions or {}).get("policy_versions") or [])
    active_policy = next((x for x in policy_versions if int(x.get("is_active") or 0) == 1), {})
    snap = dashboard_snapshot()
    home = snap.get("home", {}) or {}
    market = _market_brief(home)
    report_panels = dashboard_report_panels()
    thesis_breach_alerts = list_thesis_breach_alerts(status="open", limit=10)
    cascade_alerts = list_cascade_alerts(status="open", limit=10)
    ai_accuracy = get_ai_accuracy_stats(days=90)
    ai_audit_timeline = get_ai_audit_timeline(limit=30)
    from app.services.earnings_transcript_service import list_earnings_analysis
    earnings_analyses = list_earnings_analysis(limit=10)
    tpl = "components/dashboard_body.html" if _is_hx(request) else "dashboard.html"
    return templates.TemplateResponse(
        tpl,
        {
            "request": request,
            "message": message,
            "counts": snap.get("counts", {}),
            "movers_up": snap.get("movers_up", []),
            "movers_down": snap.get("movers_down", []),
            "feed": snap.get("feed", []),
            "freshness": snap.get("freshness", {}),
            "signals": snap.get("signals", {}),
            "signal_items": (snap.get("signals", {}) or {}).get("items", []),
            "ask_q": ask_q,
            "ask_a": ask_a,
            "home": home,
            "market": market,
            "report_panels": report_panels,
            "my_metrics": _my_companies_metrics(home),
            "portfolio_intel": portfolio_intel,
            "action_proposals": proposals,
            "risk_veto_decisions": risk_veto,
            "risk_veto_config": risk_veto_config,
            "monitor_info": monitor_info,
            "agent_runs": agent_runs,
            "reflexions": reflexions,
            "active_reflexion_policy": active_policy,
            "thesis_breach_alerts": thesis_breach_alerts,
            "cascade_alerts": cascade_alerts,
            "ai_accuracy": ai_accuracy,
            "ai_audit_timeline": ai_audit_timeline,
            "earnings_analyses": earnings_analyses,
        },
    )


def _desk_parse(raw: str) -> tuple[str, str]:
    s = str(raw or "").strip()
    if not s:
        return "note", ""
    low = s.lower()
    if low.startswith("/ask "):
        return "ask", s[5:].strip()
    if low.startswith("ask:"):
        return "ask", s[4:].strip()
    if low.startswith("/task "):
        return "task", s[6:].strip()
    if low.startswith("task:"):
        return "task", s[5:].strip()
    if low.startswith("todo:"):
        return "task", s[5:].strip()
    if re.match(r".+\?$", s):
        return "ask", s
    return "note", s


def _contains_fuzzy_term(text: str, term: str, min_ratio: float = 0.78) -> bool:
    low = str(text or "").strip().lower()
    t = str(term or "").strip().lower()
    if not low or not t:
        return False
    if t in low:
        return True
    if t == "watchlist" and "watch list" in low:
        return True
    toks = re.findall(r"[a-z]+", low)
    for tok in toks:
        if difflib.SequenceMatcher(None, tok, t).ratio() >= min_ratio:
            return True
    return False


def _has_add_intent(text: str) -> bool:
    low = str(text or "").strip().lower()
    if re.search(r"\b(add|set|update|top\s*up|increase|put|buy|allocate|fund)\b", low):
        return True
    toks = re.findall(r"[a-z]+", low)
    add_words = ("add", "set", "update", "buy")
    for tok in toks:
        if any(difflib.SequenceMatcher(None, tok, w).ratio() >= 0.8 for w in add_words):
            return True
    return False


def _has_remove_intent(text: str) -> bool:
    low = str(text or "").strip().lower()
    if re.search(r"\b(remove|delete|drop|sell|close)\b", low):
        return True
    toks = re.findall(r"[a-z]+", low)
    rem_words = ("remove", "delete", "drop", "sell")
    for tok in toks:
        if any(difflib.SequenceMatcher(None, tok, w).ratio() >= 0.8 for w in rem_words):
            return True
    return False


def _parse_ai_command(raw: str) -> dict[str, str] | None:
    s = str(raw or "").strip()
    if not s:
        return None
    low = re.sub(r"\s+", " ", s.lower()).strip()

    # Intent-first parsing so similar phrasings still work.
    has_add = _has_add_intent(low)
    has_remove = _has_remove_intent(low)
    has_watchlist = _contains_fuzzy_term(low, "watchlist")
    has_portfolio = _contains_fuzzy_term(low, "portfolio")
    has_cash = _contains_fuzzy_term(low, "cash")
    has_balance = _contains_fuzzy_term(low, "balance")

    # Cash (flexible)
    if has_cash:
        c_match = re.search(r"\b(usd|eur|gbp)\b", low)
        cur = str(c_match.group(1) if c_match else "USD").upper()
        a_match = re.search(r"\b(\d+(?:[.,]\d+)?\s*[kKmMbB]?)\b", s)
        amt = _parse_human_amount(str(a_match.group(1) if a_match else ""), 0.0)
        if has_remove:
            return {"mode": "cash_remove", "currency": cur}
        if has_add and amt > 0:
            return {"mode": "cash_upsert", "currency": cur, "amount": f"{amt:g}"}
    # Cash (implicit, no explicit word "cash")
    # Examples:
    # - add 10k usd to balance
    # - set usd 5000 balance
    if has_balance:
        c_match = re.search(r"\b(usd|eur|gbp)\b", low)
        cur = str(c_match.group(1) if c_match else "USD").upper()
        a_match = re.search(r"\b(\d+(?:[.,]\d+)?\s*[kKmMbB]?)\b", s)
        amt = _parse_human_amount(str(a_match.group(1) if a_match else ""), 0.0)
        if has_remove:
            return {"mode": "cash_remove", "currency": cur}
        if has_add and amt > 0:
            return {"mode": "cash_upsert", "currency": cur, "amount": f"{amt:g}"}

    # Watchlist (flexible)
    if has_watchlist:
        if has_remove:
            ticker = _infer_ticker(s)
            if ticker:
                return {"mode": "watchlist_remove", "text": "", "ticker": ticker}
        if has_add:
            ticker = _infer_ticker(s)
            if ticker:
                return {"mode": "watchlist_add", "text": "", "ticker": ticker}
        ticker = _infer_ticker(s)
        if ticker:
            return {"mode": "watchlist_add", "text": "", "ticker": ticker}

    # Portfolio (flexible)
    if has_portfolio:
        ticker = _infer_ticker(s)
        if has_remove and ticker:
            return {"mode": "portfolio_remove", "text": "", "ticker": ticker, "shares": "0", "cost": "0"}
        if has_add and ticker:
            m_sh = re.search(r"(\d+(?:\.\d+)?)\s*shares?\b", low, flags=re.I)
            m_cost = re.search(r"(?:at|cost|price)\s*\$?\s*(\d+(?:\.\d+)?)", low, flags=re.I)
            m_any = re.search(r"\b(\d+(?:\.\d+)?)\b", low)
            shares = str(m_sh.group(1) if m_sh else (m_any.group(1) if m_any else "1"))
            cost = str(m_cost.group(1) if m_cost else "0")
            return {"mode": "portfolio_upsert", "text": "", "ticker": ticker, "shares": shares, "cost": cost}

    # Portfolio shorthand without explicit "portfolio":
    # - add CRM 45 at 185
    # - buy UNH 20 at 490.5
    # - add CRM 45 shares 185
    m_port_short = re.match(
        r"^\s*(?:please\s+)?(?:add|buy|set|update)\s+(?P<t>[A-Za-z.\-]{1,12})\s+(?P<sh>\d+(?:\.\d+)?)\s*(?:shares?)?\s*(?:at|@|price|cost)?\s*\$?\s*(?P<px>\d+(?:\.\d+)?)(?:\b.*)?$",
        s,
        flags=re.I,
    )
    if m_port_short:
        tk_guess = _safe_ticker(m_port_short.group("t") or "")
        tk = _infer_ticker(tk_guess) or tk_guess
        if tk:
            return {
                "mode": "portfolio_upsert",
                "text": "",
                "ticker": tk,
                "shares": str(m_port_short.group("sh") or "0"),
                "cost": str(m_port_short.group("px") or "0"),
            }

    # Two-number fallback with clear buy/add intent:
    # - add salesforce 45 at 185
    # - buy hubspot 10 650
    if has_add:
        tk = _infer_ticker(s)
        if tk:
            nums = re.findall(r"\b(\d+(?:\.\d+)?)\b", low)
            if len(nums) >= 2:
                return {"mode": "portfolio_upsert", "text": "", "ticker": tk, "shares": nums[0], "cost": nums[1]}

    # Cash commands:
    # - add 10k usd cash
    # - add usd 10000 cash
    # - set cash usd 5000
    m_cash_add = re.match(
        r"^\s*(?:please\s+)?(?:add|set|update)\s+(?:(?P<a1>[\d.,]+[kKmMbB]?)\s*(?P<c1>[A-Za-z]{3})|(?P<c2>[A-Za-z]{3})\s*(?P<a2>[\d.,]+[kKmMbB]?))\s+cash(?:\b.*)?$",
        s,
        flags=re.I,
    )
    if not m_cash_add:
        m_cash_add = re.match(
            r"^\s*(?:please\s+)?(?:add|set|update)\s+(?P<a4>[\d.,]+[kKmMbB]?)\s+(?:to\s+)?(?:my\s+|the\s+)?cash(?:\s+balance)?(?:\s+in\s+(?P<c4>[A-Za-z]{3}))?(?:\b.*)?$",
            s,
            flags=re.I,
        )
    if not m_cash_add:
        m_cash_add = re.match(
            r"^\s*(?:please\s+)?(?:add|set|update)\s+cash\s+(?P<c3>[A-Za-z]{3})\s*(?P<a3>[\d.,]+[kKmMbB]?)(?:\b.*)?$",
            s,
            flags=re.I,
        )
    if m_cash_add:
        cur = str(
            m_cash_add.groupdict().get("c1")
            or m_cash_add.groupdict().get("c2")
            or m_cash_add.groupdict().get("c3")
            or m_cash_add.groupdict().get("c4")
            or "USD"
        ).strip().upper()
        amt_txt = str(
            m_cash_add.groupdict().get("a1")
            or m_cash_add.groupdict().get("a2")
            or m_cash_add.groupdict().get("a3")
            or m_cash_add.groupdict().get("a4")
            or ""
        ).strip()
        amt = _parse_human_amount(amt_txt, 0.0)
        if amt <= 0:
            return None
        return {"mode": "cash_upsert", "currency": cur, "amount": f"{amt:g}"}

    m_cash_remove = re.match(
        r"^\s*(?:please\s+)?remove\s+(?:(?P<c1>[A-Za-z]{3})\s+)?cash(?:\s+(?P<c2>[A-Za-z]{3}))?(?:\b.*)?$",
        s,
        flags=re.I,
    )
    if m_cash_remove:
        cur = str(m_cash_remove.group("c1") or m_cash_remove.group("c2") or "USD").strip().upper()
        return {"mode": "cash_remove", "currency": cur}

    m_watch_remove = re.match(
        r"^\s*(?:please\s+)?remove\s+(?P<target>.+?)\s+from\s+(?:my\s+|the\s+)?watchlist(?:\b.*)?$",
        s,
        flags=re.I,
    )
    if m_watch_remove:
        target = str(m_watch_remove.group("target") or "").strip()
        ticker = _infer_ticker(target or s)
        if not ticker:
            return None
        return {"mode": "watchlist_remove", "text": "", "ticker": ticker}

    m_watch = re.match(
        r"^\s*(?:please\s+)?add\s+(?P<target>.+?)\s+to\s+(?:my\s+|the\s+)?watchlist(?:\b.*)?$",
        s,
        flags=re.I,
    )
    if m_watch:
        target = str(m_watch.group("target") or "").strip()
        ticker = _infer_ticker(target or s)
        if not ticker:
            return None
        return {"mode": "watchlist_add", "text": "", "ticker": ticker}

    m_port_remove = re.match(
        r"^\s*(?:please\s+)?remove\s+(?P<target>.+?)\s+from\s+(?:my\s+|the\s+)?portfolio(?:\b.*)?$",
        s,
        flags=re.I,
    )
    if m_port_remove:
        target = str(m_port_remove.group("target") or "").strip()
        ticker = _infer_ticker(target or s)
        if not ticker:
            return None
        return {"mode": "portfolio_remove", "text": "", "ticker": ticker, "shares": "0", "cost": "0"}

    m_port = re.match(
        r"^\s*(?:please\s+)?(?:add|update|set)\s+(?P<target>.+?)\s+to\s+(?:my\s+|the\s+)?portfolio(?:\s+(?P<rest>.*))?$",
        s,
        flags=re.I,
    )
    if m_port:
        target = str(m_port.group("target") or "").strip()
        ticker = _infer_ticker(target or s)
        if not ticker:
            return None
        rest = str(m_port.group("rest") or "").strip()
        shares = ""
        cost = ""
        m_sh = re.search(r"(\d+(?:\.\d+)?)\s*shares?\b", rest, flags=re.I)
        if m_sh:
            shares = str(m_sh.group(1) or "").strip()
        m_cost = re.search(r"(?:at|cost|price)\s*\$?\s*(\d+(?:\.\d+)?)", rest, flags=re.I)
        if m_cost:
            cost = str(m_cost.group(1) or "").strip()
        if not shares:
            m_num = re.search(r"\b(\d+(?:\.\d+)?)\b", rest)
            if m_num:
                shares = str(m_num.group(1) or "").strip()
        if not shares:
            shares = "1"
        if not cost:
            cost = "0"
        return {"mode": "portfolio_upsert", "text": "", "ticker": ticker, "shares": shares, "cost": cost}

    # Examples:
    # - add note for salesforce: check Q4 margins
    # - create task for hubspot - read earnings call
    # - save note: portfolio rebalance thought
    m = re.match(
        r"^\s*(?:please\s+)?(?:add|create|save|log|record|set)\s+(?:a\s+)?(?P<kind>note|task|todo|reminder)\b(?P<rest>.*)$",
        s,
        flags=re.I,
    )
    if not m:
        return None
    kind = str(m.group("kind") or "").strip().lower()
    if kind in {"task", "todo"}:
        mode = "task"
    elif kind == "reminder":
        mode = "reminder"
    else:
        mode = "note"
    rest = str(m.group("rest") or "").strip()
    if not rest:
        return None

    target = ""
    text = ""
    # target + payload separated with ":" or "-"
    m2 = re.match(r"^(?:for|about|on)\s+(?P<target>[^:\-]+?)\s*(?::|\-)\s*(?P<body>.+)$", rest, flags=re.I)
    if m2:
        target = str(m2.group("target") or "").strip()
        text = str(m2.group("body") or "").strip()
    else:
        # Natural style: "for hubspot to read their 10k"
        m2b = re.match(r"^(?:for|about|on)\s+(?P<target>[a-z0-9 .&'-]+?)\s+to\s+(?P<body>.+)$", rest, flags=re.I)
        if m2b:
            target = str(m2b.group("target") or "").strip()
            text = str(m2b.group("body") or "").strip()
        else:
        # payload-only command (no explicit target)
            m3 = re.match(r"^(?::|\-)\s*(?P<body>.+)$", rest, flags=re.I)
            if m3:
                text = str(m3.group("body") or "").strip()
            else:
                # Soft fallback: "for X <body...>" where body follows target words.
                m4 = re.match(r"^(?:for|about|on)\s+(?P<all>.+)$", rest, flags=re.I)
                if m4:
                    all_part = str(m4.group("all") or "").strip()
                    toks = all_part.split()
                    if len(toks) >= 4:
                        target = " ".join(toks[:2]).strip()
                        text = " ".join(toks[2:]).strip()
                    else:
                        target = all_part
                        text = ""
                else:
                    text = rest

    ticker = _infer_ticker(target or s)
    if not text:
        if ticker:
            default_name = "reminder" if mode == "reminder" else ("task" if mode == "task" else "note")
            text = f"{default_name} added via Ask AI"
        else:
            return None
    return {"mode": mode, "text": text, "ticker": ticker}


def _is_op_like(text: str) -> bool:
    s = str(text or "")
    low = s.lower()
    topic = (
        _contains_fuzzy_term(low, "cash")
        or _contains_fuzzy_term(low, "balance")
        or _contains_fuzzy_term(low, "watchlist")
        or _contains_fuzzy_term(low, "portfolio")
    )
    action = _has_add_intent(low) or _has_remove_intent(low)
    if topic and action:
        return True
    return bool(
        re.search(
            r"\b(add|remove|set|update|buy|sell|allocate|fund|delete|drop)\b.*\b(cash|balance|watchlist|portfolio)\b|\b(cash|balance|watchlist|portfolio)\b.*\b(add|remove|set|update|buy|sell|allocate|fund|delete|drop)\b",
            s,
            flags=re.I,
        )
    )


def _structured_capture_text(raw: str, mode: str, ticker: str = "") -> str:
    src = str(raw or "").strip()
    md = str(mode or "note").strip().lower()
    tk = _safe_ticker(ticker)
    if not src:
        return src

    # Compact pattern:
    # "hubspot | task | listen to CEO interview | why: ... | next: ... | priority: p1"
    parts = [p.strip() for p in src.split("|") if p.strip()]
    company = tk
    kind = "task" if md == "task" else "note"
    summary = ""
    why = ""
    next_step = ""
    priority = ""

    key_map = {
        "why": "why",
        "reason": "why",
        "next": "next",
        "next step": "next",
        "action": "next",
        "priority": "priority",
        "prio": "priority",
        "type": "type",
        "kind": "type",
        "summary": "summary",
    }

    def _set_kv(k: str, v: str) -> None:
        nonlocal kind, summary, why, next_step, priority
        kk = key_map.get(k.strip().lower(), "")
        vv = v.strip()
        if not kk:
            return
        if kk == "type" and vv:
            kind = vv.lower()
        elif kk == "summary" and vv:
            summary = vv
        elif kk == "why":
            why = vv
        elif kk == "next":
            next_step = vv
        elif kk == "priority":
            priority = vv.upper()

    if len(parts) >= 2:
        # First pass parse key:value parts.
        free: list[str] = []
        for p in parts:
            if ":" in p:
                k, v = p.split(":", 1)
                _set_kv(k, v)
            else:
                free.append(p)
        # Free segments: [company?], [type?], [summary?]
        if free:
            c0 = _infer_ticker(free[0])
            if c0:
                company = c0
                free = free[1:]
        if free:
            k0 = free[0].strip().lower()
            if k0 in {"task", "note", "idea", "risk", "question", "thesis"}:
                kind = k0
                free = free[1:]
        if free and not summary:
            summary = free[0].strip()
    else:
        # Natural text fallback: preserve original as summary only.
        summary = src

    if not company:
        company = _infer_ticker(src)
    if not summary:
        summary = src

    # Never invent missing fields. Keep blank markers when absent.
    lines = [
        "[Structured Note]",
        f"Company: {company or '-'}",
        f"Type: {kind or ('task' if md == 'task' else 'note')}",
        f"Summary: {summary or '-'}",
        f"Why: {why or '-'}",
        f"Next: {next_step or '-'}",
        f"Priority: {priority or '-'}",
    ]
    return "\n".join(lines)


def _execute_ai_command(cmd: dict[str, str]) -> tuple[bool, str]:
    mode = str(cmd.get("mode") or "").strip().lower()
    ticker = _safe_ticker(cmd.get("ticker", ""))
    if mode == "cash_upsert":
        ccy = str(cmd.get("currency") or "USD").strip().upper() or "USD"
        amt = _to_float(cmd.get("amount", ""), 0.0)
        rows = _read_cash_rows()
        rows = [r for r in rows if str(r.get("currency") or "").strip().upper() != ccy]
        if amt > 0:
            rows.append({"currency": ccy, "amount": f"{amt:g}"})
        _write_cash_rows(rows)
        return True, f"Cash balance updated: {ccy} {amt:,.2f}."
    if mode == "cash_remove":
        ccy = str(cmd.get("currency") or "USD").strip().upper() or "USD"
        rows = _read_cash_rows()
        rows = [r for r in rows if str(r.get("currency") or "").strip().upper() != ccy]
        _write_cash_rows(rows)
        return True, f"Cash balance removed: {ccy}."
    if mode == "watchlist_add":
        if not ticker:
            return False, "Could not detect ticker/company for watchlist add."
        rows = _read_watchlist_rows()
        rows = [r for r in rows if _safe_ticker(r.get("ticker", "")) != ticker]
        reason = "Added via Ask AI"
        rows.append(
            {
                "ticker": ticker,
                "added_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "reason": reason,
            }
        )
        _write_watchlist_file(rows)
        # Verify-after-write
        chk = _read_watchlist_rows()
        if not any(_safe_ticker(x.get("ticker", "")) == ticker for x in chk):
            return False, f"Write verify failed: {ticker} was not saved to watchlist."
        record_decision(action="watchlist_add", ticker=ticker, reason=reason, confidence=0.9, source="dashboard_ai")
        upsert_watchlist_thesis(ticker=ticker, thesis=reason, pick_method="ask_ai", status="active")
        return True, "Watchlist updated."
    if mode == "watchlist_remove":
        if not ticker:
            return False, "Could not detect ticker/company for watchlist remove."
        rows = _read_watchlist_rows()
        rows = [r for r in rows if _safe_ticker(r.get("ticker", "")) != ticker]
        _write_watchlist_file(rows)
        # Verify-after-write
        chk = _read_watchlist_rows()
        if any(_safe_ticker(x.get("ticker", "")) == ticker for x in chk):
            return False, f"Write verify failed: {ticker} still exists in watchlist."
        record_decision(action="watchlist_remove", ticker=ticker, reason="Removed via Ask AI", confidence=0.9, source="dashboard_ai")
        return True, "Removed from watchlist."
    if mode == "portfolio_remove":
        if not ticker:
            return False, "Could not detect ticker/company for portfolio remove."
        rows = _read_portfolio_file()
        prev = next((r for r in rows if _safe_ticker(r.get("ticker", "")) == ticker), None)
        rows = [r for r in rows if _safe_ticker(r.get("ticker", "")) != ticker]
        _write_portfolio_file(rows)
        chk = _read_portfolio_file()
        if any(_safe_ticker(x.get("ticker", "")) == ticker for x in chk):
            return False, f"Write verify failed: {ticker} still exists in portfolio."
        record_portfolio_transaction(
            ticker=ticker,
            action="sell",
            shares=_to_float((prev or {}).get("shares", ""), 0.0),
            price=_to_float((prev or {}).get("cost", ""), 0.0),
            note="Removed from portfolio via Ask AI",
            source="dashboard_ai",
        )
        record_decision(action="portfolio_remove", ticker=ticker, reason="Removed from portfolio via Ask AI", confidence=0.92, source="dashboard_ai")
        return True, "Removed from portfolio."
    if mode == "portfolio_upsert":
        if not ticker:
            return False, "Could not detect ticker/company for portfolio update."
        shares = _to_float(cmd.get("shares", ""), 0.0)
        cost = _to_float(cmd.get("cost", ""), 0.0)
        if shares <= 0 or cost <= 0:
            return False, "For portfolio add/update include both shares and average price. Example: add crm to portfolio 120 shares at 240"
        rows = _read_portfolio_file()
        existing = None
        kept: list[dict[str, str]] = []
        for r in rows:
            if _safe_ticker(r.get("ticker", "")) == ticker and existing is None:
                existing = r
            else:
                kept.append(r)
        if existing:
            old_sh = _to_float(existing.get("shares", ""), 0.0)
            old_cost = _to_float(existing.get("cost", ""), 0.0)
            new_sh = old_sh + shares
            if new_sh <= 0:
                return False, "Resulting shares must be positive."
            # Weighted average cost across lots.
            new_cost = ((old_sh * old_cost) + (shares * cost)) / new_sh if old_sh > 0 else cost
            kept.append(
                {
                    "ticker": ticker,
                    "shares": f"{new_sh:g}",
                    "cost": f"{new_cost:.6f}",
                    "note": "Accumulated via Ask AI",
                }
            )
            rows = kept
            record_portfolio_transaction(
                ticker=ticker,
                action="buy",
                shares=shares,
                price=cost,
                note="Accumulated via Ask AI",
                source="dashboard_ai",
            )
            record_decision(action="portfolio_buy", ticker=ticker, reason="Position accumulated via Ask AI", confidence=0.92, source="dashboard_ai")
        else:
            kept.append(
                {
                    "ticker": ticker,
                    "shares": f"{shares:g}",
                    "cost": str(cost),
                    "note": "Added via Ask AI",
                }
            )
            rows = kept
            record_portfolio_transaction(
                ticker=ticker,
                action="buy",
                shares=shares,
                price=cost,
                note="Added via Ask AI",
                source="dashboard_ai",
            )
            record_decision(action="portfolio_buy", ticker=ticker, reason="New position via Ask AI", confidence=0.92, source="dashboard_ai")
        _write_portfolio_file(rows)
        chk = _read_portfolio_file()
        if not any(_safe_ticker(x.get("ticker", "")) == ticker for x in chk):
            return False, f"Write verify failed: {ticker} was not saved to portfolio."
        return True, "Portfolio updated (position accumulated)."
    if mode == "reminder":
        tk = str(cmd.get("ticker") or "").strip().upper()
        ok = add_company_reminder(ticker=tk, remind_at="", note=str(cmd.get("text") or "")) if tk else False
        return (ok, "Reminder added." if ok else "Could not add reminder. Include a company name/ticker.")
    raw_text = str(cmd.get("text") or "")
    tkr = str(cmd.get("ticker") or "")
    structured = _structured_capture_text(raw_text, mode=mode, ticker=tkr)
    ok, msg = quick_capture(mode=mode, text=structured, ticker=tkr)
    return ok, msg


def _infer_ticker(text: str, user_ticker: str = "") -> str:
    t = str(user_ticker or "").strip().upper()
    if t:
        return t
    q = str(text or "").strip()
    if not q:
        return ""

    # 1) Strong ticker patterns first.
    tokens: list[str] = []
    for m in re.findall(r"\$([A-Za-z]{1,6})\b|\b([A-Za-z]{2,5})\b", q):
        tk = str(m[0] or m[1] or "").strip().upper()
        if tk:
            tokens.append(tk)

    if core_backend() != "postgres":
        return ""
    con_pg = pg_connect()
    if con_pg is None:
        return ""
    try:
        cur = con_pg.cursor()
        for tk in tokens[:6]:
            cur.execute(
                "SELECT ticker FROM company_profile_cache_core WHERE ticker = %s LIMIT 1",
                (tk,),
            )
            row = cur.fetchone()
            if row:
                return str(row[0] or "").strip().upper()
        # 1b) Probable ticker fallback when DB cache doesn't contain it yet.
        stop_tickers = {
            "ADD",
            "SET",
            "PUT",
            "BUY",
            "SELL",
            "THE",
            "AND",
            "FOR",
            "FROM",
            "WITH",
            "TO",
            "MY",
            "YOUR",
            "USD",
            "EUR",
            "GBP",
            "PLS",
            "PLEASE",
            "CASH",
        }
        for tk in tokens[:8]:
            if tk in stop_tickers:
                continue
            if 3 <= len(tk) <= 5:
                return tk

        # 2) Fallback by company-name mention.
        low = q.lower()
        cur.execute(
            "SELECT ticker FROM company_profile_cache_core WHERE POSITION(lower(name) IN %s) > 0 ORDER BY LENGTH(name) DESC LIMIT 1",
            (low,),
        )
        row = cur.fetchone()
        if row:
            return str(row[0] or "").strip().upper()
        # 3) Token-based fallback for short company mentions (e.g., "salesforce", "hubspot").
        stop = {
            "please",
            "can",
            "could",
            "would",
            "you",
            "your",
            "my",
            "me",
            "into",
            "from",
            "to",
            "the",
            "a",
            "an",
            "now",
            "add",
            "create",
            "save",
            "log",
            "record",
            "note",
            "task",
            "todo",
            "for",
            "about",
            "on",
            "read",
            "check",
            "call",
            "transcript",
            "earnings",
        }
        words = [w for w in re.findall(r"[a-z][a-z0-9]{2,}", low) if w not in stop and len(w) >= 4]
        for w in words[:8]:
            cur.execute(
                "SELECT ticker FROM company_profile_cache_core WHERE POSITION(%s IN LOWER(name)) > 0 ORDER BY LENGTH(name) ASC LIMIT 1",
                (w,),
            )
            row = cur.fetchone()
            if row:
                return str(row[0] or "").strip().upper()
        return ""
    finally:
        con_pg.close()


@router.get("/")
@router.get("/dashboard")
def dashboard_page(request: Request):
    templates = request.app.state.templates
    channels = list_workspace_channels()
    return templates.TemplateResponse(
        "workspace_dashboard.html",
        {
            "request": request,
            "channels": channels,
            "active_channel": "all",
        },
    )


@router.get("/dashboard/classic")
def dashboard_page_classic(request: Request):
    return _render(request)


@router.get("/dashboard/feed")
def dashboard_workspace_feed(channel: str = "all", limit: int = 50):
    items = load_workspace_feed(channel=channel, limit=limit)
    return JSONResponse({"ok": True, "channel": str(channel or "all"), "items": items})


@router.post("/dashboard/message")
def dashboard_workspace_message(channel: str = Form("ai-agent"), text: str = Form("")):
    ch = str(channel or "ai-agent").strip().lower() or "ai-agent"
    prompt = str(text or "").strip()
    if not prompt:
        return JSONResponse({"ok": False, "error": "Message required."}, status_code=400)
    add_workspace_message(channel=ch, role="user", message=prompt)
    if ch != "ai-agent":
        return JSONResponse(
            {
                "ok": True,
                "channel": ch,
                "reply": f"Posted to #{ch}.",
                "ts": dt.datetime.now().isoformat(),
            }
        )
    reply = str(ask_workspace_ai(prompt) or "").strip() or "No response."
    add_workspace_message(channel="ai-agent", role="assistant", message=reply[:5000])
    return JSONResponse(
        {
            "ok": True,
            "channel": ch,
            "reply": reply[:5000],
            "ts": dt.datetime.now().isoformat(),
        }
    )


@router.post("/dashboard/ai-agent/save-note")
def dashboard_workspace_save_note(text: str = Form(""), ticker: str = Form("")):
    body = str(text or "").strip()
    tk = _safe_ticker(str(ticker or "").strip())
    if not body:
        return JSONResponse({"ok": False, "error": "missing_text"}, status_code=400)
    if not tk:
        tk = _infer_ticker(body)
    ok, msg = quick_capture(mode="note", text=body, ticker=tk)
    return JSONResponse(
        {
            "ok": bool(ok),
            "message": str(msg or ("Saved." if ok else "Could not save note.")),
            "ticker": tk,
        },
        status_code=200 if ok else 400,
    )


@router.post("/dashboard/quick-capture")
def dashboard_quick_capture(
    request: Request,
    mode: str = Form("note"),
    text: str = Form(""),
    ticker: str = Form(""),
):
    ok, msg = quick_capture(mode=mode, text=text, ticker=ticker)
    return _render(request, message=msg if ok else msg)


@router.post("/dashboard/ask")
def dashboard_ask(
    request: Request,
    question: str = Form(""),
):
    q = str(question or "").strip()
    cmd = _parse_ai_command(q)
    if cmd:
        ok, msg = _execute_ai_command(cmd)
        suffix = f" Linked: {cmd['ticker']}" if cmd.get("ticker") else ""
        return _render(request, message=(msg + suffix) if ok else msg, ask_q=q, ask_a="")
    if _is_op_like(q):
        return _render(
            request,
            message="Command not recognized. Try: add 10k usd cash, remove usd cash, add oxy to watchlist.",
            ask_q=q,
            ask_a="",
        )
    a = ask_ai_local(q) if q else ""
    msg = "" if q else "Ask a question first."
    return _render(request, message=msg, ask_q=q, ask_a=a)


@router.post("/dashboard/desk")
def dashboard_desk(
    request: Request,
    text: str = Form(""),
    intent: str = Form("auto"),
):
    try:
        mode, payload = _desk_parse(text)
        it = str(intent or "auto").strip().lower()
        if it in {"note", "task", "ask"}:
            mode = it
            payload = str(text or "").strip()
        if not payload:
            return _render(request, message="Type a note, /task ..., or /ask ...")
        # Intent-based first: execute command if detected regardless of selected mode.
        cmd = _parse_ai_command(payload)
        if cmd:
            ok, msg = _execute_ai_command(cmd)
            suffix = f" Linked: {cmd['ticker']}" if cmd.get("ticker") else ""
            return _render(request, message=(msg + suffix) if ok else msg, ask_q=payload, ask_a="")
        if mode == "ask":
            if _is_op_like(payload):
                return _render(
                    request,
                    message="Command not recognized. Try: add 10k usd cash, remove usd cash, add oxy to watchlist.",
                    ask_q=payload,
                    ask_a="",
                )
            a = ask_ai_local(payload)
            return _render(request, message="", ask_q=payload, ask_a=a)
        linked_ticker = _infer_ticker(payload)
        ok, msg = quick_capture(mode=mode, text=payload, ticker=linked_ticker)
        return _render(request, message=msg if ok else msg)
    except Exception as exc:
        return _render(request, message=f"Desk error: {exc}")


@router.post("/dashboard/portfolio-intelligence")
def dashboard_portfolio_intelligence(request: Request):
    try:
        snap = dashboard_snapshot()
        home = snap.get("home", {}) or {}
        metrics = _my_companies_metrics(home)
        brief = portfolio_intelligence_brief(home, metrics)
        return _render(request, message="Portfolio intelligence updated.", portfolio_intel=brief)
    except Exception as exc:
        return _render(request, message=f"Portfolio intelligence failed: {exc}")


@router.post("/dashboard/proposals/scan")
def dashboard_proposals_scan(request: Request):
    try:
        out = run_event_driven_monitor(force=True)
        created = int(out.get("created") or 0)
        return _render(request, message=f"Insight refresh complete. New insights: {created}.")
    except Exception as exc:
        return _render(request, message=f"Insight refresh failed: {exc}")


@router.post("/dashboard/proposals/{proposal_id}/dismiss")
def dashboard_proposals_dismiss(
    request: Request,
    proposal_id: int,
    reason: str = Form(""),
):
    ok = dismiss_action_proposal(proposal_id=proposal_id, reason=reason)
    if not ok:
        return _render(request, message="Could not dismiss insight.")
    return _render(request, message="Insight dismissed.")


@router.post("/dashboard/proposals/{proposal_id}/execute")
def dashboard_proposals_execute(request: Request, proposal_id: int):
    out = execute_action_proposal(proposal_id=proposal_id)
    if not bool(out.get("ok")):
        err = out.get("error") or "unknown_error"
        msg = out.get("message") or f"Could not execute proposal: {err}"
        wants_json = _is_hx(request) or "application/json" in str(request.headers.get("accept") or "").lower()
        if wants_json:
            return JSONResponse({"ok": False, "error": err, "message": msg}, status_code=400)
        if _is_hx(request):
            return JSONResponse({"ok": False, "error": err, "message": msg}, status_code=400)
        return _render(request, message=msg)
    route = str(out.get("route") or "/dashboard").strip()
    if not route.startswith("/"):
        route = "/dashboard"
    wants_json = _is_hx(request) or "application/json" in str(request.headers.get("accept") or "").lower()
    if wants_json:
        resp = JSONResponse({"ok": True, "route": route})
        resp.headers["HX-Redirect"] = route
        return resp
    return RedirectResponse(url=route, status_code=303)


@router.post("/api/proposals/{proposal_id}/reject")
async def api_reject_proposal(
    proposal_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
):
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    reason = str((payload or {}).get("reason") or "").strip()
    ok = reject_action_proposal(proposal_id=proposal_id, reason=reason)
    if not ok:
        return JSONResponse({"ok": False, "error": "proposal_not_found_or_not_updated"}, status_code=404)
    background_tasks.add_task(learn_from_rejection, proposal_id, reason)
    return JSONResponse({"ok": True, "status": "REJECTED", "proposal_id": int(proposal_id), "learning_queued": True})


@router.post("/dashboard/proposals/{proposal_id}/reject")
def dashboard_proposals_reject(
    request: Request,
    proposal_id: int,
    reason: str = Form(""),
):
    rs = str(reason or "").strip()
    ok = reject_action_proposal(proposal_id=proposal_id, reason=rs)
    wants_json = _is_hx(request) or "application/json" in str(request.headers.get("accept") or "").lower()
    if not ok:
        if wants_json:
            return JSONResponse({"ok": False, "error": "proposal_not_found_or_not_updated"}, status_code=404)
        return _render(request, message="Could not reject proposal.")
    try:
        learn_from_rejection(proposal_id=proposal_id, reason=rs)
    except Exception:
        pass
    if wants_json:
        return JSONResponse({"ok": True, "proposal_id": int(proposal_id), "status": "REJECTED"})
    return _render(request, message="Proposal rejected and preference learning saved.")


@router.post("/api/simulate-macro-shock")
async def api_simulate_macro_shock(request: Request):
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    event_description = str((payload or {}).get("event_description") or "").strip()
    if not event_description:
        return JSONResponse(
            {
                "answer": "No event description provided.",
                "confidence_score": 20,
                "missing_variables": ["event_description"],
                "citations": [],
                "impacts": [],
            },
            status_code=400,
        )
    out = simulate_macro_shock(event_description=event_description, max_depth=3)
    return JSONResponse(out)


@router.post("/api/simulate-macro-shock/batch")
async def api_simulate_macro_shock_batch(request: Request):
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    scenarios = (payload or {}).get("scenarios")
    if not isinstance(scenarios, list):
        return JSONResponse(
            {
                "ok": False,
                "error": "invalid_payload",
                "message": "Provide JSON: {\"scenarios\": [\"...\", \"...\"], \"max_depth\": 3, \"top_n\": 5}",
            },
            status_code=400,
        )
    max_depth = int((payload or {}).get("max_depth") or 3)
    top_n = int((payload or {}).get("top_n") or 5)
    out = simulate_macro_shock_batch(scenarios=scenarios, max_depth=max_depth, top_n=top_n)
    code = 200 if bool(out.get("ok")) else 400
    return JSONResponse(out, status_code=code)


@router.get("/api/agent/runs")
async def api_agent_runs(limit: int = 20):
    return JSONResponse({"ok": True, "runs": list_recent_agent_runs(limit=max(1, min(200, int(limit or 20))))})


@router.get("/api/agent/reflexions")
async def api_agent_reflexions(limit: int = 20):
    out = list_recent_reflexions(limit=max(1, min(200, int(limit or 20))))
    return JSONResponse({"ok": True, **out})


@router.get("/api/risk-veto/recent")
async def api_risk_veto_recent(limit: int = 30):
    rows = list_recent_risk_veto_decisions(limit=max(1, min(300, int(limit or 30))))
    return JSONResponse({"ok": True, "items": rows, "count": len(rows)})


@router.get("/api/ai/meta-suggestions")
async def api_ai_meta_suggestions(limit: int = 30, status: str = "open"):
    rows = list_ai_meta_suggestions(limit=max(1, min(300, int(limit or 30))), status=str(status or "open"))
    return JSONResponse({"ok": True, "items": rows, "count": len(rows)})


@router.post("/api/ai/meta-suggestions/run")
async def api_ai_meta_suggestions_run(request: Request):
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    days = int((payload or {}).get("days") or 30)
    out = run_ai_meta_suggestions(days=max(1, min(365, int(days or 30))))
    return JSONResponse(out)


@router.get("/api/risk-veto/config")
async def api_risk_veto_config():
    return JSONResponse({"ok": True, "config": get_risk_veto_config()})


@router.post("/api/risk-veto/config")
async def api_risk_veto_config_update(request: Request):
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    cfg = update_risk_veto_config(payload if isinstance(payload, dict) else {})
    return JSONResponse({"ok": True, "config": cfg})


@router.post("/dashboard/risk-veto/config")
def dashboard_risk_veto_config_update(
    request: Request,
    enabled: str = Form("1"),
    min_confidence_for_mutation: str = Form("0.62"),
    max_single_add_pct: str = Form("5.0"),
    max_position_weight_pct: str = Form("20.0"),
    max_var95_pct: str = Form("6.0"),
    max_cvar95_pct: str = Form("8.0"),
    min_quote_coverage_pct: str = Form("75.0"),
    high_impact_notional_pct: str = Form("3.0"),
    require_known_ticker_scope: str = Form("1"),
    block_on_unknown_ticker: str = Form("1"),
    review_for_high_impact: str = Form("1"),
):
    def _to_bool(v: str) -> bool:
        return str(v or "").strip().lower() in {"1", "true", "yes", "on"}

    cfg = update_risk_veto_config(
        {
            "enabled": _to_bool(enabled),
            "min_confidence_for_mutation": to_float(min_confidence_for_mutation, 0.62),
            "max_single_add_pct": to_float(max_single_add_pct, 5.0),
            "max_position_weight_pct": to_float(max_position_weight_pct, 20.0),
            "max_var95_pct": to_float(max_var95_pct, 6.0),
            "max_cvar95_pct": to_float(max_cvar95_pct, 8.0),
            "min_quote_coverage_pct": to_float(min_quote_coverage_pct, 75.0),
            "high_impact_notional_pct": to_float(high_impact_notional_pct, 3.0),
            "require_known_ticker_scope": _to_bool(require_known_ticker_scope),
            "block_on_unknown_ticker": _to_bool(block_on_unknown_ticker),
            "review_for_high_impact": _to_bool(review_for_high_impact),
        }
    )
    return _render(
        request,
        message=(
            "Risk Veto settings updated. "
            f"MinConf={float(cfg.get('min_confidence_for_mutation') or 0.0):.2f}, "
            f"MaxAdd={float(cfg.get('max_single_add_pct') or 0.0):.2f}%."
        ),
    )


@router.post("/api/agent/reflexions/rollback")
async def api_agent_reflexion_rollback(request: Request):
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    target_version = str((payload or {}).get("target_version") or "").strip()
    if not target_version:
        return JSONResponse({"ok": False, "error": "missing_target_version"}, status_code=400)
    out = rollback_reflexion_policy(target_version=target_version)
    return JSONResponse(out, status_code=200 if bool(out.get("ok")) else 400)


@router.post("/api/agent/breach-alerts/{alert_id}/dismiss")
async def api_dismiss_breach_alert(alert_id: int):
    ok = dismiss_thesis_breach_alert(alert_id)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@router.post("/api/agent/cascade-alerts/{alert_id}/dismiss")
async def api_dismiss_cascade_alert(alert_id: int):
    ok = dismiss_cascade_alert(alert_id)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@router.post("/api/sec/ingest-new-filings")
async def api_sec_ingest_new_filings(
    request: Request,
    background_tasks: BackgroundTasks,
):
    payload = {}
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    raw_ids = (payload or {}).get("filing_ids")
    if not isinstance(raw_ids, list):
        return JSONResponse({"ok": False, "error": "invalid_payload", "message": "Provide {\"filing_ids\":[1,2,...]}"}, status_code=400)
    ids: list[int] = []
    for x in raw_ids:
        try:
            v = int(x)
        except Exception:
            continue
        if v > 0:
            ids.append(v)
    ids = sorted(set(ids))[:500]
    if not ids:
        return JSONResponse({"ok": False, "error": "no_filing_ids"}, status_code=400)
    background_tasks.add_task(process_new_filings_pipeline, ids)
    return JSONResponse({"ok": True, "queued": True, "filing_ids": ids, "count": len(ids)})


@router.post("/desk/action")
def desk_action(
    text: str = Form(""),
    intent: str = Form("auto"),
):
    try:
        mode, payload = _desk_parse(text)
        it = str(intent or "auto").strip().lower()
        if it in {"note", "task", "ask"}:
            mode = it
            payload = str(text or "").strip()
        if not payload:
            return JSONResponse({"ok": False, "message": "Type a note, task, or question first."}, status_code=400)
        # Intent-based first: execute command if detected regardless of selected mode.
        cmd = _parse_ai_command(payload)
        if cmd:
            ok, msg = _execute_ai_command(cmd)
            return JSONResponse(
                {
                    "ok": bool(ok),
                    "mode": cmd["mode"],
                    "message": (msg + (f" Linked: {cmd['ticker']}" if cmd.get("ticker") else "")),
                    "ticker": cmd.get("ticker", ""),
                },
                status_code=200 if ok else 500,
            )
        if mode == "ask":
            # If user intent looks like an operation command, fail fast
            # instead of falling back to slow/freeform AI.
            if _is_op_like(payload):
                return JSONResponse(
                    {
                        "ok": False,
                        "mode": "ask",
                        "message": "Command not recognized. Try: 'add 10k usd cash', 'remove usd cash', 'add oxy to watchlist'.",
                    },
                    status_code=400,
                )
            ans = ask_ai_local(payload)
            return JSONResponse({"ok": True, "mode": "ask", "answer": ans or "No answer found."})
        linked_ticker = _infer_ticker(payload)
        ok, msg = quick_capture(mode=mode, text=payload, ticker=linked_ticker)
        return JSONResponse(
            {
                "ok": bool(ok),
                "mode": mode,
                "message": msg,
                "ticker": linked_ticker,
            },
            status_code=200 if ok else 500,
        )
    except Exception as exc:
        return JSONResponse({"ok": False, "message": f"Desk error: {exc}"}, status_code=500)


@router.get("/my_companies")
def my_companies_page(request: Request, msg: str = "", tab: str = "all"):
    target = "/my_universe?tab=all"
    if str(msg or "").strip():
        target += f"&msg={quote(str(msg).strip())}"
    return RedirectResponse(url=target, status_code=307)


@router.get("/my_universe")
def my_universe_page(request: Request, msg: str = "", tab: str = "all"):
    requested = str(tab or "all").strip().lower()
    if requested not in {"all", "portfolio", "watchlist", "bluechips"}:
        requested = "all"
    templates = request.app.state.templates
    snap = dashboard_snapshot()
    home = snap.get("home", {}) or {}
    tv = requested
    metrics = _my_companies_metrics(home)
    # Durable sources first: do not rely on transient dashboard snapshot for list membership.
    watchlist_rows = _read_watchlist_rows()
    if not watchlist_rows:
        watchlist_rows = list(home.get("watchlist", []) or [])
    wl_tickers = [_safe_ticker(r.get("ticker", "")) for r in watchlist_rows]
    blue_rows = _read_blue_chips_rows()
    if not blue_rows:
        blue_rows = list(home.get("bluechips", []) or [])
    blue_tickers = [_safe_ticker(r.get("ticker", "")) for r in blue_rows]
    all_quote_tickers = sorted(
        {
            t
            for t in (
                [_safe_ticker(r.get("ticker", "")) for r in (metrics.get("portfolio_rows") or [])]
                + wl_tickers
                + blue_tickers
            )
            if t
        }
    )
    quote_map_all = _last_quote_map(all_quote_tickers)
    wl_quotes = {t: quote_map_all.get(t, {}) for t in wl_tickers}
    blue_quotes = {t: quote_map_all.get(t, {}) for t in blue_tickers}
    wl_added_map = {str(r.get("ticker") or "").strip().upper(): str(r.get("added_at") or "") for r in watchlist_rows}
    blue_added_map = {str(r.get("ticker") or "").strip().upper(): str(r.get("added_at") or "") for r in blue_rows}
    wl_cur_px = {
        t: (
            _to_float((wl_quotes.get(t, {}) or {}).get("price"), 0.0)
            or _to_float(next((r.get("price_now") for r in watchlist_rows if _safe_ticker(r.get("ticker", "")) == t), 0.0), 0.0)
            or _to_float(next((r.get("price") for r in watchlist_rows if _safe_ticker(r.get("ticker", "")) == t), 0.0), 0.0)
        )
        for t in wl_tickers
    }
    blue_cur_px = {
        t: (
            _to_float((blue_quotes.get(t, {}) or {}).get("price"), 0.0)
            or _to_float(next((r.get("price_now") for r in blue_rows if _safe_ticker(r.get("ticker", "")) == t), 0.0), 0.0)
            or _to_float(next((r.get("price") for r in blue_rows if _safe_ticker(r.get("ticker", "")) == t), 0.0), 0.0)
        )
        for t in blue_tickers
    }
    wl_since_added = _since_added_pct_map(wl_added_map, wl_cur_px)
    blue_since_added = _since_added_pct_map(blue_added_map, blue_cur_px)
    name_map = _company_name_map(wl_tickers)
    watchlist_full: list[dict[str, object]] = []
    for r in watchlist_rows:
        t = _safe_ticker(r.get("ticker", ""))
        q = wl_quotes.get(t, {})
        day_live = _pick_day_pct(q.get("day_pct"), r.get("day_pct"))
        day_safe = _to_float(day_live, 0.0)
        live_px = _to_float(q.get("price"), 0.0) or _to_float(r.get("price_now"), 0.0) or _to_float(r.get("price"), 0.0)
        mcap_live = _to_float(q.get("market_cap"), 0.0) or _parse_human_amount(str(r.get("market_cap") or ""), 0.0)
        watchlist_full.append(
            {
                **r,
                "ticker": t,
                "name": str(name_map.get(t) or "").strip(),
                "price_now": live_px,
                "market_cap": mcap_live,
                "since_added_pct": wl_since_added.get(t),
                "day_pct": day_live,
                "day_pct_safe": day_safe,
                "day_abs_pct": abs(day_safe),
            }
        )
    blue_name_map = _company_name_map(blue_tickers)
    bluechips_full: list[dict[str, object]] = []
    for r in blue_rows:
        t = _safe_ticker(r.get("ticker", ""))
        q = blue_quotes.get(t, {})
        day_live = _pick_day_pct(q.get("day_pct"), r.get("day_pct"))
        day_safe = _to_float(day_live, 0.0)
        live_px = _to_float(q.get("price"), 0.0) or _to_float(r.get("price_now"), 0.0) or _to_float(r.get("price"), 0.0)
        mcap_live = _to_float(q.get("market_cap"), 0.0) or _parse_human_amount(str(r.get("market_cap") or ""), 0.0)
        bluechips_full.append(
            {
                **r,
                "ticker": t,
                "name": str(blue_name_map.get(t) or "").strip(),
                "price_now": live_px,
                "market_cap": mcap_live,
                "since_added_pct": blue_since_added.get(t),
                "day_pct": day_live,
                "day_pct_safe": day_safe,
                "day_abs_pct": abs(day_safe),
            }
        )
    p_report, w_report = _universe_reports({**home, "watchlist": watchlist_full}, metrics)
    return templates.TemplateResponse(
        "my_universe.html",
        {
            "request": request,
            "message": str(msg or "").strip(),
            "tab": tv,
            "generated_at": str((home.get("generated_at") or "")),
            "portfolio": list(home.get("portfolio", []) or []),
            "watchlist": watchlist_rows,
            "watchlist_full": watchlist_full,
            "bluechips": blue_rows,
            "bluechips_full": bluechips_full,
            "metrics": metrics,
            "cash_rows": _read_cash_rows(),
            "universe_portfolio_report": p_report,
            "universe_watchlist_report": w_report,
            "watchlist_opportunities": _watchlist_opportunities(home, limit=10),
            "movers_up": snap.get("movers_up", []),
            "movers_down": snap.get("movers_down", []),
            "feed": snap.get("feed", []),
            "signals": snap.get("signals", {}),
            "signal_items": (snap.get("signals", {}) or {}).get("items", []),
        },
    )


@router.post("/my_companies/portfolio/upsert")
def my_companies_portfolio_upsert(
    ticker: str = Form(""),
    side: str = Form(""),
    shares: str = Form(""),
    cost: str = Form(""),
    note: str = Form(""),
    trade_reason: str = Form(""),
    buy_belief: str = Form(""),
):
    t = _safe_ticker(ticker)
    if not t:
        return JSONResponse({"ok": False, "message": "Ticker required."}, status_code=400)
    sh = _to_float(shares, 0.0)
    c = _to_float(cost, 0.0)
    side_key = str(side or "").strip().lower()
    if side_key not in {"buy", "sell"}:
        side_key = ""
    rows = _read_portfolio_file()
    existing = None
    kept: list[dict[str, str]] = []
    for r in rows:
        if _safe_ticker(r.get("ticker", "")) == t and existing is None:
            existing = r
        else:
            kept.append(r)

    # explicit sell path: sell quantity from existing position (or full remove).
    if side_key == "sell":
        if not existing:
            return JSONResponse({"ok": False, "message": "No existing position to sell."}, status_code=400)
        removed = existing or {}
        old_sh = _to_float(removed.get("shares", ""), 0.0)
        old_c = _to_float(removed.get("cost", ""), 0.0)
        sell_qty = sh if sh > 0 else old_sh
        if sell_qty <= 0:
            return JSONResponse({"ok": False, "message": "Shares to sell must be positive."}, status_code=400)
        if sell_qty >= old_sh:
            next_sh = 0.0
        else:
            next_sh = old_sh - sell_qty
        reason_key = normalize_sell_reason(trade_reason)
        reason_label = label_for_sell_reason(reason_key)
        if next_sh > 0:
            kept.append(
                {
                    "ticker": t,
                    "shares": f"{next_sh:g}",
                    "cost": f"{old_c:.6f}",
                    "note": str(note or "").strip() or str(removed.get("note", "")).strip(),
                }
            )
        _write_portfolio_file(kept)
        record_portfolio_transaction(
            ticker=t,
            action="sell",
            shares=sell_qty,
            price=(c if c > 0 else old_c),
            note=str(note or "").strip() or ("Sold from portfolio" if next_sh > 0 else "Removed from portfolio"),
            source="my_companies_form",
            meta={
                "decision_reason": reason_key,
                "decision_reason_label": reason_label,
                "was_existing_position": bool(existing),
            },
        )
        record_decision(
            action=("portfolio_sell" if next_sh > 0 else "portfolio_remove"),
            ticker=t,
            reason=(str(note or "").strip() or f"Sell ({reason_label})"),
            confidence=0.95,
            source="my_companies_form",
        )
        if next_sh > 0:
            return JSONResponse({"ok": True, "message": "Position updated (sold shares)."})
        return JSONResponse({"ok": True, "message": "Removed from portfolio."})

    # legacy remove path for callers that still post shares<=0 without side.
    if sh <= 0:
        removed = existing or {}
        reason_key = normalize_sell_reason(trade_reason)
        reason_label = label_for_sell_reason(reason_key)
        _write_portfolio_file(kept)
        record_portfolio_transaction(
            ticker=t,
            action="sell",
            shares=_to_float(removed.get("shares", ""), 0.0),
            price=(c if c > 0 else _to_float(removed.get("cost", ""), 0.0)),
            note=str(note or "").strip() or "Removed from portfolio",
            source="my_companies_form",
            meta={
                "decision_reason": reason_key,
                "decision_reason_label": reason_label,
                "was_existing_position": bool(existing),
            },
        )
        record_decision(
            action="portfolio_remove",
            ticker=t,
            reason=(str(note or "").strip() or f"Removed from portfolio ({reason_label})"),
            confidence=0.95,
            source="my_companies_form",
        )
        return JSONResponse({"ok": True, "message": "Removed from portfolio."})

    if c <= 0:
        return JSONResponse(
            {
                "ok": False,
                "message": "Average price required for adds/updates.",
            },
            status_code=400,
        )

    if existing:
        old_sh = _to_float(existing.get("shares", ""), 0.0)
        old_c = _to_float(existing.get("cost", ""), 0.0)
        new_sh = old_sh + sh
        if new_sh <= 0:
            return JSONResponse({"ok": False, "message": "Resulting shares must be positive."}, status_code=400)
        new_c = ((old_sh * old_c) + (sh * c)) / new_sh if old_sh > 0 else c
        kept.append(
            {
                "ticker": t,
                "shares": f"{new_sh:g}",
                "cost": f"{new_c:.6f}",
                "note": str(note or "").strip() or "Accumulated via Ask AI",
            }
        )
        _write_portfolio_file(kept)
        reason_key = normalize_buy_reason(trade_reason) if trade_reason else "existing_position_add"
        reason_label = label_for_buy_reason(reason_key)
        record_portfolio_transaction(
            ticker=t,
            action="buy",
            shares=sh,
            price=c,
            note=str(note or "").strip() or "Position accumulated",
            source="my_companies_form",
            meta={
                "decision_reason": reason_key,
                "decision_reason_label": reason_label,
                "was_existing_position": True,
            },
        )
        record_decision(
            action="portfolio_buy",
            ticker=t,
            reason=(str(note or "").strip() or reason_label),
            confidence=0.96,
            source="my_companies_form",
        )
        return JSONResponse({"ok": True, "message": "Portfolio updated (position accumulated)."})

    reason_key = normalize_buy_reason(trade_reason)
    reason_label = label_for_buy_reason(reason_key)
    belief_key = normalize_buy_belief(buy_belief)
    belief_label = label_for_buy_belief(belief_key)
    kept.append(
        {
            "ticker": t,
            "shares": f"{sh:g}",
            "cost": f"{c:.6f}",
            "note": str(note or "").strip(),
        }
    )
    _write_portfolio_file(kept)
    record_portfolio_transaction(
        ticker=t,
        action="buy",
        shares=sh,
        price=c,
        note=str(note or "").strip() or "New position added",
        source="my_companies_form",
        meta={
            "decision_reason": reason_key,
            "decision_reason_label": reason_label,
            "buy_belief": belief_key,
            "buy_belief_label": belief_label,
            "was_existing_position": False,
        },
    )
    record_decision(
        action="portfolio_buy",
        ticker=t,
        reason=(
            str(note or "").strip()
            or f"New position ({reason_label})"
            + (f" | belief: {belief_label}" if belief_label else "")
        ),
        confidence=0.96,
        source="my_companies_form",
    )
    return JSONResponse({"ok": True, "message": "Portfolio updated."})


@router.post("/my_universe/portfolio/upsert")
def my_universe_portfolio_upsert(
    ticker: str = Form(""),
    side: str = Form(""),
    shares: str = Form(""),
    cost: str = Form(""),
    note: str = Form(""),
    trade_reason: str = Form(""),
    buy_belief: str = Form(""),
):
    return my_companies_portfolio_upsert(
        ticker=ticker,
        side=side,
        shares=shares,
        cost=cost,
        note=note,
        trade_reason=trade_reason,
        buy_belief=buy_belief,
    )


@router.post("/my_companies/watchlist/add")
def my_companies_watchlist_add(
    ticker: str = Form(""),
    reason: str = Form(""),
):
    t = _safe_ticker(ticker)
    if not t:
        return JSONResponse({"ok": False, "message": "Ticker required."}, status_code=400)
    rows = _read_watchlist_rows()
    rows = [r for r in rows if _safe_ticker(r.get("ticker", "")) != t]
    rows.append(
        {
            "ticker": t,
            "added_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "reason": str(reason or "").strip(),
        }
    )
    _write_watchlist_file(rows)
    # Keep list membership exclusive: if it is in Watchlist, it is not in Blue Chips.
    _ = _remove_blue_chip(t)
    why = str(reason or "").strip() or "Added to watchlist"
    record_decision(action="watchlist_add", ticker=t, reason=why, confidence=0.96, source="my_companies_form")
    upsert_watchlist_thesis(ticker=t, thesis=why, pick_method="manual", status="active")
    return JSONResponse({"ok": True, "message": "Watchlist updated."})


@router.post("/my_universe/watchlist/add")
def my_universe_watchlist_add(
    ticker: str = Form(""),
    reason: str = Form(""),
):
    return my_companies_watchlist_add(ticker=ticker, reason=reason)


@router.post("/my_companies/watchlist/remove")
def my_companies_watchlist_remove(
    ticker: str = Form(""),
):
    t = _safe_ticker(ticker)
    rows = _read_watchlist_rows()
    rows = [r for r in rows if _safe_ticker(r.get("ticker", "")) != t]
    _write_watchlist_file(rows)
    record_decision(action="watchlist_remove", ticker=t, reason="Removed from watchlist", confidence=0.96, source="my_companies_form")
    return JSONResponse({"ok": True, "message": "Removed."})


@router.post("/my_universe/watchlist/remove")
def my_universe_watchlist_remove(
    ticker: str = Form(""),
):
    return my_companies_watchlist_remove(ticker=ticker)


@router.post("/my_companies/bluechips/add")
def my_companies_bluechips_add(
    ticker: str = Form(""),
    reason: str = Form(""),
):
    t = _safe_ticker(ticker)
    if not t:
        return JSONResponse({"ok": False, "message": "Ticker required."}, status_code=400)
    ok = _upsert_blue_chip(t, reason=reason)
    if not ok:
        return JSONResponse({"ok": False, "message": "Could not add blue chip."}, status_code=500)
    # Keep list membership exclusive: if it is in Blue Chips, remove from Watchlist.
    wl_rows = _read_watchlist_rows()
    wl_next = [r for r in wl_rows if _safe_ticker(r.get("ticker", "")) != t]
    if len(wl_next) != len(wl_rows):
        _write_watchlist_file(wl_next)
    why = str(reason or "").strip() or "Added to Blue Chips macro radar"
    record_decision(action="blue_chip_add", ticker=t, reason=why, confidence=0.96, source="my_companies_form")
    return JSONResponse({"ok": True, "message": "Blue Chips updated."})


@router.post("/my_universe/bluechips/add")
def my_universe_bluechips_add(
    ticker: str = Form(""),
    reason: str = Form(""),
):
    return my_companies_bluechips_add(ticker=ticker, reason=reason)


@router.post("/my_companies/bluechips/remove")
def my_companies_bluechips_remove(
    ticker: str = Form(""),
):
    t = _safe_ticker(ticker)
    ok = _remove_blue_chip(t)
    if not ok:
        return JSONResponse({"ok": False, "message": "Ticker not found in Blue Chips."}, status_code=404)
    record_decision(action="blue_chip_remove", ticker=t, reason="Removed from Blue Chips", confidence=0.96, source="my_companies_form")
    return JSONResponse({"ok": True, "message": "Removed."})


@router.post("/my_universe/bluechips/remove")
def my_universe_bluechips_remove(
    ticker: str = Form(""),
):
    return my_companies_bluechips_remove(ticker=ticker)


@router.post("/my_universe/bluechips/seed")
def my_universe_bluechips_seed():
    added = 0
    for tk in BLUE_CHIPS_SEED_TICKERS:
        ok = _upsert_blue_chip(tk, reason="Seeded large-cap blue chip")
        if ok:
            added += 1
    try:
        record_decision(
            action="blue_chip_seed",
            ticker="",
            reason=f"Seeded blue chips list ({added} tickers).",
            confidence=0.98,
            source="my_universe_seed",
        )
    except Exception:
        pass
    return JSONResponse({"ok": True, "message": f"Blue Chips seeded ({added}).", "added": added})


@router.post("/my_universe/cash/upsert")
def my_universe_cash_upsert(
    currency: str = Form("USD"),
    amount: str = Form("0"),
):
    ccy = str(currency or "USD").strip().upper()
    if not ccy:
        ccy = "USD"
    amt = _to_float(amount, 0.0)
    rows = _read_cash_rows()
    rows = [r for r in rows if str(r.get("currency") or "").strip().upper() != ccy]
    if amt > 0:
        rows.append({"currency": ccy, "amount": f"{amt:g}"})
    _write_cash_rows(rows)
    return JSONResponse({"ok": True, "message": "Cash balance updated."})


@router.post("/my_universe/cash/remove")
def my_universe_cash_remove(
    currency: str = Form("USD"),
):
    ccy = str(currency or "USD").strip().upper()
    rows = _read_cash_rows()
    rows = [r for r in rows if str(r.get("currency") or "").strip().upper() != ccy]
    _write_cash_rows(rows)
    return JSONResponse({"ok": True, "message": "Cash balance removed."})


@router.post("/my_universe/command")
def my_universe_command(command: str = Form("")):
    parsed = parse_universe_command(command)
    if not bool(parsed.get("ok")):
        return JSONResponse({"ok": False, "message": str(parsed.get("error") or "Invalid command.")}, status_code=400)
    kind = str(parsed.get("type") or "")
    payload = parsed.get("payload") if isinstance(parsed.get("payload"), dict) else {}

    if kind == "portfolio_upsert":
        return my_universe_portfolio_upsert(
            ticker=str(payload.get("ticker") or ""),
            side=str(payload.get("side") or ""),
            shares=str(payload.get("shares") or ""),
            cost=str(payload.get("cost") or ""),
            note=str(payload.get("note") or ""),
            trade_reason=str(payload.get("trade_reason") or ""),
            buy_belief="",
        )
    if kind == "cash_upsert":
        return my_universe_cash_upsert(
            currency=str(payload.get("currency") or "USD"),
            amount=str(payload.get("amount") or "0"),
        )
    if kind == "watchlist_add":
        return my_universe_watchlist_add(
            ticker=str(payload.get("ticker") or ""),
            reason=str(payload.get("reason") or ""),
        )
    if kind == "watchlist_remove":
        return my_universe_watchlist_remove(
            ticker=str(payload.get("ticker") or ""),
        )
    if kind == "bluechips_add":
        return my_universe_bluechips_add(
            ticker=str(payload.get("ticker") or ""),
            reason=str(payload.get("reason") or ""),
        )
    if kind == "bluechips_remove":
        return my_universe_bluechips_remove(
            ticker=str(payload.get("ticker") or ""),
        )
    return JSONResponse({"ok": False, "message": "Unsupported command type."}, status_code=400)


@router.get("/api/global-search")
def api_global_search(q: str = "", limit: int = 12):
    needle = str(q or "").strip()
    if not needle:
        return JSONResponse({"ok": True, "query": "", "results": []})
    lim = max(4, min(30, int(limit or 12)))
    low, toks, toks_norm = _search_tokens(needle)
    is_url_like = ("http://" in low) or ("https://" in low) or ("www." in low)
    requested_forms, company_query = _sec_query_parts(toks, toks_norm)
    sec_like = _is_sec_like_query(low, toks)
    cache_key = f"{low}|{lim}"
    now = time.time()
    if not sec_like:
        with _GLOBAL_SEARCH_LOCK:
            cached = _GLOBAL_SEARCH_CACHE.get(cache_key)
            if cached and (now - float(cached[0])) <= _GLOBAL_SEARCH_CACHE_TTL_SEC:
                return JSONResponse({"ok": True, "query": needle, "results": list(cached[1])[:lim]})
    results: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(kind: str, title: str, url: str, subtitle: str = "") -> None:
        u = str(url or "").strip()
        t = str(title or "").strip()
        if not u or not t:
            return
        key = f"{kind}|{u}|{t}".lower()
        if key in seen:
            return
        seen.add(key)
        results.append(
            {
                "kind": str(kind or "result"),
                "title": t[:120],
                "subtitle": str(subtitle or "").strip()[:180],
                "url": u,
            }
        )

    def _match(*parts: object) -> bool:
        txt = " ".join(str(x or "") for x in parts).strip().lower()
        if not txt:
            return False
        if low in txt:
            return True
        # Normalize punctuation to support queries like "10k nvda" matching "10-K".
        txt_norm = re.sub(r"[^a-z0-9]+", "", txt)
        return bool(toks) and all((t in txt) or (tn and tn in txt_norm) for t, tn in zip(toks, toks_norm))

    def _add_filing_hits(
        scan_limit: int,
        *,
        q_text: str = "",
        tks: list[str] | None = None,
        force_ticker_match: bool = False,
    ) -> int:
        added_before = len(results)
        forced_tickers = {str(x or "").strip().upper() for x in list(tks or []) if str(x or "").strip()}
        filing_rows = query_report_facts(
            query=str(q_text or needle),
            tickers=(list(tks or []) or None),
            limit=int(scan_limit),
            official_only=True,
        )
        if not filing_rows and tks:
            # Fallback for cases like "nvidia 10k": resolve by ticker + form filter even when free-text misses.
            filing_rows = query_report_facts(
                query="",
                tickers=list(tks),
                limit=int(scan_limit),
                official_only=True,
            )
        filing_tickers = sorted(
            {
                str(fr.get("ticker") or "").strip().upper()
                for fr in filing_rows
                if str(fr.get("ticker") or "").strip()
            }
        )
        filing_names = lookup_company_name_map(filing_tickers) if filing_tickers else {}
        for fr in filing_rows:
            tk = str(fr.get("ticker") or "").strip().upper()
            nm = str(filing_names.get(tk) or "").strip()
            kind = str(fr.get("report_kind") or "Filing").strip()
            kind_up = kind.upper()
            rpt = str(fr.get("report_name") or "").strip()
            fact = str(fr.get("fact_text") or "").strip()
            if requested_forms:
                if not any((fm in kind_up) or (fm.lower() in rpt.lower()) for fm in requested_forms):
                    continue
            if force_ticker_match and forced_tickers and tk in forced_tickers:
                pass
            elif not _match(tk, nm, kind, rpt, fact):
                continue
            if tk:
                title = f"{tk} · {kind}"
                if nm:
                    title = f"{tk} · {nm} · {kind}"
                _add("filing", title, f"/company_file?t={quote(tk)}", fact[:160] or rpt[:160])
            else:
                _add("filing", (rpt or kind or "Filing")[:120], "/reports", fact[:160])
            if len(results) >= lim:
                break
        return len(results) - added_before

    def _add_filings_core_hits(tks: list[str], max_rows: int = 12) -> int:
        added_before = len(results)
        want_tks = [str(x or "").strip().upper() for x in list(tks or []) if str(x or "").strip()]
        if not want_tks:
            return 0
        forms = sorted({f.upper() for f in requested_forms if str(f or "").strip()})
        if core_backend() == "postgres":
            con_pg = pg_connect()
            if con_pg is None:
                return 0
            try:
                cur = con_pg.cursor()
                for tk in want_tks[:3]:
                    fetched_any = False
                    if forms:
                        cur.execute(
                            """
                            SELECT form, date, accession, doc_url
                            FROM filings_core
                            WHERE ticker=%s AND form = ANY(%s)
                            ORDER BY date DESC, id DESC
                            LIMIT %s
                            """,
                            (tk, forms, max(1, min(50, int(max_rows)))),
                        )
                        rows = cur.fetchall() or []
                        fetched_any = bool(rows)
                    else:
                        rows = []
                    if not rows:
                        cur.execute(
                            """
                            SELECT form, date, accession, doc_url
                            FROM filings_core
                            WHERE ticker=%s
                            ORDER BY date DESC, id DESC
                            LIMIT %s
                            """,
                            (tk, max(1, min(50, int(max_rows)))),
                        )
                        rows = cur.fetchall() or []
                    for r in rows:
                        fm = str(r[0] or "").strip().upper()
                        dt_s = str(r[1] or "").strip()[:10]
                        acc = str(r[2] or "").strip()
                        subtitle = f"{dt_s} • {fm}"
                        if forms and (not fetched_any):
                            subtitle += " • nearest match"
                        if acc:
                            subtitle += f" • {acc}"
                        _add("filing", f"{tk} · {fm} SEC Filing", f"/company_file/sec?t={quote(tk)}&form={quote(fm)}", subtitle)
                        if len(results) >= lim:
                            break
                    if len(results) >= lim:
                        break
            except Exception:
                return len(results) - added_before
            finally:
                try:
                    con_pg.close()
                except Exception:
                    pass
            return len(results) - added_before
        con = get_sqlite_conn(CORE_DB_PATH)
        try:
            for tk in want_tks[:3]:
                fetched_any = False
                if forms:
                    marks = ",".join("?" for _ in forms)
                    rows = con.execute(
                        f"""
                        SELECT form, date, accession, doc_url
                        FROM filings
                        WHERE UPPER(ticker)=? AND UPPER(form) IN ({marks})
                        ORDER BY date DESC, id DESC
                        LIMIT ?
                        """,
                        tuple([tk] + forms + [max(1, min(50, int(max_rows)))]),
                    ).fetchall()
                    fetched_any = bool(rows)
                else:
                    rows = []
                if not rows:
                    rows = con.execute(
                        """
                        SELECT form, date, accession, doc_url
                        FROM filings
                        WHERE UPPER(ticker)=?
                        ORDER BY date DESC, id DESC
                        LIMIT ?
                        """,
                        (tk, max(1, min(50, int(max_rows)))),
                    ).fetchall()
                for r in rows:
                    fm = str(r["form"] or "").strip().upper()
                    dt_s = str(r["date"] or "").strip()[:10]
                    acc = str(r["accession"] or "").strip()
                    subtitle = f"{dt_s} • {fm}"
                    if forms and (not fetched_any):
                        subtitle += " • nearest match"
                    if acc:
                        subtitle += f" • {acc}"
                    _add("filing", f"{tk} · {fm} SEC Filing", f"/company_file/sec?t={quote(tk)}&form={quote(fm)}", subtitle)
                    if len(results) >= lim:
                        break
                if len(results) >= lim:
                    break
        except Exception:
            return len(results) - added_before
        finally:
            try:
                con.close()
            except Exception:
                pass
        return len(results) - added_before

    app_routes = [
        ("page", "Dashboard", "/dashboard", "Market overview"),
        ("page", "Portfolio / Universe", "/my_universe", "Holdings, watchlist, blue chips"),
        ("page", "Company Search", "/company_file", "Browse company files"),
        ("page", "Organizer", "/organizer", "Tasks, notes, timeline"),
        ("page", "Settings", "/observability", "System and observability"),
        ("page", "Reports", "/reports", "Generated reports"),
    ]
    for kind, title, url, subtitle in app_routes:
        if _match(title, subtitle):
            _add(kind, title, url, subtitle)

    if len(results) < lim:
        try:
            task_seen: set[int] = set()
            task_scan = max(300, min(900, lim * 70))
            for open_only in (True, False):
                for r in list_tasks(open_only=open_only, limit=task_scan):
                    rid = int((r.get("id") if isinstance(r, dict) else r["id"]) or 0)
                    if rid > 0 and rid in task_seen:
                        continue
                    task = str((r.get("task") if isinstance(r, dict) else r["task"]) or "").strip()
                    status = str((r.get("status") if isinstance(r, dict) else r["status"]) or "").strip().lower()
                    tk = str((r.get("ticker") if isinstance(r, dict) else r["ticker"]) or "").strip().upper()
                    if not task or not _match(task, tk, status):
                        continue
                    if rid > 0:
                        task_seen.add(rid)
                    if tk:
                        _add("task", task, f"/company_file?t={quote(tk)}", f"Task - {tk} ({status or 'open'})")
                    else:
                        _add("task", task, f"/organizer?q={quote(needle)}", f"Task ({status or 'open'})")
                    if len(results) >= lim:
                        break
                if len(results) >= lim:
                    break
        except Exception:
            pass

    if len(results) < lim:
        try:
            note_scan = max(300, min(1200, lim * 90))
            for r in list_recent_notes(limit=note_scan):
                txt = str(r.get("text") or "").strip()
                tag = str(r.get("tag") or "").strip()
                tk = str(r.get("ticker") or "").strip().upper()
                if not txt or not _match(txt, tag, tk):
                    continue
                if tk:
                    _add("note", txt[:90], f"/company_file?t={quote(tk)}", f"Note - {tk}")
                else:
                    _add("note", txt[:90], f"/organizer?q={quote(needle)}", "Note")
                if len(results) >= lim:
                    break
        except Exception:
            pass

    if len(results) < lim:
        try:
            if core_backend() == "postgres":
                con_pg = pg_connect()
                if con_pg is not None:
                    try:
                        cur = con_pg.cursor()
                        cur.execute(
                            """
                            SELECT day, content
                            FROM daily_notes_core
                            ORDER BY day DESC
                            LIMIT 120
                            """
                        )
                        for r in cur.fetchall() or []:
                            day = str(r[0] or "").strip()
                            txt = str(r[1] or "").strip()
                            if not txt or not _match(txt, day):
                                continue
                            _add("note", f"Daily note {day}", f"/organizer?day={quote(day)}", txt[:160])
                            if len(results) >= lim:
                                break
                    finally:
                        con_pg.close()
            else:
                con = get_sqlite_conn(CORE_DB_PATH)
                try:
                    rows = con.execute(
                        """
                        SELECT day, content
                        FROM daily_notes
                        ORDER BY day DESC
                        LIMIT 120
                        """
                    ).fetchall()
                    for r in rows:
                        day = str(r["day"] or "").strip()
                        txt = str(r["content"] or "").strip()
                        if not txt or not _match(txt, day):
                            continue
                        _add("note", f"Daily note {day}", f"/organizer?day={quote(day)}", txt[:160])
                        if len(results) >= lim:
                            break
                finally:
                    con.close()
        except Exception:
            pass

    if len(results) < lim and sec_like and not is_url_like:
        try:
            sec_tickers: list[str] = []
            if company_query:
                sec_tickers = _resolve_company_tickers_for_search(company_query, limit=8)
            added = _add_filing_hits(
                scan_limit=min(30, max(lim * 2, 12)),
                q_text=(company_query or needle),
                tks=sec_tickers,
                force_ticker_match=bool(sec_tickers and requested_forms),
            )
            if added <= 0 and sec_tickers:
                added = _add_filing_hits(
                    scan_limit=min(30, max(lim * 2, 12)),
                    q_text="",
                    tks=sec_tickers,
                    force_ticker_match=bool(requested_forms),
                )
            # Final fallback: query the same SEC earnings-release source shown on company pages.
            if added <= 0 and sec_tickers:
                for tk in sec_tickers[:2]:
                    sec_rows = list_sec_earnings_releases(tk, limit=6, lookback_years=10, max_filings=260)
                    matched = False
                    for sr in sec_rows:
                        form = str(sr.get("form") or "").strip().upper()
                        if requested_forms and form and form not in requested_forms:
                            continue
                        matched = True
                        title = str(sr.get("title") or f"{form} Earnings Release").strip()[:120]
                        excerpt = str(sr.get("excerpt") or "").strip()[:160]
                        _add("filing", f"{tk} · {title}", f"/company_file?t={quote(tk)}", excerpt)
                        if len(results) >= lim:
                            break
                    if (not matched) and sec_rows:
                        for sr in sec_rows[:3]:
                            form = str(sr.get("form") or "").strip().upper()
                            title = str(sr.get("title") or f"{form} Earnings Release").strip()[:120]
                            excerpt = str(sr.get("excerpt") or "").strip()[:140]
                            _add(
                                "filing",
                                f"{tk} · {title}",
                                f"/company_file?t={quote(tk)}",
                                (excerpt + " • nearest match"),
                            )
                            if len(results) >= lim:
                                break
                    if len(results) >= lim:
                        break
            if added <= 0 and sec_tickers and len(results) < lim:
                _add_filings_core_hits(sec_tickers, max_rows=min(16, max(lim, 8)))
            # Hard fallback: always provide direct SEC route entries for resolved ticker(s).
            if len(results) < lim and sec_tickers and requested_forms:
                for tk in sec_tickers[:3]:
                    for fm in sorted(requested_forms):
                        _add(
                            "filing",
                            f"{tk} · {fm} SEC Filings",
                            f"/company_file/sec?t={quote(tk)}&form={quote(fm)}",
                            "Open filtered SEC filings",
                        )
                        if len(results) >= lim:
                            break
                    if len(results) >= lim:
                        break
        except Exception:
            pass

    if len(results) < lim and not sec_like and not is_url_like:
        try:
            _add_filing_hits(scan_limit=min(12, lim))
        except Exception:
            pass

    if len(results) < lim and not is_url_like:
        try:
            for r in list_reports(limit=80):
                nm = str(r.get("name") or "").strip()
                title = str(r.get("title") or nm).strip()
                if low not in f"{nm} {title}".lower():
                    continue
                _add("report", title, f"/reports?open={quote(nm)}", str(r.get("kind") or "Report"))
                if len(results) >= lim:
                    break
        except Exception:
            pass

    if len(results) < lim:
        try:
            company_rows = (
                list_companies(query=needle, page=1, page_size=min(6, lim), scope="all", sort="mcap_desc").get("rows") or []
            )
            for r in company_rows:
                tk = str(getattr(r, "ticker", "") or (r.get("ticker") if isinstance(r, dict) else "") or "").strip().upper()
                nm = str(getattr(r, "name", "") or (r.get("name") if isinstance(r, dict) else "") or tk).strip()
                ind = str(getattr(r, "industry", "") or (r.get("industry") if isinstance(r, dict) else "") or "").strip()
                if tk:
                    _add("company", f"{tk} - {nm}", f"/company_file?t={quote(tk)}", ind)
                if len(results) >= lim:
                    break
        except Exception:
            pass

    final_results = results[:lim]
    if not sec_like:
        with _GLOBAL_SEARCH_LOCK:
            _GLOBAL_SEARCH_CACHE[cache_key] = (time.time(), list(final_results))
            if len(_GLOBAL_SEARCH_CACHE) > 256:
                for k, _v in sorted(_GLOBAL_SEARCH_CACHE.items(), key=lambda kv: float(kv[1][0]))[:64]:
                    _GLOBAL_SEARCH_CACHE.pop(k, None)
    return JSONResponse({"ok": True, "query": needle, "results": final_results})
