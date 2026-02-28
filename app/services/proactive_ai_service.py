from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
from typing import Any

from app.core.config import ROOT
from app.core.analysis_context import (
    build_analysis_context,
    build_execute_payload,
    format_citation_url,
)
from app.core.db import core_conn as _conn_core, onyx_conn as _conn_onyx, sqlite_retry
from app.core.num import to_float as _to_float
from app.core.ticker import safe_ticker as _safe_ticker
from app.core.proposal_pipeline import (
    insights_from_reasoning,
    passes_reasoning_quality,
    run_proposal_pipeline,
)
from app.services.company_file_service import add_company_note, add_company_reminder, add_company_task
from app.services.company_lookup_service import company_name_map
from app.services.phase2_scaling_service import mirror_action_proposal
from app.services.postgres_core_service import (
    core_backend,
    ensure_postgres_core_schema,
    list_action_proposals_pg,
    list_investor_style_memory_pg,
    list_watchlist_thesis_pg,
    pg_connect,
)
from app.services.user_preferences_service import upsert_user_preference
from app.services.mini_statements_service import fetch_historical_financials
from app.services.company_intel_service import get_company_intel
from app.services.price_metrics_service import get_price_metrics
from app.services.portfolio_state_service import read_portfolio_rows_state, read_watchlist_rows_state

try:
    from tools.llm_engine import ask_ai, ask_ai_json_schema
except Exception:  # pragma: no cover
    ask_ai = None  # type: ignore[assignment]
    ask_ai_json_schema = None  # type: ignore[assignment]


MAX_PROPOSALS_PER_MONITOR_RUN = 30
PROPOSAL_COOLDOWN_HOURS = 24
SIM_MAX_DEPTH = 3

def _read_scope_tickers() -> tuple[set[str], set[str], set[str]]:
    portfolio: set[str] = set()
    watchlist: set[str] = set()
    bluechips: set[str] = set()
    for r in read_portfolio_rows_state():
        t = _safe_ticker(str(r.get("ticker") or ""))
        if t:
            portfolio.add(t)
    for r in read_watchlist_rows_state():
        t = _safe_ticker(str(r.get("ticker") or ""))
        if t:
            watchlist.add(t)
    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is not None:
            try:
                cur = con_pg.cursor()
                cur.execute("SELECT to_regclass('public.blue_chips_core')")
                if (cur.fetchone() or [None])[0]:
                    cur.execute("SELECT ticker FROM blue_chips_core")
                    for r in cur.fetchall() or []:
                        t = _safe_ticker(str(r[0] or ""))
                        if t:
                            bluechips.add(t)
            except Exception:
                pass
            finally:
                con_pg.close()
    else:
        con = _conn_core()
        try:
            con.execute(
                """CREATE TABLE IF NOT EXISTS blue_chips (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL UNIQUE,
                    added_at TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT ''
                )"""
            )
            rows = con.execute("SELECT ticker FROM blue_chips").fetchall()
            for r in rows:
                t = _safe_ticker(str(r["ticker"] or ""))
                if t:
                    bluechips.add(t)
        except Exception:
            pass
        finally:
            con.close()
    return portfolio, watchlist, bluechips


def ensure_proactive_schema() -> None:
    ensure_postgres_core_schema()

    def _has_col(con: sqlite3.Connection, table: str, col: str) -> bool:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
        names = {str(r["name"] if isinstance(r, sqlite3.Row) else r[1]).strip().lower() for r in rows}
        return str(col or "").strip().lower() in names

    def _add_col_if_missing(con: sqlite3.Connection, table: str, col_def: str) -> None:
        col = str(col_def or "").strip().split(" ", 1)[0].strip()
        if not col or _has_col(con, table, col):
            return
        con.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")

    def _write() -> None:
        if core_backend() == "postgres":
            return
        con_onyx = _conn_onyx()
        con_core = _conn_core()
        try:
            con_core.execute(
                """CREATE TABLE IF NOT EXISTS user_preferences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pref_key TEXT NOT NULL UNIQUE,
                    pref_value TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'chat',
                    preference_key TEXT NOT NULL DEFAULT '',
                    preference_value TEXT NOT NULL DEFAULT '',
                    context_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            _add_col_if_missing(con_core, "user_preferences", "preference_key TEXT NOT NULL DEFAULT ''")
            _add_col_if_missing(con_core, "user_preferences", "preference_value TEXT NOT NULL DEFAULT ''")
            _add_col_if_missing(con_core, "user_preferences", "context_reason TEXT NOT NULL DEFAULT ''")
            con_core.execute("CREATE INDEX IF NOT EXISTS idx_core_pref_key_v2 ON user_preferences(preference_key)")
            con_core.execute(
                """CREATE TABLE IF NOT EXISTS proactive_monitor_state (
                    state_key TEXT PRIMARY KEY,
                    state_value TEXT NOT NULL DEFAULT ''
                )"""
            )
            con_core.execute(
                """CREATE TABLE IF NOT EXISTS agent_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_uid TEXT NOT NULL UNIQUE,
                    agent_name TEXT NOT NULL,
                    trigger_type TEXT NOT NULL DEFAULT 'event_driven',
                    status TEXT NOT NULL DEFAULT 'running',
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL DEFAULT '',
                    duration_ms REAL NOT NULL DEFAULT 0.0,
                    trace_id TEXT NOT NULL DEFAULT '',
                    input_json TEXT NOT NULL DEFAULT '{}',
                    output_json TEXT NOT NULL DEFAULT '{}',
                    error_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            con_onyx.execute(
                """CREATE TABLE IF NOT EXISTS entities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    entity_type TEXT NOT NULL DEFAULT '',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    normalized_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(type, normalized_name)
                )"""
            )
            con_onyx.execute(
                """CREATE TABLE IF NOT EXISTS relationships (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL,
                    target_id INTEGER NOT NULL,
                    relationship_type TEXT NOT NULL,
                    citation_link TEXT NOT NULL,
                    citation_url TEXT NOT NULL DEFAULT '',
                    citation_text TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0.0,
                    confidence_score REAL NOT NULL DEFAULT 0.0,
                    created_at TEXT NOT NULL,
                    UNIQUE(source_id, target_id, relationship_type, citation_link)
                )"""
            )
            con_onyx.execute(
                """CREATE TABLE IF NOT EXISTS ontology_ingest_state (
                    state_key TEXT PRIMARY KEY,
                    state_value TEXT NOT NULL DEFAULT ''
                )"""
            )
            con_onyx.execute(
                """CREATE TABLE IF NOT EXISTS user_preferences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    preference_key TEXT NOT NULL UNIQUE,
                    preference_value TEXT NOT NULL,
                    context_reason TEXT NOT NULL DEFAULT '',
                    source_ref TEXT NOT NULL DEFAULT 'reject_feedback',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            con_onyx.execute("UPDATE entities SET id_text = 'ent_' || id WHERE COALESCE(id_text,'') = ''")
            con_onyx.execute("UPDATE relationships SET id_text = 'rel_' || id WHERE COALESCE(id_text,'') = ''")
            con_core.commit()
            con_onyx.commit()
        finally:
            con_core.close()
            con_onyx.close()

    sqlite_retry(_write)


def start_agent_run(agent_name: str, trigger_type: str = "event_driven", input_payload: dict[str, Any] | None = None, trace_id: str = "") -> str:
    ensure_proactive_schema()
    run_uid = "run_" + dt.datetime.now().strftime("%Y%m%d%H%M%S%f")
    now = dt.datetime.now().isoformat()

    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is not None:
            try:
                cur = con_pg.cursor()
                cur.execute(
                    """INSERT INTO agent_runs_core
                       (id, run_uid, agent_name, trigger_type, status, started_at, trace_id, input_json, output_json, created_at, updated_at)
                       VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM agent_runs_core), %s,%s,%s,'running',%s,%s,%s::jsonb,'{}'::jsonb,%s,%s)""",
                    (
                        run_uid,
                        str(agent_name or "agent").strip()[:80],
                        str(trigger_type or "event_driven").strip()[:80],
                        now,
                        str(trace_id or "")[:120],
                        json.dumps(input_payload or {}, ensure_ascii=True),
                        now,
                        now,
                    ),
                )
                con_pg.commit()
            finally:
                con_pg.close()
    else:
        def _write() -> None:
            con = _conn_core()
            try:
                con.execute(
                    """INSERT INTO agent_runs
                       (run_uid, agent_name, trigger_type, status, started_at, trace_id, input_json, output_json, created_at, updated_at)
                       VALUES (?, ?, ?, 'running', ?, ?, ?, '{}', ?, ?)""",
                    (
                        run_uid,
                        str(agent_name or "agent").strip()[:80],
                        str(trigger_type or "event_driven").strip()[:80],
                        now,
                        str(trace_id or "")[:120],
                        json.dumps(input_payload or {}, ensure_ascii=True),
                        now,
                        now,
                    ),
                )
                con.commit()
            finally:
                con.close()

        sqlite_retry(_write)
    return run_uid


def finish_agent_run(run_uid: str, status: str, output_payload: dict[str, Any] | None = None, error_text: str = "") -> bool:
    ensure_proactive_schema()
    uid = str(run_uid or "").strip()
    if not uid:
        return False
    now = dt.datetime.now().isoformat()
    ok = {"done": False}

    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is not None:
            try:
                cur = con_pg.cursor()
                cur.execute("SELECT started_at FROM agent_runs_core WHERE run_uid=%s LIMIT 1", (uid,))
                row = cur.fetchone()
                started = str((row[0] if row else "") or "").strip()
                dur_ms = 0.0
                if started:
                    try:
                        dur_ms = max(0.0, (dt.datetime.now() - dt.datetime.fromisoformat(started)).total_seconds() * 1000.0)
                    except Exception:
                        dur_ms = 0.0
                cur.execute(
                    """UPDATE agent_runs_core
                       SET status=%s, finished_at=%s, duration_ms=%s, output_json=%s::jsonb, error_text=%s, updated_at=%s
                       WHERE run_uid=%s""",
                    (
                        str(status or "finished").strip()[:40],
                        now,
                        float(dur_ms),
                        json.dumps(output_payload or {}, ensure_ascii=True),
                        str(error_text or "")[:1000],
                        now,
                        uid,
                    ),
                )
                ok["done"] = cur.rowcount > 0
                con_pg.commit()
            finally:
                con_pg.close()
    else:
        def _write() -> None:
            con = _conn_core()
            try:
                row = con.execute("SELECT started_at FROM agent_runs WHERE run_uid=? LIMIT 1", (uid,)).fetchone()
                started = str((row["started_at"] if row else "") or "").strip()
                dur_ms = 0.0
                if started:
                    try:
                        dur_ms = max(0.0, (dt.datetime.now() - dt.datetime.fromisoformat(started)).total_seconds() * 1000.0)
                    except Exception:
                        dur_ms = 0.0
                con.execute(
                    """UPDATE agent_runs
                       SET status=?, finished_at=?, duration_ms=?, output_json=?, error_text=?, updated_at=?
                       WHERE run_uid=?""",
                    (
                        str(status or "finished").strip()[:40],
                        now,
                        float(dur_ms),
                        json.dumps(output_payload or {}, ensure_ascii=True),
                        str(error_text or "")[:1000],
                        now,
                        uid,
                    ),
                )
                ok["done"] = con.total_changes > 0
                con.commit()
            finally:
                con.close()

        sqlite_retry(_write)
    return bool(ok["done"])


def cleanup_stuck_agent_runs(stale_minutes: int = 60) -> int:
    """Mark any agent_runs rows stuck in 'running' state for > stale_minutes as 'error'.
    Returns count of rows cleaned up. Safe to call on every worker startup."""
    cutoff = (dt.datetime.now() - dt.timedelta(minutes=int(stale_minutes or 60))).isoformat()
    now = dt.datetime.now().isoformat()
    cleaned = 0
    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is None:
            return 0
        try:
            cur = con_pg.cursor()
            cur.execute(
                """UPDATE agent_runs_core
                   SET status='error', finished_at=%s, error_text='cleaned_up_stuck_run', updated_at=%s
                   WHERE status='running' AND started_at < %s""",
                (now, now, cutoff),
            )
            cleaned = cur.rowcount
            con_pg.commit()
        except Exception:
            try:
                con_pg.rollback()
            except Exception:
                pass
        finally:
            con_pg.close()
    else:
        con = _conn_core()
        try:
            cur = con.execute(
                """UPDATE agent_runs
                   SET status='error', finished_at=?, error_text='cleaned_up_stuck_run', updated_at=?
                   WHERE status='running' AND started_at < ?""",
                (now, now, cutoff),
            )
            cleaned = cur.rowcount
            con.commit()
        except Exception:
            pass
        finally:
            con.close()
    return cleaned


def _reflexion_pattern_key(event_type: str, detail: dict[str, Any]) -> str:
    ev = str(event_type or "unknown").strip().lower()
    intent = str((detail or {}).get("intent") or "").strip().lower()
    route = str((detail or {}).get("route") or "").strip().lower()
    tool = str((detail or {}).get("tool_name") or "").strip().lower()
    parts = [p for p in [ev, intent, route, tool] if p]
    key = "|".join(parts) or ev or "unknown"
    return key[:180]


def _build_reflexion_rule(event_type: str, query: str, detail: dict[str, Any] | None = None) -> tuple[str, str, float]:
    """Let the LLM reason about the failure and generate a contextual learning rule."""
    ev = str(event_type or "").strip().lower()
    q = str(query or "").strip()
    d = dict(detail or {})

    if ask_ai is not None:
        try:
            prompt = (
                "You are an AI self-improvement engine. A failure event occurred in an investment assistant.\n"
                "Analyze the failure and generate ONE learning rule to prevent recurrence.\n\n"
                f"Event type: {ev}\n"
                f"User query: {q[:800]}\n"
                f"Detail: {json.dumps(d, ensure_ascii=True)[:600]}\n\n"
                "Return strict JSON only:\n"
                '{"rule_key": "snake_case_name_max_80_chars", '
                '"rule_text": "Actionable instruction for the AI to follow (max 300 chars)", '
                '"confidence": 0.0}\n\n'
                "Rules:\n"
                "- rule_key: unique descriptive snake_case (max 80 chars)\n"
                "- rule_text: clear, actionable instruction the AI can follow in future interactions\n"
                "- confidence: 0.0-1.0 based on how clearly this failure implies the rule\n"
                "- Focus on root cause, not symptoms\n"
                "- Be specific to this failure pattern, not generic"
            )
            raw = str(ask_ai(prompt, "AI reflexion engine. JSON only.", mode="fast", json_mode=True, temperature=0.0) or "").strip()
            if raw:
                parsed = json.loads(raw)
                rk = str(parsed.get("rule_key") or "")[:80].strip()
                rt = str(parsed.get("rule_text") or "")[:300].strip()
                rc = float(parsed.get("confidence") or 0.0)
                if rk and rt and 0.0 < rc <= 1.0:
                    return (rk, rt, rc)
        except Exception:
            pass

    # Fallback: generic rule when LLM unavailable
    return (
        f"reflexion_{ev[:40]}_{dt.datetime.now().strftime('%Y%m%d%H%M')}",
        f"Review and improve handling of '{ev}' events to reduce user friction.",
        0.65,
    )


def record_reflexion_from_quality_event(event_type: str, query: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    ensure_proactive_schema()
    ev = str(event_type or "").strip().lower()
    d = dict(detail or {})
    q = str(query or "").strip()
    tracked = {
        "action_confidence_gate", "clarify_before_action", "needs_clarification",
        "mutation_verify_failed", "verify_failed", "mutation_execution_error",
        "llm_fallback_error", "risk_veto_blocked", "risk_veto_review",
        "outcome_miss",
    }
    if ev not in tracked:
        return {"ok": True, "skipped": True, "reason": "event_not_tracked"}
    now = dt.datetime.now().isoformat()
    pattern_key = _reflexion_pattern_key(ev, d)
    rule_key, rule_text, conf = _build_reflexion_rule(ev, q, d)
    note_text = f"Observed failure pattern '{ev}'. Applied rule '{rule_key}' to reduce repeats."
    out = {"ok": False, "policy_version": "", "rule_key": rule_key}

    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is None:
            return {"ok": False, "error": "pg_not_available"}
        try:
            cur = con_pg.cursor()
            cur.execute(
                """INSERT INTO reflexion_notes_core
                   (id, created_at, event_type, query, detail_json, note_text, rule_key, rule_text, confidence)
                   VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM reflexion_notes_core), %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                   RETURNING id""",
                (now, ev[:120], q[:4000], json.dumps(d, ensure_ascii=True), note_text[:1200], rule_key[:120], rule_text[:1200], float(conf)),
            )
            rowid = cur.fetchone()
            note_id = int((rowid[0] if rowid else 0) or 0)
            cur.execute("SELECT id, total_count, open_count FROM failure_patterns_core WHERE pattern_key=%s LIMIT 1", (pattern_key,))
            row = cur.fetchone()
            if row:
                cur.execute(
                    """UPDATE failure_patterns_core
                       SET total_count=%s, open_count=%s, last_seen_at=%s, last_query=%s, last_detail_json=%s::jsonb, last_reflexion=%s
                       WHERE id=%s""",
                    (
                        int(row[1] or 0) + 1,
                        int(row[2] or 0) + 1,
                        now,
                        q[:500],
                        json.dumps(d, ensure_ascii=True),
                        note_text[:800],
                        int(row[0] or 0),
                    ),
                )
            else:
                cur.execute(
                    """INSERT INTO failure_patterns_core
                       (id, pattern_key, event_type, total_count, open_count, resolved_count, first_seen_at, last_seen_at, last_query, last_detail_json, last_reflexion)
                       VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM failure_patterns_core), %s, %s, 1, 1, 0, %s, %s, %s, %s::jsonb, %s)""",
                    (pattern_key, ev[:120], now, now, q[:500], json.dumps(d, ensure_ascii=True), note_text[:800]),
                )
            policy = {
                "rule_key": rule_key,
                "rule_text": rule_text,
                "event_type": ev,
                "pattern_key": pattern_key,
                "confidence": float(conf),
                "created_at": now,
            }
            tag = "rv_" + dt.datetime.now().strftime("%Y%m%d%H%M%S%f")
            cur.execute("UPDATE reflexion_policy_versions_core SET is_active=0 WHERE is_active=1")
            cur.execute(
                """INSERT INTO reflexion_policy_versions_core
                   (id, version_tag, created_at, source_event_id, policy_json, is_active, rolled_back_from)
                   VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM reflexion_policy_versions_core), %s, %s, %s, %s::jsonb, 1, '')""",
                (tag, now, note_id, json.dumps(policy, ensure_ascii=True)),
            )
            con_pg.commit()
            out["ok"] = True
            out["policy_version"] = tag
            upsert_user_preference(pref_key=f"reflexion::{rule_key}", pref_value=rule_text, source=f"reflexion:{tag}")
        finally:
            con_pg.close()
        return out

    def _write() -> None:
        con = _conn_core()
        try:
            con.execute(
                """INSERT INTO reflexion_notes
                   (created_at, event_type, query, detail_json, note_text, rule_key, rule_text, confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, ev[:120], q[:4000], json.dumps(d, ensure_ascii=True), note_text[:1200], rule_key[:120], rule_text[:1200], float(conf)),
            )
            note_id = int((con.execute("SELECT last_insert_rowid() AS x").fetchone() or {"x": 0})["x"] or 0)
            row = con.execute("SELECT id, total_count, open_count FROM failure_patterns WHERE pattern_key=? LIMIT 1", (pattern_key,)).fetchone()
            if row:
                con.execute(
                    """UPDATE failure_patterns
                       SET total_count=?, open_count=?, last_seen_at=?, last_query=?, last_detail_json=?, last_reflexion=?
                       WHERE id=?""",
                    (
                        int(row["total_count"] or 0) + 1,
                        int(row["open_count"] or 0) + 1,
                        now,
                        q[:500],
                        json.dumps(d, ensure_ascii=True),
                        note_text[:800],
                        int(row["id"] or 0),
                    ),
                )
            else:
                con.execute(
                    """INSERT INTO failure_patterns
                       (pattern_key, event_type, total_count, open_count, resolved_count, first_seen_at, last_seen_at, last_query, last_detail_json, last_reflexion)
                       VALUES (?, ?, 1, 1, 0, ?, ?, ?, ?, ?)""",
                    (pattern_key, ev[:120], now, now, q[:500], json.dumps(d, ensure_ascii=True), note_text[:800]),
                )
            policy = {
                "rule_key": rule_key,
                "rule_text": rule_text,
                "event_type": ev,
                "pattern_key": pattern_key,
                "confidence": float(conf),
                "created_at": now,
            }
            tag = "rv_" + dt.datetime.now().strftime("%Y%m%d%H%M%S%f")
            con.execute("UPDATE reflexion_policy_versions SET is_active=0 WHERE is_active=1")
            con.execute(
                """INSERT INTO reflexion_policy_versions
                   (version_tag, created_at, source_event_id, policy_json, is_active, rolled_back_from)
                   VALUES (?, ?, ?, ?, 1, '')""",
                (tag, now, note_id, json.dumps(policy, ensure_ascii=True)),
            )
            con.commit()
            out["ok"] = True
            out["policy_version"] = tag
            upsert_user_preference(pref_key=f"reflexion::{rule_key}", pref_value=rule_text, source=f"reflexion:{tag}")
        finally:
            con.close()

    sqlite_retry(_write)
    return out


