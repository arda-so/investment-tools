"""research_thread_service.py — Research Threads & Projects engine.

Tables:
  research_threads_core   — thread/project definitions
  thread_sessions_core    — work sessions ("where I left off")
  thread_entries_core     — findings, questions, notes, blockers per thread
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from app.services.postgres_core_service import pg_connect, pg_enabled

LOGGER = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS research_threads_core (
    id          BIGSERIAL PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    emoji       TEXT NOT NULL DEFAULT '',
    thread_type TEXT NOT NULL DEFAULT 'research',
    ticker      TEXT NOT NULL DEFAULT '',
    thesis      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',
    priority    TEXT NOT NULL DEFAULT 'normal',
    tags        JSONB NOT NULL DEFAULT '[]'::jsonb,
    canvas_markdown TEXT NOT NULL DEFAULT '',
    canvas_updated_at TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rt_status ON research_threads_core(status);
CREATE INDEX IF NOT EXISTS idx_rt_type ON research_threads_core(thread_type);
CREATE INDEX IF NOT EXISTS idx_rt_ticker ON research_threads_core(ticker);

CREATE TABLE IF NOT EXISTS thread_sessions_core (
    id              BIGSERIAL PRIMARY KEY,
    thread_id       BIGINT NOT NULL REFERENCES research_threads_core(id) ON DELETE CASCADE,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    summary         TEXT NOT NULL DEFAULT '',
    where_left_off  TEXT NOT NULL DEFAULT '',
    duration_min    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ts_thread ON thread_sessions_core(thread_id, started_at DESC);

CREATE TABLE IF NOT EXISTS thread_entries_core (
    id          BIGSERIAL PRIMARY KEY,
    thread_id   BIGINT NOT NULL REFERENCES research_threads_core(id) ON DELETE CASCADE,
    session_id  BIGINT REFERENCES thread_sessions_core(id) ON DELETE SET NULL,
    kind        TEXT NOT NULL DEFAULT 'note',
    content     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'open',
    source_url  TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_te_thread ON thread_entries_core(thread_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_te_kind ON thread_entries_core(kind);
CREATE INDEX IF NOT EXISTS idx_te_status ON thread_entries_core(status);
"""


def ensure_research_thread_schema() -> None:
    if not pg_enabled():
        return
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        for stmt in _DDL.strip().split(";"):
            s = stmt.strip()
            if s:
                cur.execute(s)
        # Migrate: add canvas columns if missing
        try:
            cur.execute(
                "ALTER TABLE research_threads_core ADD COLUMN IF NOT EXISTS canvas_markdown TEXT NOT NULL DEFAULT ''"
            )
            cur.execute(
                "ALTER TABLE research_threads_core ADD COLUMN IF NOT EXISTS canvas_updated_at TIMESTAMPTZ"
            )
        except Exception:
            pass
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


def _row_to_dict(cur_description, row) -> dict:
    cols = [d[0] for d in cur_description]
    d: dict = {}
    for k, v in zip(cols, row):
        if isinstance(v, (dt.date, dt.datetime)):
            d[k] = v.isoformat()
        else:
            d[k] = v
    return d


# ── Thread CRUD ──────────────────────────────────────────────────────────────

def list_threads(status: str = "active", thread_type: str = "") -> list[dict]:
    ensure_research_thread_schema()
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        if thread_type:
            cur.execute(
                """SELECT * FROM research_threads_core
                   WHERE status=%s AND thread_type=%s ORDER BY updated_at DESC""",
                (status, thread_type),
            )
        else:
            cur.execute(
                "SELECT * FROM research_threads_core WHERE status=%s ORDER BY updated_at DESC",
                (status,),
            )
        threads = [_row_to_dict(cur.description, r) for r in (cur.fetchall() or [])]
        # Attach counts and last session
        for t in threads:
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM thread_entries_core WHERE thread_id=%s AND kind='question' AND status='open'",
                    (t["id"],),
                )
                row = cur.fetchone()
                t["open_questions"] = int(row[0]) if row else 0
            except Exception:
                t["open_questions"] = 0
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM thread_entries_core WHERE thread_id=%s AND kind='finding'",
                    (t["id"],),
                )
                row = cur.fetchone()
                t["findings_count"] = int(row[0]) if row else 0
            except Exception:
                t["findings_count"] = 0
            try:
                cur.execute(
                    "SELECT COUNT(*) FROM thread_entries_core WHERE thread_id=%s AND kind='blocker' AND status='open'",
                    (t["id"],),
                )
                row = cur.fetchone()
                t["open_blockers"] = int(row[0]) if row else 0
            except Exception:
                t["open_blockers"] = 0
            try:
                cur.execute(
                    """SELECT id, started_at, ended_at, where_left_off
                       FROM thread_sessions_core
                       WHERE thread_id=%s ORDER BY started_at DESC LIMIT 1""",
                    (t["id"],),
                )
                row = cur.fetchone()
                t["last_session"] = _row_to_dict(cur.description, row) if row else None
            except Exception:
                t["last_session"] = None
        return threads
    except Exception as exc:
        LOGGER.warning("list_threads failed: %s", str(exc))
        return []
    finally:
        con.close()


