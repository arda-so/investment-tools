#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.config import ROOT
from app.services.portfolio_memory_service import (
    compact_compact_memory,
    ingest_recent_report_facts,
    run_learning_cycle,
    save_morning_brief_snapshot,
)


def _append_log(line: str) -> None:
    log = ROOT / "logs" / "daily_catchup.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


def main() -> int:
    now = dt.datetime.now().isoformat(timespec="seconds")
    _append_log(f"=== daily_catchup {now} ===")

    out: dict[str, object] = {}
    try:
        facts = ingest_recent_report_facts(limit_reports=80, facts_per_report=14)
        out["report_facts"] = facts
        _append_log("report_facts=" + json.dumps(facts, ensure_ascii=True))
    except Exception as exc:
        out["report_facts_error"] = str(exc)
        _append_log("report_facts_error=" + str(exc))

    try:
        brief = save_morning_brief_snapshot(limit_holdings=8, source="daily_catchup")
        out["morning_brief_bullets"] = len(list((brief or {}).get("bullets") or []))
        _append_log("morning_brief_bullets=" + str(out["morning_brief_bullets"]))
    except Exception as exc:
        out["morning_brief_error"] = str(exc)
        _append_log("morning_brief_error=" + str(exc))

    try:
        comp = compact_compact_memory(max_keep_active=700, stale_days=120)
        out["memory_compaction"] = comp
        _append_log("memory_compaction=" + json.dumps(comp, ensure_ascii=True))
    except Exception as exc:
        out["memory_compaction_error"] = str(exc)
        _append_log("memory_compaction_error=" + str(exc))

    try:
        learn = run_learning_cycle()
        out["learning_cycle"] = learn
        _append_log("learning_cycle=" + json.dumps(learn, ensure_ascii=True))
    except Exception as exc:
        out["learning_cycle_error"] = str(exc)
        _append_log("learning_cycle_error=" + str(exc))

    _append_log("done")
    print(json.dumps({"ok": True, "at": now, "summary": out}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
