from __future__ import annotations

import difflib
import os
import re
from typing import Callable

_SEC_FORM_ALIAS: dict[str, str] = {
    "6k": "6-K",
    "8k": "8-K",
    "10k": "10-K",
    "10q": "10-Q",
    "20f": "20-F",
    "40f": "40-F",
}
_SEC_SEARCH_WORDS: set[str] = {
    "sec",
    "filing",
    "filings",
    "earnings",
    "release",
    "releases",
    "report",
    "reports",
}
_SEC_HINT_TOKENS: set[str] = {
    "sec",
    "filing",
    "earnings",
    "10k",
    "10-k",
    "10q",
    "10-q",
    "8k",
    "8-k",
    "20f",
    "20-f",
    "40f",
    "40-f",
    "6k",
    "6-k",
}

GLOBAL_SEARCH_APP_ROUTES: list[tuple[str, str, str, str]] = [
    ("page", "Dashboard", "/dashboard", "Market overview"),
    ("page", "Portfolio / Universe", "/my_universe", "Holdings, watchlist, blue chips"),
    ("page", "Company Search", "/company_file", "Browse company files"),
    ("page", "Organizer", "/organizer", "Tasks, notes, timeline"),
    ("page", "Settings", "/observability", "System and observability"),
    ("page", "Reports", "/reports", "Generated reports"),
]


def search_tokens(raw_query: str) -> tuple[str, list[str], list[str]]:
    low = str(raw_query or "").strip().lower()
    toks = [t for t in re.split(r"\s+", low) if t]
    toks_norm = [re.sub(r"[^a-z0-9]+", "", t) for t in toks if t]
    return low, toks, toks_norm


def global_search_add_result(
    results: list[dict[str, str]],
    seen: set[str],
    kind: str,
    title: str,
    url: str,
    subtitle: str = "",
) -> None:
    u = str(url or "").strip()
    t = str(title or "").strip()
    if not u or not t:
        return
    key = f"{kind}|{u}|{t}".lower()
    if key in seen:
        return
    seen.add(key)
    results.append(
        {
            "kind": str(kind or "result"),
            "title": t[:120],
            "subtitle": str(subtitle or "").strip()[:180],
            "url": u,
        }
    )


def global_search_match(low: str, toks: list[str], toks_norm: list[str], *parts: object) -> bool:
    txt = " ".join(str(x or "") for x in parts).strip().lower()
    if not txt:
        return False
    if low in txt:
        return True
    # Normalize punctuation to support queries like "10k nvda" matching "10-K".
    txt_norm = re.sub(r"[^a-z0-9]+", "", txt)
    return bool(toks) and all((t in txt) or (tn and tn in txt_norm) for t, tn in zip(toks, toks_norm))


def global_search_add_task_row(
    row: object,
    *,
    low: str,
    toks: list[str],
    toks_norm: list[str],
    needle: str,
    results: list[dict[str, str]],
    seen: set[str],
    task_seen: set[int],
    quote_fn,
) -> None:
    rid = int((row.get("id") if isinstance(row, dict) else row["id"]) or 0)
    if rid > 0 and rid in task_seen:
        return
    task = str((row.get("task") if isinstance(row, dict) else row["task"]) or "").strip()
    status = str((row.get("status") if isinstance(row, dict) else row["status"]) or "").strip().lower()
    tk = str((row.get("ticker") if isinstance(row, dict) else row["ticker"]) or "").strip().upper()
    if not task or not global_search_match(low, toks, toks_norm, task, tk, status):
        return
    if rid > 0:
        task_seen.add(rid)
    if tk:
        global_search_add_result(results, seen, "task", task, f"/company_file?t={quote_fn(tk)}", f"Task - {tk} ({status or 'open'})")
    else:
        global_search_add_result(results, seen, "task", task, f"/organizer?q={quote_fn(needle)}", f"Task ({status or 'open'})")


