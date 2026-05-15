#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import requests
import yfinance as yf

try:
    from youtubesearchpython import VideosSearch
    import youtubesearchpython.core.requests as ys_requests
except Exception:
    VideosSearch = None
    ys_requests = None

try:
    from youtube_transcript_api import YouTubeTranscriptApi
except Exception:
    YouTubeTranscriptApi = None

ROOT = Path("/Users/solmaz/Investment_Tools")
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
OUT_PATH = REPORTS / "executive_signal_latest.json"
sys.path.insert(0, str(ROOT))
from tools.llm_engine import ask_ai

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
NETWORK_TIMEOUT_SECONDS = 30
socket.setdefaulttimeout(12)

TITANS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM"]

KNOWN_CEO_BY_TICKER = {
    "AAPL": "Tim Cook",
    "AMZN": "Andy Jassy",
    "CRM": "Marc Benioff",
    "GOOGL": "Sundar Pichai",
    "GOOG": "Sundar Pichai",
    "HUBS": "Yamini Rangan",
    "INTU": "Sasan Goodarzi",
    "JPM": "Jamie Dimon",
    "MDB": "Dev Ittycheria",
    "META": "Mark Zuckerberg",
    "MSFT": "Satya Nadella",
    "NOW": "Bill McDermott",
    "NVDA": "Jensen Huang",
    "ORCL": "Safra Catz",
    "SAP": "Christian Klein",
    "TSLA": "Elon Musk",
}

_HTTPX_PATCHED = False


def _configure_search_headers() -> None:
    # Use a modern browser UA to reduce bot-style blocking by upstream.
    if ys_requests is not None:
        try:
            ys_requests.userAgent = USER_AGENT
        except Exception:
            pass
    _configure_http_transport()


def _configure_http_transport() -> None:
    global _HTTPX_PATCHED
    if _HTTPX_PATCHED:
        return
    # Ignore potentially broken proxy env in background app contexts.
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ.pop(k, None)

    orig_post = httpx.post

    def patched_post(url, *args, **kwargs):
        headers = kwargs.get("headers") or {}
        headers = dict(headers)
        headers.setdefault("User-Agent", USER_AGENT)
        kwargs["headers"] = headers
        kwargs.setdefault("timeout", NETWORK_TIMEOUT_SECONDS)
        kwargs.setdefault("trust_env", False)
        return orig_post(url, *args, **kwargs)

    httpx.post = patched_post  # type: ignore[assignment]
    _HTTPX_PATCHED = True


def _age_hours(path: Path) -> float:
    if not path.exists():
        return 1e9
    return max(0.0, (dt.datetime.now().timestamp() - path.stat().st_mtime) / 3600.0)


def _read_personal_tickers() -> list[str]:
    tickers: set[str] = set()
    p = DATA / "portfolio.csv"
    if p.exists():
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            t = s.split(",", 1)[0].strip().upper()
            if t:
                tickers.add(t)
    w = DATA / "my_watchlist.txt"
    if w.exists():
        for ln in w.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            t = s.split(",", 1)[0].strip().upper()
            if t:
                tickers.add(t)
    return sorted(tickers)


def _parse_duration_seconds(d: str) -> int:
    if not d:
        return 0
    parts = [int(x) for x in d.split(":") if x.isdigit()]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 1:
        return parts[0]
    return 0


def _age_days_from_label(label: str) -> float:
    s = (label or "").lower().strip()
    m = re.search(r"(\d+)\s+minute", s)
    if m:
        return float(m.group(1)) / 1440.0
    h = re.search(r"(\d+)\s+hour", s)
    if h:
        return float(h.group(1)) / 24.0
    d = re.search(r"(\d+)\s+day", s)
    if d:
        return float(d.group(1))
    w = re.search(r"(\d+)\s+week", s)
    if w:
        return float(w.group(1)) * 7.0
    mo = re.search(r"(\d+)\s+month", s)
    if mo:
        return float(mo.group(1)) * 30.0
    y = re.search(r"(\d+)\s+year", s)
    if y:
        return float(y.group(1)) * 365.0
    return 9999.0


def _find_ceo_name(ticker: str) -> tuple[str, str]:
    t = ticker.strip().upper()
    try:
        tk = yf.Ticker(t)
        for getter_name in ("get_info", "info"):
            try:
                info = getattr(tk, getter_name)
                info = info() if callable(info) else info
                info = info or {}
                officers = info.get("companyOfficers") or []
                if isinstance(officers, list):
                    for o in officers:
                        if not isinstance(o, dict):
                            continue
                        title = str(o.get("title") or "").lower()
                        if "chief executive" in title or title.startswith("ceo") or "ceo" in title:
                            nm = str(o.get("name") or "").strip()
                            if nm:
                                return nm, f"yfinance:{getter_name}"
            except Exception:
                continue
    except Exception:
        pass
    fb = KNOWN_CEO_BY_TICKER.get(t, "")
    if fb:
        return fb, "fallback:known_map"
    return "", "unavailable"


