from __future__ import annotations

import concurrent.futures as _cf
import datetime as dt
import threading
from email.utils import parsedate_to_datetime
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import yfinance as yf

from app.core import cloud_files
from app.services.agent_service import ask_agent, memorize_user_note
from app.core.config import ROOT
from app.core.market import finnhub_key
from app.core.ticker import normalize_ticker, yfinance_symbol
from app.services.company_lookup_service import company_name_map, market_cap_map
from app.services.organizer_service import add_general_note, add_task, recall
from app.services.memory_engine import OnyxMemory
from app.services.portfolio_state_service import read_portfolio_rows_state, read_watchlist_rows_state
from app.services.postgres_core_service import (
    company_news_from_report_facts_pg,
    filing_stats_map_pg,
    list_news_wire_snapshot_pg,
    pg_connect,
    upsert_news_wire_snapshot_pg,
)

try:
    from tools.llm_engine import ask_ai
except Exception:  # pragma: no cover
    ask_ai = None  # type: ignore[assignment]

_HOME_CACHE: dict[str, object] = {"ts": 0.0, "pulse": {}, "news": {}, "snapshot_ts": 0.0, "snapshot": {}}
_HOME_SNAPSHOT_TTL_SEC = 180.0  # 3 min — background thread refreshes every 3 min
_SEC_SYNC_STATE_PATH = ROOT / "data" / "sec_sync_state.json"
_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _executive_signal_summary() -> dict[str, object]:
    raw = cloud_files.read_text("reports/executive_signal_latest.json")
    if not raw:
        return {"generated_at": "", "items": []}
    try:
        obj = json.loads(raw)
        items = obj.get("items") if isinstance(obj, dict) else []
        if not isinstance(items, list):
            items = []
        out = []
        for x in items[:12]:
            if not isinstance(x, dict):
                continue
            out.append(
                {
                    "ticker": str(x.get("ticker") or "").strip().upper(),
                    "score": str(x.get("score") or x.get("total_score") or ""),
                    "priority": str(x.get("priority") or ""),
                    "summary": str(x.get("summary") or x.get("reason") or ""),
                }
            )
        return {"generated_at": str(obj.get("generated_at") or ""), "items": out}
    except Exception:
        return {"generated_at": "", "items": []}


def dashboard_snapshot() -> dict[str, object]:
    counts = {"companies": 0, "open_tasks": 0, "daily_notes": 0, "notes": 0, "reminders_open": 0}
    movers_up: list[dict[str, object]] = []
    movers_down: list[dict[str, object]] = []
    feed: list[dict[str, str]] = []
    freshness = {"intel24": "", "news": "", "audit": ""}
    return {
        "counts": counts,
        "movers_up": movers_up,
        "movers_down": movers_down,
        "feed": feed,
        "freshness": freshness,
        "signals": _executive_signal_summary(),
        "home": home_snapshot(),
    }


_normalize_ticker = normalize_ticker
_yf_symbol = yfinance_symbol


def quick_capture(mode: str, text: str, ticker: str = "") -> tuple[bool, str]:
    m = str(mode or "note").strip().lower()
    txt = str(text or "").strip()
    tk = _normalize_ticker(ticker)
    if not txt:
        return False, "Please enter text."
    if m == "task":
        cat = "company" if tk else "general"
        ok = add_task(task=txt[:1000], ticker=tk, category=cat, priority="P2", due_date="")
        return (ok, "Task saved." if ok else "Task save failed.")
    scope = "company_note" if tk else "organizer_note"
    ok = add_general_note(txt[:4000], scope=scope, ticker=tk, tags="quick_capture")
    return (ok, "Note saved." if ok else "Note save failed.")


def ask_workspace_ai(question: str) -> str:
    """Full LLM response for workspace AI Agent channel, with investment context."""
    q = str(question or "").strip()
    if not q:
        return ""
    try:
        from tools.llm_engine import ask_ai as _ask_ai, get_ai_runtime_metrics as _ai_metrics
        today = dt.datetime.now().strftime("%A, %B %d, %Y")
        prompt = (
            f"You are an investment AI assistant. Today is {today}.\n"
            f"You help analyze portfolios, SEC filings, earnings, and investment theses.\n"
            f"Be concise (3-5 sentences max) and factual. Do not guess numbers.\n\n"
            f"User: {q}"
        )
        # Workspace chat defaults to fast lane; deep analysis paths remain explicit elsewhere.
        reply = _ask_ai(prompt, context="", mode="fast")
        if reply and reply.strip():
            out = reply.strip()
            low = out.lower()
            if ("transport issue while waiting for analysis result" in low) or ("service temporarily degraded" in low):
                detail = ""
                try:
                    m = dict(_ai_metrics() or {})
                    detail = str(m.get("last_error") or "").strip()
                except Exception:
                    detail = ""
                if detail:
                    return f"{out} Details: {detail[:220]}"
            return out
    except Exception:
        pass
    return ask_ai_local(q)


