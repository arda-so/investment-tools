from __future__ import annotations

import re


def normalize_text(
    text: str,
    *,
    is_ticker: bool = False,
    keep_extra: str = "",
    lowercase: bool = True,
    max_len: int = 0,
) -> str:
    s = str(text or "").strip()
    if is_ticker:
        s = s.upper()
        s = re.sub(r"[^A-Z0-9.\-]", "", s)
        return s[: (max_len if max_len > 0 else 12)]

    if lowercase:
        s = s.lower()
    keep = re.escape(str(keep_extra or ""))
    s = re.sub(rf"[^a-z0-9\s{keep}]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if max_len > 0:
        return s[:max_len]
    return s

