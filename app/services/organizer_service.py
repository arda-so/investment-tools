from __future__ import annotations

import datetime as dt
import re
import sqlite3
from dataclasses import dataclass
import csv
import io

from app.core import cloud_files
from app.core.db import core_conn as _conn
from app.core.normalize import normalize_text as _norm
from app.services.company_lookup_service import company_name_map
from app.services.postgres_core_service import (
    approve_investor_note_draft_pg,
    add_investor_note_pg,
    add_todo_pg,
    close_day_pg,
    core_backend,
    delete_notes_pg,
    discard_investor_note_draft_pg,
    enqueue_action_pg,
    get_daily_note_pg,
    list_action_log_pg,
    list_action_queue_pg,
    list_recent_notes_pg,
    list_todos_pg,
    resolve_action_queue_pg,
    save_daily_note_pg,
    snooze_todo_pg,
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


def _has_column(con: sqlite3.Connection, table: str, column: str) -> bool:
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    return any(str(r["name"] or "") == column for r in rows)


def ensure_schema() -> None:
    if core_backend() == "postgres":
        return
    con = _conn()
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                priority TEXT NOT NULL DEFAULT 'P2',
                due_date TEXT NOT NULL DEFAULT '',
                ticker TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'general',
                snooze_until TEXT NOT NULL DEFAULT ''
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status, id DESC)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_todos_ticker ON todos(ticker, id DESC)")
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


def _is_known_ticker(ticker: str) -> bool:
    tk = str(ticker or "").strip().upper()
    if not tk:
        return False
    if tk in set(ENTITY_ALIASES.values()):
        return True
    if core_backend() == "postgres":
        try:
            return bool(company_name_map([tk]).get(tk))
        except Exception:
            return False
    con = None
    try:
        con = _conn()
        row = con.execute("SELECT 1 FROM companies WHERE UPPER(ticker)=? LIMIT 1", (tk,)).fetchone()
        return bool(row)
    except Exception as exc:
        print(f"[organizer.add_task] sqlite insert failed: {type(exc).__name__}: {exc}")
        return False
    finally:
        if con is not None:
            con.close()


