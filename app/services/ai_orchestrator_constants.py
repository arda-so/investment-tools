from __future__ import annotations

import re

from app.core.config import ROOT

ORCHESTRATOR_VERSION = "v1.2.0"
FUZZY_THRESHOLD = 0.74
OPEN_NOTES_FUZZY_THRESHOLD = 0.72
LOW_CONFIDENCE_THRESHOLD = 0.45
MID_CONFIDENCE_THRESHOLD = 0.70
LLM_FALLBACK_TIMEOUT_SEC = 6.0
MANAGER_LLM_TIMEOUT_SEC = 1.2
MANAGER_INTERRUPT_THRESHOLD = 0.82
ANALYST_MAX_CONTEXT_TOKENS = 800_000

WEEKDAY_TO_INT = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

ENTITY_ALIASES = {
    "google": "GOOGL",
    "alphabet": "GOOGL",
    "facebook": "META",
    "meta": "META",
    "tesla": "TSLA",
    "amazon": "AMZN",
    "apple": "AAPL",
    "microsoft": "MSFT",
    "netflix": "NFLX",
    "nvidia": "NVDA",
    "salesforce": "CRM",
    "hubspot": "HUBS",
    "adobe": "ADBE",
}

WATCHLIST_PATH = ROOT / "data" / "my_watchlist.txt"

WORKER_INTENTS = {
    "portfolio_interview",
    "list_watchlist",
    "list_portfolio",
    "list_blue_chips",
    "list_earnings",
    "list_notes",
    "list_reports",
    "list_tasks",
}

INTERRUPTIBLE_INTENTS = WORKER_INTENTS | {
    "summarize_current_report",
    "summarize_latest_report",
    "report_section",
    "notes_summary",
    "open_notes",
    "open_company",
    "search_reports",
    "morning_updates",
    "portfolio_today_status",
}

YES_NO_ONLY_RE = re.compile(
    r"^\s*(yes|yeah|yep|sure|ok|okay|open|open it|go ahead|do it|no|nope|nah|not now|later|cancel)\s*$"
)
