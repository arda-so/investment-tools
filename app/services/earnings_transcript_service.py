from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.parse
import urllib.request
from typing import Any

from app.core.filing_text import resolve_filing_path, read_filing_text
from app.core.market import finnhub_key
from app.services.postgres_core_service import (
    pg_connect,
    query_report_facts_pg,
    upsert_earnings_transcript_pg,
    list_earnings_transcripts_pg,
    delete_earnings_transcripts_by_ids_pg,
)

try:
    from tools.llm_engine import ask_ai
except Exception:
    ask_ai = None  # type: ignore


_FORMS = {"8-K", "6-K", "10-Q", "10-K", "20-F", "40-F"}
_HTTP_TIMEOUT = 20


def _ticker(raw: str) -> str:
    s = re.sub(r"[^A-Z0-9.\-]", "", str(raw or "").strip().upper())
    return s[:16]


def _extract_release_payload(raw: str) -> dict[str, Any] | None:
    s = str(raw or "").strip()
    if not s or len(s) < 180:
        return None
    low = s.lower()
    # Earnings-release style signals (less strict than transcript).
    has_release = bool(
        re.search(
            r"\b(earnings release|financial results|quarterly results|results for the quarter|reported .* earnings)\b",
            low,
        )
    )
    if not has_release:
        return None
    # Skip obvious boilerplate-only fragments.
    if _looks_like_sec_boilerplate(low) and len(s) < 2500:
        return None
    m = re.search(r"(earnings release|financial results|quarterly results|results for the quarter)", low)
    i = m.start() if m else 0
    excerpt = s[max(0, i - 80) : min(len(s), i + 520)].strip()
    return {"title": "Earnings Release", "excerpt": excerpt[:700], "char_count": int(len(s))}


