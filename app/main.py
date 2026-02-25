from __future__ import annotations

import math
import os
import threading

from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from app.core.config import ROOT
from app.db.database import create_tables
from app.routers.company_file import router as company_file_router
from app.routers.ai import router as ai_router
from app.routers.dashboard import router as dashboard_router
from app.routers.observability import router as observability_router
from app.routers.organizer import router as organizer_router
from app.routers.reports import router as reports_router
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


def create_app() -> FastAPI:
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

    create_tables()
    ensure_schema()
    ensure_ai_schema()
    ensure_app_knowledge_map()
    ensure_chat_memory_schema()
    ensure_observability_schema()
    ensure_portfolio_memory_schema()
    ensure_ai_job_queue_schema()
    ensure_postgres_core_schema()
    # Enforce Postgres as the only runtime backend.
    if core_backend() != "postgres":
        raise RuntimeError("postgres_required: set CORE_DB_BACKEND=postgres")
    if not strict_postgres_mode():
        guard_core_backend_cutover()
    enforce_strict_postgres_ready()
    ensure_sec_ingest_schema()
    ensure_ai_insight_schema()
    # Phase 3.1: event infrastructure
    from app.services.events_service import ensure_events_schema
    ensure_events_schema()
    from app.services.sec_edgar_poller_service import ensure_poller_schema
    ensure_poller_schema()
    from app.services.earnings_transcript_service import ensure_earnings_analysis_schema
    ensure_earnings_analysis_schema()

    refresh_stop = threading.Event()
    app.state.market_refresh_stop = refresh_stop

    def _market_snapshot_loop() -> None:
        try:
            interval = max(30, int(str(os.getenv("MARKET_SNAPSHOT_REFRESH_SEC", "180")).strip() or "180"))
        except Exception:
            interval = 180
        while not refresh_stop.is_set():
            try:
                # Refresh news snapshots via existing home pipeline.
                _ = home_snapshot()
                # Refresh earnings snapshot via existing dashboard report pipeline.
                from app.routers.dashboard import dashboard_report_panels

                _ = dashboard_report_panels()
            except Exception:
                pass
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
            except Exception:
                pass
            refresh_stop.wait(_OUTCOME_INTERVAL_SEC)

    # Phase 3.1: SEC EDGAR poller (default every 6 hours, configurable via SEC_POLL_INTERVAL_SEC)
    try:
        _SEC_POLL_INTERVAL_SEC = max(300, int(str(os.getenv("SEC_POLL_INTERVAL_SEC", "21600")).strip() or "21600"))
    except Exception:
        _SEC_POLL_INTERVAL_SEC = 21600

    def _sec_edgar_poll_loop() -> None:
        # Stagger by 90s so startup schema runs complete first.
        refresh_stop.wait(90)
        while not refresh_stop.is_set():
            try:
                from app.services.sec_edgar_poller_service import poll_and_ingest_tickers
                poll_and_ingest_tickers()
            except Exception:
                pass
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
            except Exception:
                pass
            refresh_stop.wait(_FORM4_POLL_INTERVAL_SEC)

    @app.on_event("startup")
    def _start_market_refresh_loop() -> None:
        if str(os.getenv("MARKET_SNAPSHOT_REFRESH_ENABLED", "1")).strip().lower() not in {"1", "true", "yes", "on"}:
            return
        th = threading.Thread(target=_market_snapshot_loop, name="market-snapshot-refresh", daemon=True)
        app.state.market_refresh_thread = th
        th.start()
        th2 = threading.Thread(target=_outcome_measurement_loop, name="outcome-measurement", daemon=True)
        app.state.outcome_measurement_thread = th2
        th2.start()
        # Phase 3.1: SEC EDGAR auto-poll (skip if disabled)
        if str(os.getenv("SEC_POLL_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}:
            th3 = threading.Thread(target=_sec_edgar_poll_loop, name="sec-edgar-poll", daemon=True)
            app.state.sec_poll_thread = th3
            th3.start()
        # Form 4 daily insider trade poll
        if str(os.getenv("FORM4_POLL_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}:
            th4 = threading.Thread(target=_form4_poll_loop, name="form4-insider-poll", daemon=True)
            app.state.form4_poll_thread = th4
            th4.start()

    @app.on_event("shutdown")
    def _stop_market_refresh_loop() -> None:
        try:
            refresh_stop.set()
        except Exception:
            pass
        for attr in ("market_refresh_thread", "outcome_measurement_thread", "sec_poll_thread"):
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

    return app


app = create_app()
