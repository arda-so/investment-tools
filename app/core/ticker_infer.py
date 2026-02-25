from __future__ import annotations

import difflib
import re


def infer_ticker_explicit(text: str, *, max_len: int = 5) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    m = re.search(rf"\$([A-Za-z]{{1,{max_len}}})\b", raw)
    if m:
        return str(m.group(1) or "").strip().upper()
    m = re.search(rf"\b(?:ticker|company)\s+\$?([A-Za-z]{{1,{max_len}}})\b", raw, flags=re.I)
    if m:
        return str(m.group(1) or "").strip().upper()
    return ""


def infer_ticker_from_aliases(text: str, aliases: list[tuple[str, str]]) -> str:
    s = re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(text or "").lower())).strip()
    if not s:
        return ""
    padded = f" {s} "
    for alias, ticker in aliases:
        if len(alias) < 4:
            continue
        if f" {alias} " in padded:
            return ticker
    words = [w for w in s.split(" ") if len(w) >= 6]
    if not words:
        return ""
    one_word_aliases = [a for a, _ in aliases if " " not in a and len(a) >= 6]
    alias_to_ticker = {a: t for a, t in aliases}
    for w in words[:12]:
        match = difflib.get_close_matches(w, one_word_aliases, n=1, cutoff=0.84)
        if match:
            ticker = str(alias_to_ticker.get(match[0]) or "")
            if ticker:
                return ticker
    return ""
