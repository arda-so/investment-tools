from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading

from app.core.ticker import normalize_ticker
from app.core.ticker import yfinance_symbol
from app.core.config import DATA_DIR
from app.services.postgres_core_service import core_backend, pg_connect

_MCAP_CACHE_PATH = Path(DATA_DIR) / "cache" / "market_cap_cache.json"
_MCAP_TTL_HOURS = 24
_MCAP_FETCH_BATCH_MAX = 80
_MCAP_PREFETCH_LOCK = threading.Lock()
_MCAP_PREFETCH_INFLIGHT: set[str] = set()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _format_market_cap(value: float) -> str:
    try:
        v = float(value or 0.0)
    except Exception:
        return "-"
    if v <= 0:
        return "-"
    abs_v = abs(v)
    if abs_v >= 1_000_000_000_000:
        return f"${v / 1_000_000_000_000:.2f}T"
    if abs_v >= 1_000_000_000:
        return f"${v / 1_000_000_000:.2f}B"
    if abs_v >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if abs_v >= 1_000:
        return f"${v / 1_000:.2f}K"
    return f"${v:,.0f}"


def _parse_market_cap(raw: object) -> float:
    s = str(raw or "").strip().upper().replace("$", "").replace(",", "")
    if not s or s == "-":
        return 0.0
    mul = 1.0
    if s.endswith("T"):
        mul = 1e12
        s = s[:-1]
    elif s.endswith("B"):
        mul = 1e9
        s = s[:-1]
    elif s.endswith("M"):
        mul = 1e6
        s = s[:-1]
    elif s.endswith("K"):
        mul = 1e3
        s = s[:-1]
    try:
        return float(s) * mul
    except Exception:
        return 0.0


def _load_market_cap_cache() -> dict[str, dict[str, object]]:
    try:
        if not _MCAP_CACHE_PATH.exists():
            return {}
        raw = json.loads(_MCAP_CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict[str, object]] = {}
        for k, v in raw.items():
            tk = normalize_ticker(k)
            if not tk or not isinstance(v, dict):
                continue
            out[tk] = v
        return out
    except Exception:
        return {}


def _save_market_cap_cache(data: dict[str, dict[str, object]]) -> None:
    try:
        _MCAP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _MCAP_CACHE_PATH.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
    except Exception:
        return


def _save_market_caps_pg(tk_vals: dict[str, int]) -> None:
    """Persist fetched market caps to Postgres so they survive container restarts."""
    if not tk_vals:
        return
    try:
        from app.services.postgres_core_service import pg_connect  # noqa: PLC0415
        con = pg_connect()
        if con is None:
            return
        try:
            cur = con.cursor()
            for tk, val in tk_vals.items():
                if not tk or val <= 0:
                    continue
                cur.execute(
                    """
                    INSERT INTO company_profile_cache_core(ticker, market_cap, updated_at)
                    VALUES (%s, %s, NOW()::text)
                    ON CONFLICT(ticker) DO UPDATE SET market_cap = EXCLUDED.market_cap
                    """,
                    (tk, int(val)),
                )
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def _cache_value_valid(item: dict[str, object]) -> float:
    try:
        mcap = float(item.get("market_cap") or 0.0)
    except Exception:
        return 0.0
    if mcap <= 0:
        return 0.0
    asof = str(item.get("asof") or "").strip()
    if not asof:
        return 0.0
    try:
        ts = datetime.fromisoformat(asof.replace("Z", "+00:00"))
    except Exception:
        return 0.0
    if _now_utc() - ts > timedelta(hours=_MCAP_TTL_HOURS):
        return 0.0
    return mcap


def _fetch_market_cap_live(ticker: str) -> tuple[str, float]:
    tk = normalize_ticker(ticker)
    if not tk:
        return "", 0.0
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return tk, 0.0
    try:
        obj = yf.Ticker(yfinance_symbol(tk))
        fi = obj.fast_info or {}
        mcap = float(fi.get("market_cap") or 0.0)
        if mcap <= 0:
            info = obj.info or {}
            mcap = float(info.get("marketCap") or 0.0)
        return tk, (mcap if mcap > 0 else 0.0)
    except Exception:
        return tk, 0.0


def company_name_map(tickers: list[str] | None = None) -> dict[str, str]:
    out: dict[str, str] = {}
    if core_backend() != "postgres":
        return out
    con_pg = pg_connect()
    if con_pg is None:
        return out
    try:
        cur = con_pg.cursor()
        cur.execute("SELECT to_regclass('public.company_profile_cache_core')")
        exists = cur.fetchone()
        if not exists or not exists[0]:
            return out
        wanted = sorted({normalize_ticker(t) for t in (tickers or []) if normalize_ticker(t)})
        if wanted:
            marks = ",".join("%s" for _ in wanted)
            cur.execute(
                f"SELECT UPPER(ticker) AS ticker, name FROM company_profile_cache_core WHERE UPPER(ticker) IN ({marks})",
                tuple(wanted),
            )
        else:
            cur.execute("SELECT ticker, name FROM company_profile_cache_core")
        for r in cur.fetchall() or []:
            tk = normalize_ticker(str(r[0] or ""))
            nm = str(r[1] or "").strip()
            if tk:
                out[tk] = nm
    except Exception:
        return {}
    finally:
        con_pg.close()
    return out