def _search_best_video(
    queries: list[str],
    min_duration_seconds: int = 8 * 60,
    max_age_days: float | None = None,
    limit: int = 20,
) -> tuple[dict[str, str], str]:
    if VideosSearch is None:
        return _search_best_video_requests_fallback(queries, max_age_days=max_age_days)

    best: dict[str, str] = {}
    best_score = -10**9
    seen: set[str] = set()
    last_err = ""

    for q in queries:
        try:
            search = VideosSearch(q, limit=limit, timeout=NETWORK_TIMEOUT_SECONDS)
            rows = search.result().get("result") or []
        except Exception as exc:
            last_err = str(exc)
            continue

        for r in rows:
            dur = _parse_duration_seconds(str(r.get("duration") or ""))
            if dur < min_duration_seconds:
                continue
            pub = str(r.get("publishedTime") or "")
            age_days = _age_days_from_label(pub)
            if max_age_days is not None and age_days > max_age_days:
                continue
            link = str(r.get("link") or "")
            vid = str(r.get("id") or "")
            if not vid and "v=" in link:
                vid = link.split("v=", 1)[-1].split("&", 1)[0]
            if not vid or vid in seen:
                continue
            seen.add(vid)

            title = str(r.get("title") or "")
            tl = title.lower()
            score = 0
            if "interview" in tl:
                score += 6
            if "earnings" in tl or "conference" in tl or "press conference" in tl or "speech" in tl:
                score += 3
            score += max(0, int((30.0 - min(age_days, 30.0)) * 2))
            score += min(dur, 3600) // 120

            cand = {
                "video_id": vid,
                "title": title,
                "link": link,
                "duration": str(r.get("duration") or ""),
                "published_time": pub,
                "age_days": f"{age_days:.1f}",
                "query": q,
            }
            if score > best_score:
                best_score = score
                best = cand

    if best:
        return best, ""
    # Fallback path when library returns no candidates or is blocked in this runtime.
    fb_video, fb_err = _search_best_video_requests_fallback(queries, max_age_days=max_age_days)
    if fb_video:
        return fb_video, ""
    if last_err:
        return {}, f"search_error: {last_err}"
    return {}, (fb_err or "no_hit")


def _search_best_video_requests_fallback(
    queries: list[str],
    max_age_days: float | None = None,
) -> tuple[dict[str, str], str]:
    last_err = ""
    for q in queries:
        try:
            r = requests.get(
                "https://www.youtube.com/results",
                params={"search_query": q},
                headers={"User-Agent": USER_AGENT},
                timeout=NETWORK_TIMEOUT_SECONDS,
            )
            r.raise_for_status()
            body = r.text or ""
        except Exception as exc:
            last_err = str(exc)
            continue

        # Extract coarse metadata from page JSON blobs when available.
        ids = re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', body)
        if not ids:
            continue
        seen: set[str] = set()
        for vid in ids:
            if vid in seen:
                continue
            seen.add(vid)
            # Try to find published text near the video id region.
            idx = body.find(f'"videoId":"{vid}"')
            snippet = body[max(0, idx - 2000) : idx + 4000] if idx >= 0 else ""
            pub_match = re.search(r'"publishedTimeText":\{"simpleText":"([^"]+)"\}', snippet)
            dur_match = re.search(r'"lengthText":\{"simpleText":"([^"]+)"\}', snippet)
            title_match = re.search(r'"title":\{"runs":\[\{"text":"([^"]+)"\}\]\}', snippet)
            pub = pub_match.group(1) if pub_match else ""
            if max_age_days is not None:
                if _age_days_from_label(pub) > max_age_days:
                    continue
            dur = dur_match.group(1) if dur_match else ""
            title = title_match.group(1) if title_match else f"YouTube result for {q}"
            return (
                {
                    "video_id": vid,
                    "title": title,
                    "link": f"https://www.youtube.com/watch?v={vid}",
                    "duration": dur,
                    "published_time": pub,
                    "query": q,
                },
                "",
            )
    # Final fallback: use system curl (often works even when Python resolver fails).
    for q in queries:
        url = f"https://www.youtube.com/results?search_query={q.replace(' ', '+')}"
        try:
            proc = subprocess.run(
                ["curl", "-L", "--max-time", str(NETWORK_TIMEOUT_SECONDS), "-A", USER_AGENT, url],
                capture_output=True,
                text=True,
                timeout=NETWORK_TIMEOUT_SECONDS + 5,
            )
            if proc.returncode != 0:
                last_err = (proc.stderr or "").strip() or f"curl_exit_{proc.returncode}"
                continue
            body = proc.stdout or ""
        except Exception as exc:
            last_err = str(exc)
            continue

        ids = re.findall(r'"videoId":"([A-Za-z0-9_-]{11})"', body)
        if not ids:
            continue
        seen: set[str] = set()
        for vid in ids:
            if vid in seen:
                continue
            seen.add(vid)
            idx = body.find(f'"videoId":"{vid}"')
            snippet = body[max(0, idx - 2000) : idx + 4000] if idx >= 0 else ""
            pub_match = re.search(r'"publishedTimeText":\{"simpleText":"([^"]+)"\}', snippet)
            dur_match = re.search(r'"lengthText":\{"simpleText":"([^"]+)"\}', snippet)
            title_match = re.search(r'"title":\{"runs":\[\{"text":"([^"]+)"\}\]\}', snippet)
            pub = pub_match.group(1) if pub_match else ""
            if max_age_days is not None and _age_days_from_label(pub) > max_age_days:
                continue
            dur = dur_match.group(1) if dur_match else ""
            title = title_match.group(1) if title_match else f"YouTube result for {q}"
            return (
                {
                    "video_id": vid,
                    "title": title,
                    "link": f"https://www.youtube.com/watch?v={vid}",
                    "duration": dur,
                    "published_time": pub,
                    "query": q,
                },
                "",
            )

    return {}, (f"search_error: {last_err}" if last_err else "no_hit")


