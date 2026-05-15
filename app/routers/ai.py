from __future__ import annotations

import base64
import concurrent.futures as cf
import datetime as dt
import json
import re
import threading
import time
import traceback

from fastapi import APIRouter, Body, Query
from fastapi.responses import StreamingResponse

from app.core.config import app_env
from app.core.analysis_context import build_analysis_context, build_chat_hydration_payload
from app.core.date import parse_datetime_flexible
from app.core.normalize import normalize_text as _norm
from app.core.num import to_float as _to_float
from app.services.chat_memory_service import append_chat_message, list_recent_chat_messages
from app.services.ai_job_queue_service import enqueue_job, ensure_ai_job_queue_schema, get_cached_result, get_job, worker_health
from app.services.ai_orchestrator import ensure_ai_schema, get_adaptive_turn_policy_snapshot, get_ai_quality_report, run_ai_command
from app.services.phase2_scaling_service import (
    collect_map_reduce_snapshot,
    enqueue_sector_map_reduce,
    ensure_phase2_postgres_schema,
    get_map_reduce_report,
    phase2_status,
)
from app.services.postgres_core_service import (
    core_backend,
    ensure_postgres_core_schema,
    guard_core_backend_cutover,
    strict_postgres_mode,
)
from app.services.portfolio_memory_service import (
    forget_compact_memory,
    get_cached_morning_brief,
    get_interview_progress,
    learn_from_chat_turn,
    learn_compact_memory_from_text,
    learn_investor_style_from_answer,
    get_proactive_gap_prompt,
    import_portfolio_history_csv,
    inject_runtime_context,
    list_compact_memories,
    list_active_rules,
    list_portfolio_transactions,
    list_rule_candidates,
    promote_rule_candidate,
    remember_compact_memory,
    run_learning_cycle,
    get_live_portfolio_summary,
    get_holdings,
    summarize_active_rules_for_prompt,
    save_morning_brief_snapshot,
    list_recent_portfolio_transactions,
    summarize_trade_decision_reasons,
    summarize_compact_memory_for_prompt,
    summarize_investor_style_memory,
)
from app.services.daily_operator import get_dynamic_gap_audit, resolve_dynamic_gap_answer
from app.services.organizer_service import complete_task, complete_task_by_text, list_recent_notes, list_tasks
from app.services.mini_statements_service import fetch_historical_financials
from app.services.user_preferences_service import (
    ensure_user_preferences_schema,
    learn_preferences_from_text,
    learn_preferences_from_trajectory,
    summarize_user_preferences,
    upsert_user_preference,
)
from tools.llm_engine import ask_ai, ask_ai_vision, get_ai_runtime_metrics
from tools.sync_us_listed_universe import sync_universe as sync_us_listed_universe_now
from app.services.memory_engine import OnyxMemory

from app.services.postgres_core_service import pg_connect
from app.services.postgres_core_service import add_agent_feedback_memory_pg
from app.services.web_search_service import is_realtime_query, search_news, format_news_text
from app.services.postgres_core_service import store_ai_response_feedback
from app.services.web_search_service import get_search_engine_status


router = APIRouter()
DEFAULT_USER_PROFILE = (
    "Building Python/FastAPI app. Hates messy data. Wants Apple-level UI. Risk tolerance: Low."
)
_MEMORY_ENGINE: OnyxMemory | None = None
_OPERATOR_GUARDRAILS: list[tuple[str, str]] = [
    ("engineering.no_hardcoding", "Never hardcode outputs or company-specific behavior. Use data/services and model reasoning."),
    ("engineering.dry_first", "Before adding code, check for existing utilities/services and avoid duplicate logic."),
    ("engineering.postgres_first", "Use Postgres-backed runtime paths as source of truth; avoid SQLite fallback in active runtime."),
    ("engineering.validate_after_change", "Run compile and health checks after changes before declaring done."),
    ("engineering.async_preferred", "Prefer async queue/SSE for deep AI analysis to keep UI responsive."),
]
_SYNC_UNIVERSE_LOCK = threading.Lock()
_SYNC_UNIVERSE_STATE: dict = {
    "running": False,
    "started_at": "",
    "finished_at": "",
    "last_error": "",
    "last_result": None,
    "run_count": 0,
}


def _sync_universe_worker() -> None:
    started_at = dt.datetime.now().isoformat()
    with _SYNC_UNIVERSE_LOCK:
        _SYNC_UNIVERSE_STATE["running"] = True
        _SYNC_UNIVERSE_STATE["started_at"] = started_at
        _SYNC_UNIVERSE_STATE["finished_at"] = ""
        _SYNC_UNIVERSE_STATE["last_error"] = ""
    try:
        result = sync_us_listed_universe_now()
        with _SYNC_UNIVERSE_LOCK:
            _SYNC_UNIVERSE_STATE["last_result"] = result
            _SYNC_UNIVERSE_STATE["finished_at"] = dt.datetime.now().isoformat()
            _SYNC_UNIVERSE_STATE["run_count"] = int(_SYNC_UNIVERSE_STATE.get("run_count") or 0) + 1
    except Exception:
        with _SYNC_UNIVERSE_LOCK:
            _SYNC_UNIVERSE_STATE["last_error"] = traceback.format_exc(limit=6)
            _SYNC_UNIVERSE_STATE["finished_at"] = dt.datetime.now().isoformat()
            _SYNC_UNIVERSE_STATE["run_count"] = int(_SYNC_UNIVERSE_STATE.get("run_count") or 0) + 1
    finally:
        with _SYNC_UNIVERSE_LOCK:
            _SYNC_UNIVERSE_STATE["running"] = False


def _sync_universe_start() -> bool:
    with _SYNC_UNIVERSE_LOCK:
        if bool(_SYNC_UNIVERSE_STATE.get("running")):
            return False
    t = threading.Thread(target=_sync_universe_worker, name="sync-universe-worker", daemon=True)
    t.start()
    return True


@router.get("/ai/runtime/metrics")
def ai_runtime_metrics():
    return {"ok": True, "metrics": get_ai_runtime_metrics(), "asof": dt.datetime.now().isoformat()}


def _get_memory_engine() -> OnyxMemory | None:
    global _MEMORY_ENGINE
    if _MEMORY_ENGINE is None:
        try:
            _MEMORY_ENGINE = OnyxMemory()
        except Exception:
            _MEMORY_ENGINE = None
    return _MEMORY_ENGINE


