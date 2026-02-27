from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from app.core.config import CORE_DB_PATH, ROOT
from app.core.sqlite_hardening import connect_sqlite, sqlite_retry as _sqlite_retry


def _sqlite_forbidden() -> bool:
    # Local import avoids module-load cycles.
    from app.services.postgres_core_service import core_backend as _core_backend, strict_postgres_mode as _strict

    return _core_backend() == "postgres" and _strict()


def get_sqlite_conn(path: str | Path | None = None, *, row_factory: bool = True) -> sqlite3.Connection:
    if _sqlite_forbidden():
        raise RuntimeError("sqlite_forbidden_in_strict_postgres_mode")
    p = str(path or CORE_DB_PATH)
    return connect_sqlite(p, row_factory=row_factory)


def core_conn(*, row_factory: bool = True) -> sqlite3.Connection:
    return get_sqlite_conn(CORE_DB_PATH, row_factory=row_factory)


def onyx_conn(*, row_factory: bool = True) -> sqlite3.Connection:
    return get_sqlite_conn(ROOT / "onyx_brain.db", row_factory=row_factory)


def sqlite_retry(fn, *, retries: int = 8, base_sleep: float = 0.06) -> Any:
    # Underlying hardened helper is env-driven and only accepts fn.
    _ = retries
    _ = base_sleep
    return _sqlite_retry(fn)


def get_pg_conn():
    # Local import avoids module-load cycles.
    from app.services.postgres_core_service import pg_connect

    return pg_connect()


def core_backend() -> str:
    from app.services.postgres_core_service import core_backend as _core_backend

    return _core_backend()


def strict_postgres_mode() -> bool:
    from app.services.postgres_core_service import strict_postgres_mode as _strict

    return _strict()
