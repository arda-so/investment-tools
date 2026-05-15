from __future__ import annotations

from typing import Any


def ensure_postgres_core_base_tables(cur: Any) -> None:
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


def ensure_postgres_core_thesis_tables(cur: Any) -> None:
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
    try:
        cur.execute("ALTER TABLE watchlist_thesis_core ADD COLUMN IF NOT EXISTS target_price DOUBLE PRECISION")
        cur.execute("ALTER TABLE watchlist_thesis_core ADD COLUMN IF NOT EXISTS key_questions JSONB NOT NULL DEFAULT '[]'::jsonb")
        cur.execute("ALTER TABLE watchlist_thesis_core ADD COLUMN IF NOT EXISTS phase TEXT NOT NULL DEFAULT 'watching'")
    except Exception:
        pass
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS quarterly_reviews_core (
            id BIGSERIAL PRIMARY KEY,
            quarter TEXT NOT NULL,
            year INTEGER NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            portfolio_return DOUBLE PRECISION,
            benchmark_return DOUBLE PRECISION,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(quarter, year)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS idea_list_core (
            id BIGSERIAL PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            ticker TEXT,
            notes TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'manual',
            status TEXT NOT NULL DEFAULT 'open',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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


def ensure_postgres_core_filing_tables(cur: Any) -> None:
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
    cur.execute(
        """
        ALTER TABLE filings_core ADD COLUMN IF NOT EXISTS content TEXT NOT NULL DEFAULT ''
        """
    )
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


def ensure_postgres_core_annotation_tables(cur: Any) -> None:
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


def ensure_postgres_core_registry_tables(cur: Any) -> None:
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


def ensure_postgres_core_monitoring_tables(cur: Any) -> None:
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


def ensure_postgres_core_memory_tables(cur: Any) -> None:
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
            market_cap BIGINT,
            updated_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cpc_core_name ON company_profile_cache_core(name)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cpc_core_industry ON company_profile_cache_core(industry)")
    try:
        cur.execute("ALTER TABLE company_profile_cache_core ADD COLUMN IF NOT EXISTS market_cap BIGINT")
    except Exception:
        pass
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


def ensure_postgres_core_intelligence_tables(cur: Any) -> None:
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


def ensure_postgres_core_operator_tables(cur: Any) -> None:
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
    cur.execute("CREATE SEQUENCE IF NOT EXISTS decision_log_core_id_seq")
    cur.execute(
        "SELECT setval('decision_log_core_id_seq', COALESCE((SELECT MAX(id) FROM decision_log_core), 0) + 1, false)"
    )
    cur.execute("ALTER TABLE decision_log_core ALTER COLUMN id SET DEFAULT nextval('decision_log_core_id_seq')")