def _requires_recent_data(query: str) -> bool:
    low = _norm(query)
    return any(k in low for k in {"today", "now", "recent", "current", "right now", "latest"})


def _looks_like_portfolio_query(query: str) -> bool:
    low = _norm(query)
    if any(k in low for k in {"portfolio", "holdings", "active positions"}):
        return True
    # tolerate common typo
    return "portoflio" in low


def _fast_trade_history_reply(query: str) -> dict | None:
    low = _norm(query)
    if not any(k in low for k in {"trade", "trades", "transaction", "history", "entries", "exits", "bought", "sold"}):
        return None
    years = []
    for y in re.findall(r"\b(19\d{2}|20\d{2})\b", low):
        yi = int(y)
        if yi not in years:
            years.append(yi)
    if not years:
        return None
    years = sorted(years)
    year = years[0]
    raw = str(query or "")
    cands: list[str] = []
    for m in re.findall(r"\b[A-Z][A-Z0-9.\-]{0,5}\b", raw):
        tk = str(m or "").strip().upper()
        if tk and tk not in cands:
            cands.append(tk)
    ticker = ""
    for tk in cands[:8]:
        if list_portfolio_transactions(limit=1, ticker=tk, year=year):
            ticker = tk
            break
    rows = list_portfolio_transactions(limit=2000, ticker=ticker, year=year)
    if len(years) > 1:
        lines = ["Trade history summary by year:"]
        for y in years[:8]:
            yr = list_portfolio_transactions(limit=2000, ticker=ticker, year=y)
            buy_n = sum(1 for r in yr if str(r.get("action") or "").lower() in {"buy", "add"})
            sell_n = sum(1 for r in yr if str(r.get("action") or "").lower() in {"sell", "trim"})
            scope = f" ({ticker})" if ticker else ""
            lines.append(f"- {y}{scope}: rows={len(yr)}, buys={buy_n}, sells={sell_n}")
        return {
            "status": "ok",
            "intent": "trade_history_period",
            "message": "\n".join(lines),
            "confidence": 0.98,
            "redirect_url": "/my_universe?tab=all",
            "citations": [],
            "matched_by": "router_fast_path",
            "version": "v1.2.0",
            "traces": [{"step": "route", "detail": "router_fast_trade_history"}],
            "action": {"type": "NONE", "payload": {}},
            "ui": {},
        }
    if not rows:
        scope = f" for {ticker}" if ticker else ""
        return {
            "status": "ok",
            "intent": "trade_history_period",
            "message": f"No recorded trades found{scope} in {year}.",
            "confidence": 0.95,
            "redirect_url": "/my_universe?tab=all",
            "citations": [],
            "matched_by": "router_fast_path",
            "version": "v1.2.0",
            "traces": [{"step": "route", "detail": "router_fast_trade_history"}],
            "action": {"type": "NONE", "payload": {}},
            "ui": {},
        }
    buy_n = sum(1 for r in rows if str(r.get("action") or "").lower() in {"buy", "add"})
    sell_n = sum(1 for r in rows if str(r.get("action") or "").lower() in {"sell", "trim"})
    avg_buy = 0.0
    avg_sell = 0.0
    buy_px = [float(r.get("price") or 0.0) for r in rows if str(r.get("action") or "").lower() in {"buy", "add"} and float(r.get("price") or 0.0) > 0]
    sell_px = [float(r.get("price") or 0.0) for r in rows if str(r.get("action") or "").lower() in {"sell", "trim"} and float(r.get("price") or 0.0) > 0]
    if buy_px:
        avg_buy = sum(buy_px) / float(len(buy_px))
    if sell_px:
        avg_sell = sum(sell_px) / float(len(sell_px))
    scope = f" ({ticker})" if ticker else ""
    lines = [f"Trade history {year}{scope}: {len(rows)} rows."]
    lines.append(f"Pattern: buys={buy_n}, sells={sell_n}.")
    if buy_px:
        lines.append(f"Avg buy price: {avg_buy:.2f}.")
    if sell_px:
        lines.append(f"Avg sell price: {avg_sell:.2f}.")
    lines.append("Latest rows:")
    for r in rows[:10]:
        ts = str(r.get("created_at") or "")[:16].replace("T", " ")
        lines.append(
            f"- {ts} {str(r.get('action') or '').upper()} {str(r.get('ticker') or '-')}"
            f" {float(r.get('shares') or 0.0):g} @ {float(r.get('price') or 0.0):.2f}"
        )
    return {
        "status": "ok",
        "intent": "trade_history_period",
        "message": "\n".join(lines),
        "confidence": 0.98,
        "redirect_url": "/my_universe?tab=all",
        "citations": [],
        "matched_by": "router_fast_path",
        "version": "v1.2.0",
        "traces": [{"step": "route", "detail": "router_fast_trade_history"}],
        "action": {"type": "NONE", "payload": {}},
        "ui": {},
    }


