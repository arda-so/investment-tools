from __future__ import annotations

import datetime as dt
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
import csv

from app.core.config import CORE_DB_PATH
from app.core.config import ROOT
from app.core.sqlite_hardening import connect_sqlite
from app.services.postgres_core_service import (
    add_investor_note_pg,
    add_todo_pg,
    core_backend,
    delete_todo_pg,
    list_recent_notes_pg,
    list_todos_pg,
    toggle_todo_pg,
    update_todo_status_pg,
)

ENTITY_ALIASES = {
    "google": "GOOGL",
    "alphabet": "GOOGL",
    "facebook": "META",
    "meta": "META",
    "tesla": "TSLA",
    "amazon": "AMZN",
    "apple": "AAPL",
    "microsoft": "MSFT",
    "netflix": "NFLX",
    "nvidia": "NVDA",
}
INVALID_TICKER_WORDS = {
    "BUY", "SELL", "HOLD", "WATCH", "READ", "WRITE", "ADD", "NOTE", "TASK",
    "OPEN", "SHOW", "MY", "ME", "LIST", "UPDATE", "DELETE", "REPORT",
}


def _conn() -> sqlite3.Connection:
    return connect_sqlite(str(CORE_DB_PATH), row_factory=True)


def _has_column(con: sqlite3.Connection, table: str, column: str) -> bool:
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return any(str(r["name"] or "") == column for r in rows)