def market_cap_map(tickers: list[str] | None = None, *, live_fetch: bool = True) -> dict[str, str]:
    wanted = [normalize_ticker(t) for t in (tickers or []) if normalize_ticker(t)]
    if not wanted:
        return {}
    wanted = sorted(set(wanted))
    out = {t: "-" for t in wanted}

    if core_backend() != "postgres":
        cache = _load_market_cap_cache()
        for tk in wanted:
            cached = _cache_value_valid(cache.get(tk, {}))
            if cached > 0:
                out[tk] = _format_market_cap(cached)
        return out

    con_pg = pg_connect()
    if con_pg is not None:
        try:
            cur = con_pg.cursor()
            cur.execute("SELECT to_regclass('public.company_profile_cache_core')")
            exists = cur.fetchone()
            if exists and exists[0]:
                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema='public' AND table_name='company_profile_cache_core'
                    """
                )
                cols = {str(r[0] or "").strip().lower() for r in (cur.fetchall() or [])}
                if "market_cap" in cols:
                    marks = ",".join("%s" for _ in wanted)
                    cur.execute(
                        f"SELECT UPPER(ticker), market_cap FROM company_profile_cache_core WHERE UPPER(ticker) IN ({marks})",
                        tuple(wanted),
                    )
                    for r in cur.fetchall() or []:
                        tk = normalize_ticker(str(r[0] or ""))
                        val = _parse_market_cap(r[1])
                        if tk and val > 0:
                            out[tk] = _format_market_cap(val)
        except Exception:
            pass
        finally:
            con_pg.close()

    cache = _load_market_cap_cache()
    for tk in wanted:
        if out.get(tk) != "-":
            continue
        cached = _cache_value_valid(cache.get(tk, {}))
        if cached > 0:
            out[tk] = _format_market_cap(cached)

    if not bool(live_fetch):
        return out

    missing = [tk for tk in wanted if out.get(tk) == "-"][:_MCAP_FETCH_BATCH_MAX]
    if not missing:
        return out

    fetched_any = False
    pg_vals: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=min(12, len(missing))) as ex:
        for tk, val in ex.map(_fetch_market_cap_live, missing):
            if not tk or val <= 0:
                continue
            out[tk] = _format_market_cap(val)
            cache[tk] = {"market_cap": float(val), "asof": _now_utc().isoformat()}
            pg_vals[tk] = int(val)
            fetched_any = True
    if fetched_any:
        _save_market_cap_cache(cache)
        _save_market_caps_pg(pg_vals)
    return out


def prefetch_market_cap_async(tickers: list[str] | None = None, *, limit: int = 80) -> None:
    wanted = [normalize_ticker(t) for t in (tickers or []) if normalize_ticker(t)]
    if not wanted:
        return
    wanted = sorted(set(wanted))[: max(1, min(200, int(limit or 80)))]
    cache = _load_market_cap_cache()
    missing: list[str] = []
    for tk in wanted:
        if _cache_value_valid(cache.get(tk, {})) > 0:
            continue
        missing.append(tk)
    if not missing:
        return

    with _MCAP_PREFETCH_LOCK:
        run_list = [tk for tk in missing if tk not in _MCAP_PREFETCH_INFLIGHT][: _MCAP_FETCH_BATCH_MAX]
        for tk in run_list:
            _MCAP_PREFETCH_INFLIGHT.add(tk)
    if not run_list:
        return

    def _worker(batch: list[str]) -> None:
        local_cache = _load_market_cap_cache()
        updated = False
        pg_vals: dict[str, int] = {}
        try:
            with ThreadPoolExecutor(max_workers=min(10, len(batch))) as ex:
                for tk, val in ex.map(_fetch_market_cap_live, batch):
                    if not tk or val <= 0:
                        continue
                    local_cache[tk] = {"market_cap": float(val), "asof": _now_utc().isoformat()}
                    pg_vals[tk] = int(val)
                    updated = True
            if updated:
                _save_market_cap_cache(local_cache)
                _save_market_caps_pg(pg_vals)
        finally:
            with _MCAP_PREFETCH_LOCK:
                for tk in batch:
                    _MCAP_PREFETCH_INFLIGHT.discard(tk)

    th = threading.Thread(target=_worker, args=(run_list,), daemon=True)
    th.start()
