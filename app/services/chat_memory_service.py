from __future__ import annotations

import datetime as dt
from app.services.postgres_core_service import pg_connect


def ensure_chat_memory_schema() -> None:
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        cur.execute(
            """CREATE TABLE IF NOT EXISTS ai_chat_memory (
                id BIGSERIAL PRIMARY KEY,
                created_at TEXT NOT NULL,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                intent TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT ''
            )"""
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_chat_memory_session ON ai_chat_memory(session_id, id)")
        con.commit()
    finally:
        con.close()


def append_chat_message(
    session_id: str,
    role: str,
    text: str,
    intent: str = "",
    status: str = "",
) -> bool:
    sid = str(session_id or "default").strip()[:120]
    rl = str(role or "").strip().lower()[:20]
    body = str(text or "").strip()
    if not sid or rl not in {"user", "assistant"} or not body:
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            """INSERT INTO ai_chat_memory (created_at, session_id, role, text, intent, status)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                dt.datetime.now().isoformat(),
                sid,
                rl,
                body[:6000],
                str(intent or "")[:120],
                str(status or "")[:80],
            ),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def list_recent_chat_messages(session_id: str, limit: int = 24) -> list[dict[str, str]]:
    sid = str(session_id or "default").strip()[:120]
    lim = max(1, min(200, int(limit or 24)))
    con = pg_connect()
    if con is None:
        return []
    out: list[dict[str, str]] = []
    try:
        cur = con.cursor()
        cur.execute(
            """SELECT role, text, intent, status, created_at
               FROM ai_chat_memory
               WHERE session_id = %s
               ORDER BY id DESC
               LIMIT %s""",
            (sid, lim),
        )
        rows = cur.fetchall() or []
        for r in reversed(rows):
            role, text, intent, status, created_at = r
            out.append(
                {
                    "role": str(role or ""),
                    "text": str(text or ""),
                    "intent": str(intent or ""),
                    "status": str(status or ""),
                    "created_at": str(created_at or ""),
                }
            )
        return out
    finally:
        con.close()


def latest_assistant_analysis(session_id: str) -> dict[str, str]:
    sid = str(session_id or "default").strip()[:120]
    con = pg_connect()
    if con is None:
        return {}
    try:
        cur = con.cursor()
        cur.execute(
            """SELECT text, intent, status, created_at
               FROM ai_chat_memory
               WHERE session_id = %s
                 AND role = 'assistant'
                 AND (
                   intent IN ('map_reduce_reduce', 'llm_fallback')
                   OR text LIKE '%Integrated Conclusion%'
                   OR text LIKE '%Sector Analysis Complete%'
                 )
               ORDER BY
                 CASE WHEN intent = 'map_reduce_reduce' THEN 0 ELSE 1 END,
                 id DESC
               LIMIT 1""",
            (sid,),
        )
        row = cur.fetchone()
        if not row:
            return {}
        text, intent, status, created_at = row
        return {
            "text": str(text or ""),
            "intent": str(intent or ""),
            "status": str(status or ""),
            "created_at": str(created_at or ""),
        }
    finally:
        con.close()