def ensure_schema() -> None:
    con = _conn()
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS daily_note_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT NOT NULL,
                ticker TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_daily_note_tags_day ON daily_note_tags(day)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_daily_note_tags_ticker ON daily_note_tags(ticker)")
        if not _has_column(con, "daily_notes", "locked"):
            con.execute("ALTER TABLE daily_notes ADD COLUMN locked INTEGER NOT NULL DEFAULT 0")
        if not _has_column(con, "daily_notes", "archived_at"):
            con.execute("ALTER TABLE daily_notes ADD COLUMN archived_at TEXT NOT NULL DEFAULT ''")
        if not _has_column(con, "investor_notes", "status"):
            con.execute("ALTER TABLE investor_notes ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'")
        if not _has_column(con, "investor_notes", "created_by"):
            con.execute("ALTER TABLE investor_notes ADD COLUMN created_by TEXT NOT NULL DEFAULT 'human'")
        if not _has_column(con, "investor_notes", "ai_confidence"):
            con.execute("ALTER TABLE investor_notes ADD COLUMN ai_confidence REAL NOT NULL DEFAULT 0")
        if not _has_column(con, "investor_notes", "ai_reasoning"):
            con.execute("ALTER TABLE investor_notes ADD COLUMN ai_reasoning TEXT NOT NULL DEFAULT ''")
        if not _has_column(con, "investor_notes", "trace_id"):
            con.execute("ALTER TABLE investor_notes ADD COLUMN trace_id TEXT NOT NULL DEFAULT ''")
        if not _has_column(con, "workspace_journal", "status"):
            con.execute("ALTER TABLE workspace_journal ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'")
        if not _has_column(con, "workspace_journal", "created_by"):
            con.execute("ALTER TABLE workspace_journal ADD COLUMN created_by TEXT NOT NULL DEFAULT 'human'")
        con.execute(
            """CREATE TABLE IF NOT EXISTS ai_action_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                tool_name TEXT NOT NULL,
                params_json TEXT NOT NULL,
                reasoning TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0,
                trace_id TEXT NOT NULL DEFAULT ''
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_ai_action_queue_status ON ai_action_queue(status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_ai_action_queue_created ON ai_action_queue(created_at)")
        # One-pass hygiene: correct AI notes with malformed/unknown tickers.
        rows = con.execute(
            """SELECT id, ticker, note FROM investor_notes
                WHERE LOWER(COALESCE(created_by,'')) = 'ai'
                ORDER BY id DESC
                LIMIT 2000""",
        ).fetchall()
        for r in rows:
            nid = int(r["id"] or 0)
            if nid <= 0:
                continue
            cur_tk = str(r["ticker"] or "").strip().upper()
            if cur_tk and cur_tk not in INVALID_TICKER_WORDS and _is_known_ticker(cur_tk):
                continue
            fixed_tk = _resolve_ticker_from_text(str(r["note"] or ""), current_ticker=cur_tk)
            if fixed_tk and fixed_tk.upper() not in INVALID_TICKER_WORDS:
                con.execute("UPDATE investor_notes SET ticker = ? WHERE id = ?", (fixed_tk[:16], nid))
        con.commit()
    finally:
        con.close()


def _extract_tickers(text: str, limit: int = 50) -> list[str]:
    found = re.findall(r"\$([A-Za-z]{1,6})\b|\b([A-Z]{2,5})\b", text or "")
    out: list[str] = []
    seen: set[str] = set()
    stop = {"THE", "AND", "FOR", "WITH", "THIS", "THAT", "FROM", "NOTE", "IDEA", "BOOK"}
    for pair in found:
        raw = (pair[0] or pair[1] or "").upper().strip()
        if not raw or raw in stop or raw in seen:
            continue
        seen.add(raw)
        out.append(raw)
        if len(out) >= max(1, min(200, int(limit))):
            break
    return out


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", str(text or "").lower())).strip()


def _is_known_ticker(ticker: str) -> bool:
    tk = str(ticker or "").strip().upper()
    if not tk:
        return False
    if tk in set(ENTITY_ALIASES.values()):
        return True
    try:
        con = _conn()
        try:
            row = con.execute("SELECT 1 FROM companies WHERE UPPER(ticker)=? LIMIT 1", (tk,)).fetchone()
            return bool(row)
        finally:
            con.close()
    except Exception:
        return False


def _resolve_ticker_from_text(text: str, current_ticker: str = "") -> str:
    cur = str(current_ticker or "").strip().upper()
    if cur and cur not in INVALID_TICKER_WORDS and _is_known_ticker(cur):
        return cur
    s = str(text or "")
    if not s:
        return ""
    m = re.search(r"\b(?:company|ticker)\s+\$?([A-Za-z]{1,5})\b", s, flags=re.I)
    if m:
        tk = str(m.group(1) or "").strip().upper()
        if tk and tk not in INVALID_TICKER_WORDS:
            return tk
    m = re.search(r"\$([A-Za-z]{1,5})\b", s)
    if m:
        tk = str(m.group(1) or "").strip().upper()
        if tk and tk not in INVALID_TICKER_WORDS:
            return tk
    low = _norm(s)
    for alias, tk in ENTITY_ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}\b", low):
            return tk
    try:
        con = _conn()
        try:
            rows = con.execute(
                "SELECT ticker, name FROM companies WHERE name IS NOT NULL AND name <> '' LIMIT 1500"
            ).fetchall()
        finally:
            con.close()
        for r in rows:
            nm = _norm(str(r["name"] or ""))
            tk = str(r["ticker"] or "").strip().upper()
            if nm and tk and len(nm) >= 4 and nm in low and tk not in INVALID_TICKER_WORDS:
                return tk
    except Exception:
        pass
    for tok in re.findall(r"\b([A-Za-z]{2,5})\b", s):
        up = tok.upper()
        if up in INVALID_TICKER_WORDS:
            continue
        return up
    return ""


@dataclass
class DailyNoteView:
    day: str
    content: str
    updated_at: str
    locked: bool
    archived_at: str
    tags: list[str]


def get_daily_note(day: str) -> DailyNoteView:
    ensure_schema()
    d = str(day or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        d = dt.date.today().isoformat()
    con = _conn()
    try:
        row = con.execute(
            "SELECT day, content, updated_at, locked, archived_at FROM daily_notes WHERE day = ?",
            (d,),
        ).fetchone()
        tags = [
            str(r["ticker"] or "").strip().upper()
            for r in con.execute(
                "SELECT ticker FROM daily_note_tags WHERE day = ? ORDER BY ticker ASC",
                (d,),
            ).fetchall()
            if str(r["ticker"] or "").strip()
        ]
    finally:
        con.close()
    return DailyNoteView(
        day=d,
        content=str((row["content"] if row else "") or ""),
        updated_at=str((row["updated_at"] if row else "") or ""),
        locked=int((row["locked"] if row else 0) or 0) == 1,
        archived_at=str((row["archived_at"] if row else "") or ""),
        tags=tags,
    )


def save_daily_note(day: str, content: str) -> bool:
    ensure_schema()
    d = str(day or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        d = dt.date.today().isoformat()
    txt = str(content or "").strip()
    now = dt.datetime.now().isoformat()
    con = _conn()
    try:
        row = con.execute("SELECT locked FROM daily_notes WHERE day = ?", (d,)).fetchone()
        if row and int(row["locked"] or 0) == 1:
            return False
        con.execute(
            """INSERT INTO daily_notes (day, content, updated_at, locked, archived_at)
               VALUES (?, ?, ?, 0, '')
               ON CONFLICT(day) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at""",
            (d, txt[:120000], now),
        )
        con.execute("DELETE FROM daily_note_tags WHERE day = ?", (d,))
        for ticker in _extract_tickers(txt, limit=60):
            con.execute(
                "INSERT INTO daily_note_tags (day, ticker, created_at) VALUES (?, ?, ?)",
                (d, ticker, now),
            )
        con.commit()
        try:
            # Local import avoids hard dependency/circular import at module load.
            from app.services.agent_service import memorize_user_note

            memorize_user_note(
                txt[:120000],
                ticker="",
                source_type="daily_note",
                source_id=f"daily_notes:{d}",
            )
        except Exception:
            pass
        return True
    finally:
        con.close()


def close_day(day: str) -> bool:
    ensure_schema()
    d = str(day or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        return False
    now = dt.datetime.now().isoformat()
    con = _conn()
    try:
        cur = con.execute(
            "UPDATE daily_notes SET locked = 1, archived_at = ?, updated_at = ? WHERE day = ?",
            (now, now, d),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def list_tasks(open_only: bool = True, limit: int = 400) -> list[sqlite3.Row]:
    if core_backend() == "postgres":
        return list_todos_pg(open_only=open_only, limit=limit)
    con = _conn()
    try:
        if open_only:
            rows = con.execute(
                """SELECT id, task, status, priority, due_date, ticker, category, created_at
                   FROM todos
                   WHERE status = 'open'
                   ORDER BY CASE priority WHEN 'P1' THEN 0 WHEN 'P2' THEN 1 ELSE 2 END,
                            CASE WHEN due_date <> '' THEN due_date ELSE '9999-12-31' END,
                            id DESC
                   LIMIT ?""",
                (max(1, min(2000, int(limit))),),
            ).fetchall()
        else:
            rows = con.execute(
                """SELECT id, task, status, priority, due_date, ticker, category, created_at
                   FROM todos
                   WHERE status IN ('done', 'archived')
                   ORDER BY id DESC
                   LIMIT ?""",
                (max(1, min(2000, int(limit))),),
            ).fetchall()
        return rows
    finally:
        con.close()


def toggle_task(todo_id: int) -> bool:
    if todo_id <= 0:
        return False
    if core_backend() == "postgres":
        return toggle_todo_pg(todo_id)
    con = _conn()
    try:
        row = con.execute("SELECT status, category FROM todos WHERE id = ?", (todo_id,)).fetchone()
        if not row:
            return False
        status = str(row["status"] or "open").strip().lower()
        cat = str(row["category"] or "general").strip().lower()
        if status == "open":
            new_status = "archived" if cat == "quick" else "done"
        else:
            new_status = "open"
        con.execute("UPDATE todos SET status = ? WHERE id = ?", (new_status, todo_id))
        con.commit()
        return True
    finally:
        con.close()


def complete_task(todo_id: int) -> bool:
    rid = int(todo_id or 0)
    if rid <= 0:
        return False
    if core_backend() == "postgres":
        rows = list_todos_pg(open_only=True, limit=1)
        row = next((r for r in rows if int(r.get("id") or 0) == rid), None)
        if row is None:
            return False
        cat = str(row.get("category") or "general").strip().lower()
        return update_todo_status_pg(rid, "archived" if cat == "quick" else "done")
    con = _conn()
    try:
        row = con.execute("SELECT status, category FROM todos WHERE id = ?", (rid,)).fetchone()
        if not row:
            return False
        status = str(row["status"] or "open").strip().lower()
        if status in {"done", "archived"}:
            return True
        cat = str(row["category"] or "general").strip().lower()
        new_status = "archived" if cat == "quick" else "done"
        con.execute("UPDATE todos SET status = ? WHERE id = ?", (new_status, rid))
        con.commit()
        return True
    finally:
        con.close()


def complete_task_by_text(task_text: str) -> dict[str, object]:
    txt = str(task_text or "").strip()
    if not txt:
        return {"ok": False, "error": "task_text_required"}
    if core_backend() == "postgres":
        rows = list_todos_pg(open_only=False, limit=2000)
        row = next((r for r in rows if str(r.get("task") or "").strip().lower() == txt.lower()), None)
        if row is None:
            row = next(
                (
                    r
                    for r in rows
                    if str(r.get("status") or "").strip().lower() == "open"
                    and txt.lower() in str(r.get("task") or "").lower()
                ),
                None,
            )
        if not row:
            return {"ok": False, "error": "not_found"}
        rid = int(row.get("id") or 0)
        status = str(row.get("status") or "open").strip().lower()
        if status not in {"done", "archived"}:
            cat = str(row.get("category") or "general").strip().lower()
            status = "archived" if cat == "quick" else "done"
            _ = update_todo_status_pg(rid, status)
        return {"ok": True, "id": rid, "task": str(row.get("task") or ""), "status": status}
    con = _conn()
    try:
        row = con.execute(
            """SELECT id, task, status, category
               FROM todos
               WHERE LOWER(TRIM(task)) = LOWER(TRIM(?))
               ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, id DESC
               LIMIT 1""",
            (txt,),
        ).fetchone()
        if not row:
            row = con.execute(
                """SELECT id, task, status, category
                   FROM todos
                   WHERE status = 'open' AND LOWER(task) LIKE LOWER(?)
                   ORDER BY id DESC
                   LIMIT 1""",
                (f"%{txt[:80]}%",),
            ).fetchone()
        if not row:
            return {"ok": False, "error": "not_found"}
        rid = int(row["id"] or 0)
        if rid <= 0:
            return {"ok": False, "error": "not_found"}
        status = str(row["status"] or "open").strip().lower()
        if status not in {"done", "archived"}:
            cat = str(row["category"] or "general").strip().lower()
            new_status = "archived" if cat == "quick" else "done"
            con.execute("UPDATE todos SET status = ? WHERE id = ?", (new_status, rid))
            con.commit()
            status = new_status
        return {"ok": True, "id": rid, "task": str(row["task"] or ""), "status": status}
    finally:
        con.close()


def list_recent_notes(limit: int = 200) -> list[dict[str, str]]:
    if core_backend() == "postgres":
        return list_recent_notes_pg(limit=limit)
    con = _conn()
    rows: list[dict[str, str]] = []
    lim = max(1, min(2000, int(limit)))
    try:
        for r in con.execute(
            "SELECT id, created_at, scope, ticker, note, status, created_by, ai_confidence, ai_reasoning, trace_id FROM investor_notes ORDER BY id DESC LIMIT ?",
            (lim,),
        ).fetchall():
            rows.append(
                {
                    "id": str(r["id"] or ""),
                    "source_table": "investor_notes",
                    "date": str(r["created_at"] or ""),
                    "kind": "General",
                    "ticker": str(r["ticker"] or "").strip().upper(),
                    "tag": str(r["scope"] or "general"),
                    "text": str(r["note"] or ""),
                    "status": str(r["status"] or "approved"),
                    "created_by": str(r["created_by"] or "human"),
                    "ai_confidence": str(r["ai_confidence"] or "0"),
                    "ai_reasoning": str(r["ai_reasoning"] or ""),
                    "trace_id": str(r["trace_id"] or ""),
                }
            )
        for r in con.execute(
            "SELECT id, created_at, ticker, action, note, status, created_by FROM workspace_journal ORDER BY id DESC LIMIT ?",
            (lim,),
        ).fetchall():
            rows.append(
                {
                    "id": str(r["id"] or ""),
                    "source_table": "workspace_journal",
                    "date": str(r["created_at"] or ""),
                    "kind": "Company",
                    "ticker": str(r["ticker"] or "").strip().upper(),
                    "tag": str(r["action"] or "note"),
                    "text": str(r["note"] or ""),
                    "status": str(r["status"] or "approved"),
                    "created_by": str(r["created_by"] or "human"),
                    "ai_confidence": "0",
                    "ai_reasoning": "",
                    "trace_id": "",
                }
            )
        rows.sort(key=lambda x: x["date"], reverse=True)
        return rows[:lim]
    finally:
        con.close()


def add_general_note(
    note: str,
    *,
    scope: str = "organizer",
    ticker: str = "",
    tags: str = "log",
    status: str = "approved",
    created_by: str = "human",
    ai_confidence: float = 0.0,
    ai_reasoning: str = "",
    trace_id: str = "",
) -> bool:
    txt = str(note or "").strip()
    if not txt:
        return False
    st = str(status or "approved").strip().lower()
    if st not in {"pending", "approved", "rejected"}:
        st = "approved"
    cb = str(created_by or "human").strip().lower()
    if cb not in {"human", "ai"}:
        cb = "human"
    now = dt.datetime.now().isoformat()
    if core_backend() == "postgres":
        return add_investor_note_pg(
            scope=str(scope or "organizer")[:40],
            ticker=str(ticker or "").strip().upper()[:16],
            sentiment="neutral",
            note=txt[:4000],
            tags=str(tags or "log")[:200],
            status=st,
            created_by=cb,
            ai_confidence=float(ai_confidence or 0.0),
            ai_reasoning=str(ai_reasoning or "")[:3000],
            trace_id=str(trace_id or "")[:120],
        )
    con = _conn()
    try:
        con.execute(
            """
            INSERT INTO investor_notes (scope, ticker, sentiment, note, tags, created_at, status, created_by, ai_confidence, ai_reasoning, trace_id)
            VALUES (?, ?, 'neutral', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(scope or "organizer")[:40],
                str(ticker or "").strip().upper()[:16],
                txt[:4000],
                str(tags or "log")[:200],
                now,
                st,
                cb,
                float(ai_confidence or 0.0),
                str(ai_reasoning or "")[:3000],
                str(trace_id or "")[:120],
            ),
        )
        con.commit()
        return True
    except Exception:
        return False
    finally:
        con.close()