def _fiscal_quarter(call_date: str) -> tuple[int | None, str]:
    s = str(call_date or "").strip()
    if not s:
        return None, ""
    try:
        d = dt.date.fromisoformat(s[:10])
    except Exception:
        return None, ""
    q = ((d.month - 1) // 3) + 1
    return int(d.year), f"Q{q}"


def _extract_payload(raw: str) -> dict[str, Any] | None:
    s = str(raw or "").strip()
    if not s or len(s) < 240:
        return None
    low = s.lower()
    if _looks_like_sec_boilerplate(low):
        return None
    key_hits = [
        "earnings call",
        "conference call",
        "prepared remarks",
        "question-and-answer",
        "question and answer",
        "operator:",
        "earnings release",
        "transcript",
    ]
    if not any(k in low for k in key_hits):
        return None
    if not _is_transcript_like(low):
        return None
    if not _is_earnings_context(low):
        return None
    score = 0.35
    score += 0.1 if "earnings call" in low or "conference call" in low else 0.0
    score += 0.1 if "prepared remarks" in low else 0.0
    score += 0.1 if "question-and-answer" in low or "question and answer" in low else 0.0
    score += 0.1 if "operator:" in low else 0.0
    score += 0.05 if len(s) > 8000 else 0.0
    score = min(0.95, score)

    m = re.search(r"(earnings call|conference call|prepared remarks|question(?:-and-|\s+and\s+)answer)", low)
    i = m.start() if m else 0
    start = max(0, i - 120)
    end = min(len(s), i + 540)
    excerpt = s[start:end].strip()
    title = "Earnings Call Transcript"
    t = re.search(r"([A-Z][A-Za-z0-9&,\-(). ]{0,120}(?:earnings|conference)\s+call)", s, flags=re.I)
    if t:
        title = t.group(1).strip()[:140]
    return {
        "title": title,
        "excerpt": excerpt[:700],
        "transcript_text": s[:180000],
        "char_count": int(len(s)),
        "quality_score": float(score),
        "is_partial": bool(len(s) < 2500),
    }


def _looks_like_sec_boilerplate(low: str) -> bool:
    s = str(low or "")
    boilerplate_markers = (
        "material financial information to our investors using our investor relations website",
        "press releases, sec filings and public conference calls and webcasts",
        "complying with our disclosure obligations under regulation fd",
        "we also use the following social media channels as a means of disclosing information",
    )
    return any(m in s for m in boilerplate_markers)


def _is_transcript_like(low: str) -> bool:
    s = str(low or "")
    strong = 0
    if "operator:" in s:
        strong += 1
    if "question-and-answer" in s or "question and answer" in s:
        strong += 1
    if "prepared remarks" in s:
        strong += 1
    if "analyst" in s:
        strong += 1
    if re.search(r"\b(good morning|good afternoon|welcome)\b", s):
        strong += 1
    # Require at least 2 strong markers to avoid SEC boilerplate false positives.
    return strong >= 2


def _is_earnings_context(low: str) -> bool:
    s = str(low or "")
    deny = (
        "merger agreement",
        "acquisition",
        "tender offer",
        "special meeting",
        "proxy statement",
    )
    if any(x in s for x in deny):
        return False
    hits = 0
    if "earnings call" in s:
        hits += 1
    if "earnings release" in s:
        hits += 1
    if "financial results" in s:
        hits += 1
    if "fiscal" in s:
        hits += 1
    if "quarter ended" in s or "for the quarter" in s:
        hits += 1
    return hits >= 2


def _http_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "InvestorOS/2.0 (+local)"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
        raw = r.read()
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", errors="ignore"))
    except Exception:
        return None


def _normalize_ext_item(item: dict[str, Any]) -> dict[str, Any] | None:
    d = dict(item or {})
    txt = (
        d.get("transcript")
        or d.get("content")
        or d.get("text")
        or d.get("body")
        or d.get("prepared_remarks")
        or d.get("remarks")
        or ""
    )
    payload = _extract_payload(str(txt or ""))
    if not payload:
        return None
    date_s = str(
        d.get("date")
        or d.get("publishedDate")
        or d.get("published_at")
        or d.get("fiscalDateEnding")
        or d.get("fiscal_date")
        or d.get("datetime")
        or ""
    ).strip()[:10]
    fy = d.get("year") or d.get("fiscalYear")
    fq_raw = str(d.get("quarter") or d.get("fiscalQuarter") or "").strip().upper()
    fq = f"Q{fq_raw}" if fq_raw.isdigit() else (fq_raw if fq_raw.startswith("Q") else "")
    if (not fy or not fq) and date_s:
        cy, cq = _fiscal_quarter(date_s)
        fy = fy or cy
        fq = fq or cq
    title = str(
        d.get("title")
        or d.get("name")
        or d.get("headline")
        or payload.get("title")
        or "Earnings Call Transcript"
    ).strip()[:220]
    source_url = str(
        d.get("url")
        or d.get("link")
        or d.get("transcript_url")
        or d.get("source_url")
        or ""
    ).strip()[:1200]
    return {
        "call_date": date_s,
        "fiscal_year": int(fy) if str(fy or "").isdigit() else None,
        "fiscal_quarter": fq[:8],
        "title": title,
        "source_url": source_url,
        "excerpt": str(payload.get("excerpt") or ""),
        "transcript_text": str(payload.get("transcript_text") or ""),
        "char_count": int(payload.get("char_count") or 0),
        "quality_score": float(payload.get("quality_score") or 0.0),
        "is_partial": bool(payload.get("is_partial")),
    }


def _flatten_payload(obj: Any) -> list[dict[str, Any]]:
    if isinstance(obj, list):
        return [dict(x) for x in obj if isinstance(x, dict)]
    if not isinstance(obj, dict):
        return []
    keys = (
        "transcripts",
        "earnings_call_transcripts",
        "data",
        "results",
        "items",
        "calls",
        "history",
    )
    for k in keys:
        v = obj.get(k)
        if isinstance(v, list):
            return [dict(x) for x in v if isinstance(x, dict)]
    # sometimes payload itself is one record
    if any(k in obj for k in ("transcript", "content", "text", "prepared_remarks")):
        return [obj]
    return []


def _provider_items_fmp(ticker: str, limit: int) -> list[dict[str, Any]]:
    key = str(os.getenv("FMP_API_KEY", "")).strip()
    if not key:
        return []
    tk = urllib.parse.quote(_ticker(ticker))
    lim = max(1, min(80, int(limit or 24)))
    # Stable API flow:
    # 1) get transcript dates by symbol
    # 2) fetch transcript per (year, quarter)
    # Docs show stable endpoints under /stable/*
    date_urls = [
        f"https://financialmodelingprep.com/stable/earning-call-transcript-dates?symbol={tk}&apikey={urllib.parse.quote(key)}",
        f"https://financialmodelingprep.com/stable/transcripts-dates?symbol={tk}&apikey={urllib.parse.quote(key)}",
    ]
    date_rows: list[dict[str, Any]] = []
    for u in date_urls:
        try:
            obj = _http_json(u)
            rows = _flatten_payload(obj)
            if rows:
                date_rows = rows
                break
        except Exception:
            continue
    out: list[dict[str, Any]] = []
    if date_rows:
        pairs: list[tuple[int, int, str]] = []
        for r in date_rows:
            try:
                y = int(r.get("year") or r.get("fiscalYear") or 0)
                q = int(r.get("quarter") or r.get("fiscalQuarter") or 0)
            except Exception:
                continue
            if y <= 0 or q not in {1, 2, 3, 4}:
                continue
            d = str(r.get("date") or r.get("publishedDate") or "")
            pairs.append((y, q, d))
        pairs.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        for y, q, _d in pairs[:lim]:
            u = (
                "https://financialmodelingprep.com/stable/earning-call-transcript"
                f"?symbol={tk}&year={y}&quarter={q}&apikey={urllib.parse.quote(key)}"
            )
            try:
                obj = _http_json(u)
                rows = _flatten_payload(obj)
                if rows:
                    out.extend(rows[:1])
            except Exception:
                continue
        if out:
            return out[:lim]
    # Final fallback: latest transcripts endpoint (if plan supports it)
    latest_urls = [
        f"https://financialmodelingprep.com/stable/earning-call-transcript-latest?apikey={urllib.parse.quote(key)}&limit={lim}",
        f"https://financialmodelingprep.com/stable/latest-transcripts?apikey={urllib.parse.quote(key)}&limit={lim}",
    ]
    for u in latest_urls:
        try:
            obj = _http_json(u)
            rows = _flatten_payload(obj)
            if rows:
                filtered = [r for r in rows if str(r.get('symbol') or r.get('ticker') or '').strip().upper() == _ticker(ticker)]
                if filtered:
                    return filtered[:lim]
        except Exception:
            continue
    return []


def _provider_items_finnhub(ticker: str, lookback_years: int) -> list[dict[str, Any]]:
    key = finnhub_key()
    if not key:
        return []
    tk = urllib.parse.quote(_ticker(ticker))
    out: list[dict[str, Any]] = []
    this_year = dt.date.today().year
    min_year = max(2000, this_year - max(1, int(lookback_years)))
    # Finnhub transcript API is typically year/quarter based.
    for y in range(this_year, min_year - 1, -1):
        for q in (4, 3, 2, 1):
            u = (
                "https://finnhub.io/api/v1/stock/transcripts?"
                f"symbol={tk}&year={y}&quarter={q}&token={urllib.parse.quote(key)}"
            )
            try:
                obj = _http_json(u)
                rows = _flatten_payload(obj)
                if rows:
                    out.extend(rows)
            except Exception:
                continue
    return out


def _fallback_provider_rows(ticker: str, *, lookback_years: int, limit: int) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    providers = [
        ("fmp_api", _provider_items_fmp(ticker, limit)),
        ("finnhub_api", _provider_items_finnhub(ticker, lookback_years)),
    ]
    for src, rows in providers:
        for r in rows:
            n = _normalize_ext_item(r)
            if not n:
                continue
            out.append((src, n))
    return out


def refresh_earnings_transcripts_from_sec(
    ticker: str,
    *,
    max_filings: int = 400,
    lookback_years: int = 10,
) -> dict[str, int | str]:
    tk = _ticker(ticker)
    if not tk:
        return {"ok": 0, "error": "ticker_required", "scanned": 0, "saved": 0}
    con = pg_connect()
    if con is None:
        return {"ok": 0, "error": "postgres_unavailable", "scanned": 0, "saved": 0}
    scanned = 0
    saved = 0
    skipped = 0
    removed = 0
    try:
        cur = con.cursor()
        cutoff = (dt.date.today() - dt.timedelta(days=max(365, int(lookback_years) * 365))).isoformat()
        cur.execute(
            """
            SELECT id, form, date, accession, doc_url, path
            FROM filings_core
            WHERE ticker=%s AND form = ANY(%s) AND COALESCE(date,'') >= %s
            ORDER BY date DESC, id DESC
            LIMIT %s
            """,
            (tk, list(_FORMS), cutoff, max(20, min(2000, int(max_filings)))),
        )
        rows = cur.fetchall() or []
    except Exception:
        con.close()
        return {"ok": 0, "error": "filings_query_failed", "scanned": 0, "saved": 0}
    finally:
        try:
            con.close()
        except Exception:
            pass

    for r in rows:
        scanned += 1
        filing_id = int(r[0] or 0)
        form = str(r[1] or "").strip().upper()
        date_s = str(r[2] or "").strip()[:10]
        accession = str(r[3] or "").strip()
        doc_url = str(r[4] or "").strip()
        path_s = str(r[5] or "").strip()
        p = resolve_filing_path(path_s)
        if p is None:
            skipped += 1
            continue
        try:
            raw = read_filing_text(p, strip_html=True)
        except Exception:
            skipped += 1
            continue
        payload = _extract_payload(raw)
        if not payload:
            skipped += 1
            continue
        fy, fq = _fiscal_quarter(date_s)
        ok = upsert_earnings_transcript_pg(
            ticker=tk,
            call_date=date_s,
            fiscal_year=fy,
            fiscal_quarter=fq,
            title=str(payload.get("title") or f"{form} earnings transcript"),
            source_type="sec_filing",
            source_url=(doc_url or f"/filing?path={str(p)}"),
            filing_id=filing_id,
            accession=accession,
            excerpt=str(payload.get("excerpt") or ""),
            transcript_text=str(payload.get("transcript_text") or ""),
            char_count=int(payload.get("char_count") or 0),
            quality_score=float(payload.get("quality_score") or 0.0),
            is_partial=bool(payload.get("is_partial")),
        )
        if ok:
            saved += 1
    fallback_saved = 0
    fallback_seen: set[tuple[str, str, str]] = set()
    for src, n in _fallback_provider_rows(tk, lookback_years=lookback_years, limit=max_filings):
        cdate = str(n.get("call_date") or "").strip()
        title = str(n.get("title") or "").strip().lower()[:180]
        if not cdate or not title:
            continue
        key = (src, cdate, title)
        if key in fallback_seen:
            continue
        fallback_seen.add(key)
        ok = upsert_earnings_transcript_pg(
            ticker=tk,
            call_date=cdate,
            fiscal_year=n.get("fiscal_year"),
            fiscal_quarter=str(n.get("fiscal_quarter") or ""),
            title=str(n.get("title") or "Earnings Call Transcript"),
            source_type=src,
            source_url=str(n.get("source_url") or ""),
            filing_id=0,
            accession="",
            excerpt=str(n.get("excerpt") or ""),
            transcript_text=str(n.get("transcript_text") or ""),
            char_count=int(n.get("char_count") or 0),
            quality_score=float(n.get("quality_score") or 0.0),
            is_partial=bool(n.get("is_partial")),
        )
        if ok:
            fallback_saved += 1
    # Cleanup existing false positives that slipped in from older parser versions.
    current_rows = list_earnings_transcripts_pg(tk, limit=300)
    bad_ids: list[int] = []
    for r in current_rows:
        txt = str((r or {}).get("transcript_text") or "").lower()
        ex = str((r or {}).get("excerpt") or "").lower()
        ti = str((r or {}).get("title") or "").lower()
        low = " ".join([ti, ex, txt[:4000]])
        if _looks_like_sec_boilerplate(low) or (not _is_transcript_like(low)) or (not _is_earnings_context(low)):
            bad_ids.append(int(r.get("id") or 0))
    if bad_ids:
        removed = delete_earnings_transcripts_by_ids_pg(bad_ids)

    return {
        "ok": 1,
        "error": "",
        "scanned": scanned,
        "saved": saved,
        "skipped": skipped,
        "fallback_saved": fallback_saved,
        "removed_bad": removed,
    }


def list_earnings_transcripts(ticker: str, limit: int = 24) -> list[dict[str, Any]]:
    return list_earnings_transcripts_pg(_ticker(ticker), limit=max(1, min(120, int(limit))))


def list_sec_earnings_releases(
    ticker: str,
    *,
    limit: int = 12,
    lookback_years: int = 10,
    max_filings: int = 260,
) -> list[dict[str, Any]]:
    tk = _ticker(ticker)
    if not tk:
        return []
    con = pg_connect()
    if con is None:
        return []
    out: list[dict[str, Any]] = []
    try:
        cutoff = (dt.date.today() - dt.timedelta(days=max(365, int(lookback_years) * 365))).isoformat()
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, form, date, accession, doc_url, path
            FROM filings_core
            WHERE ticker=%s AND form = ANY(%s) AND COALESCE(date,'') >= %s
            ORDER BY date DESC, id DESC
            LIMIT %s
            """,
            (tk, list(_FORMS), cutoff, max(20, min(1200, int(max_filings)))),
        )
        rows = cur.fetchall() or []
    except Exception:
        rows = []
    finally:
        try:
            con.close()
        except Exception:
            pass
    seen: set[str] = set()
    for r in rows:
        if len(out) >= max(1, min(50, int(limit))):
            break
        filing_id = int(r[0] or 0)
        form = str(r[1] or "").strip().upper()
        date_s = str(r[2] or "").strip()[:10]
        accession = str(r[3] or "").strip()
        doc_url = str(r[4] or "").strip()
        p = resolve_filing_path(str(r[5] or "").strip())
        if p is None:
            continue
        try:
            raw = read_filing_text(p, strip_html=True)
        except Exception:
            continue
        payload = _extract_release_payload(raw)
        if not payload:
            continue
        key = f"{date_s}|{form}|{str(payload.get('excerpt') or '')[:100].lower()}"
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "filing_id": filing_id,
                "form": form,
                "call_date": date_s,
                "title": f"{form} Earnings Release",
                "source_type": "sec_filing",
                "source_url": (doc_url or f"/filing?path={str(p)}"),
                "accession": accession,
                "excerpt": str(payload.get("excerpt") or ""),
                "char_count": int(payload.get("char_count") or 0),
            }
        )
    return out


def list_quarterly_result_signals(ticker: str, limit: int = 8) -> list[dict[str, str]]:
    tk = _ticker(ticker)
    if not tk:
        return []
    rows = query_report_facts_pg(query="", tickers=[tk], limit=80, official_only=True)
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for r in rows:
        if str((r or {}).get("ticker") or "").strip().upper() != tk:
            continue
        src = str((r or {}).get("report_name") or "").strip()
        src_up = src.upper()
        # Keep only SEC-ingest facts for this panel; skip deep-dive memo noise.
        if not src_up.startswith("SEC:"):
            continue
        txt = str((r or {}).get("fact_text") or "").strip()
        if not txt:
            continue
        low = txt.lower()
        if not re.search(r"\b(earnings|guidance|margin|revenue|eps|cash flow|10-q|10-k|8-k|results?)\b", low):
            continue
        key = " ".join(low.split())[:200]
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "date": str((r or {}).get("fact_date") or ""),
                "text": txt[:220],
                "source": src[:120] or "Official Reports",
            }
        )
        if len(out) >= max(1, min(30, int(limit))):
            break
    return out


# ---------------------------------------------------------------------------
# Earnings Analysis — LLM layer
# ---------------------------------------------------------------------------

def ensure_earnings_analysis_schema() -> None:
    """Create earnings_analysis_core table."""
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS earnings_analysis_core (
                id BIGINT PRIMARY KEY,
                ticker TEXT NOT NULL,
                filing_date TEXT NOT NULL DEFAULT '',
                form TEXT NOT NULL DEFAULT '',
                fiscal_quarter TEXT NOT NULL DEFAULT '',
                transcript_id BIGINT NOT NULL DEFAULT 0,
                guidance_direction TEXT NOT NULL DEFAULT '',
                tone TEXT NOT NULL DEFAULT '',
                eps_vs_prior TEXT NOT NULL DEFAULT '',
                revenue_actual TEXT NOT NULL DEFAULT '',
                margin_commentary TEXT NOT NULL DEFAULT '',
                key_signals JSONB NOT NULL DEFAULT '[]',
                deflected_topics TEXT NOT NULL DEFAULT '',
                vs_prior_quarter TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                analyzed_at TEXT NOT NULL DEFAULT '',
                quality_score REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_earnings_analysis_ticker ON earnings_analysis_core(ticker)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_earnings_analysis_date ON earnings_analysis_core(filing_date DESC)"
        )
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


def analyze_earnings_transcript(
    ticker: str,
    text: str,
    filing_date: str,
    form: str,
    fiscal_quarter: str = "",
    transcript_id: int = 0,
    quality_score: float = 0.0,
) -> dict[str, Any]:
    """
    Run LLM analysis on earnings text (transcript or release).
    Extracts: guidance direction, tone, EPS vs prior, margin commentary,
    key signals, deflected topics, vs-prior-quarter narrative change.
    Stores result in earnings_analysis_core. Returns the analysis dict.
    """
    if ask_ai is None:
        return {"ok": False, "error": "llm_unavailable"}
    tk = _ticker(ticker)
    if not tk or not text:
        return {"ok": False, "error": "missing_input"}

    # Load prior quarter summary for comparison (last 1 analysis)
    prior_summary = ""
    con = pg_connect()
    if con is not None:
        try:
            cur = con.cursor()
            cur.execute(
                """SELECT summary, filing_date FROM earnings_analysis_core
                   WHERE ticker=%s AND filing_date < %s
                   ORDER BY filing_date DESC LIMIT 1""",
                (tk, str(filing_date or "9999")),
            )
            row = cur.fetchone()
            if row:
                prior_summary = f"Prior quarter ({row[1]}): {str(row[0] or '')[:400]}"
        except Exception:
            pass
        finally:
            con.close()

    prompt = (
        f"You are analyzing an earnings document for {tk} ({form}, {filing_date}).\n"
        f"Extract structured intelligence from this earnings text.\n\n"
        f"EARNINGS TEXT:\n{text[:8000]}\n\n"
        f"{('PRIOR QUARTER CONTEXT:\n' + prior_summary + chr(10) + chr(10)) if prior_summary else ''}"
        "Return strict JSON:\n"
        "{\n"
        '  "guidance_direction": "raised|maintained|lowered|withdrawn|none",\n'
        '  "tone": "confident|neutral|cautious|defensive",\n'
        '  "eps_vs_prior": "e.g. EPS $2.40 vs $2.18 prior quarter (+10%) or N/A",\n'
        '  "revenue_actual": "e.g. Revenue $94.9B vs $89.5B prior (+6.0%) or N/A",\n'
        '  "margin_commentary": "specific gross/operating margin numbers and trend",\n'
        '  "key_signals": ["signal 1 with number", "signal 2 with number", "signal 3"],\n'
        '  "deflected_topics": "topics management avoided or gave vague answers on, or N/A",\n'
        '  "vs_prior_quarter": "how the narrative/tone/guidance changed vs prior quarter, or N/A",\n'
        '  "summary": "2-sentence summary of the most important takeaway with specific numbers"\n'
        "}\n\n"
        "Rules:\n"
        "- Every field must have specific numbers where available — never say 'strong results'\n"
        "- key_signals: exactly 3 bullets, each citing a specific metric\n"
        "- If a field is not determinable from the text, use 'N/A'"
    )
    try:
        raw = str(ask_ai(
            prompt,
            f"Earnings analyst for {tk}. JSON only.",
            mode="smart", json_mode=True, temperature=0.1,
        ) or "").strip()
        if not raw:
            return {"ok": False, "error": "empty_llm_response"}
        parsed = json.loads(raw)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}

    now = dt.datetime.now().isoformat()
    con = pg_connect()
    if con is None:
        return {"ok": False, "error": "pg_unavailable"}
    try:
        cur = con.cursor()
        cur.execute("SELECT COALESCE(MAX(id),0)+1 FROM earnings_analysis_core")
        new_id = int((cur.fetchone() or [1])[0] or 1)
        cur.execute(
            """
            INSERT INTO earnings_analysis_core
              (id, ticker, filing_date, form, fiscal_quarter, transcript_id,
               guidance_direction, tone, eps_vs_prior, revenue_actual, margin_commentary,
               key_signals, deflected_topics, vs_prior_quarter, summary, analyzed_at, quality_score)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                new_id, tk, str(filing_date or "")[:10], str(form or "")[:16],
                str(fiscal_quarter or "")[:8], int(transcript_id or 0),
                str(parsed.get("guidance_direction") or "")[:30],
                str(parsed.get("tone") or "")[:20],
                str(parsed.get("eps_vs_prior") or "")[:200],
                str(parsed.get("revenue_actual") or "")[:200],
                str(parsed.get("margin_commentary") or "")[:500],
                json.dumps(list(parsed.get("key_signals") or []), ensure_ascii=True),
                str(parsed.get("deflected_topics") or "")[:400],
                str(parsed.get("vs_prior_quarter") or "")[:400],
                str(parsed.get("summary") or "")[:800],
                now,
                float(quality_score or 0.0),
            ),
        )
        con.commit()
        return {"ok": True, "id": new_id, "ticker": tk, **parsed}
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        con.close()


