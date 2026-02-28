from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from typing import Any

from app.core.config import CORE_DB_PATH
from app.core.sqlite_hardening import connect_sqlite
from sqlalchemy.pool import QueuePool


def core_backend() -> str:
    return str(os.getenv("CORE_DB_BACKEND", "sqlite")).strip().lower()


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


def _sqlite_table_exists(con: sqlite3.Connection, table: str) -> bool:
    try:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (str(table or "").strip(),),
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def ensure_postgres_core_schema() -> dict[str, Any]:
    con = pg_connect()
    if con is None:
        return {"ok": False, "error": "pg_not_available"}
    try:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS action_proposals_core (
                id BIGINT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL,
                kind TEXT NOT NULL,
                ticker TEXT NOT NULL,
                title TEXT NOT NULL,
                thesis_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                citations_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                insights_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                reasoning_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                priority_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                execute_route TEXT NOT NULL DEFAULT '',
                execute_payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                source_event_key TEXT NOT NULL UNIQUE,
                proposal_uid TEXT NOT NULL DEFAULT '',
                target_ticker TEXT NOT NULL DEFAULT '',
                suggested_action TEXT NOT NULL DEFAULT 'REVIEW',
                thesis_summary TEXT NOT NULL DEFAULT '',
                confidence_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                dismissed_reason TEXT NOT NULL DEFAULT '',
                rejection_reason TEXT NOT NULL DEFAULT '',
                rejected_at TEXT NOT NULL DEFAULT '',
                executed_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ap_core_status ON action_proposals_core(status, priority_score DESC, id DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ap_core_ticker ON action_proposals_core(ticker)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_transactions_core (
                id BIGINT PRIMARY KEY,
                created_at TEXT NOT NULL,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL,
                shares DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                price DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                note TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'app',
                meta_json JSONB NOT NULL DEFAULT '{}'::jsonb
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pt_core_ticker ON portfolio_transactions_core(ticker)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pt_core_created ON portfolio_transactions_core(created_at DESC)")
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
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist_thesis_core (
                ticker TEXT PRIMARY KEY,
                thesis TEXT NOT NULL DEFAULT '',
                thesis_summary TEXT NOT NULL DEFAULT '',
                pick_method TEXT NOT NULL DEFAULT '',
                triggers TEXT NOT NULL DEFAULT '',
                invalidation TEXT NOT NULL DEFAULT '',
                conviction_rating INTEGER NOT NULL DEFAULT 0,
                time_horizon TEXT NOT NULL DEFAULT '',
                invalidation_criteria TEXT NOT NULL DEFAULT '',
                strategy_tag TEXT NOT NULL DEFAULT 'CORE',
                pattern_learnable INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS investor_style_memory_core (
                key TEXT PRIMARY KEY,
                answer TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS filings_core (
                id BIGINT PRIMARY KEY,
                ticker TEXT NOT NULL DEFAULT '',
                form TEXT NOT NULL DEFAULT '',
                date TEXT NOT NULL DEFAULT '',
                accession TEXT NOT NULL DEFAULT '',
                doc_url TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL DEFAULT '',
                downloaded_at TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_filings_core_ticker_date ON filings_core(ticker, date DESC)")
        # Migration: add content column to existing tables that don't have it yet
        cur.execute("""
            ALTER TABLE filings_core ADD COLUMN IF NOT EXISTS content TEXT NOT NULL DEFAULT ''
        """)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS report_facts_core (
                id BIGINT PRIMARY KEY,
                report_name TEXT NOT NULL DEFAULT '',
                report_kind TEXT NOT NULL DEFAULT '',
                report_modified TEXT NOT NULL DEFAULT '',
                fact_date TEXT NOT NULL DEFAULT '',
                ticker TEXT NOT NULL DEFAULT '',
                fact_text TEXT NOT NULL DEFAULT '',
                importance INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT '',
                fact_hash TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_rf_core_ticker_date ON report_facts_core(ticker, fact_date DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_rf_core_importance ON report_facts_core(importance DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS todos_core (
                id BIGINT PRIMARY KEY,
                task TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL DEFAULT '',
                priority TEXT NOT NULL DEFAULT 'P2',
                due_date TEXT NOT NULL DEFAULT '',
                snooze_until TEXT NOT NULL DEFAULT '',
                ticker TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'general'
            )
            """
        )
        try:
            cur.execute("ALTER TABLE todos_core ADD COLUMN IF NOT EXISTS snooze_until TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        cur.execute("CREATE INDEX IF NOT EXISTS idx_todos_core_status ON todos_core(status, id DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_todos_core_ticker ON todos_core(ticker, id DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS workspace_journal_core (
                id BIGINT PRIMARY KEY,
                ticker TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL DEFAULT 'Note',
                emotion TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'approved',
                created_by TEXT NOT NULL DEFAULT 'human'
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_wj_core_ticker ON workspace_journal_core(ticker, id DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS investor_notes_core (
                id BIGINT PRIMARY KEY,
                scope TEXT NOT NULL DEFAULT 'organizer',
                ticker TEXT NOT NULL DEFAULT '',
                sentiment TEXT NOT NULL DEFAULT 'neutral',
                note TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT 'log',
                created_at TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'approved',
                created_by TEXT NOT NULL DEFAULT 'human',
                ai_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                ai_reasoning TEXT NOT NULL DEFAULT '',
                trace_id TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_inv_notes_core_created ON investor_notes_core(created_at DESC, id DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_inv_notes_core_ticker ON investor_notes_core(ticker, id DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS investor_annotations_core (
                id BIGSERIAL PRIMARY KEY,
                entity_type TEXT NOT NULL DEFAULT 'global',
                entity_id TEXT NOT NULL DEFAULT '',
                annotation_type TEXT NOT NULL DEFAULT 'note',
                content TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                priority TEXT NOT NULL DEFAULT 'P2',
                due_date TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'general',
                snooze_until TEXT NOT NULL DEFAULT '',
                source_ref TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT 'human',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        try:
            cur.execute("ALTER TABLE investor_annotations_core ADD COLUMN IF NOT EXISTS priority TEXT NOT NULL DEFAULT 'P2'")
            cur.execute("ALTER TABLE investor_annotations_core ADD COLUMN IF NOT EXISTS due_date TEXT NOT NULL DEFAULT ''")
            cur.execute("ALTER TABLE investor_annotations_core ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'general'")
            cur.execute("ALTER TABLE investor_annotations_core ADD COLUMN IF NOT EXISTS source_ref TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        cur.execute("CREATE INDEX IF NOT EXISTS idx_anno_core_entity ON investor_annotations_core(entity_type, entity_id, created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_anno_core_type ON investor_annotations_core(annotation_type, status, created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_anno_core_due ON investor_annotations_core(due_date, snooze_until)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_anno_core_source_ref ON investor_annotations_core(source_ref) WHERE source_ref <> ''")
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
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS company_reminders_core (
                id BIGINT PRIMARY KEY,
                ticker TEXT NOT NULL DEFAULT '',
                remind_at TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cr_core_ticker ON company_reminders_core(ticker, id DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_compact_core (
                id BIGINT PRIMARY KEY,
                memory_key TEXT NOT NULL UNIQUE,
                bucket TEXT NOT NULL DEFAULT 'preference',
                value TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'chat_turn',
                reliability DOUBLE PRECISION NOT NULL DEFAULT 0.7,
                reuse_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                conflict_of TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                last_used_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_mem_core_bucket ON memory_compact_core(bucket)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_mem_core_status ON memory_compact_core(status)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS company_profile_cache_core (
                ticker TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                country TEXT NOT NULL DEFAULT '',
                industry TEXT NOT NULL DEFAULT '',
                sector TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cpc_core_name ON company_profile_cache_core(name)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cpc_core_industry ON company_profile_cache_core(industry)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS companies_core (
                ticker TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                cik TEXT NOT NULL DEFAULT '',
                added_date TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_companies_core_name ON companies_core(name)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS universe_registry_core (
                ticker TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                cik TEXT NOT NULL DEFAULT '',
                exchange TEXT NOT NULL DEFAULT '',
                is_us_listed BOOLEAN NOT NULL DEFAULT FALSE,
                is_otc BOOLEAN NOT NULL DEFAULT FALSE,
                source TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_universe_registry_core_exchange ON universe_registry_core(exchange)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_universe_registry_core_us ON universe_registry_core(is_us_listed)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS company_lists_core (
                id BIGINT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_company_lists_core_name ON company_lists_core(name)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS company_list_items_core (
                id BIGINT PRIMARY KEY,
                list_id BIGINT NOT NULL,
                ticker TEXT NOT NULL,
                added_at TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                UNIQUE(list_id, ticker)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cli_core_list_id ON company_list_items_core(list_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cli_core_ticker ON company_list_items_core(ticker)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS company_moat_tags_core (
                id BIGINT PRIMARY KEY,
                ticker TEXT NOT NULL,
                moat_key TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                UNIQUE(ticker, moat_key)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_moat_core_ticker ON company_moat_tags_core(ticker)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_moat_core_key ON company_moat_tags_core(moat_key)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS company_sec_competitors_core (
                id BIGINT PRIMARY KEY,
                ticker TEXT NOT NULL,
                competitor_ticker TEXT NOT NULL DEFAULT '',
                competitor_name TEXT NOT NULL DEFAULT '',
                source_form TEXT NOT NULL DEFAULT '',
                source_date TEXT NOT NULL DEFAULT '',
                source_path TEXT NOT NULL DEFAULT '',
                evidence TEXT NOT NULL DEFAULT '',
                confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                status TEXT NOT NULL DEFAULT 'active',
                updated_at TEXT NOT NULL DEFAULT '',
                UNIQUE(ticker, competitor_ticker)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_comp_core_ticker ON company_sec_competitors_core(ticker, status)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS blue_chips_core (
                ticker TEXT PRIMARY KEY,
                added_at TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_blue_chips_core_added ON blue_chips_core(added_at DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS proactive_monitor_state_core (
                state_key TEXT PRIMARY KEY,
                state_value TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_runs_core (
                id BIGINT PRIMARY KEY,
                run_uid TEXT NOT NULL UNIQUE,
                agent_name TEXT NOT NULL,
                trigger_type TEXT NOT NULL DEFAULT 'event_driven',
                status TEXT NOT NULL DEFAULT 'running',
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL DEFAULT '',
                duration_ms DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                trace_id TEXT NOT NULL DEFAULT '',
                input_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                output_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                error_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_core_created ON agent_runs_core(created_at DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS reflexion_notes_core (
                id BIGINT PRIMARY KEY,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                query TEXT NOT NULL DEFAULT '',
                detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                note_text TEXT NOT NULL DEFAULT '',
                rule_key TEXT NOT NULL DEFAULT '',
                rule_text TEXT NOT NULL DEFAULT '',
                confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_reflexion_notes_core_created ON reflexion_notes_core(created_at DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS failure_patterns_core (
                id BIGINT PRIMARY KEY,
                pattern_key TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL,
                total_count INTEGER NOT NULL DEFAULT 0,
                open_count INTEGER NOT NULL DEFAULT 0,
                resolved_count INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_query TEXT NOT NULL DEFAULT '',
                last_detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                last_reflexion TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_failure_patterns_core_last_seen ON failure_patterns_core(last_seen_at DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS reflexion_policy_versions_core (
                id BIGINT PRIMARY KEY,
                version_tag TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                source_event_id BIGINT NOT NULL DEFAULT 0,
                policy_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                is_active INTEGER NOT NULL DEFAULT 0,
                rolled_back_from TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_reflexion_policy_core_active ON reflexion_policy_versions_core(is_active, created_at DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS intel24_snapshot_core (
                ticker TEXT PRIMARY KEY,
                asof TEXT NOT NULL,
                day_pct DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                insider_txt TEXT NOT NULL DEFAULT '',
                sec_txt TEXT NOT NULL DEFAULT '',
                happened TEXT NOT NULL DEFAULT '',
                suggestion TEXT NOT NULL DEFAULT '',
                event_score INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'scheduler'
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_intel24_core_asof ON intel24_snapshot_core(asof DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS intel_feed_core (
                id BIGINT PRIMARY KEY,
                created_at TEXT NOT NULL,
                ticker TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                severity INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                unique_key TEXT NOT NULL UNIQUE
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_intel_feed_core_created ON intel_feed_core(created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_intel_feed_core_ticker ON intel_feed_core(ticker)")
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
            CREATE TABLE IF NOT EXISTS changes_core (
                id BIGINT PRIMARY KEY,
                ticker TEXT NOT NULL,
                filing_id BIGINT NOT NULL DEFAULT 0,
                section_name TEXT NOT NULL DEFAULT '',
                change_type TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                detected_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_changes_core_detected ON changes_core(detected_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_changes_core_ticker ON changes_core(ticker)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_notes_core (
                day TEXT PRIMARY KEY,
                content TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                locked INTEGER NOT NULL DEFAULT 0,
                archived_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_note_tags_core (
                id BIGINT PRIMARY KEY,
                day TEXT NOT NULL,
                ticker TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_daily_note_tags_core_day ON daily_note_tags_core(day)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_daily_note_tags_core_ticker ON daily_note_tags_core(ticker)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_action_queue_core (
                id BIGINT PRIMARY KEY,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                tool_name TEXT NOT NULL,
                params_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                reasoning TEXT NOT NULL DEFAULT '',
                confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                trace_id TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_action_queue_core_status ON ai_action_queue_core(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_action_queue_core_created ON ai_action_queue_core(created_at DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_action_log_core (
                id BIGINT PRIMARY KEY,
                created_at TEXT NOT NULL,
                query TEXT NOT NULL DEFAULT '',
                intent TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                payload_json JSONB NOT NULL DEFAULT '{}'::jsonb
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_action_log_core_created ON ai_action_log_core(created_at DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS system_events_core (
                id BIGINT PRIMARY KEY,
                created_at TEXT NOT NULL,
                service TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                latency_ms DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                message TEXT NOT NULL DEFAULT '',
                trace_id TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_system_events_core_created ON system_events_core(created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_system_events_core_service ON system_events_core(service)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS decision_log_core (
                id BIGINT PRIMARY KEY,
                created_at TEXT NOT NULL,
                timestamp TEXT NOT NULL DEFAULT '',
                ticker TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                reasoning TEXT NOT NULL DEFAULT '',
                confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                source TEXT NOT NULL DEFAULT '',
                trace_id TEXT NOT NULL DEFAULT '',
                quantity DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                price DOUBLE PRECISION NOT NULL DEFAULT 0.0
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_decision_log_core_created ON decision_log_core(created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_decision_log_core_ticker ON decision_log_core(ticker, created_at DESC)")
        # Ensure runtime inserts can omit id safely on existing deployments.
        cur.execute("CREATE SEQUENCE IF NOT EXISTS decision_log_core_id_seq")
        cur.execute(
            "SELECT setval('decision_log_core_id_seq', COALESCE((SELECT MAX(id) FROM decision_log_core), 0) + 1, false)"
        )
        cur.execute("ALTER TABLE decision_log_core ALTER COLUMN id SET DEFAULT nextval('decision_log_core_id_seq')")
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


def sync_core_from_sqlite() -> dict[str, Any]:
    st = ensure_postgres_core_schema()
    if not st.get("ok"):
        return st
    con_pg = pg_connect()
    con_sq = connect_sqlite(str(CORE_DB_PATH), row_factory=True)
    if con_pg is None:
        con_sq.close()
        return {"ok": False, "error": "pg_not_available"}
    counts = {
        "action_proposals_core": 0,
        "portfolio_transactions_core": 0,
        "watchlist_thesis_core": 0,
        "investor_style_memory_core": 0,
        "filings_core": 0,
        "report_facts_core": 0,
        "todos_core": 0,
        "workspace_journal_core": 0,
        "investor_notes_core": 0,
        "company_reminders_core": 0,
        "memory_compact_core": 0,
        "company_profile_cache_core": 0,
        "companies_core": 0,
        "universe_registry_core": 0,
        "company_lists_core": 0,
        "company_list_items_core": 0,
        "company_moat_tags_core": 0,
        "company_sec_competitors_core": 0,
        "blue_chips_core": 0,
        "agent_runs_core": 0,
        "reflexion_notes_core": 0,
        "failure_patterns_core": 0,
        "reflexion_policy_versions_core": 0,
        "intel24_snapshot_core": 0,
        "intel_feed_core": 0,
        "changes_core": 0,
        "daily_notes_core": 0,
        "daily_note_tags_core": 0,
        "ai_action_queue_core": 0,
        "ai_action_log_core": 0,
        "system_events_core": 0,
        "decision_log_core": 0,
        "investor_question_overrides_core": 0,
        "portfolio_interview_queue_core": 0,
        "report_fact_ingest_state_core": 0,
        "morning_briefs_core": 0,
        "learning_events_core": 0,
        "rule_candidates_core": 0,
        "active_rules_core": 0,
        "ai_tool_log_core": 0,
        "ai_quality_log_core": 0,
        "risk_veto_decisions_core": 0,
        "risk_veto_config_core": 0,
        "daily_operator_state_core": 0,
        "daily_operator_gap_prompts_core": 0,
        "user_preferences_core": 0,
        "ai_meta_suggestions_core": 0,
    }
    try:
        cp = con_pg.cursor()
        if _sqlite_table_exists(con_sq, "action_proposals"):
            for r in con_sq.execute("SELECT * FROM action_proposals").fetchall():
                cp.execute(
                """
                INSERT INTO action_proposals_core
                (id, created_at, updated_at, status, kind, ticker, title, thesis_json, citations_json, insights_json, reasoning_json,
                 confidence, priority_score, execute_route, execute_payload_json, source_event_key, proposal_uid, target_ticker,
                 suggested_action, thesis_summary, confidence_score, dismissed_reason, rejection_reason, rejected_at, executed_at)
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
                    int(r["id"] or 0),
                    str(r["created_at"] or ""),
                    str(r["updated_at"] or ""),
                    str(r["status"] or ""),
                    str(r["kind"] or ""),
                    str(r["ticker"] or ""),
                    str(r["title"] or ""),
                    json.dumps(json.loads(str(r["thesis_json"] or "[]")), ensure_ascii=True),
                    json.dumps(json.loads(str(r["citations_json"] or "[]")), ensure_ascii=True),
                    json.dumps(json.loads(str(r["insights_json"] or "[]")), ensure_ascii=True),
                    json.dumps(json.loads(str(r["reasoning_json"] or "{}")), ensure_ascii=True),
                    float(r["confidence"] or 0.0),
                    float(r["priority_score"] or 0.0),
                    str(r["execute_route"] or ""),
                    json.dumps(json.loads(str(r["execute_payload_json"] or "{}")), ensure_ascii=True),
                    str(r["source_event_key"] or ""),
                    str(r["proposal_uid"] or ""),
                    str(r["target_ticker"] or ""),
                    str(r["suggested_action"] or "REVIEW"),
                    str(r["thesis_summary"] or ""),
                    float(r["confidence_score"] or 0.0),
                    str(r["dismissed_reason"] or ""),
                    str(r["rejection_reason"] or ""),
                    str(r["rejected_at"] or ""),
                    str(r["executed_at"] or ""),
                ),
                )
                counts["action_proposals_core"] += 1

        if _sqlite_table_exists(con_sq, "portfolio_transactions"):
            for r in con_sq.execute("SELECT * FROM portfolio_transactions").fetchall():
                cp.execute(
                """
                INSERT INTO portfolio_transactions_core (id, created_at, ticker, action, shares, price, note, source, meta_json)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT(id) DO UPDATE SET
                  created_at=EXCLUDED.created_at,ticker=EXCLUDED.ticker,action=EXCLUDED.action,shares=EXCLUDED.shares,price=EXCLUDED.price,
                  note=EXCLUDED.note,source=EXCLUDED.source,meta_json=EXCLUDED.meta_json
                """,
                (
                    int(r["id"] or 0),
                    str(r["created_at"] or ""),
                    str(r["ticker"] or ""),
                    str(r["action"] or ""),
                    float(r["shares"] or 0.0),
                    float(r["price"] or 0.0),
                    str(r["note"] or ""),
                    str(r["source"] or ""),
                    json.dumps(json.loads(str(r["meta_json"] or "{}")), ensure_ascii=True),
                ),
                )
                counts["portfolio_transactions_core"] += 1

        if _sqlite_table_exists(con_sq, "watchlist_thesis"):
            for r in con_sq.execute("SELECT * FROM watchlist_thesis").fetchall():
                cp.execute(
                """
                INSERT INTO watchlist_thesis_core
                (ticker, thesis, thesis_summary, pick_method, triggers, invalidation, conviction_rating, time_horizon, invalidation_criteria, strategy_tag, pattern_learnable, status, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(ticker) DO UPDATE SET
                  thesis=EXCLUDED.thesis,thesis_summary=EXCLUDED.thesis_summary,pick_method=EXCLUDED.pick_method,triggers=EXCLUDED.triggers,invalidation=EXCLUDED.invalidation,
                  conviction_rating=EXCLUDED.conviction_rating,time_horizon=EXCLUDED.time_horizon,invalidation_criteria=EXCLUDED.invalidation_criteria,
                  strategy_tag=EXCLUDED.strategy_tag,pattern_learnable=EXCLUDED.pattern_learnable,status=EXCLUDED.status,updated_at=EXCLUDED.updated_at
                """,
                (
                    str(r["ticker"] or ""),
                    str(r["thesis"] or ""),
                    str(r["thesis_summary"] or ""),
                    str(r["pick_method"] or ""),
                    str(r["triggers"] or ""),
                    str(r["invalidation"] or ""),
                    int(r["conviction_rating"] or 0),
                    str(r["time_horizon"] or ""),
                    str(r["invalidation_criteria"] or ""),
                    str(r["strategy_tag"] or "CORE"),
                    int(r["pattern_learnable"] or 1),
                    str(r["status"] or "active"),
                    str(r["created_at"] or ""),
                    str(r["updated_at"] or ""),
                ),
                )
                counts["watchlist_thesis_core"] += 1

        if _sqlite_table_exists(con_sq, "investor_style_memory"):
            for r in con_sq.execute("SELECT key, answer, created_at, updated_at FROM investor_style_memory").fetchall():
                cp.execute(
                """
                INSERT INTO investor_style_memory_core(key, answer, created_at, updated_at)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT(key) DO UPDATE SET answer=EXCLUDED.answer, updated_at=EXCLUDED.updated_at
                """,
                (str(r["key"] or ""), str(r["answer"] or ""), str(r["created_at"] or ""), str(r["updated_at"] or "")),
                )
                counts["investor_style_memory_core"] += 1

        if _sqlite_table_exists(con_sq, "filings"):
            for r in con_sq.execute("SELECT id, ticker, form, date, accession, doc_url, path, downloaded_at FROM filings").fetchall():
                cp.execute(
                """
                INSERT INTO filings_core(id, ticker, form, date, accession, doc_url, path, downloaded_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id) DO UPDATE SET
                  ticker=EXCLUDED.ticker, form=EXCLUDED.form, date=EXCLUDED.date, accession=EXCLUDED.accession,
                  doc_url=EXCLUDED.doc_url, path=EXCLUDED.path, downloaded_at=EXCLUDED.downloaded_at
                """,
                (
                    int(r["id"] or 0),
                    str(r["ticker"] or ""),
                    str(r["form"] or ""),
                    str(r["date"] or ""),
                    str(r["accession"] or ""),
                    str(r["doc_url"] or ""),
                    str(r["path"] or ""),
                    str(r["downloaded_at"] or ""),
                ),
                )
                counts["filings_core"] += 1

        if _sqlite_table_exists(con_sq, "report_facts"):
            for r in con_sq.execute(
                "SELECT id, report_name, report_kind, report_modified, fact_date, ticker, fact_text, importance, source, fact_hash, created_at FROM report_facts"
            ).fetchall():
                cp.execute(
                """
                INSERT INTO report_facts_core
                (id, report_name, report_kind, report_modified, fact_date, ticker, fact_text, importance, source, fact_hash, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id) DO UPDATE SET
                  report_name=EXCLUDED.report_name, report_kind=EXCLUDED.report_kind, report_modified=EXCLUDED.report_modified,
                  fact_date=EXCLUDED.fact_date, ticker=EXCLUDED.ticker, fact_text=EXCLUDED.fact_text, importance=EXCLUDED.importance,
                  source=EXCLUDED.source, fact_hash=EXCLUDED.fact_hash, created_at=EXCLUDED.created_at
                """,
                (
                    int(r["id"] or 0),
                    str(r["report_name"] or ""),
                    str(r["report_kind"] or ""),
                    str(r["report_modified"] or ""),
                    str(r["fact_date"] or ""),
                    str(r["ticker"] or ""),
                    str(r["fact_text"] or ""),
                    int(r["importance"] or 0),
                    str(r["source"] or ""),
                    str(r["fact_hash"] or ""),
                    str(r["created_at"] or ""),
                ),
                )
                counts["report_facts_core"] += 1

        if _sqlite_table_exists(con_sq, "todos"):
            for r in con_sq.execute(
                "SELECT id, task, status, created_at, priority, due_date, ticker, category FROM todos"
            ).fetchall():
                cp.execute(
                    """
                    INSERT INTO todos_core(id, task, status, created_at, priority, due_date, ticker, category)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                      task=EXCLUDED.task, status=EXCLUDED.status, created_at=EXCLUDED.created_at, priority=EXCLUDED.priority,
                      due_date=EXCLUDED.due_date, ticker=EXCLUDED.ticker, category=EXCLUDED.category
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["task"] or ""),
                        str(r["status"] or "open"),
                        str(r["created_at"] or ""),
                        str(r["priority"] or "P2"),
                        str(r["due_date"] or ""),
                        str(r["ticker"] or "").upper(),
                        str(r["category"] or "general"),
                    ),
                )
                counts["todos_core"] += 1

        if _sqlite_table_exists(con_sq, "workspace_journal"):
            for r in con_sq.execute(
                "SELECT id, ticker, action, emotion, note, created_at, COALESCE(status,'approved') AS status, COALESCE(created_by,'human') AS created_by FROM workspace_journal"
            ).fetchall():
                cp.execute(
                    """
                    INSERT INTO workspace_journal_core(id, ticker, action, emotion, note, created_at, status, created_by)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                      ticker=EXCLUDED.ticker, action=EXCLUDED.action, emotion=EXCLUDED.emotion, note=EXCLUDED.note,
                      created_at=EXCLUDED.created_at, status=EXCLUDED.status, created_by=EXCLUDED.created_by
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["ticker"] or "").upper(),
                        str(r["action"] or "Note"),
                        str(r["emotion"] or ""),
                        str(r["note"] or ""),
                        str(r["created_at"] or ""),
                        str(r["status"] or "approved"),
                        str(r["created_by"] or "human"),
                    ),
                )
                counts["workspace_journal_core"] += 1

        if _sqlite_table_exists(con_sq, "investor_notes"):
            for r in con_sq.execute(
                """SELECT id, scope, ticker, sentiment, note, tags, created_at,
                          COALESCE(status,'approved') AS status,
                          COALESCE(created_by,'human') AS created_by,
                          COALESCE(ai_confidence,0) AS ai_confidence,
                          COALESCE(ai_reasoning,'') AS ai_reasoning,
                          COALESCE(trace_id,'') AS trace_id
                     FROM investor_notes"""
            ).fetchall():
                cp.execute(
                    """
                    INSERT INTO investor_notes_core(id, scope, ticker, sentiment, note, tags, created_at, status, created_by, ai_confidence, ai_reasoning, trace_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                      scope=EXCLUDED.scope, ticker=EXCLUDED.ticker, sentiment=EXCLUDED.sentiment, note=EXCLUDED.note, tags=EXCLUDED.tags,
                      created_at=EXCLUDED.created_at, status=EXCLUDED.status, created_by=EXCLUDED.created_by,
                      ai_confidence=EXCLUDED.ai_confidence, ai_reasoning=EXCLUDED.ai_reasoning, trace_id=EXCLUDED.trace_id
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["scope"] or "organizer"),
                        str(r["ticker"] or "").upper(),
                        str(r["sentiment"] or "neutral"),
                        str(r["note"] or ""),
                        str(r["tags"] or "log"),
                        str(r["created_at"] or ""),
                        str(r["status"] or "approved"),
                        str(r["created_by"] or "human"),
                        float(r["ai_confidence"] or 0.0),
                        str(r["ai_reasoning"] or ""),
                        str(r["trace_id"] or ""),
                    ),
                )
                counts["investor_notes_core"] += 1

        if _sqlite_table_exists(con_sq, "company_reminders"):
            for r in con_sq.execute(
                "SELECT id, ticker, remind_at, note, status, created_at FROM company_reminders"
            ).fetchall():
                cp.execute(
                    """
                    INSERT INTO company_reminders_core(id, ticker, remind_at, note, status, created_at)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                      ticker=EXCLUDED.ticker, remind_at=EXCLUDED.remind_at, note=EXCLUDED.note,
                      status=EXCLUDED.status, created_at=EXCLUDED.created_at
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["ticker"] or "").upper(),
                        str(r["remind_at"] or ""),
                        str(r["note"] or ""),
                        str(r["status"] or "open"),
                        str(r["created_at"] or ""),
                    ),
                )
                counts["company_reminders_core"] += 1

        if _sqlite_table_exists(con_sq, "memory_compact"):
            for r in con_sq.execute(
                """SELECT id, memory_key, bucket, value, source, reliability, reuse_count,
                          status, conflict_of, created_at, updated_at, last_used_at
                   FROM memory_compact"""
            ).fetchall():
                cp.execute(
                    """
                    INSERT INTO memory_compact_core
                    (id, memory_key, bucket, value, source, reliability, reuse_count, status, conflict_of, created_at, updated_at, last_used_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                      memory_key=EXCLUDED.memory_key, bucket=EXCLUDED.bucket, value=EXCLUDED.value, source=EXCLUDED.source,
                      reliability=EXCLUDED.reliability, reuse_count=EXCLUDED.reuse_count, status=EXCLUDED.status,
                      conflict_of=EXCLUDED.conflict_of, created_at=EXCLUDED.created_at, updated_at=EXCLUDED.updated_at, last_used_at=EXCLUDED.last_used_at
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["memory_key"] or ""),
                        str(r["bucket"] or "preference"),
                        str(r["value"] or ""),
                        str(r["source"] or "chat_turn"),
                        float(r["reliability"] or 0.7),
                        int(r["reuse_count"] or 0),
                        str(r["status"] or "active"),
                        str(r["conflict_of"] or ""),
                        str(r["created_at"] or ""),
                        str(r["updated_at"] or ""),
                        str(r["last_used_at"] or ""),
                    ),
                )
                counts["memory_compact_core"] += 1

        if _sqlite_table_exists(con_sq, "company_profile_cache"):
            for r in con_sq.execute(
                "SELECT ticker, name, country, industry, sector, updated_at FROM company_profile_cache"
            ).fetchall():
                cp.execute(
                    """
                    INSERT INTO company_profile_cache_core(ticker, name, country, industry, sector, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(ticker) DO UPDATE SET
                      name=EXCLUDED.name, country=EXCLUDED.country, industry=EXCLUDED.industry,
                      sector=EXCLUDED.sector, updated_at=EXCLUDED.updated_at
                    """,
                    (
                        str(r["ticker"] or "").upper(),
                        str(r["name"] or ""),
                        str(r["country"] or ""),
                        str(r["industry"] or ""),
                        str(r["sector"] or ""),
                        str(r["updated_at"] or ""),
                    ),
                )
                counts["company_profile_cache_core"] += 1

        if _sqlite_table_exists(con_sq, "companies"):
            for r in con_sq.execute("SELECT ticker, name, cik, added_date FROM companies").fetchall():
                cp.execute(
                    """
                    INSERT INTO companies_core(ticker, name, cik, added_date)
                    VALUES (%s,%s,%s,%s)
                    ON CONFLICT(ticker) DO UPDATE SET
                      name=EXCLUDED.name, cik=EXCLUDED.cik, added_date=EXCLUDED.added_date
                    """,
                    (
                        str(r["ticker"] or "").upper(),
                        str(r["name"] or ""),
                        str(r["cik"] or ""),
                        str(r["added_date"] or ""),
                    ),
                )
                counts["companies_core"] += 1

        if _sqlite_table_exists(con_sq, "universe_registry"):
            for r in con_sq.execute(
                "SELECT ticker, name, cik, exchange, is_us_listed, is_otc, source, updated_at FROM universe_registry"
            ).fetchall():
                cp.execute(
                    """
                    INSERT INTO universe_registry_core(ticker, name, cik, exchange, is_us_listed, is_otc, source, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(ticker) DO UPDATE SET
                      name=EXCLUDED.name, cik=EXCLUDED.cik, exchange=EXCLUDED.exchange,
                      is_us_listed=EXCLUDED.is_us_listed, is_otc=EXCLUDED.is_otc,
                      source=EXCLUDED.source, updated_at=EXCLUDED.updated_at
                    """,
                    (
                        str(r["ticker"] or "").upper(),
                        str(r["name"] or ""),
                        str(r["cik"] or ""),
                        str(r["exchange"] or ""),
                        bool(int(r["is_us_listed"] or 0)),
                        bool(int(r["is_otc"] or 0)),
                        str(r["source"] or ""),
                        str(r["updated_at"] or ""),
                    ),
                )
                counts["universe_registry_core"] += 1

        if _sqlite_table_exists(con_sq, "company_lists"):
            for r in con_sq.execute("SELECT id, name, created_at, updated_at FROM company_lists").fetchall():
                cp.execute(
                    """
                    INSERT INTO company_lists_core(id, name, created_at, updated_at)
                    VALUES (%s,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                      name=EXCLUDED.name, created_at=EXCLUDED.created_at, updated_at=EXCLUDED.updated_at
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["name"] or ""),
                        str(r["created_at"] or ""),
                        str(r["updated_at"] or ""),
                    ),
                )
                counts["company_lists_core"] += 1

        if _sqlite_table_exists(con_sq, "company_list_items"):
            for r in con_sq.execute("SELECT id, list_id, ticker, added_at, source FROM company_list_items").fetchall():
                cp.execute(
                    """
                    INSERT INTO company_list_items_core(id, list_id, ticker, added_at, source)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                      list_id=EXCLUDED.list_id, ticker=EXCLUDED.ticker, added_at=EXCLUDED.added_at, source=EXCLUDED.source
                    """,
                    (
                        int(r["id"] or 0),
                        int(r["list_id"] or 0),
                        str(r["ticker"] or "").upper(),
                        str(r["added_at"] or ""),
                        str(r["source"] or ""),
                    ),
                )
                counts["company_list_items_core"] += 1

        if _sqlite_table_exists(con_sq, "company_moat_tags"):
            for r in con_sq.execute("SELECT id, ticker, moat_key, updated_at, note FROM company_moat_tags").fetchall():
                cp.execute(
                    """
                    INSERT INTO company_moat_tags_core(id, ticker, moat_key, updated_at, note)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                      ticker=EXCLUDED.ticker, moat_key=EXCLUDED.moat_key, updated_at=EXCLUDED.updated_at, note=EXCLUDED.note
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["ticker"] or "").upper(),
                        str(r["moat_key"] or "").lower(),
                        str(r["updated_at"] or ""),
                        str(r["note"] or ""),
                    ),
                )
                counts["company_moat_tags_core"] += 1

        if _sqlite_table_exists(con_sq, "company_sec_competitors"):
            for r in con_sq.execute(
                "SELECT id, ticker, competitor_ticker, competitor_name, source_form, source_date, source_path, evidence, confidence, status, updated_at FROM company_sec_competitors"
            ).fetchall():
                cp.execute(
                    """
                    INSERT INTO company_sec_competitors_core
                    (id, ticker, competitor_ticker, competitor_name, source_form, source_date, source_path, evidence, confidence, status, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                      ticker=EXCLUDED.ticker, competitor_ticker=EXCLUDED.competitor_ticker, competitor_name=EXCLUDED.competitor_name,
                      source_form=EXCLUDED.source_form, source_date=EXCLUDED.source_date, source_path=EXCLUDED.source_path,
                      evidence=EXCLUDED.evidence, confidence=EXCLUDED.confidence, status=EXCLUDED.status, updated_at=EXCLUDED.updated_at
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["ticker"] or "").upper(),
                        str(r["competitor_ticker"] or "").upper(),
                        str(r["competitor_name"] or ""),
                        str(r["source_form"] or ""),
                        str(r["source_date"] or ""),
                        str(r["source_path"] or ""),
                        str(r["evidence"] or ""),
                        float(r["confidence"] or 0.0),
                        str(r["status"] or "active"),
                        str(r["updated_at"] or ""),
                    ),
                )
                counts["company_sec_competitors_core"] += 1
        if _sqlite_table_exists(con_sq, "blue_chips"):
            for r in con_sq.execute("SELECT ticker, added_at, reason FROM blue_chips").fetchall():
                cp.execute(
                    """
                    INSERT INTO blue_chips_core(ticker, added_at, reason)
                    VALUES (%s,%s,%s)
                    ON CONFLICT(ticker) DO UPDATE SET added_at=EXCLUDED.added_at, reason=EXCLUDED.reason
                    """,
                    (str(r["ticker"] or "").upper(), str(r["added_at"] or ""), str(r["reason"] or "")),
                )
                counts["blue_chips_core"] += 1
        if _sqlite_table_exists(con_sq, "agent_runs"):
            for r in con_sq.execute(
                "SELECT id, run_uid, agent_name, trigger_type, status, started_at, finished_at, duration_ms, trace_id, input_json, output_json, error_text, created_at, updated_at FROM agent_runs"
            ).fetchall():
                cp.execute(
                    """
                    INSERT INTO agent_runs_core
                    (id, run_uid, agent_name, trigger_type, status, started_at, finished_at, duration_ms, trace_id, input_json, output_json, error_text, created_at, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                      run_uid=EXCLUDED.run_uid, agent_name=EXCLUDED.agent_name, trigger_type=EXCLUDED.trigger_type, status=EXCLUDED.status,
                      started_at=EXCLUDED.started_at, finished_at=EXCLUDED.finished_at, duration_ms=EXCLUDED.duration_ms,
                      trace_id=EXCLUDED.trace_id, input_json=EXCLUDED.input_json, output_json=EXCLUDED.output_json,
                      error_text=EXCLUDED.error_text, created_at=EXCLUDED.created_at, updated_at=EXCLUDED.updated_at
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["run_uid"] or ""),
                        str(r["agent_name"] or ""),
                        str(r["trigger_type"] or ""),
                        str(r["status"] or ""),
                        str(r["started_at"] or ""),
                        str(r["finished_at"] or ""),
                        float(r["duration_ms"] or 0.0),
                        str(r["trace_id"] or ""),
                        json.dumps(json.loads(str(r["input_json"] or "{}")), ensure_ascii=True),
                        json.dumps(json.loads(str(r["output_json"] or "{}")), ensure_ascii=True),
                        str(r["error_text"] or ""),
                        str(r["created_at"] or ""),
                        str(r["updated_at"] or ""),
                    ),
                )
                counts["agent_runs_core"] += 1
        if _sqlite_table_exists(con_sq, "reflexion_notes"):
            for r in con_sq.execute(
                "SELECT id, created_at, event_type, query, detail_json, note_text, rule_key, rule_text, confidence FROM reflexion_notes"
            ).fetchall():
                cp.execute(
                    """
                    INSERT INTO reflexion_notes_core(id, created_at, event_type, query, detail_json, note_text, rule_key, rule_text, confidence)
                    VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                      created_at=EXCLUDED.created_at, event_type=EXCLUDED.event_type, query=EXCLUDED.query, detail_json=EXCLUDED.detail_json,
                      note_text=EXCLUDED.note_text, rule_key=EXCLUDED.rule_key, rule_text=EXCLUDED.rule_text, confidence=EXCLUDED.confidence
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["created_at"] or ""),
                        str(r["event_type"] or ""),
                        str(r["query"] or ""),
                        json.dumps(json.loads(str(r["detail_json"] or "{}")), ensure_ascii=True),
                        str(r["note_text"] or ""),
                        str(r["rule_key"] or ""),
                        str(r["rule_text"] or ""),
                        float(r["confidence"] or 0.0),
                    ),
                )
                counts["reflexion_notes_core"] += 1
        if _sqlite_table_exists(con_sq, "failure_patterns"):
            for r in con_sq.execute(
                "SELECT id, pattern_key, event_type, total_count, open_count, resolved_count, first_seen_at, last_seen_at, last_query, last_detail_json, last_reflexion FROM failure_patterns"
            ).fetchall():
                cp.execute(
                    """
                    INSERT INTO failure_patterns_core
                    (id, pattern_key, event_type, total_count, open_count, resolved_count, first_seen_at, last_seen_at, last_query, last_detail_json, last_reflexion)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                    ON CONFLICT(id) DO UPDATE SET
                      pattern_key=EXCLUDED.pattern_key, event_type=EXCLUDED.event_type, total_count=EXCLUDED.total_count,
                      open_count=EXCLUDED.open_count, resolved_count=EXCLUDED.resolved_count, first_seen_at=EXCLUDED.first_seen_at,
                      last_seen_at=EXCLUDED.last_seen_at, last_query=EXCLUDED.last_query, last_detail_json=EXCLUDED.last_detail_json,
                      last_reflexion=EXCLUDED.last_reflexion
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["pattern_key"] or ""),
                        str(r["event_type"] or ""),
                        int(r["total_count"] or 0),
                        int(r["open_count"] or 0),
                        int(r["resolved_count"] or 0),
                        str(r["first_seen_at"] or ""),
                        str(r["last_seen_at"] or ""),
                        str(r["last_query"] or ""),
                        json.dumps(json.loads(str(r["last_detail_json"] or "{}")), ensure_ascii=True),
                        str(r["last_reflexion"] or ""),
                    ),
                )
                counts["failure_patterns_core"] += 1
        if _sqlite_table_exists(con_sq, "reflexion_policy_versions"):
            for r in con_sq.execute(
                "SELECT id, version_tag, created_at, source_event_id, policy_json, is_active, rolled_back_from FROM reflexion_policy_versions"
            ).fetchall():
                cp.execute(
                    """
                    INSERT INTO reflexion_policy_versions_core(id, version_tag, created_at, source_event_id, policy_json, is_active, rolled_back_from)
                    VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                      version_tag=EXCLUDED.version_tag, created_at=EXCLUDED.created_at, source_event_id=EXCLUDED.source_event_id,
                      policy_json=EXCLUDED.policy_json, is_active=EXCLUDED.is_active, rolled_back_from=EXCLUDED.rolled_back_from
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["version_tag"] or ""),
                        str(r["created_at"] or ""),
                        int(r["source_event_id"] or 0),
                        json.dumps(json.loads(str(r["policy_json"] or "{}")), ensure_ascii=True),
                        int(r["is_active"] or 0),
                        str(r["rolled_back_from"] or ""),
                    ),
                )
                counts["reflexion_policy_versions_core"] += 1
        if _sqlite_table_exists(con_sq, "intel24_snapshot"):
            for r in con_sq.execute(
                "SELECT ticker, asof, day_pct, insider_txt, sec_txt, happened, suggestion, event_score, source FROM intel24_snapshot"
            ).fetchall():
                cp.execute(
                    """
                    INSERT INTO intel24_snapshot_core(ticker, asof, day_pct, insider_txt, sec_txt, happened, suggestion, event_score, source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(ticker) DO UPDATE SET
                      asof=EXCLUDED.asof, day_pct=EXCLUDED.day_pct, insider_txt=EXCLUDED.insider_txt, sec_txt=EXCLUDED.sec_txt,
                      happened=EXCLUDED.happened, suggestion=EXCLUDED.suggestion, event_score=EXCLUDED.event_score, source=EXCLUDED.source
                    """,
                    (
                        str(r["ticker"] or "").upper(),
                        str(r["asof"] or ""),
                        float(r["day_pct"] or 0.0),
                        str(r["insider_txt"] or ""),
                        str(r["sec_txt"] or ""),
                        str(r["happened"] or ""),
                        str(r["suggestion"] or ""),
                        int(r["event_score"] or 0),
                        str(r["source"] or ""),
                    ),
                )
                counts["intel24_snapshot_core"] += 1
        if _sqlite_table_exists(con_sq, "intel_feed"):
            for r in con_sq.execute(
                "SELECT id, created_at, ticker, category, title, summary, detail, severity, source, model, unique_key FROM intel_feed"
            ).fetchall():
                cp.execute(
                    """
                    INSERT INTO intel_feed_core
                    (id, created_at, ticker, category, title, summary, detail, severity, source, model, unique_key)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                      created_at=EXCLUDED.created_at, ticker=EXCLUDED.ticker, category=EXCLUDED.category, title=EXCLUDED.title,
                      summary=EXCLUDED.summary, detail=EXCLUDED.detail, severity=EXCLUDED.severity, source=EXCLUDED.source,
                      model=EXCLUDED.model, unique_key=EXCLUDED.unique_key
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["created_at"] or ""),
                        str(r["ticker"] or "").upper(),
                        str(r["category"] or ""),
                        str(r["title"] or ""),
                        str(r["summary"] or ""),
                        str(r["detail"] or ""),
                        int(r["severity"] or 0),
                        str(r["source"] or ""),
                        str(r["model"] or ""),
                        str(r["unique_key"] or ""),
                    ),
                )
                counts["intel_feed_core"] += 1
        if _sqlite_table_exists(con_sq, "changes"):
            for r in con_sq.execute(
                "SELECT id, ticker, filing_id, section_name, change_type, summary, detected_at FROM changes"
            ).fetchall():
                cp.execute(
                    """
                    INSERT INTO changes_core(id, ticker, filing_id, section_name, change_type, summary, detected_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                      ticker=EXCLUDED.ticker, filing_id=EXCLUDED.filing_id, section_name=EXCLUDED.section_name,
                      change_type=EXCLUDED.change_type, summary=EXCLUDED.summary, detected_at=EXCLUDED.detected_at
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["ticker"] or "").upper(),
                        int(r["filing_id"] or 0),
                        str(r["section_name"] or ""),
                        str(r["change_type"] or ""),
                        str(r["summary"] or ""),
                        str(r["detected_at"] or ""),
                    ),
                )
                counts["changes_core"] += 1
        if _sqlite_table_exists(con_sq, "daily_notes"):
            for r in con_sq.execute("SELECT day, content, updated_at, locked, archived_at FROM daily_notes").fetchall():
                cp.execute(
                    """
                    INSERT INTO daily_notes_core(day, content, updated_at, locked, archived_at)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT(day) DO UPDATE SET
                      content=EXCLUDED.content, updated_at=EXCLUDED.updated_at, locked=EXCLUDED.locked, archived_at=EXCLUDED.archived_at
                    """,
                    (
                        str(r["day"] or ""),
                        str(r["content"] or ""),
                        str(r["updated_at"] or ""),
                        int(r["locked"] or 0),
                        str(r["archived_at"] or ""),
                    ),
                )
                counts["daily_notes_core"] += 1
        if _sqlite_table_exists(con_sq, "daily_note_tags"):
            for r in con_sq.execute("SELECT id, day, ticker, created_at FROM daily_note_tags").fetchall():
                cp.execute(
                    """
                    INSERT INTO daily_note_tags_core(id, day, ticker, created_at)
                    VALUES (%s,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                      day=EXCLUDED.day, ticker=EXCLUDED.ticker, created_at=EXCLUDED.created_at
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["day"] or ""),
                        str(r["ticker"] or "").upper(),
                        str(r["created_at"] or ""),
                    ),
                )
                counts["daily_note_tags_core"] += 1
        if _sqlite_table_exists(con_sq, "ai_action_queue"):
            for r in con_sq.execute(
                "SELECT id, created_at, status, tool_name, params_json, reasoning, confidence, trace_id FROM ai_action_queue"
            ).fetchall():
                cp.execute(
                    """
                    INSERT INTO ai_action_queue_core(id, created_at, status, tool_name, params_json, reasoning, confidence, trace_id)
                    VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                      created_at=EXCLUDED.created_at, status=EXCLUDED.status, tool_name=EXCLUDED.tool_name,
                      params_json=EXCLUDED.params_json, reasoning=EXCLUDED.reasoning, confidence=EXCLUDED.confidence, trace_id=EXCLUDED.trace_id
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["created_at"] or ""),
                        str(r["status"] or "pending"),
                        str(r["tool_name"] or ""),
                        json.dumps(json.loads(str(r["params_json"] or "{}")), ensure_ascii=True),
                        str(r["reasoning"] or ""),
                        float(r["confidence"] or 0.0),
                        str(r["trace_id"] or ""),
                    ),
                )
                counts["ai_action_queue_core"] += 1
        if _sqlite_table_exists(con_sq, "ai_action_log"):
            for r in con_sq.execute(
                "SELECT id, created_at, query, intent, status, confidence, payload_json FROM ai_action_log"
            ).fetchall():
                cp.execute(
                    """
                    INSERT INTO ai_action_log_core(id, created_at, query, intent, status, confidence, payload_json)
                    VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT(id) DO UPDATE SET
                      created_at=EXCLUDED.created_at, query=EXCLUDED.query, intent=EXCLUDED.intent,
                      status=EXCLUDED.status, confidence=EXCLUDED.confidence, payload_json=EXCLUDED.payload_json
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["created_at"] or ""),
                        str(r["query"] or ""),
                        str(r["intent"] or ""),
                        str(r["status"] or ""),
                        float(r["confidence"] or 0.0),
                        json.dumps(json.loads(str(r["payload_json"] or "{}")), ensure_ascii=True),
                    ),
                )
                counts["ai_action_log_core"] += 1
        if _sqlite_table_exists(con_sq, "system_events"):
            for r in con_sq.execute(
                "SELECT id, created_at, service, status, latency_ms, message, trace_id FROM system_events"
            ).fetchall():
                cp.execute(
                    """
                    INSERT INTO system_events_core(id, created_at, service, status, latency_ms, message, trace_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                      created_at=EXCLUDED.created_at, service=EXCLUDED.service, status=EXCLUDED.status,
                      latency_ms=EXCLUDED.latency_ms, message=EXCLUDED.message, trace_id=EXCLUDED.trace_id
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["created_at"] or ""),
                        str(r["service"] or ""),
                        str(r["status"] or ""),
                        float(r["latency_ms"] or 0.0),
                        str(r["message"] or ""),
                        str(r["trace_id"] or ""),
                    ),
                )
                counts["system_events_core"] += 1

        if _sqlite_table_exists(con_sq, "decision_log"):
            for r in con_sq.execute("SELECT * FROM decision_log").fetchall():
                cp.execute(
                    """
                    INSERT INTO decision_log_core
                    (id, created_at, timestamp, ticker, action, reason, reasoning, confidence, source, trace_id, quantity, price)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                      created_at=EXCLUDED.created_at,timestamp=EXCLUDED.timestamp,ticker=EXCLUDED.ticker,action=EXCLUDED.action,
                      reason=EXCLUDED.reason,reasoning=EXCLUDED.reasoning,confidence=EXCLUDED.confidence,source=EXCLUDED.source,
                      trace_id=EXCLUDED.trace_id,quantity=EXCLUDED.quantity,price=EXCLUDED.price
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["created_at"] or ""),
                        str(r["timestamp"] or ""),
                        str(r["ticker"] or ""),
                        str(r["action"] or ""),
                        str(r["reason"] or ""),
                        str(r["reasoning"] or ""),
                        float(r["confidence"] or 0.0),
                        str(r["source"] or ""),
                        str(r["trace_id"] or ""),
                        float(r["quantity"] or 0.0),
                        float(r["price"] or 0.0),
                    ),
                )
                counts["decision_log_core"] += 1

        if _sqlite_table_exists(con_sq, "investor_question_overrides"):
            for r in con_sq.execute("SELECT * FROM investor_question_overrides").fetchall():
                cp.execute(
                    """
                    INSERT INTO investor_question_overrides_core (key, question, created_at, updated_at)
                    VALUES (%s,%s,%s,%s)
                    ON CONFLICT(key) DO UPDATE SET
                      question=EXCLUDED.question,updated_at=EXCLUDED.updated_at
                    """,
                    (
                        str(r["key"] or ""),
                        str(r["question"] or ""),
                        str(r["created_at"] or ""),
                        str(r["updated_at"] or ""),
                    ),
                )
                counts["investor_question_overrides_core"] += 1

        if _sqlite_table_exists(con_sq, "portfolio_interview_queue"):
            for r in con_sq.execute("SELECT * FROM portfolio_interview_queue").fetchall():
                cp.execute(
                    """
                    INSERT INTO portfolio_interview_queue_core
                    (id, ticker, status, step, last_question, session_id, completed_at, created_at, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(ticker) DO UPDATE SET
                      status=EXCLUDED.status,step=EXCLUDED.step,last_question=EXCLUDED.last_question,session_id=EXCLUDED.session_id,
                      completed_at=EXCLUDED.completed_at,updated_at=EXCLUDED.updated_at
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["ticker"] or ""),
                        str(r["status"] or "pending"),
                        int(r["step"] or 0),
                        str(r["last_question"] or ""),
                        str(r["session_id"] or ""),
                        str(r["completed_at"] or ""),
                        str(r["created_at"] or ""),
                        str(r["updated_at"] or ""),
                    ),
                )
                counts["portfolio_interview_queue_core"] += 1

        if _sqlite_table_exists(con_sq, "report_fact_ingest_state"):
            for r in con_sq.execute("SELECT * FROM report_fact_ingest_state").fetchall():
                cp.execute(
                    """
                    INSERT INTO report_fact_ingest_state_core (state_key, state_value)
                    VALUES (%s,%s)
                    ON CONFLICT(state_key) DO UPDATE SET state_value=EXCLUDED.state_value
                    """,
                    (
                        str(r["state_key"] or ""),
                        str(r["state_value"] or ""),
                    ),
                )
                counts["report_fact_ingest_state_core"] += 1

        if _sqlite_table_exists(con_sq, "morning_briefs"):
            for r in con_sq.execute("SELECT * FROM morning_briefs").fetchall():
                cp.execute(
                    """
                    INSERT INTO morning_briefs_core (id, brief_day, created_at, source, brief_json)
                    VALUES (%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT(brief_day) DO UPDATE SET
                      created_at=EXCLUDED.created_at,source=EXCLUDED.source,brief_json=EXCLUDED.brief_json
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["brief_day"] or ""),
                        str(r["created_at"] or ""),
                        str(r["source"] or ""),
                        json.dumps(json.loads(str(r["brief_json"] or "{}")), ensure_ascii=True),
                    ),
                )
                counts["morning_briefs_core"] += 1

        if _sqlite_table_exists(con_sq, "learning_events"):
            for r in con_sq.execute("SELECT * FROM learning_events").fetchall():
                cp.execute(
                    """
                    INSERT INTO learning_events_core (id, created_at, event_type, source, payload_json)
                    VALUES (%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT(id) DO UPDATE SET
                      created_at=EXCLUDED.created_at,event_type=EXCLUDED.event_type,source=EXCLUDED.source,payload_json=EXCLUDED.payload_json
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["created_at"] or ""),
                        str(r["event_type"] or ""),
                        str(r["source"] or ""),
                        json.dumps(json.loads(str(r["payload_json"] or "{}")), ensure_ascii=True),
                    ),
                )
                counts["learning_events_core"] += 1

        if _sqlite_table_exists(con_sq, "rule_candidates"):
            for r in con_sq.execute("SELECT * FROM rule_candidates").fetchall():
                cp.execute(
                    """
                    INSERT INTO rule_candidates_core
                    (id, rule_key, rule_text, source_pattern, support_count, accept_count, reject_count, confidence, status, created_at, updated_at, last_evaluated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(rule_key) DO UPDATE SET
                      rule_text=EXCLUDED.rule_text,source_pattern=EXCLUDED.source_pattern,support_count=EXCLUDED.support_count,
                      accept_count=EXCLUDED.accept_count,reject_count=EXCLUDED.reject_count,confidence=EXCLUDED.confidence,
                      status=EXCLUDED.status,updated_at=EXCLUDED.updated_at,last_evaluated_at=EXCLUDED.last_evaluated_at
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["rule_key"] or ""),
                        str(r["rule_text"] or ""),
                        str(r["source_pattern"] or ""),
                        int(r["support_count"] or 0),
                        int(r["accept_count"] or 0),
                        int(r["reject_count"] or 0),
                        float(r["confidence"] or 0.0),
                        str(r["status"] or "candidate"),
                        str(r["created_at"] or ""),
                        str(r["updated_at"] or ""),
                        str(r["last_evaluated_at"] or ""),
                    ),
                )
                counts["rule_candidates_core"] += 1

        if _sqlite_table_exists(con_sq, "active_rules"):
            for r in con_sq.execute("SELECT * FROM active_rules").fetchall():
                cp.execute(
                    """
                    INSERT INTO active_rules_core
                    (id, rule_key, rule_text, confidence, source_candidate_id, reuse_count, status, created_at, updated_at, last_used_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(rule_key) DO UPDATE SET
                      rule_text=EXCLUDED.rule_text,confidence=EXCLUDED.confidence,source_candidate_id=EXCLUDED.source_candidate_id,
                      reuse_count=EXCLUDED.reuse_count,status=EXCLUDED.status,updated_at=EXCLUDED.updated_at,last_used_at=EXCLUDED.last_used_at
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["rule_key"] or ""),
                        str(r["rule_text"] or ""),
                        float(r["confidence"] or 0.0),
                        int(r["source_candidate_id"] or 0),
                        int(r["reuse_count"] or 0),
                        str(r["status"] or "active"),
                        str(r["created_at"] or ""),
                        str(r["updated_at"] or ""),
                        str(r["last_used_at"] or ""),
                    ),
                )
                counts["active_rules_core"] += 1

        if _sqlite_table_exists(con_sq, "ai_tool_log"):
            for r in con_sq.execute("SELECT * FROM ai_tool_log").fetchall():
                cp.execute(
                    """
                    INSERT INTO ai_tool_log_core
                    (id, created_at, query, tool_name, args_json, status, latency_ms, error, trace_id, model_name, capability)
                    VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                      created_at=EXCLUDED.created_at,query=EXCLUDED.query,tool_name=EXCLUDED.tool_name,args_json=EXCLUDED.args_json,
                      status=EXCLUDED.status,latency_ms=EXCLUDED.latency_ms,error=EXCLUDED.error,trace_id=EXCLUDED.trace_id,
                      model_name=EXCLUDED.model_name,capability=EXCLUDED.capability
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["created_at"] or ""),
                        str(r["query"] or ""),
                        str(r["tool_name"] or ""),
                        json.dumps(json.loads(str(r["args_json"] or "{}")), ensure_ascii=True),
                        str(r["status"] or ""),
                        float(r["latency_ms"] or 0.0),
                        str(r["error"] or ""),
                        str(r["trace_id"] or ""),
                        str(r["model_name"] or ""),
                        str(r["capability"] or ""),
                    ),
                )
                counts["ai_tool_log_core"] += 1

        if _sqlite_table_exists(con_sq, "ai_quality_log"):
            for r in con_sq.execute("SELECT * FROM ai_quality_log").fetchall():
                cp.execute(
                    """
                    INSERT INTO ai_quality_log_core (id, created_at, event_type, query, detail_json)
                    VALUES (%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT(id) DO UPDATE SET
                      created_at=EXCLUDED.created_at,event_type=EXCLUDED.event_type,query=EXCLUDED.query,detail_json=EXCLUDED.detail_json
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["created_at"] or ""),
                        str(r["event_type"] or ""),
                        str(r["query"] or ""),
                        json.dumps(json.loads(str(r["detail_json"] or "{}")), ensure_ascii=True),
                    ),
                )
                counts["ai_quality_log_core"] += 1

        if _sqlite_table_exists(con_sq, "risk_veto_decisions"):
            for r in con_sq.execute("SELECT * FROM risk_veto_decisions").fetchall():
                cp.execute(
                    """
                    INSERT INTO risk_veto_decisions_core
                    (id, created_at, trace_id, action, query, verdict, confidence, reason, metrics_json, detail_json)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
                    ON CONFLICT(id) DO UPDATE SET
                      created_at=EXCLUDED.created_at,trace_id=EXCLUDED.trace_id,action=EXCLUDED.action,query=EXCLUDED.query,
                      verdict=EXCLUDED.verdict,confidence=EXCLUDED.confidence,reason=EXCLUDED.reason,metrics_json=EXCLUDED.metrics_json,
                      detail_json=EXCLUDED.detail_json
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["created_at"] or ""),
                        str(r["trace_id"] or ""),
                        str(r["action"] or ""),
                        str(r["query"] or ""),
                        str(r["verdict"] or ""),
                        float(r["confidence"] or 0.0),
                        str(r["reason"] or ""),
                        json.dumps(json.loads(str(r["metrics_json"] or "{}")), ensure_ascii=True),
                        json.dumps(json.loads(str(r["detail_json"] or "{}")), ensure_ascii=True),
                    ),
                )
                counts["risk_veto_decisions_core"] += 1

        if _sqlite_table_exists(con_sq, "risk_veto_config"):
            for r in con_sq.execute("SELECT * FROM risk_veto_config").fetchall():
                cp.execute(
                    """
                    INSERT INTO risk_veto_config_core (id, config_json, updated_at)
                    VALUES (%s,%s::jsonb,%s)
                    ON CONFLICT(id) DO UPDATE SET
                      config_json=EXCLUDED.config_json,updated_at=EXCLUDED.updated_at
                    """,
                    (
                        int(r["id"] or 1),
                        json.dumps(json.loads(str(r["config_json"] or "{}")), ensure_ascii=True),
                        str(r["updated_at"] or ""),
                    ),
                )
                counts["risk_veto_config_core"] += 1

        if _sqlite_table_exists(con_sq, "daily_operator_state"):
            for r in con_sq.execute("SELECT * FROM daily_operator_state").fetchall():
                cp.execute(
                    """
                    INSERT INTO daily_operator_state_core (state_key, state_value, updated_at)
                    VALUES (%s,%s,%s)
                    ON CONFLICT(state_key) DO UPDATE SET
                      state_value=EXCLUDED.state_value,updated_at=EXCLUDED.updated_at
                    """,
                    (
                        str(r["state_key"] or ""),
                        str(r["state_value"] or ""),
                        str(r["updated_at"] or ""),
                    ),
                )
                counts["daily_operator_state_core"] += 1

        if _sqlite_table_exists(con_sq, "daily_operator_gap_prompts"):
            for r in con_sq.execute("SELECT * FROM daily_operator_gap_prompts").fetchall():
                cp.execute(
                    """
                    INSERT INTO daily_operator_gap_prompts_core
                    (id, created_at, updated_at, prompt_id, user_name, prompt_text, context_json, context_fingerprint, status, answered_at, answer_text, resolved_json)
                    VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT(prompt_id) DO UPDATE SET
                      updated_at=EXCLUDED.updated_at,user_name=EXCLUDED.user_name,prompt_text=EXCLUDED.prompt_text,
                      context_json=EXCLUDED.context_json,context_fingerprint=EXCLUDED.context_fingerprint,status=EXCLUDED.status,
                      answered_at=EXCLUDED.answered_at,answer_text=EXCLUDED.answer_text,resolved_json=EXCLUDED.resolved_json
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["created_at"] or ""),
                        str(r["updated_at"] or ""),
                        str(r["prompt_id"] or ""),
                        str(r["user_name"] or ""),
                        str(r["prompt_text"] or ""),
                        json.dumps(json.loads(str(r["context_json"] or "{}")), ensure_ascii=True),
                        str(r["context_fingerprint"] or ""),
                        str(r["status"] or "open"),
                        str(r["answered_at"] or ""),
                        str(r["answer_text"] or ""),
                        json.dumps(json.loads(str(r["resolved_json"] or "{}")), ensure_ascii=True),
                    ),
                )
                counts["daily_operator_gap_prompts_core"] += 1

        if _sqlite_table_exists(con_sq, "user_preferences"):
            for r in con_sq.execute("SELECT * FROM user_preferences").fetchall():
                cp.execute(
                    """
                    INSERT INTO user_preferences_core
                    (id, pref_key, pref_value, source, preference_key, preference_value, context_reason, created_at, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(pref_key) DO UPDATE SET
                      pref_value=EXCLUDED.pref_value,source=EXCLUDED.source,preference_key=EXCLUDED.preference_key,
                      preference_value=EXCLUDED.preference_value,context_reason=EXCLUDED.context_reason,updated_at=EXCLUDED.updated_at
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["pref_key"] or ""),
                        str(r["pref_value"] or ""),
                        str(r["source"] or "chat"),
                        str(r["preference_key"] or ""),
                        str(r["preference_value"] or ""),
                        str(r["context_reason"] or ""),
                        str(r["created_at"] or ""),
                        str(r["updated_at"] or ""),
                    ),
                )
                counts["user_preferences_core"] += 1

        if _sqlite_table_exists(con_sq, "ai_meta_suggestions"):
            for r in con_sq.execute("SELECT * FROM ai_meta_suggestions").fetchall():
                cp.execute(
                    """
                    INSERT INTO ai_meta_suggestions_core
                    (id, created_at, category, title, summary, detail_json, priority, source, status)
                    VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET
                      created_at=EXCLUDED.created_at,category=EXCLUDED.category,title=EXCLUDED.title,
                      summary=EXCLUDED.summary,detail_json=EXCLUDED.detail_json,priority=EXCLUDED.priority,
                      source=EXCLUDED.source,status=EXCLUDED.status
                    """,
                    (
                        int(r["id"] or 0),
                        str(r["created_at"] or ""),
                        str(r["category"] or ""),
                        str(r["title"] or ""),
                        str(r["summary"] or ""),
                        json.dumps(json.loads(str(r["detail_json"] or "{}")), ensure_ascii=True),
                        float(r["priority"] or 0.0),
                        str(r["source"] or "heuristic"),
                        str(r["status"] or "open"),
                    ),
                )
                counts["ai_meta_suggestions_core"] += 1

        con_pg.commit()
        return {"ok": True, "synced": counts}
    except Exception as exc:
        try:
            con_pg.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc), "synced": counts}
    finally:
        con_sq.close()
        con_pg.close()


def verify_core_counts() -> dict[str, Any]:
    con_pg = pg_connect()
    con_sq = connect_sqlite(str(CORE_DB_PATH), row_factory=True)
    if con_pg is None:
        con_sq.close()
        return {"ok": False, "error": "pg_not_available"}
    try:
        cp = con_pg.cursor()
        out: dict[str, Any] = {"ok": True, "counts": {}}
        pairs = [
            ("action_proposals", "action_proposals_core"),
            ("portfolio_transactions", "portfolio_transactions_core"),
            ("watchlist_thesis", "watchlist_thesis_core"),
            ("investor_style_memory", "investor_style_memory_core"),
            ("filings", "filings_core"),
            ("report_facts", "report_facts_core"),
            ("todos", "todos_core"),
            ("workspace_journal", "workspace_journal_core"),
            ("investor_notes", "investor_notes_core"),
            ("company_reminders", "company_reminders_core"),
            ("memory_compact", "memory_compact_core"),
            ("company_profile_cache", "company_profile_cache_core"),
            ("companies", "companies_core"),
            ("universe_registry", "universe_registry_core"),
            ("company_lists", "company_lists_core"),
            ("company_list_items", "company_list_items_core"),
            ("company_moat_tags", "company_moat_tags_core"),
            ("company_sec_competitors", "company_sec_competitors_core"),
            ("blue_chips", "blue_chips_core"),
            ("agent_runs", "agent_runs_core"),
            ("reflexion_notes", "reflexion_notes_core"),
            ("failure_patterns", "failure_patterns_core"),
            ("reflexion_policy_versions", "reflexion_policy_versions_core"),
            ("intel24_snapshot", "intel24_snapshot_core"),
            ("intel_feed", "intel_feed_core"),
            ("changes", "changes_core"),
            ("daily_notes", "daily_notes_core"),
            ("daily_note_tags", "daily_note_tags_core"),
            ("ai_action_queue", "ai_action_queue_core"),
            ("ai_action_log", "ai_action_log_core"),
            ("system_events", "system_events_core"),
            ("decision_log", "decision_log_core"),
            ("investor_question_overrides", "investor_question_overrides_core"),
            ("portfolio_interview_queue", "portfolio_interview_queue_core"),
            ("report_fact_ingest_state", "report_fact_ingest_state_core"),
            ("morning_briefs", "morning_briefs_core"),
            ("learning_events", "learning_events_core"),
            ("rule_candidates", "rule_candidates_core"),
            ("active_rules", "active_rules_core"),
            ("ai_tool_log", "ai_tool_log_core"),
            ("ai_quality_log", "ai_quality_log_core"),
            ("risk_veto_decisions", "risk_veto_decisions_core"),
            ("risk_veto_config", "risk_veto_config_core"),
            ("daily_operator_state", "daily_operator_state_core"),
            ("daily_operator_gap_prompts", "daily_operator_gap_prompts_core"),
            ("user_preferences", "user_preferences_core"),
            ("ai_meta_suggestions", "ai_meta_suggestions_core"),
        ]
        for sq, pg in pairs:
            if _sqlite_table_exists(con_sq, sq):
                sq_n = int((con_sq.execute(f"SELECT COUNT(*) AS c FROM {sq}").fetchone() or {"c": 0})["c"] or 0)
            else:
                sq_n = 0
            cp.execute(f"SELECT COUNT(*) FROM {pg}")
            pg_n = int((cp.fetchone() or [0])[0] or 0)
            out["counts"][sq] = {"sqlite": sq_n, "postgres": pg_n, "match": bool(sq_n == pg_n)}
        return out
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        con_sq.close()
        con_pg.close()


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
    v = verify_core_counts()
    if not bool(v.get("ok")):
        return {"ok": False, "backend": "postgres", "guard_enforced": True, "switched": False, "reason": "verify_failed", "verify": v}
    counts = dict(v.get("counts") or {})
    all_match = True
    for _, meta in counts.items():
        if not bool((meta or {}).get("match")):
            all_match = False
            break
    if all_match:
        return {"ok": True, "backend": "postgres", "guard_enforced": True, "switched": False, "reason": "verified_match", "verify": v}
    return {"ok": False, "backend": "postgres", "guard_enforced": True, "switched": False, "reason": "count_mismatch", "verify": v}


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
        rows = cur.fetchall() or []
        out: list[dict[str, Any]] = []
        for r in rows:
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
        for r in cur.fetchall() or []:
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
        cols = {str(x[0] or "").strip().lower() for x in (cur.fetchall() or [])}
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
        cols = {str(x[0] or "").strip().lower() for x in (cur.fetchall() or [])}
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
        for r in cur.fetchall() or []:
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
        for r in cur.fetchall() or []:
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
        for r in cur.fetchall() or []:
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


def list_watchlist_thesis_pg(limit: int = 300) -> list[dict[str, str]]:
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            """SELECT ticker, thesis_summary, time_horizon, invalidation_criteria, strategy_tag, created_at, updated_at
               FROM watchlist_thesis_core
               ORDER BY updated_at DESC
               LIMIT %s""",
            (max(1, min(1000, int(limit or 300))),),
        )
        out: list[dict[str, str]] = []
        for r in cur.fetchall() or []:
            out.append(
                {
                    "ticker": str(r[0] or ""),
                    "thesis_summary": str(r[1] or ""),
                    "time_horizon": str(r[2] or ""),
                    "invalidation_criteria": str(r[3] or ""),
                    "strategy_tag": str(r[4] or ""),
                    "created_at": str(r[5] or ""),
                    "updated_at": str(r[6] or ""),
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
) -> bool:
    con = pg_connect()
    if con is None:
        return False
    now = dt.datetime.now().isoformat()
    t = str(ticker or "").strip().upper()
    if not t:
        con.close()
        return False
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO watchlist_thesis_core
            (ticker, thesis, thesis_summary, pick_method, triggers, invalidation, conviction_rating, time_horizon, invalidation_criteria, strategy_tag, pattern_learnable, status, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
        for r in cur.fetchall() or []:
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
        for r in cur.fetchall() or []:
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
        rows = cur.fetchall() or []
        return [
            {
                "report_name": str(r[0] or ""),
                "report_kind": str(r[1] or ""),
                "fact_date": str(r[2] or ""),
                "ticker": str(r[3] or "").strip().upper(),
                "fact_text": str(r[4] or ""),
                "importance": int(r[5] or 0),
                "created_at": str(r[6] or ""),
            }
            for r in rows
        ]
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
        cols = {str(r[0] or "").strip().lower() for r in (cur.fetchall() or [])}
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
        for r in cur.fetchall() or []:
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
        col_rows = cur.fetchall() or []
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
        cols = {str(r[0] or "").strip().lower() for r in (cur.fetchall() or [])}
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
        for r in cur.fetchall() or []:
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
        cols = {str(r[0] or "").strip().lower() for r in (cur.fetchall() or [])}
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
        tags = [str(r[0] or "").strip().upper() for r in (cur.fetchall() or []) if str(r[0] or "").strip()]
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
        for r in cur.fetchall() or []:
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
        for r in cur.fetchall() or []:
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
        for r in cur.fetchall() or []:
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
        for r in cur.fetchall() or []:
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
        for r in cur.fetchall() or []:
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
        for r in cur.fetchall() or []:
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