def rollback_reflexion_policy(target_version: str) -> dict[str, Any]:
    ensure_proactive_schema()
    tv = str(target_version or "").strip()
    if not tv:
        return {"ok": False, "error": "missing_version"}
    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is None:
            return {"ok": False, "error": "pg_not_available"}
        try:
            cur = con_pg.cursor()
            cur.execute("SELECT version_tag FROM reflexion_policy_versions_core WHERE version_tag=%s LIMIT 1", (tv,))
            row = cur.fetchone()
            if not row:
                return {"ok": False, "error": "version_not_found"}
            cur.execute("SELECT version_tag FROM reflexion_policy_versions_core WHERE is_active=1 LIMIT 1")
            current = cur.fetchone()
            prev = str((current[0] if current else "") or "")
            cur.execute("UPDATE reflexion_policy_versions_core SET is_active=0 WHERE is_active=1")
            cur.execute(
                "UPDATE reflexion_policy_versions_core SET is_active=1, rolled_back_from=%s WHERE version_tag=%s",
                (prev[:80], tv),
            )
            con_pg.commit()
            return {"ok": True, "active_version": tv, "rolled_back_from": prev}
        finally:
            con_pg.close()
    con = _conn_core()
    try:
        row = con.execute("SELECT version_tag FROM reflexion_policy_versions WHERE version_tag=? LIMIT 1", (tv,)).fetchone()
        if not row:
            return {"ok": False, "error": "version_not_found"}
        current = con.execute("SELECT version_tag FROM reflexion_policy_versions WHERE is_active=1 LIMIT 1").fetchone()
        prev = str((current["version_tag"] if current else "") or "")
        con.execute("UPDATE reflexion_policy_versions SET is_active=0 WHERE is_active=1")
        con.execute(
            "UPDATE reflexion_policy_versions SET is_active=1, rolled_back_from=? WHERE version_tag=?",
            (prev[:80], tv),
        )
        con.commit()
        return {"ok": True, "active_version": tv, "rolled_back_from": prev}
    finally:
        con.close()


def list_recent_agent_runs(limit: int = 20) -> list[dict[str, Any]]:
    ensure_proactive_schema()
    lim = max(1, min(200, int(limit or 20)))
    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is None:
            return []
        try:
            cur = con_pg.cursor()
            cur.execute(
                """SELECT run_uid, agent_name, trigger_type, status, started_at, finished_at, duration_ms, error_text, created_at
                   FROM agent_runs_core
                   ORDER BY id DESC
                   LIMIT %s""",
                (lim,),
            )
            rows = cur.fetchall() or []
            return [
                {
                    "run_uid": str(r[0] or ""),
                    "agent_name": str(r[1] or ""),
                    "trigger_type": str(r[2] or ""),
                    "status": str(r[3] or ""),
                    "started_at": str(r[4] or ""),
                    "finished_at": str(r[5] or ""),
                    "duration_ms": float(r[6] or 0.0),
                    "error_text": str(r[7] or ""),
                    "created_at": str(r[8] or ""),
                }
                for r in rows
            ]
        finally:
            con_pg.close()
    con = _conn_core()
    try:
        rows = con.execute(
            """SELECT run_uid, agent_name, trigger_type, status, started_at, finished_at, duration_ms, error_text, created_at
               FROM agent_runs
               ORDER BY id DESC
               LIMIT ?""",
            (lim,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "run_uid": str(r["run_uid"] or ""),
                    "agent_name": str(r["agent_name"] or ""),
                    "trigger_type": str(r["trigger_type"] or ""),
                    "status": str(r["status"] or ""),
                    "started_at": str(r["started_at"] or ""),
                    "finished_at": str(r["finished_at"] or ""),
                    "duration_ms": float(r["duration_ms"] or 0.0),
                    "error_text": str(r["error_text"] or ""),
                    "created_at": str(r["created_at"] or ""),
                }
            )
        return out
    finally:
        con.close()


def list_recent_reflexions(limit: int = 20) -> dict[str, Any]:
    ensure_proactive_schema()
    lim = max(1, min(200, int(limit or 20)))
    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is None:
            return {"notes": [], "policy_versions": []}
        try:
            cur = con_pg.cursor()
            cur.execute(
                """SELECT id, created_at, event_type, query, note_text, rule_key, rule_text, confidence
                   FROM reflexion_notes_core
                   ORDER BY id DESC
                   LIMIT %s""",
                (lim,),
            )
            notes = cur.fetchall() or []
            cur.execute(
                """SELECT version_tag, created_at, source_event_id, policy_json::text, is_active, rolled_back_from
                   FROM reflexion_policy_versions_core
                   ORDER BY id DESC
                   LIMIT 8"""
            )
            policy = cur.fetchall() or []
            return {
                "notes": [
                    {
                        "id": int(r[0] or 0),
                        "created_at": str(r[1] or ""),
                        "event_type": str(r[2] or ""),
                        "query": str(r[3] or ""),
                        "note_text": str(r[4] or ""),
                        "rule_key": str(r[5] or ""),
                        "rule_text": str(r[6] or ""),
                        "confidence": float(r[7] or 0.0),
                    }
                    for r in notes
                ],
                "policy_versions": [
                    {
                        "version_tag": str(r[0] or ""),
                        "created_at": str(r[1] or ""),
                        "source_event_id": int(r[2] or 0),
                        "policy_json": str(r[3] or "{}"),
                        "is_active": int(r[4] or 0),
                        "rolled_back_from": str(r[5] or ""),
                    }
                    for r in policy
                ],
            }
        finally:
            con_pg.close()
    con = _conn_core()
    try:
        notes = con.execute(
            """SELECT id, created_at, event_type, query, note_text, rule_key, rule_text, confidence
               FROM reflexion_notes
               ORDER BY id DESC
               LIMIT ?""",
            (lim,),
        ).fetchall()
        policy = con.execute(
            """SELECT version_tag, created_at, source_event_id, policy_json, is_active, rolled_back_from
               FROM reflexion_policy_versions
               ORDER BY id DESC
               LIMIT 8"""
        ).fetchall()
        return {
            "notes": [
                {
                    "id": int(r["id"] or 0),
                    "created_at": str(r["created_at"] or ""),
                    "event_type": str(r["event_type"] or ""),
                    "query": str(r["query"] or ""),
                    "note_text": str(r["note_text"] or ""),
                    "rule_key": str(r["rule_key"] or ""),
                    "rule_text": str(r["rule_text"] or ""),
                    "confidence": float(r["confidence"] or 0.0),
                }
                for r in notes
            ],
            "policy_versions": [
                {
                    "version_tag": str(r["version_tag"] or ""),
                    "created_at": str(r["created_at"] or ""),
                    "source_event_id": int(r["source_event_id"] or 0),
                    "policy_json": str(r["policy_json"] or "{}"),
                    "is_active": int(r["is_active"] or 0),
                    "rolled_back_from": str(r["rolled_back_from"] or ""),
                }
                for r in policy
            ],
        }
    finally:
        con.close()


def get_ai_audit_timeline(limit: int = 40) -> list[dict[str, Any]]:
    """Merge agent runs, proposal actions, and policy updates into a single chronological audit log."""
    lim = max(1, min(200, int(limit or 40)))
    events: list[dict[str, Any]] = []

    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is None:
            return []
        try:
            cur = con_pg.cursor()
            proposal_cols: set[str] = set()
            try:
                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema='public' AND table_name='action_proposals_core'
                    """
                )
                proposal_cols = {str(r[0] or "").strip().lower() for r in (cur.fetchall() or [])}
            except Exception:
                proposal_cols = set()
            # Agent runs
            cur.execute(
                """SELECT started_at, agent_name, status, trigger_type, duration_ms, error_text, run_uid
                   FROM agent_runs_core ORDER BY id DESC LIMIT %s""",
                (lim,),
            )
            for r in cur.fetchall() or []:
                dur = float(r[4] or 0)
                events.append({
                    "ts": str(r[0] or ""),
                    "kind": "agent_run",
                    "label": f"Agent: {r[1] or 'worker'}",
                    "status": str(r[2] or ""),
                    "detail": f"trigger={r[3] or '-'}, {int(dur/1000)}s" if dur else f"trigger={r[3] or '-'}",
                    "error": str(r[5] or ""),
                    "uid": str(r[6] or ""),
                })
            # Proposal executions and rejections
            dir_expr = "direction" if "direction" in proposal_cols else (
                "suggested_action" if "suggested_action" in proposal_cols else "''"
            )
            conf_expr = "confidence" if "confidence" in proposal_cols else (
                "confidence_score" if "confidence_score" in proposal_cols else "0.0"
            )
            cur.execute(
                f"""SELECT created_at, ticker, {dir_expr} AS direction, title, status, {conf_expr} AS confidence
                   FROM action_proposals_core
                   WHERE status IN ('executed','rejected','debate_rejected')
                   ORDER BY id DESC LIMIT %s""",
                (lim,),
            )
            for r in cur.fetchall() or []:
                st = str(r[4] or "")
                conf = float(r[5] or 0.0)
                events.append({
                    "ts": str(r[0] or ""),
                    "kind": f"proposal_{st}",
                    "label": f"Proposal {st.replace('_',' ').title()}: {r[1] or '?'} {r[2] or ''}",
                    "status": st,
                    "detail": (str(r[3] or "")[:80]) + (f"  [conf {conf:.0%}]" if conf else ""),
                    "error": "",
                    "uid": "",
                })
            # Reflexion policy updates
            cur.execute(
                """SELECT created_at, version_tag, is_active, rolled_back_from
                   FROM reflexion_policy_versions_core ORDER BY id DESC LIMIT 10"""
            )
            for r in cur.fetchall() or []:
                tag = str(r[1] or "")
                active = int(r[2] or 0)
                rolled = str(r[3] or "")
                events.append({
                    "ts": str(r[0] or ""),
                    "kind": "policy_update",
                    "label": f"Policy {'rolled back to' if rolled else 'updated'}: {tag}",
                    "status": "active" if active else "superseded",
                    "detail": f"rolled_back_from={rolled}" if rolled else "",
                    "error": "",
                    "uid": "",
                })
        finally:
            con_pg.close()
    else:
        # SQLite fallback
        con = _conn_core()
        try:
            rows = con.execute(
                """SELECT started_at, agent_name, status, trigger_type, duration_ms, error_text, run_uid
                   FROM agent_runs ORDER BY id DESC LIMIT ?""", (lim,)
            ).fetchall()
            for r in rows:
                dur = float(r["duration_ms"] or 0)
                events.append({
                    "ts": str(r["started_at"] or ""),
                    "kind": "agent_run",
                    "label": f"Agent: {r['agent_name'] or 'worker'}",
                    "status": str(r["status"] or ""),
                    "detail": f"trigger={r['trigger_type'] or '-'}, {int(dur/1000)}s" if dur else f"trigger={r['trigger_type'] or '-'}",
                    "error": str(r["error_text"] or ""),
                    "uid": str(r["run_uid"] or ""),
                })
        finally:
            con.close()

    # Sort by timestamp descending
    def _ts_key(e: dict[str, Any]) -> str:
        return str(e.get("ts") or "")

    events.sort(key=_ts_key, reverse=True)
    return events[:lim]


def _state_get(con: sqlite3.Connection, key: str, default: str = "") -> str:
    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is None:
            return default
        try:
            cur = con_pg.cursor()
            cur.execute("SELECT state_value FROM proactive_monitor_state_core WHERE state_key=%s LIMIT 1", (key,))
            row = cur.fetchone()
            if not row:
                return default
            return str(row[0] or default)
        finally:
            con_pg.close()
    row = con.execute("SELECT state_value FROM proactive_monitor_state WHERE state_key = ? LIMIT 1", (key,)).fetchone()
    if not row:
        return default
    return str(row["state_value"] or default)


def _state_set(con: sqlite3.Connection, key: str, value: str) -> None:
    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is None:
            return
        try:
            cur = con_pg.cursor()
            cur.execute(
                "INSERT INTO proactive_monitor_state_core(state_key, state_value) VALUES(%s, %s) "
                "ON CONFLICT(state_key) DO UPDATE SET state_value=EXCLUDED.state_value",
                (key, str(value or "")),
            )
            con_pg.commit()
        finally:
            con_pg.close()
        return
    con.execute(
        "INSERT INTO proactive_monitor_state(state_key, state_value) VALUES(?, ?) "
        "ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value",
        (key, str(value or "")),
    )


def _entity_id(con: sqlite3.Connection, name: str, typ: str) -> int:
    nm = str(name or "").strip()
    tp = str(typ or "").strip().upper()
    norm = re.sub(r"\s+", " ", nm.lower())
    now = dt.datetime.now().isoformat()
    con.execute(
        "INSERT INTO entities(name, type, entity_type, metadata, normalized_name, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(type, normalized_name) DO UPDATE SET name=excluded.name, entity_type=excluded.entity_type, updated_at=excluded.updated_at",
        (nm, tp, tp, "{}", norm, now, now),
    )
    row = con.execute("SELECT id FROM entities WHERE type = ? AND normalized_name = ? LIMIT 1", (tp, norm)).fetchone()
    return int(row["id"] or 0) if row else 0


def _entity_id_pg(con_pg: Any, name: str, typ: str) -> int:
    nm = str(name or "").strip()
    tp = str(typ or "").strip().upper()
    norm = re.sub(r"\s+", " ", nm.lower())
    now = dt.datetime.now().isoformat()
    cur = con_pg.cursor()
    cur.execute(
        """
        INSERT INTO entities_core(name, type, entity_type, metadata_json, normalized_name, id_text, created_at, updated_at, id)
        VALUES(%s,%s,%s,%s::jsonb,%s,%s,%s,%s,(SELECT COALESCE(MAX(id),0)+1 FROM entities_core))
        ON CONFLICT(type, normalized_name) DO UPDATE SET
          name=EXCLUDED.name,
          entity_type=EXCLUDED.entity_type,
          id_text=EXCLUDED.id_text,
          updated_at=EXCLUDED.updated_at
        """,
        (nm, tp, tp, "{}", norm, f"{tp.lower()}:{norm}", now, now),
    )
    cur.execute("SELECT id FROM entities_core WHERE type = %s AND normalized_name = %s LIMIT 1", (tp, norm))
    row = cur.fetchone()
    return int((row or [0])[0] or 0)


def _relationship_upsert(
    con: sqlite3.Connection,
    source_id: int,
    target_id: int,
    rel_type: str,
    citation_link: str,
    citation_text: str,
    confidence: float = 0.8,
) -> None:
    if source_id <= 0 or target_id <= 0:
        return
    con.execute(
        """DELETE FROM relationships
           WHERE source_id = ? AND target_id = ? AND relationship_type = ?""",
        (
            int(source_id),
            int(target_id),
            str(rel_type or "").strip().upper(),
        ),
    )


def _relationship_upsert_pg(
    con_pg: Any,
    source_id: int,
    target_id: int,
    rel_type: str,
    citation_link: str,
    citation_text: str,
    confidence: float = 0.8,
) -> None:
    if source_id <= 0 or target_id <= 0:
        return
    cur = con_pg.cursor()
    cur.execute(
        """DELETE FROM relationships_core
           WHERE source_id = %s AND target_id = %s AND relationship_type = %s""",
        (int(source_id), int(target_id), str(rel_type or "").strip().upper()),
    )
    cur.execute(
        """INSERT INTO relationships_core
           (id, source_id, target_id, relationship_type, citation_link, citation_url, citation_text, confidence, confidence_score, created_at)
           VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM relationships_core), %s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT(source_id, target_id, relationship_type, citation_link) DO NOTHING""",
        (
            int(source_id),
            int(target_id),
            str(rel_type or "").strip().upper(),
            str(citation_link or "").strip(),
            str(citation_link or "").strip(),
            str(citation_text or "")[:500],
            float(confidence or 0.0),
            float(confidence or 0.0),
            dt.datetime.now().isoformat(),
        ),
    )


def _extract_themes(text: str) -> list[str]:
    low = str(text or "").lower()
    out: list[str] = []
    theme_map = {
        "REGULATORY_RISK": ("regulat", "sec", "antitrust", "litigation", "legal", "compliance"),
        "DEMAND_RISK": ("demand", "slowdown", "churn", "retention"),
        "MARGIN_PRESSURE": ("margin", "cost inflation", "pricing pressure"),
        "BALANCE_SHEET": ("debt", "liquidity", "refinanc", "cash flow"),
        "AI_EXECUTION": ("ai", "artificial intelligence", "model"),
        "MACRO_RISK": ("inflation", "rate", "recession", "fx", "currency"),
    }
    for theme, keys in theme_map.items():
        if any(k in low for k in keys):
            out.append(theme)
    return out[:4]


def _extract_peer_tickers(text: str, self_ticker: str) -> list[str]:
    peers: list[str] = []
    seen: set[str] = set()
    for m in re.findall(r"\b[A-Z]{2,5}\b", str(text or "").upper()):
        tk = _safe_ticker(m)
        if not tk or tk == self_ticker or tk in seen:
            continue
        seen.add(tk)
        peers.append(tk)
        if len(peers) >= 6:
            break
    return peers


def ingest_ontology_from_report_facts(limit_rows: int = 200) -> dict[str, int]:
    ensure_proactive_schema()
    created_entities = 0
    created_edges = 0
    scanned = 0
    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is None:
            return {"scanned": 0, "entities": 0, "edges": 0}
        try:
            cur = con_pg.cursor()
            cur.execute("SELECT state_value FROM ontology_ingest_state_core WHERE state_key='last_report_fact_id' LIMIT 1")
            row = cur.fetchone()
            last_id = int(str(((row or [None])[0] if row else "0") or "0") or "0")
            cur.execute(
                """SELECT id, ticker, fact_text, report_name, importance
                   FROM report_facts_core
                   WHERE id > %s
                   ORDER BY id ASC
                   LIMIT %s""",
                (last_id, max(20, min(2000, int(limit_rows or 200)))),
            )
            fetched = cur.fetchall() or []
            rows = [
                {"id": int(r[0] or 0), "ticker": str(r[1] or ""), "fact_text": str(r[2] or ""), "report_name": str(r[3] or ""), "importance": int(r[4] or 0)}
                for r in fetched
            ]
            max_seen = last_id
            for r in rows:
                scanned += 1
                rid = int(r["id"] or 0)
                max_seen = max(max_seen, rid)
                tk = _safe_ticker(str(r["ticker"] or ""))
                txt = str(r["fact_text"] or "").strip()
                rep = str(r["report_name"] or "").strip()
                if not tk or not txt:
                    continue
                citation = f"/reports/view?name={rep}" if rep else "/reports"

                src_id = _entity_id_pg(con_pg, tk, "COMPANY")
                if src_id > 0:
                    created_entities += 1

                themes = _extract_themes(txt)
                for th in themes:
                    tid = _entity_id_pg(con_pg, th, "RISK_THEME")
                    if tid > 0:
                        created_entities += 1
                    _relationship_upsert_pg(con_pg, src_id, tid, "EXPOSED_TO", citation, txt, confidence=0.82)
                    created_edges += 1

                peers = _extract_peer_tickers(txt, tk)
                for peer in peers:
                    pid = _entity_id_pg(con_pg, peer, "COMPANY")
                    if pid > 0:
                        created_entities += 1
                    rel = "COMPETES_WITH"
                    low = txt.lower()
                    if "supplier" in low or "supply" in low:
                        rel = "SUPPLIER_TO"
                    elif "customer" in low:
                        rel = "CUSTOMER_OF"
                    _relationship_upsert_pg(con_pg, src_id, pid, rel, citation, txt, confidence=0.76)
                    created_edges += 1

            cur.execute(
                "INSERT INTO ontology_ingest_state_core(state_key, state_value) VALUES(%s,%s) "
                "ON CONFLICT(state_key) DO UPDATE SET state_value=EXCLUDED.state_value",
                ("last_report_fact_id", str(max_seen)),
            )
            con_pg.commit()
        except Exception:
            try:
                con_pg.rollback()
            except Exception:
                pass
        finally:
            con_pg.close()
    else:
        con_core = _conn_core()
        con_onyx = _conn_onyx()
        try:
            row = con_onyx.execute("SELECT state_value FROM ontology_ingest_state WHERE state_key='last_report_fact_id' LIMIT 1").fetchone()
            last_id = int(str((row["state_value"] if row else "0") or "0") or "0")
            rows = []
            max_seen = last_id
            for r in rows:
                scanned += 1
                rid = int(r["id"] or 0)
                max_seen = max(max_seen, rid)
                tk = _safe_ticker(str(r["ticker"] or ""))
                txt = str(r["fact_text"] or "").strip()
                rep = str(r["report_name"] or "").strip()
                if not tk or not txt:
                    continue
                citation = f"/reports/view?name={rep}" if rep else "/reports"

                src_id = _entity_id(con_onyx, tk, "COMPANY")
                if src_id > 0:
                    created_entities += 1

                themes = _extract_themes(txt)
                for th in themes:
                    tid = _entity_id(con_onyx, th, "RISK_THEME")
                    if tid > 0:
                        created_entities += 1
                    _relationship_upsert(con_onyx, src_id, tid, "EXPOSED_TO", citation, txt, confidence=0.82)
                    created_edges += 1

                peers = _extract_peer_tickers(txt, tk)
                for peer in peers:
                    pid = _entity_id(con_onyx, peer, "COMPANY")
                    if pid > 0:
                        created_entities += 1
                    rel = "COMPETES_WITH"
                    low = txt.lower()
                    if "supplier" in low or "supply" in low:
                        rel = "SUPPLIER_TO"
                    elif "customer" in low:
                        rel = "CUSTOMER_OF"
                    _relationship_upsert(con_onyx, src_id, pid, rel, citation, txt, confidence=0.76)
                    created_edges += 1

            con_onyx.execute(
                "INSERT INTO ontology_ingest_state(state_key, state_value) VALUES('last_report_fact_id', ?) "
                "ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value",
                (str(max_seen),),
            )
            con_onyx.commit()
        finally:
            con_core.close()
            con_onyx.close()
    return {"scanned": scanned, "entities": created_entities, "edges": created_edges}


def _build_thesis_bullets(ticker: str, text: str, kind: str) -> list[str]:
    t = str(text or "").strip()
    if not t:
        return []
    if ask_ai is not None:
        try:
            prompt = (
                "Return strict JSON with exactly 3 concise bullets for an investment action card.\n"
                '{"bullets":["...","...","..."]}\n'
                f"Ticker: {ticker}\nType: {kind}\nEvidence: {t[:1200]}"
            )
            raw = str(ask_ai(prompt, "Investment strategist. JSON only.", mode="smart", json_mode=True, temperature=0.1) or "").strip()
            obj = json.loads(raw)
            bullets = [str(x).strip() for x in list(obj.get("bullets") or []) if str(x).strip()]
            if len(bullets) >= 3:
                return bullets[:3]
        except Exception:
            pass
    s = t[:280]
    return [
        f"New signal detected for {ticker}.",
        s,
        "Review this signal against your thesis before any action.",
    ]


def _proposal_upsert(
    *,
    source_event_key: str,
    kind: str,
    ticker: str,
    title: str,
    bullets: list[str],
    citations: list[dict[str, str]],
    confidence: float,
    priority_score: float,
    execute_route: str,
    execute_payload: dict[str, Any] | None = None,
) -> bool:
    if core_backend() != "postgres":
        return False
    source_key = str(source_event_key or "").strip()[:240]
    if not source_key:
        return False
    now = dt.datetime.now().isoformat()
    tk = _safe_ticker(ticker)
    signal_blob = f"{title}\n" + "\n".join([str(x or "").strip() for x in list(bullets or [])[:3]])
    if _is_non_event_signal(signal_blob, tk):
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("SELECT id FROM action_proposals_core WHERE source_event_key=%s LIMIT 1", (source_key,))
        exists = cur.fetchone()
        if exists:
            return False
        cur.execute("SELECT COALESCE(MAX(id),0)+1 FROM action_proposals_core")
        new_id = int((cur.fetchone() or [0])[0] or 1)
        weight = float((_portfolio_weight_map().get(tk, 0.0) if tk else 0.0) or 0.0)
        thesis_ctx = _watchlist_thesis_context(None, tk)
        style_ctx = _investor_style_context(None, limit=20)
        decisions_ctx = _recent_decision_context(None, tk, limit=12)
        reasoning = _evaluate_signal_reasoning(
            ticker=tk,
            signal_text=signal_blob,
            portfolio_weight_pct=weight,
            thesis_text=thesis_ctx,
            style_text=style_ctx,
            decisions_text=decisions_ctx,
        )
        verify = _verify_reasoning_relevance(
            ticker=tk,
            signal_text=signal_blob,
            thesis_text=thesis_ctx,
            style_text=style_ctx,
            reasoning=reasoning,
        )
        pipeline = run_proposal_pipeline(
            relevance_ok=not _is_non_event_signal(signal_blob, tk),
            raw_reasoning=reasoning,
            citations=citations,
            confidence=confidence,
            priority_score=priority_score,
            verify_result=verify,
        )
        if not bool(pipeline.get("accepted")):
            return False
        reasoning = dict(pipeline.get("reasoning") or {})
        insights = [x for x in list(pipeline.get("insights") or []) if isinstance(x, dict)]
        citations = [x for x in list(pipeline.get("citations") or []) if isinstance(x, dict)]
        confidence = float(pipeline.get("confidence") or 0.0)
        priority_score = float(pipeline.get("priority_score") or 0.0)
        analysis_ctx = build_analysis_context(analysis_id=source_key, source=str(kind or ""), trace_id=f"ap_{new_id}")
        exec_payload = build_execute_payload(
            route=str(execute_route or "/dashboard"),
            context=analysis_ctx,
            payload=dict(execute_payload or {}),
        )
        row = {
            "id": new_id,
            "created_at": now,
            "updated_at": now,
            "status": "open",
            "kind": str(kind or "thesis_trigger"),
            "ticker": str(ticker or "").upper(),
            "title": str(title or "")[:240],
            "thesis_json": list(bullets or [])[:3],
            "citations_json": list(citations or [])[:8],
            "insights_json": list(insights or [])[:3],
            "reasoning_json": dict(reasoning or {}),
            "confidence": float(confidence or 0.0),
            "priority_score": float(priority_score or 0.0),
            "execute_route": str(exec_payload.get("route") or "/dashboard"),
            "execute_payload_json": dict(exec_payload or {}),
            "source_event_key": source_key,
            "proposal_uid": f"ap_{new_id}",
            "target_ticker": str(ticker or "").upper(),
            "suggested_action": "REVIEW",
            "thesis_summary": " ".join([str(x).strip() for x in list(bullets or [])[:3] if str(x).strip()])[:1500],
            "confidence_score": float(confidence or 0.0),
            "dismissed_reason": "",
            "rejection_reason": "",
            "rejected_at": "",
            "executed_at": "",
        }
        # Multi-agent debate gate — run for high-confidence proposals (>= 0.5)
        debate_approved = True
        if confidence >= 0.5:
            try:
                from app.services.debate_service import run_proposal_debate
                debate_result = run_proposal_debate(
                    ticker=tk,
                    signal=signal_blob,
                    proposed_stance=str(row.get("suggested_action") or "REVIEW"),
                    reasoning_summary=str(row.get("thesis_summary") or "")[:600],
                    proposal_id=new_id,
                )
                debate_approved = bool(debate_result.get("approved", True))
                if not debate_approved:
                    row["status"] = "debate_rejected"
                    artifacts = debate_result.get("artifacts") or {}
                    row["rejection_reason"] = str(artifacts.get("judge_rationale", "debate_rejected"))[:500]
            except Exception:
                pass  # debate errors → allow proposal through
        con.commit()
        _upsert_action_proposal_core_pg(row)
        if not debate_approved:
            return False  # stored for audit but skip mirror/cascade
        mirror_action_proposal(
            {
                "source_event_key": source_key,
                "proposal_id": new_id,
                "ticker": str(row["ticker"] or ""),
                "kind": str(row["kind"] or ""),
                "status": "open",
                "title": str(row["title"] or ""),
                "insights_json": list(row["insights_json"] or []),
                "reasoning_json": dict(row["reasoning_json"] or {}),
                "created_at": now,
                "updated_at": now,
            }
        )
        # Phase 3.4: auto-run cascade analysis when a new proposal is created
        if tk:
            try:
                analyze_portfolio_cascades(
                    trigger_ticker=tk,
                    trigger_signal=signal_blob[:500],
                    trigger_proposal_id=new_id,
                )
            except Exception:
                pass
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def _upsert_action_proposal_core_pg(row: dict[str, Any]) -> None:
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO action_proposals_core
            (id, created_at, updated_at, status, kind, ticker, title, thesis_json, citations_json, insights_json, reasoning_json,
             confidence, priority_score, execute_route, execute_payload_json, source_event_key, proposal_uid, target_ticker, suggested_action,
             thesis_summary, confidence_score, dismissed_reason, rejection_reason, rejected_at, executed_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(id) DO UPDATE SET
              updated_at=EXCLUDED.updated_at,status=EXCLUDED.status,kind=EXCLUDED.kind,ticker=EXCLUDED.ticker,title=EXCLUDED.title,
              thesis_json=EXCLUDED.thesis_json,citations_json=EXCLUDED.citations_json,insights_json=EXCLUDED.insights_json,reasoning_json=EXCLUDED.reasoning_json,
              confidence=EXCLUDED.confidence,priority_score=EXCLUDED.priority_score,execute_route=EXCLUDED.execute_route,execute_payload_json=EXCLUDED.execute_payload_json,
              source_event_key=EXCLUDED.source_event_key,proposal_uid=EXCLUDED.proposal_uid,target_ticker=EXCLUDED.target_ticker,
              suggested_action=EXCLUDED.suggested_action,thesis_summary=EXCLUDED.thesis_summary,confidence_score=EXCLUDED.confidence_score,
              dismissed_reason=EXCLUDED.dismissed_reason,rejection_reason=EXCLUDED.rejection_reason,rejected_at=EXCLUDED.rejected_at,executed_at=EXCLUDED.executed_at
            """,
            (
                int(row.get("id") or 0),
                str(row.get("created_at") or ""),
                str(row.get("updated_at") or ""),
                str(row.get("status") or "open"),
                str(row.get("kind") or ""),
                str(row.get("ticker") or ""),
                str(row.get("title") or ""),
                json.dumps(list(row.get("thesis_json") or []), ensure_ascii=True),
                json.dumps(list(row.get("citations_json") or []), ensure_ascii=True),
                json.dumps(list(row.get("insights_json") or []), ensure_ascii=True),
                json.dumps(dict(row.get("reasoning_json") or {}), ensure_ascii=True),
                float(row.get("confidence") or 0.0),
                float(row.get("priority_score") or 0.0),
                str(row.get("execute_route") or ""),
                json.dumps(dict(row.get("execute_payload_json") or {}), ensure_ascii=True),
                str(row.get("source_event_key") or ""),
                str(row.get("proposal_uid") or ""),
                str(row.get("target_ticker") or ""),
                str(row.get("suggested_action") or "REVIEW"),
                str(row.get("thesis_summary") or ""),
                float(row.get("confidence_score") or 0.0),
                str(row.get("dismissed_reason") or ""),
                str(row.get("rejection_reason") or ""),
                str(row.get("rejected_at") or ""),
                str(row.get("executed_at") or ""),
            ),
        )
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


