from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Any


_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"ts": 0.0, "payload": None}
_CACHE_TTL_SEC = 15 * 60


_PAIRS: list[tuple[str, str, str, str]] = [
    # Energy transmission
    ("WTI Crude", "CL=F", "US Dollar Index", "DX-Y.NYB"),
    ("WTI Crude", "CL=F", "USD/CAD", "CAD=X"),
    ("WTI Crude", "CL=F", "USD/NOK", "NOK=X"),
    ("Brent", "BZ=F", "US Dollar Index", "DX-Y.NYB"),
    ("Gold", "GC=F", "US 10Y Yield", "^TNX"),
    ("Natural Gas", "NG=F", "US Dollar Index", "DX-Y.NYB"),
    ("Gasoline", "RB=F", "US Dollar Index", "DX-Y.NYB"),
    # Metals transmission
    ("Gold", "GC=F", "US Dollar Index", "DX-Y.NYB"),
    ("Silver", "SI=F", "US Dollar Index", "DX-Y.NYB"),
    ("Copper", "HG=F", "AUD/USD", "AUDUSD=X"),
    ("Copper", "HG=F", "S&P 500", "^GSPC"),
    ("Platinum", "PL=F", "US Dollar Index", "DX-Y.NYB"),
    ("Palladium", "PA=F", "US Dollar Index", "DX-Y.NYB"),
    # Agriculture transmission
    ("Wheat", "ZW=F", "US Dollar Index", "DX-Y.NYB"),
    ("Corn", "ZC=F", "US Dollar Index", "DX-Y.NYB"),
    ("Soybeans", "ZS=F", "US Dollar Index", "DX-Y.NYB"),
    ("Coffee", "KC=F", "US Dollar Index", "DX-Y.NYB"),
    ("Sugar", "SB=F", "US Dollar Index", "DX-Y.NYB"),
    ("Cocoa", "CC=F", "US Dollar Index", "DX-Y.NYB"),
    # Cross-asset anchors
    ("S&P 500", "^GSPC", "VIX", "^VIX"),
    ("Brent", "BZ=F", "EUR/USD", "EURUSD=X"),
]


def _beta(x_vals: list[float], y_vals: list[float]) -> float:
    n = min(len(x_vals), len(y_vals))
    if n < 5:
        return 0.0
    xs = x_vals[-n:]
    ys = y_vals[-n:]
    mx = sum(xs) / n
    my = sum(ys) / n
    var_x = sum((v - mx) * (v - mx) for v in xs) / n
    if abs(var_x) < 1e-12:
        return 0.0
    cov_xy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / n
    return cov_xy / var_x


def _corr(x_vals: list[float], y_vals: list[float], window: int) -> float:
    n = min(len(x_vals), len(y_vals), int(window))
    if n < 5:
        return 0.0
    xs = x_vals[-n:]
    ys = y_vals[-n:]
    mx = sum(xs) / n
    my = sum(ys) / n
    vx = sum((v - mx) * (v - mx) for v in xs)
    vy = sum((v - my) * (v - my) for v in ys)
    if vx <= 1e-12 or vy <= 1e-12:
        return 0.0
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    return cov / (vx**0.5 * vy**0.5)


def _safe_pct(v: float) -> float:
    try:
        return float(v) * 100.0
    except Exception:
        return 0.0


