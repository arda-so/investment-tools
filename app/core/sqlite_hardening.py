from __future__ import annotations

import os
import sqlite3
import time
from typing import Callable, TypeVar

T = TypeVar("T")


def _busy_timeout_ms() -> int:
    try:
        return max(1000, int(str(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "8000")).strip() or "8000"))
    except Exception:
        return 8000


def _retry_attempts() -> int:
    try:
        return max(1, int(str(os.getenv("SQLITE_RETRY_ATTEMPTS", "5")).strip() or "5"))
    except Exception:
        return 5


def _retry_base_ms() -> int:
    try:
        return max(10, int(str(os.getenv("SQLITE_RETRY_BASE_MS", "80")).strip() or "80"))
    except Exception:
        return 80


def connect_sqlite(path: str, *, row_factory: bool = True) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=float(_busy_timeout_ms()) / 1000.0)
    if row_factory:
        con.row_factory = sqlite3.Row
    # Hardening for concurrent read/write patterns.
    con.execute(f"PRAGMA busy_timeout={_busy_timeout_ms()}")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def is_lock_error(exc: BaseException) -> bool:
    s = str(exc or "").lower()
    return "database is locked" in s or "database is busy" in s or "locked" in s


def sqlite_retry(fn: Callable[[], T]) -> T:
    attempts = _retry_attempts()
    base_ms = _retry_base_ms()
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if not is_lock_error(exc) or i >= attempts - 1:
                raise
            time.sleep((base_ms * (i + 1)) / 1000.0)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("sqlite_retry failed without exception")

