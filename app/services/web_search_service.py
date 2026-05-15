"""web_search_service.py — Live market snapshot + web search for AI chat enrichment.

Three-tier news search (best available is used automatically):
  Tier 1 — Brave Search API   if BRAVE_SEARCH_API_KEY env var is set.
                               Industry standard for AI apps (Perplexity, LangChain etc).
                               Full web search, excellent quality, 2000 free queries/month.
  Tier 2 — DuckDuckGo         Free, no key needed, searches the full web.
                               Good quality fallback (ddgs package).
  Tier 3 — RSS headlines      Always-available last resort from 4 financial feeds.
                               Limited to what those sites publish but zero dependency.

Macro snapshot (always injected):
  — 18 key asset prices from yfinance (oil, gold, indices, VIX, bonds, FX, crypto).
  — Pre-warmed by background daemon thread, refreshed every 2 minutes.
  — Inline reads use 0.5s timeout so they never block the AI response.
"""

from __future__ import annotations

import concurrent.futures as cf
import os
import re
import threading
import time
from typing import Optional


# ─── Macro asset tickers ───────────────────────────────────────────────────────

_MACRO_TICKERS_DEFAULT: dict[str, str] = {
    "CL=F":     "WTI Crude Oil",
    "BZ=F":     "Brent Crude",
    "GC=F":     "Gold",
    "SI=F":     "Silver",
    "NG=F":     "Natural Gas",
    "HG=F":     "Copper",
    "^GSPC":    "S&P 500",
    "^IXIC":    "Nasdaq",
    "^DJI":     "Dow Jones",
    "^RUT":     "Russell 2000",
    "^VIX":     "VIX (Fear Index)",
    "^TNX":     "10Y Treasury Yield",
    "^TYX":     "30Y Treasury Yield",
    "DX-Y.NYB": "US Dollar Index",
    "EURUSD=X": "EUR/USD",
    "JPY=X":    "USD/JPY",
    "BTC-USD":  "Bitcoin",
    "ETH-USD":  "Ethereum",
}

_SNAP_CACHE: dict = {}
_SNAP_LOCK = threading.Lock()
_SNAP_TTL_SEC = 120  # 2-minute cache


def _fetch_ticker_price(sym: str) -> tuple[str, Optional[float], Optional[float]]:
    """Return (symbol, last_price, prev_close) using yfinance fast_info attribute access."""
    try:
        import yfinance as yf  # type: ignore
        fi = yf.Ticker(sym).fast_info
        last, prev = 0.0, 0.0
        try:
            v = fi.last_price
            last = float(v) if v is not None else 0.0
        except Exception:
            pass
        try:
            v = fi.previous_close
            prev = float(v) if v is not None else 0.0
        except Exception:
            pass
        if prev <= 0:
            try:
                v = fi.regular_market_previous_close
                prev = float(v) if v is not None else 0.0
            except Exception:
                pass
        return (sym, last if last > 0 else None, prev if prev > 0 else None)
    except Exception:
        return (sym, None, None)


def get_live_macro_snapshot(timeout_sec: float = 6.0) -> dict:
    """Fetch live prices for 18 key macro assets. Cached 2 minutes."""
    with _SNAP_LOCK:
        cached = _SNAP_CACHE.get("data")
        ts = float(_SNAP_CACHE.get("ts", 0))
        if cached is not None and (time.time() - ts) < _SNAP_TTL_SEC:
            return dict(cached)

    # Load macro tickers: DB config > hardcoded default
    try:
        from app.services.ai_config_service import get_macro_tickers
        _active_tickers = get_macro_tickers() or _MACRO_TICKERS_DEFAULT
    except Exception:
        _active_tickers = _MACRO_TICKERS_DEFAULT

    result: dict = {}
    try:
        syms = list(_active_tickers.keys())
        with cf.ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(_fetch_ticker_price, sym): sym for sym in syms}
            for fut in cf.as_completed(futs, timeout=timeout_sec):
                try:
                    sym, last, prev = fut.result(timeout=0)
                    label = _active_tickers.get(sym, sym)
                    if last is not None:
                        chg = ((last - prev) / prev * 100) if (prev and prev > 0) else None
                        result[label] = {
                            "price": round(last, 4),
                            "change_pct": round(chg, 2) if chg is not None else None,
                            "symbol": sym,
                        }
                except Exception:
                    pass
    except Exception as exc:
        result["_error"] = str(exc)[:200]

    if result and any(not k.startswith("_") for k in result):
        with _SNAP_LOCK:
            _SNAP_CACHE["data"] = result
            _SNAP_CACHE["ts"] = time.time()
    return result


