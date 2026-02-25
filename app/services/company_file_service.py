from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.config import DATA_DIR, ROOT
from app.core.db import core_conn as _conn_core
from app.core.date import parse_datetime_flexible
from app.core.filing_text import resolve_filing_path
from app.core.proposal_text import clean_task_text, is_ai_task_text
from app.core.ticker import normalize_ticker as _normalize_ticker
from app.services.postgres_core_service import (
    add_company_reminder_pg,
    add_todo_pg,
    add_workspace_journal_note_pg,
    core_backend,
    delete_company_reminder_pg,
    delete_todo_pg,
    delete_workspace_journal_note_pg,
    list_action_proposals_pg,
    list_company_reminders_pg,
    list_todos_pg,
    list_recent_notes_pg,
    toggle_company_reminder_pg,
    toggle_todo_pg,
    update_company_reminder_pg,
    update_todo_pg,
    update_workspace_journal_note_pg,
)
from app.services.price_metrics_service import get_price_metrics
from app.services.mini_statements_service import get_mini_statements, compute_financial_deltas
from app.services.company_intel_service import get_company_intel
from app.services.earnings_transcript_service import (
    list_earnings_transcripts,
    list_quarterly_result_signals,
    list_sec_earnings_releases,
)
from app.services.company_lookup_service import market_cap_map, prefetch_market_cap_async


INDEX_FILES: list[tuple[str, str, str]] = [
    ("sp500", "S&P 500", "sp500.txt"),
    ("russell1000", "Russell 1000", "russell1000.txt"),
    ("russell2000", "Russell 2000", "russell2000.txt"),
    ("ftse100", "FTSE 100", "ftse100.txt"),
    ("dax40", "DAX 40", "dax40.txt"),
    ("cac40", "CAC 40", "cac40.txt"),
    ("kospi200", "KOSPI 200", "kospi200.txt"),
    ("tsx60", "S&P/TSX 60", "tsx60.txt"),
    ("asx200", "S&P/ASX 200", "asx200.txt"),
]

MOAT_OPTIONS: list[tuple[str, str]] = [
    ("network_effect", "Network Effect"),
    ("switching_costs", "Switching Costs"),
    ("cost_advantage", "Cost Advantage"),
    ("intangible_assets", "Intangible Assets"),
    ("efficient_scale", "Efficient Scale"),
    ("brand", "Brand"),
    ("distribution", "Distribution"),
    ("regulatory_license", "Regulatory / License"),
    ("ip_patents", "IP / Patents"),
    ("ecosystem_lockin", "Ecosystem Lock-in"),
]

FILING_CATEGORY_ORDER: list[tuple[str, str]] = [
    ("annual", "Annual Reports"),
    ("quarterly", "Quarterly Reports"),
    ("proxy", "Proxy / Shareholder"),
    ("current", "Current Reports"),
    ("ownership", "Ownership / Beneficial"),
    ("other", "Other Filings"),
]

_LOOKUP_CACHE_LOCK = threading.Lock()
_LOOKUP_CACHE_TTL_SEC = 45.0
_PROFILES_CACHE: tuple[float, dict[str, dict[str, str]]] = (0.0, {})
_NAMES_CACHE: tuple[float, dict[str, str]] = (0.0, {})
_UNIVERSE_CACHE_TTL_SEC = 45.0
_UNIVERSE_ALL_CACHE: tuple[float, list[str]] = (0.0, [])
_UNIVERSE_REG_CACHE: dict[str, tuple[float, list[str]]] = {}


def _clean_seg_label(label: str) -> str:
    s = str(label or "").strip()
    if not s:
        return "Other"
    if ":" in s:
        s = s.split(":", 1)[1]
    s = re.sub(r"(Member|Axis)$", "", s, flags=re.I)
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    s = s.replace("_", " ").replace("-", " ")
    s = re.sub(r"\bi Phone\b", "iPhone", s, flags=re.I)
    s = re.sub(r"\bi Pad\b", "iPad", s, flags=re.I)
    s = re.sub(r"\bU S\b", "U.S.", s, flags=re.I)
    s = re.sub(r"\bUsa\b", "U.S.", s, flags=re.I)
    s = re.sub(r"\bUs And Canada\b", "U.S. and Canada", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:60] if s else "Other"


def _normalize_segments_for_view(seg: dict[str, object]) -> dict[str, object]:
    out_product: list[dict[str, object]] = []
    out_geo: list[dict[str, object]] = []
    for row in list((seg or {}).get("product") or []):
        if not isinstance(row, dict):
            continue
        lbl = _clean_seg_label(str(row.get("label") or ""))
        out_product.append({"label": lbl, "value": row.get("value"), "pct": row.get("pct")})
    for row in list((seg or {}).get("geography") or []):
        if not isinstance(row, dict):
            continue
        lbl = _clean_seg_label(str(row.get("label") or ""))
        pct = float(row.get("pct") or 0.0)
        if pct <= 0:
            continue
        out_geo.append({"label": lbl, "value": row.get("value"), "pct": pct})
    return {"product": out_product[:8], "geography": out_geo[:8]}


@dataclass
class CompanyRow:
    ticker: str
    name: str
    country: str
    industry: str
    market_cap: str


def safe_resolve_filing_path(path_s: str) -> Path | None:
    return resolve_filing_path(path_s)


def _filing_category(form: str) -> str:
    f = str(form or "").strip().upper()
    if f in {"10-K", "20-F", "40-F"}:
        return "annual"
    if f in {"10-Q"}:
        return "quarterly"
    if "DEF 14A" in f or "DEFA14A" in f:
        return "proxy"
    if f in {"8-K", "6-K"}:
        return "current"
    if f in {"3", "4", "5"} or f.startswith("SC 13"):
        return "ownership"
    return "other"


def _parse_mcap_num(text: str) -> float:
    s = str(text or "").strip().upper().replace("$", "").replace(",", "")
    if not s or s == "-":
        return -1.0
    mult = 1.0
    if s.endswith("T"):
        mult = 1e12
        s = s[:-1]
    elif s.endswith("B"):
        mult = 1e9
        s = s[:-1]
    elif s.endswith("M"):
        mult = 1e6
        s = s[:-1]
    elif s.endswith("K"):
        mult = 1e3
        s = s[:-1]
    try:
        return float(s) * mult
    except Exception:
        return -1.0


