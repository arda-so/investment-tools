from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Any

from app.core.config import CORE_DB_PATH, ROOT
from app.core.sqlite_hardening import connect_sqlite, sqlite_retry
from app.services.organizer_service import add_general_note
from app.services.proactive_ai_service import ensure_proactive_schema, run_event_driven_monitor

ONYX_BRAIN_DB_PATH = ROOT / "onyx_brain.db"
try:
    from tools.llm_engine import ask_ai
except Exception:  # pragma: no cover
    ask_ai = None  # type: ignore[assignment]


def _conn_core() -> sqlite3.Connection:
    return connect_sqlite(str(CORE_DB_PATH), row_factory=True)


def _conn_onyx() -> sqlite3.Connection:
    return connect_sqlite(str(ONYX_BRAIN_DB_PATH), row_factory=True)


def ensure_sec_ingest_schema() -> None:
    def _write() -> None:
        con = _conn_core()
        try:
            con.execute(
                """CREATE TABLE IF NOT EXISTS filing_chunk_vectors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filing_id INTEGER NOT NULL,
                    ticker TEXT NOT NULL DEFAULT '',
                    form TEXT NOT NULL DEFAULT '',
                    chunk_index INTEGER NOT NULL,
                    chunk_text TEXT NOT NULL,
                    embedding_json TEXT NOT NULL DEFAULT '[]',
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    UNIQUE(filing_id, chunk_index) ON CONFLICT REPLACE
                )"""
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_fcv_filing ON filing_chunk_vectors(filing_id)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_fcv_ticker ON filing_chunk_vectors(ticker)")
            con.commit()
        finally:
            con.close()
    sqlite_retry(_write)
    ensure_proactive_schema()


def _safe_ticker(raw: str) -> str:
    s = re.sub(r"[^A-Z0-9.\-]", "", str(raw or "").strip().upper())
    return s if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,11}", s) else ""


def _chunk_text(text: str, chunk_size: int = 1200, overlap: int = 200) -> list[str]:
    src = re.sub(r"\s+", " ", str(text or "")).strip()
    if not src:
        return []
    if len(src) <= chunk_size:
        return [src]
    out: list[str] = []
    step = max(200, int(chunk_size - overlap))
    n = len(src)
    i = 0
    while i < n:
        part = src[i : i + chunk_size].strip()
        if part:
            out.append(part)
        if i + chunk_size >= n:
            break
        i += step
    return out


def _chunk_structured_sections(sections: dict[str, str], chunk_size: int = 1200) -> list[str]:
    """
    Semantic + table-aware chunking:
    - preserve section boundaries
    - keep markdown table blocks coherent
    - split on paragraph boundaries first, then fallback to fixed chunking
    """
    out: list[str] = []
    for sec, raw in list((sections or {}).items()):
        txt = str(raw or "").strip()
        if not txt:
            continue
        header = f"[SECTION:{str(sec).upper()}]"
        lines = [str(x or "").rstrip() for x in txt.splitlines()]
        # build blocks, preserving table blocks together
        blocks: list[str] = []
        cur: list[str] = []
        in_table = False
        for ln in lines:
            s = ln.strip()
            if not s:
                if cur:
                    blocks.append("\n".join(cur).strip())
                    cur = []
                in_table = False
                continue
            looks_table = s.startswith("|") and s.endswith("|")
            if looks_table and not in_table and cur:
                blocks.append("\n".join(cur).strip())
                cur = []
            in_table = looks_table
            cur.append(s)
        if cur:
            blocks.append("\n".join(cur).strip())

        buff = header
        for b in blocks:
            cand = (buff + "\n" + b).strip()
            if len(cand) <= chunk_size:
                buff = cand
                continue
            if len(buff) > len(header):
                out.append(buff)
            if len(b) <= chunk_size:
                buff = (header + "\n" + b).strip()
            else:
                # fallback for oversized table/paragraph
                parts = _chunk_text(b, chunk_size=max(700, chunk_size - 120), overlap=120)
                for p in parts:
                    out.append((header + "\n" + p).strip())
                buff = header
        if len(buff) > len(header):
            out.append(buff)
    return out


