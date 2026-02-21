#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures as cf
import os
import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.routers.ai import _ai_command_sync
from app.services.chat_memory_service import append_chat_message, ensure_chat_memory_schema
from app.services.ai_job_queue_service import (
    claim_next_job,
    complete_job,
    ensure_ai_job_queue_schema,
    fail_job,
    get_job,
    heartbeat,
    set_cached_result,
    touch_worker,
)
from app.services.phase2_scaling_service import save_map_reduce_report
from tools.llm_engine import ask_ai


def _run_with_timeout(fn, timeout_sec: float):
    with cf.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(fn)
        return fut.result(timeout=max(5.0, float(timeout_sec or 45.0)))


def _map_analyze(ticker: str, question: str, task_prompt: str) -> dict:
    tk = str(ticker or "").strip().upper()
    q = str(question or "").strip()
    tp = str(task_prompt or "").strip()
    prompt = (
        f"You are an elite fundamental equity analyst.\n"
        f"Ticker: {tk}\n"
        f"Sector question: {q}\n"
        f"Task context: {tp[:1800]}\n\n"
        "Return concise company-specific analysis focused on margins, demand, pricing power, and near-term durability."
    )
    try:
        txt = str(
            ask_ai(
                prompt,
                "Map-step analyst. Direct answer only.",
                mode="smart",
                temperature=0.1,
            )
            or ""
        ).strip()
    except Exception:
        txt = ""
    if not txt:
        txt = f"{tk}: insufficient signal extracted for margin durability analysis."
    return {
        "status": "ok",
        "intent": "map_reduce_map",
        "message": txt[:4000],
        "confidence": 0.65,
    }


def _run_reduce_task(payload: dict) -> dict:
    p = payload if isinstance(payload, dict) else {}
    child_ids = [str(x or "").strip() for x in list(p.get("child_job_ids") or []) if str(x or "").strip()]
    tickers = [str(x or "").strip().upper() for x in list(p.get("tickers") or []) if str(x or "").strip()]
    question = str(p.get("question") or "").strip()
    if not child_ids:
        return {"status": "error", "intent": "map_reduce_reduce", "message": "No child jobs found."}
    wait_sec = max(2.0, min(45.0, float(os.getenv("AI_MAP_REDUCE_WAIT_SEC", "18"))))
    deadline = time.time() + wait_sec
    complete_rows: list[dict] = []
    errored = 0
    while True:
        complete_rows = []
        errored = 0
        pending = 0
        for jid in child_ids:
            row = get_job(jid)
            if not isinstance(row, dict):
                pending += 1
                continue
            st = str(row.get("status") or "").strip().lower()
            if st == "done":
                complete_rows.append(row)
            elif st == "error":
                errored += 1
            else:
                pending += 1
        if pending <= 0 or time.time() >= deadline:
            break
        time.sleep(0.5)
    mini_reports: list[str] = []
    for row in complete_rows:
        res = row.get("result") if isinstance(row.get("result"), dict) else {}
        msg = str((res or {}).get("message") or "").strip()
        if msg:
            mini_reports.append(msg[:1200])
    combined = "\n\n".join(f"- {x}" for x in mini_reports[:40])
    q = question or "Summarize the sector readout"
    synth_payload = {
        "query": (
            f"Sector synthesis for tickers {', '.join(tickers[:40])}.\n"
            f"Question: {q}\n"
            "Use the child analyses below and produce one concise integrated conclusion with key divergences.\n"
            "Do not route to navigation, reports list, or follow-up confirmation prompts.\n"
            "Return direct analysis only.\n"
            f"Child analyses:\n{combined}"
        ),
        "context": {
            "session_id": str((p.get("context") or {}).get("session_id") or "map_reduce_reduce"),
            "force_deep_reasoning": True,
            "disable_manager_interrupts": True,
        },
        "job_type": "reduce_task",
    }
    try:
        out_txt = str(
            ask_ai(
                synth_payload["query"],
                "Reducer analyst. Synthesize cross-company margin durability with direct answer only.",
                mode="smart",
                temperature=0.1,
            )
            or ""
        ).strip()
    except Exception:
        out_txt = ""
    out = {
        "status": "ok" if out_txt else "needs_clarification",
        "intent": "map_reduce_reduce",
        "message": out_txt or "Reducer could not synthesize a confident sector view from child analyses.",
        "confidence": 0.68 if out_txt else 0.35,
    }
    final = out if isinstance(out, dict) else {"status": "ok", "message": str(out or "")}
    if not str(final.get("message") or "").strip():
        if mini_reports:
            final["message"] = "Sector analysis completed. Key findings were generated; open traces/result details for full output."
        else:
            final["message"] = "Sector analysis completed, but no child analyses were available."
    final["intent"] = "map_reduce_reduce"
    final["child_total"] = len(child_ids)
    final["child_done"] = len(complete_rows)
    final["child_error"] = errored
    try:
        save_map_reduce_report(
            reducer_job_id=str(p.get("job_id") or ""),
            session_id=str((p.get("context") or {}).get("session_id") or "map_reduce"),
            tickers=tickers,
            question=question,
            result=final,
            status=str(final.get("status") or "done"),
        )
    except Exception:
        pass
    # Push reducer completion into chat memory so users see result without polling another endpoint.
    try:
        sid = str((p.get("context") or {}).get("session_id") or "map_reduce").strip()[:120]
        if sid:
            ensure_chat_memory_schema()
            title = f"Phase 2 Sector Analysis Complete ({', '.join(tickers[:8])})"
            body = str(final.get("message") or "").strip()
            msg = f"{title}\n\n{body}" if body else title
            append_chat_message(
                session_id=sid,
                role="assistant",
                text=msg[:6000],
                intent="map_reduce_reduce",
                status=str(final.get("status") or "done")[:80],
            )
    except Exception:
        pass
    return final


