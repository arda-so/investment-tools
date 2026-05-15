from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.num import to_float
from app.services.ai_orchestrator import (
    get_risk_veto_config,
    list_recent_risk_veto_decisions,
    update_risk_veto_config,
)
from app.services.proactive_ai_service import list_recent_agent_runs, list_recent_reflexions
from app.services.observability_service import list_recent_system_events


router = APIRouter()


@router.get("/api/observability/snapshot")
def api_observability_snapshot():
    """Full observability snapshot as JSON — powers the inline workspace space."""
    from app.services.dashboard_service import get_data_integrity_health
    rows = list_recent_system_events(limit=50)
    risk_veto = list_recent_risk_veto_decisions(limit=12)
    risk_veto_config = get_risk_veto_config()
    agent_runs = list_recent_agent_runs(limit=12)
    reflexions = list_recent_reflexions(limit=12)
    policy_versions = list((reflexions or {}).get("policy_versions") or [])
    active_policy = next((x for x in policy_versions if int(x.get("is_active") or 0) == 1), {})
    data_integrity = get_data_integrity_health()
    return JSONResponse({
        "ok": True,
        "system_events": rows,
        "risk_veto_decisions": risk_veto,
        "risk_veto_config": risk_veto_config,
        "agent_runs": agent_runs,
        "reflexions": reflexions,
        "active_reflexion_policy": active_policy,
        "data_integrity": data_integrity,
    })


@router.get("/observability")
def observability_page(request: Request, msg: str = ""):
    if request.headers.get("HX-Request") == "true":
        return JSONResponse({"redirect": "/today?space=observability"})
    return RedirectResponse(url="/today?space=observability", status_code=302)


@router.post("/observability/risk-veto/config")
def observability_risk_veto_config_update(
    request: Request,
    enabled: str = Form("1"),
    min_confidence_for_mutation: str = Form("0.62"),
    max_single_add_pct: str = Form("5.0"),
    max_position_weight_pct: str = Form("20.0"),
    max_var95_pct: str = Form("6.0"),
    max_cvar95_pct: str = Form("8.0"),
    min_quote_coverage_pct: str = Form("75.0"),
    high_impact_notional_pct: str = Form("3.0"),
    require_known_ticker_scope: str = Form("1"),
    block_on_unknown_ticker: str = Form("1"),
    review_for_high_impact: str = Form("1"),
):
    def _to_bool(v: str) -> bool:
        return str(v or "").strip().lower() in {"1", "true", "yes", "on"}

    cfg = update_risk_veto_config(
        {
            "enabled": _to_bool(enabled),
            "min_confidence_for_mutation": to_float(min_confidence_for_mutation, 0.62),
            "max_single_add_pct": to_float(max_single_add_pct, 5.0),
            "max_position_weight_pct": to_float(max_position_weight_pct, 20.0),
            "max_var95_pct": to_float(max_var95_pct, 6.0),
            "max_cvar95_pct": to_float(max_cvar95_pct, 8.0),
            "min_quote_coverage_pct": to_float(min_quote_coverage_pct, 75.0),
            "high_impact_notional_pct": to_float(high_impact_notional_pct, 3.0),
            "require_known_ticker_scope": _to_bool(require_known_ticker_scope),
            "block_on_unknown_ticker": _to_bool(block_on_unknown_ticker),
            "review_for_high_impact": _to_bool(review_for_high_impact),
        }
    )
    msg = (
        "Risk Veto settings updated. "
        f"MinConf={float(cfg.get('min_confidence_for_mutation') or 0.0):.2f}, "
        f"MaxAdd={float(cfg.get('max_single_add_pct') or 0.0):.2f}%."
    )
    # Return JSON for fetch requests, redirect for form submissions
    accept = request.headers.get("accept", "")
    if "application/json" in accept or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JSONResponse({"ok": True, "message": msg, "config": cfg})
    return RedirectResponse(url="/observability?msg=" + quote(msg), status_code=303)