def _pg_get_action_proposal_row(pid: int) -> dict[str, Any] | None:
    con = pg_connect()
    if con is None:
        return None
    try:
        cur = con.cursor()
        cur.execute(
            """SELECT id, created_at, updated_at, status, kind, ticker, title, thesis_json, citations_json, insights_json, reasoning_json,
                      confidence, priority_score, execute_route, execute_payload_json, source_event_key, proposal_uid, target_ticker, suggested_action,
                      thesis_summary, confidence_score, dismissed_reason, rejection_reason, rejected_at, executed_at
               FROM action_proposals_core
               WHERE id=%s LIMIT 1""",
            (int(pid),),
        )
        r = cur.fetchone()
        if not r:
            return None
        return {
            "id": int(r[0] or 0),
            "created_at": str(r[1] or ""),
            "updated_at": str(r[2] or ""),
            "status": str(r[3] or ""),
            "kind": str(r[4] or ""),
            "ticker": str(r[5] or ""),
            "title": str(r[6] or ""),
            "thesis_json": r[7] if isinstance(r[7], list) else (json.loads(str(r[7] or "[]")) if str(r[7] or "").strip() else []),
            "citations_json": r[8] if isinstance(r[8], list) else (json.loads(str(r[8] or "[]")) if str(r[8] or "").strip() else []),
            "insights_json": r[9] if isinstance(r[9], list) else (json.loads(str(r[9] or "[]")) if str(r[9] or "").strip() else []),
            "reasoning_json": r[10] if isinstance(r[10], dict) else (json.loads(str(r[10] or "{}")) if str(r[10] or "").strip() else {}),
            "confidence": float(r[11] or 0.0),
            "priority_score": float(r[12] or 0.0),
            "execute_route": str(r[13] or ""),
            "execute_payload_json": r[14] if isinstance(r[14], dict) else (json.loads(str(r[14] or "{}")) if str(r[14] or "").strip() else {}),
            "source_event_key": str(r[15] or ""),
            "proposal_uid": str(r[16] or ""),
            "target_ticker": str(r[17] or ""),
            "suggested_action": str(r[18] or ""),
            "thesis_summary": str(r[19] or ""),
            "confidence_score": float(r[20] or 0.0),
            "dismissed_reason": str(r[21] or ""),
            "rejection_reason": str(r[22] or ""),
            "rejected_at": str(r[23] or ""),
            "executed_at": str(r[24] or ""),
        }
    except Exception:
        return None
    finally:
        con.close()


def _has_open_proposal(kind: str, ticker: str) -> bool:
    if core_backend() != "postgres":
        return False
    k = str(kind or "").strip()
    t = str(ticker or "").strip().upper()
    if not k or not t:
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT id FROM action_proposals_core WHERE status='open' AND kind=%s AND ticker=%s LIMIT 1",
            (k, t),
        )
        return bool(cur.fetchone())
    except Exception:
        return False
    finally:
        con.close()


def _has_recent_proposal(kind: str, ticker: str, cooldown_hours: int = PROPOSAL_COOLDOWN_HOURS) -> bool:
    if core_backend() != "postgres":
        return False
    k = str(kind or "").strip()
    t = str(ticker or "").strip().upper()
    if not k or not t:
        return False
    since = (dt.datetime.now() - dt.timedelta(hours=max(1, int(cooldown_hours or 1)))).isoformat()
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT id FROM action_proposals_core WHERE kind=%s AND ticker=%s AND created_at>=%s LIMIT 1",
            (k, t, since),
        )
        return bool(cur.fetchone())
    except Exception:
        return False
    finally:
        con.close()


def _day_pct_map() -> dict[str, float]:
    out: dict[str, float] = {}
    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is None:
            return out
        try:
            cur = con_pg.cursor()
            cur.execute("SELECT ticker, day_pct FROM intel24_snapshot_core")
            for r in cur.fetchall() or []:
                tk = _safe_ticker(str(r[0] or ""))
                if not tk:
                    continue
                out[tk] = _to_float(r[1], 0.0)
        except Exception:
            return {}
        finally:
            con_pg.close()
        return out
    con = _conn_core()
    try:
        rows = con.execute("SELECT ticker, day_pct FROM intel24_snapshot").fetchall()
        for r in rows:
            tk = _safe_ticker(str(r["ticker"] or ""))
            if not tk:
                continue
            out[tk] = _to_float(r["day_pct"], 0.0)
    finally:
        con.close()
    return out

def _portfolio_weight_map() -> dict[str, float]:
    positions: dict[str, tuple[float, float]] = {}
    for r in read_portfolio_rows_state():
        tk = _safe_ticker(str(r.get("ticker") or ""))
        if not tk:
            continue
        sh = _to_float(r.get("shares"), 0.0)
        cost = _to_float(r.get("cost"), 0.0)
        if sh <= 0:
            continue
        positions[tk] = (sh, cost)

    if not positions:
        return {}
    day_map = _day_pct_map()
    total = 0.0
    values: dict[str, float] = {}
    for tk, (sh, cost) in positions.items():
        # Prefer a live proxy from intel snapshot if available; fallback to avg cost.
        live_proxy = 0.0
        d = _to_float(day_map.get(tk), 0.0)
        if cost > 0:
            live_proxy = cost * (1.0 + d / 100.0)
        px = live_proxy if live_proxy > 0 else cost
        val = max(0.0, sh * px)
        values[tk] = val
        total += val
    if total <= 0:
        return {}
    return {tk: (val / total) * 100.0 for tk, val in values.items()}


def _extract_company_insights(bullets: list[str], use_ai: bool = True) -> list[dict[str, str]]:
    lines = [str(x or "").strip() for x in (bullets or []) if str(x or "").strip()]
    if not lines:
        return []
    if use_ai and ask_ai is not None:
        evidence = "\n".join(f"- {x}" for x in lines[:8])
        prompt = (
            "You are extracting dashboard insight cards from evidence.\n"
            "Return STRICT JSON only with this schema:\n"
            '{"insights":[{"label":"Business/Product|Margins/Profitability|Risk/Red Flags","text":"..."}]}\n'
            "Rules:\n"
            "- Max 3 insights.\n"
            "- Use only evidence provided.\n"
            "- Keep each text under 180 chars.\n"
            "- If evidence is weak, omit that insight instead of guessing.\n\n"
            f"Evidence:\n{evidence}"
        )
        try:
            raw = str(
                ask_ai(
                    prompt,
                    "Financial dashboard insight extractor. Strict JSON only.",
                    mode="fast",
                    json_mode=True,
                    temperature=0.0,
                )
                or ""
            ).strip()
            obj = json.loads(raw)
            out: list[dict[str, str]] = []
            for item in list(obj.get("insights") or []):
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label") or "").strip()
                text = str(item.get("text") or "").strip()
                if not label or not text:
                    continue
                out.append({"label": label[:36], "text": text[:220]})
            if out:
                return out[:3]
        except Exception:
            pass

    # Deterministic fallback only when model extraction is unavailable.
    return [{"label": "Key Insight", "text": x[:220]} for x in lines[:3]]


def _watchlist_thesis_context(con: sqlite3.Connection | None, ticker: str) -> str:
    tk = _safe_ticker(ticker)
    if not tk:
        return ""
    if core_backend() == "postgres":
        rows = list_watchlist_thesis_pg(limit=600)
        row = next((r for r in rows if str(r.get("ticker") or "").strip().upper() == tk), None)
        if not row:
            return ""
        parts = [
            str(row.get("thesis_summary") or "").strip(),
            str(row.get("thesis") or "").strip(),
            str(row.get("invalidation_criteria") or row.get("invalidation") or "").strip(),
        ]
        return " | ".join([p for p in parts if p])[:2600]
    return ""


def _investor_style_context(con: sqlite3.Connection | None, limit: int = 20) -> str:
    if core_backend() == "postgres":
        rows_pg = list_investor_style_memory_pg(limit=max(1, int(limit or 20)))
        out_pg: list[str] = []
        for r in rows_pg:
            k = str(r.get("key") or "").strip()
            a = str(r.get("answer") or "").strip()
            if not (k or a):
                continue
            out_pg.append(f"- {k}: {a}")
        return "\n".join(out_pg)[:3200]
    return ""