def get_thread(thread_id: int) -> dict | None:
    ensure_research_thread_schema()
    if not pg_enabled():
        return None
    con = pg_connect()
    if con is None:
        return None
    try:
        cur = con.cursor()
        cur.execute("SELECT * FROM research_threads_core WHERE id=%s", (thread_id,))
        row = cur.fetchone()
        if not row:
            return None
        t = _row_to_dict(cur.description, row)
        # Attach entries
        cur.execute(
            "SELECT * FROM thread_entries_core WHERE thread_id=%s ORDER BY created_at DESC",
            (thread_id,),
        )
        t["entries"] = [_row_to_dict(cur.description, r) for r in (cur.fetchall() or [])]
        # Attach sessions
        cur.execute(
            "SELECT * FROM thread_sessions_core WHERE thread_id=%s ORDER BY started_at DESC",
            (thread_id,),
        )
        t["sessions"] = [_row_to_dict(cur.description, r) for r in (cur.fetchall() or [])]
        return t
    except Exception:
        return None
    finally:
        con.close()


def create_thread(title: str, thread_type: str = "research", emoji: str = "",
                  ticker: str = "", thesis: str = "", priority: str = "normal",
                  tags: list | None = None) -> int:
    ensure_research_thread_schema()
    if not pg_enabled():
        return 0
    if not emoji:
        emoji = "\U0001f52c" if thread_type == "research" else "\U0001f4c1"
    con = pg_connect()
    if con is None:
        return 0
    try:
        cur = con.cursor()
        cur.execute(
            """INSERT INTO research_threads_core (title, emoji, thread_type, ticker, thesis, priority, tags)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                str(title or "")[:300],
                str(emoji)[:10],
                str(thread_type or "research")[:20],
                str(ticker or "").upper()[:10],
                str(thesis or "")[:5000],
                str(priority or "normal")[:20],
                json.dumps(tags or []),
            ),
        )
        row = cur.fetchone()
        con.commit()
        return int(row[0]) if row else 0
    except Exception as exc:
        LOGGER.warning("create_thread failed: %s", str(exc))
        try:
            con.rollback()
        except Exception:
            pass
        return 0
    finally:
        con.close()


def update_thread(thread_id: int, **fields) -> bool:
    allowed = {"title", "emoji", "thread_type", "ticker", "thesis", "status", "priority", "tags", "canvas_markdown"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates or not pg_enabled():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        if "tags" in updates and not isinstance(updates["tags"], str):
            updates["tags"] = json.dumps(updates["tags"])
        set_parts = ", ".join(f"{k}=%s" for k in updates)
        vals = list(updates.values()) + [thread_id]
        cur.execute(
            f"UPDATE research_threads_core SET {set_parts}, updated_at=NOW() WHERE id=%s",
            vals,
        )
        con.commit()
        return True
    except Exception as exc:
        LOGGER.warning("update_thread failed: %s", str(exc))
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def delete_thread(thread_id: int) -> bool:
    if not pg_enabled():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM research_threads_core WHERE id=%s", (thread_id,))
        con.commit()
        return True
    except Exception as exc:
        LOGGER.warning("delete_thread failed: %s", str(exc))
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


# ── Sessions ─────────────────────────────────────────────────────────────────

def start_session(thread_id: int) -> int:
    if not pg_enabled():
        return 0
    con = pg_connect()
    if con is None:
        return 0
    try:
        cur = con.cursor()
        # Close any open session for this thread
        cur.execute(
            """UPDATE thread_sessions_core SET ended_at=NOW()
               WHERE thread_id=%s AND ended_at IS NULL""",
            (thread_id,),
        )
        cur.execute(
            "INSERT INTO thread_sessions_core (thread_id) VALUES (%s) RETURNING id",
            (thread_id,),
        )
        row = cur.fetchone()
        # Touch updated_at on thread
        cur.execute(
            "UPDATE research_threads_core SET updated_at=NOW() WHERE id=%s",
            (thread_id,),
        )
        con.commit()
        return int(row[0]) if row else 0
    except Exception as exc:
        LOGGER.warning("start_session failed: %s", str(exc))
        try:
            con.rollback()
        except Exception:
            pass
        return 0
    finally:
        con.close()


def end_session(session_id: int, summary: str = "", where_left_off: str = "") -> bool:
    if not pg_enabled():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        # Calculate duration
        cur.execute("SELECT started_at FROM thread_sessions_core WHERE id=%s", (session_id,))
        row = cur.fetchone()
        duration = 0
        if row and row[0]:
            diff = dt.datetime.now(dt.timezone.utc) - row[0]
            duration = max(1, int(diff.total_seconds() / 60))
        cur.execute(
            """UPDATE thread_sessions_core
               SET ended_at=NOW(), summary=%s, where_left_off=%s, duration_min=%s
               WHERE id=%s""",
            (str(summary or ""), str(where_left_off or ""), duration, session_id),
        )
        # Also update thread's updated_at
        cur.execute(
            """UPDATE research_threads_core SET updated_at=NOW()
               WHERE id=(SELECT thread_id FROM thread_sessions_core WHERE id=%s)""",
            (session_id,),
        )
        con.commit()
        return True
    except Exception as exc:
        LOGGER.warning("end_session failed: %s", str(exc))
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def get_session(session_id: int) -> dict | None:
    if not pg_enabled():
        return None
    con = pg_connect()
    if con is None:
        return None
    try:
        cur = con.cursor()
        cur.execute("SELECT * FROM thread_sessions_core WHERE id=%s", (session_id,))
        row = cur.fetchone()
        if not row:
            return None
        return _row_to_dict(cur.description, row)
    except Exception:
        return None
    finally:
        con.close()


# ── Entries (findings, questions, notes, blockers) ───────────────────────────

def add_entry(thread_id: int, kind: str = "note", content: str = "",
              session_id: int | None = None, source_url: str = "") -> int:
    if not pg_enabled():
        return 0
    con = pg_connect()
    if con is None:
        return 0
    try:
        cur = con.cursor()
        cur.execute(
            """INSERT INTO thread_entries_core (thread_id, session_id, kind, content, source_url)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (
                thread_id,
                session_id if session_id else None,
                str(kind or "note")[:20],
                str(content or "")[:10000],
                str(source_url or "")[:2000],
            ),
        )
        row = cur.fetchone()
        cur.execute(
            "UPDATE research_threads_core SET updated_at=NOW() WHERE id=%s",
            (thread_id,),
        )
        con.commit()
        return int(row[0]) if row else 0
    except Exception as exc:
        LOGGER.warning("add_entry failed: %s", str(exc))
        try:
            con.rollback()
        except Exception:
            pass
        return 0
    finally:
        con.close()


