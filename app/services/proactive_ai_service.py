from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from typing import Any

from app.core.config import CORE_DB_PATH, ROOT
from app.core.sqlite_hardening import connect_sqlite, sqlite_retry
from app.services.user_preferences_service import upsert_user_preference

try:
    from tools.llm_engine import ask_ai
except Exception:  # pragma: no cover
    ask_ai = None  # type: ignore[assignment]


ONYX_BRAIN_DB_PATH = ROOT / "onyx_brain.db"
MAX_PROPOSALS_PER_MONITOR_RUN = 30
PROPOSAL_COOLDOWN_HOURS = 24
SIM_MAX_DEPTH = 3


def _conn_core() -> sqlite3.Connection:
    return connect_sqlite(str(CORE_DB_PATH), row_factory=True)


def _conn_onyx() -> sqlite3.Connection:
    return connect_sqlite(str(ONYX_BRAIN_DB_PATH), row_factory=True)


def _to_float(v: object, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _safe_ticker(raw: str) -> str:
    s = re.sub(r"[^A-Z0-9.\-]", "", str(raw or "").strip().upper())
    if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,11}", s):
        return s
    return ""


def _read_scope_tickers() -> tuple[set[str], set[str], set[str]]:
    portfolio: set[str] = set()
    watchlist: set[str] = set()
    bluechips: set[str] = set()
    p = ROOT / "data" / "portfolio.csv"
    if p.exists():
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = [x.strip() for x in ln.split(",")]
            t = _safe_ticker(parts[0] if parts else "")
            if t:
                portfolio.add(t)
    w = ROOT / "data" / "my_watchlist.txt"
    if w.exists():
        for ln in w.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = str(ln or "").strip()
            if not s or s.startswith("#"):
                continue
            parts = [x.strip() for x in s.split(",")]
            t = _safe_ticker(parts[0] if parts else "")
            if t:
                watchlist.add(t)
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
        con_core = _conn_core()
        con_onyx = _conn_onyx()
        try:
            con_core.execute(
                """CREATE TABLE IF NOT EXISTS action_proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    kind TEXT NOT NULL,
                    ticker TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    thesis_json TEXT NOT NULL DEFAULT '[]',
                    citations_json TEXT NOT NULL DEFAULT '[]',
                    confidence REAL NOT NULL DEFAULT 0.0,
                    priority_score REAL NOT NULL DEFAULT 0.0,
                    source_event_key TEXT NOT NULL UNIQUE,
                    execute_route TEXT NOT NULL DEFAULT '',
                    execute_payload_json TEXT NOT NULL DEFAULT '{}',
                    dismissed_reason TEXT NOT NULL DEFAULT '',
                    executed_at TEXT NOT NULL DEFAULT ''
                )"""
            )
            _add_col_if_missing(con_core, "action_proposals", "proposal_uid TEXT NOT NULL DEFAULT ''")
            _add_col_if_missing(con_core, "action_proposals", "target_ticker TEXT NOT NULL DEFAULT ''")
            _add_col_if_missing(con_core, "action_proposals", "suggested_action TEXT NOT NULL DEFAULT 'REVIEW'")
            _add_col_if_missing(con_core, "action_proposals", "thesis_summary TEXT NOT NULL DEFAULT ''")
            _add_col_if_missing(con_core, "action_proposals", "confidence_score REAL NOT NULL DEFAULT 0.0")
            _add_col_if_missing(con_core, "action_proposals", "rejection_reason TEXT NOT NULL DEFAULT ''")
            _add_col_if_missing(con_core, "action_proposals", "rejected_at TEXT NOT NULL DEFAULT ''")
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
            con_core.execute("CREATE INDEX IF NOT EXISTS idx_action_proposals_status ON action_proposals(status, priority_score DESC, id DESC)")
            con_core.execute("CREATE INDEX IF NOT EXISTS idx_action_proposals_uid ON action_proposals(proposal_uid)")
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
            con_core.execute(
                """CREATE TABLE IF NOT EXISTS reflexion_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    query TEXT NOT NULL DEFAULT '',
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    note_text TEXT NOT NULL DEFAULT '',
                    rule_key TEXT NOT NULL DEFAULT '',
                    rule_text TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0.0
                )"""
            )
            con_core.execute(
                """CREATE TABLE IF NOT EXISTS failure_patterns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pattern_key TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    total_count INTEGER NOT NULL DEFAULT 0,
                    open_count INTEGER NOT NULL DEFAULT 0,
                    resolved_count INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_query TEXT NOT NULL DEFAULT '',
                    last_detail_json TEXT NOT NULL DEFAULT '{}',
                    last_reflexion TEXT NOT NULL DEFAULT ''
                )"""
            )
            con_core.execute(
                """CREATE TABLE IF NOT EXISTS reflexion_policy_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_tag TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    source_event_id INTEGER NOT NULL DEFAULT 0,
                    policy_json TEXT NOT NULL DEFAULT '{}',
                    is_active INTEGER NOT NULL DEFAULT 0,
                    rolled_back_from TEXT NOT NULL DEFAULT ''
                )"""
            )
            con_core.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_created ON agent_runs(created_at DESC)")
            con_core.execute("CREATE INDEX IF NOT EXISTS idx_reflexion_notes_created ON reflexion_notes(created_at DESC)")
            con_core.execute("CREATE INDEX IF NOT EXISTS idx_failure_patterns_last_seen ON failure_patterns(last_seen_at DESC)")
            con_core.execute("CREATE INDEX IF NOT EXISTS idx_reflexion_policy_active ON reflexion_policy_versions(is_active, created_at DESC)")

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
                    UNIQUE(source_id, target_id, relationship_type, citation_link),
                    FOREIGN KEY(source_id) REFERENCES entities(id) ON DELETE CASCADE,
                    FOREIGN KEY(target_id) REFERENCES entities(id) ON DELETE CASCADE
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
            _add_col_if_missing(con_onyx, "entities", "entity_type TEXT NOT NULL DEFAULT ''")
            _add_col_if_missing(con_onyx, "entities", "metadata TEXT NOT NULL DEFAULT '{}'")
            _add_col_if_missing(con_onyx, "entities", "id_text TEXT NOT NULL DEFAULT ''")
            _add_col_if_missing(con_onyx, "relationships", "citation_url TEXT NOT NULL DEFAULT ''")
            _add_col_if_missing(con_onyx, "relationships", "confidence_score REAL NOT NULL DEFAULT 0.0")
            _add_col_if_missing(con_onyx, "relationships", "id_text TEXT NOT NULL DEFAULT ''")
            con_core.execute("UPDATE action_proposals SET proposal_uid = 'ap_' || id WHERE COALESCE(proposal_uid,'') = ''")
            con_core.execute(
                "UPDATE user_preferences SET preference_key = pref_key, preference_value = pref_value WHERE COALESCE(preference_key,'') = ''"
            )
            con_onyx.execute("UPDATE entities SET id_text = 'ent_' || id WHERE COALESCE(id_text,'') = ''")
            con_onyx.execute("UPDATE relationships SET id_text = 'rel_' || id WHERE COALESCE(id_text,'') = ''")
            con_onyx.execute("CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type)")
            con_onyx.execute("CREATE INDEX IF NOT EXISTS idx_rels_src ON relationships(source_id)")
            con_onyx.execute("CREATE INDEX IF NOT EXISTS idx_rels_tgt ON relationships(target_id)")
            con_onyx.execute("CREATE INDEX IF NOT EXISTS idx_rels_type ON relationships(relationship_type)")
            con_onyx.execute("CREATE INDEX IF NOT EXISTS idx_onyx_pref_key ON user_preferences(preference_key)")
            con_onyx.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_id_text ON entities(id_text)")
            con_onyx.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_relationships_id_text ON relationships(id_text)")
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


