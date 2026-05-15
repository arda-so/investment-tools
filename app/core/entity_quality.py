from __future__ import annotations

import os
import re

from app.core.ticker import safe_ticker


_NOISE_COMPANY_TOKENS = {
    "A",
    "AN",
    "AND",
    "ARE",
    "AS",
    "AT",
    "BE",
    "BUT",
    "BY",
    "CAN",
    "CO",
    "FILED",
    "FOR",
    "FORM",
    "FROM",
    "HAS",
    "HAVE",
    "IN",
    "INTO",
    "IS",
    "IT",
    "ITS",
    "ITEM",
    "KEY",
    "MAY",
    "NOT",
    "OF",
    "ON",
    "OR",
    "OUR",
    "OVER",
    "RISK",
    "SECTION",
    "SUCH",
    "THAN",
    "THAT",
    "THE",
    "THEIR",
    "THESE",
    "THIS",
    "TO",
    "UNDER",
    "US",
    "WAS",
    "WE",
    "WERE",
    "WHICH",
    "WITH",
    "WOULD",
    "YEAR",
}


def entity_quality_gate_enabled() -> bool:
    return str(os.getenv("ONTOLOGY_ENTITY_QUALITY_GATE_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}


def is_valid_company_entity_name(name: str) -> bool:
    s = str(name or "").strip()
    if not s:
        return False
    up = re.sub(r"[^A-Z0-9.\-\s]", "", s.upper()).strip()
    if not up:
        return False
    tokens = [t for t in re.split(r"\s+", up) if t]
    if not tokens:
        return False
    if len(tokens) == 1 and tokens[0] in _NOISE_COMPANY_TOKENS:
        return False
    if all(t in _NOISE_COMPANY_TOKENS for t in tokens):
        return False
    return True


def clean_peer_ticker_candidate(raw: str) -> str:
    tk = safe_ticker(raw)
    if not tk:
        return ""
    if entity_quality_gate_enabled() and tk in _NOISE_COMPANY_TOKENS:
        return ""
    return tk

