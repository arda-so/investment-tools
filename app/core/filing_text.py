from __future__ import annotations

import re
from pathlib import Path

from app.core.config import ROOT
from app.core import cloud_files


def _read_content_from_db(path_s: str, max_chars: int | None = None) -> str:
    """
    Look up filing text stored in filings_core.content by matching path or accession.
    Returns empty string if not found or DB unavailable.
    This is the primary read path in Cloud Run (no local filesystem).
    """
    try:
        import os
        if os.getenv("CORE_DB_BACKEND", "").lower() != "postgres":
            return ""
        from app.services.postgres_core_service import pg_connect
        con = pg_connect()
        if con is None:
            return ""
        try:
            cur = con.cursor()
            # Match by stored path first, then by accession extracted from filename
            cur.execute(
                "SELECT content FROM filings_core WHERE path=%s AND content<>'' LIMIT 1",
                (str(path_s or ""),),
            )
            row = cur.fetchone()
            if not row:
                # Try matching by accession parsed from the path filename
                # e.g. "filing_docs/IT_10-Q_0001193125-26-012345.txt" → accession segment
                fname = Path(str(path_s or "")).stem  # "IT_10-Q_0001193125-26-012345"
                parts = fname.split("_", 2)
                if len(parts) >= 3:
                    acc_fragment = parts[2]  # "0001193125-26-012345"
                    cur.execute(
                        "SELECT content FROM filings_core WHERE accession LIKE %s AND content<>'' ORDER BY id DESC LIMIT 1",
                        (f"%{acc_fragment[:20]}%",),
                    )
                    row = cur.fetchone()
            if row:
                txt = str(row[0] or "")
                return txt[:max_chars] if max_chars and max_chars > 0 else txt
            return ""
        finally:
            con.close()
    except Exception:
        return ""


_ALLOWED_REL_PREFIXES = ("filings/", "filing_docs/", "reports/")


