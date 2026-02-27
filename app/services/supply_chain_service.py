from __future__ import annotations

from typing import Any

from app.core.db import core_conn as _conn_core
from app.services.postgres_core_service import core_backend, pg_connect


def ensure_supply_chain_schema() -> dict[str, Any]:
    ddl_companies = """
        CREATE TABLE IF NOT EXISTS companies (
            id BIGSERIAL PRIMARY KEY,
            ticker VARCHAR(16) NOT NULL UNIQUE,
            name TEXT NOT NULL,
            market_cap NUMERIC(20,2),
            sector TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """
    ddl_relationships = """
        CREATE TABLE IF NOT EXISTS relationships (
            id BIGSERIAL PRIMARY KEY,
            source_company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            target_company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            relationship_type TEXT NOT NULL CHECK (relationship_type IN ('supplies', 'partners_with')),
            evidence_text TEXT NOT NULL,
            source_url TEXT,
            confidence_score INTEGER NOT NULL CHECK (confidence_score BETWEEN 1 AND 100),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE (source_company_id, target_company_id, relationship_type, evidence_text)
        )
    """
    if core_backend() == "postgres":
        con = pg_connect()
        if con is None:
            return {"ok": False, "error": "pg_not_available"}
        try:
            cur = con.cursor()
            cur.execute(ddl_companies)
            cur.execute(ddl_relationships)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_companies_ticker ON companies(ticker)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_relationships_source ON relationships(source_company_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_relationships_target ON relationships(target_company_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_relationships_confidence ON relationships(confidence_score)")
            con.commit()
            return {"ok": True}
        except Exception as exc:
            try:
                con.rollback()
            except Exception:
                pass
            return {"ok": False, "error": str(exc)}
        finally:
            con.close()

    con_sq = _conn_core()
    try:
        con_sq.execute(
            """
            CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                market_cap REAL,
                sector TEXT,
                created_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        con_sq.execute(
            """
            CREATE TABLE IF NOT EXISTS relationships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_company_id INTEGER NOT NULL,
                target_company_id INTEGER NOT NULL,
                relationship_type TEXT NOT NULL,
                evidence_text TEXT NOT NULL,
                source_url TEXT,
                confidence_score INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT '',
                UNIQUE (source_company_id, target_company_id, relationship_type, evidence_text)
            )
            """
        )
        con_sq.execute("CREATE INDEX IF NOT EXISTS idx_companies_ticker ON companies(ticker)")
        con_sq.execute("CREATE INDEX IF NOT EXISTS idx_relationships_source ON relationships(source_company_id)")
        con_sq.execute("CREATE INDEX IF NOT EXISTS idx_relationships_target ON relationships(target_company_id)")
        con_sq.execute("CREATE INDEX IF NOT EXISTS idx_relationships_confidence ON relationships(confidence_score)")
        con_sq.commit()
        return {"ok": True}
    finally:
        con_sq.close()


def _edge_style(confidence_score: int) -> dict[str, Any]:
    c = int(confidence_score or 0)
    if c > 90:
        return {"color": "#16a34a", "width": 3.5, "dash": "0"}
    if c < 50:
        return {"color": "#eab308", "width": 1.6, "dash": "6,4"}
    return {"color": "#f97316", "width": 2.4, "dash": "0"}


def _fetch_network_rows_pg(ticker: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    con = pg_connect()
    if con is None:
        return ([], [])
    try:
        cur = con.cursor()
        cur.execute("SELECT id, ticker, name, market_cap, sector FROM companies WHERE UPPER(ticker) = UPPER(%s) LIMIT 1", (ticker,))
        root = cur.fetchone()
        if not root:
            return ([], [])
        root_id = int((root[0] if isinstance(root, (tuple, list)) else root["id"]) or 0)

        # First and second degree relationships from the anchor ticker.
        cur.execute(
            """
            WITH first_edges AS (
              SELECT id, source_company_id, target_company_id, relationship_type, evidence_text, source_url, confidence_score
              FROM relationships
              WHERE source_company_id = %s OR target_company_id = %s
            ),
            first_nodes AS (
              SELECT source_company_id AS id FROM first_edges
              UNION
              SELECT target_company_id AS id FROM first_edges
            ),
            second_edges AS (
              SELECT r.id, r.source_company_id, r.target_company_id, r.relationship_type, r.evidence_text, r.source_url, r.confidence_score
              FROM relationships r
              JOIN first_nodes fn ON r.source_company_id = fn.id OR r.target_company_id = fn.id
            ),
            all_edges AS (
              SELECT * FROM first_edges
              UNION
              SELECT * FROM second_edges
            )
            SELECT DISTINCT id, source_company_id, target_company_id, relationship_type, evidence_text, source_url, confidence_score
            FROM all_edges
            """,
            (root_id, root_id),
        )
        edge_rows = cur.fetchall() or []

        node_ids: set[int] = {root_id}
        for r in edge_rows:
            src = int((r[1] if isinstance(r, (tuple, list)) else r["source_company_id"]) or 0)
            tgt = int((r[2] if isinstance(r, (tuple, list)) else r["target_company_id"]) or 0)
            if src > 0:
                node_ids.add(src)
            if tgt > 0:
                node_ids.add(tgt)
        if not node_ids:
            return ([], [])
        cur.execute(
            "SELECT id, ticker, name, market_cap, sector FROM companies WHERE id = ANY(%s::bigint[])",
            (list(node_ids),),
        )
        node_rows = cur.fetchall() or []
        nodes = [
            {
                "id": str(int((n[0] if isinstance(n, (tuple, list)) else n["id"]) or 0)),
                "ticker": str((n[1] if isinstance(n, (tuple, list)) else n["ticker"]) or ""),
                "name": str((n[2] if isinstance(n, (tuple, list)) else n["name"]) or ""),
                "market_cap": float((n[3] if isinstance(n, (tuple, list)) else n["market_cap"]) or 0.0)
                if (n[3] if isinstance(n, (tuple, list)) else n["market_cap"]) is not None
                else None,
                "sector": str((n[4] if isinstance(n, (tuple, list)) else n["sector"]) or ""),
            }
            for n in node_rows
        ]
        edges = []
        for r in edge_rows:
            edge_id = int((r[0] if isinstance(r, (tuple, list)) else r["id"]) or 0)
            src = int((r[1] if isinstance(r, (tuple, list)) else r["source_company_id"]) or 0)
            tgt = int((r[2] if isinstance(r, (tuple, list)) else r["target_company_id"]) or 0)
            c = int((r[6] if isinstance(r, (tuple, list)) else r["confidence_score"]) or 0)
            edge = {
                "id": str(edge_id),
                "source": str(src),
                "target": str(tgt),
                "relationship_type": str((r[3] if isinstance(r, (tuple, list)) else r["relationship_type"]) or ""),
                "evidence_text": str((r[4] if isinstance(r, (tuple, list)) else r["evidence_text"]) or ""),
                "source_url": str((r[5] if isinstance(r, (tuple, list)) else r["source_url"]) or ""),
                "confidence_score": c,
            }
            edge["style"] = _edge_style(c)
            edges.append(edge)
        return (nodes, edges)
    finally:
        con.close()


def _fetch_network_rows_sqlite(ticker: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    con = _conn_core()
    try:
        cur = con.execute("SELECT id, ticker, name, market_cap, sector FROM companies WHERE UPPER(ticker) = UPPER(?) LIMIT 1", (ticker,))
        root = cur.fetchone()
        if not root:
            return ([], [])
        root_id = int(root["id"] or 0)
        edge_rows = con.execute(
            """
            WITH first_edges AS (
              SELECT id, source_company_id, target_company_id, relationship_type, evidence_text, source_url, confidence_score
              FROM relationships
              WHERE source_company_id = ? OR target_company_id = ?
            ),
            first_nodes AS (
              SELECT source_company_id AS id FROM first_edges
              UNION
              SELECT target_company_id AS id FROM first_edges
            ),
            second_edges AS (
              SELECT r.id, r.source_company_id, r.target_company_id, r.relationship_type, r.evidence_text, r.source_url, r.confidence_score
              FROM relationships r
              JOIN first_nodes fn ON r.source_company_id = fn.id OR r.target_company_id = fn.id
            ),
            all_edges AS (
              SELECT * FROM first_edges
              UNION
              SELECT * FROM second_edges
            )
            SELECT DISTINCT id, source_company_id, target_company_id, relationship_type, evidence_text, source_url, confidence_score
            FROM all_edges
            """,
            (root_id, root_id),
        ).fetchall()
        node_ids: set[int] = {root_id}
        for r in edge_rows:
            if int(r["source_company_id"] or 0) > 0:
                node_ids.add(int(r["source_company_id"]))
            if int(r["target_company_id"] or 0) > 0:
                node_ids.add(int(r["target_company_id"]))
        if not node_ids:
            return ([], [])
        placeholders = ",".join(["?"] * len(node_ids))
        node_rows = con.execute(
            f"SELECT id, ticker, name, market_cap, sector FROM companies WHERE id IN ({placeholders})",
            tuple(node_ids),
        ).fetchall()
        nodes = [
            {
                "id": str(int(n["id"] or 0)),
                "ticker": str(n["ticker"] or ""),
                "name": str(n["name"] or ""),
                "market_cap": float(n["market_cap"]) if n["market_cap"] is not None else None,
                "sector": str(n["sector"] or ""),
            }
            for n in node_rows
        ]
        edges = []
        for r in edge_rows:
            c = int(r["confidence_score"] or 0)
            edge = {
                "id": str(int(r["id"] or 0)),
                "source": str(int(r["source_company_id"] or 0)),
                "target": str(int(r["target_company_id"] or 0)),
                "relationship_type": str(r["relationship_type"] or ""),
                "evidence_text": str(r["evidence_text"] or ""),
                "source_url": str(r["source_url"] or ""),
                "confidence_score": c,
            }
            edge["style"] = _edge_style(c)
            edges.append(edge)
        return (nodes, edges)
    finally:
        con.close()


def get_network_graph(ticker: str) -> dict[str, Any]:
    t = str(ticker or "").strip().upper()
    if not t:
        return {"nodes": [], "edges": []}
    if core_backend() == "postgres":
        nodes, edges = _fetch_network_rows_pg(t)
    else:
        nodes, edges = _fetch_network_rows_sqlite(t)
    return {"ticker": t, "nodes": nodes, "edges": edges}


def _upsert_company_pg(ticker: str, name: str = "") -> int:
    con = pg_connect()
    if con is None:
        return 0
    try:
        cur = con.cursor()
        cur.execute("SELECT id FROM companies WHERE UPPER(ticker)=UPPER(%s) LIMIT 1", (ticker,))
        row = cur.fetchone()
        if row:
            cid = int((row[0] if isinstance(row, (tuple, list)) else row["id"]) or 0)
            if name:
                cur.execute("UPDATE companies SET name=%s WHERE id=%s", (name[:200], cid))
            con.commit()
            return cid
        cur.execute(
            "INSERT INTO companies(ticker, name) VALUES (%s, %s) RETURNING id",
            (str(ticker or "").upper()[:16], (name or ticker)[:200]),
        )
        row2 = cur.fetchone()
        con.commit()
        return int((row2[0] if isinstance(row2, (tuple, list)) else row2["id"]) or 0) if row2 else 0
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return 0
    finally:
        con.close()


def _upsert_company_sqlite(ticker: str, name: str = "") -> int:
    con = _conn_core()
    try:
        row = con.execute("SELECT id FROM companies WHERE UPPER(ticker)=UPPER(?) LIMIT 1", (ticker,)).fetchone()
        if row:
            cid = int(row["id"] or 0)
            if name:
                con.execute("UPDATE companies SET name=? WHERE id=?", ((name or ticker)[:200], cid))
                con.commit()
            return cid
        cur = con.execute(
            "INSERT INTO companies(ticker, name, created_at) VALUES (?, ?, datetime('now'))",
            (str(ticker or "").upper()[:16], (name or ticker)[:200]),
        )
        con.commit()
        return int(cur.lastrowid or 0)
    except Exception:
        return 0
    finally:
        con.close()


def ingest_sec_relationship_to_network(
    *,
    source_ticker: str,
    target_ticker: str,
    relationship_type: str,
    evidence_text: str,
    source_url: str = "",
    confidence_score: int = 70,
    source_name: str = "",
    target_name: str = "",
) -> bool:
    ensure_supply_chain_schema()
    src_tk = str(source_ticker or "").strip().upper()[:16]
    tgt_tk = str(target_ticker or "").strip().upper()[:16]
    if not src_tk or not tgt_tk or src_tk == tgt_tk:
        return False
    rel = str(relationship_type or "").strip().lower()
    if rel not in {"supplies", "partners_with"}:
        return False
    conf = max(1, min(100, int(confidence_score or 0)))
    ev = str(evidence_text or "").strip()[:1200]
    src_url = str(source_url or "").strip()[:1000]

    if core_backend() == "postgres":
        sid = _upsert_company_pg(src_tk, source_name or src_tk)
        tid = _upsert_company_pg(tgt_tk, target_name or tgt_tk)
        if sid <= 0 or tid <= 0:
            return False
        con = pg_connect()
        if con is None:
            return False
        try:
            cur = con.cursor()
            cur.execute(
                """SELECT id, confidence_score
                   FROM relationships
                   WHERE source_company_id=%s AND target_company_id=%s AND relationship_type=%s
                   ORDER BY id DESC LIMIT 1""",
                (sid, tid, rel),
            )
            row = cur.fetchone()
            if row:
                rid = int((row[0] if isinstance(row, (tuple, list)) else row["id"]) or 0)
                prev_conf = int((row[1] if isinstance(row, (tuple, list)) else row["confidence_score"]) or 0)
                if conf >= prev_conf:
                    cur.execute(
                        """UPDATE relationships
                           SET evidence_text=%s, source_url=%s, confidence_score=%s
                           WHERE id=%s""",
                        (ev or f"SEC extracted link: {src_tk}->{tgt_tk}", src_url, conf, rid),
                    )
                con.commit()
                return True
            cur.execute(
                """INSERT INTO relationships
                   (source_company_id, target_company_id, relationship_type, evidence_text, source_url, confidence_score)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (sid, tid, rel, ev or f"SEC extracted link: {src_tk}->{tgt_tk}", src_url, conf),
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

    sid = _upsert_company_sqlite(src_tk, source_name or src_tk)
    tid = _upsert_company_sqlite(tgt_tk, target_name or tgt_tk)
    if sid <= 0 or tid <= 0:
        return False
    con = _conn_core()
    try:
        row = con.execute(
            """SELECT id, confidence_score
               FROM relationships
               WHERE source_company_id=? AND target_company_id=? AND relationship_type=?
               ORDER BY id DESC LIMIT 1""",
            (sid, tid, rel),
        ).fetchone()
        if row:
            rid = int(row["id"] or 0)
            prev_conf = int(row["confidence_score"] or 0)
            if conf >= prev_conf:
                con.execute(
                    """UPDATE relationships
                       SET evidence_text=?, source_url=?, confidence_score=?
                       WHERE id=?""",
                    (ev or f"SEC extracted link: {src_tk}->{tgt_tk}", src_url, conf, rid),
                )
                con.commit()
            return True
        con.execute(
            """INSERT INTO relationships
               (source_company_id, target_company_id, relationship_type, evidence_text, source_url, confidence_score, created_at)
               VALUES (?,?,?,?,?,?,datetime('now'))""",
            (sid, tid, rel, ev or f"SEC extracted link: {src_tk}->{tgt_tk}", src_url, conf),
        )
        con.commit()
        return True
    except Exception:
        return False
    finally:
        con.close()
