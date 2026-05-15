#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [str(r[1]) for r in rows]


def _all_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    return [str(r[0]) for r in rows]


def _audit_db(db_path: Path, spec: dict[str, dict[str, object]]) -> tuple[bool, list[str]]:
    lines: list[str] = []
    ok = True

    if not db_path.exists():
        return False, [f"[FAIL] DB missing: {db_path}"]

    conn = sqlite3.connect(str(db_path))
    try:
        tables = set(_all_tables(conn))
        lines.append(f"DB: {db_path}")
        lines.append(f"Tables found: {len(tables)}")

        for table, rules in spec.items():
            if table not in tables:
                ok = False
                lines.append(f"  [FAIL] Missing table: {table}")
                continue

            cols = _table_columns(conn, table)
            cset = set(cols)
            lines.append(f"  [OK] Table: {table} ({len(cols)} cols)")

            required = set(rules.get("required", []))
            missing = sorted(required - cset)
            if missing:
                ok = False
                lines.append(f"    [FAIL] Missing required columns: {', '.join(missing)}")

            any_of = list(rules.get("required_any_of", []))
            for group in any_of:
                g = [str(x) for x in group]
                if not any(x in cset for x in g):
                    ok = False
                    lines.append(f"    [FAIL] Missing one-of columns: {' | '.join(g)}")
                else:
                    got = [x for x in g if x in cset]
                    lines.append(f"    [OK] One-of satisfied: {' | '.join(g)} -> {', '.join(got)}")

            optional = set(rules.get("optional", []))
            extras = sorted(cset - required - optional)
            # Extras are informational; not failures.
            if extras:
                lines.append(f"    [INFO] Extra columns: {', '.join(extras)}")

            if table == "signals":
                if "created_at" not in cset and "timestamp" in cset:
                    lines.append("    [NOTE] signals uses 'timestamp' instead of 'created_at' (compatible).")
                    lines.append(
                        "    [HINT] If you want both, run: "
                        "ALTER TABLE signals ADD COLUMN created_at TEXT DEFAULT CURRENT_TIMESTAMP;"
                    )
    finally:
        conn.close()

    return ok, lines


def main() -> int:
    p = argparse.ArgumentParser(description="Audit SQLite table/column schema for Investment_Tools.")
    p.add_argument(
        "--db",
        action="append",
        default=[],
        help="Explicit DB path(s). If omitted, audits known app DBs.",
    )
    args = p.parse_args()

    known = [
        ROOT / "onyx_brain.db",
        ROOT / "data" / "research.db",
    ]
    dbs = [Path(x).expanduser().resolve() for x in args.db] if args.db else known

    specs: dict[str, dict[str, dict[str, object]]] = {
        str((ROOT / "onyx_brain.db").resolve()): {
            "holdings": {
                "required": ["ticker", "shares", "avg_cost", "thesis_text", "status"],
                "optional": ["id"],
            },
            "evidence": {
                "required": ["id", "ticker", "type", "content", "date", "source_url"],
                "optional": ["evidence_hash"],
            },
            "signals": {
                "required": ["id", "ticker", "status", "reasoning"],
                "required_any_of": [["timestamp", "created_at"]],
                "optional": ["confidence", "contradiction_score", "evidence_hash", "evidence_summary", "timestamp", "created_at"],
            },
            "signal_audit": {
                "required": ["id", "ticker", "model", "prompt", "response", "created_at"],
            },
        },
        str((ROOT / "data" / "research.db").resolve()): {
            "investor_notes": {
                "required": ["id", "scope", "ticker", "sentiment", "note", "tags", "created_at"],
            },
            "stock_thesis": {
                "required": ["id", "ticker", "sentiment", "content", "date"],
            },
            "todos": {
                "required": ["id", "task", "status", "created_at", "priority", "due_date"],
            },
            "scratchpad": {
                "required": ["id", "content", "last_updated", "pinned"],
            },
            "l2_runs": {
                "required": ["id", "run_ts", "scope", "summary"],
            },
            "l2_insights": {
                "required": [
                    "id",
                    "run_id",
                    "ticker",
                    "support_score",
                    "conflict_score",
                    "novelty_score",
                    "urgency_score",
                    "total_score",
                    "priority",
                    "evidence_json",
                    "created_at",
                ],
            },
        },
    }

    overall_ok = True
    out: list[str] = []
    for db in dbs:
        spec = specs.get(str(db.resolve()), {})
        if not spec:
            # Generic mode: only print discovered tables if DB is unknown.
            if not db.exists():
                overall_ok = False
                out.append(f"[FAIL] DB missing: {db}")
                continue
            conn = sqlite3.connect(str(db))
            try:
                tabs = _all_tables(conn)
                out.append(f"DB: {db}")
                out.append(f"Tables found ({len(tabs)}): {', '.join(tabs)}")
                out.append("[INFO] No expected-schema spec for this DB.")
            finally:
                conn.close()
            continue

        ok, lines = _audit_db(db, spec)
        overall_ok = overall_ok and ok
        out.extend(lines)
        out.append("")

    print("\n".join(out).rstrip())
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