def _recent_decision_context(con: sqlite3.Connection | None, ticker: str, limit: int = 12) -> str:
    tk = _safe_ticker(ticker)
    if not tk or con is None:
        return ""
    rows = con.execute(
        """SELECT COALESCE(action,''), COALESCE(status,''), COALESCE(thesis,''), COALESCE(key_risks,''),
                  COALESCE(outcome_note,''), COALESCE(created_at,'')
           FROM decisions
           WHERE ticker = ?
           ORDER BY id DESC
           LIMIT ?""",
        (tk, max(1, int(limit or 12))),
    ).fetchall()
    out: list[str] = []
    for r in rows:
        action = str(r[0] or "").strip()
        status = str(r[1] or "").strip()
        thesis = str(r[2] or "").strip()
        key_risks = str(r[3] or "").strip()
        outcome = str(r[4] or "").strip()
        created = str(r[5] or "").strip()
        txt = " | ".join([x for x in [created, action, status, thesis or key_risks or outcome] if x])
        if txt:
            out.append(f"- {txt[:220]}")
    return "\n".join(out)[:2400]


def _build_financial_context(ticker: str) -> str:
    """Build a structured financial context block with actual numbers for the LLM."""
    tk = _safe_ticker(ticker)
    if not tk:
        return ""
    lines: list[str] = []

    # 1. Historical financials (5yr)
    try:
        fin = fetch_historical_financials(tk, metric="all", years=5)
        if fin.get("ok") and fin.get("years"):
            yrs = fin["years"]
            rev = fin.get("revenue") or []
            gp = fin.get("gross_profit") or []
            ocf = fin.get("operating_cash_flow") or []
            fcf = fin.get("free_cash_flow") or []
            debt = fin.get("total_debt") or []
            equity = fin.get("total_equity") or []
            cash = fin.get("total_cash") or []
            ccy = fin.get("currency") or "USD"

            def _fmt(v: Any) -> str:
                if v is None:
                    return "N/A"
                try:
                    f = float(v)
                    if abs(f) >= 1e9:
                        return f"{f/1e9:.1f}B"
                    if abs(f) >= 1e6:
                        return f"{f/1e6:.0f}M"
                    return f"{f:,.0f}"
                except Exception:
                    return "N/A"

            def _pct(a: Any, b: Any) -> str:
                try:
                    af, bf = float(a), float(b)
                    if bf == 0:
                        return "N/A"
                    return f"{((af - bf) / abs(bf)) * 100:+.1f}%"
                except Exception:
                    return "N/A"

            def _margin(num: Any, denom: Any) -> str:
                try:
                    n, d = float(num), float(denom)
                    if d == 0:
                        return "N/A"
                    return f"{(n / d) * 100:.1f}%"
                except Exception:
                    return "N/A"

            lines.append(f"FINANCIAL DATA ({ccy}, 5-year):")
            # Revenue row
            rev_row = " | ".join(f"{y}: {_fmt(r)}" for y, r in zip(yrs, rev))
            lines.append(f"  Revenue: {rev_row}")
            # YoY revenue growth
            if len(rev) >= 2:
                growths = [_pct(rev[i], rev[i - 1]) for i in range(1, len(rev))]
                lines.append(f"  Revenue YoY Growth: {' → '.join(growths)}")
            # Gross margin row
            if rev and gp:
                margins = [_margin(g, r) for g, r in zip(gp, rev)]
                lines.append(f"  Gross Margin: {' → '.join(margins)}")
            # FCF row
            if fcf:
                fcf_row = " | ".join(f"{y}: {_fmt(f)}" for y, f in zip(yrs, fcf))
                lines.append(f"  Free Cash Flow: {fcf_row}")
            # Debt/Equity
            if debt and equity:
                de_ratios = []
                for d_val, e_val in zip(debt, equity):
                    try:
                        dv, ev = float(d_val), float(e_val)
                        de_ratios.append(f"{dv / ev:.2f}" if ev != 0 else "N/A")
                    except Exception:
                        de_ratios.append("N/A")
                lines.append(f"  Debt/Equity: {' → '.join(de_ratios)}")
            # Net cash position (latest)
            if cash and debt:
                try:
                    net_cash = float(cash[-1] or 0) - float(debt[-1] or 0)
                    lines.append(f"  Net Cash Position (latest): {_fmt(net_cash)}")
                except Exception:
                    pass
    except Exception:
        pass

    # 2. XBRL intel (segments, buybacks, insider trades)
    try:
        intel = get_company_intel(tk, refresh=False)
        if intel and intel.get("ticker"):
            segs = intel.get("revenue_segments") or {}
            products = segs.get("product") or []
            geos = segs.get("geography") or []
            if products:
                lines.append("REVENUE SEGMENTS (Product):")
                for s in products[:8]:
                    lbl = str(s.get("label") or "").strip()
                    pct = s.get("pct")
                    val = s.get("value")
                    if lbl:
                        parts = [lbl]
                        if pct is not None:
                            parts.append(f"{float(pct):.1f}%")
                        if val is not None:
                            try:
                                v = float(val)
                                parts.append(f"({v / 1e9:.1f}B)" if abs(v) >= 1e9 else f"({v / 1e6:.0f}M)")
                            except Exception:
                                pass
                        lines.append(f"  {' — '.join(parts)}")
            if geos:
                lines.append("REVENUE SEGMENTS (Geography):")
                for s in geos[:6]:
                    lbl = str(s.get("label") or "").strip()
                    pct = s.get("pct")
                    if lbl and pct is not None:
                        lines.append(f"  {lbl}: {float(pct):.1f}%")

            buyback = intel.get("buyback") or {}
            ttm = buyback.get("ttm_value")
            quarters = buyback.get("quarters") or []
            if ttm is not None or quarters:
                lines.append("BUYBACK ACTIVITY:")
                if ttm is not None:
                    try:
                        lines.append(f"  TTM Buybacks: {float(ttm) / 1e9:.1f}B")
                    except Exception:
                        pass
                for q in quarters[:4]:
                    qp = str(q.get("period") or "").strip()
                    qv = q.get("value")
                    if qp and qv is not None:
                        try:
                            lines.append(f"  {qp}: {float(qv) / 1e9:.1f}B")
                        except Exception:
                            pass

            insiders = intel.get("insider_trades") or []
            if insiders:
                buys = sum(1 for t in insiders if str(t.get("tx_type") or "").upper() == "BUY")
                sells = sum(1 for t in insiders if str(t.get("tx_type") or "").upper() == "SELL")
                lines.append(f"INSIDER ACTIVITY: {buys} buys, {sells} sells (recent)")
                for t in insiders[:4]:
                    owner = str(t.get("owner") or "")[:30]
                    tx = str(t.get("tx_type") or "")
                    shares = t.get("net_shares")
                    date = str(t.get("date") or "")[:10]
                    if owner and tx:
                        sh_str = f" ({int(shares):,} shares)" if shares else ""
                        lines.append(f"  {date} {owner}: {tx}{sh_str}")
    except Exception:
        pass

    # 3. Peer comparison (gross margin + revenue growth vs sector peers)
    try:
        peers = _get_sector_peers(tk)
        if peers:
            peer_lines: list[str] = []
            for ptk in peers[:3]:
                pfin = fetch_historical_financials(ptk, metric="all", years=2)
                if not pfin.get("ok") or not pfin.get("years"):
                    continue
                prev = pfin.get("revenue") or []
                pgp = pfin.get("gross_profit") or []
                if len(prev) >= 2 and len(pgp) >= 2:
                    try:
                        pm = float(pgp[-1]) / float(prev[-1]) * 100 if float(prev[-1]) else 0
                        pg = (float(prev[-1]) - float(prev[-2])) / abs(float(prev[-2])) * 100 if float(prev[-2]) else 0
                        peer_lines.append(f"  {ptk}: margin {pm:.1f}%, rev growth {pg:+.1f}%")
                    except Exception:
                        pass
            if peer_lines:
                lines.append("PEER COMPARISON:")
                lines.extend(peer_lines)
    except Exception:
        pass

    return "\n".join(lines)[:3600]


_SECTOR_PEERS: dict[str, list[str]] = {
    "AAPL": ["MSFT", "GOOGL", "META"],
    "MSFT": ["AAPL", "GOOGL", "CRM"],
    "GOOGL": ["META", "MSFT", "AMZN"],
    "GOOG": ["META", "MSFT", "AMZN"],
    "META": ["GOOGL", "SNAP", "PINS"],
    "AMZN": ["MSFT", "GOOGL", "WMT"],
    "NVDA": ["AMD", "INTC", "QCOM"],
    "AMD": ["NVDA", "INTC", "QCOM"],
    "INTC": ["NVDA", "AMD", "TSM"],
    "QCOM": ["NVDA", "AMD", "MRVL"],
    "TSM": ["INTC", "SMSN", "UMC"],
    "CRM": ["MSFT", "SAP", "ORCL"],
    "ORCL": ["MSFT", "CRM", "SAP"],
    "NFLX": ["DIS", "WBD", "PARA"],
    "DIS": ["NFLX", "WBD", "CMCSA"],
    "JPM": ["BAC", "WFC", "GS"],
    "BAC": ["JPM", "WFC", "C"],
    "GS": ["MS", "JPM", "BLK"],
    "XOM": ["CVX", "COP", "BP"],
    "CVX": ["XOM", "COP", "SLB"],
    "TSLA": ["GM", "F", "RIVN"],
    "COST": ["WMT", "TGT", "BJ"],
    "WMT": ["COST", "TGT", "AMZN"],
    "UNH": ["CVS", "CI", "HUM"],
    "LLY": ["NVO", "PFE", "MRK"],
    "ASML": ["AMAT", "LRCX", "KLAC"],
    "WYNN": ["LVS", "MGM", "CZR"],
    "ADBE": ["CRM", "MSFT", "FIGMA"],
    "PYPL": ["V", "MA", "SQ"],
}


def _get_sector_peers(ticker: str) -> list[str]:
    """Return 3-4 peer tickers for comparison. Entity graph first, hardcoded map as fallback."""
    tk = _safe_ticker(ticker)
    if not tk:
        return []
    peers: list[str] = []
    # 1. Try entity graph
    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is not None:
            try:
                cur = con_pg.cursor()
                cur.execute(
                    """SELECT e2.name FROM relationships_core r
                       JOIN entities_core e1 ON r.source_entity_id = e1.id
                       JOIN entities_core e2 ON r.target_entity_id = e2.id
                       WHERE e1.name = %s AND r.relationship_type = 'COMPETES_WITH'
                       LIMIT 4""",
                    (tk,),
                )
                for row in cur.fetchall() or []:
                    p = _safe_ticker(str(row[0] or ""))
                    if p and p != tk:
                        peers.append(p)
            except Exception:
                pass
            finally:
                con_pg.close()
    # 2. Fall back to hardcoded map
    if not peers:
        peers = [p for p in (_SECTOR_PEERS.get(tk) or []) if p != tk]
    return peers[:4]


def _read_active_reflexion_rules(limit: int = 8) -> str:
    """Return formatted string of high-confidence reflexion rules for injection into LLM prompts."""
    if core_backend() != "postgres":
        return ""
    con_pg = pg_connect()
    if con_pg is None:
        return ""
    try:
        cur = con_pg.cursor()
        cur.execute(
            """SELECT rule_text FROM reflexion_notes_core
               WHERE confidence >= 0.6 AND rule_text IS NOT NULL AND rule_text != ''
               ORDER BY id DESC LIMIT %s""",
            (max(1, min(12, int(limit))),),
        )
        rows = cur.fetchall() or []
        if not rows:
            return ""
        return "\n".join(f"• {str(r[0] or '').strip()}" for r in rows if r[0])
    except Exception:
        return ""
    finally:
        con_pg.close()


def _evaluate_signal_reasoning(
    *,
    ticker: str,
    signal_text: str,
    portfolio_weight_pct: float,
    thesis_text: str,
    style_text: str,
    decisions_text: str = "",
) -> dict[str, Any]:
    sig = str(signal_text or "").strip()
    tk = _safe_ticker(ticker)
    if not tk or not sig:
        return {}
    if ask_ai is None:
        return {}

    # Build rich financial context with actual numbers
    financial_ctx = _build_financial_context(tk)

    # Load reflexion rules from past prediction misses
    reflexion_rules = _read_active_reflexion_rules()
    reflexion_section = (
        f"\nLESSONS FROM PAST PREDICTION MISSES (apply these):\n{reflexion_rules}\n"
        if reflexion_rules else ""
    )

    prompt = (
        "You are an Elite Fundamental Equity Analyst with full access to financial data.\n"
        "You DO the analysis — you don't tell the user to check things. You give clear conclusions with specific numbers.\n\n"
        f"TARGET ASSET: {tk} (Current Weight: {float(portfolio_weight_pct or 0.0):.2f}%)\n\n"
        f"STRUCTURED FINANCIAL DATA:\n{financial_ctx or '(unavailable)'}\n\n"
        f"USER'S INVESTMENT THESIS & EXPECTATIONS:\n{thesis_text or 'not provided'}\n\n"
        f"USER'S STYLE & PREFERENCES:\n{style_text or 'not provided'}\n\n"
        f"RECENT DECISIONS:\n{decisions_text or 'none'}\n\n"
        f"{reflexion_section}"
        f"NEW SIGNAL:\n{sig[:2600]}\n\n"
        "STEP 1: RELEVANCE CHECK\n"
        "Does this signal directly impact this company's core business, margins, or moat? "
        "If it's generic macro noise or a non-event, ABORT — return empty JSON `{}`.\n\n"
        "STEP 2: DEEP NUMERICAL ANALYSIS\n"
        "Using the financial data above, perform concrete analysis:\n"
        "- MARGINS: What are the actual margins? How have they trended? Does this signal change the trajectory?\n"
        "- GROWTH: What is the revenue growth rate? Is it accelerating or decelerating?\n"
        "- CASH FLOW: Is FCF improving or deteriorating? What does the debt position look like?\n"
        "- SEGMENTS: Which business segments are driving growth vs dragging? How concentrated is revenue?\n"
        "- INSIDER SIGNALS: Are insiders buying or selling? What does this tell us about management confidence?\n"
        "- THESIS CHECK: Does the data confirm or contradict the user's thesis? Be specific.\n\n"
        "STEP 3: CAUSE-EFFECT REASONING\n"
        "Think in chains: if X happens → Y impact on margins → Z impact on FCF → implications for valuation.\n"
        "Identify secondary effects the investor might miss.\n\n"
        "STEP 4: GENERATE INSIGHT CARDS\n"
        "Return STRICT JSON. Generate 2-3 `insight_cards` with:\n"
        "- Specific numbers in every insight (e.g., 'Gross margin dropped from 37.8% to 34.2%')\n"
        "- Clear conclusions (not 'check the margin' but 'margin is compressing because...')\n"
        "- Cause-effect chains where applicable\n"
        "{\n"
        '  "insight_cards": [\n'
        '    {"label": "<SPECIFIC_LABEL>", "text": "<Analysis WITH numbers. Max 2 sentences.>"}\n'
        "  ],\n"
        '  "confidence": "high|medium|low",\n'
        '  "margin_impact": "<specific margin analysis with numbers>",\n'
        '  "thesis_validation": "<does data confirm or contradict thesis? be specific>",\n'
        '  "risk_assessment": "<key risks with quantification>",\n'
        '  "recommended_stance": "HOLD|ADD|TRIM|WATCH|EXIT"\n'
        "}"
    )
    try:
        schema = {
            "type": "object",
            "properties": {
                "insight_cards": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "text": {"type": "string"},
                        },
                        "required": ["label", "text"],
                    },
                    "maxItems": 3,
                },
                "confidence": {"type": "string"},
                "margin_impact": {"type": "string"},
                "thesis_validation": {"type": "string"},
                "risk_assessment": {"type": "string"},
                "actionable_proposal": {"type": "string"},
                "peer_contagion": {"type": "string"},
                "recommended_stance": {"type": "string"},
                "invalidation_hit": {"type": "string"},
            },
        }
        if ask_ai_json_schema is not None:
            raw = str(
                ask_ai_json_schema(
                    prompt,
                    "Portfolio risk evaluator. JSON only. No markdown.",
                    response_json_schema=schema,
                    mode="smart",
                    temperature=0.0,
                )
                or ""
            ).strip()
        else:
            raw = str(
                ask_ai(
                    prompt,
                    "Portfolio risk evaluator. JSON only. No markdown.",
                    mode="smart",
                    json_mode=True,
                    temperature=0.0,
                )
                or ""
            ).strip()
        obj = json.loads(raw) if raw else {}
        if not isinstance(obj, dict):
            return {}
        out: dict[str, Any] = {}
        # Keep optional fields if model returns them, but never require static categories.
        for k, limit in {
            "margin_impact": 320,
            "thesis_validation": 320,
            "risk_assessment": 320,
            "actionable_proposal": 320,
            "peer_contagion": 320,
        }.items():
            v = str(obj.get(k) or "").strip()
            if v:
                out[k] = v[:limit]
        c = str(obj.get("confidence") or "").strip().lower()
        if c:
            out["confidence"] = c[:16]
        st = str(obj.get("recommended_stance") or "").strip().upper()
        if st:
            out["recommended_stance"] = st[:12]
        inv = str(obj.get("invalidation_hit") or "").strip().lower()
        if inv:
            out["invalidation_hit"] = inv[:16]
        cards: list[dict[str, str]] = []
        for c in list(obj.get("insight_cards") or []):
            if not isinstance(c, dict):
                continue
            lb = str(c.get("label") or "").strip()[:42]
            tx = str(c.get("text") or "").strip()[:320]
            if lb and tx:
                cards.append({"label": lb, "text": tx})
        if cards:
            out["insight_cards"] = cards[:3]
        if out:
            return out
    except Exception:
        pass
    return {}


def _is_non_event_signal(signal_text: str, ticker: str) -> bool:
    s = str(signal_text or "").strip().lower()
    tk = _safe_ticker(ticker).lower()
    if not s:
        return True
    noise_markers = (
        "not in this week calendar",
        "new signal detected",
        "terminal: daily brief",
    )
    if any(m in s for m in noise_markers):
        return True
    sym_matches = re.findall(r"\b[A-Z]{2,6}\b", str(signal_text or ""))
    sym_set = {x.strip().lower() for x in sym_matches if _safe_ticker(x)}
    if tk and sym_set and tk not in sym_set:
        has_link = any(
            k in s for k in ("supplier", "customer", "compete", "peer", "sector", "macro", "rate", "fx", "yield", "contagion")
        )
        if not has_link:
            return True
    return False


def _verify_reasoning_relevance(
    *,
    ticker: str,
    signal_text: str,
    thesis_text: str,
    style_text: str,
    reasoning: dict[str, Any],
) -> dict[str, str]:
    if ask_ai is None:
        return {"quality_verdict": "unknown", "quality_reason": ""}
    tk = _safe_ticker(ticker)
    if not tk:
        return {"quality_verdict": "reject", "quality_reason": "bad_ticker"}
    prompt = (
        "You are a strict QA verifier for equity signal reasoning.\n"
        "Decide if this reasoning is materially relevant and evidence-grounded, without invented user rules.\n"
        'Return STRICT JSON only with keys: {"accept":true|false,"reason":"..."}\n\n'
        f"ticker: {tk}\n"
        f"signal_text: {str(signal_text or '')[:2200]}\n"
        f"thesis_text: {str(thesis_text or '')[:1800]}\n"
        f"style_text: {str(style_text or '')[:1800]}\n"
        f"reasoning_json: {json.dumps(dict(reasoning or {}), ensure_ascii=True)[:3500]}"
    )
    try:
        raw = str(
            ask_ai(
                prompt,
                "Strict reasoning verifier. JSON only.",
                mode="fast",
                json_mode=True,
                temperature=1.0,
            )
            or ""
        ).strip()
        obj = json.loads(raw) if raw else {}
        ok = bool(obj.get("accept"))
        rs = str(obj.get("reason") or "").strip()[:220]
        return {"quality_verdict": "accept" if ok else "reject", "quality_reason": rs}
    except Exception:
        return {"quality_verdict": "unknown", "quality_reason": ""}


