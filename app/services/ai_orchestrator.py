from __future__ import annotations

import datetime as dt
import concurrent.futures as cf
import time
import math
import subprocess
from difflib import SequenceMatcher
import json
import logging
from pathlib import Path
import re
import sqlite3
import uuid
from dataclasses import dataclass
from contextvars import ContextVar

from app.core.config import app_env
from app.core.config import ROOT
from app.core.db import core_conn as _conn, sqlite_retry
from app.core.normalize import normalize_text as _norm
from app.core.ticker import safe_ticker_flexible as _safe_ticker
from app.services.agent_service import ask_agent
from app.services.app_knowledge_service import format_app_knowledge, retrieve_app_knowledge
from app.services.ai_react_service import run_react_information
from app.services.observability_service import log_system_event
from app.services.blue_chip_service import (
    list_blue_chips_rows as _read_blue_chips_companies,
    remove_blue_chip as _remove_blue_chip,
    upsert_blue_chip as _upsert_blue_chip,
)
from app.services.memory_engine import OnyxMemory
from app.services.portfolio_memory_service import (
    backfill_trade_history,
    forget_compact_memory,
    get_cached_morning_brief,
    get_pending_interview_question,
    get_holdings,
    get_live_portfolio_summary,
    get_position_history,
    list_compact_memories,
    list_active_rules,
    list_rule_candidates,
    remember_compact_memory,
    save_morning_brief_snapshot,
    summarize_active_rules_for_prompt,
    summarize_compact_memory_for_prompt,
    get_watchlist_rationale,
    list_recent_portfolio_transactions,
    list_portfolio_transactions,
    save_thesis,
    start_portfolio_interview,
    submit_portfolio_interview_answer,
)
from app.services.organizer_service import (
    add_general_note,
    add_task,
    delete_notes,
    enqueue_action,
    get_daily_note,
    list_tasks,
    list_recent_notes,
    save_daily_note,
)
from app.services.reports_service import list_reports, read_report_file
try:
    from tools.llm_engine import ask_ai, ask_ai_with_tools
except Exception:  # pragma: no cover - optional at runtime
    ask_ai = None  # type: ignore[assignment]
    ask_ai_with_tools = None  # type: ignore[assignment]

from app.services.mini_statements_service import fetch_historical_financials
from app.services.postgres_core_service import strict_postgres_mode


@dataclass
class AICommandResult:
    status: str
    intent: str
    message: str
    confidence: float
    redirect_url: str = ""
    citations: list[dict[str, str]] | None = None
    matched_by: str = "rule"
    version: str = "v1.2.0"
    traces: list[dict[str, str]] | None = None
    ui: dict | None = None


ORCHESTRATOR_VERSION = "v1.2.0"
FUZZY_THRESHOLD = 0.74
OPEN_NOTES_FUZZY_THRESHOLD = 0.72
LOW_CONFIDENCE_THRESHOLD = 0.45
MID_CONFIDENCE_THRESHOLD = 0.70
LLM_FALLBACK_TIMEOUT_SEC = 6.0
MANAGER_LLM_TIMEOUT_SEC = 1.2
MANAGER_INTERRUPT_THRESHOLD = 0.82
ANALYST_MAX_CONTEXT_TOKENS = 800_000
WEEKDAY_TO_INT = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
ENTITY_ALIASES = {
    "google": "GOOGL",
    "alphabet": "GOOGL",
    "facebook": "META",
    "meta": "META",
    "tesla": "TSLA",
    "amazon": "AMZN",
    "apple": "AAPL",
    "microsoft": "MSFT",
    "netflix": "NFLX",
    "nvidia": "NVDA",
    "salesforce": "CRM",
    "hubspot": "HUBS",
    "adobe": "ADBE",
}
WATCHLIST_PATH = ROOT / "data" / "my_watchlist.txt"
WORKER_INTENTS = {
    "portfolio_interview",
    "list_watchlist",
    "list_portfolio",
    "list_blue_chips",
    "list_earnings",
    "list_notes",
    "list_reports",
    "list_tasks",
}
INTERRUPTIBLE_INTENTS = WORKER_INTENTS | {
    "summarize_current_report",
    "summarize_latest_report",
    "report_section",
    "notes_summary",
    "open_notes",
    "open_company",
    "search_reports",
    "morning_updates",
    "portfolio_today_status",
}
YES_NO_ONLY_RE = re.compile(
    r"^\s*(yes|yeah|yep|sure|ok|okay|open|open it|go ahead|do it|no|nope|nah|not now|later|cancel)\s*$"
)

logger = logging.getLogger(__name__)
_ADAPTIVE_POLICY_CACHE: dict[str, object] = {"ts": 0.0, "data": None}
_SEMANTIC_MEMORY: OnyxMemory | None = None
_TURN_RUNTIME: ContextVar[dict[str, object] | None] = ContextVar("_turn_runtime", default=None)


def _col_exists(con: sqlite3.Connection, table: str, col: str) -> bool:
    try:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
        for r in rows:
            name = str(r["name"] if isinstance(r, sqlite3.Row) else r[1]).strip()
            if name == col:
                return True
    except Exception:
        return False
    return False


def _get_semantic_memory() -> OnyxMemory | None:
    global _SEMANTIC_MEMORY
    if _SEMANTIC_MEMORY is None:
        try:
            _SEMANTIC_MEMORY = OnyxMemory()
        except Exception:
            _SEMANTIC_MEMORY = None
    return _SEMANTIC_MEMORY


@dataclass
class ParsedCommand:
    action: str = ""
    ticker: str = ""
    form: str = ""
    due_date: str = ""
    body: str = ""
    notes: str = ""


@dataclass
class Understanding:
    intent_class: str = ""
    confidence: float = 0.0
    topic: str = ""
    clarifying_question: str = ""
    missing_info: list[str] | None = None


@dataclass
class DecisionContract:
    intent_type: str = "chat"  # chat | action | mixed
    confidence: float = 0.0
    needs_clarification: bool = False
    proposed_action: str = ""
    reason: str = ""


@dataclass
class IntentCandidate:
    intent: str = ""
    confidence: float = 0.0
    source: str = "heuristic"
    ticker: str = ""
    due_date: str = ""
    rationale: str = ""


def ensure_ai_schema() -> None:
    if strict_postgres_mode():
        return
    con = _conn()
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS ai_action_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                query TEXT NOT NULL,
                intent TEXT NOT NULL,
                status TEXT NOT NULL,
                confidence REAL NOT NULL,
                payload_json TEXT NOT NULL
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_ai_action_created ON ai_action_log(created_at)")
        con.execute(
            """CREATE TABLE IF NOT EXISTS ai_tool_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                query TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                args_json TEXT NOT NULL,
                status TEXT NOT NULL,
                latency_ms REAL NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                trace_id TEXT NOT NULL DEFAULT '',
                model_name TEXT NOT NULL DEFAULT '',
                capability TEXT NOT NULL DEFAULT ''
            )"""
        )
        if not _col_exists(con, "ai_tool_log", "trace_id"):
            con.execute("ALTER TABLE ai_tool_log ADD COLUMN trace_id TEXT NOT NULL DEFAULT ''")
        if not _col_exists(con, "ai_tool_log", "model_name"):
            con.execute("ALTER TABLE ai_tool_log ADD COLUMN model_name TEXT NOT NULL DEFAULT ''")
        if not _col_exists(con, "ai_tool_log", "capability"):
            con.execute("ALTER TABLE ai_tool_log ADD COLUMN capability TEXT NOT NULL DEFAULT ''")
        con.execute("CREATE INDEX IF NOT EXISTS idx_ai_tool_created ON ai_tool_log(created_at)")
        con.execute(
            """CREATE TABLE IF NOT EXISTS ai_quality_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                query TEXT NOT NULL,
                detail_json TEXT NOT NULL
            )"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS risk_veto_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                trace_id TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL DEFAULT '',
                query TEXT NOT NULL DEFAULT '',
                verdict TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0.0,
                reason TEXT NOT NULL DEFAULT '',
                metrics_json TEXT NOT NULL DEFAULT '{}',
                detail_json TEXT NOT NULL DEFAULT '{}'
            )"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS risk_veto_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                config_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_ai_quality_created ON ai_quality_log(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_risk_veto_created ON risk_veto_decisions(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_risk_veto_trace ON risk_veto_decisions(trace_id)")
        row = con.execute("SELECT id FROM risk_veto_config WHERE id=1 LIMIT 1").fetchone()
        if not row:
            con.execute(
                "INSERT INTO risk_veto_config (id, config_json, updated_at) VALUES (1, ?, ?)",
                (json.dumps({}, ensure_ascii=True), dt.datetime.now().isoformat()),
            )
        con.commit()
    finally:
        con.close()


def _log_action(query: str, res: AICommandResult) -> None:
    if strict_postgres_mode():
        return
    payload = {
        "message": res.message,
        "redirect_url": res.redirect_url,
        "citations": res.citations or [],
        "matched_by": res.matched_by,
        "version": res.version,
        "traces": res.traces or [],
    }
    def _write() -> None:
        con = _conn()
        try:
            con.execute(
                """INSERT INTO ai_action_log (created_at, query, intent, status, confidence, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    dt.datetime.now().isoformat(),
                    str(query or "")[:4000],
                    str(res.intent or "unknown")[:120],
                    str(res.status or "ok")[:40],
                    float(res.confidence or 0.0),
                    json.dumps(payload, ensure_ascii=True),
                ),
            )
            con.commit()
        finally:
            con.close()
    sqlite_retry(_write)


def _log_tool_call(
    query: str,
    tool_name: str,
    args: dict[str, object],
    status: str,
    latency_ms: float,
    error: str = "",
    trace_id: str = "",
    model_name: str = "deterministic",
    capability: str = "",
) -> None:
    if strict_postgres_mode():
        return
    def _write() -> None:
        con = _conn()
        try:
            con.execute(
                """INSERT INTO ai_tool_log (created_at, query, tool_name, args_json, status, latency_ms, error, trace_id, model_name, capability)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    dt.datetime.now().isoformat(),
                    str(query or "")[:4000],
                    str(tool_name or "")[:120],
                    json.dumps(args or {}, ensure_ascii=True),
                    str(status or "ok")[:32],
                    float(latency_ms or 0.0),
                    str(error or "")[:500],
                    str(trace_id or "")[:120],
                    str(model_name or "")[:120],
                    str(capability or "")[:64],
                ),
            )
            con.commit()
        finally:
            con.close()
    sqlite_retry(_write)


def _log_quality_event(event_type: str, query: str, detail: dict[str, object] | None = None) -> None:
    _et = str(event_type or "unknown")[:120]
    _q = str(query or "")[:4000]
    _dj = json.dumps(detail or {}, ensure_ascii=True)
    _now = dt.datetime.now().isoformat()

    if strict_postgres_mode():
        try:
            from app.services.postgres_core_service import pg_connect
            con_pg = pg_connect()
            if con_pg is not None:
                try:
                    cur = con_pg.cursor()
                    cur.execute(
                        """INSERT INTO ai_quality_log_core
                           (id, created_at, event_type, query, detail_json)
                           VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM ai_quality_log_core),
                                   %s, %s, %s, %s::jsonb)""",
                        (_now, _et, _q, _dj),
                    )
                    con_pg.commit()
                finally:
                    con_pg.close()
        except Exception:
            pass
    else:
        def _write() -> None:
            con = _conn()
            try:
                con.execute(
                    """INSERT INTO ai_quality_log (created_at, event_type, query, detail_json)
                       VALUES (?, ?, ?, ?)""",
                    (_now, _et, _q, _dj),
                )
                con.commit()
            finally:
                con.close()
        sqlite_retry(_write)

    try:
        from app.services.proactive_ai_service import record_reflexion_from_quality_event

        record_reflexion_from_quality_event(
            event_type=str(event_type or ""),
            query=str(query or ""),
            detail=dict(detail or {}),
        )
    except Exception:
        pass


def _log_risk_veto_decision(
    trace_id: str,
    action: str,
    query: str,
    verdict: str,
    confidence: float,
    reason: str,
    metrics: dict[str, object] | None = None,
    detail: dict[str, object] | None = None,
) -> None:
    if strict_postgres_mode():
        return
    def _write() -> None:
        con = _conn()
        try:
            con.execute(
                """INSERT INTO risk_veto_decisions
                   (created_at, trace_id, action, query, verdict, confidence, reason, metrics_json, detail_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    dt.datetime.now().isoformat(),
                    str(trace_id or "")[:120],
                    str(action or "")[:80],
                    str(query or "")[:4000],
                    str(verdict or "")[:20],
                    float(confidence or 0.0),
                    str(reason or "")[:600],
                    json.dumps(metrics or {}, ensure_ascii=True),
                    json.dumps(detail or {}, ensure_ascii=True),
                ),
            )
            con.commit()
        finally:
            con.close()

    sqlite_retry(_write)


def _risk_veto_defaults() -> dict[str, object]:
    return {
        "enabled": True,
        "min_confidence_for_mutation": 0.62,
        "max_single_add_pct": 5.0,
        "max_position_weight_pct": 20.0,
        "max_var95_pct": 6.0,
        "max_cvar95_pct": 8.0,
        "min_quote_coverage_pct": 75.0,
        "high_impact_notional_pct": 3.0,
        "require_known_ticker_scope": True,
        "block_on_unknown_ticker": True,
        "review_for_high_impact": True,
    }


def get_risk_veto_config() -> dict[str, object]:
    if strict_postgres_mode():
        return dict(_risk_veto_defaults())
    ensure_ai_schema()
    cfg = dict(_risk_veto_defaults())
    con = _conn()
    try:
        row = con.execute("SELECT config_json FROM risk_veto_config WHERE id=1 LIMIT 1").fetchone()
        if row:
            raw = str(row["config_json"] or "").strip()
            if raw:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    cfg.update(obj)
    except Exception:
        pass
    finally:
        con.close()
    cfg["enabled"] = bool(cfg.get("enabled", True))
    for k in ("min_confidence_for_mutation", "max_single_add_pct", "max_position_weight_pct", "max_var95_pct", "max_cvar95_pct", "min_quote_coverage_pct", "high_impact_notional_pct"):
        try:
            cfg[k] = float(cfg.get(k) or _risk_veto_defaults()[k])  # type: ignore[index]
        except Exception:
            cfg[k] = float(_risk_veto_defaults()[k])  # type: ignore[index]
    cfg["require_known_ticker_scope"] = bool(cfg.get("require_known_ticker_scope", True))
    cfg["block_on_unknown_ticker"] = bool(cfg.get("block_on_unknown_ticker", True))
    cfg["review_for_high_impact"] = bool(cfg.get("review_for_high_impact", True))
    return cfg


def update_risk_veto_config(patch: dict[str, object]) -> dict[str, object]:
    if strict_postgres_mode():
        base = get_risk_veto_config()
        allowed = set(_risk_veto_defaults().keys())
        for k, v in dict(patch or {}).items():
            if k in allowed:
                base[k] = v
        return base
    base = get_risk_veto_config()
    allowed = set(_risk_veto_defaults().keys())
    for k, v in dict(patch or {}).items():
        if k not in allowed:
            continue
        base[k] = v
    # sanitize
    base["enabled"] = bool(base.get("enabled", True))
    for k, lo, hi in (
        ("min_confidence_for_mutation", 0.3, 0.95),
        ("max_single_add_pct", 0.25, 50.0),
        ("max_position_weight_pct", 1.0, 100.0),
        ("max_var95_pct", 0.5, 50.0),
        ("max_cvar95_pct", 1.0, 80.0),
        ("min_quote_coverage_pct", 1.0, 100.0),
        ("high_impact_notional_pct", 0.25, 50.0),
    ):
        try:
            base[k] = max(lo, min(hi, float(base.get(k) or _risk_veto_defaults()[k])))  # type: ignore[index]
        except Exception:
            base[k] = float(_risk_veto_defaults()[k])  # type: ignore[index]
    base["require_known_ticker_scope"] = bool(base.get("require_known_ticker_scope", True))
    base["block_on_unknown_ticker"] = bool(base.get("block_on_unknown_ticker", True))
    base["review_for_high_impact"] = bool(base.get("review_for_high_impact", True))

    def _write() -> None:
        con = _conn()
        try:
            con.execute(
                "UPDATE risk_veto_config SET config_json=?, updated_at=? WHERE id=1",
                (json.dumps(base, ensure_ascii=True), dt.datetime.now().isoformat()),
            )
            con.commit()
        finally:
            con.close()

    sqlite_retry(_write)
    return base