def global_search_add_note_row(
    row: dict[str, object],
    *,
    low: str,
    toks: list[str],
    toks_norm: list[str],
    needle: str,
    results: list[dict[str, str]],
    seen: set[str],
    quote_fn,
) -> None:
    txt = str(row.get("text") or "").strip()
    tag = str(row.get("tag") or "").strip()
    tk = str(row.get("ticker") or "").strip().upper()
    if not txt or not global_search_match(low, toks, toks_norm, txt, tag, tk):
        return
    if tk:
        global_search_add_result(results, seen, "note", txt[:90], f"/company_file?t={quote_fn(tk)}", f"Note - {tk}")
    else:
        global_search_add_result(results, seen, "note", txt[:90], f"/organizer?q={quote_fn(needle)}", "Note")


def is_sec_like_query(low: str, toks: list[str]) -> bool:
    return any(t in _SEC_HINT_TOKENS for t in toks) or ("earnings release" in str(low or ""))


def sec_query_parts(toks: list[str], toks_norm: list[str]) -> tuple[set[str], str]:
    requested_forms = {_SEC_FORM_ALIAS[t] for t in toks_norm if t in _SEC_FORM_ALIAS}
    company_terms = [
        t for t, tn in zip(toks, toks_norm) if tn and tn not in _SEC_SEARCH_WORDS and tn not in _SEC_FORM_ALIAS
    ]
    return requested_forms, " ".join(company_terms).strip()


def resolve_company_tickers_for_search(
    company_query: str,
    list_companies_fn,
    limit: int = 8,
) -> list[str]:
    q = str(company_query or "").strip()
    if not q:
        return []
    out: list[str] = []
    try:
        crows = list_companies_fn(
            query=q,
            page=1,
            page_size=max(1, min(24, int(limit))),
            scope="all",
            sort="mcap_desc",
        ).get("rows") or []
        for r in crows:
            tk = str(getattr(r, "ticker", "") or (r.get("ticker") if isinstance(r, dict) else "") or "").strip().upper()
            if tk and tk not in out:
                out.append(tk)
    except Exception:
        return []
    return out


def parse_human_amount(raw: str, default: float = 0.0) -> float:
    s = str(raw or "").strip().lower().replace(",", "")
    if not s:
        return default
    mul = 1.0
    if s.endswith("k"):
        mul = 1_000.0
        s = s[:-1]
    elif s.endswith("m"):
        mul = 1_000_000.0
        s = s[:-1]
    elif s.endswith("b"):
        mul = 1_000_000_000.0
        s = s[:-1]
    try:
        return float(s) * mul
    except Exception:
        return default


def desk_parse(raw: str) -> tuple[str, str]:
    s = str(raw or "").strip()
    if not s:
        return "note", ""
    low = s.lower()
    if low.startswith("/ask "):
        return "ask", s[5:].strip()
    if low.startswith("ask:"):
        return "ask", s[4:].strip()
    if low.startswith("/task "):
        return "task", s[6:].strip()
    if low.startswith("task:"):
        return "task", s[5:].strip()
    if low.startswith("todo:"):
        return "task", s[5:].strip()
    if re.match(r".+\?$", s):
        return "ask", s
    return "note", s


def contains_fuzzy_term(text: str, term: str, min_ratio: float = 0.78) -> bool:
    low = str(text or "").strip().lower()
    t = str(term or "").strip().lower()
    if not low or not t:
        return False
    if t in low:
        return True
    if t == "watchlist" and "watch list" in low:
        return True
    toks = re.findall(r"[a-z]+", low)
    for tok in toks:
        if difflib.SequenceMatcher(None, tok, t).ratio() >= min_ratio:
            return True
    return False


def has_add_intent(text: str) -> bool:
    low = str(text or "").strip().lower()
    if re.search(r"\b(add|set|update|top\s*up|increase|put|buy|allocate|fund)\b", low):
        return True
    toks = re.findall(r"[a-z]+", low)
    add_words = ("add", "set", "update", "buy")
    for tok in toks:
        if any(difflib.SequenceMatcher(None, tok, w).ratio() >= 0.8 for w in add_words):
            return True
    return False


