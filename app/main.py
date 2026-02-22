from __future__ import annotations

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


def create_app() -> FastAPI:
    app = FastAPI(title="Investor OS v2", version="2.0.0")
    app.state.templates = Jinja2Templates(directory=str(ROOT / "app" / "templates"))

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

    app.include_router(dashboard_router)
    app.include_router(reports_router)
    app.include_router(organizer_router)
    app.include_router(observability_router)
    app.include_router(company_file_router)
    app.include_router(ai_router)

    return app


app = create_app()