def run_event_driven_monitor(force: bool = False) -> dict[str, Any]:
    ensure_proactive_schema()
    run_uid = start_agent_run(
        agent_name="event_driven_monitor",
        trigger_type="forced" if bool(force) else "event_driven",
        input_payload={"force": bool(force)},
    )
    con = None
    created = 0
    try:
        portfolio, watchlist, bluechips = _read_scope_tickers()
        scope = set(portfolio) | set(watchlist) | set(bluechips)
        con = _conn_core() if core_backend() != "postgres" else None
        if core_backend() == "postgres":
            con_pg = pg_connect()
            if con_pg is None:
                rf_max = 0
                fl_max = 0
            else:
                try:
                    cur = con_pg.cursor()
                    cur.execute("SELECT COALESCE(MAX(id),0) FROM report_facts_core")
                    rf_max = int((cur.fetchone() or [0])[0] or 0)
                    cur.execute("SELECT COALESCE(MAX(id),0) FROM filings_core")
                    fl_max = int((cur.fetchone() or [0])[0] or 0)
                finally:
                    con_pg.close()
        else:
            rf_max = 0
            fl_max = 0
        rf_last = int(_state_get(con, "last_report_fact_id", "0") or "0")
        fl_last = int(_state_get(con, "last_filing_id", "0") or "0")
        if not force and rf_max <= rf_last and fl_max <= fl_last:
            out = {"ok": True, "ran": False, "reason": "no_new_reports_or_filings", "created": 0}
            finish_agent_run(run_uid, "skipped", output_payload=out)
            return out

        # Phase 2: ontology ingest from newly observed facts.
        ont = ingest_ontology_from_report_facts(limit_rows=500)

        name_map = company_name_map()
        day_map = _day_pct_map()

        # Phase 3/4: build action proposals from new report facts for in-scope companies.
        throttled = False
        seen_rf_tickers: set[str] = set()
        if core_backend() == "postgres":
            con_pg = pg_connect()
            if con_pg is None:
                rf_rows = []
            else:
                try:
                    cur = con_pg.cursor()
                    cur.execute(
                        """SELECT id, ticker, fact_text, report_name, importance
                           FROM report_facts_core
                           WHERE id > %s
                           ORDER BY id DESC
                           LIMIT 240""",
                        (rf_last,),
                    )
                    rf_rows = [
                        {"id": int(r[0] or 0), "ticker": str(r[1] or ""), "fact_text": str(r[2] or ""), "report_name": str(r[3] or ""), "importance": int(r[4] or 0)}
                        for r in (cur.fetchall() or [])
                    ]
                finally:
                    con_pg.close()
        else:
            rf_rows = []
        for r in rf_rows:
            if created >= MAX_PROPOSALS_PER_MONITOR_RUN:
                throttled = True
                break
            tk = _safe_ticker(str(r["ticker"] or ""))
            if not tk or (scope and tk not in scope):
                continue
            if tk in seen_rf_tickers:
                continue
            imp = int(r["importance"] or 0)
            if imp < 7:
                continue
            if _has_open_proposal("thesis_trigger", tk) or _has_recent_proposal("thesis_trigger", tk):
                continue
            text = str(r["fact_text"] or "").strip()
            rep = str(r["report_name"] or "").strip()
            src_key = f"rf:{int(r['id'] or 0)}:{tk}"
            actx = build_analysis_context(analysis_id=src_key, source="thesis_trigger")
            title = f"Review {tk} Thesis Trigger"
            bullets = _build_thesis_bullets(tk, text, "THESIS_TRIGGER")
            cites = (
                [{"label": "Report Evidence", "url": format_citation_url(f"/reports/view?name={rep}", actx)}]
                if rep
                else [{"label": "Reports", "url": format_citation_url("/reports", actx)}]
            )
            if _proposal_upsert(
                source_event_key=src_key,
                kind="thesis_trigger",
                ticker=tk,
                title=title,
                bullets=bullets,
                citations=cites,
                confidence=min(0.99, 0.55 + (imp * 0.04)),
                priority_score=float(imp),
                execute_route=f"/company_file?t={tk}",
                execute_payload=build_execute_payload(
                    route=f"/company_file?t={tk}",
                    context=actx,
                    payload={"ticker": tk, "source": "report_facts"},
                ),
            ):
                created += 1
                seen_rf_tickers.add(tk)

        # New filing monitor (event-driven).
        if core_backend() == "postgres":
            con_pg = pg_connect()
            if con_pg is None:
                filing_rows = []
            else:
                try:
                    cur = con_pg.cursor()
                    cur.execute(
                        """SELECT id, ticker, form, date, accession
                           FROM filings_core
                           WHERE id > %s
                           ORDER BY id DESC
                           LIMIT 200""",
                        (fl_last,),
                    )
                    filing_rows = [
                        {"id": int(r[0] or 0), "ticker": str(r[1] or ""), "form": str(r[2] or ""), "date": str(r[3] or ""), "accession": str(r[4] or "")}
                        for r in (cur.fetchall() or [])
                    ]
                finally:
                    con_pg.close()
        else:
            filing_rows = []
        for r in filing_rows:
            if created >= MAX_PROPOSALS_PER_MONITOR_RUN:
                throttled = True
                break
            tk = _safe_ticker(str(r["ticker"] or ""))
            if not tk or (scope and tk not in scope):
                continue
            fm = str(r["form"] or "").upper()
            if fm not in {"8-K", "6-K", "10-Q", "10-K", "20-F", "40-F"}:
                continue
            if _has_open_proposal("filing_update", tk) or _has_recent_proposal("filing_update", tk):
                continue
            src_key = f"fil:{int(r['id'] or 0)}:{tk}:{fm}"
            actx = build_analysis_context(analysis_id=src_key, source="filing_update")
            title = f"{tk} filed {fm} — review update"
            bullets = [
                f"New official filing detected: {fm} ({str(r['date'] or '-')}).",
                "Check if this changes your core thesis, invalidation level, or sizing.",
                "Open workspace and record decision rationale before acting.",
            ]
            cites = [{"label": "SEC Filings", "url": format_citation_url(f"/company_file/sec?t={tk}", actx)}]
            if _proposal_upsert(
                source_event_key=src_key,
                kind="filing_update",
                ticker=tk,
                title=title,
                bullets=bullets,
                citations=cites,
                confidence=0.9,
                priority_score=8.5 if fm in {"8-K", "10-Q", "10-K"} else 7.5,
                execute_route=f"/company_file/sec?t={tk}",
                execute_payload=build_execute_payload(
                    route=f"/company_file/sec?t={tk}",
                    context=actx,
                    payload={"ticker": tk, "form": fm},
                ),
            ):
                created += 1

        # Contagion / peer alert from ontology relationships.
        if core_backend() == "postgres":
            con_pg_graph = pg_connect()
            if con_pg_graph is not None:
                try:
                    cur = con_pg_graph.cursor()
                    for tk in sorted(scope):
                        if created >= MAX_PROPOSALS_PER_MONITOR_RUN:
                            throttled = True
                            break
                        cur.execute(
                            "SELECT id FROM entities_core WHERE type='COMPANY' AND normalized_name=%s LIMIT 1",
                            (tk.lower(),),
                        )
                        src = cur.fetchone()
                        if not src:
                            continue
                        src_id = int((src or [0])[0] or 0)
                        if src_id <= 0:
                            continue
                        cur.execute(
                            """SELECT e2.name AS peer_name, r.relationship_type
                               FROM relationships_core r
                               JOIN entities_core e2 ON e2.id = r.target_id
                               WHERE r.source_id = %s AND e2.type='COMPANY'
                               ORDER BY r.id DESC
                               LIMIT 40""",
                            (src_id,),
                        )
                        rels = cur.fetchall() or []
                        for rr in rels:
                            if created >= MAX_PROPOSALS_PER_MONITOR_RUN:
                                throttled = True
                                break
                            peer = _safe_ticker(str(rr[0] or ""))
                            if not peer:
                                continue
                            d = _to_float(day_map.get(peer), 0.0)
                            if abs(d) < 10.0:
                                continue
                            if _has_open_proposal("contagion_peer_alert", tk) or _has_recent_proposal("contagion_peer_alert", tk):
                                continue
                            rel_type = str(rr[1] or "LINKED")
                            src_key = f"contagion:{tk}:{peer}:{dt.date.today().isoformat()}"
                            actx = build_analysis_context(analysis_id=src_key, source="contagion_peer_alert")
                            title = f"Contagion Alert: {peer} {d:+.2f}% may impact {tk}"
                            bullets = [
                                f"Linked peer {peer} moved {d:+.2f}% today.",
                                f"Relationship in ontology: {rel_type}.",
                                f"Re-check {tk} thesis assumptions and cross-company risk transmission.",
                            ]
                            cites = [
                                {"label": "Company Workspace", "url": format_citation_url(f"/company_file?t={tk}", actx)},
                                {"label": f"{peer} Workspace", "url": format_citation_url(f"/company_file?t={peer}", actx)},
                            ]
                            if _proposal_upsert(
                                source_event_key=src_key,
                                kind="contagion_peer_alert",
                                ticker=tk,
                                title=title,
                                bullets=bullets,
                                citations=cites,
                                confidence=0.88,
                                priority_score=9.2 + min(3.0, abs(d) / 10.0),
                                execute_route=f"/company_file?t={tk}",
                                execute_payload=build_execute_payload(
                                    route=f"/company_file?t={tk}",
                                    context=actx,
                                    payload={"ticker": tk, "peer": peer, "day_pct": d},
                                ),
                            ):
                                created += 1
                finally:
                    con_pg_graph.close()
        else:
            con_onyx = _conn_onyx()
            try:
                for tk in sorted(scope):
                    if created >= MAX_PROPOSALS_PER_MONITOR_RUN:
                        throttled = True
                        break
                    src = con_onyx.execute(
                        "SELECT id FROM entities WHERE type='COMPANY' AND normalized_name = ? LIMIT 1",
                        (tk.lower(),),
                    ).fetchone()
                    if not src:
                        continue
                    rels = con_onyx.execute(
                        """SELECT e2.name AS peer_name, r.relationship_type
                           FROM relationships r
                           JOIN entities e2 ON e2.id = r.target_id
                           WHERE r.source_id = ? AND e2.type='COMPANY'
                           ORDER BY r.id DESC
                           LIMIT 40""",
                        (int(src["id"]),),
                    ).fetchall()
                    for rr in rels:
                        if created >= MAX_PROPOSALS_PER_MONITOR_RUN:
                            throttled = True
                            break
                        peer = _safe_ticker(str(rr["peer_name"] or ""))
                        if not peer:
                            continue
                        d = _to_float(day_map.get(peer), 0.0)
                        if abs(d) < 10.0:
                            continue
                        if _has_open_proposal("contagion_peer_alert", tk) or _has_recent_proposal("contagion_peer_alert", tk):
                            continue
                        rel_type = str(rr["relationship_type"] or "LINKED")
                        src_key = f"contagion:{tk}:{peer}:{dt.date.today().isoformat()}"
                        actx = build_analysis_context(analysis_id=src_key, source="contagion_peer_alert")
                        title = f"Contagion Alert: {peer} {d:+.2f}% may impact {tk}"
                        bullets = [
                            f"Linked peer {peer} moved {d:+.2f}% today.",
                            f"Relationship in ontology: {rel_type}.",
                            f"Re-check {tk} thesis assumptions and cross-company risk transmission.",
                        ]
                        cites = [
                            {"label": "Company Workspace", "url": format_citation_url(f"/company_file?t={tk}", actx)},
                            {"label": f"{peer} Workspace", "url": format_citation_url(f"/company_file?t={peer}", actx)},
                        ]
                        if _proposal_upsert(
                            source_event_key=src_key,
                            kind="contagion_peer_alert",
                            ticker=tk,
                            title=title,
                            bullets=bullets,
                            citations=cites,
                            confidence=0.88,
                            priority_score=9.2 + min(3.0, abs(d) / 10.0),
                            execute_route=f"/company_file?t={tk}",
                            execute_payload=build_execute_payload(
                                route=f"/company_file?t={tk}",
                                context=actx,
                                payload={"ticker": tk, "peer": peer, "day_pct": d},
                            ),
                        ):
                            created += 1
            finally:
                con_onyx.close()

        _state_set(con, "last_report_fact_id", str(rf_max))
        _state_set(con, "last_filing_id", str(fl_max))
        if con is not None:
            con.commit()
        out = {
            "ok": True,
            "ran": True,
            "created": created,
            "throttled": throttled,
            "max_created_per_run": MAX_PROPOSALS_PER_MONITOR_RUN,
            "cooldown_hours": PROPOSAL_COOLDOWN_HOURS,
            "monitor": {"report_fact_max": rf_max, "filing_max": fl_max},
            "ontology": ont,
        }
        finish_agent_run(run_uid, "ok", output_payload=out)
        return out
    except Exception as exc:
        finish_agent_run(run_uid, "failed", output_payload={"ok": False}, error_text=str(exc))
        raise
    finally:
        if con is not None:
            con.close()


def list_action_proposals(status: str = "open", limit: int = 8) -> list[dict[str, Any]]:
    ensure_proactive_schema()
    if core_backend() != "postgres":
        return []
    st = str(status or "open").strip().lower()
    blue_chip_set: set[str] = set()
    con_pg = pg_connect()
    if con_pg is not None:
        try:
            cur = con_pg.cursor()
            cur.execute("SELECT to_regclass('public.blue_chips_core')")
            if (cur.fetchone() or [None])[0]:
                cur.execute("SELECT ticker FROM blue_chips_core")
                for r in cur.fetchall() or []:
                    tk = _safe_ticker(str(r[0] or ""))
                    if tk:
                        blue_chip_set.add(tk)
        except Exception:
            blue_chip_set = set()
        finally:
            con_pg.close()

    name_map = company_name_map()
    weight_map = _portfolio_weight_map()
    lim = max(1, min(250, int(limit or 8) * 12))
    rows = list_action_proposals_pg(status=st, limit=lim)
    raw_items: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []
    def _to_list(val: Any) -> list[Any]:
        if isinstance(val, list):
            return list(val)
        if isinstance(val, tuple):
            return list(val)
        if isinstance(val, dict):
            return [dict(val)]
        try:
            return list(json.loads(str(val or "[]")))
        except Exception:
            return []
    def _to_dict(val: Any) -> dict[str, Any]:
        if isinstance(val, dict):
            return dict(val)
        try:
            obj = json.loads(str(val or "{}"))
            return dict(obj) if isinstance(obj, dict) else {}
        except Exception:
            return {}
    if not rows:
        return []

    for r in rows:
        bullets = _to_list(r["thesis_json"])
        cites = _to_list(r["citations_json"])
        source_event_key = str(r["source_event_key"] or "")
        actx = build_analysis_context(
            analysis_id=source_event_key,
            source=str(r["kind"] or ""),
            trace_id=f"ap_{int(r['id'] or 0)}",
        )
        reasoning_raw = _to_dict(r["reasoning_json"])
        pipeline = run_proposal_pipeline(
            relevance_ok=True,
            raw_reasoning=reasoning_raw,
            citations=cites,
            confidence=r.get("confidence"),
            priority_score=r.get("priority_score"),
            verify_result=None,
        )
        insights = [x for x in list(pipeline.get("insights") or []) if isinstance(x, dict)]
        reasoning = dict(pipeline.get("reasoning") or {})
        cites_norm: list[dict[str, str]] = []
        for c in list(pipeline.get("citations") or []):
            if not isinstance(c, dict):
                continue
            lb = str(c.get("label") or "Source").strip()
            url = format_citation_url(str(c.get("url") or "").strip(), actx)
            if url:
                cites_norm.append({"label": lb or "Source", "url": url})
        raw_items.append(
            {
                "id": int(r["id"] or 0),
                "created_at": str(r["created_at"] or ""),
                "status": str(r["status"] or ""),
                "kind": str(r["kind"] or ""),
                "ticker": str(r["ticker"] or ""),
                "title": str(r["title"] or ""),
                "bullets": [str(x) for x in bullets[:3]],
                "citations": cites_norm[:6],
                "insights": [x for x in insights[:3] if isinstance(x, dict)],
                "reasoning": reasoning if isinstance(reasoning, dict) else {},
                "confidence": float(pipeline.get("confidence") or 0.0),
                "priority_score": float(pipeline.get("priority_score") or 0.0),
                "execute_route": str(r["execute_route"] or ""),
                "source_event_key": source_event_key,
                "analysis_context": actx.to_dict(),
                "is_blue_chip": str(r["ticker"] or "").strip().upper() in blue_chip_set,
            }
        )

        # Consolidate alert fatigue: merge multiple open signals per ticker into one card.
        grouped: dict[str, list[dict[str, Any]]] = {}
        for p in raw_items:
            tk = _safe_ticker(str(p.get("ticker") or ""))
            key = tk or f"__id_{int(p.get('id') or 0)}"
            grouped.setdefault(key, []).append(p)

        def _reasoning_strength(item: dict[str, Any]) -> tuple[int, int]:
            rz = dict(item.get("reasoning") or {})
            score = 0
            for k in ("margin_impact", "thesis_validation", "risk_assessment", "actionable_proposal", "peer_contagion"):
                if str(rz.get(k) or "").strip():
                    score += 2
            if str(rz.get("recommended_stance") or "").strip():
                score += 1
            ins_count = len([x for x in list(item.get("insights") or []) if isinstance(x, dict)])
            score += min(ins_count, 3)
            return (score, int(item.get("id") or 0))

        for key, items in grouped.items():
            items_sorted = sorted(items, key=lambda x: float(x.get("priority_score") or 0.0), reverse=True)
            base = dict(items_sorted[0])
            best_reasoning_item = max(items_sorted, key=_reasoning_strength)
            tk = _safe_ticker(str(base.get("ticker") or ""))
            if tk and len(items_sorted) > 1:
                kinds = [str(x.get("kind") or "").strip() for x in items_sorted if str(x.get("kind") or "").strip()]
                uniq_kinds: list[str] = []
                for k in kinds:
                    if k not in uniq_kinds:
                        uniq_kinds.append(k)
                merged_bullets: list[str] = []
                for x in items_sorted[:6]:
                    for b in list(x.get("bullets") or []):
                        bb = str(b or "").strip()
                        if bb and bb not in merged_bullets:
                            merged_bullets.append(bb)
                        if len(merged_bullets) >= 3:
                            break
                    if len(merged_bullets) >= 3:
                        break
                if not merged_bullets:
                    merged_bullets = list(base.get("bullets") or [])
                base["bullets"] = merged_bullets[:3]
                base["kind"] = "multi_signal" if len(set(uniq_kinds)) > 1 else (uniq_kinds[0] if uniq_kinds else str(base.get("kind") or ""))
                base["priority_score"] = min(99.9, float(base.get("priority_score") or 0.0) + min(1.5, 0.35 * (len(items_sorted) - 1)))
                base["confidence"] = max(float(x.get("confidence") or 0.0) for x in items_sorted)
                base["signal_count"] = len(items_sorted)
                base["title"] = f"{len(items_sorted)} macro/filing signals consolidated"
                merged_cites: list[dict[str, str]] = []
                seen_urls: set[str] = set()
                for x in items_sorted[:8]:
                    for c in list(x.get("citations") or []):
                        if not isinstance(c, dict):
                            continue
                        u = str(c.get("url") or "").strip()
                        if u and u not in seen_urls:
                            seen_urls.add(u)
                            merged_cites.append({"label": str(c.get("label") or "Source"), "url": u})
                if merged_cites:
                    base["citations"] = merged_cites[:8]
                base["kind_details"] = ", ".join(uniq_kinds[:4])
            # Preserve card ordering by priority, but always render the richest AI reasoning.
            merged_reasoning: dict[str, Any] = {}
            reasoning_items = sorted(items_sorted, key=_reasoning_strength, reverse=True)
            for ritem in reasoning_items:
                rz = dict(ritem.get("reasoning") or {})
                if not isinstance(rz, dict):
                    continue
                for k, v in rz.items():
                    if k in merged_reasoning and str(merged_reasoning.get(k) or "").strip():
                        continue
                    if isinstance(v, list):
                        if v:
                            merged_reasoning[k] = v
                    elif str(v or "").strip():
                        merged_reasoning[k] = v
            merged_insights: list[dict[str, str]] = []
            seen_pairs: set[str] = set()
            for iitem in reasoning_items:
                for ins in list(iitem.get("insights") or []):
                    if not isinstance(ins, dict):
                        continue
                    lb = str(ins.get("label") or "").strip()
                    tx = str(ins.get("text") or "").strip()
                    key_ins = f"{lb}|{tx}"
                    if not lb or not tx or key_ins in seen_pairs:
                        continue
                    seen_pairs.add(key_ins)
                    merged_insights.append({"label": lb[:42], "text": tx[:320]})
                    if len(merged_insights) >= 3:
                        break
                if len(merged_insights) >= 3:
                    break
            base["reasoning"] = merged_reasoning or dict(best_reasoning_item.get("reasoning") or {})
            base["insights"] = merged_insights[:3]
            out.append(base)

        for p in out:
            kind = str(p.get("kind") or "").strip().lower()
            is_blue = bool(p.get("is_blue_chip"))
            is_macro_signal = bool(
                kind in {"macro_signal", "macro_context", "contagion_peer_alert"}
                or kind.startswith("macro_")
                or "macro" in kind
            )
            tk = _safe_ticker(str(p.get("ticker") or ""))
            nm = str(name_map.get(tk) or "").strip()
            w = float(weight_map.get(tk, 0.0) or 0.0)
            rz0 = dict(p.get("reasoning") or {})
            direction = str(rz0.get("recommended_stance") or "").strip().upper() or "REVIEW"
            p["direction"] = direction
            p["company_name"] = nm
            p["portfolio_weight_pct"] = w
            # Headline clarity.
            base_title = str(p.get("title") or "").strip()
            if tk:
                bt = base_title
                if bt.upper().startswith((tk + " ·").upper()):
                    bt = bt[len(tk) + 2 :].strip()
                p["title"] = f"{tk} · {direction} · {bt}"
            bs = [str(x or "").strip() for x in list(p.get("bullets") or []) if str(x or "").strip()]
            rz = dict(p.get("reasoning") or {})
            ins = [x for x in list(p.get("insights") or []) if isinstance(x, dict)]
            if not ins:
                ins = insights_from_reasoning(rz)
            p["insights"] = ins[:3]
            p["reasoning"] = rz
            p["bullets"] = bs[:3]

            if is_macro_signal:
                p["badge_variant"] = "macro_signal"
                p["badge_label"] = "📡 Macro Signal"
            elif is_blue:
                p["badge_variant"] = "blue_chip"
                p["badge_label"] = "💎 Blue Chip"
            else:
                p["badge_variant"] = ""
                p["badge_label"] = ""
        # Only surface high-quality, reasoning-backed cards.
        out = [
            p for p in out
            if _reasoning_strength(p)[0] > 0
            and passes_reasoning_quality(
                dict(p.get("reasoning") or {}),
                [x for x in list(p.get("insights") or []) if isinstance(x, dict)],
                [x for x in list(p.get("citations") or []) if isinstance(x, dict)],
            )
        ]
    out_sorted = sorted(out, key=lambda x: float(x.get("priority_score") or 0.0), reverse=True)
    return out_sorted[: max(1, min(50, int(limit or 8)))]


