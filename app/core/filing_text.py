from __future__ import annotations

import re
from pathlib import Path

from app.core.config import ROOT


def resolve_filing_path(path_s: str) -> Path | None:
    raw = str(path_s or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (ROOT / p).resolve()
    else:
        p = p.resolve()
    allowed = [ROOT / "filings", ROOT / "filing_docs", ROOT / "reports"]
    if not any(str(p).startswith(str(a.resolve())) for a in allowed):
        return None
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

