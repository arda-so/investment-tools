from __future__ import annotations

import re

from app.core.ticker import safe_ticker_flexible as _safe_ticker
from app.services.ai_orchestrator_constants import WATCHLIST_PATH


def read_watchlist_companies(limit: int = 120) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not WATCHLIST_PATH.exists():
        return out
    try:
        for line in WATCHLIST_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
            raw = str(line or "").strip()
            if not raw or raw.startswith("#"):
                continue
            parts = [part.strip() for part in raw.split(",")]
            ticker = _safe_ticker(parts[0] if parts else "")
            if not ticker:
                match = re.search(r"\b([A-Z][A-Z0-9.\-]{0,11})\b", raw.upper())
                ticker = _safe_ticker(match.group(1) if match else "")
            if not ticker:
                continue
            name = ""
            if len(parts) > 2:
                name = str(parts[2] or "").strip()
            elif len(parts) > 1:
                name = str(parts[1] or "").strip()
            out.append({"ticker": ticker, "name": name})
            if len(out) >= max(1, min(500, int(limit))):
                break
    except Exception:
        return []
    return out


def is_info_request(low: str) -> bool:
    return bool(
        re.search(
            r"\b(what|which|why|important|today|update|updates|summary|summarize|explain|tell me)\b",
            str(low or ""),
        )
    )


def format_watchlist_lines(rows: list[dict[str, str]], limit: int = 30) -> list[str]:
    lines = ["Your watchlist companies:"]
    for row in rows[:limit]:
        ticker = str(row.get("ticker") or "").strip().upper()
        name = str(row.get("name") or "").strip()
        lines.append(f"- {ticker}" + (f" — {name}" if name else ""))
    if len(rows) > limit:
        lines.append(f"- ...and {len(rows) - limit} more")
    return lines


def format_blue_chip_lines(rows: list[dict[str, object]], limit: int = 30) -> list[str]:
    lines = ["Your blue chips companies:"]
    for row in rows[:limit]:
        ticker = str(row.get("ticker") or "").strip().upper()
        name = str(row.get("name") or "").strip()
        lines.append(f"- {ticker}" + (f" — {name}" if name else ""))
    if len(rows) > limit:
        lines.append(f"- ...and {len(rows) - limit} more")
    return lines


def format_portfolio_lines(rows: list[dict[str, object]], limit: int = 20) -> list[str]:
    lines = ["Your portfolio holdings:"]
    for row in rows[:limit]:
        ticker = str(row.get("ticker") or "").strip().upper()
        shares = float(row.get("shares") or 0.0)
        lines.append(f"- {ticker} ({shares:g} shares)")
    if len(rows) > limit:
        lines.append(f"- ...and {len(rows) - limit} more")
    return lines


def format_note_lines(rows: list[dict[str, object]], limit: int = 6) -> list[str]:
    lines = ["Your recent notes:"]
    for row in rows[:limit]:
        ticker = str(row.get("ticker") or "").strip().upper()
        text = str(row.get("text") or "").strip().replace("\n", " ")
        head = f"[{ticker}] " if ticker else ""
        lines.append(f"- {head}{text[:120]}")
    return lines


def format_report_lines(rows: list[dict[str, object]], limit: int = 5) -> list[str]:
    lines = ["Recent reports:"]
    for row in rows[:limit]:
        name = str(row.get("name") or "").strip()
        modified_at = str(row.get("modified_at") or "").replace("T", " ")[:16]
        lines.append(f"- {name} ({modified_at})")
    return lines


def format_task_lines(rows: list[dict[str, object]], limit: int = 20) -> list[str]:
    lines = ["Your open tasks and to-do items:"]
    for row in rows[:limit]:
        row_id = int(row.get("id") or 0)
        task = str(row.get("task") or "").strip().replace("\n", " ")
        due = str(row.get("due_date") or "").strip()
        ticker = str(row.get("ticker") or "").strip().upper()
        task_line = f"- {task}"
        if ticker:
            task_line += f" [{ticker}]"
        if due:
            task_line += f" (due {due})"
        if row_id > 0:
            task_line += f" [#{row_id}]"
        lines.append(task_line)
    if len(rows) > limit:
        lines.append(f"- ...and {len(rows) - limit} more")
    return lines
