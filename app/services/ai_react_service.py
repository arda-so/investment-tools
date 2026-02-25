from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from app.core.config import ROOT
from app.core.normalize import normalize_text
from app.core.query_intent import contains_portfolio_term, looks_like_portfolio_status_query
from app.core.ticker import safe_ticker
from app.services.organizer_service import list_recent_notes, list_tasks
from app.services.portfolio_memory_service import (
    get_cached_morning_brief,
    get_live_portfolio_summary,
    get_holdings,
    query_10k_risk_map,
    query_report_facts,
    save_morning_brief_snapshot,
)
from app.services.proactive_ai_service import simulate_macro_shock, simulate_macro_shock_batch
from app.services.reports_service import list_reports, read_report_file
try:
    from tools.llm_engine import ask_ai
except Exception:  # pragma: no cover
    ask_ai = None  # type: ignore[assignment]


WATCHLIST_PATH = ROOT / "data" / "my_watchlist.txt"


def _is_info_query(low: str) -> bool:
    return bool(
        re.search(
            r"\b(what|which|why|important|today|update|updates|summary|summarize|explain|tell me|simulate|scenario|what if|shock)\b",
            str(low or ""),
        )
    )


def _is_open_request(low: str) -> bool:
    return bool(re.search(r"\b(open|go|take|navigate|redirect)\b", str(low or "")))


def _contains_portfolio_term(text: str) -> bool:
    return contains_portfolio_term(text)


def _looks_like_portfolio_status_query(text: str) -> bool:
    return looks_like_portfolio_status_query(text)


def _row_value(row: Any, key: str, default: Any = "") -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]  # sqlite3.Row style access
    except Exception:
        return default


def _tool_read_morning_brief(_args: dict[str, Any]) -> dict[str, Any]:
    cached = get_cached_morning_brief()
    if isinstance(cached, dict) and cached:
        return {"source": "cache", "brief": cached}
    return {"source": "fresh", "brief": save_morning_brief_snapshot(limit_holdings=5, source="on_demand")}


def _tool_read_watchlist(_args: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, str]] = []
    p = Path(WATCHLIST_PATH)
    if p.exists():
        try:
            for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                s = str(ln or "").strip()
                if not s or s.startswith("#"):
                    continue
                parts = [x.strip() for x in s.split(",")]
                tk = safe_ticker(parts[0] if parts else "")
                if not tk:
                    m = re.search(r"\b([A-Z][A-Z0-9.\-]{0,11})\b", s.upper())
                    tk = safe_ticker(m.group(1) if m else "")
                if not tk:
                    continue
                nm = str(parts[2] or "").strip() if len(parts) > 2 else (str(parts[1] or "").strip() if len(parts) > 1 else "")
                rows.append({"ticker": tk, "name": nm})
                if len(rows) >= 80:
                    break
        except Exception:
            rows = []
    return {"rows": rows}


def _tool_get_holdings(_args: dict[str, Any]) -> dict[str, Any]:
    return {"rows": get_holdings(limit=60)}


def _tool_get_live_portfolio_summary(_args: dict[str, Any]) -> dict[str, Any]:
    return dict(get_live_portfolio_summary() or {})


def _tool_list_notes(_args: dict[str, Any]) -> dict[str, Any]:
    return {"rows": list_recent_notes(limit=12)}


def _tool_list_tasks(_args: dict[str, Any]) -> dict[str, Any]:
    return {"rows": list_tasks(open_only=True, limit=30)}


def _tool_list_reports(_args: dict[str, Any]) -> dict[str, Any]:
    return {"rows": list_reports(limit=8)}


def _tool_read_latest_report(_args: dict[str, Any]) -> dict[str, Any]:
    section = str((_args or {}).get("section") or "").strip()
    rows = list_reports(limit=40)
    if not rows:
        return {"name": "", "title": "", "text": "", "url": "", "section": section}
    top = rows[0]
    name = str(top.get("name") or "").strip()
    if not name:
        return {"name": "", "title": "", "text": "", "url": "", "section": section}
    txt, err = read_report_file(name, max_chars=120000)
    if err:
        return {"name": name, "title": str(top.get("title") or name), "text": "", "url": f"/reports/view?name={name}", "section": section}
    return {
        "name": name,
        "title": str(top.get("title") or name),
        "text": txt,
        "url": f"/reports/view?name={name}",
        "section": section,
    }


def _tool_read_current_report(args: dict[str, Any]) -> dict[str, Any]:
    name = str((args or {}).get("current_report_name") or "").strip()
    section = str((args or {}).get("section") or "").strip()
    if not name:
        return {"name": "", "title": "", "text": "", "url": "", "section": section}
    txt, err = read_report_file(name, max_chars=120000)
    if err:
        return {"name": name, "title": name, "text": "", "url": f"/reports/view?name={name}", "section": section}
    return {"name": name, "title": name, "text": txt, "url": f"/reports/view?name={name}", "section": section}


