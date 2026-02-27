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
