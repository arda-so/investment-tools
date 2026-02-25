from __future__ import annotations

import datetime as dt

from app.core.config import ROOT
from app.core.ticker import normalize_ticker


WATCHLIST_PATH = ROOT / "data" / "my_watchlist.txt"


def read_watchlist_rows() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not WATCHLIST_PATH.exists():
        return out
    for ln in WATCHLIST_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = str(ln or "").strip()
        if not s or s.startswith("#"):
            continue
        parts = [x.strip() for x in s.split(",")]
        t = normalize_ticker(parts[0] if parts else "")
        if not t:
            continue
        out.append(
            {
                "ticker": t,
                "added_at": parts[1] if len(parts) >= 2 else "",
                "reason": parts[2] if len(parts) >= 3 else "",
            }
        )
    return out


def read_watchlist_tickers() -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for r in read_watchlist_rows():
        tk = normalize_ticker(str(r.get("ticker") or ""))
        if not tk or tk in seen:
            continue
        seen.add(tk)
        out.append(tk)
    return out


def write_watchlist_rows(rows: list[dict[str, str]]) -> None:
    WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# TICKER,ADDED_AT,REASON"]
    for r in rows:
        t = normalize_ticker(r.get("ticker", ""))
        if not t:
            continue
        added_at = str(r.get("added_at", "")).strip() or dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        reason = str(r.get("reason", "")).replace("\n", " ").strip()
        lines.append(",".join([t, added_at, reason]))
    WATCHLIST_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")