def ask_ai_local(question: str) -> str:
    q = str(question or "").strip()
    if not q:
        return ""
    low = q.lower()
    if "what day is today" in low or low in {"today?", "today", "date today", "what is today"}:
        now = dt.datetime.now()
        return now.strftime("Today is %A, %B %d, %Y.")
    if "what time is it" in low or "current time" in low:
        now = dt.datetime.now()
        return now.strftime("Current time is %H:%M.")
    tk = _infer_ticker_for_quote_question(q)
    if tk and _looks_like_price_question(low):
        live = _ticker_live_status(tk)
        if live:
            return live
        return f"[LIVE] No fresh market snapshot found for {tk}."
    # Gather live market context (macro snapshot + news for realtime queries)
    live_parts: list[str] = []
    try:
        from app.services.web_search_service import (
            get_live_macro_snapshot,
            format_macro_snapshot_text,
            is_realtime_query,
            search_news,
            format_news_text,
        )
        macro = get_live_macro_snapshot(timeout_sec=2.5)
        macro_text = format_macro_snapshot_text(macro)
        if macro_text:
            live_parts.append(f"LIVE MARKET SNAPSHOT:\n{macro_text}")
        if is_realtime_query(q):
            headlines = search_news(q, 6)
            news_text = format_news_text(headlines)
            if news_text:
                live_parts.append(f"RECENT NEWS HEADLINES:\n{news_text}")
    except Exception:
        pass
    extra_ctx = "\n\n".join(live_parts)
    try:
        out = ask_agent(q, n_results=6, extra_context=extra_ctx)
        if out:
            return out
    except Exception as exc:
        return f"AI temporarily unavailable: {exc}"
    ans, _src = recall(q, limit=8)
    return ans or "No answer found yet. Try adding more notes/tasks context."


def _looks_like_price_question(low: str) -> bool:
    keys = (
        " is ",
        " up",
        " down",
        " today",
        "green",
        "red",
        "price",
        "move",
        "%",
        "gain",
        "loss",
    )
    return any(k in low for k in keys)


def _infer_ticker_for_quote_question(text: str) -> str:
    q = str(text or "").strip()
    if not q:
        return ""
    # 1) Explicit ticker tokens first.
    tokens: list[str] = []
    stop = {
        "IS",
        "ARE",
        "WAS",
        "WERE",
        "UP",
        "DOWN",
        "TODAY",
        "NOW",
        "PRICE",
        "GREEN",
        "RED",
        "THE",
        "A",
        "AN",
        "OF",
        "TO",
        "IN",
        "ON",
        "AT",
        "FOR",
        "AND",
        "OR",
        "DO",
        "DID",
    }
    for m in re.findall(r"\$([A-Za-z]{1,6})\b|\b([A-Za-z]{2,5})\b", q):
        tk = _normalize_ticker(m[0] or m[1] or "")
        if tk and tk not in stop:
            tokens.append(tk)
    name_map = company_name_map([])
    for tk in tokens[:8]:
        if tk in name_map:
            return tk
    low = q.lower()
    best = ""
    best_len = 0
    for tk, nm in name_map.items():
        n = str(nm or "").strip().lower()
        if n and n in low and len(n) > best_len:
            best = tk
            best_len = len(n)
    return best


def _ticker_live_status(ticker: str) -> str:
    t = _normalize_ticker(ticker)
    if not t:
        return ""
    # Direct quote pull only.
    try:
        tk = yf.Ticker(_yf_symbol(t))
        fi = tk.fast_info or {}
        last = fi.get("lastPrice")
        prev = fi.get("previousClose")
        if last is not None and prev not in (None, 0):
            d = (float(last) - float(prev)) / abs(float(prev)) * 100.0
            asof = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            direction = "up" if d > 0 else ("down" if d < 0 else "flat")
            return f"[LIVE] {t} is {direction} {d:+.2f}% (as of {asof})."
    except Exception:
        pass
    return ""


def _profile_map() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for tk, nm in company_name_map([]).items():
        out[tk] = {"name": str(nm or "").strip(), "industry": ""}
    return out


def _daypct_map() -> dict[str, float]:
    return {}


def _filing_stats_map(tickers: list[str]) -> dict[str, dict[str, str | int]]:
    if not tickers:
        return {}
    try:
        out_pg = filing_stats_map_pg(tickers)
        if out_pg:
            return out_pg
    except Exception:
        pass
    return {t: {"filings": 0, "last_filing_date": ""} for t in tickers}


def _sec_sync_state_map() -> dict[str, dict[str, str]]:
    try:
        raw = cloud_files.read_text("data/sec_sync_state.json")
        if not raw:
            return {}
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            return {}
        out: dict[str, dict[str, str]] = {}
        for k, v in obj.items():
            t = _normalize_ticker(str(k or ""))
            if not t or not isinstance(v, dict):
                continue
            out[t] = {
                "running": str(v.get("running") or "0"),
                "last": str(v.get("last") or ""),
                "result": str(v.get("result") or ""),
                "message": str(v.get("message") or ""),
            }
        return out
    except Exception:
        return {}


def _read_portfolio() -> list[dict[str, str]]:
    return read_portfolio_rows_state()


def _read_watchlist() -> list[dict[str, str]]:
    return read_watchlist_rows_state()


def _fetch_text(url: str, timeout: int = 8) -> str:
    u = str(url or "").strip()
    to = max(3, int(timeout))
    # Prefer requests + certifi to avoid platform SSL store issues.
    try:
        import certifi  # type: ignore
        import requests  # type: ignore

        with requests.get(u, headers=dict(_HTTP_HEADERS), timeout=to, verify=certifi.where()) as r:
            r.raise_for_status()
            return str(r.text or "")
    except Exception:
        pass
    # Fallback to urllib default behavior.
    req = urllib.request.Request(u, headers=dict(_HTTP_HEADERS))
    with urllib.request.urlopen(req, timeout=to) as r:
        data = r.read()
    return data.decode("utf-8", errors="ignore")