def approve_note_draft(note_id: int) -> bool:
    rid = int(note_id or 0)
    if rid <= 0:
        return False
    con = _conn()
    try:
        row = con.execute(
            "SELECT ticker, note, created_by, status FROM investor_notes WHERE id = ?",
            (rid,),
        ).fetchone()
        if not row or str(row["status"] or "").strip().lower() != "pending":
            return False
        cur_tk = str(row["ticker"] or "").strip().upper()
        note_txt = str(row["note"] or "")
        created_by = str(row["created_by"] or "").strip().lower()
        fixed_tk = cur_tk
        # AI drafts get ticker normalization on approval to prevent malformed tickers like BUY/APPLE.
        if created_by == "ai" and (not cur_tk or cur_tk in INVALID_TICKER_WORDS or not _is_known_ticker(cur_tk)):
            fixed_tk = _resolve_ticker_from_text(note_txt, current_ticker=cur_tk)
        cur = con.execute(
            "UPDATE investor_notes SET status='approved', ticker=? WHERE id = ? AND status='pending'",
            (fixed_tk[:16], rid),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def discard_note_draft(note_id: int) -> bool:
    rid = int(note_id or 0)
    if rid <= 0:
        return False
    con = _conn()
    try:
        cur = con.execute(
            "DELETE FROM investor_notes WHERE id = ? AND status='pending'",
            (rid,),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def delete_notes(
    *,
    ticker: str = "",
    text_contains: str = "",
    created_by: str = "",
    include_company_journal: bool = True,
) -> dict[str, int]:
    tk = str(ticker or "").strip().upper()[:16]
    txt = str(text_contains or "").strip()
    cb = str(created_by or "").strip().lower()
    if cb not in {"", "ai", "human"}:
        cb = ""
    out = {"investor_notes": 0, "workspace_journal": 0, "total": 0}
    con = _conn()
    try:
        where = ["1=1"]
        params: list[object] = []
        if tk:
            where.append("ticker = ?")
            params.append(tk)
        if txt:
            where.append("LOWER(note) LIKE ?")
            params.append("%" + txt.lower() + "%")
        if cb:
            where.append("LOWER(created_by) = ?")
            params.append(cb)
        sql = "DELETE FROM investor_notes WHERE " + " AND ".join(where)
        cur = con.execute(sql, tuple(params))
        out["investor_notes"] = int(cur.rowcount or 0)
        if include_company_journal:
            where2 = ["1=1"]
            params2: list[object] = []
            if tk:
                where2.append("ticker = ?")
                params2.append(tk)
            if txt:
                where2.append("LOWER(note) LIKE ?")
                params2.append("%" + txt.lower() + "%")
            sql2 = "DELETE FROM workspace_journal WHERE " + " AND ".join(where2)
            cur2 = con.execute(sql2, tuple(params2))
            out["workspace_journal"] = int(cur2.rowcount or 0)
        out["total"] = out["investor_notes"] + out["workspace_journal"]
        con.commit()
        return out
    finally:
        con.close()


def enqueue_action(
    tool_name: str,
    params_json: str,
    reasoning: str,
    confidence: float,
    trace_id: str = "",
) -> bool:
    nm = str(tool_name or "").strip()
    if not nm:
        return False
    con = _conn()
    try:
        con.execute(
            """INSERT INTO ai_action_queue (created_at, status, tool_name, params_json, reasoning, confidence, trace_id)
               VALUES (?, 'pending', ?, ?, ?, ?, ?)""",
            (
                dt.datetime.now().isoformat(),
                nm[:120],
                str(params_json or "")[:8000],
                str(reasoning or "")[:3000],
                float(confidence or 0.0),
                str(trace_id or "")[:120],
            ),
        )
        con.commit()
        return True
    finally:
        con.close()


def list_action_queue(limit: int = 120) -> list[dict[str, str]]:
    con = _conn()
    lim = max(1, min(1000, int(limit or 120)))
    out: list[dict[str, str]] = []
    try:
        rows = con.execute(
            """SELECT id, created_at, status, tool_name, params_json, reasoning, confidence, trace_id
               FROM ai_action_queue
               WHERE status = 'pending'
               ORDER BY id DESC
               LIMIT ?""",
            (lim,),
        ).fetchall()
        for r in rows:
            out.append(
                {
                    "id": str(r["id"] or ""),
                    "created_at": str(r["created_at"] or ""),
                    "status": str(r["status"] or ""),
                    "tool_name": str(r["tool_name"] or ""),
                    "params_json": str(r["params_json"] or ""),
                    "reasoning": str(r["reasoning"] or ""),
                    "confidence": str(r["confidence"] or "0"),
                    "trace_id": str(r["trace_id"] or ""),
                }
            )
        return out
    finally:
        con.close()


def list_action_log(limit: int = 120) -> list[dict[str, str]]:
    con = _conn()
    lim = max(1, min(1000, int(limit or 120)))
    out: list[dict[str, str]] = []
    try:
        rows = con.execute(
            """SELECT id, created_at, query, intent, status, confidence, payload_json
               FROM ai_action_log
               ORDER BY id DESC
               LIMIT ?""",
            (lim,),
        ).fetchall()
        for r in rows:
            out.append(
                {
                    "id": str(r["id"] or ""),
                    "created_at": str(r["created_at"] or ""),
                    "query": str(r["query"] or ""),
                    "intent": str(r["intent"] or ""),
                    "status": str(r["status"] or ""),
                    "confidence": str(r["confidence"] or "0"),
                    "payload_json": str(r["payload_json"] or ""),
                }
            )
        return out
    except Exception:
        return out
    finally:
        con.close()


def resolve_action_queue(action_id: int, decision: str) -> bool:
    rid = int(action_id or 0)
    dec = str(decision or "").strip().lower()
    if rid <= 0 or dec not in {"approved", "rejected"}:
        return False
    con = _conn()
    try:
        cur = con.execute(
            "UPDATE ai_action_queue SET status = ? WHERE id = ? AND status = 'pending'",
            (dec, rid),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def add_task(
    task: str,
    *,
    ticker: str = "",
    category: str = "general",
    priority: str = "P2",
    due_date: str = "",
) -> bool:
    txt = str(task or "").strip()
    if not txt:
        return False
    tk = str(ticker or "").strip().upper()[:16]
    cat = str(category or "general").strip().lower()
    if cat not in {"company", "quick", "general"}:
        cat = "general"
    pr = str(priority or "P2").strip().upper()
    if pr not in {"P1", "P2", "P3"}:
        pr = "P2"
    dd = str(due_date or "").strip()
    if dd and not re.match(r"^\d{4}-\d{2}-\d{2}$", dd):
        dd = ""
    now = dt.datetime.now().isoformat()
    if core_backend() == "postgres":
        return add_todo_pg(task=txt[:4000], ticker=tk, category=cat, priority=pr, due_date=dd)
    con = _conn()
    try:
        con.execute(
            """
            INSERT INTO todos (task, status, created_at, priority, due_date, ticker, category)
            VALUES (?, 'open', ?, ?, ?, ?, ?)
            """,
            (txt[:4000], now, pr, dd, tk, cat),
        )
        con.commit()
        return True
    except Exception:
        return False
    finally:
        con.close()


def suggest_ir_emails(ticker: str = "", company: str = "", domain: str = "", limit: int = 10) -> list[dict[str, str]]:
    t = str(ticker or "").strip().upper()
    dm = str(domain or "").strip().lower().replace("https://", "").replace("http://", "").strip("/")
    email_re = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(email: str, confidence: str, source: str) -> None:
        e = str(email or "").strip().lower()
        if not e or e in seen:
            return
        seen.add(e)
        out.append({"email": e, "confidence": confidence, "source": source})

    # 1) User-maintained override file (highest trust)
    p = ROOT / "data" / "ir_contacts.csv"
    if p.exists():
        try:
            with p.open("r", encoding="utf-8", errors="ignore", newline="") as fh:
                rd = csv.DictReader(fh)
                for r in rd:
                    tk = str((r.get("ticker") or "")).strip().upper()
                    em = str((r.get("email") or "")).strip()
                    if t and tk != t:
                        continue
                    if em:
                        add(em, "high", "ir_contacts.csv")
        except Exception:
            pass

    # 2) Extract from existing notes for this ticker (high confidence)
    if t:
        con = _conn()
        try:
            tables = [
                ("workspace_journal", "note", "ticker = ?"),
                ("company_reminders", "note", "ticker = ?"),
                ("investor_notes", "note", "ticker = ?"),
            ]
            for tbl, col, where in tables:
                for r in con.execute(f"SELECT {col} AS txt FROM {tbl} WHERE {where} ORDER BY id DESC LIMIT 300", (t,)).fetchall():
                    txt = str(r["txt"] or "")
                    for em in email_re.findall(txt):
                        add(em, "high", f"{tbl}.{col}")
        finally:
            con.close()

    # 3) If a domain is provided, generate common IR aliases (medium confidence)
    if dm and "." in dm:
        aliases = [
            "ir",
            "investorrelations",
            "investor.relations",
            "investors",
            "investor",
            "irteam",
            "corpcomm",
            "media",
            "press",
        ]
        for a in aliases:
            add(f"{a}@{dm}", "medium", "pattern")

    # 4) If we already have a high confidence email, use its domain for extra medium suggestions.
    if not dm:
        for row in out:
            if row.get("confidence") == "high":
                em = str(row.get("email") or "")
                if "@" in em:
                    dm2 = em.split("@", 1)[1]
                    for a in ("ir", "investorrelations", "investors"):
                        add(f"{a}@{dm2}", "medium", "pattern-from-known-domain")
                break

    # 5) If still nothing, infer likely domain from company name (low confidence).
    if not out and company:
        raw = str(company or "").strip().lower()
        # Remove common legal/company suffixes.
        raw = re.sub(
            r"\b(inc|inc\.|corp|corporation|company|co|ltd|limited|plc|group|holdings|sa|ag|nv|llc|the)\b",
            " ",
            raw,
        )
        tokens = [re.sub(r"[^a-z0-9]", "", x) for x in raw.split()]
        tokens = [x for x in tokens if len(x) >= 2]
        guesses: list[str] = []
        if tokens:
            guesses.append(tokens[0] + ".com")
            if len(tokens) >= 2:
                guesses.append(tokens[0] + tokens[1] + ".com")
            guesses.append("".join(tokens[:3]) + ".com")
        if t and len(t) >= 2:
            guesses.append(t.lower() + ".com")
        for g in guesses:
            if "." not in g or len(g) < 6:
                continue
            for a in ("ir", "investorrelations", "investors"):
                add(f"{a}@{g}", "low", "pattern-from-company-name")

    # Prefer high confidence first, then medium.
    rank = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda x: (rank.get(str(x.get("confidence")), 9), str(x.get("email") or "")))
    return out[: max(1, min(30, int(limit or 10)))]


def recall(question: str, limit: int = 10) -> tuple[str, list[dict[str, str]]]:
    q = str(question or "").strip()
    if not q:
        return "", []
    toks = [
        x
        for x in re.findall(r"[a-z0-9]{3,}", q.lower())
        if x not in {"what", "when", "this", "that", "with", "from", "about", "your"}
    ]
    pool = list_recent_notes(limit=500)
    dn = get_daily_note(dt.date.today().isoformat())
    if dn.content:
        pool.insert(
            0,
            {"date": dn.day, "kind": "Daily", "ticker": ",".join(dn.tags), "tag": "daily", "text": dn.content},
        )
    scored: list[tuple[int, dict[str, str]]] = []
    for row in pool:
        hay = f"{row.get('date','')} {row.get('ticker','')} {row.get('tag','')} {row.get('text','')}".lower()
        s = sum(1 for t in toks if t in hay)
        if s > 0:
            scored.append((s, row))
    scored.sort(key=lambda x: (-x[0], str(x[1].get("date") or "")), reverse=False)
    top = [r for _s, r in scored[: max(1, min(25, limit * 2))]]
    if not top:
        return "No matching memory found yet.", []
    lines = [f"- {r.get('date','-')} | {r.get('ticker','-')} | {str(r.get('text',''))[:220]}" for r in top[:limit]]
    return "Best matches:\n" + "\n".join(lines), top[:limit]
