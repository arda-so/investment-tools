from __future__ import annotations

import logging
import math
import os
import threading

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.core.config import ROOT
from app.routers.company_file import router as company_file_router
from app.routers.ai import router as ai_router
from app.routers.dashboard import router as dashboard_router, dashboard_report_panels
from app.routers.observability import router as observability_router
from app.routers.organizer import router as organizer_router
from app.routers.reports import router as reports_router
from app.routers.supply_chain import router as supply_chain_router
from app.routers.workspace_os import router as workspace_os_router
from app.services.ai_orchestrator import ensure_ai_schema
from app.services.ai_job_queue_service import ensure_ai_job_queue_schema, queue_backend
from app.services.app_knowledge_service import ensure_app_knowledge_map
from app.services.chat_memory_service import ensure_chat_memory_schema
from app.services.observability_service import ensure_observability_schema
from app.services.organizer_service import ensure_schema
from app.services.portfolio_memory_service import ensure_portfolio_memory_schema
from app.services.postgres_core_service import ensure_postgres_core_schema, guard_core_backend_cutover, enforce_strict_postgres_ready, strict_postgres_mode, core_backend, pg_connect
from app.services.sec_ingest_pipeline_service import ensure_sec_ingest_schema
from app.services.ai_insight_service import ensure_ai_insight_schema
from app.services.supply_chain_service import ensure_supply_chain_schema
from app.services.dashboard_service import home_snapshot


def _abbr_money(value: object) -> str:
    try:
        if value is None:
            return "-"
        n = float(value)
    except Exception:
        return "-"
    if math.isnan(n) or math.isinf(n):
        return "-"
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1_000_000_000_000:
        return f"{sign}${n/1_000_000_000_000:.1f}T"
    if n >= 1_000_000_000:
        return f"{sign}${n/1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{sign}${n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{sign}${n/1_000:.1f}K"
    return f"{sign}${n:,.0f}"


LOGGER = logging.getLogger(__name__)


def _log_boot_warning(stage: str, exc: Exception) -> None:
    LOGGER.warning("[startup] %s failed: %s", stage, str(exc), exc_info=True)


