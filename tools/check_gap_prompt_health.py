#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CORE_DB = ROOT / "data" / "core.db"


def _core_backend() -> str:
    return str(os.getenv("CORE_DB_BACKEND", "sqlite")).strip().lower()


def _pg_dsn() -> str:
    return str(os.getenv("POSTGRES_DSN", "")).strip()


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))


def _sqlite_health() -> dict[str, Any]:
    out: dict[str, Any] = {"backend": "sqlite", "ok": False}
    if not CORE_DB.exists():
        out["error"] = f"missing_db:{CORE_DB}"
        return out
    con = sqlite3.connect(str(CORE_DB))
    try:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT "
            "SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_cnt, "
            "SUM(CASE WHEN status='answered' THEN 1 ELSE 0 END) AS answered_cnt, "
            "SUM(CASE WHEN status='expired' THEN 1 ELSE 0 END) AS expired_cnt, "
            "COUNT(*) AS total_cnt "
            "FROM daily_operator_gap_prompts"
        ).fetchone()
        latest = con.execute(
            "SELECT id, created_at, updated_at, prompt_id, status, length(answer_text) AS ans_len "
            "FROM daily_operator_gap_prompts ORDER BY id DESC LIMIT 5"
        ).fetchall()
        style = con.execute(
            "SELECT key, updated_at FROM investor_style_memory ORDER BY updated_at DESC LIMIT 10"
        ).fetchall()
        audit = con.execute(
            "SELECT reason, COUNT(*) AS cnt FROM daily_operator_gap_audit GROUP BY reason ORDER BY cnt DESC LIMIT 10"
        ).fetchall()
        out.update(
            {
                "ok": True,
                "counts": {
                    "open": int((row["open_cnt"] if row else 0) or 0),
                    "answered": int((row["answered_cnt"] if row else 0) or 0),
                    "expired": int((row["expired_cnt"] if row else 0) or 0),
                    "total": int((row["total_cnt"] if row else 0) or 0),
                },
                "latest_prompts": [dict(r) for r in latest],
                "recent_style_keys": [dict(r) for r in style],
                "audit_reasons": [dict(r) for r in audit],
            }
        )
        return out
    finally:
        con.close()


def _postgres_health() -> dict[str, Any]:
    out: dict[str, Any] = {"backend": "postgres", "ok": False}
    dsn = _pg_dsn()
    if not dsn:
        out["error"] = "missing_POSTGRES_DSN"
        return out
    try:
        import psycopg  # type: ignore

        con = psycopg.connect(dsn)
    except Exception:
        try:
            import psycopg2  # type: ignore

            con = psycopg2.connect(dsn)
        except Exception as e:
            out["error"] = f"pg_connect_failed:{type(e).__name__}"
            return out
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT "
            "SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open_cnt, "
            "SUM(CASE WHEN status='answered' THEN 1 ELSE 0 END) AS answered_cnt, "
            "SUM(CASE WHEN status='expired' THEN 1 ELSE 0 END) AS expired_cnt, "
            "COUNT(*) AS total_cnt "
            "FROM daily_operator_gap_prompts_core"
        )
        row = cur.fetchone() or (0, 0, 0, 0)
        cur.execute(
            "SELECT id, created_at, updated_at, prompt_id, status, length(answer_text) AS ans_len "
            "FROM daily_operator_gap_prompts_core ORDER BY id DESC LIMIT 5"
        )
        latest = cur.fetchall() or []
        cur.execute(
            "SELECT key, updated_at FROM investor_style_memory_core ORDER BY updated_at DESC LIMIT 10"
        )
        style = cur.fetchall() or []
        try:
            cur.execute(
                "SELECT reason, COUNT(*) AS cnt FROM daily_operator_gap_audit_core GROUP BY reason ORDER BY cnt DESC LIMIT 10"
            )
            audit = cur.fetchall() or []
        except Exception:
            audit = []
        out.update(
            {
                "ok": True,
                "counts": {
                    "open": int((row[0] or 0)),
                    "answered": int((row[1] or 0)),
                    "expired": int((row[2] or 0)),
                    "total": int((row[3] or 0)),
                },
                "latest_prompts": [
                    {
                        "id": int(r[0] or 0),
                        "created_at": str(r[1] or ""),
                        "updated_at": str(r[2] or ""),
                        "prompt_id": str(r[3] or ""),
                        "status": str(r[4] or ""),
                        "ans_len": int(r[5] or 0),
                    }
                    for r in latest
                ],
                "recent_style_keys": [{"key": str(r[0] or ""), "updated_at": str(r[1] or "")} for r in style],
                "audit_reasons": [{"reason": str(r[0] or ""), "cnt": int(r[1] or 0)} for r in audit],
            }
        )
        return out
    finally:
        try:
            con.close()
        except Exception:
            pass


def main() -> None:
    backend = _core_backend()
    if backend == "postgres":
        _print_json(_postgres_health())
        return
    _print_json(_sqlite_health())


if __name__ == "__main__":
    main()