def _fetch_transcript(video_id: str) -> tuple[str, str]:
    if YouTubeTranscriptApi is None:
        return "", "missing youtube-transcript-api"
    try:
        # youtube-transcript-api v1.x exposes instance method `fetch`;
        # older versions exposed classmethod `get_transcript`.
        if hasattr(YouTubeTranscriptApi, "get_transcript"):
            rows = YouTubeTranscriptApi.get_transcript(video_id)  # type: ignore[attr-defined]
            text = " ".join((x.get("text") or "").strip() for x in rows if x.get("text"))
        else:
            api = YouTubeTranscriptApi()
            fetched = api.fetch(video_id, languages=("en", "en-US"))
            text = " ".join((getattr(s, "text", "") or "").strip() for s in fetched if getattr(s, "text", ""))
    except Exception as exc:
        return "", f"transcript_error: {exc}"
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "", "transcript_empty"
    return text, ""


def _ask(system: str, prompt: str) -> str:
    use_llm = os.getenv("EXEC_SIGNAL_USE_LLM", "").strip().lower() in {"1", "true", "yes", "on"}
    if not use_llm:
        # Fast deterministic mode to avoid long network waits in scheduled/dashboard runs.
        body = prompt[-12000:]
        lines = re.split(r"(?<=[.!?])\s+", body)
        keywords = (
            "rate", "inflation", "liquidity", "buyback", "dividend", "capex",
            "guidance", "revenue", "margin", "cost", "demand", "pricing",
        )
        picks: list[str] = []
        for ln in lines:
            s = ln.strip()
            if len(s) < 40:
                continue
            sl = s.lower()
            if any(k in sl for k in keywords) or re.search(r"\d", s):
                picks.append(s[:180])
            if len(picks) >= 3:
                break
        if not picks:
            return "Transcript captured. No high-confidence extractive signal lines found."
        return "\n".join(f"- {p}" for p in picks)
    try:
        return ask_ai(prompt, system).strip()
    except Exception as exc:
        return f"Signal generation unavailable: {exc}"


def _fallback_local_signal(ticker: str) -> str:
    db = DATA / "research.db"
    if not db.exists():
        return ""
    t = ticker.strip().upper()
    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        filings = conn.execute(
            "SELECT form, date FROM filings WHERE ticker = ? ORDER BY date DESC LIMIT 3",
            (t,),
        ).fetchall()
        changes = conn.execute(
            "SELECT summary FROM changes WHERE ticker = ? ORDER BY detected_at DESC LIMIT 2",
            (t,),
        ).fetchall()
    except Exception:
        return ""
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not filings and not changes:
        return ""
    parts: list[str] = []
    if filings:
        fs = ", ".join(f"{r['form']} {r['date']}" for r in filings if r["form"] and r["date"])
        parts.append(f"Latest filings: {fs}.")
    if changes:
        cs = [str(r["summary"] or "").strip()[:140] for r in changes if str(r["summary"] or "").strip()]
        if cs:
            parts.append("Recent deltas: " + " | ".join(cs) + ".")
    return " ".join(parts).strip()