def format_macro_snapshot_text(snap: dict) -> str:
    """Render macro snapshot as terse grouped text for LLM context."""
    if not snap or all(k.startswith("_") for k in snap):
        return "(Live macro data unavailable)"

    _cat = {
        "WTI Crude Oil": "Commodities", "Brent Crude": "Commodities",
        "Gold": "Commodities", "Silver": "Commodities",
        "Natural Gas": "Commodities", "Copper": "Commodities",
        "S&P 500": "Equity Indices", "Nasdaq": "Equity Indices",
        "Dow Jones": "Equity Indices", "Russell 2000": "Equity Indices",
        "VIX (Fear Index)": "Rates & Volatility",
        "10Y Treasury Yield": "Rates & Volatility",
        "30Y Treasury Yield": "Rates & Volatility",
        "US Dollar Index": "FX", "EUR/USD": "FX", "USD/JPY": "FX",
        "Bitcoin": "Crypto", "Ethereum": "Crypto",
    }
    groups: dict[str, list[str]] = {c: [] for c in ["Commodities", "Equity Indices", "Rates & Volatility", "FX", "Crypto"]}

    def _fmt(px: float) -> str:
        if px >= 10_000: return f"{px:,.0f}"
        if px >= 100:    return f"{px:,.2f}"
        if px >= 1:      return f"{px:.4f}"
        return f"{px:.6f}"

    for label, v in snap.items():
        if label.startswith("_") or not isinstance(v, dict):
            continue
        px = v.get("price")
        if px is None:
            continue
        chg = v.get("change_pct")
        sign = "+" if (chg or 0) >= 0 else ""
        suffix = f"  ({sign}{chg:.2f}% today)" if chg is not None else ""
        groups.setdefault(_cat.get(label, "Other"), []).append(f"  {label}: {_fmt(float(px))}{suffix}")

    parts = [f"{cat}:\n" + "\n".join(lines) for cat, lines in groups.items() if lines]
    return "\n\n".join(parts) if parts else "(No data)"


# ─── Tier 1: Brave Search API ──────────────────────────────────────────────────

_BRAVE_NEWS_URL = "https://api.search.brave.com/res/v1/news/search"
_BRAVE_WEB_URL  = "https://api.search.brave.com/res/v1/web/search"


def _brave_search(query: str, max_results: int = 6, timeout: float = 6.0) -> list[dict]:
    """Search via Brave Search API. Returns [] if key not set or on error."""
    api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if not api_key:
        return []
    try:
        import requests  # type: ignore
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": api_key,
        }
        # Try news endpoint first, fall back to web search
        with requests.get(
            _BRAVE_NEWS_URL,
            params={"q": query, "count": max_results, "search_lang": "en", "freshness": "pd"},
            headers=headers,
            timeout=timeout,
        ) as resp:
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("results") or []
                results = []
                for item in items[:max_results]:
                    results.append({
                        "title":   str(item.get("title") or "").strip(),
                        "summary": str(item.get("description") or item.get("extra_snippets", [""])[0] if item.get("extra_snippets") else "").strip()[:300],
                        "url":     str(item.get("url") or "").strip(),
                        "source":  str((item.get("meta_url") or {}).get("hostname") or item.get("source") or "Brave").strip(),
                        "published": str(item.get("age") or "").strip(),
                        "engine":  "brave",
                    })
                if results:
                    return results
        # Fallback to web search if news returns nothing
        with requests.get(
            _BRAVE_WEB_URL,
            params={"q": query, "count": max_results, "search_lang": "en"},
            headers=headers,
            timeout=timeout,
        ) as resp2:
            if resp2.status_code == 200:
                data2 = resp2.json()
                web_items = (data2.get("web") or {}).get("results") or []
                results2 = []
                for item in web_items[:max_results]:
                    results2.append({
                        "title":   str(item.get("title") or "").strip(),
                        "summary": str(item.get("description") or "").strip()[:300],
                        "url":     str(item.get("url") or "").strip(),
                        "source":  str((item.get("meta_url") or {}).get("hostname") or "Brave").strip(),
                        "published": "",
                        "engine":  "brave",
                    })
                return results2
    except Exception:
        pass
    return []


