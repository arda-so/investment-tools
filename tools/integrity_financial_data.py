#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
from typing import Any
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.company_file_service import list_companies, company_detail


def _days_old(asof_s: str) -> int | None:
    s = str(asof_s or "").strip()
    if not s:
        return None
    try:
        d = dt.date.fromisoformat(s[:10])
        return int((dt.date.today() - d).days)
    except Exception:
        return None


def main() -> int:
    data = list_companies(scope="all", page=1, page_size=3000)
    rows = list(data.get("rows") or [])
    tickers = [str(getattr(r, "ticker", "") or "").strip().upper() for r in rows if str(getattr(r, "ticker", "") or "").strip()]
    total = len(tickers)
    missing = 0
    stale = 0
    stale_cut = 3
    sample: list[dict[str, Any]] = []
    for tk in tickers:
        d = company_detail(tk)
        cov = dict(d.get("data_coverage") or {})
        if str(cov.get("state") or "") == "missing":
            missing += 1
            if len(sample) < 20:
                sample.append({"ticker": tk, "issue": "missing"})
        mini_days = _days_old(str(cov.get("mini_asof") or ""))
        intel_days = _days_old(str(cov.get("intel_asof") or ""))
        if (mini_days is not None and mini_days > stale_cut) or (intel_days is not None and intel_days > stale_cut):
            stale += 1
            if len(sample) < 20:
                sample.append({"ticker": tk, "issue": "stale", "mini_days": mini_days, "intel_days": intel_days})
    print(
        {
            "ok": True,
            "asof": dt.datetime.now().isoformat(),
            "total_tickers": total,
            "missing_coverage": missing,
            "stale_coverage": stale,
            "stale_threshold_days": stale_cut,
            "sample": sample,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
