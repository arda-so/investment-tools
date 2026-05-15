from __future__ import annotations

import re

from app.core.normalize import normalize_text as _norm
from app.services.ai_orchestrator_constants import YES_NO_ONLY_RE
from app.services.ai_orchestrator_parsing import extract_ticker
from app.services.ai_orchestrator_signals import (
    contains_blue_chip_term,
    contains_portfolio_term,
    contains_watchlist_term,
    looks_like_portfolio_status_query,
)
from app.services.ai_orchestrator_types import AICommandResult


def intent_navigation_followup(query: str, context: dict | None = None) -> AICommandResult | None:
    ctx = context or {}
    last = str(ctx.get("last_intent") or "").strip()
    target_by_intent = {
        "list_watchlist": ("/my_universe?tab=all", "Watchlist"),
        "list_portfolio": ("/my_universe?tab=all", "Portfolio"),
        "list_blue_chips": ("/my_universe?tab=bluechips", "Blue Chips"),
        "list_notes": ("/organizer", "Notes"),
        "list_reports": ("/reports", "Reports"),
        "list_tasks": ("/organizer", "Organizer"),
    }
    if last not in target_by_intent:
        return None
    low_q = _norm(query)
    command_like = any(
        token in low_q
        for token in {
            "show",
            "open",
            "list",
            "go",
            "take",
            "navigate",
            "what",
            "which",
            "tell",
            "my",
            "add",
            "remove",
            "delete",
            "create",
        }
    )
    analysis_like = bool(
        re.search(
            r"\b(compare|comparison|better|best|worse|vs|versus|business|moat|margin|growth|thesis|risk|valuation|quality)\b",
            low_q,
        )
    ) or bool(re.search(r"\b[A-Z]{2,6}\s+(or|vs|versus)\s+[A-Z]{2,6}\b", str(query or ""), flags=re.I))
    if analysis_like:
        return None
    domain_like = (
        contains_watchlist_term(low_q)
        or contains_blue_chip_term(low_q)
        or contains_portfolio_term(low_q)
        or any(
            token in low_q
            for token in {
                "notes",
                "note",
                "reports",
                "report",
                "organizer",
                "dashboard",
                "task",
                "tasks",
                "todo",
                "to-do",
                "to do",
                "morning",
                "update",
                "updates",
                "brief",
                "briefing",
            }
        )
        or bool(re.search(r"\bto[\s-]?do\b", low_q))
    )
    if command_like and domain_like:
        return None
    if last == "list_watchlist" and contains_watchlist_term(low_q):
        return None
    if last == "list_blue_chips" and contains_blue_chip_term(low_q):
        return None
    if last == "list_portfolio" and contains_portfolio_term(low_q):
        return None
    if last == "list_notes" and ("note" in low_q or "notes" in low_q):
        return None
    if last == "list_reports" and ("report" in low_q or "reports" in low_q):
        return None
    if last == "list_tasks" and re.search(r"\b(task|tasks|todo|to[\s-]?do)\b", low_q):
        return None
    if re.fullmatch(r"(yes|yeah|yep|sure|ok|okay|open|open it|open now|go ahead|do it)", low_q):
        redirect_url, label = target_by_intent[last]
        return AICommandResult(
            status="ok",
            intent="open_page",
            message=f"Opening {label}.",
            confidence=0.96,
            redirect_url=redirect_url,
            citations=[],
            traces=[{"step": "tool", "detail": "maps_to"}],
        )
    if re.fullmatch(r"(no|nope|nah|not now|later|cancel|back to chat)", low_q):
        return AICommandResult(
            status="ok",
            intent=last,
            message="Perfect, keeping it in chat only.",
            confidence=0.96,
            redirect_url="",
            citations=[],
            traces=[],
        )
    hist = ctx.get("history") if isinstance(ctx.get("history"), list) else []
    repeated_unclear = 0
    for item in reversed(hist[-20:]):
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "").strip().lower() != "assistant":
            continue
        if str(item.get("intent") or "").strip() != last:
            continue
        if str(item.get("status") or "").strip() != "needs_confirmation":
            continue
        message = str(item.get("text") or "")
        if "Please reply with `yes` to open the page" in message:
            repeated_unclear += 1
        else:
            break
    if repeated_unclear >= 1:
        return AICommandResult(
            status="needs_clarification",
            intent=last,
            message="I’m stuck in confirmation. Choose one: Open now, Cancel, or Back to chat.",
            confidence=0.96,
            redirect_url="",
            citations=[],
            traces=[],
        )
    return AICommandResult(
        status="needs_confirmation",
        intent=last,
        message="Please reply with `yes` to open the page or `no` to stay in chat.",
        confidence=0.96,
        redirect_url="",
        citations=[],
        traces=[],
    )


