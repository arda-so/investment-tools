from __future__ import annotations

import threading
import warnings
from typing import Any, Callable

from app.services.ai_job_queue_service import (
    complete_job,
    enqueue_job,
    fail_job,
    get_cached_result as _get_cached_result,
    get_job as _get_job,
    set_cached_result as _set_cached_result,
)


_DEPRECATION_MSG = (
    "app.services.ai_async_service is deprecated. "
    "Use app.services.ai_job_queue_service directly."
)


def _warn_deprecated() -> None:
    warnings.warn(_DEPRECATION_MSG, DeprecationWarning, stacklevel=2)


def create_job(payload: dict[str, Any]) -> str:
    """Compatibility wrapper: legacy callers should use enqueue_job directly."""
    _warn_deprecated()
    return enqueue_job(payload)


def get_job(job_id: str) -> dict[str, Any] | None:
    _warn_deprecated()
    return _get_job(job_id)


def get_cached_result(payload: dict[str, Any], ttl_sec: int = 75) -> dict[str, Any] | None:
    _warn_deprecated()
    return _get_cached_result(payload, ttl_sec=ttl_sec)


def set_cached_result(payload: dict[str, Any], result: dict[str, Any]) -> None:
    _warn_deprecated()
    _set_cached_result(payload, result)


def start_job(job_id: str, fn: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    """Compatibility wrapper for legacy fire-and-forget start semantics."""
    _warn_deprecated()

    row = get_job(job_id)
    if not isinstance(row, dict):
        return

    def _runner() -> None:
        try:
            payload = dict(row.get("payload") or {})
            out = fn(payload)
            final = out if isinstance(out, dict) else {"status": "ok", "message": str(out or "")}
            complete_job(job_id, final)
            set_cached_result(payload, final)
        except Exception as exc:
            fail_job(
                job_id,
                error=str(exc or "async_job_error"),
                result={"status": "error", "intent": "async_job_error", "message": "Command failed."},
            )

    th = threading.Thread(target=_runner, name=f"ai-async-compat-{job_id[:8]}", daemon=True)
    th.start()