def dismiss_action_proposal(proposal_id: int, reason: str = "") -> bool:
    ensure_proactive_schema()
    pid = int(proposal_id or 0)
    if pid <= 0:
        return False
    if core_backend() != "postgres":
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        now = dt.datetime.now().isoformat()
        cur.execute(
            "UPDATE action_proposals_core SET status='dismissed', updated_at=%s, dismissed_reason=%s WHERE id=%s",
            (now, str(reason or "")[:240], pid),
        )
        changed = int(cur.rowcount or 0) > 0
        con.commit()
        if changed:
            row = _pg_get_action_proposal_row(pid)
            if row:
                mirror_action_proposal(
                    {
                        "source_event_key": str(row.get("source_event_key") or ""),
                        "proposal_id": int(row.get("id") or 0),
                        "ticker": str(row.get("ticker") or ""),
                        "kind": str(row.get("kind") or ""),
                        "status": str(row.get("status") or "open"),
                        "title": str(row.get("title") or ""),
                        "insights_json": list(row.get("insights_json") or []),
                        "reasoning_json": dict(row.get("reasoning_json") or {}),
                        "created_at": str(row.get("created_at") or ""),
                        "updated_at": str(row.get("updated_at") or ""),
                    }
                )
                try:
                    from app.services.postgres_core_service import add_agent_feedback_memory_pg
                    ticker = str(row.get("ticker") or "").strip().upper()
                    kind = str(row.get("kind") or "").strip()
                    title = str(row.get("title") or "").strip()
                    add_agent_feedback_memory_pg(
                        ticker,
                        f"[User feedback] Dismissed {kind} proposal: '{title[:80]}'. "
                        f"Reason: {str(reason or 'not provided')[:120]}. "
                        f"Avoid repeating this type without stronger evidence."
                    )
                except Exception:
                    pass
        return changed
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def reject_action_proposal(proposal_id: int, reason: str = "") -> bool:
    ensure_proactive_schema()
    pid = int(proposal_id or 0)
    if pid <= 0:
        return False
    rs = str(reason or "").strip()[:500]
    if core_backend() != "postgres":
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        now = dt.datetime.now().isoformat()
        cur.execute(
            "UPDATE action_proposals_core SET status='rejected', updated_at=%s, rejection_reason=%s, rejected_at=%s WHERE id=%s",
            (now, rs, now, pid),
        )
        changed = int(cur.rowcount or 0) > 0
        con.commit()
        if changed:
            row = _pg_get_action_proposal_row(pid)
            if row:
                mirror_action_proposal(
                    {
                        "source_event_key": str(row.get("source_event_key") or ""),
                        "proposal_id": int(row.get("id") or 0),
                        "ticker": str(row.get("ticker") or ""),
                        "kind": str(row.get("kind") or ""),
                        "status": str(row.get("status") or "open"),
                        "title": str(row.get("title") or ""),
                        "insights_json": list(row.get("insights_json") or []),
                        "reasoning_json": dict(row.get("reasoning_json") or {}),
                        "created_at": str(row.get("created_at") or ""),
                        "updated_at": str(row.get("updated_at") or ""),
                    }
                )
        return changed
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def _upsert_onyx_preference(preference_key: str, preference_value: str, context_reason: str, source_ref: str = "reject_feedback") -> bool:
    ensure_proactive_schema()
    k = str(preference_key or "").strip().lower()[:120]
    v = str(preference_value or "").strip()[:1200]
    c = str(context_reason or "").strip()[:1200]
    s = str(source_ref or "reject_feedback").strip()[:120]
    if not k or not v:
        return False
    if core_backend() == "postgres":
        return bool(upsert_user_preference(pref_key=k, pref_value=v, source=s))
    now = dt.datetime.now().isoformat()
    con = _conn_onyx()
    try:
        row = con.execute("SELECT id FROM user_preferences WHERE preference_key=? LIMIT 1", (k,)).fetchone()
        if row:
            con.execute(
                "UPDATE user_preferences SET preference_value=?, context_reason=?, source_ref=?, updated_at=? WHERE preference_key=?",
                (v, c, s, now, k),
            )
        else:
            con.execute(
                """INSERT INTO user_preferences
                   (preference_key, preference_value, context_reason, source_ref, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (k, v, c, s, now, now),
            )
        con.commit()
        return True
    finally:
        con.close()


def learn_from_rejection(proposal_id: int, reason: str = "") -> dict[str, Any]:
    ensure_proactive_schema()
    pid = int(proposal_id or 0)
    rs = str(reason or "").strip()
    if pid <= 0:
        return {"ok": False, "error": "invalid_id"}
    if core_backend() != "postgres":
        return {"ok": False, "error": "backend_not_postgres"}
    row = _pg_get_action_proposal_row(pid)
    if not row:
        return {"ok": False, "error": "proposal_not_found"}

    ticker = _safe_ticker(str(row.get("ticker") or ""))
    kind = str(row.get("kind") or "").strip()
    title = str(row.get("title") or "").strip()
    bullets = [str(x or "").strip() for x in list(row.get("thesis_json") or []) if str(x or "").strip()]
    evidence = " | ".join([str(x).strip() for x in bullets if str(x).strip()])[:1600]

    pref_key = f"reject_rule_{kind or 'proposal'}"
    pref_value = f"Avoid repeating {kind or 'proposal'} style for {ticker or 'this ticker'} without stronger evidence."
    context_reason = f"Rejected proposal {pid}: {title}. Reason: {rs or '-'}"

    if ask_ai is not None and rs:
        try:
            prompt = (
                "Extract one durable investor preference rule from this rejection.\n"
                "Return strict JSON: "
                '{"preference_key":"...","preference_value":"...","risk_profile_delta":"...","confidence_score":0}\n'
                f"Ticker: {ticker}\nProposal kind: {kind}\nProposal title: {title}\nEvidence: {evidence}\n"
                f"User rejection reason: {rs}"
            )
            raw = str(ask_ai(prompt, "Investment preference extractor. JSON only.", mode="smart", json_mode=True, temperature=0.1) or "").strip()
            obj = json.loads(raw)
            pref_key = str(obj.get("preference_key") or pref_key).strip().lower()[:120] or pref_key
            pref_value = str(obj.get("preference_value") or pref_value).strip()[:1200] or pref_value
            risk_delta = str(obj.get("risk_profile_delta") or "").strip()
            if risk_delta:
                _upsert_onyx_preference("risk_profile_delta", risk_delta, context_reason=context_reason, source_ref=f"proposal:{pid}")
        except Exception:
            pass

    saved_onyx = _upsert_onyx_preference(pref_key, pref_value, context_reason=context_reason, source_ref=f"proposal:{pid}")
    saved_core = upsert_user_preference(pref_key=pref_key, pref_value=pref_value, source=f"proposal_reject:{pid}")
    return {"ok": True, "saved_onyx": bool(saved_onyx), "saved_core": bool(saved_core), "preference_key": pref_key}


def _event_seed_entities(event_description: str) -> list[int]:
    seeds: list[int] = []
    text = str(event_description or "").strip()
    low = text.lower()
    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is not None:
            try:
                cur = con_pg.cursor()
                # 1) direct ticker entities
                for tk in sorted(set(_extract_peer_tickers(text, ""))):
                    cur.execute(
                        "SELECT id FROM entities_core WHERE type='COMPANY' AND normalized_name=%s LIMIT 1",
                        (tk.lower(),),
                    )
                    row = cur.fetchone()
                    if row:
                        seeds.append(int((row or [0])[0] or 0))
                # 2) theme keyword match
                theme_keys = [k.lower() for k in _extract_themes(text)]
                for k in theme_keys:
                    cur.execute(
                        "SELECT id FROM entities_core WHERE type='RISK_THEME' AND normalized_name=%s LIMIT 1",
                        (k.lower(),),
                    )
                    row = cur.fetchone()
                    if row:
                        seeds.append(int((row or [0])[0] or 0))
                # 3) free-text on risk theme names
                cur.execute("SELECT id, normalized_name FROM entities_core WHERE type='RISK_THEME' ORDER BY id DESC LIMIT 200")
                rows = cur.fetchall() or []
                for r in rows:
                    nm = str(r[1] or "")
                    if nm and nm.replace("_", " ") in low:
                        seeds.append(int(r[0] or 0))
            finally:
                con_pg.close()
    else:
        con = _conn_onyx()
        try:
            # 1) direct ticker entities
            for tk in sorted(set(_extract_peer_tickers(text, ""))):
                row = con.execute(
                    "SELECT id FROM entities WHERE type='COMPANY' AND normalized_name=? LIMIT 1",
                    (tk.lower(),),
                ).fetchone()
                if row:
                    seeds.append(int(row["id"] or 0))
            # 2) theme keyword match
            theme_keys = [k.lower() for k in _extract_themes(text)]
            for k in theme_keys:
                row = con.execute(
                    "SELECT id FROM entities WHERE type='RISK_THEME' AND normalized_name=? LIMIT 1",
                    (k.lower(),),
                ).fetchone()
                if row:
                    seeds.append(int(row["id"] or 0))
            # 3) free-text on risk theme names
            rows = con.execute(
                "SELECT id, normalized_name FROM entities WHERE type='RISK_THEME' ORDER BY id DESC LIMIT 200"
            ).fetchall()
            for r in rows:
                nm = str(r["normalized_name"] or "")
                if nm and nm.replace("_", " ") in low:
                    seeds.append(int(r["id"] or 0))
        finally:
            con.close()
    out: list[int] = []
    seen: set[int] = set()
    for s in seeds:
        if s > 0 and s not in seen:
            seen.add(s)
            out.append(s)
    return out[:24]


def _scope_company_ids() -> tuple[set[int], dict[int, str]]:
    portfolio, watchlist, bluechips = _read_scope_tickers()
    scope = sorted(set(portfolio) | set(watchlist) | set(bluechips))
    out_ids: set[int] = set()
    id_to_ticker: dict[int, str] = {}
    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is not None:
            try:
                cur = con_pg.cursor()
                for tk in scope:
                    cur.execute(
                        "SELECT id FROM entities_core WHERE type='COMPANY' AND normalized_name=%s LIMIT 1",
                        (tk.lower(),),
                    )
                    row = cur.fetchone()
                    if row:
                        i = int((row or [0])[0] or 0)
                        if i > 0:
                            out_ids.add(i)
                            id_to_ticker[i] = tk
            finally:
                con_pg.close()
    else:
        con = _conn_onyx()
        try:
            for tk in scope:
                row = con.execute(
                    "SELECT id FROM entities WHERE type='COMPANY' AND normalized_name=? LIMIT 1",
                    (tk.lower(),),
                ).fetchone()
                if row:
                    i = int(row["id"] or 0)
                    if i > 0:
                        out_ids.add(i)
                        id_to_ticker[i] = tk
        finally:
            con.close()
    return out_ids, id_to_ticker


def _infer_impact_directions(event_description: str, impact_rows: list[dict[str, Any]]) -> dict[str, str]:
    """
    Non-hardcoded direction inference from event + graph evidence.
    Uses LLM classification with a strict schema and falls back to 'uncertain'.
    """
    out: dict[str, str] = {}
    if not impact_rows:
        return out
    if ask_ai is None:
        return out
    try:
        payload_rows: list[dict[str, Any]] = []
        for r in impact_rows[:10]:
            tk = _safe_ticker(str(r.get("ticker") or ""))
            if not tk:
                continue
            payload_rows.append(
                {
                    "ticker": tk,
                    "order": str(r.get("order") or ""),
                    "impact_score": float(r.get("impact_score") or 0.0),
                    "why": str(r.get("why") or "")[:220],
                }
            )
        if not payload_rows:
            return out
        prompt = (
            "Given scenario and evidence per ticker, classify likely direction for each ticker.\n"
            "Return strict JSON only:\n"
            '{"items":[{"ticker":"CRM","direction":"bullish|bearish|mixed|uncertain"}]}\n'
            "Do not hallucinate certainty. If unclear, use uncertain.\n"
            f"Scenario: {event_description}\n"
            f"Rows: {json.dumps(payload_rows, ensure_ascii=False)}"
        )
        raw = str(ask_ai(prompt, "Macro risk classifier. JSON only.", mode="smart", json_mode=True, temperature=0.0) or "").strip()
        obj = json.loads(raw) if raw else {}
        items = list((obj or {}).get("items") or [])
        for it in items:
            tk = _safe_ticker(str((it or {}).get("ticker") or ""))
            d = str((it or {}).get("direction") or "").strip().lower()
            if tk and d in {"bullish", "bearish", "mixed", "uncertain"}:
                out[tk] = d
    except Exception:
        return {}
    return out


def simulate_macro_shock(event_description: str, max_depth: int = SIM_MAX_DEPTH) -> dict[str, Any]:
    """
    Palantir-style what-if simulation over ontology graph.
    Returns structured output with confidence and missing variables.
    """
    ensure_proactive_schema()
    evt = str(event_description or "").strip()
    if not evt:
        return {
            "answer": "No event description provided.",
            "confidence_score": 20,
            "missing_variables": ["event_description"],
            "citations": [],
            "impacts": [],
        }
    seeds = _event_seed_entities(evt)
    scope_ids, id_to_ticker = _scope_company_ids()
    if not seeds:
        return {
            "answer": "I could not map the event to known ontology entities yet.",
            "confidence_score": 38,
            "missing_variables": ["Mapped themes or companies linked to this event in ontology graph"],
            "citations": [],
            "impacts": [],
        }
    if not scope_ids:
        return {
            "answer": "No portfolio/watchlist scope available for simulation.",
            "confidence_score": 30,
            "missing_variables": ["Scope tickers in portfolio/watchlist and corresponding ontology entities"],
            "citations": [],
            "impacts": [],
        }

    impact_scores: dict[int, float] = {}
    impact_depth: dict[int, int] = {}
    citations: set[str] = set()
    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is not None:
            try:
                cur = con_pg.cursor()
                for seed in seeds:
                    cur.execute(
                        """
                        WITH RECURSIVE walk(node_id, depth, score, path) AS (
                          SELECT %s::bigint, 0, 1.0::double precision, ',' || CAST(%s AS TEXT) || ','
                          UNION ALL
                          SELECT
                            CASE WHEN r.source_id = walk.node_id THEN r.target_id ELSE r.source_id END AS next_id,
                            walk.depth + 1,
                            walk.score * COALESCE(NULLIF(r.confidence_score, 0), NULLIF(r.confidence, 0), 0.65) *
                              CASE
                                WHEN walk.depth = 0 THEN 0.92
                                WHEN walk.depth = 1 THEN 0.80
                                ELSE 0.68
                              END,
                            walk.path || CAST((CASE WHEN r.source_id = walk.node_id THEN r.target_id ELSE r.source_id END) AS TEXT) || ','
                          FROM relationships_core r
                          JOIN walk ON (r.source_id = walk.node_id OR r.target_id = walk.node_id)
                          WHERE walk.depth < %s
                            AND walk.score > 0.03
                            AND POSITION(
                              ',' || CAST((CASE WHEN r.source_id = walk.node_id THEN r.target_id ELSE r.source_id END) AS TEXT) || ','
                              IN walk.path
                            ) = 0
                        )
                        SELECT node_id, MIN(depth) AS min_depth, MAX(score) AS best_score
                        FROM walk
                        GROUP BY node_id
                        """,
                        (int(seed), int(seed), max(1, min(4, int(max_depth or SIM_MAX_DEPTH)))),
                    )
                    rows = cur.fetchall() or []
                    for r in rows:
                        node = int(r[0] or 0)
                        if node not in scope_ids:
                            continue
                        sc = float(r[2] or 0.0)
                        dp = int(r[1] or 0)
                        if sc <= 0:
                            continue
                        impact_scores[node] = max(sc, impact_scores.get(node, 0.0))
                        if node not in impact_depth:
                            impact_depth[node] = dp
                        else:
                            impact_depth[node] = min(impact_depth[node], dp)

                if impact_scores:
                    ids = list(impact_scores.keys())
                    cur.execute(
                        """SELECT DISTINCT citation_link
                           FROM relationships_core
                           WHERE source_id = ANY(%s) OR target_id = ANY(%s)
                           ORDER BY citation_link DESC
                           LIMIT 24""",
                        (ids, ids),
                    )
                    crows = cur.fetchall() or []
                    for r in crows:
                        u = str(r[0] or "").strip()
                        if u:
                            citations.add(u)
            finally:
                con_pg.close()
    else:
        con = _conn_onyx()
        try:
            for seed in seeds:
                rows = con.execute(
                    """
                    WITH RECURSIVE walk(node_id, depth, score, path) AS (
                      SELECT ?, 0, 1.0, ',' || CAST(? AS TEXT) || ','
                      UNION ALL
                      SELECT
                        CASE WHEN r.source_id = walk.node_id THEN r.target_id ELSE r.source_id END AS next_id,
                        walk.depth + 1,
                        walk.score * COALESCE(NULLIF(r.confidence_score, 0), NULLIF(r.confidence, 0), 0.65) *
                          CASE
                            WHEN walk.depth = 0 THEN 0.92
                            WHEN walk.depth = 1 THEN 0.80
                            ELSE 0.68
                          END,
                        walk.path || CAST((CASE WHEN r.source_id = walk.node_id THEN r.target_id ELSE r.source_id END) AS TEXT) || ','
                      FROM relationships r
                      JOIN walk ON (r.source_id = walk.node_id OR r.target_id = walk.node_id)
                      WHERE walk.depth < ?
                        AND walk.score > 0.03
                        AND INSTR(
                          walk.path,
                          ',' || CAST((CASE WHEN r.source_id = walk.node_id THEN r.target_id ELSE r.source_id END) AS TEXT) || ','
                        ) = 0
                    )
                    SELECT node_id, MIN(depth) AS min_depth, MAX(score) AS best_score
                    FROM walk
                    GROUP BY node_id
                    """,
                    (int(seed), int(seed), max(1, min(4, int(max_depth or SIM_MAX_DEPTH)))),
                ).fetchall()
                for r in rows:
                    node = int(r["node_id"] or 0)
                    if node not in scope_ids:
                        continue
                    sc = float(r["best_score"] or 0.0)
                    dp = int(r["min_depth"] or 0)
                    if sc <= 0:
                        continue
                    impact_scores[node] = max(sc, impact_scores.get(node, 0.0))
                    if node not in impact_depth:
                        impact_depth[node] = dp
                    else:
                        impact_depth[node] = min(impact_depth[node], dp)

            if impact_scores:
                marks = ",".join("?" for _ in impact_scores)
                crows = con.execute(
                    f"""SELECT DISTINCT citation_link
                        FROM relationships
                        WHERE source_id IN ({marks}) OR target_id IN ({marks})
                        ORDER BY id DESC
                        LIMIT 24""",
                    tuple(list(impact_scores.keys()) + list(impact_scores.keys())),
                ).fetchall()
                for r in crows:
                    u = str(r["citation_link"] or "").strip()
                    if u:
                        citations.add(u)
        finally:
            con.close()

    impacts: list[dict[str, Any]] = []
    evidence_map: dict[int, dict[str, Any]] = {}
    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is not None:
            try:
                cur = con_pg.cursor()
                for cid in impact_scores.keys():
                    cur.execute(
                        """
                        SELECT
                          r.relationship_type,
                          r.citation_link,
                          r.citation_text,
                          COALESCE(r.confidence_score, r.confidence, 0.0) AS conf,
                          e1.id AS s_id, e1.name AS s_name, e1.type AS s_type,
                          e2.id AS t_id, e2.name AS t_name, e2.type AS t_type
                        FROM relationships_core r
                        JOIN entities_core e1 ON e1.id = r.source_id
                        JOIN entities_core e2 ON e2.id = r.target_id
                        WHERE r.source_id = %s OR r.target_id = %s
                        ORDER BY conf DESC, r.id DESC
                        LIMIT 60
                        """,
                        (int(cid), int(cid)),
                    )
                    rows = cur.fetchall() or []
                    sample: list[str] = []
                    cites_local: list[str] = []
                    for rr in rows:
                        s_id = int(rr[4] or 0)
                        other_name = str(rr[7] if s_id == cid else rr[5] or "").strip()
                        other_type = str(rr[8] if s_id == cid else rr[6] or "").strip().upper()
                        rel = str(rr[0] or "").strip().upper()
                        ctext = str(rr[2] or "").strip()
                        cu = str(rr[1] or "").strip()
                        if cu:
                            cites_local.append(cu)
                        if len(sample) < 3 and (other_type in {"RISK_THEME", "COMPANY"}):
                            why = f"{rel} -> {other_name}" if other_name else rel
                            if ctext:
                                why += f" ({ctext[:100]})"
                            sample.append(why)
                    evidence_map[cid] = {
                        "why": "; ".join(sample[:3]),
                        "citations": list(dict.fromkeys([x for x in cites_local if x]))[:3],
                    }
            finally:
                con_pg.close()
    else:
        con = _conn_onyx()
        try:
            for cid in impact_scores.keys():
                rows = con.execute(
                    """
                    SELECT
                      r.relationship_type,
                      r.citation_link,
                      r.citation_text,
                      COALESCE(r.confidence_score, r.confidence, 0.0) AS conf,
                      e1.id AS s_id, e1.name AS s_name, e1.type AS s_type,
                      e2.id AS t_id, e2.name AS t_name, e2.type AS t_type
                    FROM relationships r
                    JOIN entities e1 ON e1.id = r.source_id
                    JOIN entities e2 ON e2.id = r.target_id
                    WHERE r.source_id = ? OR r.target_id = ?
                    ORDER BY conf DESC, r.id DESC
                    LIMIT 60
                    """,
                    (int(cid), int(cid)),
                ).fetchall()
                sample: list[str] = []
                cites_local: list[str] = []
                for rr in rows:
                    s_id = int(rr["s_id"] or 0)
                    other_name = str(rr["t_name"] if s_id == cid else rr["s_name"] or "").strip()
                    other_type = str(rr["t_type"] if s_id == cid else rr["s_type"] or "").strip().upper()
                    rel = str(rr["relationship_type"] or "").strip().upper()
                    ctext = str(rr["citation_text"] or "").strip()
                    cu = str(rr["citation_link"] or "").strip()
                    if cu:
                        cites_local.append(cu)
                    if len(sample) < 3 and (other_type in {"RISK_THEME", "COMPANY"}):
                        why = f"{rel} -> {other_name}" if other_name else rel
                        if ctext:
                            why += f" ({ctext[:100]})"
                        sample.append(why)
                evidence_map[cid] = {
                    "why": "; ".join(sample[:3]),
                    "citations": list(dict.fromkeys([x for x in cites_local if x]))[:3],
                }
        finally:
            con.close()

    for cid, score in sorted(impact_scores.items(), key=lambda kv: kv[1], reverse=True):
        depth = int(impact_depth.get(cid, 3))
        ev = evidence_map.get(cid) if isinstance(evidence_map.get(cid), dict) else {}
        impacts.append(
            {
                "ticker": id_to_ticker.get(cid, ""),
                "impact_score": round(float(score), 4),
                "order": "direct" if depth <= 1 else ("second_order" if depth == 2 else "third_order"),
                "min_hops": depth,
                "why": str((ev or {}).get("why") or "").strip(),
                "citations": list((ev or {}).get("citations") or []),
            }
        )
    impacts = impacts[:15]

    directions = _infer_impact_directions(evt, impacts)
    for it in impacts:
        tk = _safe_ticker(str((it or {}).get("ticker") or ""))
        it["likely_direction"] = directions.get(tk, "uncertain")

    missing: list[str] = []
    if len(citations) < 3:
        missing.append("More cited filings/reports linking event themes to holdings")
    if not impacts:
        missing.append("Graph links from event seeds to current holdings")
    if len(seeds) < 2:
        missing.append("Broader event decomposition into macro themes")

    coverage = min(1.0, len(impacts) / 8.0)
    citation_factor = min(1.0, len(citations) / 10.0)
    seed_factor = min(1.0, len(seeds) / 5.0)
    conf = int(round(max(20.0, min(96.0, 35.0 + 35.0 * coverage + 18.0 * citation_factor + 12.0 * seed_factor - (10.0 if missing else 0.0)))))

    if impacts:
        top = ", ".join([f"{x['ticker']} ({x['order']})" for x in impacts[:5] if str(x.get("ticker") or "")])
        answer = f"Simulated macro shock impact mapped. Most exposed holdings: {top}."
    else:
        answer = "Simulation ran, but no strong graph-supported impact path was found for current holdings."

    return {
        "answer": answer,
        "confidence_score": conf,
        "missing_variables": missing,
        "citations": sorted(list(citations))[:20],
        "impacts": impacts,
    }


def simulate_macro_shock_batch(
    scenarios: list[str],
    max_depth: int = SIM_MAX_DEPTH,
    top_n: int = 5,
) -> dict[str, Any]:
    """
    Run multiple what-if scenarios and compare portfolio/watchlist exposure.
    Returns side-by-side scenario outputs + aggregate most-exposed tickers.
    """
    clean = [str(x or "").strip() for x in list(scenarios or []) if str(x or "").strip()]
    clean = clean[:12]
    if not clean:
        return {
            "ok": False,
            "error": "no_scenarios",
            "results": [],
            "comparison": {"top_exposed": []},
        }

    results: list[dict[str, Any]] = []
    agg: dict[str, float] = {}
    per_scenario_top: list[dict[str, Any]] = []
    total_conf = 0.0
    for sc in clean:
        out = simulate_macro_shock(event_description=sc, max_depth=max_depth)
        impacts = list(out.get("impacts") or [])
        total_conf += float(out.get("confidence_score") or 0.0)
        for it in impacts:
            tk = _safe_ticker(str((it or {}).get("ticker") or ""))
            if not tk:
                continue
            score = _to_float((it or {}).get("impact_score"), 0.0)
            agg[tk] = agg.get(tk, 0.0) + score
        per_scenario_top.append(
            {
                "scenario": sc,
                "top_impacts": [
                    {
                        "ticker": _safe_ticker(str((x or {}).get("ticker") or "")),
                        "impact_score": _to_float((x or {}).get("impact_score"), 0.0),
                        "order": str((x or {}).get("order") or ""),
                    }
                    for x in impacts[: max(1, min(8, int(top_n or 5)))]
                    if _safe_ticker(str((x or {}).get("ticker") or ""))
                ],
            }
        )
        results.append({"scenario": sc, **out})

    top_exposed = [
        {"ticker": tk, "aggregate_impact_score": round(sc, 4)}
        for tk, sc in sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[: max(1, min(20, int(top_n or 5)))]
    ]
    avg_conf = int(round(total_conf / max(1, len(results))))
    return {
        "ok": True,
        "count": len(results),
        "average_confidence_score": avg_conf,
        "results": results,
        "comparison": {
            "top_exposed": top_exposed,
            "per_scenario_top": per_scenario_top,
        },
    }


def execute_action_proposal(proposal_id: int) -> dict[str, Any]:
    ensure_proactive_schema()
    pid = int(proposal_id or 0)
    if pid <= 0:
        return {"ok": False, "route": "/dashboard", "error": "invalid_id"}
    if core_backend() != "postgres":
        return {"ok": False, "route": "/dashboard", "error": "backend_not_postgres"}
    row = _pg_get_action_proposal_row(pid)
    if not row:
        return {"ok": False, "route": "/dashboard", "error": "not_found"}
    # Low-confidence gate: block execution if confidence < 0.70
    _CONFIDENCE_GATE = float(os.getenv("AI_CONFIDENCE_GATE", "0.70"))
    _conf = float(row.get("confidence") or row.get("confidence_score") or 0.0)
    if _conf > 0.0 and _conf < _CONFIDENCE_GATE:
        return {
            "ok": False,
            "route": "/dashboard",
            "error": "confidence_below_gate",
            "confidence": _conf,
            "gate": _CONFIDENCE_GATE,
            "message": f"Confidence {_conf:.0%} is below the {_CONFIDENCE_GATE:.0%} execution threshold. Review the proposal or lower the gate with AI_CONFIDENCE_GATE env var.",
        }
    payload = dict(row.get("execute_payload_json") or {})
    route = str((payload or {}).get("route") or row.get("execute_route") or "/dashboard").strip()
    tk = _safe_ticker(str(row.get("ticker") or "") or str((payload or {}).get("ticker") or ""))
    title = str(row.get("title") or "").strip()
    source_key = str(row.get("source_event_key") or "").strip()
    bullets = [str(x or "").strip() for x in list(row.get("thesis_json") or []) if str(x or "").strip()]
    reasoning = dict(row.get("reasoning_json") or {})
    if tk:
        actionable = str((reasoning or {}).get("actionable_proposal") or "").strip()
        margin = str((reasoning or {}).get("margin_impact") or "").strip()
        thesis = str((reasoning or {}).get("thesis_validation") or "").strip()
        risk = str((reasoning or {}).get("risk_assessment") or "").strip()
        note_lines = [
            f"[Active Proposal] {title or 'Action Proposal'}",
            f"Source: {source_key or '-'}",
            f"Actionable: {actionable or '-'}",
            f"Margins: {margin or '-'}",
            f"Thesis: {thesis or '-'}",
            f"Risk: {risk or '-'}",
        ]
        if bullets:
            note_lines.append("Evidence:")
            for b in bullets[:3]:
                note_lines.append(f"- {b}")
        _ = add_company_note(
            ticker=tk,
            note="\n".join(note_lines)[:3900],
            action="Proposal Execute",
            emotion="Focused",
            created_by="ai",
        )
        task_text = (actionable or f"Review and validate proposal for {tk}.").strip() + " [active_proposal]"
        _ = add_company_task(ticker=tk, task=task_text[:900], due_date="", priority="P2")
        remind_at = (dt.datetime.now() + dt.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
        _ = add_company_reminder(ticker=tk, remind_at=remind_at, note=f"Follow up accepted proposal for {tk}. [active_proposal]"[:900])
    con_pg = pg_connect()
    if con_pg is None:
        return {"ok": False, "route": "/dashboard", "error": "pg_unavailable"}
    try:
        cur = con_pg.cursor()
        now = dt.datetime.now().isoformat()
        cur.execute(
            "UPDATE action_proposals_core SET status='executed', updated_at=%s, executed_at=%s WHERE id=%s",
            (now, now, pid),
        )
        con_pg.commit()
        up = _pg_get_action_proposal_row(pid)
        if up:
            mirror_action_proposal(
                {
                    "source_event_key": str(up.get("source_event_key") or ""),
                    "proposal_id": int(up.get("id") or 0),
                    "ticker": str(up.get("ticker") or ""),
                    "kind": str(up.get("kind") or ""),
                    "status": str(up.get("status") or "open"),
                    "title": str(up.get("title") or ""),
                    "insights_json": list(up.get("insights_json") or []),
                    "reasoning_json": dict(up.get("reasoning_json") or {}),
                    "created_at": str(up.get("created_at") or ""),
                    "updated_at": str(up.get("updated_at") or ""),
                }
            )
        # Phase 3.2: Capture outcome tracking baseline
        try:
            _capture_outcome_baseline(pid, row)
        except Exception:
            pass
        return {"ok": True, "route": route if route.startswith("/") else "/dashboard", "payload": payload, "workspace_seeded": bool(tk)}
    except Exception:
        try:
            con_pg.rollback()
        except Exception:
            pass
        return {"ok": False, "route": "/dashboard", "error": "update_failed"}
    finally:
        con_pg.close()


# ---------------------------------------------------------------------------
# Phase 3.2: Outcome Tracking
# ---------------------------------------------------------------------------

def _capture_outcome_baseline(proposal_id: int, proposal_row: dict[str, Any]) -> None:
    """Capture price + thesis baseline when a proposal is executed, for future measurement."""
    if core_backend() != "postgres":
        return
    tk = _safe_ticker(str(proposal_row.get("ticker") or ""))
    if not tk:
        return
    # Get current price as baseline
    pm = get_price_metrics(tk)
    baseline_price = float((pm or {}).get("price") or (pm or {}).get("end_px") or 0.0)
    if baseline_price <= 0:
        return
    reasoning = dict(proposal_row.get("reasoning_json") or {})
    conf = str(reasoning.get("confidence") or "").strip()
    stance = str(reasoning.get("recommended_stance") or "").strip()
    summary = str(reasoning.get("thesis_validation") or reasoning.get("margin_impact") or "").strip()[:500]
    now = dt.datetime.now().isoformat()

    con_pg = pg_connect()
    if con_pg is None:
        return
    try:
        cur = con_pg.cursor()
        for window in ("7d", "30d", "90d"):
            cur.execute(
                """INSERT INTO proposal_outcomes_core
                   (id, proposal_id, ticker, outcome_type, created_at, measurement_window,
                    baseline_price, confidence_at_proposal, stance_at_proposal, reasoning_summary, status)
                   VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM proposal_outcomes_core),
                           %s, %s, 'price', %s, %s, %s, %s, %s, %s, 'pending')""",
                (int(proposal_id), tk, now, window, baseline_price, conf[:16], stance[:12], summary),
            )
        con_pg.commit()
    except Exception:
        try:
            con_pg.rollback()
        except Exception:
            pass
    finally:
        con_pg.close()


def measure_proposal_outcomes() -> dict[str, Any]:
    """Measure pending outcomes where the measurement window has elapsed."""
    if core_backend() != "postgres":
        return {"ok": False, "error": "not_postgres"}
    con_pg = pg_connect()
    if con_pg is None:
        return {"ok": False, "error": "pg_unavailable"}
    now = dt.datetime.now()
    measured = 0
    try:
        cur = con_pg.cursor()
        cur.execute(
            """SELECT id, proposal_id, ticker, measurement_window, baseline_price,
                      confidence_at_proposal, stance_at_proposal, reasoning_summary, created_at
               FROM proposal_outcomes_core
               WHERE status='pending'
               ORDER BY id
               LIMIT 200""",
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        for row in rows:
            r = dict(zip(cols, row))
            oid = int(r["id"])
            tk = str(r["ticker"])
            window = str(r["measurement_window"])
            created = str(r["created_at"])
            baseline = float(r["baseline_price"])
            # Check if window has elapsed
            try:
                created_dt = dt.datetime.fromisoformat(created)
            except Exception:
                continue
            window_days = {"7d": 7, "30d": 30, "90d": 90}.get(window, 30)
            if (now - created_dt).days < window_days:
                continue
            # Measure outcome
            pm = get_price_metrics(tk)
            outcome_price = float((pm or {}).get("price") or (pm or {}).get("end_px") or 0.0)
            if outcome_price <= 0 or baseline <= 0:
                continue
            return_pct = ((outcome_price - baseline) / baseline) * 100.0
            stance = str(r.get("stance_at_proposal") or "").strip().upper()
            # Direction correct: if stance was ADD/HOLD and price went up, or TRIM/EXIT and price went down
            direction_correct = None
            if stance in ("ADD", "HOLD", "WATCH"):
                direction_correct = return_pct > 0
            elif stance in ("TRIM", "EXIT"):
                direction_correct = return_pct < 0
            # Score: 1.0 = perfect, 0.0 = bad
            score = 1.0 if direction_correct else 0.0 if direction_correct is not None else 0.5

            cur.execute(
                """UPDATE proposal_outcomes_core
                   SET measured_at=%s, outcome_price=%s, return_pct=%s,
                       direction_correct=%s, score=%s, status='measured'
                   WHERE id=%s""",
                (now.isoformat(), outcome_price, round(return_pct, 4),
                 direction_correct, round(score, 4), oid),
            )
            measured += 1

            # Feed back into reflexion if the AI was wrong
            if direction_correct is False:
                try:
                    record_reflexion_from_quality_event(
                        event_type="outcome_miss",
                        query=f"Proposal {r['proposal_id']} for {tk}: predicted {stance}, actual {return_pct:+.1f}%",
                        detail={
                            "proposal_id": int(r["proposal_id"]),
                            "ticker": tk,
                            "stance": stance,
                            "return_pct": round(return_pct, 2),
                            "window": window,
                            "reasoning": str(r.get("reasoning_summary") or ""),
                        },
                    )
                except Exception:
                    pass

        con_pg.commit()
        return {"ok": True, "measured": measured}
    except Exception as exc:
        try:
            con_pg.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}
    finally:
        con_pg.close()


def get_ai_accuracy_stats(days: int = 90) -> dict[str, Any]:
    """Get AI prediction accuracy stats for the dashboard."""
    if core_backend() != "postgres":
        return {"ok": False}
    con_pg = pg_connect()
    if con_pg is None:
        return {"ok": False}
    since = (dt.datetime.now() - dt.timedelta(days=max(1, int(days or 90)))).isoformat()
    try:
        cur = con_pg.cursor()
        cur.execute(
            """SELECT measurement_window, COUNT(*) as total,
                      SUM(CASE WHEN direction_correct=TRUE THEN 1 ELSE 0 END) as correct,
                      AVG(return_pct) as avg_return,
                      AVG(score) as avg_score
               FROM proposal_outcomes_core
               WHERE status='measured' AND created_at >= %s
               GROUP BY measurement_window
               ORDER BY measurement_window""",
            (since,),
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description] if cur.description else []
        windows = {}
        total_correct = 0
        total_measured = 0
        for row in rows:
            r = dict(zip(cols, row))
            w = str(r["measurement_window"])
            t = int(r["total"] or 0)
            c = int(r["correct"] or 0)
            windows[w] = {
                "total": t,
                "correct": c,
                "hit_rate": round(c / t * 100, 1) if t > 0 else 0.0,
                "avg_return_pct": round(float(r["avg_return"] or 0), 2),
                "avg_score": round(float(r["avg_score"] or 0), 3),
            }
            total_correct += c
            total_measured += t
        # --- Proposal acceptance / rejection stats ---
        cur.execute(
            """SELECT
                 COUNT(*) FILTER (WHERE status='open')           AS open_count,
                 COUNT(*) FILTER (WHERE status='executed')       AS executed_count,
                 COUNT(*) FILTER (WHERE status='rejected')       AS rejected_count,
                 COUNT(*) FILTER (WHERE status='dismissed')      AS dismissed_count,
                 COUNT(*) FILTER (WHERE status='debate_rejected') AS debate_rejected_count,
                 COUNT(*) AS total
               FROM action_proposals_core
               WHERE created_at >= %s""",
            (since,),
        )
        pr = cur.fetchone()
        p_open     = int((pr[0] or 0)) if pr else 0
        p_executed = int((pr[1] or 0)) if pr else 0
        p_rejected = int((pr[2] or 0)) if pr else 0
        p_dismissed= int((pr[3] or 0)) if pr else 0
        p_debate_r = int((pr[4] or 0)) if pr else 0
        p_total    = int((pr[5] or 0)) if pr else 0
        p_resolved = p_executed + p_rejected + p_dismissed + p_debate_r
        acceptance_rate  = round(p_executed  / p_resolved * 100, 1) if p_resolved > 0 else None
        false_pos_rate   = round((p_dismissed + p_rejected) / p_resolved * 100, 1) if p_resolved > 0 else None

        # --- Agent run success stats ---
        cur.execute(
            """SELECT
                 COUNT(*) FILTER (WHERE status='completed') AS completed,
                 COUNT(*) FILTER (WHERE status='error')     AS errors,
                 COUNT(*) AS total
               FROM agent_runs_core
               WHERE created_at >= %s""",
            (since,),
        )
        ar = cur.fetchone()
        ar_completed = int((ar[0] or 0)) if ar else 0
        ar_errors    = int((ar[1] or 0)) if ar else 0
        ar_total     = int((ar[2] or 0)) if ar else 0
        agent_success_rate = round(ar_completed / ar_total * 100, 1) if ar_total > 0 else None

        return {
            "ok": True,
            "overall_hit_rate": round(total_correct / total_measured * 100, 1) if total_measured > 0 else 0.0,
            "total_measured": total_measured,
            "by_window": windows,
            # Proposal lifecycle KPIs
            "proposals_total":        p_total,
            "proposals_executed":     p_executed,
            "proposals_dismissed":    p_dismissed,
            "proposals_rejected":     p_rejected,
            "proposals_open":         p_open,
            "acceptance_rate":        acceptance_rate,   # % of resolved proposals that were executed
            "false_positive_rate":    false_pos_rate,    # % dismissed or rejected
            # Agent worker KPIs
            "agent_runs_total":       ar_total,
            "agent_runs_completed":   ar_completed,
            "agent_runs_errors":      ar_errors,
            "agent_success_rate":     agent_success_rate,
        }
    except Exception:
        return {"ok": False}
    finally:
        con_pg.close()


# ---------------------------------------------------------------------------
# Phase 3.3: Thesis Breach Detector
# ---------------------------------------------------------------------------

def detect_thesis_breaches(ticker: str | None = None) -> dict[str, Any]:
    """
    Compare current financial data against stored thesis invalidation criteria.
    If ticker is None, check all tickers with active theses.
    """
    if core_backend() != "postgres" or ask_ai is None:
        return {"ok": False, "error": "requires_postgres_and_llm"}

    # Load theses
    theses = list_watchlist_thesis_pg(limit=200)
    if ticker:
        tk = _safe_ticker(ticker)
        theses = [t for t in theses if _safe_ticker(str(t.get("ticker") or "")) == tk]
    if not theses:
        return {"ok": True, "breaches": 0, "checked": 0}

    breaches_created = 0
    checked = 0
    for thesis in theses:
        tk = _safe_ticker(str(thesis.get("ticker") or ""))
        if not tk:
            continue
        thesis_text = str(thesis.get("thesis_summary") or thesis.get("thesis") or "").strip()
        invalidation = str(thesis.get("invalidation_criteria") or "").strip()
        if not thesis_text and not invalidation:
            continue

        checked += 1
        financial_ctx = _build_financial_context(tk)
        if not financial_ctx:
            continue

        prompt = (
            "You are a thesis validation engine for an investment portfolio.\n"
            "Compare the investor's thesis and invalidation criteria against the actual financial data.\n"
            "Determine if any invalidation criteria have been breached.\n\n"
            f"TICKER: {tk}\n\n"
            f"INVESTOR'S THESIS:\n{thesis_text[:1500]}\n\n"
            f"INVALIDATION CRITERIA:\n{invalidation[:800] or '(none specified — infer reasonable criteria from the thesis)'}\n\n"
            f"CURRENT FINANCIAL DATA:\n{financial_ctx}\n\n"
            "Analyze:\n"
            "1. Does the actual data confirm or contradict the thesis?\n"
            "2. Have any invalidation criteria been breached? Be specific with numbers.\n"
            "3. What is the severity? (low/medium/high/critical)\n\n"
            "Return strict JSON only:\n"
            '{"breached": true/false, "breach_type": "margin_breach|growth_miss|debt_concern|concentration_risk|thesis_invalid|none", '
            '"breach_detail": "Specific explanation with numbers (e.g., gross margin at 33.2% vs thesis minimum of 35%)", '
            '"severity": "low|medium|high|critical", '
            '"actual_values": "Key metrics that triggered the breach"}\n\n'
            "Rules:\n"
            "- Only flag a breach if the data clearly contradicts the thesis or hits an invalidation criterion\n"
            "- Be specific — cite exact numbers from the financial data\n"
            "- severity=critical only if the core thesis is fundamentally broken\n"
            "- If no breach, return {\"breached\": false}"
        )
        try:
            raw = str(ask_ai(prompt, "Thesis breach detector. JSON only.", mode="fast", json_mode=True, temperature=0.0) or "").strip()
            if not raw:
                continue
            parsed = json.loads(raw)
            if not parsed.get("breached"):
                continue
            # Store breach alert
            con_pg = pg_connect()
            if con_pg is None:
                continue
            try:
                cur = con_pg.cursor()
                now = dt.datetime.now().isoformat()
                thesis_id = int(thesis.get("id") or 0)
                # Dedupe: don't create duplicate breach for same ticker+type in last 7 days
                cur.execute(
                    """SELECT id FROM thesis_breach_alerts_core
                       WHERE ticker=%s AND breach_type=%s AND detected_at >= %s AND status='open'
                       LIMIT 1""",
                    (tk, str(parsed.get("breach_type") or "")[:60],
                     (dt.datetime.now() - dt.timedelta(days=7)).isoformat()),
                )
                if cur.fetchone():
                    continue
                cur.execute(
                    """INSERT INTO thesis_breach_alerts_core
                       (id, ticker, thesis_id, breach_type, breach_detail, severity, detected_at,
                        financial_context, thesis_text, invalidation_criteria, actual_values, status)
                       VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM thesis_breach_alerts_core),
                               %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open')""",
                    (tk, thesis_id,
                     str(parsed.get("breach_type") or "")[:60],
                     str(parsed.get("breach_detail") or "")[:600],
                     str(parsed.get("severity") or "medium")[:16],
                     now,
                     financial_ctx[:2000],
                     thesis_text[:1000],
                     invalidation[:500],
                     str(parsed.get("actual_values") or "")[:500]),
                )
                con_pg.commit()
                breaches_created += 1
            finally:
                con_pg.close()
        except Exception:
            continue

    return {"ok": True, "breaches": breaches_created, "checked": checked}