def _fast_db_reply(query: str, context: dict | None = None) -> dict | None:
    low = _norm(query)
    ctx = context or {}
    if any(k in low for k in {"5 year", "5y", "historical", "trend", "revenue", "debt", "fcf", "cash flow", "balance sheet"}):
        cpath = str(ctx.get("current_path") or "").strip()
        cquery = str(ctx.get("current_query") or "").strip()
        tk = ""
        if cpath.startswith("/company_file"):
            try:
                import urllib.parse as _up

                qmap = _up.parse_qs(cquery.lstrip("?"), keep_blank_values=False)
                for key in ("t", "ticker"):
                    vals = qmap.get(key) or []
                    if vals:
                        tk = str(vals[0] or "").strip().upper()
                        break
            except Exception:
                tk = ""
        if tk:
            fin = fetch_historical_financials(ticker=tk, metric="all", years=5, refresh=False)
            if bool(fin.get("ok")):
                years = list(fin.get("years") or [])
                rev = list(fin.get("revenue") or [])
                debt = list(fin.get("total_debt") or [])
                fcf = list(fin.get("free_cash_flow") or [])
                def _fmt(v):
                    if v is None:
                        return "-"
                    try:
                        return f"{float(v):,.0f}"
                    except Exception:
                        return str(v)
                rows = []
                for i, y in enumerate(years):
                    rows.append(
                        f"- {y}: revenue={_fmt(rev[i] if i < len(rev) else None)}, "
                        f"debt={_fmt(debt[i] if i < len(debt) else None)}, "
                        f"fcf={_fmt(fcf[i] if i < len(fcf) else None)}"
                    )
                msg = (
                    f"{tk} 5-year trends ({str(fin.get('source') or '-')}, asof {str(fin.get('asof') or '-')})\n"
                    + "\n".join(rows[:8])
                )
                return {
                    "status": "ok",
                    "intent": "financial_trends_company",
                    "message": msg,
                    "confidence": 0.96,
                    "redirect_url": f"/company_file?t={tk}",
                    "citations": [],
                    "matched_by": "router_fast_path",
                    "version": "v1.2.0",
                    "traces": [{"step": "route", "detail": "router_fast_financial_trends"}],
                    "action": {"type": "NONE", "payload": {}},
                    "ui": {},
                }
    if any(k in low for k in {"holdings", "current positions", "what do we own", "list holdings", "show holdings"}):
        rows = get_holdings(limit=40)
        if not rows:
            msg = "No holdings found yet."
        else:
            lines = ["Current holdings:"]
            for r in rows[:12]:
                lines.append(f"- {str(r.get('ticker') or '-')} ({float(r.get('shares') or 0.0):g} shares)")
            msg = "\n".join(lines)
        return {
            "status": "ok",
            "intent": "get_holdings",
            "message": msg,
            "confidence": 0.97,
            "redirect_url": "/my_universe?tab=all",
            "citations": [],
            "matched_by": "router_fast_path",
            "version": "v1.2.0",
            "traces": [{"step": "route", "detail": "router_fast_holdings"}],
            "action": {"type": "NONE", "payload": {}},
            "ui": {},
        }
    if any(k in low for k in {"open tasks", "my tasks", "list tasks", "show tasks"}):
        rows = list_tasks(open_only=True, limit=30)
        if not rows:
            msg = "No open tasks."
        else:
            lines = ["Open tasks:"]
            for r in rows[:12]:
                lines.append(f"- {str(r['task'] or '').strip()} ({str(r['priority'] or 'P2')})")
            msg = "\n".join(lines)
        return {
            "status": "ok",
            "intent": "list_tasks",
            "message": msg,
            "confidence": 0.97,
            "redirect_url": "/organizer",
            "citations": [],
            "matched_by": "router_fast_path",
            "version": "v1.2.0",
            "traces": [{"step": "route", "detail": "router_fast_tasks"}],
            "action": {"type": "NONE", "payload": {}},
            "ui": {},
        }
    if any(k in low for k in {"recent notes", "my notes", "show notes", "list notes"}):
        rows = list_recent_notes(limit=30)
        if not rows:
            msg = "No notes yet."
        else:
            lines = ["Recent notes:"]
            for r in rows[:12]:
                tk = str(r.get("ticker") or "").strip().upper()
                text = str(r.get("text") or "").strip()
                lines.append(f"- {tk + ': ' if tk else ''}{text[:120]}")
            msg = "\n".join(lines)
        return {
            "status": "ok",
            "intent": "list_notes",
            "message": msg,
            "confidence": 0.97,
            "redirect_url": "/organizer",
            "citations": [],
            "matched_by": "router_fast_path",
            "version": "v1.2.0",
            "traces": [{"step": "route", "detail": "router_fast_notes"}],
            "action": {"type": "NONE", "payload": {}},
            "ui": {},
        }
    if any(k in low for k in {"recent transactions", "recent trades", "trade history", "transaction history"}):
        rows = list_recent_portfolio_transactions(limit=20)
        if not rows:
            msg = "No portfolio transaction history recorded yet."
        else:
            lines = ["Recent portfolio transactions:"]
            for r in rows[:12]:
                ts = str(r.get("created_at") or "")[:16].replace("T", " ")
                lines.append(
                    f"- {ts} {str(r.get('action') or '').upper()} {str(r.get('ticker') or '-')} "
                    f"{float(r.get('shares') or 0.0):g} @ {float(r.get('price') or 0.0):.2f}"
                )
            msg = "\n".join(lines)
        return {
            "status": "ok",
            "intent": "portfolio_change_log",
            "message": msg,
            "confidence": 0.97,
            "redirect_url": "/my_universe?tab=all",
            "citations": [],
            "matched_by": "router_fast_path",
            "version": "v1.2.0",
            "traces": [{"step": "route", "detail": "router_fast_transactions"}],
            "action": {"type": "NONE", "payload": {}},
            "ui": {},
        }
    if any(k in low for k in {"sell reason", "sell reasons", "buy reason", "buy reasons", "why did i sell", "why did i buy"}):
        action = "sell" if "sell" in low else ("buy" if "buy" in low else "")
        summ = summarize_trade_decision_reasons(limit=500, action=action)
        by_reason = list(summ.get("by_reason") or [])
        if not by_reason:
            msg = "No structured trade reasons recorded yet."
        else:
            title = "Trade decision reasons:"
            if action == "sell":
                title = "Sell decision reasons:"
            elif action == "buy":
                title = "Buy decision reasons:"
            lines = [title]
            for r in by_reason[:8]:
                lines.append(
                    f"- {str(r.get('label') or r.get('reason') or '-')}: "
                    f"{int(r.get('count') or 0)} ({float(r.get('pct') or 0.0):.1f}%)"
                )
            msg = "\n".join(lines)
        return {
            "status": "ok",
            "intent": "trade_reason_summary",
            "message": msg,
            "confidence": 0.97,
            "redirect_url": "/my_universe?tab=all",
            "citations": [],
            "matched_by": "router_fast_path",
            "version": "v1.2.0",
            "traces": [{"step": "route", "detail": "router_fast_trade_reasons"}],
            "action": {"type": "NONE", "payload": {}},
            "ui": {},
        }
    return None


def _is_fresh_portfolio_live(summary: dict) -> tuple[bool, str]:
    if not isinstance(summary, dict) or not summary:
        return False, "missing_live_summary"
    asof = str(summary.get("asof") or "").strip()
    if not asof:
        return False, "missing_asof"
    ts = parse_datetime_flexible(asof)
    if ts is None:
        return False, "bad_asof"
    age_sec = (dt.datetime.now() - ts).total_seconds()
    if age_sec > 180:
        return False, "stale_asof"
    src = str(summary.get("quote_source") or "").strip().lower()
    if src in {"", "none", "unavailable"}:
        return False, "quote_source_unavailable"
    prev_val = _to_float(summary.get("tracked_prev_close_value"), 0.0)
    if prev_val <= 0:
        return False, "no_tracked_prev_value"
    cov = _to_float(summary.get("coverage_pct"), 0.0)
    if cov > 0 and cov < 60.0:
        return False, "low_coverage"
    return True, "ok"


