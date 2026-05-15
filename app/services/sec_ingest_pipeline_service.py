from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from app.core.config import ROOT
from app.core import cloud_files
from app.core.entity_quality import clean_peer_ticker_candidate, entity_quality_gate_enabled, is_valid_company_entity_name
from app.core.ontology_math import decay_enabled as _decay_enabled, decay_half_life_days as _decay_half_life_days, effective_confidence as _effective_confidence, signed_weight_for_rel as _signed_weight_for_rel
from app.core.filing_text import resolve_filing_path, read_filing_text
from app.core.ticker import safe_ticker as _safe_ticker
from app.services.organizer_service import add_general_note
from app.services.postgres_core_service import core_backend, pg_connect
from app.services.entity_resolution_service import ensure_entity_resolution_schema
from app.services.proactive_ai_service import detect_thesis_breaches, ensure_proactive_schema, run_event_driven_monitor
from app.services.company_intel_service import get_company_intel, maybe_refresh_company_intel_on_filing
from app.services.mini_statements_service import get_mini_statements
from app.services.supply_chain_service import ingest_sec_relationship_to_network

try:
    from tools.llm_engine import ask_ai
except Exception:  # pragma: no cover
    ask_ai = None  # type: ignore[assignment]


STRUCTURED_DATA_UNAVAILABLE_MSG = "Data not available in structured filings."
FILING_CONTENT_MAX_CHARS = 15000


def _temporal_enabled() -> bool:
    return str(os.getenv("ONTOLOGY_TEMPORAL_ENABLED", "0")).strip().lower() in {"1", "true", "yes", "on"}



# _decay_enabled, _decay_half_life_days, _effective_confidence, _signed_weight_for_rel
# imported from app.core.ontology_math