def main() -> int:
    ensure_ai_job_queue_schema()
    worker_id = os.getenv("AI_WORKER_ID", "").strip() or f"{socket.gethostname()}-{os.getpid()}"
    idle_sleep = max(0.1, min(5.0, float(os.getenv("AI_WORKER_IDLE_SEC", "0.6"))))
    job_timeout_sec = max(8.0, min(240.0, float(os.getenv("AI_WORKER_JOB_TIMEOUT_SEC", "65"))))
    run_once = str(os.getenv("AI_WORKER_ONCE", "0")).strip().lower() in {"1", "true", "yes", "on"}
    print(f"[ai-worker] started worker_id={worker_id}")
    touch_worker(worker_id)
    while True:
        touch_worker(worker_id)
        job = claim_next_job(worker_id=worker_id, stale_after_sec=120)
        if not isinstance(job, dict):
            if run_once:
                return 0
            time.sleep(idle_sleep)
            continue
        jid = str(job.get("id") or "").strip()
        payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        if not jid:
            time.sleep(idle_sleep)
            continue
        stop_hb = None
        try:
            print(f"[ai-worker] claimed job={jid}")
            heartbeat(jid, worker_id)
            stop_hb = threading.Event()

            def _job_hb() -> None:
                while not stop_hb.wait(2.5):
                    try:
                        heartbeat(jid, worker_id)
                        touch_worker(worker_id)
                    except Exception:
                        pass

            hb_thread = threading.Thread(target=_job_hb, name=f"ai-worker-hb-{jid[:6]}", daemon=True)
            hb_thread.start()
            p = payload if isinstance(payload, dict) else {}
            job_type = str(p.get("job_type") or "").strip().lower()
            if job_type == "reduce_task":
                p["job_id"] = jid
                out = _run_with_timeout(lambda: _run_reduce_task(p), job_timeout_sec)
            elif job_type == "map_task":
                mp = dict(p)
                q = str(mp.get("query") or "").strip()
                tk = str(mp.get("ticker") or "").strip().upper()
                qq = str(mp.get("question") or "").strip()
                out = _run_with_timeout(lambda: _map_analyze(tk, qq, q), job_timeout_sec)
            else:
                out = _run_with_timeout(lambda: _ai_command_sync(p), job_timeout_sec)
            stop_hb.set()
            result = out if isinstance(out, dict) else {"status": "ok", "message": str(out or "")}
            complete_job(jid, result)
            if isinstance(payload, dict):
                set_cached_result(payload, result)
            print(f"[ai-worker] completed job={jid} intent={str(result.get('intent') or '')}")
        except cf.TimeoutError:
            try:
                if stop_hb is not None:
                    stop_hb.set()
            except Exception:
                pass
            msg = f"worker_timeout_after_{int(job_timeout_sec)}s"
            print(f"[ai-worker] failed job={jid} error={msg}")
            fail_job(
                jid,
                error=msg,
                result={"status": "error", "intent": "worker_timeout", "message": "Background analysis timed out. Please retry."},
            )
        except Exception as exc:
            try:
                if stop_hb is not None:
                    stop_hb.set()
            except Exception:
                pass
            print(f"[ai-worker] failed job={jid} error={str(exc)}")
            fail_job(
                jid,
                error=str(exc),
                result={"status": "error", "intent": "worker_error", "message": "Command failed."},
            )
        if run_once:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
