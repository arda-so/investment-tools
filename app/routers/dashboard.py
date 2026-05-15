from __future__ import annotations

import concurrent.futures
import csv
import datetime as dt
import json
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.config import ROOT
from app.core.date import parse_datetime_flexible
from app.core.market import finnhub_key
from app.core.num import to_float, to_float_clean as _to_float
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
from app.routers.dashboard_helpers import (
    allow_text_mutations as _dh_allow_text_mutations,
    contains_fuzzy_term as _dh_contains_fuzzy_term,
    desk_parse as _dh_desk_parse,
    explicit_mutation_request as _dh_explicit_mutation_request,
    GLOBAL_SEARCH_APP_ROUTES as _DH_GLOBAL_SEARCH_APP_ROUTES,
    global_search_add_result as _dh_global_search_add_result,
    global_search_add_note_row as _dh_global_search_add_note_row,
    global_search_add_task_row as _dh_global_search_add_task_row,
    global_search_match as _dh_global_search_match,
    has_add_intent as _dh_has_add_intent,
    has_remove_intent as _dh_has_remove_intent,
    parse_ai_command as _dh_parse_ai_command,
    is_sec_like_query as _dh_is_sec_like_query,
    is_op_like as _dh_is_op_like,
    is_mutating_command_mode as _dh_is_mutating_command_mode,
    looks_like_question as _dh_looks_like_question,
    mutation_guard_enabled as _dh_mutation_guard_enabled,
    parse_human_amount as _dh_parse_human_amount,
    resolve_company_tickers_for_search as _dh_resolve_company_tickers_for_search,
    search_tokens as _dh_search_tokens,
    sec_query_parts as _dh_sec_query_parts,
    starts_with_command_verb as _dh_starts_with_command_verb,
    structured_capture_text as _dh_structured_capture_text,
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
from app.services.dashboard_service import ask_ai_local, ask_workspace_ai, dashboard_snapshot, quick_capture, portfolio_intelligence_brief, get_data_integrity_health
from app.services.earnings_transcript_service import list_sec_earnings_releases
from app.services.organizer_service import list_recent_notes, list_tasks
from app.services.portfolio_memory_service import (
    get_cached_morning_brief,
    query_report_facts,
    record_decision,
    record_portfolio_transaction,
    render_morning_brief_bullets,
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
    list_lows_snapshot_pg,
    pg_connect,
    strict_postgres_mode,
    upsert_earnings_calendar_snapshot_pg,
    upsert_lows_snapshot_pg,
)
from app.services.watchlist_service import read_watchlist_rows, write_watchlist_rows
from app.services.workspace_feed_service import add_workspace_message, get_workspace_context, list_workspace_channels, load_workspace_feed
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
# Batch name-map cache: avoids repeated DB hits within short windows
_NAMEMAP_CACHE: dict[str, str] = {}
_NAMEMAP_CACHE_TS: float = 0.0
_NAMEMAP_CACHE_TTL_SEC: float = 120.0  # 2 min
_GLOBAL_SEARCH_LOCK = threading.Lock()
_GLOBAL_SEARCH_CACHE_TTL_SEC = 20.0
_GLOBAL_SEARCH_CACHE: dict[str, tuple[float, list[dict[str, str]]]] = {}
BLUE_CHIPS_SEED_TICKERS: tuple[str, ...] = (
    "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "BRK.B", "JPM", "V", "UNH",
    "XOM", "JNJ", "PG", "AVGO", "MA", "HD", "COST", "KO", "PEP", "ABBV",
    "BAC", "WMT", "CRM", "ORCL", "CVX", "MRK", "CSCO", "ACN", "LIN", "TMO",
    "MCD", "NEE",
)


def _search_tokens(raw_query: str) -> tuple[str, list[str], list[str]]:
    return _dh_search_tokens(raw_query)


def _is_sec_like_query(low: str, toks: list[str]) -> bool:
    return _dh_is_sec_like_query(low, toks)


def _sec_query_parts(toks: list[str], toks_norm: list[str]) -> tuple[set[str], str]:
    return _dh_sec_query_parts(toks, toks_norm)


def _resolve_company_tickers_for_search(company_query: str, limit: int = 8) -> list[str]:
    return _dh_resolve_company_tickers_for_search(company_query, list_companies_fn=list_companies, limit=limit)


def _parse_human_amount(raw: str, default: float = 0.0) -> float:
    return _dh_parse_human_amount(raw, default)


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
    global _NAMEMAP_CACHE, _NAMEMAP_CACHE_TS
    wanted = [_safe_ticker(t) for t in (tickers or []) if _safe_ticker(t)]
    if not wanted:
        return {}

    # Fast path: if all wanted tickers are in the batch cache and cache is fresh, return immediately
    now = time.time()
    if (now - _NAMEMAP_CACHE_TS) < _NAMEMAP_CACHE_TTL_SEC:
        cached_hits = {t: _NAMEMAP_CACHE[t] for t in wanted if t in _NAMEMAP_CACHE and _NAMEMAP_CACHE[t]}
        if len(cached_hits) == len(wanted):
            return cached_hits

    out = lookup_company_name_map(wanted)
    missing = sorted({t for t in wanted if t and not str(out.get(t) or "").strip()})
    if not missing:
        _NAMEMAP_CACHE.update(out)
        _NAMEMAP_CACHE_TS = now
        return out

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
    _NAMEMAP_CACHE.update(out)
    _NAMEMAP_CACHE_TS = now
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

            with requests.get(
                "https://finnhub.io/api/v1/stock/profile2",
                params={"symbol": tk, "token": key},
                timeout=(1.5, 2.5),
                verify=certifi.where(),
                headers={"Accept": "application/json", "User-Agent": "InvestorOS/1.0"},
            ) as r:
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
        futs = {ex.submit(_fetch_company_name_live, tk, key): tk for tk in miss}
        for f in concurrent.futures.as_completed(futs, timeout=8):
            try:
                tk, nm = f.result(timeout=4)
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
        # Sync-fetch ONLY a small batch of tickers with no cached price.
        # This keeps the response fast even on cold start.  The rest refresh async.
        no_price = [tk for tk in stale_or_missing if tk not in out]
        _SYNC_FETCH_LIMIT = 8  # max tickers to fetch synchronously
        if no_price:
            sync_batch = no_price[:_SYNC_FETCH_LIMIT]
            async_batch = no_price[_SYNC_FETCH_LIMIT:]
            fresh_boot = _fetch_quote_map_sync(sync_batch)
            if fresh_boot:
                out.update(fresh_boot)
                with _UNIVERSE_QUOTES_LOCK:
                    cache2 = _load_universe_quotes_cache()
                    ts2 = time.time()
                    for tk, payload in fresh_boot.items():
                        cache2[str(tk or "").strip().upper()] = {"ts": ts2, "payload": payload}
                    _write_universe_quotes_cache(cache2)
            # Remaining no-price tickers get fetched in background
            if async_batch:
                _refresh_universe_quotes_async(async_batch)
        # Refresh stale (but cached) tickers in background.
        stale_only = [tk for tk in stale_or_missing if tk in out]
        if stale_only:
            _refresh_universe_quotes_async(stale_only)
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
            px = _to_float(fi.get("lastPrice"), 0.0)
            prev = _to_float(fi.get("previousClose"), 0.0)
            mcap = _to_float(fi.get("marketCap"), 0.0)
            if px <= 0:
                px = _to_float(fi.get("regularMarketPrice"), 0.0)
            if prev <= 0:
                prev = _to_float(fi.get("regularMarketPreviousClose"), 0.0)
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
        futures = {ex.submit(_fetch_one, tk): tk for tk in uniq}
        for fut in concurrent.futures.as_completed(futures, timeout=12):
            try:
                rec = fut.result(timeout=4)
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
        # NOTE: ^DE10Y and ^JP10Y are not valid yfinance symbols (404).
        # No free real-time German/Japan 10Y yield tickers exist on Yahoo Finance.
        {"id": "vix", "label": "VIX", "symbol": "^VIX", "decimals": 2},
        # Credit & inflation proxies
        {"id": "hyg", "label": "High Yield", "symbol": "HYG", "decimals": 2},
        {"id": "lqd", "label": "Inv Grade", "symbol": "LQD", "decimals": 2},
        {"id": "tip", "label": "TIPS (Infl)", "symbol": "TIP", "decimals": 2},
        # International bonds
        {"id": "bndx", "label": "Intl Bonds", "symbol": "BNDX", "decimals": 2},
        {"id": "emb", "label": "EM Bonds", "symbol": "EMB", "decimals": 2},
        # Sector ETFs
        {"id": "xlk", "label": "Tech", "symbol": "XLK", "decimals": 2},
        {"id": "xlf", "label": "Financials", "symbol": "XLF", "decimals": 2},
        {"id": "xle", "label": "Energy", "symbol": "XLE", "decimals": 2},
        {"id": "xlv", "label": "Healthcare", "symbol": "XLV", "decimals": 2},
        {"id": "xlc", "label": "Comms", "symbol": "XLC", "decimals": 2},
        {"id": "xli", "label": "Industrials", "symbol": "XLI", "decimals": 2},
        {"id": "xlb", "label": "Materials", "symbol": "XLB", "decimals": 2},
        {"id": "xlre", "label": "Real Estate", "symbol": "XLRE", "decimals": 2},
        {"id": "xlu", "label": "Utilities", "symbol": "XLU", "decimals": 2},
        {"id": "xlp", "label": "Staples", "symbol": "XLP", "decimals": 2},
        {"id": "xly", "label": "Discret.", "symbol": "XLY", "decimals": 2},
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
        # TIO=F is delisted on Yahoo Finance — skip Iron Ore.
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

    # Always return cached/home data immediately.
    # If stale, trigger a background refresh so next request is fresh.
    if cache_fresh:
        return {
            "as_of": str(cache.get("updated_at") or dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            "items": items,
            "live_hits": 0,
        }

    # Stale-while-revalidate: return what we have now, refresh in background.
    has_any_data = any(str(v.get("price", "-")) != "-" for v in items.values())
    if has_any_data:
        # Kick off background refresh
        _specs_copy = list(specs)
        def _bg_market_refresh():
            try:
                _bg_items, _bg_hits = _market_brief_fetch_live(_specs_copy)
                if _bg_hits > 0:
                    _save_market_cache(
                        {
                            "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "items": {k: {"price": v.get("price"), "day": v.get("day")} for k, v in _bg_items.items()},
                        }
                    )
            except Exception:
                pass
        import threading as _thr
        _thr.Thread(target=_bg_market_refresh, daemon=True, name="market-brief-bg").start()
        return {
            "as_of": str(cache.get("updated_at") or dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")) + " (refreshing)",
            "items": items,
            "live_hits": 0,
        }

    # No cached data at all (cold start) — must fetch synchronously.
    live_items, live_hits = _market_brief_fetch_live(specs)
    if live_hits > 0:
        items.update(live_items)
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


def _market_brief_fetch_live(specs: list[dict]) -> tuple[dict[str, dict[str, object]], int]:
    """Fetch live prices for market brief symbols. Returns (items_dict, hit_count)."""
    items: dict[str, dict[str, object]] = {}
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
                px = _to_float(fi.get("lastPrice"), 0.0)
                prev = _to_float(fi.get("previousClose"), 0.0)
                if px <= 0:
                    px = _to_float(fi.get("regularMarketPrice"), 0.0)
                if prev <= 0:
                    prev = _to_float(fi.get("regularMarketPreviousClose"), 0.0)
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

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(_fetch_live_row, spec): spec for spec in specs}
            for fut in concurrent.futures.as_completed(futs, timeout=12):
                try:
                    sid, label, price, day = fut.result(timeout=4)
                except Exception:
                    continue
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
    return items, live_hits


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

        with requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={"from": date_from, "to": date_to, "token": key},
            timeout=(2.0, 4.0),
            verify=certifi.where(),
            headers={"Accept": "application/json", "User-Agent": "InvestorOS/1.0"},
        ) as r:
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
            # avoid showing it as "upcoming" forever — mark as reported (no EPS data yet).
            try:
                ed = dt.datetime.strptime(d, "%Y-%m-%d").date()
                if ed < today:
                    rr["reported"] = "1"
                    rr["event_status"] = "reported"
                    rr["verdict"] = ""
                    rr["confidence"] = "preliminary"
            except Exception:
                pass
        out.append(rr)
    final_rows = _verify_reported_earnings_with_sec(out)
    return _cache_and_return([dict(x) for x in final_rows])


def _yfinance_earnings_for_tickers(tickers: list[str], today: dt.date, days_forward: int = 14) -> list[dict[str, str]]:
    """Check upcoming earnings dates for specific tickers via yfinance."""
    if not tickers:
        return []
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    cutoff = today + dt.timedelta(days=max(1, days_forward))
    lookback = today - dt.timedelta(days=3)  # include recent past for reported status
    try:
        import yfinance as yf  # type: ignore

        def _fetch_earnings(sym: str) -> list[dict[str, str]]:
            results = []
            try:
                obj = yf.Ticker(sym)
                # earnings_dates returns a DataFrame with index=datetime
                ed = obj.earnings_dates
                if ed is None or ed.empty:
                    return results
                for idx, row in ed.iterrows():
                    try:
                        edate = idx.date() if hasattr(idx, 'date') else dt.datetime.strptime(str(idx)[:10], "%Y-%m-%d").date()
                    except Exception:
                        continue
                    if edate < lookback or edate > cutoff:
                        continue
                    # Determine time of day
                    hour = idx.hour if hasattr(idx, 'hour') else 0
                    time_label = "pre" if hour < 10 else ("post" if hour >= 16 else "day")
                    entry: dict[str, str] = {
                        "date": edate.isoformat(),
                        "symbol": sym,
                        "company": "-",
                        "time": time_label,
                    }
                    # Check if EPS actual is available (reported)
                    eps_actual = row.get("Reported EPS") if hasattr(row, 'get') else None
                    eps_est = row.get("EPS Estimate") if hasattr(row, 'get') else None
                    if eps_actual is not None and not (isinstance(eps_actual, float) and (eps_actual != eps_actual)):
                        entry["eps_actual"] = str(round(float(eps_actual), 2))
                        entry["reported"] = "1"
                        if eps_est is not None and not (isinstance(eps_est, float) and (eps_est != eps_est)):
                            entry["eps_estimate"] = str(round(float(eps_est), 2))
                            entry["verdict"] = "BEAT" if float(eps_actual) >= float(eps_est) else "MISS"
                            surprise = ((float(eps_actual) - float(eps_est)) / abs(float(eps_est)) * 100) if float(eps_est) != 0 else 0
                            entry["surprise_txt"] = f"{surprise:+.1f}%"
                    results.append(entry)
            except Exception:
                pass
            return results

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(tickers))) as ex:
            futs = {ex.submit(_fetch_earnings, t): t for t in tickers[:30]}
            for fut in concurrent.futures.as_completed(futs, timeout=10):
                try:
                    ticker_results = fut.result(timeout=4)
                except Exception:
                    continue
                for r in ticker_results:
                    key = (r["date"], r["symbol"])
                    if key not in seen:
                        seen.add(key)
                        out.append(r)
    except Exception:
        pass
    out.sort(key=lambda x: (str(x.get("date") or ""), str(x.get("symbol") or "")))
    return out


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

        with requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={"from": week_start.isoformat(), "to": week_end.isoformat(), "token": key},
            timeout=(2.0, 4.0),
            verify=certifi.where(),
            headers={"Accept": "application/json", "User-Agent": "InvestorOS/1.0"},
        ) as r:
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
    con_pg = pg_connect()
    if con_pg is None:
        return rows
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
    except Exception:
        return rows
    finally:
        con_pg.close()

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
    # Auto-generate if stale (>6h) or missing — Cloud Run kills bg threads on scale-to-zero.
    try:
        cached = get_cached_morning_brief()
        _brief_stale = True
        if cached and cached.get("asof"):
            try:
                _age = (dt.datetime.now() - dt.datetime.fromisoformat(str(cached["asof"]))).total_seconds()
                _brief_stale = _age > 21600  # 6 hours
            except Exception:
                pass
        # Also force regen if cached bullets contain garbage (filing metadata)
        if not _brief_stale and cached:
            _garbage_pats = ('10-K', '10-Q', '8-K', '6-K', '20-F')
            _garbage_phrases = ('filing', 'captured', 'Latest filing')
            _cb = [str(b or "") for b in (cached.get("bullets") or [])]
            if any(
                (b.endswith(g) or g in b) for b in _cb for g in _garbage_pats
            ) or any(
                p in b for b in _cb for p in _garbage_phrases
            ):
                _brief_stale = True
        if _brief_stale:
            try:
                from app.services.portfolio_memory_service import save_morning_brief_snapshot
                cached = save_morning_brief_snapshot(limit_holdings=5, source="dashboard-auto")
            except Exception:
                pass
        morning_points = render_morning_brief_bullets(cached, max_bullets=7, allow_runtime_fallback=True)
        if morning_points:
            asof = str((cached or {}).get("asof") or "").strip()
            if asof:
                try:
                    morning_updated_fallback = dt.datetime.fromisoformat(asof).strftime("%H:%M")
                except Exception:
                    morning_updated_fallback = asof[:5] if len(asof) >= 5 else asof
    except Exception:
        pass

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

    # Persist lows to Postgres (so Cloud Run can read them even without report files).
    if lows_both or lows_52:
        try:
            if lows_both:
                upsert_lows_snapshot_pg("both", lows_both)
            if lows_52:
                upsert_lows_snapshot_pg("52", lows_52)
        except Exception:
            pass
    # Fallback: read from Postgres if file parsing returned nothing.
    if not lows_both:
        try:
            lows_both = list_lows_snapshot_pg("both")
        except Exception:
            pass
    if not lows_52:
        try:
            lows_52 = list_lows_snapshot_pg("52")
        except Exception:
            pass

    # Prefer fresh appendix when earnings_radar is stale.
    earnings_primary = appendix_txt if _is_stale(earnings_path, max_age_hours=36) and appendix_txt else (earnings_txt or appendix_txt)
    earnings_week = _extract_earnings_week_rows(earnings_primary, today=today, row_limit=120)
    if not earnings_week and earnings_primary is not appendix_txt:
        earnings_week = _extract_earnings_week_rows(appendix_txt, today=today, row_limit=120)
    # If report parsing yields nothing, fallback to live Finnhub calendar.
    if not earnings_week:
        earnings_week = _finnhub_earnings_week_rows(today=today, row_limit=120)
    # Durable fallback from Postgres snapshot (stale-while-revalidate behavior).
    if not earnings_week:
        ws = (today - dt.timedelta(days=today.weekday())).isoformat()
        we = (today - dt.timedelta(days=today.weekday()) + dt.timedelta(days=6)).isoformat()
        earnings_week = list_earnings_calendar_snapshot_pg(ws, we, limit=200)
    # Supplement with yfinance earnings for the user's own tickers (portfolio + watchlist).
    # Only fetch if we have NO earnings data at all — otherwise skip to keep page fast.
    if not earnings_week:
        try:
            from app.services.dashboard_service import _read_portfolio
            from app.services.portfolio_state_service import read_watchlist_rows_state
            pf_tickers = [_safe_ticker(r.get("ticker", "")) for r in _read_portfolio() if _safe_ticker(r.get("ticker", ""))]
            wl_tickers = [_safe_ticker(r.get("ticker", "")) for r in read_watchlist_rows_state() if _safe_ticker(r.get("ticker", ""))]
            all_my_tickers = list(dict.fromkeys(pf_tickers + wl_tickers))  # dedup, preserve order
            if all_my_tickers:
                yf_rows = _yfinance_earnings_for_tickers(all_my_tickers, today=today, days_forward=10)
                earnings_week.extend(yf_rows)
        except Exception:
            pass
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
    if earnings_week:
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

    result = {
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
    # Auto-sync to Cloud Run in background (non-blocking).
    if (earnings_week or lows_both or lows_52):
        _bg_sync_to_cloud(earnings_week, lows_both, lows_52)
    return result


_CLOUD_SYNC_LOCK = threading.Lock()
_CLOUD_SYNC_LAST: float = 0.0
_CLOUD_SYNC_INTERVAL = 3600  # once per hour max


def _bg_sync_to_cloud(
    earnings: list[dict], lows_both: list[dict], lows_52: list[dict]
) -> None:
    """Push dashboard data to Cloud Run in a daemon thread (fire-and-forget)."""
    global _CLOUD_SYNC_LAST
    cloud_url = os.getenv("CLOUD_SYNC_URL", "").strip()
    if not cloud_url:
        return
    now = time.time()
    with _CLOUD_SYNC_LOCK:
        if (now - _CLOUD_SYNC_LAST) < _CLOUD_SYNC_INTERVAL:
            return
        _CLOUD_SYNC_LAST = now

    def _do_sync():
        try:
            import json as _json

            import requests  # type: ignore

            payload = {
                "earnings_week": earnings,
                "lows_both": lows_both,
                "lows_52": lows_52,
            }
            # Also include cross-asset if available.
            try:
                from app.services.cross_asset_service import get_cross_asset_snapshot

                ca = get_cross_asset_snapshot()
                if ca.get("ok") and ca.get("rows"):
                    payload["cross_asset"] = ca
            except Exception:
                pass
            resp = requests.post(
                cloud_url.rstrip("/") + "/api/internal/sync-dashboard",
                json=payload,
                timeout=(5.0, 30.0),
            )
            if resp.status_code == 200:
                pass  # success
        except Exception:
            pass

    t = threading.Thread(target=_do_sync, daemon=True, name="cloud-dashboard-sync")
    t.start()


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


def _my_companies_metrics(home: dict, preloaded_quotes: dict | None = None) -> dict[str, object]:
    portfolio = list(home.get("portfolio", []) or [])
    cash_usd, cash_lines = _cash_usd_total()

    # ── Stock enrichment (safe when portfolio is empty — loops just don't run) ──
    tks = [_safe_ticker(r.get("ticker", "")) for r in portfolio]
    quote_map = preloaded_quotes if preloaded_quotes is not None else (_last_quote_map(tks) if tks else {})

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
                "shares": sh,
                "shares_num": sh,
                "cost": cost,
                "cost_num": cost,
                "price": px,
                "price_now": px,
                "price_live": live_px,
                "day_pct": day_pct_live,
                "value": val,
                "value_usd": val,
                "pnl": pnl,
                "pnl_usd": pnl,
                "day_pnl_usd": day_pnl,
                "prev_close_value_usd": prev_val,
            }
        )

    # ── AUM = stocks + cash (always computed, never skipped) ──
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
    if not portfolio:
        quick.append("No portfolio holdings yet.")
        if cash_usd > 0:
            quick.append(f"Cash: USD {cash_usd:,.2f}")
    else:
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
    # Run the 4 heaviest dashboard calls in parallel instead of sequentially.
    from app.services.earnings_transcript_service import list_earnings_analysis
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as _dash_ex:
        _fut_market = _dash_ex.submit(_market_brief, home)
        _fut_panels = _dash_ex.submit(dashboard_report_panels)
        _fut_metrics = _dash_ex.submit(_my_companies_metrics, home)
        _fut_earnings = _dash_ex.submit(list_earnings_analysis, 10)
    market = _fut_market.result(timeout=20)
    report_panels = _fut_panels.result(timeout=20)
    my_metrics = _fut_metrics.result(timeout=20)
    earnings_analyses = _fut_earnings.result(timeout=10)
    thesis_breach_alerts = list_thesis_breach_alerts(status="open", limit=10)
    cascade_alerts = list_cascade_alerts(status="open", limit=10)
    ai_accuracy = get_ai_accuracy_stats(days=90)
    ai_audit_timeline = get_ai_audit_timeline(limit=30)
    data_integrity = get_data_integrity_health()
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
            "my_metrics": my_metrics,
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
            "data_integrity": data_integrity,
            "earnings_analyses": earnings_analyses,
        },
    )