def _insert_report_fact_core_pg(
    *,
    report_name: str,
    report_kind: str,
    report_modified: str,
    fact_date: str,
    ticker: str,
    fact_text: str,
    importance: int,
    source: str,
    fact_hash: str,
    created_at: str,
) -> None:
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO report_facts_core
            (report_name, report_kind, report_modified, fact_date, ticker, fact_text, importance, source, fact_hash, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(fact_hash) DO UPDATE SET
              report_name=EXCLUDED.report_name,
              report_kind=EXCLUDED.report_kind,
              report_modified=EXCLUDED.report_modified,
              fact_date=EXCLUDED.fact_date,
              ticker=EXCLUDED.ticker,
              fact_text=EXCLUDED.fact_text,
              importance=EXCLUDED.importance,
              source=EXCLUDED.source,
              created_at=EXCLUDED.created_at
            """,
            (
                str(report_name or "")[:400],
                str(report_kind or "")[:120],
                str(report_modified or "")[:40],
                str(fact_date or "")[:20],
                str(ticker or "")[:16],
                str(fact_text or "")[:4000],
                int(importance or 0),
                str(source or "")[:80],
                str(fact_hash or "")[:80],
                str(created_at or "")[:40],
            ),
        )
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


def ensure_sec_ingest_schema() -> None:
    if core_backend() != "postgres":
        return
    con_pg = pg_connect()
    if con_pg is not None:
        try:
            cur = con_pg.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS filing_chunk_vectors_core (
                    id BIGINT PRIMARY KEY,
                    filing_id BIGINT NOT NULL,
                    ticker TEXT NOT NULL DEFAULT '',
                    form TEXT NOT NULL DEFAULT '',
                    chunk_index INTEGER NOT NULL,
                    chunk_text TEXT NOT NULL,
                    embedding_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                    meta_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TEXT NOT NULL,
                    UNIQUE(filing_id, chunk_index)
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_fcv_core_filing ON filing_chunk_vectors_core(filing_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_fcv_core_ticker ON filing_chunk_vectors_core(ticker)")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS entities_core (
                    id BIGINT PRIMARY KEY,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    entity_type TEXT NOT NULL DEFAULT '',
                    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    normalized_name TEXT NOT NULL,
                    id_text TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(type, normalized_name)
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_entities_core_type_norm ON entities_core(type, normalized_name)")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS relationships_core (
                    id BIGINT PRIMARY KEY,
                    source_id BIGINT NOT NULL,
                    target_id BIGINT NOT NULL,
                    relationship_type TEXT NOT NULL,
                    citation_link TEXT NOT NULL DEFAULT '',
                    citation_url TEXT NOT NULL DEFAULT '',
                    citation_text TEXT NOT NULL DEFAULT '',
                    confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                    confidence_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                    valid_from TEXT NOT NULL DEFAULT '',
                    valid_to TEXT NOT NULL DEFAULT '',
                    last_verified_at TEXT NOT NULL DEFAULT '',
                    decay_half_life_days INTEGER NOT NULL DEFAULT 365,
                    effective_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                    status TEXT NOT NULL DEFAULT 'active',
                    signed_weight DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                    criticality_score DOUBLE PRECISION NOT NULL DEFAULT 0.5,
                    created_at TEXT NOT NULL,
                    UNIQUE(source_id, target_id, relationship_type, citation_link)
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_rel_core_source ON relationships_core(source_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_rel_core_target ON relationships_core(target_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_rel_core_type ON relationships_core(relationship_type)")
            # Traversal hot-path indexes for macro shock simulation (active edges + confidence ordering).
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_rel_core_active_source_conf
                ON relationships_core(source_id, effective_confidence DESC, id DESC)
                WHERE status='active'
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_rel_core_active_target_conf
                ON relationships_core(target_id, effective_confidence DESC, id DESC)
                WHERE status='active'
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_rel_core_status_valid_to ON relationships_core(status, valid_to)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_rel_core_status_effective_conf ON relationships_core(status, effective_confidence DESC)")
            cur.execute("ALTER TABLE relationships_core ADD COLUMN IF NOT EXISTS valid_from TEXT NOT NULL DEFAULT ''")
            cur.execute("ALTER TABLE relationships_core ADD COLUMN IF NOT EXISTS valid_to TEXT NOT NULL DEFAULT ''")
            cur.execute("ALTER TABLE relationships_core ADD COLUMN IF NOT EXISTS last_verified_at TEXT NOT NULL DEFAULT ''")
            cur.execute("ALTER TABLE relationships_core ADD COLUMN IF NOT EXISTS decay_half_life_days INTEGER NOT NULL DEFAULT 365")
            cur.execute("ALTER TABLE relationships_core ADD COLUMN IF NOT EXISTS effective_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0")
            cur.execute("ALTER TABLE relationships_core ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'")
            cur.execute("ALTER TABLE relationships_core ADD COLUMN IF NOT EXISTS signed_weight DOUBLE PRECISION NOT NULL DEFAULT 1.0")
            cur.execute("ALTER TABLE relationships_core ADD COLUMN IF NOT EXISTS criticality_score DOUBLE PRECISION NOT NULL DEFAULT 0.5")
            con_pg.commit()
            ensure_entity_resolution_schema()
        except Exception:
            try:
                con_pg.rollback()
            except Exception:
                pass
        finally:
            con_pg.close()
    ensure_proactive_schema()

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


def _extract_target_sections(form: str, raw_text: str) -> dict[str, str]:
    """
    Buffett-style selective extraction — delegates to shared extractor in filing_text.py.
    Applies HTML→markdown preprocessing before extraction.
    """
    from app.core.filing_text import extract_filing_sections
    txt = _html_to_structured_markdown(raw_text)
    return extract_filing_sections(form, txt)


def _entity_id_pg(con_pg: Any, name: str, entity_type: str) -> int:
    nm = str(name or "").strip()
    et = str(entity_type or "").strip().upper()
    if not nm or not et:
        return 0
    if et == "COMPANY" and entity_quality_gate_enabled() and not is_valid_company_entity_name(nm):
        return 0
    norm = re.sub(r"\s+", " ", nm.lower())
    id_text = f"{et.lower()}:{norm}"
    now = dt.datetime.now().isoformat()
    cur = con_pg.cursor()
    cur.execute(
        """
        INSERT INTO entities_core(name, type, entity_type, metadata_json, normalized_name, id_text, created_at, updated_at, id)
        VALUES(%s,%s,%s,%s::jsonb,%s,%s,%s,%s,(SELECT COALESCE(MAX(id),0)+1 FROM entities_core))
        ON CONFLICT(type, normalized_name) DO UPDATE SET
          name=EXCLUDED.name,
          entity_type=EXCLUDED.entity_type,
          id_text=EXCLUDED.id_text,
          updated_at=EXCLUDED.updated_at
        """,
        (nm, et, et, "{}", norm, id_text, now, now),
    )
    cur.execute("SELECT id FROM entities_core WHERE type=%s AND normalized_name=%s LIMIT 1", (et, norm))
    row = cur.fetchone()
    return int((row or [0])[0] or 0)


def _link_pg(
    con_pg: Any,
    source_id: int,
    target_id: int,
    rel_type: str,
    citation_url: str,
    citation_text: str,
    conf: float = 0.72,
) -> None:
    if source_id <= 0 or target_id <= 0:
        return
    cur = con_pg.cursor()
    now = dt.datetime.now().isoformat()
    half_life = _decay_half_life_days()
    eff = _effective_confidence(float(conf or 0.0), now, half_life)
    valid_from = now if _temporal_enabled() else ""
    valid_to = ""
    status = "active"
    signed_weight = _signed_weight_for_rel(rel_type)
    criticality = _estimate_relationship_criticality(rel_type, citation_text)
    cur.execute(
        """
        INSERT INTO relationships_core
        (id, source_id, target_id, relationship_type, citation_link, citation_url, citation_text,
         confidence, confidence_score, created_at, valid_from, valid_to, last_verified_at,
         decay_half_life_days, effective_confidence, status, signed_weight, criticality_score)
        VALUES
        ((SELECT COALESCE(MAX(id),0)+1 FROM relationships_core), %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(source_id, target_id, relationship_type, citation_link) DO NOTHING
        """,
        (
            int(source_id),
            int(target_id),
            str(rel_type or "").strip().upper(),
            str(citation_url or "").strip(),
            str(citation_url or "").strip(),
            str(citation_text or "")[:500],
            float(conf or 0.0),
            float(conf or 0.0),
            now,
            valid_from,
            valid_to,
            now,
            int(half_life),
            float(eff),
            status,
            float(signed_weight),
            float(criticality),
        ),
    )


def _estimate_relationship_criticality(rel_type: str, citation_text: str) -> float:
    rel = str(rel_type or "").strip().upper()
    text = str(citation_text or "").lower()
    base = 0.5
    if rel in {"SUPPLIER_TO", "CUSTOMER_OF"}:
        base = 0.65
    elif rel in {"COMPETES_WITH"}:
        base = 0.45
    elif rel in {"HEDGES_AGAINST", "IMMUNE_TO"}:
        base = 0.35
    if any(k in text for k in ("sole-source", "primary supplier", "largest customer", "key supplier", "material")):
        base += 0.2
    if re.search(r"\b\d{1,2}(\.\d+)?%\b", text):
        base += 0.05
    if any(k in text for k in ("immaterial", "minor", "limited exposure", "small portion")):
        base -= 0.2
    return max(0.05, min(1.0, float(base)))


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
        tk = clean_peer_ticker_candidate(m)
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
    allowed = {"EXPOSED_TO", "COMPETES_WITH", "SUPPLIER_TO", "CUSTOMER_OF", "SIGNALS_MACRO", "HEDGES_AGAINST", "IMMUNE_TO"}
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
            "Allowed relationship_type: EXPOSED_TO, COMPETES_WITH, SUPPLIER_TO, CUSTOMER_OF, SIGNALS_MACRO, HEDGES_AGAINST, IMMUNE_TO.\n"
            "Do not invent new targets. Only keep or downgrade candidates based on evidence.\n"
            "If asked for quantitative SEC metrics without structured_financial_data_json, return exactly: Data not available in structured filings.\n"
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
    con_pg = pg_connect()
    if con_pg is not None:
        try:
            cur = con_pg.cursor()
            cur.execute("SELECT 1 FROM investor_notes_core WHERE trace_id=%s LIMIT 1", (trace,))
            if cur.fetchone():
                return
        except Exception:
            pass
        finally:
            con_pg.close()
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
    """Read filing text via the shared reader (local → DB → GCS)."""
    from app.core.filing_text import read_filing_text_any
    raw = read_filing_text_any(path_s)
    if not raw:
        return ""
    return _extract_primary_doc_from_submission(raw, form=form)


def _fetch_url_text(url: str, timeout_sec: int = 20) -> str:
    u = str(url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return ""
    req = urllib.request.Request(
        u,
        headers={
            "User-Agent": "InvestorOS SEC Ingest/1.0 (research@investoros.local)",
            "Accept": "text/html, text/plain, */*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=max(5, int(timeout_sec or 20))) as resp:
            return str(resp.read().decode("utf-8", errors="ignore") or "")
    except Exception:
        return ""


def _sec_doc_candidates(doc_url: str) -> list[str]:
    base = str(doc_url or "").strip()
    if not base:
        return []
    out: list[str] = []
    seen: set[str] = set()
    # SEC filing index URL often has full-text submission as same accession .txt
    if base.lower().endswith("-index.html"):
        out.append(base[:-11] + ".txt")
    out.append(base)
    low = base.lower()
    if low.endswith(".htm"):
        out.append(base[:-4] + ".txt")
    if low.endswith(".html"):
        out.append(base[:-5] + ".txt")
    uniq: list[str] = []
    for u in out:
        if u and u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def _load_or_fetch_filing_text(
    *,
    ticker: str,
    form: str,
    path_s: str,
    doc_url: str,
    filing_id: int,
) -> tuple[str, str]:
    txt = _read_filing_text(path_s, form=form)
    if txt:
        return txt, str(path_s or "")
    for cand in _sec_doc_candidates(doc_url):
        raw = _fetch_url_text(cand, timeout_sec=20)
        if not raw or len(raw) < 300:
            continue
        parsed = _extract_primary_doc_from_submission(raw, form=form) or raw
        if len(parsed) < 300:
            continue
        # Persist fetched text so future passes don't refetch.
        safe_id = re.sub(r"[^A-Za-z0-9\-]", "", str(filing_id or 0))
        rel_path = f"filing_docs/{ticker}_{form}_{safe_id}_fetched.txt"
        if cloud_files.write_text(rel_path, parsed):
            return parsed, rel_path
        return parsed, str(path_s or "")
    return "", str(path_s or "")


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
    con_pg = pg_connect()
    if con_pg is not None:
        try:
            cur = con_pg.cursor()
            cur.execute("SELECT to_regclass('public.blue_chips_core')")
            if (cur.fetchone() or [None])[0]:
                cur.execute("SELECT ticker FROM blue_chips_core")
                for r in cur.fetchall() or []:
                    t = _safe_ticker(str(r[0] or ""))
                    if t:
                        out.add(t)
        except Exception:
            pass
        finally:
            con_pg.close()
    return out


def _blue_chip_set() -> set[str]:
    out: set[str] = set()
    con_pg = pg_connect()
    if con_pg is None:
        return out
    try:
        cur = con_pg.cursor()
        cur.execute("SELECT to_regclass('public.blue_chips_core')")
        if (cur.fetchone() or [None])[0]:
            cur.execute("SELECT ticker FROM blue_chips_core")
            for r in cur.fetchall() or []:
                t = _safe_ticker(str(r[0] or ""))
                if t:
                    out.add(t)
    except Exception:
        return out
    finally:
        con_pg.close()
    return out


def _recent_competitor_mda(ticker: str, limit: int = 6) -> list[dict[str, str]]:
    """
    Uses ontology graph to find competitors and then fetches recent MD&A-derived facts for cross-reference.
    """
    tk = _safe_ticker(ticker)
    if not tk:
        return []
    peer_tickers: list[str] = []
    con_pg = pg_connect()
    if con_pg is None:
        return []
    try:
        cur = con_pg.cursor()
        cur.execute("SELECT id FROM entities_core WHERE type='COMPANY' AND normalized_name=%s LIMIT 1", (tk.lower(),))
        row = cur.fetchone()
        if not row:
            return []
        cid = int((row or [0])[0] or 0)
        cur.execute(
            """SELECT e2.name
               FROM relationships_core r
               JOIN entities_core e2 ON e2.id = r.target_id
               WHERE r.source_id=%s AND r.relationship_type IN ('COMPETES_WITH','PEER_OF')
               ORDER BY r.id DESC
               LIMIT 12""",
            (cid,),
        )
        rels = cur.fetchall() or []
        for rr in rels:
            t = _safe_ticker(str(rr[0] or ""))
            if t and t != tk:
                peer_tickers.append(t)
    finally:
        con_pg.close()
    if not peer_tickers:
        return []
    con_pg = pg_connect()
    if con_pg is not None:
        out_pg: list[dict[str, str]] = []
        try:
            marks = ",".join("%s" for _ in peer_tickers)
            cur = con_pg.cursor()
            cur.execute(
                f"""SELECT ticker, fact_text, report_name, created_at
                    FROM report_facts_core
                    WHERE ticker IN ({marks}) AND source='sec_buffett'
                    ORDER BY id DESC
                    LIMIT %s""",
                tuple(peer_tickers + [max(2, int(limit or 6))]),
            )
            for r in cur.fetchall() or []:
                out_pg.append(
                    {
                        "ticker": str(r[0] or "").upper(),
                        "fact_text": str(r[1] or ""),
                        "report_name": str(r[2] or ""),
                        "created_at": str(r[3] or ""),
                    }
                )
            return out_pg
        except Exception:
            return []
        finally:
            con_pg.close()
    return []


def _structured_financial_context(ticker: str) -> dict[str, Any]:
    tk = _safe_ticker(ticker)
    if not tk:
        return {"ok": False, "message": STRUCTURED_DATA_UNAVAILABLE_MSG}
    intel = get_company_intel(tk, refresh=False)
    mini = get_mini_statements(tk)
    seg = dict((intel or {}).get("revenue_segments") or {})
    buy = dict((intel or {}).get("buyback") or {})
    mini_years = list((mini or {}).get("years") or [])
    has_seg = bool(seg.get("product") or seg.get("geography"))
    has_buy = bool(buy.get("ttm_value") is not None or buy.get("quarters"))
    has_mini = bool(mini_years)
    if not (has_seg or has_buy or has_mini):
        return {"ok": False, "message": STRUCTURED_DATA_UNAVAILABLE_MSG}
    return {
        "ok": True,
        "message": "",
        "financials": {
            "ticker": tk,
            "mini_statements": mini,
            "revenue_segments": seg,
            "buyback": buy,
        },
    }


def _validate_analysis_json(obj: Any) -> dict[str, Any]:
    d = obj if isinstance(obj, dict) else {}
    out = {
        "capital_allocation": str(d.get("capital_allocation") or "").strip(),
        "margin_trends": str(d.get("margin_trends") or "").strip(),
        "revenue_concentration": str(d.get("revenue_concentration") or "").strip(),
        "red_flags": str(d.get("red_flags") or "").strip(),
        "confidence_score": 0,
        "missing_variables": [],
        "citations": [],
    }
    try:
        out["confidence_score"] = max(0, min(100, int(float(d.get("confidence_score") or 0))))
    except Exception:
        out["confidence_score"] = 0
    mv = d.get("missing_variables")
    if isinstance(mv, list):
        out["missing_variables"] = [str(x).strip() for x in mv if str(x).strip()][:12]
    ct = d.get("citations")
    if isinstance(ct, list):
        out["citations"] = [str(x).strip() for x in ct if str(x).strip()][:12]
    for k in ("capital_allocation", "margin_trends", "revenue_concentration", "red_flags"):
        if not str(out.get(k) or "").strip():
            out[k] = STRUCTURED_DATA_UNAVAILABLE_MSG
    return out


def _buffett_analyze(
    ticker: str,
    form: str,
    sections: dict[str, str],
    competitor_mda: list[dict[str, str]],
    structured_ctx: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "business": str(sections.get("business") or "")[:8000],
        "risk_factors": str(sections.get("risk_factors") or "")[:9000],
        "mda": str(sections.get("mda") or "")[:12000],
        "notes": str(sections.get("notes") or "")[:8000],
        "segment_info": str(sections.get("segment_info") or "")[:4000],
        "executive_compensation": str(sections.get("executive_compensation") or "")[:7000],
        "related_party_transactions": str(sections.get("related_party_transactions") or "")[:7000],
        "competitor_mda": competitor_mda[:8],
    }
    if not bool((structured_ctx or {}).get("ok")):
        msg = str((structured_ctx or {}).get("message") or STRUCTURED_DATA_UNAVAILABLE_MSG)
        return {
            "capital_allocation": msg,
            "margin_trends": msg,
            "revenue_concentration": msg,
            "red_flags": msg,
            "confidence_score": 0,
            "missing_variables": [msg],
            "citations": [f"/company_file/sec?t={ticker}&form={form}"],
        }
    if ask_ai is None:
        return {
            "capital_allocation": STRUCTURED_DATA_UNAVAILABLE_MSG,
            "margin_trends": STRUCTURED_DATA_UNAVAILABLE_MSG,
            "revenue_concentration": STRUCTURED_DATA_UNAVAILABLE_MSG,
            "red_flags": STRUCTURED_DATA_UNAVAILABLE_MSG,
            "confidence_score": 35,
            "missing_variables": ["LLM unavailable for Buffett analysis"],
            "citations": [f"/company_file/sec?t={ticker}&form={form}"],
        }
    prompt = (
        "You are an expert financial analyst.\n"
        "You will be provided with 100% accurate quantitative data in JSON format.\n"
        "Do NOT calculate, infer, or extract new numbers from raw filing text.\n"
        "If quantitative data is unavailable, output exactly: Data not available in structured filings.\n"
        "Your task is qualitative synthesis only from MD&A and Risk Factors: explain WHY the structured numbers changed, "
        "management tone, and new risk factors.\n"
        "Return strict JSON with keys:\n"
        '{"capital_allocation":"...","margin_trends":"...","revenue_concentration":"...","red_flags":"...",'
        '"confidence_score":0,"missing_variables":["..."],"citations":["/company_file/sec?t=TICKER&form=FORM"]}\n\n'
        f"Ticker: {ticker}\nForm: {form}\n"
        f"STRUCTURED_FINANCIAL_DATA_JSON: {json.dumps((structured_ctx or {}).get('financials') or {}, ensure_ascii=False)}\n"
        f"QUALITATIVE_SECTIONS_JSON: {json.dumps(payload, ensure_ascii=False)}"
    )
    try:
        raw = str(
            ask_ai(
                prompt,
                "Financial synthesizer. Use provided structured JSON for numbers; qualitative sections for narrative only.",
                mode="smart",
                json_mode=True,
                temperature=0.1,
            )
            or ""
        ).strip()
        obj = json.loads(raw) if raw else {}
        return _validate_analysis_json(obj)
    except Exception:
        return {
            "capital_allocation": STRUCTURED_DATA_UNAVAILABLE_MSG,
            "margin_trends": STRUCTURED_DATA_UNAVAILABLE_MSG,
            "revenue_concentration": STRUCTURED_DATA_UNAVAILABLE_MSG,
            "red_flags": STRUCTURED_DATA_UNAVAILABLE_MSG,
            "confidence_score": 40,
            "missing_variables": ["LLM parsing failure"],
            "citations": [f"/company_file/sec?t={ticker}&form={form}"],
        }


def _process_one_filing(
    row: dict[str, Any],
    blue_chip_tickers: set[str] | None = None,
) -> dict[str, int]:
    filing_id = int(row["id"] or 0)
    ticker = _safe_ticker(str(row["ticker"] or ""))
    form = str(row["form"] or "").strip().upper()
    filing_date = str(row["date"] or "").strip()
    fpath = str(row["path"] or "").strip()
    doc_url = str(row.get("doc_url") or "").strip() if isinstance(row, dict) else ""
    txt, fetched_path = _load_or_fetch_filing_text(
        ticker=ticker,
        form=form,
        path_s=fpath,
        doc_url=doc_url,
        filing_id=filing_id,
    )
    if fetched_path and fetched_path != fpath:
        fpath = fetched_path
        con_fix = pg_connect()
        if con_fix is not None:
            try:
                cur_fix = con_fix.cursor()
                # Update path and back-fill content so Cloud Run can read it without GCS
                cur_fix.execute(
                    "UPDATE filings_core SET path=%s, content=CASE WHEN content='' THEN %s ELSE content END WHERE id=%s",
                    (fpath, str(txt or "")[:FILING_CONTENT_MAX_CHARS], filing_id),
                )
                con_fix.commit()
            except Exception:
                try:
                    con_fix.rollback()
                except Exception:
                    pass
            finally:
                con_fix.close()
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

    con_pg_graph = None
    con_pg_graph = pg_connect()
    if con_pg_graph is not None:
        try:
            cur = con_pg_graph.cursor()
            for idx, ch in enumerate(chunks):
                emb = _hash_embedding(ch, dims=48)
                meta = {"ticker": ticker, "form": form, "path": fpath, "filing_id": filing_id}
                cur.execute(
                    """
                    INSERT INTO filing_chunk_vectors_core
                    (id, filing_id, ticker, form, chunk_index, chunk_text, embedding_json, meta_json, created_at)
                    VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM filing_chunk_vectors_core), %s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s)
                    ON CONFLICT(filing_id, chunk_index) DO UPDATE SET
                      ticker=EXCLUDED.ticker, form=EXCLUDED.form, chunk_text=EXCLUDED.chunk_text,
                      embedding_json=EXCLUDED.embedding_json, meta_json=EXCLUDED.meta_json, created_at=EXCLUDED.created_at
                    """,
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
        except Exception:
            pass

    company_id = _entity_id_pg(con_pg_graph, ticker, "COMPANY") if con_pg_graph is not None else 0
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
        if "hedge against" in low or "hedges against" in low or "hedging" in low:
            rel = "HEDGES_AGAINST"
        elif "immune to" in low or "insulated from" in low or "resilient to" in low:
            rel = "IMMUNE_TO"
        elif "supplier" in low or "supply" in low:
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
        tid = _entity_id_pg(con_pg_graph, target, etype) if con_pg_graph is not None else 0
        if tid:
            entity_rows += 1
            if con_pg_graph is not None:
                _link_pg(con_pg_graph, company_id, tid, rel_type, cite, ev, conf=conf_rc)
            rel_rows += 1

        # Auto-wire SEC extraction output into Supply Chain Mapper graph tables.
        # Mapping:
        # - SUPPLIER_TO: ticker supplies target => (ticker -> target) supplies
        # - CUSTOMER_OF: ticker is customer of target => (target -> ticker) supplies
        # - COMPETES_WITH: kept as non-directional partnership proxy
        try:
            conf_100 = max(1, min(100, int(round(conf_rc * 100.0))))
            if rel_type == "SUPPLIER_TO":
                ingest_sec_relationship_to_network(
                    source_ticker=ticker,
                    target_ticker=target,
                    relationship_type="supplies",
                    evidence_text=ev,
                    source_url=cite,
                    confidence_score=conf_100,
                )
            elif rel_type == "CUSTOMER_OF":
                ingest_sec_relationship_to_network(
                    source_ticker=target,
                    target_ticker=ticker,
                    relationship_type="supplies",
                    evidence_text=ev,
                    source_url=cite,
                    confidence_score=conf_100,
                )
            elif rel_type == "COMPETES_WITH":
                ingest_sec_relationship_to_network(
                    source_ticker=ticker,
                    target_ticker=target,
                    relationship_type="partners_with",
                    evidence_text=ev,
                    source_url=cite,
                    confidence_score=max(40, min(conf_100, 85)),
                )
        except Exception:
            pass

    competitor_mda = _recent_competitor_mda(ticker=ticker, limit=6)
    try:
        maybe_refresh_company_intel_on_filing(ticker=ticker, form=form, filing_date=filing_date, max_stale_days=14)
    except Exception:
        pass
    structured_ctx = _structured_financial_context(ticker)
    analysis = _buffett_analyze(
        ticker=ticker,
        form=form,
        sections=sections,
        competitor_mda=competitor_mda,
        structured_ctx=structured_ctx,
    )
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
        _insert_report_fact_core_pg(
            report_name=f"SEC:{ticker}:{form}:{filing_id}",
            report_kind="sec_filing",
            report_modified="",
            fact_date=dt.date.today().isoformat(),
            ticker=ticker,
            fact_text=fact,
            importance=int(imp),
            source="sec_buffett",
            fact_hash=h,
            created_at=now,
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
        macro_id = _entity_id_pg(con_pg_graph, macro_node_name, "MACRO_NODE") if con_pg_graph is not None else 0
        if con_pg_graph is not None and macro_id > 0:
            _link_pg(
                con_pg_graph,
                company_id,
                macro_id,
                "SIGNALS_MACRO",
                cite,
                f"{cap[:140]} | {mar[:140]} | {rev[:140]} | {red[:140]}",
                conf=0.86,
            )
    if con_pg_graph is not None:
        try:
            con_pg_graph.commit()
        except Exception:
            try:
                con_pg_graph.rollback()
            except Exception:
                pass
        finally:
            con_pg_graph.close()
    return {"chunks": chunk_rows, "entities": entity_rows, "relationships": rel_rows}


def process_new_filings_pipeline(
    filing_ids: list[int],
    *,
    scope_override: set[str] | None = None,
) -> dict[str, Any]:
    if core_backend() != "postgres":
        return {"ok": False, "error": "postgres_required", "processed": 0, "chunks": 0, "entities": 0, "relationships": 0, "monitor": {}}
    ensure_sec_ingest_schema()
    ids = sorted({int(x) for x in list(filing_ids or []) if int(x) > 0})
    if not ids:
        return {"ok": True, "processed": 0, "chunks": 0, "entities": 0, "relationships": 0, "monitor": {}}

    scope = set(scope_override or set()) or _scope_tickers()
    blue_chip_tickers = _blue_chip_set()
    processed = 0
    chunks = 0
    entities = 0
    rels = 0
    processed_tickers: set[str] = set()
    rows_iter: list[dict[str, Any]] = []
    con_pg = pg_connect()
    if con_pg is not None:
        try:
            marks_pg = ",".join("%s" for _ in ids)
            cur = con_pg.cursor()
            cur.execute(
                f"""SELECT id, ticker, form, path, date, doc_url
                    FROM filings_core
                    WHERE id IN ({marks_pg})
                    ORDER BY id ASC""",
                tuple(ids),
            )
            rows_iter = [
                {
                    "id": int(r[0] or 0),
                    "ticker": str(r[1] or ""),
                    "form": str(r[2] or ""),
                    "path": str(r[3] or ""),
                    "date": str(r[4] or ""),
                    "doc_url": str(r[5] or ""),
                }
                for r in (cur.fetchall() or [])
            ]
        except Exception:
            rows_iter = []
        finally:
            con_pg.close()

    for r in rows_iter:
        tk = _safe_ticker(str(r["ticker"] or ""))
        if scope and tk not in scope:
            continue
        st = _process_one_filing(r, blue_chip_tickers=blue_chip_tickers)
        processed += 1
        chunks += int(st.get("chunks") or 0)
        entities += int(st.get("entities") or 0)
        rels += int(st.get("relationships") or 0)
        if tk:
            processed_tickers.add(tk)

    # Phase 3.3: auto-run thesis breach detection for each ticker that had new filings
    for tk in processed_tickers:
        try:
            detect_thesis_breaches(ticker=tk)
        except Exception:
            pass

    monitor = run_event_driven_monitor(force=True)
    return {
        "ok": True,
        "processed": processed,
        "chunks": chunks,
        "entities": entities,
        "relationships": rels,
        "monitor": monitor,
    }


def ingest_sec_facts_for_ticker(
    ticker: str,
    *,
    max_filings: int = 24,
    forms: tuple[str, ...] = ("8-K", "6-K", "10-Q", "10-K", "20-F", "40-F"),
) -> dict[str, Any]:
    tk = _safe_ticker(ticker)
    if not tk:
        return {"ok": False, "error": "ticker_required", "selected": 0}
    if core_backend() != "postgres":
        return {"ok": False, "error": "postgres_required", "selected": 0}
    con_pg = pg_connect()
    if con_pg is None:
        return {"ok": False, "error": "postgres_unavailable", "selected": 0}
    ids: list[int] = []
    try:
        cur = con_pg.cursor()
        cur.execute(
            """
            SELECT id
            FROM filings_core
            WHERE ticker=%s AND form = ANY(%s)
            ORDER BY date DESC, id DESC
            LIMIT %s
            """,
            (tk, list(forms), max(1, min(400, int(max_filings or 24)))),
        )
        ids = [int(r[0] or 0) for r in (cur.fetchall() or []) if int(r[0] or 0) > 0]
    except Exception as exc:
        return {"ok": False, "error": f"filings_query_failed:{exc}", "selected": 0}
    finally:
        try:
            con_pg.close()
        except Exception:
            pass
    if not ids:
        return {"ok": True, "selected": 0, "processed": 0, "chunks": 0, "entities": 0, "relationships": 0}
    out = process_new_filings_pipeline(ids, scope_override={tk})
    out["selected"] = len(ids)
    out["ticker"] = tk
    return out
