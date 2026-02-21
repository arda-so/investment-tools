from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from typing import Any

from app.core.config import CORE_DB_PATH
from app.core.sqlite_hardening import connect_sqlite


def core_backend() -> str:
    return str(os.getenv("CORE_DB_BACKEND", "sqlite")).strip().lower()


def pg_enabled() -> bool:
    return core_backend() == "postgres"


def strict_postgres_mode() -> bool:
    return str(os.getenv("CORE_DB_STRICT_POSTGRES", "0")).strip().lower() in {"1", "true", "yes", "on"}


def pg_dsn() -> str:
    return str(os.getenv("POSTGRES_DSN", "")).strip()


def _pg_client():
    try:
        import psycopg  # type: ignore

        return ("psycopg", psycopg)
    except Exception:
        pass
    try:
        import psycopg2  # type: ignore

        return ("psycopg2", psycopg2)
    except Exception:
        return ("", None)


def pg_connect():
    dsn = pg_dsn()
    if not dsn:
        return None
    _name, mod = _pg_client()
    if mod is None:
        return None
    try:
        return mod.connect(dsn)
    except Exception:
        return None


def ensure_postgres_core_schema() -> dict[str, Any]:
    con = pg_connect()
    if con is None:
        return {"ok": False, "error": "pg_not_available"}
    try:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS action_proposals_core (
                id BIGINT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL,
                kind TEXT NOT NULL,
                ticker TEXT NOT NULL,
                title TEXT NOT NULL,
                thesis_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                citations_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                insights_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                reasoning_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                priority_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                execute_route TEXT NOT NULL DEFAULT '',
                execute_payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                source_event_key TEXT NOT NULL UNIQUE,
                proposal_uid TEXT NOT NULL DEFAULT '',
                target_ticker TEXT NOT NULL DEFAULT '',
                suggested_action TEXT NOT NULL DEFAULT 'REVIEW',
                thesis_summary TEXT NOT NULL DEFAULT '',
                confidence_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                dismissed_reason TEXT NOT NULL DEFAULT '',
                rejection_reason TEXT NOT NULL DEFAULT '',
                rejected_at TEXT NOT NULL DEFAULT '',
                executed_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ap_core_status ON action_proposals_core(status, priority_score DESC, id DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ap_core_ticker ON action_proposals_core(ticker)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_transactions_core (
                id BIGINT PRIMARY KEY,
                created_at TEXT NOT NULL,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL,
                shares DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                price DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                note TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'app',
                meta_json JSONB NOT NULL DEFAULT '{}'::jsonb
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pt_core_ticker ON portfolio_transactions_core(ticker)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pt_core_created ON portfolio_transactions_core(created_at DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist_thesis_core (
                ticker TEXT PRIMARY KEY,
                thesis TEXT NOT NULL DEFAULT '',
                thesis_summary TEXT NOT NULL DEFAULT '',
                pick_method TEXT NOT NULL DEFAULT '',
                triggers TEXT NOT NULL DEFAULT '',
                invalidation TEXT NOT NULL DEFAULT '',
                conviction_rating INTEGER NOT NULL DEFAULT 0,
                time_horizon TEXT NOT NULL DEFAULT '',
                invalidation_criteria TEXT NOT NULL DEFAULT '',
                strategy_tag TEXT NOT NULL DEFAULT 'CORE',
                pattern_learnable INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS investor_style_memory_core (
                key TEXT PRIMARY KEY,
                answer TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS filings_core (
                id BIGINT PRIMARY KEY,
                ticker TEXT NOT NULL DEFAULT '',
                form TEXT NOT NULL DEFAULT '',
                date TEXT NOT NULL DEFAULT '',
                accession TEXT NOT NULL DEFAULT '',
                doc_url TEXT NOT NULL DEFAULT '',
                path TEXT NOT NULL DEFAULT '',
                downloaded_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_filings_core_ticker_date ON filings_core(ticker, date DESC)")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS report_facts_core (
                id BIGINT PRIMARY KEY,
                report_name TEXT NOT NULL DEFAULT '',
                report_kind TEXT NOT NULL DEFAULT '',
                report_modified TEXT NOT NULL DEFAULT '',
                fact_date TEXT NOT NULL DEFAULT '',
                ticker TEXT NOT NULL DEFAULT '',
                fact_text TEXT NOT NULL DEFAULT '',
                importance INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT '',
                fact_hash TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_rf_core_ticker_date ON report_facts_core(ticker, fact_date DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_rf_core_importance ON report_facts_core(importance DESC)")
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


def sync_core_from_sqlite() -> dict[str, Any]:
    st = ensure_postgres_core_schema()
    if not st.get("ok"):
        return st
    con_pg = pg_connect()
    con_sq = connect_sqlite(str(CORE_DB_PATH), row_factory=True)
    if con_pg is None:
        con_sq.close()
        return {"ok": False, "error": "pg_not_available"}
    counts = {
        "action_proposals_core": 0,
        "portfolio_transactions_core": 0,
        "watchlist_thesis_core": 0,
        "investor_style_memory_core": 0,
        "filings_core": 0,
        "report_facts_core": 0,
    }
    try:
        cp = con_pg.cursor()
        for r in con_sq.execute("SELECT * FROM action_proposals").fetchall():
            cp.execute(
                """
                INSERT INTO action_proposals_core
                (id, created_at, updated_at, status, kind, ticker, title, thesis_json, citations_json, insights_json, reasoning_json,
                 confidence, priority_score, execute_route, execute_payload_json, source_event_key, proposal_uid, target_ticker,
                 suggested_action, thesis_summary, confidence_score, dismissed_reason, rejection_reason, rejected_at, executed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id) DO UPDATE SET
                  updated_at=EXCLUDED.updated_at,status=EXCLUDED.status,kind=EXCLUDED.kind,ticker=EXCLUDED.ticker,title=EXCLUDED.title,
                  thesis_json=EXCLUDED.thesis_json,citations_json=EXCLUDED.citations_json,insights_json=EXCLUDED.insights_json,reasoning_json=EXCLUDED.reasoning_json,
                  confidence=EXCLUDED.confidence,priority_score=EXCLUDED.priority_score,execute_route=EXCLUDED.execute_route,execute_payload_json=EXCLUDED.execute_payload_json,
                  source_event_key=EXCLUDED.source_event_key,proposal_uid=EXCLUDED.proposal_uid,target_ticker=EXCLUDED.target_ticker,
                  suggested_action=EXCLUDED.suggested_action,thesis_summary=EXCLUDED.thesis_summary,confidence_score=EXCLUDED.confidence_score,
                  dismissed_reason=EXCLUDED.dismissed_reason,rejection_reason=EXCLUDED.rejection_reason,rejected_at=EXCLUDED.rejected_at,executed_at=EXCLUDED.executed_at
                """,
                (
                    int(r["id"] or 0),
                    str(r["created_at"] or ""),
                    str(r["updated_at"] or ""),
                    str(r["status"] or ""),
                    str(r["kind"] or ""),
                    str(r["ticker"] or ""),
                    str(r["title"] or ""),
                    json.dumps(json.loads(str(r["thesis_json"] or "[]")), ensure_ascii=True),
                    json.dumps(json.loads(str(r["citations_json"] or "[]")), ensure_ascii=True),
                    json.dumps(json.loads(str(r["insights_json"] or "[]")), ensure_ascii=True),
                    json.dumps(json.loads(str(r["reasoning_json"] or "{}")), ensure_ascii=True),
                    float(r["confidence"] or 0.0),
                    float(r["priority_score"] or 0.0),
                    str(r["execute_route"] or ""),
                    json.dumps(json.loads(str(r["execute_payload_json"] or "{}")), ensure_ascii=True),
                    str(r["source_event_key"] or ""),
                    str(r["proposal_uid"] or ""),
                    str(r["target_ticker"] or ""),
                    str(r["suggested_action"] or "REVIEW"),
                    str(r["thesis_summary"] or ""),
                    float(r["confidence_score"] or 0.0),
                    str(r["dismissed_reason"] or ""),
                    str(r["rejection_reason"] or ""),
                    str(r["rejected_at"] or ""),
                    str(r["executed_at"] or ""),
                ),
            )
            counts["action_proposals_core"] += 1

        for r in con_sq.execute("SELECT * FROM portfolio_transactions").fetchall():
            cp.execute(
                """
                INSERT INTO portfolio_transactions_core (id, created_at, ticker, action, shares, price, note, source, meta_json)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT(id) DO UPDATE SET
                  created_at=EXCLUDED.created_at,ticker=EXCLUDED.ticker,action=EXCLUDED.action,shares=EXCLUDED.shares,price=EXCLUDED.price,
                  note=EXCLUDED.note,source=EXCLUDED.source,meta_json=EXCLUDED.meta_json
                """,
                (
                    int(r["id"] or 0),
                    str(r["created_at"] or ""),
                    str(r["ticker"] or ""),
                    str(r["action"] or ""),
                    float(r["shares"] or 0.0),
                    float(r["price"] or 0.0),
                    str(r["note"] or ""),
                    str(r["source"] or ""),
                    json.dumps(json.loads(str(r["meta_json"] or "{}")), ensure_ascii=True),
                ),
            )
            counts["portfolio_transactions_core"] += 1

        for r in con_sq.execute("SELECT * FROM watchlist_thesis").fetchall():
            cp.execute(
                """
                INSERT INTO watchlist_thesis_core
                (ticker, thesis, thesis_summary, pick_method, triggers, invalidation, conviction_rating, time_horizon, invalidation_criteria, strategy_tag, pattern_learnable, status, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(ticker) DO UPDATE SET
                  thesis=EXCLUDED.thesis,thesis_summary=EXCLUDED.thesis_summary,pick_method=EXCLUDED.pick_method,triggers=EXCLUDED.triggers,invalidation=EXCLUDED.invalidation,
                  conviction_rating=EXCLUDED.conviction_rating,time_horizon=EXCLUDED.time_horizon,invalidation_criteria=EXCLUDED.invalidation_criteria,
                  strategy_tag=EXCLUDED.strategy_tag,pattern_learnable=EXCLUDED.pattern_learnable,status=EXCLUDED.status,updated_at=EXCLUDED.updated_at
                """,
                (
                    str(r["ticker"] or ""),
                    str(r["thesis"] or ""),
                    str(r["thesis_summary"] or ""),
                    str(r["pick_method"] or ""),
                    str(r["triggers"] or ""),
                    str(r["invalidation"] or ""),
                    int(r["conviction_rating"] or 0),
                    str(r["time_horizon"] or ""),
                    str(r["invalidation_criteria"] or ""),
                    str(r["strategy_tag"] or "CORE"),
                    int(r["pattern_learnable"] or 1),
                    str(r["status"] or "active"),
                    str(r["created_at"] or ""),
                    str(r["updated_at"] or ""),
                ),
            )
            counts["watchlist_thesis_core"] += 1

        for r in con_sq.execute("SELECT key, answer, created_at, updated_at FROM investor_style_memory").fetchall():
            cp.execute(
                """
                INSERT INTO investor_style_memory_core(key, answer, created_at, updated_at)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT(key) DO UPDATE SET answer=EXCLUDED.answer, updated_at=EXCLUDED.updated_at
                """,
                (str(r["key"] or ""), str(r["answer"] or ""), str(r["created_at"] or ""), str(r["updated_at"] or "")),
            )
            counts["investor_style_memory_core"] += 1

        for r in con_sq.execute("SELECT id, ticker, form, date, accession, doc_url, path, downloaded_at FROM filings").fetchall():
            cp.execute(
                """
                INSERT INTO filings_core(id, ticker, form, date, accession, doc_url, path, downloaded_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id) DO UPDATE SET
                  ticker=EXCLUDED.ticker, form=EXCLUDED.form, date=EXCLUDED.date, accession=EXCLUDED.accession,
                  doc_url=EXCLUDED.doc_url, path=EXCLUDED.path, downloaded_at=EXCLUDED.downloaded_at
                """,
                (
                    int(r["id"] or 0),
                    str(r["ticker"] or ""),
                    str(r["form"] or ""),
                    str(r["date"] or ""),
                    str(r["accession"] or ""),
                    str(r["doc_url"] or ""),
                    str(r["path"] or ""),
                    str(r["downloaded_at"] or ""),
                ),
            )
            counts["filings_core"] += 1

        for r in con_sq.execute(
            "SELECT id, report_name, report_kind, report_modified, fact_date, ticker, fact_text, importance, source, fact_hash, created_at FROM report_facts"
        ).fetchall():
            cp.execute(
                """
                INSERT INTO report_facts_core
                (id, report_name, report_kind, report_modified, fact_date, ticker, fact_text, importance, source, fact_hash, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(id) DO UPDATE SET
                  report_name=EXCLUDED.report_name, report_kind=EXCLUDED.report_kind, report_modified=EXCLUDED.report_modified,
                  fact_date=EXCLUDED.fact_date, ticker=EXCLUDED.ticker, fact_text=EXCLUDED.fact_text, importance=EXCLUDED.importance,
                  source=EXCLUDED.source, fact_hash=EXCLUDED.fact_hash, created_at=EXCLUDED.created_at
                """,
                (
                    int(r["id"] or 0),
                    str(r["report_name"] or ""),
                    str(r["report_kind"] or ""),
                    str(r["report_modified"] or ""),
                    str(r["fact_date"] or ""),
                    str(r["ticker"] or ""),
                    str(r["fact_text"] or ""),
                    int(r["importance"] or 0),
                    str(r["source"] or ""),
                    str(r["fact_hash"] or ""),
                    str(r["created_at"] or ""),
                ),
            )
            counts["report_facts_core"] += 1

        con_pg.commit()
        return {"ok": True, "synced": counts}
    except Exception as exc:
        try:
            con_pg.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc), "synced": counts}
    finally:
        con_sq.close()
        con_pg.close()


def verify_core_counts() -> dict[str, Any]:
    con_pg = pg_connect()
    con_sq = connect_sqlite(str(CORE_DB_PATH), row_factory=True)
    if con_pg is None:
        con_sq.close()
        return {"ok": False, "error": "pg_not_available"}
    try:
        cp = con_pg.cursor()
        out: dict[str, Any] = {"ok": True, "counts": {}}
        pairs = [
            ("action_proposals", "action_proposals_core"),
            ("portfolio_transactions", "portfolio_transactions_core"),
            ("watchlist_thesis", "watchlist_thesis_core"),
            ("investor_style_memory", "investor_style_memory_core"),
            ("filings", "filings_core"),
            ("report_facts", "report_facts_core"),
        ]
        for sq, pg in pairs:
            sq_n = int((con_sq.execute(f"SELECT COUNT(*) AS c FROM {sq}").fetchone() or {"c": 0})["c"] or 0)
            cp.execute(f"SELECT COUNT(*) FROM {pg}")
            pg_n = int((cp.fetchone() or [0])[0] or 0)
            out["counts"][sq] = {"sqlite": sq_n, "postgres": pg_n, "match": bool(sq_n == pg_n)}
        return out
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        con_sq.close()
        con_pg.close()


def guard_core_backend_cutover() -> dict[str, Any]:
    backend = core_backend()
    enforce = str(os.getenv("CORE_DB_GUARD_ENFORCE", "1")).strip().lower() in {"1", "true", "yes", "on"}
    if backend != "postgres":
        return {"ok": True, "backend": backend, "guard_enforced": enforce, "switched": False, "reason": "backend_not_postgres"}
    if not enforce:
        return {"ok": True, "backend": backend, "guard_enforced": False, "switched": False, "reason": "guard_disabled"}
    v = verify_core_counts()
    if not bool(v.get("ok")):
        os.environ["CORE_DB_BACKEND"] = "sqlite"
        return {"ok": False, "backend": "sqlite", "guard_enforced": True, "switched": True, "reason": "verify_failed", "verify": v}
    counts = dict(v.get("counts") or {})
    all_match = True
    for _, meta in counts.items():
        if not bool((meta or {}).get("match")):
            all_match = False
            break
    if all_match:
        return {"ok": True, "backend": "postgres", "guard_enforced": True, "switched": False, "reason": "verified_match", "verify": v}
    os.environ["CORE_DB_BACKEND"] = "sqlite"
    return {"ok": False, "backend": "sqlite", "guard_enforced": True, "switched": True, "reason": "count_mismatch", "verify": v}


def enforce_strict_postgres_ready() -> dict[str, Any]:
    if not strict_postgres_mode():
        return {"ok": True, "strict": False, "enforced": False, "reason": "strict_disabled"}
    if core_backend() != "postgres":
        raise RuntimeError("strict_postgres_requires_core_db_backend_postgres")
    v = verify_core_counts()
    if not bool(v.get("ok")):
        raise RuntimeError("strict_postgres_verify_failed")
    counts = dict(v.get("counts") or {})
    bad = [k for k, meta in counts.items() if not bool((meta or {}).get("match"))]
    if bad:
        raise RuntimeError("strict_postgres_count_mismatch:" + ",".join(bad))
    return {"ok": True, "strict": True, "enforced": True, "reason": "verified_match", "verify": v}


def list_action_proposals_pg(status: str = "open", limit: int = 96) -> list[dict[str, Any]]:
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT id, created_at, updated_at, status, kind, ticker, title, thesis_json, citations_json, insights_json, reasoning_json,
                   confidence, priority_score, execute_route, source_event_key
            FROM action_proposals_core
            WHERE status=%s
            ORDER BY priority_score DESC, id DESC
            LIMIT %s
            """,
            (str(status or "open"), max(1, min(300, int(limit or 96)))),
        )
        rows = cur.fetchall() or []
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "id": int(r[0] or 0),
                    "created_at": str(r[1] or ""),
                    "updated_at": str(r[2] or ""),
                    "status": str(r[3] or ""),
                    "kind": str(r[4] or ""),
                    "ticker": str(r[5] or ""),
                    "title": str(r[6] or ""),
                    "thesis_json": list(r[7] or []),
                    "citations_json": list(r[8] or []),
                    "insights_json": list(r[9] or []),
                    "reasoning_json": dict(r[10] or {}),
                    "confidence": float(r[11] or 0.0),
                    "priority_score": float(r[12] or 0.0),
                    "execute_route": str(r[13] or ""),
                    "source_event_key": str(r[14] or ""),
                }
            )
        return out
    except Exception:
        return []
    finally:
        con.close()


def list_portfolio_transactions_pg(limit: int = 100, ticker: str = "", year: int = 0) -> list[dict[str, Any]]:
    con = pg_connect()
    if con is None:
        return []
    try:
        clauses: list[str] = []
        vals: list[Any] = []
        if str(ticker or "").strip():
            clauses.append("ticker = %s")
            vals.append(str(ticker or "").strip().upper())
        if int(year or 0) >= 1900:
            # Support mixed broker timestamp formats by extracting a 4-digit year
            # from anywhere in created_at instead of relying on lexicographic ranges.
            y = int(year)
            clauses.append("COALESCE(NULLIF(substring(created_at from '([12][0-9]{3})'), '')::int, 0) = %s")
            vals.append(y)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        q = (
            "SELECT created_at, ticker, action, shares, price, note, source "
            "FROM portfolio_transactions_core "
            + where
            + " ORDER BY created_at DESC, id DESC LIMIT %s"
        )
        vals.append(max(1, min(2000, int(limit or 100))))
        cur = con.cursor()
        cur.execute(q, tuple(vals))
        out: list[dict[str, Any]] = []
        for r in cur.fetchall() or []:
            out.append(
                {
                    "created_at": str(r[0] or ""),
                    "ticker": str(r[1] or ""),
                    "action": str(r[2] or ""),
                    "shares": float(r[3] or 0.0),
                    "price": float(r[4] or 0.0),
                    "note": str(r[5] or ""),
                    "source": str(r[6] or ""),
                }
            )
        return out
    except Exception:
        return []
    finally:
        con.close()


def list_recent_portfolio_transactions_pg(limit: int = 20, ticker: str = "") -> list[dict[str, Any]]:
    return list_portfolio_transactions_pg(limit=limit, ticker=ticker, year=0)


def summarize_investor_style_memory_pg(limit: int = 24) -> str:
    con = pg_connect()
    if con is None:
        return ""
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT key, answer FROM investor_style_memory_core WHERE COALESCE(answer,'') <> '' ORDER BY updated_at DESC LIMIT %s",
            (max(1, min(200, int(limit or 24))),),
        )
        out: list[str] = []
        for r in cur.fetchall() or []:
            k = str(r[0] or "").strip()
            a = str(r[1] or "").strip()
            if k and a:
                out.append(f"- {k}: {a[:180]}")
        return "\n".join(out)
    except Exception:
        return ""
    finally:
        con.close()


def upsert_watchlist_thesis_pg(
    *,
    ticker: str,
    thesis: str = "",
    thesis_summary: str = "",
    pick_method: str = "",
    triggers: str = "",
    invalidation: str = "",
    conviction_rating: int = 0,
    time_horizon: str = "",
    invalidation_criteria: str = "",
    strategy_tag: str = "CORE",
    pattern_learnable: int = 1,
    status: str = "active",
) -> bool:
    con = pg_connect()
    if con is None:
        return False
    now = dt.datetime.now().isoformat()
    t = str(ticker or "").strip().upper()
    if not t:
        con.close()
        return False
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO watchlist_thesis_core
            (ticker, thesis, thesis_summary, pick_method, triggers, invalidation, conviction_rating, time_horizon, invalidation_criteria, strategy_tag, pattern_learnable, status, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(ticker) DO UPDATE SET
              thesis=COALESCE(NULLIF(EXCLUDED.thesis,''),watchlist_thesis_core.thesis),
              thesis_summary=COALESCE(NULLIF(EXCLUDED.thesis_summary,''),watchlist_thesis_core.thesis_summary),
              pick_method=COALESCE(NULLIF(EXCLUDED.pick_method,''),watchlist_thesis_core.pick_method),
              triggers=COALESCE(NULLIF(EXCLUDED.triggers,''),watchlist_thesis_core.triggers),
              invalidation=COALESCE(NULLIF(EXCLUDED.invalidation,''),watchlist_thesis_core.invalidation),
              conviction_rating=CASE WHEN EXCLUDED.conviction_rating > 0 THEN EXCLUDED.conviction_rating ELSE watchlist_thesis_core.conviction_rating END,
              time_horizon=COALESCE(NULLIF(EXCLUDED.time_horizon,''),watchlist_thesis_core.time_horizon),
              invalidation_criteria=COALESCE(NULLIF(EXCLUDED.invalidation_criteria,''),watchlist_thesis_core.invalidation_criteria),
              strategy_tag=COALESCE(NULLIF(EXCLUDED.strategy_tag,''),watchlist_thesis_core.strategy_tag),
              pattern_learnable=CASE WHEN EXCLUDED.pattern_learnable IN (0,1) THEN EXCLUDED.pattern_learnable ELSE watchlist_thesis_core.pattern_learnable END,
              status=COALESCE(NULLIF(EXCLUDED.status,''),watchlist_thesis_core.status),
              updated_at=EXCLUDED.updated_at
            """,
            (
                t,
                str(thesis or "")[:3000],
                str((thesis_summary or thesis) or "")[:3000],
                str(pick_method or "")[:500],
                str(triggers or "")[:1000],
                str(invalidation or "")[:1000],
                int(conviction_rating or 0),
                str(time_horizon or "")[:120],
                str((invalidation_criteria or invalidation) or "")[:1000],
                str(strategy_tag or "CORE")[:16].upper(),
                1 if int(pattern_learnable) != 0 else 0,
                str(status or "active")[:32],
                now,
                now,
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


def upsert_investor_style_memory_pg(key: str, answer: str) -> bool:
    con = pg_connect()
    if con is None:
        return False
    k = str(key or "").strip()
    if not k:
        con.close()
        return False
    now = dt.datetime.now().isoformat()
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO investor_style_memory_core(key, answer, created_at, updated_at)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT(key) DO UPDATE SET answer=EXCLUDED.answer, updated_at=EXCLUDED.updated_at
            """,
            (k[:300], str(answer or "")[:2000], now, now),
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


def filing_stats_map_pg(tickers: list[str]) -> dict[str, dict[str, str | int]]:
    tks = [str(t or "").strip().upper() for t in (tickers or []) if str(t or "").strip()]
    if not tks:
        return {}
    con = pg_connect()
    if con is None:
        return {}
    out: dict[str, dict[str, str | int]] = {t: {"filings": 0, "last_filing_date": ""} for t in tks}
    try:
        marks = ",".join("%s" for _ in tks)
        cur = con.cursor()
        cur.execute(
            f"""
            SELECT ticker, COUNT(*) AS c, MAX(date) AS last_date
            FROM filings_core
            WHERE ticker IN ({marks})
            GROUP BY ticker
            """,
            tuple(tks),
        )
        for r in cur.fetchall() or []:
            t = str(r[0] or "").strip().upper()
            if not t:
                continue
            out[t] = {"filings": int(r[1] or 0), "last_filing_date": str(r[2] or "")}
        return out
    except Exception:
        return out
    finally:
        con.close()


def company_news_from_report_facts_pg(tickers: list[str], limit: int = 8) -> list[dict[str, str]]:
    tks = [str(x or "").strip().upper() for x in (tickers or []) if str(x or "").strip()]
    if not tks:
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        marks = ",".join("%s" for _ in tks)
        cur = con.cursor()
        cur.execute(
            f"""SELECT ticker, fact_text, fact_date
                FROM report_facts_core
                WHERE ticker IN ({marks})
                ORDER BY importance DESC, fact_date DESC, id DESC
                LIMIT %s""",
            tuple(tks + [max(1, min(40, int(limit) * 3))]),
        )
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for r in cur.fetchall() or []:
            tk = str(r[0] or "").strip().upper()
            txt = str(r[1] or "").strip()
            if not tk or not txt:
                continue
            title = f"{tk}: {txt[:160]}"
            key = " ".join(title.lower().split())
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "title": title,
                    "ticker": tk,
                    "link": f"/company_file/sec?t={tk}",
                    "source": "Official Reports",
                    "published_at": str(r[2] or ""),
                }
            )
            if len(out) >= max(1, min(30, int(limit))):
                break
        return out
    except Exception:
        return []
    finally:
        con.close()


def query_report_facts_pg(
    query: str = "",
    tickers: list[str] | None = None,
    limit: int = 12,
    official_only: bool = True,
) -> list[dict[str, Any]]:
    con = pg_connect()
    if con is None:
        return []
    lim = max(1, min(100, int(limit or 12)))
    tks = [str(t or "").strip().upper() for t in (tickers or []) if str(t or "").strip()]
    try:
        clauses = ["1=1"]
        vals: list[Any] = []
        if tks:
            clauses.append("ticker = ANY(%s)")
            vals.append(tks)
        q = str(query or "").strip()
        if q:
            like = "%" + q[:120] + "%"
            clauses.append("(fact_text ILIKE %s OR report_name ILIKE %s OR report_kind ILIKE %s)")
            vals.extend([like, like, like])
        if bool(official_only):
            allowed_kinds = sorted(
                {
                    "Daily Brief",
                    "Appendix",
                    "Morning Intelligence",
                    "Signal Tracker",
                    "Earnings Radar",
                    "Market Scanner",
                    "L2 Digest",
                    "Weekly",
                    "Monthly",
                    "Quarterly",
                    "Deep Dive",
                    "Red Flag Alert",
                }
            )
            clauses.append(
                "(report_kind = ANY(%s) OR report_name ILIKE %s OR report_name ILIKE %s OR report_name ILIKE %s OR report_name ILIKE %s OR report_name ILIKE %s OR report_name ILIKE %s)"
            )
            vals.append(allowed_kinds)
            vals.extend(["%10-k%", "%10-q%", "%8-k%", "%20-f%", "%40-f%", "%sec%"])
        sql = (
            "SELECT report_name, report_kind, fact_date, ticker, fact_text, importance, created_at "
            "FROM report_facts_core WHERE "
            + " AND ".join(clauses)
            + " ORDER BY importance DESC, fact_date DESC, id DESC LIMIT %s"
        )
        vals.append(lim)
        cur = con.cursor()
        cur.execute(sql, tuple(vals))
        rows = cur.fetchall() or []
        return [
            {
                "report_name": str(r[0] or ""),
                "report_kind": str(r[1] or ""),
                "fact_date": str(r[2] or ""),
                "ticker": str(r[3] or "").strip().upper(),
                "fact_text": str(r[4] or ""),
                "importance": int(r[5] or 0),
                "created_at": str(r[6] or ""),
            }
            for r in rows
        ]
    except Exception:
        return []
    finally:
        con.close()