# ─── Tier 2: DuckDuckGo (ddgs package) ────────────────────────────────────────

def _ddg_search(query: str, max_results: int = 6, timeout: float = 8.0) -> list[dict]:
    """Search via DuckDuckGo. Free, no API key needed.
    Note: must use DDGS() without context manager — the `with` form throttles results."""
    try:
        from ddgs import DDGS  # type: ignore
        ddgs = DDGS()
        # Try news search first — better recency for financial queries
        try:
            try:
                news = list(ddgs.news(query, max_results=max_results, timelimit="w"))
                if news:
                    return [
                        {
                            "title":     str(item.get("title") or "").strip(),
                            "summary":   str(item.get("body") or "").strip()[:300],
                            "url":       str(item.get("url") or "").strip(),
                            "source":    str(item.get("source") or "DuckDuckGo").strip(),
                            "published": str(item.get("date") or "").strip(),
                            "engine":    "ddg_news",
                        }
                        for item in news
                    ]
            except Exception:
                pass
            # Fall back to web text search
            try:
                web = list(ddgs.text(query, max_results=max_results))
                return [
                    {
                        "title":     str(item.get("title") or "").strip(),
                        "summary":   str(item.get("body") or "").strip()[:300],
                        "url":       str(item.get("href") or "").strip(),
                        "source":    "DuckDuckGo",
                        "published": "",
                        "engine":    "ddg_web",
                    }
                    for item in web
                ]
            except Exception:
                pass
        finally:
            try:
                close_fn = getattr(ddgs, "close", None)
                if callable(close_fn):
                    close_fn()
            except Exception:
                pass
    except Exception:
        pass
    return []


# ─── Tier 3: RSS headline fallback ────────────────────────────────────────────