def _portfolio_live_message(summary: dict) -> str:
    day_pct = _to_float(summary.get("day_change_pct"), 0.0)
    day_usd = _to_float(summary.get("day_change_usd"), 0.0)
    prev_val = _to_float(summary.get("tracked_prev_close_value"), 0.0)
    best = summary.get("best") if isinstance(summary.get("best"), dict) else {}
    worst = summary.get("worst") if isinstance(summary.get("worst"), dict) else {}
    asof = str(summary.get("asof") or "")
    asof_txt = asof
    asof_dt = parse_datetime_flexible(asof)
    if asof_dt is not None:
        asof_txt = asof_dt.strftime("%H:%M:%S")
    return (
        f"Portfolio today (as of {asof_txt}): {day_pct:+.2f}% "
        f"(~${day_usd:,.0f} on ${prev_val:,.0f} tracked previous-close value).\n"
        f"Best: {str(best.get('ticker') or '-')} {_to_float(best.get('day_pct'), 0.0):+.2f}%.\n"
        f"Worst: {str(worst.get('ticker') or '-')} {_to_float(worst.get('day_pct'), 0.0):+.2f}%."
    )


def _build_action(intent: str, redirect_url: str) -> dict:
    it = str(intent or "").strip().lower()
    rd = str(redirect_url or "").strip()
    if rd and it not in {"notes_summary", "summarize_latest_report", "summarize_current_report"}:
        return {"type": "NAVIGATE", "payload": rd}
    return {"type": "NONE", "payload": {}}


def _build_execution_block(ui: dict | None) -> dict:
    u = ui if isinstance(ui, dict) else {}
    ex = u.get("execution")
    if isinstance(ex, dict):
        return ex
    return {"type": "none"}


def _profile_text(ctx: dict) -> str:
    pref = summarize_user_preferences(limit=20)
    style = summarize_investor_style_memory(limit=20)
    mem = summarize_compact_memory_for_prompt(query=str(ctx.get("last_query") or ""), limit=14)
    rules = summarize_active_rules_for_prompt(query=str(ctx.get("last_query") or ""), limit=10)
    raw = str(ctx.get("user_profile") or "").strip()
    base = raw or app_env("AI_USER_PROFILE", "").strip() or DEFAULT_USER_PROFILE
    if pref:
        base += "\nUser Preferences:\n" + pref
    if style:
        base += "\nInvestor Style Memory:\n" + style
    if mem:
        base += "\nCompact Memory:\n" + mem
    if rules:
        base += "\nActive Rules:\n" + rules
    return base[:2400]


def _parse_image_data_url(data_url: str) -> tuple[bytes, str]:
    raw = str(data_url or "").strip()
    if not raw:
        return b"", ""
    m = re.match(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", raw, flags=re.S)
    if not m:
        return b"", ""
    mime = str(m.group(1) or "").strip().lower()
    b64 = str(m.group(2) or "").strip()
    try:
        blob = base64.b64decode(b64, validate=True)
    except Exception:
        return b"", ""
    return blob, mime


def _history_block(ctx: dict, limit: int = 30) -> str:
    out: list[str] = []
    hist = ctx.get("history")
    if not isinstance(hist, list):
        return ""
    for m in hist[-max(1, min(80, int(limit))):]:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip().lower()
        txt = str(m.get("text") or "").strip()
        if role not in {"user", "assistant"} or not txt:
            continue
        out.append(f"{role.upper()}: {txt[:240]}")
    return "\n".join(out)


def _load_chat_summaries() -> str:
    """Load last 5 LLM-compressed conversation summaries from agent_memory_core."""
    try:
        con = pg_connect()
        if con is None:
            return ""
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT summary, updated_at FROM agent_memory_core "
                "WHERE ticker='__CHAT__' ORDER BY updated_at DESC LIMIT 5",
            )
            rows = cur.fetchall() or []
        finally:
            con.close()
        if not rows:
            return ""
        lines = ["\nPAST CONVERSATION MEMORY (what we've discussed before):"]
        for r in reversed(rows):  # oldest first so context reads chronologically
            date = str(r[1] or "")[:10]
            lines.append(f"  [{date}] {str(r[0] or '')[:350]}")
        return "\n".join(lines)
    except Exception:
        return ""


def _maybe_summarize_session(session_id: str, history: list[dict]) -> None:
    """Every 20 messages, compress the session into a persistent memory entry."""
    try:
        count = len(history)
        if count < 20 or count % 20 != 0:
            return
        lines: list[str] = []
        for m in history[-40:]:
            role = str(m.get("role") or "").strip().lower()
            text = str(m.get("text") or "").strip()[:400]
            if role in {"user", "assistant"} and text:
                lines.append(f"{role.upper()}: {text}")
        if len(lines) < 4:
            return
        transcript = "\n".join(lines)
        prompt = (
            "Summarize this investment conversation in 4-6 sentences.\n"
            "Focus on: tickers discussed, decisions or conclusions reached, "
            "preferences or rules expressed, open questions left unresolved.\n"
            "Be specific with names, numbers, and dates where present.\n\n"
            f"Conversation:\n{transcript}\n\n"
            "Output ONLY the summary sentences, nothing else."
        )
        summary = ask_ai(prompt, context="", mode="fast").strip()
        if summary and len(summary) > 20:
            add_agent_feedback_memory_pg(
                "__CHAT__",
                f"[Session {session_id[:20]} · {count} msgs] {summary}",
            )
    except Exception:
        pass


def _fire_background_learning(q: str, history: list[dict], session_id: str) -> None:
    """Fire all learning side-effects in a daemon thread after the response is sent."""
    def _run() -> None:
        try:
            learn_preferences_from_text(q, history=history)
        except Exception:
            pass
        try:
            learn_preferences_from_trajectory(history, latest_user_text=q)
        except Exception:
            pass
        try:
            learn_compact_memory_from_text(q, source="chat_turn")
        except Exception:
            pass
        try:
            learn_from_chat_turn(q, source="chat")
        except Exception:
            pass
        try:
            learn_investor_style_from_answer(q)
        except Exception:
            pass
        _maybe_summarize_session(session_id, history)
    threading.Thread(target=_run, daemon=True).start()


