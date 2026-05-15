from __future__ import annotations


def format_holdings_response_lines(rows: list[dict[str, object]], limit: int = 12) -> list[str]:
    lines: list[str] = []
    for row in rows[:limit]:
        lines.append(f"- {str(row.get('ticker') or '-')} ({float(row.get('shares') or 0.0):g} shares)")
    return lines


def format_position_timeline_lines(rows: list[dict[str, object]], limit: int = 6) -> list[str]:
    bullets: list[str] = []
    for row in rows[:limit]:
        ts = str(row.get("created_at") or "")[:16].replace("T", " ")
        note = str(row.get("note") or "").strip()
        note_suffix = f" | note: {note[:80]}" if note else ""
        bullets.append(
            f"- {ts} {str(row.get('action') or '').upper()} {float(row.get('shares') or 0.0):g} @ {float(row.get('price') or 0.0):.2f}{note_suffix}"
        )
    return bullets


def format_trade_history_summary(years: list[int], ticker: str, rows_by_year: dict[int, list[dict[str, object]]]) -> list[str]:
    scope = f" ({ticker})" if ticker else ""
    parts: list[str] = ["Trade history summary by year:"]
    for year in years[:8]:
        year_rows = rows_by_year.get(year) or []
        buys = sum(1 for row in year_rows if str(row.get("action") or "").lower() in {"buy", "add"})
        sells = sum(1 for row in year_rows if str(row.get("action") or "").lower() in {"sell", "trim"})
        parts.append(f"- {year}{scope}: rows={len(year_rows)}, buys={buys}, sells={sells}")
    return parts


def format_trade_history_detail(year: int, ticker: str, rows: list[dict[str, object]], top: list[tuple[str, int]], limit: int = 10) -> list[str]:
    scope = f" ({ticker})" if ticker else ""
    lines = [f"Trade history {year}{scope}: {len(rows)} rows."]
    if top:
        lines.append("Top tickers: " + ", ".join(f"{symbol} ({count})" for symbol, count in top))
    lines.append("Recent rows:")
    for row in rows[:limit]:
        ts = str(row.get("created_at") or "")[:16].replace("T", " ")
        lines.append(
            f"- {ts} {str(row.get('action') or '').upper()} {str(row.get('ticker') or '-')}"
            f" {float(row.get('shares') or 0.0):g} @ {float(row.get('price') or 0.0):.2f}"
        )
    return lines


def format_watchlist_rationale_lines(ticker: str, thesis: dict[str, object], decisions: list[dict[str, object]]) -> list[str]:
    lines = [f"Watchlist rationale for {ticker}:"]
    thesis_text = str(thesis.get("thesis") or "").strip()
    if thesis_text:
        lines.append(f"- Thesis: {thesis_text[:220]}")
    pick_method = str(thesis.get("pick_method") or "").strip()
    if pick_method:
        lines.append(f"- Pick method: {pick_method[:180]}")
    triggers = str(thesis.get("triggers") or "").strip()
    if triggers:
        lines.append(f"- Triggers: {triggers[:220]}")
    if decisions:
        latest = decisions[0]
        lines.append(
            f"- Latest decision: {str(latest.get('action') or '')} at {str(latest.get('created_at') or '')[:16].replace('T', ' ')}"
        )
        reason = str(latest.get("reason") or "").strip()
        if reason:
            lines.append(f"- Reason: {reason[:220]}")
    if len(lines) == 1:
        lines.append("- No rationale captured yet. Add a reason when adding to watchlist.")
    return lines