def _desk_parse(raw: str) -> tuple[str, str]:
    return _dh_desk_parse(raw)


def _contains_fuzzy_term(text: str, term: str, min_ratio: float = 0.78) -> bool:
    return _dh_contains_fuzzy_term(text, term, min_ratio=min_ratio)


def _has_add_intent(text: str) -> bool:
    return _dh_has_add_intent(text)


def _has_remove_intent(text: str) -> bool:
    return _dh_has_remove_intent(text)


def _looks_like_question(text: str) -> bool:
    return _dh_looks_like_question(text)


def _starts_with_command_verb(text: str) -> bool:
    return _dh_starts_with_command_verb(text)


def _is_mutating_command_mode(mode: str) -> bool:
    return _dh_is_mutating_command_mode(mode)


def _explicit_mutation_request(text: str) -> bool:
    return _dh_explicit_mutation_request(text)


def _mutation_guard_enabled() -> bool:
    return _dh_mutation_guard_enabled()


def _allow_text_mutations() -> bool:
    return _dh_allow_text_mutations()


def _parse_ai_command(raw: str) -> dict[str, str] | None:
    return _dh_parse_ai_command(raw, infer_ticker_fn=_infer_ticker, safe_ticker_fn=_safe_ticker)


def _is_op_like(text: str) -> bool:
    return _dh_is_op_like(text)