def _hash_embedding(text: str, dims: int = 48) -> list[float]:
    """
    Lightweight deterministic vectorizer for local SQLite storage.
    No external model dependency.
    """
    vec = [0.0] * max(8, int(dims))
    toks = re.findall(r"[a-zA-Z0-9]{3,}", str(text or "").lower())
    if not toks:
        return vec
    for tk in toks:
        h = hashlib.sha256(tk.encode("utf-8", errors="ignore")).hexdigest()
        idx = int(h[:8], 16) % len(vec)
        sign = -1.0 if (int(h[8:10], 16) % 2) else 1.0
        vec[idx] += sign * (1.0 + (len(tk) / 10.0))
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return [round(v, 6) for v in vec]


def _strip_html(raw: str) -> str:
    s = str(raw or "")
    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(s, "html.parser")
        for bad in soup(["script", "style", "noscript"]):
            bad.extract()
        txt = soup.get_text(" ", strip=True)
    except Exception:
        txt = re.sub(r"<[^>]+>", " ", s)
    txt = re.sub(r"&nbsp;|&#160;", " ", txt, flags=re.I)
    txt = re.sub(r"&amp;", "&", txt, flags=re.I)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def _html_to_structured_markdown(raw: str) -> str:
    s = str(raw or "")
    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(s, "html.parser")
        for bad in soup(["script", "style", "noscript"]):
            bad.extract()
        out: list[str] = []
        for node in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "table"]):
            nm = str(getattr(node, "name", "") or "").lower()
            txt = node.get_text(" ", strip=True)
            if not txt:
                continue
            if nm in {"h1", "h2", "h3", "h4"}:
                lvl = {"h1": "#", "h2": "##", "h3": "###", "h4": "####"}.get(nm, "##")
                out.append(f"{lvl} {txt}")
                continue
            if nm == "table":
                rows = node.find_all("tr")
                md_rows: list[str] = []
                for tr in rows:
                    cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                    cells = [re.sub(r"\s+", " ", str(c or "").strip()) for c in cells if str(c or "").strip()]
                    if not cells:
                        continue
                    md_rows.append("| " + " | ".join(cells) + " |")
                if md_rows:
                    out.append(md_rows[0])
                    if len(md_rows[0].split("|")) > 3:
                        cols = max(1, len(md_rows[0].split("|")) - 2)
                        out.append("| " + " | ".join(["---"] * cols) + " |")
                    out.extend(md_rows[1:])
                continue
            if nm == "li":
                out.append(f"- {txt}")
            else:
                out.append(txt)
        merged = "\n".join(out).strip()
        if merged:
            return merged
    except Exception:
        pass
    return _strip_html(s)


def _extract_between(text: str, start_patterns: list[str], end_patterns: list[str], max_chars: int = 12000) -> str:
    src = str(text or "")
    low = src.lower()
    start_idx = -1
    for p in start_patterns:
        m = re.search(p, low, flags=re.I)
        if m:
            start_idx = m.start()
            break
    if start_idx < 0:
        return ""
    end_idx = len(src)
    segment_low = low[start_idx + 1 :]
    for p in end_patterns:
        m2 = re.search(p, segment_low, flags=re.I)
        if m2:
            cand = start_idx + 1 + m2.start()
            if cand > start_idx and cand < end_idx:
                end_idx = cand
    out = src[start_idx:end_idx].strip()
    return out[: max(800, int(max_chars or 12000))]


