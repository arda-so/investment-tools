#!/usr/bin/env python3
from __future__ import annotations

import json
import datetime as dt
import re
from typing import Any

import requests
import xml.etree.ElementTree as ET
from app.core.market import finnhub_key

try:
    import feedparser  # type: ignore
except Exception:
    feedparser = None

FINNHUB_NEWS_URL = "https://finnhub.io/api/v1/news"
FINNHUB_COMPANY_NEWS_URL = "https://finnhub.io/api/v1/company-news"
OLLAMA_CHAT_URL = "http://127.0.0.1:11434/api/chat"

RSS_SOURCES: list[tuple[str, str]] = [
    ("Reuters", "https://feeds.reuters.com/reuters/businessNews"),
    ("Reuters", "https://feeds.reuters.com/reuters/marketsNews"),
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("AP", "https://apnews.com/hub/business/rss"),
    ("Barrons", "https://www.barrons.com/feeds/barrons-news"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
]

SOURCE_WEIGHT: dict[str, int] = {
    "Finnhub": 8,
    "Reuters": 7,
    "AP": 6,
    "CNBC": 5,
    "Barrons": 5,
    "MarketWatch": 4,
}


def fetch_latest_headlines(limit: int = 20, api_key: str = "", timeout: int = 10) -> list[dict[str, str]]:
    key = (api_key or finnhub_key()).strip()
    if not key:
        return []
    try:
        resp = requests.get(
            FINNHUB_NEWS_URL,
            params={"category": "general", "token": key},
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        rows = resp.json()
        out: list[dict[str, str]] = []
        for r in (rows or [])[: max(1, int(limit))]:
            title = str((r or {}).get("headline") or "").strip()
            if not title:
                continue
            out.append(
                {
                    "headline": title,
                    "summary": str((r or {}).get("summary") or "").strip(),
                    "url": str((r or {}).get("url") or "").strip(),
                    "datetime": str((r or {}).get("datetime") or "").strip(),
                    "source": "Finnhub",
                    "origin": str((r or {}).get("source") or "").strip(),
                }
            )
        return out
    except Exception:
        return []


def fetch_company_headlines(ticker: str, days_back: int = 3, limit: int = 20, api_key: str = "", timeout: int = 10) -> list[dict[str, str]]:
    key = (api_key or finnhub_key()).strip()
    t = (ticker or "").strip().upper()
    if not key or not t:
        return []
    today = dt.date.today()
    fr = today - dt.timedelta(days=max(1, int(days_back)))
    try:
        resp = requests.get(
            FINNHUB_COMPANY_NEWS_URL,
            params={"symbol": t, "from": fr.isoformat(), "to": today.isoformat(), "token": key},
            timeout=timeout,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        rows = resp.json()
        out: list[dict[str, str]] = []
        for r in (rows or [])[: max(1, int(limit))]:
            title = str((r or {}).get("headline") or "").strip()
            if not title:
                continue
            out.append(
                {
                    "headline": title,
                    "summary": str((r or {}).get("summary") or "").strip(),
                    "url": str((r or {}).get("url") or "").strip(),
                    "datetime": str((r or {}).get("datetime") or "").strip(),
                    "source": "Finnhub",
                    "origin": str((r or {}).get("source") or "").strip(),
                    "ticker": t,
                }
            )
        return out
    except Exception:
        return []


def _safe_text(x: Any) -> str:
    return str(x or "").strip()


def _norm_headline(s: str) -> str:
    x = _safe_text(s).lower()
    x = re.sub(r"[^a-z0-9 ]+", " ", x)
    return re.sub(r"\s+", " ", x).strip()


def _event_score(headline: str, summary: str) -> int:
    blob = f"{headline} {summary}".lower()
    strong = (
        "earnings",
        "guidance",
        "merger",
        "acquisition",
        "lawsuit",
        "sues",
        "sec",
        "doj",
        "investigation",
        "antitrust",
        "bankruptcy",
        "default",
        "cuts forecast",
        "raises forecast",
        "warns",
        "downgrade",
        "upgrade",
    )
    weak_noise = ("opinion", "top 10", "how to", "analysis:")
    score = 0
    for k in strong:
        if k in blob:
            score += 2
    for k in weak_noise:
        if k in blob:
            score -= 2
    return score


def _is_market_moving(headline: str, summary: str) -> bool:
    return _event_score(headline, summary) > 0


def fetch_rss_news(limit: int = 80, timeout: int = 8) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for source, url in RSS_SOURCES:
        if feedparser is not None:
            try:
                feed = feedparser.parse(url, request_headers={"User-Agent": "OnyxTerminal/3.1"}, timeout=timeout)  # type: ignore[arg-type]
            except TypeError:
                feed = feedparser.parse(url, request_headers={"User-Agent": "OnyxTerminal/3.1"})
            except Exception:
                feed = None
            entries = getattr(feed, "entries", []) if feed is not None else []
            for e in (entries or [])[:15]:
                title = _safe_text(getattr(e, "title", ""))
                if not title:
                    continue
                link = _safe_text(getattr(e, "link", ""))
                summary = _safe_text(getattr(e, "summary", ""))
                published = _safe_text(getattr(e, "published", "")) or _safe_text(getattr(e, "updated", ""))
                rows.append(
                    {
                        "headline": title,
                        "summary": summary,
                        "url": link,
                        "datetime": published,
                        "source": source,
                    }
                )
                if len(rows) >= limit:
                    return rows
            continue
        # Fallback when feedparser is not available.
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": "OnyxTerminal/3.1"})
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            for item in root.findall(".//item")[:15]:
                title = _safe_text(item.findtext("title"))
                if not title:
                    continue
                rows.append(
                    {
                        "headline": title,
                        "summary": _safe_text(item.findtext("description")),
                        "url": _safe_text(item.findtext("link")),
                        "datetime": _safe_text(item.findtext("pubDate")),
                        "source": source,
                    }
                )
                if len(rows) >= limit:
                    return rows
        except Exception:
            continue
    return rows


def _dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    best: dict[str, dict[str, str]] = {}
    for r in rows:
        h = _safe_text(r.get("headline"))
        if not h:
            continue
        key = _norm_headline(h)
        cur = best.get(key)
        if not cur:
            best[key] = r
            continue
        src_cur = _safe_text(cur.get("source"))
        src_new = _safe_text(r.get("source"))
        score_cur = SOURCE_WEIGHT.get(src_cur, 1) + _event_score(_safe_text(cur.get("headline")), _safe_text(cur.get("summary")))
        score_new = SOURCE_WEIGHT.get(src_new, 1) + _event_score(_safe_text(r.get("headline")), _safe_text(r.get("summary")))
        if score_new > score_cur:
            best[key] = r
    return list(best.values())


def _rank_and_diversify(rows: list[dict[str, str]], limit: int = 12, max_per_source: int = 3) -> list[dict[str, str]]:
    if not rows:
        return []
    rows = _dedupe_rows(rows)
    scored: list[tuple[int, dict[str, str]]] = []
    for r in rows:
        h = _safe_text(r.get("headline"))
        s = _safe_text(r.get("summary"))
        src = _safe_text(r.get("source"))
        base = SOURCE_WEIGHT.get(src, 1)
        ev = _event_score(h, s)
        scored.append((base * 10 + ev, r))
    scored.sort(key=lambda x: x[0], reverse=True)

    out: list[dict[str, str]] = []
    per_source: dict[str, int] = {}
    for _, r in scored:
        src = _safe_text(r.get("source")) or "Unknown"
        if per_source.get(src, 0) >= max_per_source:
            continue
        out.append(r)
        per_source[src] = per_source.get(src, 0) + 1
        if len(out) >= limit:
            break
    return out


def _keyword_market_moving(rows: list[dict[str, str]], limit: int = 12) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for r in rows:
        h = _safe_text(r.get("headline"))
        s = _safe_text(r.get("summary"))
        if _is_market_moving(h, s):
            out.append(r)
        if len(out) >= limit:
            break
    return _rank_and_diversify(out, limit=limit, max_per_source=3)


def _ollama_filter(rows: list[dict[str, str]], model: str = "llama3", timeout: int = 25) -> list[dict[str, str]]:
    if not rows:
        return []
    payload_rows = [
        {"headline": r.get("headline", ""), "summary": r.get("summary", ""), "url": r.get("url", ""), "source": r.get("source", "")}
        for r in rows[:20]
    ]
    system = (
        "You are a news editor. Filter these headlines. Return JSON list of ONLY the market-moving stories "
        "(Earnings, Mergers, Lawsuits, major regulatory actions). Ignore generic opinion pieces. "
        "Each JSON item must include: headline, url, source, reason. Return JSON only."
    )
    prompt = "Headlines JSON:\n" + json.dumps(payload_rows, ensure_ascii=True)
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "stream": False,
    }
    try:
        resp = requests.post(OLLAMA_CHAT_URL, json=body, timeout=timeout, headers={"Content-Type": "application/json"})
        resp.raise_for_status()
        txt = str(((resp.json() or {}).get("message") or {}).get("content") or "").strip()
        if not txt:
            return []
        m = txt.find("[")
        n = txt.rfind("]")
        if m < 0 or n <= m:
            return []
        parsed: Any = json.loads(txt[m : n + 1])
        if not isinstance(parsed, list):
            return []
        out: list[dict[str, str]] = []
        by_norm: dict[str, dict[str, str]] = {}
        for r in rows:
            key = _norm_headline(_safe_text(r.get("headline")))
            if key:
                by_norm[key] = r
        for r in parsed:
            if not isinstance(r, dict):
                continue
            h = str(r.get("headline") or "").strip()
            if not h:
                continue
            fallback_src = _safe_text((by_norm.get(_norm_headline(h)) or {}).get("source")) or "Mixed"
            out.append(
                {
                    "headline": h,
                    "url": str(r.get("url") or "").strip(),
                    "source": str(r.get("source") or "").strip() or fallback_src,
                    "reason": str(r.get("reason") or "").strip(),
                }
            )
        return _rank_and_diversify(out, limit=12, max_per_source=3)
    except Exception:
        return []


def get_market_moving_news(limit: int = 12, api_key: str = "", model: str = "llama3") -> list[dict[str, str]]:
    base = fetch_latest_headlines(limit=max(20, limit * 2), api_key=api_key, timeout=10)
    broad = fetch_rss_news(limit=max(40, limit * 4), timeout=8)
    merged = _rank_and_diversify(base + broad, limit=max(24, limit * 3), max_per_source=3)
    if not merged:
        return []
    filtered = _ollama_filter(merged, model=model, timeout=25)
    if filtered:
        return filtered[:limit]
    return _keyword_market_moving(merged, limit=limit)


def _contains_any_ticker(text: str, tickers: list[str]) -> bool:
    up = f" {text.upper()} "
    for t in tickers:
        if f" {t} " in up or f"({t})" in up:
            return True
    return False


def _filter_broad_for_tickers(rows: list[dict[str, str]], tickers: list[str]) -> list[dict[str, str]]:
    if not tickers:
        return rows
    out: list[dict[str, str]] = []
    for r in rows:
        h = _safe_text(r.get("headline"))
        s = _safe_text(r.get("summary"))
        if _contains_any_ticker(h, tickers) or _contains_any_ticker(s, tickers):
            out.append(r)
    return out


def get_market_moving_news_for_tickers(
    tickers: list[str],
    limit: int = 12,
    api_key: str = "",
    model: str = "llama3",
) -> list[dict[str, str]]:
    uniq = []
    seen = set()
    for t in tickers:
        s = str(t or "").strip().upper()
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    if not uniq:
        return []
    rows: list[dict[str, str]] = []
    per = max(4, int(limit / max(1, len(uniq))) + 2)
    for t in uniq[:8]:
        rows.extend(fetch_company_headlines(t, days_back=3, limit=per, api_key=api_key, timeout=10))
    broad = _filter_broad_for_tickers(fetch_rss_news(limit=max(40, limit * 4), timeout=8), uniq)
    merged = _rank_and_diversify(rows + broad, limit=max(24, limit * 3), max_per_source=3)
    if not merged:
        return []
    filtered = _ollama_filter(merged, model=model, timeout=25)
    if filtered:
        return filtered[:limit]
    return _keyword_market_moving(merged, limit=limit)
