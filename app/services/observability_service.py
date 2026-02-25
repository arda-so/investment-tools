from __future__ import annotations

import datetime as dt

from app.core.db import core_conn as _conn
from app.core.sqlite_hardening import sqlite_retry
from app.services.postgres_core_service import pg_connect, pg_enabled


def ensure_observability_schema() -> None:
    if pg_enabled():
        return
    con = _conn()
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS system_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                service TEXT NOT NULL,
                status TEXT NOT NULL,
                latency_ms REAL NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                trace_id TEXT NOT NULL DEFAULT ''
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_system_events_created ON system_events(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_system_events_service ON system_events(service)")
        con.commit()
    finally:
        con.close()


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
    if pg_enabled():
        con_pg = pg_connect()
        if con_pg is None:
            return False
        try:
            cur = con_pg.cursor()
            cur.execute(
                """INSERT INTO system_events_core (id, created_at, service, status, latency_ms, message, trace_id)
                   VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM system_events_core), %s,%s,%s,%s,%s,%s)""",
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
    try:
        def _write() -> None:
            con = _conn()
            try:
                con.execute(
                    """INSERT INTO system_events (created_at, service, status, latency_ms, message, trace_id)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        dt.datetime.now().isoformat(),
                        svc,
                        st,
                        float(latency_ms or 0.0),
                        str(message or "")[:500],
                        str(trace_id or "")[:120],
                    ),
                )
                con.commit()
            finally:
                con.close()
        sqlite_retry(_write)
        return True
    except Exception:
        return False


def list_recent_system_events(limit: int = 50) -> list[dict[str, str]]:
    lim = max(1, min(500, int(limit or 50)))
    if pg_enabled():
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
    con = _conn()
    out: list[dict[str, str]] = []
    try:
        rows = con.execute(
            """SELECT created_at, service, status, latency_ms, message, trace_id
               FROM system_events
               ORDER BY id DESC
               LIMIT ?""",
            (lim,),
        ).fetchall()
        for r in rows:
            out.append(
                {
                    "created_at": str(r["created_at"] or ""),
                    "service": str(r["service"] or ""),
                    "status": str(r["status"] or ""),
                    "latency_ms": f"{float(r['latency_ms'] or 0.0):.1f}",
                    "message": str(r["message"] or ""),
                    "trace_id": str(r["trace_id"] or ""),
                }
            )
        return out
    finally:
        con.close()