_RSS_FEEDS = [
    ("Yahoo Finance",  "https://finance.yahoo.com/rss/topstories"),
    ("CNBC Markets",   "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("MarketWatch",    "https://www.marketwatch.com/rss/topstories"),
    ("Reuters",        "https://feeds.reuters.com/reuters/businessNews"),
]

_RSS_CACHE: dict = {}
_RSS_LOCK  = threading.Lock()
_RSS_TTL_SEC = 300  # 5-minute cache

_HTML_TAG_RE  = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _fetch_rss_feed(name: str, url: str, timeout: float = 5.0) -> list[dict]:
    try:
        import feedparser  # type: ignore
        import requests    # type: ignore
        with requests.get(url, timeout=timeout, headers={"User-Agent": "InvestorOS/1.0"}) as resp:
            if resp.status_code != 200:
                return []
            feed = feedparser.parse(resp.text)
        entries: list[dict] = []
        for e in (feed.entries or [])[:25]:
            title = str(getattr(e, "title", "") or "").strip()
            if not title:
                continue
            raw = str(getattr(e, "summary", "") or getattr(e, "description", "") or "").strip()
            summary = _WHITESPACE_RE.sub(" ", _HTML_TAG_RE.sub(" ", raw)).strip()[:300]
            entries.append({
                "title":     title,
                "summary":   summary,
                "url":       str(getattr(e, "link", "") or "").strip(),
                "source":    name,
                "published": str(getattr(e, "published", "") or "").strip()[:32],
                "engine":    "rss",
            })
        return entries
    except Exception:
        return []


def _get_rss_headlines() -> list[dict]:
    """Fetch and cache RSS headlines from all feeds. Refreshes every 5 minutes."""
    with _RSS_LOCK:
        cached = _RSS_CACHE.get("entries")
        if cached is not None and (time.time() - float(_RSS_CACHE.get("ts", 0))) < _RSS_TTL_SEC:
            return list(cached)
    all_entries: list[dict] = []
    try:
        with cf.ThreadPoolExecutor(max_workers=4) as pool:
            futs = {pool.submit(_fetch_rss_feed, name, url): name for name, url in _RSS_FEEDS}
            for fut in cf.as_completed(futs, timeout=10):
                try:
                    all_entries.extend(fut.result(timeout=0))
                except Exception:
                    pass
    except Exception:
        pass
    if all_entries:
        with _RSS_LOCK:
            _RSS_CACHE["entries"] = all_entries
            _RSS_CACHE["ts"] = time.time()
    return all_entries


_STOP_WORDS = frozenset({
    "the","is","are","was","were","be","been","being","a","an","to","of","in","on",
    "at","by","for","and","or","but","if","as","with","from","it","this","that",
    "these","those","its","their","they","he","she","we","i","you","my","our","your",
    "do","does","did","have","has","had","what","why","how","when","where","who",
    "which","not","no","can","will","would","could","should","may","just","about",
    "more","also","into","get","got","going","go",
    "up","down","new","first","last","high","low","big","top","year","month","week",
    "day","time","make","take","say","see","look","come",
})


def _rss_score_search(query: str, max_results: int = 6) -> list[dict]:
    """Score RSS headlines against query keywords. Used as Tier 3 fallback."""
    entries = _get_rss_headlines()
    if not entries:
        return []
    keywords = frozenset(
        w for w in re.findall(r"\b[a-z]{2,}\b", query.lower())
        if w not in _STOP_WORDS
    )
    if not keywords:
        return entries[:max_results]
    scored: list[tuple[int, dict]] = []
    q_lower = query.lower()
    for e in entries:
        raw = (e.get("title", "") + " " + e.get("summary", "")).lower()
        words = frozenset(re.findall(r"\b[a-z]{2,}\b", raw))
        score = sum(1 for kw in keywords if kw in words)
        sig = [w for w in q_lower.split() if len(w) >= 4 and w not in _STOP_WORDS]
        if len(sig) >= 2 and " ".join(sig[:2]) in raw:
            score += 2
        if score > 0:
            scored.append((score, e))
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:max_results]]


# ─── Public search function (3-tier auto-fallback) ────────────────────────────

def search_web(query: str, max_results: int = 6, timeout: float = 8.0) -> list[dict]:
    """
    Search the web for news/info about query. Uses best available tier:
      1. Brave Search API  (set BRAVE_SEARCH_API_KEY env var)
      2. DuckDuckGo        (free, no key, full web)
      3. RSS keyword match (always available, limited to 4 financial feeds)

    Returns list of {title, summary, url, source, published, engine}.
    """
    # Tier 1: Brave
    try:
        results = _brave_search(query, max_results=max_results, timeout=timeout)
        if results:
            return results
    except Exception:
        pass

    # Tier 2: DuckDuckGo
    try:
        results = _ddg_search(query, max_results=max_results, timeout=timeout)
        if results:
            return results
    except Exception:
        pass

    # Tier 3: RSS fallback
    return _rss_score_search(query, max_results=max_results)


# Keep old name as alias so existing callers don't break
def search_news(query: str, max_results: int = 6) -> list[dict]:
    return search_web(query, max_results=max_results)


def format_news_text(results: list[dict]) -> str:
    """Format search results as terse text for LLM context."""
    if not results:
        return "(No relevant news found)"
    lines: list[str] = []
    for r in results:
        source  = r.get("source", "")
        title   = r.get("title", "").strip()
        summary = r.get("summary", "").strip()
        pub     = r.get("published", "")[:20]
        engine  = r.get("engine", "")
        if not title:
            continue
        tag = f"[{source}]" if source else f"[{engine}]"
        line = f"• {tag} {title}"
        if summary:
            line += f"\n  {summary[:200]}"
        if pub:
            line += f"  ({pub})"
        lines.append(line)
    return "\n".join(lines)


