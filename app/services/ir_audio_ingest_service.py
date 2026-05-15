from __future__ import annotations

import datetime as dt
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

import ssl as _ssl

_HTTP_TIMEOUT = 20
# Use a browser-like UA — many IR sites (Q4Inc, Nasdaq IR, etc.) block obvious bot UAs.
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# SSL context that tolerates corporate/CDN certs (Cloud Run sometimes has chain issues).
_SSL_CTX = _ssl.create_default_context()
try:
    import certifi as _certifi
    _SSL_CTX = _ssl.create_default_context(cafile=_certifi.where())
except Exception:
    _SSL_CTX.check_hostname = False
    _SSL_CTX.verify_mode = _ssl.CERT_NONE


def _http_text(url: str) -> str:
    u = str(url or "").strip()
    if not u:
        return ""
    req = urllib.request.Request(u, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT, context=_SSL_CTX) as r:
        raw = r.read()
    return raw.decode("utf-8", errors="ignore")


def _abs_url(base_url: str, url: str) -> str:
    import html as _html
    u = _html.unescape(str(url or "").strip())
    return str(urllib.parse.urljoin(str(base_url or "").strip(), u))


def _audio_score(url: str, title: str = "") -> int:
    s = f"{str(url or '').lower()} {str(title or '').lower()}"
    score = 0
    if ".mp3" in s:
        score += 5
    if ".m3u8" in s:
        score += 4
    if ".mp4" in s or ".wav" in s:
        score += 3
    if "earnings" in s:
        score += 3
    if "replay" in s or "recording" in s:
        score += 2
    if "webcast" in s or "conference" in s or "call" in s:
        score += 2
    # Webcast platform event paths score high — yt-dlp can extract the stream from these.
    # These only appear on IR pages in earnings context, never as generic media.
    if any(p in s for p in ("event.webcasts.com/", "edge.media-server.com/mmc/", "on24.com/event", "viavid.com/", "chorus.ai/")):
        score += 4
    elif any(p in s for p in ("viavid.com", "on24.com", "webcasts.com", "chorus.ai")):
        score += 1
    return score


def _extract_audio_urls_from_html(base_url: str, html: str) -> list[str]:
    txt = str(html or "")
    if not txt:
        return []
    urls: list[str] = []
    # href/src/url style candidates that already include media extension.
    for pat in (
        r"""(?:href|src)\s*=\s*["']([^"']+\.(?:mp3|m3u8|mp4|wav|ogg)(?:\?[^"']*)?)["']""",
        r"""["'](https?://[^"']+\.(?:mp3|m3u8|mp4|wav|ogg)(?:\?[^"']*)?)["']""",
        # data-* attributes that carry media or stream URLs.
        r"""data-(?:url|src|audio|media|stream|webcast)\s*=\s*["']([^"']+)["']""",
        # iframe embeds pointing to common webcast/replay platforms.
        r"""<iframe[^>]+src\s*=\s*["']([^"']*(?:viavid\.com|on24\.com|webcasts\.com|notified\.com|chorus\.ai|media-server\.com|limelight\.com|q4cdn\.com)[^"']*)["']""",
        # anchor/button links to webcast platform pages (follow these for m3u8 in next hop).
        r"""href\s*=\s*["']([^"']*(?:viavid\.com|on24\.com|webcasts\.com|edge\.media-server\.com|notified\.com|chorus\.ai)[^"']*)["']""",
    ):
        for m in re.findall(pat, txt, flags=re.I):
            u = _abs_url(base_url, str(m))
            if u:
                urls.append(u)
    # JSON-ish blobs can hide streamUrl/audioUrl values.
    for key in ("streamUrl", "audioUrl", "audio_url", "mediaUrl", "webcastUrl", "replayUrl", "recordingUrl", "playbackUrl"):
        for m in re.findall(rf"""{key}\s*["']?\s*:\s*["']([^"']+)["']""", txt, flags=re.I):
            u = _abs_url(base_url, str(m))
            if re.search(r"\.(mp3|m3u8|mp4|wav)(\?|$)|viavid\.com|on24\.com|webcasts\.com|chorus\.ai|media-server\.com", u, flags=re.I):
                urls.append(u)
    out: list[str] = []
    seen: set[str] = set()
    for u in urls:
        su = str(u).strip()
        if not su or su in seen:
            continue
        seen.add(su)
        out.append(su)
    return out


def _extract_event_links_from_html(base_url: str, html: str) -> list[str]:
    txt = str(html or "")
    if not txt:
        return []
    links: list[str] = []
    for href in re.findall(r"""href\s*=\s*["']([^"']+)["']""", txt, flags=re.I):
        low = str(href or "").lower()
        if not any(k in low for k in ("earnings", "webcast", "events", "investor", "conference", "call", "replay", "recording", "archive", "presentation", "results")):
            continue
        u = _abs_url(base_url, href)
        if u:
            links.append(u)
    out: list[str] = []
    seen: set[str] = set()
    for u in links:
        su = str(u).strip()
        if not su or su in seen:
            continue
        seen.add(su)
        out.append(su)
    return out[:12]