def _structured_capture_text(raw: str, mode: str, ticker: str = "") -> str:
    return _dh_structured_capture_text(
        raw,
        mode=mode,
        ticker=ticker,
        infer_ticker_fn=_infer_ticker,
        safe_ticker_fn=_safe_ticker,
    )


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
        record_decision(
            action="cash_upsert",
            ticker=ccy,
            reason=f"Cash set to {ccy} {amt:,.2f} via Ask AI",
            confidence=0.9,
            source="dashboard_ai",
        )
        return True, f"Cash balance updated: {ccy} {amt:,.2f}."
    if mode == "cash_remove":
        ccy = str(cmd.get("currency") or "USD").strip().upper() or "USD"
        rows = _read_cash_rows()
        rows = [r for r in rows if str(r.get("currency") or "").strip().upper() != ccy]
        _write_cash_rows(rows)
        record_decision(
            action="cash_remove",
            ticker=ccy,
            reason=f"Cash removed for {ccy} via Ask AI",
            confidence=0.9,
            source="dashboard_ai",
        )
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
                "category": "",
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

    # Shared parser first (aliases + explicit forms) so local mode still
    # resolves common commands like "add apple to watchlist" or "$AAPL".
    try:
        from app.services.orchestrator.command_parser import extract_ticker
        cand = _safe_ticker(extract_ticker(q))
        if cand:
            return cand
    except Exception:
        pass

    # 1) Strong ticker patterns first.
    tokens: list[str] = []
    for m in re.findall(r"\$([A-Za-z]{1,6})\b|\b([A-Za-z]{2,5})\b", q):
        tk = str(m[0] or m[1] or "").strip().upper()
        if tk:
            tokens.append(tk)

    if core_backend() != "postgres":
        # Local fallback when postgres directory is not available.
        stop_tickers = {
            "ADD", "SET", "PUT", "BUY", "SELL", "THE", "AND", "FOR", "FROM", "WITH",
            "TO", "MY", "YOUR", "USD", "EUR", "GBP", "PLS", "PLEASE", "CASH",
            "REMOVE", "DELETE", "WATCHLIST", "PORTFOLIO", "NOTE", "TASK",
        }
        for tk in tokens[:8]:
            if tk in stop_tickers:
                continue
            if 2 <= len(tk) <= 6:
                return tk
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
def root_redirect():
    return RedirectResponse(url="/today", status_code=302)


@router.get("/dashboard")
def dashboard_page(request: Request):
    # HTMX requests: serve content inline, not a redirect (avoids nested shell)
    if request.headers.get("HX-Request") == "true":
        return JSONResponse({"redirect": "/today?space=dashboard"})
    return RedirectResponse(url="/today?space=dashboard", status_code=302)


@router.get("/dashboard/classic")
def dashboard_page_classic(request: Request):
    return _render(request)


@router.get("/dashboard/feed")
def dashboard_workspace_feed(channel: str = "all", limit: int = 50):
    items = load_workspace_feed(channel=channel, limit=limit)
    return JSONResponse({"ok": True, "channel": str(channel or "all"), "items": items})


@router.get("/dashboard/context")
def dashboard_workspace_context():
    return JSONResponse({"ok": True, **get_workspace_context()})


@router.get("/dashboard/context/live")
def dashboard_workspace_context_live():
    from app.services.workspace_feed_service import get_workspace_live_panel  # noqa: PLC0415
    return JSONResponse({"ok": True, **get_workspace_live_panel()})


@router.get("/dashboard/cross-asset/snapshot")
def dashboard_cross_asset_snapshot(force: int = 0):
    from app.services.cross_asset_service import get_cross_asset_snapshot  # noqa: PLC0415

    payload = get_cross_asset_snapshot(force_refresh=bool(int(force or 0)))
    return JSONResponse(payload)


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
    # Don't persist transport/degraded errors — they're transient and pollute history
    _is_transient_err = (
        "transport issue while waiting for analysis result" in reply.lower()
        or "service temporarily degraded" in reply.lower()
    )
    if not _is_transient_err:
        add_workspace_message(channel="ai-agent", role="assistant", message=reply[:5000])
    return JSONResponse(
        {
            "ok": True,
            "channel": ch,
            "reply": reply[:5000],
            "ts": dt.datetime.now().isoformat(),
        }
    )


@router.post("/dashboard/ai-agent/clear-history")
def dashboard_clear_ai_history():
    from app.services.workspace_feed_service import clear_workspace_channel_history
    n = clear_workspace_channel_history(channel="ai-agent")
    return JSONResponse({"ok": True, "deleted": n})


@router.post("/dashboard/cleanup-stuck-runs")
def dashboard_cleanup_stuck_runs():
    """Mark all agent runs stuck in 'running' for > 5 min as timeout. Called on deploy and via UI."""
    from app.services.postgres_core_service import pg_connect
    try:
        con = pg_connect()
        cur = con.cursor()
        cur.execute(
            """
            UPDATE agent_runs_core
            SET status='timeout', finished_at=started_at,
                error_text='Auto-timeout: no completion received', updated_at=NOW()::text
            WHERE status='running'
              AND started_at < (NOW() - INTERVAL '5 minutes')::text
            RETURNING id
            """
        )
        n = len(cur.fetchall() or [])
        con.commit()
        con.close()
        return JSONResponse({"ok": True, "cleaned": n})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/dashboard/agent-query")
def dashboard_agent_query(
    background_tasks: BackgroundTasks,
    text: str = Form(""),
    ticker: str = Form(""),
):
    """
    Run a free-form investment question through the real agent loop (invest_cli data access).
    Returns immediately with run_uid; the feed auto-refreshes to show the result.
    The agent posts its answer to the ai-agent channel so it appears in the feed.
    """
    query = str(text or "").strip()
    tk = _safe_ticker(str(ticker or "").strip())
    if not query:
        return JSONResponse({"ok": False, "error": "Query required."}, status_code=400)

    # Save user message immediately so it appears in feed right away
    from app.services.workspace_feed_service import add_workspace_message
    add_workspace_message(channel="ai-agent", role="user", message=query)

    def _run_and_post(q: str, t: str) -> None:
        try:
            from tools.agent_worker import run_query
            from app.services.workspace_feed_service import add_workspace_message as _add
            result = run_query(query=q, ticker=t)
            report = str(result.get("report") or "").strip()
            status = str(result.get("status") or "")
            cmds = result.get("commands") or []
            if not report:
                report = "No analysis generated."
            # Append data sources footer if commands were run
            if cmds:
                sources = ", ".join(c.split("--")[0].strip().replace("> invest_app ", "") for c in cmds[:5])
                report = f"{report}\n\n_Sources: {sources}_"
            _add(channel="ai-agent", role="assistant", message=report[:5000])
        except Exception as exc:
            from app.services.workspace_feed_service import add_workspace_message as _add
            _add(channel="ai-agent", role="assistant", message=f"Agent error: {exc}")

    background_tasks.add_task(_run_and_post, query, tk)
    return JSONResponse({
        "ok": True,
        "queued": True,
        "message": "Analyzing… result will appear in the feed shortly.",
    })


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
        if _is_mutating_command_mode(cmd.get("mode", "")) and not _allow_text_mutations():
            return _render(
                request,
                message="Blocked: text-based cash/portfolio/watchlist changes are disabled.",
                ask_q=q,
                ask_a="",
            )
        if _is_mutating_command_mode(cmd.get("mode", "")) and _mutation_guard_enabled() and not _explicit_mutation_request(q):
            return _render(
                request,
                message="Blocked sensitive change. Use explicit prefix: /apply ... (example: /apply add 10k usd cash).",
                ask_q=q,
                ask_a="",
            )
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
            if _is_mutating_command_mode(cmd.get("mode", "")) and not _allow_text_mutations():
                return _render(
                    request,
                    message="Blocked: text-based cash/portfolio/watchlist changes are disabled.",
                    ask_q=payload,
                    ask_a="",
                )
            if _is_mutating_command_mode(cmd.get("mode", "")) and _mutation_guard_enabled() and not _explicit_mutation_request(payload):
                return _render(
                    request,
                    message="Blocked sensitive change. Use explicit prefix: /apply ... (example: /apply add oxy to watchlist).",
                    ask_q=payload,
                    ask_a="",
                )
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
    # Signal preference learning
    try:
        from app.services.cognitive_engine_service import record_signal_action
        record_signal_action("proposal", "dismissed", signal_id=proposal_id)
    except Exception:
        pass
    return _render(request, message="Insight dismissed.")


@router.post("/dashboard/proposals/{proposal_id}/execute")
def dashboard_proposals_execute(request: Request, proposal_id: int):
    out = execute_action_proposal(proposal_id=proposal_id)
    # Signal preference learning
    try:
        from app.services.cognitive_engine_service import record_signal_action
        record_signal_action("proposal", "acted", signal_id=proposal_id)
    except Exception:
        pass
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
    # Signal preference learning
    try:
        from app.services.cognitive_engine_service import record_signal_action
        record_signal_action("proposal", "dismissed", signal_id=proposal_id)
    except Exception:
        pass
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


@router.get("/api/health/data-integrity")
async def api_data_integrity_health():
    return JSONResponse(get_data_integrity_health())