def has_remove_intent(text: str) -> bool:
    low = str(text or "").strip().lower()
    if re.search(r"\b(remove|delete|drop|sell|close)\b", low):
        return True
    toks = re.findall(r"[a-z]+", low)
    rem_words = ("remove", "delete", "drop", "sell")
    for tok in toks:
        if any(difflib.SequenceMatcher(None, tok, w).ratio() >= 0.8 for w in rem_words):
            return True
    return False


def looks_like_question(text: str) -> bool:
    s = str(text or "").strip().lower()
    if not s:
        return False
    if "?" in s:
        return True
    return bool(re.match(r"^(what|why|how|when|where|which|can|could|would|should|do|does|did|is|are)\b", s))


def starts_with_command_verb(text: str) -> bool:
    s = str(text or "").strip().lower()
    return bool(re.match(r"^(add|set|update|remove|delete|drop|buy|sell|close|fund|allocate|put)\b", s))


def is_mutating_command_mode(mode: str) -> bool:
    m = str(mode or "").strip().lower()
    return m in {
        "cash_upsert",
        "cash_remove",
        "watchlist_add",
        "watchlist_remove",
        "portfolio_upsert",
        "portfolio_remove",
        "bluechips_add",
        "bluechips_remove",
    }


def explicit_mutation_request(text: str) -> bool:
    s = str(text or "").strip().lower()
    return bool(re.match(r"^(?:/apply|apply)\b", s))


def mutation_guard_enabled() -> bool:
    v = str(os.getenv("AI_MUTATION_GUARD", "1")).strip().lower()
    return v not in {"0", "false", "no", "off"}


def allow_text_mutations() -> bool:
    v = str(os.getenv("ALLOW_TEXT_MUTATIONS", "0")).strip().lower()
    return v in {"1", "true", "yes", "on"}