def _extract_target_sections(form: str, raw_text: str) -> dict[str, str]:
    """
    Buffett-style selective extraction:
    10-K/10-Q: Business, Risk Factors, MD&A, Notes + Segment info.
    DEF 14A: Executive Compensation + Related Party Transactions.
    """
    txt = _html_to_structured_markdown(raw_text)
    fm = str(form or "").upper()
    sections: dict[str, str] = {}
    if fm in {"10-K", "20-F", "40-F", "10-Q", "6-K"}:
        # Business
        sections["business"] = _extract_between(
            txt,
            [r"\bitem\s*1[\.\:\-\s]+business\b", r"\bbusiness overview\b"],
            [r"\bitem\s*1a\b", r"\bitem\s*2\b", r"\bitem\s*7\b", r"\bmanagement.?s discussion\b"],
            max_chars=10000,
        )
        # Risk factors
        sections["risk_factors"] = _extract_between(
            txt,
            [r"\bitem\s*1a[\.\:\-\s]+risk factors?\b", r"\brisk factors?\b"],
            [r"\bitem\s*1b\b", r"\bitem\s*2\b", r"\bitem\s*7\b", r"\bmanagement.?s discussion\b"],
            max_chars=14000,
        )
        # MD&A (10-Q is typically Item 2)
        sections["mda"] = _extract_between(
            txt,
            [r"\bitem\s*7[\.\:\-\s]+management.?s discussion\b", r"\bmanagement.?s discussion and analysis\b", r"\bitem\s*2[\.\:\-\s]+management.?s discussion\b"],
            [r"\bitem\s*7a\b", r"\bitem\s*8\b", r"\bfinancial statements\b", r"\bitem\s*3\b", r"\bcontrols and procedures\b"],
            max_chars=18000,
        )
        # Notes to financial statements (Item 8 + segment info)
        notes = _extract_between(
            txt,
            [r"\bitem\s*8[\.\:\-\s]+financial statements", r"\bnotes to (?:the )?financial statements?\b"],
            [r"\bitem\s*9\b", r"\bitem\s*9a\b", r"\bcontrols and procedures\b"],
            max_chars=22000,
        )
        seg = _extract_between(
            txt,
            [r"\bsegment reporting\b", r"\bsegment information\b", r"\basc\s*280\b"],
            [r"\bnote\s+\d+\b", r"\bitem\s*9\b", r"\bcontrols and procedures\b"],
            max_chars=12000,
        )
        sections["notes"] = notes
        sections["segment_info"] = seg
    elif fm in {"DEF 14A", "DEF14A"}:
        sections["executive_compensation"] = _extract_between(
            txt,
            [r"\bexecutive compensation\b", r"\bsummary compensation table\b", r"\bcompensation discussion\b"],
            [r"\bsecurity ownership\b", r"\brelated party transactions?\b", r"\baudit committee\b"],
            max_chars=18000,
        )
        sections["related_party_transactions"] = _extract_between(
            txt,
            [r"\brelated party transactions?\b", r"\bcertain relationships and related transactions\b"],
            [r"\bproposal\b", r"\bsecurity ownership\b", r"\baudit committee\b", r"\bcompensation committee\b"],
            max_chars=12000,
        )
    return {k: v for k, v in sections.items() if str(v or "").strip()}


def _entity_id(con: sqlite3.Connection, name: str, entity_type: str) -> int:
    nm = str(name or "").strip()
    et = str(entity_type or "").strip().upper()
    if not nm or not et:
        return 0
    norm = re.sub(r"\s+", " ", nm.lower())
    id_text = f"{et.lower()}:{norm}"
    now = dt.datetime.now().isoformat()
    # Backfill legacy rows that may still have empty id_text.
    try:
        con.execute(
            "UPDATE entities SET id_text = lower(type) || ':' || normalized_name "
            "WHERE COALESCE(id_text,'') = ''"
        )
    except Exception:
        pass
    con.execute(
        "INSERT INTO entities(name, type, entity_type, metadata, normalized_name, id_text, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(type, normalized_name) DO UPDATE SET name=excluded.name, entity_type=excluded.entity_type, id_text=excluded.id_text, updated_at=excluded.updated_at",
        (nm, et, et, "{}", norm, id_text, now, now),
    )
    row = con.execute("SELECT id FROM entities WHERE type=? AND normalized_name=? LIMIT 1", (et, norm)).fetchone()
    return int(row["id"] or 0) if row else 0


