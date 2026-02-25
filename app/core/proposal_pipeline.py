from __future__ import annotations

import json
from typing import Any


def _to_list(val: Any) -> list[Any]:
    if isinstance(val, list):
        return list(val)
    if isinstance(val, tuple):
        return list(val)
    if isinstance(val, dict):
        return [dict(val)]
    try:
        return list(json.loads(str(val or "[]")))
    except Exception:
        return []


def _to_dict(val: Any) -> dict[str, Any]:
    if isinstance(val, dict):
        return dict(val)
    try:
        obj = json.loads(str(val or "{}"))
        return dict(obj) if isinstance(obj, dict) else {}
    except Exception:
        return {}


def normalize_score(value: Any, *, min_value: float, max_value: float, default: float = 0.0) -> float:
    try:
        num = float(value or 0.0)
    except Exception:
        num = float(default)
    if num < min_value:
        return float(min_value)
    if num > max_value:
        return float(max_value)
    return float(num)


def normalize_reasoning_payload(raw_reasoning: dict[str, Any] | None) -> dict[str, Any]:
    rr = _to_dict(raw_reasoning)
    out: dict[str, Any] = {}
    for k, lim in {
        "margin_impact": 320,
        "thesis_validation": 320,
        "risk_assessment": 320,
        "actionable_proposal": 320,
        "peer_contagion": 320,
        "confidence": 16,
        "recommended_stance": 12,
        "invalidation_hit": 16,
        "quality_verdict": 16,
        "quality_reason": 220,
    }.items():
        v = rr.get(k)
        if v is None:
            continue
        txt = str(v).strip()
        if txt:
            out[k] = txt[:lim]
    cards: list[dict[str, str]] = []
    for c in _to_list(rr.get("insight_cards")):
        if not isinstance(c, dict):
            continue
        lb = str(c.get("label") or "").strip()[:42]
        tx = str(c.get("text") or "").strip()[:320]
        if lb and tx:
            cards.append({"label": lb, "text": tx})
    if cards:
        out["insight_cards"] = cards[:3]
    return out


def insights_from_reasoning(reasoning: dict[str, Any]) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    for c in _to_list(_to_dict(reasoning).get("insight_cards")):
        if not isinstance(c, dict):
            continue
        lb = str(c.get("label") or "").strip()[:42]
        tx = str(c.get("text") or "").strip()[:320]
        if lb and tx:
            cards.append({"label": lb, "text": tx})
    return cards[:3]


def normalize_citations(citations: list[dict[str, Any]] | Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for c in _to_list(citations):
        if not isinstance(c, dict):
            continue
        label = str(c.get("label") or "Source").strip()[:64]
        url = str(c.get("url") or "").strip()
        if url:
            out.append({"label": label or "Source", "url": url})
    return out[:8]


def passes_reasoning_quality(reasoning: dict[str, Any], insights: list[dict[str, str]], citations: list[dict[str, str]]) -> bool:
    rz = _to_dict(reasoning)
    ins = [x for x in _to_list(insights) if isinstance(x, dict)]
    cits = [x for x in _to_list(citations) if isinstance(x, dict)]
    if len(ins) < 2:
        return False
    labels: set[str] = set()
    strong = 0
    for card in ins[:3]:
        lb = str(card.get("label") or "").strip()
        tx = str(card.get("text") or "").strip()
        if not lb or not tx:
            continue
        labels.add(lb.lower())
        if len(tx) >= 40:
            strong += 1
    if len(labels) < 2 or strong < 2:
        return False
    if not cits:
        return False
    qv = str(rz.get("quality_verdict") or "").strip().lower()
    if qv and qv != "accept":
        return False
    return True


def run_proposal_pipeline(
    *,
    relevance_ok: bool,
    raw_reasoning: dict[str, Any] | None,
    citations: list[dict[str, Any]] | Any,
    confidence: Any,
    priority_score: Any,
    verify_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not relevance_ok:
        return {
            "accepted": False,
            "reasoning": {},
            "insights": [],
            "citations": [],
            "confidence": normalize_score(confidence, min_value=0.0, max_value=1.0, default=0.0),
            "priority_score": normalize_score(priority_score, min_value=0.0, max_value=99.9, default=0.0),
        }
    reasoning = normalize_reasoning_payload(raw_reasoning)
    verify = _to_dict(verify_result)
    if verify:
        verdict = str(verify.get("quality_verdict") or "").strip().lower()
        reason = str(verify.get("quality_reason") or "").strip()[:220]
        if verdict:
            reasoning["quality_verdict"] = verdict[:16]
        if reason:
            reasoning["quality_reason"] = reason
    reasoning = normalize_reasoning_payload(reasoning)
    insights = insights_from_reasoning(reasoning)
    cits = normalize_citations(citations)
    conf = normalize_score(confidence, min_value=0.0, max_value=1.0, default=0.0)
    prio = normalize_score(priority_score, min_value=0.0, max_value=99.9, default=0.0)
    accepted = passes_reasoning_quality(reasoning, insights, cits)
    return {
        "accepted": bool(accepted),
        "reasoning": reasoning,
        "insights": insights,
        "citations": cits,
        "confidence": conf,
        "priority_score": prio,
    }
