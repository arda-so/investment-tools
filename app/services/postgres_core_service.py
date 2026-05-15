from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
import uuid
from typing import Any

from sqlalchemy.pool import QueuePool
from app.services.postgres_schema_core import (
    ensure_postgres_core_annotation_tables,
    ensure_postgres_core_base_tables,
    ensure_postgres_core_filing_tables,
    ensure_postgres_core_intelligence_tables,
    ensure_postgres_core_memory_tables,
    ensure_postgres_core_monitoring_tables,
    ensure_postgres_core_operator_tables,
    ensure_postgres_core_registry_tables,
    ensure_postgres_core_thesis_tables,
)


def core_backend() -> str:
    return str(os.getenv("CORE_DB_BACKEND", "postgres")).strip().lower()


def pg_enabled() -> bool:
    return core_backend() == "postgres"


def strict_postgres_mode() -> bool:
    return str(os.getenv("CORE_DB_STRICT_POSTGRES", "1")).strip().lower() in {"1", "true", "yes", "on"}


def _is_non_dev_env() -> bool:
    env = str(os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "dev"))).strip().lower()
    return env in {"prod", "production", "staging"}


def pg_dsn() -> str:
    return str(os.getenv("POSTGRES_DSN", "")).strip()


def _pg_client():
    try:
        import psycopg  # type: ignore

        return ("psycopg", psycopg)
    except Exception:
        pass
    try:
        import psycopg2  # type: ignore

        return ("psycopg2", psycopg2)
    except Exception:
        return ("", None)


_PG_POOL_LOCK = threading.Lock()
_PG_POOL: QueuePool | None = None


def _pool_size() -> int:
    try:
        return max(1, int(os.getenv("POSTGRES_POOL_SIZE", "8")))
    except Exception:
        return 8


def _max_overflow() -> int:
    try:
        return max(0, int(os.getenv("POSTGRES_MAX_OVERFLOW", "16")))
    except Exception:
        return 16


def _pool_recycle_sec() -> int:
    try:
        return max(30, int(os.getenv("POSTGRES_POOL_RECYCLE_SEC", "1800")))
    except Exception:
        return 1800


def _iter_cursor_rows(cur: Any, batch_size: int = 256):
    # Stream rows in chunks to avoid full materialization with fetchall().
    size = max(1, int(batch_size or 256))
    while True:
        rows = cur.fetchmany(size)
        if not rows:
            break
        for row in rows:
            yield row


def _extract_ticker_from_text(text: str) -> str:
    s = str(text or "").strip()
    if not s:
        return ""
    # Explicit ticker mention only (e.g., "$AAPL") to avoid noisy auto-linking.
    m = re.search(r"\$([A-Za-z]{1,5})\b", s)
    if not m:
        return ""
    return str(m.group(1) or "").strip().upper()[:16]


def _pg_creator():
    dsn = pg_dsn()
    if not dsn:
        raise RuntimeError("missing_POSTGRES_DSN")
    _name, mod = _pg_client()
    if mod is None:
        raise RuntimeError("pg_driver_unavailable")
    # keepalives prevent idle connection drops from Cloud SQL proxy
    # connect_timeout prevents hanging forever on cold Cloud SQL start
    try:
        return mod.connect(
            dsn,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
            connect_timeout=10,
        )
    except TypeError:
        # psycopg v3 uses different kwarg names; fall back to plain connect
        return mod.connect(dsn)


def pg_pool() -> QueuePool | None:
    global _PG_POOL
    dsn = pg_dsn()
    if not dsn:
        return None
    if _PG_POOL is not None:
        return _PG_POOL
    with _PG_POOL_LOCK:
        if _PG_POOL is not None:
            return _PG_POOL
        try:
            _PG_POOL = QueuePool(
                _pg_creator,
                pool_size=_pool_size(),
                max_overflow=_max_overflow(),
                recycle=_pool_recycle_sec(),
                pre_ping=True,
                timeout=5,  # fail fast if pool exhausted (prevents 30s/call blocking at startup)
            )
        except Exception:
            _PG_POOL = None
    return _PG_POOL


def reset_pg_pool() -> None:
    global _PG_POOL
    with _PG_POOL_LOCK:
        if _PG_POOL is not None:
            try:
                _PG_POOL.dispose()
            except Exception:
                pass
        _PG_POOL = None


def pg_connect():
    pool = pg_pool()
    if pool is None:
        return None
    try:
        # SQLAlchemy pool proxy; .close() returns connection to pool.
        return pool.connect()
    except Exception:
        # Self-heal stale/broken pool state (e.g., env/DSN changed at runtime).
        reset_pg_pool()
        pool2 = pg_pool()
        if pool2 is None:
            return None
        try:
            return pool2.connect()
        except Exception:
            return None