_PREFS_SCHEMA_CHECKED = False


def _ai_command_sync(payload: dict) -> dict:
    global _PREFS_SCHEMA_CHECKED
    if not _PREFS_SCHEMA_CHECKED:
        ensure_user_preferences_schema()
        _PREFS_SCHEMA_CHECKED = True
    q = str((payload or {}).get("query") or "").strip()
    job_type = str((payload or {}).get("job_type") or "").strip().lower()
    image_data_url = str((payload or {}).get("image_data_url") or "").strip()
    ctx = (payload or {}).get("context") or {}
    if not isinstance(ctx, dict):
        ctx = {}
    ctx["last_query"] = q
    force_deep = bool(ctx.get("force_deep_reasoning")) or (job_type in {"map_task", "reduce_task"})
    # Fast DB-first route for historical trade questions; avoids LLM stalls.
    if q and not force_deep:
        fast = _fast_trade_history_reply(q)
        if isinstance(fast, dict):
            return fast
        fast_db = _fast_db_reply(q, context=ctx)
        if isinstance(fast_db, dict):
            return fast_db
    session_id = str(ctx.get("session_id") or "default").strip()[:120]
    db_hist = list_recent_chat_messages(session_id=session_id, limit=60)
    client_hist = ctx.get("history")
    merged_hist = db_hist[:]
    if isinstance(client_hist, list):
        for m in client_hist[-60:]:
            if not isinstance(m, dict):
                continue
            merged_hist.append(
                {
                    "role": str(m.get("role") or ""),
                    "text": str(m.get("text") or ""),
                    "intent": str(m.get("intent") or ""),
                    "status": str(m.get("status") or ""),
                    "created_at": "",
                }
            )
    ctx["history"] = merged_hist[-80:]
    # Learning side-effects run in background AFTER the response — keeps latency low.
    # (Preferences written here are read on the NEXT turn, not the current one.)
    try:
        ctx["user_profile"] = _profile_text(ctx)
    except Exception:
        ctx["user_profile"] = ""
    # Inject long-term conversation memory (compressed summaries of past sessions)
    try:
        chat_summaries = _load_chat_summaries()
        if chat_summaries:
            ctx["user_profile"] = str(ctx.get("user_profile") or "") + chat_summaries
    except Exception:
        pass
    if not str(ctx.get("last_intent") or "").strip():
        for m in reversed(ctx["history"]):
            if str(m.get("role") or "") == "assistant" and str(m.get("intent") or "").strip():
                ctx["last_intent"] = str(m.get("intent") or "")
                break
    # Dynamic gap-analysis resolution: parse natural user reply into strict memory K/V updates.
    if q:
        try:
            gap_res = resolve_dynamic_gap_answer(q, context=ctx)
            if int(gap_res.get("saved") or 0) > 0:
                ctx["dynamic_gap_saved"] = int(gap_res.get("saved") or 0)
        except Exception:
            pass

    if q or image_data_url:
        append_chat_message(session_id=session_id, role="user", text=q if q else "[Image uploaded]")
        mem = _get_memory_engine()
        if mem is not None and bool(getattr(mem, "available", False)):
            try:
                mem.memorize(
                    text=(q if q else "[Image uploaded]"),
                    metadata={
                        "source": "chat_user",
                        "session_id": session_id,
                        "role": "user",
                        "timestamp": dt.datetime.now().isoformat(),
                    },
                )
            except Exception:
                pass
    ctx = inject_runtime_context(ctx, query=q)

    # News search — only for real-time/current-events queries ("why is oil up today?")
    import logging as _logging
    _ai_log = _logging.getLogger("ai.realtime")
    try:
        _is_rt = bool(q and is_realtime_query(q))
        _ai_log.info("realtime_check query=%r is_realtime=%s", q[:80] if q else "", _is_rt)
        if _is_rt:
            _headlines = search_news(q, 6)
            ctx["live_news_text"] = format_news_text(_headlines)
            _ai_log.info("news_search results=%d text_len=%d", len(_headlines), len(ctx["live_news_text"]))
        else:
            ctx["live_news_text"] = ""
    except Exception as _nex:
        _ai_log.warning("news_search failed: %s", _nex)
        ctx["live_news_text"] = ""
    # Log macro snapshot status
    _macro = str(ctx.get("runtime", {}).get("macro_snapshot_text") or "")[:60]
    _ai_log.info("macro_snapshot preview=%r", _macro)

    image_bytes, mime_type = _parse_image_data_url(image_data_url)
    if image_bytes:
        try:
            profile = _profile_text(ctx)
        except Exception:
            profile = ""
        hist = _history_block(ctx, limit=30)
        system = (
            "You are a Senior Investment Strategist and Product Architect.\n"
            "Style: opinionated, proactive, concise.\n"
            "Reasoning: analyze intent/constraints step-by-step internally before answering.\n"
            "Never expose hidden reasoning or any <thought> content.\n"
            "Verification loop: if the response includes numbers, internally re-check them before final output.\n"
            "If user asks for dangerous action, warn and suggest a safer path.\n"
            f"User Profile: {profile}\n"
        )
        if hist:
            system += "\nRecent conversation:\n" + hist
        try:
            msg = str(
                ask_ai_vision(
                    prompt=(q or "Analyze this image and explain key investment signal."),
                    context=system,
                    image_bytes=image_bytes,
                    mime_type=(mime_type or "image/png"),
                    temperature=0.2,
                )
                or ""
            ).strip()
        except Exception:
            msg = ""
        if not msg:
            msg = "I could not extract enough signal from the image. Try a clearer screenshot."
        append_chat_message(
            session_id=session_id,
            role="assistant",
            text=msg,
            intent="vision_analysis",
            status="ok",
        )
        mem = _get_memory_engine()
        if mem is not None and bool(getattr(mem, "available", False)):
            try:
                mem.memorize(
                    text=msg,
                    metadata={
                        "source": "chat_assistant",
                        "session_id": session_id,
                        "role": "assistant",
                        "intent": "vision_analysis",
                        "timestamp": dt.datetime.now().isoformat(),
                    },
                )
            except Exception:
                pass
        return {
            "status": "ok",
            "intent": "vision_analysis",
            "message": msg,
            "confidence": 0.86,
            "redirect_url": "",
            "citations": [],
            "matched_by": "gemini_vision",
            "version": "v1.2.0",
            "traces": [{"step": "vision", "detail": "gemini_multimodal"}],
            "action": {"type": "NONE", "payload": {}},
            "ui": {},
            "interview_progress": get_interview_progress(),
        }
    res = run_ai_command(q, context=ctx)
    # Reflection memory: persist compact lesson from failed or unclear turns.
    try:
        st = str(res.status or "").strip().lower()
        if q and st in {"error", "timeout", "needs_clarification", "needs_input"}:
            lesson = (
                f"When user asked: '{q[:180]}', outcome was {st}/{str(res.intent or '').strip()}. "
                f"Prefer direct in-chat answer or one precise clarification; avoid dead-end loops."
            )
            _ = remember_compact_memory(
                text=lesson,
                bucket="process_rule",
                source="failure_reflection",
                reliability=0.74,
            )
    except Exception:
        pass
    # Global freshness gate: if user asks for current/now/today portfolio state,
    # enforce fresh live-market evidence before returning any numeric answer.
    if _requires_recent_data(q) and _looks_like_portfolio_query(q):
        live = {}
        rt = ctx.get("runtime") if isinstance(ctx.get("runtime"), dict) else {}
        if isinstance(rt.get("portfolio_live"), dict):
            live = dict(rt.get("portfolio_live") or {})
        if not live:
            try:
                live = dict(get_live_portfolio_summary() or {})
            except Exception:
                live = {}
        ok_fresh, reason = _is_fresh_portfolio_live(live)
        if not ok_fresh:
            res.status = "needs_input"
            res.intent = "portfolio_today_status"
            res.message = (
                "I can’t verify live market data for *today* right now. "
                "Please refresh market data/sync and ask again."
            )
            res.confidence = 0.35
            res.redirect_url = ""
            res.traces = list(res.traces or []) + [{"step": "freshness_gate", "detail": f"blocked:{reason}"}]
        else:
            res.status = "ok"
            res.intent = "portfolio_today_status"
            res.message = _portfolio_live_message(live)
            res.confidence = max(float(res.confidence or 0.0), 0.9)
            res.redirect_url = ""
            res.traces = list(res.traces or []) + [{"step": "freshness_gate", "detail": "pass"}]
    append_chat_message(
        session_id=session_id,
        role="assistant",
        text=str(res.message or ""),
        intent=str(res.intent or ""),
        status=str(res.status or ""),
    )
    mem = _get_memory_engine()
    if mem is not None and bool(getattr(mem, "available", False)):
        try:
            mem.memorize(
                text=str(res.message or ""),
                metadata={
                    "source": "chat_assistant",
                    "session_id": session_id,
                    "role": "assistant",
                    "intent": str(res.intent or ""),
                    "status": str(res.status or ""),
                    "timestamp": dt.datetime.now().isoformat(),
                },
            )
        except Exception:
            pass
    # Fire all learning side-effects in background after response is built
    if q:
        _fire_background_learning(q, list(ctx.get("history") or []), session_id)
    return {
        "status": res.status,
        "intent": res.intent,
        "message": res.message,
        "confidence": round(float(res.confidence or 0.0), 3),
        "redirect_url": res.redirect_url,
        "citations": res.citations or [],
        "matched_by": res.matched_by,
        "version": res.version,
        "traces": res.traces or [],
        "action": _build_action(res.intent, res.redirect_url),
        "execution": _build_execution_block(res.ui),
        "ui": res.ui or {},
        "interview_progress": get_interview_progress(),
    }


