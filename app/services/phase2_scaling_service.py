from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from typing import Any

from app.services.ai_job_queue_service import enqueue_job, get_job
from app.services.chat_memory_service import append_chat_message, ensure_chat_memory_schema

_PG_CLIENT = None
_PG_CONNECT_ERR = ""


def _pg_enabled() -> bool:
    return str(os.getenv("PHASE2_POSTGRES_ENABLED", "0")).strip().lower() in {"1", "true", "yes", "on"}


def _pg_dsn() -> str:
    return str(os.getenv("POSTGRES_DSN", "")).strip()


def _pg_client():
    global _PG_CLIENT, _PG_CONNECT_ERR
    if _PG_CLIENT is not None:
        return _PG_CLIENT
    dsn = _pg_dsn()
    if not dsn:
        _PG_CONNECT_ERR = "missing_POSTGRES_DSN"
        return None
    try:
        import psycopg  # type: ignore

        _PG_CLIENT = psycopg
        return _PG_CLIENT
    except Exception:
        pass
    try:
        import psycopg2  # type: ignore

        _PG_CLIENT = psycopg2
        return _PG_CLIENT
    except Exception as exc:
        _PG_CONNECT_ERR = f"pg_driver_unavailable:{exc}"
        return None


def _pg_connect():
    pg = _pg_client()
    if pg is None:
        return None
    dsn = _pg_dsn()
    try:
        return pg.connect(dsn)
    except Exception as exc:
        global _PG_CONNECT_ERR
        _PG_CONNECT_ERR = f"pg_connect_failed:{exc}"
        return None


def _json_s(v: Any) -> str:
    return json.dumps(v if v is not None else {}, ensure_ascii=True)


