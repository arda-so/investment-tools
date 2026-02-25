from __future__ import annotations

import re


def safe_ticker(raw: str) -> str:
    s = re.sub(r"[^A-Z0-9.\-]", "", str(raw or "").strip().upper())
    if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,11}", s):
        return s
    return ""


def safe_ticker_flexible(raw: str) -> str:
    s = re.sub(r"[^A-Z0-9.\-]", "", str(raw or "").strip().upper())
    if re.fullmatch(r"[A-Z0-9][A-Z0-9.\-]{0,11}", s) and re.search(r"[A-Z]", s):
        return s
    return ""


def normalize_ticker(raw: str) -> str:
    return re.sub(r"[^A-Z0-9.\-]", "", str(raw or "").strip().upper())[:12]


def yfinance_symbol(raw: str) -> str:
    """
    Convert internal ticker format to Yahoo symbol format.

    Rules:
    - Keep exchange suffix dots for non-US symbols (e.g. AIR.PA, ADS.DE, 005930.KS).
    - Convert US share-class dot to dash (e.g. BRK.B -> BRK-B, BF.B -> BF-B).
    """
    t = normalize_ticker(str(raw or "").replace("$", ""))
    if "." not in t:
        return t
    head, tail = t.rsplit(".", 1)
    # Exchange suffixes are typically 2-5 chars (PA, DE, KS, LON, ...).
    # Single-letter suffixes are usually US share classes.
    if 2 <= len(tail) <= 5:
        return t
    return f"{head}-{tail}"