def _macro_layer(year: int) -> list[dict[str, object]]:
    targets = [
        {"ticker": "FED", "label": "Fed Chair", "query": f"Federal Reserve Chair speech {year}"},
        {"ticker": "ECB", "label": "ECB President", "query": f"ECB President press conference {year}"},
    ]
    out: list[dict[str, object]] = []
    for tgt in targets:
        queries = [tgt["query"], f"{tgt['label']} speech {year}", f"{tgt['label']} interview {year}"]
        video, err = _search_best_video(queries, min_duration_seconds=8 * 60, max_age_days=365)
        item: dict[str, object] = {
            "layer": "macro",
            "ticker": tgt["ticker"],
            "role": tgt["label"],
            "status": "",
            "signal": "",
        }
        if not video:
            item["status"] = err or "no_hit"
            item["signal"] = "No recent signal"
            out.append(item)
            continue
        item["video"] = video
        transcript, terr = _fetch_transcript(str(video.get("video_id") or ""))
        if not transcript:
            item["status"] = terr or "transcript_unavailable"
            item["signal"] = "No recent signal"
            out.append(item)
            continue
        system = (
            f"Analyze this macro interview/speech from {tgt['label']}. "
            "Extract 3 signals: (1) Rate Path (Hikes/Cuts?), (2) Inflation Tone, (3) Liquidity."
        )
        prompt = (
            f"Role: {tgt['label']}\nVideo title: {video.get('title')}\n"
            f"Published: {video.get('published_time')}\n\nTranscript excerpt:\n{transcript[:12000]}"
        )
        item["status"] = "ok"
        item["signal"] = _ask(system, prompt)
        out.append(item)
    return out