def _latest_earnings_source() -> tuple[Path | None, str]:
    reports_dir = ROOT / "reports"
    if not reports_dir.exists():
        return None, ""
    cands: list[Path] = []
    for pat in ("earnings_radar_*.md", "terminal_appendix_*.md"):
        cands.extend(reports_dir.glob(pat))
    cands = [p for p in cands if p.is_file()]
    if not cands:
        return None, ""
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    p = cands[0]
    try:
        txt = p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return p, ""
    return p, txt


def _extract_earnings_rows(txt: str, row_limit: int = 20) -> list[dict[str, str]]:
    lines = [str(x or "").rstrip() for x in str(txt or "").splitlines()]
    if not lines:
        return []
    rows: list[dict[str, str]] = []
    in_section = False
    cur_date = ""
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        if re.match(r"^##\s+", s):
            if re.search(r"earnings\s+calendar", s, flags=re.I):
                in_section = True
                continue
            if in_section:
                break
        if not in_section:
            continue
        md = re.search(r"(\d{4}-\d{2}-\d{2})", s)
        if md:
            cur_date = md.group(1)
            continue
        m1 = re.match(r"^(Pre|Post|Day|BMO|AMC)\s+([A-Za-z0-9.\-]+)\s*[•\-]\s*(.+)$", s, flags=re.I)
        if m1:
            rows.append(
                {
                    "date": cur_date,
                    "time": str(m1.group(1) or "").upper(),
                    "symbol": str(m1.group(2) or "").upper(),
                    "company": str(m1.group(3) or "").strip(),
                }
            )
            if len(rows) >= row_limit:
                break
    return rows[:row_limit]