def _wrap_inline(r: dict) -> dict:
    """Wrap a sync/fast/cached result in the envelope the JS expects: {result: ..., job_id: ''}."""
    return {"ok": True, "status": "done", "result": r, "job_id": ""}


@router.post("/ai/command")
def ai_command(payload: dict = Body(default={})):  # simple JSON endpoint for command bar
    p = payload if isinstance(payload, dict) else {}
    q = str((p or {}).get("query") or "").strip()
    if q:
        fast = _fast_trade_history_reply(q)
        if isinstance(fast, dict):
            return _wrap_inline(fast)
        fast_db = _fast_db_reply(q, context=(p.get("context") if isinstance(p.get("context"), dict) else {}))
        if isinstance(fast_db, dict):
            return _wrap_inline(fast_db)
    cached = get_cached_result(p, ttl_sec=max(15, min(180, int(float(app_env("AI_PROMPT_CACHE_TTL_SEC", "75"))))))
    if isinstance(cached, dict) and cached:
        return _wrap_inline(cached)
    budget_ms = max(300, int(float(app_env("AI_COMMAND_SYNC_BUDGET_MS", "2500"))))
    ex = cf.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(_ai_command_sync, p)
        try:
            return _wrap_inline(fut.result(timeout=float(budget_ms) / 1000.0))
        except cf.TimeoutError:
            ensure_ai_job_queue_schema()
            jid = enqueue_job(p)
            return {
                "status": "queued",
                "intent": "analysis_queued",
                "message": "Analysis queued. Streaming result shortly.",
                "confidence": 0.6,
                "redirect_url": "",
                "citations": [],
                "matched_by": "sync_timeout_fallback",
                "version": "v1.2.0",
                "traces": [{"step": "route", "detail": "sync_timeout_to_queue"}],
                "action": {"type": "NONE", "payload": {}},
                "execution": {"type": "none"},
                "ui": {"job_id": jid, "type": "queued"},
                "job_id": jid,
                "interview_progress": get_interview_progress(),
            }
        except Exception:
            ensure_ai_job_queue_schema()
            jid = enqueue_job(p)
            return {
                "status": "queued",
                "intent": "analysis_queued",
                "message": "Analysis queued due to transient sync issue.",
                "confidence": 0.5,
                "redirect_url": "",
                "citations": [],
                "matched_by": "sync_exception_fallback",
                "version": "v1.2.0",
                "traces": [{"step": "route", "detail": "sync_exception_to_queue"}],
                "action": {"type": "NONE", "payload": {}},
                "execution": {"type": "none"},
                "ui": {"job_id": jid, "type": "queued"},
                "job_id": jid,
                "interview_progress": get_interview_progress(),
            }
    finally:
        # Do not wait for long-running sync branch to finish.
        ex.shutdown(wait=False, cancel_futures=True)


