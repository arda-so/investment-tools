from __future__ import annotations

import re
from typing import Any

from app.core.normalize import normalize_text
from app.core.ticker import normalize_ticker


def parse_universe_command(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {"ok": False, "error": "Empty input."}

    low = normalize_text(raw)
    m: re.Match[str] | None

    # buy 20 aapl 185 rebalance
    m = re.match(
        r"^\s*(buy|sell)\s+([0-9]+(?:\.[0-9]+)?)\s+([a-z.\-]{1,12})(?:\s+([0-9]+(?:\.[0-9]+)?))?(?:\s+([a-z_][a-z0-9_ -]{0,60}))?\s*$",
        low,
        flags=re.IGNORECASE,
    )
    if m:
        side = str(m.group(1) or "").strip().lower()
        shares = str(m.group(2) or "").strip()
        ticker = normalize_ticker(m.group(3) or "")
        cost = str(m.group(4) or "").strip()
        reason = str(m.group(5) or "").strip().replace(" ", "_")
        if not ticker:
            return {"ok": False, "error": "Ticker required."}
        return {
            "ok": True,
            "type": "portfolio_upsert",
            "payload": {
                "ticker": ticker,
                "side": side,
                "shares": shares,
                "cost": (cost or "0"),
                "trade_reason": reason,
                "note": "",
            },
        }

    # exit aapl [reason]
    m = re.match(r"^\s*exit\s+([a-z.\-]{1,12})(?:\s+([a-z_][a-z0-9_ -]{0,60}))?\s*$", low, flags=re.IGNORECASE)
    if m:
        ticker = normalize_ticker(m.group(1) or "")
        reason = str(m.group(2) or "").strip().replace(" ", "_")
        if not ticker:
            return {"ok": False, "error": "Ticker required."}
        return {
            "ok": True,
            "type": "portfolio_upsert",
            "payload": {
                "ticker": ticker,
                "side": "sell",
                "shares": "0",
                "cost": "0",
                "trade_reason": reason,
                "note": "",
            },
        }

    # cash +5000 usd
    m = re.match(r"^\s*cash\s+([+\-]?[0-9]+(?:\.[0-9]+)?)(?:\s+([a-z]{3}))?\s*$", low, flags=re.IGNORECASE)
    if m:
        amount = str(m.group(1) or "").strip()
        currency = str(m.group(2) or "USD").strip().upper()
        return {"ok": True, "type": "cash_upsert", "payload": {"amount": amount, "currency": currency}}

    # watchlist add aapl / wl add aapl / add aapl to watchlist
    m = re.match(r"^\s*(?:watchlist|wl)\s+(add|remove)\s+([a-z.\-]{1,12})\s*$", low, flags=re.IGNORECASE)
    if m:
        action = str(m.group(1) or "").strip().lower()
        ticker = normalize_ticker(m.group(2) or "")
        if not ticker:
            return {"ok": False, "error": "Ticker required."}
        return {
            "ok": True,
            "type": ("watchlist_add" if action == "add" else "watchlist_remove"),
            "payload": {"ticker": ticker, "reason": ""},
        }
    m = re.match(r"^\s*add\s+([a-z.\-]{1,12})\s+to\s+watchlist\s*$", low, flags=re.IGNORECASE)
    if m:
        ticker = normalize_ticker(m.group(1) or "")
        if not ticker:
            return {"ok": False, "error": "Ticker required."}
        return {"ok": True, "type": "watchlist_add", "payload": {"ticker": ticker, "reason": ""}}
    m = re.match(r"^\s*remove\s+([a-z.\-]{1,12})\s+from\s+watchlist\s*$", low, flags=re.IGNORECASE)
    if m:
        ticker = normalize_ticker(m.group(1) or "")
        if not ticker:
            return {"ok": False, "error": "Ticker required."}
        return {"ok": True, "type": "watchlist_remove", "payload": {"ticker": ticker}}

    # bluechips add/remove
    m = re.match(r"^\s*(?:bluechips|blue_chips|bc)\s+(add|remove)\s+([a-z.\-]{1,12})\s*$", low, flags=re.IGNORECASE)
    if m:
        action = str(m.group(1) or "").strip().lower()
        ticker = normalize_ticker(m.group(2) or "")
        if not ticker:
            return {"ok": False, "error": "Ticker required."}
        return {
            "ok": True,
            "type": ("bluechips_add" if action == "add" else "bluechips_remove"),
            "payload": {"ticker": ticker, "reason": ""},
        }
    m = re.match(r"^\s*add\s+([a-z.\-]{1,12})\s+to\s+blue\s*chips\s*$", low, flags=re.IGNORECASE)
    if m:
        ticker = normalize_ticker(m.group(1) or "")
        if not ticker:
            return {"ok": False, "error": "Ticker required."}
        return {"ok": True, "type": "bluechips_add", "payload": {"ticker": ticker, "reason": ""}}
    m = re.match(r"^\s*remove\s+([a-z.\-]{1,12})\s+from\s+blue\s*chips\s*$", low, flags=re.IGNORECASE)
    if m:
        ticker = normalize_ticker(m.group(1) or "")
        if not ticker:
            return {"ok": False, "error": "Ticker required."}
        return {"ok": True, "type": "bluechips_remove", "payload": {"ticker": ticker}}

    return {"ok": False, "error": "Command not recognized."}
