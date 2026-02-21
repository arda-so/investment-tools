from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from app.core.config import CORE_DB_PATH, DATA_DIR
from app.core.sqlite_hardening import connect_sqlite, sqlite_retry
from app.services.postgres_core_service import pg_connect

try:
    import redis
except Exception:  # pragma: no cover
    redis = None  # type: ignore[assignment]


def _conn() -> sqlite3.Connection:
    qpath = str(os.getenv("AI_QUEUE_DB_PATH", "")).strip()
    if not qpath:
        qpath = str(DATA_DIR / "ai_queue.db")
    p = Path(qpath)
    try:
        if not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Fallback to core DB if custom queue path is not writable.
        p = Path(str(CORE_DB_PATH))
    return connect_sqlite(str(p), row_factory=True)


def _queue_backend() -> str:
    return str(os.getenv("AI_QUEUE_BACKEND", "postgres")).strip().lower()


def _is_production() -> bool:
    env = str(os.getenv("APP_ENV", os.getenv("ENVIRONMENT", ""))).strip().lower()
    return env in {"prod", "production"}


def _queue_policy_guard() -> None:
    strict_prod = str(os.getenv("AI_QUEUE_STRICT_PROD", "1")).strip().lower() in {"1", "true", "yes", "on"}
    if strict_prod and _is_production() and _queue_backend() == "sqlite":
        raise RuntimeError("sqlite_queue_forbidden_in_production")


def _redis_client():
    if redis is None:
        return None
    url = str(os.getenv("AI_REDIS_URL", "redis://127.0.0.1:6379/0")).strip()
    try:
        return redis.Redis.from_url(url, decode_responses=True)
    except Exception:
        return None


def _use_redis() -> bool:
    if _queue_backend() != "redis":
        return False
    r = _redis_client()
    if r is None:
        return False
    try:
        return bool(r.ping())
    except Exception:
        return False


def _use_postgres() -> bool:
    if _queue_backend() != "postgres":
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("SELECT 1")
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