def analyze_latest_earnings_for_ticker(ticker: str) -> dict[str, Any]:
    """
    Called automatically after a new 8-K/10-Q is ingested.
    1. Refreshes earnings transcripts from SEC filings for this ticker.
    2. Finds any transcripts not yet analyzed.
    3. Runs LLM analysis on each new one.
    Returns summary of work done.
    """
    tk = _ticker(ticker)
    if not tk:
        return {"ok": False, "error": "invalid_ticker"}

    # Step 1: refresh transcript extraction from SEC filings
    try:
        refresh_earnings_transcripts_from_sec(tk, max_filings=50, lookback_years=2)
    except Exception:
        pass

    # Step 2: find transcripts not yet analyzed
    con = pg_connect()
    if con is None:
        return {"ok": False, "error": "pg_unavailable"}
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT et.id, et.ticker, et.call_date, et.form, et.fiscal_quarter,
                   et.transcript_text, et.quality_score
            FROM earnings_transcripts_core et
            WHERE et.ticker = %s
              AND et.quality_score >= 0.4
              AND NOT EXISTS (
                SELECT 1 FROM earnings_analysis_core ea
                WHERE ea.transcript_id = et.id
              )
            ORDER BY et.call_date DESC
            LIMIT 3
            """,
            (tk,),
        )
        rows = cur.fetchall() or []
        cols = [d[0] for d in cur.description] if cur.description else []
    except Exception as exc:
        con.close()
        return {"ok": False, "error": str(exc)[:200]}
    finally:
        con.close()

    analyzed = 0
    for row in rows:
        r = dict(zip(cols, row))
        text = str(r.get("transcript_text") or "").strip()
        if not text:
            continue
        result = analyze_earnings_transcript(
            ticker=tk,
            text=text,
            filing_date=str(r.get("call_date") or ""),
            form=str(r.get("form") or "8-K"),
            fiscal_quarter=str(r.get("fiscal_quarter") or ""),
            transcript_id=int(r.get("id") or 0),
            quality_score=float(r.get("quality_score") or 0.0),
        )
        if result.get("ok"):
            analyzed += 1

    return {"ok": True, "ticker": tk, "analyzed": analyzed}


def list_earnings_analysis(ticker: str = "", limit: int = 10) -> list[dict[str, Any]]:
    """List recent earnings analyses for dashboard display."""
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        if ticker:
            tk = _ticker(ticker)
            cur.execute(
                """SELECT id, ticker, filing_date, form, fiscal_quarter,
                          guidance_direction, tone, eps_vs_prior, revenue_actual,
                          margin_commentary, key_signals, deflected_topics,
                          vs_prior_quarter, summary, analyzed_at, quality_score
                   FROM earnings_analysis_core
                   WHERE ticker=%s
                   ORDER BY filing_date DESC, id DESC LIMIT %s""",
                (tk, max(1, min(50, int(limit)))),
            )
        else:
            cur.execute(
                """SELECT id, ticker, filing_date, form, fiscal_quarter,
                          guidance_direction, tone, eps_vs_prior, revenue_actual,
                          margin_commentary, key_signals, deflected_topics,
                          vs_prior_quarter, summary, analyzed_at, quality_score
                   FROM earnings_analysis_core
                   ORDER BY filing_date DESC, id DESC LIMIT %s""",
                (max(1, min(50, int(limit))),),
            )
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall() or []
        out = []
        for row in rows:
            r = dict(zip(cols, row))
            # Parse key_signals JSONB
            ks = r.get("key_signals")
            if isinstance(ks, str):
                try:
                    r["key_signals"] = json.loads(ks)
                except Exception:
                    r["key_signals"] = []
            elif ks is None:
                r["key_signals"] = []
            out.append(r)
        return out
    except Exception:
        return []
    finally:
        con.close()