def parse_ai_command(
    raw: str,
    infer_ticker_fn: Callable[[str], str],
    safe_ticker_fn: Callable[[str], str],
) -> dict[str, str] | None:
    s = str(raw or "").strip()
    if not s:
        return None
    low = re.sub(r"\s+", " ", s.lower()).strip()
    if looks_like_question(low) and not starts_with_command_verb(low):
        return None

    has_add = has_add_intent(low)
    has_remove = has_remove_intent(low)
    has_watchlist = contains_fuzzy_term(low, "watchlist")
    has_portfolio = contains_fuzzy_term(low, "portfolio")
    has_cash = contains_fuzzy_term(low, "cash")
    has_balance = contains_fuzzy_term(low, "balance")

    if has_cash:
        c_match = re.search(r"\b(usd|eur|gbp)\b", low)
        cur = str(c_match.group(1) if c_match else "USD").upper()
        a_match = re.search(r"\b(\d+(?:[.,]\d+)?\s*[kKmMbB]?)\b", s)
        amt = parse_human_amount(str(a_match.group(1) if a_match else ""), 0.0)
        if has_remove:
            return {"mode": "cash_remove", "currency": cur}
        if has_add and amt > 0:
            return {"mode": "cash_upsert", "currency": cur, "amount": f"{amt:g}"}

    if has_balance:
        c_match = re.search(r"\b(usd|eur|gbp)\b", low)
        cur = str(c_match.group(1) if c_match else "USD").upper()
        a_match = re.search(r"\b(\d+(?:[.,]\d+)?\s*[kKmMbB]?)\b", s)
        amt = parse_human_amount(str(a_match.group(1) if a_match else ""), 0.0)
        if has_remove:
            return {"mode": "cash_remove", "currency": cur}
        if has_add and amt > 0:
            return {"mode": "cash_upsert", "currency": cur, "amount": f"{amt:g}"}

    if has_watchlist:
        if has_remove:
            ticker = infer_ticker_fn(s)
            if ticker:
                return {"mode": "watchlist_remove", "text": "", "ticker": ticker}
        if has_add:
            ticker = infer_ticker_fn(s)
            if ticker:
                return {"mode": "watchlist_add", "text": "", "ticker": ticker}
        ticker = infer_ticker_fn(s)
        if ticker:
            return {"mode": "watchlist_add", "text": "", "ticker": ticker}

    if has_portfolio:
        ticker = infer_ticker_fn(s)
        if has_remove and ticker:
            return {"mode": "portfolio_remove", "text": "", "ticker": ticker, "shares": "0", "cost": "0"}
        if has_add and ticker:
            m_sh = re.search(r"(\d+(?:\.\d+)?)\s*shares?\b", low, flags=re.I)
            m_cost = re.search(r"(?:at|cost|price)\s*\$?\s*(\d+(?:\.\d+)?)", low, flags=re.I)
            m_any = re.search(r"\b(\d+(?:\.\d+)?)\b", low)
            shares = str(m_sh.group(1) if m_sh else (m_any.group(1) if m_any else "1"))
            cost = str(m_cost.group(1) if m_cost else "0")
            return {"mode": "portfolio_upsert", "text": "", "ticker": ticker, "shares": shares, "cost": cost}

    m_port_short = re.match(
        r"^\s*(?:please\s+)?(?:add|buy|set|update)\s+(?P<t>[A-Za-z.\-]{1,12})\s+(?P<sh>\d+(?:\.\d+)?)\s*(?:shares?)?\s*(?:at|@|price|cost)?\s*\$?\s*(?P<px>\d+(?:\.\d+)?)(?:\b.*)?$",
        s,
        flags=re.I,
    )
    if m_port_short:
        tk_guess = safe_ticker_fn(m_port_short.group("t") or "")
        tk = infer_ticker_fn(tk_guess) or tk_guess
        if tk:
            return {
                "mode": "portfolio_upsert",
                "text": "",
                "ticker": tk,
                "shares": str(m_port_short.group("sh") or "0"),
                "cost": str(m_port_short.group("px") or "0"),
            }

    if has_add:
        tk = infer_ticker_fn(s)
        if tk:
            nums = re.findall(r"\b(\d+(?:\.\d+)?)\b", low)
            if len(nums) >= 2:
                return {"mode": "portfolio_upsert", "text": "", "ticker": tk, "shares": nums[0], "cost": nums[1]}

    m_cash_add = re.match(
        r"^\s*(?:please\s+)?(?:add|set|update)\s+(?:(?P<a1>[\d.,]+[kKmMbB]?)\s*(?P<c1>[A-Za-z]{3})|(?P<c2>[A-Za-z]{3})\s*(?P<a2>[\d.,]+[kKmMbB]?))\s+cash(?:\b.*)?$",
        s,
        flags=re.I,
    )
    if not m_cash_add:
        m_cash_add = re.match(
            r"^\s*(?:please\s+)?(?:add|set|update)\s+(?P<a4>[\d.,]+[kKmMbB]?)\s+(?:to\s+)?(?:my\s+|the\s+)?cash(?:\s+balance)?(?:\s+in\s+(?P<c4>[A-Za-z]{3}))?(?:\b.*)?$",
            s,
            flags=re.I,
        )
    if not m_cash_add:
        m_cash_add = re.match(
            r"^\s*(?:please\s+)?(?:add|set|update)\s+cash\s+(?P<c3>[A-Za-z]{3})\s*(?P<a3>[\d.,]+[kKmMbB]?)(?:\b.*)?$",
            s,
            flags=re.I,
        )
    if m_cash_add:
        cur = str(
            m_cash_add.groupdict().get("c1")
            or m_cash_add.groupdict().get("c2")
            or m_cash_add.groupdict().get("c3")
            or m_cash_add.groupdict().get("c4")
            or "USD"
        ).strip().upper()
        amt_txt = str(
            m_cash_add.groupdict().get("a1")
            or m_cash_add.groupdict().get("a2")
            or m_cash_add.groupdict().get("a3")
            or m_cash_add.groupdict().get("a4")
            or ""
        ).strip()
        amt = parse_human_amount(amt_txt, 0.0)
        if amt <= 0:
            return None
        return {"mode": "cash_upsert", "currency": cur, "amount": f"{amt:g}"}

    m_cash_remove = re.match(
        r"^\s*(?:please\s+)?remove\s+(?:(?P<c1>[A-Za-z]{3})\s+)?cash(?:\s+(?P<c2>[A-Za-z]{3}))?(?:\b.*)?$",
        s,
        flags=re.I,
    )
    if m_cash_remove:
        cur = str(m_cash_remove.group("c1") or m_cash_remove.group("c2") or "USD").strip().upper()
        return {"mode": "cash_remove", "currency": cur}

    m_watch_remove = re.match(
        r"^\s*(?:please\s+)?remove\s+(?P<target>.+?)\s+from\s+(?:my\s+|the\s+)?watchlist(?:\b.*)?$",
        s,
        flags=re.I,
    )
    if m_watch_remove:
        target = str(m_watch_remove.group("target") or "").strip()
        ticker = infer_ticker_fn(target or s)
        if not ticker:
            return None
        return {"mode": "watchlist_remove", "text": "", "ticker": ticker}

    m_watch = re.match(
        r"^\s*(?:please\s+)?add\s+(?P<target>.+?)\s+to\s+(?:my\s+|the\s+)?watchlist(?:\b.*)?$",
        s,
        flags=re.I,
    )
    if m_watch:
        target = str(m_watch.group("target") or "").strip()
        ticker = infer_ticker_fn(target or s)
        if not ticker:
            return None
        return {"mode": "watchlist_add", "text": "", "ticker": ticker}

    m_port_remove = re.match(
        r"^\s*(?:please\s+)?remove\s+(?P<target>.+?)\s+from\s+(?:my\s+|the\s+)?portfolio(?:\b.*)?$",
        s,
        flags=re.I,
    )
    if m_port_remove:
        target = str(m_port_remove.group("target") or "").strip()
        ticker = infer_ticker_fn(target or s)
        if not ticker:
            return None
        return {"mode": "portfolio_remove", "text": "", "ticker": ticker, "shares": "0", "cost": "0"}

    m_port = re.match(
        r"^\s*(?:please\s+)?(?:add|update|set)\s+(?P<target>.+?)\s+to\s+(?:my\s+|the\s+)?portfolio(?:\s+(?P<rest>.*))?$",
        s,
        flags=re.I,
    )
    if m_port:
        target = str(m_port.group("target") or "").strip()
        ticker = infer_ticker_fn(target or s)
        if not ticker:
            return None
        rest = str(m_port.group("rest") or "").strip()
        shares = ""
        cost = ""
        m_sh = re.search(r"(\d+(?:\.\d+)?)\s*shares?\b", rest, flags=re.I)
        if m_sh:
            shares = str(m_sh.group(1) or "").strip()
        m_cost = re.search(r"(?:at|cost|price)\s*\$?\s*(\d+(?:\.\d+)?)", rest, flags=re.I)
        if m_cost:
            cost = str(m_cost.group(1) or "").strip()
        if not shares:
            m_num = re.search(r"\b(\d+(?:\.\d+)?)\b", rest)
            if m_num:
                shares = str(m_num.group(1) or "").strip()
        if not shares:
            shares = "1"
        if not cost:
            cost = "0"
        return {"mode": "portfolio_upsert", "text": "", "ticker": ticker, "shares": shares, "cost": cost}

    m = re.match(
        r"^\s*(?:please\s+)?(?:add|create|save|log|record|set)\s+(?:a\s+)?(?P<kind>note|task|todo|reminder)\b(?P<rest>.*)$",
        s,
        flags=re.I,
    )
    if not m:
        return None
    kind = str(m.group("kind") or "").strip().lower()
    if kind in {"task", "todo"}:
        mode = "task"
    elif kind == "reminder":
        mode = "reminder"
    else:
        mode = "note"
    rest = str(m.group("rest") or "").strip()
    if not rest:
        return None

    target = ""
    text = ""
    m2 = re.match(r"^(?:for|about|on)\s+(?P<target>[^:\-]+?)\s*(?::|\-)\s*(?P<body>.+)$", rest, flags=re.I)
    if m2:
        target = str(m2.group("target") or "").strip()
        text = str(m2.group("body") or "").strip()
    else:
        m2b = re.match(r"^(?:for|about|on)\s+(?P<target>[a-z0-9 .&'-]+?)\s+to\s+(?P<body>.+)$", rest, flags=re.I)
        if m2b:
            target = str(m2b.group("target") or "").strip()
            text = str(m2b.group("body") or "").strip()
        else:
            m3 = re.match(r"^(?::|\-)\s*(?P<body>.+)$", rest, flags=re.I)
            if m3:
                text = str(m3.group("body") or "").strip()
            else:
                m4 = re.match(r"^(?:for|about|on)\s+(?P<all>.+)$", rest, flags=re.I)
                if m4:
                    all_part = str(m4.group("all") or "").strip()
                    toks = all_part.split()
                    if len(toks) >= 4:
                        target = " ".join(toks[:2]).strip()
                        text = " ".join(toks[2:]).strip()
                    else:
                        target = all_part
                        text = ""
                else:
                    text = rest

    ticker = infer_ticker_fn(target or s)
    if not text:
        if ticker:
            default_name = "reminder" if mode == "reminder" else ("task" if mode == "task" else "note")
            text = f"{default_name} added via Ask AI"
        else:
            return None
    return {"mode": mode, "text": text, "ticker": ticker}


