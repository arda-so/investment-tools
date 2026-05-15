"""thinking_service.py — Thinking Lab: thought chains, quotes, bookmarks, books, mental models.

Tables:
  thinking_chains_core  — chain/quote/bookmark/book/mental_model definitions
  thinking_nodes_core   — tree-structured nodes within each chain
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from app.services.postgres_core_service import pg_connect, pg_enabled

LOGGER = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS thinking_chains_core (
    id          BIGSERIAL PRIMARY KEY,
    chain_type  TEXT NOT NULL DEFAULT 'chain',
    title       TEXT NOT NULL DEFAULT '',
    emoji       TEXT NOT NULL DEFAULT '\U0001f4a1',
    status      TEXT NOT NULL DEFAULT 'active',
    tags        TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_tc_chain_type ON thinking_chains_core(chain_type);
CREATE INDEX IF NOT EXISTS idx_tc_status ON thinking_chains_core(status);

CREATE TABLE IF NOT EXISTS thinking_nodes_core (
    id              BIGSERIAL PRIMARY KEY,
    chain_id        BIGINT NOT NULL REFERENCES thinking_chains_core(id) ON DELETE CASCADE,
    parent_node_id  BIGINT REFERENCES thinking_nodes_core(id) ON DELETE CASCADE,
    node_type       TEXT NOT NULL DEFAULT 'idea',
    content         TEXT NOT NULL,
    source_url      TEXT NOT NULL DEFAULT '',
    sort_order      INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_tn_chain ON thinking_nodes_core(chain_id);
CREATE INDEX IF NOT EXISTS idx_tn_parent ON thinking_nodes_core(parent_node_id);
"""


# ── Schema ───────────────────────────────────────────────────────────────────

def ensure_thinking_schema() -> None:
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


# ── Helpers ──────────────────────────────────────────────────────────────────

def _row_to_dict(cur_description, row) -> dict:
    cols = [d[0] for d in cur_description]
    d: dict = {}
    for k, v in zip(cols, row):
        if isinstance(v, (dt.date, dt.datetime)):
            d[k] = v.isoformat()
        else:
            d[k] = v
    return d


# ── Chain CRUD ───────────────────────────────────────────────────────────────

def list_chains(chain_type: str | None = None, status: str = "active") -> list[dict]:
    """List chains, optionally filtered by type and status. Includes node_count."""
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        base = """SELECT c.*, COALESCE(n.cnt, 0) AS node_count
                  FROM thinking_chains_core c
                  LEFT JOIN (SELECT chain_id, COUNT(*) AS cnt FROM thinking_nodes_core GROUP BY chain_id) n
                    ON n.chain_id = c.id"""
        if chain_type:
            cur.execute(
                base + " WHERE c.status = %s AND c.chain_type = %s ORDER BY c.updated_at DESC",
                (status, chain_type),
            )
        else:
            cur.execute(
                base + " WHERE c.status = %s ORDER BY c.updated_at DESC",
                (status,),
            )
        rows = cur.fetchall()
        return [_row_to_dict(cur.description, r) for r in rows]
    except Exception:
        LOGGER.exception("list_chains failed")
        return []
    finally:
        con.close()


def get_chain(chain_id: int) -> dict | None:
    """Get a chain with its full node tree."""
    if not pg_enabled():
        return None
    con = pg_connect()
    if con is None:
        return None
    try:
        cur = con.cursor()
        cur.execute("SELECT * FROM thinking_chains_core WHERE id = %s", (chain_id,))
        row = cur.fetchone()
        if not row:
            return None
        chain = _row_to_dict(cur.description, row)
        chain["nodes"] = get_node_tree(chain_id)
        return chain
    except Exception:
        LOGGER.exception("get_chain failed for id=%s", chain_id)
        return None
    finally:
        con.close()


