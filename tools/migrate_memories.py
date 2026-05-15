#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.memory_engine import OnyxMemory  # noqa: E402


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(r[1] or "").strip() for r in rows if str(r[1] or "").strip()}


def _pick_col(cols: set[str], candidates: list[str], default: str = "") -> str:
    for c in candidates:
        if c in cols:
            return c
    return default


def _iter_holdings(con: sqlite3.Connection):
    # Requested schema
    if _table_exists(con, "holdings"):
        cols = _table_columns(con, "holdings")
        text_col = _pick_col(cols, ["thesis", "content", "note", "notes", "summary"])
        date_col = _pick_col(cols, ["created_at", "date", "updated_at", "timestamp"])
        ticker_col = _pick_col(cols, ["ticker", "symbol"], "ticker")
        if text_col:
            date_expr = date_col if date_col else "''"
            sql = f"SELECT rowid, {ticker_col}, {text_col}, {date_expr} FROM holdings"
        else:
            sql = ""
        for r in con.execute(sql) if sql else []:
            yield {
                "source_type": "holding_thesis",
                "source_id": f"holdings:{int(r[0])}",
                "ticker": str(r[1] or "").strip().upper(),
                "text": str(r[2] or "").strip(),
                "created_at": str(r[3] or "").strip(),
            }
    # Existing schema compatibility
    if _table_exists(con, "stock_thesis"):
        cols = _table_columns(con, "stock_thesis")
        text_col = _pick_col(cols, ["thesis", "content", "note", "notes", "summary"])
        date_col = _pick_col(cols, ["created_at", "date", "updated_at", "timestamp"])
        ticker_col = _pick_col(cols, ["ticker", "symbol"], "ticker")
        id_col = _pick_col(cols, ["id", "rowid"], "id")
        if text_col:
            date_expr = date_col if date_col else "''"
            sql = f"SELECT {id_col}, {ticker_col}, {text_col}, {date_expr} FROM stock_thesis"
        else:
            sql = ""
        for r in con.execute(sql) if sql else []:
            yield {
                "source_type": "holding_thesis",
                "source_id": f"stock_thesis:{int(r[0])}",
                "ticker": str(r[1] or "").strip().upper(),
                "text": str(r[2] or "").strip(),
                "created_at": str(r[3] or "").strip(),
            }


def _iter_journal(con: sqlite3.Connection):
    # Requested schema
    if _table_exists(con, "journal"):
        cols = _table_columns(con, "journal")
        text_col = _pick_col(cols, ["content", "note", "notes", "text", "body"])
        date_col = _pick_col(cols, ["created_at", "date", "updated_at", "timestamp"])
        ticker_col = _pick_col(cols, ["ticker", "symbol"], "ticker")
        if text_col:
            date_expr = date_col if date_col else "''"
            sql = f"SELECT rowid, {ticker_col}, {text_col}, {date_expr} FROM journal"
        else:
            sql = ""
        for r in con.execute(sql) if sql else []:
            yield {
                "source_type": "journal",
                "source_id": f"journal:{int(r[0])}",
                "ticker": str(r[1] or "").strip().upper(),
                "text": str(r[2] or "").strip(),
                "created_at": str(r[3] or "").strip(),
            }
    # Existing schema compatibility
    if _table_exists(con, "workspace_journal"):
        cols = _table_columns(con, "workspace_journal")
        text_col = _pick_col(cols, ["note", "content", "text", "body"])
        date_col = _pick_col(cols, ["created_at", "date", "updated_at", "timestamp"])
        ticker_col = _pick_col(cols, ["ticker", "symbol"], "ticker")
        id_col = _pick_col(cols, ["id", "rowid"], "id")
        if text_col:
            date_expr = date_col if date_col else "''"
            sql = f"SELECT {id_col}, {ticker_col}, {text_col}, {date_expr} FROM workspace_journal"
        else:
            sql = ""
        for r in con.execute(sql) if sql else []:
            yield {
                "source_type": "journal",
                "source_id": f"workspace_journal:{int(r[0])}",
                "ticker": str(r[1] or "").strip().upper(),
                "text": str(r[2] or "").strip(),
                "created_at": str(r[3] or "").strip(),
            }
    if _table_exists(con, "investor_notes"):
        cols = _table_columns(con, "investor_notes")
        text_col = _pick_col(cols, ["note", "content", "text", "body"])
        date_col = _pick_col(cols, ["created_at", "date", "updated_at", "timestamp"])
        ticker_col = _pick_col(cols, ["ticker", "symbol"], "ticker")
        id_col = _pick_col(cols, ["id", "rowid"], "id")
        if text_col:
            date_expr = date_col if date_col else "''"
            sql = f"SELECT {id_col}, {ticker_col}, {text_col}, {date_expr} FROM investor_notes"
        else:
            sql = ""
        for r in con.execute(sql) if sql else []:
            yield {
                "source_type": "investor_note",
                "source_id": f"investor_notes:{int(r[0])}",
                "ticker": str(r[1] or "").strip().upper(),
                "text": str(r[2] or "").strip(),
                "created_at": str(r[3] or "").strip(),
            }


def main() -> int:
    ap = argparse.ArgumentParser(description="Migrate existing notes/thesis into Onyx local memory.")
    ap.add_argument("--db", default=str(ROOT / "investment_tool.db"), help="SQLite DB path")
    ap.add_argument("--memory-db", default=str(ROOT / "onyx_data" / "memory_db"), help="Chroma persistence dir")
    args = ap.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        fallback = (ROOT / "data" / "core.db").resolve()
        if fallback.exists():
            db_path = fallback
        else:
            print(f"Database not found: {db_path}")
            return 1

    mem = OnyxMemory(db_path=args.memory_db)
    if not mem.available:
        print("Memory engine unavailable. Install: pip install chromadb sentence-transformers")
        return 2

    con = sqlite3.connect(str(db_path))
    migrated = 0
    skipped = 0
    try:
        for row in list(_iter_holdings(con)) + list(_iter_journal(con)):
            txt = str(row.get("text") or "").strip()
            if not txt:
                skipped += 1
                continue
            meta = {
                "source_type": str(row.get("source_type") or ""),
                "source_id": str(row.get("source_id") or ""),
                "ticker": str(row.get("ticker") or ""),
            }
            created_at = str(row.get("created_at") or "").strip()
            if created_at:
                meta["created_at"] = created_at
                meta["timestamp"] = created_at
            migrated += mem.memorize(txt, meta)
    finally:
        con.close()

    print(f"Migrated {migrated} memories (skipped {skipped} empty rows) from {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