def _load_index_tickers(index_key: str) -> list[str]:
    k = str(index_key or "").strip().lower()
    file_name = ""
    for kk, _lbl, fn in INDEX_FILES:
        if kk == k:
            file_name = fn
            break
    if not file_name:
        return []
    p = DATA_DIR / "index_lists" / file_name
    if not p.exists():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        t = _normalize_ticker(ln)
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _company_profiles() -> dict[str, dict[str, str]]:
    global _PROFILES_CACHE
    now = time.time()
    with _LOOKUP_CACHE_LOCK:
        ts, cached = _PROFILES_CACHE
        if cached and (now - float(ts)) <= _LOOKUP_CACHE_TTL_SEC:
            return dict(cached)
    con = _conn_core()
    out: dict[str, dict[str, str]] = {}
    try:
        rows = con.execute(
            "SELECT ticker, name, country, industry FROM company_profile_cache"
        ).fetchall()
        for r in rows:
            t = _normalize_ticker(str(r["ticker"] or ""))
            if not t:
                continue
            out[t] = {
                "name": str(r["name"] or "").strip(),
                "country": str(r["country"] or "").strip(),
                "industry": str(r["industry"] or "").strip(),
            }
    finally:
        con.close()
    with _LOOKUP_CACHE_LOCK:
        _PROFILES_CACHE = (now, dict(out))
    return out


def _similar_companies_for(
    ticker: str,
    *,
    industry: str,
    market_cap: str,
    profiles: dict[str, dict[str, str]],
    names: dict[str, str],
    existing_competitors: list[dict[str, str | int]],
    limit: int = 8,
) -> list[dict[str, str]]:
    tk = _normalize_ticker(ticker)
    if not tk:
        return []
    ind = str(industry or "").strip()
    base_mcap = _parse_mcap_num(market_cap)
    existing = {
        _normalize_ticker(str(x.get("ticker") or ""))
        for x in list(existing_competitors or [])
        if _normalize_ticker(str(x.get("ticker") or ""))
    }

    candidates: list[str] = []
    for t, p in profiles.items():
        ct = _normalize_ticker(t)
        if not ct or ct == tk or ct in existing:
            continue
        if ind and str(p.get("industry") or "").strip() != ind:
            continue
        candidates.append(ct)
    if not candidates:
        return []

    mcap_map = market_cap_map(candidates)
    scored: list[tuple[float, str]] = []
    for ct in candidates:
        cmp_mcap = _parse_mcap_num(str(mcap_map.get(ct) or "-"))
        if base_mcap > 0 and cmp_mcap > 0:
            score = abs((cmp_mcap - base_mcap) / base_mcap)
        elif cmp_mcap > 0:
            score = 10.0
        else:
            score = 99.0
        scored.append((score, ct))
    scored.sort(key=lambda x: (x[0], x[1]))

    out: list[dict[str, str]] = []
    for _score, ct in scored[: max(1, min(20, int(limit or 8)))]:
        p = profiles.get(ct, {})
        out.append(
            {
                "ticker": ct,
                "name": str(p.get("name") or names.get(ct) or ct).strip(),
                "industry": str(p.get("industry") or "").strip(),
                "market_cap": str(mcap_map.get(ct) or "-").strip() or "-",
            }
        )
    return out


def _companies_name_map() -> dict[str, str]:
    global _NAMES_CACHE
    now = time.time()
    with _LOOKUP_CACHE_LOCK:
        ts, cached = _NAMES_CACHE
        if cached and (now - float(ts)) <= _LOOKUP_CACHE_TTL_SEC:
            return dict(cached)
    con = _conn_core()
    out: dict[str, str] = {}
    try:
        for r in con.execute("SELECT ticker, name FROM companies").fetchall():
            t = _normalize_ticker(str(r["ticker"] or ""))
            if not t:
                continue
            out[t] = str(r["name"] or "").strip()
    finally:
        con.close()
    with _LOOKUP_CACHE_LOCK:
        _NAMES_CACHE = (now, dict(out))
    return out


def _live_price(ticker: str) -> str:
    t = _normalize_ticker(ticker)
    if not t:
        return "-"
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return "-"
    try:
        obj = yf.Ticker(t)
        fi = (obj.fast_info or {})
        px = float(fi.get("last_price") or 0.0)
        if px <= 0:
            px = float(fi.get("regular_market_price") or 0.0)
        if px <= 0:
            info = (obj.info or {})
            px = float(info.get("currentPrice") or 0.0)
        if px <= 0:
            hist = obj.history(period="5d", interval="1d")
            if not hist.empty:
                px = float(hist["Close"].dropna().iloc[-1] or 0.0)
        if px > 0:
            return f"{px:.2f}"
    except Exception:
        return "-"
    return "-"


def _saved_lists() -> list[dict[str, str | int]]:
    con = _conn_core()
    rows: list[dict[str, str | int]] = []
    try:
        sql = """
            SELECT l.id, l.name, COUNT(i.id) AS cnt
            FROM company_lists l
            LEFT JOIN company_list_items i ON i.list_id = l.id
            GROUP BY l.id, l.name
            ORDER BY l.name ASC
        """
        for r in con.execute(sql).fetchall():
            rows.append(
                {
                    "id": int(r["id"]),
                    "name": str(r["name"] or ""),
                    "count": int(r["cnt"] or 0),
                }
            )
    finally:
        con.close()
    return rows


def _saved_list_tickers(list_name: str) -> list[str]:
    n = str(list_name or "").strip()
    if not n:
        return []
    con = _conn_core()
    try:
        row = con.execute("SELECT id FROM company_lists WHERE name = ?", (n,)).fetchone()
        if not row:
            return []
        lid = int(row["id"])
        out = []
        seen: set[str] = set()
        for r in con.execute("SELECT ticker FROM company_list_items WHERE list_id = ?", (lid,)).fetchall():
            t = _normalize_ticker(str(r["ticker"] or ""))
            if not t or t in seen:
                continue
            seen.add(t)
            out.append(t)
        return out
    finally:
        con.close()


