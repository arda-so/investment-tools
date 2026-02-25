from __future__ import annotations

from fastapi import Request


def is_hx_request(request: Request) -> bool:
    return str(request.headers.get("HX-Request") or "").strip().lower() == "true"