# ── Market history sparkline endpoint ──────────────────────────────────────
_MARKET_ID_TO_SYMBOL: dict[str, tuple[str, bool]] = {
    "sp500": ("^GSPC", False), "nasdaq": ("^IXIC", False), "dow": ("^DJI", False),
    "russell2000": ("^RUT", False), "vix": ("^VIX", False),
    "us2y": ("^IRX", True), "us5y": ("^FVX", True), "us10y": ("^TNX", True), "us30y": ("^TYX", True),
    "hyg": ("HYG", False), "lqd": ("LQD", False), "tip": ("TIP", False),
    "bndx": ("BNDX", False), "emb": ("EMB", False),
    "gold": ("GC=F", False), "silver": ("SI=F", False), "platinum": ("PL=F", False),
    "palladium": ("PA=F", False), "crude": ("CL=F", False), "brent": ("BZ=F", False),
    "natgas": ("NG=F", False), "copper": ("HG=F", False), "aluminum": ("ALI=F", False),
    "heatoil": ("HO=F", False), "gasoline": ("RB=F", False),
    "corn": ("ZC=F", False), "wheat": ("ZW=F", False), "soybeans": ("ZS=F", False),
    "lumber": ("LBS=F", False),
    "cotton": ("CT=F", False), "sugar": ("SB=F", False), "coffee": ("KC=F", False), "cocoa": ("CC=F", False),
    "dxy": ("DX-Y.NYB", False), "eurusd": ("EURUSD=X", False), "eurgbp": ("EURGBP=X", False),
    "usdjpy": ("JPY=X", False), "gbpusd": ("GBPUSD=X", False), "usdcnh": ("CNH=X", False),
    "xlk": ("XLK", False), "xlf": ("XLF", False), "xle": ("XLE", False),
    "xlv": ("XLV", False), "xlc": ("XLC", False), "xli": ("XLI", False),
    "xlb": ("XLB", False), "xlre": ("XLRE", False), "xlu": ("XLU", False),
    "xlp": ("XLP", False), "xly": ("XLY", False),
}

_HIST_CACHE: dict[str, tuple[float, list]] = {}  # key → (timestamp, data)
_HIST_CACHE_TTL = 600  # 10 min


@router.get("/api/market/history/{key}")
def api_market_history(key: str, period: str = "1y"):
    """Return daily close prices for a market data key (sparkline data)."""
    import time as _time
    allowed_periods = {"1mo", "3mo", "6mo", "1y", "2y", "5y"}
    if period not in allowed_periods:
        period = "1y"
    cache_key = f"{key}:{period}"
    cached = _HIST_CACHE.get(cache_key)
    if cached and (_time.time() - cached[0]) < _HIST_CACHE_TTL:
        return JSONResponse({"ok": True, "key": key, "period": period, "data": cached[1]})

    entry = _MARKET_ID_TO_SYMBOL.get(key)
    if not entry:
        return JSONResponse({"ok": False, "error": "unknown key"})
    symbol, div10 = entry
    try:
        import yfinance as yf  # type: ignore
        obj = yf.Ticker(symbol)
        hist = obj.history(period=period, interval="1d")
        if hist.empty:
            return JSONResponse({"ok": False, "error": "no data"})
        closes = hist["Close"].dropna()
        points = []
        for ts, val in closes.items():
            v = float(val)
            if div10 and v > 20:
                v = v / 10.0
            points.append({"d": ts.strftime("%Y-%m-%d"), "c": round(v, 4)})
        _HIST_CACHE[cache_key] = (_time.time(), points)
        return JSONResponse({"ok": True, "key": key, "period": period, "data": points})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)})


@router.get("/api/dashboard/snapshot")
def api_dashboard_snapshot():
    """Full dashboard snapshot as JSON — powers the inline workspace dashboard."""
    snap = dashboard_snapshot()
    home = snap.get("home", {}) or {}
    # Run the heaviest calls in parallel
    from app.services.earnings_transcript_service import list_earnings_analysis
    from app.services.portfolio_memory_service import get_live_portfolio_summary
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as _snap_ex:
        _fut_market = _snap_ex.submit(_market_brief, home)
        _fut_panels = _snap_ex.submit(dashboard_report_panels)
        _fut_portfolio = _snap_ex.submit(get_live_portfolio_summary)
        _fut_earnings = _snap_ex.submit(list_earnings_analysis, 10)
    market = _fut_market.result(timeout=20)
    report_panels = _fut_panels.result(timeout=20)
    earnings_analyses = _fut_earnings.result(timeout=10)
    proposals = list_action_proposals(status="open", limit=60)
    proposals = sorted(proposals, key=lambda p: int(p.get("id") or 0), reverse=True)[:10]
    thesis_breach_alerts = list_thesis_breach_alerts(status="open", limit=10)
    cascade_alerts = list_cascade_alerts(status="open", limit=10)
    ai_accuracy = get_ai_accuracy_stats(days=90)
    ai_audit_timeline = get_ai_audit_timeline(limit=30)
    data_integrity = get_data_integrity_health()
    agent_runs = list_recent_agent_runs(limit=8)
    reflexions = list_recent_reflexions(limit=8)
    # Cross-asset correlations
    cross_asset = {}
    try:
        from app.services.cross_asset_service import get_cross_asset_snapshot
        cross_asset = get_cross_asset_snapshot() or {}
    except Exception:
        pass
    # Portfolio live summary
    portfolio_live = {}
    try:
        portfolio_live = _fut_portfolio.result(timeout=20) or {}
    except Exception:
        pass
    # Market status (open/closed/pre/after)
    market_status = "closed"
    try:
        import pytz as _pytz
        _et = _pytz.timezone("US/Eastern")
        _now_et = dt.datetime.now(_et)
        _wd = _now_et.weekday()  # 0=Mon..6=Sun
        _hm = _now_et.hour * 100 + _now_et.minute
        if _wd >= 5:
            market_status = "closed"
        elif 930 <= _hm < 1600:
            market_status = "open"
        elif 400 <= _hm < 930:
            market_status = "pre-market"
        elif 1600 <= _hm < 2000:
            market_status = "after-hours"
        else:
            market_status = "closed"
    except Exception:
        try:
            _now_utc = dt.datetime.utcnow()
            _hm_utc = _now_utc.hour * 100 + _now_utc.minute
            _wd = _now_utc.weekday()
            if _wd < 5 and 1430 <= _hm_utc < 2100:
                market_status = "open"
            elif _wd < 5 and 900 <= _hm_utc < 1430:
                market_status = "pre-market"
            elif _wd < 5 and 2100 <= _hm_utc < 2400:
                market_status = "after-hours"
        except Exception:
            pass
    # Relative performance: portfolio vs S&P
    sp500_day = 0.0
    try:
        sp = ((market or {}).get("items") or {}).get("sp500") or {}
        sp500_day = float(str(sp.get("day") or "0").replace("%", "").replace("+", ""))
    except Exception:
        pass
    portfolio_day = float(portfolio_live.get("day_change_pct") or 0.0)
    relative_perf = portfolio_day - sp500_day
    # Attention count (proposals + breaches + cascades needing review)
    attention_count = len(proposals) + len(thesis_breach_alerts) + len(cascade_alerts)
    # Greeting
    _hour = dt.datetime.now().hour
    greeting_time = "Good morning" if _hour < 12 else ("Good afternoon" if _hour < 17 else "Good evening")
    return JSONResponse({
        "ok": True,
        "market": market,
        "market_status": market_status,
        "greeting_time": greeting_time,
        "attention_count": attention_count,
        "relative_perf": round(relative_perf, 2),
        "sp500_day_pct": round(sp500_day, 2),
        "portfolio_day_pct": round(portfolio_day, 2),
        "report_panels": report_panels,
        "proposals": proposals,
        "thesis_breach_alerts": thesis_breach_alerts,
        "cascade_alerts": cascade_alerts,
        "ai_accuracy": ai_accuracy,
        "ai_audit_timeline": ai_audit_timeline,
        "data_integrity": data_integrity,
        "earnings_analyses": earnings_analyses,
        "agent_runs": agent_runs,
        "reflexions": reflexions,
        "cross_asset": cross_asset,
        "portfolio_live": portfolio_live,
        "home": {
            "news_general": list(home.get("news_general", []) or [])[:10],
            "news_company": list(home.get("news_company", []) or [])[:10],
        },
        "freshness": snap.get("freshness", {}),
        "counts": snap.get("counts", {}),
        "movers_up": snap.get("movers_up", []),
        "movers_down": snap.get("movers_down", []),
    })


@router.post("/api/agent/runs/cleanup-stuck")
async def api_cleanup_stuck_agent_runs(stale_minutes: int = 60):
    from app.services.proactive_ai_service import cleanup_stuck_agent_runs
    cleaned = cleanup_stuck_agent_runs(stale_minutes=max(5, int(stale_minutes or 60)))
    return JSONResponse({"ok": True, "cleaned": cleaned})


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
    try:
        from app.services.cognitive_engine_service import record_signal_action
        record_signal_action("cascade_alert", "dismissed", signal_id=alert_id)
    except Exception:
        pass
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
            if _is_mutating_command_mode(cmd.get("mode", "")) and not _allow_text_mutations():
                return JSONResponse(
                    {
                        "ok": False,
                        "mode": cmd["mode"],
                        "message": "Blocked: text-based cash/portfolio/watchlist changes are disabled.",
                    },
                    status_code=400,
                )
            if _is_mutating_command_mode(cmd.get("mode", "")) and _mutation_guard_enabled() and not _explicit_mutation_request(payload):
                return JSONResponse(
                    {
                        "ok": False,
                        "mode": cmd["mode"],
                        "message": "Blocked sensitive change. Use explicit prefix: /apply ...",
                    },
                    status_code=400,
                )
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