def _moat_tickers(moat_key: str) -> list[str]:
    mk = str(moat_key or "").strip().lower()
    if not mk:
        return []
    con = _conn_core()
    try:
        out = []
        seen: set[str] = set()
        for r in con.execute("SELECT ticker FROM company_moat_tags WHERE moat_key = ?", (mk,)).fetchall():
            t = _normalize_ticker(str(r["ticker"] or ""))
            if not t or t in seen:
                continue
            seen.add(t)
            out.append(t)
        return out
    finally:
        con.close()


def _all_universe() -> list[str]:
    global _UNIVERSE_ALL_CACHE
    now = time.time()
    with _LOOKUP_CACHE_LOCK:
        ts, cached = _UNIVERSE_ALL_CACHE
        if cached and (now - float(ts)) <= _UNIVERSE_CACHE_TTL_SEC:
            return list(cached)
    con = _conn_core()
    seen: set[str] = set()
    out: list[str] = []
    try:
        for sql in (
            "SELECT ticker FROM company_profile_cache",
            "SELECT ticker FROM companies",
            "SELECT ticker FROM company_list_items",
            "SELECT ticker FROM universe_registry WHERE is_us_listed = 1",
        ):
            try:
                rows = con.execute(sql).fetchall()
            except Exception:
                continue
            for r in rows:
                t = _normalize_ticker(str(r["ticker"] or ""))
                if not t or t in seen:
                    continue
                seen.add(t)
                out.append(t)
    finally:
        con.close()
    with _LOOKUP_CACHE_LOCK:
        _UNIVERSE_ALL_CACHE = (now, list(out))
    return out


def _registry_universe(mode: str) -> list[str]:
    global _UNIVERSE_REG_CACHE
    m = str(mode or "").strip().lower()
    if m not in {"us_listed", "us_otc", "otc_only"}:
        return []
    now = time.time()
    with _LOOKUP_CACHE_LOCK:
        row = _UNIVERSE_REG_CACHE.get(m)
        if row:
            ts, cached = row
            if cached and (now - float(ts)) <= _UNIVERSE_CACHE_TTL_SEC:
                return list(cached)
    con = _conn_core()
    seen: set[str] = set()
    out: list[str] = []
    try:
        if m == "us_listed":
            sql = "SELECT ticker FROM universe_registry WHERE is_us_listed = 1"
        elif m == "otc_only":
            sql = "SELECT ticker FROM universe_registry WHERE is_otc = 1"
        else:
            sql = "SELECT ticker FROM universe_registry WHERE is_us_listed = 1 OR is_otc = 1"
        rows = con.execute(sql).fetchall()
        for r in rows:
            t = _normalize_ticker(str(r["ticker"] or ""))
            if not t or t in seen:
                continue
            seen.add(t)
            out.append(t)
    except Exception:
        return []
    finally:
        con.close()
    with _LOOKUP_CACHE_LOCK:
        _UNIVERSE_REG_CACHE[m] = (now, list(out))
    return out


def _read_my_companies() -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    p = DATA_DIR / "portfolio.csv"
    if p.exists():
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = [x.strip() for x in ln.split(",")]
            t = _normalize_ticker(parts[0] if parts else "")
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    w = DATA_DIR / "my_watchlist.txt"
    if w.exists():
        for ln in w.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            parts = [x.strip() for x in s.split(",")]
            t = _normalize_ticker(parts[0] if parts else "")
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def index_filters() -> list[dict[str, str | int]]:
    rows: list[dict[str, str | int]] = []
    for key, label, _fn in INDEX_FILES:
        cnt = len(_load_index_tickers(key))
        rows.append({"key": key, "label": label, "count": cnt})
    return rows


def list_filters() -> list[dict[str, str | int]]:
    return _saved_lists()


def moat_filters() -> list[dict[str, str | int]]:
    con = _conn_core()
    cnt_map: dict[str, int] = {}
    try:
        for r in con.execute("SELECT moat_key, COUNT(*) AS cnt FROM company_moat_tags GROUP BY moat_key").fetchall():
            cnt_map[str(r["moat_key"] or "").strip().lower()] = int(r["cnt"] or 0)
    finally:
        con.close()
    out: list[dict[str, str | int]] = []
    for key, label in MOAT_OPTIONS:
        out.append({"key": key, "label": label, "count": int(cnt_map.get(key, 0))})
    return out


