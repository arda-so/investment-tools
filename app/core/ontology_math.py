from __future__ import annotations

import datetime as dt
import os


def decay_enabled() -> bool:
    return str(os.getenv("ONTOLOGY_DECAY_ENABLED", "0")).strip().lower() in {"1", "true", "yes", "on"}


def decay_half_life_days() -> int:
    try:
        return max(30, int(str(os.getenv("ONTOLOGY_DECAY_HALF_LIFE_DAYS", "365")).strip() or "365"))
    except Exception:
        return 365


def effective_confidence(conf: float, last_verified_at: str, half_life_days: int) -> float:
    base = max(0.0, min(1.0, float(conf or 0.0)))
    if not decay_enabled():
        return base
    try:
        ts = dt.datetime.fromisoformat(str(last_verified_at or "").strip())
        age_days = max(0.0, (dt.datetime.now() - ts).total_seconds() / 86400.0)
        hl = max(1.0, float(half_life_days or 365))
        decayed = base * (0.5 ** (age_days / hl))
        return max(0.0, min(1.0, float(decayed)))
    except Exception:
        return base


def signed_weight_for_rel(rel_type: str) -> float:
    rt = str(rel_type or "").strip().upper()
    if rt in {"HEDGES_AGAINST", "IMMUNE_TO"}:
        return -1.0
    return 1.0
