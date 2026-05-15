from __future__ import annotations

import re
from difflib import SequenceMatcher

from app.core.normalize import normalize_text as _norm
from app.services.ai_orchestrator_constants import YES_NO_ONLY_RE
from app.services.ai_orchestrator_parsing import extract_ticker


def looks_like_fresh_command(query: str) -> bool:
    low = _norm(query)
    if not low:
        return False
    starts = (
        "add task",
        "task:",
        "todo:",
        "add note",
        "note:",
        "draft note",
        "open company",
        "show me my notes",
        "summarize",
        "summary",
        "summiraze",
        "find ",
        "search ",
        "open ",
        "go to ",
        "take me ",
        "take me to ",
        "where is ",
        "what page ",
        "main page",
        "report",
        "reports",
        "daily log",
        "add this to daily log",
    )
    return any(low.startswith(prefix) for prefix in starts)


def is_confirmation_text(text: str) -> bool:
    return bool(YES_NO_ONLY_RE.fullmatch(_norm(text)))


def looks_like_correction(text: str) -> bool:
    low = _norm(text)
    if not low:
        return False
    return bool(
        re.search(r"\b(not this|not that|instead|i meant|you should|dont do|don't do|wrong)\b", low)
        or re.search(r"\bthis is wrong\b", low)
    )


def has_objective_signal(low: str) -> bool:
    return bool(
        re.search(
            r"\b(compare|summari[sz]e|explain|analy[sz]e|decide|recommend|add|save|open|find|search|map|review|assess)\b",
            low,
        )
    )


def has_scope_signal(text: str) -> bool:
    query = str(text or "")
    low = _norm(query)
    if extract_ticker(query):
        return True
    return bool(
        re.search(
            r"\b(report|10-k|10-q|8-k|portfolio|watchlist|holdings|tasks|notes|company|industry|industries)\b",
            low,
        )
    )


def has_lens_signal(low: str) -> bool:
    return bool(
        re.search(
            r"\b(horizon|risk|low risk|medium risk|high risk|short term|long term|valuation|quality|growth|income)\b",
            low,
        )
    )


def contains_watchlist_term(text: str) -> bool:
    low = _norm(text)
    if not low:
        return False
    if "watchlist" in low or "watch list" in low or "market radar" in low:
        return True
    for token in re.findall(r"[a-z]+", low):
        if len(token) < 6:
            continue
        if SequenceMatcher(None, token, "watchlist").ratio() >= 0.72:
            return True
    return False


def contains_blue_chip_term(text: str) -> bool:
    low = _norm(text)
    if not low:
        return False
    if "blue chips" in low or "blue chip" in low or "bluechips" in low or "macro radar" in low:
        return True
    for token in re.findall(r"[a-z]+", low):
        if len(token) < 5:
            continue
        if SequenceMatcher(None, token, "bluechip").ratio() >= 0.72:
            return True
    return False


def contains_portfolio_term(text: str) -> bool:
    low = _norm(text)
    if not low:
        return False
    direct_terms = {"portfolio", "holdings", "active positions", "active position"}
    if any(term in low for term in direct_terms):
        return True
    for token in re.findall(r"[a-z]+", low):
        if len(token) < 5:
            continue
        if SequenceMatcher(None, token, "portfolio").ratio() >= 0.7:
            return True
        if SequenceMatcher(None, token, "holdings").ratio() >= 0.72:
            return True
    return False


def looks_like_portfolio_status_query(text: str) -> bool:
    low = _norm(text)
    if not low or not contains_portfolio_term(low):
        return False
    has_time_anchor = any(term in low for term in ("today", "daily", "day", "now", "current", "right now"))
    perf_terms = any(
        term in low
        for term in (
            "doing",
            "performance",
            "perform",
            "pnl",
            "up",
            "down",
            "return",
            "change",
            "status",
            "going",
            "happening",
        )
    )
    ask_pattern = bool(
        re.search(r"\bhow\s+is\s+my\b", low)
        or re.search(r"\bwhat\s+is\s+going\s+on\b", low)
        or re.search(r"\bwhat(?:'s|\s+is)?\s+happening\b", low)
        or re.search(r"\bwhat(?:'s|\s+is)?\s+the\s+status\b", low)
    )
    return (has_time_anchor and perf_terms) or ask_pattern


def looks_like_company_navigation_query(raw_query: str) -> bool:
    query = str(raw_query or "").strip()
    low = _norm(query)
    if not query or not low:
        return False
    if re.search(r"\b(open|show|go|take|navigate)\b.*\b(company|ticker)\b", low):
        return True
    if re.search(r"\b(company|ticker)\b.*\b(open|show|go|take|navigate)\b", low):
        return True
    if re.search(r"\bresearch\b.*\b(company|ticker)\b", low):
        return True
    if re.search(r"\$[A-Za-z]{1,5}\b", query):
        return True
    if re.search(r"\b(?:company|ticker)\s+\$?[A-Za-z]{1,5}\b", query, flags=re.I):
        return True
    return False