@router.post("/ai/command/async")
def ai_command_async(payload: dict = Body(default={})):
    ensure_ai_job_queue_schema()
    p = payload if isinstance(payload, dict) else {}
    q = str((p or {}).get("query") or "").strip()
    # Preserve ultra-fast deterministic routes without queue overhead.
    if q:
        fast = _fast_trade_history_reply(q)
        if isinstance(fast, dict):
            return {"ok": True, "status": "done", "result": fast, "job_id": ""}
        fast_db = _fast_db_reply(q, context=(p.get("context") if isinstance(p.get("context"), dict) else {}))
        if isinstance(fast_db, dict):
            return {"ok": True, "status": "done", "result": fast_db, "job_id": ""}
    cached = get_cached_result(p, ttl_sec=max(15, min(180, int(float(app_env("AI_PROMPT_CACHE_TTL_SEC", "75"))))))
    if isinstance(cached, dict) and cached:
        return {"ok": True, "status": "done", "result": cached, "job_id": ""}
    jid = enqueue_job(p)
    return {"ok": True, "status": "queued", "job_id": jid}


@router.get("/ai/command/sse/{job_id}")
def ai_command_sse(job_id: str):
    jid = str(job_id or "").strip()

    def _gen():
        sent_status = ""
        started = time.time()
        partial_sent = False
        while True:
            row = get_job(jid)
            if not isinstance(row, dict):
                yield "event: error\ndata: " + json.dumps({"ok": False, "error": "job_not_found"}) + "\n\n"
                break
            st = str(row.get("status") or "").strip().lower()
            if st != sent_status:
                sent_status = st
                yield "event: status\ndata: " + json.dumps({"ok": True, "job_id": jid, "status": st}) + "\n\n"
            if st == "done":
                yield "event: result\ndata: " + json.dumps({"ok": True, "job_id": jid, "result": row.get("result") or {}}) + "\n\n"
                break
            if st == "error":
                yield "event: error\ndata: " + json.dumps({"ok": False, "job_id": jid, "error": str(row.get("error") or "failed"), "result": row.get("result") or {}}) + "\n\n"
                break
            if (not partial_sent) and (time.time() - started) >= 3.0:
                partial_sent = True
                yield "event: partial\ndata: " + json.dumps({"ok": True, "job_id": jid, "message": "Analysis still running..."}) + "\n\n"
            yield "event: ping\ndata: {}\n\n"
            time.sleep(0.6)

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.post("/ai/memory/remember")
def ai_memory_remember(payload: dict = Body(default={})):
    text = str((payload or {}).get("text") or "").strip()
    bucket = str((payload or {}).get("bucket") or "process_rule").strip().lower()
    reliability = float((payload or {}).get("reliability") or 0.9)
    return remember_compact_memory(text=text, bucket=bucket, source="manual", reliability=reliability)


@router.post("/ai/memory/bootstrap-guardrails")
def ai_memory_bootstrap_guardrails():
    ensure_user_preferences_schema()
    pref_ok = 0
    mem_ok = 0
    rows: list[dict[str, str]] = []
    for k, v in _OPERATOR_GUARDRAILS:
        if upsert_user_preference(pref_key=k, pref_value=v, source="operator_bootstrap"):
            pref_ok += 1
        mem = remember_compact_memory(
            text=f"{k}: {v}",
            bucket="process_rule",
            source="operator_bootstrap",
            reliability=0.98,
        )
        if bool(mem.get("ok")):
            mem_ok += 1
        rows.append({"key": k, "value": v})
    return {
        "ok": True,
        "inserted_preferences": pref_ok,
        "inserted_memories": mem_ok,
        "guardrails": rows,
        "asof": dt.datetime.now().isoformat(),
    }


@router.post("/ai/feedback")
def ai_feedback(payload: dict = Body(default={})):
    """Store thumbs-up/down feedback for an AI response."""
    rating = str((payload or {}).get("rating") or "").strip().lower()
    if rating not in {"up", "down"}:
        return {"ok": False, "error": "rating must be 'up' or 'down'"}
    ok = store_ai_response_feedback(
        session_id=str((payload or {}).get("session_id") or ""),
        message_id=str((payload or {}).get("message_id") or ""),
        query_text=str((payload or {}).get("query_text") or ""),
        response_text=str((payload or {}).get("response_text") or ""),
        intent=str((payload or {}).get("intent") or ""),
        rating=rating,
        comment=str((payload or {}).get("comment") or ""),
    )
    return {"ok": ok}


@router.post("/ai/memory/forget")
def ai_memory_forget(payload: dict = Body(default={})):
    query = str((payload or {}).get("query") or "").strip()
    limit = int((payload or {}).get("limit") or 10)
    return forget_compact_memory(query=query, limit=limit)


@router.get("/ai/memory/list")
def ai_memory_list(q: str = Query(default=""), bucket: str = Query(default=""), limit: int = Query(default=24)):
    return {"rows": list_compact_memories(query=q, bucket=bucket, limit=limit, include_archived=False)}


@router.get("/ai/learning/status")
def ai_learning_status():
    return {
        "active_rules": list_active_rules(limit=40),
        "candidates": list_rule_candidates(limit=60),
    }


@router.post("/ai/learning/run")
def ai_learning_run():
    return run_learning_cycle()


@router.post("/ai/learning/promote")
def ai_learning_promote(payload: dict = Body(default={})):
    rule_key = str((payload or {}).get("rule_key") or "").strip()
    return promote_rule_candidate(rule_key)