def _reflexion_pattern_key(event_type: str, detail: dict[str, Any]) -> str:
    ev = str(event_type or "unknown").strip().lower()
    intent = str((detail or {}).get("intent") or "").strip().lower()
    route = str((detail or {}).get("route") or "").strip().lower()
    tool = str((detail or {}).get("tool_name") or "").strip().lower()
    parts = [p for p in [ev, intent, route, tool] if p]
    key = "|".join(parts) or ev or "unknown"
    return key[:180]


def _build_reflexion_rule(event_type: str, query: str) -> tuple[str, str, float]:
    ev = str(event_type or "").strip().lower()
    q = str(query or "").strip().lower()
    if ev in {"action_confidence_gate", "clarify_before_action", "needs_clarification"}:
        return (
            "ask_clarify_when_uncertain",
            "When intent confidence is low, ask one explicit clarifying question before any mutation/navigation.",
            0.86,
        )
    if ev in {"mutation_verify_failed", "verify_failed", "mutation_execution_error"}:
        return (
            "strengthen_verify_after_write",
            "On any write verification mismatch, return explicit failure and never claim success.",
            0.94,
        )
    if "watchlist" in q or "portfolio" in q:
        return (
            "prefer_info_then_navigation",
            "For list/info requests, answer in chat first; ask before navigation.",
            0.81,
        )
    return (
        "improve_intent_disambiguation",
        "Disambiguate user intent (chat vs action) before routing to execution.",
        0.74,
    )