def _tool_read_earnings_calendar(args: dict[str, Any]) -> dict[str, Any]:
    refresh = bool((args or {}).get("refresh"))
    refresh_status = ""
    refresh_detail = ""
    if refresh:
        script = ROOT / "bin" / "run_earnings"
        if script.exists():
            try:
                proc = subprocess.run(
                    [str(script)],
                    cwd=str(ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=150,
                    check=False,
                )
                refresh_status = "ok" if int(proc.returncode or 1) == 0 else "failed"
                if refresh_status != "ok":
                    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                    refresh_detail = detail[-1][:180] if detail else "refresh_failed"
            except Exception:
                refresh_status = "failed"
                refresh_detail = "exception"
    p, txt = _latest_earnings_source()
    rows = _extract_earnings_rows(txt, row_limit=24)
    if not rows and not refresh:
        # Self-recover once before returning empty.
        script = ROOT / "bin" / "run_earnings"
        if script.exists():
            try:
                proc = subprocess.run(
                    [str(script)],
                    cwd=str(ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=150,
                    check=False,
                )
                refresh_status = "ok" if int(proc.returncode or 1) == 0 else "failed"
                if refresh_status != "ok":
                    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
                    refresh_detail = detail[-1][:180] if detail else "refresh_failed"
                p, txt = _latest_earnings_source()
                rows = _extract_earnings_rows(txt, row_limit=24)
            except Exception:
                refresh_status = "failed"
                refresh_detail = "exception"
    return {"rows": rows, "source": (p.name if p else "-"), "refresh_status": refresh_status, "refresh_detail": refresh_detail}


def _tool_report_facts(args: dict[str, Any]) -> dict[str, Any]:
    q = str((args or {}).get("query") or "").strip()
    tickers = list((args or {}).get("tickers") or [])
    return {"rows": query_report_facts(query=q, tickers=tickers, limit=16)}


def _tool_10k_risk_map(args: dict[str, Any]) -> dict[str, Any]:
    q = str((args or {}).get("query") or "").strip()
    tickers = list((args or {}).get("tickers") or [])
    return {"rows": query_10k_risk_map(query=q, tickers=tickers, limit_reports=100, per_ticker_limit=5)}


def _tool_simulate_macro_shock(args: dict[str, Any]) -> dict[str, Any]:
    event_description = str((args or {}).get("event_description") or "").strip()
    max_depth = int((args or {}).get("max_depth") or 3)
    return simulate_macro_shock(event_description=event_description, max_depth=max_depth)


def _tool_simulate_macro_shock_batch(args: dict[str, Any]) -> dict[str, Any]:
    scenarios = [str(x or "").strip() for x in list((args or {}).get("scenarios") or []) if str(x or "").strip()]
    max_depth = int((args or {}).get("max_depth") or 3)
    top_n = int((args or {}).get("top_n") or 5)
    return simulate_macro_shock_batch(scenarios=scenarios, max_depth=max_depth, top_n=top_n)


TOOL_REGISTRY: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "read_morning_briefing": _tool_read_morning_brief,
    "read_watchlist": _tool_read_watchlist,
    "get_holdings": _tool_get_holdings,
    "get_live_portfolio_summary": _tool_get_live_portfolio_summary,
    "list_notes": _tool_list_notes,
    "list_tasks": _tool_list_tasks,
    "list_reports": _tool_list_reports,
    "read_latest_report": _tool_read_latest_report,
    "read_current_report": _tool_read_current_report,
    "read_earnings_calendar": _tool_read_earnings_calendar,
    "report_facts": _tool_report_facts,
    "risk_map_10k": _tool_10k_risk_map,
    "simulate_macro_shock": _tool_simulate_macro_shock,
    "simulate_macro_shock_batch": _tool_simulate_macro_shock_batch,
}


def _plan(query: str, context: dict[str, Any] | None = None) -> dict[str, Any] | None:
    low = normalize_text(query, keep_extra="_-")
    ctx = context or {}
    hist = ctx.get("history") if isinstance(ctx.get("history"), list) else []
    recent = " ".join(
        str(m.get("text") or "").strip().lower()
        for m in (hist[-8:] if isinstance(hist, list) else [])
        if isinstance(m, dict)
    )
    if not low or _is_open_request(low):
        return None
    # Conversational follow-up resolver: short, context-dependent replies.
    if len(low.split()) <= 6 and any(k in low for k in {"list", "get the list", "show list", "here", "in chat"}):
        if "earnings" in recent or "calendar" in recent:
            return {
                "intent": "earnings_calendar_chat",
                "tools": ["read_earnings_calendar"],
                "args": {"refresh": False},
            }
    if re.search(r"\b(simulate|scenario|what if|shock|stress test)\b", low):
        # Batch mode: "run 3 scenarios: ...; ...; ..."
        m = re.search(r"(?:scenarios?\s*:)(.+)$", str(query or ""), flags=re.I)
        if m:
            raw = str(m.group(1) or "").strip()
            parts = [p.strip() for p in re.split(r"[;\n]+", raw) if p.strip()]
            if len(parts) >= 2:
                return {
                    "intent": "macro_shock_batch",
                    "tools": ["simulate_macro_shock_batch"],
                    "args": {"scenarios": parts[:12], "max_depth": 3, "top_n": 6},
                }
        return {
            "intent": "macro_shock_simulation",
            "tools": ["simulate_macro_shock"],
            "args": {"event_description": query, "max_depth": 3},
        }
    # Morning brief is always informational unless user explicitly asks to open/navigate.
    # This prevents it from falling back into confirmation/navigation loops.
    if ("morning" in low and any(k in low for k in {"update", "updates", "brief", "briefing"})) or (
        "important" in low and "today" in low and ("morning" in low or "update" in low or "brief" in low)
    ):
        return {"intent": "morning_updates", "tools": ["read_morning_briefing"]}
    if "risk" in low and any(k in low for k in {"10-k", "10k", "risk factors", "map", "industry", "industries"}):
        return {"intent": "risk_map_10k", "tools": ["risk_map_10k"], "args": {"query": query}}
    if "earnings" in low and any(k in low for k in {"calendar", "this week", "today", "show", "list", "here", "chat", "update", "refresh"}):
        return {
            "intent": "earnings_calendar_chat",
            "tools": ["read_earnings_calendar"],
            "args": {"refresh": bool(any(k in low for k in {"update", "refresh", "sync", "reload"}))},
        }
    if any(k in low for k in {"important today", "daily report", "daily reports", "earnings results"}):
        return {"intent": "report_facts_summary", "tools": ["report_facts"], "args": {"query": query}}
    if _looks_like_portfolio_status_query(low):
        return {"intent": "portfolio_today_status", "tools": ["get_live_portfolio_summary"]}
    if not _is_info_query(low):
        return None

    if "watchlist" in low or "watch list" in low:
        return {"intent": "list_watchlist", "tools": ["read_watchlist"]}
    if _contains_portfolio_term(low):
        return {"intent": "list_portfolio", "tools": ["get_holdings"]}
    if re.search(r"\b(task|tasks|todo|to[\s-]?do)\b", low):
        return {"intent": "list_tasks", "tools": ["list_tasks"]}
    if "report" in low and any(k in low for k in {"summarize", "summary", "explain", "analysis", "breakdown", "important", "what"}):
        current = str(ctx.get("current_report_name") or "").strip()
        if current and any(k in low for k in {"this", "it", "current", "opened", "open"}):
            return {"intent": "summarize_current_report", "tools": ["read_current_report"], "args": {"current_report_name": current}}
        return {"intent": "summarize_latest_report", "tools": ["read_latest_report"]}
    if ("report" in low or "10-k" in low or "10-q" in low) and any(k in low for k in {"section", "part", "excerpt"}):
        section = ""
        m = re.search(r"[\"“](.+?)[\"”]", str(query or ""))
        if m:
            section = str(m.group(1) or "").strip()
        if not section:
            m2 = re.search(r"\b(section|part|excerpt|focus|about|on)\s+(.+?)(?:\s+\b(from|in)\b.*|$)", str(query or ""), flags=re.I)
            if m2:
                section = str(m2.group(2) or "").strip()
        current = str(ctx.get("current_report_name") or "").strip()
        if current and any(k in low for k in {"this", "it", "current", "opened", "open"}):
            return {"intent": "report_section", "tools": ["read_current_report"], "args": {"current_report_name": current, "section": section}}
        return {"intent": "report_section", "tools": ["read_latest_report"], "args": {"section": section}}
    if "note" in low:
        if any(k in low for k in {"what", "which", "summary", "summarize", "today", "important", "tell"}):
            return {"intent": "notes_summary", "tools": ["list_notes"]}
        return {"intent": "list_notes", "tools": ["list_notes"]}
    if "report" in low:
        return {"intent": "list_reports", "tools": ["list_reports"]}
    return None


def _llm_plan(query: str, context: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if ask_ai is None:
        return None
    q = str(query or "").strip()
    if not q:
        return None
    ctx = context or {}
    current = str(ctx.get("current_report_name") or "").strip()
    hist = ctx.get("history") if isinstance(ctx.get("history"), list) else []
    hist_lines: list[str] = []
    for m in (hist[-10:] if isinstance(hist, list) else []):
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip().lower()
        txt = str(m.get("text") or "").strip()
        if role not in {"user", "assistant"} or not txt:
            continue
        hist_lines.append(f"{role.upper()}: {txt[:220]}")
    hist_block = "\n".join(hist_lines) if hist_lines else "(none)"
    prompt = (
        "Choose best info intent and tools for this user request.\n"
        "Allowed intents: earnings_calendar_chat, report_section, morning_updates, list_watchlist, list_portfolio, "
        "list_tasks, list_notes, list_reports, summarize_latest_report, summarize_current_report, report_facts_summary, none.\n"
        "Allowed tools: read_earnings_calendar, read_latest_report, read_current_report, read_morning_briefing, read_watchlist, "
        "get_holdings, list_tasks, list_notes, list_reports, report_facts.\n"
        "Return strict JSON only: "
        '{"intent":"...","tools":["..."],"args":{"refresh":false,"section":"","current_report_name":""},"confidence":0.0}\n'
        "Rules:\n"
        "- If user asks to see earnings in chat, use intent=earnings_calendar_chat + tool=read_earnings_calendar.\n"
        "- If user asks for specific part/section/excerpt from report, use intent=report_section and proper report tool.\n"
        "- Resolve short follow-ups using recent chat context (e.g., 'to get the list' after earnings request => earnings_calendar_chat).\n"
        "- Never return navigation intents.\n"
        f"Current open report: {current or '-'}\n"
        f"Recent conversation:\n{hist_block}\n"
        f"User: {q}"
    )
    try:
        raw = str(
            ask_ai(
                prompt,
                "ReAct planner. JSON only.",
                mode="fast",
                json_mode=True,
                temperature=0.0,
            )
            or ""
        ).strip()
        if not raw:
            return None
        obj = json.loads(raw)
        conf = float(obj.get("confidence") or 0.0)
        if conf < 0.72:
            return None
        intent = str(obj.get("intent") or "").strip()
        tools = [str(x or "").strip() for x in list(obj.get("tools") or []) if str(x or "").strip()]
        args = obj.get("args") if isinstance(obj.get("args"), dict) else {}
        allowed_intents = {
            "earnings_calendar_chat",
            "report_section",
            "morning_updates",
            "list_watchlist",
            "list_portfolio",
            "list_tasks",
            "list_notes",
            "list_reports",
            "summarize_latest_report",
            "summarize_current_report",
            "report_facts_summary",
        }
        allowed_tools = {
            "read_earnings_calendar",
            "read_latest_report",
            "read_current_report",
            "read_morning_briefing",
            "read_watchlist",
            "get_holdings",
            "list_tasks",
            "list_notes",
            "list_reports",
            "report_facts",
        }
        if intent not in allowed_intents or not tools or any(t not in allowed_tools for t in tools):
            return None
        return {"intent": intent, "tools": tools[:3], "args": args}
    except Exception:
        return None


def _extract_bullets_from_text(text: str, max_items: int = 3) -> list[str]:
    out: list[str] = []
    for raw in str(text or "").splitlines():
        s = str(raw or "").strip()
        if not s:
            continue
        ls = s.lower()
        if ls.startswith("source:") or ls.startswith("file:") or ls.startswith("path:") or s.startswith("#"):
            continue
        if s.startswith(("- ", "* ")):
            s = s[2:].strip()
        elif re.match(r"^\d+\.\s+", s):
            s = re.sub(r"^\d+\.\s+", "", s)
        if len(s) < 35:
            continue
        out.append(s[:220])
        if len(out) >= max_items:
            break
    return out


def _render(intent: str, obs: dict[str, dict[str, Any]]) -> str:
    if intent == "earnings_calendar_chat":
        data = obs.get("read_earnings_calendar") or {}
        rows = list(data.get("rows") or [])
        src = str(data.get("source") or "-")
        rf = str(data.get("refresh_status") or "").strip()
        rd = str(data.get("refresh_detail") or "").strip()
        if not rows:
            msg = "I could not find earnings calendar rows from current sources."
            msg += f"\nSource scanned: {src}."
            if rf:
                msg += f"\nRefresh status: {rf}" + (f" ({rd})" if rd else "") + "."
            msg += "\nLikely reason: source report not generated yet or no structured earnings rows were parsed."
            return msg
        lines = []
        if rf == "ok":
            lines.append("Refreshed earnings feed.\n")
        lines.append("Earnings Calendar (This Week):")
        last_d = ""
        shown = 0
        for r in rows:
            d = str(r.get("date") or "").strip() or "-"
            tm = str(r.get("time") or "").strip() or "-"
            tk = str(r.get("symbol") or "").strip().upper()
            nm = str(r.get("company") or "").strip()
            if d != last_d:
                lines.append(f"\n{d}")
                last_d = d
            lines.append(f"- {tm} {tk}" + (f" — {nm}" if nm else ""))
            shown += 1
            if shown >= 16:
                break
        lines.append(f"\nSource: {src}")
        return "\n".join(lines)
    if intent == "report_section":
        current = obs.get("read_current_report") or {}
        latest = obs.get("read_latest_report") or {}
        data = current if current else latest
        title = str(data.get("title") or data.get("name") or "Report")
        txt = str(data.get("text") or "")
        section = str((data.get("section") or "")).strip()
        if not txt:
            return "I couldn't read the report text yet."
        if not section:
            return f"{title}\nTell me which section you want (e.g., risk factors, MD&A, segment reporting)."
        lines = txt.splitlines()
        sec = normalize_text(section, keep_extra="_-")
        hit = -1
        for i, raw in enumerate(lines):
            s = normalize_text(raw, keep_extra="_-")
            if sec and (sec in s):
                hit = i
                break
        if hit < 0:
            return f"{title}\nI couldn't find a clear '{section}' heading. Try another section name."
        lo = max(0, hit - 6)
        hi = min(len(lines), hit + 24)
        excerpt = "\n".join(lines[lo:hi]).strip()[:2200]
        return f"{title} — {section}\n```\n{excerpt}\n```"
    if intent == "morning_updates":
        brief = (obs.get("read_morning_briefing") or {}).get("brief")
        bullets = [str(x).strip() for x in list((brief or {}).get("bullets") or []) if str(x).strip()]
        if not bullets:
            return "Morning update is quiet right now. No critical anomalies detected yet."
        return (
            "Morning updates (today):\n"
            + "\n".join(f"- {b}" for b in bullets[:3])
            + "\n\nMost important today is the item with the largest direct portfolio impact."
            + "\nIf you want, I can open the full report page."
        )
    if intent == "list_watchlist":
        rows = list((obs.get("read_watchlist") or {}).get("rows") or [])
        if not rows:
            return "Your watchlist is empty.\nIf you want, I can open the Watchlist page."
        lines = ["Your watchlist companies:"]
        for r in rows[:30]:
            tk = str(r.get("ticker") or "").strip().upper()
            nm = str(r.get("name") or "").strip()
            lines.append(f"- {tk}" + (f" — {nm}" if nm else ""))
        if len(rows) > 30:
            lines.append(f"- ...and {len(rows)-30} more")
        lines.append("")
        lines.append("If you want, I can open the Watchlist page.")
        return "\n".join(lines)
    if intent == "list_portfolio":
        rows = list((obs.get("get_holdings") or {}).get("rows") or [])
        if not rows:
            return "No portfolio holdings found yet.\nIf you want, I can open the Portfolio page."
        lines = ["Your portfolio holdings:"]
        for r in rows[:20]:
            tk = str(r.get("ticker") or "").strip().upper()
            sh = float(r.get("shares") or 0.0)
            lines.append(f"- {tk} ({sh:g} shares)")
        if len(rows) > 20:
            lines.append(f"- ...and {len(rows)-20} more")
        lines.append("")
        lines.append("If you want, I can open the Portfolio page.")
        return "\n".join(lines)
    if intent == "portfolio_today_status":
        s = obs.get("get_live_portfolio_summary") or {}
        if not isinstance(s, dict) or int(s.get("holdings_count") or 0) <= 0:
            return "No portfolio holdings found yet."
        basis = float(s.get("tracked_basis") or 0.0)
        day_pct = float(s.get("day_change_pct") or 0.0)
        day_usd = float(s.get("day_change_usd") or 0.0)
        best = s.get("best") if isinstance(s.get("best"), dict) else {}
        worst = s.get("worst") if isinstance(s.get("worst"), dict) else {}
        return (
            f"Portfolio today: {day_pct:+.2f}% (~${day_usd:,.0f} on ${basis:,.0f} tracked previous-close value).\n"
            f"Best: {str(best.get('ticker') or '-')} {float(best.get('day_pct') or 0.0):+.2f}%.\n"
            f"Worst: {str(worst.get('ticker') or '-')} {float(worst.get('day_pct') or 0.0):+.2f}%."
        )
    if intent == "list_notes":
        rows = list((obs.get("list_notes") or {}).get("rows") or [])
        if not rows:
            return "No notes found yet.\nIf you want, I can open the Notes page."
        lines = ["Your recent notes:"]
        for r in rows[:6]:
            tk = str(r.get("ticker") or "").strip().upper()
            txt = str(r.get("text") or "").strip().replace("\n", " ")
            head = f"[{tk}] " if tk else ""
            lines.append(f"- {head}{txt[:120]}")
        lines.append("")
        lines.append("If you want, I can open the Notes page.")
        return "\n".join(lines)
    if intent == "list_tasks":
        rows = list((obs.get("list_tasks") or {}).get("rows") or [])
        if not rows:
            return "No open tasks right now.\nIf you want, I can open Organizer."
        lines = ["Your open tasks and to-do items:"]
        for r in rows[:20]:
            rid = int(_row_value(r, "id", 0) or 0)
            task = str(_row_value(r, "task", "") or "").strip().replace("\n", " ")
            due = str(_row_value(r, "due_date", "") or "").strip()
            tk = str(_row_value(r, "ticker", "") or "").strip().upper()
            s = f"- {task}"
            if tk:
                s += f" [{tk}]"
            if due:
                s += f" (due {due})"
            if rid > 0:
                s += f" [#{rid}]"
            lines.append(s)
        lines.append("")
        lines.append("If you want, I can open Organizer.")
        return "\n".join(lines)
    if intent == "list_reports":
        rows = list((obs.get("list_reports") or {}).get("rows") or [])
        if not rows:
            return "No reports found yet.\nIf you want, I can open the Reports page."
        lines = ["Recent reports:"]
        for r in rows[:5]:
            nm = str(r.get("name") or "").strip()
            ts = str(r.get("modified_at") or "").replace("T", " ")[:16]
            lines.append(f"- {nm} ({ts})")
        lines.append("")
        lines.append("If you want, I can open the Reports page.")
        return "\n".join(lines)
    if intent == "notes_summary":
        rows = list((obs.get("list_notes") or {}).get("rows") or [])
        if not rows:
            return "No notes found yet."
        lines = ["Notes summary (recent):"]
        for r in rows[:5]:
            tk = str(r.get("ticker") or "").strip().upper()
            txt = str(r.get("text") or "").strip().replace("\n", " ")
            if not txt:
                continue
            head = f"[{tk}] " if tk else ""
            lines.append(f"- {head}{txt[:160]}")
        return "\n".join(lines)
    if intent == "summarize_latest_report":
        d = (obs.get("read_latest_report") or {})
        title = str(d.get("title") or d.get("name") or "Latest report").strip()
        txt = str(d.get("text") or "").strip()
        bullets = _extract_bullets_from_text(txt, max_items=3)
        if not bullets:
            return f"{title}\nSummary unavailable from report text.\nIf you want, I can open the full report page."
        return f"{title}\nSummary:\n" + "\n".join(f"- {b}" for b in bullets) + "\n\nIf you want, I can open the full report page."
    if intent == "summarize_current_report":
        d = (obs.get("read_current_report") or {})
        title = str(d.get("title") or d.get("name") or "Current report").strip()
        txt = str(d.get("text") or "").strip()
        if not title or title == "Current report":
            return "No open report context found. Open a report first, then ask for summary."
        bullets = _extract_bullets_from_text(txt, max_items=3)
        if not bullets:
            return f"Current Report ({title})\nSummary unavailable from report text.\nIf you want, I can open the full report page."
        return f"Current Report ({title})\nSummary:\n" + "\n".join(f"- {b}" for b in bullets) + "\n\nIf you want, I can open the full report page."
    if intent == "report_facts_summary":
        rows = list((obs.get("report_facts") or {}).get("rows") or [])
        if not rows:
            return "I don’t have high-confidence report facts yet. If you want, I can ask 2-3 questions and build your baseline memory."
        lines = ["Most important from recent reports:"]
        for r in rows[:6]:
            tk = str(r.get("ticker") or "").strip().upper()
            txt = str(r.get("fact_text") or "").strip()
            if not txt:
                continue
            lines.append(f"- {tk + ': ' if tk else ''}{txt[:180]}")
        lines.append("")
        lines.append("If you want, I can convert these into thesis/risk notes.")
        return "\n".join(lines)
    if intent == "risk_map_10k":
        rows = list((obs.get("risk_map_10k") or {}).get("rows") or [])
        if not rows:
            facts = list((obs.get("report_facts") or {}).get("rows") or [])
            if facts:
                lines = ["10-K risk sections are not available yet from official filings. Using official report-facts fallback:"]
                for r in facts[:6]:
                    tk = str(r.get("ticker") or "").strip().upper()
                    txt = str(r.get("fact_text") or "").strip()
                    if txt:
                        lines.append(f"- {tk + ': ' if tk else ''}{txt[:180]}")
                lines.append("")
                lines.append("I will re-check official SEC filing coverage automatically after automation updates.")
                return "\n".join(lines)
            return "I don’t have enough official 10-K risk-factor text yet. I will re-check automatically after the next automation cycle."
        lines = ["10-K risk map (evidence from Risk Factors sections):"]
        by_tk: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            tk = str(r.get("ticker") or "").strip().upper()
            if not tk:
                continue
            by_tk.setdefault(tk, []).append(r)
        for tk in sorted(by_tk.keys())[:12]:
            grp = by_tk[tk]
            industry = str((grp[0] or {}).get("industry") or "Unknown").strip()
            lines.append(f"- {tk} ({industry})")
            for x in grp[:2]:
                th = str(x.get("theme") or "General").strip()
                txt = str(x.get("risk_text") or "").strip()
                if txt:
                    lines.append(f"  - {th}: {txt[:150]}")
        lines.append("")
        lines.append("If you want, I can compare risk concentration by industry next.")
        return "\n".join(lines)
    if intent == "macro_shock_simulation":
        d = obs.get("simulate_macro_shock") or {}
        answer = str(d.get("answer") or "").strip() or "Simulation ran."
        conf = int(float(d.get("confidence_score") or 0))
        missing = [str(x).strip() for x in list(d.get("missing_variables") or []) if str(x).strip()]
        impacts = list(d.get("impacts") or [])
        lines = [answer, f"Confidence: {conf}/100"]
        if impacts:
            lines.append("Top exposed:")
            for x in impacts[:6]:
                tk = str((x or {}).get("ticker") or "").strip().upper()
                order = str((x or {}).get("order") or "").strip()
                sc = float((x or {}).get("impact_score") or 0.0)
                direction = str((x or {}).get("likely_direction") or "uncertain").strip().lower()
                why = str((x or {}).get("why") or "").strip()
                if tk:
                    line = f"- {tk} ({order}) score {sc:.3f}, likely {direction}"
                    if why:
                        line += f" | why: {why[:140]}"
                    lines.append(line)
                    cites = [str(c).strip() for c in list((x or {}).get("citations") or []) if str(c).strip()]
                    if cites:
                        lines.append(f"  source: {cites[0]}")
        if missing:
            lines.append("Missing data / blindspots:")
            for m in missing[:5]:
                lines.append(f"- {m}")
        return "\n".join(lines)
    if intent == "macro_shock_batch":
        d = obs.get("simulate_macro_shock_batch") or {}
        top = list(((d.get("comparison") if isinstance(d.get("comparison"), dict) else {}).get("top_exposed") or []))
        cnt = int(d.get("count") or 0)
        conf = int(float(d.get("average_confidence_score") or 0))
        lines = [f"Scenario batch simulated ({cnt} scenarios).", f"Average confidence: {conf}/100"]
        if top:
            lines.append("Most exposed across scenarios:")
            for x in top[:8]:
                tk = str((x or {}).get("ticker") or "").strip().upper()
                sc = float((x or {}).get("aggregate_impact_score") or 0.0)
                if tk:
                    lines.append(f"- {tk}: {sc:.3f}")
        return "\n".join(lines)
    return ""


def _ui_payload(intent: str, obs: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if intent == "morning_updates":
        brief = (obs.get("read_morning_briefing") or {}).get("brief")
        bullets = [str(x).strip() for x in list((brief or {}).get("bullets") or []) if str(x).strip()]
        return {
            "type": "brief_card",
            "title": "Morning Updates",
            "items": bullets[:3],
        }
    if intent == "list_tasks":
        rows = list((obs.get("list_tasks") or {}).get("rows") or [])
        items = []
        for r in rows[:20]:
            items.append(
                {
                    "task": str(_row_value(r, "task", "") or "").strip(),
                    "due": str(_row_value(r, "due_date", "") or "").strip(),
                    "todo_id": int(_row_value(r, "id", 0) or 0),
                }
            )
        return {"type": "task_list", "title": "Open Tasks", "items": items}
    if intent == "list_watchlist":
        rows = list((obs.get("read_watchlist") or {}).get("rows") or [])
        items = []
        for r in rows[:40]:
            tk = str(r.get("ticker") or "").strip().upper()
            nm = str(r.get("name") or "").strip()
            if tk:
                items.append({"label": tk + (f" — {nm}" if nm else "")})
        return {"type": "checklist", "title": "Watchlist", "items": items}
    if intent == "list_portfolio":
        rows = list((obs.get("get_holdings") or {}).get("rows") or [])
        items = []
        for r in rows[:30]:
            tk = str(r.get("ticker") or "").strip().upper()
            sh = float(r.get("shares") or 0.0)
            if tk:
                items.append({"label": f"{tk} ({sh:g} shares)"})
        return {"type": "checklist", "title": "Portfolio Holdings", "items": items}
    if intent == "report_facts_summary":
        rows = list((obs.get("report_facts") or {}).get("rows") or [])
        items = []
        for r in rows[:8]:
            tk = str(r.get("ticker") or "").strip().upper()
            txt = str(r.get("fact_text") or "").strip()
            if txt:
                items.append({"label": (f"{tk}: " if tk else "") + txt[:170]})
        return {"type": "brief_card", "title": "Report Intelligence", "items": items}
    if intent == "risk_map_10k":
        rows = list((obs.get("risk_map_10k") or {}).get("rows") or [])
        items = []
        for r in rows[:10]:
            tk = str(r.get("ticker") or "").strip().upper()
            th = str(r.get("theme") or "General").strip()
            txt = str(r.get("risk_text") or "").strip()
            ind = str(r.get("industry") or "Unknown").strip()
            if txt:
                items.append({"label": f"{tk} ({ind}) - {th}: {txt[:120]}"})
        return {"type": "brief_card", "title": "10-K Risk Map", "items": items}
    if intent == "macro_shock_simulation":
        d = obs.get("simulate_macro_shock") or {}
        items = []
        for x in list(d.get("impacts") or [])[:8]:
            tk = str((x or {}).get("ticker") or "").strip().upper()
            order = str((x or {}).get("order") or "").strip()
            sc = float((x or {}).get("impact_score") or 0.0)
            direction = str((x or {}).get("likely_direction") or "uncertain").strip().lower()
            if tk:
                items.append({"label": f"{tk} ({order}) score {sc:.3f}, likely {direction}"})
        return {"type": "brief_card", "title": "Macro Shock Simulation", "items": items}
    if intent == "macro_shock_batch":
        d = obs.get("simulate_macro_shock_batch") or {}
        top = list(((d.get("comparison") if isinstance(d.get("comparison"), dict) else {}).get("top_exposed") or []))
        items = []
        for x in top[:10]:
            tk = str((x or {}).get("ticker") or "").strip().upper()
            sc = float((x or {}).get("aggregate_impact_score") or 0.0)
            if tk:
                items.append({"label": f"{tk}: {sc:.3f}"})
        return {"type": "brief_card", "title": "Scenario Comparison", "items": items}
    return None


def run_react_information(query: str, context: dict[str, Any] | None = None, max_steps: int = 3) -> dict[str, Any] | None:
    plan = _llm_plan(query, context=context) or _plan(query, context=context)
    if not plan:
        return None
    intent = str(plan.get("intent") or "").strip()
    tools = [str(x or "").strip() for x in list(plan.get("tools") or []) if str(x or "").strip()]
    args = plan.get("args") if isinstance(plan.get("args"), dict) else {}
    if not intent or not tools:
        return None

    traces: list[dict[str, str]] = [{"step": "react.think", "detail": f"intent={intent}"}]
    observations: dict[str, dict[str, Any]] = {}
    for tool_name in tools[: max(1, min(3, int(max_steps)))]:
        fn = TOOL_REGISTRY.get(tool_name)
        if fn is None:
            traces.append({"step": "react.observe", "detail": f"missing_tool={tool_name}"})
            continue
        traces.append({"step": "react.act", "detail": tool_name})
        try:
            observations[tool_name] = fn(args)
            traces.append({"step": "react.observe", "detail": f"{tool_name}:ok"})
        except Exception:
            observations[tool_name] = {}
            traces.append({"step": "react.observe", "detail": f"{tool_name}:error"})

    # Proactive evidence loop (bounded): verify -> fill gaps -> verify again.
    if intent == "risk_map_10k":
        rows = list((observations.get("risk_map_10k") or {}).get("rows") or [])
        if not rows:
            # Retry once after refreshing official report facts cache.
            rf_tool = TOOL_REGISTRY.get("report_facts")
            if rf_tool is not None:
                traces.append({"step": "react.act", "detail": "report_facts(retry)"})
                try:
                    observations["report_facts"] = rf_tool({"query": str(args.get("query") or "")})
                    traces.append({"step": "react.observe", "detail": "report_facts:ok"})
                except Exception:
                    observations["report_facts"] = {}
                    traces.append({"step": "react.observe", "detail": "report_facts:error"})
            rm_tool = TOOL_REGISTRY.get("risk_map_10k")
            if rm_tool is not None:
                traces.append({"step": "react.act", "detail": "risk_map_10k(retry)"})
                try:
                    observations["risk_map_10k"] = rm_tool(args)
                    traces.append({"step": "react.observe", "detail": "risk_map_10k:ok_retry"})
                except Exception:
                    observations["risk_map_10k"] = {}
                    traces.append({"step": "react.observe", "detail": "risk_map_10k:error_retry"})

    msg = _render(intent, observations).strip()
    if not msg:
        return None
    ui = _ui_payload(intent, observations)
    traces.append({"step": "react.final", "detail": "answer"})
    return {
        "status": "ok",
        "intent": intent,
        "message": msg,
        "confidence": 0.93,
        "redirect_url": "",
        "citations": [],
        "traces": traces,
        "matched_by": "react_tool_registry",
        "ui": ui if isinstance(ui, dict) else {},
    }
