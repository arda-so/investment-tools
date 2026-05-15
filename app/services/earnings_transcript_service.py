from __future__ import annotations

import datetime as dt
import json
import os
import re
from typing import Any

from app.core.filing_text import read_filing_text_any
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


# For "earnings release from SEC filings", keep this strict to release-style filings.
# 10-Q/10-K signals are handled separately via quarterly facts.
_RELEASE_FORMS = {"8-K", "6-K"}
_EARNINGS_MAX_CHARS = int(os.getenv("EARNINGS_ANALYSIS_MAX_CHARS", "12000"))
_EARNINGS_OPENING_RATIO = float(os.getenv("EARNINGS_OPENING_RATIO", "0.4"))


def _ticker(raw: str) -> str:
    s = re.sub(r"[^A-Z0-9.\-]", "", str(raw or "").strip().upper())
    return s[:16]


def _compress_earnings_for_analysis(text: str, max_chars: int = _EARNINGS_MAX_CHARS) -> str:
    s = str(text or "").strip()
    if not s:
        return ""
    if len(s) <= max_chars:
        return s
    opening_budget = max(1000, int(max_chars * _EARNINGS_OPENING_RATIO))
    qa_budget = max(1000, max_chars - opening_budget)
    low = s.lower()
    qa_match = re.search(r"(question-and-answer|question and answer|q&a|operator:)", low)
    if not qa_match:
        return s[:max_chars]
    qa_start = max(0, int(qa_match.start()))
    opening = s[:opening_budget]
    qa = s[qa_start: qa_start + qa_budget]
    return (opening + "\n\n[...]\n\n" + qa)[:max_chars]


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
        # Press release markers
        "financial results",
        "revenue",
        "fourth quarter",
        "third quarter",
        "q4 ",
        "q3 ",
    ]
    if not any(k in low for k in key_hits):
        return None
    # Accept either a call transcript OR an earnings press release.
    is_transcript = _is_transcript_like(low)
    is_press_release = _is_press_release_like(low)
    if not (is_transcript or is_press_release):
        return None
    if not _is_earnings_context(low):
        return None
    # Quality score: transcripts score higher than press releases.
    if is_transcript:
        score = 0.35
        score += 0.1 if "earnings call" in low or "conference call" in low else 0.0
        score += 0.1 if "prepared remarks" in low else 0.0
        score += 0.1 if "question-and-answer" in low or "question and answer" in low else 0.0
        score += 0.1 if "operator:" in low else 0.0
        score += 0.05 if len(s) > 8000 else 0.0
    else:
        # Press release
        score = 0.45
        score += 0.05 if len(s) > 8000 else 0.0
    score = min(0.95, score)

    if is_press_release and not is_transcript:
        # For press releases, anchor on the first financial highlight (revenue/income figure)
        # rather than "conference call" which hits the non-GAAP boilerplate disclaimer.
        pr_anchor = re.search(
            r"(total\s+revenue|net\s+revenue|operating\s+revenue|revenue\s+(?:was|of|grew|increased|decreased)"
            r"|net\s+income|operating\s+income|diluted\s+(?:eps|earnings)"
            r"|\$\s*\d[\d,.]+\s*(?:million|billion)"
            r"|\d+(?:\.\d+)?%\s+(?:year-over-year|yoy|growth|increase|decrease))",
            low,
        )
        i = pr_anchor.start() if pr_anchor else 0
        # Fall back to first paragraph that mentions quarter results
        if i == 0:
            qtr_m = re.search(r"(fourth quarter|third quarter|second quarter|first quarter|full[ -]year)", low)
            i = qtr_m.start() if qtr_m else 0
    else:
        m = re.search(r"(earnings call|conference call|prepared remarks|question(?:-and-|\s+and\s+)answer)", low)
        i = m.start() if m else 0
    start = max(0, i - 80)
    end = min(len(s), i + 600)
    excerpt = s[start:end].strip()
    title = "Earnings Press Release" if is_press_release and not is_transcript else "Earnings Call Transcript"
    if is_press_release and not is_transcript:
        # For press releases, capture the headline (typically first non-empty line with "Reports")
        headline_m = re.search(r"([A-Z][A-Za-z0-9& ,.\-']{5,120}(?:Reports?|Announces?|Results?)[A-Za-z0-9 ,.\-']{0,80})", s)
        if headline_m:
            title = headline_m.group(1).strip()[:140]
    else:
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


