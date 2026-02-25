from __future__ import annotations

import datetime as dt
import re
import sqlite3
from typing import Any

from app.core.ticker import safe_ticker


def upsert_entity_sqlite(con: sqlite3.Connection, name: str, entity_type: str) -> int:
    nm = str(name or "").strip()
    et = str(entity_type or "").strip().upper()
    if not nm or not et:
        return 0
    norm = re.sub(r"\s+", " ", nm.lower())
    id_text = f"{et.lower()}:{norm}"
    now = dt.datetime.now().isoformat()
    try:
        con.execute(
            "UPDATE entities SET id_text = lower(type) || ':' || normalized_name "
            "WHERE COALESCE(id_text,'') = ''"
        )
    except Exception:
        pass
    con.execute(
        "INSERT INTO entities(name, type, entity_type, metadata, normalized_name, id_text, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(type, normalized_name) DO UPDATE SET name=excluded.name, entity_type=excluded.entity_type, id_text=excluded.id_text, updated_at=excluded.updated_at",
        (nm, et, et, "{}", norm, id_text, now, now),
    )
    row = con.execute("SELECT id FROM entities WHERE type=? AND normalized_name=? LIMIT 1", (et, norm)).fetchone()
    if not row:
        return 0
    if isinstance(row, dict):
        return int(row.get("id") or 0)
    try:
        return int(row["id"] or 0)
    except Exception:
        return int((row[0] if len(row) > 0 else 0) or 0)


def upsert_entity_pg(con_pg: Any, name: str, entity_type: str) -> int:
    nm = str(name or "").strip()
    et = str(entity_type or "").strip().upper()
    if not nm or not et:
        return 0
    norm = re.sub(r"\s+", " ", nm.lower())
    id_text = f"{et.lower()}:{norm}"
    now = dt.datetime.now().isoformat()
    cur = con_pg.cursor()
    cur.execute(
        """
        INSERT INTO entities_core(name, type, entity_type, metadata_json, normalized_name, id_text, created_at, updated_at, id)
        VALUES(%s,%s,%s,%s::jsonb,%s,%s,%s,%s,(SELECT COALESCE(MAX(id),0)+1 FROM entities_core))
        ON CONFLICT(type, normalized_name) DO UPDATE SET
          name=EXCLUDED.name,
          entity_type=EXCLUDED.entity_type,
          id_text=EXCLUDED.id_text,
          updated_at=EXCLUDED.updated_at
        """,
        (nm, et, et, "{}", norm, id_text, now, now),
    )
    cur.execute("SELECT id FROM entities_core WHERE type=%s AND normalized_name=%s LIMIT 1", (et, norm))
    row = cur.fetchone()
    return int((row or [0])[0] or 0)


def extract_peer_tickers(text: str, self_ticker: str, max_peers: int = 8) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    own = safe_ticker(self_ticker)
    for m in re.findall(r"\b[A-Z]{2,5}\b", str(text or "").upper()):
        tk = safe_ticker(m)
        if not tk or tk == own or tk in seen:
            continue
        seen.add(tk)
        out.append(tk)
        if len(out) >= max(1, int(max_peers or 8)):
            break
    return out

