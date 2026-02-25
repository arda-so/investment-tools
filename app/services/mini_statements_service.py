from __future__ import annotations

import datetime as dt
from typing import Any

from app.services.postgres_core_service import get_mini_statements_pg, upsert_mini_statements_pg

_CACHE_TTL_SEC = 300
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
SCHEMA_VERSION = "financials_schema_v1"


def _now_ts() -> float:
    return dt.datetime.now().timestamp()


def _to_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        x = float(v)
        if x != x:  # NaN guard
            return None
        return x
    except Exception:
        return None


def _norm_key(s: str) -> str:
    return "".join(ch.lower() for ch in str(s or "") if ch.isalnum())


def _pick_row(df: Any, candidates: list[str]) -> list[float | None]:
    idx: list[Any] = []
    try:
        if df is not None and hasattr(df, "index"):
            idx = list(df.index)
    except Exception:
        idx = []
    if not idx:
        return []
    target_keys = {_norm_key(x) for x in candidates}
    picked = None
    for name in idx:
        if _norm_key(str(name)) in target_keys:
            picked = name
            break
    if picked is None:
        return []
    try:
        row = df.loc[picked]
    except Exception:
        return []
    out: list[float | None] = []
    for v in list(row.values if hasattr(row, "values") else row):
        out.append(_to_float(v))
    return out


def _latest_years(df_like: Any, n: int = 5) -> list[int]:
    years: list[int] = []
    cols: list[Any] = []
    try:
        if df_like is not None and hasattr(df_like, "columns"):
            cols = list(df_like.columns)
    except Exception:
        cols = []
    for c in cols:
        y = None
        if hasattr(c, "year"):
            try:
                y = int(c.year)
            except Exception:
                y = None
        if y is None:
            txt = str(c or "")
            try:
                y = int(txt[:4])
            except Exception:
                y = None
        if y is not None:
            years.append(y)
    years = sorted(set(years), reverse=True)[: max(1, int(n or 5))]
    years = sorted(years)
    return years


def _row_map_to_years(df: Any, candidates: list[str], years: list[int]) -> list[float | None]:
    vals = _pick_row(df, candidates)
    if not years:
        return []
    cols: list[Any] = []
    try:
        if df is not None and hasattr(df, "columns"):
            cols = list(df.columns)
    except Exception:
        cols = []
    by_year: dict[int, float | None] = {}
    for i, c in enumerate(cols):
        y = None
        if hasattr(c, "year"):
            try:
                y = int(c.year)
            except Exception:
                y = None
        if y is None:
            try:
                y = int(str(c or "")[:4])
            except Exception:
                y = None
        if y is None:
            continue
        by_year[y] = vals[i] if i < len(vals) else None
    return [by_year.get(y) for y in years]


def get_mini_statements(ticker: str) -> dict[str, Any]:
    tk = str(ticker or "").strip().upper()
    if not tk:
        return {}
    hit = _CACHE.get(tk)
    now = _now_ts()
    if hit and now - hit[0] <= _CACHE_TTL_SEC:
        return dict(hit[1] or {})
    row = get_mini_statements_pg(tk)
    if row:
        _CACHE[tk] = (now, dict(row))
    return row or {}


def refresh_mini_statements(ticker: str) -> dict[str, Any]:
    tk = str(ticker or "").strip().upper()
    if not tk:
        return {}
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return {}
    try:
        obj = yf.Ticker(tk)
        fin = getattr(obj, "financials", None)
        cf = getattr(obj, "cashflow", None)
        bs = getattr(obj, "balance_sheet", None)
        base_df = None
        for candidate in (fin, cf, bs):
            if candidate is not None:
                base_df = candidate
                break
        years = _latest_years(base_df, n=5)
        if not years:
            return {}

        revenue = _row_map_to_years(fin, ["Total Revenue", "Revenue"], years)
        gross_profit = _row_map_to_years(fin, ["Gross Profit"], years)
        operating_cash_flow = _row_map_to_years(cf, ["Operating Cash Flow", "Total Cash From Operating Activities"], years)
        capex = _row_map_to_years(cf, ["Capital Expenditure", "Capital Expenditures"], years)
        total_cash = _row_map_to_years(bs, ["Cash And Cash Equivalents", "Cash And Short Term Investments", "Cash"], years)
        total_debt = _row_map_to_years(bs, ["Total Debt", "Long Term Debt", "Current Debt"], years)
        total_equity = _row_map_to_years(bs, ["Stockholders Equity", "Total Equity Gross Minority Interest", "Total Equity"], years)

        free_cash_flow: list[float | None] = []
        for ocf, cx in zip(operating_cash_flow, capex):
            if ocf is None:
                free_cash_flow.append(None)
                continue
            if cx is None:
                free_cash_flow.append(ocf)
                continue
            free_cash_flow.append(ocf + cx if cx < 0 else ocf - cx)

        asof = dt.date.today().isoformat()
        ok = upsert_mini_statements_pg(
            ticker=tk,
            asof=asof,
            source="yfinance",
            currency="USD",
            years=years,
            revenue=revenue,
            gross_profit=gross_profit,
            operating_cash_flow=operating_cash_flow,
            capex=capex,
            free_cash_flow=free_cash_flow,
            total_cash=total_cash,
            total_debt=total_debt,
            total_equity=total_equity,
        )
        if not ok:
            return {}
        row = get_mini_statements_pg(tk)
        if row:
            _CACHE[tk] = (_now_ts(), dict(row))
        return row or {}
    except Exception:
        return {}


