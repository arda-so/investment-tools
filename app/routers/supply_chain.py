from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.services.supply_chain_service import get_network_graph


router = APIRouter()


@router.get("/api/network/{ticker}")
def api_network(ticker: str):
    out = get_network_graph(ticker)
    if not out.get("nodes"):
        return JSONResponse({"ok": False, "error": "ticker_not_found", "nodes": [], "edges": []}, status_code=404)
    return JSONResponse({"ok": True, **out})
