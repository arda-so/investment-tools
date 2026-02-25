from __future__ import annotations

import datetime as dt
import threading
from typing import Any

from app.services.postgres_core_service import get_price_metrics_pg, upsert_price_metrics_pg

_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_TTL_SEC = 60.0


def _now_ts() -> float:
    return dt.datetime.now().timestamp()


def _safe_float(v: Any) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def _to_pct(start_px: float, end_px: float) -> float:
    if start_px <= 0:
        return 0.0
    return ((end_px / start_px) - 1.0) * 100.0


def get_price_metrics(ticker: str) -> dict[str, Any]:
    tk = str(ticker or "").strip().upper()[:16]
    if not tk:
        return {}
    with _CACHE_LOCK:
        cached = _CACHE.get(tk)
        if cached and (_now_ts() - float(cached[0])) <= _TTL_SEC:
            return dict(cached[1])
    row = get_price_metrics_pg(tk)
    if row:
        with _CACHE_LOCK:
            _CACHE[tk] = (_now_ts(), dict(row))
    return row


def refresh_price_metrics(ticker: str) -> dict[str, Any]:
    tk = str(ticker or "").strip().upper()[:16]
    if not tk:
        return {}
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return {}
    try:
        hist = yf.Ticker(tk).history(period="6y", interval="1d", auto_adjust=False)
        if hist is None or hist.empty:
            return {}
        closes = hist.get("Close")
        if closes is None or len(closes) < 5:
            return {}
        end_px = _safe_float(closes.iloc[-1])
        asof = str(getattr(closes.index[-1], "date", lambda: closes.index[-1])())

        # 12M and 5Y from nearest available bars.
        n = len(closes)
        px_12m = _safe_float(closes.iloc[max(0, n - 252)])
        px_5y = _safe_float(closes.iloc[max(0, n - 252 * 5)])

        # YTD: first close in current year.
        y = dt.date.today().year
        ytd_px = 0.0
        for i in range(n):
            idx = closes.index[i]
            yy = int(getattr(idx, "year", y))
            if yy == y:
                ytd_px = _safe_float(closes.iloc[i])
                break
        if ytd_px <= 0:
            ytd_px = _safe_float(closes.iloc[0])

        out = {
            "ticker": tk,
            "asof": asof,
            "source": "yfinance",
            "ytd_return": _to_pct(ytd_px, end_px),
            "m12_return": _to_pct(px_12m, end_px),
            "y5_return": _to_pct(px_5y, end_px),
            "ytd_start_px": ytd_px,
            "ytd_end_px": end_px,
            "m12_start_px": px_12m,
            "m12_end_px": end_px,
            "y5_start_px": px_5y,
            "y5_end_px": end_px,
            "updated_at": dt.datetime.now().isoformat(),
        }
        _ = upsert_price_metrics_pg(
            ticker=tk,
            asof=out["asof"],
            source=out["source"],
            ytd_return=float(out["ytd_return"]),
            m12_return=float(out["m12_return"]),
            y5_return=float(out["y5_return"]),
            ytd_start_px=float(out["ytd_start_px"]),
            ytd_end_px=float(out["ytd_end_px"]),
            m12_start_px=float(out["m12_start_px"]),
            m12_end_px=float(out["m12_end_px"]),
            y5_start_px=float(out["y5_start_px"]),
            y5_end_px=float(out["y5_end_px"]),
        )
        with _CACHE_LOCK:
            _CACHE[tk] = (_now_ts(), dict(out))
        return out
    except Exception:
        return {}