def ensure_ai_job_queue_schema() -> None:
    _queue_policy_guard()
    if _use_redis():
        # Redis backend has no SQL schema requirement.
        return
    if _use_postgres():
        con = pg_connect()
        if con is None:
            return
        try:
            cur = con.cursor()
            cur.execute(
                """CREATE TABLE IF NOT EXISTS ai_command_jobs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    error_text TEXT NOT NULL DEFAULT '',
                    worker_id TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    heartbeat_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT ''
                )"""
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_jobs_status_created ON ai_command_jobs(status, created_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_jobs_heartbeat ON ai_command_jobs(heartbeat_at)")
            cur.execute(
                """CREATE TABLE IF NOT EXISTS ai_worker_heartbeats (
                    worker_id TEXT PRIMARY KEY,
                    heartbeat_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_worker_heartbeat ON ai_worker_heartbeats(heartbeat_at)")
            cur.execute(
                """CREATE TABLE IF NOT EXISTS ai_command_cache (
                    cache_key TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_cache_updated ON ai_command_cache(updated_at DESC)")
            con.commit()
            return
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
            return
        finally:
            con.close()
    con = _conn()
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS ai_command_jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                result_json TEXT NOT NULL DEFAULT '{}',
                error_text TEXT NOT NULL DEFAULT '',
                worker_id TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                started_at TEXT NOT NULL DEFAULT '',
                heartbeat_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT ''
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_ai_jobs_status_created ON ai_command_jobs(status, created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_ai_jobs_heartbeat ON ai_command_jobs(heartbeat_at)")
        con.execute(
            """CREATE TABLE IF NOT EXISTS ai_worker_heartbeats (
                worker_id TEXT PRIMARY KEY,
                heartbeat_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_ai_worker_heartbeat ON ai_worker_heartbeats(heartbeat_at)")
        con.execute(
            """CREATE TABLE IF NOT EXISTS ai_command_cache (
                cache_key TEXT PRIMARY KEY,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_ai_cache_updated ON ai_command_cache(updated_at DESC)")
        con.commit()
    finally:
        con.close()


def touch_worker(worker_id: str) -> None:
    wid = str(worker_id or "").strip()[:80]
    if not wid:
        return
    now = dt.datetime.now().isoformat()
    if _use_redis():
        r = _redis_client()
        if r is None:
            return
        try:
            r.hset(f"ai:worker:{wid}", mapping={"worker_id": wid, "heartbeat_at": now, "updated_at": now})
            r.expire(f"ai:worker:{wid}", max(60, int(str(os.getenv("AI_WORKER_HEARTBEAT_TTL_SEC", "300")).strip() or "300")))
        except Exception:
            pass
        return
    if _use_postgres():
        con = pg_connect()
        if con is None:
            return
        try:
            cur = con.cursor()
            cur.execute(
                """INSERT INTO ai_worker_heartbeats(worker_id, heartbeat_at, updated_at)
                   VALUES (%s, %s, %s)
                   ON CONFLICT(worker_id) DO UPDATE SET heartbeat_at=EXCLUDED.heartbeat_at, updated_at=EXCLUDED.updated_at""",
                (wid, now, now),
            )
            con.commit()
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
        finally:
            con.close()
        return
    ensure_ai_job_queue_schema()
    con = _conn()
    try:
        con.execute(
            """INSERT INTO ai_worker_heartbeats(worker_id, heartbeat_at, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(worker_id) DO UPDATE SET heartbeat_at=excluded.heartbeat_at, updated_at=excluded.updated_at""",
            (wid, now, now),
        )
        con.commit()
    finally:
        con.close()


def _cache_key(payload: dict[str, Any]) -> str:
    p = payload if isinstance(payload, dict) else {}
    q = str(p.get("query") or "").strip()
    ctx = p.get("context") if isinstance(p.get("context"), dict) else {}
    sid = str(ctx.get("session_id") or "default").strip()
    body = json.dumps({"q": q, "sid": sid}, ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8", errors="ignore")).hexdigest()


def get_cached_result(payload: dict[str, Any], ttl_sec: int = 75) -> dict[str, Any] | None:
    if _use_redis():
        r = _redis_client()
        if r is None:
            return None
        key = "ai:cache:" + _cache_key(payload)
        try:
            raw = r.get(key)
            if not raw:
                return None
            obj = json.loads(str(raw or "{}"))
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    if _use_postgres():
        ensure_ai_job_queue_schema()
        key = _cache_key(payload)
        now = dt.datetime.now()
        con = pg_connect()
        if con is None:
            return None
        try:
            cur = con.cursor()
            cur.execute("SELECT result_json, updated_at FROM ai_command_cache WHERE cache_key = %s LIMIT 1", (key,))
            row = cur.fetchone()
            if not row:
                return None
            upd = str(row[1] or "").strip()
            try:
                ts = dt.datetime.fromisoformat(upd.replace("Z", ""))
            except Exception:
                return None
            if (now - ts).total_seconds() > float(max(5, int(ttl_sec or 75))):
                return None
            try:
                obj = json.loads(str(row[0] or "{}"))
            except Exception:
                return None
            return obj if isinstance(obj, dict) else None
        finally:
            con.close()
    ensure_ai_job_queue_schema()
    key = _cache_key(payload)
    now = dt.datetime.now()
    con = _conn()
    try:
        row = con.execute(
            "SELECT result_json, updated_at FROM ai_command_cache WHERE cache_key = ? LIMIT 1",
            (key,),
        ).fetchone()
        if not row:
            return None
        upd = str(row["updated_at"] or "").strip()
        try:
            ts = dt.datetime.fromisoformat(upd.replace("Z", ""))
        except Exception:
            return None
        if (now - ts).total_seconds() > float(max(5, int(ttl_sec or 75))):
            return None
        try:
            obj = json.loads(str(row["result_json"] or "{}"))
        except Exception:
            return None
        return obj if isinstance(obj, dict) else None
    finally:
        con.close()


def set_cached_result(payload: dict[str, Any], result: dict[str, Any]) -> None:
    if _use_redis():
        r = _redis_client()
        if r is None:
            return
        key = "ai:cache:" + _cache_key(payload)
        ttl = max(15, min(600, int(float(os.getenv("AI_PROMPT_CACHE_TTL_SEC", "75")))))
        try:
            r.setex(key, ttl, json.dumps(result or {}, ensure_ascii=True))
        except Exception:
            pass
        return
    if _use_postgres():
        ensure_ai_job_queue_schema()
        key = _cache_key(payload)
        now = dt.datetime.now().isoformat()
        con = pg_connect()
        if con is None:
            return
        try:
            cur = con.cursor()
            cur.execute(
                """INSERT INTO ai_command_cache(cache_key, result_json, created_at, updated_at)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT(cache_key) DO UPDATE SET
                     result_json=EXCLUDED.result_json,
                     updated_at=EXCLUDED.updated_at""",
                (key, json.dumps(result or {}, ensure_ascii=True), now, now),
            )
            con.commit()
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
        finally:
            con.close()
        return
    ensure_ai_job_queue_schema()
    key = _cache_key(payload)
    now = dt.datetime.now().isoformat()
    con = _conn()
    try:
        con.execute(
            """INSERT INTO ai_command_cache(cache_key, result_json, created_at, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(cache_key) DO UPDATE SET
                 result_json=excluded.result_json,
                 updated_at=excluded.updated_at""",
            (key, json.dumps(result or {}, ensure_ascii=True), now, now),
        )
        con.commit()
    finally:
        con.close()


def enqueue_job(payload: dict[str, Any]) -> str:
    if _use_redis():
        r = _redis_client()
        if r is not None:
            jid = "aj_" + uuid.uuid4().hex[:16]
            now = dt.datetime.now().isoformat()
            job_key = f"ai:job:{jid}"
            try:
                p = r.pipeline()
                p.hset(
                    job_key,
                    mapping={
                        "id": jid,
                        "status": "queued",
                        "payload_json": json.dumps(payload or {}, ensure_ascii=True),
                        "result_json": "{}",
                        "error_text": "",
                        "worker_id": "",
                        "attempts": "0",
                        "created_at": now,
                        "updated_at": now,
                        "started_at": "",
                        "heartbeat_at": "",
                        "finished_at": "",
                    },
                )
                p.rpush("ai:jobs:queue", jid)
                p.execute()
                return jid
            except Exception:
                pass
    if _use_postgres():
        ensure_ai_job_queue_schema()
        jid = "aj_" + uuid.uuid4().hex[:16]
        now = dt.datetime.now().isoformat()
        con = pg_connect()
        if con is None:
            return jid
        try:
            cur = con.cursor()
            cur.execute(
                """INSERT INTO ai_command_jobs
                   (id, status, payload_json, result_json, error_text, worker_id, attempts, created_at, updated_at, started_at, heartbeat_at, finished_at)
                   VALUES (%s, 'queued', %s, '{}', '', '', 0, %s, %s, '', '', '')""",
                (jid, json.dumps(payload or {}, ensure_ascii=True), now, now),
            )
            con.commit()
            return jid
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
            return jid
        finally:
            con.close()
    ensure_ai_job_queue_schema()
    jid = "aj_" + uuid.uuid4().hex[:16]
    now = dt.datetime.now().isoformat()
    out: dict[str, str] = {"id": ""}

    def _write() -> None:
        con = _conn()
        try:
            con.execute(
                """INSERT INTO ai_command_jobs
                   (id, status, payload_json, result_json, error_text, worker_id, attempts, created_at, updated_at, started_at, heartbeat_at, finished_at)
                   VALUES (?, 'queued', ?, '{}', '', '', 0, ?, ?, '', '', '')""",
                (jid, json.dumps(payload or {}, ensure_ascii=True), now, now),
            )
            con.commit()
            out["id"] = jid
        finally:
            con.close()

    sqlite_retry(_write)
    return str(out.get("id") or jid)


def get_job(job_id: str) -> dict[str, Any] | None:
    if _use_redis():
        r = _redis_client()
        if r is None:
            return None
        jid = str(job_id or "").strip()
        if not jid:
            return None
        key = f"ai:job:{jid}"
        try:
            data = r.hgetall(key) or {}
            if not data:
                return None
            payload = {}
            result = {}
            try:
                payload = json.loads(str(data.get("payload_json") or "{}"))
            except Exception:
                payload = {}
            try:
                result = json.loads(str(data.get("result_json") or "{}"))
            except Exception:
                result = {}
            return {
                "id": str(data.get("id") or jid),
                "status": str(data.get("status") or ""),
                "payload": payload if isinstance(payload, dict) else {},
                "result": result if isinstance(result, dict) else {},
                "error": str(data.get("error_text") or ""),
                "worker_id": str(data.get("worker_id") or ""),
                "attempts": int(str(data.get("attempts") or "0") or 0),
                "created_at": str(data.get("created_at") or ""),
                "updated_at": str(data.get("updated_at") or ""),
                "started_at": str(data.get("started_at") or ""),
                "heartbeat_at": str(data.get("heartbeat_at") or ""),
                "finished_at": str(data.get("finished_at") or ""),
            }
        except Exception:
            return None
    if _use_postgres():
        ensure_ai_job_queue_schema()
        jid = str(job_id or "").strip()
        if not jid:
            return None
        con = pg_connect()
        if con is None:
            return None
        try:
            cur = con.cursor()
            cur.execute(
                """SELECT id, status, payload_json, result_json, error_text, worker_id, attempts,
                          created_at, updated_at, started_at, heartbeat_at, finished_at
                   FROM ai_command_jobs WHERE id = %s LIMIT 1""",
                (jid,),
            )
            row = cur.fetchone()
            if not row:
                return None
            try:
                payload = json.loads(str(row[2] or "{}"))
            except Exception:
                payload = {}
            try:
                result = json.loads(str(row[3] or "{}"))
            except Exception:
                result = {}
            return {
                "id": str(row[0] or ""),
                "status": str(row[1] or ""),
                "payload": payload if isinstance(payload, dict) else {},
                "result": result if isinstance(result, dict) else {},
                "error": str(row[4] or ""),
                "worker_id": str(row[5] or ""),
                "attempts": int(row[6] or 0),
                "created_at": str(row[7] or ""),
                "updated_at": str(row[8] or ""),
                "started_at": str(row[9] or ""),
                "heartbeat_at": str(row[10] or ""),
                "finished_at": str(row[11] or ""),
            }
        finally:
            con.close()
    ensure_ai_job_queue_schema()
    jid = str(job_id or "").strip()
    if not jid:
        return None
    con = _conn()
    try:
        row = con.execute(
            """SELECT id, status, payload_json, result_json, error_text, worker_id, attempts,
                      created_at, updated_at, started_at, heartbeat_at, finished_at
               FROM ai_command_jobs WHERE id = ? LIMIT 1""",
            (jid,),
        ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except Exception:
            payload = {}
        try:
            result = json.loads(str(row["result_json"] or "{}"))
        except Exception:
            result = {}
        return {
            "id": str(row["id"] or ""),
            "status": str(row["status"] or ""),
            "payload": payload if isinstance(payload, dict) else {},
            "result": result if isinstance(result, dict) else {},
            "error": str(row["error_text"] or ""),
            "worker_id": str(row["worker_id"] or ""),
            "attempts": int(row["attempts"] or 0),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
            "started_at": str(row["started_at"] or ""),
            "heartbeat_at": str(row["heartbeat_at"] or ""),
            "finished_at": str(row["finished_at"] or ""),
        }
    finally:
        con.close()


def claim_next_job(worker_id: str, stale_after_sec: int = 120) -> dict[str, Any] | None:
    if _use_redis():
        r = _redis_client()
        if r is None:
            return None
        wid = str(worker_id or "").strip()[:80] or "worker"
        jid = ""
        try:
            jid = str(r.lpop("ai:jobs:queue") or "").strip()
        except Exception:
            jid = ""
        if not jid:
            return None
        key = f"ai:job:{jid}"
        now = dt.datetime.now().isoformat()
        try:
            p = r.pipeline()
            p.hincrby(key, "attempts", 1)
            p.hset(
                key,
                mapping={
                    "status": "running",
                    "worker_id": wid,
                    "updated_at": now,
                    "started_at": now,
                    "heartbeat_at": now,
                },
            )
            p.execute()
        except Exception:
            return None
        return get_job(jid)
    if _use_postgres():
        ensure_ai_job_queue_schema()
        wid = str(worker_id or "").strip()[:80] or "worker"
        now_dt = dt.datetime.now()
        now = now_dt.isoformat()
        stale_cut = (now_dt - dt.timedelta(seconds=max(30, int(stale_after_sec or 120)))).isoformat()
        con = pg_connect()
        if con is None:
            return None
        try:
            cur = con.cursor()
            cur.execute("BEGIN")
            cur.execute(
                """SELECT id, attempts
                   FROM ai_command_jobs
                   WHERE status='queued'
                      OR (status='running' AND COALESCE(heartbeat_at,'') <> '' AND heartbeat_at < %s)
                   ORDER BY created_at ASC
                   LIMIT 1
                   FOR UPDATE SKIP LOCKED""",
                (stale_cut,),
            )
            row = cur.fetchone()
            if not row:
                con.commit()
                return None
            jid = str(row[0] or "").strip()
            attempts = int(row[1] or 0) + 1
            cur.execute(
                """UPDATE ai_command_jobs
                   SET status='running',
                       worker_id=%s,
                       attempts=%s,
                       updated_at=%s,
                       started_at=CASE WHEN COALESCE(started_at,'')='' THEN %s ELSE started_at END,
                       heartbeat_at=%s
                   WHERE id=%s""",
                (wid, attempts, now, now, now, jid),
            )
            con.commit()
            return get_job(jid)
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
            return None
        finally:
            con.close()
    ensure_ai_job_queue_schema()
    wid = str(worker_id or "").strip()[:80] or "worker"
    now_dt = dt.datetime.now()
    now = now_dt.isoformat()
    stale_cut = (now_dt - dt.timedelta(seconds=max(30, int(stale_after_sec or 120)))).isoformat()
    con = _conn()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """SELECT id, payload_json, attempts FROM ai_command_jobs
               WHERE status='queued'
                  OR (status='running' AND COALESCE(heartbeat_at,'') <> '' AND heartbeat_at < ?)
               ORDER BY created_at ASC
               LIMIT 1""",
            (stale_cut,),
        ).fetchone()
        if not row:
            con.execute("COMMIT")
            return None
        jid = str(row["id"] or "").strip()
        attempts = int(row["attempts"] or 0) + 1
        con.execute(
            """UPDATE ai_command_jobs
               SET status='running',
                   worker_id=?,
                   attempts=?,
                   updated_at=?,
                   started_at=CASE WHEN COALESCE(started_at,'')='' THEN ? ELSE started_at END,
                   heartbeat_at=?
               WHERE id=?""",
            (wid, attempts, now, now, now, jid),
        )
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        con.close()
    return get_job(jid)


def heartbeat(job_id: str, worker_id: str) -> None:
    if _use_redis():
        r = _redis_client()
        if r is None:
            return
        jid = str(job_id or "").strip()
        wid = str(worker_id or "").strip()
        if not jid:
            return
        now = dt.datetime.now().isoformat()
        try:
            r.hset(
                f"ai:job:{jid}",
                mapping={"heartbeat_at": now, "updated_at": now, "worker_id": wid},
            )
        except Exception:
            pass
        return
    if _use_postgres():
        jid = str(job_id or "").strip()
        wid = str(worker_id or "").strip()
        if not jid:
            return
        now = dt.datetime.now().isoformat()
        con = pg_connect()
        if con is None:
            return
        try:
            cur = con.cursor()
            cur.execute(
                "UPDATE ai_command_jobs SET heartbeat_at=%s, updated_at=%s, worker_id=%s WHERE id=%s AND status='running'",
                (now, now, wid, jid),
            )
            con.commit()
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
        finally:
            con.close()
        return
    jid = str(job_id or "").strip()
    wid = str(worker_id or "").strip()
    if not jid:
        return
    now = dt.datetime.now().isoformat()
    con = _conn()
    try:
        con.execute(
            "UPDATE ai_command_jobs SET heartbeat_at=?, updated_at=?, worker_id=? WHERE id=? AND status='running'",
            (now, now, wid, jid),
        )
        con.commit()
    finally:
        con.close()


def complete_job(job_id: str, result: dict[str, Any]) -> None:
    if _use_redis():
        r = _redis_client()
        if r is None:
            return
        jid = str(job_id or "").strip()
        if not jid:
            return
        now = dt.datetime.now().isoformat()
        try:
            r.hset(
                f"ai:job:{jid}",
                mapping={
                    "status": "done",
                    "result_json": json.dumps(result or {}, ensure_ascii=True),
                    "error_text": "",
                    "updated_at": now,
                    "finished_at": now,
                    "heartbeat_at": now,
                },
            )
        except Exception:
            pass
        return
    if _use_postgres():
        jid = str(job_id or "").strip()
        if not jid:
            return
        now = dt.datetime.now().isoformat()
        con = pg_connect()
        if con is None:
            return
        try:
            cur = con.cursor()
            cur.execute(
                """UPDATE ai_command_jobs
                   SET status='done', result_json=%s, error_text='', updated_at=%s, finished_at=%s, heartbeat_at=%s
                   WHERE id=%s""",
                (json.dumps(result or {}, ensure_ascii=True), now, now, now, jid),
            )
            con.commit()
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
        finally:
            con.close()
        return
    jid = str(job_id or "").strip()
    if not jid:
        return
    now = dt.datetime.now().isoformat()
    con = _conn()
    try:
        con.execute(
            """UPDATE ai_command_jobs
               SET status='done', result_json=?, error_text='', updated_at=?, finished_at=?, heartbeat_at=?
               WHERE id=?""",
            (json.dumps(result or {}, ensure_ascii=True), now, now, now, jid),
        )
        con.commit()
    finally:
        con.close()


def fail_job(job_id: str, error: str, result: dict[str, Any] | None = None) -> None:
    if _use_redis():
        r = _redis_client()
        if r is None:
            return
        jid = str(job_id or "").strip()
        if not jid:
            return
        now = dt.datetime.now().isoformat()
        try:
            r.hset(
                f"ai:job:{jid}",
                mapping={
                    "status": "error",
                    "result_json": json.dumps(result or {"status": "error", "message": "Command failed."}, ensure_ascii=True),
                    "error_text": str(error or "")[:1000],
                    "updated_at": now,
                    "finished_at": now,
                    "heartbeat_at": now,
                },
            )
        except Exception:
            pass
        return
    if _use_postgres():
        jid = str(job_id or "").strip()
        if not jid:
            return
        now = dt.datetime.now().isoformat()
        con = pg_connect()
        if con is None:
            return
        try:
            cur = con.cursor()
            cur.execute(
                """UPDATE ai_command_jobs
                   SET status='error', result_json=%s, error_text=%s, updated_at=%s, finished_at=%s, heartbeat_at=%s
                   WHERE id=%s""",
                (json.dumps(result or {"status": "error", "message": "Command failed."}, ensure_ascii=True), str(error or "")[:1000], now, now, now, jid),
            )
            con.commit()
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
        finally:
            con.close()
        return
    jid = str(job_id or "").strip()
    if not jid:
        return
    now = dt.datetime.now().isoformat()
    con = _conn()
    try:
        con.execute(
            """UPDATE ai_command_jobs
               SET status='error', result_json=?, error_text=?, updated_at=?, finished_at=?, heartbeat_at=?
               WHERE id=?""",
            (json.dumps(result or {"status": "error", "message": "Command failed."}, ensure_ascii=True), str(error or "")[:1000], now, now, now, jid),
        )
        con.commit()
    finally:
        con.close()


def worker_health(active_within_sec: int = 120) -> dict[str, Any]:
    now = dt.datetime.now()
    backend = "redis" if _use_redis() else ("postgres" if _use_postgres() else "sqlite")
    recent_window_sec = max(30, int(str(os.getenv("AI_HEALTH_ERROR_WINDOW_SEC", "300")).strip() or "300"))
    if backend == "redis":
        r = _redis_client()
        if r is None:
            return {
                "ok": False,
                "backend": "redis",
                "redis_connected": False,
                "queue_depth": 0,
                "counts": {"queued": 0, "running": 0, "done": 0, "error": 0},
                "active_workers": [],
            }
        counts = {"queued": 0, "running": 0, "done": 0, "error": 0}
        recent_error_count = 0
        workers: dict[str, str] = {}
        try:
            qd = int(r.llen("ai:jobs:queue") or 0)
        except Exception:
            qd = 0
        try:
            for key in r.scan_iter(match="ai:job:*", count=200):
                d = r.hgetall(key) or {}
                st = str(d.get("status") or "").strip().lower()
                if st in counts:
                    counts[st] += 1
                if st == "error":
                    upd = str(d.get("updated_at") or "").strip()
                    try:
                        upd_dt = dt.datetime.fromisoformat(upd.replace("Z", ""))
                        if (now - upd_dt).total_seconds() <= float(recent_window_sec):
                            recent_error_count += 1
                    except Exception:
                        # If timestamp is missing/invalid, count once as recent for safety.
                        recent_error_count += 1
                wid = str(d.get("worker_id") or "").strip()
                hb = str(d.get("heartbeat_at") or "").strip()
                if wid and hb:
                    workers[wid] = hb
            for wkey in r.scan_iter(match="ai:worker:*", count=100):
                wd = r.hgetall(wkey) or {}
                wid = str(wd.get("worker_id") or "").strip()
                hb = str(wd.get("heartbeat_at") or "").strip()
                if wid and hb:
                    workers[wid] = hb
        except Exception:
            pass
        active_workers: list[dict[str, str]] = []
        for wid, hb in workers.items():
            try:
                hb_dt = dt.datetime.fromisoformat(hb.replace("Z", ""))
                age = (now - hb_dt).total_seconds()
                if age <= float(max(10, int(active_within_sec or 120))):
                    active_workers.append({"worker_id": wid, "heartbeat_at": hb, "age_sec": f"{age:.1f}"})
            except Exception:
                continue
        return {
            "ok": True,
            "backend": "redis",
            "redis_connected": True,
            "queue_depth": qd,
            "counts": counts,
            "recent_error_count": int(recent_error_count),
            "active_workers": sorted(active_workers, key=lambda x: float(x.get("age_sec") or 0.0)),
            "asof": now.isoformat(),
        }
    if backend == "postgres":
        ensure_ai_job_queue_schema()
        con = pg_connect()
        if con is None:
            return {
                "ok": False,
                "backend": "postgres",
                "redis_connected": False,
                "queue_depth": 0,
                "counts": {"queued": 0, "running": 0, "done": 0, "error": 0},
                "active_workers": [],
            }
        try:
            counts = {"queued": 0, "running": 0, "done": 0, "error": 0}
            cur = con.cursor()
            cur.execute("SELECT status, COUNT(*) AS c FROM ai_command_jobs GROUP BY status")
            for st, c in cur.fetchall() or []:
                ss = str(st or "").strip().lower()
                if ss in counts:
                    counts[ss] = int(c or 0)
            recent_error_count = 0
            try:
                since = (now - dt.timedelta(seconds=recent_window_sec)).isoformat()
                cur.execute("SELECT COUNT(*) FROM ai_command_jobs WHERE status='error' AND updated_at >= %s", (since,))
                recent_error_count = int((cur.fetchone() or [0])[0] or 0)
            except Exception:
                recent_error_count = 0
            qd = int(counts.get("queued") or 0)
            cur.execute(
                """SELECT worker_id, MAX(heartbeat_at) AS hb
                   FROM ai_command_jobs
                   WHERE COALESCE(worker_id,'') <> ''
                   GROUP BY worker_id
                   ORDER BY hb DESC
                   LIMIT 50"""
            )
            wrs = [{"worker_id": str(r[0] or ""), "hb": str(r[1] or "")} for r in (cur.fetchall() or [])]
            try:
                cur.execute(
                    """SELECT worker_id, heartbeat_at AS hb
                       FROM ai_worker_heartbeats
                       WHERE COALESCE(worker_id,'') <> ''
                       ORDER BY heartbeat_at DESC
                       LIMIT 100"""
                )
                by_id = {str(r["worker_id"] or "").strip(): str(r["hb"] or "").strip() for r in wrs}
                for r in cur.fetchall() or []:
                    wid = str(r[0] or "").strip()
                    hb = str(r[1] or "").strip()
                    if wid and hb and (wid not in by_id or hb > by_id[wid]):
                        by_id[wid] = hb
                wrs = [{"worker_id": k, "hb": v} for k, v in by_id.items()]
            except Exception:
                pass
            active_workers: list[dict[str, str]] = []
            for r in wrs:
                wid = str(r.get("worker_id") or "").strip()
                hb = str(r.get("hb") or "").strip()
                if not wid or not hb:
                    continue
                try:
                    hb_dt = dt.datetime.fromisoformat(hb.replace("Z", ""))
                    age = (now - hb_dt).total_seconds()
                    if age <= float(max(10, int(active_within_sec or 120))):
                        active_workers.append({"worker_id": wid, "heartbeat_at": hb, "age_sec": f"{age:.1f}"})
                except Exception:
                    continue
            return {
                "ok": True,
                "backend": "postgres",
                "redis_connected": False,
                "queue_depth": qd,
                "counts": counts,
                "recent_error_count": int(recent_error_count),
                "active_workers": sorted(active_workers, key=lambda x: float(x.get("age_sec") or 0.0)),
                "asof": now.isoformat(),
            }
        finally:
            con.close()

    ensure_ai_job_queue_schema()
    con = _conn()
    try:
        counts = {"queued": 0, "running": 0, "done": 0, "error": 0}
        rows = con.execute(
            """SELECT status, COUNT(*) AS c
               FROM ai_command_jobs
               GROUP BY status"""
        ).fetchall()
        for r in rows:
            st = str(r["status"] or "").strip().lower()
            if st in counts:
                counts[st] = int(r["c"] or 0)
        recent_error_count = 0
        try:
            since = (now - dt.timedelta(seconds=recent_window_sec)).isoformat()
            rr = con.execute(
                "SELECT COUNT(*) AS c FROM ai_command_jobs WHERE status='error' AND updated_at >= ?",
                (since,),
            ).fetchone()
            recent_error_count = int((rr["c"] if rr else 0) or 0)
        except Exception:
            recent_error_count = 0
        qd = int(counts.get("queued") or 0)
        wrs = con.execute(
            """SELECT worker_id, MAX(heartbeat_at) AS hb
               FROM ai_command_jobs
               WHERE COALESCE(worker_id,'') <> ''
               GROUP BY worker_id
               ORDER BY hb DESC
               LIMIT 50"""
        ).fetchall()
        try:
            wr2 = con.execute(
                """SELECT worker_id, heartbeat_at AS hb
                   FROM ai_worker_heartbeats
                   WHERE COALESCE(worker_id,'') <> ''
                   ORDER BY heartbeat_at DESC
                   LIMIT 100"""
            ).fetchall()
            by_id = {str(r["worker_id"] or "").strip(): str(r["hb"] or "").strip() for r in wrs}
            for r in wr2:
                wid = str(r["worker_id"] or "").strip()
                hb = str(r["hb"] or "").strip()
                if wid and hb and (wid not in by_id or hb > by_id[wid]):
                    by_id[wid] = hb
            wrs = [{"worker_id": k, "hb": v} for k, v in by_id.items()]
        except Exception:
            pass
        active_workers: list[dict[str, str]] = []
        for r in wrs:
            wid = str(r["worker_id"] or "").strip()
            hb = str(r["hb"] or "").strip()
            if not wid or not hb:
                continue
            try:
                hb_dt = dt.datetime.fromisoformat(hb.replace("Z", ""))
                age = (now - hb_dt).total_seconds()
                if age <= float(max(10, int(active_within_sec or 120))):
                    active_workers.append({"worker_id": wid, "heartbeat_at": hb, "age_sec": f"{age:.1f}"})
            except Exception:
                continue
        return {
            "ok": True,
            "backend": "sqlite",
            "redis_connected": False,
            "queue_depth": qd,
            "counts": counts,
            "recent_error_count": int(recent_error_count),
            "active_workers": sorted(active_workers, key=lambda x: float(x.get("age_sec") or 0.0)),
            "asof": now.isoformat(),
        }
    finally:
        con.close()