def ensure_postgres_core_schema() -> dict[str, Any]:
    con = pg_connect()
    if con is None:
        return {"ok": False, "error": "pg_not_available"}
    try:
        cur = con.cursor()
        ensure_postgres_core_base_tables(cur)
        ensure_postgres_core_thesis_tables(cur)
        ensure_postgres_core_filing_tables(cur)
        ensure_postgres_core_annotation_tables(cur)
        # Heavy backfill is opt-in to keep app startup fast and deterministic.
        if str(os.getenv("CORE_DB_ANNOTATIONS_BACKFILL", "0")).strip().lower() in {"1", "true", "yes", "on"}:
            cur.execute(
                """
                INSERT INTO investor_annotations_core
                (entity_type, entity_id, annotation_type, content, status, priority, due_date, category, snooze_until, source_ref, created_by, created_at, updated_at)
                SELECT
                  CASE WHEN COALESCE(ticker,'') <> '' THEN 'company' ELSE 'global' END,
                  COALESCE(ticker,''),
                  'task',
                  COALESCE(task,''),
                  COALESCE(status,'open'),
                  COALESCE(priority,'P2'),
                  COALESCE(due_date,''),
                  COALESCE(category,'general'),
                  COALESCE(snooze_until,''),
                  'todo:' || COALESCE(id,0)::text,
                  'human',
                  COALESCE(created_at,''),
                  COALESCE(created_at,'')
                FROM todos_core
                ON CONFLICT DO NOTHING
                """
            )
            cur.execute(
                """
                INSERT INTO investor_annotations_core
                (entity_type, entity_id, annotation_type, content, status, priority, due_date, category, snooze_until, source_ref, created_by, created_at, updated_at)
                SELECT
                  CASE WHEN COALESCE(ticker,'') <> '' THEN 'company' ELSE 'global' END,
                  COALESCE(ticker,''),
                  'note',
                  COALESCE(note,''),
                  COALESCE(status,'approved'),
                  'P2',
                  '',
                  'general',
                  '',
                  'invnote:' || COALESCE(id,0)::text,
                  COALESCE(created_by,'human'),
                  COALESCE(created_at,''),
                  COALESCE(created_at,'')
                FROM investor_notes_core
                ON CONFLICT DO NOTHING
                """
            )
            cur.execute(
                """
                INSERT INTO investor_annotations_core
                (entity_type, entity_id, annotation_type, content, status, priority, due_date, category, snooze_until, source_ref, created_by, created_at, updated_at)
                SELECT
                  CASE WHEN COALESCE(ticker,'') <> '' THEN 'company' ELSE 'global' END,
                  COALESCE(ticker,''),
                  'note',
                  COALESCE(note,''),
                  COALESCE(status,'approved'),
                  'P2',
                  '',
                  'general',
                  '',
                  'wj:' || COALESCE(id,0)::text,
                  COALESCE(created_by,'human'),
                  COALESCE(created_at,''),
                  COALESCE(created_at,'')
                FROM workspace_journal_core
                ON CONFLICT DO NOTHING
                """
            )
        ensure_postgres_core_memory_tables(cur)
        ensure_postgres_core_registry_tables(cur)
        ensure_postgres_core_monitoring_tables(cur)
        ensure_postgres_core_intelligence_tables(cur)
        ensure_postgres_core_operator_tables(cur)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS investor_question_overrides_core (
                key TEXT PRIMARY KEY,
                question TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_interview_queue_core (
                id BIGINT PRIMARY KEY,
                ticker TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                step INTEGER NOT NULL DEFAULT 0,
                last_question TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                completed_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_piq_core_status ON portfolio_interview_queue_core(status)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS report_fact_ingest_state_core (
                state_key TEXT PRIMARY KEY,
                state_value TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS morning_briefs_core (
                id BIGINT PRIMARY KEY,
                brief_day TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                brief_json JSONB NOT NULL DEFAULT '{}'::jsonb
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_morning_briefs_core_day ON morning_briefs_core(brief_day)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS learning_events_core (
                id BIGINT PRIMARY KEY,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                payload_json JSONB NOT NULL DEFAULT '{}'::jsonb
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_learning_events_core_created ON learning_events_core(created_at DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS rule_candidates_core (
                id BIGINT PRIMARY KEY,
                rule_key TEXT NOT NULL UNIQUE,
                rule_text TEXT NOT NULL DEFAULT '',
                source_pattern TEXT NOT NULL DEFAULT '',
                support_count INTEGER NOT NULL DEFAULT 0,
                accept_count INTEGER NOT NULL DEFAULT 0,
                reject_count INTEGER NOT NULL DEFAULT 0,
                confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                status TEXT NOT NULL DEFAULT 'candidate',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                last_evaluated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_rule_candidates_core_conf ON rule_candidates_core(confidence DESC, support_count DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS active_rules_core (
                id BIGINT PRIMARY KEY,
                rule_key TEXT NOT NULL UNIQUE,
                rule_text TEXT NOT NULL DEFAULT '',
                confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                source_candidate_id BIGINT NOT NULL DEFAULT 0,
                reuse_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                last_used_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_active_rules_core_status ON active_rules_core(status, confidence DESC, reuse_count DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_tool_log_core (
                id BIGINT PRIMARY KEY,
                created_at TEXT NOT NULL,
                query TEXT NOT NULL DEFAULT '',
                tool_name TEXT NOT NULL DEFAULT '',
                args_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                status TEXT NOT NULL DEFAULT '',
                latency_ms DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                error TEXT NOT NULL DEFAULT '',
                trace_id TEXT NOT NULL DEFAULT '',
                model_name TEXT NOT NULL DEFAULT '',
                capability TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_tool_log_core_created ON ai_tool_log_core(created_at DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_quality_log_core (
                id BIGINT PRIMARY KEY,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL DEFAULT '',
                query TEXT NOT NULL DEFAULT '',
                detail_json JSONB NOT NULL DEFAULT '{}'::jsonb
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_quality_log_core_created ON ai_quality_log_core(created_at DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS risk_veto_decisions_core (
                id BIGINT PRIMARY KEY,
                created_at TEXT NOT NULL,
                trace_id TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL DEFAULT '',
                query TEXT NOT NULL DEFAULT '',
                verdict TEXT NOT NULL DEFAULT '',
                confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                reason TEXT NOT NULL DEFAULT '',
                metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                detail_json JSONB NOT NULL DEFAULT '{}'::jsonb
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_risk_veto_decisions_core_created ON risk_veto_decisions_core(created_at DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS risk_veto_config_core (
                id INTEGER PRIMARY KEY,
                config_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            INSERT INTO risk_veto_config_core (id, config_json, updated_at)
            VALUES (1, '{}'::jsonb, %s)
            ON CONFLICT(id) DO NOTHING
            """,
            (dt.datetime.now().isoformat(),),
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_operator_state_core (
                state_key TEXT PRIMARY KEY,
                state_value TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_operator_gap_prompts_core (
                id BIGINT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                prompt_id TEXT NOT NULL UNIQUE,
                user_name TEXT NOT NULL DEFAULT '',
                prompt_text TEXT NOT NULL DEFAULT '',
                context_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                context_fingerprint TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                answered_at TEXT NOT NULL DEFAULT '',
                answer_text TEXT NOT NULL DEFAULT '',
                resolved_json JSONB NOT NULL DEFAULT '{}'::jsonb
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_daily_operator_gap_status_core ON daily_operator_gap_prompts_core(status, id DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_preferences_core (
                id BIGINT PRIMARY KEY,
                pref_key TEXT NOT NULL UNIQUE,
                pref_value TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'chat',
                preference_key TEXT NOT NULL DEFAULT '',
                preference_value TEXT NOT NULL DEFAULT '',
                context_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_preferences_core_updated ON user_preferences_core(updated_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_user_preferences_core_key ON user_preferences_core(preference_key)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_meta_suggestions_core (
                id BIGINT PRIMARY KEY,
                created_at TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                priority DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                source TEXT NOT NULL DEFAULT 'heuristic',
                status TEXT NOT NULL DEFAULT 'open'
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_meta_suggestions_core_created ON ai_meta_suggestions_core(created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_meta_suggestions_core_status ON ai_meta_suggestions_core(status, priority DESC)")

        # Phase 3.2: Outcome Tracking
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS proposal_outcomes_core (
                id BIGINT PRIMARY KEY,
                proposal_id BIGINT NOT NULL,
                ticker TEXT NOT NULL,
                outcome_type TEXT NOT NULL DEFAULT 'price',
                created_at TEXT NOT NULL,
                measured_at TEXT NOT NULL DEFAULT '',
                measurement_window TEXT NOT NULL DEFAULT '',
                baseline_price DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                outcome_price DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                return_pct DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                direction_correct BOOLEAN DEFAULT NULL,
                thesis_confirmed BOOLEAN DEFAULT NULL,
                confidence_at_proposal TEXT NOT NULL DEFAULT '',
                stance_at_proposal TEXT NOT NULL DEFAULT '',
                reasoning_summary TEXT NOT NULL DEFAULT '',
                score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                status TEXT NOT NULL DEFAULT 'pending'
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_proposal_outcomes_core_proposal ON proposal_outcomes_core(proposal_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_proposal_outcomes_core_status ON proposal_outcomes_core(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_proposal_outcomes_core_ticker ON proposal_outcomes_core(ticker)")

        # Phase 3.3: Thesis Breach Alerts
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS thesis_breach_alerts_core (
                id BIGINT PRIMARY KEY,
                ticker TEXT NOT NULL,
                thesis_id BIGINT NOT NULL DEFAULT 0,
                breach_type TEXT NOT NULL DEFAULT '',
                breach_detail TEXT NOT NULL DEFAULT '',
                severity TEXT NOT NULL DEFAULT 'medium',
                detected_at TEXT NOT NULL,
                financial_context TEXT NOT NULL DEFAULT '',
                thesis_text TEXT NOT NULL DEFAULT '',
                invalidation_criteria TEXT NOT NULL DEFAULT '',
                actual_values TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open'
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_thesis_breach_core_ticker ON thesis_breach_alerts_core(ticker)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_thesis_breach_core_status ON thesis_breach_alerts_core(status)")

        # Phase 3.4: Cross-Portfolio Cascade Alerts
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_cascade_alerts_core (
                id BIGINT PRIMARY KEY,
                trigger_ticker TEXT NOT NULL,
                trigger_signal TEXT NOT NULL DEFAULT '',
                trigger_proposal_id BIGINT NOT NULL DEFAULT 0,
                affected_ticker TEXT NOT NULL,
                effect_type TEXT NOT NULL DEFAULT '',
                effect_summary TEXT NOT NULL DEFAULT '',
                relationship TEXT NOT NULL DEFAULT '',
                confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                severity TEXT NOT NULL DEFAULT 'medium',
                detected_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cascade_core_trigger ON portfolio_cascade_alerts_core(trigger_ticker)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cascade_core_affected ON portfolio_cascade_alerts_core(affected_ticker)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cascade_core_status ON portfolio_cascade_alerts_core(status)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cascade_lag_stats_core (
                trigger_ticker TEXT NOT NULL,
                affected_ticker TEXT NOT NULL,
                sample_count INTEGER NOT NULL DEFAULT 0,
                avg_lag_days DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                last_lag_days INTEGER NOT NULL DEFAULT 0,
                last_seen_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (trigger_ticker, affected_ticker)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cascade_lag_trigger ON cascade_lag_stats_core(trigger_ticker)")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_response_feedback (
                id BIGSERIAL PRIMARY KEY,
                created_at TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                message_id TEXT NOT NULL DEFAULT '',
                query_text TEXT NOT NULL DEFAULT '',
                response_text TEXT NOT NULL DEFAULT '',
                intent TEXT NOT NULL DEFAULT '',
                rating TEXT NOT NULL DEFAULT '',
                comment TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_feedback_rating ON ai_response_feedback(rating)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_feedback_session ON ai_response_feedback(session_id)")

        con.commit()
        return {"ok": True}
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}
    finally:
        con.close()



def verify_postgres_core_ready() -> dict[str, Any]:
    dsn = pg_dsn()
    if not dsn:
        return {"ok": False, "error": "missing_POSTGRES_DSN"}
    _name, mod = _pg_client()
    if mod is None:
        return {"ok": False, "error": "pg_driver_unavailable"}
    try:
        con_pg = mod.connect(dsn)
    except Exception as exc:
        return {"ok": False, "error": f"pg_connect_failed:{exc}"}
    required = [
        "action_proposals_core",
        "portfolio_transactions_core",
        "watchlist_thesis_core",
        "investor_style_memory_core",
        "filings_core",
        "report_facts_core",
        "todos_core",
        "workspace_journal_core",
        "investor_notes_core",
        "company_reminders_core",
        "company_profile_cache_core",
        "companies_core",
        "universe_registry_core",
        "company_lists_core",
        "company_list_items_core",
        "company_moat_tags_core",
        "company_sec_competitors_core",
        "blue_chips_core",
        "agent_runs_core",
        "reflexion_notes_core",
        "failure_patterns_core",
        "reflexion_policy_versions_core",
        "intel24_snapshot_core",
        "intel_feed_core",
        "changes_core",
        "daily_notes_core",
        "daily_note_tags_core",
        "ai_action_queue_core",
        "ai_action_log_core",
        "system_events_core",
        "decision_log_core",
        "investor_question_overrides_core",
        "portfolio_interview_queue_core",
        "report_fact_ingest_state_core",
        "morning_briefs_core",
        "learning_events_core",
        "rule_candidates_core",
        "active_rules_core",
        "ai_tool_log_core",
        "ai_quality_log_core",
        "risk_veto_decisions_core",
        "risk_veto_config_core",
        "daily_operator_state_core",
        "daily_operator_gap_prompts_core",
        "user_preferences_core",
        "ai_meta_suggestions_core",
    ]
    try:
        cur = con_pg.cursor()
        cur.execute("SELECT 1")
        _ = cur.fetchone()
        missing: list[str] = []
        for t in required:
            cur.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=%s LIMIT 1",
                (t,),
            )
            if not cur.fetchone():
                missing.append(t)
        if missing:
            return {"ok": False, "error": "missing_tables", "missing": missing}
        return {"ok": True, "backend": "postgres", "required_tables": required}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            con_pg.close()
        except Exception:
            pass


def guard_core_backend_cutover() -> dict[str, Any]:
    backend = core_backend()
    enforce = str(os.getenv("CORE_DB_GUARD_ENFORCE", "1")).strip().lower() in {"1", "true", "yes", "on"}
    if backend != "postgres":
        return {"ok": True, "backend": backend, "guard_enforced": enforce, "switched": False, "reason": "backend_not_postgres"}
    if not enforce:
        return {"ok": True, "backend": backend, "guard_enforced": False, "switched": False, "reason": "guard_disabled"}
    # In non-dev, never auto-downgrade to sqlite. Keep postgres as source of truth.
    if _is_non_dev_env():
        v_pg = verify_postgres_core_ready()
        return {
            "ok": bool(v_pg.get("ok")),
            "backend": "postgres",
            "guard_enforced": True,
            "switched": False,
            "reason": "non_dev_no_downgrade",
            "verify": v_pg,
        }
    v_pg = verify_postgres_core_ready()
    if not bool(v_pg.get("ok")):
        return {"ok": False, "backend": "postgres", "guard_enforced": True, "switched": False, "reason": "verify_failed", "verify": v_pg}
    return {"ok": True, "backend": "postgres", "guard_enforced": True, "switched": False, "reason": "postgres_ready", "verify": v_pg}


def enforce_strict_postgres_ready() -> dict[str, Any]:
    if not strict_postgres_mode():
        return {"ok": True, "strict": False, "enforced": False, "reason": "strict_disabled"}
    if core_backend() != "postgres":
        raise RuntimeError("strict_postgres_requires_core_db_backend_postgres")

    is_cloud = _is_non_dev_env() or str(os.getenv("APP_ENV", "")).strip().lower() == "cloud"
    try:
        max_wait = float(os.getenv("STRICT_POSTGRES_READY_MAX_WAIT_SEC", "45" if is_cloud else "5"))
    except Exception:
        max_wait = 45.0 if is_cloud else 5.0
    try:
        sleep_sec = float(os.getenv("STRICT_POSTGRES_READY_RETRY_SEC", "2"))
    except Exception:
        sleep_sec = 2.0
    deadline = time.time() + max(0.0, max_wait)
    v: dict[str, Any] = {"ok": False, "error": "not_checked"}
    while True:
        v = verify_postgres_core_ready()
        if bool(v.get("ok")):
            break
        if time.time() >= deadline:
            raise RuntimeError("strict_postgres_verify_failed:" + str(v.get("error") or "unknown"))
        time.sleep(max(0.2, sleep_sec))
    return {"ok": True, "strict": True, "enforced": True, "reason": "postgres_ready", "verify": v}


def list_action_proposals_pg(status: str = "open", limit: int = 96) -> list[dict[str, Any]]:
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, created_at, updated_at, status, kind, ticker, title, thesis_json, citations_json, insights_json, reasoning_json,
                   confidence, priority_score, execute_route, source_event_key
            FROM action_proposals_core
            WHERE status=%s
            ORDER BY priority_score DESC, id DESC
            LIMIT %s
            """,
            (str(status or "open"), max(1, min(300, int(limit or 96)))),
        )
        out: list[dict[str, Any]] = []
        for r in _iter_cursor_rows(cur):
            out.append(
                {
                    "id": int(r[0] or 0),
                    "created_at": str(r[1] or ""),
                    "updated_at": str(r[2] or ""),
                    "status": str(r[3] or ""),
                    "kind": str(r[4] or ""),
                    "ticker": str(r[5] or ""),
                    "title": str(r[6] or ""),
                    "thesis_json": list(r[7] or []),
                    "citations_json": list(r[8] or []),
                    "insights_json": list(r[9] or []),
                    "reasoning_json": dict(r[10] or {}),
                    "confidence": float(r[11] or 0.0),
                    "priority_score": float(r[12] or 0.0),
                    "execute_route": str(r[13] or ""),
                    "source_event_key": str(r[14] or ""),
                }
            )
        return out
    except Exception:
        return []
    finally:
        con.close()


def list_portfolio_transactions_pg(limit: int = 100, ticker: str = "", year: int = 0) -> list[dict[str, Any]]:
    con = pg_connect()
    if con is None:
        return []
    try:
        clauses: list[str] = []
        vals: list[Any] = []
        if str(ticker or "").strip():
            clauses.append("ticker = %s")
            vals.append(str(ticker or "").strip().upper())
        if int(year or 0) >= 1900:
            # Support mixed broker timestamp formats by extracting a 4-digit year
            # from anywhere in created_at instead of relying on lexicographic ranges.
            y = int(year)
            clauses.append("COALESCE(NULLIF(substring(created_at from '([12][0-9]{3})'), '')::int, 0) = %s")
            vals.append(y)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        q = (
            "SELECT created_at, ticker, action, shares, price, note, source "
            "FROM portfolio_transactions_core "
            + where
            + " ORDER BY created_at DESC, id DESC LIMIT %s"
        )
        vals.append(max(1, min(2000, int(limit or 100))))
        cur = con.cursor()
        cur.execute(q, tuple(vals))
        out: list[dict[str, Any]] = []
        for r in _iter_cursor_rows(cur):
            out.append(
                {
                    "created_at": str(r[0] or ""),
                    "ticker": str(r[1] or ""),
                    "action": str(r[2] or ""),
                    "shares": float(r[3] or 0.0),
                    "price": float(r[4] or 0.0),
                    "note": str(r[5] or ""),
                    "source": str(r[6] or ""),
                }
            )
        return out
    except Exception:
        return []
    finally:
        con.close()


def list_recent_portfolio_transactions_pg(limit: int = 20, ticker: str = "") -> list[dict[str, Any]]:
    return list_portfolio_transactions_pg(limit=limit, ticker=ticker, year=0)


def ensure_price_metrics_schema_pg() -> bool:
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS price_metrics_core (
                ticker TEXT PRIMARY KEY,
                asof TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                ytd_return DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                m12_return DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                y5_return DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                ytd_start_px DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                ytd_end_px DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                m12_start_px DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                m12_end_px DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                y5_start_px DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                y5_end_px DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        try:
            cur.execute("ALTER TABLE price_metrics_core ADD COLUMN IF NOT EXISTS ytd_start_px DOUBLE PRECISION NOT NULL DEFAULT 0.0")
            cur.execute("ALTER TABLE price_metrics_core ADD COLUMN IF NOT EXISTS ytd_end_px DOUBLE PRECISION NOT NULL DEFAULT 0.0")
            cur.execute("ALTER TABLE price_metrics_core ADD COLUMN IF NOT EXISTS m12_start_px DOUBLE PRECISION NOT NULL DEFAULT 0.0")
            cur.execute("ALTER TABLE price_metrics_core ADD COLUMN IF NOT EXISTS m12_end_px DOUBLE PRECISION NOT NULL DEFAULT 0.0")
            cur.execute("ALTER TABLE price_metrics_core ADD COLUMN IF NOT EXISTS y5_start_px DOUBLE PRECISION NOT NULL DEFAULT 0.0")
            cur.execute("ALTER TABLE price_metrics_core ADD COLUMN IF NOT EXISTS y5_end_px DOUBLE PRECISION NOT NULL DEFAULT 0.0")
        except Exception:
            pass
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def get_price_metrics_pg(ticker: str) -> dict[str, Any]:
    _ = ensure_price_metrics_schema_pg()
    tk = str(ticker or "").strip().upper()[:16]
    if not tk:
        return {}
    con = pg_connect()
    if con is None:
        return {}
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='price_metrics_core'
            """
        )
        cols = {str(x[0] or "").strip().lower() for x in _iter_cursor_rows(cur)}
        extra_sel = []
        for nm in ("ytd_start_px", "ytd_end_px", "m12_start_px", "m12_end_px", "y5_start_px", "y5_end_px"):
            extra_sel.append(nm if nm in cols else "0.0")
        cur.execute(
            f"""
            SELECT ticker, asof, source, ytd_return, m12_return, y5_return, updated_at,
                   {extra_sel[0]}, {extra_sel[1]}, {extra_sel[2]}, {extra_sel[3]}, {extra_sel[4]}, {extra_sel[5]}
            FROM price_metrics_core
            WHERE ticker=%s
            LIMIT 1
            """,
            (tk,),
        )
        r = cur.fetchone()
        if not r:
            return {}
        return {
            "ticker": str(r[0] or ""),
            "asof": str(r[1] or ""),
            "source": str(r[2] or ""),
            "ytd_return": float(r[3] or 0.0),
            "m12_return": float(r[4] or 0.0),
            "y5_return": float(r[5] or 0.0),
            "updated_at": str(r[6] or ""),
            "ytd_start_px": float(r[7] or 0.0),
            "ytd_end_px": float(r[8] or 0.0),
            "m12_start_px": float(r[9] or 0.0),
            "m12_end_px": float(r[10] or 0.0),
            "y5_start_px": float(r[11] or 0.0),
            "y5_end_px": float(r[12] or 0.0),
        }
    except Exception:
        return {}
    finally:
        con.close()


def upsert_price_metrics_pg(
    *,
    ticker: str,
    asof: str,
    source: str,
    ytd_return: float,
    m12_return: float,
    y5_return: float,
    ytd_start_px: float = 0.0,
    ytd_end_px: float = 0.0,
    m12_start_px: float = 0.0,
    m12_end_px: float = 0.0,
    y5_start_px: float = 0.0,
    y5_end_px: float = 0.0,
) -> bool:
    _ = ensure_price_metrics_schema_pg()
    tk = str(ticker or "").strip().upper()[:16]
    if not tk:
        return False
    con = pg_connect()
    if con is None:
        return False
    now = dt.datetime.now().isoformat()
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='price_metrics_core'
            """
        )
        cols = {str(x[0] or "").strip().lower() for x in _iter_cursor_rows(cur)}
        has_pairs = all(x in cols for x in ("ytd_start_px", "ytd_end_px", "m12_start_px", "m12_end_px", "y5_start_px", "y5_end_px"))
        if has_pairs:
            cur.execute(
                """
                INSERT INTO price_metrics_core
                (ticker, asof, source, ytd_return, m12_return, y5_return, ytd_start_px, ytd_end_px, m12_start_px, m12_end_px, y5_start_px, y5_end_px, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (ticker) DO UPDATE SET
                  asof=EXCLUDED.asof,
                  source=EXCLUDED.source,
                  ytd_return=EXCLUDED.ytd_return,
                  m12_return=EXCLUDED.m12_return,
                  y5_return=EXCLUDED.y5_return,
                  ytd_start_px=EXCLUDED.ytd_start_px,
                  ytd_end_px=EXCLUDED.ytd_end_px,
                  m12_start_px=EXCLUDED.m12_start_px,
                  m12_end_px=EXCLUDED.m12_end_px,
                  y5_start_px=EXCLUDED.y5_start_px,
                  y5_end_px=EXCLUDED.y5_end_px,
                  updated_at=EXCLUDED.updated_at
                """,
                (
                    tk,
                    str(asof or "")[:40],
                    str(source or "")[:40],
                    float(ytd_return or 0.0),
                    float(m12_return or 0.0),
                    float(y5_return or 0.0),
                    float(ytd_start_px or 0.0),
                    float(ytd_end_px or 0.0),
                    float(m12_start_px or 0.0),
                    float(m12_end_px or 0.0),
                    float(y5_start_px or 0.0),
                    float(y5_end_px or 0.0),
                    now,
                ),
            )
        else:
            cur.execute(
                """
                INSERT INTO price_metrics_core
                (ticker, asof, source, ytd_return, m12_return, y5_return, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (ticker) DO UPDATE SET
                  asof=EXCLUDED.asof,
                  source=EXCLUDED.source,
                  ytd_return=EXCLUDED.ytd_return,
                  m12_return=EXCLUDED.m12_return,
                  y5_return=EXCLUDED.y5_return,
                  updated_at=EXCLUDED.updated_at
                """,
                (
                    tk,
                    str(asof or "")[:40],
                    str(source or "")[:40],
                    float(ytd_return or 0.0),
                    float(m12_return or 0.0),
                    float(y5_return or 0.0),
                    now,
                ),
            )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def ensure_mini_statements_schema_pg() -> bool:
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mini_statements_core (
                ticker TEXT PRIMARY KEY,
                asof TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                currency TEXT NOT NULL DEFAULT 'USD',
                years_json TEXT NOT NULL DEFAULT '[]',
                revenue_json TEXT NOT NULL DEFAULT '[]',
                gross_profit_json TEXT NOT NULL DEFAULT '[]',
                operating_cash_flow_json TEXT NOT NULL DEFAULT '[]',
                capex_json TEXT NOT NULL DEFAULT '[]',
                free_cash_flow_json TEXT NOT NULL DEFAULT '[]',
                total_cash_json TEXT NOT NULL DEFAULT '[]',
                total_debt_json TEXT NOT NULL DEFAULT '[]',
                total_equity_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def ensure_company_intel_schema_pg() -> bool:
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS company_intel_core (
                ticker TEXT PRIMARY KEY,
                asof TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                revenue_segments_json TEXT NOT NULL DEFAULT '{}',
                buyback_json TEXT NOT NULL DEFAULT '{}',
                insider_trades_json TEXT NOT NULL DEFAULT '[]',
                status_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def get_company_intel_pg(ticker: str) -> dict[str, Any]:
    _ = ensure_company_intel_schema_pg()
    tk = str(ticker or "").strip().upper()[:16]
    if not tk:
        return {}
    con = pg_connect()
    if con is None:
        return {}
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT ticker, asof, source, revenue_segments_json, buyback_json, insider_trades_json, status_json, updated_at
            FROM company_intel_core
            WHERE ticker=%s
            LIMIT 1
            """,
            (tk,),
        )
        r = cur.fetchone()
        if not r:
            return {}

        def _loads(v: Any, fallback: str) -> Any:
            try:
                return json.loads(str(v or fallback))
            except Exception:
                return json.loads(fallback)

        return {
            "ticker": str(r[0] or ""),
            "asof": str(r[1] or ""),
            "source": str(r[2] or ""),
            "revenue_segments": _loads(r[3], "{}"),
            "buyback": _loads(r[4], "{}"),
            "insider_trades": _loads(r[5], "[]"),
            "status": _loads(r[6], "{}"),
            "updated_at": str(r[7] or ""),
        }
    except Exception:
        return {}
    finally:
        con.close()


def upsert_company_intel_pg(
    *,
    ticker: str,
    asof: str,
    source: str,
    revenue_segments: dict[str, Any] | None,
    buyback: dict[str, Any] | None,
    insider_trades: list[dict[str, Any]] | None,
    status: dict[str, Any] | None,
) -> bool:
    _ = ensure_company_intel_schema_pg()
    tk = str(ticker or "").strip().upper()[:16]
    if not tk:
        return False
    con = pg_connect()
    if con is None:
        return False
    now = dt.datetime.now().isoformat()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO company_intel_core
            (ticker, asof, source, revenue_segments_json, buyback_json, insider_trades_json, status_json, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (ticker) DO UPDATE SET
              asof=EXCLUDED.asof,
              source=EXCLUDED.source,
              revenue_segments_json=EXCLUDED.revenue_segments_json,
              buyback_json=EXCLUDED.buyback_json,
              insider_trades_json=EXCLUDED.insider_trades_json,
              status_json=EXCLUDED.status_json,
              updated_at=EXCLUDED.updated_at
            """,
            (
                tk,
                str(asof or "")[:40],
                str(source or "")[:40],
                json.dumps(dict(revenue_segments or {}), ensure_ascii=True),
                json.dumps(dict(buyback or {}), ensure_ascii=True),
                json.dumps(list(insider_trades or [])[:32], ensure_ascii=True),
                json.dumps(dict(status or {}), ensure_ascii=True),
                now,
            ),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def ensure_earnings_transcripts_schema_pg() -> bool:
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS earnings_transcripts_core (
                id BIGSERIAL PRIMARY KEY,
                ticker TEXT NOT NULL,
                call_date TEXT NOT NULL DEFAULT '',
                fiscal_year INTEGER,
                fiscal_quarter TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT 'sec_filing',
                source_url TEXT NOT NULL DEFAULT '',
                filing_id BIGINT NOT NULL DEFAULT 0,
                accession TEXT NOT NULL DEFAULT '',
                excerpt TEXT NOT NULL DEFAULT '',
                transcript_text TEXT NOT NULL DEFAULT '',
                char_count INTEGER NOT NULL DEFAULT 0,
                quality_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                is_partial BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                UNIQUE(ticker, call_date, accession, source_type)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_et_core_ticker_date ON earnings_transcripts_core(ticker, call_date DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_et_core_quality ON earnings_transcripts_core(ticker, quality_score DESC)")
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def upsert_earnings_transcript_pg(
    *,
    ticker: str,
    call_date: str,
    fiscal_year: int | None,
    fiscal_quarter: str,
    title: str,
    source_type: str,
    source_url: str,
    filing_id: int,
    accession: str,
    excerpt: str,
    transcript_text: str,
    char_count: int,
    quality_score: float,
    is_partial: bool,
) -> bool:
    _ = ensure_earnings_transcripts_schema_pg()
    tk = str(ticker or "").strip().upper()[:16]
    if not tk:
        return False
    con = pg_connect()
    if con is None:
        return False
    now = dt.datetime.now().isoformat()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO earnings_transcripts_core
            (ticker, call_date, fiscal_year, fiscal_quarter, title, source_type, source_url, filing_id, accession, excerpt, transcript_text, char_count, quality_score, is_partial, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (ticker, call_date, accession, source_type) DO UPDATE SET
              fiscal_year=EXCLUDED.fiscal_year,
              fiscal_quarter=EXCLUDED.fiscal_quarter,
              title=EXCLUDED.title,
              source_url=EXCLUDED.source_url,
              filing_id=EXCLUDED.filing_id,
              excerpt=EXCLUDED.excerpt,
              transcript_text=EXCLUDED.transcript_text,
              char_count=EXCLUDED.char_count,
              quality_score=EXCLUDED.quality_score,
              is_partial=EXCLUDED.is_partial,
              updated_at=EXCLUDED.updated_at
            """,
            (
                tk,
                str(call_date or "")[:20],
                int(fiscal_year) if fiscal_year is not None else None,
                str(fiscal_quarter or "")[:8],
                str(title or "")[:220],
                str(source_type or "sec_filing")[:40],
                str(source_url or "")[:1200],
                int(filing_id or 0),
                str(accession or "")[:80],
                str(excerpt or "")[:2400],
                str(transcript_text or "")[:220000],
                int(char_count or 0),
                float(quality_score or 0.0),
                bool(is_partial),
                now,
                now,
            ),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def list_earnings_transcripts_pg(ticker: str, limit: int = 24) -> list[dict[str, Any]]:
    _ = ensure_earnings_transcripts_schema_pg()
    tk = str(ticker or "").strip().upper()[:16]
    if not tk:
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        lim = max(1, min(200, int(limit or 24)))
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, ticker, call_date, fiscal_year, fiscal_quarter, title, source_type, source_url,
                   filing_id, accession, excerpt, char_count, quality_score, is_partial, updated_at
            FROM earnings_transcripts_core
            WHERE ticker=%s
            ORDER BY call_date DESC, quality_score DESC, id DESC
            LIMIT %s
            """,
            (tk, lim),
        )
        out: list[dict[str, Any]] = []
        for r in _iter_cursor_rows(cur):
            out.append(
                {
                    "id": int(r[0] or 0),
                    "ticker": str(r[1] or ""),
                    "call_date": str(r[2] or ""),
                    "fiscal_year": int(r[3]) if r[3] is not None else None,
                    "fiscal_quarter": str(r[4] or ""),
                    "title": str(r[5] or ""),
                    "source_type": str(r[6] or ""),
                    "source_url": str(r[7] or ""),
                    "filing_id": int(r[8] or 0),
                    "accession": str(r[9] or ""),
                    "excerpt": str(r[10] or ""),
                    "char_count": int(r[11] or 0),
                    "quality_score": float(r[12] or 0.0),
                    "is_partial": bool(r[13]),
                    "updated_at": str(r[14] or ""),
                }
            )
        return out
    except Exception:
        return []
    finally:
        con.close()


def delete_earnings_transcripts_by_ids_pg(ids: list[int]) -> int:
    _ = ensure_earnings_transcripts_schema_pg()
    clean_ids = [int(x) for x in (ids or []) if int(x or 0) > 0]
    if not clean_ids:
        return 0
    con = pg_connect()
    if con is None:
        return 0
    try:
        cur = con.cursor()
        marks = ",".join("%s" for _ in clean_ids)
        cur.execute(f"DELETE FROM earnings_transcripts_core WHERE id IN ({marks})", tuple(clean_ids))
        n = int(getattr(cur, "rowcount", 0) or 0)
        con.commit()
        return n
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return 0
    finally:
        con.close()


def ensure_earnings_call_artifacts_schema_pg() -> bool:
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ir_source_registry_core (
                ticker TEXT PRIMARY KEY,
                ir_home_url TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                rss_url TEXT NOT NULL DEFAULT '',
                last_good_audio_pattern TEXT NOT NULL DEFAULT '',
                last_good_event_url TEXT NOT NULL DEFAULT '',
                active BOOLEAN NOT NULL DEFAULT TRUE,
                meta_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ir_registry_provider_active ON ir_source_registry_core(provider, active)")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS earnings_call_artifacts_core (
                id BIGSERIAL PRIMARY KEY,
                ticker TEXT NOT NULL DEFAULT '',
                event_datetime TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                audio_uri TEXT NOT NULL DEFAULT '',
                audio_source_url TEXT NOT NULL DEFAULT '',
                audio_checksum TEXT NOT NULL DEFAULT '',
                transcript_text TEXT NOT NULL DEFAULT '',
                transcript_source TEXT NOT NULL DEFAULT 'whisper_local',
                transcript_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                ingest_status TEXT NOT NULL DEFAULT 'pending',
                idempotency_key TEXT NOT NULL DEFAULT '',
                meta_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_eca_core_idempotency ON earnings_call_artifacts_core(idempotency_key)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_eca_core_ticker_event_dt ON earnings_call_artifacts_core(ticker, event_datetime DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_eca_core_status_updated ON earnings_call_artifacts_core(ingest_status, updated_at DESC)")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS earnings_call_ingest_runs_core (
                id BIGSERIAL PRIMARY KEY,
                ticker TEXT NOT NULL DEFAULT '',
                event_datetime TEXT NOT NULL DEFAULT '',
                idempotency_key TEXT NOT NULL DEFAULT '',
                stage TEXT NOT NULL DEFAULT 'discovery',
                status TEXT NOT NULL DEFAULT 'queued',
                attempt INTEGER NOT NULL DEFAULT 0,
                error_text TEXT NOT NULL DEFAULT '',
                detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                next_retry_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ecir_core_status_retry ON earnings_call_ingest_runs_core(status, next_retry_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ecir_core_ticker_created ON earnings_call_ingest_runs_core(ticker, created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ecir_core_idempotency ON earnings_call_ingest_runs_core(idempotency_key)")
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def upsert_ir_source_registry_pg(
    *,
    ticker: str,
    ir_home_url: str,
    provider: str,
    rss_url: str,
    last_good_audio_pattern: str = "",
    last_good_event_url: str = "",
    active: bool = True,
    meta: dict[str, Any] | None = None,
) -> bool:
    _ = ensure_earnings_call_artifacts_schema_pg()
    tk = str(ticker or "").strip().upper()[:16]
    if not tk:
        return False
    con = pg_connect()
    if con is None:
        return False
    now = dt.datetime.now().isoformat()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO ir_source_registry_core
            (ticker, ir_home_url, provider, rss_url, last_good_audio_pattern, last_good_event_url, active, meta_json, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT(ticker) DO UPDATE SET
              ir_home_url=EXCLUDED.ir_home_url,
              provider=EXCLUDED.provider,
              rss_url=EXCLUDED.rss_url,
              last_good_audio_pattern=EXCLUDED.last_good_audio_pattern,
              last_good_event_url=EXCLUDED.last_good_event_url,
              active=EXCLUDED.active,
              meta_json=EXCLUDED.meta_json,
              updated_at=EXCLUDED.updated_at
            """,
            (
                tk,
                str(ir_home_url or "")[:1600],
                str(provider or "")[:80],
                str(rss_url or "")[:1600],
                str(last_good_audio_pattern or "")[:800],
                str(last_good_event_url or "")[:1600],
                bool(active),
                json.dumps(dict(meta or {}), ensure_ascii=True),
                now,
                now,
            ),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def get_ir_source_registry_pg(ticker: str) -> dict[str, Any]:
    _ = ensure_earnings_call_artifacts_schema_pg()
    tk = str(ticker or "").strip().upper()[:16]
    if not tk:
        return {}
    con = pg_connect()
    if con is None:
        return {}
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT ticker, ir_home_url, provider, rss_url, last_good_audio_pattern, last_good_event_url, active, meta_json, updated_at
            FROM ir_source_registry_core
            WHERE ticker=%s
            LIMIT 1
            """,
            (tk,),
        )
        r = cur.fetchone()
        if not r:
            return {}
        return {
            "ticker": str(r[0] or ""),
            "ir_home_url": str(r[1] or ""),
            "provider": str(r[2] or ""),
            "rss_url": str(r[3] or ""),
            "last_good_audio_pattern": str(r[4] or ""),
            "last_good_event_url": str(r[5] or ""),
            "active": bool(r[6]),
            "meta": dict(r[7] or {}) if isinstance(r[7], dict) else {},
            "updated_at": str(r[8] or ""),
        }
    except Exception:
        return {}
    finally:
        con.close()


def upsert_earnings_call_artifact_pg(
    *,
    ticker: str,
    event_datetime: str,
    title: str,
    audio_uri: str,
    audio_source_url: str,
    audio_checksum: str,
    transcript_text: str,
    transcript_source: str,
    transcript_confidence: float,
    ingest_status: str,
    idempotency_key: str,
    meta: dict[str, Any] | None = None,
) -> bool:
    _ = ensure_earnings_call_artifacts_schema_pg()
    tk = str(ticker or "").strip().upper()[:16]
    key = str(idempotency_key or "").strip()[:200]
    if not tk or not key:
        return False
    con = pg_connect()
    if con is None:
        return False
    now = dt.datetime.now().isoformat()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO earnings_call_artifacts_core
            (ticker, event_datetime, title, audio_uri, audio_source_url, audio_checksum, transcript_text, transcript_source, transcript_confidence, ingest_status, idempotency_key, meta_json, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT(idempotency_key) DO UPDATE SET
              ticker=EXCLUDED.ticker,
              event_datetime=EXCLUDED.event_datetime,
              title=EXCLUDED.title,
              audio_uri=EXCLUDED.audio_uri,
              audio_source_url=EXCLUDED.audio_source_url,
              audio_checksum=EXCLUDED.audio_checksum,
              transcript_text=EXCLUDED.transcript_text,
              transcript_source=EXCLUDED.transcript_source,
              transcript_confidence=EXCLUDED.transcript_confidence,
              ingest_status=EXCLUDED.ingest_status,
              meta_json=EXCLUDED.meta_json,
              updated_at=EXCLUDED.updated_at
            """,
            (
                tk,
                str(event_datetime or "")[:40],
                str(title or "")[:320],
                str(audio_uri or "")[:2000],
                str(audio_source_url or "")[:2000],
                str(audio_checksum or "")[:160],
                str(transcript_text or "")[:220000],
                str(transcript_source or "whisper_local")[:60],
                float(transcript_confidence or 0.0),
                str(ingest_status or "pending")[:32],
                key,
                json.dumps(dict(meta or {}), ensure_ascii=True),
                now,
                now,
            ),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def insert_earnings_call_ingest_run_pg(
    *,
    ticker: str,
    event_datetime: str,
    idempotency_key: str,
    stage: str,
    status: str,
    attempt: int = 0,
    error_text: str = "",
    detail: dict[str, Any] | None = None,
    next_retry_at: str = "",
) -> bool:
    _ = ensure_earnings_call_artifacts_schema_pg()
    tk = str(ticker or "").strip().upper()[:16]
    if not tk:
        return False
    con = pg_connect()
    if con is None:
        return False
    now = dt.datetime.now().isoformat()
    try:
        cur = con.cursor()
        stage_norm = str(stage or "discovery")[:40]
        key_norm = str(idempotency_key or "")[:200]
        # Debounce active duplicate runs for the same ticker/stage in a short time window.
        cutoff = (dt.datetime.now() - dt.timedelta(minutes=5)).isoformat()
        cur.execute(
            """
            SELECT id
            FROM earnings_call_ingest_runs_core
            WHERE ticker=%s
              AND stage=%s
              AND status IN ('queued','retry','processing')
              AND created_at >= %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (tk, stage_norm, cutoff),
        )
        if cur.fetchone():
            con.commit()
            return True
        if key_norm:
            cur.execute(
                """
                SELECT id
                FROM earnings_call_ingest_runs_core
                WHERE idempotency_key=%s
                ORDER BY id DESC
                LIMIT 1
                """,
                (key_norm,),
            )
            if cur.fetchone():
                con.commit()
                return True
        cur.execute(
            """
            INSERT INTO earnings_call_ingest_runs_core
            (ticker, event_datetime, idempotency_key, stage, status, attempt, error_text, detail_json, next_retry_at, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
            """,
            (
                tk,
                str(event_datetime or "")[:40],
                key_norm,
                stage_norm,
                str(status or "queued")[:24],
                max(0, int(attempt or 0)),
                str(error_text or "")[:4000],
                json.dumps(dict(detail or {}), ensure_ascii=True),
                str(next_retry_at or "")[:40],
                now,
                now,
            ),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def claim_next_earnings_call_ingest_run_pg(*, worker_id: str) -> dict[str, Any] | None:
    _ = ensure_earnings_call_artifacts_schema_pg()
    wid = str(worker_id or "").strip()[:80]
    if not wid:
        return None
    con = pg_connect()
    if con is None:
        return None
    now = dt.datetime.now().isoformat()
    try:
        cur = con.cursor()
        cur.execute(
            """
            WITH next_run AS (
                SELECT id
                FROM earnings_call_ingest_runs_core
                WHERE status IN ('queued', 'retry')
                  AND (COALESCE(next_retry_at,'') = '' OR next_retry_at <= %s)
                ORDER BY created_at ASC, id ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE earnings_call_ingest_runs_core r
            SET
              status='processing',
              attempt=attempt+1,
              updated_at=%s,
              detail_json=COALESCE(r.detail_json, '{}'::jsonb) || %s::jsonb
            FROM next_run
            WHERE r.id = next_run.id
            RETURNING r.id, r.ticker, r.event_datetime, r.idempotency_key, r.stage, r.status, r.attempt, r.error_text, r.detail_json, r.next_retry_at, r.created_at, r.updated_at
            """,
            (now, now, json.dumps({"worker_id": wid, "claimed_at": now}, ensure_ascii=True)),
        )
        row = cur.fetchone()
        if not row:
            con.commit()
            return None
        con.commit()
        return {
            "id": int(row[0] or 0),
            "ticker": str(row[1] or ""),
            "event_datetime": str(row[2] or ""),
            "idempotency_key": str(row[3] or ""),
            "stage": str(row[4] or ""),
            "status": str(row[5] or ""),
            "attempt": int(row[6] or 0),
            "error_text": str(row[7] or ""),
            "detail": dict(row[8] or {}) if isinstance(row[8], dict) else {},
            "next_retry_at": str(row[9] or ""),
            "created_at": str(row[10] or ""),
            "updated_at": str(row[11] or ""),
        }
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return None
    finally:
        con.close()


def update_earnings_call_ingest_run_status_pg(
    *,
    run_id: int,
    status: str,
    error_text: str = "",
    detail: dict[str, Any] | None = None,
    next_retry_at: str = "",
) -> bool:
    _ = ensure_earnings_call_artifacts_schema_pg()
    rid = int(run_id or 0)
    if rid <= 0:
        return False
    con = pg_connect()
    if con is None:
        return False
    now = dt.datetime.now().isoformat()
    try:
        cur = con.cursor()
        cur.execute(
            """
            UPDATE earnings_call_ingest_runs_core
            SET
              status=%s,
              error_text=%s,
              detail_json=CASE
                WHEN %s::jsonb = '{}'::jsonb THEN detail_json
                ELSE COALESCE(detail_json, '{}'::jsonb) || %s::jsonb
              END,
              next_retry_at=%s,
              updated_at=%s
            WHERE id=%s
            """,
            (
                str(status or "done")[:24],
                str(error_text or "")[:4000],
                json.dumps(dict(detail or {}), ensure_ascii=True),
                json.dumps(dict(detail or {}), ensure_ascii=True),
                str(next_retry_at or "")[:40],
                now,
                rid,
            ),
        )
        ok = int(getattr(cur, "rowcount", 0) or 0) > 0
        con.commit()
        return ok
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def get_mini_statements_pg(ticker: str) -> dict[str, Any]:
    _ = ensure_mini_statements_schema_pg()
    tk = str(ticker or "").strip().upper()[:16]
    if not tk:
        return {}
    con = pg_connect()
    if con is None:
        return {}
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT ticker, asof, source, currency, years_json, revenue_json, gross_profit_json,
                   operating_cash_flow_json, capex_json, free_cash_flow_json,
                   total_cash_json, total_debt_json, total_equity_json, updated_at
            FROM mini_statements_core
            WHERE ticker=%s
            LIMIT 1
            """,
            (tk,),
        )
        r = cur.fetchone()
        if not r:
            return {}
        def _loads(v: Any, fallback: str = "[]") -> Any:
            try:
                return json.loads(str(v or fallback))
            except Exception:
                return json.loads(fallback)
        return {
            "ticker": str(r[0] or ""),
            "asof": str(r[1] or ""),
            "source": str(r[2] or ""),
            "currency": str(r[3] or "USD"),
            "years": _loads(r[4], "[]"),
            "revenue": _loads(r[5], "[]"),
            "gross_profit": _loads(r[6], "[]"),
            "operating_cash_flow": _loads(r[7], "[]"),
            "capex": _loads(r[8], "[]"),
            "free_cash_flow": _loads(r[9], "[]"),
            "total_cash": _loads(r[10], "[]"),
            "total_debt": _loads(r[11], "[]"),
            "total_equity": _loads(r[12], "[]"),
            "updated_at": str(r[13] or ""),
        }
    except Exception:
        return {}
    finally:
        con.close()


def upsert_mini_statements_pg(
    *,
    ticker: str,
    asof: str,
    source: str,
    currency: str,
    years: list[int],
    revenue: list[float | None],
    gross_profit: list[float | None],
    operating_cash_flow: list[float | None],
    capex: list[float | None],
    free_cash_flow: list[float | None],
    total_cash: list[float | None],
    total_debt: list[float | None],
    total_equity: list[float | None],
) -> bool:
    _ = ensure_mini_statements_schema_pg()
    tk = str(ticker or "").strip().upper()[:16]
    if not tk:
        return False
    con = pg_connect()
    if con is None:
        return False
    now = dt.datetime.now().isoformat()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO mini_statements_core
            (ticker, asof, source, currency, years_json, revenue_json, gross_profit_json,
             operating_cash_flow_json, capex_json, free_cash_flow_json,
             total_cash_json, total_debt_json, total_equity_json, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (ticker) DO UPDATE SET
              asof=EXCLUDED.asof,
              source=EXCLUDED.source,
              currency=EXCLUDED.currency,
              years_json=EXCLUDED.years_json,
              revenue_json=EXCLUDED.revenue_json,
              gross_profit_json=EXCLUDED.gross_profit_json,
              operating_cash_flow_json=EXCLUDED.operating_cash_flow_json,
              capex_json=EXCLUDED.capex_json,
              free_cash_flow_json=EXCLUDED.free_cash_flow_json,
              total_cash_json=EXCLUDED.total_cash_json,
              total_debt_json=EXCLUDED.total_debt_json,
              total_equity_json=EXCLUDED.total_equity_json,
              updated_at=EXCLUDED.updated_at
            """,
            (
                tk,
                str(asof or "")[:40],
                str(source or "")[:40],
                (str(currency or "USD").strip().upper() or "USD")[:8],
                json.dumps(list(years or [])[:8]),
                json.dumps(list(revenue or [])[:8]),
                json.dumps(list(gross_profit or [])[:8]),
                json.dumps(list(operating_cash_flow or [])[:8]),
                json.dumps(list(capex or [])[:8]),
                json.dumps(list(free_cash_flow or [])[:8]),
                json.dumps(list(total_cash or [])[:8]),
                json.dumps(list(total_debt or [])[:8]),
                json.dumps(list(total_equity or [])[:8]),
                now,
            ),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def insert_portfolio_transaction_pg(
    *,
    created_at: str,
    ticker: str,
    action: str,
    shares: float,
    price: float,
    note: str = "",
    source: str = "app",
    meta_json: dict[str, Any] | None = None,
) -> bool:
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO portfolio_transactions_core
            (id, created_at, ticker, action, shares, price, note, source, meta_json)
            VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM portfolio_transactions_core), %s,%s,%s,%s,%s,%s,%s,%s::jsonb)
            """,
            (
                str(created_at or "").strip(),
                str(ticker or "").strip().upper()[:16],
                str(action or "").strip().lower()[:32],
                float(shares or 0.0),
                float(price or 0.0),
                str(note or "")[:2000],
                str(source or "app")[:64],
                json.dumps(meta_json or {}, ensure_ascii=True),
            ),
        )
        con.commit()
        return True
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        print(f"[add_todo_pg] {type(exc).__name__}: {exc}")
        return False
    finally:
        con.close()


def summarize_investor_style_memory_pg(limit: int = 24) -> str:
    con = pg_connect()
    if con is None:
        return ""
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT key, answer FROM investor_style_memory_core WHERE COALESCE(answer,'') <> '' ORDER BY updated_at DESC LIMIT %s",
            (max(1, min(200, int(limit or 24))),),
        )
        out: list[str] = []
        for r in _iter_cursor_rows(cur):
            k = str(r[0] or "").strip()
            a = str(r[1] or "").strip()
            if k and a:
                out.append(f"- {k}: {a[:180]}")
        return "\n".join(out)
    except Exception:
        return ""
    finally:
        con.close()


def list_investor_style_memory_pg(limit: int = 300) -> list[dict[str, str]]:
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT key, answer, updated_at FROM investor_style_memory_core ORDER BY updated_at DESC LIMIT %s",
            (max(1, min(1000, int(limit or 300))),),
        )
        out: list[dict[str, str]] = []
        for r in _iter_cursor_rows(cur):
            out.append(
                {
                    "key": str(r[0] or ""),
                    "answer": str(r[1] or ""),
                    "updated_at": str(r[2] or ""),
                }
            )
        return out
    except Exception:
        return []
    finally:
        con.close()


# ── Idea List CRUD ──────────────────────────────────────────────────────────

def list_ideas_pg(status: str = "open", limit: int = 200) -> list[dict]:
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        if status == "all":
            cur.execute(
                "SELECT id, title, ticker, notes, source, status, created_at FROM idea_list_core ORDER BY created_at DESC LIMIT %s",
                (max(1, min(500, int(limit))),),
            )
        else:
            cur.execute(
                "SELECT id, title, ticker, notes, source, status, created_at FROM idea_list_core WHERE status=%s ORDER BY created_at DESC LIMIT %s",
                (str(status), max(1, min(500, int(limit)))),
            )
        return [
            {
                "id": int(r[0]),
                "title": str(r[1] or ""),
                "ticker": str(r[2] or "") if r[2] else None,
                "notes": str(r[3] or ""),
                "source": str(r[4] or "manual"),
                "status": str(r[5] or "open"),
                "created_at": str(r[6] or ""),
            }
            for r in _iter_cursor_rows(cur)
        ]
    except Exception:
        return []
    finally:
        con.close()


def add_idea_pg(title: str, ticker: str | None = None, notes: str = "", source: str = "manual") -> int | None:
    con = pg_connect()
    if con is None:
        return None
    try:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO idea_list_core (title, ticker, notes, source) VALUES (%s, %s, %s, %s) RETURNING id",
            (str(title or "").strip(), (str(ticker).strip().upper() if ticker else None), str(notes or "").strip(), str(source or "manual").strip()),
        )
        row = cur.fetchone()
        con.commit()
        return int(row[0]) if row else None
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return None
    finally:
        con.close()


def update_idea_status_pg(idea_id: int, status: str) -> bool:
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("UPDATE idea_list_core SET status=%s WHERE id=%s", (str(status), int(idea_id)))
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def delete_idea_pg(idea_id: int) -> bool:
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM idea_list_core WHERE id=%s", (int(idea_id),))
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def list_watchlist_thesis_pg(limit: int = 300) -> list[dict[str, str]]:
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            """SELECT ticker, thesis_summary, time_horizon, invalidation_criteria, strategy_tag, created_at, updated_at,
                      target_price, key_questions, phase
               FROM watchlist_thesis_core
               ORDER BY updated_at DESC
               LIMIT %s""",
            (max(1, min(1000, int(limit or 300))),),
        )
        out: list[dict[str, str]] = []
        for r in _iter_cursor_rows(cur):
            out.append(
                {
                    "ticker": str(r[0] or ""),
                    "thesis_summary": str(r[1] or ""),
                    "time_horizon": str(r[2] or ""),
                    "invalidation_criteria": str(r[3] or ""),
                    "strategy_tag": str(r[4] or ""),
                    "created_at": str(r[5] or ""),
                    "updated_at": str(r[6] or ""),
                    "target_price": float(r[7]) if r[7] is not None else None,
                    "key_questions": r[8] if isinstance(r[8], list) else [],
                    "phase": str(r[9] or "watching"),
                }
            )
        return out
    except Exception:
        return []
    finally:
        con.close()


def upsert_watchlist_thesis_pg(
    *,
    ticker: str,
    thesis: str = "",
    thesis_summary: str = "",
    pick_method: str = "",
    triggers: str = "",
    invalidation: str = "",
    conviction_rating: int = 0,
    time_horizon: str = "",
    invalidation_criteria: str = "",
    strategy_tag: str = "CORE",
    pattern_learnable: int = 1,
    status: str = "active",
    target_price: float | None = None,
    key_questions: str = "",
    phase: str = "",
) -> bool:
    con = pg_connect()
    if con is None:
        return False
    now = dt.datetime.now().isoformat()
    t = str(ticker or "").strip().upper()
    if not t:
        con.close()
        return False
    kq_json = "[]"
    if key_questions:
        try:
            import json as _json
            _json.loads(key_questions)
            kq_json = key_questions
        except Exception:
            kq_json = "[]"
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO watchlist_thesis_core
            (ticker, thesis, thesis_summary, pick_method, triggers, invalidation, conviction_rating, time_horizon, invalidation_criteria, strategy_tag, pattern_learnable, status, target_price, key_questions, phase, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
            ON CONFLICT(ticker) DO UPDATE SET
              thesis=COALESCE(NULLIF(EXCLUDED.thesis,''),watchlist_thesis_core.thesis),
              thesis_summary=COALESCE(NULLIF(EXCLUDED.thesis_summary,''),watchlist_thesis_core.thesis_summary),
              pick_method=COALESCE(NULLIF(EXCLUDED.pick_method,''),watchlist_thesis_core.pick_method),
              triggers=COALESCE(NULLIF(EXCLUDED.triggers,''),watchlist_thesis_core.triggers),
              invalidation=COALESCE(NULLIF(EXCLUDED.invalidation,''),watchlist_thesis_core.invalidation),
              conviction_rating=CASE WHEN EXCLUDED.conviction_rating > 0 THEN EXCLUDED.conviction_rating ELSE watchlist_thesis_core.conviction_rating END,
              time_horizon=COALESCE(NULLIF(EXCLUDED.time_horizon,''),watchlist_thesis_core.time_horizon),
              invalidation_criteria=COALESCE(NULLIF(EXCLUDED.invalidation_criteria,''),watchlist_thesis_core.invalidation_criteria),
              strategy_tag=COALESCE(NULLIF(EXCLUDED.strategy_tag,''),watchlist_thesis_core.strategy_tag),
              pattern_learnable=CASE WHEN EXCLUDED.pattern_learnable IN (0,1) THEN EXCLUDED.pattern_learnable ELSE watchlist_thesis_core.pattern_learnable END,
              status=COALESCE(NULLIF(EXCLUDED.status,''),watchlist_thesis_core.status),
              target_price=COALESCE(EXCLUDED.target_price, watchlist_thesis_core.target_price),
              key_questions=CASE WHEN EXCLUDED.key_questions != '[]'::jsonb THEN EXCLUDED.key_questions ELSE watchlist_thesis_core.key_questions END,
              phase=COALESCE(NULLIF(EXCLUDED.phase,''),watchlist_thesis_core.phase),
              updated_at=EXCLUDED.updated_at
            """,
            (
                t,
                str(thesis or "")[:3000],
                str((thesis_summary or thesis) or "")[:3000],
                str(pick_method or "")[:500],
                str(triggers or "")[:1000],
                str(invalidation or "")[:1000],
                int(conviction_rating or 0),
                str(time_horizon or "")[:120],
                str((invalidation_criteria or invalidation) or "")[:1000],
                str(strategy_tag or "CORE")[:16].upper(),
                1 if int(pattern_learnable) != 0 else 0,
                str(status or "active")[:32],
                target_price,
                kq_json,
                str(phase or "")[:32],
                now,
                now,
            ),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


## ── ai_response_feedback PG helpers ─────────────────────────────────────────


def store_ai_response_feedback(
    session_id: str = "",
    message_id: str = "",
    query_text: str = "",
    response_text: str = "",
    intent: str = "",
    rating: str = "",
    comment: str = "",
) -> bool:
    """Store user thumbs-up/down feedback for an AI response."""
    con = pg_connect()
    if con is None:
        return False
    now = dt.datetime.now().isoformat(timespec="seconds")
    try:
        cur = con.cursor()
        cur.execute(
            """INSERT INTO ai_response_feedback
               (created_at, session_id, message_id, query_text, response_text, intent, rating, comment)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                now,
                str(session_id or "")[:120],
                str(message_id or "")[:64],
                str(query_text or "")[:2000],
                str(response_text or "")[:4000],
                str(intent or "")[:120],
                str(rating or "")[:20],
                str(comment or "")[:500],
            ),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


## ── memory_compact_core PG helpers ──────────────────────────────────────────


def list_compact_memories_pg(
    query: str = "",
    bucket: str = "",
    limit: int = 30,
    include_archived: bool = False,
) -> list[dict]:
    """Read compact memories from Postgres memory_compact_core table."""
    con = pg_connect()
    if con is None:
        return []
    q = str(query or "").strip().lower()
    b = str(bucket or "").strip().lower()
    lim = max(1, min(300, int(limit or 30)))
    try:
        cur = con.cursor()
        clauses = ["1=1"]
        vals: list = []
        if not include_archived:
            clauses.append("status='active'")
        if b:
            clauses.append("bucket=%s")
            vals.append(b[:32])
        if q:
            like = "%" + q[:120] + "%"
            clauses.append("(LOWER(value) LIKE %s OR LOWER(memory_key) LIKE %s)")
            vals.extend([like, like])
        sql = (
            "SELECT id, memory_key, bucket, value, source, reliability, reuse_count, "
            "status, conflict_of, created_at, updated_at, last_used_at "
            "FROM memory_compact_core WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC LIMIT %s"
        )
        vals.append(lim)
        cur.execute(sql, tuple(vals))
        out: list[dict] = []
        for r in _iter_cursor_rows(cur):
            rel = float(r[5] or 0.0)
            reuse = int(r[6] or 0)
            # Freshness scoring (mirrors SQLite path)
            fw = 0.5
            try:
                ua = str(r[10] or "")
                if ua:
                    ts = dt.datetime.fromisoformat(ua.replace("Z", "+00:00").split("+")[0])
                    age_days = max(0.0, (dt.datetime.now() - ts).total_seconds() / 86400.0)
                    if age_days <= 7:
                        fw = 1.0
                    elif age_days <= 30:
                        fw = 0.85
                    elif age_days <= 90:
                        fw = 0.65
                    else:
                        fw = 0.45
            except Exception:
                pass
            score = rel * fw * (1.0 + min(2.0, reuse / 4.0))
            out.append(
                {
                    "id": int(r[0] or 0),
                    "memory_key": str(r[1] or ""),
                    "bucket": str(r[2] or ""),
                    "value": str(r[3] or ""),
                    "source": str(r[4] or ""),
                    "reliability": rel,
                    "reuse_count": reuse,
                    "status": str(r[7] or ""),
                    "conflict_of": str(r[8] or ""),
                    "created_at": str(r[9] or ""),
                    "updated_at": str(r[10] or ""),
                    "last_used_at": str(r[11] or ""),
                    "score": float(score),
                }
            )
        out.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
        return out
    except Exception:
        return []
    finally:
        con.close()


def upsert_compact_memory_pg(
    bucket: str,
    value: str,
    *,
    source: str = "chat_turn",
    reliability: float = 0.7,
    memory_key: str = "",
    status: str = "active",
    conflict_of: str = "",
) -> bool:
    """Write a compact memory to Postgres memory_compact_core table."""
    import hashlib

    b = str(bucket or "preference").strip().lower()[:32] or "preference"
    v = str(value or "").strip()[:2000]
    if not v:
        return False
    k = str(memory_key or "").strip().lower()[:64]
    if not k:
        base = f"{b}::{v.lower()}"
        k = hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()[:40]
    now = dt.datetime.now().isoformat(timespec="seconds")
    rel = max(0.0, min(1.0, float(reliability or 0.0)))
    st = str(status or "active").strip().lower()
    if st not in {"active", "archived", "pending_confirmation"}:
        st = "active"
    cf_of = str(conflict_of or "").strip()[:64]
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO memory_compact_core
                (id, memory_key, bucket, value, source, reliability, reuse_count, status, conflict_of, created_at, updated_at, last_used_at)
            VALUES (
                COALESCE((SELECT id FROM memory_compact_core WHERE memory_key=%s), (SELECT COALESCE(MAX(id),0)+1 FROM memory_compact_core)),
                %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s
            )
            ON CONFLICT(memory_key) DO UPDATE SET
                value=EXCLUDED.value, bucket=EXCLUDED.bucket, source=EXCLUDED.source,
                reliability=EXCLUDED.reliability, status=EXCLUDED.status, conflict_of=EXCLUDED.conflict_of,
                reuse_count=memory_compact_core.reuse_count+1, updated_at=EXCLUDED.updated_at
            """,
            (k, k, b, v, str(source or "chat_turn")[:64], rel, st, cf_of, now, now, now),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def upsert_investor_style_memory_pg(key: str, answer: str) -> bool:
    con = pg_connect()
    if con is None:
        return False
    k = str(key or "").strip()
    if not k:
        con.close()
        return False
    now = dt.datetime.now().isoformat()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO investor_style_memory_core(key, answer, created_at, updated_at)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT(key) DO UPDATE SET answer=EXCLUDED.answer, updated_at=EXCLUDED.updated_at
            """,
            (k[:300], str(answer or "")[:2000], now, now),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def filing_stats_map_pg(tickers: list[str]) -> dict[str, dict[str, str | int]]:
    tks = [str(t or "").strip().upper() for t in (tickers or []) if str(t or "").strip()]
    if not tks:
        return {}
    con = pg_connect()
    if con is None:
        return {}
    out: dict[str, dict[str, str | int]] = {t: {"filings": 0, "last_filing_date": ""} for t in tks}
    try:
        marks = ",".join("%s" for _ in tks)
        cur = con.cursor()
        cur.execute(
            f"""
            SELECT ticker, COUNT(*) AS c, MAX(date) AS last_date
            FROM filings_core
            WHERE ticker IN ({marks})
            GROUP BY ticker
            """,
            tuple(tks),
        )
        for r in _iter_cursor_rows(cur):
            t = str(r[0] or "").strip().upper()
            if not t:
                continue
            out[t] = {"filings": int(r[1] or 0), "last_filing_date": str(r[2] or "")}
        return out
    except Exception:
        return out
    finally:
        con.close()


def company_news_from_report_facts_pg(tickers: list[str], limit: int = 8) -> list[dict[str, str]]:
    tks = [str(x or "").strip().upper() for x in (tickers or []) if str(x or "").strip()]
    if not tks:
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        marks = ",".join("%s" for _ in tks)
        cur = con.cursor()
        cur.execute(
            f"""SELECT ticker, fact_text, fact_date
                FROM report_facts_core
                WHERE ticker IN ({marks})
                ORDER BY importance DESC, fact_date DESC, id DESC
                LIMIT %s""",
            tuple(tks + [max(1, min(40, int(limit) * 3))]),
        )
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for r in _iter_cursor_rows(cur):
            tk = str(r[0] or "").strip().upper()
            txt = str(r[1] or "").strip()
            if not tk or not txt:
                continue
            title = f"{tk}: {txt[:160]}"
            key = " ".join(title.lower().split())
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "title": title,
                    "ticker": tk,
                    "link": f"/company_file/sec?t={tk}",
                    "source": "Official Reports",
                    "published_at": str(r[2] or ""),
                }
            )
            if len(out) >= max(1, min(30, int(limit))):
                break
        return out
    except Exception:
        return []
    finally:
        con.close()


def query_report_facts_pg(
    query: str = "",
    tickers: list[str] | None = None,
    limit: int = 12,
    official_only: bool = True,
) -> list[dict[str, Any]]:
    con = pg_connect()
    if con is None:
        return []
    lim = max(1, min(100, int(limit or 12)))
    tks = [str(t or "").strip().upper() for t in (tickers or []) if str(t or "").strip()]
    try:
        clauses = ["1=1"]
        vals: list[Any] = []
        if tks:
            clauses.append("ticker = ANY(%s)")
            vals.append(tks)
        q = str(query or "").strip()
        if q:
            like = "%" + q[:120] + "%"
            clauses.append("(fact_text ILIKE %s OR report_name ILIKE %s OR report_kind ILIKE %s)")
            vals.extend([like, like, like])
        if bool(official_only):
            allowed_kinds = sorted(
                {
                    "Daily Brief",
                    "Appendix",
                    "Morning Intelligence",
                    "Signal Tracker",
                    "Earnings Radar",
                    "Market Scanner",
                    "L2 Digest",
                    "Weekly",
                    "Monthly",
                    "Quarterly",
                    "Deep Dive",
                    "Red Flag Alert",
                }
            )
            clauses.append(
                "(report_kind = ANY(%s) OR report_name ILIKE %s OR report_name ILIKE %s OR report_name ILIKE %s OR report_name ILIKE %s OR report_name ILIKE %s OR report_name ILIKE %s)"
            )
            vals.append(allowed_kinds)
            vals.extend(["%10-k%", "%10-q%", "%8-k%", "%20-f%", "%40-f%", "%sec%"])
        sql = (
            "SELECT report_name, report_kind, fact_date, ticker, fact_text, importance, created_at "
            "FROM report_facts_core WHERE "
            + " AND ".join(clauses)
            + " ORDER BY importance DESC, fact_date DESC, id DESC LIMIT %s"
        )
        vals.append(lim)
        cur = con.cursor()
        cur.execute(sql, tuple(vals))
        out: list[dict[str, Any]] = []
        for r in _iter_cursor_rows(cur):
            out.append(
                {
                    "report_name": str(r[0] or ""),
                    "report_kind": str(r[1] or ""),
                    "fact_date": str(r[2] or ""),
                    "ticker": str(r[3] or "").strip().upper(),
                    "fact_text": str(r[4] or ""),
                    "importance": int(r[5] or 0),
                    "created_at": str(r[6] or ""),
                }
            )
        return out
    except Exception:
        return []
    finally:
        con.close()


def list_todos_pg(open_only: bool = True, limit: int = 400, ticker: str = "") -> list[dict[str, Any]]:
    con = pg_connect()
    if con is None:
        return []
    lim = max(1, min(2000, int(limit or 400)))
    tk = str(ticker or "").strip().upper()
    try:
        # Compatibility: investor_annotations_core exists in two schema variants.
        # New polymorphic schema uses `content_text`; older one used `content`.
        cur = con.cursor()
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='investor_annotations_core'
            """
        )
        cols = {str(r[0] or "").strip().lower() for r in _iter_cursor_rows(cur)}
        text_col = "content" if "content" in cols else ("content_text" if "content_text" in cols else "")
        has_priority = "priority" in cols
        has_due_date = "due_date" in cols
        has_snooze_until = "snooze_until" in cols
        has_category = "category" in cols

        clauses: list[str] = []
        vals: list[Any] = []
        clauses.append("annotation_type = 'task'")
        if tk:
            clauses.append("entity_type='company' AND entity_id = %s")
            vals.append(tk)
        if open_only:
            # "snoozed" tasks stay hidden until snooze date is reached.
            if has_snooze_until:
                clauses.append("(status = 'open' OR (status = 'snoozed' AND COALESCE(snooze_until,'') <> '' AND snooze_until <= %s))")
                vals.append(dt.date.today().isoformat())
            else:
                clauses.append("status = 'open'")
        else:
            if has_snooze_until:
                clauses.append("(status IN ('done','archived') OR (status='snoozed' AND COALESCE(snooze_until,'') <> '' AND snooze_until > %s))")
                vals.append(dt.date.today().isoformat())
            else:
                clauses.append("status IN ('done','archived')")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        order = (
            (
                "ORDER BY "
                + ("CASE priority WHEN 'P1' THEN 0 WHEN 'P2' THEN 1 ELSE 2 END, " if has_priority else "")
                + ("CASE WHEN due_date <> '' THEN due_date ELSE '9999-12-31' END, " if has_due_date else "")
                + "created_at DESC, id DESC"
            )
            if open_only
            else "ORDER BY created_at DESC, id DESC"
        )
        task_sel = text_col if text_col else "''"
        pri_sel = "priority" if has_priority else "'P2'"
        due_sel = "due_date" if has_due_date else "''"
        snooze_sel = "snooze_until" if has_snooze_until else "''"
        cat_sel = "category" if has_category else "'general'"
        cur.execute(
            f"""SELECT id, {task_sel}, status, {pri_sel}, {due_sel}, {snooze_sel}, entity_id, {cat_sel}, created_at
                FROM investor_annotations_core {where} {order} LIMIT %s""",
            tuple(vals + [lim]),
        )
        out: list[dict[str, Any]] = []
        for r in _iter_cursor_rows(cur):
            out.append(
                {
                    "id": int(r[0] or 0),
                    "task": str(r[1] or ""),
                    "status": str(r[2] or "open"),
                    "priority": str(r[3] or "P2"),
                    "due_date": str(r[4] or ""),
                    "snooze_until": str(r[5] or ""),
                    "ticker": str(r[6] or "").upper(),
                    "category": str(r[7] or "general"),
                    "created_at": str(r[8] or ""),
                }
            )
        return out
    except Exception:
        return []
    finally:
        con.close()


def add_todo_pg(task: str, *, ticker: str = "", category: str = "general", priority: str = "P2", due_date: str = "") -> bool:
    con = pg_connect()
    if con is None:
        return False
    txt = str(task or "").strip()
    if not txt:
        con.close()
        return False
    tk = str(ticker or "").strip().upper()[:16]
    if not tk:
        tk = _extract_ticker_from_text(txt)
    cat = str(category or "general").strip().lower()
    pr = str(priority or "P2").strip().upper()
    dd = str(due_date or "").strip()
    if cat not in {"company", "quick", "general"}:
        cat = "general"
    if pr not in {"P1", "P2", "P3"}:
        pr = "P2"
    now = dt.datetime.now().isoformat()
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT column_name, COALESCE(column_default, '')
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='investor_annotations_core'
            """
        )
        col_rows = list(_iter_cursor_rows(cur))
        cols = {str(r[0] or "").strip().lower() for r in col_rows}
        id_has_default = False
        for r in col_rows:
            if str(r[0] or "").strip().lower() == "id":
                id_has_default = bool(str(r[1] or "").strip())
                break
        base_cols = ["entity_type", "entity_id", "annotation_type", "status", "created_at", "updated_at"]
        base_vals: list[Any] = ["company" if tk else "global", tk, "task", "open", now, now]
        row_id: int | None = None
        if "id" in cols and not id_has_default:
            cur.execute("SELECT COALESCE(MAX(id),0)+1 FROM investor_annotations_core")
            row_id = int((cur.fetchone() or [1])[0] or 1)
            base_cols.insert(0, "id")
            base_vals.insert(0, row_id)
        if "content" in cols:
            base_cols.append("content")
            base_vals.append(txt[:4000])
        elif "content_text" in cols:
            base_cols.append("content_text")
            base_vals.append(txt[:4000])
        if "priority" in cols:
            base_cols.append("priority")
            base_vals.append(pr)
        if "due_date" in cols:
            base_cols.append("due_date")
            base_vals.append(dd)
        if "category" in cols:
            base_cols.append("category")
            base_vals.append(cat)
        if "snooze_until" in cols:
            base_cols.append("snooze_until")
            base_vals.append("")
        if "source_ref" in cols:
            base_cols.append("source_ref")
            base_vals.append("")
        if "created_by" in cols:
            base_cols.append("created_by")
            base_vals.append("human")
        if "tenant_id" in cols:
            base_cols.append("tenant_id")
            base_vals.append("default")
        if "user_id" in cols:
            base_cols.append("user_id")
            base_vals.append("default")
        if "source_table" in cols:
            base_cols.append("source_table")
            base_vals.append("todos_core")
        if "source_id" in cols:
            sid = f"todo:{row_id}" if row_id is not None else f"todo:{int(dt.datetime.now().timestamp() * 1_000_000)}:{uuid.uuid4().hex[:8]}"
            base_cols.append("source_id")
            base_vals.append(sid)
        if "content_json" in cols:
            base_cols.append("content_json")
            base_vals.append(json.dumps({}, ensure_ascii=True))
        marks = ", ".join(["%s"] * len(base_cols))
        cur.execute(
            f"INSERT INTO investor_annotations_core ({', '.join(base_cols)}) VALUES ({marks})",
            tuple(base_vals),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def update_todo_status_pg(todo_id: int, new_status: str) -> bool:
    rid = int(todo_id or 0)
    if rid <= 0:
        return False
    st = str(new_status or "").strip().lower()
    if st not in {"open", "done", "archived", "snoozed"}:
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        if st == "open":
            cur.execute(
                "UPDATE investor_annotations_core SET status=%s, snooze_until='', updated_at=%s WHERE id=%s AND annotation_type='task'",
                (st, dt.datetime.now().isoformat(), rid),
            )
        else:
            cur.execute(
                "UPDATE investor_annotations_core SET status=%s, updated_at=%s WHERE id=%s AND annotation_type='task'",
                (st, dt.datetime.now().isoformat(), rid),
            )
        con.commit()
        return int(cur.rowcount or 0) > 0
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def toggle_todo_pg(todo_id: int) -> bool:
    rid = int(todo_id or 0)
    if rid <= 0:
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("SELECT status, category FROM investor_annotations_core WHERE id=%s AND annotation_type='task'", (rid,))
        row = cur.fetchone()
        if not row:
            return False
        cur_status = str(row[0] or "open").strip().lower()
        cat = str(row[1] or "general").strip().lower()
        nxt = ("archived" if cat == "quick" else "done") if cur_status == "open" else "open"
        cur.execute(
            "UPDATE investor_annotations_core SET status=%s, updated_at=%s WHERE id=%s AND annotation_type='task'",
            (nxt, dt.datetime.now().isoformat(), rid),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def delete_todo_pg(todo_id: int) -> bool:
    rid = int(todo_id or 0)
    if rid <= 0:
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM investor_annotations_core WHERE id=%s AND annotation_type='task'", (rid,))
        con.commit()
        return int(cur.rowcount or 0) > 0
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def update_todo_pg(todo_id: int, *, task: str, due_date: str = "", priority: str = "P2") -> bool:
    rid = int(todo_id or 0)
    txt = str(task or "").strip()
    if rid <= 0 or not txt:
        return False
    pr = str(priority or "").strip().upper()
    if pr not in {"", "P1", "P2", "P3"}:
        pr = ""
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='investor_annotations_core'
            """
        )
        cols = {str(r[0] or "").strip().lower() for r in _iter_cursor_rows(cur)}
        text_col = "content" if "content" in cols else ("content_text" if "content_text" in cols else "")
        if not text_col:
            return False
        tk = _extract_ticker_from_text(txt)
        set_parts = [f"{text_col}=%s", "updated_at=%s", "entity_type=CASE WHEN %s <> '' THEN 'company' ELSE entity_type END", "entity_id=CASE WHEN %s <> '' THEN %s ELSE entity_id END"]
        vals: list[Any] = [txt[:4000], dt.datetime.now().isoformat(), tk, tk, tk]
        if "due_date" in cols:
            set_parts.append("due_date=CASE WHEN %s <> '' THEN %s ELSE due_date END")
            vals.extend([str(due_date or "").strip(), str(due_date or "").strip()])
        if "priority" in cols:
            set_parts.append("priority=CASE WHEN %s <> '' THEN %s ELSE priority END")
            vals.extend([pr, pr])
        vals.append(rid)
        cur.execute(
            f"UPDATE investor_annotations_core SET {', '.join(set_parts)} WHERE id=%s AND annotation_type='task'",
            tuple(vals),
        )
        con.commit()
        return int(cur.rowcount or 0) > 0
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def snooze_todo_pg(todo_id: int, snooze_until: str) -> bool:
    rid = int(todo_id or 0)
    if rid <= 0:
        return False
    su = str(snooze_until or "").strip()
    if not su:
        return False
    try:
        dt.date.fromisoformat(su)
    except Exception:
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE investor_annotations_core SET status='snoozed', snooze_until=%s, updated_at=%s WHERE id=%s AND annotation_type='task'",
            (su, dt.datetime.now().isoformat(), rid),
        )
        con.commit()
        return int(cur.rowcount or 0) > 0
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def list_recent_notes_pg(limit: int = 200) -> list[dict[str, str]]:
    con = pg_connect()
    if con is None:
        return []
    lim = max(1, min(2000, int(limit or 200)))
    try:
        cur = con.cursor()
        out: list[dict[str, str]] = []
        cur.execute(
            """
            SELECT id, entity_type, entity_id, annotation_type, content, status, created_by, created_at
            FROM investor_annotations_core
            WHERE annotation_type IN ('note','log')
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            (lim,),
        )
        for r in _iter_cursor_rows(cur):
            et = str(r[1] or "global").strip().lower()
            out.append(
                {
                    "id": str(r[0] or ""),
                    "source_table": "workspace_journal" if et == "company" else "investor_notes",
                    "date": str(r[7] or ""),
                    "kind": "Company" if et == "company" else "General",
                    "ticker": str(r[2] or "").strip().upper(),
                    "tag": str(r[3] or "note"),
                    "text": str(r[4] or ""),
                    "status": str(r[5] or "approved"),
                    "created_by": str(r[6] or "human"),
                    "ai_confidence": "0",
                    "ai_reasoning": "",
                    "trace_id": "",
                }
            )
        return out[:lim]
    except Exception:
        return []
    finally:
        con.close()


def add_investor_note_pg(
    *,
    scope: str,
    ticker: str,
    sentiment: str,
    note: str,
    tags: str,
    status: str,
    created_by: str,
    ai_confidence: float = 0.0,
    ai_reasoning: str = "",
    trace_id: str = "",
) -> bool:
    con = pg_connect()
    if con is None:
        return False
    now = dt.datetime.now().isoformat()
    try:
        tk = str(ticker or "").strip().upper()[:16]
        if not tk:
            tk = _extract_ticker_from_text(str(note or ""))
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO investor_annotations_core
            (entity_type, entity_id, annotation_type, content, status, priority, due_date, category, snooze_until, source_ref, created_by, created_at, updated_at)
            VALUES (%s, %s, 'note', %s, %s, 'P2', '', %s, '', '', %s, %s, %s)
            """,
            (
                "company" if tk else "global",
                tk,
                str(note or "")[:4000],
                str(status or "approved")[:20],
                str(scope or "organizer")[:40],
                str(created_by or "human")[:20],
                now,
                now,
            ),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def approve_investor_note_draft_pg(note_id: int, ticker: str = "") -> bool:
    rid = int(note_id or 0)
    if rid <= 0:
        return False
    tk = str(ticker or "").strip().upper()[:16]
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            """
            UPDATE investor_annotations_core
            SET status='approved',
                entity_type=CASE WHEN %s <> '' THEN 'company' ELSE entity_type END,
                entity_id=CASE WHEN %s <> '' THEN %s ELSE entity_id END,
                updated_at=%s
            WHERE id=%s AND annotation_type='note' AND status='pending'
            """,
            (tk, tk, tk, dt.datetime.now().isoformat(), rid),
        )
        con.commit()
        return int(cur.rowcount or 0) > 0
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def discard_investor_note_draft_pg(note_id: int) -> bool:
    rid = int(note_id or 0)
    if rid <= 0:
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM investor_annotations_core WHERE id=%s AND annotation_type='note' AND status='pending'", (rid,))
        con.commit()
        return int(cur.rowcount or 0) > 0
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def delete_notes_pg(
    *,
    ticker: str = "",
    text_contains: str = "",
    created_by: str = "",
    include_company_journal: bool = True,
) -> dict[str, int]:
    tk = str(ticker or "").strip().upper()[:16]
    txt = str(text_contains or "").strip()
    cb = str(created_by or "").strip().lower()
    if cb not in {"", "ai", "human"}:
        cb = ""
    out = {"investor_notes": 0, "workspace_journal": 0, "total": 0}
    con = pg_connect()
    if con is None:
        return out
    try:
        cur = con.cursor()
        where = ["annotation_type IN ('note','log')"]
        vals: list[Any] = []
        if tk:
            where.append("entity_id = %s")
            vals.append(tk)
        if txt:
            where.append("LOWER(content) LIKE %s")
            vals.append("%" + txt.lower() + "%")
        if cb:
            where.append("LOWER(created_by) = %s")
            vals.append(cb)
        cur.execute("DELETE FROM investor_annotations_core WHERE " + " AND ".join(where), tuple(vals))
        out["investor_notes"] = int(cur.rowcount or 0)
        out["workspace_journal"] = 0
        out["total"] = out["investor_notes"]
        con.commit()
        return out
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return out
    finally:
        con.close()


def add_workspace_journal_note_pg(
    ticker: str,
    note: str,
    action: str = "Note",
    emotion: str = "Calm",
    created_by: str = "human",
) -> bool:
    con = pg_connect()
    if con is None:
        return False
    now = dt.datetime.now().isoformat()
    try:
        cur = con.cursor()
        cb = str(created_by or "human").strip().lower()
        if cb not in {"human", "ai"}:
            cb = "human"
        tk = str(ticker or "").upper()[:16]
        cur.execute(
            """
            INSERT INTO investor_annotations_core
            (entity_type, entity_id, annotation_type, content, status, priority, due_date, category, snooze_until, source_ref, created_by, created_at, updated_at)
            VALUES ('company', %s, 'note', %s, 'approved', 'P2', '', %s, '', '', %s, %s, %s)
            """,
            (
                tk,
                str(note or "")[:4000],
                str(action or "note")[:80],
                cb,
                now,
                now,
            ),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def update_workspace_journal_note_pg(note_id: int, note: str) -> bool:
    rid = int(note_id or 0)
    if rid <= 0:
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='investor_annotations_core'
            """
        )
        cols = {str(r[0] or "").strip().lower() for r in _iter_cursor_rows(cur)}
        text_col = "content" if "content" in cols else ("content_text" if "content_text" in cols else "")
        if not text_col:
            return False
        cur.execute(
            f"UPDATE investor_annotations_core SET {text_col}=%s, updated_at=%s WHERE id=%s AND annotation_type='note'",
            (str(note or "")[:4000], dt.datetime.now().isoformat(), rid),
        )
        con.commit()
        return int(cur.rowcount or 0) > 0
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def delete_workspace_journal_note_pg(note_id: int) -> bool:
    rid = int(note_id or 0)
    if rid <= 0:
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM investor_annotations_core WHERE id=%s AND annotation_type='note'", (rid,))
        con.commit()
        return int(cur.rowcount or 0) > 0
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def set_task_company_pg(todo_id: int, ticker: str = "", due_date: str = "") -> bool:
    rid = int(todo_id or 0)
    if rid <= 0:
        return False
    tk = str(ticker or "").strip().upper()[:16]
    dd = str(due_date or "").strip()
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            """
            UPDATE investor_annotations_core
            SET entity_type=CASE WHEN %s <> '' THEN 'company' ELSE 'global' END,
                entity_id=%s,
                due_date=CASE WHEN %s <> '' THEN %s ELSE due_date END,
                category=CASE WHEN %s <> '' THEN 'company' ELSE category END,
                updated_at=%s
            WHERE id=%s AND annotation_type='task'
            """,
            (tk, tk, dd, dd, tk, dt.datetime.now().isoformat(), rid),
        )
        con.commit()
        return int(cur.rowcount or 0) > 0
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def get_daily_note_pg(day: str) -> dict[str, Any]:
    d = str(day or "").strip()
    con = pg_connect()
    if con is None:
        return {"day": d, "content": "", "updated_at": "", "locked": False, "archived_at": "", "tags": []}
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT day, content, updated_at, locked, archived_at FROM daily_notes_core WHERE day=%s LIMIT 1",
            (d,),
        )
        row = cur.fetchone()
        cur.execute(
            "SELECT ticker FROM daily_note_tags_core WHERE day=%s ORDER BY ticker ASC",
            (d,),
        )
        tags = [str(r[0] or "").strip().upper() for r in _iter_cursor_rows(cur) if str(r[0] or "").strip()]
        if not row:
            return {"day": d, "content": "", "updated_at": "", "locked": False, "archived_at": "", "tags": tags}
        return {
            "day": str(row[0] or d),
            "content": str(row[1] or ""),
            "updated_at": str(row[2] or ""),
            "locked": bool(int(row[3] or 0)),
            "archived_at": str(row[4] or ""),
            "tags": tags,
        }
    except Exception:
        return {"day": d, "content": "", "updated_at": "", "locked": False, "archived_at": "", "tags": []}
    finally:
        con.close()


def save_daily_note_pg(day: str, content: str, tags: list[str] | None = None) -> bool:
    d = str(day or "").strip()
    txt = str(content or "").strip()
    now = dt.datetime.now().isoformat()
    tag_rows = [str(t or "").strip().upper()[:16] for t in (tags or []) if str(t or "").strip()]
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("SELECT locked FROM daily_notes_core WHERE day=%s LIMIT 1", (d,))
        locked_row = cur.fetchone()
        if locked_row and bool(int(locked_row[0] or 0)):
            return False
        cur.execute(
            """
            INSERT INTO daily_notes_core(day, content, updated_at, locked, archived_at)
            VALUES (%s, %s, %s, 0, '')
            ON CONFLICT(day) DO UPDATE SET content=EXCLUDED.content, updated_at=EXCLUDED.updated_at
            """,
            (d, txt[:120000], now),
        )
        cur.execute("DELETE FROM daily_note_tags_core WHERE day=%s", (d,))
        for tk in tag_rows:
            cur.execute(
                "INSERT INTO daily_note_tags_core(day, ticker, created_at) VALUES (%s, %s, %s)",
                (d, tk, now),
            )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def close_day_pg(day: str) -> bool:
    d = str(day or "").strip()
    now = dt.datetime.now().isoformat()
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE daily_notes_core SET locked=1, archived_at=%s, updated_at=%s WHERE day=%s",
            (now, now, d),
        )
        con.commit()
        return int(cur.rowcount or 0) > 0
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def enqueue_action_pg(
    tool_name: str,
    params_json: str,
    reasoning: str,
    confidence: float,
    trace_id: str = "",
) -> bool:
    nm = str(tool_name or "").strip()
    if not nm:
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO ai_action_queue_core(created_at, status, tool_name, params_json, reasoning, confidence, trace_id)
            VALUES (%s, 'pending', %s, %s, %s, %s, %s)
            """,
            (
                dt.datetime.now().isoformat(),
                nm[:120],
                str(params_json or "")[:8000],
                str(reasoning or "")[:3000],
                float(confidence or 0.0),
                str(trace_id or "")[:120],
            ),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def list_action_queue_pg(limit: int = 120) -> list[dict[str, str]]:
    lim = max(1, min(1000, int(limit or 120)))
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, created_at, status, tool_name, params_json, reasoning, confidence, trace_id
            FROM ai_action_queue_core
            WHERE status='pending'
            ORDER BY id DESC
            LIMIT %s
            """,
            (lim,),
        )
        out: list[dict[str, str]] = []
        for r in _iter_cursor_rows(cur):
            out.append(
                {
                    "id": str(r[0] or ""),
                    "created_at": str(r[1] or ""),
                    "status": str(r[2] or ""),
                    "tool_name": str(r[3] or ""),
                    "params_json": str(r[4] or ""),
                    "reasoning": str(r[5] or ""),
                    "confidence": str(r[6] or "0"),
                    "trace_id": str(r[7] or ""),
                }
            )
        return out
    except Exception:
        return []
    finally:
        con.close()


def resolve_action_queue_pg(action_id: int, decision: str) -> bool:
    rid = int(action_id or 0)
    dec = str(decision or "").strip().lower()
    if rid <= 0 or dec not in {"approved", "rejected"}:
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE ai_action_queue_core SET status=%s WHERE id=%s AND status='pending'",
            (dec, rid),
        )
        con.commit()
        return int(cur.rowcount or 0) > 0
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def list_action_log_pg(limit: int = 120) -> list[dict[str, str]]:
    con = pg_connect()
    if con is None:
        return []
    lim = max(1, min(1000, int(limit or 120)))
    out: list[dict[str, str]] = []
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, created_at, query, intent, status, confidence, payload_json
            FROM ai_action_log_core
            ORDER BY id DESC
            LIMIT %s
            """,
            (lim,),
        )
        for r in _iter_cursor_rows(cur):
            out.append(
                {
                    "id": str((r[0] if isinstance(r, (tuple, list)) else r["id"]) or ""),
                    "created_at": str((r[1] if isinstance(r, (tuple, list)) else r["created_at"]) or ""),
                    "query": str((r[2] if isinstance(r, (tuple, list)) else r["query"]) or ""),
                    "intent": str((r[3] if isinstance(r, (tuple, list)) else r["intent"]) or ""),
                    "status": str((r[4] if isinstance(r, (tuple, list)) else r["status"]) or ""),
                    "confidence": str((r[5] if isinstance(r, (tuple, list)) else r["confidence"]) or "0"),
                    "payload_json": str((r[6] if isinstance(r, (tuple, list)) else r["payload_json"]) or ""),
                }
            )
        return out
    except Exception:
        return []
    finally:
        con.close()


def list_company_reminders_pg(ticker: str, limit: int = 160) -> list[dict[str, Any]]:
    tk = str(ticker or "").strip().upper()
    if not tk:
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            """SELECT id, remind_at, note, status, created_at
               FROM company_reminders_core
               WHERE ticker=%s
               ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, id DESC
               LIMIT %s""",
            (tk, max(1, min(1000, int(limit or 160)))),
        )
        out: list[dict[str, Any]] = []
        for r in _iter_cursor_rows(cur):
            out.append(
                {
                    "id": int(r[0] or 0),
                    "remind_at": str(r[1] or ""),
                    "note": str(r[2] or ""),
                    "status": str(r[3] or "open"),
                    "created_at": str(r[4] or ""),
                }
            )
        return out
    except Exception:
        return []
    finally:
        con.close()


def add_company_reminder_pg(ticker: str, remind_at: str, note: str) -> bool:
    tk = str(ticker or "").strip().upper()
    if not tk or not str(note or "").strip():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO company_reminders_core (id, ticker, remind_at, note, status, created_at)
            VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM company_reminders_core), %s,%s,%s,'open',%s)
            """,
            (tk[:16], str(remind_at or "")[:64], str(note or "")[:500], dt.datetime.now().isoformat()),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def toggle_company_reminder_pg(reminder_id: int) -> bool:
    rid = int(reminder_id or 0)
    if rid <= 0:
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("SELECT status FROM company_reminders_core WHERE id=%s", (rid,))
        row = cur.fetchone()
        if not row:
            return False
        nxt = "done" if str(row[0] or "open").strip().lower() == "open" else "open"
        cur.execute("UPDATE company_reminders_core SET status=%s WHERE id=%s", (nxt, rid))
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def update_company_reminder_pg(reminder_id: int, remind_at: str, note: str) -> bool:
    rid = int(reminder_id or 0)
    if rid <= 0 or not str(note or "").strip():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE company_reminders_core SET remind_at=%s, note=%s, created_at=%s WHERE id=%s",
            (str(remind_at or "")[:64], str(note or "")[:500], dt.datetime.now().isoformat(), rid),
        )
        con.commit()
        return int(cur.rowcount or 0) > 0
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def delete_company_reminder_pg(reminder_id: int) -> bool:
    rid = int(reminder_id or 0)
    if rid <= 0:
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM company_reminders_core WHERE id=%s", (rid,))
        con.commit()
        return int(cur.rowcount or 0) > 0
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def list_blue_chips_pg(limit: int = 500) -> list[dict[str, str]]:
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute("SELECT ticker, added_at, reason FROM blue_chips_core ORDER BY added_at DESC, ticker ASC LIMIT %s", (max(1, min(5000, int(limit or 500))),))
        out: list[dict[str, str]] = []
        for r in _iter_cursor_rows(cur):
            out.append(
                {
                    "ticker": str(r[0] or "").strip().upper(),
                    "added_at": str(r[1] or "").strip(),
                    "reason": str(r[2] or "").strip(),
                }
            )
        return out
    except Exception:
        return []
    finally:
        con.close()


def upsert_blue_chip_pg(ticker: str, reason: str = "") -> bool:
    tk = str(ticker or "").strip().upper()
    if not tk:
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        cur.execute(
            """
            INSERT INTO blue_chips_core(ticker, added_at, reason)
            VALUES (%s,%s,%s)
            ON CONFLICT(ticker) DO UPDATE SET
              added_at=EXCLUDED.added_at,
              reason=EXCLUDED.reason
            """,
            (tk, now, str(reason or "").strip()[:240]),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def remove_blue_chip_pg(ticker: str) -> bool:
    tk = str(ticker or "").strip().upper()
    if not tk:
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM blue_chips_core WHERE ticker=%s", (tk,))
        ok = cur.rowcount > 0
        con.commit()
        return bool(ok)
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def ensure_market_snapshots_schema_pg() -> bool:
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS news_wire_snapshot_core (
                id BIGSERIAL PRIMARY KEY,
                panel TEXT NOT NULL DEFAULT 'general',
                title TEXT NOT NULL DEFAULT '',
                link TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',
                fetched_at TEXT NOT NULL DEFAULT '',
                unique_key TEXT NOT NULL UNIQUE
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_news_wire_core_panel_fetched ON news_wire_snapshot_core(panel, fetched_at DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS earnings_calendar_snapshot_core (
                id BIGSERIAL PRIMARY KEY,
                event_date TEXT NOT NULL DEFAULT '',
                symbol TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '',
                time_text TEXT NOT NULL DEFAULT '',
                reported TEXT NOT NULL DEFAULT '0',
                verdict TEXT NOT NULL DEFAULT '',
                surprise_txt TEXT NOT NULL DEFAULT '',
                eps_actual TEXT NOT NULL DEFAULT '',
                eps_estimate TEXT NOT NULL DEFAULT '',
                result_source TEXT NOT NULL DEFAULT '',
                event_status TEXT NOT NULL DEFAULT 'upcoming',
                confidence TEXT NOT NULL DEFAULT '',
                fetched_at TEXT NOT NULL DEFAULT '',
                UNIQUE(event_date, symbol, time_text)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_earnings_snap_core_date ON earnings_calendar_snapshot_core(event_date, symbol)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_earnings_snap_core_fetched ON earnings_calendar_snapshot_core(fetched_at DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS lows_snapshot_core (
                id BIGSERIAL PRIMARY KEY,
                category TEXT NOT NULL DEFAULT 'both',
                ticker TEXT NOT NULL DEFAULT '',
                current_price TEXT NOT NULL DEFAULT '',
                above_low TEXT NOT NULL DEFAULT '',
                from_high TEXT NOT NULL DEFAULT '',
                is_at_low BOOLEAN NOT NULL DEFAULT FALSE,
                company TEXT NOT NULL DEFAULT '',
                fetched_at TEXT NOT NULL DEFAULT '',
                UNIQUE(category, ticker, fetched_at)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lows_snap_core_cat ON lows_snapshot_core(category, fetched_at DESC)")
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def upsert_news_wire_snapshot_pg(panel: str, items: list[dict[str, str]]) -> int:
    pnl = str(panel or "general").strip().lower()
    if pnl not in {"general", "company"}:
        pnl = "general"
    rows = [x for x in (items or []) if isinstance(x, dict)]
    if not rows:
        return 0
    con = pg_connect()
    if con is None:
        return 0
    _ = ensure_market_snapshots_schema_pg()
    now = dt.datetime.now().isoformat()
    written = 0
    try:
        cur = con.cursor()
        for it in rows:
            title = str(it.get("title") or "").strip()
            if not title:
                continue
            link = str(it.get("link") or "").strip()
            source = str(it.get("source") or "").strip()
            published_at = str(it.get("published_at") or "").strip()
            key = " ".join(f"{pnl}|{title}|{link}".lower().split())
            cur.execute(
                """
                INSERT INTO news_wire_snapshot_core(panel, title, link, source, published_at, fetched_at, unique_key)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(unique_key) DO UPDATE SET
                  source=EXCLUDED.source,
                  published_at=EXCLUDED.published_at,
                  fetched_at=EXCLUDED.fetched_at
                """,
                (pnl, title[:600], link[:1200], source[:200], published_at[:80], now, key[:1200]),
            )
            written += 1
        con.commit()
        try:
            cur.execute(
                """
                DELETE FROM news_wire_snapshot_core
                WHERE panel=%s
                  AND id NOT IN (
                    SELECT id FROM news_wire_snapshot_core
                    WHERE panel=%s
                    ORDER BY fetched_at DESC, id DESC
                    LIMIT 400
                  )
                """,
                (pnl, pnl),
            )
            con.commit()
        except Exception:
            pass
        return written
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return 0
    finally:
        con.close()


def list_news_wire_snapshot_pg(panel: str, limit: int = 12, max_age_hours: int = 168) -> list[dict[str, str]]:
    pnl = str(panel or "general").strip().lower()
    if pnl not in {"general", "company"}:
        pnl = "general"
    con = pg_connect()
    if con is None:
        return []
    _ = ensure_market_snapshots_schema_pg()
    out: list[dict[str, str]] = []
    try:
        cur = con.cursor()
        cutoff = (dt.datetime.now() - dt.timedelta(hours=max(1, int(max_age_hours or 168)))).isoformat()
        cur.execute(
            """
            SELECT title, link, source, published_at, fetched_at
            FROM news_wire_snapshot_core
            WHERE panel=%s AND fetched_at >= %s
            ORDER BY fetched_at DESC, id DESC
            LIMIT %s
            """,
            (pnl, cutoff, max(1, min(200, int(limit or 12)))),
        )
        for r in _iter_cursor_rows(cur):
            out.append(
                {
                    "title": str(r[0] or ""),
                    "link": str(r[1] or ""),
                    "source": str(r[2] or ""),
                    "published_at": str(r[3] or ""),
                    "fetched_at": str(r[4] or ""),
                }
            )
        return out
    except Exception:
        return []
    finally:
        con.close()


def upsert_earnings_calendar_snapshot_pg(rows: list[dict[str, str]]) -> int:
    events = [x for x in (rows or []) if isinstance(x, dict)]
    if not events:
        return 0
    con = pg_connect()
    if con is None:
        return 0
    _ = ensure_market_snapshots_schema_pg()
    now = dt.datetime.now().isoformat()
    written = 0
    try:
        cur = con.cursor()
        for ev in events:
            d = str(ev.get("date") or "").strip()
            sym = str(ev.get("symbol") or "").strip().upper()
            if not d or not sym:
                continue
            cur.execute(
                """
                INSERT INTO earnings_calendar_snapshot_core
                (event_date, symbol, company, time_text, reported, verdict, surprise_txt, eps_actual, eps_estimate, result_source, event_status, confidence, fetched_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(event_date, symbol, time_text) DO UPDATE SET
                  company=EXCLUDED.company,
                  reported=EXCLUDED.reported,
                  verdict=EXCLUDED.verdict,
                  surprise_txt=EXCLUDED.surprise_txt,
                  eps_actual=EXCLUDED.eps_actual,
                  eps_estimate=EXCLUDED.eps_estimate,
                  result_source=EXCLUDED.result_source,
                  event_status=EXCLUDED.event_status,
                  confidence=EXCLUDED.confidence,
                  fetched_at=EXCLUDED.fetched_at
                """,
                (
                    d[:20],
                    sym[:16],
                    str(ev.get("company") or "-")[:300],
                    str(ev.get("time") or "-")[:40],
                    str(ev.get("reported") or "0")[:8],
                    str(ev.get("verdict") or "")[:20],
                    str(ev.get("surprise_txt") or "")[:32],
                    str(ev.get("eps_actual") or "-")[:32],
                    str(ev.get("eps_estimate") or "-")[:32],
                    str(ev.get("result_source") or "")[:220],
                    str(ev.get("event_status") or "upcoming")[:20],
                    str(ev.get("confidence") or "")[:20],
                    now,
                ),
            )
            written += 1
        con.commit()
        try:
            cutoff = (dt.datetime.now() - dt.timedelta(days=21)).isoformat()
            cur.execute("DELETE FROM earnings_calendar_snapshot_core WHERE fetched_at < %s", (cutoff,))
            con.commit()
        except Exception:
            pass
        return written
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return 0
    finally:
        con.close()


def list_earnings_calendar_snapshot_pg(week_start: str, week_end: str, limit: int = 200) -> list[dict[str, str]]:
    ws = str(week_start or "").strip()
    we = str(week_end or "").strip()
    if not ws or not we:
        return []
    con = pg_connect()
    if con is None:
        return []
    _ = ensure_market_snapshots_schema_pg()
    out: list[dict[str, str]] = []
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT event_date, symbol, company, time_text, reported, verdict, surprise_txt,
                   eps_actual, eps_estimate, result_source, event_status, confidence
            FROM earnings_calendar_snapshot_core
            WHERE event_date >= %s AND event_date <= %s
            ORDER BY event_date ASC, symbol ASC
            LIMIT %s
            """,
            (ws, we, max(1, min(1000, int(limit or 200)))),
        )
        for r in _iter_cursor_rows(cur):
            out.append(
                {
                    "date": str(r[0] or ""),
                    "symbol": str(r[1] or "").strip().upper(),
                    "company": str(r[2] or "-"),
                    "time": str(r[3] or "-"),
                    "reported": str(r[4] or "0"),
                    "verdict": str(r[5] or ""),
                    "surprise_txt": str(r[6] or ""),
                    "eps_actual": str(r[7] or "-"),
                    "eps_estimate": str(r[8] or "-"),
                    "result_source": str(r[9] or ""),
                    "event_status": str(r[10] or "upcoming"),
                    "confidence": str(r[11] or ""),
                }
            )
        return out
    except Exception:
        return []
    finally:
        con.close()


def add_agent_feedback_memory_pg(ticker: str, summary: str) -> None:
    """Write a user-feedback signal into agent_memory_core so future agent runs see it."""
    tk = str(ticker or "").strip().upper()
    msg = str(summary or "").strip()[:600]
    if not tk or not msg:
        return
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agent_memory_core (
                id BIGSERIAL PRIMARY KEY,
                ticker TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            )
        """)
        import datetime as _dt
        now = _dt.datetime.now().isoformat()
        cur.execute(
            "INSERT INTO agent_memory_core (ticker, summary, updated_at, created_at) "
            "VALUES (%s, %s, %s, %s)",
            (tk, msg, now, now),
        )
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


def save_company_moats_pg(ticker: str, moat_keys: list) -> bool:
    import random as _random
    t = str(ticker or "").strip().upper()
    if not t:
        return False
    picked = sorted({str(k or "").strip().lower() for k in moat_keys if str(k or "").strip()})
    now = dt.datetime.now().isoformat()
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM company_moat_tags_core WHERE ticker = %s", (t,))
        for mk in picked:
            row_id = _random.randint(1, 2**62)
            cur.execute(
                "INSERT INTO company_moat_tags_core(id, ticker, moat_key, updated_at, note)"
                " VALUES (%s,%s,%s,%s,%s)"
                " ON CONFLICT(ticker, moat_key) DO UPDATE SET updated_at=EXCLUDED.updated_at",
                (row_id, t, mk, now, ""),
            )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def add_company_competitor_pg(
    ticker: str,
    competitor_ticker: str,
    competitor_name: str,
    source_form: str = "",
    source_date: str = "",
    evidence: str = "",
    confidence: float = 1.0,
) -> bool:
    import random as _random
    t = str(ticker or "").strip().upper()
    ct = str(competitor_ticker or "").strip().upper()
    name = str(competitor_name or "").strip()[:160]
    if not t or (not ct and not name):
        return False
    now = dt.datetime.now().isoformat()
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        row_id = _random.randint(1, 2**62)
        cur.execute(
            """
            INSERT INTO company_sec_competitors_core
              (id, ticker, competitor_ticker, competitor_name, source_form, source_date,
               source_path, evidence, confidence, status, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(ticker, competitor_ticker) DO NOTHING
            """,
            (row_id, t, ct, name,
             str(source_form or "")[:80], str(source_date or "")[:40],
             "", str(evidence or "")[:1200], float(confidence or 1.0), "active", now),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def update_company_competitor_pg(
    comp_id: int,
    competitor_ticker: str = "",
    competitor_name: str = "",
    evidence: str = "",
) -> bool:
    rid = int(comp_id or 0)
    if rid <= 0:
        return False
    now = dt.datetime.now().isoformat()
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            """
            UPDATE company_sec_competitors_core
            SET competitor_ticker=%s, competitor_name=%s, evidence=%s, updated_at=%s
            WHERE id=%s
            """,
            (str(competitor_ticker or "").strip().upper(),
             str(competitor_name or "").strip()[:160],
             str(evidence or "").strip()[:1200],
             now, rid),
        )
        con.commit()
        return cur.rowcount > 0
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def remove_company_competitor_pg(comp_id: int) -> bool:
    rid = int(comp_id or 0)
    if rid <= 0:
        return False
    now = dt.datetime.now().isoformat()
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE company_sec_competitors_core SET status='removed', updated_at=%s WHERE id=%s",
            (now, rid),
        )
        con.commit()
        return cur.rowcount > 0
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


# ── Financial Lab: case studies ──────────────────────────────────────────────

def ensure_lab_schema() -> bool:
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lab_case_studies_core (
                id          SERIAL PRIMARY KEY,
                ticker      TEXT NOT NULL,
                title       TEXT NOT NULL,
                module      TEXT NOT NULL,
                params_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT NOT NULL DEFAULT '{}',
                notes       TEXT DEFAULT '',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_lab_cases_ticker ON lab_case_studies_core(ticker);
        """)
        con.commit()
        return True
    except Exception:
        return False
    finally:
        con.close()


def save_lab_case_study_pg(ticker: str, title: str, module: str,
                           params_json: str, result_json: str, notes: str) -> int:
    now = dt.datetime.now().isoformat()
    con = pg_connect()
    if con is None:
        return 0
    try:
        cur = con.cursor()
        cur.execute(
            """INSERT INTO lab_case_studies_core
               (ticker, title, module, params_json, result_json, notes, created_at, updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (ticker, title, module, params_json, result_json, notes, now, now),
        )
        row = cur.fetchone()
        con.commit()
        return int(row[0]) if row else 0
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return 0
    finally:
        con.close()


def list_lab_case_studies_pg(ticker: str) -> list[dict]:
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            """SELECT id, ticker, title, module, params_json, result_json, notes, created_at, updated_at
               FROM lab_case_studies_core WHERE ticker=%s ORDER BY created_at DESC""",
            (ticker,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in _iter_cursor_rows(cur)]
    except Exception:
        return []
    finally:
        con.close()


def delete_lab_case_study_pg(case_id: int) -> bool:
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM lab_case_studies_core WHERE id=%s", (case_id,))
        con.commit()
        return cur.rowcount > 0
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


# ── 52-Week Lows Snapshot ──────────────────────────────────────────


def upsert_lows_snapshot_pg(category: str, rows: list[dict[str, object]]) -> int:
    """Save parsed 52-week lows to Postgres so Cloud Run can read them."""
    cat = str(category or "both").strip().lower()
    if cat not in {"both", "52"}:
        cat = "both"
    con = pg_connect()
    if con is None:
        return 0
    _ = ensure_market_snapshots_schema_pg()
    now = dt.datetime.now().strftime("%Y-%m-%d")
    written = 0
    try:
        cur = con.cursor()
        # Clear stale entries for this category+date before inserting fresh data.
        cur.execute("DELETE FROM lows_snapshot_core WHERE category = %s AND fetched_at = %s", (cat, now))
        for row in rows:
            tk = str(row.get("ticker") or "").strip().upper()
            if not tk:
                continue
            cur.execute(
                """
                INSERT INTO lows_snapshot_core
                (category, ticker, current_price, above_low, from_high, is_at_low, company, fetched_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(category, ticker, fetched_at) DO UPDATE SET
                  current_price=EXCLUDED.current_price,
                  above_low=EXCLUDED.above_low,
                  from_high=EXCLUDED.from_high,
                  is_at_low=EXCLUDED.is_at_low,
                  company=EXCLUDED.company
                """,
                (
                    cat,
                    tk[:16],
                    str(row.get("current") or "-")[:32],
                    str(row.get("above_low") or "-")[:16],
                    str(row.get("from_high") or "-")[:16],
                    bool(row.get("is_at_low")),
                    str(row.get("company") or "")[:200],
                    now,
                ),
            )
            written += 1
        con.commit()
        # Purge entries older than 7 days.
        cutoff = (dt.datetime.now() - dt.timedelta(days=7)).strftime("%Y-%m-%d")
        try:
            cur.execute("DELETE FROM lows_snapshot_core WHERE fetched_at < %s", (cutoff,))
            con.commit()
        except Exception:
            pass
        return written
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return 0
    finally:
        con.close()


def list_lows_snapshot_pg(category: str) -> list[dict[str, object]]:
    """Read latest 52-week lows from Postgres."""
    cat = str(category or "both").strip().lower()
    con = pg_connect()
    if con is None:
        return []
    _ = ensure_market_snapshots_schema_pg()
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT ticker, current_price, above_low, from_high, is_at_low, company
            FROM lows_snapshot_core
            WHERE category = %s
              AND fetched_at = (SELECT MAX(fetched_at) FROM lows_snapshot_core WHERE category = %s)
            ORDER BY above_low ASC
            LIMIT 30
            """,
            (cat, cat),
        )
        out: list[dict[str, object]] = []
        for r in _iter_cursor_rows(cur):
            out.append(
                {
                    "ticker": str(r[0] or ""),
                    "current": str(r[1] or "-"),
                    "above_low": str(r[2] or "-"),
                    "from_high": str(r[3] or "-"),
                    "is_at_low": bool(r[4]),
                    "company": str(r[5] or ""),
                }
            )
        return out
    except Exception:
        return []
    finally:
        con.close()
