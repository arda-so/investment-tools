from __future__ import annotations

import json
import re
from typing import Any


DATA_UNAVAILABLE_MSG = "Data not available in structured filings."

_SEC_HINTS = (
    "10-k",
    "10-q",
    "8-k",
    "sec filing",
    "risk factors",
    "item 1a",
    "item 7",
    "mda",
    "management discussion",
)

_QUANT_EXTRACTION_HINTS = (
    "exact revenue breakdown",
    "revenue concentration",
    "margin trends",
    "operating margins",
    "extract",
    "table",
    "calculate",
    "numbers from raw",
    "segment reporting",
)

_STRUCTURED_HINTS = (
    "structured_financial_data_json",
    "mini_statements",
    "revenue_segments",
    "buyback",
    "provided with 100% accurate quantitative data in json",
)


def _low(s: str) -> str:
    return str(s or "").strip().lower()


def _looks_sec_quant_extraction(prompt: str, context: str) -> bool:
    text = _low(prompt) + "\n" + _low(context)
    sec_hit = any(h in text for h in _SEC_HINTS)
    quant_hit = any(h in text for h in _QUANT_EXTRACTION_HINTS)
    return sec_hit and quant_hit


def _has_structured_financial_data(prompt: str, context: str) -> bool:
    text = _low(prompt) + "\n" + _low(context)
    return any(h in text for h in _STRUCTURED_HINTS)


def _json_fallback_for_prompt(prompt: str) -> str:
    p = _low(prompt)
    if all(k in p for k in ("capital_allocation", "margin_trends", "revenue_concentration", "red_flags")):
        return json.dumps(
            {
                "capital_allocation": DATA_UNAVAILABLE_MSG,
                "margin_trends": DATA_UNAVAILABLE_MSG,
                "revenue_concentration": DATA_UNAVAILABLE_MSG,
                "red_flags": DATA_UNAVAILABLE_MSG,
                "confidence_score": 0,
                "missing_variables": [DATA_UNAVAILABLE_MSG],
                "citations": [],
            },
            ensure_ascii=True,
        )
    if all(k in p for k in ("summary_bullets", "risk_bullets", "confidence")):
        return json.dumps(
            {
                "summary_bullets": [DATA_UNAVAILABLE_MSG],
                "risk_bullets": [],
                "confidence": 0.0,
            },
            ensure_ascii=True,
        )
    if all(k in p for k in ("whats_happening", "connect_the_dots", "watch_next")):
        return json.dumps(
            {
                "whats_happening": [DATA_UNAVAILABLE_MSG],
                "connect_the_dots": [],
                "watch_next": [],
            },
            ensure_ascii=True,
        )
    return json.dumps({"message": DATA_UNAVAILABLE_MSG}, ensure_ascii=True)


def enforce_data_first_policy(prompt: str, context: str, *, json_mode: bool = False) -> tuple[bool, str]:
    """
    Returns (blocked, output_text).
    Blocks SEC quantitative extraction attempts that do not provide structured data.
    """
    if not _looks_sec_quant_extraction(prompt, context):
        return False, ""
    if _has_structured_financial_data(prompt, context):
        return False, ""
    if json_mode:
        return True, _json_fallback_for_prompt(prompt)
    return True, DATA_UNAVAILABLE_MSG


def has_legacy_sec_extraction_phrase(text: str) -> bool:
    s = _low(text)
    patterns = (
        r"exact revenue breakdown by product and geography",
        r"do not summarize text\.\s*answer the 4 required questions",
        r"buffett-style filing analyst",
    )
    return any(re.search(pat, s, flags=re.I) for pat in patterns)