def is_op_like(text: str) -> bool:
    s = str(text or "")
    low = s.lower()
    topic = (
        contains_fuzzy_term(low, "cash")
        or contains_fuzzy_term(low, "balance")
        or contains_fuzzy_term(low, "watchlist")
        or contains_fuzzy_term(low, "portfolio")
    )
    action = has_add_intent(low) or has_remove_intent(low)
    if topic and action:
        return True
    return bool(
        re.search(
            r"\b(add|remove|set|update|buy|sell|allocate|fund|delete|drop)\b.*\b(cash|balance|watchlist|portfolio)\b|\b(cash|balance|watchlist|portfolio)\b.*\b(add|remove|set|update|buy|sell|allocate|fund|delete|drop)\b",
            s,
            flags=re.I,
        )
    )


def structured_capture_text(
    raw: str,
    mode: str = "note",
    ticker: str = "",
    *,
    infer_ticker_fn: Callable[[str], str],
    safe_ticker_fn: Callable[[str], str],
) -> str:
    src = str(raw or "").strip()
    md = str(mode or "note").strip().lower()
    tk = safe_ticker_fn(ticker)
    if not src:
        return src

    parts = [p.strip() for p in src.split("|") if p.strip()]
    company = tk
    kind = "task" if md == "task" else "note"
    summary = ""
    why = ""
    next_step = ""
    priority = ""

    key_map = {
        "why": "why",
        "reason": "why",
        "next": "next",
        "next step": "next",
        "action": "next",
        "priority": "priority",
        "prio": "priority",
        "type": "type",
        "kind": "type",
        "summary": "summary",
    }

    def _set_kv(k: str, v: str) -> None:
        nonlocal kind, summary, why, next_step, priority
        kk = key_map.get(k.strip().lower(), "")
        vv = v.strip()
        if not kk:
            return
        if kk == "type" and vv:
            kind = vv.lower()
        elif kk == "summary" and vv:
            summary = vv
        elif kk == "why":
            why = vv
        elif kk == "next":
            next_step = vv
        elif kk == "priority":
            priority = vv.upper()

    if len(parts) >= 2:
        free: list[str] = []
        for p in parts:
            if ":" in p:
                k, v = p.split(":", 1)
                _set_kv(k, v)
            else:
                free.append(p)
        if free:
            c0 = infer_ticker_fn(free[0])
            if c0:
                company = c0
                free = free[1:]
        if free:
            k0 = free[0].strip().lower()
            if k0 in {"task", "note", "idea", "risk", "question", "thesis"}:
                kind = k0
                free = free[1:]
        if free and not summary:
            summary = free[0].strip()
    else:
        summary = src

    if not company:
        company = infer_ticker_fn(src)
    if not summary:
        summary = src

    lines = [
        "[Structured Note]",
        f"Company: {company or '-'}",
        f"Type: {kind or ('task' if md == 'task' else 'note')}",
        f"Summary: {summary or '-'}",
        f"Why: {why or '-'}",
        f"Next: {next_step or '-'}",
        f"Priority: {priority or '-'}",
    ]
    return "\n".join(lines)