def phase2_status() -> dict[str, Any]:
    if not _pg_enabled():
        return {
            "ok": True,
            "postgres_enabled": False,
            "postgres_ready": False,
            "pgvector_ready": False,
            "error": "",
            "asof": dt.datetime.now().isoformat(),
        }
    con = _pg_connect()
    if con is None:
        return {
            "ok": False,
            "postgres_enabled": True,
            "postgres_ready": False,
            "pgvector_ready": False,
            "error": _PG_CONNECT_ERR,
            "asof": dt.datetime.now().isoformat(),
        }
    pgvector_ok = False
    try:
        cur = con.cursor()
        cur.execute("SELECT 1")
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            pgvector_ok = True
        except Exception:
            pgvector_ok = False
        con.commit()
        return {
            "ok": True,
            "postgres_enabled": True,
            "postgres_ready": True,
            "pgvector_ready": pgvector_ok,
            "error": "",
            "asof": dt.datetime.now().isoformat(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "postgres_enabled": True,
            "postgres_ready": False,
            "pgvector_ready": False,
            "error": str(exc),
            "asof": dt.datetime.now().isoformat(),
        }
    finally:
        con.close()


def ensure_phase2_postgres_schema() -> dict[str, Any]:
    if not _pg_enabled():
        return {"ok": True, "enabled": False, "created": False}
    con = _pg_connect()
    if con is None:
        return {"ok": False, "enabled": True, "created": False, "error": _PG_CONNECT_ERR}
    try:
        cur = con.cursor()
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS action_proposals_mirror (
                source_event_key TEXT PRIMARY KEY,
                proposal_id BIGINT NOT NULL DEFAULT 0,
                ticker TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                title TEXT NOT NULL DEFAULT '',
                confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                priority_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                insights_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                reasoning_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sec_signal_chunks (
                id BIGSERIAL PRIMARY KEY,
                source_key TEXT NOT NULL,
                ticker TEXT NOT NULL DEFAULT '',
                chunk_text TEXT NOT NULL,
                embedding vector(1536),
                metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS phase2_sector_reports (
                reducer_job_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL DEFAULT '',
                tickers_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                question TEXT NOT NULL DEFAULT '',
                result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                summary_text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_apm_ticker_status ON action_proposals_mirror(ticker, status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source_key ON sec_signal_chunks(source_key)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_phase2_reports_updated ON phase2_sector_reports(updated_at DESC)")
        con.commit()
        return {"ok": True, "enabled": True, "created": True}
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return {"ok": False, "enabled": True, "created": False, "error": str(exc)}
    finally:
        con.close()


def mirror_action_proposal(row: dict[str, Any]) -> None:
    if not _pg_enabled():
        return
    con = _pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO action_proposals_mirror
            (source_event_key, proposal_id, ticker, kind, status, title, confidence, priority_score, insights_json, reasoning_json, updated_at, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)
            ON CONFLICT(source_event_key) DO UPDATE SET
              proposal_id=EXCLUDED.proposal_id,
              ticker=EXCLUDED.ticker,
              kind=EXCLUDED.kind,
              status=EXCLUDED.status,
              title=EXCLUDED.title,
              confidence=EXCLUDED.confidence,
              priority_score=EXCLUDED.priority_score,
              insights_json=EXCLUDED.insights_json,
              reasoning_json=EXCLUDED.reasoning_json,
              updated_at=EXCLUDED.updated_at
            """,
            (
                str(row.get("source_event_key") or ""),
                int(row.get("proposal_id") or 0),
                str(row.get("ticker") or ""),
                str(row.get("kind") or ""),
                str(row.get("status") or "open"),
                str(row.get("title") or ""),
                float(row.get("confidence") or 0.0),
                float(row.get("priority_score") or 0.0),
                _json_s(row.get("insights_json") or []),
                _json_s(row.get("reasoning_json") or {}),
                str(row.get("updated_at") or dt.datetime.now().isoformat()),
                str(row.get("created_at") or dt.datetime.now().isoformat()),
            ),
        )
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


def _stable_embedding_stub(text: str, dim: int = 1536) -> list[float]:
    # Fallback deterministic embedding to keep pipeline non-blocking when no embed provider is configured.
    raw = str(text or "").encode("utf-8", errors="ignore")
    if not raw:
        return [0.0] * dim
    out = [0.0] * dim
    for i in range(dim):
        h = hashlib.sha256(raw + b"::" + str(i).encode("ascii")).digest()
        v = int.from_bytes(h[:4], "big", signed=False) / 4294967295.0
        out[i] = (v * 2.0) - 1.0
    return out


def upsert_signal_chunks(source_key: str, ticker: str, chunks: list[str], metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    if not _pg_enabled():
        return {"ok": True, "enabled": False, "inserted": 0}
    con = _pg_connect()
    if con is None:
        return {"ok": False, "enabled": True, "inserted": 0, "error": _PG_CONNECT_ERR}
    inserted = 0
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM sec_signal_chunks WHERE source_key = %s", (str(source_key or ""),))
        for c in list(chunks or [])[:400]:
            text = str(c or "").strip()
            if not text:
                continue
            emb = _stable_embedding_stub(text)
            cur.execute(
                """
                INSERT INTO sec_signal_chunks (source_key, ticker, chunk_text, embedding, metadata_json, created_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    str(source_key or ""),
                    str(ticker or "").upper(),
                    text[:8000],
                    emb,
                    _json_s(metadata or {}),
                    dt.datetime.now().isoformat(),
                ),
            )
            inserted += 1
        con.commit()
        return {"ok": True, "enabled": True, "inserted": inserted}
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return {"ok": False, "enabled": True, "inserted": inserted, "error": str(exc)}
    finally:
        con.close()


def query_relevant_chunks(ticker: str, query_text: str, limit: int = 3) -> list[dict[str, Any]]:
    if not _pg_enabled():
        return []
    con = _pg_connect()
    if con is None:
        return []
    try:
        qemb = _stable_embedding_stub(query_text)
        cur = con.cursor()
        cur.execute(
            """
            SELECT source_key, ticker, chunk_text, metadata_json, created_at
            FROM sec_signal_chunks
            WHERE ticker = %s
            ORDER BY embedding <-> %s
            LIMIT %s
            """,
            (str(ticker or "").upper(), qemb, max(1, min(20, int(limit or 3)))),
        )
        rows = cur.fetchall() or []
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                md = dict(r[3] or {})
            except Exception:
                md = {}
            out.append(
                {
                    "source_key": str(r[0] or ""),
                    "ticker": str(r[1] or ""),
                    "chunk_text": str(r[2] or ""),
                    "metadata": md,
                    "created_at": str(r[4] or ""),
                }
            )
        return out
    except Exception:
        return []
    finally:
        con.close()


def enqueue_sector_map_reduce(*, tickers: list[str], question: str, session_id: str = "") -> dict[str, Any]:
    clean_tickers: list[str] = []
    for t in list(tickers or [])[:120]:
        tk = str(t or "").strip().upper()
        if tk and tk not in clean_tickers:
            clean_tickers.append(tk)
    if not clean_tickers:
        return {"ok": False, "error": "tickers_required"}
    q = str(question or "").strip()
    if not q:
        return {"ok": False, "error": "question_required"}

    child_ids: list[str] = []
    try:
        for tk in clean_tickers:
            child_payload = {
                "query": f"For {tk}: {q}",
                "context": {"session_id": session_id or "map_reduce", "force_deep_reasoning": True, "disable_manager_interrupts": True},
                "job_type": "map_task",
                "ticker": tk,
                "question": q,
            }
            child_ids.append(enqueue_job(child_payload))

        reducer_payload = {
            "query": "reduce sector analysis",
            "context": {"session_id": session_id or "map_reduce", "force_deep_reasoning": True, "disable_manager_interrupts": True},
            "job_type": "reduce_task",
            "question": q,
            "tickers": clean_tickers,
            "child_job_ids": child_ids,
        }
        reducer_job_id = enqueue_job(reducer_payload)
        return {"ok": True, "child_job_ids": child_ids, "reducer_job_id": reducer_job_id, "count": len(child_ids)}
    except Exception as exc:
        return {"ok": False, "error": f"enqueue_failed:{str(exc)[:240]}", "child_job_ids": child_ids, "count": len(child_ids)}


def save_map_reduce_report(
    *,
    reducer_job_id: str,
    session_id: str,
    tickers: list[str],
    question: str,
    result: dict[str, Any],
    status: str = "done",
) -> dict[str, Any]:
    if not _pg_enabled():
        return {"ok": True, "enabled": False}
    try:
        ensure_phase2_postgres_schema()
    except Exception:
        pass
    rid = str(reducer_job_id or "").strip()
    if not rid:
        return {"ok": False, "error": "missing_reducer_job_id"}
    con = _pg_connect()
    if con is None:
        return {"ok": False, "error": _PG_CONNECT_ERR}
    try:
        cur = con.cursor()
        msg = str((result or {}).get("message") or "").strip()
        cur.execute(
            """
            INSERT INTO phase2_sector_reports
            (reducer_job_id, session_id, tickers_json, question, result_json, summary_text, status, created_at, updated_at)
            VALUES (%s, %s, %s::jsonb, %s, %s::jsonb, %s, %s, %s, %s)
            ON CONFLICT(reducer_job_id) DO UPDATE SET
              session_id=EXCLUDED.session_id,
              tickers_json=EXCLUDED.tickers_json,
              question=EXCLUDED.question,
              result_json=EXCLUDED.result_json,
              summary_text=EXCLUDED.summary_text,
              status=EXCLUDED.status,
              updated_at=EXCLUDED.updated_at
            """,
            (
                rid,
                str(session_id or "")[:120],
                _json_s([str(x or "").strip().upper() for x in list(tickers or []) if str(x or "").strip()]),
                str(question or "")[:2000],
                _json_s(result or {}),
                msg[:8000],
                str(status or "")[:24],
                dt.datetime.now().isoformat(),
                dt.datetime.now().isoformat(),
            ),
        )
        con.commit()
        return {"ok": True, "enabled": True, "saved": True}
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}
    finally:
        con.close()


def get_map_reduce_report(reducer_job_id: str) -> dict[str, Any] | None:
    if not _pg_enabled():
        return None
    rid = str(reducer_job_id or "").strip()
    if not rid:
        return None
    con = _pg_connect()
    if con is None:
        return None
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT reducer_job_id, session_id, tickers_json, question, result_json, summary_text, status, created_at, updated_at
            FROM phase2_sector_reports
            WHERE reducer_job_id = %s
            LIMIT 1
            """,
            (rid,),
        )
        r = cur.fetchone()
        if not r:
            return None
        tickers = list(r[2] or []) if isinstance(r[2], list) else []
        res = dict(r[4] or {}) if isinstance(r[4], dict) else {}
        return {
            "reducer_job_id": str(r[0] or ""),
            "session_id": str(r[1] or ""),
            "tickers": [str(x or "").strip().upper() for x in tickers if str(x or "").strip()],
            "question": str(r[3] or ""),
            "result": res,
            "summary_text": str(r[5] or ""),
            "status": str(r[6] or ""),
            "created_at": str(r[7] or ""),
            "updated_at": str(r[8] or ""),
        }
    except Exception:
        return None
    finally:
        con.close()


def collect_map_reduce_snapshot(reducer_job_id: str) -> dict[str, Any]:
    rid = str(reducer_job_id or "").strip()
    if not rid:
        return {"ok": False, "error": "missing_reducer_job_id"}
    row = get_job(rid)
    if not isinstance(row, dict):
        return {"ok": False, "error": "reducer_not_found"}
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    child_ids = [str(x or "").strip() for x in list(payload.get("child_job_ids") or []) if str(x or "").strip()]
    stats = {"queued": 0, "running": 0, "done": 0, "error": 0, "missing": 0}
    for jid in child_ids:
        jr = get_job(jid)
        if not isinstance(jr, dict):
            stats["missing"] += 1
            continue
        st = str(jr.get("status") or "").strip().lower()
        if st in stats:
            stats[st] += 1
        else:
            stats["missing"] += 1
    snap = {
        "ok": True,
        "reducer_job_id": rid,
        "reducer_status": str(row.get("status") or ""),
        "child_total": len(child_ids),
        "child_stats": stats,
        "result": row.get("result") if isinstance(row.get("result"), dict) else {},
    }
    if str(snap["reducer_status"]).lower() == "done":
        rep = get_map_reduce_report(rid)
        if isinstance(rep, dict):
            if not isinstance(snap.get("result"), dict) or not snap["result"]:
                snap["result"] = dict(rep.get("result") or {})
            snap["report"] = rep
            # Backfill reducer result into chat memory for this session once available.
            try:
                sid = str(rep.get("session_id") or "map_reduce").strip()[:120]
                ticks = [str(x or "").strip().upper() for x in list(rep.get("tickers") or []) if str(x or "").strip()]
                msg = str(((rep.get("result") or {}) if isinstance(rep.get("result"), dict) else {}).get("message") or "").strip()
                if sid and msg:
                    ensure_chat_memory_schema()
                    title = f"Phase 2 Sector Analysis Complete ({', '.join(ticks[:8])})"
                    text = f"{title}\n\n{msg}"[:6000]
                    append_chat_message(
                        session_id=sid,
                        role="assistant",
                        text=text,
                        intent="map_reduce_reduce",
                        status=str(((rep.get("result") or {}) if isinstance(rep.get("result"), dict) else {}).get("status") or "done")[:80],
                    )
            except Exception:
                pass
    return snap