def create_chain(
    chain_type: str = "chain",
    title: str = "",
    emoji: str = "\U0001f4a1",
    tags: str = "",
    source: str = "",
    notes: str = "",
) -> int:
    """Create a new chain. Returns chain id, 0 on failure."""
    if not pg_enabled():
        return 0
    con = pg_connect()
    if con is None:
        return 0
    try:
        cur = con.cursor()
        cur.execute(
            """INSERT INTO thinking_chains_core (chain_type, title, emoji, tags, source, notes)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (chain_type, title, emoji, tags, source, notes),
        )
        row = cur.fetchone()
        con.commit()
        return int(row[0]) if row else 0
    except Exception:
        LOGGER.exception("create_chain failed")
        try:
            con.rollback()
        except Exception:
            pass
        return 0
    finally:
        con.close()


def update_chain(chain_id: int, **fields: Any) -> bool:
    """Update any fields on a chain. Sets updated_at automatically."""
    if not pg_enabled():
        return False
    allowed = {"chain_type", "title", "emoji", "status", "tags", "source", "notes"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        set_parts = [f"{k} = %s" for k in updates]
        set_parts.append("updated_at = NOW()")
        vals = list(updates.values()) + [chain_id]
        cur.execute(
            f"UPDATE thinking_chains_core SET {', '.join(set_parts)} WHERE id = %s",
            vals,
        )
        con.commit()
        return True
    except Exception:
        LOGGER.exception("update_chain failed for id=%s", chain_id)
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def delete_chain(chain_id: int) -> bool:
    """Hard-delete a chain (CASCADE handles nodes)."""
    if not pg_enabled():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM thinking_chains_core WHERE id = %s", (chain_id,))
        con.commit()
        return True
    except Exception:
        LOGGER.exception("delete_chain failed for id=%s", chain_id)
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def archive_chain(chain_id: int) -> bool:
    """Set chain status to 'archived'."""
    return update_chain(chain_id, status="archived")


# ── Node CRUD ────────────────────────────────────────────────────────────────

def add_node(
    chain_id: int,
    parent_node_id: int | None = None,
    node_type: str = "idea",
    content: str = "",
    source_url: str = "",
    sort_order: int | None = None,
) -> int:
    """Add a node to a chain. Auto-sets sort_order if not provided. Returns node id, 0 on failure."""
    if not pg_enabled():
        return 0
    con = pg_connect()
    if con is None:
        return 0
    try:
        cur = con.cursor()
        # Auto-compute sort_order as max(sort_order)+1 among siblings
        if sort_order is None:
            if parent_node_id is not None:
                cur.execute(
                    "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM thinking_nodes_core "
                    "WHERE chain_id = %s AND parent_node_id = %s",
                    (chain_id, parent_node_id),
                )
            else:
                cur.execute(
                    "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM thinking_nodes_core "
                    "WHERE chain_id = %s AND parent_node_id IS NULL",
                    (chain_id,),
                )
            row = cur.fetchone()
            sort_order = int(row[0]) if row else 0

        cur.execute(
            """INSERT INTO thinking_nodes_core (chain_id, parent_node_id, node_type, content, source_url, sort_order)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (chain_id, parent_node_id, node_type, content, source_url, sort_order),
        )
        row = cur.fetchone()
        node_id = int(row[0]) if row else 0

        # Touch parent chain's updated_at
        cur.execute(
            "UPDATE thinking_chains_core SET updated_at = NOW() WHERE id = %s",
            (chain_id,),
        )
        con.commit()
        return node_id
    except Exception:
        LOGGER.exception("add_node failed for chain_id=%s", chain_id)
        try:
            con.rollback()
        except Exception:
            pass
        return 0
    finally:
        con.close()


def update_node(
    node_id: int,
    content: str | None = None,
    node_type: str | None = None,
    sort_order: int | None = None,
    source_url: str | None = None,
) -> bool:
    """Update node fields."""
    if not pg_enabled():
        return False
    updates: dict[str, Any] = {}
    if content is not None:
        updates["content"] = content
    if node_type is not None:
        updates["node_type"] = node_type
    if sort_order is not None:
        updates["sort_order"] = sort_order
    if source_url is not None:
        updates["source_url"] = source_url
    if not updates:
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        set_parts = [f"{k} = %s" for k in updates]
        vals = list(updates.values()) + [node_id]
        cur.execute(
            f"UPDATE thinking_nodes_core SET {', '.join(set_parts)} WHERE id = %s",
            vals,
        )
        # Touch parent chain's updated_at
        cur.execute(
            "UPDATE thinking_chains_core SET updated_at = NOW() "
            "WHERE id = (SELECT chain_id FROM thinking_nodes_core WHERE id = %s)",
            (node_id,),
        )
        con.commit()
        return True
    except Exception:
        LOGGER.exception("update_node failed for id=%s", node_id)
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def delete_node(node_id: int) -> bool:
    """Delete a node and all its children (CASCADE handles subtree)."""
    if not pg_enabled():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        # Touch parent chain's updated_at before deleting
        cur.execute(
            "UPDATE thinking_chains_core SET updated_at = NOW() "
            "WHERE id = (SELECT chain_id FROM thinking_nodes_core WHERE id = %s)",
            (node_id,),
        )
        cur.execute("DELETE FROM thinking_nodes_core WHERE id = %s", (node_id,))
        con.commit()
        return True
    except Exception:
        LOGGER.exception("delete_node failed for id=%s", node_id)
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def reorder_node(node_id: int, new_sort_order: int) -> bool:
    """Change a node's sort_order."""
    return update_node(node_id, sort_order=new_sort_order)


