from __future__ import annotations

import datetime as dt


def safe_day_iso(day: str, fallback: dt.date | None = None) -> str:
    d = str(day or "").strip()
    fb = fallback or dt.date.today()
    try:
        return dt.date.fromisoformat(d).isoformat()
    except Exception:
        return fb.isoformat()


def parse_datetime_flexible(value: str, formats: tuple[str, ...] = ()) -> dt.datetime | None:
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", ""))
    except Exception:
        pass
    for fmt in formats:
        try:
            return dt.datetime.strptime(s, fmt)
        except Exception:
            continue
    return None