@router.get("/api/universe/full")
def api_universe_full():
    """Full universe data as JSON — powers inline portfolio/watchlist/bluechips view."""
    # Build a lightweight home dict with just portfolio rows (skip expensive dashboard_snapshot).
    from app.services.dashboard_service import _read_portfolio
    portfolio_raw = _read_portfolio()
    name_map_pf = _company_name_map([_safe_ticker(r.get("ticker", "")) for r in portfolio_raw])
    for r in portfolio_raw:
        t = _safe_ticker(r.get("ticker", ""))
        r["name"] = str(name_map_pf.get(t) or t)
        r["industry"] = "Unknown"
    home = {"portfolio": portfolio_raw}
    watchlist_rows = _read_watchlist_rows()
    wl_tickers = [_safe_ticker(r.get("ticker", "")) for r in watchlist_rows]
    blue_rows = _read_blue_chips_rows()
    blue_tickers = [_safe_ticker(r.get("ticker", "")) for r in blue_rows]
    pf_tickers = [_safe_ticker(r.get("ticker", "")) for r in portfolio_raw]
    # Portfolio tickers first so they get sync-fetched on cold start, then watchlist, then bluechips
    seen_tickers: set[str] = set()
    all_quote_tickers: list[str] = []
    for t in pf_tickers + wl_tickers + blue_tickers:
        if t and t not in seen_tickers:
            seen_tickers.add(t)
            all_quote_tickers.append(t)
    quote_map_all = _last_quote_map(all_quote_tickers)
    metrics = _my_companies_metrics(home, preloaded_quotes=quote_map_all)

    # Enrich watchlist
    wl_quotes = {t: quote_map_all.get(t, {}) for t in wl_tickers}
    wl_added_map = {str(r.get("ticker") or "").strip().upper(): str(r.get("added_at") or "") for r in watchlist_rows}
    wl_cur_px = {
        t: (_to_float((wl_quotes.get(t, {}) or {}).get("price"), 0.0)
            or _to_float(next((r.get("price_now") for r in watchlist_rows if _safe_ticker(r.get("ticker", "")) == t), 0.0), 0.0)
            or _to_float(next((r.get("price") for r in watchlist_rows if _safe_ticker(r.get("ticker", "")) == t), 0.0), 0.0))
        for t in wl_tickers
    }
    wl_since_added = _since_added_pct_map(wl_added_map, wl_cur_px)
    name_map = _company_name_map(wl_tickers)
    thesis_map = _load_thesis_map()
    watchlist_full = []
    for r in watchlist_rows:
        t = _safe_ticker(r.get("ticker", ""))
        q = wl_quotes.get(t, {})
        day_live = _pick_day_pct(q.get("day_pct"), r.get("day_pct"))
        day_safe = _to_float(day_live, 0.0)
        live_px = _to_float(q.get("price"), 0.0) or _to_float(r.get("price_now"), 0.0) or _to_float(r.get("price"), 0.0)
        mcap_live = _to_float(q.get("market_cap"), 0.0) or _parse_human_amount(str(r.get("market_cap") or ""), 0.0)
        watchlist_full.append({
            **r, "ticker": t, "name": str(name_map.get(t) or "").strip(),
            "price_now": live_px, "market_cap": mcap_live,
            "since_added_pct": wl_since_added.get(t),
            "day_pct": day_live, "day_pct_safe": day_safe, "day_abs_pct": abs(day_safe),
            "has_thesis": bool(thesis_map.get(t, {}).get("thesis")),
        })

    # Enrich bluechips
    blue_quotes = {t: quote_map_all.get(t, {}) for t in blue_tickers}
    blue_added_map = {str(r.get("ticker") or "").strip().upper(): str(r.get("added_at") or "") for r in blue_rows}
    blue_cur_px = {
        t: (_to_float((blue_quotes.get(t, {}) or {}).get("price"), 0.0)
            or _to_float(next((r.get("price_now") for r in blue_rows if _safe_ticker(r.get("ticker", "")) == t), 0.0), 0.0)
            or _to_float(next((r.get("price") for r in blue_rows if _safe_ticker(r.get("ticker", "")) == t), 0.0), 0.0))
        for t in blue_tickers
    }
    blue_since_added = _since_added_pct_map(blue_added_map, blue_cur_px)
    blue_name_map = _company_name_map(blue_tickers)
    bluechips_full = []
    for r in blue_rows:
        t = _safe_ticker(r.get("ticker", ""))
        q = blue_quotes.get(t, {})
        day_live = _pick_day_pct(q.get("day_pct"), r.get("day_pct"))
        day_safe = _to_float(day_live, 0.0)
        live_px = _to_float(q.get("price"), 0.0) or _to_float(r.get("price_now"), 0.0) or _to_float(r.get("price"), 0.0)
        mcap_live = _to_float(q.get("market_cap"), 0.0) or _parse_human_amount(str(r.get("market_cap") or ""), 0.0)
        bluechips_full.append({
            **r, "ticker": t, "name": str(blue_name_map.get(t) or "").strip(),
            "price_now": live_px, "market_cap": mcap_live,
            "since_added_pct": blue_since_added.get(t),
            "day_pct": day_live, "day_pct_safe": day_safe, "day_abs_pct": abs(day_safe),
        })

    timeline_events = _recent_universe_timeline(limit=60)
    quarterly_reviews = _load_quarterly_reviews()
    cash_rows = _read_cash_rows()

    # Portfolio accounts
    from app.services.portfolio_state_service import read_portfolio_accounts
    portfolio_accounts = read_portfolio_accounts()

    # Build category groups for watchlist
    from app.services.portfolio_state_service import read_watchlist_groups
    wl_group_settings = read_watchlist_groups()  # {name: {color, emoji, sort_order}}
    wl_categories: dict[str, list] = {}
    for item in watchlist_full:
        cat = str(item.get("category") or "").strip() or "General"
        wl_categories.setdefault(cat, []).append(item)
    # Include empty groups from DB so newly created groups appear immediately
    for gname in wl_group_settings:
        if gname not in wl_categories:
            wl_categories[gname] = []
    # Sort: by group sort_order (from settings), then General last, then alphabetical
    def _group_sort_key(c: str) -> tuple:
        gs = wl_group_settings.get(c, {})
        return (gs.get("sort_order", 999), c == "General", c)
    sorted_cats = sorted(wl_categories.keys(), key=_group_sort_key)
    watchlist_grouped = []
    for c in sorted_cats:
        gs = wl_group_settings.get(c, {})
        watchlist_grouped.append({
            "category": c,
            "color": gs.get("color", ""),
            "emoji": gs.get("emoji", ""),
            "sort_order": gs.get("sort_order", 0),
            "items": wl_categories[c],
        })

    return JSONResponse({
        "ok": True,
        "portfolio": list(home.get("portfolio", []) or []),
        "metrics": metrics,
        "portfolio_accounts": portfolio_accounts,
        "watchlist_full": watchlist_full,
        "watchlist_grouped": watchlist_grouped,
        "watchlist_group_settings": wl_group_settings,
        "bluechips_full": bluechips_full,
        "thesis_map": thesis_map,
        "timeline_events": timeline_events,
        "quarterly_reviews": quarterly_reviews,
        "cash_rows": cash_rows,
        "movers_up": [],
        "movers_down": [],
    })


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
    space = "watchlist" if requested == "watchlist" else "portfolio"
    if request.headers.get("HX-Request") == "true":
        return JSONResponse({"redirect": f"/today?space={space}"})
    return RedirectResponse(url=f"/today?space={space}", status_code=302)


# ── Portfolio Review Room + Watchlist Thesis Board ─────────────────────────


def _load_thesis_map() -> dict[str, dict]:
    """Return {TICKER: {thesis, thesis_summary, target_price, key_questions, phase, ...}} from watchlist_thesis_core."""
    from app.services.postgres_core_service import pg_connect, pg_enabled
    if not pg_enabled():
        return {}
    con = pg_connect()
    if con is None:
        return {}
    try:
        cur = con.cursor()
        cur.execute(
            """SELECT ticker, thesis, thesis_summary, conviction_rating,
                      time_horizon, invalidation_criteria, strategy_tag,
                      status, target_price, key_questions, phase,
                      created_at, updated_at
               FROM watchlist_thesis_core ORDER BY updated_at DESC"""
        )
        out: dict[str, dict] = {}
        for r in cur.fetchall():
            out[str(r[0] or "").upper()] = {
                "ticker": str(r[0] or ""),
                "thesis": str(r[1] or ""),
                "thesis_summary": str(r[2] or ""),
                "conviction_rating": int(r[3] or 0),
                "time_horizon": str(r[4] or ""),
                "invalidation_criteria": str(r[5] or ""),
                "strategy_tag": str(r[6] or "CORE"),
                "status": str(r[7] or "active"),
                "target_price": float(r[8]) if r[8] is not None else None,
                "key_questions": r[9] if isinstance(r[9], list) else [],
                "phase": str(r[10] or "watching"),
                "created_at": str(r[11] or ""),
                "updated_at": str(r[12] or ""),
            }
        return out
    except Exception:
        return {}
    finally:
        con.close()


def _load_earnings_map(tickers: list[str]) -> dict[str, list[dict]]:
    """Return {TICKER: [earnings_rows]} from earnings_analysis_core."""
    from app.services.postgres_core_service import pg_connect, pg_enabled
    if not pg_enabled() or not tickers:
        return {}
    con = pg_connect()
    if con is None:
        return {}
    try:
        cur = con.cursor()
        ph = ",".join(["%s"] * len(tickers))
        cur.execute(
            f"""SELECT ticker, quarter, summary, guidance_direction, filing_date, created_at
               FROM earnings_analysis_core
               WHERE UPPER(ticker) IN ({ph})
               ORDER BY created_at DESC""",
            tuple(t.upper() for t in tickers),
        )
        out: dict[str, list[dict]] = {}
        for r in cur.fetchall():
            t = str(r[0] or "").upper()
            if t not in out:
                out[t] = []
            out[t].append({
                "quarter": str(r[1] or ""),
                "summary": str(r[2] or ""),
                "guidance_direction": str(r[3] or ""),
                "filing_date": str(r[4] or ""),
                "created_at": str(r[5] or ""),
            })
        return out
    except Exception:
        return {}
    finally:
        con.close()


def _load_assumptions_map(tickers: list[str]) -> dict[str, list[dict]]:
    """Return {TICKER: [assumption_rows]} from position_assumptions_core."""
    from app.services.postgres_core_service import pg_connect, pg_enabled
    if not pg_enabled() or not tickers:
        return {}
    con = pg_connect()
    if con is None:
        return {}
    try:
        cur = con.cursor()
        ph = ",".join(["%s"] * len(tickers))
        cur.execute(
            f"""SELECT ticker, assumption_text, measurable_condition, status, last_checked_at
               FROM position_assumptions_core
               WHERE UPPER(ticker) IN ({ph})
               ORDER BY id DESC""",
            tuple(t.upper() for t in tickers),
        )
        out: dict[str, list[dict]] = {}
        for r in cur.fetchall():
            t = str(r[0] or "").upper()
            if t not in out:
                out[t] = []
            out[t].append({
                "text": str(r[1] or ""),
                "condition": str(r[2] or ""),
                "status": str(r[3] or "active"),
                "last_checked": str(r[4] or ""),
            })
        return out
    except Exception:
        return {}
    finally:
        con.close()


def _load_conviction_map(tickers: list[str]) -> dict[str, dict]:
    """Return {TICKER: {score, components}} from conviction_scores_core."""
    from app.services.postgres_core_service import pg_connect, pg_enabled
    if not pg_enabled() or not tickers:
        return {}
    con = pg_connect()
    if con is None:
        return {}
    try:
        cur = con.cursor()
        ph = ",".join(["%s"] * len(tickers))
        cur.execute(
            f"""SELECT ticker, score, components, updated_at
               FROM conviction_scores_core
               WHERE UPPER(ticker) IN ({ph})""",
            tuple(t.upper() for t in tickers),
        )
        out: dict[str, dict] = {}
        for r in cur.fetchall():
            t = str(r[0] or "").upper()
            out[t] = {
                "score": int(r[1] or 0),
                "components": r[2] if isinstance(r[2], dict) else {},
                "updated_at": str(r[3] or ""),
            }
        return out
    except Exception:
        return {}
    finally:
        con.close()


def _load_records_map(tickers: list[str], limit_per: int = 10) -> dict[str, list[dict]]:
    """Return {TICKER: [record_rows]} from investment_records_core."""
    from app.services.postgres_core_service import pg_connect, pg_enabled
    if not pg_enabled() or not tickers:
        return {}
    con = pg_connect()
    if con is None:
        return {}
    try:
        cur = con.cursor()
        ph = ",".join(["%s"] * len(tickers))
        cur.execute(
            f"""SELECT ticker, kind, title, body, sentiment, created_at
               FROM investment_records_core
               WHERE UPPER(ticker) IN ({ph}) AND status != 'deleted'
               ORDER BY created_at DESC
               LIMIT %s""",
            tuple(t.upper() for t in tickers) + (len(tickers) * limit_per,),
        )
        out: dict[str, list[dict]] = {}
        for r in cur.fetchall():
            t = str(r[0] or "").upper()
            if t not in out:
                out[t] = []
            if len(out[t]) < limit_per:
                out[t].append({
                    "kind": str(r[1] or "note"),
                    "title": str(r[2] or ""),
                    "body": str(r[3] or ""),
                    "sentiment": str(r[4] or ""),
                    "created_at": str(r[5] or ""),
                })
        return out
    except Exception:
        return {}
    finally:
        con.close()


def _load_quarterly_reviews() -> list[dict]:
    """Return quarterly review records."""
    from app.services.postgres_core_service import pg_connect, pg_enabled
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            """SELECT id, quarter, year, notes, portfolio_return, benchmark_return, created_at, updated_at
               FROM quarterly_reviews_core ORDER BY year DESC, quarter DESC"""
        )
        out = []
        for r in cur.fetchall():
            out.append({
                "id": int(r[0] or 0),
                "quarter": str(r[1] or ""),
                "year": int(r[2] or 0),
                "notes": str(r[3] or ""),
                "portfolio_return": float(r[4]) if r[4] is not None else None,
                "benchmark_return": float(r[5]) if r[5] is not None else None,
                "created_at": str(r[6] or ""),
                "updated_at": str(r[7] or ""),
            })
        return out
    except Exception:
        return []
    finally:
        con.close()