def _portfolio_layer(year: int, personal: list[str]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for ticker in personal:
        ceo, ceo_source = _find_ceo_name(ticker)
        queries = []
        if ceo:
            queries.append(f"{ceo} {ticker} interview {year}")
            queries.append(f"{ceo} {ticker} earnings call {year}")
        queries.append(f"{ticker} CEO interview {year}")
        queries.append(f"{ticker} earnings call {year}")
        queries.append(f"{ticker} conference {year}")

        video, err = _search_best_video(queries, min_duration_seconds=8 * 60, max_age_days=365)
        item: dict[str, object] = {
            "layer": "portfolio",
            "ticker": ticker,
            "ceo": ceo or None,
            "ceo_source": ceo_source,
            "status": "",
            "signal": "",
        }
        if not video:
            fb = _fallback_local_signal(ticker)
            if fb:
                item["status"] = f"fallback_local ({err or 'no_hit'})"
                item["signal"] = fb
            else:
                item["status"] = err or "no_hit"
                item["signal"] = "No recent signal"
            out.append(item)
            continue
        item["video"] = video
        transcript, terr = _fetch_transcript(str(video.get("video_id") or ""))
        if not transcript:
            fb = _fallback_local_signal(ticker)
            if fb:
                item["status"] = f"fallback_local ({terr or 'transcript_unavailable'})"
                item["signal"] = fb
            else:
                item["status"] = terr or "transcript_unavailable"
                item["signal"] = "No recent signal"
            out.append(item)
            continue
        system = (
            f"Analyze this interview with CEO {ceo or 'unknown'} of {ticker}. "
            "Extract: (1) Capital Allocation, (2) Confidence, (3) Future Alpha."
        )
        prompt = (
            f"Ticker: {ticker}\nCEO: {ceo or 'Unknown'}\nVideo title: {video.get('title')}\n"
            f"Published: {video.get('published_time')}\n\nTranscript excerpt:\n{transcript[:12000]}"
        )
        item["status"] = "ok"
        item["signal"] = _ask(system, prompt)
        out.append(item)
    return out


def _titan_layer(year: int, owned: set[str]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for ticker in TITANS:
        if ticker in owned:
            continue
        ceo, ceo_source = _find_ceo_name(ticker)
        queries = []
        if ceo:
            queries.append(f"{ceo} {ticker} interview {year}")
            queries.append(f"{ceo} {ticker} earnings call {year}")
        queries.append(f"{ticker} CEO interview {year}")
        queries.append(f"{ticker} earnings call {year}")

        # Titan constraint: include only when video is < 7 days old.
        video, err = _search_best_video(queries, min_duration_seconds=8 * 60, max_age_days=7)
        if not video:
            continue
        item: dict[str, object] = {
            "layer": "titan",
            "ticker": ticker,
            "ceo": ceo or None,
            "ceo_source": ceo_source,
            "video": video,
            "status": "",
            "signal": "",
        }
        transcript, terr = _fetch_transcript(str(video.get("video_id") or ""))
        if not transcript:
            item["status"] = terr or err or "transcript_unavailable"
            item["signal"] = "No recent signal"
            out.append(item)
            continue
        system = (
            f"Analyze this interview with CEO {ceo or 'unknown'} of {ticker}. "
            "Extract: (1) Capital Allocation, (2) Confidence, (3) Future Alpha."
        )
        prompt = (
            f"Ticker: {ticker}\nCEO: {ceo or 'Unknown'}\nVideo title: {video.get('title')}\n"
            f"Published: {video.get('published_time')}\n\nTranscript excerpt:\n{transcript[:12000]}"
        )
        item["status"] = "ok"
        item["signal"] = _ask(system, prompt)
        out.append(item)
    return out


def build_payload(limit: int) -> dict[str, object]:
    _configure_search_headers()
    now = dt.datetime.now()
    year = now.year
    personal = _read_personal_tickers()
    owned = set(personal)

    macro = _macro_layer(year)
    portfolio = _portfolio_layer(year, personal)
    titan = _titan_layer(year, owned)

    items = (macro + portfolio + titan)[: max(1, limit)]
    return {
        "generated_at": now.isoformat(),
        "year": year,
        "count": len(items),
        "personal_universe": personal,
        "items": items,
    }


def run_diagnostics() -> dict[str, object]:
    _configure_search_headers()
    proxies = {k: os.getenv(k) for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")}
    checks: dict[str, object] = {
        "python": sys.executable,
        "httpx_version": getattr(httpx, "__version__", "unknown"),
        "proxies": proxies,
    }
    try:
        r = requests.get(
            "https://www.youtube.com",
            headers={"User-Agent": USER_AGENT},
            timeout=NETWORK_TIMEOUT_SECONDS,
        )
        checks["youtube_http_requests"] = {"ok": True, "status": r.status_code}
    except Exception as exc:
        checks["youtube_http_requests"] = {"ok": False, "error": str(exc)}
    try:
        r2 = httpx.get(
            "https://www.youtube.com",
            headers={"User-Agent": USER_AGENT},
            timeout=NETWORK_TIMEOUT_SECONDS,
            trust_env=False,
        )
        checks["youtube_http_httpx"] = {"ok": True, "status": r2.status_code}
    except Exception as exc:
        checks["youtube_http_httpx"] = {"ok": False, "error": str(exc)}
    try:
        c = subprocess.run(
            ["curl", "-I", "-L", "--max-time", str(NETWORK_TIMEOUT_SECONDS), "https://www.youtube.com"],
            capture_output=True,
            text=True,
            timeout=NETWORK_TIMEOUT_SECONDS + 5,
        )
        checks["youtube_http_curl"] = {
            "ok": c.returncode == 0,
            "returncode": c.returncode,
            "head": (c.stdout or c.stderr or "")[:220],
        }
    except Exception as exc:
        checks["youtube_http_curl"] = {"ok": False, "error": str(exc)}
    if VideosSearch is None:
        checks["youtube_search_lib"] = {"ok": False, "error": "missing youtube-search-python"}
    else:
        try:
            rows = VideosSearch("NVIDIA Jensen Huang interview", limit=2, timeout=NETWORK_TIMEOUT_SECONDS).result().get("result") or []
            checks["youtube_search_lib"] = {"ok": True, "count": len(rows)}
        except Exception as exc:
            checks["youtube_search_lib"] = {"ok": False, "error": str(exc)}
    return checks


def main() -> None:
    p = argparse.ArgumentParser(description="Build executive signal panel data (Macro -> Portfolio -> Titan).")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--max-age-hours", type=float, default=6.0)
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--diag", action="store_true", help="Print YouTube/network diagnostics JSON.")
    args = p.parse_args()

    if args.diag:
        print(json.dumps(run_diagnostics(), indent=2))
        return

    REPORTS.mkdir(parents=True, exist_ok=True)
    if (not args.refresh) and _age_hours(OUT_PATH) <= args.max_age_hours:
        print(str(OUT_PATH))
        return

    try:
        payload = build_payload(limit=args.limit)
    except Exception as exc:
        payload = {
            "generated_at": dt.datetime.now().isoformat(),
            "count": 1,
            "items": [
                {
                    "layer": "other",
                    "ticker": "EXEC",
                    "status": "build_failed",
                    "signal": f"Executive signal build failed: {exc}",
                }
            ],
        }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(str(OUT_PATH), flush=True)


if __name__ == "__main__":
    main()
