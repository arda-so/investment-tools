"""work_graph_service.py — Work Graph: connects any work object to any other.

Table:
  work_links_core — bidirectional links between objects (record, thread, project,
                    playbook, playbook_run, proposal, ticker, entry, session)

Every link stores (a_type, a_id, b_type, b_id, relation).
Links are bidirectional: querying either side returns the connection.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from app.services.postgres_core_service import pg_connect, pg_enabled

LOGGER = logging.getLogger(__name__)

# Valid object types for linking
LINKABLE_TYPES = {
    "record",       # investment_records_core
    "thread",       # research_threads_core
    "entry",        # thread_entries_core
    "session",      # thread_sessions_core
    "playbook",     # playbooks_core
    "playbook_run", # playbook_runs_core
    "proposal",     # action_proposals_core
    "ticker",       # virtual — just a ticker symbol, no table row
    "chain",        # thinking_chains_core
}

_DDL = """
CREATE TABLE IF NOT EXISTS work_links_core (
    id          BIGSERIAL PRIMARY KEY,
    a_type      TEXT NOT NULL,
    a_id        TEXT NOT NULL,
    b_type      TEXT NOT NULL,
    b_id        TEXT NOT NULL,
    relation    TEXT NOT NULL DEFAULT 'related',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_wl_a ON work_links_core(a_type, a_id);
CREATE INDEX IF NOT EXISTS idx_wl_b ON work_links_core(b_type, b_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wl_pair ON work_links_core(a_type, a_id, b_type, b_id);
"""


def ensure_work_graph_schema() -> None:
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


def _normalize(a_type: str, a_id: str, b_type: str, b_id: str):
    """Canonical order so (A,B) and (B,A) don't create duplicates."""
    key_a = (a_type, str(a_id))
    key_b = (b_type, str(b_id))
    if key_a > key_b:
        return b_type, str(b_id), a_type, str(a_id)
    return a_type, str(a_id), b_type, str(b_id)


# ── Link CRUD ────────────────────────────────────────────────────────────────

def add_link(a_type: str, a_id: str | int, b_type: str, b_id: str | int,
             relation: str = "related") -> int:
    """Create a link between two objects. Returns link id, 0 on failure."""
    if not pg_enabled():
        return 0
    at, ai, bt, bi = _normalize(a_type, str(a_id), b_type, str(b_id))
    con = pg_connect()
    if con is None:
        return 0
    try:
        cur = con.cursor()
        cur.execute(
            """INSERT INTO work_links_core (a_type, a_id, b_type, b_id, relation)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (a_type, a_id, b_type, b_id) DO UPDATE SET relation=EXCLUDED.relation
               RETURNING id""",
            (at, ai, bt, bi, str(relation or "related")[:50]),
        )
        row = cur.fetchone()
        con.commit()
        return int(row[0]) if row else 0
    except Exception as exc:
        LOGGER.warning("add_link failed: %s", str(exc))
        try:
            con.rollback()
        except Exception:
            pass
        return 0
    finally:
        con.close()


def remove_link(a_type: str, a_id: str | int, b_type: str, b_id: str | int) -> bool:
    if not pg_enabled():
        return False
    at, ai, bt, bi = _normalize(a_type, str(a_id), b_type, str(b_id))
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            "DELETE FROM work_links_core WHERE a_type=%s AND a_id=%s AND b_type=%s AND b_id=%s",
            (at, ai, bt, bi),
        )
        con.commit()
        return True
    except Exception as exc:
        LOGGER.warning("remove_link failed: %s", str(exc))
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def remove_link_by_id(link_id: int) -> bool:
    if not pg_enabled():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM work_links_core WHERE id=%s", (link_id,))
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


def get_links(obj_type: str, obj_id: str | int, filter_type: str = "") -> list[dict]:
    """Get all links for an object. Optionally filter by the other side's type."""
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    oid = str(obj_id)
    try:
        cur = con.cursor()
        if filter_type:
            cur.execute(
                """SELECT * FROM work_links_core
                   WHERE (a_type=%s AND a_id=%s AND b_type=%s)
                      OR (b_type=%s AND b_id=%s AND a_type=%s)
                   ORDER BY created_at DESC""",
                (obj_type, oid, filter_type, obj_type, oid, filter_type),
            )
        else:
            cur.execute(
                """SELECT * FROM work_links_core
                   WHERE (a_type=%s AND a_id=%s) OR (b_type=%s AND b_id=%s)
                   ORDER BY created_at DESC""",
                (obj_type, oid, obj_type, oid),
            )
        links = []
        for row in (cur.fetchall() or []):
            link = _row_to_dict(cur.description, row)
            # Determine "other" side
            if link["a_type"] == obj_type and link["a_id"] == oid:
                link["other_type"] = link["b_type"]
                link["other_id"] = link["b_id"]
            else:
                link["other_type"] = link["a_type"]
                link["other_id"] = link["a_id"]
            links.append(link)
        return links
    except Exception as exc:
        LOGGER.warning("get_links failed: %s", str(exc))
        return []
    finally:
        con.close()


# ── Resolve linked objects for display ───────────────────────────────────────

def resolve_linked_objects(links: list[dict]) -> list[dict]:
    """Enrich links with title/emoji from the linked objects."""
    if not links or not pg_enabled():
        return links
    con = pg_connect()
    if con is None:
        return links
    try:
        cur = con.cursor()
        for link in links:
            otype = link.get("other_type", "")
            oid = link.get("other_id", "")
            link["other_title"] = ""
            link["other_emoji"] = ""
            link["other_status"] = ""
            try:
                if otype == "record":
                    cur.execute(
                        "SELECT title, kind, status, COALESCE(url,'') FROM investment_records_core WHERE id=%s",
                        (int(oid),),
                    )
                    row = cur.fetchone()
                    if row:
                        link["other_title"] = row[0]
                        link["other_emoji"] = {"task": "\u2705", "note": "\U0001f4dd", "question": "\u2753",
                                               "reference": "\U0001f4ce", "inbox": "\U0001f4ec"}.get(row[1], "\U0001f4dd")
                        link["other_status"] = row[2]
                        if row[3]:
                            link["other_url"] = row[3]
                elif otype == "thread":
                    cur.execute(
                        "SELECT title, emoji, status, ticker FROM research_threads_core WHERE id=%s",
                        (int(oid),),
                    )
                    row = cur.fetchone()
                    if row:
                        link["other_title"] = row[0]
                        link["other_emoji"] = row[1] or "\U0001f52c"
                        link["other_status"] = row[2]
                        link["other_ticker"] = row[3]
                elif otype == "playbook":
                    cur.execute(
                        "SELECT name, emoji FROM playbooks_core WHERE id=%s",
                        (int(oid),),
                    )
                    row = cur.fetchone()
                    if row:
                        link["other_title"] = row[0]
                        link["other_emoji"] = row[1] or "\U0001f4d6"
                elif otype == "playbook_run":
                    cur.execute(
                        """SELECT r.id, r.status, r.ticker, p.name, p.emoji
                           FROM playbook_runs_core r
                           JOIN playbooks_core p ON p.id = r.playbook_id
                           WHERE r.id=%s""",
                        (int(oid),),
                    )
                    row = cur.fetchone()
                    if row:
                        link["other_title"] = f"Run #{row[0]} — {row[3]}"
                        link["other_emoji"] = row[4] or "\U0001f4d6"
                        link["other_status"] = row[1]
                elif otype == "proposal":
                    cur.execute(
                        "SELECT ticker, action_type, headline FROM action_proposals_core WHERE id=%s",
                        (int(oid),),
                    )
                    row = cur.fetchone()
                    if row:
                        link["other_title"] = f"${row[0]} — {row[2] or row[1]}"
                        link["other_emoji"] = "\U0001f4a1"
                elif otype == "entry":
                    cur.execute(
                        "SELECT kind, content FROM thread_entries_core WHERE id=%s",
                        (int(oid),),
                    )
                    row = cur.fetchone()
                    if row:
                        kind_icons = {"finding": "\U0001f4a1", "question": "\u2753", "note": "\U0001f4dd", "blocker": "\U0001f6a7"}
                        link["other_title"] = (row[1] or "")[:80]
                        link["other_emoji"] = kind_icons.get(row[0], "\U0001f4dd")
                elif otype == "chain":
                    cur.execute(
                        "SELECT title, emoji, chain_type FROM thinking_chains_core WHERE id=%s",
                        (int(oid),),
                    )
                    row = cur.fetchone()
                    if row:
                        link["other_title"] = row[0]
                        link["other_emoji"] = row[1] or "\U0001f4a1"
                elif otype == "ticker":
                    link["other_title"] = f"${oid}"
                    link["other_emoji"] = "\U0001f4c8"
            except Exception:
                pass
        return links
    except Exception:
        return links
    finally:
        con.close()


def get_object_graph(obj_type: str, obj_id: str | int, max_depth: int = 4) -> dict:
    """BFS traversal of the work graph starting from an object.
    Returns {nodes: [{type, id, title, emoji}], edges: [{from_type, from_id, to_type, to_id, relation}]}
    """
    if not pg_enabled():
        return {"nodes": [], "edges": []}

    visited: set[tuple[str, str]] = set()
    queue: list[tuple[str, str, int]] = [(obj_type, str(obj_id), 0)]
    nodes: list[dict] = []
    edges: list[dict] = []

    while queue:
        otype, oid, depth = queue.pop(0)
        key = (otype, oid)
        if key in visited:
            continue
        visited.add(key)
        nodes.append({"type": otype, "id": oid, "depth": depth})

        if depth >= max_depth:
            continue

        links = get_links(otype, oid)
        for link in links:
            other_type = link.get("other_type", "")
            other_id = link.get("other_id", "")
            other_key = (other_type, other_id)
            if other_key not in visited:
                edges.append({
                    "from_type": otype, "from_id": oid,
                    "to_type": other_type, "to_id": other_id,
                    "relation": link.get("relation", "related"),
                })
                queue.append((other_type, other_id, depth + 1))

    # Resolve titles/emojis for all nodes
    resolved = resolve_linked_objects([
        {"other_type": n["type"], "other_id": n["id"]} for n in nodes
    ])
    for i, n in enumerate(nodes):
        if i < len(resolved):
            n["title"] = resolved[i].get("other_title", "")
            n["emoji"] = resolved[i].get("other_emoji", "")

    return {"nodes": nodes, "edges": edges}
