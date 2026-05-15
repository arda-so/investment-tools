from __future__ import annotations

from app.core.ticker import normalize_ticker
from app.services.postgres_core_service import (
    list_blue_chips_pg,
    remove_blue_chip_pg,
    upsert_blue_chip_pg,
)

def list_blue_chips_rows(limit: int = 120) -> list[dict[str, str]]:
    lim = max(1, min(5000, int(limit or 120)))
    out: list[dict[str, str]] = []
    for r in list_blue_chips_pg(limit=lim):
        tk = normalize_ticker(str(r.get("ticker") or ""))
        if not tk:
            continue
        reason = str(r.get("reason") or "").strip()
        added_at = str(r.get("added_at") or "")
        out.append(
            {
                "ticker": tk,
                "reason": reason,
                "added_at": added_at,
                "name": reason,
            }
        )
    return out


def upsert_blue_chip(ticker: str, reason: str = "") -> bool:
    tk = normalize_ticker(ticker)
    if not tk:
        return False
    rsn = str(reason or "").strip()
    return bool(upsert_blue_chip_pg(tk, reason=rsn))


def remove_blue_chip(ticker: str) -> bool:
    tk = normalize_ticker(ticker)
    if not tk:
        return False
    return bool(remove_blue_chip_pg(tk))