@router.get("/my_universe/portfolio")
def portfolio_review_page(request: Request):
    return RedirectResponse(url="/today?space=portfolio", status_code=302)


@router.get("/my_universe/watchlist")
def watchlist_thesis_page(request: Request):
    return RedirectResponse(url="/today?space=watchlist", status_code=302)


@router.put("/api/thesis/{ticker}")
async def api_save_thesis(ticker: str, request: Request):
    """Save/update thesis for a ticker."""
    t = _safe_ticker(ticker)
    if not t:
        return JSONResponse({"ok": False, "error": "ticker_required"}, status_code=400)
    body = await request.json()
    from app.services.postgres_core_service import pg_connect, pg_enabled
    if not pg_enabled():
        return JSONResponse({"ok": False, "error": "pg_required"}, status_code=500)
    con = pg_connect()
    if con is None:
        return JSONResponse({"ok": False, "error": "db_error"}, status_code=500)
    try:
        cur = con.cursor()
        thesis_text = str(body.get("thesis") or "").strip()[:3000]
        target_price = body.get("target_price")
        if target_price is not None:
            try:
                target_price = float(target_price)
            except (ValueError, TypeError):
                target_price = None
        time_horizon = str(body.get("time_horizon") or "").strip()[:120]
        invalidation = str(body.get("invalidation_criteria") or "").strip()[:1000]
        status = str(body.get("status") or "").strip()[:32] or "active"
        phase = str(body.get("phase") or "").strip()[:32]
        key_questions = body.get("key_questions")
        if not isinstance(key_questions, list):
            key_questions = None
        now = dt.datetime.now().isoformat()
        cur.execute(
            """INSERT INTO watchlist_thesis_core
               (ticker, thesis, thesis_summary, target_price, key_questions, phase,
                time_horizon, invalidation_criteria, status, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s)
               ON CONFLICT(ticker) DO UPDATE SET
                 thesis = COALESCE(NULLIF(EXCLUDED.thesis, ''), watchlist_thesis_core.thesis),
                 thesis_summary = COALESCE(NULLIF(EXCLUDED.thesis, ''), watchlist_thesis_core.thesis_summary),
                 target_price = COALESCE(EXCLUDED.target_price, watchlist_thesis_core.target_price),
                 key_questions = CASE WHEN EXCLUDED.key_questions != '[]'::jsonb THEN EXCLUDED.key_questions ELSE watchlist_thesis_core.key_questions END,
                 phase = COALESCE(NULLIF(EXCLUDED.phase, ''), watchlist_thesis_core.phase),
                 time_horizon = COALESCE(NULLIF(EXCLUDED.time_horizon, ''), watchlist_thesis_core.time_horizon),
                 invalidation_criteria = COALESCE(NULLIF(EXCLUDED.invalidation_criteria, ''), watchlist_thesis_core.invalidation_criteria),
                 status = COALESCE(NULLIF(EXCLUDED.status, ''), watchlist_thesis_core.status),
                 updated_at = EXCLUDED.updated_at""",
            (
                t,
                thesis_text,
                thesis_text,
                target_price,
                json.dumps(key_questions) if key_questions else "[]",
                phase,
                time_horizon,
                invalidation,
                status,
                now,
                now,
            ),
        )
        con.commit()
        record_decision(ticker=t, action="thesis_update", reason=thesis_text[:500], confidence=0.95, source="thesis_board")
        return JSONResponse({"ok": True, "ticker": t})
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=500)
    finally:
        con.close()


@router.post("/api/thesis/{ticker}/question")
async def api_add_question(ticker: str, request: Request):
    """Add a key question to a ticker's thesis."""
    t = _safe_ticker(ticker)
    if not t:
        return JSONResponse({"ok": False, "error": "ticker_required"}, status_code=400)
    body = await request.json()
    question = str(body.get("question") or "").strip()[:500]
    if not question:
        return JSONResponse({"ok": False, "error": "question_required"}, status_code=400)
    from app.services.postgres_core_service import pg_connect, pg_enabled
    if not pg_enabled():
        return JSONResponse({"ok": False, "error": "pg_required"}, status_code=500)
    con = pg_connect()
    if con is None:
        return JSONResponse({"ok": False, "error": "db_error"}, status_code=500)
    try:
        cur = con.cursor()
        now = dt.datetime.now().isoformat()
        new_q = json.dumps({"question": question, "answered": False, "date": now[:10]})
        cur.execute(
            """UPDATE watchlist_thesis_core
               SET key_questions = key_questions || %s::jsonb,
                   updated_at = %s
               WHERE ticker = %s""",
            (f"[{new_q}]", now, t),
        )
        if cur.rowcount == 0:
            cur.execute(
                """INSERT INTO watchlist_thesis_core (ticker, key_questions, created_at, updated_at)
                   VALUES (%s, %s::jsonb, %s, %s)""",
                (t, f"[{new_q}]", now, now),
            )
        con.commit()
        return JSONResponse({"ok": True})
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=500)
    finally:
        con.close()


@router.put("/api/thesis/{ticker}/question/{idx}")
async def api_toggle_question(ticker: str, idx: int, request: Request):
    """Toggle a question's answered state."""
    t = _safe_ticker(ticker)
    if not t:
        return JSONResponse({"ok": False, "error": "ticker_required"}, status_code=400)
    from app.services.postgres_core_service import pg_connect, pg_enabled
    if not pg_enabled():
        return JSONResponse({"ok": False, "error": "pg_required"}, status_code=500)
    con = pg_connect()
    if con is None:
        return JSONResponse({"ok": False, "error": "db_error"}, status_code=500)
    try:
        cur = con.cursor()
        cur.execute("SELECT key_questions FROM watchlist_thesis_core WHERE ticker = %s", (t,))
        row = cur.fetchone()
        if not row or not row[0]:
            return JSONResponse({"ok": False, "error": "not_found"}, status_code=404)
        qs = row[0] if isinstance(row[0], list) else []
        if idx < 0 or idx >= len(qs):
            return JSONResponse({"ok": False, "error": "invalid_index"}, status_code=400)
        qs[idx]["answered"] = not qs[idx].get("answered", False)
        if qs[idx]["answered"]:
            qs[idx]["answered_date"] = dt.datetime.now().isoformat()[:10]
        now = dt.datetime.now().isoformat()
        cur.execute(
            "UPDATE watchlist_thesis_core SET key_questions = %s::jsonb, updated_at = %s WHERE ticker = %s",
            (json.dumps(qs), now, t),
        )
        con.commit()
        return JSONResponse({"ok": True})
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=500)
    finally:
        con.close()


