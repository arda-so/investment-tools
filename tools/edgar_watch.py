#!/usr/bin/env python3
"""
EDGAR Watch Job — runs every 15 minutes via Cloud Run.

Polls held/watchlist tickers for new SEC filings and fires run_event_driven_monitor()
if any new filings are ingested. Closes the ~6h detection lag to ~15 minutes.

The nightly investor-agent-worker (02:30 UTC) continues to handle deep debate-mode
analysis — this script only handles detection + quick proposal generation.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Set Postgres mode before importing any app code.
os.environ.setdefault("CORE_DB_BACKEND", "postgres")
os.environ.setdefault("CORE_DB_GUARD_ENFORCE", "1")
os.environ.setdefault("CORE_DB_STRICT_POSTGRES", "1")
os.environ.setdefault("PHASE2_POSTGRES_ENABLED", "1")
os.environ.setdefault("AI_QUEUE_BACKEND", "postgres")
os.environ.setdefault("AI_QUEUE_STRICT_PROD", "1")

from app.services.sec_edgar_poller_service import poll_and_ingest_tickers
from app.services.proactive_ai_service import run_event_driven_monitor


def main() -> int:
    print("[edgar-watch] Starting — polling held/watchlist tickers for new SEC filings…", flush=True)

    result = poll_and_ingest_tickers(held_only=True)
    new_count = int(result.get("new_filings") or 0)
    held_checked = int(result.get("held_checked") or result.get("tickers_checked") or 0)

    print(
        f"[edgar-watch] Ingested {new_count} new filing(s) across {held_checked} held ticker(s). "
        f"Result: {json.dumps(result)}",
        flush=True,
    )

    if new_count > 0:
        print("[edgar-watch] New filings detected — running event-driven monitor…", flush=True)
        monitor_result = run_event_driven_monitor(force=True)
        print(f"[edgar-watch] Monitor result: {json.dumps(monitor_result)}", flush=True)
    else:
        print("[edgar-watch] No new filings — skipping monitor run.", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