def normalize_filing_rel_path(path_s: str) -> str | None:
    raw = str(path_s or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    if p.is_absolute():
        try:
            p = p.resolve()
        except Exception:
            return None
        root = ROOT.resolve()
        if not str(p).startswith(str(root)):
            return None
        rel = str(p.relative_to(root)).replace("\\", "/")
    else:
        rel = str(p).strip().lstrip("/").replace("\\", "/")
    if ".." in rel.split("/"):
        return None
    if not any(rel.startswith(pref) for pref in _ALLOWED_REL_PREFIXES):
        return None
    return rel


def filing_path_available(path_s: str) -> bool:
    rel = normalize_filing_rel_path(path_s)
    if not rel:
        return False
    p = (ROOT / rel).resolve()
    if p.exists() and p.is_file():
        return True
    return bool(cloud_files.exists(rel))


def resolve_filing_path(path_s: str) -> Path | None:
    rel = normalize_filing_rel_path(path_s)
    if not rel:
        return None
    p = (ROOT / rel).resolve()
    if not p.exists() or not p.is_file():
        return None
    return p


def read_filing_text(path: Path, *, strip_html: bool = True) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return ""
    txt = path.read_text(encoding="utf-8", errors="ignore")
    if strip_html and ("<html" in txt[:4000].lower() or "<body" in txt[:4000].lower()):
        txt = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", txt)
        txt = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", txt)
        txt = re.sub(r"(?is)<[^>]+>", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


# ── Section extraction (shared by lab, ingest, scenario) ─────────────────────

def extract_risk_mda_sections(form: str, text: str, *, max_chars: int = 40000) -> dict[str, str]:
    """Extract risk_factors and mda from filing text, skipping TOC entries.

    The trick: TOC entries like 'Item 1A. Risk Factors  5' are short lines with a
    page number. The real section header is followed by substantial paragraph text.
    We find ALL occurrences and pick the one followed by real content (>200 chars
    before the next Item heading).
    """
    sections: dict[str, str] = {}

    configs = {
        "risk_factors": {
            "start": [
                r"\bitem\s*1a[\.\:\s]+risk\s+factors?\b",
                r"\brisk\s+factors?\s*\n",
            ],
            "end": [
                r"\bitem\s*1b\b",
                r"\bitem\s*2[\.\:\s]",
                r"\bunresolved\s+staff\s+comments\b",
            ],
        },
        "mda": {
            "start": [
                r"\bitem\s*7[\.\:\s]+management.?s\s+discussion\b",
                r"\bmanagement.?s\s+discussion\s+and\s+analysis\b",
                r"\bitem\s*2[\.\:\s]+management.?s\s+discussion\b",
            ],
            "end": [
                r"\nitem\s*7a[\.\:\s]",
                r"\nitem\s*8[\.\:\s]",
                r"\nitem\s*3[\.\:\s]+quantitative\b",
                r"\ncontrols\s+and\s+procedures\b",
            ],
        },
    }

    for key, cfg in configs.items():
        best_text = ""
        for pattern in cfg["start"]:
            for m in re.finditer(pattern, text, flags=re.I):
                start_pos = m.start()
                end_pos = min(start_pos + max_chars, len(text))
                remaining = text[start_pos + len(m.group()):]
                for ep in cfg["end"]:
                    em = re.search(ep, remaining, flags=re.I)
                    if em:
                        cand = start_pos + len(m.group()) + em.start()
                        if cand < end_pos:
                            end_pos = cand
                chunk = text[start_pos:end_pos].strip()
                # Skip TOC entries: real sections have >200 chars of content
                if len(chunk) > 200 and len(chunk) > len(best_text):
                    best_text = chunk
            if best_text:
                break
        sections[key] = best_text[:max_chars]

    return sections


def extract_filing_sections(form: str, text: str) -> dict[str, str]:
    """Extract all key sections from a filing (business, risk, mda, notes, etc).

    Superset of extract_risk_mda_sections — also extracts business overview,
    notes to financial statements, segment info, and proxy statement sections.
    """
    sections = extract_risk_mda_sections(form, text, max_chars=40000)

    fm = str(form or "").upper()
    if fm in {"10-K", "20-F", "40-F", "10-Q", "6-K"}:
        sections["business"] = _extract_between(
            text,
            [r"\bitem\s*1[\.\:\-\s]+business\b", r"\bbusiness overview\b"],
            [r"\bitem\s*1a\b", r"\bitem\s*2\b", r"\bitem\s*7\b", r"\bmanagement.?s discussion\b"],
            max_chars=10000,
        )
        sections["notes"] = _extract_between(
            text,
            [r"\bitem\s*8[\.\:\-\s]+financial statements", r"\bnotes to (?:the )?financial statements?\b"],
            [r"\bitem\s*9\b", r"\bitem\s*9a\b", r"\bcontrols and procedures\b"],
            max_chars=22000,
        )
        sections["segment_info"] = _extract_between(
            text,
            [r"\bsegment reporting\b", r"\bsegment information\b", r"\basc\s*280\b"],
            [r"\bnote\s+\d+\b", r"\bitem\s*9\b", r"\bcontrols and procedures\b"],
            max_chars=12000,
        )
    elif fm in {"DEF 14A", "DEF14A", "DEFA14A", "PRE 14A", "DEF 14C"}:
        sections["executive_compensation"] = _extract_between(
            text,
            [r"\bexecutive compensation\b", r"\bsummary compensation table\b", r"\bcompensation discussion\b"],
            [r"\bsecurity ownership\b", r"\brelated party transactions?\b", r"\baudit committee\b"],
            max_chars=18000,
        )
        sections["related_party_transactions"] = _extract_between(
            text,
            [r"\brelated party transactions?\b", r"\bcertain relationships and related transactions\b"],
            [r"\bproposal\b", r"\bsecurity ownership\b", r"\baudit committee\b", r"\bcompensation committee\b"],
            max_chars=12000,
        )

    return {k: v for k, v in sections.items() if str(v or "").strip()}


def _extract_between(text: str, start_patterns: list[str], end_patterns: list[str], max_chars: int = 12000) -> str:
    """Extract text between start and end regex patterns."""
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
    segment_low = low[start_idx + 1:]
    for p in end_patterns:
        m2 = re.search(p, segment_low, flags=re.I)
        if m2:
            cand = start_idx + 1 + m2.start()
            if cand > start_idx and cand < end_idx:
                end_idx = cand
    out = src[start_idx:end_idx].strip()
    return out[:max(800, int(max_chars or 12000))]


# ── Unified filing text reader ───────────────────────────────────────────────

def read_filing_text_any(path_s: str, *, max_chars: int | None = None) -> str:
    # 1. Local file (fastest — works in dev)
    p = resolve_filing_path(path_s)
    if p is not None:
        txt = p.read_text(encoding="utf-8", errors="ignore")
        return txt[:max_chars] if max_chars and max_chars > 0 else txt
    # 2. Postgres content column (primary path in Cloud Run — no local disk)
    txt = _read_content_from_db(path_s, max_chars=max_chars)
    if txt:
        return txt
    # 3. GCS fallback (if CLOUD_FILES_BUCKET is set)
    rel = normalize_filing_rel_path(path_s)
    if not rel:
        return ""
    return cloud_files.read_text(rel, max_chars=max_chars)
