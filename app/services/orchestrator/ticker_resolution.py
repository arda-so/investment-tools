from __future__ import annotations

import re

from app.core.normalize import normalize_text as _norm
from app.services.postgres_core_service import pg_connect


def resolve_ticker_from_company_name(norm_text: str) -> str:
    try:
        con = pg_connect()
        if con is None:
            return ""
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT ticker, name FROM companies_core WHERE name IS NOT NULL AND name <> '' LIMIT 1500"
            )
            rows = cur.fetchall() or []
        finally:
            con.close()
    except Exception:
        return ""
    ntext = str(norm_text or "")
    for r in rows:
        name = _norm(str(r[1] or ""))
        ticker = str(r[0] or "").strip().upper()
        if not name or not ticker:
            continue
        if len(name) < 4:
            continue
        if name in ntext:
            return ticker
    return ""


def resolve_ticker_from_company_hint(text: str) -> str:
    q = str(text or "").strip()
    if not q:
        return ""
    m = re.search(r"\b(?:from|for|about)\s+([A-Za-z][A-Za-z0-9&\.\-\s]{1,40})$", q, flags=re.I)
    phrase = str(m.group(1) if m else "").strip(" .,:;")
    if not phrase:
        return ""
    cand = phrase.split()
    phrase_short = " ".join(cand[:3]).strip()
    if not phrase_short:
        return ""
    rows = []
    try:
        con = pg_connect()
        if con is None:
            return ""
        try:
            cur = con.cursor()
            up = phrase_short.upper()
            cur.execute(
                "SELECT ticker FROM companies_core WHERE UPPER(ticker)=%s LIMIT 1",
                (up,),
            )
            row = cur.fetchone()
            if row and str(row[0] or "").strip():
                return str(row[0]).strip().upper()
            like = "%" + _norm(phrase_short).replace(" ", "%") + "%"
            cur.execute(
                "SELECT ticker, name FROM companies_core WHERE LOWER(name) LIKE %s LIMIT 20",
                (like,),
            )
            rows = cur.fetchall() or []
        finally:
            con.close()
    except Exception:
        return ""
    if not rows:
        return ""
    best = sorted(
        (
            (str(r[0] or "").strip().upper(), len(str(r[1] or "")))
            for r in rows
            if str(r[0] or "").strip()
        ),
        key=lambda x: x[1],
    )
    return best[0][0] if best else ""
