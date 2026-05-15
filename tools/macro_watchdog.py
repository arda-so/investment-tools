#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
INPUTS = REPORTS / ".terminal_inputs"
DATA = ROOT / "data"

BLS_API = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
BLS_RSS = "https://www.bls.gov/feed/bls_latest.rss"


def _safe_float(v: Any) -> float | None:
    try:
        return float(v)
    except Exception:
        return None


def _bls_series(series_id: str, years: int = 2, timeout: int = 15) -> list[dict[str, Any]]:
    payload = {"seriesid": [series_id], "startyear": str(dt.date.today().year - years), "endyear": str(dt.date.today().year)}
    try:
        r = requests.post(BLS_API, json=payload, timeout=timeout)
        r.raise_for_status()
        obj = r.json() or {}
        series = (((obj.get("Results") or {}).get("series") or [{}])[0]).get("data") or []
        out: list[dict[str, Any]] = []
        for row in series:
            period = str(row.get("period") or "")
            if not period.startswith("M"):
                continue
            m = int(period[1:])
            y = int(row.get("year"))
            d = dt.date(y, m, 1)
            out.append({"date": d.isoformat(), "value": _safe_float(row.get("value"))})
        out.sort(key=lambda x: str(x["date"]), reverse=True)
        return out
    except Exception:
        return []


def _pct(new: float | None, old: float | None) -> float | None:
    if new is None or old in (None, 0.0):
        return None
    return ((new / old) - 1.0) * 100.0


def _load_consensus(month_ym: str) -> dict[str, float]:
    """
    Optional consensus file:
      data/cpi_consensus.json
    Example:
    {
      "2026-02": {
        "all_items_mom_consensus": 0.30,
        "core_mom_consensus": 0.30
      }
    }
    """
    p = DATA / "cpi_consensus.json"
    if not p.exists():
        return {}
    try:
        obj = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        row = (obj or {}).get(month_ym) or {}
        out: dict[str, float] = {}
        for k in ("all_items_mom_consensus", "core_mom_consensus"):
            v = _safe_float((row or {}).get(k))
            if v is not None:
                out[k] = v
        return out
    except Exception:
        return {}


def _bls_cpi_release_today(timeout: int = 10) -> bool:
    today = dt.date.today().strftime("%Y-%m-%d")
    try:
        txt = requests.get(BLS_RSS, timeout=timeout).text
        low = txt.lower()
        # BLS RSS usually includes "Consumer Price Index" in title/description.
        if "consumer price index" not in low and "cpi" not in low:
            return False
        # Conservative check: if feed contains today's date and CPI mention.
        return today in txt and ("consumer price index" in low or "cpi" in low)
    except Exception:
        return False


def build_macro_watchdog_payload() -> dict[str, Any]:
    all_items = _bls_series("CUSR0000SA0", years=3)      # CPI All items
    core_items = _bls_series("CUSR0000SA0L1E", years=3)  # CPI less food & energy

    all_latest = all_items[0]["value"] if all_items else None
    all_prev = all_items[1]["value"] if len(all_items) > 1 else None
    all_yago = all_items[12]["value"] if len(all_items) > 12 else None

    core_latest = core_items[0]["value"] if core_items else None
    core_prev = core_items[1]["value"] if len(core_items) > 1 else None
    core_yago = core_items[12]["value"] if len(core_items) > 12 else None

    cpi_today = _bls_cpi_release_today()
    now_utc = dt.datetime.now(dt.timezone.utc)
    asof_utc = now_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
    freshness_minutes = 90 if cpi_today else 240

    month_ym = all_items[0]["date"][:7] if all_items else "-"
    consensus = _load_consensus(month_ym if month_ym != "-" else "")
    all_mom = _pct(all_latest, all_prev)
    core_mom = _pct(core_latest, core_prev)
    all_mom_prior = _pct(all_prev, all_items[2]["value"] if len(all_items) > 2 else None)
    core_mom_prior = _pct(core_prev, core_items[2]["value"] if len(core_items) > 2 else None)
    all_cons = _safe_float(consensus.get("all_items_mom_consensus"))
    core_cons = _safe_float(consensus.get("core_mom_consensus"))

    def _delta(a: float | None, b: float | None) -> float | None:
        if a is None or b is None:
            return None
        return a - b

    return {
        "asof_utc": asof_utc,
        "cpi_release_today": bool(cpi_today),
        "freshness_minutes": freshness_minutes,
        "cpi_surprise": {
            "month": month_ym,
            "all_items_mom_actual": all_mom,
            "all_items_mom_prior": all_mom_prior,
            "all_items_mom_consensus": all_cons,
            "all_items_mom_vs_consensus": _delta(all_mom, all_cons),
            "core_mom_actual": core_mom,
            "core_mom_prior": core_mom_prior,
            "core_mom_consensus": core_cons,
            "core_mom_vs_consensus": _delta(core_mom, core_cons),
        },
        "series": {
            "all_items": {
                "latest_month": month_ym,
                "mom_pct": all_mom,
                "yoy_pct": _pct(all_latest, all_yago),
            },
            "core": {
                "latest_month": core_items[0]["date"][:7] if core_items else "-",
                "mom_pct": _pct(core_latest, core_prev),
                "yoy_pct": _pct(core_latest, core_yago),
            },
        },
        "headline": (
            "CPI release day detected; enforce intraday macro refresh windows."
            if cpi_today
            else "No CPI release flag today; normal macro cadence."
        ),
        "source": "BLS API + BLS RSS",
    }


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:+.2f}%"