def dismiss_thesis_breach_alert(alert_id: int) -> bool:
    """Mark a thesis breach alert as dismissed."""
    if core_backend() != "postgres":
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        # Read row before updating so we can write feedback memory
        cur.execute(
            "SELECT ticker, breach_type, breach_detail FROM thesis_breach_alerts_core WHERE id=%s LIMIT 1",
            (int(alert_id),),
        )
        alert_row = cur.fetchone()
        cur.execute(
            "UPDATE thesis_breach_alerts_core SET status='dismissed' WHERE id=%s AND status='open'",
            (int(alert_id),),
        )
        changed = int(cur.rowcount or 0) > 0
        con.commit()
        if changed and alert_row:
            ticker = str(alert_row[0] or "").strip().upper()
            breach_type = str(alert_row[1] or "").strip()
            breach_detail = str(alert_row[2] or "").strip()
            try:
                from app.services.postgres_core_service import add_agent_feedback_memory_pg
                add_agent_feedback_memory_pg(
                    ticker,
                    f"[User feedback] Dismissed thesis breach: type='{breach_type}', "
                    f"detail='{breach_detail[:120]}'. "
                    f"May be false positive — lower sensitivity for similar {breach_type} signals."
                )
            except Exception:
                pass
        return changed
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def dismiss_cascade_alert(alert_id: int) -> bool:
    """Mark a cascade alert as dismissed."""
    if core_backend() != "postgres":
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE portfolio_cascade_alerts_core SET status='dismissed' WHERE id=%s AND status='open'",
            (int(alert_id),),
        )
        changed = int(cur.rowcount or 0) > 0
        con.commit()
        return changed
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def list_thesis_breach_alerts(status: str = "open", limit: int = 20) -> list[dict[str, Any]]:
    """List thesis breach alerts for dashboard display."""
    if core_backend() != "postgres":
        return []
    con_pg = pg_connect()
    if con_pg is None:
        return []
    try:
        cur = con_pg.cursor()
        cur.execute(
            """SELECT id, ticker, thesis_id, breach_type, breach_detail, severity,
                      detected_at, thesis_text, invalidation_criteria, actual_values, status
               FROM thesis_breach_alerts_core
               WHERE status=%s
               ORDER BY id DESC
               LIMIT %s""",
            (str(status or "open"), max(1, min(100, int(limit or 20)))),
        )
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        con_pg.close()