def get_ai_quality_report(hours: int = 168, limit: int = 300) -> dict[str, object]:
    h = max(1, min(24 * 30, int(hours or 168)))
    lim = max(1, min(2000, int(limit or 300)))
    since = (dt.datetime.now() - dt.timedelta(hours=h)).isoformat()

    rows: list[dict[str, object]] = []
    if strict_postgres_mode():
        try:
            from app.services.postgres_core_service import pg_connect
            con_pg = pg_connect()
            if con_pg is not None:
                try:
                    cur = con_pg.cursor()
                    cur.execute(
                        """SELECT created_at, event_type, query, detail_json
                           FROM ai_quality_log_core
                           WHERE created_at >= %s
                           ORDER BY id DESC
                           LIMIT %s""",
                        (since, lim),
                    )
                    cols = [d[0] for d in cur.description] if cur.description else []
                    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                finally:
                    con_pg.close()
        except Exception:
            pass
    else:
        con = _conn()
        try:
            rows = [
                dict(r)
                for r in con.execute(
                    """SELECT created_at, event_type, query, detail_json
                       FROM ai_quality_log
                       WHERE created_at >= ?
                       ORDER BY id DESC
                       LIMIT ?""",
                    (since, lim),
                ).fetchall()
            ]
        finally:
            con.close()

    counts: dict[str, int] = {}
    recent: list[dict[str, object]] = []
    for r in rows:
        event_type = str(r["event_type"] or "unknown")
        counts[event_type] = int(counts.get(event_type, 0)) + 1
        detail_obj: dict[str, object] = {}
        raw_detail = r.get("detail_json") if isinstance(r, dict) else r["detail_json"]
        if isinstance(raw_detail, dict):
            detail_obj = dict(raw_detail)
        elif raw_detail:
            try:
                parsed = json.loads(str(raw_detail))
                if isinstance(parsed, dict):
                    detail_obj = dict(parsed)
            except Exception:
                detail_obj = {}
        recent.append(
            {
                "created_at": str(r["created_at"] or ""),
                "event_type": event_type,
                "query": str(r["query"] or ""),
                "detail": detail_obj,
            }
        )

    by_event = [{"event_type": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    return {
        "window_hours": h,
        "since": since,
        "total_events": len(recent),
        "by_event": by_event,
        "recent": recent[:80],
    }


def list_recent_risk_veto_decisions(limit: int = 30) -> list[dict[str, object]]:
    if strict_postgres_mode():
        return []
    lim = max(1, min(300, int(limit or 30)))
    con = _conn()
    try:
        rows = con.execute(
            """SELECT created_at, trace_id, action, query, verdict, confidence, reason, metrics_json
               FROM risk_veto_decisions
               ORDER BY id DESC
               LIMIT ?""",
            (lim,),
        ).fetchall()
    finally:
        con.close()
    out: list[dict[str, object]] = []
    for r in rows:
        mj = {}
        try:
            mj = json.loads(str(r["metrics_json"] or "{}"))
        except Exception:
            mj = {}
        out.append(
            {
                "created_at": str(r["created_at"] or ""),
                "trace_id": str(r["trace_id"] or ""),
                "action": str(r["action"] or ""),
                "query": str(r["query"] or ""),
                "verdict": str(r["verdict"] or ""),
                "confidence": float(r["confidence"] or 0.0),
                "reason": str(r["reason"] or ""),
                "metrics": mj if isinstance(mj, dict) else {},
            }
        )
    return out


def _compute_adaptive_turn_thresholds_from_quality(hours: int = 72, limit: int = 600) -> dict[str, float]:
    base = {
        "force_action_min_conf": 0.45,
        "decision_action_min_conf": 0.68,
        "clarify_min_conf": 0.48,
        "mutation_min_conf": 0.55,
    }
    report = get_ai_quality_report(hours=hours, limit=limit)
    counts = {str(x.get("event_type") or ""): int(x.get("count") or 0) for x in list(report.get("by_event") or [])}
    total = max(1, int(report.get("total_events") or 0))
    n_clar = int(counts.get("needs_clarification", 0))
    n_gate = int(counts.get("action_confidence_gate", 0))
    clar_rate = float(n_clar) / float(total)
    gate_rate = float(n_gate) / float(total)

    # If action gate fires too often, become stricter on action execution.
    if gate_rate >= 0.20:
        base["decision_action_min_conf"] += 0.04
        base["mutation_min_conf"] += 0.04
    elif gate_rate >= 0.10:
        base["decision_action_min_conf"] += 0.02
        base["mutation_min_conf"] += 0.02

    # If clarification rate is very high, relax slightly to avoid annoying loops.
    if clar_rate >= 0.40:
        base["clarify_min_conf"] -= 0.03
        base["decision_action_min_conf"] -= 0.01
    elif clar_rate <= 0.08:
        # Very low clarification suggests over-committing. Ask more when uncertain.
        base["clarify_min_conf"] += 0.02

    # Keep thresholds in safe, stable bounds.
    base["force_action_min_conf"] = max(0.35, min(0.60, float(base["force_action_min_conf"])))
    base["decision_action_min_conf"] = max(0.58, min(0.82, float(base["decision_action_min_conf"])))
    base["clarify_min_conf"] = max(0.38, min(0.62, float(base["clarify_min_conf"])))
    base["mutation_min_conf"] = max(0.48, min(0.75, float(base["mutation_min_conf"])))
    return base


def get_adaptive_turn_policy_snapshot(force_refresh: bool = False) -> dict[str, object]:
    now = time.time()
    if not force_refresh:
        ts = float(_ADAPTIVE_POLICY_CACHE.get("ts") or 0.0)
        data = _ADAPTIVE_POLICY_CACHE.get("data")
        if data and (now - ts) <= 120.0:
            return dict(data) if isinstance(data, dict) else {}

    thresholds = _compute_adaptive_turn_thresholds_from_quality(hours=72, limit=600)
    snap: dict[str, object] = {
        "thresholds": thresholds,
        "window_hours": 72,
        "sample_limit": 600,
        "updated_at_epoch": now,
    }
    _ADAPTIVE_POLICY_CACHE["ts"] = now
    _ADAPTIVE_POLICY_CACHE["data"] = dict(snap)
    return snap


def _extract_ticker(text: str) -> str:
    s = str(text or "")
    # Prefer explicit patterns like "company ADBE" or "ticker adbe".
    m = re.search(r"\b(?:company|ticker)\s+\$?([A-Za-z]{1,5})\b", s, flags=re.I)
    if m:
        return m.group(1).upper()
    # Then accept $TICKER form.
    m = re.search(r"\$([A-Za-z]{1,5})\b", s)
    if m:
        return m.group(1).upper()
    # Company alias resolution (google -> GOOGL, facebook -> META, etc).
    low = _norm(s)
    for alias, tk in ENTITY_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", low):
            return tk
    # DB-backed company name resolution.
    tk_from_db = _resolve_ticker_from_company_name(low)
    if tk_from_db:
        return tk_from_db
    tk_from_hint = _resolve_ticker_from_company_hint(s)
    if tk_from_hint:
        return tk_from_hint
    # Fallback: choose first non-stopword ALL-CAPS 2-5 char token only.
    # This prevents accidental extraction from natural words like "can", "read", etc.
    stop = {
        "OPEN", "COMPANY", "TICKER", "SHOW", "RESEARCH", "ADD", "TASK",
        "NOTE", "DRAFT", "SUMMARIZE", "LATEST", "REPORT", "REPORTS", "FIND",
        "SEARCH", "DELETE", "UPDATE", "THESIS", "READ", "BUY", "SELL",
        "HOLD", "WATCH", "WRITE", "LIST", "MY", "ME", "DAILY", "BRIEF",
        "BRIEFING", "LASTEST", "SUMMIRAZE", "THIS", "TO", "ABOUT", "LOG",
        "NOTES", "REMOVE", "FROM", "FOR", "THE", "ALL", "WHY", "WHAT",
        "WHEN", "WHO", "WHICH", "HOW", "IS", "ARE", "DO", "DID", "WE",
        "OUR", "IN", "ON", "AT", "BY", "SAVE", "SET",
        "OWN",
    }
    for tok in re.findall(r"\b([A-Z]{2,5})\b", s):
        if tok in stop:
            continue
        return tok
    return ""


def _extract_tickers_bulk(text: str) -> list[str]:
    s = str(text or "")
    out: list[str] = []
    seen: set[str] = set()
    noise_tokens = {
        "YES",
        "NO",
        "OK",
        "OPEN",
        "NOW",
        "CANCEL",
        "BACK",
        "CHAT",
        "REMOVE",
        "ADD",
        "FROM",
        "TO",
        "BLUE",
        "CHIPS",
        "CHIP",
        "LIST",
        "MY",
        "THE",
        "PLEASE",
    }

    # Prefer explicit ticker format in parentheses: Company (TICKER)
    for m in re.findall(r"\(([A-Za-z0-9.\-]{1,12})\)", s):
        tk = _safe_ticker(m)
        if not tk or tk in seen:
            continue
        seen.add(tk)
        out.append(tk)

    # Also include known aliases mentioned as plain words (e.g., google).
    low = _norm(s)
    for alias, tk0 in ENTITY_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", low):
            tk = _safe_ticker(tk0)
            if tk and tk not in seen:
                seen.add(tk)
                out.append(tk)

    # Parse ticker-like tokens directly from text (e.g., "remove CRM from blue chips").
    for m in re.findall(r"\b([A-Za-z0-9.\-]{1,12})\b", s):
        raw = str(m or "").strip()
        if not raw:
            continue
        up = raw.upper()
        if up in noise_tokens:
            continue
        tk = _safe_ticker(up)
        if not tk or tk in seen:
            continue
        seen.add(tk)
        out.append(tk)

    # Fallback: if none found, reuse single-ticker extractor.
    if not out:
        one = _safe_ticker(_extract_ticker(s))
        if one:
            out.append(one)
    return out


def _is_known_ticker_for_user_scope(ticker: str) -> bool:
    tk = _safe_ticker(ticker)
    if not tk:
        return False
    # Validate against app data instead of hardcoded word blacklists.
    try:
        con = _conn()
        try:
            row = con.execute("SELECT 1 FROM companies WHERE UPPER(ticker)=? LIMIT 1", (tk,)).fetchone()
            if row:
                return True
        finally:
            con.close()
    except Exception:
        pass
    try:
        for r in get_holdings(limit=200):
            if _safe_ticker(str(r.get("ticker") or "")) == tk:
                return True
    except Exception:
        pass
    try:
        p = ROOT / "data" / "my_watchlist.txt"
        if p.exists():
            for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                s = str(ln or "").strip()
                if not s or s.startswith("#"):
                    continue
                parts = [x.strip() for x in s.split(",")]
                cand = _safe_ticker(parts[0] if parts else "")
                if cand == tk:
                    return True
    except Exception:
        pass
    return False


def _extract_ticker_for_sec_query(text: str) -> str:
    s = str(text or "").strip()
    if not s:
        return ""
    # Explicit ticker/company syntax should be accepted as-is.
    explicit = re.search(r"\b(?:company|ticker)\s+\$?([A-Za-z]{1,5})\b", s, flags=re.I)
    if explicit:
        return _safe_ticker(explicit.group(1))
    dollar = re.search(r"\$([A-Za-z]{1,5})\b", s)
    if dollar:
        return _safe_ticker(dollar.group(1))
    # Prioritize symbols adjacent to filing forms to avoid false positives
    # from conversational words (e.g., "can", "lets").
    form_re = r"(?:10[\s-]?k|10[\s-]?q|8[\s-]?k)"
    checks = [
        re.search(rf"\b([A-Za-z]{{1,5}})\s+{form_re}\b", s, flags=re.I),
        re.search(rf"\b{form_re}\s+(?:for|of)\s+([A-Za-z]{{1,5}})\b", s, flags=re.I),
        re.search(rf"\b(?:for|of)\s+([A-Za-z]{{1,5}})\s+{form_re}\b", s, flags=re.I),
    ]
    for m in checks:
        if not m:
            continue
        tk = _safe_ticker(m.group(1))
        if not tk:
            continue
        if _is_known_ticker_for_user_scope(tk):
            return tk
    return ""


def _resolve_ticker_from_company_name(norm_text: str) -> str:
    try:
        con = _conn()
        try:
            rows = con.execute(
                "SELECT ticker, name FROM companies WHERE name IS NOT NULL AND name <> '' LIMIT 1500"
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return ""
    ntext = str(norm_text or "")
    for r in rows:
        name = _norm(str(r["name"] or ""))
        ticker = str(r["ticker"] or "").strip().upper()
        if not name or not ticker:
            continue
        if len(name) < 4:
            continue
        if name in ntext:
            return ticker
    return ""


def _resolve_ticker_from_company_hint(text: str) -> str:
    q = str(text or "").strip()
    if not q:
        return ""
    low = _norm(q)
    # Capture common intent tails: "from Salesforce", "for Adobe", "about Apple"
    m = re.search(r"\b(?:from|for|about)\s+([A-Za-z][A-Za-z0-9&\.\-\s]{1,40})$", q, flags=re.I)
    phrase = str(m.group(1) if m else "").strip(" .,:;")
    if not phrase:
        return ""
    cand = phrase.split()
    phrase_short = " ".join(cand[:3]).strip()
    if not phrase_short:
        return ""
    try:
        con = _conn()
        try:
            # 1) Exact ticker
            up = phrase_short.upper()
            row = con.execute(
                "SELECT ticker FROM companies WHERE UPPER(ticker)=? LIMIT 1",
                (up,),
            ).fetchone()
            if row and str(row["ticker"] or "").strip():
                return str(row["ticker"]).strip().upper()
            # 2) Company name contains phrase
            like = "%" + _norm(phrase_short).replace(" ", "%") + "%"
            rows = con.execute(
                "SELECT ticker, name FROM companies WHERE LOWER(name) LIKE ? LIMIT 20",
                (like.replace("%", "%"),),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return ""
    if not rows:
        return ""
    # Prefer shortest matching company name (usually closest entity mention)
    best = sorted(
        (
            (str(r["ticker"] or "").strip().upper(), len(str(r["name"] or "")))
            for r in rows
            if str(r["ticker"] or "").strip()
        ),
        key=lambda x: x[1],
    )
    return best[0][0] if best else ""


def _next_weekday(base: dt.date, target_weekday: int) -> dt.date:
    delta = (target_weekday - base.weekday()) % 7
    if delta == 0:
        delta = 7
    return base + dt.timedelta(days=delta)


def _next_business_day(base: dt.date) -> dt.date:
    d = base + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d = d + dt.timedelta(days=1)
    return d


def _normalize_due_weekend(day: dt.date) -> dt.date:
    if day.weekday() == 5:
        return day + dt.timedelta(days=2)
    if day.weekday() == 6:
        return day + dt.timedelta(days=1)
    return day


def _extract_due_date(task_text: str) -> tuple[str, str]:
    txt = str(task_text or "").strip()
    low = _norm(txt)
    today = dt.date.today()
    due: dt.date | None = None
    if "today" in low:
        due = _normalize_due_weekend(today)
        txt = re.sub(r"\btoday\b", "", txt, flags=re.I).strip(" ,.-")
    elif "tomorrow" in low:
        due = _next_business_day(today)
        txt = re.sub(r"\btomorrow\b", "", txt, flags=re.I).strip(" ,.-")
    elif "next week" in low:
        due = _next_weekday(today, 0)
        txt = re.sub(r"\bnext\s+week\b", "", txt, flags=re.I).strip(" ,.-")
    else:
        for wd, wd_int in WEEKDAY_TO_INT.items():
            if re.search(rf"\b{wd}\b", low):
                due = _next_weekday(today, wd_int)
                txt = re.sub(rf"\b{wd}\b", "", txt, flags=re.I).strip(" ,.-")
                break
    due_str = due.isoformat() if due else ""
    return due_str, txt.strip()


def _apply_confidence_policy(res: AICommandResult) -> AICommandResult:
    c = float(res.confidence or 0.0)
    # Keep deterministic CRUD intents fast unless confidence is truly low.
    if c < LOW_CONFIDENCE_THRESHOLD:
        res.status = "needs_clarification"
        res.message = f"Low confidence. Please clarify: {res.message}"
        return res
    if LOW_CONFIDENCE_THRESHOLD <= c < MID_CONFIDENCE_THRESHOLD:
        # Only unknown intent needs confirmation. LLM/text answers are read-only.
        if res.intent in {"unknown"}:
            res.status = "needs_confirmation"
            res.message = "Please confirm before I act: " + res.message
        return res
    return res


def _looks_like_fresh_command(q: str) -> bool:
    low = _norm(q)
    if not low:
        return False
    starts = (
        "add task",
        "task:",
        "todo:",
        "add note",
        "note:",
        "draft note",
        "open company",
        "show me my notes",
        "summarize",
        "summary",
        "summiraze",
        "find ",
        "search ",
        "open ",
        "go to ",
        "take me ",
        "take me to ",
        "where is ",
        "what page ",
        "main page",
        "report",
        "reports",
        "daily log",
        "add this to daily log",
    )
    return any(low.startswith(s) for s in starts)


def _is_confirmation_text(text: str) -> bool:
    low = _norm(text)
    return bool(YES_NO_ONLY_RE.fullmatch(low))


def _looks_like_correction(text: str) -> bool:
    low = _norm(text)
    if not low:
        return False
    return bool(
        re.search(r"\b(not this|not that|instead|i meant|you should|dont do|don't do|wrong)\b", low)
        or re.search(r"\bthis is wrong\b", low)
    )


def _understand_user_turn(query: str, context: dict | None = None) -> Understanding:
    q = str(query or "").strip()
    low = _norm(q)
    ctx = context or {}
    if not q:
        return Understanding(intent_class="empty", confidence=1.0, topic="none", clarifying_question="", missing_info=[])
    if _is_confirmation_text(q):
        return Understanding(intent_class="confirmation", confidence=0.98, topic="followup", clarifying_question="", missing_info=[])
    if _looks_like_correction(q):
        return Understanding(intent_class="correction", confidence=0.92, topic="preference", clarifying_question="", missing_info=[])

    # Lightweight heuristic fallback if LLM classify is unavailable.
    questionish = bool(
        re.search(r"\b(what|why|how|which|who|when|vs|versus|better|status|today|explain|summarize|compare)\b", low)
        or low.endswith("?")
    )
    actionish = bool(
        re.match(r"^\s*(add|remove|delete|open|go to|navigate|take me|create|save|update|set|sync|show)\b", low)
        and not questionish
    )
    fallback_intent = "action" if actionish else "question"
    fallback_conf = 0.58 if fallback_intent == "action" else 0.68
    fallback_topic = "general"
    if _contains_portfolio_term(low):
        fallback_topic = "portfolio"
    elif _contains_watchlist_term(low):
        fallback_topic = "watchlist"
    elif _contains_blue_chip_term(low):
        fallback_topic = "blue_chips"
    elif "report" in low:
        fallback_topic = "reports"
    elif "note" in low:
        fallback_topic = "notes"

    if ask_ai is None:
        cq_fb = ""
        if fallback_intent == "action" and not re.search(r"\b(to|for|from|in)\b", low):
            cq_fb = "Do you want an answer here in chat, or should I perform an app action?"
        return Understanding(
            intent_class=fallback_intent,
            confidence=fallback_conf,
            topic=fallback_topic,
            clarifying_question=cq_fb,
            missing_info=[],
        )

    prompt = (
        "Classify user message for an in-app copilot. Return strict JSON only:\n"
        '{"intent_class":"question|action|correction|confirmation|chitchat","confidence":0.0,'
        '"topic":"portfolio|watchlist|blue_chips|notes|reports|company|tasks|general",'
        '"missing_info":["..."],"clarifying_question":"..."}\n'
        "Rules: If user asks for information, comparison, explanation, or status, set intent_class=question. "
        "Default ambiguous input to question (conversation-first). "
        "If user edits prior instruction (not this / i meant), set correction. "
        "If only yes/no, set confirmation. "
        "Use action only when user clearly asks to execute app changes/navigation.\n"
        f"Last intent: {str(ctx.get('last_intent') or '')}\n"
        f"Message: {q}"
    )
    try:
        with cf.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(
                ask_ai,
                prompt,
                "Conversation understanding classifier. JSON only.",
                "fast",
                True,
                0.0,
            )
            raw = str(fut.result(timeout=1.5) or "").strip()
        obj = json.loads(raw)
        it = str(obj.get("intent_class") or fallback_intent).strip().lower()
        if it not in {"question", "action", "correction", "confirmation", "chitchat"}:
            it = fallback_intent
        cfv = float(obj.get("confidence") or fallback_conf)
        topic = str(obj.get("topic") or fallback_topic).strip().lower()
        if topic not in {"portfolio", "watchlist", "blue_chips", "notes", "reports", "company", "tasks", "general"}:
            topic = fallback_topic
        missing = [str(x).strip() for x in list(obj.get("missing_info") or []) if str(x).strip()][:4]
        cq = str(obj.get("clarifying_question") or "").strip()
        if not cq and it == "action" and cfv < 0.62:
            cq = "Do you want me to execute an app action, or just answer in chat?"
        return Understanding(
            intent_class=it,
            confidence=max(0.0, min(1.0, cfv)),
            topic=topic,
            clarifying_question=cq[:220],
            missing_info=missing,
        )
    except Exception:
        return Understanding(
            intent_class=fallback_intent,
            confidence=fallback_conf,
            topic=fallback_topic,
            clarifying_question="",
            missing_info=[],
        )


def _learn_from_correction(query: str) -> None:
    q = str(query or "").strip()
    if not q:
        return
    txt = f"User correction pattern: {q}"
    try:
        remember_compact_memory(
            text=txt[:600],
            bucket="interaction_preference",
            source="chat_correction",
            reliability=0.95,
        )
    except Exception:
        pass


def _semantic_memory_lines(query: str, limit: int = 6) -> list[str]:
    mem = _get_semantic_memory()
    if mem is None or not bool(getattr(mem, "available", False)):
        return []
    q = str(query or "").strip()
    if not q:
        return []
    try:
        rows = mem.recall(q, n_results=max(1, min(12, int(limit or 6))))
    except Exception:
        return []
    out: list[str] = []
    for r in rows[: max(1, min(12, int(limit or 6)))]:
        txt = str((r or {}).get("text") or "").strip().replace("\n", " ")
        md = (r or {}).get("metadata") if isinstance((r or {}).get("metadata"), dict) else {}
        src = str((md or {}).get("source") or "").strip()
        if not txt:
            continue
        label = f"[{src}] " if src else ""
        out.append(f"- {label}{txt[:200]}")
    return out


def _compose_context_packet(ctx: dict | None, query: str) -> str:
    c = ctx or {}
    runtime = c.get("runtime") if isinstance(c.get("runtime"), dict) else {}
    page = runtime.get("page_hint") if isinstance(runtime.get("page_hint"), dict) else {}
    live = runtime.get("portfolio_live") if isinstance(runtime.get("portfolio_live"), dict) else {}
    hist = c.get("history") if isinstance(c.get("history"), list) else []
    pref = str(c.get("user_prefs") or "").strip()
    profile = str(c.get("user_profile") or "").strip()
    mem = summarize_compact_memory_for_prompt(query=query, limit=10)
    sem = _semantic_memory_lines(query, limit=6)
    rules = summarize_active_rules_for_prompt(query=query, limit=8)
    h: list[str] = []
    for m in hist[-16:]:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip().lower()
        txt = str(m.get("text") or "").strip()
        if role in {"user", "assistant"} and txt:
            h.append(f"{role.upper()}: {txt[:180]}")
    return (
        f"profile={profile[:280]}\n"
        f"preferences={pref[:240]}\n"
        f"page={str(page.get('title') or '-')}: {str(page.get('content') or '-')[:180]}\n"
        f"portfolio_live=holdings:{int(live.get('holdings_count') or 0)}, day:{float(live.get('day_change_pct') or 0.0):+.2f}%\n"
        f"rules={rules[:260]}\n"
        f"memory={mem[:260]}\n"
        + (("semantic_memory:\n" + "\n".join(sem) + "\n") if sem else "")
        + ("history:\n" + "\n".join(h) if h else "history:(none)")
    )


def _decide_turn_contract(query: str, understanding: Understanding, parsed: ParsedCommand, context: dict | None = None) -> DecisionContract:
    q = str(query or "").strip()
    low = _norm(q)
    ctx = context or {}
    if not q:
        return DecisionContract(intent_type="chat", confidence=1.0, needs_clarification=True, reason="empty")
    if understanding.intent_class == "correction":
        return DecisionContract(intent_type="chat", confidence=max(0.7, understanding.confidence), needs_clarification=False, reason="correction")
    if understanding.intent_class == "confirmation":
        return DecisionContract(intent_type="action", confidence=0.95, needs_clarification=False, reason="confirmation")

    explicit_action_cmd = bool(
        re.match(r"^\s*(add|remove|delete|open|go to|navigate|take me|create|save|update|set|sync)\b", low)
    )
    heur_action = bool(parsed.action) or explicit_action_cmd
    heur_question = bool(re.search(r"\b(what|why|how|which|who|when|better|vs|versus|explain|summarize|status|today)\b", low))
    fallback_type = "action" if heur_action and not heur_question else "chat"
    fallback_conf = max(0.45, float(understanding.confidence or 0.0))
    if ask_ai is None:
        return DecisionContract(
            intent_type=fallback_type,
            confidence=fallback_conf,
            needs_clarification=(fallback_type == "action" and fallback_conf < 0.62),
            proposed_action=parsed.action or "",
            reason="fallback",
        )

    prompt = (
        "Decide turn policy for in-app copilot. Return strict JSON only:\n"
        '{"intent_type":"chat|action|mixed","confidence":0.0,"needs_clarification":false,"proposed_action":"",'
        '"reason":""}\n'
        "Policy: default to chat unless user clearly requests an app action.\n"
        "Use action only for explicit execution/navigation commands.\n"
        "If intent is mixed or uncertain, set needs_clarification=true.\n"
        f"understanding={understanding.intent_class}:{understanding.confidence:.2f}\n"
        f"parsed_action={parsed.action or '-'}\n"
        f"context=\n{_compose_context_packet(ctx, q)}\n"
        f"user={q}"
    )
    try:
        with cf.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(
                ask_ai,
                prompt,
                "Turn policy contract classifier. JSON only.",
                "fast",
                True,
                0.0,
            )
            raw = str(fut.result(timeout=1.6) or "").strip()
        obj = json.loads(raw)
        it = str(obj.get("intent_type") or fallback_type).strip().lower()
        if it not in {"chat", "action", "mixed"}:
            it = fallback_type
        conf = float(obj.get("confidence") or fallback_conf)
        clar = bool(obj.get("needs_clarification"))
        pa = str(obj.get("proposed_action") or parsed.action or "").strip().lower()
        rs = str(obj.get("reason") or "").strip()[:180]
        if it == "action" and not parsed.action and not explicit_action_cmd and understanding.intent_class == "question":
            it = "chat"
            clar = False
            rs = (rs + " | downgraded_to_chat").strip(" |")
        if it == "action" and conf < 0.62:
            clar = True
        return DecisionContract(
            intent_type=it,
            confidence=max(0.0, min(1.0, conf)),
            needs_clarification=clar,
            proposed_action=pa,
            reason=rs or "llm",
        )
    except Exception:
        return DecisionContract(
            intent_type=fallback_type,
            confidence=fallback_conf,
            needs_clarification=(fallback_type == "action" and fallback_conf < 0.62),
            proposed_action=parsed.action or "",
            reason="fallback_exception",
        )


def _heuristic_intent_candidates(query: str, parsed: ParsedCommand) -> list[IntentCandidate]:
    q = str(query or "").strip()
    low = _norm(q)
    out: list[IntentCandidate] = []
    if parsed.action:
        out.append(
            IntentCandidate(
                intent=str(parsed.action or "").strip(),
                confidence=0.86,
                source="strict_parse",
                ticker=str(parsed.ticker or "").strip().upper(),
                due_date=str(parsed.due_date or "").strip(),
                rationale="strict_parse_match",
            )
        )
    if any(k in low for k in {"remind me", "reminder", "to do", "todo", "task"}) and not parsed.action:
        out.append(
            IntentCandidate(
                intent="add_task",
                confidence=0.72,
                source="heuristic",
                ticker=str(_extract_ticker(q) or "").strip().upper(),
                due_date=str(_extract_due_date(q)[0] or "").strip(),
                rationale="task_language",
            )
        )
    if any(k in low for k in {"daily log", "journal", "log this", "log that"}) and not parsed.action:
        out.append(
            IntentCandidate(
                intent="add_daily_log",
                confidence=0.70,
                source="heuristic",
                ticker="",
                due_date="",
                rationale="daily_log_language",
            )
        )
    if any(k in low for k in {"note this", "save a note", "note about", "draft note"}) and not parsed.action:
        out.append(
            IntentCandidate(
                intent="add_note_draft",
                confidence=0.69,
                source="heuristic",
                ticker=str(_extract_ticker(q) or "").strip().upper(),
                due_date="",
                rationale="note_language",
            )
        )
    if _contains_blue_chip_term(low) and any(k in low for k in {"add", "remove", "delete", "drop", "include", "put"}) and not parsed.action:
        out.append(
            IntentCandidate(
                intent="manage_blue_chips",
                confidence=0.78,
                source="heuristic",
                ticker=str(_extract_ticker(q) or "").strip().upper(),
                due_date="",
                rationale="blue_chip_language",
            )
        )
    return out[:3]


def _llm_intent_candidates(query: str, context: dict | None = None) -> list[IntentCandidate]:
    q = str(query or "").strip()
    if not q or ask_ai is None:
        return []
    ctx = context or {}
    prompt = (
        "Generate top intent candidates for this message. Return strict JSON only:\n"
        '{"candidates":[{"intent":"add_task|add_note_draft|add_daily_log|manage_blue_chips|none","confidence":0.0,'
        '"ticker":"",\"due_date\":\"\",\"rationale\":\"\"}]}\n'
        "Rules:\n"
        "- Max 3 candidates sorted by confidence desc.\n"
        "- Use intent='none' if no mutation intent is likely.\n"
        "- Confidence 0..1.\n"
        f"Last intent: {str(ctx.get('last_intent') or '')}\n"
        f"Message: {q}"
    )
    try:
        with cf.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(
                ask_ai,
                prompt,
                "Intent candidate generator for safe action routing. JSON only.",
                "fast",
                True,
                0.0,
            )
            raw = str(fut.result(timeout=1.4) or "").strip()
        obj = json.loads(raw)
        arr = obj.get("candidates") if isinstance(obj, dict) else []
        out: list[IntentCandidate] = []
        for it in list(arr or [])[:3]:
            if not isinstance(it, dict):
                continue
            name = str(it.get("intent") or "").strip().lower()
            if name not in {"add_task", "add_note_draft", "add_daily_log", "manage_blue_chips", "none"}:
                continue
            if name == "none":
                continue
            conf = max(0.0, min(1.0, float(it.get("confidence") or 0.0)))
            out.append(
                IntentCandidate(
                    intent=name,
                    confidence=conf,
                    source="llm",
                    ticker=str(it.get("ticker") or "").strip().upper(),
                    due_date=str(it.get("due_date") or "").strip(),
                    rationale=str(it.get("rationale") or "").strip()[:120],
                )
            )
        return out
    except Exception:
        return []


def _generate_intent_candidates(query: str, parsed: ParsedCommand, context: dict | None = None) -> list[IntentCandidate]:
    all_cands = _heuristic_intent_candidates(query, parsed) + _llm_intent_candidates(query, context=context)
    merged: dict[str, IntentCandidate] = {}
    for c in all_cands:
        key = str(c.intent or "").strip().lower()
        if not key:
            continue
        prev = merged.get(key)
        if prev is None or float(c.confidence or 0.0) > float(prev.confidence or 0.0):
            merged[key] = c
    out = sorted(merged.values(), key=lambda x: float(x.confidence or 0.0), reverse=True)
    return out[:3]


def _arbitrate_action_candidate(
    candidates: list[IntentCandidate],
    parsed: ParsedCommand,
    understanding: Understanding,
    *,
    decision_action_min_conf: float,
    mutation_min_conf: float,
) -> tuple[str, float, str]:
    if str(parsed.action or "").strip():
        return str(parsed.action or "").strip(), 0.99, "strict_parse"
    explicit_action_cmd = bool(
        re.match(r"^\s*(add|remove|delete|open|go to|navigate|take me|create|save|update|set|sync)\b", _norm(str(parsed.body or "")))
    )
    for c in candidates:
        name = str(c.intent or "").strip().lower()
        conf = float(c.confidence or 0.0)
        if name not in {"add_task", "add_note_draft", "add_daily_log", "manage_blue_chips"}:
            continue
        # Conversation-first: question-like turns require stronger confidence unless explicit.
        needed_for_question = min(0.74, mutation_min_conf + 0.08)
        if understanding.intent_class == "question" and not explicit_action_cmd and conf < needed_for_question:
            continue
        if conf < min(decision_action_min_conf, mutation_min_conf):
            continue
        return name, conf, str(c.source or "candidate")
    return "", 0.0, ""


def _has_objective_signal(low: str) -> bool:
    return bool(
        re.search(
            r"\b(compare|summari[sz]e|explain|analy[sz]e|decide|recommend|add|save|open|find|search|map|review|assess)\b",
            low,
        )
    )


def _has_scope_signal(text: str) -> bool:
    q = str(text or "")
    low = _norm(q)
    if _extract_ticker(q):
        return True
    return bool(
        re.search(
            r"\b(report|10-k|10-q|8-k|portfolio|watchlist|holdings|tasks|notes|company|industry|industries)\b",
            low,
        )
    )


def _has_lens_signal(low: str) -> bool:
    return bool(
        re.search(
            r"\b(horizon|risk|low risk|medium risk|high risk|short term|long term|valuation|quality|growth|income)\b",
            low,
        )
    )


def _build_reasoning_followup_question(query: str, context: dict | None = None) -> str:
    q = str(query or "").strip()
    low = _norm(q)
    ctx = context or {}
    runtime = ctx.get("runtime") if isinstance(ctx.get("runtime"), dict) else {}
    page = runtime.get("page_hint") if isinstance(runtime.get("page_hint"), dict) else {}
    page_title = str(page.get("title") or "").strip()
    page_kind = str(page.get("kind") or "").strip()
    missing: list[str] = []
    if not _has_objective_signal(low):
        missing.append("objective")
    if not _has_scope_signal(q):
        missing.append("scope")
    if not _has_lens_signal(low):
        missing.append("lens")
    if not missing:
        return "What outcome do you want from this: decision, summary, comparison, or action?"
    # Keep one question per turn.
    if missing[0] == "scope":
        if page_title:
            return f"Do you want me to focus on `{page_title}` on this page, or another specific ticker/report?"
        return "Which ticker or report should I focus on first?"
    if missing[0] == "objective":
        return "What is your goal right now: decide, compare, summarize, or execute an action?"
    if missing[0] == "lens":
        return "Which lens should I use: short-term trade, long-term thesis, or risk-first?"
    return "What is the most important thing you want me to solve first?"


def _contains_watchlist_term(text: str) -> bool:
    low = _norm(text)
    if not low:
        return False
    if "watchlist" in low or "watch list" in low or "market radar" in low:
        return True
    toks = re.findall(r"[a-z]+", low)
    for tk in toks:
        if len(tk) < 6:
            continue
        if SequenceMatcher(None, tk, "watchlist").ratio() >= 0.72:
            return True
    return False


def _contains_blue_chip_term(text: str) -> bool:
    low = _norm(text)
    if not low:
        return False
    if "blue chips" in low or "blue chip" in low or "bluechips" in low or "macro radar" in low:
        return True
    toks = re.findall(r"[a-z]+", low)
    for tk in toks:
        if len(tk) < 5:
            continue
        if SequenceMatcher(None, tk, "bluechip").ratio() >= 0.72:
            return True
    return False


def _contains_portfolio_term(text: str) -> bool:
    low = _norm(text)
    if not low:
        return False
    direct_terms = {"portfolio", "holdings", "active positions", "active position"}
    if any(t in low for t in direct_terms):
        return True
    toks = re.findall(r"[a-z]+", low)
    for tk in toks:
        if len(tk) < 5:
            continue
        if SequenceMatcher(None, tk, "portfolio").ratio() >= 0.7:
            return True
        if SequenceMatcher(None, tk, "holdings").ratio() >= 0.72:
            return True
    return False


def _looks_like_portfolio_status_query(text: str) -> bool:
    low = _norm(text)
    if not low or not _contains_portfolio_term(low):
        return False
    has_time_anchor = any(k in low for k in ("today", "daily", "day", "now", "current", "right now"))
    perf_terms = any(
        k in low
        for k in (
            "doing",
            "performance",
            "perform",
            "pnl",
            "up",
            "down",
            "return",
            "change",
            "status",
            "going",
            "happening",
        )
    )
    ask_pattern = bool(
        re.search(r"\bhow\s+is\s+my\b", low)
        or re.search(r"\bwhat\s+is\s+going\s+on\b", low)
        or re.search(r"\bwhat(?:'s|\s+is)?\s+happening\b", low)
        or re.search(r"\bwhat(?:'s|\s+is)?\s+the\s+status\b", low)
    )
    return (has_time_anchor and perf_terms) or ask_pattern


def _looks_like_company_navigation_query(raw_query: str) -> bool:
    q = str(raw_query or "").strip()
    low = _norm(q)
    if not q or not low:
        return False
    if re.search(r"\b(open|show|go|take|navigate)\b.*\b(company|ticker)\b", low):
        return True
    if re.search(r"\b(company|ticker)\b.*\b(open|show|go|take|navigate)\b", low):
        return True
    if re.search(r"\bresearch\b.*\b(company|ticker)\b", low):
        return True
    if re.search(r"\$[A-Za-z]{1,5}\b", q):
        return True
    if re.search(r"\b(?:company|ticker)\s+\$?[A-Za-z]{1,5}\b", q, flags=re.I):
        return True
    return False


def _strict_finance_parse(query: str) -> ParsedCommand:
    q = str(query or "").strip()
    low = _norm(q)
    action = ""
    if _contains_blue_chip_term(low) and any(k in low for k in {"add", "remove", "delete", "show", "list", "open", "go", "take", "navigate", "my", "what", "which", "tell"}):
        action = "manage_blue_chips"
    if "show me my notes" in low or ("notes" in low and any(k in low for k in ["show", "open", "list", "my"])):
        action = "open_notes"
    elif "daily log" in low and any(k in low for k in ["add", "append", "log", "write"]):
        action = "add_daily_log"
    elif low.startswith("add task") or low.startswith("task:") or low.startswith("todo:"):
        action = "add_task"
    elif low.startswith("add note") or low.startswith("note:") or low.startswith("draft note"):
        action = "add_note_draft"
    elif any(k in low for k in ["10-k", "10k", "10-q", "10q", "8-k", "8k", "sec filing", "sec filings"]):
        action = "open_sec_filings"
    elif _looks_like_portfolio_status_query(low):
        action = "portfolio_today_status"
    elif any(k in low for k in ["dashboard", "my companies", "my universe", "organizer", "reports", "intel feed"]) and any(k in low for k in ["open", "go", "show", "navigate"]):
        action = "open_page"
    elif _looks_like_company_navigation_query(q):
        action = "open_company"
    elif "summarize latest report" in low or _fuzzy_match(
        low,
        [
            "summarize latest report",
            "summarize latest daily briefing",
            "summiraze lastest daily briefing",
            "summary latest daily briefing",
        ],
        threshold=0.62,
    ):
        action = "summarize_latest_report"
    elif any(k in low for k in ["report", "reports", "find", "search"]):
        action = "search_reports"
    form = ""
    if re.search(r"\b10[\s-]?k\b", low):
        form = "10-K"
    elif re.search(r"\b10[\s-]?q\b", low):
        form = "10-Q"
    elif re.search(r"\b8[\s-]?k\b", low):
        form = "8-K"
    ticker_actions = {
        "open_company",
        "open_sec_filings",
        "add_task",
        "add_note_draft",
        "manage_blue_chips",
    }
    tk = _extract_ticker(q) if action in ticker_actions else ""
    if action in {"summarize_latest_report", "search_reports"} and tk in {"DAILY", "BRIEF", "BRIEFING", "REPORT", "REPORTS"}:
        tk = ""
    due = ""
    body = q
    if action == "add_task":
        due, body = _extract_due_date(q)
    return ParsedCommand(action=action, ticker=tk, form=form, due_date=due, body=body, notes="strict_finance_parse")


def _fuzzy_match(query: str, patterns: list[str], threshold: float = FUZZY_THRESHOLD) -> str:
    q = _norm(query)
    best = ""
    best_score = 0.0
    for p in patterns:
        s = SequenceMatcher(None, q, _norm(p)).ratio()
        if s > best_score:
            best_score = s
            best = p
    return best if best_score >= threshold else ""


def _tool_get_live_portfolio_summary(_args: dict[str, object]) -> dict[str, object]:
    return dict(get_live_portfolio_summary() or {})


def _tool_list_recent_portfolio_transactions(args: dict[str, object]) -> dict[str, object]:
    limit = int(args.get("limit") or 8)
    return {"rows": list_recent_portfolio_transactions(limit=max(1, min(80, limit)))}


def _tool_get_position_history(args: dict[str, object]) -> dict[str, object]:
    ticker = str(args.get("ticker") or "").strip().upper()
    limit = int(args.get("limit") or 20)
    if not ticker:
        return {"rows": []}
    return {"rows": get_position_history(ticker, limit=max(1, min(100, limit)))}


def _tool_get_watchlist_rationale(args: dict[str, object]) -> dict[str, object]:
    ticker = str(args.get("ticker") or "").strip().upper()
    limit = int(args.get("limit") or 6)
    if not ticker:
        return {"ticker": "", "thesis": {}, "decisions": []}
    return dict(get_watchlist_rationale(ticker, limit=max(1, min(30, limit))) or {})


def _tool_get_holdings(args: dict[str, object]) -> dict[str, object]:
    limit = int(args.get("limit") or 500)
    return {"rows": get_holdings(limit=max(1, min(2000, limit)))}


def _tool_maps_to(args: dict[str, object]) -> dict[str, object]:
    route = str(args.get("route") or "").strip()
    if not route.startswith("/"):
        route = "/dashboard"
    # Keep in-app only.
    if route.startswith("//") or route.startswith("/http"):
        route = "/dashboard"
    return {"route": route}


def _tool_save_thesis(args: dict[str, object]) -> dict[str, object]:
    ticker = str(args.get("ticker") or "").strip().upper()
    thesis = str(args.get("thesis") or "").strip()
    conviction = int(args.get("conviction") or 0)
    time_horizon = str(args.get("time_horizon") or "").strip()
    invalidation_criteria = str(args.get("invalidation_criteria") or "").strip()
    return save_thesis(
        ticker=ticker,
        thesis=thesis,
        conviction=conviction,
        time_horizon=time_horizon,
        invalidation_criteria=invalidation_criteria,
    )


def _tool_start_portfolio_interview(_args: dict[str, object]) -> dict[str, object]:
    session_id = str(_args.get("session_id") or "").strip()
    return start_portfolio_interview(session_id=session_id)


def _tool_submit_portfolio_interview_answer(args: dict[str, object]) -> dict[str, object]:
    answer = str(args.get("answer") or "").strip()
    ticker = str(args.get("ticker") or "").strip().upper()
    return submit_portfolio_interview_answer(answer=answer, ticker=ticker)


def _tool_backfill_trade_history(args: dict[str, object]) -> dict[str, object]:
    ticker = str(args.get("ticker") or "").strip().upper()
    approx_date = str(args.get("approx_date") or "").strip()
    approx_price = float(args.get("approx_price") or 0.0)
    reason = str(args.get("reason") or "").strip()
    action = str(args.get("action") or "buy").strip().lower()
    quantity = float(args.get("quantity") or 0.0)
    return backfill_trade_history(
        ticker=ticker,
        approx_date=approx_date,
        approx_price=approx_price,
        reason=reason,
        action=action,
        quantity=quantity,
    )


def _tool_create_task(args: dict[str, object]) -> dict[str, object]:
    text = str(args.get("text") or "").strip()
    ticker = str(args.get("ticker") or "").strip().upper()
    category = str(args.get("category") or ("company" if ticker else "quick")).strip().lower()
    priority = str(args.get("priority") or "P2").strip().upper()
    due_date = str(args.get("due_date") or "").strip()
    ok = bool(add_task(text, ticker=ticker, category=category, priority=priority, due_date=due_date))
    return {"ok": ok}


def _tool_create_note_draft(args: dict[str, object]) -> dict[str, object]:
    text = str(args.get("text") or "").strip()
    ticker = str(args.get("ticker") or "").strip().upper()
    trace_id = str(args.get("trace_id") or "").strip()
    ai_conf = float(args.get("ai_confidence") or 0.78)
    ai_reasoning = str(args.get("ai_reasoning") or "AI-generated draft pending user approval.").strip()
    ok = bool(
        add_general_note(
            text,
            scope="ai_draft",
            ticker=ticker,
            tags="ai,draft,organizer",
            status="pending",
            created_by="ai",
            ai_confidence=ai_conf,
            ai_reasoning=ai_reasoning,
            trace_id=trace_id,
        )
    )
    return {"ok": ok}


def _tool_append_daily_log(args: dict[str, object]) -> dict[str, object]:
    day = str(args.get("day") or "").strip()
    text = str(args.get("text") or "").strip()
    if not day or not text:
        return {"ok": False}
    cur = get_daily_note(day)
    base = str(cur.content or "").rstrip()
    combined = (base + "\n\n" + text).strip() if base else text
    ok = bool(save_daily_note(day, combined))
    return {"ok": ok, "day": day}


def _tool_delete_notes(args: dict[str, object]) -> dict[str, object]:
    ticker = str(args.get("ticker") or "").strip().upper()
    text_contains = str(args.get("text_contains") or "").strip()
    created_by = str(args.get("created_by") or "").strip().lower()
    deleted = delete_notes(
        ticker=ticker,
        text_contains=text_contains,
        created_by=created_by,
        include_company_journal=True,
    )
    return {"deleted": dict(deleted or {})}


TOOL_REGISTRY: dict[str, dict[str, object]] = {
    "get_holdings": {
        "schema": {"limit": int},
        "fn": _tool_get_holdings,
    },
    "maps_to": {
        "schema": {"route": str},
        "fn": _tool_maps_to,
    },
    "save_thesis": {
        "schema": {"ticker": str, "thesis": str, "conviction": int, "time_horizon": str, "invalidation_criteria": str},
        "fn": _tool_save_thesis,
    },
    "start_portfolio_interview": {
        "schema": {"session_id": str},
        "fn": _tool_start_portfolio_interview,
    },
    "submit_portfolio_interview_answer": {
        "schema": {"answer": str, "ticker": str},
        "fn": _tool_submit_portfolio_interview_answer,
    },
    "backfill_trade_history": {
        "schema": {"ticker": str, "approx_date": str, "approx_price": float, "reason": str, "action": str, "quantity": float},
        "fn": _tool_backfill_trade_history,
    },
    "get_live_portfolio_summary": {
        "schema": {},
        "fn": _tool_get_live_portfolio_summary,
    },
    "list_recent_portfolio_transactions": {
        "schema": {"limit": int},
        "fn": _tool_list_recent_portfolio_transactions,
    },
    "get_position_history": {
        "schema": {"ticker": str, "limit": int},
        "fn": _tool_get_position_history,
    },
    "get_watchlist_rationale": {
        "schema": {"ticker": str, "limit": int},
        "fn": _tool_get_watchlist_rationale,
    },
    "create_task": {
        "schema": {"text": str, "ticker": str, "category": str, "priority": str, "due_date": str},
        "fn": _tool_create_task,
    },
    "create_note_draft": {
        "schema": {"text": str, "ticker": str, "trace_id": str, "ai_confidence": float, "ai_reasoning": str},
        "fn": _tool_create_note_draft,
    },
    "append_daily_log": {
        "schema": {"day": str, "text": str},
        "fn": _tool_append_daily_log,
    },
    "delete_notes": {
        "schema": {"ticker": str, "text_contains": str, "created_by": str},
        "fn": _tool_delete_notes,
    },
}


def _validate_tool_args(schema: dict[str, type], args: dict[str, object]) -> dict[str, object]:
    out: dict[str, object] = {}
    for k, t in schema.items():
        if k not in args:
            continue
        v = args.get(k)
        if t is int:
            out[k] = int(v)  # type: ignore[arg-type]
            continue
        if t is float:
            out[k] = float(v)  # type: ignore[arg-type]
            continue
        if t is str:
            out[k] = str(v or "")
            continue
        if isinstance(v, t):
            out[k] = v
    return out


_READ_ONLY_TOOLS = {
    "get_holdings",
    "maps_to",
    "start_portfolio_interview",
    "submit_portfolio_interview_answer",
    "get_live_portfolio_summary",
    "list_recent_portfolio_transactions",
    "get_position_history",
    "get_watchlist_rationale",
}
_HIGH_RISK_TOOLS = {
    "delete_notes",
    "backfill_trade_history",   # writes to portfolio_transactions_core
    "save_thesis",              # writes investment thesis
    "append_daily_log",         # writes journal
}


def _tool_capability(tool_name: str) -> str:
    n = str(tool_name or "").strip()
    if n in _HIGH_RISK_TOOLS:
        return "high_risk"
    if n in _READ_ONLY_TOOLS:
        return "read"
    return "write"


def _capability_allowed(tool_name: str) -> bool:
    mode = str(app_env("AI_CAPABILITY_MODE", "normal") or "normal").strip().lower()
    cap = _tool_capability(tool_name)
    if mode in {"full", "unsafe"}:
        return True
    if mode in {"readonly", "read_only"}:
        return cap == "read"
    # AI_CHAT_READONLY=1 (default): block high_risk tools from AI chat path.
    # Protects portfolio_transactions_core from accidental text-triggered writes.
    chat_readonly = str(app_env("AI_CHAT_READONLY", "1") or "1").strip() not in {"0", "false", "off", "no"}
    if chat_readonly and cap == "high_risk":
        return False
    # normal mode: allow read/write, keep high_risk gated.
    return cap in {"read", "write"}


def _run_tool(tool_name: str, args: dict[str, object] | None = None, query: str = "") -> dict[str, object]:
    name = str(tool_name or "").strip()
    spec = TOOL_REGISTRY.get(name)
    in_args = dict(args or {})
    rt = _TURN_RUNTIME.get() or {}
    trace_id = str(rt.get("trace_id") or "").strip()
    capability = _tool_capability(name)
    if spec is None:
        return {"ok": False, "error": f"unknown_tool:{name}", "data": {}}
    schema = dict(spec.get("schema") or {})
    fn = spec.get("fn")
    if not callable(fn):
        return {"ok": False, "error": f"invalid_tool:{name}", "data": {}}
    if not _capability_allowed(name):
        _log_tool_call(
            query=query,
            tool_name=name,
            args=in_args,
            status="denied",
            latency_ms=0.0,
            error="capability_denied",
            trace_id=trace_id,
            model_name="deterministic",
            capability=capability,
        )
        tools = rt.get("tools") if isinstance(rt.get("tools"), list) else []
        tools.append({"tool": name, "status": "denied", "duration_ms": 0.0, "capability": capability, "model": "deterministic"})
        rt["tools"] = tools
        rt["denied"] = int(rt.get("denied") or 0) + 1
        _TURN_RUNTIME.set(rt)
        return {"ok": False, "error": f"capability_denied:{name}", "data": {}}
    max_calls = int(rt.get("max_tool_calls") or 14)
    used_calls = int(rt.get("tool_calls") or 0)
    if max_calls > 0 and used_calls >= max_calls:
        _log_tool_call(
            query=query,
            tool_name=name,
            args=in_args,
            status="denied",
            latency_ms=0.0,
            error="budget_max_tool_calls",
            trace_id=trace_id,
            model_name="deterministic",
            capability=capability,
        )
        tools = rt.get("tools") if isinstance(rt.get("tools"), list) else []
        tools.append({"tool": name, "status": "denied", "duration_ms": 0.0, "capability": capability, "model": "deterministic"})
        rt["tools"] = tools
        rt["denied"] = int(rt.get("denied") or 0) + 1
        _TURN_RUNTIME.set(rt)
        return {"ok": False, "error": f"budget_max_tool_calls:{name}", "data": {}}
    max_ms = float(rt.get("max_tool_ms") or 8000.0)
    used_ms = float(rt.get("tool_ms") or 0.0)
    if max_ms > 0 and used_ms >= max_ms:
        _log_tool_call(
            query=query,
            tool_name=name,
            args=in_args,
            status="denied",
            latency_ms=0.0,
            error="budget_max_tool_ms",
            trace_id=trace_id,
            model_name="deterministic",
            capability=capability,
        )
        tools = rt.get("tools") if isinstance(rt.get("tools"), list) else []
        tools.append({"tool": name, "status": "denied", "duration_ms": 0.0, "capability": capability, "model": "deterministic"})
        rt["tools"] = tools
        rt["denied"] = int(rt.get("denied") or 0) + 1
        _TURN_RUNTIME.set(rt)
        return {"ok": False, "error": f"budget_max_tool_ms:{name}", "data": {}}
    t0 = time.perf_counter()
    try:
        safe_args = _validate_tool_args(schema, in_args)
        data = fn(safe_args)  # type: ignore[misc]
        elapsed = (time.perf_counter() - t0) * 1000.0
        _log_tool_call(
            query=query,
            tool_name=name,
            args=safe_args,
            status="ok",
            latency_ms=elapsed,
            trace_id=trace_id,
            model_name="deterministic",
            capability=capability,
        )
        tools = rt.get("tools") if isinstance(rt.get("tools"), list) else []
        tools.append({"tool": name, "status": "ok", "duration_ms": round(float(elapsed), 1), "capability": capability, "model": "deterministic"})
        rt["tools"] = tools
        rt["tool_calls"] = int(rt.get("tool_calls") or 0) + 1
        rt["tool_ms"] = float(rt.get("tool_ms") or 0.0) + float(elapsed)
        _TURN_RUNTIME.set(rt)
        return {"ok": True, "error": "", "data": data if isinstance(data, dict) else {"result": data}}
    except Exception as exc:
        elapsed = (time.perf_counter() - t0) * 1000.0
        _log_tool_call(
            query=query,
            tool_name=name,
            args=in_args,
            status="error",
            latency_ms=elapsed,
            error=str(exc),
            trace_id=trace_id,
            model_name="deterministic",
            capability=capability,
        )
        tools = rt.get("tools") if isinstance(rt.get("tools"), list) else []
        tools.append({"tool": name, "status": "error", "duration_ms": round(float(elapsed), 1), "capability": capability, "model": "deterministic"})
        rt["tools"] = tools
        rt["tool_calls"] = int(rt.get("tool_calls") or 0) + 1
        rt["tool_ms"] = float(rt.get("tool_ms") or 0.0) + float(elapsed)
        _TURN_RUNTIME.set(rt)
        return {"ok": False, "error": str(exc), "data": {}}


def _mapped_route(route: str, query: str = "") -> str:
    tr = _run_tool("maps_to", {"route": route}, query=query)
    data = tr.get("data") if isinstance(tr.get("data"), dict) else {}
    rd = str(data.get("route") or "").strip()
    return rd if rd.startswith("/") else "/dashboard"


def _intent_open_company(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    if not q:
        return None
    low = q.lower()
    if not _looks_like_company_navigation_query(q):
        return None
    # Strict extraction for navigation to avoid accidental ticker creation from normal words.
    tk = ""
    m = re.search(r"\b(?:company|ticker)\s+\$?([A-Za-z]{1,5})\b", q, flags=re.I)
    if m:
        tk = _safe_ticker(m.group(1))
    if not tk:
        m2 = re.search(r"\$([A-Za-z]{1,5})\b", q)
        if m2:
            tk = _safe_ticker(m2.group(1))
    if not tk:
        for alias, alias_tk in ENTITY_ALIASES.items():
            if re.search(rf"\b{re.escape(alias)}\b", _norm(q)):
                tk = _safe_ticker(alias_tk)
                break
    if not tk:
        tk = _safe_ticker(_resolve_ticker_from_company_name(_norm(q)))
    if not tk:
        tk = _safe_ticker(_resolve_ticker_from_company_hint(q))
    if tk and not _is_known_ticker_for_user_scope(tk):
        tk = ""
    if not tk:
        # Only ask for clarification if user clearly asked to open/research a company.
        if re.search(r"\b(open|research|show)\b.*\b(company|ticker)\b", low):
            return AICommandResult(
                status="needs_input",
                intent="open_company",
                message="Ticker missing or unknown. Example: open company CRM",
                confidence=0.35,
                citations=[],
                traces=[],
            )
        return None
    return AICommandResult(
        status="ok",
        intent="open_company",
        message=f"Opening company file for {tk}.",
        confidence=0.96,
        redirect_url=f"/company_file?t={tk}",
        citations=[],
        traces=[],
    )


def _intent_open_page(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    low = _norm(q)
    if not low:
        return None
    wants_open = any(k in low for k in {"open", "go", "show", "take", "navigate"})
    # Also allow direct noun commands like "dashboard", "my universe", "reports".
    short_direct = low in {"dashboard", "reports", "organizer", "my companies", "my universe", "portfolio", "intel feed", "blue chips", "blue chip", "bluechips"}
    if not wants_open and not short_direct:
        return None
    if any(k in low for k in {"my companies", "my universe", "portfolio", "holdings"}):
        rd = _mapped_route("/my_universe?tab=all", q)
        return AICommandResult(
            status="ok",
            intent="open_page",
            message="Opening Portfolio.",
            confidence=0.95,
            redirect_url=rd,
            citations=[],
            traces=[{"step": "tool", "detail": "maps_to"}],
        )
    if _contains_blue_chip_term(low):
        rd = _mapped_route("/my_universe?tab=bluechips", q)
        return AICommandResult(
            status="ok",
            intent="open_page",
            message="Opening Blue Chips.",
            confidence=0.95,
            redirect_url=rd,
            citations=[],
            traces=[{"step": "tool", "detail": "maps_to"}],
        )
    if "intel feed" in low:
        rd = _mapped_route("/dashboard", q)
        return AICommandResult(
            status="ok",
            intent="open_page",
            message="Opening Intel Feed.",
            confidence=0.95,
            redirect_url=rd,
            citations=[],
            traces=[{"step": "tool", "detail": "maps_to"}],
        )
    if "dashboard" in low or "home" in low or ("main" in low and "page" in low):
        rd = _mapped_route("/dashboard", q)
        return AICommandResult(
            status="ok",
            intent="open_page",
            message="Opening Dashboard.",
            confidence=0.95,
            redirect_url=rd,
            citations=[],
            traces=[{"step": "tool", "detail": "maps_to"}],
        )
    if "organizer" in low or "tasks" in low:
        rd = _mapped_route("/organizer", q)
        return AICommandResult(
            status="ok",
            intent="open_page",
            message="Opening Organizer.",
            confidence=0.94,
            redirect_url=rd,
            citations=[],
            traces=[{"step": "tool", "detail": "maps_to"}],
        )
    if "report" in low:
        rd = _mapped_route("/reports", q)
        return AICommandResult(
            status="ok",
            intent="open_page",
            message="Opening Reports.",
            confidence=0.94,
            redirect_url=rd,
            citations=[],
            traces=[{"step": "tool", "detail": "maps_to"}],
        )
    return None


def _intent_create_company_guard(query: str) -> AICommandResult | None:
    low = _norm(query)
    if not low:
        return None
    if not re.search(r"\b(create|add|new)\b.*\b(company|ticker)\b", low):
        return None
    tk = _extract_ticker(query)
    hint = f" {tk}" if tk else " [TICKER]"
    return AICommandResult(
        status="needs_confirmation",
        intent="create_company_guard",
        message=(
            "I will not create a company automatically.\n"
            "Please confirm explicitly in chat first.\n"
            f"Reply: `confirm create company{hint}`"
        ),
        confidence=0.99,
        redirect_url="",
        citations=[],
        traces=[],
    )


def _route_alias_target(query: str) -> tuple[str, str] | None:
    low = _norm(query)
    # route, label
    route_map: list[tuple[set[str], str, str]] = [
        ({"dashboard", "home", "main", "main page", "landing", "start page", "briefing", "market"}, "/dashboard", "Dashboard"),
        ({"my companies", "my universe", "portfolio", "holdings", "active positions", "market radar"}, "/my_universe?tab=all", "Portfolio"),
        ({"blue chips", "blue chip", "bluechips", "macro radar"}, "/my_universe?tab=bluechips", "Blue Chips"),
        ({"intel feed", "feed", "live wire"}, "/dashboard", "Intel Feed"),
        ({"organizer", "tasks", "daily log", "approvals"}, "/organizer", "Organizer"),
        ({"notes file", "notes page", "my notes"}, "/organizer", "Notes"),
        ({"reports", "report library", "deep dive", "daily brief"}, "/reports", "Reports"),
        ({"observability", "system events", "latency"}, "/observability", "Observability"),
    ]
    for aliases, route, label in route_map:
        for a in aliases:
            if a in low:
                return route, label
    return None


def _intent_app_map_route(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    low = _norm(q)
    if not low:
        return None
    asks_where = bool(re.search(r"\b(where|which page|what page|route|map)\b", low))
    wants_nav = any(k in low for k in {"open", "go", "take", "navigate", "show"}) and bool(
        re.search(r"\b(notes|tasks|reports|dashboard|portfolio|organizer|observability|main page|home|intel feed|feed|blue chips|blue chip|bluechips)\b", low)
    )
    if not asks_where and not wants_nav:
        return None

    target = _route_alias_target(low)
    if target is not None:
        route, label = target
        route = _mapped_route(route, q)
        if any(k in low for k in {"open", "go", "take", "navigate", "show"}):
            return AICommandResult(
                status="ok",
                intent="app_map_route",
                message=f"Opening {label}.",
                confidence=0.96,
                redirect_url=route,
                citations=[],
                traces=[{"step": "tool", "detail": "maps_to"}],
            )
        return AICommandResult(
            status="ok",
            intent="app_map_route",
            message=f"{label} is at `{route}`.",
            confidence=0.95,
            redirect_url=route,
            citations=[],
            traces=[{"step": "tool", "detail": "maps_to"}],
        )

    hits = retrieve_app_knowledge(q, limit=6)
    if not hits:
        return None
    top = hits[0]
    title = str(top.get("title") or "App Map").strip()
    content = str(top.get("content") or "").strip()
    # Try to extract an in-app route from knowledge text, e.g., "Route /dashboard ..."
    m = re.search(r"\b(/[a-z0-9_./?=&-]+)\b", content.lower())
    route = m.group(1) if m else "/dashboard"
    msg = f"{title}: {content}"
    return AICommandResult(
        status="ok",
        intent="app_map_route",
        message=msg,
        confidence=0.86,
        redirect_url=route,
        citations=[{"label": title, "url": route}],
        traces=[],
    )


def _intent_app_capabilities(query: str) -> AICommandResult | None:
    low = _norm(query)
    if not low:
        return None
    asks_help = bool(
        re.search(
            r"\b(what can you do|help|capabilities|commands|how to use|what do you know|how does this work)\b",
            low,
        )
    )
    if not asks_help:
        return None
    msg = (
        "I can operate Investor OS directly:\n"
        "1. Portfolio: \"how is my portfolio doing today?\", \"open my universe\".\n"
        "2. Company workflow: \"open company CRM\", \"find CRM 10-K\".\n"
        "3. Notes/tasks: \"show me my notes\", \"add task review HUBS guidance tomorrow\", \"add note ...\".\n"
        "4. Reports: \"open reports\", \"summarize latest report\", \"summarize this report\".\n"
        "5. Organizer: \"open organizer\", \"show high risk queue\".\n"
        "6. Intel: \"open intel feed\"."
    )
    return AICommandResult(
        status="ok",
        intent="app_capabilities",
        message=msg,
        confidence=0.97,
        redirect_url="/dashboard",
        citations=[],
        traces=[],
    )


def _intent_memory_remember(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    if not q:
        return None
    m = re.search(r"^\s*(?:remember this|remember)\s*[:\-]\s*(.+)$", q, flags=re.I)
    if not m:
        return None
    text = str(m.group(1) or "").strip()
    if not text:
        return AICommandResult(
            status="needs_input",
            intent="memory_remember",
            message="Tell me what to remember. Example: remember this: prioritize capital preservation.",
            confidence=0.3,
            citations=[],
            traces=[],
        )
    res = remember_compact_memory(text=text, bucket="process_rule", source="chat_manual", reliability=0.96)
    if not bool(res.get("ok")):
        return AICommandResult(
            status="error",
            intent="memory_remember",
            message="Could not save memory right now.",
            confidence=0.3,
            citations=[],
            traces=[],
        )
    return AICommandResult(
        status="ok",
        intent="memory_remember",
        message="Saved. I will remember this as a long-term rule.",
        confidence=0.95,
        citations=[],
        traces=[],
    )


def _intent_memory_forget(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    if not q:
        return None
    m = re.search(r"^\s*(?:forget|remove memory)\s*[:\-]?\s*(.+)$", q, flags=re.I)
    if not m:
        return None
    term = str(m.group(1) or "").strip()
    if not term:
        return AICommandResult(
            status="needs_input",
            intent="memory_forget",
            message="Tell me what to forget. Example: forget concise summaries.",
            confidence=0.3,
            citations=[],
            traces=[],
        )
    out = forget_compact_memory(term, limit=20)
    if not bool(out.get("ok")):
        return AICommandResult(
            status="error",
            intent="memory_forget",
            message="Could not update memory right now.",
            confidence=0.3,
            citations=[],
            traces=[],
        )
    return AICommandResult(
        status="ok",
        intent="memory_forget",
        message=f"Done. Archived {int(out.get('archived') or 0)} memory item(s).",
        confidence=0.94,
        citations=[],
        traces=[],
    )


def _intent_memory_show(query: str) -> AICommandResult | None:
    low = _norm(query)
    if not low:
        return None
    if not re.search(r"\b(show|list|what)\b.*\b(memory|rules|preferences)\b", low) and "show memory" not in low:
        return None
    rows = list_compact_memories(query="", bucket="", limit=16, include_archived=False)
    if not rows:
        return AICommandResult(
            status="ok",
            intent="memory_show",
            message="No compact memory stored yet.",
            confidence=0.9,
            citations=[],
            traces=[],
        )
    lines = ["Your active long-term memory:"]
    for r in rows[:12]:
        b = str(r.get("bucket") or "memory")
        v = str(r.get("value") or "").strip()
        sc = float(r.get("score") or 0.0)
        if not v:
            continue
        lines.append(f"- [{b}] {v[:150]} (score {sc:.2f})")
    return AICommandResult(
        status="ok",
        intent="memory_show",
        message="\n".join(lines),
        confidence=0.93,
        citations=[],
        traces=[],
    )


def _intent_learning_status(query: str) -> AICommandResult | None:
    low = _norm(query)
    if not low:
        return None
    if not re.search(r"\b(show|list|status|how)\b.*\b(learning|rules|candidates)\b", low) and "learning status" not in low:
        return None
    rules = list_active_rules(limit=12)
    cands = list_rule_candidates(limit=12)
    lines = [
        f"Learning status: {len(rules)} active rule(s), {len(cands)} top candidate(s).",
        "",
        "Active rules:",
    ]
    if not rules:
        lines.append("- (none yet)")
    for r in rules[:8]:
        lines.append(f"- {str(r.get('rule_text') or '')[:150]} (conf {float(r.get('confidence') or 0.0):.2f})")
    lines.append("")
    lines.append("Top candidates:")
    if not cands:
        lines.append("- (none yet)")
    for c in cands[:8]:
        lines.append(
            f"- {str(c.get('rule_text') or '')[:120]} "
            f"(conf {float(c.get('confidence') or 0.0):.2f}, support {int(c.get('support_count') or 0)})"
        )
    return AICommandResult(
        status="ok",
        intent="learning_status",
        message="\n".join(lines),
        confidence=0.92,
        citations=[],
        traces=[],
    )


def _intent_add_task(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    low = q.lower()
    taskish = bool(
        low.startswith("add task")
        or low.startswith("task:")
        or low.startswith("todo:")
        or low.startswith("to do:")
        or low.startswith("to-do:")
        or low.startswith("remind me to")
        or bool(re.search(r"\bremind me to\b", low))
        or re.match(r"^\s*(add|create|new)\s+(a\s+)?(to[\s-]?do|todo|task)\b", low)
        or re.match(r"^\s*(to[\s-]?do|todo|task)\b", low)
        or re.match(r"^\s*daily\s+task\b", low)
        or re.search(r"\b(add|create|new)\b.*\b(to[\s-]?do|todo|task|daily task)\b", low)
    )
    if not taskish:
        return None

    text = q
    text = re.sub(
        r"^(add\s+task\s*:?|task:\s*|todo:\s*|to\s*do:\s*|to-do:\s*|"
        r"(add|create|new)\s+(a\s+)?(to[\s-]?do|todo|task)\s*:?\s*|remind\s+me\s+to\s*)",
        "",
        text,
        flags=re.I,
    ).strip()
    m_after = re.match(
        r"^\s*(?:please\s+)?add\s+(?:it\s+)?to\s+(?:daily\s+)?(?:to[\s-]?do|todo|task)\s*[:,-]?\s*(.+)$",
        text,
        flags=re.I,
    )
    if m_after and str(m_after.group(1) or "").strip():
        text = str(m_after.group(1) or "").strip()
    else:
        m_natural = re.match(
            r"^\s*(.+?)\s*,?\s*(?:please\s+)?add\s+(?:it\s+)?to\s+(?:daily\s+)?(?:to[\s-]?do|todo|task)\b.*$",
            text,
            flags=re.I,
        )
        if m_natural and str(m_natural.group(1) or "").strip():
            text = str(m_natural.group(1) or "").strip()
    text = re.sub(r"^\s*that\s+to\s+", "", text, flags=re.I).strip()
    text = re.sub(r"^\s*(please\s+)?(add|create|new)\s+", "", text, flags=re.I).strip()
    text = re.sub(r"^\s*daily\s+task\s*[:,-]?\s*", "", text, flags=re.I).strip()
    tk = _extract_ticker(text)
    due_date, text_wo_due = _extract_due_date(text)
    text_clean = re.sub(r"\$[A-Za-z]{1,5}\b", "", text_wo_due).strip(" -:")
    text_clean = re.sub(r"\b(by|on|for)\s*$", "", text_clean, flags=re.I).strip()
    if not text_clean:
        return AICommandResult(
            status="needs_input",
            intent="add_task",
            message="Task text is missing. Example: add task read latest CRM 10-Q",
            confidence=0.4,
            citations=[],
            traces=[],
        )

    tr = _run_tool(
        "create_task",
        {
            "text": text_clean,
            "ticker": tk,
            "category": "company" if tk else "quick",
            "priority": "P2",
            "due_date": due_date,
        },
        query=q,
    )
    ok = bool((tr.get("data") or {}).get("ok")) if isinstance(tr.get("data"), dict) else False
    if not tr.get("ok") or not ok:
        return AICommandResult(
            status="error",
            intent="add_task",
            message="Could not create task due to a storage error.",
            confidence=0.2,
            citations=[],
            traces=[{"step": "tool", "detail": "create_task:error"}],
        )
    # Verify-after-write to prevent false positives.
    try:
        rows = list_tasks(open_only=True, limit=400)
        found = False
        for rr in rows:
            ttxt = str(rr["task"] or "").strip().lower()
            ttk = str(rr["ticker"] or "").strip().upper()
            dd = str(rr["due_date"] or "").strip()
            if ttxt == str(text_clean or "").strip().lower() and (not tk or ttk == tk) and (not due_date or dd == due_date):
                found = True
                break
        if not found:
            return AICommandResult(
                status="error",
                intent="add_task",
                message="Task write could not be verified. Please retry.",
                confidence=0.25,
                citations=[],
                traces=[{"step": "tool", "detail": "create_task:verify_failed"}],
            )
    except Exception:
        return AICommandResult(
            status="error",
            intent="add_task",
            message="Task write verification failed.",
            confidence=0.25,
            citations=[],
            traces=[{"step": "tool", "detail": "create_task:verify_error"}],
        )
    return AICommandResult(
        status="ok",
        intent="add_task",
        message=(
            f"Task added{' for ' + tk if tk else ''}: {text_clean}"
            + (f" (due {due_date})" if due_date else "")
        ),
        confidence=0.92,
        redirect_url="/organizer",
        citations=[],
        traces=[{"step": "tool", "detail": "create_task"}],
    )


def _intent_add_note_draft(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    low = _norm(q)
    text = ""
    # Strict prefix path.
    if re.match(r"^\s*(add\s+note\s*:?|draft\s+note\s*:?|note:\s*)", q, flags=re.I):
        text = re.sub(r"^(add\s+note\s*:?|note:\s*|draft\s+note\s*:?)", "", q, flags=re.I).strip()
    else:
        # Fuzzy prefix path for typo-prone quick capture on command bar.
        words = q.split()
        prefix2 = _norm(" ".join(words[:2]))
        prefix1 = _norm(words[0] if words else "")
        matched2 = _fuzzy_match(prefix2, ["add note", "draft note"], threshold=0.78)
        matched1 = _fuzzy_match(prefix1, ["note"], threshold=0.9)
        if matched2:
            text = " ".join(words[2:]).strip()
        elif matched1 and len(words) > 1 and ":" in q[:8]:
            # Accept compact forms like "note: buy Apple".
            text = re.sub(r"^\s*note\s*:?\s*", "", q, flags=re.I).strip()
        else:
            return None
    tk = _extract_ticker(text)
    clean = re.sub(r"\$[A-Za-z]{1,5}\b", "", text).strip(" -:")
    if not clean:
        return AICommandResult(
            status="needs_input",
            intent="add_note_draft",
            message="Note text is missing. Example: add note CRM guidance still weak in SMB.",
            confidence=0.4,
            citations=[],
            traces=[],
        )
    trace = "tr_" + uuid.uuid4().hex[:12]
    tr = _run_tool(
        "create_note_draft",
        {
            "text": clean,
            "ticker": tk,
            "trace_id": trace,
            "ai_confidence": 0.78,
            "ai_reasoning": "AI-generated draft pending user approval.",
        },
        query=q,
    )
    ok = bool((tr.get("data") or {}).get("ok")) if isinstance(tr.get("data"), dict) else False
    if not tr.get("ok") or not ok:
        return AICommandResult(
            status="error",
            intent="add_note_draft",
            message="Could not create draft note due to a storage error.",
            confidence=0.2,
            citations=[],
            traces=[{"step": "tool", "detail": "create_note_draft:error"}],
        )
    return AICommandResult(
        status="ok",
        intent="add_note_draft",
        message=f"AI draft note created{' for ' + tk if tk else ''}. Review in Organizer Notes.",
        confidence=0.86,
        redirect_url="/organizer",
        citations=[],
        traces=[{"step": "tool", "detail": "create_note_draft"}],
    )


def _extract_daily_log_payload(query: str) -> str:
    q = str(query or "").strip()
    if not q:
        return ""
    # Common phrases:
    # - add this to daily log ...
    # - add daily log ...
    # - daily log: ...
    patterns = [
        r"^\s*add\s+this\s+to\s+daily\s+log[:\s-]*",
        r"^\s*add\s+to\s+daily\s+log[:\s-]*",
        r"^\s*append\s+to\s+daily\s+log[:\s-]*",
        r"^\s*add\s+daily\s+log(?:\s+about)?[:\s-]*",
        r"^\s*daily\s+log[:\s-]*",
        r"^\s*log[:\s-]*",
    ]
    out = q
    for p in patterns:
        out2 = re.sub(p, "", out, flags=re.I)
        if out2 != out:
            out = out2
            break
    return out.strip(" .:-\n\t")


def _intent_add_daily_log(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    low = _norm(q)
    # Safety: task-like commands must never be routed to daily log.
    if re.search(r"\b(to[\s-]?do|todo|task|remind me to)\b", low):
        return None
    if "daily log" not in low and not low.startswith("log:"):
        return None
    if not any(k in low for k in ["add", "append", "write", "log"]):
        return None
    payload = _extract_daily_log_payload(q)
    if not payload:
        return AICommandResult(
            status="needs_input",
            intent="add_daily_log",
            message="Daily log text is missing. Example: add this to daily log CRM down, HUBS up.",
            confidence=0.4,
            citations=[],
            traces=[],
        )
    day = dt.date.today().isoformat()
    tr = _run_tool("append_daily_log", {"day": day, "text": payload}, query=q)
    ok = bool((tr.get("data") or {}).get("ok")) if isinstance(tr.get("data"), dict) else False
    if not tr.get("ok") or not ok:
        return AICommandResult(
            status="error",
            intent="add_daily_log",
            message="Could not update daily log (it may be locked).",
            confidence=0.2,
            citations=[],
            traces=[{"step": "tool", "detail": "append_daily_log:error"}],
        )
    return AICommandResult(
        status="ok",
        intent="add_daily_log",
        message=f"Added to Daily Log ({day}).",
        confidence=0.95,
        redirect_url=f"/organizer?day={day}",
        citations=[],
        traces=[{"step": "tool", "detail": "append_daily_log"}],
    )


def _dispatch_mutation_action(query: str, parsed: ParsedCommand) -> AICommandResult | None:
    action = str(parsed.action or "").strip()
    if not action:
        return None
    handlers: dict[str, object] = {
        "manage_blue_chips": _intent_manage_blue_chips,
        "add_daily_log": _intent_add_daily_log,
        "add_note_draft": _intent_add_note_draft,
        "add_task": _intent_add_task,
    }
    fn = handlers.get(action)
    if not callable(fn):
        return None
    try:
        return fn(query)  # type: ignore[misc]
    except Exception:
        return AICommandResult(
            status="error",
            intent=action,
            message="I couldn't complete that write action due to an internal error.",
            confidence=0.2,
            citations=[],
            matched_by="mutation_gateway",
            version=ORCHESTRATOR_VERSION,
            traces=[{"step": "mutation_gateway", "detail": f"{action}:exception"}],
        )


def _risk_veto_assess_mutation(
    parsed: ParsedCommand,
    query: str,
    understanding_confidence: float,
    trace_id: str,
    context: dict | None = None,
) -> dict[str, object]:
    action = str(parsed.action or "").strip().lower()
    q = str(query or "").strip()
    ctx = context or {}
    cfg = get_risk_veto_config()
    mode = str(app_env("AI_RISK_VETO_MODE", "enabled") or "enabled").strip().lower()
    if mode in {"off", "disabled", "0"} or not bool(cfg.get("enabled", True)):
        return {"verdict": "allow", "confidence": 1.0, "reason": "risk_veto_disabled", "metrics": {}}

    low_risk_actions = {"add_task", "add_note_draft", "add_daily_log"}
    high_risk_actions = {"buy", "sell", "trim", "add_position", "update_cash", "execute_trade", "record_portfolio_transaction"}
    low = _norm(q)

    def _parse_amount(raw: str) -> float:
        s = str(raw or "").strip().lower().replace(",", "")
        if not s:
            return 0.0
        mul = 1.0
        if s.endswith("k"):
            mul = 1_000.0
            s = s[:-1]
        elif s.endswith("m"):
            mul = 1_000_000.0
            s = s[:-1]
        elif s.endswith("b"):
            mul = 1_000_000_000.0
            s = s[:-1]
        try:
            return float(s) * mul
        except Exception:
            return 0.0

    m_amt = re.search(r"\$?\s*(\d+(?:\.\d+)?\s*[kmb]?)", q, flags=re.I)
    explicit_amount = _parse_amount(m_amt.group(1)) if m_amt else 0.0
    m_sh = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:shares?|qty|quantity)\b", q, flags=re.I)
    m_px = re.search(r"\b(?:at|@\s*)\s*\$?\s*(\d+(?:\.\d+)?)\b", q, flags=re.I)
    shares = float(m_sh.group(1)) if m_sh else 0.0
    px = float(m_px.group(1)) if m_px else 0.0
    trade_notional = explicit_amount if explicit_amount > 0 else (shares * px if shares > 0 and px > 0 else 0.0)

    holds = get_holdings(limit=1200)
    values: dict[str, float] = {}
    day_moves: list[float] = []
    for r in holds:
        tk = _safe_ticker(str(r.get("ticker") or ""))
        sh = float(r.get("shares") or 0.0)
        cost = float(r.get("cost") or 0.0)
        if not tk or sh <= 0:
            continue
        val = sh * cost if cost > 0 else sh
        values[tk] = max(0.0, val)
        try:
            d = float(r.get("day_pct") or 0.0)
            day_moves.append(d)
        except Exception:
            pass
    total_val = sum(v for v in values.values() if v > 0)
    max_weight_pct = (max(values.values()) / total_val * 100.0) if total_val > 0 and values else 0.0
    target_ticker = _safe_ticker(str(parsed.ticker or ""))
    current_target_weight = (float(values.get(target_ticker, 0.0)) / total_val * 100.0) if total_val > 0 and target_ticker else 0.0

    live = get_live_portfolio_summary()
    cov = float(live.get("coverage_pct") or 0.0)
    denom = float(live.get("tracked_prev_close_value") or live.get("tracked_basis") or total_val or 0.0)
    notional_pct = (trade_notional / denom * 100.0) if trade_notional > 0 and denom > 0 else 0.0
    projected_weight = current_target_weight + (notional_pct if action in {"buy", "add_position", "record_portfolio_transaction"} else 0.0)

    n = len(day_moves)
    mu = (sum(day_moves) / n) if n > 0 else 0.0
    var = (sum((x - mu) ** 2 for x in day_moves) / (n - 1)) if n > 1 else 0.0
    stdev = math.sqrt(max(0.0, var))
    var95_pct = 1.65 * stdev
    cvar95_pct = 2.06 * stdev

    known_scope_ticker = bool(target_ticker and _is_known_ticker_for_user_scope(target_ticker))
    liquidity_score = 1.0 if known_scope_ticker else (0.4 if target_ticker else 0.7)

    metrics: dict[str, object] = {
        "understanding_confidence": float(understanding_confidence or 0.0),
        "action": action,
        "high_risk_action": action in high_risk_actions,
        "trade_notional": round(trade_notional, 4),
        "trade_notional_pct": round(notional_pct, 4),
        "max_position_weight_pct": round(max_weight_pct, 4),
        "current_target_weight_pct": round(current_target_weight, 4),
        "projected_target_weight_pct": round(projected_weight, 4),
        "var95_pct": round(var95_pct, 4),
        "cvar95_pct": round(cvar95_pct, 4),
        "coverage_pct": round(cov, 2),
        "liquidity_score": round(liquidity_score, 4),
        "known_scope_ticker": known_scope_ticker,
    }

    min_conf = float(cfg.get("min_confidence_for_mutation") or 0.62)
    max_single_add = float(cfg.get("max_single_add_pct") or 5.0)
    max_pos_w = float(cfg.get("max_position_weight_pct") or 20.0)
    max_var = float(cfg.get("max_var95_pct") or 6.0)
    max_cvar = float(cfg.get("max_cvar95_pct") or 8.0)
    min_cov = float(cfg.get("min_quote_coverage_pct") or 75.0)
    hi_notional = float(cfg.get("high_impact_notional_pct") or 3.0)
    req_known = bool(cfg.get("require_known_ticker_scope", True))
    block_unknown = bool(cfg.get("block_on_unknown_ticker", True))
    review_hi = bool(cfg.get("review_for_high_impact", True))

    conf_u = float(understanding_confidence or 0.0)
    force_review = bool(
        action in high_risk_actions
        or any(k in low for k in {"buy", "sell", "trim", "allocate", "size up", "reduce position", "deposit", "withdraw"})
    )
    if action in low_risk_actions:
        verdict = "allow"
        reason = "low_risk_mutation"
        conf = 0.96
    elif conf_u < min_conf:
        verdict = "veto"
        reason = "low_intent_confidence_for_mutation"
        conf = 0.91
    elif target_ticker and req_known and not known_scope_ticker and block_unknown:
        verdict = "veto"
        reason = "unknown_ticker_scope_blocked"
        conf = 0.93
    elif cov > 0 and cov < min_cov:
        verdict = "review"
        reason = "quote_coverage_too_low"
        conf = 0.87
    elif force_review and (projected_weight > max_pos_w):
        verdict = "veto"
        reason = "projected_position_weight_exceeds_limit"
        conf = 0.94
    elif force_review and (notional_pct > (max_single_add * 1.5)):
        verdict = "veto"
        reason = "single_add_size_far_above_limit"
        conf = 0.93
    elif force_review and (notional_pct > max_single_add):
        verdict = "review"
        reason = "single_add_size_above_limit"
        conf = 0.89
    elif force_review and ((var95_pct > max_var) or (cvar95_pct > max_cvar)):
        verdict = "review"
        reason = "portfolio_risk_budget_hot"
        conf = 0.88
    elif force_review and review_hi and (notional_pct >= hi_notional):
        verdict = "review"
        reason = "high_impact_notional_requires_review"
        conf = 0.86
    else:
        verdict = "allow"
        reason = "no_high_impact_signal"
        conf = 0.82

    detail = {
        "last_intent": str(ctx.get("last_intent") or "").strip(),
        "pending_query": str(ctx.get("pending_query") or "").strip(),
        "parsed_ticker": str(parsed.ticker or "").strip().upper(),
        "parsed_due_date": str(parsed.due_date or "").strip(),
    }
    _log_risk_veto_decision(
        trace_id=trace_id,
        action=action,
        query=q,
        verdict=verdict,
        confidence=float(conf),
        reason=reason,
        metrics=metrics,
        detail=detail,
    )
    return {
        "verdict": verdict,
        "confidence": float(conf),
        "reason": reason,
        "metrics": metrics,
        "detail": detail,
    }


def _mutation_call_args(parsed: ParsedCommand, query: str) -> dict[str, object]:
    action = str(parsed.action or "").strip()
    args: dict[str, object] = {}
    if parsed.ticker:
        args["ticker"] = str(parsed.ticker or "").strip().upper()
    if parsed.form:
        args["form"] = str(parsed.form or "").strip().upper()
    if parsed.due_date:
        args["due_date"] = str(parsed.due_date or "").strip()
    body = str(parsed.body or query or "").strip()
    if action == "add_task":
        # Avoid dumping entire query; keep user-facing task payload compact.
        args["text"] = re.sub(r"^\s*(add\s+task|task:|todo:)\s*", "", body, flags=re.I).strip()[:220]
    elif action == "add_note_draft":
        args["text"] = re.sub(r"^\s*(add\s+note|note:|draft\s+note)\s*", "", body, flags=re.I).strip()[:220]
    elif action == "add_daily_log":
        args["text"] = _extract_daily_log_payload(body)[:220]
    elif action == "manage_blue_chips":
        toks = _extract_tickers_bulk(body)
        if toks:
            args["tickers"] = toks[:60]
            if re.search(r"\b(remove|delete|drop|exclude)\b", _norm(body)):
                args["operation"] = "remove"
            elif re.search(r"\b(add|include|put)\b", _norm(body)):
                args["operation"] = "add"
            else:
                args["operation"] = "list"
    return args


def _attach_mutation_execution_contract(res: AICommandResult, parsed: ParsedCommand, query: str) -> AICommandResult:
    action = str(parsed.action or "").strip()
    if not action:
        return res
    args = _mutation_call_args(parsed, query)
    status = str(res.status or "").strip().lower()
    verified = bool(status == "ok")
    payload = {
        "execution": {
            "type": "tool_execution",
            "function_call": {"name": action, "args": args},
            "function_response": {
                "name": action,
                "response": {
                    "status": "success" if verified else "failed",
                    "verified": verified,
                    "message": str(res.message or "")[:260],
                },
            },
        }
    }
    if isinstance(res.ui, dict):
        merged = dict(res.ui)
        merged.update(payload)
        res.ui = merged
    else:
        res.ui = payload
    return res


def _blend_mutation_message(
    query: str,
    base_message: str,
    *,
    action: str,
    verified: bool,
    status: str,
) -> str:
    if ask_ai is None:
        return str(base_message or "").strip()
    q = str(query or "").strip()
    bm = str(base_message or "").strip()
    if not bm:
        return bm
    prompt = (
        "Rewrite the system action result in natural conversational style.\n"
        "Keep it concise (1-2 short sentences), concrete, and truthful.\n"
        "Do not invent actions or numbers.\n"
        "Do not ask extra confirmation questions.\n"
        f"action={action}\nstatus={status}\nverified={1 if verified else 0}\n"
        f"user_query={q}\n"
        f"system_result={bm}\n"
        "Return plain text only."
    )
    try:
        with cf.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(
                ask_ai,
                prompt,
                "Concise post-action response writer. Plain text only.",
                "fast",
                False,
                0.1,
            )
            out = str(fut.result(timeout=1.4) or "").strip()
        if out:
            return out[:360]
    except Exception:
        pass
    return bm


def _intent_high_risk_queue(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    low = q.lower()
    risky = ("delete note" in low) or ("remove note" in low) or ("update thesis" in low)
    if not risky:
        return None
    trace = "tr_" + uuid.uuid4().hex[:12]
    ok = enqueue_action(
        tool_name="high_risk_request",
        params_json=json.dumps({"query": q}, ensure_ascii=True),
        reasoning="High-risk request requires manual approval before execution.",
        confidence=0.74,
        trace_id=trace,
    )
    if not ok:
        return AICommandResult(
            status="error",
            intent="queue_high_risk",
            message="Could not enqueue high-risk action.",
            confidence=0.2,
            citations=[],
            traces=[],
        )
    return AICommandResult(
        status="ok",
        intent="queue_high_risk",
        message="Action added to Approval Queue. Review in Organizer.",
        confidence=0.82,
        redirect_url="/organizer",
        citations=[],
        traces=[],
    )


def _intent_delete_notes(query: str, context: dict | None = None) -> AICommandResult | None:
    q = str(query or "").strip()
    if not q:
        return None
    low = _norm(q)
    has_delete_verb = any(k in low for k in ["delete", "remove"])
    if not has_delete_verb:
        return None
    has_note_target = ("note" in low or "notes" in low)
    has_this_target = bool(re.search(r"\b(this|that|it)\b", low))
    if not has_note_target and not has_this_target:
        return None

    ctx = context or {}
    tk = _extract_ticker(q)
    created_by = "ai" if ("ai generated" in low or "you generated" in low or "ai note" in low) else ""
    text_contains = ""
    m = re.search(r"\b(?:about|containing|with)\s+(.+)$", q, flags=re.I)
    if m:
        text_contains = str(m.group(1) or "").strip().strip("\"'`")
        # Avoid over-broad accidental wipes.
        if len(text_contains) < 3:
            text_contains = ""
    # If user says "remove this", infer target from visible notes context on the page.
    if not tk and has_this_target:
        vis = ctx.get("visible_notes")
        if isinstance(vis, list):
            tickers: dict[str, int] = {}
            for it in vis:
                if not isinstance(it, dict):
                    continue
                t = str(it.get("ticker") or "").strip().upper()
                if not t or t == "-":
                    continue
                tickers[t] = tickers.get(t, 0) + 1
            if len(tickers) == 1:
                tk = next(iter(tickers.keys()))

    # If the user explicitly asks "remove this", rely on current ticker context in sentence if present.
    if not tk and not text_contains and created_by == "":
        return AICommandResult(
            status="needs_input",
            intent="delete_notes",
            message="Specify what to delete. Example: `remove all notes from CRM` or `delete AI notes for ADBE`.",
            confidence=0.45,
            citations=[],
            traces=[],
        )

    tr = _run_tool(
        "delete_notes",
        {"ticker": tk, "text_contains": text_contains, "created_by": created_by},
        query=q,
    )
    data = tr.get("data") if isinstance(tr.get("data"), dict) else {}
    deleted = data.get("deleted") if isinstance(data.get("deleted"), dict) else {}
    if not tr.get("ok"):
        return AICommandResult(
            status="error",
            intent="delete_notes",
            message="Could not delete notes due to a storage error.",
            confidence=0.2,
            citations=[],
            traces=[{"step": "tool", "detail": "delete_notes:error"}],
        )
    total = int(deleted.get("total") or 0)
    if total <= 0:
        return AICommandResult(
            status="ok",
            intent="delete_notes",
            message="No matching notes found to delete.",
            confidence=0.9,
            redirect_url="/organizer",
            citations=[],
            traces=[{"step": "tool", "detail": "delete_notes"}],
        )
    scope = []
    if tk:
        scope.append(tk)
    if created_by:
        scope.append(f"created_by={created_by}")
    if text_contains:
        scope.append(f'text~"{text_contains[:40]}"')
    scope_msg = (" (" + ", ".join(scope) + ")") if scope else ""
    return AICommandResult(
        status="ok",
        intent="delete_notes",
        message=f"Deleted {total} note(s){scope_msg}.",
        confidence=0.95,
        redirect_url="/organizer",
        citations=[],
        traces=[{"step": "tool", "detail": "delete_notes"}],
    )


def _intent_search_reports(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    low = q.lower()
    norm = _norm(q)
    summary_fuzzy = _fuzzy_match(
        norm,
        [
            "summarize latest report",
            "summarize latest daily brief",
            "summarize latest daily briefing",
            "summary latest daily briefing",
            "summarize daily briefing",
            "latest daily briefing summary",
        ],
        threshold=0.62,
    )
    reportish = any(k in low for k in ["report", "reports", "summarize latest", "brief", "briefing"])
    searchish = any(k in low for k in ["search", "find"]) and any(k in low for k in ["report", "reports", "brief", "briefing"])
    if not (reportish or searchish or summary_fuzzy):
        return None

    rows = list_reports(limit=400)
    if not rows:
        return AICommandResult(
            status="ok",
            intent="search_reports",
            message="No reports found.",
            confidence=0.8,
            redirect_url="/reports",
            citations=[],
            traces=[],
        )

    want_daily_summary = bool(summary_fuzzy) or (
        ("summarize" in low or "summary" in low) and any(k in low for k in ["daily", "brief", "briefing"])
    )
    if "summarize latest" in low or want_daily_summary:
        top = rows[0]
        target_rows = rows
        if want_daily_summary:
            daily_rows = [
                r
                for r in rows
                if str(r.get("kind") or "").strip().lower() in {"daily brief", "morning intelligence"}
            ]
            if daily_rows:
                top = daily_rows[0]
                target_rows = daily_rows
        selected = target_rows[:5]
        texts: list[dict[str, str]] = []
        used_est_tokens = 0
        for r in selected:
            nm = str(r.get("name") or "").strip()
            if not nm:
                continue
            txt, err = read_report_file(nm, max_chars=2_000_000)
            if err:
                continue
            # Rough token estimate: ~4 chars/token.
            est = max(1, int(len(txt) / 4))
            if used_est_tokens + est > ANALYST_MAX_CONTEXT_TOKENS:
                continue
            used_est_tokens += est
            texts.append({"name": nm, "title": str(r.get("title") or nm), "text": txt})
        if not texts:
            return AICommandResult(
                status="error",
                intent="summarize_latest_report",
                message="Found reports but could not read content.",
                confidence=0.3,
                redirect_url="/reports",
                citations=[],
                traces=[],
            )
        snippet = ""
        if ask_ai is not None:
            try:
                blocks = []
                for i, t in enumerate(texts, start=1):
                    blocks.append(
                        f"[DOC {i}] {t['title']} ({t['name']})\n{t['text']}\n"
                    )
                prompt = (
                    "You are a skeptical Buy-Side Analyst. Use only provided documents. "
                    "Prioritize disconfirming evidence first. Think step-by-step internally, "
                    "but do not reveal chain-of-thought. "
                    "Do NOT extract/calculate financial metrics from raw SEC text; if numerical data is not provided as structured JSON, "
                    "state exactly: Data not available in structured filings.\n\n"
                    "Return STRICT JSON:\n"
                    '{"summary_bullets":["..."],"risk_bullets":["..."],"confidence":0.0}\n\n'
                    "DOCUMENTS:\n"
                    + "\n".join(blocks)
                )
                out = str(
                    ask_ai(
                        prompt,
                        "Financial analyst. Evidence-first output.",
                        mode="smart",
                        json_mode=True,
                        temperature=0.1,
                    )
                    or ""
                ).strip()
                obj = json.loads(out)
                sb = [str(x).strip() for x in list(obj.get("summary_bullets") or []) if str(x).strip()][:3]
                rb = [str(x).strip() for x in list(obj.get("risk_bullets") or []) if str(x).strip()][:2]
                parts = []
                if sb:
                    parts.append("Summary:\n" + "\n".join(f"- {x}" for x in sb))
                if rb:
                    parts.append("Key Risks:\n" + "\n".join(f"- {x}" for x in rb))
                snippet = "\n\n".join(parts).strip()
            except Exception:
                snippet = ""
        if not snippet:
            # Fallback deterministic extraction.
            lines: list[str] = []
            for raw in texts[0]["text"].splitlines():
                s = str(raw or "").strip()
                if not s:
                    continue
                l = s.lower()
                if "source:" in l or l.startswith("file:") or l.startswith("path:") or s.startswith("#"):
                    continue
                if s.startswith(("- ", "* ")):
                    s = s[2:].strip()
                elif re.match(r"^\d+\.\s+", s):
                    s = re.sub(r"^\d+\.\s+", "", s)
                if len(s) < 30:
                    continue
                lines.append(s)
                if len(lines) >= 3:
                    break
            snippet = "Summary:\n" + "\n".join(f"- {x}" for x in lines) if lines else "Summary unavailable from report text."
        return AICommandResult(
            status="ok",
            intent="summarize_latest_report",
            message=f"{top.get('title') or top.get('name')}\n{snippet}",
            confidence=0.75,
            # Keep summary-first UX: do not force a file-open button for summarize.
            redirect_url="",
            citations=[{"label": str(t["title"]), "url": f"/reports/view?name={t['name']}"} for t in texts[:5]],
            traces=[],
        )

    cleaned = re.sub(r"\b(search|find)\b", "", q, flags=re.I)
    cleaned = re.sub(r"\breports?\b", "", cleaned, flags=re.I).strip()
    if not cleaned:
        cleaned = q
    ql = cleaned.lower()
    hit = [
        r
        for r in rows
        if ql in str(r.get("name") or "").lower()
        or ql in str(r.get("title") or "").lower()
        or ql in str(r.get("kind") or "").lower()
    ]
    picked = (hit if hit else rows)[:5]
    citations = [
        {
            "label": str(r.get("title") or r.get("name") or "Report"),
            "url": f"/reports/view?name={r.get('name')}",
        }
        for r in picked
    ]
    return AICommandResult(
        status="ok",
        intent="search_reports",
        message=f"Found {len(hit) if hit else len(rows)} matching report(s). Showing top {len(picked)}.",
        confidence=0.8,
        redirect_url="/reports",
        citations=citations,
        traces=[],
    )


def _pick_report_for_query(query: str, rows: list[dict]) -> dict | None:
    if not rows:
        return None
    ql = _norm(query)
    if not ql:
        return rows[0]
    best = None
    best_score = -1.0
    for r in rows:
        name = str(r.get("name") or "").strip()
        title = str(r.get("title") or "").strip()
        kind = str(r.get("kind") or "").strip()
        hay = _norm(f"{name} {title} {kind}")
        if not hay:
            continue
        score = SequenceMatcher(None, ql, hay).ratio()
        if ql in hay:
            score += 0.4
        if score > best_score:
            best_score = score
            best = r
    return best or rows[0]


def _extract_section_request(query: str) -> str:
    q = str(query or "").strip()
    if not q:
        return ""
    m_quote = re.search(r"[\"“](.+?)[\"”]", q)
    if m_quote:
        return str(m_quote.group(1) or "").strip()[:120]
    m = re.search(
        r"\b(section|part|excerpt|focus|about|on)\s+(.+?)(?:\s+\b(from|in)\b.*|$)",
        q,
        flags=re.I,
    )
    if m:
        return str(m.group(2) or "").strip()[:120]
    low = _norm(q)
    for k in (
        "risk factors",
        "md&a",
        "management discussion",
        "segment",
        "earnings",
        "guidance",
        "capital allocation",
        "revenue",
        "margin",
    ):
        if k in low:
            return k
    return ""


def _extract_report_section_excerpt(report_text: str, section_query: str, max_chars: int = 2200) -> str:
    txt = str(report_text or "")
    sec = _norm(section_query)
    if not txt or not sec:
        return ""
    lines = txt.splitlines()
    if not lines:
        return ""

    # Prefer heading match first.
    heading_idxs: list[tuple[int, str]] = []
    for i, raw in enumerate(lines):
        s = str(raw or "").strip()
        if not s:
            continue
        if s.startswith("#") or re.match(r"^(ITEM|Item)\s+\d+[A-Z]?\b", s):
            heading_idxs.append((i, s))
    sec_toks = [t for t in sec.split() if len(t) >= 3][:6]
    for idx, h in heading_idxs:
        hn = _norm(h)
        if sec in hn or (sec_toks and sum(1 for t in sec_toks if t in hn) >= max(1, min(2, len(sec_toks)))):
            end = min(len(lines), idx + 1 + 180)
            for j, _h in heading_idxs:
                if j > idx:
                    end = j
                    break
            block = "\n".join(lines[idx:end]).strip()
            return block[:max_chars].strip()

    # Fallback: first semantic line hit + local context window.
    hit_idx = -1
    for i, raw in enumerate(lines):
        s = str(raw or "").strip()
        if not s:
            continue
        sn = _norm(s)
        if sec in sn or (sec_toks and sum(1 for t in sec_toks if t in sn) >= max(1, min(2, len(sec_toks)))):
            hit_idx = i
            break
    if hit_idx >= 0:
        lo = max(0, hit_idx - 8)
        hi = min(len(lines), hit_idx + 26)
        return "\n".join(lines[lo:hi]).strip()[:max_chars].strip()
    return ""


def _intent_report_section(query: str, context: dict | None = None) -> AICommandResult | None:
    q = str(query or "").strip()
    low = _norm(q)
    wants_part = bool(re.search(r"\b(section|part|excerpt|specific part|bring .* part|show .* part)\b", low))
    talks_report = "report" in low or "10-k" in low or "10-q" in low or "filing" in low
    in_chat = "chat" in low or "here" in low
    matched_by_rule = ((wants_part and talks_report) or (talks_report and in_chat and any(k in low for k in {"show", "bring", "give"})))
    if not matched_by_rule and not _llm_matches_intent(q, "report_section", min_conf=0.84):
        return None

    section_req = _extract_section_request(q)
    if not section_req:
        return AICommandResult(
            status="needs_input",
            intent="report_section",
            message="Which part do you want from the report? Example: `risk factors`, `MD&A`, or `segment reporting`.",
            confidence=0.52,
            citations=[],
            traces=[],
        )

    ctx = context or {}
    current = str(ctx.get("current_report_name") or "").strip()
    rows = list_reports(limit=400)
    target = None
    if current:
        target = {"name": current, "title": current, "kind": "Current"}
    if target is None:
        target = _pick_report_for_query(q, rows)
    if not target:
        return AICommandResult(
            status="ok",
            intent="report_section",
            message="No reports found yet.",
            confidence=0.72,
            citations=[],
            traces=[],
        )
    name = str(target.get("name") or "").strip()
    txt, err = read_report_file(name, max_chars=600_000)
    if err:
        return AICommandResult(
            status="error",
            intent="report_section",
            message=f"Could not read report section: {err}",
            confidence=0.32,
            citations=[],
            traces=[],
        )
    excerpt = _extract_report_section_excerpt(txt, section_req, max_chars=2200)
    if not excerpt:
        return AICommandResult(
            status="needs_input",
            intent="report_section",
            message=(
                f"I couldn’t find a clear `{section_req}` section in `{name}`.\n"
                "Try another part name or tell me the exact heading."
            ),
            confidence=0.46,
            citations=[{"label": str(target.get("title") or name), "url": f"/reports/view?name={name}"}],
            traces=[],
        )
    return AICommandResult(
        status="ok",
        intent="report_section",
        message=(
            f"{str(target.get('title') or name)} — {section_req}\n"
            f"```\n{excerpt}\n```"
        ),
        confidence=0.86,
        redirect_url="",
        citations=[{"label": str(target.get("title") or name), "url": f"/reports/view?name={name}"}],
        traces=[{"step": "report.section", "detail": section_req}],
    )


def _intent_summarize_current_report(query: str, context: dict | None = None) -> AICommandResult | None:
    q = str(query or "").strip()
    low = _norm(q)
    if not re.search(r"\b(summarize|summary|summiraze|explain|explanation|analyze|analysis|breakdown)\b", low):
        return None
    # Pronoun-based command must rely on current-page context.
    if not any(k in low for k in ["it", "this", "current", "opened", "open", "report"]):
        return None
    ctx = context or {}
    name = str(ctx.get("current_report_name") or "").strip()
    if not name:
        return AICommandResult(
            status="needs_input",
            intent="summarize_current_report",
            message="No open report context found. Open a report first, then run: summarize it.",
            confidence=0.35,
            citations=[],
            traces=[],
        )
    txt, err = read_report_file(name, max_chars=20000)
    if err:
        return AICommandResult(
            status="error",
            intent="summarize_current_report",
            message=f"Could not read current report: {err}",
            confidence=0.3,
            citations=[],
            traces=[],
        )
    explain_mode = bool(re.search(r"\b(explain|explanation|analyze|analysis|breakdown)\b", low))
    followup_focus = ""
    m_follow = re.search(r"follow-up:\s*(.+)$", q, flags=re.I | re.S)
    if m_follow:
        followup_focus = str(m_follow.group(1) or "").strip()
    snippet = ""
    if explain_mode and ask_ai is not None:
        try:
            prompt = (
                "You are a skeptical buy-side analyst.\n"
                "Read the report and explain what is happening in plain language.\n"
                "Do NOT extract/calculate financial metrics from raw SEC text; if numerical data is not provided as structured JSON, "
                "state exactly: Data not available in structured filings.\n"
                "Return STRICT JSON only, concise and non-repetitive.\n"
                "Schema:\n"
                '{"whats_happening":["<=18 words","..."],'
                '"connect_the_dots":["<=22 words","..."],'
                '"watch_next":["<=14 words","..."]}\n'
                "Rules:\n"
                "- Max 2 bullets per section.\n"
                "- No quoting long text.\n"
                "- No markdown headings.\n"
                "- Prioritize risks/disconfirming evidence first.\n\n"
            )
            if followup_focus:
                prompt += f"USER_FOLLOWUP_FOCUS: {followup_focus}\n\n"
            prompt += f"REPORT_NAME: {name}\nREPORT_TEXT:\n{txt[:200000]}"
            out = str(
                ask_ai(
                    prompt,
                    "Financial analyst. Evidence-first explanation from provided report.",
                    mode="smart",
                    json_mode=True,
                    temperature=0.1,
                )
                or ""
            ).strip()
            if out:
                obj = json.loads(out)
                w = [str(x).strip() for x in list(obj.get("whats_happening") or []) if str(x).strip()][:2]
                c = [str(x).strip() for x in list(obj.get("connect_the_dots") or []) if str(x).strip()][:2]
                n = [str(x).strip() for x in list(obj.get("watch_next") or []) if str(x).strip()][:2]
                parts: list[str] = []
                if w:
                    parts.append("What's Happening:\n" + "\n".join(f"- {x}" for x in w))
                if c:
                    parts.append("Connect The Dots:\n" + "\n".join(f"- {x}" for x in c))
                if n:
                    parts.append("Watch Next:\n" + "\n".join(f"- {x}" for x in n))
                snippet = "\n\n".join(parts).strip()
        except Exception:
            snippet = ""
    if not snippet:
        lines: list[str] = []
        for raw in txt.splitlines():
            s = str(raw or "").strip()
            if not s:
                continue
            low_s = s.lower().strip("*` ")
            if "source:" in low_s or "source mode" in low_s or low_s.startswith("file:") or low_s.startswith("path:") or s.startswith("#"):
                continue
            if s.startswith(("- ", "* ")):
                s = s[2:].strip()
            elif re.match(r"^\d+\.\s+", s):
                s = re.sub(r"^\d+\.\s+", "", s)
            if len(s) < 30:
                continue
            lines.append(s)
            if len(lines) >= 6:
                break
        bullets = lines[:3]
        lead = "Explanation" if explain_mode else "Summary"
        snippet = (
            f"{lead}:\n" + "\n".join(f"- {b}" for b in bullets)
            if bullets
            else f"{lead} unavailable from report text; open the report for details."
        )
    if explain_mode:
        snippet = (
            snippet.rstrip()
            + "\n\nIf you want, reply with one focus: `risks only`, `saas impact`, or `action list`."
        )
    return AICommandResult(
        status="ok",
        intent="summarize_current_report",
        message=f"Current Report ({name})\n{snippet}",
        confidence=0.83,
        redirect_url="",
        citations=[{"label": name, "url": f"/reports/view?name={name}"}],
        traces=[],
    )


def _intent_open_notes(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    low = _norm(q)
    if _contains_watchlist_term(low):
        return None
    toks = set(low.split())
    note_like = any(t in toks for t in {"note", "notes", "nots", "noets", "mynotes"})
    show_like = any(t in toks for t in {"show", "open", "list", "see", "display", "shom"})
    question_like = bool(re.search(r"\b(what|which|today|yesterday|how many|summary|summarize|tell)\b", low))
    fuzzy = _fuzzy_match(
        low,
        ["show me my notes", "open notes", "show notes", "list my notes", "my notes"],
        threshold=OPEN_NOTES_FUZZY_THRESHOLD,
    )
    is_rule = note_like and show_like
    if not (is_rule or fuzzy) or question_like:
        return None
    matched_by = "rule" if is_rule else "fuzzy"
    return AICommandResult(
        status="ok",
        intent="open_notes",
        message="Opening your notes file.",
        confidence=0.92 if matched_by == "rule" else 0.84,
        redirect_url="/organizer",
        citations=[],
        matched_by=matched_by,
        version=ORCHESTRATOR_VERSION,
        traces=[],
    )


def _read_watchlist_companies(limit: int = 120) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not WATCHLIST_PATH.exists():
        return out
    try:
        for ln in WATCHLIST_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = str(ln or "").strip()
            if not s or s.startswith("#"):
                continue
            parts = [x.strip() for x in s.split(",")]
            # Accept csv-ish rows and free text lines robustly.
            tk = _safe_ticker(parts[0] if parts else "")
            if not tk:
                m = re.search(r"\b([A-Z][A-Z0-9.\-]{0,11})\b", s.upper())
                tk = _safe_ticker(m.group(1) if m else "")
            if not tk:
                continue
            name = ""
            if len(parts) > 2:
                # In our file, col2 is usually timestamp; col3 is reason.
                name = str(parts[2] or "").strip()
            elif len(parts) > 1:
                name = str(parts[1] or "").strip()
            out.append({"ticker": tk, "name": name})
            if len(out) >= max(1, min(500, int(limit))):
                break
    except Exception:
        return []
    return out


def _is_info_request(low: str) -> bool:
    return bool(
        re.search(
            r"\b(what|which|why|important|today|update|updates|summary|summarize|explain|tell me)\b",
            str(low or ""),
        )
    )


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


def _extract_earnings_rows_from_text(txt: str, row_limit: int = 24) -> list[dict[str, str]]:
    lines = [str(x or "").rstrip() for x in str(txt or "").splitlines()]
    if not lines:
        return []

    rows: list[dict[str, str]] = []
    in_section = False
    cur_date = ""

    def _push(date: str, tm: str, symbol: str, company: str) -> None:
        tk = str(symbol or "").strip().upper()
        if not tk:
            return
        rows.append(
            {
                "date": str(date or "").strip(),
                "time": str(tm or "").strip() or "-",
                "symbol": tk,
                "company": str(company or "").strip() or "-",
            }
        )

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

        # Markdown table row: | Date | Time | Symbol | Company |
        if s.startswith("|") and s.count("|") >= 4 and not re.search(r"^\|\s*-{2,}", s):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) >= 4:
                date_cell = cells[0]
                time_cell = cells[1]
                sym_cell = cells[2]
                cmp_cell = cells[3]
                if re.match(r"^\d{4}-\d{2}-\d{2}$", date_cell):
                    cur_date = date_cell
                _push(cur_date or date_cell, time_cell, sym_cell, cmp_cell)
                if len(rows) >= row_limit:
                    break
                continue

        # Compact row formats: "Pre WMT • Walmart Inc."
        m1 = re.match(r"^(Pre|Post|Day|BMO|AMC)\s+([A-Za-z0-9.\-]+)\s*[•\-]\s*(.+)$", s, flags=re.I)
        if m1:
            _push(cur_date, m1.group(1).upper(), m1.group(2), m1.group(3))
            if len(rows) >= row_limit:
                break
            continue

        # Compact row: "WMT • Walmart Inc."
        m2 = re.match(r"^([A-Za-z0-9.\-]{1,10})\s*[•\-]\s*(.+)$", s)
        if m2:
            _push(cur_date, "-", m2.group(1), m2.group(2))
            if len(rows) >= row_limit:
                break
            continue

    if rows:
        return rows[:row_limit]

    # Fallback: scan entire text for compact rows.
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        md = re.search(r"(\d{4}-\d{2}-\d{2})", s)
        if md:
            cur_date = md.group(1)
        m = re.match(r"^(Pre|Post|Day|BMO|AMC)?\s*([A-Za-z0-9.\-]{1,10})\s*[•\-]\s*(.+)$", s, flags=re.I)
        if m:
            _push(cur_date, (m.group(1) or "-").upper(), m.group(2), m.group(3))
            if len(rows) >= row_limit:
                break
    return rows[:row_limit]


def _read_earnings_calendar_rows(limit: int = 24) -> tuple[list[dict[str, str]], str]:
    p, txt = _latest_earnings_source()
    if not p or not txt:
        return [], "-"
    rows = _extract_earnings_rows_from_text(txt, row_limit=max(1, int(limit)))
    return rows, p.name


def _run_earnings_refresh(timeout_sec: int = 120) -> tuple[bool, str]:
    script = ROOT / "bin" / "run_earnings"
    if not script.exists():
        return False, "missing_run_earnings_script"
    try:
        proc = subprocess.run(
            [str(script)],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(20, int(timeout_sec)),
            check=False,
        )
        ok = int(proc.returncode or 1) == 0
        if ok:
            return True, "ok"
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, (err[-1][:180] if err else f"exit_{proc.returncode}")
    except subprocess.TimeoutExpired:
        return False, "timeout"
    except Exception as exc:
        return False, str(exc)[:160]


def _intent_list_earnings(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    low = _norm(q)
    matched_by_rule = (
        "earnings" in low
        or ("calendar" in low and any(k in low for k in {"week", "today", "report", "results"}))
    )
    if not matched_by_rule and not _llm_matches_intent(q, "list_earnings", min_conf=0.84):
        return None

    open_like = any(k in low for k in {"open", "go", "take", "navigate"})
    refresh_like = any(k in low for k in {"refresh", "update", "sync", "reload"})
    list_like = any(k in low for k in {"show", "list", "what", "which", "tell", "here", "chat", "calendar"})

    traces: list[dict[str, str]] = []
    refresh_note = ""
    if refresh_like:
        ok, detail = _run_earnings_refresh(timeout_sec=150)
        traces.append({"step": "earnings.refresh", "detail": "ok" if ok else f"fail:{detail}"})
        refresh_note = "Refreshed earnings feed.\n\n" if ok else ""

    rows, source = _read_earnings_calendar_rows(limit=24)
    traces.append({"step": "earnings.source", "detail": source or "-"})
    traces.append({"step": "earnings.rows", "detail": str(len(rows))})
    auto_refresh_reason = ""

    # Evidence-first recovery: if empty and user asked for earnings data, self-recover once.
    if not rows and not refresh_like:
        ok2, detail2 = _run_earnings_refresh(timeout_sec=150)
        traces.append({"step": "earnings.auto_refresh", "detail": "ok" if ok2 else f"fail:{detail2}"})
        if ok2:
            rows, source = _read_earnings_calendar_rows(limit=24)
            traces.append({"step": "earnings.rows_after_refresh", "detail": str(len(rows))})
        else:
            auto_refresh_reason = str(detail2 or "").strip()

    if open_like and not list_like and not refresh_like:
        return AICommandResult(
            status="ok",
            intent="list_earnings",
            message="Opening Dashboard earnings calendar.",
            confidence=0.95,
            redirect_url="/dashboard",
            citations=[],
            traces=traces,
        )

    if not rows:
        return AICommandResult(
            status="ok",
            intent="list_earnings",
            message=(
                refresh_note
                + "I could not find earnings calendar rows from current sources.\n"
                + f"Source scanned: {source or '-'}.\n"
                + (f"Refresh attempt result: {auto_refresh_reason}.\n" if auto_refresh_reason else "")
                + "Possible reasons: source file not generated yet, stale report feed, or parser found no structured earnings rows."
            ),
            confidence=0.74,
            redirect_url="",
            citations=[],
            traces=traces,
        )

    lines = ["Earnings Calendar (This Week):"]
    shown = 0
    last_date = ""
    for r in rows:
        d = str(r.get("date") or "").strip() or "-"
        tm = str(r.get("time") or "").strip() or "-"
        tk = str(r.get("symbol") or "").strip().upper()
        nm = str(r.get("company") or "").strip()
        if d != last_date:
            lines.append(f"\n{d}")
            last_date = d
        lines.append(f"- {tm} {tk}" + (f" — {nm}" if nm else ""))
        shown += 1
        if shown >= 16:
            break
    if len(rows) > shown:
        lines.append(f"\n...and {len(rows) - shown} more.")
    lines.append(f"\nSource: {source}")
    lines.append("If you want, I can open Dashboard too.")
    return AICommandResult(
        status="ok",
        intent="list_earnings",
        message=(refresh_note + "\n".join(lines)).strip(),
        confidence=0.91,
        redirect_url="",
        citations=[],
        traces=traces,
    )


def _intent_morning_updates(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    low = _norm(q)
    asks_morning = (
        ("morning" in low and any(k in low for k in {"update", "updates", "brief", "briefing"}))
        or ("important" in low and "today" in low and ("morning" in low or "update" in low or "brief" in low))
    )
    if not asks_morning:
        return None

    # Bounded ReAct loop: at most 3 internal steps.
    traces: list[dict[str, str]] = [{"step": "react.think", "detail": "morning_updates -> fetch + summarize"}]
    brief = get_cached_morning_brief()
    traces.append({"step": "react.act", "detail": "get_cached_morning_brief"})
    if not isinstance(brief, dict) or not brief:
        brief = save_morning_brief_snapshot(limit_holdings=5, source="on_demand")
        traces.append({"step": "react.act", "detail": "save_morning_brief_snapshot"})
    bullets = [str(x).strip() for x in list((brief or {}).get("bullets") or []) if str(x).strip()]
    if not bullets:
        traces.append({"step": "react.observe", "detail": "no_bullets"})
        return AICommandResult(
            status="ok",
            intent="morning_updates",
            message="Morning update is quiet right now. No critical anomalies detected yet.",
            confidence=0.9,
            redirect_url="",
            citations=[],
            traces=traces,
        )
    msg = "Morning updates (today):\n" + "\n".join(f"- {b}" for b in bullets[:3])
    msg += "\n\nMost important today is the item with the largest direct portfolio impact."
    msg += "\nIf you want, I can open the full report page."
    traces.append({"step": "react.final", "detail": "summarized"})
    return AICommandResult(
        status="ok",
        intent="morning_updates",
        message=msg,
        confidence=0.94,
        redirect_url="",
        citations=[],
        traces=traces,
    )


def _intent_list_watchlist(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    low = _norm(q)
    has_watchlist = _contains_watchlist_term(low)
    if not has_watchlist:
        return None
    list_like = any(k in low for k in {"show", "list", "what", "which", "tell", "my"})
    open_like = any(k in low for k in {"open", "go", "take", "navigate"})
    if not list_like and not open_like:
        return None
    info_like = _is_info_request(low) and not open_like
    rows = _read_watchlist_companies(limit=80)
    if open_like and not list_like:
        return AICommandResult(
            status="ok",
            intent="list_watchlist",
            message="Opening Watchlist.",
            confidence=0.95,
            redirect_url="/my_universe?tab=all",
            citations=[],
            traces=[],
        )
    if not rows:
        return AICommandResult(
            status="ok" if info_like else "needs_confirmation",
            intent="list_watchlist",
            message=(
                "Your watchlist is empty."
                + ("\nIf you want, I can open the Watchlist page." if info_like else " Do you want me to open the Watchlist page anyway?")
            ),
            confidence=0.94,
            redirect_url="",
            citations=[],
            traces=[],
        )
    lines = ["Your watchlist companies:"]
    for r in rows[:30]:
        tk = str(r.get("ticker") or "").strip().upper()
        nm = str(r.get("name") or "").strip()
        lines.append(f"- {tk}" + (f" — {nm}" if nm else ""))
    if len(rows) > 30:
        lines.append(f"- ...and {len(rows)-30} more")
    return AICommandResult(
        status="ok" if info_like else "needs_confirmation",
        intent="list_watchlist",
        message=(
            "\n".join(lines)
            + ("\n\nIf you want, I can open the Watchlist page." if info_like else "\n\nDo you want me to open the Watchlist page?")
        ),
        confidence=0.95,
        redirect_url="",
        citations=[],
        traces=[],
    )


def _intent_manage_blue_chips(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    low = _norm(q)
    if not _contains_blue_chip_term(low):
        return None

    list_like = any(k in low for k in {"show", "list", "what", "which", "tell", "my"})
    open_like = any(k in low for k in {"open", "go", "take", "navigate"})
    add_like = any(k in low for k in {"add", "include", "put"})
    remove_like = any(k in low for k in {"remove", "delete", "drop", "exclude"})
    info_like = _is_info_request(low) and not open_like

    if add_like:
        tks = _extract_tickers_bulk(q)
        if not tks:
            return AICommandResult(
                status="needs_input",
                intent="list_blue_chips",
                message="Which ticker should I add to Blue Chips? Example: add GOOGL to blue chips.",
                confidence=0.52,
                citations=[],
                traces=[],
            )
        added: list[str] = []
        failed: list[str] = []
        for tk in tks:
            if _upsert_blue_chip(tk, reason="Added via Ask AI"):
                added.append(tk)
            else:
                failed.append(tk)
        # Verify-after-write: only confirm truly persisted rows.
        if added:
            now_rows = {str(x.get("ticker") or "").strip().upper() for x in _read_blue_chips_companies(limit=1200)}
            verified = [t for t in added if t in now_rows]
            unverified = [t for t in added if t not in now_rows]
            added = verified
            failed.extend(unverified)
        if not added:
            return AICommandResult(
                status="error",
                intent="list_blue_chips",
                message="Could not add the requested Blue Chips right now.",
                confidence=0.3,
                citations=[],
                traces=[],
            )
        if len(added) == 1 and not failed:
            return AICommandResult(
                status="ok",
                intent="list_blue_chips",
                message=f"Added {added[0]} to Blue Chips.",
                confidence=0.95,
                citations=[],
                traces=[],
            )
        msg = f"Added {len(added)} ticker(s) to Blue Chips: " + ", ".join(added[:24])
        if len(added) > 24:
            msg += f", ... (+{len(added) - 24} more)"
        if failed:
            msg += "\nFailed: " + ", ".join(failed[:12])
        return AICommandResult(
            status="ok",
            intent="list_blue_chips",
            message=msg,
            confidence=0.95,
            citations=[],
            traces=[],
        )

    if remove_like:
        tks = _extract_tickers_bulk(q)
        if not tks:
            return AICommandResult(
                status="needs_input",
                intent="list_blue_chips",
                message="Which ticker should I remove from Blue Chips?",
                confidence=0.52,
                citations=[],
                traces=[],
            )
        removed: list[str] = []
        missing: list[str] = []
        for tk in tks:
            if _remove_blue_chip(tk):
                removed.append(tk)
            else:
                missing.append(tk)
        # Verify-after-write: ensure rows are actually removed.
        if removed:
            now_rows = {str(x.get("ticker") or "").strip().upper() for x in _read_blue_chips_companies(limit=1200)}
            still_there = [t for t in removed if t in now_rows]
            if still_there:
                removed = [t for t in removed if t not in still_there]
                missing.extend(still_there)
        if not removed and missing:
            return AICommandResult(
                status="ok",
                intent="list_blue_chips",
                message="Requested ticker(s) were not in Blue Chips: " + ", ".join(missing[:16]),
                confidence=0.9,
                citations=[],
                traces=[],
            )
        if len(removed) == 1 and not missing:
            return AICommandResult(
                status="ok",
                intent="list_blue_chips",
                message=f"Removed {removed[0]} from Blue Chips.",
                confidence=0.95,
                citations=[],
                traces=[],
            )
        msg = f"Removed {len(removed)} ticker(s) from Blue Chips: " + ", ".join(removed[:24])
        if len(removed) > 24:
            msg += f", ... (+{len(removed) - 24} more)"
        if missing:
            msg += "\nNot found: " + ", ".join(missing[:12])
        return AICommandResult(
            status="ok",
            intent="list_blue_chips",
            message=msg,
            confidence=0.95,
            citations=[],
            traces=[],
        )

    if open_like and not list_like:
        return AICommandResult(
            status="ok",
            intent="list_blue_chips",
            message="Opening Blue Chips.",
            confidence=0.95,
            redirect_url="/my_universe?tab=bluechips",
            citations=[],
            traces=[],
        )

    rows = _read_blue_chips_companies(limit=80)
    if not rows:
        return AICommandResult(
            status="ok" if info_like else "needs_confirmation",
            intent="list_blue_chips",
            message=(
                "Your Blue Chips list is empty."
                + ("\nIf you want, I can open the Blue Chips page." if info_like else " Do you want me to open the Blue Chips page anyway?")
            ),
            confidence=0.94,
            redirect_url="",
            citations=[],
            traces=[],
        )

    lines = ["Your blue chips companies:"]
    for r in rows[:30]:
        tk = str(r.get("ticker") or "").strip().upper()
        nm = str(r.get("name") or "").strip()
        lines.append(f"- {tk}" + (f" — {nm}" if nm else ""))
    if len(rows) > 30:
        lines.append(f"- ...and {len(rows)-30} more")
    return AICommandResult(
        status="ok" if info_like else "needs_confirmation",
        intent="list_blue_chips",
        message=(
            "\n".join(lines)
            + ("\n\nIf you want, I can open the Blue Chips page." if info_like else "\n\nDo you want me to open the Blue Chips page?")
        ),
        confidence=0.95,
        redirect_url="",
        citations=[],
        traces=[],
    )


def _intent_list_portfolio(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    low = _norm(q)
    # Status/performance questions should route to live portfolio summary intent.
    if _looks_like_portfolio_status_query(low):
        return None
    if not any(k in low for k in {"portfolio", "holdings", "active positions"}):
        return None
    if not any(k in low for k in {"show", "list", "what", "which", "tell", "my", "open", "go", "take", "navigate"}):
        return None
    info_like = _is_info_request(low) and not bool(re.search(r"\b(open|go|take|navigate)\b", low))
    tr = _run_tool("get_holdings", {"limit": 60}, query=q)
    data = tr.get("data") if isinstance(tr.get("data"), dict) else {}
    rows = data.get("rows") if isinstance(data.get("rows"), list) else []
    if not rows:
        return AICommandResult(
            status="ok" if info_like else "needs_confirmation",
            intent="list_portfolio",
            message=(
                "No portfolio holdings found yet."
                + ("\nIf you want, I can open the Portfolio page." if info_like else " Do you want me to open the Portfolio page anyway?")
            ),
            confidence=0.93,
            redirect_url="",
            citations=[],
            traces=[{"step": "tool", "detail": "get_holdings"}],
        )
    lines = ["Your portfolio holdings:"]
    for r in rows[:20]:
        tk = str(r.get("ticker") or "").strip().upper()
        sh = float(r.get("shares") or 0.0)
        lines.append(f"- {tk} ({sh:g} shares)")
    if len(rows) > 20:
        lines.append(f"- ...and {len(rows)-20} more")
    return AICommandResult(
        status="ok" if info_like else "needs_confirmation",
        intent="list_portfolio",
        message=(
            "\n".join(lines)
            + ("\n\nIf you want, I can open the Portfolio page." if info_like else "\n\nDo you want me to open the Portfolio page?")
        ),
        confidence=0.95,
        redirect_url="",
        citations=[],
        traces=[{"step": "tool", "detail": "get_holdings"}],
    )


def _intent_list_notes(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    low = _norm(q)
    if "note" not in low and "notes" not in low:
        return None
    if not any(k in low for k in {"show", "list", "my", "what", "which", "tell", "open", "go", "take", "navigate"}):
        return None
    info_like = _is_info_request(low) and not bool(re.search(r"\b(open|go|take|navigate)\b", low))
    rows = list_recent_notes(limit=12)
    if not rows:
        return AICommandResult(
            status="ok" if info_like else "needs_confirmation",
            intent="list_notes",
            message=(
                "No notes found yet."
                + ("\nIf you want, I can open the Notes page." if info_like else " Do you want me to open the Notes page anyway?")
            ),
            confidence=0.93,
            redirect_url="",
            citations=[],
            traces=[],
        )
    lines = ["Your recent notes:"]
    for r in rows[:6]:
        tk = str(r.get("ticker") or "").strip().upper()
        txt = str(r.get("text") or "").strip().replace("\n", " ")
        head = f"[{tk}] " if tk else ""
        lines.append(f"- {head}{txt[:120]}")
    return AICommandResult(
        status="ok" if info_like else "needs_confirmation",
        intent="list_notes",
        message=(
            "\n".join(lines)
            + ("\n\nIf you want, I can open the Notes page." if info_like else "\n\nDo you want me to open the Notes page?")
        ),
        confidence=0.95,
        redirect_url="",
        citations=[],
        traces=[],
    )


def _intent_list_reports(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    low = _norm(q)
    if "report" not in low and "reports" not in low:
        return None
    if not any(k in low for k in {"show", "list", "my", "what", "latest", "open", "go", "take", "navigate"}):
        return None
    info_like = _is_info_request(low) and not bool(re.search(r"\b(open|go|take|navigate)\b", low))
    rows = list_reports(limit=8)
    if not rows:
        return AICommandResult(
            status="ok" if info_like else "needs_confirmation",
            intent="list_reports",
            message=(
                "No reports found yet."
                + ("\nIf you want, I can open the Reports page." if info_like else " Do you want me to open the Reports page anyway?")
            ),
            confidence=0.93,
            redirect_url="",
            citations=[],
            traces=[],
        )
    lines = ["Recent reports:"]
    for r in rows[:5]:
        nm = str(r.get("name") or "").strip()
        ts = str(r.get("modified_at") or "").replace("T", " ")[:16]
        lines.append(f"- {nm} ({ts})")
    return AICommandResult(
        status="ok" if info_like else "needs_confirmation",
        intent="list_reports",
        message=(
            "\n".join(lines)
            + ("\n\nIf you want, I can open the Reports page." if info_like else "\n\nDo you want me to open the Reports page?")
        ),
        confidence=0.95,
        redirect_url="",
        citations=[],
        traces=[],
    )


def _intent_list_tasks(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    low = _norm(q)
    if not re.search(r"\b(task|tasks|todo|to[\s-]?do)\b", low):
        return None

    if re.search(r"\b(add|create|new|remind)\b", low):
        return None

    list_like = bool(re.search(r"\b(show|list|tell|what|which|my|today)\b", low)) or low in {
        "task",
        "tasks",
        "todo",
        "to do",
        "to-do",
    }
    open_like = bool(re.search(r"\b(open|go|take|navigate)\b", low))
    if not list_like and not open_like:
        return None

    if open_like and not list_like:
        return AICommandResult(
            status="ok",
            intent="list_tasks",
            message="Opening Organizer Tasks.",
            confidence=0.95,
            redirect_url="/organizer",
            citations=[],
            traces=[],
        )

    rows = list_tasks(open_only=True, limit=30)
    if not rows:
        return AICommandResult(
            status="needs_confirmation",
            intent="list_tasks",
            message="No open tasks right now. Do you want me to open Organizer anyway?",
            confidence=0.94,
            redirect_url="",
            citations=[],
            traces=[],
        )

    lines = ["Your open tasks and to-do items:"]
    for r in rows[:20]:
        rid = int(r["id"] or 0)
        task = str(r["task"] or "").strip().replace("\n", " ")
        due = str(r["due_date"] or "").strip()
        tk = str(r["ticker"] or "").strip().upper()
        task_line = f"- {task}"
        if tk:
            task_line += f" [{tk}]"
        if due:
            task_line += f" (due {due})"
        if rid > 0:
            task_line += f" [#{rid}]"
        lines.append(task_line)
    if len(rows) > 20:
        lines.append(f"- ...and {len(rows)-20} more")

    return AICommandResult(
        status="needs_confirmation",
        intent="list_tasks",
        message="\n".join(lines) + "\n\nDo you want me to open Organizer?",
        confidence=0.95,
        redirect_url="",
        citations=[],
        traces=[],
    )


def _intent_navigation_followup(query: str, context: dict | None = None) -> AICommandResult | None:
    ctx = context or {}
    last = str(ctx.get("last_intent") or "").strip()
    target_by_intent = {
        "list_watchlist": ("/my_universe?tab=all", "Watchlist"),
        "list_portfolio": ("/my_universe?tab=all", "Portfolio"),
        "list_blue_chips": ("/my_universe?tab=bluechips", "Blue Chips"),
        "list_notes": ("/organizer", "Notes"),
        "list_reports": ("/reports", "Reports"),
        "list_tasks": ("/organizer", "Organizer"),
    }
    if last not in target_by_intent:
        return None
    # If user asks the same domain again (e.g., "show me my watchlist"), treat it as a fresh request.
    low_q = _norm(query)
    # If user sends another explicit navigation/list command, do not trap it in yes/no follow-up.
    command_like = any(
        k in low_q for k in {"show", "open", "list", "go", "take", "navigate", "what", "which", "tell", "my", "add", "remove", "delete", "create"}
    )
    # Analytical/comparison questions should bypass confirmation and route to reasoning/tool flows.
    analysis_like = bool(
        re.search(
            r"\b(compare|comparison|better|best|worse|vs|versus|business|moat|margin|growth|thesis|risk|valuation|quality)\b",
            low_q,
        )
    ) or bool(re.search(r"\b[A-Z]{2,6}\s+(or|vs|versus)\s+[A-Z]{2,6}\b", str(query or ""), flags=re.I))
    if analysis_like:
        return None
    domain_like = (
        _contains_watchlist_term(low_q)
        or _contains_blue_chip_term(low_q)
        or _contains_portfolio_term(low_q)
        or any(
            k in low_q
            for k in {
                "notes",
                "note",
                "reports",
                "report",
                "organizer",
                "dashboard",
                "task",
                "tasks",
                "todo",
                "to-do",
                "to do",
                "morning",
                "update",
                "updates",
                "brief",
                "briefing",
            }
        )
        or bool(re.search(r"\bto[\s-]?do\b", low_q))
    )
    if command_like and domain_like:
        return None
    if last == "list_watchlist" and _contains_watchlist_term(low_q):
        return None
    if last == "list_blue_chips" and _contains_blue_chip_term(low_q):
        return None
    if last == "list_portfolio" and _contains_portfolio_term(low_q):
        return None
    if last == "list_notes" and ("note" in low_q or "notes" in low_q):
        return None
    if last == "list_reports" and ("report" in low_q or "reports" in low_q):
        return None
    if last == "list_tasks" and re.search(r"\b(task|tasks|todo|to[\s-]?do)\b", low_q):
        return None
    low = low_q
    if re.fullmatch(r"(yes|yeah|yep|sure|ok|okay|open|open it|open now|go ahead|do it)", low):
        rd, label = target_by_intent[last]
        return AICommandResult(
            status="ok",
            intent="open_page",
            message=f"Opening {label}.",
            confidence=0.96,
            redirect_url=rd,
            citations=[],
            traces=[{"step": "tool", "detail": "maps_to"}],
        )
    if re.fullmatch(r"(no|nope|nah|not now|later|cancel|back to chat)", low):
        return AICommandResult(
            status="ok",
            intent=last,
            message="Perfect, keeping it in chat only.",
            confidence=0.96,
            redirect_url="",
            citations=[],
            traces=[],
        )
    hist = ctx.get("history") if isinstance(ctx.get("history"), list) else []
    repeated_unclear = 0
    for h in reversed(hist[-20:]):
        if not isinstance(h, dict):
            continue
        if str(h.get("role") or "").strip().lower() != "assistant":
            continue
        if str(h.get("intent") or "").strip() != last:
            continue
        if str(h.get("status") or "").strip() != "needs_confirmation":
            continue
        msg = str(h.get("text") or "")
        if "Please reply with `yes` to open the page" in msg:
            repeated_unclear += 1
        else:
            break
    if repeated_unclear >= 1:
        return AICommandResult(
            status="needs_clarification",
            intent=last,
            message="I’m stuck in confirmation. Choose one: Open now, Cancel, or Back to chat.",
            confidence=0.96,
            redirect_url="",
            citations=[],
            traces=[],
        )
    return AICommandResult(
        status="needs_confirmation",
        intent=last,
        message="Please reply with `yes` to open the page or `no` to stay in chat.",
        confidence=0.96,
        redirect_url="",
        citations=[],
        traces=[],
    )


def _manager_rule_intent(query: str) -> tuple[str, float]:
    low = _norm(query)
    if not low:
        return "", 0.0
    if YES_NO_ONLY_RE.fullmatch(low):
        return "", 0.0

    has_nav_verb = any(
        k in low
        for k in {
            "show",
            "list",
            "open",
            "go",
            "take",
            "navigate",
            "tell",
            "what",
            "which",
            "my",
        }
    )
    only_token = low in {"watchlist", "blue chips", "blue chip", "bluechips", "portfolio", "holdings", "notes", "reports", "tasks", "task", "todo", "to do", "to-do"}

    if _looks_like_portfolio_status_query(low):
        return "portfolio_today_status", 0.98

    if _contains_watchlist_term(low) and (has_nav_verb or only_token):
        return "list_watchlist", 0.97
    if _contains_blue_chip_term(low) and (has_nav_verb or only_token):
        return "list_blue_chips", 0.97
    if _contains_portfolio_term(low) and (has_nav_verb or only_token):
        return "list_portfolio", 0.97
    if ("earnings" in low or "calendar" in low) and (has_nav_verb or only_token):
        return "list_earnings", 0.97
    if re.search(r"\b(task|tasks|todo|to[\s-]?do)\b", low) and (has_nav_verb or only_token):
        return "list_tasks", 0.97
    if any(k in low for k in {"notes", "note"}) and (has_nav_verb or only_token):
        return "list_notes", 0.95
    if any(k in low for k in {"reports", "report"}) and (has_nav_verb or only_token):
        return "list_reports", 0.95
    if ("report" in low or "10-k" in low or "10-q" in low) and any(k in low for k in {"section", "part", "excerpt"}):
        return "report_section", 0.95
    if ("morning" in low and any(k in low for k in {"update", "updates", "brief", "briefing"})) or (
        "important" in low and "today" in low and ("morning" in low or "update" in low or "brief" in low)
    ):
        return "morning_updates", 0.96
    if re.search(r"\b(open|show|take|go|navigate)\b.*\bcompany\b", low) or re.search(r"\bcompany\b.*\b(open|show)\b", low):
        if _extract_ticker(query):
            return "open_company", 0.93
    if re.search(r"\b(summarize|summary|recap|digest)\b.*\b(report|reports)\b", low):
        return "summarize_latest_report", 0.92
    if re.search(r"\b(find|search)\b.*\b(report|reports|10-k|10-q)\b", low):
        return "search_reports", 0.9
    if re.search(r"\b(what|which|summarize|summary|tell|how many)\b.*\b(note|notes)\b", low):
        return "notes_summary", 0.9
    if any(k in low for k in {"start portfolio interview", "interview mode", "start interview"}):
        return "portfolio_interview", 0.95
    return "", 0.0


def _llm_matches_intent(query: str, target_intent: str, min_conf: float = 0.82) -> bool:
    q = str(query or "").strip()
    target = str(target_intent or "").strip()
    if not q or not target or ask_ai is None:
        return False
    prompt = (
        "Classify whether this user message intends the target action.\n"
        f"Target intent: {target}\n"
        "Return strict JSON only: {\"match\":true|false,\"confidence\":0.0}\n"
        f"User message: {q}"
    )
    try:
        with cf.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(
                ask_ai,
                prompt,
                "Intent matcher. JSON only.",
                "fast",
                True,
                0.0,
            )
            raw = str(fut.result(timeout=1.2) or "").strip()
        if not raw:
            return False
        obj = json.loads(raw)
        matched = bool(obj.get("match"))
        conf = float(obj.get("confidence") or 0.0)
        return bool(matched and conf >= float(min_conf))
    except Exception:
        return False


def _manager_llm_interrupt_intent(query: str, active_worker: str) -> tuple[str, float]:
    q = str(query or "").strip()
    if not q or ask_ai is None:
        return "", 0.0
    prompt = (
        "Classify whether this message is a topic switch away from current worker.\n"
        "Current worker: " + active_worker + "\n"
        "Return strict JSON only:\n"
        '{"intent":"none|morning_updates|list_watchlist|list_blue_chips|list_portfolio|list_earnings|portfolio_today_status|list_tasks|list_notes|list_reports|report_section|open_company|open_notes|notes_summary|summarize_latest_report|search_reports|portfolio_interview","confidence":0.0}\n'
        "If message is only yes/no confirmation, return intent='none'.\n"
        "User: " + q
    )
    try:
        with cf.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(
                ask_ai,
                prompt,
                "Intent classifier for manager routing. JSON only.",
                "fast",
                True,
                0.0,
            )
            raw = str(fut.result(timeout=MANAGER_LLM_TIMEOUT_SEC) or "").strip()
        if not raw:
            return "", 0.0
        obj = json.loads(raw)
        intent = str(obj.get("intent") or "").strip()
        conf = float(obj.get("confidence") or 0.0)
        if intent == "none":
            return "", 0.0
        if intent not in INTERRUPTIBLE_INTENTS:
            return "", 0.0
        return intent, max(0.0, min(1.0, conf))
    except Exception:
        return "", 0.0


def _manager_detect_interrupt(query: str, context: dict | None = None) -> tuple[str, float, str]:
    ctx = context or {}
    if bool(ctx.get("disable_manager_interrupts")):
        return "", 0.0, "disabled"
    active_worker = str(ctx.get("last_intent") or "").strip()
    if active_worker not in INTERRUPTIBLE_INTENTS:
        return "", 0.0, ""

    rule_intent, rule_conf = _manager_rule_intent(query)
    if rule_intent and rule_intent != active_worker and rule_conf >= MANAGER_INTERRUPT_THRESHOLD:
        return rule_intent, rule_conf, "rule"

    llm_intent, llm_conf = _manager_llm_interrupt_intent(query, active_worker)
    if llm_intent and llm_intent != active_worker and llm_conf >= MANAGER_INTERRUPT_THRESHOLD:
        return llm_intent, llm_conf, "llm"
    return "", 0.0, ""


def _manager_rewrite_query(target_intent: str, original_query: str) -> str:
    low = _norm(original_query)
    if target_intent == "list_watchlist":
        if _contains_watchlist_term(low):
            return "show me my watchlist"
    elif target_intent == "list_blue_chips":
        if _contains_blue_chip_term(low):
            if any(k in low for k in {"add", "include", "put", "remove", "delete", "drop", "exclude"}):
                return original_query
            return "show me my blue chips"
    elif target_intent == "list_portfolio":
        if _contains_portfolio_term(low):
            return "show me my portfolio"
    elif target_intent == "list_earnings":
        if "earnings" in low or "calendar" in low:
            return "show earnings calendar in chat"
    elif target_intent == "portfolio_today_status":
        if _contains_portfolio_term(low):
            return "how is my portfolio today?"
    elif target_intent == "list_tasks":
        if re.search(r"\b(task|tasks|todo|to[\s-]?do)\b", low):
            return "tell me my to do and tasks"
    elif target_intent == "list_notes":
        if "note" in low:
            return "show me my notes"
    elif target_intent == "list_reports":
        if "report" in low:
            return "show me my reports"
    elif target_intent == "open_company":
        tk = _extract_ticker(original_query)
        if tk:
            return f"open company {tk}"
    elif target_intent == "notes_summary":
        if "note" in low:
            return "what are my notes today?"
    elif target_intent == "summarize_latest_report":
        if "report" in low:
            return "summarize latest report"
    elif target_intent == "search_reports":
        return original_query
    elif target_intent == "report_section":
        return original_query
    elif target_intent == "morning_updates":
        return "what is in the morning updates and what was important today?"
    return original_query


def _intent_notes_summary(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    low = _norm(q)
    if "note" not in low and "notes" not in low:
        return None
    # Keep note summaries focused on explicit question/summary phrasing.
    # "show/open/list notes" should route to _intent_open_notes (redirect flow).
    is_question = bool(re.search(r"\b(what|which|summarize|summary|tell|how many)\b", low))
    wants_today = "today" in low
    wants_yesterday = "yesterday" in low
    if not (is_question or wants_today or wants_yesterday):
        return None

    day = dt.date.today()
    label = "today"
    if wants_yesterday:
        day = day - dt.timedelta(days=1)
        label = "yesterday"
    day_s = day.isoformat()
    rows = list_recent_notes(limit=1200)
    hit = [r for r in rows if str(r.get("date") or "").startswith(day_s)]
    lines: list[str] = []
    for r in hit[:5]:
        tk = str(r.get("ticker") or "").strip().upper()
        txt = str(r.get("text") or "").strip().replace("\n", " ")
        if not txt:
            continue
        head = f"[{tk}] " if tk else ""
        lines.append(f"- {head}{txt[:120]}")
    daily = get_daily_note(day_s)
    daily_hint = ""
    if str(daily.content or "").strip():
        daily_hint = f"\nDaily log exists for {day_s}."

    msg = f"You have {len(hit)} note(s) for {label} ({day_s})."
    if lines:
        msg += "\n" + "\n".join(lines)
    else:
        msg += "\n- No notes captured for that day yet."
    msg += daily_hint
    msg += "\nIf you want, I can open the full notes file."
    return AICommandResult(
        status="ok",
        intent="notes_summary",
        message=msg,
        confidence=0.93,
        redirect_url="",
        citations=[],
        traces=[],
    )


def _intent_open_sec_filings(query: str, parsed: ParsedCommand | None = None) -> AICommandResult | None:
    q = str(query or "").strip()
    low = _norm(q)
    form = (parsed.form if parsed else "") or ("10-K" if "10k" in low or "10-k" in low else "")
    if not any(k in low for k in ["10-k", "10k", "10-q", "10q", "8-k", "8k", "sec filing", "sec filings"]):
        return None
    tk = (parsed.ticker if parsed else "") or _extract_ticker_for_sec_query(q)
    tk = _safe_ticker(tk)
    if tk and not _is_known_ticker_for_user_scope(tk):
        tk = ""
    if not tk:
        return AICommandResult(
            status="needs_input",
            intent="open_sec_filings",
            message="Ticker missing. Example: find CRM 10-K or 10-Q for ADBE",
            confidence=0.35,
            citations=[],
            traces=[],
        )
    url = f"/company_file/sec?t={tk}"
    if form:
        url += f"&form={form}"
    return AICommandResult(
        status="ok",
        intent="open_sec_filings",
        message=f"Opening {tk} SEC filings" + (f" ({form})" if form else "") + ".",
        confidence=0.94,
        redirect_url=url,
        citations=[],
        traces=[],
    )


def _intent_portfolio_today_status(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    low = _norm(q)
    if not _contains_portfolio_term(low):
        return None
    has_time_anchor = any(k in low for k in ("today", "daily", "day", "now", "current", "right now"))
    asks_perf = _looks_like_portfolio_status_query(low)
    if any(k in low for k in ("history", "recent", "when did", "transactions", "changed")) and not has_time_anchor:
        return None
    if not asks_perf:
        return None

    def _pull() -> tuple[bool, dict]:
        tx = _run_tool("get_live_portfolio_summary", {}, query=q)
        return bool(tx.get("ok")), (tx.get("data") if isinstance(tx.get("data"), dict) else {})

    ok1, s1 = _pull()
    if not ok1:
        return AICommandResult(
            status="error",
            intent="portfolio_today_status",
            message="Could not load live portfolio summary right now.",
            confidence=0.3,
            redirect_url="/my_universe?tab=all",
            citations=[],
            traces=[{"step": "tool", "detail": "get_live_portfolio_summary:error"}],
        )
    ok2, s2 = _pull()
    s = s1
    verified = False
    if ok2 and s2:
        try:
            c1 = int(s1.get("holdings_count") or 0)
            c2 = int(s2.get("holdings_count") or 0)
            p1 = float(s1.get("day_change_pct") or 0.0)
            p2 = float(s2.get("day_change_pct") or 0.0)
            m1 = float(s1.get("day_change_usd") or 0.0)
            m2 = float(s2.get("day_change_usd") or 0.0)
            verified = (c1 == c2) and (abs(p1 - p2) <= 0.25) and (abs(m1 - m2) <= max(200.0, abs(m1) * 0.08))
        except Exception:
            verified = False
        s = s2 if verified else s1
    verify_trace = {"step": "verify", "detail": "pass" if verified else "soft-fail"}
    if int(s.get("holdings_count") or 0) <= 0:
        return AICommandResult(
            status="ok",
            intent="portfolio_today_status",
            message="No portfolio holdings found yet. Add positions in `/my_universe?tab=all` to track daily performance.",
            confidence=0.95,
            redirect_url="/my_universe?tab=all",
            citations=[],
            traces=[{"step": "tool", "detail": "get_live_portfolio_summary"}, verify_trace],
        )

    basis = float(s.get("tracked_basis") or 0.0)
    move = float(s.get("day_change_usd") or 0.0)
    day_pct = float(s.get("day_change_pct") or 0.0)
    best = s.get("best") if isinstance(s.get("best"), dict) else {}
    worst = s.get("worst") if isinstance(s.get("worst"), dict) else {}
    if basis <= 0:
        return AICommandResult(
            status="ok",
            intent="portfolio_today_status",
            message=(
                "I found your portfolio, but today performance data is not available yet for holdings. "
                "Try again after market data refresh."
            ),
            confidence=0.9,
            redirect_url="/dashboard",
            citations=[],
            traces=[{"step": "tool", "detail": "get_live_portfolio_summary"}, verify_trace],
        )

    today_s = dt.date.today().isoformat()
    msg = (
        f"Portfolio today ({today_s}): {day_pct:+.2f}% "
        f"(~${move:,.0f} on ${basis:,.0f} tracked previous-close value).\n"
        f"Best: {str(best.get('ticker') or '-')} {float(best.get('day_pct') or 0.0):+.2f}%.\n"
        f"Worst: {str(worst.get('ticker') or '-')} {float(worst.get('day_pct') or 0.0):+.2f}%."
    )
    return AICommandResult(
        status="ok",
        intent="portfolio_today_status",
        message=msg,
        confidence=0.96,
        redirect_url="/my_universe?tab=all",
        citations=[],
        traces=[{"step": "tool", "detail": "get_live_portfolio_summary"}, verify_trace],
    )


def _intent_portfolio_change_log(query: str) -> AICommandResult | None:
    low = _norm(query)
    asks = (
        ("portfolio" in low or "holdings" in low)
        and any(k in low for k in {"changed", "change", "history", "recent", "transactions", "bought", "sold", "when"})
    )
    if not asks:
        return None
    tr = _run_tool("list_recent_portfolio_transactions", {"limit": 8}, query=str(query or ""))
    data = tr.get("data") if isinstance(tr.get("data"), dict) else {}
    rows = data.get("rows") if isinstance(data.get("rows"), list) else []
    if not tr.get("ok"):
        return AICommandResult(
            status="error",
            intent="portfolio_change_log",
            message="Could not load portfolio transaction history right now.",
            confidence=0.3,
            redirect_url="/my_universe?tab=all",
            citations=[],
            traces=[{"step": "tool", "detail": "list_recent_portfolio_transactions:error"}],
        )
    if not rows:
        return AICommandResult(
            status="ok",
            intent="portfolio_change_log",
            message="No portfolio transaction history recorded yet.",
            confidence=0.9,
            redirect_url="/my_universe?tab=all",
            citations=[],
            traces=[{"step": "tool", "detail": "list_recent_portfolio_transactions"}],
        )
    lines: list[str] = []
    for r in rows[:6]:
        ts = str(r.get("created_at") or "")[:16].replace("T", " ")
        lines.append(
            f"- {ts} {str(r.get('action') or '').upper()} {str(r.get('ticker') or '-')} "
            f"{float(r.get('shares') or 0.0):g} @ {float(r.get('price') or 0.0):.2f}"
        )
    return AICommandResult(
        status="ok",
        intent="portfolio_change_log",
        message="Recent portfolio changes:\n" + "\n".join(lines),
        confidence=0.95,
        redirect_url="/my_universe?tab=all",
        citations=[],
        traces=[{"step": "tool", "detail": "list_recent_portfolio_transactions"}],
    )


def _intent_get_holdings(query: str) -> AICommandResult | None:
    low = _norm(query)
    asks = any(
        k in low
        for k in {
            "what stocks we own",
            "what stocks do we own",
            "what do we own",
            "show holdings",
            "list holdings",
            "current positions",
            "our holdings",
        }
    )
    if not asks:
        return None
    tr = _run_tool("get_holdings", {"limit": 200}, query=str(query or ""))
    data = tr.get("data") if isinstance(tr.get("data"), dict) else {}
    rows = data.get("rows") if isinstance(data.get("rows"), list) else []
    if not tr.get("ok"):
        return AICommandResult(
            status="error",
            intent="get_holdings",
            message="Could not load holdings right now.",
            confidence=0.3,
            redirect_url="/my_universe?tab=all",
            citations=[],
            traces=[{"step": "tool", "detail": "get_holdings:error"}],
        )
    if not rows:
        return AICommandResult(
            status="ok",
            intent="get_holdings",
            message="No holdings found yet.",
            confidence=0.95,
            redirect_url="/my_universe?tab=all",
            citations=[],
            traces=[{"step": "tool", "detail": "get_holdings"}],
        )
    lines = []
    for r in rows[:12]:
        lines.append(f"- {str(r.get('ticker') or '-')} ({float(r.get('shares') or 0.0):g} shares)")
    return AICommandResult(
        status="ok",
        intent="get_holdings",
        message="Current holdings:\n" + "\n".join(lines),
        confidence=0.95,
        redirect_url="/my_universe?tab=all",
        citations=[],
        traces=[{"step": "tool", "detail": "get_holdings"}],
    )


def _intent_position_why(query: str) -> AICommandResult | None:
    low = _norm(query)
    asks_position = any(k in low for k in {"why we own", "why do we own", "position history", "when did we buy", "when did we sell"})
    if not asks_position:
        return None
    tk = _extract_ticker(query)
    if not tk:
        return AICommandResult(
            status="needs_input",
            intent="position_why",
            message="Specify a ticker. Example: `why do we own CRM?`",
            confidence=0.4,
            citations=[],
            traces=[],
        )
    tr = _run_tool("get_position_history", {"ticker": tk, "limit": 20}, query=str(query or ""))
    data = tr.get("data") if isinstance(tr.get("data"), dict) else {}
    rows = data.get("rows") if isinstance(data.get("rows"), list) else []
    if not tr.get("ok"):
        return AICommandResult(
            status="error",
            intent="position_why",
            message=f"Could not load position history for {tk} right now.",
            confidence=0.3,
            redirect_url=f"/company_file?t={tk}",
            citations=[],
            traces=[{"step": "tool", "detail": "get_position_history:error"}],
        )
    if not rows:
        return AICommandResult(
            status="ok",
            intent="position_why",
            message=f"No recorded trade history found for {tk} yet.",
            confidence=0.9,
            redirect_url=f"/company_file?t={tk}",
            citations=[],
            traces=[{"step": "tool", "detail": "get_position_history"}],
        )
    bullets = []
    for r in rows[:6]:
        ts = str(r.get("created_at") or "")[:16].replace("T", " ")
        nt = str(r.get("note") or "").strip()
        more = f" | note: {nt[:80]}" if nt else ""
        bullets.append(
            f"- {ts} {str(r.get('action') or '').upper()} {float(r.get('shares') or 0.0):g} @ {float(r.get('price') or 0.0):.2f}{more}"
        )
    return AICommandResult(
        status="ok",
        intent="position_why",
        message=f"{tk} position timeline:\n" + "\n".join(bullets),
        confidence=0.94,
        redirect_url=f"/company_file?t={tk}",
        citations=[],
        traces=[{"step": "tool", "detail": "get_position_history"}],
    )


def _intent_trade_history_period(query: str) -> AICommandResult | None:
    low = _norm(query)
    asks_trade_log = (
        "trade" in low
        or "transaction" in low
        or "bought" in low
        or "sold" in low
        or "history" in low
    )
    if not asks_trade_log:
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
    explicit_tickers = []
    for m in re.findall(r"\b[A-Z][A-Z0-9.\-]{0,5}\b", str(query or "")):
        up = str(m or "").strip().upper()
        if up and up not in explicit_tickers:
            explicit_tickers.append(up)
    tk = ""
    # Prefer explicit uppercase symbols that actually exist in the requested year.
    for cand in explicit_tickers[:8]:
        if list_portfolio_transactions(limit=1, ticker=cand, year=year):
            tk = cand
            break
    if not tk:
        tk = _extract_ticker(query)
    rows = list_portfolio_transactions(limit=2000, ticker=tk, year=year)
    if tk and not rows and explicit_tickers:
        # Avoid false positives from generic words (e.g., "can" -> CAN).
        tk = ""
        rows = list_portfolio_transactions(limit=2000, ticker="", year=year)
    if len(years) > 1:
        parts: list[str] = ["Trade history summary by year:"]
        for y in years[:8]:
            yr = list_portfolio_transactions(limit=2000, ticker=tk, year=y)
            buys = sum(1 for r in yr if str(r.get("action") or "").lower() in {"buy", "add"})
            sells = sum(1 for r in yr if str(r.get("action") or "").lower() in {"sell", "trim"})
            scope = f" ({tk})" if tk else ""
            parts.append(f"- {y}{scope}: rows={len(yr)}, buys={buys}, sells={sells}")
        return AICommandResult(
            status="ok",
            intent="trade_history_period",
            message="\n".join(parts),
            confidence=0.97,
            redirect_url="/my_universe?tab=all",
            citations=[],
            traces=[{"step": "tool", "detail": "list_portfolio_transactions"}],
        )
    if not rows:
        scope = f" for {tk}" if tk else ""
        return AICommandResult(
            status="ok",
            intent="trade_history_period",
            message=f"No recorded trades found{scope} in {year}.",
            confidence=0.94,
            redirect_url="/my_universe?tab=all",
            citations=[],
            traces=[{"step": "tool", "detail": "list_portfolio_transactions"}],
        )
    counts: dict[str, int] = {}
    for r in rows:
        rt = str(r.get("ticker") or "").strip().upper()
        if not rt:
            continue
        counts[rt] = counts.get(rt, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    scope = f" ({tk})" if tk else ""
    lines = [f"Trade history {year}{scope}: {len(rows)} rows."]
    if top:
        lines.append("Top tickers: " + ", ".join(f"{k} ({v})" for k, v in top))
    lines.append("Recent rows:")
    for r in rows[:10]:
        ts = str(r.get("created_at") or "")[:16].replace("T", " ")
        lines.append(
            f"- {ts} {str(r.get('action') or '').upper()} {str(r.get('ticker') or '-')}"
            f" {float(r.get('shares') or 0.0):g} @ {float(r.get('price') or 0.0):.2f}"
        )
    return AICommandResult(
        status="ok",
        intent="trade_history_period",
        message="\n".join(lines),
        confidence=0.97,
        redirect_url="/my_universe?tab=all",
        citations=[],
        traces=[{"step": "tool", "detail": "list_portfolio_transactions"}],
    )


def _intent_watchlist_why(query: str) -> AICommandResult | None:
    low = _norm(query)
    asks = ("watchlist" in low) and any(k in low for k in {"why", "picked", "pick", "rationale", "thesis"})
    if not asks:
        return None
    tk = _extract_ticker(query)
    if not tk:
        return AICommandResult(
            status="needs_input",
            intent="watchlist_why",
            message="Specify a ticker. Example: `why is HUBS in watchlist?`",
            confidence=0.4,
            citations=[],
            traces=[],
        )
    tr = _run_tool("get_watchlist_rationale", {"ticker": tk, "limit": 6}, query=str(query or ""))
    info = tr.get("data") if isinstance(tr.get("data"), dict) else {}
    if not tr.get("ok"):
        return AICommandResult(
            status="error",
            intent="watchlist_why",
            message=f"Could not load watchlist rationale for {tk} right now.",
            confidence=0.3,
            redirect_url=f"/company_file?t={tk}",
            citations=[],
            traces=[{"step": "tool", "detail": "get_watchlist_rationale:error"}],
        )
    th = info.get("thesis") if isinstance(info.get("thesis"), dict) else {}
    ds = info.get("decisions") if isinstance(info.get("decisions"), list) else []
    lines = [f"Watchlist rationale for {tk}:"]
    thesis = str(th.get("thesis") or "").strip()
    if thesis:
        lines.append(f"- Thesis: {thesis[:220]}")
    pm = str(th.get("pick_method") or "").strip()
    if pm:
        lines.append(f"- Pick method: {pm[:180]}")
    trig = str(th.get("triggers") or "").strip()
    if trig:
        lines.append(f"- Triggers: {trig[:220]}")
    if ds:
        latest = ds[0]
        lines.append(f"- Latest decision: {str(latest.get('action') or '')} at {str(latest.get('created_at') or '')[:16].replace('T',' ')}")
        rs = str(latest.get("reason") or "").strip()
        if rs:
            lines.append(f"- Reason: {rs[:220]}")
    if len(lines) == 1:
        lines.append("- No rationale captured yet. Add a reason when adding to watchlist.")
    return AICommandResult(
        status="ok",
        intent="watchlist_why",
        message="\n".join(lines),
        confidence=0.93,
        redirect_url=f"/company_file?t={tk}",
        citations=[],
        traces=[{"step": "tool", "detail": "get_watchlist_rationale"}],
    )


def _intent_company_compare(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    low = _norm(q)
    if not q:
        return None
    if _looks_like_correction(q):
        return None
    if not re.search(r"\b(or|vs|versus|better)\b", low):
        return None
    # Try explicit ticker extraction first.
    compare_stop = {"OR", "VS", "VERSUS", "BETTER", "THIS", "THAT", "NOT", "NO", "YES", "AND", "IS", "ARE"}
    alias_tickers = {str(v).strip().upper() for v in ENTITY_ALIASES.values() if str(v).strip()}
    caps = re.findall(r"\b[A-Z]{1,6}\b", q.upper())
    picks: list[str] = []
    for tk in caps:
        if tk in compare_stop:
            continue
        s = _safe_ticker(tk)
        if not s:
            continue
        if s not in alias_tickers and not _is_known_ticker_for_user_scope(s):
            continue
        if s not in picks:
            picks.append(s)
        if len(picks) >= 2:
            break
    # Fallback to entity aliases in natural language (e.g., hubspot, salesforce).
    if len(picks) < 2:
        for name, tk in ENTITY_ALIASES.items():
            if re.search(rf"\b{re.escape(name)}\b", low) and tk not in picks:
                picks.append(tk)
            if len(picks) >= 2:
                break
    if len(picks) < 2:
        return None
    a, b = picks[0], picks[1]
    msg = (
        f"Good question. To compare {a} vs {b} properly for your style, I need 3 quick inputs:\n"
        "- Priority: growth, profitability, moat durability, or valuation?\n"
        "- Time horizon: 6-12 months or 3-5 years?\n"
        "- Risk preference for this decision: low, medium, or high?\n\n"
        "Reply in one line like: `priority=moat, horizon=3-5y, risk=low`."
    )
    return AICommandResult(
        status="needs_input",
        intent="company_compare",
        message=msg,
        confidence=0.9,
        redirect_url="",
        citations=[],
        traces=[{"step": "route", "detail": "compare_prompt"}],
    )


def _intent_save_thesis(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    low = _norm(q)
    if "thesis" not in low:
        return None
    if not any(k in low for k in {"save", "set", "update"}):
        return None
    tk = _extract_ticker(q)
    if not tk:
        return AICommandResult(
            status="needs_input",
            intent="save_thesis",
            message="Specify a ticker. Example: save thesis for CRM: sticky enterprise moat conviction 8",
            confidence=0.4,
            citations=[],
            traces=[],
        )
    conv = 0
    m_conv = re.search(r"\bconviction\s*(\d{1,2})\b", q, flags=re.I)
    if m_conv:
        conv = max(0, min(10, int(m_conv.group(1))))
    m = re.search(r":\s*(.+)$", q)
    thesis = str(m.group(1) if m else "").strip()
    if not thesis:
        thesis = re.sub(r"^.*\bthesis\b", "", q, flags=re.I).strip(" :-")
    if not thesis:
        return AICommandResult(
            status="needs_input",
            intent="save_thesis",
            message="Thesis text is missing.",
            confidence=0.4,
            citations=[],
            traces=[],
        )
    strategy_tag = "CORE"
    up = _norm(q).upper()
    if "LEGACY" in up:
        strategy_tag = "LEGACY"
    elif "TRADE" in up:
        strategy_tag = "TRADE"
    elif "LONG TERM" in up or "LONG_TERM" in up:
        strategy_tag = "LONG_TERM"
    tr = _run_tool(
        "save_thesis",
        {"ticker": tk, "thesis": thesis, "conviction": conv, "time_horizon": strategy_tag, "invalidation_criteria": ""},
        query=q,
    )
    if not tr.get("ok"):
        return AICommandResult(
            status="error",
            intent="save_thesis",
            message=f"Could not save thesis for {tk}.",
            confidence=0.3,
            citations=[],
            traces=[{"step": "tool", "detail": "save_thesis:error"}],
        )
    return AICommandResult(
        status="ok",
        intent="save_thesis",
        message=f"Saved thesis for {tk}" + (f" (conviction {conv}/10)." if conv else ".") + f" Strategy: {strategy_tag}.",
        confidence=0.95,
        redirect_url=f"/company_file?t={tk}",
        citations=[],
        traces=[{"step": "tool", "detail": "save_thesis"}],
    )


def _intent_backfill_trade_history(query: str) -> AICommandResult | None:
    q = str(query or "").strip()
    low = _norm(q)
    if not any(k in low for k in {"i bought", "i sold", "we bought", "we sold", "bought", "sold"}):
        return None
    tk = _extract_ticker(q)
    if not tk:
        return None
    action = "sell" if re.search(r"\b(sold|sell)\b", low) else "buy"
    m_year = re.search(r"\b(20\d{2}|19\d{2})\b", q)
    approx_date = f"{m_year.group(1)}-01-01" if m_year else ""
    m_price = re.search(r"\$?\s*(\d+(?:\.\d+)?)", q)
    approx_price = float(m_price.group(1)) if m_price else 0.0
    reason = ""
    m_reason = re.search(r"\b(?:because|for|thesis)\b[:\s-]*(.+)$", q, flags=re.I)
    if m_reason:
        reason = str(m_reason.group(1) or "").strip()
    tr = _run_tool(
        "backfill_trade_history",
        {
            "ticker": tk,
            "approx_date": approx_date,
            "approx_price": approx_price,
            "reason": reason or "Interview/manual backfill",
            "action": action,
            "quantity": 1.0,
        },
        query=q,
    )
    if not tr.get("ok"):
        return AICommandResult(
            status="error",
            intent="backfill_trade_history",
            message=f"Could not backfill trade history for {tk}.",
            confidence=0.3,
            citations=[],
            traces=[{"step": "tool", "detail": "backfill_trade_history:error"}],
        )
    return AICommandResult(
        status="ok",
        intent="backfill_trade_history",
        message=f"Backfilled {action.upper()} history for {tk}" + (f" at ~${approx_price:.2f}" if approx_price > 0 else "") + ".",
        confidence=0.92,
        redirect_url=f"/company_file?t={tk}",
        citations=[],
        traces=[{"step": "tool", "detail": "backfill_trade_history"}],
    )


def _interview_reflect(question: str, answer: str, next_question: str) -> str:
    """Generate a natural, conversational acknowledgment after an interview answer."""
    try:
        if ask_ai is None:
            raise RuntimeError("ask_ai unavailable")
        is_short = len(answer.strip().split()) < 20
        skip_words = {"skipped by user", "skip", "pass", "idk"}
        is_skipped = answer.strip().lower() in skip_words
        if is_skipped:
            # Skipped — just move on warmly
            if next_question:
                return f"No problem, we can come back to that. Let's keep going.\n\n{next_question}"
            return "No problem, we can come back to that."
        probe_instruction = (
            "Ask ONE short probing follow-up to get a more complete answer. Do NOT show the next question yet."
            if is_short
            else f"Confirm understanding in 2 sentences, then naturally introduce the next question: '{next_question}'"
        )
        prompt = (
            f"You are an intelligent investment advisor conducting a structured portfolio interview.\n\n"
            f"INTERVIEW QUESTION: {question}\n\n"
            f"USER'S ANSWER: {answer}\n\n"
            f"Your task: {probe_instruction}\n\n"
            f"Rules:\n"
            f"- Be warm and conversational, like Claude or ChatGPT\n"
            f"- Reflect back the key point(s) you understood (max 1-2 sentences)\n"
            f"- Never be robotic or say 'Saved.' or 'Noted.'\n"
            f"- Keep total response under 80 words\n"
            f"- If you probe, ask only ONE follow-up question"
        )
        reflection = ask_ai(prompt, context="", mode="fast").strip()
        if reflection and len(reflection) > 10:
            if not is_short and next_question and next_question not in reflection:
                return reflection + f"\n\n{next_question}"
            return reflection
    except Exception:
        pass
    # Fallback: plain next question
    return next_question


def _intent_portfolio_interview(query: str, context: dict | None = None) -> AICommandResult | None:
    q = str(query or "").strip()
    low = _norm(q)
    # Start mode
    if any(k in low for k in {"start portfolio interview", "interview mode", "start interview"}):
        sid = str((context or {}).get("session_id") or "").strip()
        tr = _run_tool("start_portfolio_interview", {"session_id": sid}, query=q)
        data = tr.get("data") if isinstance(tr.get("data"), dict) else {}
        if not tr.get("ok"):
            return AICommandResult(
                status="error",
                intent="portfolio_interview",
                message="Could not start portfolio interview.",
                confidence=0.3,
                citations=[],
                traces=[{"step": "tool", "detail": "start_portfolio_interview:error"}],
            )
        nxt = data.get("next") if isinstance(data.get("next"), dict) else {}
        if not nxt:
            return AICommandResult(
                status="ok",
                intent="portfolio_interview",
                message="Interview queue is empty. All holdings already have thesis memory.",
                confidence=0.95,
                redirect_url="/my_universe?tab=all",
                citations=[],
                traces=[{"step": "tool", "detail": "start_portfolio_interview"}],
            )
        tk = str(nxt.get("ticker") or "").strip().upper()
        qu = str(nxt.get("question") or "").strip()
        return AICommandResult(
            status="needs_input",
            intent="portfolio_interview",
            message=qu,
            confidence=0.92,
            redirect_url="/my_universe?tab=all",
            citations=[],
            traces=[{"step": "tool", "detail": "start_portfolio_interview"}],
        )
    # Continue mode based on last intent; keep interview sticky until answered.
    ctx = context or {}
    last_intent = str(ctx.get("last_intent") or "").strip()
    if last_intent == "portfolio_interview":
        if _looks_like_fresh_command(q):
            cur = get_pending_interview_question()
            nxt = cur.get("next") if isinstance(cur, dict) and isinstance(cur.get("next"), dict) else {}
            if not nxt:
                return AICommandResult(
                    status="ok",
                    intent="portfolio_interview",
                    message="Interview complete for current queue.",
                    confidence=0.95,
                    redirect_url="/my_universe?tab=all",
                    citations=[],
                    traces=[{"step": "tool", "detail": "pending_interview:none"}],
                )
            tk = str(nxt.get("ticker") or "").strip().upper()
            qu = str(nxt.get("question") or "").strip()
            return AICommandResult(
                status="needs_input",
                intent="portfolio_interview",
                message=qu,
                confidence=0.94,
                redirect_url="/my_universe?tab=all",
                citations=[],
                traces=[{"step": "tool", "detail": "pending_interview:repeat"}],
            )
        ans = q
        if _norm(q) in {"skip", "pass", "next", "idk", "dont know", "don't know"}:
            ans = "SKIPPED by user."
        # Capture current question before advancing so reflection can reference it
        current_q_text = ""
        try:
            cur_state = get_pending_interview_question()
            cur_nxt = cur_state.get("next") if isinstance(cur_state, dict) and isinstance(cur_state.get("next"), dict) else {}
            current_q_text = str(cur_nxt.get("question") or "").strip()
        except Exception:
            pass
        tr = _run_tool("submit_portfolio_interview_answer", {"answer": ans}, query=q)
        data = tr.get("data") if isinstance(tr.get("data"), dict) else {}
        if not tr.get("ok"):
            return AICommandResult(
                status="error",
                intent="portfolio_interview",
                message="Could not save interview answer.",
                confidence=0.3,
                citations=[],
                traces=[{"step": "tool", "detail": "submit_portfolio_interview_answer:error"}],
            )
        nxt = data.get("next") if isinstance(data.get("next"), dict) else {}
        if not nxt:
            return AICommandResult(
                status="ok",
                intent="portfolio_interview",
                message="That covers everything I needed. Your investment profile is saved — you can view it in My Universe.",
                confidence=0.95,
                redirect_url="/my_universe?tab=all",
                citations=[],
                traces=[{"step": "tool", "detail": "submit_portfolio_interview_answer"}],
            )
        tk = str(nxt.get("ticker") or "").strip().upper()
        qu = str(nxt.get("question") or "").strip()
        reflection = _interview_reflect(current_q_text, ans, qu)
        return AICommandResult(
            status="needs_input",
            intent="portfolio_interview",
            message=reflection,
            confidence=0.92,
            redirect_url="/my_universe?tab=all",
            citations=[],
            traces=[{"step": "tool", "detail": "submit_portfolio_interview_answer"}],
        )
    return None


def _extract_query_tickers_for_tools(query: str, context: dict | None = None, max_items: int = 4) -> list[str]:
    q = str(query or "")
    out: list[str] = []
    for m in re.findall(r"\b[A-Z][A-Z0-9.\-]{0,5}\b", q):
        tk = str(m or "").strip().upper()
        if tk and tk not in out:
            out.append(tk)
        if len(out) >= max_items:
            return out
    ctx = context or {}
    rt = ctx.get("runtime") if isinstance(ctx.get("runtime"), dict) else {}
    cc = rt.get("company_context") if isinstance(rt.get("company_context"), dict) else {}
    tk = str(cc.get("ticker") or "").strip().upper()
    if tk and tk not in out:
        out.append(tk)
    return out[:max_items]


def _wants_financial_tooling(query: str) -> bool:
    low = _norm(query)
    keys = {
        "revenue", "margin", "gross", "debt", "cash flow", "fcf", "cagr",
        "balance sheet", "income statement", "valuation", "equity",
        "compare", "comparison", "peer", "trend", "historical", "5 year", "5y",
    }
    return any(k in low for k in keys)


def _execute_financial_tool_call(name: str, args: dict[str, object], context: dict | None = None) -> dict[str, object]:
    if str(name or "").strip() != "fetch_historical_financials":
        return {"ok": False, "error": "unknown_tool", "name": str(name or "")}
    t = str((args or {}).get("ticker") or "").strip().upper()
    metric = str((args or {}).get("metric") or "all").strip().lower()
    years_raw = (args or {}).get("years")
    refresh_raw = (args or {}).get("refresh")
    if not t:
        cands = _extract_query_tickers_for_tools("", context=context, max_items=1)
        t = str(cands[0] or "").strip().upper() if cands else ""
    try:
        years = int(years_raw) if years_raw is not None else 5
    except Exception:
        years = 5
    refresh = bool(refresh_raw) if isinstance(refresh_raw, bool) else (str(refresh_raw or "").strip().lower() in {"1", "true", "yes", "on"})
    return fetch_historical_financials(ticker=t, metric=metric or "all", years=years, refresh=refresh)


def _ask_llm_fallback(query: str, context: dict | None = None) -> AICommandResult:
    q = str(query or "").strip()
    ctx = context or {}
    hist = ctx.get("history") if isinstance(ctx.get("history"), list) else []
    prefs = str(ctx.get("user_prefs") or "").strip()
    user_profile = str(ctx.get("user_profile") or "").strip()
    compact_mem = summarize_compact_memory_for_prompt(query=q, limit=14)
    semantic_mem = _semantic_memory_lines(q, limit=8)
    active_rules = summarize_active_rules_for_prompt(query=q, limit=10)
    runtime = ctx.get("runtime") if isinstance(ctx.get("runtime"), dict) else {}
    runtime_txt = ""
    if runtime:
        page = runtime.get("page_hint") if isinstance(runtime.get("page_hint"), dict) else {}
        live = runtime.get("portfolio_live") if isinstance(runtime.get("portfolio_live"), dict) else {}
        evs = runtime.get("watcher_events") if isinstance(runtime.get("watcher_events"), list) else []
        txs = runtime.get("recent_transactions") if isinstance(runtime.get("recent_transactions"), list) else []
        rfacts = runtime.get("report_facts") if isinstance(runtime.get("report_facts"), list) else []
        company_ctx = runtime.get("company_context") if isinstance(runtime.get("company_context"), dict) else {}
        page_line = f"{str(page.get('title') or '-')}: {str(page.get('content') or '-')[:220]}"
        live_line = (
            f"holdings={int(live.get('holdings_count') or 0)}, "
            f"day={float(live.get('day_change_pct') or 0.0):+.2f}%, "
            f"move_usd={float(live.get('day_change_usd') or 0.0):+.0f}"
        )
        ev_lines = "\n".join(f"- {str(x or '')[:200]}" for x in evs[:6] if str(x or "").strip())
        tx_lines = []
        for x in txs[:8]:
            if not isinstance(x, dict):
                continue
            ts = str(x.get("created_at") or "")[:16]
            tk = str(x.get("ticker") or "").strip().upper()
            ac = str(x.get("action") or "").strip().upper()
            sh = float(x.get("shares") or 0.0)
            px = float(x.get("price") or 0.0)
            if not tk:
                continue
            tx_lines.append(f"- {ts} {ac} {tk} {sh:g} @ {px:.2f}")
        fact_lines = []
        for f in rfacts[:8]:
            if not isinstance(f, dict):
                continue
            tk = str(f.get("ticker") or "").strip().upper()
            tx = str(f.get("fact_text") or "").strip()
            if not tx:
                continue
            fact_lines.append(f"- {tk + ': ' if tk else ''}{tx[:180]}")
        runtime_txt = (
            f"Runtime context:\n- page={page_line}\n- portfolio={live_line}\n{ev_lines}\n"
            + ("Recent transactions:\n" + "\n".join(tx_lines) + "\n" if tx_lines else "")
            + ("Report facts:\n" + "\n".join(fact_lines) if fact_lines else "")
        )
        if company_ctx:
            cc_ticker = str(company_ctx.get("ticker") or "").strip().upper()
            val = company_ctx.get("valuation") if isinstance(company_ctx.get("valuation"), dict) else {}
            fin = company_ctx.get("financials_5y") if isinstance(company_ctx.get("financials_5y"), dict) else {}
            yrs = list(fin.get("years") or [])
            runtime_txt += (
                "\nCompany page context:\n"
                f"- ticker={cc_ticker or '-'}\n"
                f"- valuation: ytd={val.get('ytd_return')} m12={val.get('m12_return')} y5={val.get('y5_return')}\n"
                f"- financial_years={yrs}\n"
            )
    hits = retrieve_app_knowledge(q, limit=10)
    knowledge_ctx = format_app_knowledge(hits)
    hist_lines: list[str] = []
    for h in hist[-36:]:
        if not isinstance(h, dict):
            continue
        role = str(h.get("role") or "").strip().lower()
        txt = str(h.get("text") or "").strip()
        if role not in {"user", "assistant"} or not txt:
            continue
        hist_lines.append(f"{role.upper()}: {txt[:300]}")
    hist_block = "\n".join(hist_lines) if hist_lines else "(none)"
    if ask_ai is not None:
        try:
            system_contract = (
                "You are Investor OS Senior Partner: analytical, conversational, concise.\n"
                "Primary mode: answer the user's question directly in natural language.\n"
                "Intent discipline:\n"
                "- If the user asks for information/comparison/explanation, do NOT redirect to a page by default.\n"
                "- Only suggest navigation when user explicitly asks to open/go/navigate.\n"
                "- If user intent is ambiguous, ask one short clarifying question.\n"
                "Capability rules:\n"
                "- You CAN show earnings calendar rows and report sections directly in chat when asked.\n"
                "- Do NOT say you cannot display these in chat.\n"
                "- Never output placeholder/demo tables (e.g., [Date], [Company], [TKR], [EPS Value]).\n"
                "- If live data is missing, say it is missing and ask to refresh; never fabricate rows.\n"
                "Execution honesty:\n"
                "- Never claim an action was completed unless tool/runtime evidence in context confirms it.\n"
                "- If data is stale/missing, say so briefly and state what is needed.\n"
                "Data policy:\n"
                "- If a request needs SEC quantitative metrics and structured_financial_data_json is missing, respond exactly: Data not available in structured filings.\n"
                "- Do not extract/calculate new numbers from raw SEC text.\n"
                "Reasoning discipline:\n"
                "- Think internally before answering, but do not reveal private reasoning.\n"
                "- If citing numbers, re-check them against runtime context before final output.\n"
                "Output style:\n"
                "- Plain, human language. No raw JSON unless explicitly requested.\n"
                "- Prefer short paragraphs or compact bullets.\n"
            )
            prompt = (
                f"{system_contract}\n"
                "Answering priority:\n"
                "1) Runtime context and fresh data\n"
                "2) App knowledge\n"
                "3) Memory/preferences\n"
                "4) Ask one clarifying question if still uncertain\n\n"
                "Policy marker: structured_financial_data_json\n"
                "Fallback marker: Data not available in structured filings.\n\n"
                "USER PROFILE:\n"
                f"{user_profile or '(none)'}\n\n"
                "USER PREFERENCES:\n"
                f"{prefs or '(none)'}\n\n"
                "COMPACT MEMORY:\n"
                f"{compact_mem or '(none)'}\n\n"
                "SEMANTIC MEMORY RECALL:\n"
                + ("\n".join(semantic_mem) if semantic_mem else "(none)")
                + "\n\n"
                "ACTIVE RULES:\n"
                f"{active_rules or '(none)'}\n\n"
                "RECENT CHAT HISTORY:\n"
                f"{hist_block}\n\n"
                "RUNTIME CONTEXT:\n"
                f"{runtime_txt or '(none)'}\n\n"
                "APP KNOWLEDGE:\n"
                f"{knowledge_ctx}\n\n"
                "USER QUERY:\n"
                f"{q}"
            )
            sys_ctx = "Senior partner copilot for Investor OS. Conversational first, action-honest, evidence-first."
            out = ""
            if ask_ai_with_tools is not None and _wants_financial_tooling(q):
                tools = [
                    {
                        "name": "fetch_historical_financials",
                        "description": "Fetch normalized 5Y financial statements for a ticker. Supports metric-specific or full financial snapshot.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "ticker": {"type": "string", "description": "Ticker symbol, e.g. AAPL"},
                                "metric": {
                                    "type": "string",
                                    "description": "One of: all,revenue,gross_profit,operating_cash_flow,capex,free_cash_flow,total_cash,total_debt,total_equity",
                                },
                                "years": {"type": "integer", "description": "Number of years to fetch (1-10)"},
                                "refresh": {"type": "boolean", "description": "Refresh from provider before returning data"},
                            },
                            "required": ["ticker"],
                        },
                    }
                ]
                first = ask_ai_with_tools(
                    prompt,
                    sys_ctx,
                    tools=tools,
                    mode="smart",
                    temperature=0.1,
                ) or {}
                calls = list(first.get("function_calls") or [])
                if calls:
                    tool_lines: list[str] = []
                    for fc in calls[:4]:
                        nm = str(fc.get("name") or "")
                        args = fc.get("args") if isinstance(fc.get("args"), dict) else {}
                        tool_res = _execute_financial_tool_call(nm, args, context=ctx)
                        tool_lines.append(
                            f"{nm}({json.dumps(args, ensure_ascii=True)}) => {json.dumps(tool_res, ensure_ascii=True)[:3000]}"
                        )
                    follow = (
                        prompt
                        + "\n\nTOOL RESULTS (trusted runtime data):\n"
                        + "\n".join(tool_lines)
                        + "\n\nUse the tool results above in your final answer."
                    )
                    out = str(
                        ask_ai(
                            follow,
                            sys_ctx,
                            mode="smart",
                            temperature=0.1,
                        )
                        or ""
                    ).strip()
                else:
                    out = str(first.get("text") or "").strip()
            if not out:
                out = str(
                    ask_ai(
                        prompt,
                        sys_ctx,
                        mode="smart",
                        temperature=0.1,
                    )
                    or ""
                ).strip()
            out = re.sub(r"<thought>[\s\S]*?</thought>", "", out, flags=re.I).strip()
            out = re.sub(r"^\s*```thought[\s\S]*?```\s*", "", out, flags=re.I).strip()
            if out:
                return AICommandResult(
                    status="ok",
                    intent="llm_fallback",
                    message=out,
                    confidence=0.62,
                    citations=[],
                    matched_by="llm_fallback",
                    version=ORCHESTRATOR_VERSION,
                    traces=[],
                )
        except Exception as exc:
            logger.warning("LLM app-knowledge fallback failed: %s", exc)
    try:
        with cf.ThreadPoolExecutor(max_workers=1) as ex:
            merged = q
            if hist_lines:
                merged += "\n\nRecent chat:\n" + hist_block
            if prefs:
                merged += "\n\nUser preference:\n" + prefs
            if runtime_txt:
                merged += "\n\n" + runtime_txt
            fut = ex.submit(ask_agent, merged, 6)
            out = str(fut.result(timeout=LLM_FALLBACK_TIMEOUT_SEC) or "").strip()
    except cf.TimeoutError:
        return AICommandResult(
            status="timeout",
            intent="unknown",
            message=(
                "AI fallback timed out. Try a direct command like "
                "`summarize latest report` or `find CRM 10-K`."
            ),
            confidence=0.2,
            citations=[],
            matched_by="none",
            version=ORCHESTRATOR_VERSION,
            traces=[],
        )
    except Exception as exc:
        logger.warning("LLM fallback failed: %s", exc)
        follow_q = _build_reasoning_followup_question(q, ctx)
        return AICommandResult(
            status="needs_input",
            intent="unknown",
            message=(
                "I need one quick input to reason correctly.\n"
                + follow_q
            ),
            confidence=0.25,
            citations=[],
            matched_by="none",
            version=ORCHESTRATOR_VERSION,
            traces=[],
        )
    if not out:
        follow_q = _build_reasoning_followup_question(q, ctx)
        return AICommandResult(
            status="needs_input",
            intent="unknown",
            message=(
                "I need one quick input to reason correctly.\n"
                + follow_q
            ),
            confidence=0.25,
            citations=[],
            matched_by="none",
            version=ORCHESTRATOR_VERSION,
            traces=[],
        )
    return AICommandResult(
        status="ok",
        intent="llm_fallback",
        message=out,
        confidence=0.55,
        citations=[],
        matched_by="llm_fallback",
        version=ORCHESTRATOR_VERSION,
        traces=[],
    )


def _worker_registry() -> list[tuple[str, object]]:
    return [
        ("guard_worker", _intent_create_company_guard),
        ("memory_worker", _intent_memory_remember),
        ("memory_worker", _intent_memory_forget),
        ("memory_worker", _intent_memory_show),
        ("memory_worker", _intent_learning_status),
        ("platform_worker", _intent_app_capabilities),
        ("research_worker", _intent_morning_updates),
        ("universe_worker", _intent_list_watchlist),
        ("portfolio_worker", _intent_list_portfolio),
        ("research_worker", _intent_list_earnings),
        ("organizer_worker", _intent_list_tasks),
        ("organizer_worker", _intent_list_notes),
        ("research_worker", _intent_list_reports),
        ("research_worker", _intent_report_section),
        ("platform_worker", _intent_app_map_route),
        ("portfolio_worker", _intent_get_holdings),
        ("portfolio_worker", _intent_portfolio_change_log),
        ("portfolio_worker", _intent_trade_history_period),
        ("portfolio_worker", _intent_position_why),
        ("universe_worker", _intent_watchlist_why),
        ("research_worker", _intent_company_compare),
        ("research_worker", _intent_save_thesis),
        ("research_worker", _intent_notes_summary),
        ("organizer_worker", _intent_open_notes),
        ("guard_worker", _intent_high_risk_queue),
        ("platform_worker", _intent_open_page),
        ("research_worker", _intent_open_company),
        ("research_worker", _intent_search_reports),
    ]


def _runtime_trace_entries() -> list[dict[str, str]]:
    rt = _TURN_RUNTIME.get() or {}
    out: list[dict[str, str]] = []
    trace_id = str(rt.get("trace_id") or "").strip()
    if trace_id:
        out.append({"step": "trace.id", "detail": trace_id})
    out.append({"step": "budget.calls", "detail": f"{int(rt.get('tool_calls') or 0)}/{int(rt.get('max_tool_calls') or 0)}"})
    out.append({"step": "budget.ms", "detail": f"{float(rt.get('tool_ms') or 0.0):.1f}/{float(rt.get('max_tool_ms') or 0.0):.1f}"})
    out.append({"step": "budget.denied", "detail": str(int(rt.get("denied") or 0))})
    tools = rt.get("tools") if isinstance(rt.get("tools"), list) else []
    for t in tools[-6:]:
        if not isinstance(t, dict):
            continue
        out.append(
            {
                "step": "tool.step",
                "detail": (
                    f"{str(t.get('tool') or '')}:{str(t.get('status') or '')}"
                    f" {float(t.get('duration_ms') or 0.0):.1f}ms"
                ),
            }
        )
    return out


def run_ai_command(query: str, context: dict | None = None) -> AICommandResult:
    t0 = time.perf_counter()
    q_raw = str(query or "").strip()
    q = q_raw
    if not q:
        res = AICommandResult(
            status="needs_input",
            intent="none",
            message="Please enter a command.",
            confidence=0.0,
            citations=[],
            traces=[{"step": "input", "detail": "empty"}],
        )
        log_system_event(
            service="AICommand",
            status=res.status,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            message=f"{res.intent}: {res.message[:180]}",
            trace_id="",
        )
        _log_action(query, res)
        return res

    trace_id = "tr_" + uuid.uuid4().hex[:12]
    _TURN_RUNTIME.set(
        {
            "trace_id": trace_id,
            "max_tool_calls": max(4, min(40, int(app_env("AI_MAX_TOOL_CALLS_PER_TURN", "14") or 14))),
            "max_tool_ms": max(500.0, min(30000.0, float(app_env("AI_MAX_TOOL_MS_PER_TURN", "8000") or 8000.0))),
            "tool_calls": 0,
            "tool_ms": 0.0,
            "denied": 0,
            "tools": [],
        }
    )

    ctx = context or {}
    understanding = _understand_user_turn(q_raw, ctx)
    if understanding.intent_class == "correction" and last_intent != "portfolio_interview":
        _learn_from_correction(q_raw)

    pending = str(ctx.get("pending_query") or "").strip()
    last_intent = str(ctx.get("last_intent") or "").strip()
    interrupt_intent, interrupt_conf, interrupt_by = _manager_detect_interrupt(q_raw, ctx)
    # Conversation-first: normal questions should not be trapped by yes/no worker follow-ups.
    skip_worker_handlers = bool(interrupt_intent and interrupt_intent != "portfolio_interview")
    if (
        not skip_worker_handlers
        and understanding.intent_class == "question"
        and not _is_confirmation_text(q_raw)
        and str(last_intent or "").strip() in WORKER_INTENTS
    ):
        skip_worker_handlers = True
    if skip_worker_handlers:
        q = _manager_rewrite_query(interrupt_intent, q_raw)
    elif pending and last_intent not in {"portfolio_interview", "list_watchlist", "list_blue_chips", "list_portfolio", "list_tasks", "list_notes", "list_reports"} and not _looks_like_fresh_command(q_raw) and len(q_raw.split()) <= 24:
        q = pending + "\nClarification: " + q_raw
    elif str(ctx.get("current_report_name") or "").strip() and last_intent in {"summarize_current_report", "summarize_latest_report"} and not _looks_like_fresh_command(q_raw):
        # Continue report discussion naturally with follow-up prompts.
        q = "explain this report follow-up: " + q_raw

    parsed = _strict_finance_parse(q)
    decision = _decide_turn_contract(q_raw, understanding, parsed, ctx)
    adaptive_policy = get_adaptive_turn_policy_snapshot()
    th = adaptive_policy.get("thresholds") if isinstance(adaptive_policy.get("thresholds"), dict) else {}
    force_action_min_conf = float(th.get("force_action_min_conf") or 0.45)
    decision_action_min_conf = float(th.get("decision_action_min_conf") or 0.68)
    clarify_min_conf = float(th.get("clarify_min_conf") or 0.48)
    mutation_min_conf = float(th.get("mutation_min_conf") or 0.55)
    candidates = _generate_intent_candidates(q_raw, parsed, context=ctx)
    arb_action, arb_conf, arb_by = _arbitrate_action_candidate(
        candidates,
        parsed,
        understanding,
        decision_action_min_conf=decision_action_min_conf,
        mutation_min_conf=mutation_min_conf,
    )
    effective_parsed = ParsedCommand(
        action=arb_action or parsed.action,
        ticker=(parsed.ticker or (candidates[0].ticker if candidates else "")),
        form=parsed.form,
        due_date=(parsed.due_date or (candidates[0].due_date if candidates else "")),
        body=parsed.body,
        notes=parsed.notes,
    )
    traces = [
        {"step": "trace.id", "detail": trace_id},
        {"step": "supervisor", "detail": "on"},
        {"step": "understand.intent", "detail": understanding.intent_class or "-"},
        {"step": "understand.topic", "detail": understanding.topic or "-"},
        {"step": "understand.conf", "detail": f"{float(understanding.confidence or 0.0):.2f}"},
        {"step": "decision.type", "detail": decision.intent_type},
        {"step": "decision.conf", "detail": f"{float(decision.confidence or 0.0):.2f}"},
        {"step": "decision.clarify", "detail": "1" if decision.needs_clarification else "0"},
        {"step": "decision.action", "detail": decision.proposed_action or "-"},
        {"step": "parse.action", "detail": parsed.action or "-"},
        {"step": "parse.ticker", "detail": parsed.ticker or "-"},
        {"step": "parse.form", "detail": parsed.form or "-"},
        {"step": "parse.due", "detail": parsed.due_date or "-"},
        {"step": "cand.top1", "detail": (f"{candidates[0].intent}:{float(candidates[0].confidence):.2f}" if candidates else "-")},
        {"step": "arb.action", "detail": effective_parsed.action or "-"},
        {"step": "arb.conf", "detail": f"{arb_conf:.2f}" if arb_conf else "-"},
        {"step": "arb.by", "detail": arb_by or "-"},
        {"step": "policy.force_action_min_conf", "detail": f"{force_action_min_conf:.2f}"},
        {"step": "policy.decision_action_min_conf", "detail": f"{decision_action_min_conf:.2f}"},
        {"step": "policy.clarify_min_conf", "detail": f"{clarify_min_conf:.2f}"},
        {"step": "policy.mutation_min_conf", "detail": f"{mutation_min_conf:.2f}"},
    ]

    traces.append({"step": "ctx.report", "detail": str(ctx.get("current_report_name") or "-")})
    traces.append({"step": "ctx.pending", "detail": "1" if pending else "0"})
    traces.append({"step": "ctx.last_intent", "detail": last_intent or "-"})
    traces.append(
        {
            "step": "manager.interrupt",
            "detail": (f"{interrupt_intent} ({interrupt_by}:{interrupt_conf:.2f})" if skip_worker_handlers else "-"),
        }
    )

    if understanding.intent_class == "correction" and not parsed.action and last_intent != "portfolio_interview":
        follow_q = "Understood. What should I do instead in one line?"
        res = AICommandResult(
            status="needs_input",
            intent="correction_ack",
            message=follow_q,
            confidence=max(0.7, float(understanding.confidence or 0.0)),
            citations=[],
            matched_by="understanding_layer",
            version=ORCHESTRATOR_VERSION,
            traces=traces + [{"step": "route", "detail": "correction_ack"}] + _runtime_trace_entries(),
        )
        log_system_event(
            service="AICommand",
            status=res.status,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            message=f"{res.intent}: {res.message[:180]}",
            trace_id=trace_id,
        )
        _log_action(query, res)
        return res

    # Two-lane runtime: conversation is default.
    # Route to chat lane unless action intent is clear and confident.
    low_raw = _norm(q_raw)
    force_action_signal = bool(
        effective_parsed.action
        or (re.search(r"\b(add|remove|delete)\b", low_raw) and _contains_blue_chip_term(low_raw))
        or (re.search(r"\b(add|remove|delete)\b", low_raw) and _contains_watchlist_term(low_raw))
        or (re.search(r"\b(add|remove|delete|buy|sell|shares)\b", low_raw) and _contains_portfolio_term(low_raw))
        or bool(re.search(r"\b(or|vs|versus|better)\b", low_raw) and any(k in low_raw for k in ENTITY_ALIASES.keys()))
        or bool(re.search(r"\b[A-Z]{2,6}\s+(or|vs|versus)\s+[A-Z]{2,6}\b", q_raw, flags=re.I))
    )
    action_lane = bool(
        (force_action_signal and float(understanding.confidence or 0.0) >= force_action_min_conf)
        or (
            decision.intent_type in {"action", "mixed"}
            and float(decision.confidence or 0.0) >= decision_action_min_conf
            and (
                understanding.intent_class == "confirmation"
                or force_action_signal
                or bool(
                    re.search(
                        r"\b(add|remove|delete|open|navigate|go to|take me|create|save|update|set|sync)\b",
                        _norm(q_raw),
                    )
                )
            )
        )
    )
    if decision.needs_clarification and not action_lane:
        follow_q = understanding.clarifying_question or _build_reasoning_followup_question(q_raw, ctx)
        res = AICommandResult(
            status="needs_clarification",
            intent="unknown",
            message=follow_q,
            confidence=float(decision.confidence or understanding.confidence or 0.0),
            citations=[],
            matched_by="decision_contract",
            version=ORCHESTRATOR_VERSION,
            traces=traces + [{"step": "route", "detail": "decision_contract_clarify"}] + _runtime_trace_entries(),
        )
        log_system_event(
            service="AICommand",
            status=res.status,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            message=f"{res.intent}: {res.message[:180]}",
            trace_id=trace_id,
        )
        _log_quality_event(
            "needs_clarification",
            query,
            {"source": "decision_contract", "confidence": float(decision.confidence or 0.0), "topic": understanding.topic},
        )
        _log_action(query, res)
        return res
    if not action_lane:
        # Data-first override in chat lane: route trade-history queries to DB-backed handlers.
        hist_res = _intent_trade_history_period(q_raw) or _intent_position_why(q_raw) or _intent_portfolio_change_log(q_raw)
        if hist_res is not None:
            hist_res = _apply_confidence_policy(hist_res)
            hist_res.traces = traces + list(hist_res.traces or []) + [{"step": "route", "detail": "chat_lane_data_override"}] + _runtime_trace_entries()
            log_system_event(
                service="AICommand",
                status=hist_res.status,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                message=f"{hist_res.intent}: {hist_res.message[:180]}",
                trace_id=trace_id,
            )
            _log_action(query, hist_res)
            return hist_res
        chat_res = _ask_llm_fallback(q_raw, ctx)
        chat_res = _apply_confidence_policy(chat_res)
        chat_res.traces = traces + list(chat_res.traces or []) + [{"step": "route", "detail": "chat_lane_default"}] + _runtime_trace_entries()
        log_system_event(
            service="AICommand",
            status=chat_res.status,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            message=f"{chat_res.intent}: {chat_res.message[:180]}",
            trace_id=trace_id,
        )
        _log_action(query, chat_res)
        return chat_res

    # Ask one clarification question instead of executing ambiguous actions.
    if (
        understanding.confidence < clarify_min_conf
        and not parsed.action
        and understanding.intent_class in {"question", "action"}
    ):
        follow_q = understanding.clarifying_question or _build_reasoning_followup_question(q_raw, ctx)
        res = AICommandResult(
            status="needs_clarification",
            intent="unknown",
            message=follow_q,
            confidence=float(understanding.confidence or 0.0),
            citations=[],
            matched_by="understanding_layer",
            version=ORCHESTRATOR_VERSION,
            traces=traces + [{"step": "route", "detail": "clarify_before_action"}] + _runtime_trace_entries(),
        )
        log_system_event(
            service="AICommand",
            status=res.status,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            message=f"{res.intent}: {res.message[:180]}",
            trace_id=trace_id,
        )
        _log_quality_event(
            "needs_clarification",
            query,
            {"source": "understanding_layer", "confidence": float(understanding.confidence or 0.0), "topic": understanding.topic},
        )
        _log_action(query, res)
        return res

    # Action-specific confidence gate: do not mutate when understanding is weak.
    if effective_parsed.action in {"add_task", "add_note_draft", "add_daily_log"} and float(understanding.confidence or 0.0) < mutation_min_conf:
        follow_q = understanding.clarifying_question or _build_reasoning_followup_question(q_raw, ctx)
        res = AICommandResult(
            status="needs_clarification",
            intent="unknown",
            message=follow_q,
            confidence=float(understanding.confidence or 0.0),
            citations=[],
            matched_by="understanding_layer",
            version=ORCHESTRATOR_VERSION,
            traces=traces + [{"step": "route", "detail": "action_confidence_gate"}] + _runtime_trace_entries(),
        )
        log_system_event(
            service="AICommand",
            status=res.status,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            message=f"{res.intent}: {res.message[:180]}",
            trace_id=trace_id,
        )
        _log_quality_event(
            "action_confidence_gate",
            query,
            {"action": effective_parsed.action, "confidence": float(understanding.confidence or 0.0)},
        )
        _log_action(query, res)
        return res

    # ── Readonly guard ────────────────────────────────────────────────────────
    # AI_CHAT_READONLY=1 (default) blocks all portfolio/cash mutations from the
    # AI chat path. Notes/tasks still allowed. Use the Portfolio page to trade.
    _PORTFOLIO_MUTATIONS = {"buy", "sell", "trim", "add_position", "update_cash", "execute_trade", "record_portfolio_transaction"}
    _chat_readonly = str(app_env("AI_CHAT_READONLY", "1") or "1").strip() not in {"0", "false", "off", "no"}
    if _chat_readonly and effective_parsed.action in _PORTFOLIO_MUTATIONS:
        res = AICommandResult(
            status="blocked",
            intent="readonly_guard",
            message=(
                "Portfolio and cash mutations are disabled in AI chat to protect your data. "
                "Use the Portfolio page to make changes."
            ),
            confidence=1.0,
            citations=[],
            matched_by="readonly_guard",
            version=ORCHESTRATOR_VERSION,
            traces=traces + [{"step": "readonly_guard", "detail": f"blocked:{effective_parsed.action}"}],
        )
        _log_action(query, res)
        return res

    # Single mutation gateway: all parsed write actions execute from one path.
    if effective_parsed.action in {"add_task", "add_note_draft", "add_daily_log", "buy", "sell", "trim", "add_position", "update_cash", "execute_trade", "record_portfolio_transaction"}:
        veto = _risk_veto_assess_mutation(
            parsed=effective_parsed,
            query=q_raw,
            understanding_confidence=float(understanding.confidence or 0.0),
            trace_id=trace_id,
            context=ctx,
        )
        verdict = str(veto.get("verdict") or "allow").strip().lower()
        if verdict == "veto":
            res = AICommandResult(
                status="blocked",
                intent="risk_veto",
                message="Risk Manager vetoed this action due to low confidence or high mutation risk. Please clarify intent.",
                confidence=float(veto.get("confidence") or 0.0),
                citations=[],
                matched_by="risk_veto_agent",
                version=ORCHESTRATOR_VERSION,
                traces=traces + [{"step": "risk_veto", "detail": str(veto.get("reason") or "veto")} ] + _runtime_trace_entries(),
            )
            log_system_event(
                service="AICommand",
                status=res.status,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                message=f"{res.intent}: {res.message[:180]}",
                trace_id=trace_id,
            )
            _log_quality_event("risk_veto_blocked", query, {"action": effective_parsed.action, "reason": str(veto.get("reason") or "")})
            _log_action(query, res)
            return res
        if verdict == "review":
            res = AICommandResult(
                status="needs_clarification",
                intent="risk_veto_review",
                message="Risk Manager review required for this high-impact action. Confirm exact ticker, size, and rationale first.",
                confidence=float(veto.get("confidence") or 0.0),
                citations=[],
                matched_by="risk_veto_agent",
                version=ORCHESTRATOR_VERSION,
                traces=traces + [{"step": "risk_veto", "detail": str(veto.get("reason") or "review")} ] + _runtime_trace_entries(),
            )
            log_system_event(
                service="AICommand",
                status=res.status,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                message=f"{res.intent}: {res.message[:180]}",
                trace_id=trace_id,
            )
            _log_quality_event("risk_veto_review", query, {"action": effective_parsed.action, "reason": str(veto.get("reason") or "")})
            _log_action(query, res)
            return res

    mutation_res = _dispatch_mutation_action(q, effective_parsed)
    if mutation_res is not None:
        if str(mutation_res.status or "").strip().lower() == "ok":
            mutation_res.message = _blend_mutation_message(
                q_raw,
                mutation_res.message,
                action=str(effective_parsed.action or "").strip(),
                verified=True,
                status=str(mutation_res.status or "").strip(),
            )
        mutation_res = _attach_mutation_execution_contract(mutation_res, effective_parsed, q_raw)
        mutation_res = _apply_confidence_policy(mutation_res)
        mutation_res.traces = traces + list(mutation_res.traces or []) + [{"step": "route", "detail": f"mutation_gateway:{effective_parsed.action}"}] + _runtime_trace_entries()
        log_system_event(
            service="AICommand",
            status=mutation_res.status,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            message=f"{mutation_res.intent}: {mutation_res.message[:180]}",
            trace_id=trace_id,
        )
        _log_action(query, mutation_res)
        return mutation_res

    react_data = run_react_information(q_raw if not skip_worker_handlers else q, context=ctx, max_steps=3)
    if isinstance(react_data, dict) and react_data:
        react_res = AICommandResult(
            status=str(react_data.get("status") or "ok"),
            intent=str(react_data.get("intent") or "react_information"),
            message=str(react_data.get("message") or ""),
            confidence=float(react_data.get("confidence") or 0.9),
            redirect_url=str(react_data.get("redirect_url") or ""),
            citations=list(react_data.get("citations") or []),
            matched_by=str(react_data.get("matched_by") or "react_tool_registry"),
            version=ORCHESTRATOR_VERSION,
            traces=list(react_data.get("traces") or []),
            ui=(dict(react_data.get("ui")) if isinstance(react_data.get("ui"), dict) else None),
        )
        react_res = _apply_confidence_policy(react_res)
        react_res.traces = traces + list(react_res.traces or []) + [{"step": "route", "detail": "react_tool_registry"}] + _runtime_trace_entries()
        log_system_event(
            service="AICommand",
            status=react_res.status,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            message=f"{react_res.intent}: {react_res.message[:180]}",
            trace_id=trace_id,
        )
        _log_action(query, react_res)
        return react_res

    cur_rep = _intent_summarize_current_report(q, ctx)
    if cur_rep is not None:
        cur_rep = _apply_confidence_policy(cur_rep)
        cur_rep.traces = traces + list(cur_rep.traces or []) + [{"step": "route", "detail": "summarize_current_report"}] + _runtime_trace_entries()
        log_system_event(
            service="AICommand",
            status=cur_rep.status,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            message=f"{cur_rep.intent}: {cur_rep.message[:180]}",
            trace_id=trace_id,
        )
        _log_action(query, cur_rep)
        return cur_rep

    sec_res = _intent_open_sec_filings(q, effective_parsed)
    if sec_res is not None:
        sec_res = _apply_confidence_policy(sec_res)
        sec_res.traces = traces + list(sec_res.traces or []) + [{"step": "route", "detail": "open_sec_filings"}] + _runtime_trace_entries()
        log_system_event(
            service="AICommand",
            status=sec_res.status,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            message=f"{sec_res.intent}: {sec_res.message[:180]}",
            trace_id=trace_id,
        )
        _log_action(query, sec_res)
        return sec_res

    del_res = _intent_delete_notes(q, ctx)
    if del_res is not None:
        del_res = _apply_confidence_policy(del_res)
        del_res.traces = traces + list(del_res.traces or []) + [{"step": "route", "detail": "_intent_delete_notes"}] + _runtime_trace_entries()
        log_system_event(
            service="AICommand",
            status=del_res.status,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            message=f"{del_res.intent}: {del_res.message[:180]}",
            trace_id=trace_id,
        )
        _log_action(query, del_res)
        return del_res

    if not skip_worker_handlers:
        iv_res = _intent_portfolio_interview(q_raw, ctx)
        if iv_res is not None:
            iv_res = _apply_confidence_policy(iv_res)
            iv_res.traces = traces + list(iv_res.traces or []) + [{"step": "route", "detail": "_intent_portfolio_interview"}] + _runtime_trace_entries()
            log_system_event(
                service="AICommand",
                status=iv_res.status,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                message=f"{iv_res.intent}: {iv_res.message[:180]}",
                trace_id=trace_id,
            )
            _log_action(query, iv_res)
            return iv_res

        wl_follow = _intent_navigation_followup(q_raw, ctx)
        if wl_follow is not None:
            wl_follow = _apply_confidence_policy(wl_follow)
            wl_follow.traces = traces + list(wl_follow.traces or []) + [{"step": "route", "detail": "_intent_watchlist_followup"}] + _runtime_trace_entries()
            log_system_event(
                service="AICommand",
                status=wl_follow.status,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                message=f"{wl_follow.intent}: {wl_follow.message[:180]}",
                trace_id=trace_id,
            )
            _log_action(query, wl_follow)
            return wl_follow

    perf_res = _intent_portfolio_today_status(q)
    if perf_res is not None:
        perf_res = _apply_confidence_policy(perf_res)
        perf_res.traces = traces + list(perf_res.traces or []) + [{"step": "route", "detail": "_intent_portfolio_today_status"}] + _runtime_trace_entries()
        log_system_event(
            service="AICommand",
            status=perf_res.status,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            message=f"{perf_res.intent}: {perf_res.message[:180]}",
            trace_id=trace_id,
        )
        _log_action(query, perf_res)
        return perf_res

    for worker_name, fn in _worker_registry():
        res = fn(q)  # type: ignore[misc]
        if res is not None:
            res = _apply_confidence_policy(res)
            res.traces = (
                traces
                + [{"step": "worker", "detail": worker_name}]
                + list(res.traces or [])
                + [{"step": "route", "detail": fn.__name__}]
                + _runtime_trace_entries()
            )
            log_system_event(
                service="AICommand",
                status=res.status,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                message=f"{res.intent}: {res.message[:180]}",
                trace_id=trace_id,
            )
            _log_action(query, res)
            return res

    # Capability guarantee path before free-form fallback:
    # if intent strongly maps to supported in-chat capabilities, force those handlers.
    if _llm_matches_intent(q_raw, "list_earnings", min_conf=0.72):
        forced = _intent_list_earnings(q_raw)
        if forced is not None:
            forced = _apply_confidence_policy(forced)
            forced.traces = traces + list(forced.traces or []) + [{"step": "route", "detail": "forced:list_earnings"}] + _runtime_trace_entries()
            log_system_event(
                service="AICommand",
                status=forced.status,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                message=f"{forced.intent}: {forced.message[:180]}",
                trace_id=trace_id,
            )
            _log_action(query, forced)
            return forced
    if _llm_matches_intent(q_raw, "report_section", min_conf=0.72):
        forced_sec = _intent_report_section(q_raw, ctx)
        if forced_sec is not None:
            forced_sec = _apply_confidence_policy(forced_sec)
            forced_sec.traces = traces + list(forced_sec.traces or []) + [{"step": "route", "detail": "forced:report_section"}] + _runtime_trace_entries()
            log_system_event(
                service="AICommand",
                status=forced_sec.status,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                message=f"{forced_sec.intent}: {forced_sec.message[:180]}",
                trace_id=trace_id,
            )
            _log_action(query, forced_sec)
            return forced_sec

    logger.warning("UNMATCHED_INTENT: %s", q)
    res = _ask_llm_fallback(q, ctx)
    # Evidence-first fallback guard:
    # for data-oriented requests, prefer tool-backed output over free text.
    data_like = bool(re.search(r"\b(calendar|earnings|report|summary|facts|risk|portfolio)\b", _norm(q_raw)))
    if data_like:
        react_retry = run_react_information(q_raw, context=ctx, max_steps=4)
        if isinstance(react_retry, dict) and str(react_retry.get("message") or "").strip():
            forced = AICommandResult(
                status=str(react_retry.get("status") or "ok"),
                intent=str(react_retry.get("intent") or "react_information"),
                message=str(react_retry.get("message") or ""),
                confidence=float(react_retry.get("confidence") or 0.9),
                redirect_url=str(react_retry.get("redirect_url") or ""),
                citations=list(react_retry.get("citations") or []),
                matched_by=str(react_retry.get("matched_by") or "react_tool_registry"),
                version=ORCHESTRATOR_VERSION,
                traces=list(react_retry.get("traces") or []),
                ui=(dict(react_retry.get("ui")) if isinstance(react_retry.get("ui"), dict) else None),
            )
            forced = _apply_confidence_policy(forced)
            forced.traces = traces + list(forced.traces or []) + [{"step": "route", "detail": "evidence_guard:react_retry"}] + _runtime_trace_entries()
            log_system_event(
                service="AICommand",
                status=forced.status,
                latency_ms=(time.perf_counter() - t0) * 1000.0,
                message=f"{forced.intent}: {forced.message[:180]}",
                trace_id=trace_id,
            )
            _log_action(query, forced)
            return forced
    res = _apply_confidence_policy(res)
    res.traces = traces + list(res.traces or []) + [{"step": "route", "detail": "llm_fallback"}] + _runtime_trace_entries()
    log_system_event(
        service="AICommand",
        status=res.status,
        latency_ms=(time.perf_counter() - t0) * 1000.0,
        message=f"{res.intent}: {res.message[:180]}",
        trace_id=trace_id,
    )
    _log_action(query, res)
    return res