def get_search_engine_status() -> str:
    """Return which search engine tier is active."""
    if os.environ.get("BRAVE_SEARCH_API_KEY", "").strip():
        return "brave"
    try:
        from ddgs import DDGS  # type: ignore  # noqa
        return "duckduckgo"
    except Exception:
        return "rss_only"


# ─── Real-time query detection ────────────────────────────────────────────────

_REALTIME_PATTERNS = [
    r"\btoday\b", r"\bright now\b", r"\bcurrently\b", r"\blatest\b",
    r"\bjust\b", r"\bnow\b", r"\bthis (morning|week|month|quarter|year)\b",
    r"\byesterday\b", r"\brecently\b",
    r"\b(why|what).{0,30}(up|down|fell|fallen|rise|risen|rising|drop|dropped|dropping|surged|surging|rallied|rallying|plunged|plunging|tumbled|tumbling|moved|moving|crash|crashed|spike|spiked|spiking|increased|increasing|decreased|decreasing|climbing|soaring|tanking|dumping|pumping|mooning|selling off|sold off)\b",
    r"\b(oil|gold|silver|gas|copper|wheat|corn|bitcoin|crypto|dollar|yen|euro|pound|yuan|natural gas|crude|brent|wti|vix).{0,25}(up|down|price|prices|fell|rise|rising|drop|surge|surging|rally|rallying|plunge|why|today|increased|decreased|higher|lower|soar|soaring|tank|tanking|spike|spiking|climbing)\b",
    r"\b(stock|market|equity|bond|yield|rate|index|indices).{0,25}(up|down|fell|rise|drop|surge|rally|why|today|crash|increased|decreased|higher|lower)\b",
    r"\b(fed|federal reserve|ecb|boj|pboc|central bank).{0,40}(rate|decision|meeting|cut|hike|raise|lower|pause)\b",
    r"\b(cpi|pce|ppi|gdp|nfp|inflation|unemployment|jobs|retail sales|ism|pmi).{0,25}(today|this|latest|report|data|number|reading)\b",
    r"\b(earnings|eps|revenue|guidance|results|beat|miss).{0,25}(today|this|latest|report)\b",
    r"\bwhat (happened|is happening|are markets doing)\b",
    r"\bmoving (markets|stocks|prices)\b",
    r"\b(news|headlines|update|report)\b",
    r"\bmarket (open|close|session|today)\b",
]
_REALTIME_RE = [re.compile(p, re.IGNORECASE) for p in _REALTIME_PATTERNS]


def is_realtime_query(query: str) -> bool:
    """Return True if the query likely requires real-time web data."""
    q = str(query or "").strip()
    return bool(q) and any(rx.search(q) for rx in _REALTIME_RE)


# ─── Background pre-warm threads ─────────────────────────────────────────────

_BG_STARTED = False
_BG_LOCK = threading.Lock()


def _start_background_refresh() -> None:
    """Start daemon threads that keep macro snapshot + RSS cache warm."""
    global _BG_STARTED
    with _BG_LOCK:
        if _BG_STARTED:
            return
        _BG_STARTED = True

    def _macro_loop() -> None:
        while True:
            try:
                get_live_macro_snapshot(timeout_sec=10.0)
            except Exception:
                pass
            time.sleep(120)

    def _rss_loop() -> None:
        time.sleep(5)
        while True:
            try:
                _get_rss_headlines()
            except Exception:
                pass
            time.sleep(300)

    threading.Thread(target=_macro_loop, name="macro-snapshot-refresh", daemon=True).start()
    threading.Thread(target=_rss_loop, name="rss-headlines-refresh", daemon=True).start()


def start_web_search_background() -> None:
    """Public entry point — call from main.py startup, not at import time."""
    _start_background_refresh()