def list_companies(
    query: str = "",
    industry: str = "",
    index_key: str = "",
    list_name: str = "",
    moat_key: str = "",
    market: str = "",
    size: str = "",
    scope: str = "all",
    sort: str = "mcap_desc",
    page: int = 1,
    page_size: int = 80,
) -> dict[str, object]:
    q = str(query or "").strip().lower()
    ind_raw = str(industry or "").strip()
    ind_set = {x.strip() for x in ind_raw.split(",") if x.strip()}
    idx = str(index_key or "").strip().lower()
    lst = str(list_name or "").strip()
    moat = str(moat_key or "").strip().lower()
    market_key = str(market or "").strip().lower()
    size_key = str(size or "").strip().lower()
    if sort not in {"mcap_desc", "mcap_asc", "name_asc", "ticker_asc"}:
        sort = "mcap_desc"
    ps = max(20, min(200, int(page_size or 80)))
    pg = max(1, int(page or 1))

    sc = str(scope or "all").strip().lower()
    if sc not in {"all", "my", "us_listed", "us_otc"}:
        sc = "all"
    if sc == "my":
        base = _read_my_companies()
    elif sc in {"us_listed", "us_otc"}:
        reg = _registry_universe(sc)
        base = reg if reg else _all_universe()
    else:
        base = _all_universe()
    if idx:
        idx_set = set(_load_index_tickers(idx))
        base = [t for t in base if t in idx_set]
    if lst:
        lst_set = set(_saved_list_tickers(lst))
        base = [t for t in base if t in lst_set]
    if moat:
        moat_set = set(_moat_tickers(moat))
        base = [t for t in base if t in moat_set]
    if market_key in {"us_listed", "us_otc", "otc_only"}:
        market_set = set(_registry_universe(market_key))
        if market_set:
            base = [t for t in base if t in market_set]

    profiles = _company_profiles()
    names = _companies_name_map()

    # First pass: cheap text filters before market-cap lookup.
    pre_rows: list[tuple[str, str, str, str]] = []
    for t in base:
        p = profiles.get(t, {})
        nm = str(p.get("name") or names.get(t) or t).strip()
        ctry = str(p.get("country") or "-").strip() or "-"
        ind_txt = str(p.get("industry") or "Unknown").strip() or "Unknown"
        if ind_set and ind_txt not in ind_set:
            continue
        if q:
            hay = f"{t} {nm} {ind_txt}".lower()
            if q not in hay:
                continue
        pre_rows.append((t, nm, ctry, ind_txt))

    # Fetch market caps only for prefiltered candidates (large speedup on search).
    pre_tickers = [r[0] for r in pre_rows]
    mcap_map = market_cap_map(pre_tickers, live_fetch=False)

    rows: list[CompanyRow] = []
    for t, nm, ctry, ind_txt in pre_rows:
        mcap = str(mcap_map.get(t) or "-").strip() or "-"
        row = CompanyRow(ticker=t, name=nm, country=ctry, industry=ind_txt, market_cap=mcap)
        mcap_num = _parse_mcap_num(mcap)
        if size_key == "xlarge":
            if mcap_num <= 200_000_000_000:
                continue
        elif size_key == "large":
            if not (10_000_000_000 < mcap_num <= 200_000_000_000):
                continue
        elif size_key == "medium":
            if not (2_000_000_000 < mcap_num <= 10_000_000_000):
                continue
        elif size_key == "small":
            if not (300_000_000 < mcap_num <= 2_000_000_000):
                continue
        elif size_key == "micro":
            if not (50_000_000 < mcap_num <= 300_000_000):
                continue
        elif size_key == "nano":
            if not (0 <= mcap_num <= 50_000_000):
                continue
        rows.append(row)

    if sort == "name_asc":
        rows.sort(key=lambda r: (r.name.lower(), r.ticker))
    elif sort == "ticker_asc":
        rows.sort(key=lambda r: r.ticker)
    elif sort == "mcap_asc":
        rows.sort(key=lambda r: _parse_mcap_num(r.market_cap))
    else:
        rows.sort(key=lambda r: _parse_mcap_num(r.market_cap), reverse=True)

    industries: list[str] = sorted({r.industry for r in rows if r.industry})

    total = len(rows)
    pages = max(1, (total + ps - 1) // ps)
    if pg > pages:
        pg = pages
    start = (pg - 1) * ps
    end = start + ps
    page_rows = rows[start:end]
    # Ensure visible rows have market cap populated even when global fallback
    # budget is exhausted for very large universes.
    visible_tickers = [r.ticker for r in page_rows if r.ticker and (not str(r.market_cap or "").strip() or str(r.market_cap).strip() == "-")]
    if visible_tickers:
        # Read cache only for visible rows, then fetch missing caps in background.
        page_mcap = market_cap_map(visible_tickers, live_fetch=False)
        if page_mcap:
            refreshed: list[CompanyRow] = []
            for r in page_rows:
                m = str(page_mcap.get(r.ticker) or "").strip()
                if m and m != "-":
                    refreshed.append(CompanyRow(ticker=r.ticker, name=r.name, country=r.country, industry=r.industry, market_cap=m))
                else:
                    refreshed.append(r)
            page_rows = refreshed
        prefetch_market_cap_async(visible_tickers, limit=80)
    return {
        "rows": page_rows,
        "total": total,
        "page": pg,
        "pages": pages,
        "page_size": ps,
        "industries": industries,
        "sort": sort,
        "scope": sc,
    }


def company_detail(ticker: str) -> dict[str, object]:
    t = _normalize_ticker(ticker)
    if not t:
        return {}
    profiles = _company_profiles()
    names = _companies_name_map()
    p = profiles.get(t, {})
    mcap = market_cap_map([t]).get(t, "-")

    con = _conn_core()
    moats: list[str] = []
    competitors: list[dict[str, str | int]] = []
    notes: list[dict[str, str | int]] = []
    system_logs: list[dict[str, str | int]] = []
    tasks: list[dict[str, str | int]] = []
    reminders: list[dict[str, str | int]] = []
    timeline_events: list[dict[str, object]] = []
    filings: list[dict[str, str]] = []
    active_proposal: dict[str, object] = {}
    try:
        for r in con.execute("SELECT moat_key FROM company_moat_tags WHERE ticker = ? ORDER BY moat_key", (t,)).fetchall():
            mk = str(r["moat_key"] or "").strip().lower()
            if mk:
                moats.append(mk)
        for r in con.execute(
            """SELECT id, competitor_ticker, competitor_name, evidence, source_date
               FROM company_sec_competitors
               WHERE ticker = ? AND status = 'active'
               ORDER BY confidence DESC, id DESC LIMIT 50""",
            (t,),
        ).fetchall():
            competitors.append(
                {
                    "id": int(r["id"] or 0),
                    "ticker": _normalize_ticker(str(r["competitor_ticker"] or "")),
                    "name": str(r["competitor_name"] or "").strip(),
                    "evidence": str(r["evidence"] or "").strip(),
                    "date": str(r["source_date"] or "").strip(),
                }
            )
        if core_backend() == "postgres":
            for r in list_recent_notes_pg(limit=400):
                if str(r.get("source_table") or "") != "workspace_journal":
                    continue
                if str(r.get("ticker") or "").strip().upper() != t:
                    continue
                row = {
                    "id": int(r.get("id") or 0),
                    "created_at": str(r.get("date") or ""),
                    "action": str(r.get("tag") or "Note"),
                    "emotion": "",
                    "note": str(r.get("text") or ""),
                }
                action_txt = str(row.get("action") or "").strip().lower()
                note_txt = str(row.get("note") or "").strip().lower()
                is_system = (
                    str(r.get("created_by") or "human").strip().lower() != "human"
                    or ("proposal" in action_txt)
                    or ("proposal" in note_txt)
                )
                if not is_system:
                    notes.append(row)
                else:
                    system_logs.append(row)
                if len(notes) >= 120 and len(system_logs) >= 120:
                    break
            for r in list_todos_pg(open_only=True, limit=300, ticker=t) + list_todos_pg(open_only=False, limit=300, ticker=t):
                tasks.append(
                    {
                        "id": int(r.get("id") or 0),
                        "task": str(r.get("task") or ""),
                        "status": str(r.get("status") or "open"),
                        "priority": str(r.get("priority") or "P2"),
                        "due_date": str(r.get("due_date") or ""),
                        "created_at": str(r.get("created_at") or ""),
                        "category": str(r.get("category") or "company"),
                    }
                )
            reminders = list_company_reminders_pg(ticker=t, limit=160)
        else:
            for r in con.execute(
                """SELECT id, created_at, action, emotion, note, COALESCE(created_by,'human') AS created_by
                   FROM workspace_journal
                   WHERE ticker = ?
                   ORDER BY id DESC LIMIT 120""",
                (t,),
            ).fetchall():
                row = {
                    "id": int(r["id"] or 0),
                    "created_at": str(r["created_at"] or ""),
                    "action": str(r["action"] or "Note"),
                    "emotion": str(r["emotion"] or ""),
                    "note": str(r["note"] or ""),
                }
                action_txt = str(row.get("action") or "").strip().lower()
                note_txt = str(row.get("note") or "").strip().lower()
                is_system = (
                    str(r["created_by"] or "human").strip().lower() != "human"
                    or ("proposal" in action_txt)
                    or ("proposal" in note_txt)
                )
                if not is_system:
                    notes.append(row)
                else:
                    system_logs.append(row)
            for r in con.execute(
                """SELECT id, task, status, priority, due_date, created_at, category
                   FROM todos
                   WHERE ticker = ?
                   ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'done' THEN 1 ELSE 2 END, id DESC
                   LIMIT 200""",
                (t,),
            ).fetchall():
                tasks.append(
                    {
                        "id": int(r["id"] or 0),
                        "task": str(r["task"] or ""),
                        "status": str(r["status"] or "open"),
                        "priority": str(r["priority"] or "P2"),
                        "due_date": str(r["due_date"] or ""),
                        "created_at": str(r["created_at"] or ""),
                        "category": str(r["category"] or "company"),
                    }
                )
            for r in con.execute(
                """SELECT id, remind_at, note, status, created_at
                   FROM company_reminders
                   WHERE ticker = ?
                   ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, id DESC
                   LIMIT 160""",
                (t,),
            ).fetchall():
                reminders.append(
                    {
                        "id": int(r["id"] or 0),
                        "remind_at": str(r["remind_at"] or ""),
                        "note": str(r["note"] or ""),
                        "status": str(r["status"] or "open"),
                        "created_at": str(r["created_at"] or ""),
                    }
                )
        for r in con.execute(
            """SELECT form, date, accession, doc_url, path
               FROM filings
               WHERE ticker = ?
               ORDER BY date DESC, id DESC
               LIMIT 10000""",
            (t,),
        ).fetchall():
            pth = str(r["path"] or "").strip()
            fp = safe_resolve_filing_path(pth)
            filings.append(
                {
                    "form": str(r["form"] or "").strip() or "-",
                    "date": str(r["date"] or "").strip() or "-",
                    "accession": str(r["accession"] or "").strip() or "-",
                    "doc_url": str(r["doc_url"] or "").strip(),
                    "path": str(fp) if fp is not None else "",
                    "file_name": (fp.name if fp is not None else (Path(pth).name if pth else "")),
                    "has_local": "1" if fp is not None else "0",
                }
            )
        if core_backend() == "postgres":
            cands = [x for x in list_action_proposals_pg(status="executed", limit=120) if str(x.get("ticker") or "").upper() == t]
            if not cands:
                cands = [x for x in list_action_proposals_pg(status="open", limit=120) if str(x.get("ticker") or "").upper() == t]
            pr = cands[0] if cands else {}
            if pr:
                active_proposal = {
                    "id": int(pr.get("id") or 0),
                    "status": str(pr.get("status") or "").strip(),
                    "kind": str(pr.get("kind") or "").strip(),
                    "title": str(pr.get("title") or "").strip(),
                    "confidence": float(pr.get("confidence") or 0.0),
                    "priority_score": float(pr.get("priority_score") or 0.0),
                    "updated_at": str(pr.get("updated_at") or "").strip(),
                    "reasoning": dict(pr.get("reasoning_json") or {}),
                    "insights": [x for x in list(pr.get("insights_json") or []) if isinstance(x, dict)][:3],
                    "citations": [x for x in list(pr.get("citations_json") or []) if isinstance(x, dict)][:6],
                }
        else:
            pr = con.execute(
                """SELECT id, status, kind, title, confidence, priority_score, updated_at,
                          COALESCE(reasoning_json,'{}') AS reasoning_json,
                          COALESCE(insights_json,'[]') AS insights_json,
                          COALESCE(citations_json,'[]') AS citations_json
                   FROM action_proposals
                   WHERE ticker = ? AND status IN ('executed', 'open')
                   ORDER BY CASE status WHEN 'executed' THEN 0 ELSE 1 END, id DESC
                   LIMIT 1""",
                (t,),
            ).fetchone()
            if pr:
                try:
                    reasoning = json.loads(str(pr["reasoning_json"] or "{}"))
                except Exception:
                    reasoning = {}
                try:
                    insights = json.loads(str(pr["insights_json"] or "[]"))
                except Exception:
                    insights = []
                try:
                    citations = json.loads(str(pr["citations_json"] or "[]"))
                except Exception:
                    citations = []
                active_proposal = {
                    "id": int(pr["id"] or 0),
                    "status": str(pr["status"] or "").strip(),
                    "kind": str(pr["kind"] or "").strip(),
                    "title": str(pr["title"] or "").strip(),
                    "confidence": float(pr["confidence"] or 0.0),
                    "priority_score": float(pr["priority_score"] or 0.0),
                    "updated_at": str(pr["updated_at"] or "").strip(),
                    "reasoning": reasoning if isinstance(reasoning, dict) else {},
                    "insights": [x for x in list(insights or []) if isinstance(x, dict)][:3],
                    "citations": [x for x in list(citations or []) if isinstance(x, dict)][:6],
                }
    finally:
        con.close()

    filing_form_counts: dict[str, int] = {}
    for r in filings:
        fm = str(r.get("form") or "-")
        filing_form_counts[fm] = int(filing_form_counts.get(fm, 0)) + 1

    filing_group_map: dict[str, list[dict[str, str]]] = {}
    for r in filings:
        ck = _filing_category(str(r.get("form") or ""))
        filing_group_map.setdefault(ck, []).append(r)
    filing_groups: list[dict[str, object]] = []
    for key, label in FILING_CATEGORY_ORDER:
        rows = filing_group_map.get(key, [])
        if not rows:
            continue
        filing_groups.append(
            {
                "key": key,
                "label": label,
                "count": len(rows),
                "rows": rows,
            }
        )

    for n in notes:
        timeline_events.append(
            {
                "kind": "note",
                "icon": "📝",
                "created_at": str(n.get("created_at") or ""),
                "is_ai": False,
                "ticker": t,
                "title": str(n.get("action") or "Note"),
                "text": str(n.get("note") or ""),
                "note_id": int(n.get("id") or 0),
            }
        )
    for n in system_logs:
        timeline_events.append(
            {
                "kind": "log",
                "icon": "🤖",
                "created_at": str(n.get("created_at") or ""),
                "is_ai": True,
                "ticker": t,
                "title": str(n.get("action") or "System Log"),
                "text": str(n.get("note") or ""),
                "note_id": int(n.get("id") or 0),
            }
        )
    for r in tasks:
        is_ai_task = is_ai_task_text(str(r.get("task") or ""), str(r.get("category") or ""))
        title = clean_task_text(str(r.get("task") or "Task"))[:140]
        timeline_events.append(
            {
                "kind": "log" if is_ai_task else "task",
                "icon": "🤖" if is_ai_task else "✅",
                "created_at": str(r.get("created_at") or ""),
                "is_ai": is_ai_task,
                "ticker": t,
                "title": title or "Task",
                "text": (
                    f"AI follow-up • {str(r.get('status') or 'open')}"
                    if is_ai_task
                    else f"Task • {str(r.get('status') or 'open')}"
                ),
                "todo_id": int(r.get("id") or 0),
                "status": str(r.get("status") or "open"),
                "due_date": str(r.get("due_date") or ""),
            }
        )
    for r in reminders:
        note_txt = str(r.get("note") or "")
        low_note = note_txt.lower()
        is_ai_reminder = ("proposal" in low_note)
        timeline_events.append(
            {
                "kind": "log" if is_ai_reminder else "reminder",
                "icon": "🤖" if is_ai_reminder else "⏰",
                "created_at": str(r.get("created_at") or ""),
                "is_ai": is_ai_reminder,
                "ticker": t,
                "title": clean_task_text(note_txt)[:140] or "Reminder",
                "text": (
                    f"AI follow-up • {str(r.get('status') or 'open')}"
                    if is_ai_reminder
                    else f"Reminder • {str(r.get('status') or 'open')}"
                ),
                "reminder_id": int(r.get("id") or 0),
                "status": str(r.get("status") or "open"),
                "remind_at": str(r.get("remind_at") or ""),
            }
        )
    moat_map = {k: v for k, v in MOAT_OPTIONS}
    mini = get_mini_statements(t)
    intel = get_company_intel(t, refresh=False)
    revenue_segments = _normalize_segments_for_view(dict(intel.get("revenue_segments") or {}))
    buyback = dict(intel.get("buyback") or {})
    insider_trades = [x for x in list(intel.get("insider_trades") or []) if isinstance(x, dict)][:16]
    intel_status = dict(intel.get("status") or {})
    intel_asof = str(intel.get("asof") or "").strip()
    intel_source = str(intel.get("source") or "").strip()
    deltas = compute_financial_deltas(ticker=t, years=5)
    earnings_calls = list_earnings_transcripts(t, limit=36)
    earnings_releases = list_sec_earnings_releases(t, limit=12, lookback_years=10, max_filings=260)
    earnings_call_source_counts: dict[str, int] = {}
    for tr in earnings_calls:
        src = str((tr or {}).get("source_type") or "").strip().lower() or "unknown"
        earnings_call_source_counts[src] = int(earnings_call_source_counts.get(src, 0)) + 1
    quarterly_signals = list_quarterly_result_signals(t, limit=10)

    def _days_old(asof_s: str) -> int | None:
        s = str(asof_s or "").strip()
        if not s:
            return None
        try:
            d = dt.date.fromisoformat(s[:10])
            return int((dt.date.today() - d).days)
        except Exception:
            return None

    mini_asof = str((mini or {}).get("asof") or "").strip()
    mini_days = _days_old(mini_asof)
    intel_days = _days_old(intel_asof)
    quant_ready = bool((mini or {}).get("years")) and bool(revenue_segments or buyback)
    qual_ready = bool((intel or {}).get("status"))
    stale = bool((mini_days is not None and mini_days > 3) or (intel_days is not None and intel_days > 3))
    data_coverage = "quant_ready" if quant_ready else ("qual_only" if qual_ready else "missing")
    data_coverage_label = "Quant Ready" if data_coverage == "quant_ready" else ("Qual Only" if data_coverage == "qual_only" else "Missing")
    data_coverage_color = "#166534" if data_coverage == "quant_ready" else ("#92400e" if data_coverage == "qual_only" else "#b91c1c")
    data_coverage_meta = {
        "state": data_coverage,
        "label": data_coverage_label,
        "color": data_coverage_color,
        "stale": stale,
        "mini_asof": mini_asof,
        "intel_asof": intel_asof,
        "mini_days_old": mini_days,
        "intel_days_old": intel_days,
        "sla_days": 3,
        "schema_version": "financials_schema_v1",
        "provenance": {
            "mini_statements_source": str((mini or {}).get("source") or ""),
            "intel_source": intel_source,
        },
    }

    timeline_events.sort(
        key=lambda e: (
            parse_datetime_flexible(str(e.get("created_at") or ""))
            or parse_datetime_flexible(
                str(e.get("remind_at") or ""),
                formats=("%Y-%m-%d %H:%M", "%Y-%m-%d"),
            )
            or dt.datetime.min
        ),
        reverse=True,
    )

    similar_companies = _similar_companies_for(
        t,
        industry=str(p.get("industry") or "Unknown").strip() or "Unknown",
        market_cap=str(mcap or "-"),
        profiles=profiles,
        names=names,
        existing_competitors=competitors,
        limit=8,
    )
    return {
        "ticker": t,
        "name": str(p.get("name") or names.get(t) or t).strip(),
        "country": str(p.get("country") or "-").strip() or "-",
        "industry": str(p.get("industry") or "Unknown").strip() or "Unknown",
        "market_cap": str(mcap or "-").strip() or "-",
        "current_price": _live_price(t),
        "moat_keys": moats,
        "moats": [{"key": k, "label": moat_map.get(k, k)} for k in moats],
        "moat_options": [{"key": k, "label": v} for k, v in MOAT_OPTIONS],
        "competitors": competitors,
        "similar_companies": similar_companies,
        "notes": notes,
        "system_logs": system_logs,
        "tasks": tasks,
        "reminders": reminders,
        "timeline_events": timeline_events,
        "active_proposal": active_proposal,
        "price_metrics": get_price_metrics(t),
        "mini_statements": mini,
        "financial_deltas": deltas,
        "revenue_segments": revenue_segments,
        "buyback": buyback,
        "insider_trades": insider_trades,
        "intel_status": intel_status,
        "intel_asof": intel_asof,
        "intel_source": intel_source,
        "data_coverage": data_coverage_meta,
        "earnings_calls": earnings_calls,
        "earnings_releases": earnings_releases,
        "earnings_call_source_counts": earnings_call_source_counts,
        "quarterly_signals": quarterly_signals,
        "filings": filings,
        "filing_groups": filing_groups,
        "filing_form_counts": filing_form_counts,
    }


def save_company_moats(ticker: str, moat_keys: list[str]) -> bool:
    t = _normalize_ticker(ticker)
    if not t:
        return False
    allowed = {k for k, _v in MOAT_OPTIONS}
    picked = sorted({str(x or "").strip().lower() for x in moat_keys if str(x or "").strip().lower() in allowed})
    now = dt.datetime.now().isoformat()
    con = _conn_core()
    try:
        con.execute("DELETE FROM company_moat_tags WHERE ticker = ?", (t,))
        for mk in picked:
            con.execute(
                "INSERT INTO company_moat_tags (ticker, moat_key, updated_at, note) VALUES (?, ?, ?, '')",
                (t, mk, now),
            )
        con.commit()
        return True
    finally:
        con.close()


def add_competitor(ticker: str, competitor_ticker: str, competitor_name: str, evidence: str = "") -> bool:
    t = _normalize_ticker(ticker)
    ct = _normalize_ticker(competitor_ticker)
    name = str(competitor_name or "").strip()
    ev = str(evidence or "").strip()
    if not t or (not ct and not name):
        return False
    now = dt.datetime.now().isoformat()
    con = _conn_core()
    try:
        con.execute(
            """INSERT INTO company_sec_competitors
               (ticker, competitor_ticker, competitor_name, source_form, source_date, source_path, evidence, confidence, status, updated_at)
               VALUES (?, ?, ?, '', '', '', ?, 1.0, 'active', ?)""",
            (t, ct, name[:160], ev[:1200], now),
        )
        con.commit()
        return True
    finally:
        con.close()


def update_competitor(row_id: int, competitor_ticker: str, competitor_name: str, evidence: str = "") -> bool:
    rid = int(row_id or 0)
    if rid <= 0:
        return False
    ct = _normalize_ticker(competitor_ticker)
    name = str(competitor_name or "").strip()[:160]
    ev = str(evidence or "").strip()[:1200]
    now = dt.datetime.now().isoformat()
    con = _conn_core()
    try:
        cur = con.execute(
            """UPDATE company_sec_competitors
               SET competitor_ticker = ?, competitor_name = ?, evidence = ?, updated_at = ?
               WHERE id = ?""",
            (ct, name, ev, now, rid),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def remove_competitor(row_id: int) -> bool:
    rid = int(row_id or 0)
    if rid <= 0:
        return False
    con = _conn_core()
    try:
        cur = con.execute("DELETE FROM company_sec_competitors WHERE id = ?", (rid,))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def add_company_note(
    ticker: str,
    note: str,
    action: str = "Note",
    emotion: str = "Calm",
    created_by: str = "human",
) -> bool:
    t = _normalize_ticker(ticker)
    txt = str(note or "").strip()
    if not t or not txt:
        return False
    cb = str(created_by or "human").strip().lower()
    if cb not in {"human", "ai"}:
        cb = "human"
    if core_backend() == "postgres":
        return add_workspace_journal_note_pg(ticker=t, note=txt, action=action, emotion=emotion, created_by=cb)
    con = _conn_core()
    try:
        try:
            con.execute(
                "INSERT INTO workspace_journal (ticker, action, emotion, note, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?)",
                (t, str(action or "Note")[:80], str(emotion or "Calm")[:80], txt[:4000], dt.datetime.now().isoformat(), cb),
            )
        except Exception:
            con.execute(
                "INSERT INTO workspace_journal (ticker, action, emotion, note, created_at) VALUES (?, ?, ?, ?, ?)",
                (t, str(action or "Note")[:80], str(emotion or "Calm")[:80], txt[:4000], dt.datetime.now().isoformat()),
            )
        con.commit()
        return True
    finally:
        con.close()


def update_company_note(note_id: int, note: str) -> bool:
    rid = int(note_id or 0)
    txt = str(note or "").strip()
    if rid <= 0 or not txt:
        return False
    if core_backend() == "postgres":
        return update_workspace_journal_note_pg(note_id=rid, note=txt)
    con = _conn_core()
    try:
        cur = con.execute(
            "UPDATE workspace_journal SET note = ?, created_at = ? WHERE id = ?",
            (txt[:4000], dt.datetime.now().isoformat(), rid),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def delete_company_note(note_id: int) -> bool:
    rid = int(note_id or 0)
    if rid <= 0:
        return False
    if core_backend() == "postgres":
        return delete_workspace_journal_note_pg(note_id=rid)
    con = _conn_core()
    try:
        cur = con.execute("DELETE FROM workspace_journal WHERE id = ?", (rid,))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def add_company_task(ticker: str, task: str, due_date: str = "", priority: str = "P2") -> bool:
    t = _normalize_ticker(ticker)
    txt = str(task or "").strip()
    due = str(due_date or "").strip()
    p = str(priority or "P2").strip().upper()
    if p not in {"P1", "P2", "P3"}:
        p = "P2"
    if due and not re.match(r"^\d{4}-\d{2}-\d{2}$", due):
        due = ""
    if not t or not txt:
        return False
    if core_backend() == "postgres":
        return add_todo_pg(task=txt[:1000], ticker=t, category="company", priority=p, due_date=due)
    con = _conn_core()
    try:
        con.execute(
            """INSERT INTO todos (task, status, created_at, priority, due_date, ticker, category)
               VALUES (?, 'open', ?, ?, ?, ?, 'company')""",
            (txt[:1000], dt.datetime.now().isoformat(), p, due, t),
        )
        con.commit()
        return True
    finally:
        con.close()


def toggle_company_task(todo_id: int) -> bool:
    rid = int(todo_id or 0)
    if rid <= 0:
        return False
    if core_backend() == "postgres":
        return toggle_todo_pg(rid)
    con = _conn_core()
    try:
        row = con.execute("SELECT status, category FROM todos WHERE id = ?", (rid,)).fetchone()
        if not row:
            return False
        cur = str(row["status"] or "open").strip().lower()
        cat = str(row["category"] or "company").strip().lower()
        if cur == "open":
            nxt = "archived" if cat == "quick" else "done"
        else:
            nxt = "open"
        con.execute("UPDATE todos SET status = ? WHERE id = ?", (nxt, rid))
        con.commit()
        return True
    finally:
        con.close()


def delete_company_task(todo_id: int) -> bool:
    rid = int(todo_id or 0)
    if rid <= 0:
        return False
    if core_backend() == "postgres":
        return delete_todo_pg(rid)
    con = _conn_core()
    try:
        cur = con.execute("DELETE FROM todos WHERE id = ?", (rid,))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def update_company_task(todo_id: int, task: str, due_date: str = "", priority: str = "P2") -> bool:
    rid = int(todo_id or 0)
    txt = str(task or "").strip()
    due = str(due_date or "").strip()
    p = str(priority or "P2").strip().upper()
    if p not in {"P1", "P2", "P3"}:
        p = "P2"
    if due and not re.match(r"^\d{4}-\d{2}-\d{2}$", due):
        due = ""
    if rid <= 0 or not txt:
        return False
    if core_backend() == "postgres":
        return update_todo_pg(todo_id=rid, task=txt[:1000], due_date=due, priority=p)
    con = _conn_core()
    try:
        cur = con.execute(
            "UPDATE todos SET task = ?, due_date = ?, priority = ?, created_at = ? WHERE id = ?",
            (txt[:1000], due, p, dt.datetime.now().isoformat(), rid),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def add_company_reminder(ticker: str, remind_at: str, note: str) -> bool:
    t = _normalize_ticker(ticker)
    ra = str(remind_at or "").strip()
    txt = str(note or "").strip()
    if not t or not txt:
        return False
    if core_backend() == "postgres":
        return add_company_reminder_pg(ticker=t, remind_at=ra[:64], note=txt[:500])
    con = _conn_core()
    try:
        con.execute(
            """INSERT INTO company_reminders (ticker, remind_at, note, status, created_at)
               VALUES (?, ?, ?, 'open', ?)""",
            (t, ra[:64], txt[:500], dt.datetime.now().isoformat()),
        )
        con.commit()
        return True
    finally:
        con.close()


def toggle_company_reminder(reminder_id: int) -> bool:
    rid = int(reminder_id or 0)
    if rid <= 0:
        return False
    if core_backend() == "postgres":
        return toggle_company_reminder_pg(rid)
    con = _conn_core()
    try:
        row = con.execute("SELECT status FROM company_reminders WHERE id = ?", (rid,)).fetchone()
        if not row:
            return False
        cur = str(row["status"] or "open").strip().lower()
        nxt = "done" if cur == "open" else "open"
        con.execute("UPDATE company_reminders SET status = ? WHERE id = ?", (nxt, rid))
        con.commit()
        return True
    finally:
        con.close()


def update_company_reminder(reminder_id: int, remind_at: str, note: str) -> bool:
    rid = int(reminder_id or 0)
    ra = str(remind_at or "").strip()[:64]
    txt = str(note or "").strip()
    if rid <= 0 or not txt:
        return False
    if core_backend() == "postgres":
        return update_company_reminder_pg(reminder_id=rid, remind_at=ra, note=txt[:500])
    con = _conn_core()
    try:
        cur = con.execute(
            "UPDATE company_reminders SET remind_at = ?, note = ?, created_at = ? WHERE id = ?",
            (ra, txt[:500], dt.datetime.now().isoformat(), rid),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def delete_company_reminder(reminder_id: int) -> bool:
    rid = int(reminder_id or 0)
    if rid <= 0:
        return False
    if core_backend() == "postgres":
        return delete_company_reminder_pg(rid)
    con = _conn_core()
    try:
        cur = con.execute("DELETE FROM company_reminders WHERE id = ?", (rid,))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()