@router.post("/api/portfolio/import-history")
def import_portfolio_history(payload: dict = Body(default={})): 
    if app_env("IMPORT_HISTORY_ENABLED", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return {"ok": False, "inserted": 0, "error": "disabled_import_history"}
    csv_text = str((payload or {}).get("csv_text") or "").strip()
    source = str((payload or {}).get("source") or "broker_csv").strip()[:64]
    if not csv_text:
        return {"ok": False, "inserted": 0, "error": "csv_text_required"}
    out = import_portfolio_history_csv(csv_text=csv_text, source=source)
    return out


@router.get("/ai/proactive")
def ai_proactive(name: str = Query(default="Arda")):
    try:
        return get_proactive_gap_prompt(user_name=name)
    except Exception as exc:
        return {
            "ok": False,
            "error": "proactive_gap_unavailable",
            "detail": str(exc),
            "prompt": "",
            "suggested_actions": [],
        }


@router.get("/ai/proactive/audit")
def ai_proactive_audit():
    return get_dynamic_gap_audit()


@router.get("/ai/morning_brief")
def ai_morning_brief():
    cached = get_cached_morning_brief()
    if cached:
        # Force regen if cached bullets contain filing-metadata garbage
        _garbage = ('10-K', '10-Q', '8-K', '6-K', '20-F')
        _cb = [str(b or "") for b in (cached.get("bullets") or [])]
        if not any(b.endswith(g) for b in _cb for g in _garbage):
            return cached
    return save_morning_brief_snapshot(limit_holdings=5, source="on_demand")


@router.post("/ai/task/complete")
def ai_complete_task(payload: dict = Body(default={})): 
    todo_id = int((payload or {}).get("todo_id") or 0)
    task_text = str((payload or {}).get("task_text") or "").strip()
    if todo_id > 0:
        ok = complete_task(todo_id)
        return {"ok": bool(ok), "id": todo_id if ok else 0}
    if task_text:
        return complete_task_by_text(task_text)
    return {"ok": False, "error": "todo_id_or_task_text_required"}


@router.get("/ai/quality/report")
def ai_quality_report(hours: int = Query(default=168), limit: int = Query(default=300)):
    ensure_ai_schema()
    return get_ai_quality_report(hours=hours, limit=limit)


@router.get("/ai/quality/policy")
def ai_quality_policy(refresh: int = Query(default=0)):
    ensure_ai_schema()
    return get_adaptive_turn_policy_snapshot(force_refresh=bool(int(refresh or 0)))


@router.get("/ai/worker/health")
def ai_worker_health():
    return worker_health(active_within_sec=120)


@router.get("/ai/phase2/status")
def ai_phase2_status():
    return phase2_status()


@router.post("/ai/phase2/bootstrap")
def ai_phase2_bootstrap():
    return ensure_phase2_postgres_schema()


@router.get("/ai/migration/status")
def ai_migration_status():
    return {
        "ok": True,
        "core_db_backend": core_backend(),
        "core_db_strict_postgres": strict_postgres_mode(),
        "core_db_guard": guard_core_backend_cutover(),
        "phase2": phase2_status(),
    }


@router.post("/ai/migration/bootstrap")
def ai_migration_bootstrap():
    return ensure_postgres_core_schema()


@router.post("/ai/migration/sync-universe")
def ai_migration_sync_universe(background: int = Query(default=1)):
    if int(background or 0) == 0:
        return sync_us_listed_universe_now()
    started = _sync_universe_start()
    with _SYNC_UNIVERSE_LOCK:
        return {
            "ok": True,
            "started": bool(started),
            "running": bool(_SYNC_UNIVERSE_STATE.get("running")),
            "started_at": str(_SYNC_UNIVERSE_STATE.get("started_at") or ""),
            "finished_at": str(_SYNC_UNIVERSE_STATE.get("finished_at") or ""),
            "last_error": str(_SYNC_UNIVERSE_STATE.get("last_error") or ""),
            "run_count": int(_SYNC_UNIVERSE_STATE.get("run_count") or 0),
        }


@router.get("/ai/migration/sync-universe/status")
def ai_migration_sync_universe_status():
    with _SYNC_UNIVERSE_LOCK:
        return {
            "ok": True,
            "running": bool(_SYNC_UNIVERSE_STATE.get("running")),
            "started_at": str(_SYNC_UNIVERSE_STATE.get("started_at") or ""),
            "finished_at": str(_SYNC_UNIVERSE_STATE.get("finished_at") or ""),
            "last_error": str(_SYNC_UNIVERSE_STATE.get("last_error") or ""),
            "last_result": _SYNC_UNIVERSE_STATE.get("last_result"),
            "run_count": int(_SYNC_UNIVERSE_STATE.get("run_count") or 0),
        }


@router.post("/ai/migration/guard-core")
def ai_migration_guard_core():
    return guard_core_backend_cutover()


@router.post("/ai/phase2/map-reduce/sector")
def ai_phase2_map_reduce_sector(payload: dict = Body(default={})):
    p = payload if isinstance(payload, dict) else {}
    tickers = list(p.get("tickers") or [])
    question = str(p.get("question") or "").strip()
    session_id = str((p.get("context") or {}).get("session_id") or p.get("session_id") or "").strip()
    return enqueue_sector_map_reduce(tickers=tickers, question=question, session_id=session_id)


@router.get("/ai/phase2/map-reduce/{reducer_job_id}")
def ai_phase2_map_reduce_status(reducer_job_id: str):
    snap = collect_map_reduce_snapshot(reducer_job_id)
    ctx = build_analysis_context(analysis_id=str(reducer_job_id or "").strip(), source="phase2_map_reduce")
    if isinstance(snap, dict):
        snap["analysis_context"] = ctx.to_dict()
    return snap


@router.get("/ai/phase2/map-reduce/report/{reducer_job_id}")
def ai_phase2_map_reduce_report(reducer_job_id: str):
    rep = get_map_reduce_report(reducer_job_id)
    ctx = build_analysis_context(analysis_id=str(reducer_job_id or "").strip(), source="phase2_map_reduce")
    if not isinstance(rep, dict):
        return {"ok": False, "error": "report_not_found", "reducer_job_id": str(reducer_job_id or ""), "analysis_context": ctx.to_dict()}
    return {
        "ok": True,
        "report": rep,
        "analysis_context": ctx.to_dict(),
        "chat_hydration": build_chat_hydration_payload(
            ctx,
            payload={
                "analysis_type": "phase2_map_reduce",
                "reducer_job_id": str(reducer_job_id or "").strip(),
            },
        ),
    }


@router.get("/ai/search/status")
def ai_search_status():
    """Return the active web search engine tier and whether Brave API key is configured."""
    import os
    try:
        engine = get_search_engine_status()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "engine": "unknown", "brave_configured": False}
    brave_configured = bool(os.environ.get("BRAVE_SEARCH_API_KEY", "").strip())
    tier_labels = {
        "brave": "Tier 1 — Brave Search API (industry standard)",
        "duckduckgo": "Tier 2 — DuckDuckGo free search",
        "rss_only": "Tier 3 — RSS headline scoring (fallback)",
    }
    return {
        "ok": True,
        "engine": engine,
        "tier": tier_labels.get(engine, engine),
        "brave_configured": brave_configured,
        "upgrade_tip": (
            "Set BRAVE_SEARCH_API_KEY env var to upgrade to Tier 1 (2 000 free queries/month at search.brave.com/app)"
            if not brave_configured else None
        ),
    }
