from __future__ import annotations

import datetime as dt

from app.services.postgres_core_service import pg_connect


def ensure_observability_schema() -> None:
    con_pg = pg_connect()
    if con_pg is None:
        return
    try:
        cur = con_pg.cursor()
        cur.execute(
            """CREATE TABLE IF NOT EXISTS system_events_core (
                id BIGSERIAL PRIMARY KEY,
                created_at TEXT NOT NULL,
                service TEXT NOT NULL,
                status TEXT NOT NULL,
                latency_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                trace_id TEXT NOT NULL DEFAULT ''
            )"""
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_system_events_core_created ON system_events_core(created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_system_events_core_service ON system_events_core(service)")
        con_pg.commit()
    except Exception:
        try:
            con_pg.rollback()
        except Exception:
            pass
    finally:
        con_pg.close()


def log_system_event(
    service: str,
    status: str,
    latency_ms: float = 0.0,
    message: str = "",
    trace_id: str = "",
) -> bool:
    svc = str(service or "").strip()[:120]
    st = str(status or "").strip().lower()[:40]
    if not svc or not st:
        return False
    con_pg = pg_connect()
    if con_pg is None:
        return False
    try:
        cur = con_pg.cursor()
        cur.execute(
            """INSERT INTO system_events_core (created_at, service, status, latency_ms, message, trace_id)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            (
                dt.datetime.now().isoformat(),
                svc,
                st,
                float(latency_ms or 0.0),
                str(message or "")[:500],
                str(trace_id or "")[:120],
            ),
        )
        con_pg.commit()
        return True
    except Exception:
        try:
            con_pg.rollback()
        except Exception:
            pass
        return False
    finally:
        con_pg.close()


def list_recent_system_events(limit: int = 50) -> list[dict[str, str]]:
    lim = max(1, min(500, int(limit or 50)))
    con_pg = pg_connect()
    if con_pg is None:
        return []
    out: list[dict[str, str]] = []
    try:
        cur = con_pg.cursor()
        cur.execute(
            """SELECT created_at, service, status, latency_ms, message, trace_id
               FROM system_events_core
               ORDER BY id DESC
               LIMIT %s""",
            (lim,),
        )
        rows = cur.fetchall() or []
        for r in rows:
            out.append(
                {
                    "created_at": str(r[0] or ""),
                    "service": str(r[1] or ""),
                    "status": str(r[2] or ""),
                    "latency_ms": f"{float(r[3] or 0.0):.1f}",
                    "message": str(r[4] or ""),
                    "trace_id": str(r[5] or ""),
                }
            )
        return out
    except Exception:
        return []
    finally:
        con_pg.close()
