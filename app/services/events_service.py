from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from typing import Any

from app.services.ai_job_queue_service import enqueue_job
from app.services.phase2_scaling_service import enqueue_sector_map_reduce, get_map_reduce_report
from app.services.postgres_core_service import pg_connect


def _now() -> str:
    return dt.datetime.now().isoformat()


def _json_s(v: Any) -> str:
    return json.dumps(v if v is not None else {}, ensure_ascii=True)

def _dedupe_key(source: str, ticker: str, event_type: str, occurred_at: str, payload: dict[str, Any]) -> str:
    body = json.dumps(
        {
            "source": str(source or "").strip().lower(),
            "ticker": str(ticker or "").strip().upper(),
            "event_type": str(event_type or "").strip().lower(),
            "occurred_at": str(occurred_at or "").strip(),
            "payload": payload if isinstance(payload, dict) else {},
        },
        ensure_ascii=True,
        sort_keys=True,
    )
    return hashlib.sha256(body.encode("utf-8", errors="ignore")).hexdigest()


def _event_uid() -> str:
    return "ev_" + uuid.uuid4().hex[:16]


def ensure_events_schema() -> dict[str, Any]:
    con = pg_connect()
    if con is None:
        return {"ok": False, "error": "pg_not_available"}
    try:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS events_core (
                id BIGSERIAL PRIMARY KEY,
                event_uid TEXT NOT NULL UNIQUE,
                source TEXT NOT NULL DEFAULT '',
                ticker TEXT NOT NULL DEFAULT '',
                event_type TEXT NOT NULL DEFAULT '',
                occurred_at TEXT NOT NULL DEFAULT '',
                payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                dedupe_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                analysis_id TEXT NOT NULL DEFAULT '',
                result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_core_status_created ON events_core(status, created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_core_ticker_created ON events_core(ticker, created_at DESC)")
        con.commit()
        return {"ok": True, "backend": "postgres"}
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}
    finally:
        con.close()


def create_event(
    *,
    source: str,
    ticker: str,
    event_type: str,
    occurred_at: str = "",
    payload: dict[str, Any] | None = None,
    dedupe_key: str = "",
) -> dict[str, Any]:
    ensure_events_schema()
    src = str(source or "").strip()[:64]
    tk = str(ticker or "").strip().upper()[:16]
    et = str(event_type or "").strip()[:120]
    oa = str(occurred_at or "").strip() or _now()
    p = payload if isinstance(payload, dict) else {}
    dk = str(dedupe_key or "").strip() or _dedupe_key(src, tk, et, oa, p)
    uid = _event_uid()
    now = _now()
    con = pg_connect()
    if con is None:
        return {"ok": False, "error": "pg_not_available"}
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO events_core
            (event_uid, source, ticker, event_type, occurred_at, payload_json, dedupe_key, status, attempts, analysis_id, result_json, last_error, created_at, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,'pending',0,'', '{}'::jsonb, '', %s, %s)
            ON CONFLICT(dedupe_key) DO NOTHING
            RETURNING id, event_uid
            """,
            (uid, src, tk, et, oa, _json_s(p), dk, now, now),
        )
        row = cur.fetchone()
        if row:
            con.commit()
            return {"ok": True, "created": True, "id": int(row[0] or 0), "event_uid": str(row[1] or uid), "dedupe_key": dk}
        cur.execute("SELECT id, event_uid FROM events_core WHERE dedupe_key=%s LIMIT 1", (dk,))
        row2 = cur.fetchone()
        con.commit()
        return {
            "ok": True,
            "created": False,
            "id": int((row2 or [0, ""])[0] or 0),
            "event_uid": str((row2 or ["", ""])[1] or ""),
            "dedupe_key": dk,
        }
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}
    finally:
        con.close()


def get_event(event_ref: str) -> dict[str, Any] | None:
    ensure_events_schema()
    ref = str(event_ref or "").strip()
    if not ref:
        return None
    by_id = ref.isdigit()
    con = pg_connect()
    if con is None:
        return None
    try:
        cur = con.cursor()
        if by_id:
            cur.execute(
                "SELECT id,event_uid,source,ticker,event_type,occurred_at,payload_json,dedupe_key,status,attempts,analysis_id,result_json,last_error,created_at,updated_at FROM events_core WHERE id=%s LIMIT 1",
                (int(ref),),
            )
        else:
            cur.execute(
                "SELECT id,event_uid,source,ticker,event_type,occurred_at,payload_json,dedupe_key,status,attempts,analysis_id,result_json,last_error,created_at,updated_at FROM events_core WHERE event_uid=%s LIMIT 1",
                (ref,),
            )
        r = cur.fetchone()
        if not r:
            return None
        return {
            "id": int(r[0] or 0),
            "event_uid": str(r[1] or ""),
            "source": str(r[2] or ""),
            "ticker": str(r[3] or ""),
            "event_type": str(r[4] or ""),
            "occurred_at": str(r[5] or ""),
            "payload": dict(r[6] or {}) if isinstance(r[6], dict) else {},
            "dedupe_key": str(r[7] or ""),
            "status": str(r[8] or "pending"),
            "attempts": int(r[9] or 0),
            "analysis_id": str(r[10] or ""),
            "result": dict(r[11] or {}) if isinstance(r[11], dict) else {},
            "last_error": str(r[12] or ""),
            "created_at": str(r[13] or ""),
            "updated_at": str(r[14] or ""),
        }
    except Exception:
        return None
    finally:
        con.close()


def list_events(status: str = "", limit: int = 30) -> list[dict[str, Any]]:
    ensure_events_schema()
    lim = max(1, min(500, int(limit or 30)))
    st = str(status or "").strip().lower()
    out: list[dict[str, Any]] = []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        if st:
            cur.execute(
                "SELECT id,event_uid,source,ticker,event_type,occurred_at,status,attempts,analysis_id,last_error,created_at,updated_at FROM events_core WHERE status=%s ORDER BY id DESC LIMIT %s",
                (st, lim),
            )
        else:
            cur.execute(
                "SELECT id,event_uid,source,ticker,event_type,occurred_at,status,attempts,analysis_id,last_error,created_at,updated_at FROM events_core ORDER BY id DESC LIMIT %s",
                (lim,),
            )
        for r in cur.fetchall() or []:
            out.append(
                {
                    "id": int(r[0] or 0),
                    "event_uid": str(r[1] or ""),
                    "source": str(r[2] or ""),
                    "ticker": str(r[3] or ""),
                    "event_type": str(r[4] or ""),
                    "occurred_at": str(r[5] or ""),
                    "status": str(r[6] or "pending"),
                    "attempts": int(r[7] or 0),
                    "analysis_id": str(r[8] or ""),
                    "last_error": str(r[9] or ""),
                    "created_at": str(r[10] or ""),
                    "updated_at": str(r[11] or ""),
                }
            )
        return out
    except Exception:
        return []
    finally:
        con.close()


def enqueue_process_event(event_ref: str) -> dict[str, Any]:
    ev = get_event(event_ref)
    if not ev:
        return {"ok": False, "error": "event_not_found", "event_ref": str(event_ref or "")}
    payload = {
        "job_type": "process_event",
        "event_id": int(ev.get("id") or 0),
        "event_uid": str(ev.get("event_uid") or ""),
        "query": f"process event {int(ev.get('id') or 0)}",
        "context": {"session_id": str(ev.get("event_uid") or f"event:{int(ev.get('id') or 0)}")},
    }
    jid = enqueue_job(payload)
    return {"ok": True, "status": "queued", "job_id": jid, "event_id": int(ev.get("id") or 0), "event_uid": str(ev.get("event_uid") or "")}


def _update_event_row(event_id: int, *, status: str, attempts_delta: int = 0, analysis_id: str = "", result: dict[str, Any] | None = None, last_error: str = "") -> None:
    eid = int(event_id or 0)
    if eid <= 0:
        return
    now = _now()
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        cur.execute(
            """
            UPDATE events_core
            SET status=%s,
                attempts=attempts + %s,
                analysis_id=CASE WHEN %s <> '' THEN %s ELSE analysis_id END,
                result_json=CASE WHEN %s <> '{}' THEN %s::jsonb ELSE result_json END,
                last_error=%s,
                updated_at=%s
            WHERE id=%s
            """,
            (
                str(status or "pending"),
                int(attempts_delta or 0),
                str(analysis_id or ""),
                str(analysis_id or ""),
                _json_s(result or {}),
                _json_s(result or {}),
                str(last_error or "")[:1000],
                now,
                eid,
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


def process_event_now(event_ref: str) -> dict[str, Any]:
    """Legacy: routes to process_event for compatibility."""
    return process_event(event_ref)


MAX_EVENT_ATTEMPTS = 3


def process_event(event_ref: str) -> dict[str, Any]:
    """
    Phase 3.1: Idempotent event processor.

    Routes by event_type:
      - sec_filing  → process_new_filings_pipeline([filing_id]) + run_event_driven_monitor
      - news_signal / monitor_trigger → run_event_driven_monitor(force=True)
      - anything else → enqueue_sector_map_reduce (legacy path)

    Idempotency: if status='done', returns cached result immediately.
    Retry guard: skips if attempts >= MAX_EVENT_ATTEMPTS.
    """
    ev = get_event(event_ref)
    if not ev:
        return {"ok": False, "error": "event_not_found", "event_ref": str(event_ref or "")}

    eid = int(ev.get("id") or 0)
    status = str(ev.get("status") or "pending")
    attempts = int(ev.get("attempts") or 0)
    event_type = str(ev.get("event_type") or "").strip().lower()
    payload = dict(ev.get("payload") or {})
    ticker = str(ev.get("ticker") or "").strip().upper()

    # Idempotent: already done — return cached result
    if status == "done":
        return {"ok": True, "event_id": eid, "cached": True, "result": dict(ev.get("result") or {})}

    # Retry guard
    if attempts >= MAX_EVENT_ATTEMPTS:
        return {"ok": False, "event_id": eid, "error": "max_retries_exceeded", "attempts": attempts}

    _update_event_row(eid, status="running", attempts_delta=1, last_error="")

    try:
        if event_type == "sec_filing":
            res = _process_sec_filing_event(eid, ticker, payload)
        elif event_type == "universe_signal":
            res = _process_universe_signal_event(eid, ticker, payload)
        elif event_type == "insider_trade_signal":
            res = _process_insider_trade_event(eid, ticker, payload)
        elif event_type in ("news_signal", "monitor_trigger"):
            res = _process_monitor_trigger_event(eid, ticker)
        else:
            # Legacy map-reduce path for unknown event types
            res = _process_legacy_event(eid, ticker, payload, event_type)

        _update_event_row(eid, status="done", result=res)
        return {"ok": True, "event_id": eid, "result": res}

    except Exception as exc:
        err = str(exc)[:500]
        # Classify error type for observability
        if "connection" in err.lower() or "timeout" in err.lower():
            err_class = "network"
        elif "json" in err.lower() or "parse" in err.lower():
            err_class = "parse"
        elif "llm" in err.lower() or "model" in err.lower() or "openai" in err.lower():
            err_class = "model"
        else:
            err_class = "write"
        _update_event_row(
            eid,
            status="error",
            last_error=f"[{err_class}] {err}",
            result={"status": "error", "error_class": err_class, "message": err[:280]},
        )
        return {"ok": False, "event_id": eid, "error": err, "error_class": err_class}


def _process_sec_filing_event(eid: int, ticker: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Handle sec_filing event: run filing pipeline + monitor."""
    filing_id = int(payload.get("filing_id") or 0)
    form = str(payload.get("form") or "").strip()
    accession = str(payload.get("accession") or "").strip()

    pipeline_result: dict[str, Any] = {}
    if filing_id > 0:
        from app.services.sec_ingest_pipeline_service import process_new_filings_pipeline
        pipeline_result = process_new_filings_pipeline([filing_id])

    # Always run the monitor to pick up this filing for proposals
    from app.services.proactive_ai_service import run_event_driven_monitor
    monitor_result = run_event_driven_monitor(force=True)

    # Earnings analysis: if 8-K or 10-Q, try to extract and analyze earnings content
    earnings_analyzed = 0
    if form in ("8-K", "10-Q", "6-K", "20-F"):
        try:
            from app.services.earnings_transcript_service import analyze_latest_earnings_for_ticker
            ea = analyze_latest_earnings_for_ticker(ticker)
            earnings_analyzed = int((ea or {}).get("analyzed") or 0)
        except Exception:
            pass

    return {
        "status": "done",
        "filing_id": filing_id,
        "form": form,
        "accession": accession,
        "ticker": ticker,
        "pipeline": {
            "chunks": int((pipeline_result or {}).get("chunks") or 0),
            "entities": int((pipeline_result or {}).get("entities") or 0),
        },
        "monitor": {
            "created": int((monitor_result or {}).get("created") or 0),
        },
        "earnings_analyzed": earnings_analyzed,
    }


def _process_universe_signal_event(eid: int, ticker: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Handle universe_signal event (non-held ticker filing).
    Reads the filing text, then asks the LLM: does this affect any held position?
    Only creates cascade alerts if there's a real connection — discards noise.
    """
    from pathlib import Path
    path = str(payload.get("path") or "").strip()
    form = str(payload.get("form") or "").strip()

    filing_text = ""
    if path:
        try:
            filing_text = Path(path).read_text(encoding="utf-8", errors="ignore")[:10000]
        except Exception:
            pass

    if not filing_text:
        return {"status": "done", "ticker": ticker, "cascades": 0, "reason": "no_filing_text"}

    from app.services.proactive_ai_service import _read_scope_tickers, analyze_universe_signal_for_portfolio
    portfolio, watchlist, _ = _read_scope_tickers()
    held = sorted(set(portfolio) | set(watchlist))

    if not held:
        return {"status": "done", "ticker": ticker, "cascades": 0, "reason": "no_held_tickers"}

    result = analyze_universe_signal_for_portfolio(
        universe_ticker=ticker,
        filing_text=filing_text,
        filing_form=form,
        held_tickers=held,
    )
    return {
        "status": "done",
        "ticker": ticker,
        "form": form,
        "cascades": int(result.get("cascades") or 0),
        "relevant": bool(result.get("relevant")),
    }


def _process_insider_trade_event(eid: int, ticker: str, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Handle insider_trade_signal: significant executive open-market trade.
    Runs cascade analysis with the insider signal as trigger — surfaces impact on other holdings.
    """
    signal_summary = str(payload.get("signal_summary") or "").strip()
    tx_type = str(payload.get("tx_type") or "S").strip().upper()
    filer_title = str(payload.get("filer_title") or "executive").strip()
    est_value = float(payload.get("est_value") or 0.0)

    if not signal_summary:
        signal_summary = f"Form 4: {filer_title} {tx_type} ~${est_value:,.0f}"

    from app.services.proactive_ai_service import analyze_portfolio_cascades
    result = analyze_portfolio_cascades(
        trigger_ticker=ticker,
        trigger_signal=signal_summary,
        trigger_proposal_id=0,
    )
    return {
        "status": "done",
        "ticker": ticker,
        "signal": signal_summary,
        "cascades": int((result or {}).get("cascades") or 0),
    }


def _process_monitor_trigger_event(eid: int, ticker: str) -> dict[str, Any]:
    """Handle news_signal/monitor_trigger: run the proposal monitor."""
    from app.services.proactive_ai_service import run_event_driven_monitor
    monitor_result = run_event_driven_monitor(force=True)
    return {
        "status": "done",
        "ticker": ticker,
        "monitor": {
            "created": int((monitor_result or {}).get("created") or 0),
        },
    }


def _process_legacy_event(eid: int, ticker: str, payload: dict[str, Any], event_type: str) -> dict[str, Any]:
    """Legacy fallback: use sector map-reduce for unknown event types."""
    tickers = [str(x or "").strip().upper() for x in list(payload.get("tickers") or []) if str(x or "").strip()]
    if not tickers and ticker:
        tickers = [ticker]
    q = str(payload.get("question") or "").strip()
    if not q:
        q = f"Evaluate latest {event_type} impact for {', '.join(tickers[:8]) or 'watchlist'}."
    analysis_id = ""
    if tickers:
        mr = enqueue_sector_map_reduce(tickers=tickers, question=q, session_id=f"event:{eid}")
        analysis_id = str(mr.get("reducer_job_id") or "").strip()
        return {"status": "queued", "map_reduce": mr}
    return {"status": "ignored", "reason": "no_ticker_scope"}


def replay_event(event_ref: str) -> dict[str, Any]:
    ev = get_event(event_ref)
    if not ev:
        return {"ok": False, "error": "event_not_found", "event_ref": str(event_ref or "")}
    eid = int(ev.get("id") or 0)
    _update_event_row(eid, status="pending", attempts_delta=0, analysis_id="", result={"status": "replayed"}, last_error="")
    return enqueue_process_event(str(eid))


def event_snapshot(event_ref: str) -> dict[str, Any]:
    ev = get_event(event_ref)
    if not ev:
        return {"ok": False, "error": "event_not_found"}
    aid = str(ev.get("analysis_id") or "").strip()
    rep = get_map_reduce_report(aid) if aid else None
    return {"ok": True, "event": ev, "analysis_report": rep if isinstance(rep, dict) else None}
