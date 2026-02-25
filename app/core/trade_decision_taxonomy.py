from __future__ import annotations

from app.core.normalize import normalize_text

SELL_REASON_LABELS: dict[str, str] = {
    "thesis_broken": "Thesis Broken",
    "risk_reduced": "Risk Reduced",
    "position_sizing": "Position Sizing",
    "rebalance": "Rebalance",
    "cash_need": "Cash Need",
    "redeploy_better_idea": "Redeploy to Better Idea",
    "other": "Other",
}

BUY_REASON_LABELS: dict[str, str] = {
    "new_position": "New Position",
    "price_looks_cheap": "Price Looks Cheap",
    "portfolio_rebalance": "Portfolio Rebalance",
    "switch_weaker_position": "Switch from Weaker Position",
    "risk_controlled_add": "Risk-Controlled Add",
    "existing_position_add": "Existing Position Add",
    "other": "Other",
}

BUY_BELIEF_LABELS: dict[str, str] = {
    "long_term_compounder": "Long-Term Compounder",
    "undervalued_now": "Undervalued Now",
    "quality_improvement": "Quality Improvement",
    "strategic_diversification": "Strategic Diversification",
    "other": "Other",
}


def normalize_sell_reason(raw: str) -> str:
    v = normalize_text(raw).replace("-", "_").replace(" ", "_")
    return v if v in SELL_REASON_LABELS else "other"


def normalize_buy_reason(raw: str) -> str:
    v = normalize_text(raw).replace("-", "_").replace(" ", "_")
    return v if v in BUY_REASON_LABELS else "other"


def normalize_buy_belief(raw: str) -> str:
    v = normalize_text(raw).replace("-", "_").replace(" ", "_")
    return v if v in BUY_BELIEF_LABELS else ""


def label_for_sell_reason(key: str) -> str:
    return SELL_REASON_LABELS.get(str(key or "").strip(), "Other")


def label_for_buy_reason(key: str) -> str:
    return BUY_REASON_LABELS.get(str(key or "").strip(), "Other")


def label_for_buy_belief(key: str) -> str:
    return BUY_BELIEF_LABELS.get(str(key or "").strip(), "")