def _finnhub_general_news(limit: int = 12) -> list[dict[str, str]]:
    key = finnhub_key()
    if not key:
        return []
    try:
        import certifi  # type: ignore
        import requests  # type: ignore

        with requests.get(
            "https://finnhub.io/api/v1/news",
            params={"category": "general", "token": key},
            timeout=10,
            verify=certifi.where(),
            headers={"Accept": "application/json", "User-Agent": _HTTP_HEADERS.get("User-Agent", "OnyxTerminal/1.0")},
        ) as r:
            r.raise_for_status()
            rows = r.json() or []
    except Exception:
        return []
    out: list[dict[str, str]] = []
    lim = max(1, min(40, int(limit)))
    for it in rows[: lim * 3]:
        title = str((it or {}).get("headline") or "").strip()
        link = str((it or {}).get("url") or "").strip()
        src_origin = str((it or {}).get("source") or "").strip()
        ts = str((it or {}).get("datetime") or "").strip()
        if not title:
            continue
        out.append(
            {
                "title": title,
                "link": link,
                "source": "Finnhub" + (f" ({src_origin})" if src_origin else ""),
                "published_at": ts,
            }
        )
        if len(out) >= lim:
            break
    return out


def _finnhub_company_news(tickers: list[str], limit: int = 12, days_back: int = 3) -> list[dict[str, str]]:
    key = finnhub_key()
    if not key:
        return []
    ts = [str(x or "").strip().upper() for x in tickers if str(x or "").strip()]
    if not ts:
        return []
    today = dt.date.today()
    date_from = (today - dt.timedelta(days=max(1, int(days_back)))).isoformat()
    date_to = today.isoformat()
    lim = max(1, min(40, int(limit)))
    per_ticker = max(2, min(6, lim // max(1, min(8, len(ts))) + 1))
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        import certifi  # type: ignore
        import requests  # type: ignore

        def _fetch_ticker(t: str) -> list[dict[str, str]]:
            try:
                with requests.get(
                    "https://finnhub.io/api/v1/company-news",
                    params={"symbol": t, "from": date_from, "to": date_to, "token": key},
                    timeout=8,
                    verify=certifi.where(),
                    headers={"Accept": "application/json", "User-Agent": _HTTP_HEADERS.get("User-Agent", "OnyxTerminal/1.0")},
                ) as r:
                    if r.status_code != 200:
                        return []
                    items: list[dict[str, str]] = []
                    for it in (r.json() or [])[:per_ticker * 2]:
                        title = str((it or {}).get("headline") or "").strip()
                        if not title:
                            continue
                        link = str((it or {}).get("url") or "").strip()
                        src_origin = str((it or {}).get("source") or "").strip()
                        ts_raw = str((it or {}).get("datetime") or "").strip()
                        items.append({
                            "title": f"{t}: {title}",
                            "ticker": t,
                            "link": link,
                            "source": "Finnhub" + (f" ({src_origin})" if src_origin else ""),
                            "published_at": ts_raw,
                        })
                    return items
            except Exception:
                return []

        # Parallel fetch — all tickers at once instead of sequential
        with _cf.ThreadPoolExecutor(max_workers=min(8, len(ts[:8]))) as ex:
            futures = [ex.submit(_fetch_ticker, t) for t in ts[:8]]
            for fut in _cf.as_completed(futures, timeout=12):
                try:
                    for item in (fut.result() or []):
                        key_norm = " ".join(f"{item.get('title','')}|{item.get('link','')}".lower().split())
                        if key_norm not in seen:
                            seen.add(key_norm)
                            out.append(item)
                except Exception:
                    pass
    except Exception:
        return out
    return out[:lim]


def _google_news(query: str, limit: int = 6) -> list[dict[str, str]]:
    q = str(query or "").strip()
    if not q:
        return []
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    )
    try:
        xml = _fetch_text(url, timeout=8)
        root = ET.fromstring(xml)
    except Exception:
        return []
    out: list[dict[str, str]] = []
    for it in root.findall(".//item")[: max(1, min(20, int(limit)))]:
        title = str(it.findtext("title") or "").strip()
        link = str(it.findtext("link") or "").strip()
        src = str(it.findtext("source") or "").strip()
        pub = str(it.findtext("pubDate") or "").strip()
        if title:
            out.append({"title": title, "link": link, "source": src, "published_at": pub})
    return out


def _rss_news(url: str, source_label: str, limit: int = 4) -> list[dict[str, str]]:
    try:
        xml = _fetch_text(str(url), timeout=8)
        root = ET.fromstring(xml)
    except Exception:
        return []
    out: list[dict[str, str]] = []
    for it in root.findall(".//item")[: max(1, min(20, int(limit)))]:
        title = str(it.findtext("title") or "").strip()
        link = str(it.findtext("link") or "").strip()
        pub = str(it.findtext("pubDate") or "").strip()
        if title:
            out.append({"title": title, "link": link, "source": source_label, "published_at": pub})
    return out


def _multi_source_news(limit: int = 8) -> list[dict[str, str]]:
    feeds = [
        ("https://feeds.reuters.com/reuters/topNews", "Reuters"),
        ("https://www.cnbc.com/id/100003114/device/rss/rss.html", "CNBC"),
        ("https://feeds.marketwatch.com/marketwatch/topstories/", "MarketWatch"),
        ("https://www.ft.com/world?format=rss", "Financial Times"),
        ("https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms", "Economic Times"),
        ("https://finance.yahoo.com/news/rssindex", "Yahoo Finance"),
    ]
    out: list[dict[str, str]] = []
    for url, src in feeds:
        out.extend(_rss_news(url, src, limit=4))
        if len(out) >= max(1, int(limit) * 3):
            break
    return _dedupe_news(out)[: max(1, min(30, int(limit)))]


def _yahoo_ticker_news(tickers: list[str], limit: int = 10) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    lim = max(1, min(40, int(limit)))
    for t in [str(x or "").strip().upper() for x in tickers if str(x or "").strip()][:12]:
        try:
            rows = list((yf.Ticker(t).news or []))[:8]
        except Exception:
            rows = []
        for it in rows:
            title = str(it.get("title") or "").strip()
            link = str(it.get("link") or it.get("url") or "").strip()
            if not title:
                continue
            key = " ".join(title.lower().split())
            if key in seen:
                continue
            seen.add(key)
            pub_epoch = it.get("providerPublishTime")
            pub_s = ""
            try:
                if pub_epoch:
                    pub_s = dt.datetime.fromtimestamp(int(pub_epoch), tz=dt.timezone.utc).isoformat()
            except Exception:
                pub_s = ""
            out.append({"title": f"{t}: {title}", "link": link, "source": "Yahoo", "published_at": pub_s})
            if len(out) >= lim:
                return out
    return out


def _news_fallback_from_feed(
    limit: int = 8,
    *,
    include_fast: bool = False,
    tickers: list[str] | None = None,
) -> list[dict[str, str]]:
    con_pg = pg_connect()
    if con_pg is None:
        return []
    try:
        lim = max(1, min(40, int(limit)))
        where: list[str] = []
        params: list[object] = []
        if not include_fast:
            where.append("COALESCE(category,'') != 'FAST_INTEL'")
        tks = [str(x or "").strip().upper() for x in (tickers or []) if str(x or "").strip()]
        if tks:
            where.append("ticker = ANY(%s)")
            params.append(tks)
        sql = "SELECT title, summary, ticker, created_at FROM intel_feed_core"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT %s"
        params.append(lim)
        cur = con_pg.cursor()
        cur.execute(sql, tuple(params))
        rows = cur.fetchall() or []
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for r in rows:
            title = str(r[0] or "").strip() or str(r[1] or "").strip()
            ticker = str(r[2] or "").strip().upper()
            if not title:
                continue
            key = " ".join(title.lower().split())
            if key in seen:
                continue
            seen.add(key)
            if ticker:
                title = f"{ticker}: {title}"
            link = f"/company_file/sec?t={urllib.parse.quote(ticker)}" if ticker else "/reports"
            out.append({"title": title, "link": link, "source": "SEC", "published_at": str(r[3] or "")})
        return out
    finally:
        con_pg.close()


def _company_news_from_report_facts(tickers: list[str], limit: int = 8) -> list[dict[str, str]]:
    tks = [str(x or "").strip().upper() for x in (tickers or []) if str(x or "").strip()]
    if not tks:
        return []
    try:
        out_pg = company_news_from_report_facts_pg(tks, limit=limit)
        if out_pg:
            return out_pg
    except Exception:
        pass
    return []


def _dedupe_news(items: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for n in items:
        title = str((n or {}).get("title") or "").strip()
        if not title:
            continue
        key = " ".join(title.lower().split())
        if key in seen:
            continue
        seen.add(key)
        link = str((n or {}).get("link") or "").strip()
        if not link:
            link = "https://www.google.com/search?" + urllib.parse.urlencode({"q": title})
        out.append(
            {
                "title": title,
                "link": link,
                "source": str((n or {}).get("source") or "").strip(),
                "published_at": str((n or {}).get("published_at") or "").strip(),
            }
        )
    return out


def _is_market_relevant_title(title: str, *, company_mode: bool = False) -> bool:
    t = str(title or "").strip()
    if not t:
        return False
    low = t.lower()
    # Drop obvious lifestyle/advice/personal-finance noise.
    reject = (
        "career expert",
        "life-changing sum",
        "inheritance",
        "kids",
        "job seeker",
        "how to",
        "what we're watching",
        "opinion",
        "best ",
        "top ",
        "buying stocks everyone admires",
    )
    if any(x in low for x in reject):
        return False
    # If company wire entry is ticker-prefixed, keep unless rejected.
    if company_mode and re.match(r"^[A-Z0-9.\-]{1,8}:\s+", t):
        return True
    keep = (
        "stock",
        "stocks",
        "market",
        "s&p",
        "nasdaq",
        "dow",
        "russell",
        "yield",
        "fed",
        "cpi",
        "inflation",
        "rate",
        "earnings",
        "guidance",
        "revenue",
        "eps",
        "profit",
        "margin",
        "buyback",
        "dividend",
        "merger",
        "acquisition",
        "sec",
        "antitrust",
        "lawsuit",
        "bankruptcy",
        "default",
        "crude",
        "oil",
        "gold",
        "gas",
        "ai",
        "chip",
        "semiconductor",
        "tariff",
    )
    return any(k in low for k in keep)


def _filter_relevant_news(items: list[dict[str, str]], *, company_mode: bool = False) -> list[dict[str, str]]:
    return [it for it in (items or []) if _is_market_relevant_title(str((it or {}).get("title") or ""), company_mode=company_mode)]


def _news_ts(item: dict[str, str]) -> dt.datetime | None:
    raw = str((item or {}).get("published_at") or "").strip()
    if not raw:
        return None
    # Try RFC822 pubDate.
    try:
        d = parsedate_to_datetime(raw)
        if d is not None:
            return d.astimezone(dt.timezone.utc) if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except Exception:
        pass
    # Try ISO.
    try:
        # Finnhub uses epoch seconds.
        if raw.isdigit():
            d0 = dt.datetime.fromtimestamp(int(raw), tz=dt.timezone.utc)
            return d0
    except Exception:
        pass
    try:
        d2 = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return d2.astimezone(dt.timezone.utc) if d2.tzinfo else d2.replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None


def _filter_recent_news(items: list[dict[str, str]], max_age_hours: int = 72, *, keep_undated_company: bool = False) -> list[dict[str, str]]:
    lim = max(6, min(240, int(max_age_hours or 72)))
    now = dt.datetime.now(dt.timezone.utc)
    out: list[dict[str, str]] = []
    for it in items or []:
        ts = _news_ts(it)
        if ts is None:
            # Keep undated items only if source is SEC fallback.
            if str((it or {}).get("source") or "").strip().upper() == "SEC":
                out.append(it)
            elif keep_undated_company and str((it or {}).get("ticker") or "").strip():
                out.append(it)
            continue
        age_h = (now - ts).total_seconds() / 3600.0
        if age_h <= lim:
            out.append(it)
    return out


def _latest_report_name(prefixes: tuple[str, ...]) -> str:
    cands = cloud_files.list_files("reports", suffixes={".md", ".txt", ".json", ".html"})
    for f in cands:
        n = str(f.name or "").lower()
        if any(n.startswith(x.lower()) for x in prefixes):
            return str(f.name or "")
    return ""


def _report_line_to_bullet(line: str) -> str:
    s = str(line or "").strip()
    if not s:
        return ""
    # Remove markdown/list prefixes.
    if s.startswith("#"):
        s = s.lstrip("#").strip()
    elif s.startswith(("- ", "* ")):
        s = s[2:].strip()
    elif re.match(r"^\d+\.\s+", s):
        s = re.sub(r"^\d+\.\s+", "", s)
    # Ignore file-name / index-like / UI-like lines.
    low = s.lower()
    if ".md" in low:
        return ""
    if re.search(r"\b[a-z0-9_]+_\d{8}(?:_\d{4})?\.md\b", low):
        return ""
    bad_tokens = (
        "open",
        "read",
        "must read now",
        "new since yesterday",
        "generated ",
        "ai reports cockpit",
        "morning mode",
        "full mode",
        "portfolio mention",
        "watchlist mention",
        "risk keywords",
    )
    if any(tok in low for tok in bad_tokens):
        return ""
    # Skip very short or pure labels.
    if len(s) < 14:
        return ""
    if re.fullmatch(r"[A-Za-z ]{1,24}", s):
        return ""
    # Keep narrative/signal lines only.
    signal = re.search(
        r"\b(risk|guidance|margin|debt|liquidity|alert|watch|priority|beat|miss|downgrade|upgrade|"
        r"outlook|revenue|eps|cash flow|runway|valuation|multiple|macro|rates|volatility|thesis|action)\b",
        s,
        flags=re.I,
    )
    if not signal and not (s.startswith(("Key", "Action", "What changed", "Focus"))):
        return ""
    s = re.sub(r"\s+", " ", s).strip(" -\t")
    if len(s) > 180:
        s = s[:177].rstrip() + "..."
    return s


def _report_updates(limit: int = 0) -> list[dict[str, str]]:
    files: list[tuple[str, str]] = [
        ("Daily", _latest_report_name(("terminal_daily_brief_", "daily_brief_"))),
        ("Appendix", _latest_report_name(("terminal_appendix_",))),
        ("L2", _latest_report_name(("l2_digest_",))),
    ]
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    max_items = int(limit or 0)
    for label, name in files:
        if not name:
            continue
        txt = cloud_files.read_text(f"reports/{name}")
        if not txt:
            continue
        lines = str(txt).splitlines()
        for ln in lines:
            s = _report_line_to_bullet(str(ln or ""))
            if not s:
                continue
            key = " ".join(s.lower().split())
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "source": label,
                    "title": s,
                    "link": "/reports/view?name=" + urllib.parse.quote(name),
                }
            )
        if max_items > 0 and len(out) >= max_items:
            break
    if max_items > 0:
        return out[:max_items]
    return out