def manager_rule_intent(query: str) -> tuple[str, float]:
    low = _norm(query)
    if not low:
        return "", 0.0
    if YES_NO_ONLY_RE.fullmatch(low):
        return "", 0.0

    has_nav_verb = any(
        token in low
        for token in {
            "show",
            "list",
            "open",
            "go",
            "take",
            "navigate",
            "tell",
            "what",
            "which",
            "my",
        }
    )
    only_token = low in {
        "watchlist",
        "blue chips",
        "blue chip",
        "bluechips",
        "portfolio",
        "holdings",
        "notes",
        "reports",
        "tasks",
        "task",
        "todo",
        "to do",
        "to-do",
    }

    if looks_like_portfolio_status_query(low):
        return "portfolio_today_status", 0.98
    if contains_watchlist_term(low) and (has_nav_verb or only_token):
        return "list_watchlist", 0.97
    if contains_blue_chip_term(low) and (has_nav_verb or only_token):
        return "list_blue_chips", 0.97
    if contains_portfolio_term(low) and (has_nav_verb or only_token):
        return "list_portfolio", 0.97
    if ("earnings" in low or "calendar" in low) and (has_nav_verb or only_token):
        return "list_earnings", 0.97
    if re.search(r"\b(task|tasks|todo|to[\s-]?do)\b", low) and (has_nav_verb or only_token):
        return "list_tasks", 0.97
    if any(token in low for token in {"notes", "note"}) and (has_nav_verb or only_token):
        return "list_notes", 0.95
    if any(token in low for token in {"reports", "report"}) and (has_nav_verb or only_token):
        return "list_reports", 0.95
    if ("report" in low or "10-k" in low or "10-q" in low) and any(token in low for token in {"section", "part", "excerpt"}):
        return "report_section", 0.95
    if ("morning" in low and any(token in low for token in {"update", "updates", "brief", "briefing"})) or (
        "important" in low and "today" in low and ("morning" in low or "update" in low or "brief" in low)
    ):
        return "morning_updates", 0.96
    if re.search(r"\b(open|show|take|go|navigate)\b.*\bcompany\b", low) or re.search(r"\bcompany\b.*\b(open|show)\b", low):
        if extract_ticker(query):
            return "open_company", 0.93
    if re.search(r"\b(summarize|summary|recap|digest)\b.*\b(report|reports)\b", low):
        return "summarize_latest_report", 0.92
    if re.search(r"\b(find|search)\b.*\b(report|reports|10-k|10-q)\b", low):
        return "search_reports", 0.9
    if re.search(r"\b(what|which|summarize|summary|tell|how many)\b.*\b(note|notes)\b", low):
        return "notes_summary", 0.9
    if any(token in low for token in {"start portfolio interview", "interview mode", "start interview"}):
        return "portfolio_interview", 0.95
    return "", 0.0


def manager_rewrite_query(target_intent: str, original_query: str) -> str:
    low = _norm(original_query)
    if target_intent == "list_watchlist":
        if contains_watchlist_term(low):
            return "show me my watchlist"
    elif target_intent == "list_blue_chips":
        if contains_blue_chip_term(low):
            if any(token in low for token in {"add", "include", "put", "remove", "delete", "drop", "exclude"}):
                return original_query
            return "show me my blue chips"
    elif target_intent == "list_portfolio":
        if contains_portfolio_term(low):
            return "show me my portfolio"
    elif target_intent == "list_earnings":
        if "earnings" in low or "calendar" in low:
            return "show earnings calendar in chat"
    elif target_intent == "portfolio_today_status":
        if contains_portfolio_term(low):
            return "how is my portfolio today?"
    elif target_intent == "list_tasks":
        if re.search(r"\b(task|tasks|todo|to[\s-]?do)\b", low):
            return "tell me my to do and tasks"
    elif target_intent == "list_notes":
        if "note" in low:
            return "show me my notes"
    elif target_intent == "list_reports":
        if "report" in low:
            return "show me my reports"
    elif target_intent == "open_company":
        ticker = extract_ticker(original_query)
        if ticker:
            return f"open company {ticker}"
    elif target_intent == "notes_summary":
        if "note" in low:
            return "what are my notes today?"
    elif target_intent == "summarize_latest_report":
        if "report" in low:
            return "summarize latest report"
    elif target_intent == "search_reports":
        return original_query
    elif target_intent == "report_section":
        return original_query
    elif target_intent == "morning_updates":
        return "what is in the morning updates and what was important today?"
    return original_query