def update_entry(entry_id: int, **fields) -> bool:
    allowed = {"content", "status", "kind", "source_url"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates or not pg_enabled():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        set_parts = ", ".join(f"{k}=%s" for k in updates)
        vals = list(updates.values()) + [entry_id]
        cur.execute(
            f"UPDATE thread_entries_core SET {set_parts}, updated_at=NOW() WHERE id=%s",
            vals,
        )
        con.commit()
        return True
    except Exception as exc:
        LOGGER.warning("update_entry failed: %s", str(exc))
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def delete_entry(entry_id: int) -> bool:
    if not pg_enabled():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM thread_entries_core WHERE id=%s", (entry_id,))
        con.commit()
        return True
    except Exception as exc:
        LOGGER.warning("delete_entry failed: %s", str(exc))
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


# ── Resume helpers ───────────────────────────────────────────────────────────

def get_resume_threads(limit: int = 5) -> list[dict]:
    """Threads with open sessions or recent 'where_left_off' notes."""
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        # Threads with recent sessions that have where_left_off
        cur.execute(
            """SELECT DISTINCT ON (t.id)
                  t.id, t.title, t.emoji, t.thread_type, t.ticker, t.status, t.priority,
                  s.id as session_id, s.where_left_off, s.ended_at, s.started_at
               FROM research_threads_core t
               JOIN thread_sessions_core s ON s.thread_id = t.id
               WHERE t.status = 'active'
                 AND (s.ended_at IS NULL OR s.where_left_off != '')
               ORDER BY t.id, s.started_at DESC
               LIMIT %s""",
            (max(1, min(20, limit)),),
        )
        rows = cur.fetchall() or []
        results = []
        for row in rows:
            cols = [d[0] for d in cur.description]
            d = {}
            for k, v in zip(cols, row):
                if isinstance(v, (dt.date, dt.datetime)):
                    d[k] = v.isoformat()
                else:
                    d[k] = v
            results.append(d)
        return results
    except Exception as exc:
        LOGGER.warning("get_resume_threads failed: %s", str(exc))
        return []
    finally:
        con.close()


# ── AI Canvas ────────────────────────────────────────────────────────────────

def generate_canvas(thread_id: int) -> str:
    """Build a markdown canvas summarizing the thread's current state."""
    t = get_thread(thread_id)
    if not t:
        return ""
    entries = t.get("entries", [])
    sessions = t.get("sessions", [])

    lines = []
    lines.append(f"# {t.get('emoji','')} {t.get('title','')}")
    if t.get("ticker"):
        lines.append(f"**Ticker:** ${t['ticker']}")
    if t.get("thesis"):
        lines.append(f"\n**Thesis:** {t['thesis']}")
    lines.append(f"\n**Status:** {t.get('status','active')} | **Priority:** {t.get('priority','normal')}")

    # Where I left off
    if sessions:
        last = sessions[0]
        if last.get("where_left_off"):
            lines.append(f"\n## Where I Left Off\n{last['where_left_off']}")
        if last.get("summary"):
            lines.append(f"\n**Last session summary:** {last['summary']}")

    # Open questions
    questions = [e for e in entries if e.get("kind") == "question" and e.get("status") == "open"]
    if questions:
        lines.append("\n## Open Questions")
        for q in questions:
            lines.append(f"- {q['content']}")

    # Blockers
    blockers = [e for e in entries if e.get("kind") == "blocker" and e.get("status") == "open"]
    if blockers:
        lines.append("\n## Blockers")
        for b in blockers:
            lines.append(f"- {b['content']}")

    # Key findings
    findings = [e for e in entries if e.get("kind") == "finding"]
    if findings:
        lines.append(f"\n## Key Findings ({len(findings)})")
        for f in findings[:10]:
            lines.append(f"- {f['content']}")
        if len(findings) > 10:
            lines.append(f"- ... and {len(findings)-10} more")

    # Notes
    notes = [e for e in entries if e.get("kind") == "note"]
    if notes:
        lines.append(f"\n## Notes ({len(notes)})")
        for n in notes[:5]:
            lines.append(f"- {n['content']}")
        if len(notes) > 5:
            lines.append(f"- ... and {len(notes)-5} more")

    # Session history summary
    if sessions:
        lines.append(f"\n## Session History ({len(sessions)} sessions)")
        total_min = sum(s.get("duration_min", 0) for s in sessions)
        if total_min:
            lines.append(f"**Total time invested:** {total_min} minutes")
        for s in sessions[:5]:
            dt_str = s.get("started_at", "")[:10] if s.get("started_at") else ""
            dur = s.get("duration_min", 0)
            summary = s.get("summary", "")
            lines.append(f"- {dt_str} ({dur}min): {summary}" if summary else f"- {dt_str} ({dur}min)")

    canvas = "\n".join(lines)

    # Save to DB
    if not pg_enabled():
        return canvas
    con = pg_connect()
    if con is None:
        return canvas
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE research_threads_core SET canvas_markdown=%s, canvas_updated_at=NOW() WHERE id=%s",
            (canvas, thread_id),
        )
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()
    return canvas


# ── Stale / overdue helpers for Today automation ─────────────────────────────

def get_stale_threads(days: int = 7, limit: int = 5) -> list[dict]:
    """Active threads with no activity in N days."""
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            """SELECT id, title, emoji, thread_type, ticker, status, priority, updated_at
               FROM research_threads_core
               WHERE status = 'active'
                 AND updated_at < NOW() - INTERVAL '%s days'
               ORDER BY updated_at ASC
               LIMIT %s""",
            (max(1, days), max(1, min(20, limit))),
        )
        rows = cur.fetchall() or []
        return [_row_to_dict(cur.description, r) for r in rows]
    except Exception as exc:
        LOGGER.warning("get_stale_threads failed: %s", str(exc))
        return []
    finally:
        con.close()