def _build_snapshot() -> dict[str, Any]:
    try:
        import yfinance as yf  # type: ignore
    except Exception as exc:
        return {"ok": False, "error": f"missing_dependency:{exc}", "rows": [], "asof": ""}

    symbols = sorted({a for _, a, _, _ in _PAIRS} | {b for _, _, _, b in _PAIRS})
    try:
        df = yf.download(
            tickers=symbols,
            period="13mo",
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=False,
        )
    except Exception as exc:
        return {"ok": False, "error": f"download_failed:{exc}", "rows": [], "asof": ""}

    if df is None or getattr(df, "empty", True):
        return {"ok": False, "error": "no_data", "rows": [], "asof": ""}

    close = df.get("Close")
    if close is None:
        return {"ok": False, "error": "close_missing", "rows": [], "asof": ""}

    rows: list[dict[str, Any]] = []
    for driver_name, driver_sym, target_name, target_sym in _PAIRS:
        try:
            sx = close[driver_sym].dropna()
            sy = close[target_sym].dropna()
            joined = sx.to_frame("x").join(sy.to_frame("y"), how="inner").dropna()
            if len(joined.index) < 30:
                continue
            rets = joined.pct_change().dropna()
            if len(rets.index) < 20:
                continue
            x = [float(v) for v in rets["x"].tolist()]
            y = [float(v) for v in rets["y"].tolist()]
            corr20 = _corr(x, y, 20)
            corr60 = _corr(x, y, 60)
            corr252 = _corr(x, y, 252)
            beta60 = _beta(x[-60:], y[-60:])
            x1 = float(x[-1])
            y1 = float(y[-1])
            expected = beta60 * x1
            gap = y1 - expected
            rows.append(
                {
                    "driver": driver_name,
                    "target": target_name,
                    "corr20": round(corr20, 3),
                    "corr60": round(corr60, 3),
                    "corr252": round(corr252, 3),
                    "beta60": round(beta60, 3),
                    "driver_ret_1d_pct": round(_safe_pct(x1), 2),
                    "target_ret_1d_pct": round(_safe_pct(y1), 2),
                    "expected_target_1d_pct": round(_safe_pct(expected), 2),
                    "gap_1d_pct": round(_safe_pct(gap), 2),
                }
            )
        except Exception:
            continue

    rows.sort(key=lambda r: abs(float(r.get("gap_1d_pct") or 0.0)), reverse=True)
    return {
        "ok": True,
        "asof": dt.datetime.now().isoformat(timespec="seconds"),
        "rows": rows,
        "pair_count": len(rows),
    }


def _save_to_pg(payload: dict[str, Any]) -> None:
    """Persist cross-asset snapshot to Postgres for Cloud Run fallback."""
    try:
        import json as _json

        from app.services.postgres_core_service import pg_connect

        con = pg_connect()
        if con is None:
            return
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cross_asset_snapshot_core (
                id BIGSERIAL PRIMARY KEY,
                payload_json TEXT NOT NULL DEFAULT '{}',
                fetched_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        now = dt.datetime.now().isoformat(timespec="seconds")
        cur.execute(
            "INSERT INTO cross_asset_snapshot_core (payload_json, fetched_at) VALUES (%s, %s)",
            (_json.dumps(payload), now),
        )
        # Keep only last 5 snapshots.
        cur.execute(
            "DELETE FROM cross_asset_snapshot_core WHERE id NOT IN (SELECT id FROM cross_asset_snapshot_core ORDER BY id DESC LIMIT 5)"
        )
        con.commit()
        con.close()
    except Exception:
        pass


def _load_from_pg() -> dict[str, Any] | None:
    """Load latest cross-asset snapshot from Postgres."""
    try:
        import json as _json

        from app.services.postgres_core_service import pg_connect

        con = pg_connect()
        if con is None:
            return None
        cur = con.cursor()
        cur.execute("SELECT payload_json FROM cross_asset_snapshot_core ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        con.close()
        if row and row[0]:
            return _json.loads(row[0])
        return None
    except Exception:
        return None


def save_cross_asset_snapshot_pg(payload: dict[str, Any]) -> None:
    """Public API for sync endpoint to push cross-asset data."""
    _save_to_pg(payload)


def get_cross_asset_snapshot(force_refresh: bool = False) -> dict[str, Any]:
    now = time.time()
    with _CACHE_LOCK:
        if not force_refresh:
            ts = float(_CACHE.get("ts") or 0.0)
            payload = _CACHE.get("payload")
            if payload is not None and (now - ts) <= _CACHE_TTL_SEC:
                return dict(payload)
    payload = _build_snapshot()
    if payload.get("ok") and payload.get("rows"):
        _save_to_pg(payload)
    elif not payload.get("ok") or not payload.get("rows"):
        # yfinance failed — try Postgres fallback.
        pg_payload = _load_from_pg()
        if pg_payload and pg_payload.get("rows"):
            pg_payload["source"] = "pg_cache"
            payload = pg_payload
    with _CACHE_LOCK:
        _CACHE["ts"] = now
        _CACHE["payload"] = dict(payload)
    return payload