def _resolve_ticker_from_text(text: str, current_ticker: str = "") -> str:
    s = str(text or "")
    if not s:
        return ""
    # Strict policy for organizer capture: only explicit $TICKER binds scope.
    # This prevents accidental links from natural language words.
    m = re.search(r"\$([A-Za-z]{1,5})\b", s)
    if m:
        tk = str(m.group(1) or "").strip().upper()
        if tk and tk not in INVALID_TICKER_WORDS and _is_known_ticker(tk):
            return tk
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
    d = str(day or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        d = dt.date.today().isoformat()
    if core_backend() == "postgres":
        obj = get_daily_note_pg(d)
        return DailyNoteView(
            day=str(obj.get("day") or d),
            content=str(obj.get("content") or ""),
            updated_at=str(obj.get("updated_at") or ""),
            locked=bool(obj.get("locked") or False),
            archived_at=str(obj.get("archived_at") or ""),
            tags=[str(x or "").strip().upper() for x in list(obj.get("tags") or []) if str(x or "").strip()],
        )
    ensure_schema()
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
    d = str(day or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        d = dt.date.today().isoformat()
    txt = str(content or "").strip()
    if core_backend() == "postgres":
        ok = save_daily_note_pg(d, txt, tags=_extract_tickers(txt, limit=60))
        if ok:
            try:
                from app.services.agent_service import memorize_user_note

                memorize_user_note(
                    txt[:120000],
                    ticker="",
                    source_type="daily_note",
                    source_id=f"daily_notes:{d}",
                )
            except Exception:
                pass
        return ok
    ensure_schema()
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
    d = str(day or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        return False
    if core_backend() == "postgres":
        return close_day_pg(d)
    ensure_schema()
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
        row = con.execute("SELECT status FROM todos WHERE id = ?", (todo_id,)).fetchone()
        if not row:
            return False
        status = str(row["status"] or "open").strip().lower()
        if status == "open":
            new_status = "done"
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
        return update_todo_status_pg(rid, "done")
    con = _conn()
    try:
        row = con.execute("SELECT status FROM todos WHERE id = ?", (rid,)).fetchone()
        if not row:
            return False
        status = str(row["status"] or "open").strip().lower()
        if status in {"done", "archived"}:
            return True
        new_status = "done"
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
            status = "done"
            _ = update_todo_status_pg(rid, status)
        return {"ok": True, "id": rid, "task": str(row.get("task") or ""), "status": status}
    con = _conn()
    try:
        row = con.execute(
            """SELECT id, task, status
               FROM todos
               WHERE LOWER(TRIM(task)) = LOWER(TRIM(?))
               ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, id DESC
               LIMIT 1""",
            (txt,),
        ).fetchone()
        if not row:
            row = con.execute(
                """SELECT id, task, status
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
            new_status = "done"
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
    inferred_ticker = str(ticker or "").strip().upper()[:16]
    if not inferred_ticker:
        inferred_ticker = _resolve_ticker_from_text(txt)
    if core_backend() == "postgres":
        return add_investor_note_pg(
            scope=str(scope or "organizer")[:40],
            ticker=inferred_ticker,
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
                inferred_ticker,
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
    if core_backend() == "postgres":
        # Draft normalization currently optional in pg path; keep ticker untouched.
        return approve_investor_note_draft_pg(rid)
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
    if core_backend() == "postgres":
        return discard_investor_note_draft_pg(rid)
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
    if core_backend() == "postgres":
        return delete_notes_pg(
            ticker=tk,
            text_contains=txt,
            created_by=cb,
            include_company_journal=include_company_journal,
        )
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
    if core_backend() == "postgres":
        return enqueue_action_pg(nm, params_json, reasoning, confidence, trace_id)
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
    if core_backend() == "postgres":
        return list_action_queue_pg(limit=limit)
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
    if core_backend() == "postgres":
        return list_action_log_pg(limit=limit)
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
    if core_backend() == "postgres":
        return resolve_action_queue_pg(rid, dec)
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
    ensure_schema()
    txt = str(task or "").strip()
    if not txt:
        return False
    tk = str(ticker or "").strip().upper()[:16]
    if not tk:
        m = re.search(r"\$([A-Za-z]{1,5})\b", txt)
        if not m:
            m = re.search(r"\b(?:company|ticker)\s+\$?([A-Za-z]{1,5})\b", txt, flags=re.I)
        cand = str(m.group(1) if m else "").strip().upper()
        tk = cand if (cand and _is_known_ticker(cand)) else ""
    cat = str(category or "general").strip().lower()
    if cat not in {"company", "general"}:
        cat = "general"
    if tk and cat == "general":
        cat = "company"
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


def snooze_task(todo_id: int, until_date: str) -> bool:
    rid = int(todo_id or 0)
    if rid <= 0:
        return False
    su = str(until_date or "").strip()
    if not su:
        return False
    if core_backend() == "postgres":
        return snooze_todo_pg(rid, su)
    try:
        dt.date.fromisoformat(su)
    except Exception:
        return False
    con = _conn()
    try:
        cur = con.execute("UPDATE todos SET status='snoozed' WHERE id = ?", (rid,))
        con.commit()
        return bool(cur.rowcount)
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
    try:
        csv_txt = cloud_files.read_text("data/ir_contacts.csv")
        if csv_txt:
            fh = io.StringIO(csv_txt)
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
        if core_backend() == "postgres":
            for n in list_recent_notes_pg(limit=600):
                tk = str(n.get("ticker") or "").strip().upper()
                if tk and tk != t:
                    continue
                txt = str(n.get("text") or "")
                for em in email_re.findall(txt):
                    add(em, "high", "notes")
        else:
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