@router.post("/api/thesis/{ticker}/journal")
async def api_add_journal(ticker: str, request: Request):
    """Add a journal entry for a ticker (stored in investment_records_core)."""
    t = _safe_ticker(ticker)
    if not t:
        return JSONResponse({"ok": False, "error": "ticker_required"}, status_code=400)
    body = await request.json()
    text = str(body.get("text") or "").strip()[:3000]
    if not text:
        return JSONResponse({"ok": False, "error": "text_required"}, status_code=400)
    from app.services.postgres_core_service import pg_connect, pg_enabled
    if not pg_enabled():
        return JSONResponse({"ok": False, "error": "pg_required"}, status_code=500)
    con = pg_connect()
    if con is None:
        return JSONResponse({"ok": False, "error": "db_error"}, status_code=500)
    try:
        cur = con.cursor()
        cur.execute("SELECT COALESCE(MAX(id),0)+1 FROM investment_records_core")
        next_id = cur.fetchone()[0]
        now = dt.datetime.now().isoformat()
        cur.execute(
            """INSERT INTO investment_records_core
               (id, kind, ticker, title, body, source, created_by, status, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (next_id, "watchlist_journal", t, f"Journal: {t}", text, "thesis_board", "user", "open", now, now),
        )
        con.commit()
        return JSONResponse({"ok": True})
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=500)
    finally:
        con.close()


@router.post("/api/quarterly-review")
async def api_save_quarterly_review(request: Request):
    """Save/update a quarterly review."""
    body = await request.json()
    quarter = str(body.get("quarter") or "").strip().upper()[:2]
    year = int(body.get("year") or 0)
    notes = str(body.get("notes") or "").strip()[:5000]
    if not quarter or not year:
        return JSONResponse({"ok": False, "error": "quarter_and_year_required"}, status_code=400)
    p_ret = body.get("portfolio_return")
    b_ret = body.get("benchmark_return")
    try:
        p_ret = float(p_ret) if p_ret is not None else None
    except (ValueError, TypeError):
        p_ret = None
    try:
        b_ret = float(b_ret) if b_ret is not None else None
    except (ValueError, TypeError):
        b_ret = None
    from app.services.postgres_core_service import pg_connect, pg_enabled
    if not pg_enabled():
        return JSONResponse({"ok": False, "error": "pg_required"}, status_code=500)
    con = pg_connect()
    if con is None:
        return JSONResponse({"ok": False, "error": "db_error"}, status_code=500)
    try:
        cur = con.cursor()
        now = dt.datetime.now().isoformat()
        cur.execute(
            """INSERT INTO quarterly_reviews_core (quarter, year, notes, portfolio_return, benchmark_return, updated_at)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT(quarter, year) DO UPDATE SET
                 notes = EXCLUDED.notes,
                 portfolio_return = EXCLUDED.portfolio_return,
                 benchmark_return = EXCLUDED.benchmark_return,
                 updated_at = EXCLUDED.updated_at""",
            (quarter, year, notes, p_ret, b_ret, now),
        )
        con.commit()
        return JSONResponse({"ok": True})
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return JSONResponse({"ok": False, "error": str(exc)[:200]}, status_code=500)
    finally:
        con.close()


# ── Idea List API ──────────────────────────────────────────────────────────

@router.get("/api/ideas")
async def api_list_ideas(request: Request, status: str = "open"):
    from app.services.postgres_core_service import list_ideas_pg
    ideas = list_ideas_pg(status=status)
    return JSONResponse({"ok": True, "ideas": ideas})


@router.post("/api/ideas")
async def api_add_idea(request: Request):
    body = await request.json()
    title = str(body.get("title") or "").strip()[:500]
    if not title:
        return JSONResponse({"ok": False, "error": "title_required"}, status_code=400)
    ticker = body.get("ticker")
    if ticker:
        ticker = str(ticker).strip().upper()[:16] or None
    notes = str(body.get("notes") or "").strip()[:2000]
    source = str(body.get("source") or "manual").strip()[:100]
    from app.services.postgres_core_service import add_idea_pg
    idea_id = add_idea_pg(title=title, ticker=ticker, notes=notes, source=source)
    if idea_id:
        return JSONResponse({"ok": True, "id": idea_id})
    return JSONResponse({"ok": False, "error": "db_error"}, status_code=500)


@router.put("/api/ideas/{idea_id}/status")
async def api_update_idea_status(idea_id: int, request: Request):
    body = await request.json()
    status = str(body.get("status") or "").strip()
    if status not in ("open", "promoted", "archived"):
        return JSONResponse({"ok": False, "error": "invalid_status"}, status_code=400)
    from app.services.postgres_core_service import update_idea_status_pg
    ok = update_idea_status_pg(idea_id, status)
    return JSONResponse({"ok": ok})


@router.delete("/api/ideas/{idea_id}")
async def api_delete_idea(idea_id: int, request: Request):
    from app.services.postgres_core_service import delete_idea_pg
    ok = delete_idea_pg(idea_id)
    return JSONResponse({"ok": ok})


@router.post("/my_companies/portfolio/upsert")
def my_companies_portfolio_upsert(
    ticker: str = Form(""),
    side: str = Form(""),
    shares: str = Form(""),
    cost: str = Form(""),
    note: str = Form(""),
    trade_reason: str = Form(""),
    buy_belief: str = Form(""),
    account: str = Form("Main"),
):
    t = _safe_ticker(ticker)
    if not t:
        return JSONResponse({"ok": False, "message": "Ticker required."}, status_code=400)
    sh = _to_float(shares, 0.0)
    c = _to_float(cost, 0.0)
    acct = str(account or "Main").strip() or "Main"
    side_key = str(side or "").strip().lower()
    if side_key not in {"buy", "sell", "edit"}:
        side_key = ""
    rows = _read_portfolio_file()
    existing = None
    kept: list[dict[str, str]] = []
    for r in rows:
        if _safe_ticker(r.get("ticker", "")) == t and str(r.get("account") or "Main") == acct and existing is None:
            existing = r
        else:
            kept.append(r)

    # Direct edit path: overwrite shares and cost exactly as provided.
    if side_key == "edit":
        if sh <= 0 or c <= 0:
            return JSONResponse({"ok": False, "message": "Shares and average cost are both required."}, status_code=400)
        kept.append({"ticker": t, "shares": f"{sh:g}", "cost": f"{c:.6f}", "note": str(note or "").strip() or (existing or {}).get("note", ""), "account": acct})
        _write_portfolio_file(kept)
        record_portfolio_transaction(
            ticker=t, action="edit", shares=sh, price=c,
            note=str(note or "").strip() or "Position edited (direct set)",
            source="my_companies_form",
            meta={"was_existing_position": bool(existing), "edit_mode": True},
        )
        return JSONResponse({"ok": True, "message": f"Position updated: {sh:g} shares @ ${c:,.2f}"})

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
        raw_reason = str(trade_reason or "").strip()
        reason_key = normalize_sell_reason(raw_reason)
        reason_label = label_for_sell_reason(reason_key)
        free_reason = str(note or "").strip()
        if not free_reason and raw_reason and reason_key == "other":
            free_reason = raw_reason
        if next_sh > 0:
            kept.append(
                {
                    "ticker": t,
                    "shares": f"{next_sh:g}",
                    "cost": f"{old_c:.6f}",
                    "note": str(note or "").strip() or str(removed.get("note", "")).strip(),
                    "account": acct,
                }
            )
        _write_portfolio_file(kept)
        record_portfolio_transaction(
            ticker=t,
            action="sell",
            shares=sell_qty,
            price=(c if c > 0 else old_c),
            note=free_reason or ("Sold from portfolio" if next_sh > 0 else "Removed from portfolio"),
            source="my_companies_form",
            meta={
                "decision_reason": reason_key,
                "decision_reason_label": reason_label,
                "decision_reason_raw": raw_reason,
                "was_existing_position": bool(existing),
                "account": acct,
            },
        )
        record_decision(
            action=("portfolio_sell" if next_sh > 0 else "portfolio_remove"),
            ticker=t,
            reason=(free_reason or f"Sell ({reason_label})"),
            confidence=0.95,
            source="my_companies_form",
        )
        if next_sh > 0:
            return JSONResponse({"ok": True, "message": "Position updated (sold shares)."})
        return JSONResponse({"ok": True, "message": "Removed from portfolio."})

    # legacy remove path for callers that still post shares<=0 without side.
    if sh <= 0:
        removed = existing or {}
        raw_reason = str(trade_reason or "").strip()
        reason_key = normalize_sell_reason(raw_reason)
        reason_label = label_for_sell_reason(reason_key)
        free_reason = str(note or "").strip()
        if not free_reason and raw_reason and reason_key == "other":
            free_reason = raw_reason
        _write_portfolio_file(kept)
        record_portfolio_transaction(
            ticker=t,
            action="sell",
            shares=_to_float(removed.get("shares", ""), 0.0),
            price=(c if c > 0 else _to_float(removed.get("cost", ""), 0.0)),
            note=free_reason or "Removed from portfolio",
            source="my_companies_form",
            meta={
                "decision_reason": reason_key,
                "decision_reason_label": reason_label,
                "decision_reason_raw": raw_reason,
                "was_existing_position": bool(existing),
            },
        )
        record_decision(
            action="portfolio_remove",
            ticker=t,
            reason=(free_reason or f"Removed from portfolio ({reason_label})"),
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
                "account": acct,
            }
        )
        _write_portfolio_file(kept)
        raw_reason = str(trade_reason or "").strip()
        reason_key = normalize_buy_reason(raw_reason) if raw_reason else "existing_position_add"
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
                "decision_reason_raw": raw_reason,
                "was_existing_position": True,
                "account": acct,
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

    raw_reason = str(trade_reason or "").strip()
    reason_key = normalize_buy_reason(raw_reason)
    reason_label = label_for_buy_reason(reason_key)
    belief_key = normalize_buy_belief(buy_belief)
    belief_label = label_for_buy_belief(belief_key)
    kept.append(
        {
            "ticker": t,
            "shares": f"{sh:g}",
            "cost": f"{c:.6f}",
            "note": str(note or "").strip(),
            "account": acct,
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
            "decision_reason_raw": raw_reason,
            "buy_belief": belief_key,
            "buy_belief_label": belief_label,
            "was_existing_position": False,
            "account": acct,
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
    account: str = Form("Main"),
):
    return my_companies_portfolio_upsert(
        ticker=ticker,
        side=side,
        shares=shares,
        cost=cost,
        note=note,
        trade_reason=trade_reason,
        buy_belief=buy_belief,
        account=account,
    )


@router.post("/my_companies/watchlist/add")
def my_companies_watchlist_add(
    ticker: str = Form(""),
    reason: str = Form(""),
    category: str = Form(""),
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
            "category": str(category or "").strip(),
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
    category: str = Form(""),
):
    return my_companies_watchlist_add(ticker=ticker, reason=reason, category=category)


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


@router.post("/my_universe/watchlist/set-category")
def my_universe_watchlist_set_category(
    ticker: str = Form(""),
    category: str = Form(""),
):
    """Update the category for a watchlist ticker."""
    t = _safe_ticker(ticker)
    if not t:
        return JSONResponse({"ok": False, "message": "Ticker required."}, status_code=400)
    rows = _read_watchlist_rows()
    found = False
    for r in rows:
        if _safe_ticker(r.get("ticker", "")) == t:
            r["category"] = str(category or "").strip()
            found = True
            break
    if not found:
        return JSONResponse({"ok": False, "message": f"{t} not in watchlist."}, status_code=404)
    _write_watchlist_file(rows)
    return JSONResponse({"ok": True, "message": f"Category updated to '{category}' for {t}."})


@router.get("/api/watchlist-groups")
def api_watchlist_groups():
    """Return all watchlist group settings (color, emoji, sort_order)."""
    from app.services.portfolio_state_service import read_watchlist_groups
    groups = read_watchlist_groups()
    return JSONResponse({"ok": True, "groups": groups})


@router.post("/api/watchlist-groups/upsert")
def api_watchlist_groups_upsert(
    group_name: str = Form(""),
    color: str = Form(""),
    emoji: str = Form(""),
    sort_order: str = Form("0"),
):
    """Create or update a watchlist group's color/emoji."""
    from app.services.portfolio_state_service import upsert_watchlist_group
    name = str(group_name or "").strip()
    if not name:
        return JSONResponse({"ok": False, "message": "Group name required."}, status_code=400)
    ok = upsert_watchlist_group(name, color=color, emoji=emoji, sort_order=int(sort_order or 0))
    if not ok:
        return JSONResponse({"ok": False, "message": "Failed to save group."}, status_code=500)
    return JSONResponse({"ok": True, "message": f"Group '{name}' updated."})


@router.post("/api/watchlist-groups/delete")
def api_watchlist_groups_delete(
    group_name: str = Form(""),
):
    """Delete a watchlist group (moves tickers to General)."""
    from app.services.portfolio_state_service import delete_watchlist_group
    name = str(group_name or "").strip()
    if not name:
        return JSONResponse({"ok": False, "message": "Group name required."}, status_code=400)
    ok = delete_watchlist_group(name)
    return JSONResponse({"ok": True, "message": f"Group '{name}' deleted."})


@router.get("/api/portfolio-accounts")
def api_portfolio_accounts():
    """Return all portfolio accounts."""
    from app.services.portfolio_state_service import read_portfolio_accounts
    accounts = read_portfolio_accounts()
    return JSONResponse({"ok": True, "accounts": accounts})


@router.post("/api/portfolio-accounts/upsert")
def api_portfolio_accounts_upsert(
    account_name: str = Form(""),
    color: str = Form("#6366f1"),
    sort_order: str = Form("0"),
):
    """Create or update a portfolio account."""
    from app.services.portfolio_state_service import upsert_portfolio_account
    name = str(account_name or "").strip()
    if not name:
        return JSONResponse({"ok": False, "message": "Account name required."}, status_code=400)
    ok = upsert_portfolio_account(name, color=color, sort_order=int(sort_order or 0))
    if not ok:
        return JSONResponse({"ok": False, "message": "Failed to save account."}, status_code=500)
    return JSONResponse({"ok": True, "message": f"Account '{name}' saved."})


@router.post("/api/portfolio-accounts/delete")
def api_portfolio_accounts_delete(
    account_name: str = Form(""),
):
    """Delete a portfolio account (moves positions to Main)."""
    from app.services.portfolio_state_service import delete_portfolio_account
    name = str(account_name or "").strip()
    if not name:
        return JSONResponse({"ok": False, "message": "Account name required."}, status_code=400)
    if name == "Main":
        return JSONResponse({"ok": False, "message": "Cannot delete the Main account."}, status_code=400)
    ok = delete_portfolio_account(name)
    return JSONResponse({"ok": True, "message": f"Account '{name}' deleted. Positions moved to Main."})


@router.post("/api/portfolio-accounts/rename")
def api_portfolio_accounts_rename(
    old_name: str = Form(""),
    new_name: str = Form(""),
):
    """Rename a portfolio account."""
    from app.services.portfolio_state_service import rename_portfolio_account
    old = str(old_name or "").strip()
    new = str(new_name or "").strip()
    if not old or not new:
        return JSONResponse({"ok": False, "message": "Both old and new name required."}, status_code=400)
    if old == new:
        return JSONResponse({"ok": True, "message": "Name unchanged."})
    ok = rename_portfolio_account(old, new)
    if not ok:
        return JSONResponse({"ok": False, "message": "Failed to rename account."}, status_code=500)
    return JSONResponse({"ok": True, "message": f"Account renamed to '{new}'."})


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
    record_decision(
        action="cash_upsert",
        ticker=ccy,
        reason=f"Cash set to {ccy} {amt:,.2f}",
        confidence=0.98,
        source="my_universe_form",
    )
    return JSONResponse({"ok": True, "message": "Cash balance updated."})


@router.post("/my_universe/cash/remove")
def my_universe_cash_remove(
    currency: str = Form("USD"),
):
    ccy = str(currency or "USD").strip().upper()
    rows = _read_cash_rows()
    rows = [r for r in rows if str(r.get("currency") or "").strip().upper() != ccy]
    _write_cash_rows(rows)
    record_decision(
        action="cash_remove",
        ticker=ccy,
        reason=f"Cash removed for {ccy}",
        confidence=0.98,
        source="my_universe_form",
    )
    return JSONResponse({"ok": True, "message": "Cash balance removed."})


def _recent_universe_timeline(limit: int = 80) -> list[dict[str, str]]:
    if core_backend() != "postgres":
        return []
    con = pg_connect()
    if con is None:
        return []
    events: list[dict[str, str]] = []
    lim = max(10, min(int(limit or 80), 300))
    per_source = lim // 2  # cap per source to avoid one dominating
    try:
        cur = con.cursor()

        # 1. Portfolio / watchlist decisions
        cur.execute(
            """
            SELECT created_at, action, COALESCE(ticker,''), COALESCE(reason,''), COALESCE(source,'')
              FROM decision_log_core
             WHERE action LIKE 'portfolio%%'
                OR action LIKE 'watchlist%%'
                OR action LIKE 'cash%%'
                OR action LIKE 'blue_chip%%'
             ORDER BY created_at DESC
             LIMIT %s
            """,
            (per_source,),
        )
        for ts, action, ticker, reason, source in (cur.fetchall() or []):
            # Build human-readable label
            act = str(action or "")
            tk = str(ticker or "")
            label = act.replace("_", " ").title()
            if tk:
                label = f"{tk} — {label}"
            if str(reason or "").strip():
                label += f" ({str(reason).strip()[:80]})"
            events.append({"ts": str(ts or ""), "kind": "decision", "ticker": tk,
                           "label": label, "icon": "📋", "color": "blue"})

        # 2. Portfolio transactions (trades)
        cur.execute(
            """
            SELECT created_at, COALESCE(ticker,''), COALESCE(action,''),
                   COALESCE(shares,0), COALESCE(price,0), COALESCE(note,'')
              FROM portfolio_transactions_core
             ORDER BY created_at DESC LIMIT %s
            """,
            (per_source,),
        )
        for ts, ticker, action, shares, price, note in (cur.fetchall() or []):
            tk = str(ticker or "")
            act = str(action or "").upper()
            sh = _to_float(shares, 0.0)
            px = _to_float(price, 0.0)
            is_buy = act in ("BUY", "ADD")
            label = f"{'Bought' if is_buy else 'Sold'} {tk} — {sh:g} shares @ ${px:,.2f}"
            if str(note or "").strip():
                label += f" | {str(note).strip()[:60]}"
            events.append({"ts": str(ts or ""), "kind": "trade", "ticker": tk,
                           "label": label, "icon": "💰" if is_buy else "📤",
                           "color": "green" if is_buy else "red"})

        # 3. SEC filings for held + watchlist tickers
        cur.execute(
            """
            SELECT created_at, COALESCE(ticker,''), COALESCE(headline,''),
                   COALESCE(event_type,''), COALESCE(summary,'')
              FROM events_core
             WHERE event_type = 'sec_filing'
             ORDER BY created_at DESC LIMIT %s
            """,
            (per_source,),
        )
        for ts, ticker, headline, etype, summary in (cur.fetchall() or []):
            tk = str(ticker or "")
            hl = str(headline or "").strip()
            label = f"{tk} — {hl}" if hl else f"{tk} — SEC Filing"
            events.append({"ts": str(ts or ""), "kind": "filing", "ticker": tk,
                           "label": label, "icon": "🏛️", "color": "amber"})

        # 4. Insider trades
        cur.execute(
            """
            SELECT created_at, COALESCE(ticker,''), COALESCE(headline,''),
                   COALESCE(summary,'')
              FROM events_core
             WHERE event_type = 'insider_trade_signal'
             ORDER BY created_at DESC LIMIT %s
            """,
            (per_source,),
        )
        for ts, ticker, headline, summary in (cur.fetchall() or []):
            tk = str(ticker or "")
            hl = str(headline or "").strip()
            label = f"{tk} — {hl}" if hl else f"{tk} — Insider Trade"
            events.append({"ts": str(ts or ""), "kind": "insider", "ticker": tk,
                           "label": label, "icon": "👤", "color": "purple"})

        # 5. Earnings analyses
        cur.execute(
            """
            SELECT created_at, COALESCE(ticker,''), COALESCE(period,''),
                   COALESCE(guidance_direction,''), COALESCE(management_tone,'')
              FROM earnings_analysis_core
             ORDER BY created_at DESC LIMIT %s
            """,
            (per_source,),
        )
        for ts, ticker, period, guidance, tone in (cur.fetchall() or []):
            tk = str(ticker or "")
            per = str(period or "").strip()
            parts = [f"{tk} — Earnings {per}"]
            if str(guidance or "").strip():
                parts.append(f"Guidance: {guidance}")
            if str(tone or "").strip():
                parts.append(f"Tone: {tone}")
            label = " | ".join(parts)
            events.append({"ts": str(ts or ""), "kind": "earnings", "ticker": tk,
                           "label": label, "icon": "📊", "color": "green"})

        # 6. AI proposals (acted on or notable)
        cur.execute(
            """
            SELECT created_at, COALESCE(ticker,''), COALESCE(headline,''),
                   COALESCE(action_type,''), COALESCE(status,'')
              FROM proactive_proposals_core
             WHERE status IN ('pending','executed','dismissed')
             ORDER BY created_at DESC LIMIT %s
            """,
            (per_source,),
        )
        for ts, ticker, headline, action_type, status in (cur.fetchall() or []):
            tk = str(ticker or "")
            hl = str(headline or "").strip()
            st = str(status or "")
            label = f"{tk} — AI: {hl}" if hl else f"{tk} — AI Proposal"
            if st == "executed":
                label += " ✓ Acted"
            elif st == "dismissed":
                label += " ✗ Dismissed"
            events.append({"ts": str(ts or ""), "kind": "proposal", "ticker": tk,
                           "label": label, "icon": "🤖", "color": "indigo"})

    except Exception:
        import traceback
        traceback.print_exc()
        return events  # return what we have so far
    finally:
        con.close()
    events.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
    return events[:lim]


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
            elif not _dh_global_search_match(low, toks, toks_norm, tk, nm, kind, rpt, fact):
                continue
            if tk:
                title = f"{tk} · {kind}"
                if nm:
                    title = f"{tk} · {nm} · {kind}"
                _dh_global_search_add_result(results, seen, "filing", title, f"/company_file?t={quote(tk)}", fact[:160] or rpt[:160])
            else:
                _dh_global_search_add_result(results, seen, "filing", (rpt or kind or "Filing")[:120], "/reports", fact[:160])
            if len(results) >= lim:
                break
        return len(results) - added_before

    def _add_filings_core_hits(tks: list[str], max_rows: int = 12) -> int:
        added_before = len(results)
        want_tks = [str(x or "").strip().upper() for x in list(tks or []) if str(x or "").strip()]
        if not want_tks:
            return 0
        forms = sorted({f.upper() for f in requested_forms if str(f or "").strip()})
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
                    _dh_global_search_add_result(results, seen, "filing", f"{tk} · {fm} SEC Filing", f"/company_file/sec?t={quote(tk)}&form={quote(fm)}", subtitle)
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
        return len(results) - added_before

    for kind, title, url, subtitle in _DH_GLOBAL_SEARCH_APP_ROUTES:
        if _dh_global_search_match(low, toks, toks_norm, title, subtitle):
            _dh_global_search_add_result(results, seen, kind, title, url, subtitle)

    if len(results) < lim:
        try:
            task_seen: set[int] = set()
            task_scan = max(300, min(900, lim * 70))
            for open_only in (True, False):
                for r in list_tasks(open_only=open_only, limit=task_scan):
                    _dh_global_search_add_task_row(
                        r,
                        low=low,
                        toks=toks,
                        toks_norm=toks_norm,
                        needle=needle,
                        results=results,
                        seen=seen,
                        task_seen=task_seen,
                        quote_fn=quote,
                    )
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
                _dh_global_search_add_note_row(
                    r,
                    low=low,
                    toks=toks,
                    toks_norm=toks_norm,
                    needle=needle,
                    results=results,
                    seen=seen,
                    quote_fn=quote,
                )
                if len(results) >= lim:
                    break
        except Exception:
            pass

    if len(results) < lim:
        try:
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
                        if not txt or not _dh_global_search_match(low, toks, toks_norm, txt, day):
                            continue
                        _dh_global_search_add_result(results, seen, "note", f"Daily note {day}", f"/organizer?day={quote(day)}", txt[:160])
                        if len(results) >= lim:
                            break
                finally:
                    con_pg.close()
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
                        _dh_global_search_add_result(results, seen, "filing", f"{tk} · {title}", f"/company_file?t={quote(tk)}", excerpt)
                        if len(results) >= lim:
                            break
                    if (not matched) and sec_rows:
                        for sr in sec_rows[:3]:
                            form = str(sr.get("form") or "").strip().upper()
                            title = str(sr.get("title") or f"{form} Earnings Release").strip()[:120]
                            excerpt = str(sr.get("excerpt") or "").strip()[:140]
                            _dh_global_search_add_result(
                                results,
                                seen,
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
                        _dh_global_search_add_result(
                            results,
                            seen,
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
                _dh_global_search_add_result(results, seen, "report", title, f"/reports?open={quote(nm)}", str(r.get("kind") or "Report"))
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
                    _dh_global_search_add_result(results, seen, "company", f"{tk} - {nm}", f"/company_file?t={quote(tk)}", ind)
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


@router.get("/api/ticker-search")
def api_ticker_search(q: str = "", limit: int = 10):
    """
    Fast ticker autocomplete: returns [{ticker, name, industry}] matched against
    the full company universe. Used by all add-to-watchlist / add-to-portfolio inputs.
    """
    needle = str(q or "").strip()
    if not needle or len(needle) < 1:
        return JSONResponse({"ok": True, "results": []})
    lim = max(4, min(20, int(limit or 10)))
    try:
        rows = list_companies(query=needle, page=1, page_size=lim * 2, scope="all", sort="mcap_desc").get("rows") or []
        out = []
        seen: set[str] = set()
        for r in rows:
            tk = str(getattr(r, "ticker", "") or (r.get("ticker") if isinstance(r, dict) else "") or "").strip().upper()
            nm = str(getattr(r, "name", "") or (r.get("name") if isinstance(r, dict) else "") or "").strip()
            ind = str(getattr(r, "industry", "") or (r.get("industry") if isinstance(r, dict) else "") or "").strip()
            if not tk or tk in seen:
                continue
            seen.add(tk)
            out.append({"ticker": tk, "name": nm or tk, "industry": ind})
            if len(out) >= lim:
                break
        return JSONResponse({"ok": True, "results": out})
    except Exception:
        return JSONResponse({"ok": True, "results": []})


# ── Dashboard Data Sync (local → cloud) ──────────────────────────


@router.post("/api/internal/sync-dashboard")
async def api_sync_dashboard(request: Request):
    """Accept earnings + lows data pushed from the local server to populate Cloud SQL."""
    if core_backend() != "postgres":
        return JSONResponse({"ok": False, "error": "not_postgres"}, status_code=400)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad_json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "expected_object"}, status_code=400)
    counts: dict[str, int] = {}
    # Earnings
    earnings = payload.get("earnings_week")
    if isinstance(earnings, list) and earnings:
        try:
            counts["earnings"] = upsert_earnings_calendar_snapshot_pg(earnings)
        except Exception:
            counts["earnings"] = 0
    # Lows
    for cat in ("both", "52"):
        lows = payload.get(f"lows_{cat}")
        if isinstance(lows, list) and lows:
            try:
                counts[f"lows_{cat}"] = upsert_lows_snapshot_pg(cat, lows)
            except Exception:
                counts[f"lows_{cat}"] = 0
    # Cross-asset
    cross_asset = payload.get("cross_asset")
    if isinstance(cross_asset, dict) and cross_asset.get("rows"):
        try:
            from app.services.cross_asset_service import save_cross_asset_snapshot_pg  # noqa: PLC0415
            save_cross_asset_snapshot_pg(cross_asset)
            counts["cross_asset"] = len(cross_asset.get("rows", []))
        except Exception:
            counts["cross_asset"] = 0
    return JSONResponse({"ok": True, "synced": counts})