def create_app() -> FastAPI:
    if core_backend() == "postgres" and strict_postgres_mode():
        if not str(os.getenv("POSTGRES_DSN", "")).strip():
            raise RuntimeError("missing_POSTGRES_DSN_in_strict_postgres_mode")
    app = FastAPI(title="Investor OS v2", version="2.0.0")
    app.state.templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))
    app.state.templates.env.filters["abbr_money"] = _abbr_money

    @app.middleware("http")
    async def no_store_middleware(request: Request, call_next):
        response: Response = await call_next(request)
        # Prevent stale dashboard/organizer/company pages from browser/proxy cache.
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    @app.get("/health/live")
    def health_live():
        return {"ok": True, "service": "investor_app", "version": "2.0.0"}

    @app.get("/health/ready")
    def health_ready():
        backend = core_backend()
        db_ok = False
        db_error = ""
        if backend == "postgres":
            con = pg_connect()
            if con is None:
                db_error = "postgres_unavailable"
            else:
                try:
                    cur = con.cursor()
                    cur.execute("SELECT 1")
                    db_ok = bool(cur.fetchone())
                except Exception as exc:
                    db_error = str(exc)
                finally:
                    con.close()
        else:
            # In strict mode we expect postgres only; sqlite readiness is non-target.
            db_ok = not strict_postgres_mode()
            if not db_ok:
                db_error = "non_postgres_backend_in_strict_mode"
        return {
            "ok": bool(db_ok),
            "service": "investor_app",
            "core_db_backend": backend,
            "queue_backend": queue_backend(),
            "db_ok": db_ok,
            "db_error": db_error,
        }

    # Run schema ensures with per-call error isolation so a slow/failed
    # Postgres connection doesn't prevent uvicorn from starting and passing
    # the TCP startup probe.
    for _fn in (
        ensure_schema,
        ensure_ai_schema,
        ensure_app_knowledge_map,
        ensure_chat_memory_schema,
        ensure_observability_schema,
        ensure_portfolio_memory_schema,
        ensure_ai_job_queue_schema,
        ensure_postgres_core_schema,
    ):
        try:
            _fn()
        except Exception as exc:
            _log_boot_warning(getattr(_fn, "__name__", "startup_fn"), exc)
    # Enforce Postgres as the only runtime backend.
    if core_backend() != "postgres":
        raise RuntimeError("postgres_required: set CORE_DB_BACKEND=postgres")
    if not strict_postgres_mode():
        try:
            guard_core_backend_cutover()
        except Exception as exc:
            _log_boot_warning("guard_core_backend_cutover", exc)
    try:
        enforce_strict_postgres_ready()
    except Exception as exc:
        _log_boot_warning("enforce_strict_postgres_ready", exc)
    for _fn2 in (ensure_sec_ingest_schema, ensure_ai_insight_schema, ensure_supply_chain_schema):
        try:
            _fn2()
        except Exception as exc:
            _log_boot_warning(getattr(_fn2, "__name__", "startup_fn"), exc)
    # Phase 3.1: event infrastructure
    try:
        from app.services.events_service import ensure_events_schema
        ensure_events_schema()
    except Exception as exc:
        _log_boot_warning("ensure_events_schema", exc)
    try:
        from app.services.sec_edgar_poller_service import ensure_poller_schema
        ensure_poller_schema()
    except Exception as exc:
        _log_boot_warning("ensure_poller_schema", exc)
    try:
        from app.services.earnings_transcript_service import ensure_earnings_analysis_schema
        ensure_earnings_analysis_schema()
    except Exception as exc:
        _log_boot_warning("ensure_earnings_analysis_schema", exc)
    # Phase 3.5: multi-agent debate schema
    try:
        from app.services.debate_service import ensure_debate_schema
        ensure_debate_schema()
    except Exception as exc:
        _log_boot_warning("ensure_debate_schema", exc)
    # DB-driven AI config (thresholds, peer maps, macro tickers)
    try:
        from app.services.ai_config_service import ensure_ai_config_schema
        ensure_ai_config_schema()
    except Exception as exc:
        _log_boot_warning("ensure_ai_config_schema", exc)
    # Unified Work OS schema
    try:
        from app.services.workspace_os_service import ensure_workspace_os_schema
        ensure_workspace_os_schema()
    except Exception as exc:
        _log_boot_warning("ensure_workspace_os_schema", exc)
    # Playbook schema
    try:
        from app.services.playbook_service import ensure_playbook_schema
        ensure_playbook_schema()
    except Exception as exc:
        _log_boot_warning("ensure_playbook_schema", exc)
    # Research thread schema
    try:
        from app.services.research_thread_service import ensure_research_thread_schema
        ensure_research_thread_schema()
    except Exception as exc:
        _log_boot_warning("ensure_research_thread_schema", exc)
    # Work graph schema
    try:
        from app.services.work_graph_service import ensure_work_graph_schema
        ensure_work_graph_schema()
    except Exception as exc:
        _log_boot_warning("ensure_work_graph_schema", exc)
    # Thinking Lab schema
    try:
        from app.services.thinking_service import ensure_thinking_schema
        ensure_thinking_schema()
    except Exception as exc:
        _log_boot_warning("ensure_thinking_schema", exc)
    try:
        from app.services.autonomic_service import ensure_autonomic_schema
        ensure_autonomic_schema()
    except Exception as exc:
        _log_boot_warning("ensure_autonomic_schema", exc)
    # Financial Lab schema
    try:
        from app.services.postgres_core_service import ensure_lab_schema
        ensure_lab_schema()
    except Exception as exc:
        _log_boot_warning("ensure_lab_schema", exc)
    # Cognitive Engine schema (Phase 4)
    try:
        from app.services.cognitive_engine_service import ensure_cognitive_schema
        ensure_cognitive_schema()
    except Exception as exc:
        _log_boot_warning("ensure_cognitive_schema", exc)
    # Research notes schema
    try:
        from app.services.research_notes_service import ensure_research_notes_schema
        ensure_research_notes_schema()
    except Exception as exc:
        _log_boot_warning("ensure_research_notes_schema", exc)
    # Start web search background threads (macro snapshot + RSS)
    try:
        from app.services.web_search_service import start_web_search_background
        start_web_search_background()
    except Exception as exc:
        _log_boot_warning("start_web_search_background", exc)

    refresh_stop = threading.Event()
    app.state.market_refresh_stop = refresh_stop

    def _market_snapshot_loop() -> None:
        try:
            initial_delay = max(0, int(str(os.getenv("MARKET_SNAPSHOT_INITIAL_DELAY_SEC", "20")).strip() or "20"))
        except Exception:
            initial_delay = 20
        try:
            interval = max(30, int(str(os.getenv("MARKET_SNAPSHOT_REFRESH_SEC", "180")).strip() or "180"))
        except Exception:
            interval = 180
        if initial_delay:
            refresh_stop.wait(initial_delay)
        while not refresh_stop.is_set():
            try:
                # Refresh news snapshots via existing home pipeline.
                _ = home_snapshot()
                # Refresh earnings snapshot via existing dashboard report pipeline.
                _ = dashboard_report_panels()
            except Exception as exc:
                LOGGER.warning("[market-snapshot-refresh] cycle failed: %s", str(exc), exc_info=True)
            refresh_stop.wait(interval)

    # Phase 3.2: measure pending proposal outcomes (runs every 6 hours)
    _OUTCOME_INTERVAL_SEC = 6 * 60 * 60

    def _outcome_measurement_loop() -> None:
        # Stagger startup by 60s so server is fully ready before first run.
        refresh_stop.wait(60)
        while not refresh_stop.is_set():
            try:
                from app.services.proactive_ai_service import measure_proposal_outcomes
                measure_proposal_outcomes()
            except Exception as exc:
                LOGGER.warning("[outcome-measurement] cycle failed: %s", str(exc), exc_info=True)
            refresh_stop.wait(_OUTCOME_INTERVAL_SEC)

    # Phase 3.1: SEC EDGAR poller (default every 24h — real-time detection handled by
    # investor-edgar-watch-job Cloud Run job running every 15 min; this thread is the safety net)
    try:
        _SEC_POLL_INTERVAL_SEC = max(300, int(str(os.getenv("SEC_POLL_INTERVAL_SEC", "86400")).strip() or "86400"))
    except Exception:
        _SEC_POLL_INTERVAL_SEC = 86400

    def _sec_edgar_poll_loop() -> None:
        # Stagger by 90s so startup schema runs complete first.
        refresh_stop.wait(90)
        while not refresh_stop.is_set():
            try:
                from app.services.sec_edgar_poller_service import poll_and_ingest_tickers
                poll_and_ingest_tickers()
            except Exception as exc:
                LOGGER.warning("[sec-edgar-poll] cycle failed: %s", str(exc), exc_info=True)
            refresh_stop.wait(_SEC_POLL_INTERVAL_SEC)

    # Form 4 insider trade poll — daily, separate from main filing cycle
    try:
        _FORM4_POLL_INTERVAL_SEC = max(3600, int(str(os.getenv("FORM4_POLL_INTERVAL_SEC", "86400")).strip() or "86400"))
    except Exception:
        _FORM4_POLL_INTERVAL_SEC = 86400

    def _form4_poll_loop() -> None:
        # Stagger by 120s
        refresh_stop.wait(120)
        while not refresh_stop.is_set():
            try:
                from app.services.sec_edgar_poller_service import poll_form4_for_tickers
                poll_form4_for_tickers()
            except Exception as exc:
                LOGGER.warning("[form4-insider-poll] cycle failed: %s", str(exc), exc_info=True)
            refresh_stop.wait(_FORM4_POLL_INTERVAL_SEC)

    # Layer 4: Autonomic Nervous System — runs every 8 hours by default
    try:
        _AUTONOMIC_INTERVAL_SEC = max(600, int(str(os.getenv("AUTONOMIC_INTERVAL_SEC", "28800")).strip() or "28800"))
    except Exception:
        _AUTONOMIC_INTERVAL_SEC = 28800

    def _autonomic_sweep_loop() -> None:
        # Stagger by 180s so all schemas are ready
        refresh_stop.wait(180)
        while not refresh_stop.is_set():
            try:
                from app.services.autonomic_service import run_autonomic_sweep
                run_autonomic_sweep()
            except Exception as exc:
                LOGGER.warning("[autonomic-sweep] autonomic cycle failed: %s", str(exc), exc_info=True)
            # Run cognitive sweep right after autonomic sweep
            try:
                from app.services.cognitive_engine_service import run_cognitive_sweep
                run_cognitive_sweep()
            except Exception as exc:
                LOGGER.warning("[autonomic-sweep] cognitive cycle failed: %s", str(exc), exc_info=True)
            refresh_stop.wait(_AUTONOMIC_INTERVAL_SEC)

    # Morning brief scheduler — generates a fresh brief daily
    try:
        _MORNING_BRIEF_INTERVAL_SEC = max(3600, int(str(os.getenv("MORNING_BRIEF_INTERVAL_SEC", "21600")).strip() or "21600"))
    except Exception:
        _MORNING_BRIEF_INTERVAL_SEC = 21600  # 6 hours

    def _morning_brief_loop() -> None:
        # Stagger by 150s so schemas and market snapshot are ready
        refresh_stop.wait(150)
        while not refresh_stop.is_set():
            try:
                from app.services.portfolio_memory_service import save_morning_brief_snapshot
                save_morning_brief_snapshot(limit_holdings=5, source="scheduler")
                logging.getLogger(__name__).info("[morning-brief] generated fresh brief")
            except Exception:
                logging.getLogger(__name__).warning("[morning-brief] generation failed", exc_info=True)
            refresh_stop.wait(_MORNING_BRIEF_INTERVAL_SEC)

    @app.on_event("startup")
    def _seed_market_caps_to_pg() -> None:
        """Seed market caps from local JSON cache into Postgres on startup.

        The JSON cache (baked into the Docker image) may have stale timestamps but
        the raw market_cap values are still useful seeds. Postgres persists them
        across container restarts so the company list sorts correctly by market cap.
        Only seeds rows missing from Postgres; does not overwrite fresher values.
        """
        def _run() -> None:
            try:
                import json
                from pathlib import Path
                cache_path = Path(os.getenv("DATA_DIR", "/app/data")) / "cache" / "market_cap_cache.json"
                if not cache_path.exists():
                    return
                raw = json.loads(cache_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    return
                from app.services.postgres_core_service import pg_connect
                con = pg_connect()
                if con is None:
                    return
                try:
                    cur = con.cursor()
                    seeded = 0
                    for tk, v in raw.items():
                        if not isinstance(v, dict):
                            continue
                        val = v.get("market_cap") or 0
                        try:
                            val = int(float(val))
                        except Exception:
                            continue
                        if val <= 0:
                            continue
                        tk_clean = str(tk or "").strip().upper()[:16]
                        if not tk_clean:
                            continue
                        cur.execute(
                            """
                            INSERT INTO company_profile_cache_core(ticker, market_cap, updated_at)
                            VALUES (%s, %s, NOW()::text)
                            ON CONFLICT(ticker) DO UPDATE SET
                                market_cap = EXCLUDED.market_cap
                            WHERE company_profile_cache_core.market_cap IS NULL
                            """,
                            (tk_clean, val),
                        )
                        seeded += 1
                    con.commit()
                    if seeded:
                        import logging
                        logging.getLogger(__name__).info("[startup] seeded %d market caps to Postgres", seeded)
                finally:
                    con.close()
            except Exception as exc:
                LOGGER.warning("[startup] market-cap seed failed: %s", str(exc), exc_info=True)
        import threading
        threading.Thread(target=_run, name="mcap-seed", daemon=True).start()

    @app.on_event("startup")
    def _start_market_refresh_loop() -> None:
        default_enabled = "0" if str(os.getenv("APP_ENV", "")).strip().lower() in {"cloud", "production"} else "1"
        if str(os.getenv("MARKET_SNAPSHOT_REFRESH_ENABLED", default_enabled)).strip().lower() in {"1", "true", "yes", "on"}:
            th = threading.Thread(target=_market_snapshot_loop, name="market-snapshot-refresh", daemon=True)
            app.state.market_refresh_thread = th
            th.start()
            th2 = threading.Thread(target=_outcome_measurement_loop, name="outcome-measurement", daemon=True)
            app.state.outcome_measurement_thread = th2
            th2.start()
        # Phase 3.1: SEC EDGAR auto-poll (skip if disabled)
        if str(os.getenv("SEC_POLL_ENABLED", default_enabled)).strip().lower() in {"1", "true", "yes", "on"}:
            th3 = threading.Thread(target=_sec_edgar_poll_loop, name="sec-edgar-poll", daemon=True)
            app.state.sec_poll_thread = th3
            th3.start()
        # Form 4 daily insider trade poll
        if str(os.getenv("FORM4_POLL_ENABLED", default_enabled)).strip().lower() in {"1", "true", "yes", "on"}:
            th4 = threading.Thread(target=_form4_poll_loop, name="form4-insider-poll", daemon=True)
            app.state.form4_poll_thread = th4
            th4.start()
        # Layer 4: Autonomic sweep
        if str(os.getenv("AUTONOMIC_ENABLED", default_enabled)).strip().lower() in {"1", "true", "yes", "on"}:
            th5 = threading.Thread(target=_autonomic_sweep_loop, name="autonomic-sweep", daemon=True)
            app.state.autonomic_sweep_thread = th5
            th5.start()
        # Morning brief auto-generation
        if str(os.getenv("MORNING_BRIEF_ENABLED", default_enabled)).strip().lower() in {"1", "true", "yes", "on"}:
            th6 = threading.Thread(target=_morning_brief_loop, name="morning-brief-scheduler", daemon=True)
            app.state.morning_brief_thread = th6
            th6.start()

    @app.on_event("shutdown")
    def _stop_market_refresh_loop() -> None:
        try:
            refresh_stop.set()
        except Exception:
            pass
        for attr in ("market_refresh_thread", "outcome_measurement_thread", "sec_poll_thread", "form4_poll_thread", "autonomic_sweep_thread", "morning_brief_thread"):
            th = getattr(app.state, attr, None)
            if th is not None:
                try:
                    th.join(timeout=2.0)
                except Exception:
                    pass

    app.include_router(dashboard_router)
    app.include_router(reports_router)
    app.include_router(organizer_router)
    app.include_router(observability_router)
    app.include_router(company_file_router)
    app.include_router(ai_router)
    app.include_router(supply_chain_router)
    app.include_router(workspace_os_router)

    # Serve static assets (JS, CSS)
    _static_dir = ROOT / "app" / "static"
    if _static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

    return app


app = create_app()