def fetch_historical_financials(
    ticker: str,
    metric: str = "all",
    years: int = 5,
    refresh: bool = False,
) -> dict[str, Any]:
    tk = str(ticker or "").strip().upper()
    if not tk:
        return {"ok": False, "error": "ticker_required", "ticker": ""}
    m = str(metric or "all").strip().lower()
    try:
        yr_limit = max(1, min(10, int(years or 5)))
    except Exception:
        yr_limit = 5
    row = refresh_mini_statements(tk) if bool(refresh) else get_mini_statements(tk)
    if not row:
        row = refresh_mini_statements(tk)
    if not row:
        return {"ok": False, "error": "financials_unavailable", "ticker": tk}

    yrs = list(row.get("years") or [])
    if len(yrs) > yr_limit:
        yrs = yrs[-yr_limit:]

    def _slice(vals: Any) -> list[Any]:
        arr = list(vals or [])
        if len(arr) > yr_limit:
            return arr[-yr_limit:]
        return arr

    base = {
        "ok": True,
        "ticker": tk,
        "asof": str(row.get("asof") or ""),
        "source": str(row.get("source") or ""),
        "currency": str(row.get("currency") or "USD"),
        "years": yrs,
        "revenue": _slice(row.get("revenue")),
        "gross_profit": _slice(row.get("gross_profit")),
        "operating_cash_flow": _slice(row.get("operating_cash_flow")),
        "capex": _slice(row.get("capex")),
        "free_cash_flow": _slice(row.get("free_cash_flow")),
        "total_cash": _slice(row.get("total_cash")),
        "total_debt": _slice(row.get("total_debt")),
        "total_equity": _slice(row.get("total_equity")),
    }
    if m in {"all", "*"}:
        base["schema_version"] = SCHEMA_VERSION
        base["provenance"] = {
            "source": str(base.get("source") or ""),
            "asof": str(base.get("asof") or ""),
            "metric_sources": {
                "revenue": "income_statement",
                "gross_profit": "income_statement",
                "operating_cash_flow": "cashflow_statement",
                "capex": "cashflow_statement",
                "free_cash_flow": "derived:operating_cash_flow-capex",
                "total_cash": "balance_sheet",
                "total_debt": "balance_sheet",
                "total_equity": "balance_sheet",
            },
        }
        return base
    if m not in {
        "revenue",
        "gross_profit",
        "operating_cash_flow",
        "capex",
        "free_cash_flow",
        "total_cash",
        "total_debt",
        "total_equity",
    }:
        return {"ok": False, "error": "unsupported_metric", "ticker": tk, "metric": m}
    return {
        "ok": True,
        "ticker": tk,
        "asof": str(base.get("asof") or ""),
        "source": str(base.get("source") or ""),
        "currency": str(base.get("currency") or "USD"),
        "years": list(base.get("years") or []),
        "metric": m,
        "values": list(base.get(m) or []),
        "schema_version": SCHEMA_VERSION,
        "provenance": {
            "source": str(base.get("source") or ""),
            "asof": str(base.get("asof") or ""),
            "metric_source": m,
        },
    }


def compute_financial_deltas(ticker: str, years: int = 5) -> dict[str, Any]:
    base = fetch_historical_financials(ticker=ticker, metric="all", years=years, refresh=False)
    if not bool(base.get("ok")):
        return {"ok": False, "ticker": str(ticker or "").strip().upper(), "error": str(base.get("error") or "financials_unavailable")}
    ys = list(base.get("years") or [])
    if len(ys) < 2:
        return {"ok": False, "ticker": str(base.get("ticker") or "").strip().upper(), "error": "insufficient_periods"}

    def _safe(v: Any) -> float | None:
        try:
            x = float(v)
            return x if x == x else None
        except Exception:
            return None

    def _pair(arr: list[Any]) -> tuple[float | None, float | None]:
        if len(arr) < 2:
            return None, None
        return _safe(arr[-2]), _safe(arr[-1])

    def _delta(prev: float | None, cur: float | None) -> dict[str, Any]:
        if prev is None or cur is None:
            return {"abs": None, "pct": None}
        abs_v = cur - prev
        pct_v = (abs_v / abs(prev) * 100.0) if prev != 0 else None
        return {"abs": abs_v, "pct": pct_v}

    metrics = {}
    for k in ("revenue", "gross_profit", "operating_cash_flow", "free_cash_flow", "total_cash", "total_debt", "total_equity"):
        arr = list(base.get(k) or [])
        prev, cur = _pair(arr)
        metrics[k] = {
            "prev_year": ys[-2],
            "cur_year": ys[-1],
            "prev_value": prev,
            "cur_value": cur,
            "delta": _delta(prev, cur),
        }
    return {
        "ok": True,
        "ticker": str(base.get("ticker") or "").strip().upper(),
        "asof": str(base.get("asof") or ""),
        "source": str(base.get("source") or ""),
        "schema_version": SCHEMA_VERSION,
        "metrics": metrics,
    }