def _is_press_release_like(low: str) -> bool:
    """True for earnings press release content (EX-99.1 style).

    Press releases use different markers than call transcripts — they report
    quarterly results, revenue, guidance, EPS, etc. but don't have 'operator:' or
    'prepared remarks'.
    """
    s = str(low or "")
    hits = 0
    if "financial results" in s:
        hits += 1
    if "revenue" in s:
        hits += 1
    if re.search(r"\b(fourth quarter|third quarter|second quarter|first quarter|q4|q3|q2|q1)\b", s):
        hits += 1
    if "full year" in s or "fiscal year" in s:
        hits += 1
    if "guidance" in s or "outlook" in s:
        hits += 1
    if "earnings per share" in s or "diluted eps" in s or "net income" in s:
        hits += 1
    return hits >= 3


def _is_earnings_context(low: str) -> bool:
    s = str(low or "")
    deny = (
        "merger agreement",
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
    # Press release additional signals
    if "fourth quarter" in s or "third quarter" in s or "second quarter" in s or "first quarter" in s:
        hits += 1
    if "full year" in s:
        hits += 1
    return hits >= 2


def _fallback_provider_rows(ticker: str, *, lookback_years: int, limit: int) -> list[tuple[str, dict[str, Any]]]:
    _ = (ticker, lookback_years, limit)
    # Paid vendor fallback is intentionally disabled.
    return []


def _fetch_edgar_press_release(ticker: str, accession: str) -> str:
    """Fetch the earnings press release (EX-99.1) for an 8-K via edgartools.

    Returns the press release text, or empty string on failure.
    SEC rate limit: ~10 req/s; this function is only called when local content is empty.
    """
    try:
        import edgar as _edgar  # noqa: PLC0415
        _edgar.set_identity("InvestorOS arda.solmaz@pileainvest.com")
        company = _edgar.Company(str(ticker or "").upper())
        target_acc = str(accession or "").replace("-", "").replace("_", "").lower()
        # Use .head(20) to avoid pyarrow iteration issues with newer versions.
        batch = company.get_filings(form="8-K").head(20)
        for idx in range(len(batch)):
            try:
                f = batch[idx]
            except Exception:
                continue
            acc = str(getattr(f, "accession_no", "") or "").replace("-", "").lower()
            if acc == target_acc:
                filing_obj = f.obj()
                prs = getattr(filing_obj, "press_releases", None)
                if prs:
                    raw = str(prs[0])
                    # Strip rich-text box-drawing characters from edgartools output.
                    raw = re.sub(r"[│╭╰─╮╯╴╶╷╸╹╺╻╼╽╾╿┃━╔╗╚╝╠╣╦╩╬]+", " ", raw)
                    return raw.strip()
                break
    except Exception:
        pass
    return ""


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
            (tk, list(_RELEASE_FORMS), cutoff, max(20, min(2000, int(max_filings)))),
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
        try:
            raw = read_filing_text_any(path_s, max_chars=220000)
        except Exception:
            raw = ""
        payload = _extract_payload(raw) if raw else None
        # If local content is empty, boilerplate, or fails the payload check,
        # fetch the earnings press release exhibit (EX-99.1) via edgartools.
        if not payload and accession:
            pr_raw = _fetch_edgar_press_release(tk, accession)
            if pr_raw:
                raw = pr_raw
                payload = _extract_payload(raw)
        if not raw or not payload:
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
            source_url=(doc_url or f"/filing?path={path_s}"),
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
        # Only remove rows with quality_score == 0.0 (old false positives with no score).
        # Do NOT re-analyze text from excerpt alone — list query omits transcript_text.
        score = float((r or {}).get("quality_score") or 0.0)
        ex = str((r or {}).get("excerpt") or "").lower()
        if score == 0.0 and _looks_like_sec_boilerplate(ex):
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
            (tk, list(_RELEASE_FORMS), cutoff, max(20, min(1200, int(max_filings)))),
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
        path_s = str(r[5] or "").strip()
        try:
            raw = read_filing_text_any(path_s, max_chars=180000)
        except Exception:
            continue
        if not raw:
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
                "source_url": (doc_url or f"/filing?path={path_s}"),
                "path": path_s,
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
        f"EARNINGS TEXT:\n{_compress_earnings_for_analysis(text)}\n\n"
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