def write_outputs(payload: dict[str, Any]) -> tuple[Path, Path]:
    INPUTS.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    jpath = INPUTS / "macro_watchdog_latest.json"
    mpath = INPUTS / "macro_watchdog_latest.md"
    jpath.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    s = payload.get("series") or {}
    a = (s.get("all_items") or {})
    c = (s.get("core") or {})
    md = [
        f"# Macro Watchdog ({payload.get('asof_utc', '-')})",
        "",
        f"- Headline: {payload.get('headline', '-')}",
        f"- CPI release today: {'yes' if payload.get('cpi_release_today') else 'no'}",
        f"- Freshness SLA: {payload.get('freshness_minutes', '-')} minutes",
        "",
        "## CPI Snapshot (BLS)",
        f"- All-items MoM: {_fmt_pct(_safe_float(a.get('mom_pct')))} | YoY: {_fmt_pct(_safe_float(a.get('yoy_pct')))} | Month: {a.get('latest_month', '-')}",
        f"- Core CPI MoM: {_fmt_pct(_safe_float(c.get('mom_pct')))} | YoY: {_fmt_pct(_safe_float(c.get('yoy_pct')))} | Month: {c.get('latest_month', '-')}",
    ]
    sup = payload.get("cpi_surprise") or {}
    if isinstance(sup, dict):
        def _fmt(v: Any) -> str:
            f = _safe_float(v)
            return _fmt_pct(f)
        md.extend(
            [
                "",
                "## CPI Surprise",
                f"- All-items MoM actual {_fmt(sup.get('all_items_mom_actual'))} | prior {_fmt(sup.get('all_items_mom_prior'))} | consensus {_fmt(sup.get('all_items_mom_consensus'))} | vs consensus {_fmt(sup.get('all_items_mom_vs_consensus'))}",
                f"- Core MoM actual {_fmt(sup.get('core_mom_actual'))} | prior {_fmt(sup.get('core_mom_prior'))} | consensus {_fmt(sup.get('core_mom_consensus'))} | vs consensus {_fmt(sup.get('core_mom_vs_consensus'))}",
            ]
        )
    mpath.write_text("\n".join(md) + "\n", encoding="utf-8")
    return jpath, mpath


def main() -> None:
    p = argparse.ArgumentParser(description="Macro watchdog for CPI/event-day freshness.")
    p.add_argument("--json", action="store_true", help="Print payload JSON to stdout.")
    p.add_argument("--write", action="store_true", help="Write outputs to reports/.terminal_inputs.")
    args = p.parse_args()

    payload = build_macro_watchdog_payload()
    if args.write:
        jpath, mpath = write_outputs(payload)
        print(f"Wrote: {jpath}")
        print(f"Wrote: {mpath}")
    if args.json or not args.write:
        print(json.dumps(payload, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
