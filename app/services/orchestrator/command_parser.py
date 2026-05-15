from __future__ import annotations

import re
from app.core.normalize import normalize_text as _norm

ENTITY_ALIASES = {
    "google": "GOOGL",
    "goog": "GOOGL",
    "alphabet": "GOOGL",
    "facebook": "META",
    "fb": "META",
    "meta": "META",
    "tesla": "TSLA",
    "amazon": "AMZN",
    "apple": "AAPL",
    "microsoft": "MSFT",
    "netflix": "NFLX",
    "nvidia": "NVDA",
    "tsmc": "TSM",
    "salesforce": "CRM",
    "hubspot": "HUBS",
    "adobe": "ADBE",
}

def _normalize_symbol_or_alias(token: str) -> str:
    raw = str(token or "").strip()
    if not raw:
        return ""
    low = _norm(raw)
    if low in ENTITY_ALIASES:
        return ENTITY_ALIASES[low]
    up = raw.upper()
    if up in ENTITY_ALIASES.values():
        return up
    # Accept direct ticker-style input; avoid treating 5-letter company words
    # (e.g. "apple") as a symbol unless aliased above.
    if re.fullmatch(r"[A-Za-z]{1,5}", raw):
        if raw.isupper() or len(raw) <= 4:
            return up
    return ""

def _resolve_ticker_from_company_name(low: str) -> str:
    try:
        from app.services.orchestrator.ticker_resolution import resolve_ticker_from_company_name
    except Exception:
        return ""
    try:
        return str(resolve_ticker_from_company_name(low) or "").strip().upper()
    except Exception:
        return ""

def _resolve_ticker_from_company_hint(s: str) -> str:
    try:
        from app.services.orchestrator.ticker_resolution import resolve_ticker_from_company_hint
    except Exception:
        return ""
    try:
        return str(resolve_ticker_from_company_hint(s) or "").strip().upper()
    except Exception:
        return ""

def extract_ticker(text: str) -> str:
    s = str(text or "")
    m = re.search(r"\b(?:company|ticker)\s+\$?([A-Za-z]{1,5})\b", s, flags=re.I)
    if m:
        cand = _normalize_symbol_or_alias(m.group(1))
        if cand:
            return cand
    m = re.search(r"\$([A-Za-z]{1,5})\b", s)
    if m:
        cand = _normalize_symbol_or_alias(m.group(1))
        if cand:
            return cand
    low = _norm(s)
    for alias, tk in ENTITY_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", low):
            return tk
    tk_from_db = _resolve_ticker_from_company_name(low)
    if tk_from_db:
        return tk_from_db
    tk_from_hint = _resolve_ticker_from_company_hint(s)
    if tk_from_hint:
        return tk_from_hint
    stop = {
        "OPEN", "COMPANY", "TICKER", "SHOW", "RESEARCH", "ADD", "TASK",
        "NOTE", "DRAFT", "SUMMARIZE", "LATEST", "REPORT", "REPORTS", "FIND",
        "SEARCH", "DELETE", "UPDATE", "THESIS", "READ", "BUY", "SELL",
        "HOLD", "WATCH", "WRITE", "LIST", "MY", "ME", "DAILY", "BRIEF",
        "BRIEFING", "LASTEST", "SUMMIRAZE", "THIS", "TO", "ABOUT", "LOG",
        "NOTES", "REMOVE", "FROM", "FOR", "THE", "ALL", "WHY", "WHAT",
        "WHEN", "WHO", "WHICH", "HOW", "IS", "ARE", "DO", "DID", "WE",
        "OUR", "IN", "ON", "AT", "BY", "SAVE", "SET",
        "OWN",
    }
    for tok in re.findall(r"\b([A-Z]{2,5})\b", s):
        if tok in stop:
            continue
        return tok
    return ""

def extract_tickers_bulk(text: str) -> list[str]:
    s = str(text or "")
    out: list[str] = []
    seen: set[str] = set()
    noise_tokens = {
        "YES", "NO", "OK", "OPEN", "NOW", "CANCEL", "BACK", "CHAT",
        "ME", "MY", "A", "THE", "AND", "OR", "BUT", "IT", "IN", "ON",
        "AT", "TO", "FOR", "OF", "WITH", "AS", "BY", "IS", "ARE", "WAS",
        "WERE", "BE", "BEEN", "BEING", "DO", "DOES", "DID", "HAVE", "HAS",
        "HAD", "THIS", "THAT", "THESE", "THOSE", "WHAT", "WHO", "WHICH",
        "WHY", "WHEN", "WHERE", "HOW", "ALL", "ANY", "SOME", "MANY", "FEW",
        "MORE", "MOST", "OTHER", "SUCH", "ONLY", "OWN", "SAME", "SO", "THAN",
        "TOO", "VERY", "CAN", "COULD", "WILL", "WOULD", "SHALL", "SHOULD",
        "MAY", "MIGHT", "MUST", "NOT", "JUST", "UP", "OUT", "ABOUT", "OVER",
        "AGAIN", "AGAINST", "BETWEEN", "INTO", "THROUGH", "DURING", "BEFORE",
        "AFTER", "ABOVE", "BELOW", "UNDER", "DOWN", "OFF", "OVER", "UNDER",
        "AGAIN", "FURTHER", "THEN", "ONCE", "HERE", "THERE", "WHERE", "WHY",
        "HOW", "ALL", "ANY", "BOTH", "EACH", "FEW", "MORE", "MOST", "OTHER",
        "SOME", "SUCH", "NO", "NOR", "NOT", "ONLY", "OWN", "SAME", "SO", "THAN",
        "TOO", "VERY", "SAY", "SAYS", "SAID", "SHALL",
    }
    for alias, tk in ENTITY_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", _norm(s)):
            if tk not in seen:
                seen.add(tk)
                out.append(tk)

    words = re.split(r"[\s,;:\.\!\?]+", s)
    for w in words:
        w = w.strip()
        if not w:
            continue
        if w.startswith("$") and len(w) > 1:
            cand = w[1:].upper()
            if re.match(r"^[A-Z]{1,5}$", cand):
                if cand not in seen:
                    seen.add(cand)
                    out.append(cand)
                continue
        if w.upper() == w and len(w) >= 2 and len(w) <= 5 and re.match(r"^[A-Z]+$", w):
            if w.upper() not in noise_tokens:
                if w.upper() not in seen:
                    seen.add(w.upper())
                    out.append(w.upper())

    low = _norm(s)
    chunks = low.split()
    for i in range(len(chunks) - 1):
        bigram = f"{chunks[i]} {chunks[i+1]}"
        db_tk = _resolve_ticker_from_company_name(bigram)
        if db_tk and db_tk not in seen:
            seen.add(db_tk)
            out.append(db_tk)

    if not out:
        cand = extract_ticker(s)
        if cand:
            out.append(cand)

    return out