def record_reflexion_from_quality_event(event_type: str, query: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    ensure_proactive_schema()
    ev = str(event_type or "").strip().lower()
    d = dict(detail or {})
    q = str(query or "").strip()
    tracked = {"action_confidence_gate", "clarify_before_action", "needs_clarification", "mutation_verify_failed", "verify_failed", "mutation_execution_error", "llm_fallback_error"}
    if ev not in tracked:
        return {"ok": True, "skipped": True, "reason": "event_not_tracked"}
    now = dt.datetime.now().isoformat()
    pattern_key = _reflexion_pattern_key(ev, d)
    rule_key, rule_text, conf = _build_reflexion_rule(ev, q)
    note_text = f"Observed failure pattern '{ev}'. Applied rule '{rule_key}' to reduce repeats."
    out = {"ok": False, "policy_version": "", "rule_key": rule_key}

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
    con = _conn_core()
    try:
        rows = con.execute(
            """SELECT run_uid, agent_name, trigger_type, status, started_at, finished_at, duration_ms, error_text, created_at
               FROM agent_runs
               ORDER BY id DESC
               LIMIT ?""",
            (max(1, min(200, int(limit or 20))),),
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
    con = _conn_core()
    try:
        notes = con.execute(
            """SELECT id, created_at, event_type, query, note_text, rule_key, rule_text, confidence
               FROM reflexion_notes
               ORDER BY id DESC
               LIMIT ?""",
            (max(1, min(200, int(limit or 20))),),
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


def _state_get(con: sqlite3.Connection, key: str, default: str = "") -> str:
    row = con.execute("SELECT state_value FROM proactive_monitor_state WHERE state_key = ? LIMIT 1", (key,)).fetchone()
    if not row:
        return default
    return str(row["state_value"] or default)


def _state_set(con: sqlite3.Connection, key: str, value: str) -> None:
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
    con.execute(
        """INSERT OR IGNORE INTO relationships
           (source_id, target_id, relationship_type, citation_link, citation_url, citation_text, confidence, confidence_score, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
    con_core = _conn_core()
    con_onyx = _conn_onyx()
    created_entities = 0
    created_edges = 0
    scanned = 0
    try:
        row = con_onyx.execute("SELECT state_value FROM ontology_ingest_state WHERE state_key='last_report_fact_id' LIMIT 1").fetchone()
        last_id = int(str((row["state_value"] if row else "0") or "0") or "0")
        rows = con_core.execute(
            """SELECT id, ticker, fact_text, report_name, importance
               FROM report_facts
               WHERE id > ?
               ORDER BY id ASC
               LIMIT ?""",
            (last_id, max(20, min(2000, int(limit_rows or 200)))),
        ).fetchall()
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
        "Review thesis fit, downside risk, and sizing before any action.",
    ]


def _proposal_upsert(
    con: sqlite3.Connection,
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
    row = con.execute("SELECT id, status FROM action_proposals WHERE source_event_key = ? LIMIT 1", (source_event_key,)).fetchone()
    if row:
        return False
    now = dt.datetime.now().isoformat()
    con.execute(
        """INSERT INTO action_proposals
           (created_at, updated_at, status, kind, ticker, title, thesis_json, citations_json,
            confidence, priority_score, source_event_key, execute_route, execute_payload_json)
           VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            now,
            now,
            str(kind or "thesis_trigger"),
            str(ticker or "").upper(),
            str(title or "")[:240],
            json.dumps(list(bullets or [])[:3], ensure_ascii=True),
            json.dumps(list(citations or [])[:8], ensure_ascii=True),
            float(confidence or 0.0),
            float(priority_score or 0.0),
            str(source_event_key or "")[:240],
            str(execute_route or "/dashboard"),
            json.dumps(execute_payload or {}, ensure_ascii=True),
        ),
    )
    con.execute(
        """UPDATE action_proposals
           SET target_ticker = ?, suggested_action = ?, thesis_summary = ?, confidence_score = ?, updated_at = ?
           WHERE source_event_key = ?""",
        (
            str(ticker or "").upper(),
            "REVIEW",
            " ".join([str(x).strip() for x in list(bullets or [])[:3] if str(x).strip()])[:1500],
            float(confidence or 0.0),
            dt.datetime.now().isoformat(),
            str(source_event_key or "")[:240],
        ),
    )
    return True


def _has_open_proposal(con: sqlite3.Connection, kind: str, ticker: str) -> bool:
    row = con.execute(
        "SELECT id FROM action_proposals WHERE status='open' AND kind=? AND ticker=? LIMIT 1",
        (str(kind or "").strip(), str(ticker or "").strip().upper()),
    ).fetchone()
    return bool(row)


def _has_recent_proposal(con: sqlite3.Connection, kind: str, ticker: str, cooldown_hours: int = PROPOSAL_COOLDOWN_HOURS) -> bool:
    k = str(kind or "").strip()
    t = str(ticker or "").strip().upper()
    if not k or not t:
        return False
    since = (dt.datetime.now() - dt.timedelta(hours=max(1, int(cooldown_hours or 1)))).isoformat()
    row = con.execute(
        "SELECT id FROM action_proposals WHERE kind=? AND ticker=? AND created_at>=? LIMIT 1",
        (k, t, since),
    ).fetchone()
    return bool(row)


def _day_pct_map() -> dict[str, float]:
    con = _conn_core()
    out: dict[str, float] = {}
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


def _company_name_map() -> dict[str, str]:
    con = _conn_core()
    out: dict[str, str] = {}
    try:
        rows = con.execute("SELECT ticker, name FROM company_profile_cache").fetchall()
        for r in rows:
            tk = _safe_ticker(str(r["ticker"] or ""))
            if tk:
                out[tk] = str(r["name"] or "").strip()
    finally:
        con.close()
    return out


def _portfolio_weight_map() -> dict[str, float]:
    positions: dict[str, tuple[float, float]] = {}
    p = ROOT / "data" / "portfolio.csv"
    if p.exists():
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = [x.strip() for x in ln.split(",")]
            tk = _safe_ticker(parts[0] if parts else "")
            if not tk:
                continue
            sh = _to_float(parts[1] if len(parts) > 1 else 0.0, 0.0)
            cost = _to_float(parts[2] if len(parts) > 2 else 0.0, 0.0)
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


def _infer_direction(kind: str, title: str, bullets: list[str]) -> str:
    k = str(kind or "").strip().lower()
    t = (str(title or "") + " " + " ".join(str(x or "") for x in (bullets or []))).lower()
    if "macro_signal" in k or "macro" in k or "contagion" in k:
        if any(x in t for x in {"tailwind", "upside", "improving", "beat"}):
            return "LONG"
        if any(x in t for x in {"slowdown", "risk", "pressure", "default", "miss", "downside"}):
            return "RISK"
        return "RISK"
    if "thesis trigger" in t and all(x not in t for x in {"undervalued", "overvalued", "tailwind", "headwind"}):
        return "REVIEW"
    pos = sum(1 for x in {"undervalued", "dislocation", "tailwind", "beat", "upside", "improving", "opportunity", "expanding"} if x in t)
    neg = sum(1 for x in {"overvalued", "slowdown", "miss", "pressure", "liability", "churn", "contracting"} if x in t)
    if pos > neg:
        return "LONG"
    if neg > pos:
        return "SHORT"
    return "REVIEW"


def _suggest_max_add_pct(current_weight_pct: float, day_move_abs_pct: float) -> float:
    w = max(0.0, float(current_weight_pct or 0.0))
    vol = max(0.0, float(day_move_abs_pct or 0.0))
    # Volatility-adjusted room: larger recent moves -> smaller suggested add size.
    vol_scale = max(0.8, min(3.5, 0.9 + (vol / 4.0)))
    cap_room = max(0.5, 10.0 - w)
    max_add = min(cap_room, 3.5 / vol_scale)
    return max(0.5, round(max_add, 2))


def _build_cross_read_vector(ticker: str, bullets: list[str]) -> str:
    tk = _safe_ticker(ticker)
    peer = ""
    rel = ""
    for b in bullets or []:
        s = str(b or "")
        m_peer = re.search(r"\b([A-Z]{2,6})\b", s)
        if m_peer and _safe_ticker(m_peer.group(1)) != tk:
            peer = _safe_ticker(m_peer.group(1))
        m_rel = re.search(r"relationship[^:]*:\s*([A-Z_]+)", s, flags=re.I)
        if m_rel:
            rel = str(m_rel.group(1) or "").strip().upper()
        if peer and rel:
            break
    if not peer:
        return ""
    rel_txt = rel if rel else "LINKED_PEER"
    return f"Cross-Read: {peer} ({rel_txt}) -> {tk}"


def _extract_company_insights(bullets: list[str]) -> list[dict[str, str]]:
    lines = [str(x or "").strip() for x in (bullets or []) if str(x or "").strip()]
    if not lines:
        return []
    if ask_ai is not None:
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


def run_event_driven_monitor(force: bool = False) -> dict[str, Any]:
    ensure_proactive_schema()
    run_uid = start_agent_run(
        agent_name="event_driven_monitor",
        trigger_type="forced" if bool(force) else "event_driven",
        input_payload={"force": bool(force)},
    )
    portfolio, watchlist, bluechips = _read_scope_tickers()
    scope = set(portfolio) | set(watchlist) | set(bluechips)
    con = _conn_core()
    created = 0
    try:
        rf_max = int((con.execute("SELECT COALESCE(MAX(id),0) AS m FROM report_facts").fetchone() or {"m": 0})["m"] or 0)
        fl_max = int((con.execute("SELECT COALESCE(MAX(id),0) AS m FROM filings").fetchone() or {"m": 0})["m"] or 0)
        rf_last = int(_state_get(con, "last_report_fact_id", "0") or "0")
        fl_last = int(_state_get(con, "last_filing_id", "0") or "0")
        if not force and rf_max <= rf_last and fl_max <= fl_last:
            out = {"ok": True, "ran": False, "reason": "no_new_reports_or_filings", "created": 0}
            finish_agent_run(run_uid, "skipped", output_payload=out)
            return out

        # Phase 2: ontology ingest from newly observed facts.
        ont = ingest_ontology_from_report_facts(limit_rows=500)

        name_map = _company_name_map()
        day_map = _day_pct_map()

        # Phase 3/4: build action proposals from new report facts for in-scope companies.
        throttled = False
        seen_rf_tickers: set[str] = set()
        rf_rows = con.execute(
            """SELECT id, ticker, fact_text, report_name, importance
               FROM report_facts
               WHERE id > ?
               ORDER BY id DESC
               LIMIT 240""",
            (rf_last,),
        ).fetchall()
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
            if _has_open_proposal(con, "thesis_trigger", tk) or _has_recent_proposal(con, "thesis_trigger", tk):
                continue
            text = str(r["fact_text"] or "").strip()
            rep = str(r["report_name"] or "").strip()
            src_key = f"rf:{int(r['id'] or 0)}:{tk}"
            title = f"Review {tk} Thesis Trigger"
            bullets = _build_thesis_bullets(tk, text, "THESIS_TRIGGER")
            cites = [{"label": "Report Evidence", "url": f"/reports/view?name={rep}"}] if rep else [{"label": "Reports", "url": "/reports"}]
            if _proposal_upsert(
                con,
                source_event_key=src_key,
                kind="thesis_trigger",
                ticker=tk,
                title=title,
                bullets=bullets,
                citations=cites,
                confidence=min(0.99, 0.55 + (imp * 0.04)),
                priority_score=float(imp),
                    execute_route=f"/company_file?t={tk}",
                    execute_payload={"ticker": tk, "source": "report_facts"},
            ):
                created += 1
                seen_rf_tickers.add(tk)

        # New filing monitor (event-driven).
        filing_rows = con.execute(
            """SELECT id, ticker, form, date, accession
               FROM filings
               WHERE id > ?
               ORDER BY id DESC
               LIMIT 200""",
            (fl_last,),
        ).fetchall()
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
            if _has_open_proposal(con, "filing_update", tk) or _has_recent_proposal(con, "filing_update", tk):
                continue
            src_key = f"fil:{int(r['id'] or 0)}:{tk}:{fm}"
            title = f"{tk} filed {fm} — review update"
            bullets = [
                f"New official filing detected: {fm} ({str(r['date'] or '-')}).",
                "Check if this changes your core thesis, invalidation level, or sizing.",
                "Open workspace and record decision rationale before acting.",
            ]
            cites = [{"label": "SEC Filings", "url": f"/company_file/sec?t={tk}"}]
            if _proposal_upsert(
                con,
                source_event_key=src_key,
                kind="filing_update",
                ticker=tk,
                title=title,
                bullets=bullets,
                citations=cites,
                confidence=0.9,
                priority_score=8.5 if fm in {"8-K", "10-Q", "10-K"} else 7.5,
                execute_route=f"/company_file/sec?t={tk}",
                execute_payload={"ticker": tk, "form": fm},
            ):
                created += 1

        # Contagion / peer alert from ontology relationships.
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
                    if _has_open_proposal(con, "contagion_peer_alert", tk) or _has_recent_proposal(con, "contagion_peer_alert", tk):
                        continue
                    rel_type = str(rr["relationship_type"] or "LINKED")
                    src_key = f"contagion:{tk}:{peer}:{dt.date.today().isoformat()}"
                    title = f"Contagion Alert: {peer} {d:+.2f}% may impact {tk}"
                    bullets = [
                        f"Linked peer {peer} moved {d:+.2f}% today.",
                        f"Relationship in ontology: {rel_type}.",
                        f"Re-check {tk} thesis assumptions and cross-company risk transmission.",
                    ]
                    cites = [
                        {"label": "Company Workspace", "url": f"/company_file?t={tk}"},
                        {"label": f"{peer} Workspace", "url": f"/company_file?t={peer}"},
                    ]
                    if _proposal_upsert(
                        con,
                        source_event_key=src_key,
                        kind="contagion_peer_alert",
                        ticker=tk,
                        title=title,
                        bullets=bullets,
                        citations=cites,
                        confidence=0.88,
                        priority_score=9.2 + min(3.0, abs(d) / 10.0),
                        execute_route=f"/company_file?t={tk}",
                        execute_payload={"ticker": tk, "peer": peer, "day_pct": d},
                    ):
                        created += 1
        finally:
            con_onyx.close()

        _state_set(con, "last_report_fact_id", str(rf_max))
        _state_set(con, "last_filing_id", str(fl_max))
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
        con.close()


def list_action_proposals(status: str = "open", limit: int = 8) -> list[dict[str, Any]]:
    ensure_proactive_schema()
    st = str(status or "open").strip().lower()
    blue_chip_set: set[str] = set()
    bc = _conn_core()
    try:
        bc.execute(
            """CREATE TABLE IF NOT EXISTS blue_chips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL UNIQUE,
                added_at TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT ''
            )"""
        )
        for r in bc.execute("SELECT ticker FROM blue_chips").fetchall():
            tk = _safe_ticker(str(r["ticker"] or ""))
            if tk:
                blue_chip_set.add(tk)
    except Exception:
        blue_chip_set = set()
    finally:
        bc.close()

    con = _conn_core()
    try:
        name_map = _company_name_map()
        day_map = _day_pct_map()
        weight_map = _portfolio_weight_map()
        rows = con.execute(
            """SELECT id, created_at, updated_at, status, kind, ticker, title, thesis_json, citations_json,
                      confidence, priority_score, execute_route, source_event_key
               FROM action_proposals
               WHERE status = ?
               ORDER BY priority_score DESC, id DESC
               LIMIT ?""",
            (st, max(1, min(250, int(limit or 8) * 12))),
        ).fetchall()
        raw_items: list[dict[str, Any]] = []
        for r in rows:
            try:
                bullets = list(json.loads(str(r["thesis_json"] or "[]")))
            except Exception:
                bullets = []
            try:
                cites = list(json.loads(str(r["citations_json"] or "[]")))
            except Exception:
                cites = []
            raw_items.append(
                {
                    "id": int(r["id"] or 0),
                    "created_at": str(r["created_at"] or ""),
                    "status": str(r["status"] or ""),
                    "kind": str(r["kind"] or ""),
                    "ticker": str(r["ticker"] or ""),
                    "title": str(r["title"] or ""),
                    "bullets": [str(x) for x in bullets[:3]],
                    "citations": cites[:6],
                    "confidence": float(r["confidence"] or 0.0),
                    "priority_score": float(r["priority_score"] or 0.0),
                    "execute_route": str(r["execute_route"] or ""),
                    "source_event_key": str(r["source_event_key"] or ""),
                    "is_blue_chip": str(r["ticker"] or "").strip().upper() in blue_chip_set,
                }
            )

        # Consolidate alert fatigue: merge multiple open signals per ticker into one card.
        grouped: dict[str, list[dict[str, Any]]] = {}
        for p in raw_items:
            tk = _safe_ticker(str(p.get("ticker") or ""))
            key = tk or f"__id_{int(p.get('id') or 0)}"
            grouped.setdefault(key, []).append(p)

        out: list[dict[str, Any]] = []
        for key, items in grouped.items():
            items_sorted = sorted(items, key=lambda x: float(x.get("priority_score") or 0.0), reverse=True)
            base = dict(items_sorted[0])
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
            dabs = abs(float(day_map.get(tk, 0.0) or 0.0))
            direction = _infer_direction(str(p.get("kind") or ""), str(p.get("title") or ""), list(p.get("bullets") or []))
            p["direction"] = direction
            p["company_name"] = nm
            p["portfolio_weight_pct"] = w
            p["max_add_pct"] = _suggest_max_add_pct(w, dabs)
            # Headline clarity.
            base_title = str(p.get("title") or "").strip()
            if tk:
                bt = base_title
                if bt.upper().startswith((tk + " ·").upper()):
                    bt = bt[len(tk) + 2 :].strip()
                p["title"] = f"{tk} · {direction} · {bt}"
            # Add cross-read vector for linked-company alerts.
            if kind in {"contagion_peer_alert", "macro_signal", "macro_context"}:
                vec = _build_cross_read_vector(tk, list(p.get("bullets") or []))
                if vec:
                    bs = list(p.get("bullets") or [])
                    if vec not in bs:
                        p["bullets"] = [vec] + bs[:2]
            bs = [str(x or "").strip() for x in list(p.get("bullets") or []) if str(x or "").strip()]
            p["insights"] = _extract_company_insights(bs)
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
        out_sorted = sorted(out, key=lambda x: float(x.get("priority_score") or 0.0), reverse=True)
        return out_sorted[: max(1, min(50, int(limit or 8)))]
    finally:
        con.close()


def dismiss_action_proposal(proposal_id: int, reason: str = "") -> bool:
    ensure_proactive_schema()
    pid = int(proposal_id or 0)
    if pid <= 0:
        return False
    ok = {"done": False}

    def _write() -> None:
        con = _conn_core()
        try:
            con.execute(
                "UPDATE action_proposals SET status='dismissed', updated_at=?, dismissed_reason=? WHERE id=?",
                (dt.datetime.now().isoformat(), str(reason or "")[:240], pid),
            )
            ok["done"] = con.total_changes > 0
            con.commit()
        finally:
            con.close()

    sqlite_retry(_write)
    return bool(ok["done"])


def reject_action_proposal(proposal_id: int, reason: str = "") -> bool:
    ensure_proactive_schema()
    pid = int(proposal_id or 0)
    if pid <= 0:
        return False
    rs = str(reason or "").strip()[:500]
    ok = {"done": False}

    def _write() -> None:
        con = _conn_core()
        try:
            con.execute(
                "UPDATE action_proposals SET status='rejected', updated_at=?, rejection_reason=?, rejected_at=? WHERE id=?",
                (dt.datetime.now().isoformat(), rs, dt.datetime.now().isoformat(), pid),
            )
            ok["done"] = con.total_changes > 0
            con.commit()
        finally:
            con.close()

    sqlite_retry(_write)
    return bool(ok["done"])


def _upsert_onyx_preference(preference_key: str, preference_value: str, context_reason: str, source_ref: str = "reject_feedback") -> bool:
    ensure_proactive_schema()
    k = str(preference_key or "").strip().lower()[:120]
    v = str(preference_value or "").strip()[:1200]
    c = str(context_reason or "").strip()[:1200]
    s = str(source_ref or "reject_feedback").strip()[:120]
    if not k or not v:
        return False
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
    con = _conn_core()
    try:
        row = con.execute(
            """SELECT id, ticker, kind, title, thesis_json, citations_json
               FROM action_proposals
               WHERE id=? LIMIT 1""",
            (pid,),
        ).fetchone()
    finally:
        con.close()
    if not row:
        return {"ok": False, "error": "proposal_not_found"}

    ticker = _safe_ticker(str(row["ticker"] or ""))
    kind = str(row["kind"] or "").strip()
    title = str(row["title"] or "").strip()
    try:
        bullets = list(json.loads(str(row["thesis_json"] or "[]")))
    except Exception:
        bullets = []
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
    con = _conn_onyx()
    seeds: list[int] = []
    text = str(event_description or "").strip()
    low = text.lower()
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
    con = _conn_onyx()
    out_ids: set[int] = set()
    id_to_ticker: dict[int, str] = {}
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

    con = _conn_onyx()
    impact_scores: dict[int, float] = {}
    impact_depth: dict[int, int] = {}
    citations: set[str] = set()
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
                t_id = int(rr["t_id"] or 0)
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
    con = _conn_core()
    try:
        row = con.execute(
            "SELECT execute_route, execute_payload_json FROM action_proposals WHERE id=? LIMIT 1",
            (pid,),
        ).fetchone()
        if not row:
            return {"ok": False, "route": "/dashboard", "error": "not_found"}
        route = str(row["execute_route"] or "/dashboard").strip()
        payload_raw = str(row["execute_payload_json"] or "{}")
        try:
            payload = json.loads(payload_raw)
        except Exception:
            payload = {}
        con.execute(
            "UPDATE action_proposals SET status='executed', updated_at=?, executed_at=? WHERE id=?",
            (dt.datetime.now().isoformat(), dt.datetime.now().isoformat(), pid),
        )
        con.commit()
        # Human-in-loop: execute means open workspace/draft context; no trade placement.
        return {"ok": True, "route": route if route.startswith("/") else "/dashboard", "payload": payload}
    finally:
        con.close()