# ---------------------------------------------------------------------------
# Phase 3.4: Cross-Portfolio Reasoning (Secondary Effects)
# ---------------------------------------------------------------------------

def analyze_portfolio_cascades(trigger_ticker: str, trigger_signal: str, trigger_proposal_id: int = 0) -> dict[str, Any]:
    """
    When a signal hits one holding, analyze cascading effects across the portfolio.
    Uses entity graph relationships + LLM reasoning.
    """
    if core_backend() != "postgres" or ask_ai is None:
        return {"ok": False, "error": "requires_postgres_and_llm"}

    tk = _safe_ticker(trigger_ticker)
    if not tk:
        return {"ok": False, "error": "invalid_ticker"}

    # Get all portfolio holdings
    from app.services.portfolio_memory_service import get_holdings
    holdings = get_holdings(limit=200)
    other_tickers = [
        _safe_ticker(str(h.get("ticker") or ""))
        for h in holdings
        if _safe_ticker(str(h.get("ticker") or "")) and _safe_ticker(str(h.get("ticker") or "")) != tk
    ]
    if not other_tickers:
        return {"ok": True, "cascades": 0, "reason": "single_holding_portfolio"}

    # Get relationships from entity graph
    relationships: list[str] = []
    con_pg = pg_connect()
    if con_pg is not None:
        try:
            cur = con_pg.cursor()
            cur.execute(
                """SELECT r.source_entity_id, r.target_entity_id, r.relationship_type, r.detail_json
                   FROM relationships_core r
                   JOIN entities_core e1 ON r.source_entity_id = e1.id
                   JOIN entities_core e2 ON r.target_entity_id = e2.id
                   WHERE (e1.name = %s OR e2.name = %s)
                   LIMIT 50""",
                (tk, tk),
            )
            for row in cur.fetchall():
                src, tgt, rel_type, detail = row
                relationships.append(f"{src} --{rel_type}--> {tgt}")
        except Exception:
            pass
        finally:
            con_pg.close()

    # Build context for each holding
    portfolio_ctx_lines = []
    for otk in other_tickers[:12]:
        ctx = _build_financial_context(otk)
        if ctx:
            portfolio_ctx_lines.append(f"--- {otk} ---\n{ctx[:400]}")

    portfolio_summary = "\n".join(portfolio_ctx_lines)[:4000]
    rel_text = "\n".join(relationships[:20]) if relationships else "(no known relationships)"

    prompt = (
        "You are a portfolio risk cascade engine. A significant signal just hit one holding.\n"
        "Analyze the secondary effects on OTHER holdings in the portfolio.\n\n"
        f"TRIGGER: {tk}\n"
        f"SIGNAL: {trigger_signal[:1500]}\n\n"
        f"KNOWN RELATIONSHIPS:\n{rel_text}\n\n"
        f"OTHER PORTFOLIO HOLDINGS:\n{portfolio_summary}\n\n"
        "Think about:\n"
        "- Shared suppliers or customers (if AAPL supply chain hit, does QCOM share same supplier?)\n"
        "- Same sector exposure (regulatory change affecting multiple holdings)\n"
        "- Revenue geography overlap (China risk hitting multiple holdings)\n"
        "- Competitive dynamics (signal good for one = bad for competitor in portfolio)\n"
        "- Macro correlation (rate sensitivity affecting multiple holdings)\n\n"
        "Return strict JSON only:\n"
        '{"cascades": [{"affected_ticker": "TK", "effect_type": "shared_supplier|same_sector|customer_dependency|regulatory_contagion|competitive_impact|macro_correlation", '
        '"effect_summary": "Specific explanation with reasoning chain", '
        '"relationship": "How the two are connected", '
        '"confidence": 0.0, "severity": "low|medium|high|critical"}]}\n\n'
        "Rules:\n"
        "- Only include cascades with real, specific connections (not vague)\n"
        "- Explain the cause-effect chain: signal X → impact on Y → because Z\n"
        "- confidence 0.0-1.0 based on how direct the connection is\n"
        "- Maximum 6 cascades\n"
        "- If no meaningful cascades exist, return {\"cascades\": []}"
    )
    try:
        raw = str(ask_ai(prompt, "Portfolio cascade analyst. JSON only.", mode="smart", json_mode=True, temperature=0.1) or "").strip()
        if not raw:
            return {"ok": True, "cascades": 0}
        parsed = json.loads(raw)
        cascades = parsed.get("cascades") or []
        if not cascades:
            return {"ok": True, "cascades": 0}

        created = 0
        con_pg = pg_connect()
        if con_pg is None:
            return {"ok": False, "error": "pg_unavailable"}
        try:
            cur = con_pg.cursor()
            now = dt.datetime.now().isoformat()
            for c in cascades[:6]:
                atk = _safe_ticker(str(c.get("affected_ticker") or ""))
                if not atk or atk == tk:
                    continue
                conf = float(c.get("confidence") or 0.0)
                if conf < 0.4:
                    continue
                # Dedupe
                cur.execute(
                    """SELECT id FROM portfolio_cascade_alerts_core
                       WHERE trigger_ticker=%s AND affected_ticker=%s
                       AND detected_at >= %s AND status='open'
                       LIMIT 1""",
                    (tk, atk, (dt.datetime.now() - dt.timedelta(days=3)).isoformat()),
                )
                if cur.fetchone():
                    continue
                cur.execute(
                    """INSERT INTO portfolio_cascade_alerts_core
                       (id, trigger_ticker, trigger_signal, trigger_proposal_id, affected_ticker,
                        effect_type, effect_summary, relationship, confidence, severity, detected_at, status)
                       VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM portfolio_cascade_alerts_core),
                               %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open')""",
                    (tk, trigger_signal[:500], int(trigger_proposal_id or 0), atk,
                     str(c.get("effect_type") or "")[:60],
                     str(c.get("effect_summary") or "")[:600],
                     str(c.get("relationship") or "")[:300],
                     conf,
                     str(c.get("severity") or "medium")[:16],
                     now),
                )
                created += 1
            con_pg.commit()
            return {"ok": True, "cascades": created}
        finally:
            con_pg.close()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def analyze_universe_signal_for_portfolio(
    universe_ticker: str,
    filing_text: str,
    filing_form: str,
    held_tickers: list[str],
) -> dict[str, Any]:
    """
    Given a filing from a non-held ticker (e.g. TSMC 8-K about supply constraints),
    determine whether it creates signals relevant to any held positions.

    This is the noise filter: the LLM reads the filing and decides whether it's
    actually relevant to your portfolio. If not, it returns cascades=0 and nothing
    is surfaced. Only real, specific connections become cascade alerts.
    """
    if core_backend() != "postgres" or ask_ai is None:
        return {"ok": False, "cascades": 0, "relevant": False}

    tk = _safe_ticker(universe_ticker)
    if not tk or not filing_text or not held_tickers:
        return {"ok": True, "cascades": 0, "relevant": False}

    held_str = ", ".join(held_tickers[:20])

    prompt = (
        f"You are monitoring SEC filings from companies you DON'T hold, looking for signals "
        f"that affect companies you DO hold.\n\n"
        f"FILING SOURCE: {tk} ({filing_form})\n"
        f"FILING TEXT (excerpt):\n{filing_text[:6000]}\n\n"
        f"YOUR HELD POSITIONS: {held_str}\n\n"
        f"TASK:\n"
        f"1. First, is there anything in this {tk} filing that is materially relevant to any "
        f"of your held positions? Think about:\n"
        f"   - Supply chain: does {tk} supply components/services to any held company?\n"
        f"   - Competition: does {tk} compete with any held company?\n"
        f"   - Customer: is {tk} a major customer of any held company?\n"
        f"   - Macro signal: does this reveal a trend (rates, regulation, demand) that hits held positions?\n"
        f"   - Revenue geography: shared exposure (e.g. both have heavy China revenue)?\n\n"
        f"2. If relevant: for each affected held ticker, explain the specific mechanism.\n"
        f"3. If NOT relevant: return cascades=[] — do NOT invent connections.\n\n"
        f"Return strict JSON only:\n"
        f'{{"relevant": true/false, "cascades": ['
        f'{{"affected_ticker": "TK", "effect_type": "shared_supplier|competitor|customer|macro|regulatory", '
        f'"effect_summary": "Specific cause-effect chain with numbers from the filing", '
        f'"relationship": "How {tk} and TK are connected", '
        f'"confidence": 0.0, "severity": "low|medium|high|critical"}}]}}\n\n'
        f"Rules:\n"
        f"- Only include cascades where confidence >= 0.5\n"
        f"- Effect summary must cite specific data from the filing (numbers, quotes)\n"
        f"- Maximum 5 cascades\n"
        f"- If nothing is relevant, return {{\"relevant\": false, \"cascades\": []}}"
    )

    try:
        raw = str(ask_ai(
            prompt,
            f"Portfolio signal monitor. Analyzing {tk} filing for impact on held positions. JSON only.",
            mode="smart", json_mode=True, temperature=0.1,
        ) or "").strip()
        if not raw:
            return {"ok": True, "cascades": 0, "relevant": False}

        parsed = json.loads(raw)
        relevant = bool(parsed.get("relevant"))
        cascades = list(parsed.get("cascades") or [])

        if not relevant or not cascades:
            return {"ok": True, "cascades": 0, "relevant": False}

        created = 0
        con_pg = pg_connect()
        if con_pg is None:
            return {"ok": False, "error": "pg_unavailable"}
        try:
            cur = con_pg.cursor()
            now = dt.datetime.now().isoformat()
            signal_snippet = f"[{filing_form}] {filing_text[:300]}"
            for c in cascades[:5]:
                atk = _safe_ticker(str(c.get("affected_ticker") or ""))
                if not atk:
                    continue
                conf = float(c.get("confidence") or 0.0)
                if conf < 0.5:
                    continue
                # Dedupe: skip if same (trigger, affected) pair has an open alert in last 7 days
                cur.execute(
                    """SELECT id FROM portfolio_cascade_alerts_core
                       WHERE trigger_ticker=%s AND affected_ticker=%s
                       AND detected_at >= %s AND status='open' LIMIT 1""",
                    (tk, atk, (dt.datetime.now() - dt.timedelta(days=7)).isoformat()),
                )
                if cur.fetchone():
                    continue
                cur.execute(
                    """INSERT INTO portfolio_cascade_alerts_core
                       (id, trigger_ticker, trigger_signal, trigger_proposal_id, affected_ticker,
                        effect_type, effect_summary, relationship, confidence, severity, detected_at, status)
                       VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM portfolio_cascade_alerts_core),
                               %s, %s, 0, %s, %s, %s, %s, %s, %s, %s, 'open')""",
                    (
                        tk, signal_snippet[:500], atk,
                        str(c.get("effect_type") or "")[:60],
                        str(c.get("effect_summary") or "")[:600],
                        str(c.get("relationship") or "")[:300],
                        conf,
                        str(c.get("severity") or "medium")[:16],
                        now,
                    ),
                )
                created += 1
            con_pg.commit()
            return {"ok": True, "cascades": created, "relevant": True}
        finally:
            con_pg.close()

    except Exception as exc:
        return {"ok": False, "cascades": 0, "relevant": False, "error": str(exc)[:200]}


def list_cascade_alerts(status: str = "open", limit: int = 20) -> list[dict[str, Any]]:
    """List cross-portfolio cascade alerts for dashboard display."""
    if core_backend() != "postgres":
        return []
    con_pg = pg_connect()
    if con_pg is None:
        return []
    try:
        cur = con_pg.cursor()
        cur.execute(
            """SELECT id, trigger_ticker, trigger_signal, affected_ticker,
                      effect_type, effect_summary, relationship, confidence, severity,
                      detected_at, status
               FROM portfolio_cascade_alerts_core
               WHERE status=%s
               ORDER BY id DESC
               LIMIT %s""",
            (str(status or "open"), max(1, min(100, int(limit or 20)))),
        )
        cols = [d[0] for d in cur.description] if cur.description else []
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        con_pg.close()
