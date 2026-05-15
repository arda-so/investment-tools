#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.postgres_core_service import upsert_ir_source_registry_pg


def _to_bool(raw: str, default: bool = True) -> bool:
    s = str(raw or "").strip().lower()
    if not s:
        return bool(default)
    if s in {"1", "true", "yes", "on", "y"}:
        return True
    if s in {"0", "false", "no", "off", "n"}:
        return False
    return bool(default)


def _upsert_one(
    *,
    ticker: str,
    ir_home_url: str,
    provider: str,
    rss_url: str,
    last_good_audio_pattern: str = "",
    last_good_event_url: str = "",
    active: bool = True,
    meta: dict[str, Any] | None = None,
) -> bool:
    return upsert_ir_source_registry_pg(
        ticker=ticker,
        ir_home_url=ir_home_url,
        provider=provider,
        rss_url=rss_url,
        last_good_audio_pattern=last_good_audio_pattern,
        last_good_event_url=last_good_event_url,
        active=active,
        meta=meta or {},
    )


def _run_single(args: argparse.Namespace) -> int:
    ok = _upsert_one(
        ticker=str(args.ticker or "").strip().upper(),
        ir_home_url=str(args.ir_home_url or "").strip(),
        provider=str(args.provider or "").strip(),
        rss_url=str(args.rss_url or "").strip(),
        last_good_audio_pattern=str(args.last_good_audio_pattern or "").strip(),
        last_good_event_url=str(args.last_good_event_url or "").strip(),
        active=bool(args.active),
        meta={"source": "cli_single"},
    )
    print(json.dumps({"ok": bool(ok), "mode": "single", "ticker": str(args.ticker or "").strip().upper()}))
    return 0 if ok else 2


def _run_csv(args: argparse.Namespace) -> int:
    p = Path(str(args.csv or "")).expanduser().resolve()
    if not p.exists():
        print(json.dumps({"ok": False, "error": "csv_not_found", "path": str(p)}))
        return 2
    rows_ok = 0
    rows_fail = 0
    with p.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            tk = str((r or {}).get("ticker") or "").strip().upper()
            if not tk:
                rows_fail += 1
                continue
            ir_home_url = str((r or {}).get("ir_home_url") or "").strip()
            rss_url = str((r or {}).get("rss_url") or "").strip()
            provider = str((r or {}).get("provider") or "").strip()
            last_good_audio_pattern = str((r or {}).get("last_good_audio_pattern") or "").strip()
            last_good_event_url = str((r or {}).get("last_good_event_url") or "").strip()
            active = _to_bool(str((r or {}).get("active") or "1"), default=True)
            meta = {"source": "csv_bulk", "csv_file": str(p.name)}
            ok = _upsert_one(
                ticker=tk,
                ir_home_url=ir_home_url,
                provider=provider,
                rss_url=rss_url,
                last_good_audio_pattern=last_good_audio_pattern,
                last_good_event_url=last_good_event_url,
                active=active,
                meta=meta,
            )
            if ok:
                rows_ok += 1
            else:
                rows_fail += 1
    print(json.dumps({"ok": rows_fail == 0 and rows_ok > 0, "mode": "csv", "rows_ok": rows_ok, "rows_fail": rows_fail, "path": str(p)}))
    return 0 if rows_fail == 0 and rows_ok > 0 else 2


def main() -> int:
    p = argparse.ArgumentParser(description="Upsert IR source registry rows.")
    p.add_argument("--ticker", default="", help="Ticker symbol for single upsert mode.")
    p.add_argument("--ir-home-url", default="", help="Investor relations homepage URL.")
    p.add_argument("--rss-url", default="", help="IR RSS feed URL.")
    p.add_argument("--provider", default="", help="IR provider label (e.g. q4, notified, intrado).")
    p.add_argument("--last-good-audio-pattern", default="", help="Last known working audio URL pattern.")
    p.add_argument("--last-good-event-url", default="", help="Last known working event page URL.")
    p.add_argument("--active", action="store_true", default=True, help="Mark row active (default true).")
    p.add_argument("--inactive", action="store_true", help="Mark row inactive.")
    p.add_argument("--csv", default="", help="CSV path for bulk upsert mode.")
    args = p.parse_args()

    if args.inactive:
        args.active = False

    if str(args.csv or "").strip():
        return _run_csv(args)

    if not str(args.ticker or "").strip():
        print(json.dumps({"ok": False, "error": "ticker_required_or_use_csv"}))
        return 2
    return _run_single(args)


if __name__ == "__main__":
    raise SystemExit(main())