def _extract_audio_candidates_from_rss(rss_url: str, rss_xml: str) -> list[dict[str, str]]:
    raw = str(rss_xml or "").strip()
    if not raw:
        return []
    items: list[dict[str, str]] = []
    try:
        root = ET.fromstring(raw)
    except Exception:
        return []
    for item in root.findall(".//item"):
        title = str(item.findtext("title") or "").strip()
        pub = str(item.findtext("pubDate") or "").strip()
        link = str(item.findtext("link") or "").strip()
        urls: list[str] = []
        for enc in item.findall("enclosure"):
            u = str(enc.attrib.get("url") or "").strip()
            if u:
                urls.append(_abs_url(rss_url, u))
        if link:
            urls.append(_abs_url(rss_url, link))
        for u in urls:
            if not re.search(r"\.(mp3|m3u8)(\?|$)", u, flags=re.I):
                continue
            items.append(
                {
                    "audio_url": str(u),
                    "title": title[:320],
                    "event_datetime": pub[:40],
                    "event_url": link[:1600],
                    "source": "rss",
                }
            )
    # Sort by score descending, then keep unique URLs.
    items.sort(key=lambda x: _audio_score(x.get("audio_url") or "", x.get("title") or ""), reverse=True)
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for it in items:
        u = str(it.get("audio_url") or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        out.append(it)
    return out[:16]


def discover_latest_ir_audio(
    *,
    ticker: str,
    ir_home_url: str = "",
    rss_url: str = "",
    last_good_event_url: str = "",
    last_good_audio_url: str = "",
) -> dict[str, Any]:
    tk = str(ticker or "").strip().upper()[:16]
    if not tk:
        return {"ok": False, "error": "ticker_required", "candidates": []}

    candidates: list[dict[str, str]] = []
    crawled: list[str] = []

    # 0) Direct audio URL override — skips all crawling (use for JS-heavy IR sites).
    direct = str(last_good_audio_url or "").strip()
    if direct:
        candidates.append({
            "audio_url": direct,
            "title": f"{tk} earnings call (manual)",
            "event_datetime": dt.datetime.now().isoformat(),
            "event_url": direct,
            "source": "direct_override",
        })

    # 1) RSS first when available.
    rss = str(rss_url or "").strip()
    if rss:
        try:
            xml = _http_text(rss)
            candidates.extend(_extract_audio_candidates_from_rss(rss, xml))
            crawled.append(rss)
        except Exception:
            pass

    # 2) Last known good event page.
    lge = str(last_good_event_url or "").strip()
    if lge:
        try:
            html = _http_text(lge)
            crawled.append(lge)
            for u in _extract_audio_urls_from_html(lge, html):
                candidates.append(
                    {
                        "audio_url": u,
                        "title": "",
                        "event_datetime": "",
                        "event_url": lge,
                        "source": "last_good_event_url",
                    }
                )
        except Exception:
            pass

    # 3) IR home + one hop into event links.
    home = str(ir_home_url or "").strip()
    if home:
        try:
            html = _http_text(home)
            crawled.append(home)
            for u in _extract_audio_urls_from_html(home, html):
                candidates.append(
                    {
                        "audio_url": u,
                        "title": "",
                        "event_datetime": "",
                        "event_url": home,
                        "source": "ir_home",
                    }
                )
            for ev in _extract_event_links_from_html(home, html):
                try:
                    ev_html = _http_text(ev)
                    crawled.append(ev)
                except Exception:
                    continue
                for u in _extract_audio_urls_from_html(ev, ev_html):
                    candidates.append(
                        {
                            "audio_url": u,
                            "title": "",
                            "event_datetime": "",
                            "event_url": ev,
                            "source": "ir_event_page",
                        }
                    )
        except Exception:
            pass

    # Normalize, rank, dedupe.
    uniq: list[dict[str, str]] = []
    seen: set[str] = set()
    for c in candidates:
        u = str((c or {}).get("audio_url") or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        uniq.append(
            {
                "audio_url": u[:2000],
                "title": str((c or {}).get("title") or "")[:320],
                "event_datetime": str((c or {}).get("event_datetime") or "")[:40],
                "event_url": str((c or {}).get("event_url") or "")[:1600],
                "source": str((c or {}).get("source") or "unknown")[:40],
            }
        )
    uniq.sort(key=lambda x: _audio_score(x.get("audio_url") or "", x.get("title") or ""), reverse=True)

    # Require a minimum quality score (4+) to avoid promo videos / generic mp4s that
    # score only on file extension without earnings/webcast context.
    _MIN_SCORE = 4
    best = dict(uniq[0]) if uniq else {}
    best_score = _audio_score(best.get("audio_url") or "", best.get("title") or "") if best else 0
    quality_ok = bool(best) and best_score >= _MIN_SCORE
    if best and not best.get("event_datetime"):
        best["event_datetime"] = dt.datetime.now().isoformat()
    return {
        "ok": quality_ok,
        "error": "" if quality_ok else ("audio_not_found" if not best else "audio_quality_too_low"),
        "ticker": tk,
        "best": best if quality_ok else {},
        "candidates": uniq[:20],
        "crawled_urls": crawled[:24],
    }