def _link(
    con: sqlite3.Connection,
    source_id: int,
    target_id: int,
    rel_type: str,
    citation_url: str,
    citation_text: str,
    conf: float = 0.72,
) -> None:
    if source_id <= 0 or target_id <= 0:
        return
    con.execute(
        """INSERT OR IGNORE INTO relationships
           (source_id, target_id, relationship_type, citation_link, citation_url, citation_text, confidence, confidence_score, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            int(source_id),
            int(target_id),
            str(rel_type or "").strip().upper(),
            str(citation_url or "").strip(),
            str(citation_url or "").strip(),
            str(citation_text or "")[:500],
            float(conf or 0.0),
            float(conf or 0.0),
            dt.datetime.now().isoformat(),
        ),
    )


def _extract_risk_themes(text: str) -> list[str]:
    low = str(text or "").lower()
    out: list[str] = []
    keys = {
        "RATE_SENSITIVITY": ("rate", "interest"),
        "REGULATORY_RISK": ("regulat", "antitrust", "compliance", "legal", "litigation"),
        "DEMAND_RISK": ("demand", "churn", "retention", "slowdown"),
        "MARGIN_PRESSURE": ("margin", "cost", "pricing pressure", "inflation"),
        "LIQUIDITY_BALANCE_SHEET": ("debt", "liquidity", "cash flow", "refinanc"),
        "SUPPLY_CHAIN_RISK": ("supply", "supplier", "shortage", "logistics"),
    }
    for theme, words in keys.items():
        if any(w in low for w in words):
            out.append(theme)
    return out[:6]


def _extract_peer_tickers(text: str, self_ticker: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for m in re.findall(r"\b[A-Z]{2,5}\b", str(text or "").upper()):
        tk = _safe_ticker(m)
        if not tk or tk == self_ticker or tk in seen:
            continue
        seen.add(tk)
        out.append(tk)
        if len(out) >= 8:
            break
    return out


def _reflect_relationship_candidates(
    ticker: str,
    candidates: list[dict[str, Any]],
    evidence_text: str,
) -> list[dict[str, Any]]:
    """
    Iterative reflection pass:
    - keep only allowed relationship types
    - optional LLM critique/correction before commit
    """
    allowed = {"EXPOSED_TO", "COMPETES_WITH", "SUPPLIER_TO", "CUSTOMER_OF", "SIGNALS_MACRO"}
    normed: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for c in list(candidates or []):
        src = _safe_ticker(str(c.get("source") or ticker))
        tgt = str(c.get("target") or "").strip()
        typ = str(c.get("relationship_type") or "").strip().upper()
        conf = float(c.get("confidence") or 0.7)
        if not src or not tgt or typ not in allowed:
            continue
        key = (src, tgt.upper(), typ)
        if key in seen:
            continue
        seen.add(key)
        normed.append(
            {
                "source": src,
                "target": tgt,
                "relationship_type": typ,
                "confidence": max(0.1, min(0.99, conf)),
                "evidence": str(c.get("evidence") or "")[:500],
            }
        )
    if not normed or ask_ai is None:
        return normed
    try:
        prompt = (
            "Critique and correct extracted relationship candidates for SEC filing knowledge graph.\n"
            "Allowed relationship_type: EXPOSED_TO, COMPETES_WITH, SUPPLIER_TO, CUSTOMER_OF, SIGNALS_MACRO.\n"
            "Do not invent new targets. Only keep or downgrade candidates based on evidence.\n"
            "Return strict JSON: {\"relationships\":[{\"source\":\"...\",\"target\":\"...\",\"relationship_type\":\"...\",\"confidence\":0.0,\"evidence\":\"...\"}]}\n"
            f"Ticker: {ticker}\n"
            f"Candidates: {json.dumps(normed, ensure_ascii=False)}\n"
            f"Evidence: {str(evidence_text or '')[:4000]}"
        )
        raw = str(
            ask_ai(
                prompt,
                "Ontology reflection agent. JSON only.",
                mode="smart",
                json_mode=True,
                temperature=0.0,
            )
            or ""
        ).strip()
        obj = json.loads(raw) if raw else {}
        rels = list((obj or {}).get("relationships") or [])
        reviewed: list[dict[str, Any]] = []
        allowed_targets = {str(x.get("target") or "").strip().upper() for x in normed}
        for r in rels:
            tgt = str((r or {}).get("target") or "").strip()
            typ = str((r or {}).get("relationship_type") or "").strip().upper()
            if not tgt or typ not in allowed or tgt.upper() not in allowed_targets:
                continue
            reviewed.append(
                {
                    "source": _safe_ticker(str((r or {}).get("source") or ticker)) or ticker,
                    "target": tgt,
                    "relationship_type": typ,
                    "confidence": max(0.1, min(0.99, float((r or {}).get("confidence") or 0.6))),
                    "evidence": str((r or {}).get("evidence") or "")[:500],
                }
            )
        return reviewed if reviewed else normed
    except Exception:
        return normed


def _maybe_write_thesis_alert_note(ticker: str, filing_id: int, form: str, analysis: dict[str, Any], citation_url: str) -> None:
    tk = _safe_ticker(ticker)
    if not tk or int(filing_id or 0) <= 0:
        return
    red = str((analysis or {}).get("red_flags") or "").strip()
    conf = int(float((analysis or {}).get("confidence_score") or 0))
    if not red:
        return
    red_low = red.lower()
    danger = any(k in red_low for k in {"liability", "debt", "going concern", "impairment", "restatement", "litigation", "weakness", "accounting change"})
    if (not danger) and conf < 55:
        return
    trace = f"sec_ingest:{int(filing_id)}:{tk}"
    # de-dup on trace_id
    con = _conn_core()
    try:
        row = con.execute("SELECT 1 FROM investor_notes WHERE trace_id=? LIMIT 1", (trace,)).fetchone()
        if row:
            return
    except Exception:
        pass
    finally:
        con.close()
    note = (
        f"SEC Thesis Monitor Alert [{tk} {form}]\n"
        f"Potential thesis break signal detected.\n"
        f"Red flags: {red[:700]}\n"
        f"Source: {citation_url}"
    )
    try:
        add_general_note(
            note,
            scope="ai_draft",
            ticker=tk,
            tags="ai,draft,sec,thesis,alert",
            status="pending",
            created_by="ai",
            ai_confidence=max(0.55, min(0.99, conf / 100.0 if conf > 0 else 0.68)),
            ai_reasoning="SEC filing ingestion found potential thesis break risk requiring review.",
            trace_id=trace,
        )
    except Exception:
        pass


def _extract_primary_doc_from_submission(raw: str, form: str = "") -> str:
    s = str(raw or "")
    if not s:
        return ""
    if "<DOCUMENT>" not in s.upper():
        return s
    form_norm = re.sub(r"\s+", "", str(form or "").upper())
    docs = re.findall(r"(?is)<DOCUMENT>(.*?)</DOCUMENT>", s)
    best = ""
    fallback = ""
    for d in docs:
        m_type = re.search(r"(?is)<TYPE>\s*([^\n\r<]+)", d)
        doc_type = str(m_type.group(1) if m_type else "").strip().upper()
        doc_type_norm = re.sub(r"\s+", "", doc_type)
        m_text = re.search(r"(?is)<TEXT>(.*)", d)
        body = str(m_text.group(1) if m_text else d).strip()
        if not body:
            continue
        if not fallback and doc_type in {"10-K", "10-Q", "DEF 14A", "DEF14A", "20-F", "40-F", "6-K"}:
            fallback = body
        if form_norm and (doc_type_norm == form_norm or doc_type.startswith(str(form or "").upper())):
            best = body
            break
    return best or fallback or s


def _read_filing_text(path_s: str, form: str = "") -> str:
    p = Path(str(path_s or "").strip())
    if not p.exists():
        return ""
    try:
        raw = p.read_text(encoding="utf-8", errors="ignore")
        return _extract_primary_doc_from_submission(raw, form=form)
    except Exception:
        return ""


def _scope_tickers() -> set[str]:
    out: set[str] = set()
    p = ROOT / "data" / "portfolio.csv"
    if p.exists():
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            t = _safe_ticker((ln.split(",", 1)[0] if "," in ln else ln).strip())
            if t:
                out.add(t)
    w = ROOT / "data" / "my_watchlist.txt"
    if w.exists():
        for ln in w.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = str(ln or "").strip()
            if not s or s.startswith("#"):
                continue
            t = _safe_ticker((s.split(",", 1)[0] if "," in s else s).strip())
            if t:
                out.add(t)
    con = _conn_core()
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS blue_chips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL UNIQUE,
                added_at TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT ''
            )"""
        )
        rows = con.execute("SELECT ticker FROM blue_chips").fetchall()
        for r in rows:
            t = _safe_ticker(str(r["ticker"] or ""))
            if t:
                out.add(t)
    except Exception:
        pass
    finally:
        con.close()
    return out


