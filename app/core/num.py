from __future__ import annotations


def to_float(v: object, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def to_float_clean(v: object, default: float = 0.0) -> float:
    try:
        s = str(v or "").replace(",", "").strip()
        if not s:
            return float(default)
        return float(s)
    except Exception:
        return float(default)