def _fmt_num(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "-"
    try:
        return f"{float(v):,.{digits}f}"
    except Exception:
        return "-"


def _market_pulse_cached() -> dict[str, object]:
    now = time.time()
    try:
        ttl = int(str(os.getenv("INVESTOR_HOME_CACHE_TTL_SEC", "20")).strip() or "20")
    except Exception:
        ttl = 20
    if ttl > 0 and now - float(_HOME_CACHE.get("ts") or 0.0) < ttl and isinstance(_HOME_CACHE.get("pulse"), dict):
        return dict(_HOME_CACHE.get("pulse") or {})
    symbols = {
        "S&P 500": "^GSPC",
        "Nasdaq": "^IXIC",
        "Dow": "^DJI",
        "Russell 2000": "^RUT",
        "US 10Y": "^TNX",
        "VIX": "^VIX",
        "Gold": "GC=F",
        "Silver": "SI=F",
        "Crude Oil": "CL=F",
        "Nat Gas": "NG=F",
        "Copper": "HG=F",
    }
    out: dict[str, dict[str, str]] = {}
    for label, sym in symbols.items():
        px = "-"
        dp = "-"
        try:
            tk = yf.Ticker(sym)
            fi = tk.fast_info or {}
            last = fi.get("lastPrice")
            prev = fi.get("previousClose")
            if last is not None:
                px = _fmt_num(float(last), 2)
            if last is not None and prev not in (None, 0):
                d = (float(last) - float(prev)) / abs(float(prev)) * 100.0
                dp = f"{d:+.2f}%"
        except Exception:
            pass
        out[label] = {"price": px, "day": dp}
    pulse = {"as_of": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "items": out}
    _HOME_CACHE["ts"] = now
    _HOME_CACHE["pulse"] = pulse
    return pulse


def _regulatory_audit(portfolio: list[str], watchlist: list[str]) -> dict[str, object]:
    con_pg = pg_connect()
    if con_pg is None:
        return {"portfolio_count": 0, "watchlist_count": 0, "recent": []}
    try:
        cur = con_pg.cursor()
        p_cnt = 0
        if portfolio:
            cur.execute("SELECT COUNT(*) FROM changes_core WHERE ticker = ANY(%s)", (portfolio,))
            p_cnt = int((cur.fetchone() or [0])[0] or 0)
        w_cnt = 0
        if watchlist:
            cur.execute("SELECT COUNT(*) FROM changes_core WHERE ticker = ANY(%s)", (watchlist,))
            w_cnt = int((cur.fetchone() or [0])[0] or 0)
        cur.execute("SELECT ticker, section_name, summary, detected_at FROM changes_core ORDER BY id DESC LIMIT 12")
        recent = []
        for r in cur.fetchall() or []:
            recent.append(
                {
                    "ticker": str(r[0] or "").strip().upper(),
                    "section": str(r[1] or ""),
                    "summary": str(r[2] or ""),
                    "detected_at": str(r[3] or ""),
                }
            )
    finally:
        con_pg.close()
    return {"portfolio_count": p_cnt, "watchlist_count": w_cnt, "recent": recent}


def home_snapshot() -> dict[str, object]:
    now = time.time()
    try:
        snap_ts = float(_HOME_CACHE.get("snapshot_ts") or 0.0)
    except Exception:
        snap_ts = 0.0
    if now - snap_ts <= _HOME_SNAPSHOT_TTL_SEC:
        cached = _HOME_CACHE.get("snapshot")
        if isinstance(cached, dict) and cached:
            return dict(cached)

    prof = _profile_map()
    day_map = _daypct_map()
    portfolio_rows = _read_portfolio()
    watchlist_rows = _read_watchlist()
    tickers = sorted({r["ticker"] for r in portfolio_rows + watchlist_rows})
    mcap = market_cap_map(tickers)
    filing_stats = _filing_stats_map(tickers)
    sync_state = _sec_sync_state_map()
    for r in portfolio_rows:
        t = r["ticker"]
        r["name"] = str((prof.get(t) or {}).get("name") or t)
        r["industry"] = str((prof.get(t) or {}).get("industry") or "Unknown")
        r["market_cap"] = str(mcap.get(t) or "-")
        r["day_pct"] = day_map.get(t)
        r["filings"] = int((filing_stats.get(t) or {}).get("filings") or 0)
        r["last_filing_date"] = str((filing_stats.get(t) or {}).get("last_filing_date") or "")
        st = sync_state.get(t) or {}
        r["sync_running"] = str(st.get("running") or "0") == "1"
        r["sync_result"] = str(st.get("result") or "")
        r["sync_last"] = str(st.get("last") or "")
        r["sync_message"] = str(st.get("message") or "")
    for r in watchlist_rows:
        t = r["ticker"]
        r["name"] = str((prof.get(t) or {}).get("name") or t)
        r["industry"] = str((prof.get(t) or {}).get("industry") or "Unknown")
        r["market_cap"] = str(mcap.get(t) or "-")
        r["day_pct"] = day_map.get(t)
        r["filings"] = int((filing_stats.get(t) or {}).get("filings") or 0)
        r["last_filing_date"] = str((filing_stats.get(t) or {}).get("last_filing_date") or "")
        st = sync_state.get(t) or {}
        r["sync_running"] = str(st.get("running") or "0") == "1"
        r["sync_result"] = str(st.get("result") or "")
        r["sync_last"] = str(st.get("last") or "")
        r["sync_message"] = str(st.get("message") or "")

    news_general: list[dict[str, str]] = []
    news_company: list[dict[str, str]] = []
    _news_needs_refresh = True
    # Fast path: read from Postgres snapshot first (survives container restarts).
    # Only call Finnhub if data is older than 3 hours.
    try:
        pg_general = [dict(x) for x in list_news_wire_snapshot_pg("general", limit=16, max_age_hours=3)]
        pg_company = [dict(x) for x in list_news_wire_snapshot_pg("company", limit=16, max_age_hours=3)]
        if pg_general:
            news_general = pg_general
        if pg_company:
            news_company = pg_company
        _news_needs_refresh = not (news_general and news_company)
        # Even if we have cached rows, check if the newest article is too old
        # (e.g. weekend: cached Friday articles keep being served).
        # If all articles are >6h old by published_at, try Finnhub again.
        if not _news_needs_refresh:
            def _newest_pub(items: list[dict]) -> float:
                best = 0.0
                for it in items:
                    pa = str(it.get("published_at") or "").strip()
                    if not pa:
                        continue
                    try:
                        if pa.isdigit() and len(pa) >= 10:
                            best = max(best, float(pa))
                        else:
                            best = max(best, dt.datetime.fromisoformat(pa.replace("Z", "+00:00")).timestamp())
                    except Exception:
                        pass
                return best
            newest_ts = max(_newest_pub(news_general), _newest_pub(news_company))
            if newest_ts > 0:
                age_h = (dt.datetime.now(dt.timezone.utc).timestamp() - newest_ts) / 3600
                if age_h > 6:
                    _news_needs_refresh = True
    except Exception:
        _news_needs_refresh = True
    # Call Finnhub only when Postgres data is stale or missing.
    # Run in BACKGROUND thread so page render is never blocked by Finnhub latency.
    if _news_needs_refresh:
        _tickers_copy = list(tickers[:10])
        _need_general = not news_general
        _need_company = not news_company
        def _bg_finnhub_refresh():
            try:
                with _cf.ThreadPoolExecutor(max_workers=2) as ex:
                    fut_g = ex.submit(_finnhub_general_news, 16) if _need_general else None
                    fut_c = ex.submit(_finnhub_company_news, _tickers_copy, 16, 4) if _need_company else None
                    if fut_g:
                        try:
                            fh_g = _filter_relevant_news(fut_g.result(timeout=12) or [], company_mode=False)
                            fh_g = _filter_recent_news(fh_g, max_age_hours=96)
                            if fh_g:
                                upsert_news_wire_snapshot_pg("general", fh_g)
                                _HOME_CACHE["news_general_last"] = [dict(x) for x in fh_g]
                        except Exception:
                            pass
                    if fut_c:
                        try:
                            fh_c = _filter_relevant_news(fut_c.result(timeout=14) or [], company_mode=True)
                            fh_c = _filter_recent_news(fh_c, max_age_hours=120, keep_undated_company=True)
                            if fh_c:
                                upsert_news_wire_snapshot_pg("company", fh_c)
                                _HOME_CACHE["news_company_last"] = [dict(x) for x in fh_c]
                        except Exception:
                            pass
                # Invalidate home cache so next request picks up fresh news
                _HOME_CACHE["snapshot_ts"] = 0.0
            except Exception:
                pass
        threading.Thread(target=_bg_finnhub_refresh, daemon=True, name="finnhub-news-bg").start()
    # Local SEC/intel fallbacks if still empty.
    if not news_general:
        news_general = _news_fallback_from_feed(limit=8, include_fast=False)
    if not news_general:
        news_general = _news_fallback_from_feed(limit=8, include_fast=True)
    if not news_company:
        news_company = _news_fallback_from_feed(limit=8, include_fast=False, tickers=tickers[:10])
    if not news_company:
        news_company = _news_fallback_from_feed(limit=8, include_fast=True, tickers=tickers[:10])
    if not news_company:
        news_company = _company_news_from_report_facts(tickers[:12], limit=8)

    news_general = _dedupe_news(news_general)[:8]
    news_company = _dedupe_news(news_company)[:8]
    # Persist to Postgres so next cold start is instant.
    if news_general:
        try:
            upsert_news_wire_snapshot_pg("general", news_general)
        except Exception:
            pass
    if news_company:
        try:
            upsert_news_wire_snapshot_pg("company", news_company)
        except Exception:
            pass
    # Last-resort Postgres fallback (older than 3h but still valid).
    if not news_general:
        try:
            news_general = [dict(x) for x in list_news_wire_snapshot_pg("general", limit=8, max_age_hours=168)]
        except Exception:
            news_general = []
    if not news_company:
        try:
            news_company = [dict(x) for x in list_news_wire_snapshot_pg("company", limit=8, max_age_hours=168)]
        except Exception:
            news_company = []
    # Last-good fallback for transient provider outages.
    if not news_general:
        last_general = list(_HOME_CACHE.get("news_general_last") or [])
        if last_general:
            news_general = [dict(x) for x in last_general if isinstance(x, dict)][:8]
    if not news_company:
        last_company = list(_HOME_CACHE.get("news_company_last") or [])
        if last_company:
            news_company = [dict(x) for x in last_company if isinstance(x, dict)][:8]
    used = {" ".join(str(x.get("title") or "").lower().split()) for x in news_general}
    news_company = [x for x in news_company if " ".join(str(x.get("title") or "").lower().split()) not in used][:8]

    pulse = _market_pulse_cached()
    audit = _regulatory_audit([r["ticker"] for r in portfolio_rows], [r["ticker"] for r in watchlist_rows])
    report_updates = _report_updates(limit=0)
    out = {
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "Beta v2",
        "portfolio": portfolio_rows,
        "watchlist": watchlist_rows,
        "news_general": news_general,
        "news_company": news_company,
        "report_updates": report_updates,
        "pulse": pulse,
        "audit": audit,
    }
    if news_general:
        _HOME_CACHE["news_general_last"] = [dict(x) for x in news_general]
    if news_company:
        _HOME_CACHE["news_company_last"] = [dict(x) for x in news_company]
    _HOME_CACHE["snapshot_ts"] = now
    _HOME_CACHE["snapshot"] = out
    return out


def portfolio_intelligence_brief(home: dict[str, object], my_metrics: dict[str, object]) -> str:
    portfolio = list(home.get("portfolio", []) or [])
    watchlist = list(home.get("watchlist", []) or [])
    if not portfolio and not watchlist:
        return "No portfolio/watchlist data found yet."

    p_lines: list[str] = []
    for r in (my_metrics.get("portfolio_rows") or [])[:15]:
        p_lines.append(
            f"{r.get('ticker')}: weight {float(r.get('weight_pct') or 0):.1f}% | "
            f"day {float(r.get('day_pct') or 0):+.2f}% | "
            f"pnl {float(r.get('pnl_usd') or 0):,.0f}"
        )
    w_lines: list[str] = [str(r.get("ticker") or "") for r in watchlist[:20]]
    quick = [str(x) for x in (my_metrics.get("quick_analysis") or [])[:6]]
    intel = []
    for r in (my_metrics.get("intel24") or [])[:10]:
        intel.append(
            f"{r.get('ticker')}: {float(r.get('day_pct') or 0):+.2f}% | {r.get('happened') or '-'} | {r.get('suggestion') or '-'}"
        )

    memory_ctx = ""
    try:
        if str(os.getenv("INVESTOR_ENABLE_MEMORY", "0")).strip().lower() in {"1", "true", "yes", "on"}:
            mem = OnyxMemory()
            if mem.available:
                q = "portfolio allocation concentration risk watchlist catalysts"
                rows = mem.recall(q, n_results=6)
                if rows:
                    memory_ctx = "\n".join([f"- {str(x.get('text') or '')[:240]}" for x in rows])
    except Exception:
        memory_ctx = ""
    if not memory_ctx:
        ans, src = recall("portfolio concentration watchlist risk", limit=6)
        memory_ctx = (ans or "").strip()[:800]

    prompt = (
        "Create a portfolio intelligence brief for a long-term investor.\n"
        "Output sections:\n"
        "1) Exposure + diagnostics (concentration/diversification)\n"
        "2) Top risks in next 8-21 days\n"
        "3) Actionable watchlist opportunities\n"
        "4) Clear actions (max 5 bullets)\n\n"
        f"Portfolio rows:\n{chr(10).join(p_lines) or '-'}\n\n"
        f"Watchlist tickers:\n{', '.join([x for x in w_lines if x]) or '-'}\n\n"
        f"Quick analysis:\n{chr(10).join(quick) or '-'}\n\n"
        f"Intel24:\n{chr(10).join(intel) or '-'}\n\n"
        f"Memory context:\n{memory_ctx or '-'}\n"
    )
    sys = "You are a portfolio intelligence copilot. Be concise, specific, and practical."

    if ask_ai is not None:
        try:
            return str(ask_ai(prompt, sys, mode="smart") or "").strip()
        except Exception:
            pass
    return ask_agent(
        "Provide portfolio intelligence brief using concentration, catalysts, and next actions for current holdings."
    )


_PROFILE_KEYS = [
    "investing_style", "time_horizon", "position_sizing", "risk_limits",
    "strengths", "weaknesses_blindspots", "mistakes_top10", "mistakes_why_happened",
    "mistakes_avoidability", "mistakes_lessons", "decision_checklist", "sell_discipline",
]


def get_data_integrity_health() -> dict:
    """Return live counts from Postgres for the dashboard Health tab."""
    from app.services.postgres_core_service import pg_connect
    result: dict = {
        "profile": {"filled": 0, "total": len(_PROFILE_KEYS), "pct": 0, "missing_keys": list(_PROFILE_KEYS)},
        "interview_queue": {"open": 0, "done": 0},
        "gap_prompts_30d": {"open": 0, "answered": 0},
        "agent_runs_7d": {"success": 0, "error": 0, "running": 0},
    }
    con = pg_connect()
    if con is None:
        return result
    try:
        cur = con.cursor()

        # Profile completeness
        cur.execute(
            "SELECT key FROM investor_style_memory_core WHERE answer IS NOT NULL AND answer != ''"
        )
        filled_keys = {row[0] for row in cur.fetchall()}
        filled = len([k for k in _PROFILE_KEYS if k in filled_keys])
        missing = [k for k in _PROFILE_KEYS if k not in filled_keys]
        total = len(_PROFILE_KEYS)
        result["profile"] = {
            "filled": filled,
            "total": total,
            "pct": int(filled / total * 100) if total else 0,
            "missing_keys": missing,
        }

        # Interview queue
        cur.execute(
            "SELECT status, COUNT(*) FROM portfolio_interview_queue_core GROUP BY status"
        )
        for status, cnt in cur.fetchall():
            if status == "pending":
                result["interview_queue"]["open"] = cnt
            elif status == "done":
                result["interview_queue"]["done"] = cnt

        # Gap prompts (last 30 days)
        cutoff_30d = (dt.datetime.now() - dt.timedelta(days=30)).isoformat()
        cur.execute(
            "SELECT status, COUNT(*) FROM daily_operator_gap_prompts_core"
            " WHERE created_at >= %s GROUP BY status",
            (cutoff_30d,),
        )
        for status, cnt in cur.fetchall():
            if status == "open":
                result["gap_prompts_30d"]["open"] = cnt
            elif status in ("answered", "done"):
                result["gap_prompts_30d"]["answered"] += cnt

        # Agent runs (last 7 days)
        cutoff_7d = (dt.datetime.now() - dt.timedelta(days=7)).isoformat()
        cur.execute(
            "SELECT status, COUNT(*) FROM agent_runs_core"
            " WHERE started_at >= %s GROUP BY status",
            (cutoff_7d,),
        )
        for status, cnt in cur.fetchall():
            if status in ("ok", "completed", "skipped", "done"):
                result["agent_runs_7d"]["success"] += cnt
            elif status in ("error", "failed", "cleaned_up_stuck_run"):
                result["agent_runs_7d"]["error"] += cnt
            elif status == "running":
                result["agent_runs_7d"]["running"] = cnt

    except Exception:
        pass
    finally:
        con.close()
    return result