def _blue_chip_set() -> set[str]:
    out: set[str] = set()
    con = _conn_core()
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS blue_chips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL UNIQUE,
                added_at TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT ''
            )"""
        )
        rows = con.execute("SELECT ticker FROM blue_chips").fetchall()
        for r in rows:
            t = _safe_ticker(str(r["ticker"] or ""))
            if t:
                out.add(t)
    except Exception:
        pass
    finally:
        con.close()
    return out


def _recent_competitor_mda(ticker: str, limit: int = 6) -> list[dict[str, str]]:
    """
    Uses ontology graph to find competitors and then fetches recent MD&A-derived facts for cross-reference.
    """
    tk = _safe_ticker(ticker)
    if not tk:
        return []
    con_onyx = _conn_onyx()
    peer_tickers: list[str] = []
    try:
        row = con_onyx.execute(
            "SELECT id FROM entities WHERE type='COMPANY' AND normalized_name=? LIMIT 1",
            (tk.lower(),),
        ).fetchone()
        if not row:
            return []
        cid = int(row["id"] or 0)
        rels = con_onyx.execute(
            """SELECT e2.name
               FROM relationships r
               JOIN entities e2 ON e2.id = r.target_id
               WHERE r.source_id=? AND r.relationship_type IN ('COMPETES_WITH','PEER_OF')
               ORDER BY r.id DESC
               LIMIT 12""",
            (cid,),
        ).fetchall()
        for rr in rels:
            t = _safe_ticker(str(rr["name"] or ""))
            if t and t != tk:
                peer_tickers.append(t)
    finally:
        con_onyx.close()
    if not peer_tickers:
        return []
    con_core = _conn_core()
    out: list[dict[str, str]] = []
    try:
        marks = ",".join("?" for _ in peer_tickers)
        rows = con_core.execute(
            f"""SELECT ticker, fact_text, report_name, created_at
                FROM report_facts
                WHERE ticker IN ({marks}) AND source='sec_buffett'
                ORDER BY id DESC
                LIMIT ?""",
            tuple(peer_tickers + [max(2, int(limit or 6))]),
        ).fetchall()
        for r in rows:
            out.append(
                {
                    "ticker": str(r["ticker"] or "").upper(),
                    "fact_text": str(r["fact_text"] or ""),
                    "report_name": str(r["report_name"] or ""),
                    "created_at": str(r["created_at"] or ""),
                }
            )
    finally:
        con_core.close()
    return out


def _buffett_analyze(ticker: str, form: str, sections: dict[str, str], competitor_mda: list[dict[str, str]]) -> dict[str, Any]:
    questions = (
        "1. Capital Allocation: Is management buying back stock, paying down debt, or wasting money on bad acquisitions?\n"
        "2. Margin Trends: Are operating margins expanding or contracting compared to last year?\n"
        "3. Revenue Concentration: Based on Segment Reporting notes, what is the exact revenue breakdown by product and geography? Any dangerous concentration?\n"
        "4. Red Flags: Any accounting method changes or hidden liabilities in notes?\n"
    )
    payload = {
        "business": str(sections.get("business") or "")[:8000],
        "risk_factors": str(sections.get("risk_factors") or "")[:9000],
        "mda": str(sections.get("mda") or "")[:12000],
        "notes": str(sections.get("notes") or "")[:10000],
        "segment_info": str(sections.get("segment_info") or "")[:7000],
        "executive_compensation": str(sections.get("executive_compensation") or "")[:7000],
        "related_party_transactions": str(sections.get("related_party_transactions") or "")[:7000],
        "competitor_mda": competitor_mda[:8],
    }
    if ask_ai is None:
        return {
            "capital_allocation": "insufficient_data",
            "margin_trends": "insufficient_data",
            "revenue_concentration": "insufficient_data",
            "red_flags": "insufficient_data",
            "confidence_score": 35,
            "missing_variables": ["LLM unavailable for Buffett analysis"],
            "citations": [f"/company_file/sec?t={ticker}&form={form}"],
        }
    prompt = (
        "You are a fundamental value analyst. Review extracted filing sections. "
        "Do NOT summarize text. Answer the 4 required questions.\n"
        "Return strict JSON with keys:\n"
        '{"capital_allocation":"...","margin_trends":"...","revenue_concentration":"...","red_flags":"...",'
        '"confidence_score":0,"missing_variables":["..."],"citations":["/company_file/sec?t=TICKER&form=FORM"]}\n\n'
        f"Required questions:\n{questions}\n"
        f"Ticker: {ticker}\nForm: {form}\n"
        f"Extracted sections JSON: {json.dumps(payload, ensure_ascii=False)}"
    )
    try:
        raw = str(
            ask_ai(
                prompt,
                "Buffett-style filing analyst. JSON only. Base conclusions strictly on provided sections and competitor evidence.",
                mode="smart",
                json_mode=True,
                temperature=0.1,
            )
            or ""
        ).strip()
        obj = json.loads(raw) if raw else {}
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {
            "capital_allocation": "insufficient_data",
            "margin_trends": "insufficient_data",
            "revenue_concentration": "insufficient_data",
            "red_flags": "insufficient_data",
            "confidence_score": 40,
            "missing_variables": ["LLM parsing failure"],
            "citations": [f"/company_file/sec?t={ticker}&form={form}"],
        }


def _process_one_filing(
    con_core: sqlite3.Connection,
    con_onyx: sqlite3.Connection,
    row: sqlite3.Row,
    blue_chip_tickers: set[str] | None = None,
) -> dict[str, int]:
    filing_id = int(row["id"] or 0)
    ticker = _safe_ticker(str(row["ticker"] or ""))
    form = str(row["form"] or "").strip().upper()
    fpath = str(row["path"] or "").strip()
    txt = _read_filing_text(fpath, form=form)
    if filing_id <= 0 or not ticker or not txt:
        return {"chunks": 0, "entities": 0, "relationships": 0}

    sections = _extract_target_sections(form, txt)
    target_blob = "\n\n".join([f"[{k.upper()}]\n{v}" for k, v in sections.items() if str(v or "").strip()])
    if not target_blob:
        return {"chunks": 0, "entities": 0, "relationships": 0}
    chunks = _chunk_structured_sections(sections, chunk_size=1200)
    if not chunks:
        chunks = _chunk_text(target_blob, chunk_size=1200, overlap=200)
    now = dt.datetime.now().isoformat()
    chunk_rows = 0
    entity_rows = 0
    rel_rows = 0
    cite = f"/company_file/sec?t={ticker}&form={form}" if form else f"/company_file/sec?t={ticker}"

    for idx, ch in enumerate(chunks):
        emb = _hash_embedding(ch, dims=48)
        meta = {"ticker": ticker, "form": form, "path": fpath, "filing_id": filing_id}
        con_core.execute(
            """INSERT OR REPLACE INTO filing_chunk_vectors
               (filing_id, ticker, form, chunk_index, chunk_text, embedding_json, meta_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                filing_id,
                ticker,
                form,
                int(idx),
                ch,
                json.dumps(emb, ensure_ascii=True),
                json.dumps(meta, ensure_ascii=True),
                now,
            ),
        )
        chunk_rows += 1

    company_id = _entity_id(con_onyx, ticker, "COMPANY")
    if company_id:
        entity_rows += 1
    focus_text = target_blob[:12000]
    rel_candidates: list[dict[str, Any]] = []
    for th in _extract_risk_themes(focus_text):
        rel_candidates.append(
            {
                "source": ticker,
                "target": th,
                "relationship_type": "EXPOSED_TO",
                "confidence": 0.8,
                "evidence": focus_text[:360],
            }
        )
    for peer in _extract_peer_tickers(focus_text, ticker):
        rel = "COMPETES_WITH"
        low = focus_text.lower()
        if "supplier" in low or "supply" in low:
            rel = "SUPPLIER_TO"
        elif "customer" in low:
            rel = "CUSTOMER_OF"
        rel_candidates.append(
            {
                "source": ticker,
                "target": peer,
                "relationship_type": rel,
                "confidence": 0.74,
                "evidence": focus_text[:360],
            }
        )
    rel_candidates = _reflect_relationship_candidates(ticker=ticker, candidates=rel_candidates, evidence_text=focus_text)
    for rc in rel_candidates:
        target = str(rc.get("target") or "").strip()
        rel_type = str(rc.get("relationship_type") or "").strip().upper()
        conf_rc = float(rc.get("confidence") or 0.7)
        ev = str(rc.get("evidence") or "")[:360]
        etype = "RISK_THEME" if rel_type == "EXPOSED_TO" else "COMPANY"
        tid = _entity_id(con_onyx, target, etype)
        if tid:
            entity_rows += 1
            _link(con_onyx, company_id, tid, rel_type, cite, ev, conf=conf_rc)
            rel_rows += 1

    competitor_mda = _recent_competitor_mda(ticker=ticker, limit=6)
    analysis = _buffett_analyze(ticker=ticker, form=form, sections=sections, competitor_mda=competitor_mda)
    cap = str(analysis.get("capital_allocation") or "").strip()
    mar = str(analysis.get("margin_trends") or "").strip()
    rev = str(analysis.get("revenue_concentration") or "").strip()
    red = str(analysis.get("red_flags") or "").strip()
    conf = int(float(analysis.get("confidence_score") or 0))
    facts = [
        ("Capital Allocation", cap, 8),
        ("Margin Trends", mar, 8),
        ("Revenue Concentration", rev, 9),
        ("Red Flags", red, 9),
    ]
    for label, body, imp in facts:
        b = str(body or "").strip()
        if not b:
            continue
        fact = f"[{label}] {b[:1600]}"
        h = hashlib.sha256(f"{ticker}|{form}|{filing_id}|{label}|{fact}".encode("utf-8", errors="ignore")).hexdigest()
        con_core.execute(
            """INSERT OR IGNORE INTO report_facts
               (report_name, report_kind, report_modified, fact_date, ticker, fact_text, importance, source, fact_hash, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"SEC:{ticker}:{form}:{filing_id}",
                "sec_filing",
                "",
                dt.date.today().isoformat(),
                ticker,
                fact,
                int(imp),
                "sec_buffett",
                h,
                now,
            ),
            )

    _maybe_write_thesis_alert_note(
        ticker=ticker,
        filing_id=filing_id,
        form=form,
        analysis=analysis,
        citation_url=cite,
    )

    if ticker in set(blue_chip_tickers or set()):
        macro_node_name = f"MACRO:{ticker}:{dt.date.today().isoformat()}"
        macro_id = _entity_id(con_onyx, macro_node_name, "MACRO_NODE")
        _link(
            con_onyx,
            company_id,
            macro_id,
            "SIGNALS_MACRO",
            cite,
            f"{cap[:140]} | {mar[:140]} | {rev[:140]} | {red[:140]}",
            conf=0.86,
        )
    return {"chunks": chunk_rows, "entities": entity_rows, "relationships": rel_rows}


def process_new_filings_pipeline(filing_ids: list[int]) -> dict[str, Any]:
    ensure_sec_ingest_schema()
    ids = sorted({int(x) for x in list(filing_ids or []) if int(x) > 0})
    if not ids:
        return {"ok": True, "processed": 0, "chunks": 0, "entities": 0, "relationships": 0, "monitor": {}}

    scope = _scope_tickers()
    blue_chip_tickers = _blue_chip_set()
    con_core = _conn_core()
    con_onyx = _conn_onyx()
    processed = 0
    chunks = 0
    entities = 0
    rels = 0
    try:
        marks = ",".join("?" for _ in ids)
        rows = con_core.execute(
            f"""SELECT id, ticker, form, path
                FROM filings
                WHERE id IN ({marks})
                ORDER BY id ASC""",
            tuple(ids),
        ).fetchall()
        for r in rows:
            tk = _safe_ticker(str(r["ticker"] or ""))
            if scope and tk not in scope:
                continue
            st = _process_one_filing(con_core, con_onyx, r, blue_chip_tickers=blue_chip_tickers)
            processed += 1
            chunks += int(st.get("chunks") or 0)
            entities += int(st.get("entities") or 0)
            rels += int(st.get("relationships") or 0)
        con_core.commit()
        con_onyx.commit()
    finally:
        con_core.close()
        con_onyx.close()

    monitor = run_event_driven_monitor(force=True)
    return {
        "ok": True,
        "processed": processed,
        "chunks": chunks,
        "entities": entities,
        "relationships": rels,
        "monitor": monitor,
    }