def get_node_tree(chain_id: int) -> list[dict]:
    """Fetch the full node tree for a chain using a recursive CTE.

    Returns a nested structure: each node dict has a ``children`` list.
    """
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            """
            WITH RECURSIVE tree AS (
                SELECT id, chain_id, parent_node_id, node_type, content,
                       source_url, sort_order, created_at,
                       0 AS depth,
                       ARRAY[sort_order, id::int] AS path
                FROM thinking_nodes_core
                WHERE chain_id = %s AND parent_node_id IS NULL
                UNION ALL
                SELECT n.id, n.chain_id, n.parent_node_id, n.node_type, n.content,
                       n.source_url, n.sort_order, n.created_at,
                       t.depth + 1,
                       t.path || ARRAY[n.sort_order, n.id::int]
                FROM thinking_nodes_core n
                JOIN tree t ON n.parent_node_id = t.id
            )
            SELECT * FROM tree ORDER BY path
            """,
            (chain_id,),
        )
        rows = cur.fetchall()
        if not rows:
            return []

        # Build flat list of dicts
        flat: list[dict] = []
        for r in rows:
            d = _row_to_dict(cur.description, r)
            d["children"] = []
            flat.append(d)

        # Build nested tree
        by_id: dict[int, dict] = {n["id"]: n for n in flat}
        roots: list[dict] = []
        for node in flat:
            pid = node.get("parent_node_id")
            if pid is None or pid not in by_id:
                roots.append(node)
            else:
                by_id[pid]["children"].append(node)

        # Remove internal fields from output
        def _clean(node: dict) -> dict:
            node.pop("depth", None)
            node.pop("path", None)
            for child in node.get("children", []):
                _clean(child)
            return node

        return [_clean(r) for r in roots]
    except Exception:
        LOGGER.exception("get_node_tree failed for chain_id=%s", chain_id)
        return []
    finally:
        con.close()


# ── Lineage / Aisle View ─────────────────────────────────────────────────────

def get_node_ancestors(node_id: int) -> list[dict]:
    """Trace ancestors from a node back to the root. Returns list ordered root→leaf."""
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute("""
            WITH RECURSIVE lineage AS (
                SELECT id, chain_id, parent_node_id, node_type, content, source_url, sort_order, created_at, 0 AS depth
                FROM thinking_nodes_core WHERE id = %s
                UNION ALL
                SELECT n.id, n.chain_id, n.parent_node_id, n.node_type, n.content, n.source_url, n.sort_order, n.created_at, l.depth + 1
                FROM thinking_nodes_core n
                JOIN lineage l ON l.parent_node_id = n.id
            )
            SELECT * FROM lineage ORDER BY depth DESC
        """, (node_id,))
        rows = cur.fetchall() or []
        result = []
        for row in rows:
            d = _row_to_dict(cur.description, row)
            d.pop("depth", None)
            result.append(d)
        return result
    except Exception:
        LOGGER.exception("get_node_ancestors failed for node_id=%s", node_id)
        return []
    finally:
        con.close()


def get_all_leaf_nodes(chain_id: int) -> list[dict]:
    """Get all leaf nodes (no children) in a chain — these are the 'final decisions'."""
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute("""
            SELECT n.id, n.chain_id, n.parent_node_id, n.node_type, n.content, n.source_url, n.sort_order, n.created_at
            FROM thinking_nodes_core n
            LEFT JOIN thinking_nodes_core child ON child.parent_node_id = n.id
            WHERE n.chain_id = %s AND child.id IS NULL
            ORDER BY n.created_at
        """, (chain_id,))
        rows = cur.fetchall() or []
        return [_row_to_dict(cur.description, r) for r in rows]
    except Exception:
        LOGGER.exception("get_all_leaf_nodes failed")
        return []
    finally:
        con.close()
