"""research_notes_service.py — Quick research notes per ticker.

Lightweight note-taking for the Research Canvas on the Ticker Hub.
Each note is timestamped, optionally tagged with sentiment (supports/challenges/neutral),
and can be pinned to stay at the top.
"""
from __future__ import annotations

import logging
from typing import Any

from app.services.postgres_core_service import pg_connect, pg_enabled

LOGGER = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS research_notes_core (
    id          BIGSERIAL PRIMARY KEY,
    ticker      TEXT NOT NULL,
    note        TEXT NOT NULL,
    sentiment   TEXT NOT NULL DEFAULT 'neutral',
    pinned      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_research_notes_ticker ON research_notes_core (ticker, created_at DESC);
"""


def ensure_research_notes_schema() -> None:
    if not pg_enabled():
        return
    try:
        conn = pg_connect()
        with conn.cursor() as cur:
            cur.execute(_DDL)
        conn.commit()
        conn.close()
        LOGGER.info("research_notes_core schema OK")
    except Exception as exc:
        LOGGER.warning("research_notes_core schema failed: %s", exc)


def list_notes(ticker: str) -> list[dict[str, Any]]:
    """Return all notes for a ticker, pinned first then newest first."""
    if not pg_enabled():
        return []
    conn = pg_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, ticker, note, sentiment, pinned, created_at "
                "FROM research_notes_core WHERE UPPER(ticker) = %s "
                "ORDER BY pinned DESC, created_at DESC",
                (ticker.upper(),),
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in (cur.fetchall() or [])]
    finally:
        conn.close()


def create_note(ticker: str, note: str, sentiment: str = "neutral") -> dict[str, Any]:
    """Create a new research note. Returns the created note."""
    if not pg_enabled():
        return {}
    if sentiment not in ("supports", "challenges", "neutral"):
        sentiment = "neutral"
    conn = pg_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO research_notes_core (ticker, note, sentiment) "
                "VALUES (%s, %s, %s) RETURNING id, ticker, note, sentiment, pinned, created_at",
                (ticker.upper(), note.strip(), sentiment),
            )
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            conn.commit()
            return dict(zip(cols, row)) if row else {}
    finally:
        conn.close()


def update_note(note_id: int, *, pinned: bool | None = None, sentiment: str | None = None) -> bool:
    """Update pin status or sentiment of a note."""
    if not pg_enabled():
        return False
    parts, vals = [], []
    if pinned is not None:
        parts.append("pinned = %s")
        vals.append(pinned)
    if sentiment is not None and sentiment in ("supports", "challenges", "neutral"):
        parts.append("sentiment = %s")
        vals.append(sentiment)
    if not parts:
        return False
    vals.append(note_id)
    conn = pg_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE research_notes_core SET {', '.join(parts)} WHERE id = %s", vals)
            conn.commit()
            return cur.rowcount > 0
    finally:
        conn.close()


def delete_note(note_id: int) -> bool:
    """Delete a research note."""
    if not pg_enabled():
        return False
    conn = pg_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM research_notes_core WHERE id = %s", (note_id,))
            conn.commit()
            return cur.rowcount > 0
    finally:
        conn.close()
