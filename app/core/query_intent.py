from __future__ import annotations

import re
from difflib import SequenceMatcher

from app.core.normalize import normalize_text


def contains_portfolio_term(text: str) -> bool:
    low = normalize_text(text)
    if not low:
        return False
    if any(k in low for k in {"portfolio", "holdings", "active positions", "active position"}):
        return True
    toks = re.findall(r"[a-z]+", low)
    for tk in toks:
        if len(tk) < 5:
            continue
        if SequenceMatcher(None, tk, "portfolio").ratio() >= 0.7:
            return True
        if SequenceMatcher(None, tk, "holdings").ratio() >= 0.72:
            return True
    return False


def looks_like_portfolio_status_query(text: str) -> bool:
    low = normalize_text(text)
    if not contains_portfolio_term(low):
        return False
    has_time_anchor = any(k in low for k in {"today", "daily", "day", "now", "current", "right now"})
    perf_terms = any(
        k in low
        for k in {"doing", "performance", "perform", "pnl", "up", "down", "return", "change", "status", "going", "happening"}
    )
    ask_pattern = bool(
        re.search(r"\bhow\s+is\s+my\b", low)
        or re.search(r"\bwhat\s+is\s+going\s+on\b", low)
        or re.search(r"\bwhat(?:'s|\s+is)?\s+happening\b", low)
        or re.search(r"\bwhat(?:'s|\s+is)?\s+the\s+status\b", low)
    )
    return (has_time_anchor and perf_terms) or ask_pattern

