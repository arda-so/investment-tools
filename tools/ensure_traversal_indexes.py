#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.postgres_core_service import core_backend, pg_connect
from app.services.sec_ingest_pipeline_service import ensure_sec_ingest_schema


TARGET_INDEXES = [
    "idx_rel_core_active_source_conf",
    "idx_rel_core_active_target_conf",
    "idx_rel_core_status_valid_to",
    "idx_rel_core_status_effective_conf",
]


def main() -> int:
    ensure_sec_ingest_schema()
    if core_backend() != "postgres":
        print(json.dumps({"ok": False, "error": "postgres_required"}, ensure_ascii=True, indent=2))
        return 1
    con = pg_connect()
    if con is None:
        print(json.dumps({"ok": False, "error": "postgres_unavailable"}, ensure_ascii=True, indent=2))
        return 1
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname='public'
              AND tablename='relationships_core'
            """
        )
        names = {str(r[0] or "").strip() for r in (cur.fetchall() or [])}
        out = {
            "ok": True,
            "backend": "postgres",
            "table": "relationships_core",
            "present_indexes": sorted([x for x in names if x]),
            "required_indexes": TARGET_INDEXES,
            "all_required_present": all(ix in names for ix in TARGET_INDEXES),
        }
        print(json.dumps(out, ensure_ascii=True, indent=2))
        return 0 if bool(out["all_required_present"]) else 2
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
