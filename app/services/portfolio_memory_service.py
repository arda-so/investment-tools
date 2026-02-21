from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any

from app.core.config import CORE_DB_PATH, ROOT
from app.core.sqlite_hardening import connect_sqlite, sqlite_retry
from app.services.postgres_core_service import (
    core_backend,
    insert_portfolio_transaction_pg,
    list_portfolio_transactions_pg,
    list_recent_portfolio_transactions_pg,
    pg_enabled,
    query_report_facts_pg,
    strict_postgres_mode,
    summarize_investor_style_memory_pg,
    upsert_investor_style_memory_pg,
    upsert_watchlist_thesis_pg,
)
from app.services.app_knowledge_service import retrieve_app_knowledge
from app.services.reports_service import list_reports, read_report_file

_RUNTIME_CACHE_LOCK = threading.Lock()
_RUNTIME_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _conn() -> sqlite3.Connection:
    return connect_sqlite(str(CORE_DB_PATH), row_factory=True)


def _to_float(v: object, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _normalize_import_timestamp(raw: str) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    # Accept common broker export formats and normalize to ISO-8601.
    candidates = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d %H:%M:%S",
        "%m/%d/%Y",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%d/%m/%Y",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y %H:%M:%S",
    ]
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "")).isoformat()
    except Exception:
        pass
    for fmt in candidates:
        try:
            return dt.datetime.strptime(s, fmt).isoformat()
        except Exception:
            continue
    return ""


def _has_column(con: sqlite3.Connection, table: str, col: str) -> bool:
    try:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
        return any(str(r[1] if isinstance(r, tuple) else r["name"]).strip() == col for r in rows)
    except Exception:
        return False


def _safe_ticker(raw: str) -> str:
    s = str(raw or "").strip().upper()
    s = re.sub(r"[^A-Z0-9.\-]", "", s)
    if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,11}", s):
        return s
    return ""


def _latest_report_path(prefixes: tuple[str, ...]) -> Path | None:
    rep = ROOT / "reports"
    if not rep.exists():
        return None
    cands: list[Path] = []
    for p in rep.iterdir():
        if not p.is_file():
            continue
        nm = p.name.lower()
        if any(nm.startswith(str(px).lower()) for px in prefixes):
            cands.append(p)
    if not cands:
        return None
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0]


def _read_text_file(path: Path | None) -> str:
    if not path or not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _is_stale(path: Path | None, max_age_hours: int = 36) -> bool:
    if not path or not path.exists():
        return True
    try:
        age_h = (dt.datetime.now().timestamp() - float(path.stat().st_mtime)) / 3600.0
        return age_h > float(max_age_hours)
    except Exception:
        return True


def _extract_earnings_week_rows(txt: str, today: dt.date, row_limit: int = 18) -> list[dict[str, str]]:
    if not txt:
        return []
    lines = txt.splitlines()
    in_sec = False
    current_day: dt.date | None = None
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    week_end = today + dt.timedelta(days=6)
    for raw in lines:
        s = str(raw or "").strip()
        if not s:
            continue
        if not in_sec:
            if re.match(r"^##\s+EARNINGS CALENDAR", s, flags=re.I):
                in_sec = True
            continue
        if s.startswith("## ") and not re.match(r"^##\s+EARNINGS CALENDAR", s, flags=re.I):
            break
        mday = re.match(r"^###\s+(\d{4}-\d{2}-\d{2})\b", s)
        if mday:
            try:
                current_day = dt.datetime.strptime(mday.group(1), "%Y-%m-%d").date()
            except Exception:
                current_day = None
            continue
        if s.startswith("|") and "Date" in s and "Symbol" in s:
            continue
        if s.startswith("|") and re.match(r"^\|\s*-+\s*\|", s):
            continue
        if not s.startswith("|"):
            continue
        parts = [p.strip() for p in s.strip("|").split("|")]
        if len(parts) < 3:
            continue
        if re.match(r"^\d{4}-\d{2}-\d{2}$", parts[0]):
            date_s = parts[0]
            sym = parts[1] if len(parts) > 1 else ""
            comp = parts[2] if len(parts) > 2 else ""
            tm = parts[3] if len(parts) > 3 else ""
        elif current_day is not None:
            date_s = current_day.isoformat()
            sym = parts[0]
            comp = parts[1] if len(parts) > 1 else ""
            tm = parts[2] if len(parts) > 2 else ""
        else:
            continue
        sym = _safe_ticker(sym)
        if not sym:
            continue
        try:
            dd = dt.datetime.strptime(date_s, "%Y-%m-%d").date()
        except Exception:
            continue
        if dd < today or dd > week_end:
            continue
        key = (date_s, sym)
        if key in seen:
            continue
        seen.add(key)
        out.append({"date": date_s, "symbol": sym, "company": comp or "-", "time": tm or "-"})
        if len(out) >= max(1, int(row_limit)):
            break
    return out


def ensure_portfolio_memory_schema() -> None:
    con = _conn()
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS portfolio_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL,
                shares REAL NOT NULL DEFAULT 0,
                price REAL NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'app',
                meta_json TEXT NOT NULL DEFAULT '{}'
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_portfolio_tx_ticker ON portfolio_transactions(ticker)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_portfolio_tx_created ON portfolio_transactions(created_at DESC)")
        con.execute(
            """CREATE TABLE IF NOT EXISTS decision_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                ticker TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'app',
                trace_id TEXT NOT NULL DEFAULT ''
            )"""
        )
        if not _has_column(con, "decision_log", "timestamp"):
            con.execute("ALTER TABLE decision_log ADD COLUMN timestamp TEXT NOT NULL DEFAULT ''")
        if not _has_column(con, "decision_log", "reasoning"):
            con.execute("ALTER TABLE decision_log ADD COLUMN reasoning TEXT NOT NULL DEFAULT ''")
        if not _has_column(con, "decision_log", "quantity"):
            con.execute("ALTER TABLE decision_log ADD COLUMN quantity REAL NOT NULL DEFAULT 0")
        if not _has_column(con, "decision_log", "price"):
            con.execute("ALTER TABLE decision_log ADD COLUMN price REAL NOT NULL DEFAULT 0")
        con.execute("CREATE INDEX IF NOT EXISTS idx_decision_ticker ON decision_log(ticker)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_decision_created ON decision_log(created_at DESC)")
        con.execute(
            """CREATE TABLE IF NOT EXISTS watchlist_thesis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL UNIQUE,
                thesis TEXT NOT NULL DEFAULT '',
                pick_method TEXT NOT NULL DEFAULT '',
                triggers TEXT NOT NULL DEFAULT '',
                invalidation TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        if not _has_column(con, "watchlist_thesis", "thesis_summary"):
            con.execute("ALTER TABLE watchlist_thesis ADD COLUMN thesis_summary TEXT NOT NULL DEFAULT ''")
        if not _has_column(con, "watchlist_thesis", "conviction_rating"):
            con.execute("ALTER TABLE watchlist_thesis ADD COLUMN conviction_rating INTEGER NOT NULL DEFAULT 0")
        if not _has_column(con, "watchlist_thesis", "time_horizon"):
            con.execute("ALTER TABLE watchlist_thesis ADD COLUMN time_horizon TEXT NOT NULL DEFAULT ''")
        if not _has_column(con, "watchlist_thesis", "invalidation_criteria"):
            con.execute("ALTER TABLE watchlist_thesis ADD COLUMN invalidation_criteria TEXT NOT NULL DEFAULT ''")
        if not _has_column(con, "watchlist_thesis", "strategy_tag"):
            con.execute("ALTER TABLE watchlist_thesis ADD COLUMN strategy_tag TEXT NOT NULL DEFAULT 'CORE'")
        if not _has_column(con, "watchlist_thesis", "pattern_learnable"):
            con.execute("ALTER TABLE watchlist_thesis ADD COLUMN pattern_learnable INTEGER NOT NULL DEFAULT 1")
        con.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_thesis_ticker ON watchlist_thesis(ticker)")
        con.execute(
            """CREATE TABLE IF NOT EXISTS portfolio_interview_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',
                step INTEGER NOT NULL DEFAULT 0,
                last_question TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                completed_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        if not _has_column(con, "portfolio_interview_queue", "status"):
            con.execute("ALTER TABLE portfolio_interview_queue ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
        if not _has_column(con, "portfolio_interview_queue", "step"):
            con.execute("ALTER TABLE portfolio_interview_queue ADD COLUMN step INTEGER NOT NULL DEFAULT 0")
        if not _has_column(con, "portfolio_interview_queue", "last_question"):
            con.execute("ALTER TABLE portfolio_interview_queue ADD COLUMN last_question TEXT NOT NULL DEFAULT ''")
        if not _has_column(con, "portfolio_interview_queue", "session_id"):
            con.execute("ALTER TABLE portfolio_interview_queue ADD COLUMN session_id TEXT NOT NULL DEFAULT ''")
        if not _has_column(con, "portfolio_interview_queue", "completed_at"):
            con.execute("ALTER TABLE portfolio_interview_queue ADD COLUMN completed_at TEXT NOT NULL DEFAULT ''")
        if not _has_column(con, "portfolio_interview_queue", "created_at"):
            con.execute("ALTER TABLE portfolio_interview_queue ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
        if not _has_column(con, "portfolio_interview_queue", "updated_at"):
            con.execute("ALTER TABLE portfolio_interview_queue ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
        con.execute("CREATE INDEX IF NOT EXISTS idx_portfolio_interview_status ON portfolio_interview_queue(status)")
        con.execute(
            """CREATE TABLE IF NOT EXISTS investor_style_memory (
                key TEXT PRIMARY KEY,
                answer TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_investor_style_updated ON investor_style_memory(updated_at DESC)")
        con.execute(
            """CREATE TABLE IF NOT EXISTS investor_question_overrides (
                key TEXT PRIMARY KEY,
                question TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_investor_q_overrides_updated ON investor_question_overrides(updated_at DESC)")
        con.execute(
            """CREATE TABLE IF NOT EXISTS morning_briefs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brief_day TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'scheduler',
                brief_json TEXT NOT NULL DEFAULT '{}'
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_morning_briefs_day ON morning_briefs(brief_day DESC)")
        con.execute(
            """CREATE TABLE IF NOT EXISTS report_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_name TEXT NOT NULL,
                report_kind TEXT NOT NULL DEFAULT '',
                report_modified TEXT NOT NULL DEFAULT '',
                fact_date TEXT NOT NULL DEFAULT '',
                ticker TEXT NOT NULL DEFAULT '',
                fact_text TEXT NOT NULL DEFAULT '',
                importance INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'report_ingest',
                fact_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_report_facts_date ON report_facts(fact_date DESC)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_report_facts_ticker ON report_facts(ticker)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_report_facts_importance ON report_facts(importance DESC)")
        con.execute(
            """CREATE TABLE IF NOT EXISTS report_fact_ingest_state (
                state_key TEXT PRIMARY KEY,
                state_value TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS memory_compact (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_key TEXT NOT NULL UNIQUE,
                bucket TEXT NOT NULL DEFAULT 'preference',
                value TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'chat_turn',
                reliability REAL NOT NULL DEFAULT 0.7,
                reuse_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                conflict_of TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL DEFAULT ''
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_memory_compact_bucket ON memory_compact(bucket)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_memory_compact_status ON memory_compact(status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_memory_compact_updated ON memory_compact(updated_at DESC)")
        con.execute(
            """CREATE TABLE IF NOT EXISTS learning_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'chat',
                payload_json TEXT NOT NULL DEFAULT '{}'
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_learning_events_created ON learning_events(created_at DESC)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_learning_events_type ON learning_events(event_type)")
        con.execute(
            """CREATE TABLE IF NOT EXISTS rule_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_key TEXT NOT NULL UNIQUE,
                rule_text TEXT NOT NULL DEFAULT '',
                source_pattern TEXT NOT NULL DEFAULT '',
                support_count INTEGER NOT NULL DEFAULT 0,
                accept_count INTEGER NOT NULL DEFAULT 0,
                reject_count INTEGER NOT NULL DEFAULT 0,
                confidence REAL NOT NULL DEFAULT 0.0,
                status TEXT NOT NULL DEFAULT 'candidate',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_evaluated_at TEXT NOT NULL DEFAULT ''
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_rule_candidates_status ON rule_candidates(status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_rule_candidates_conf ON rule_candidates(confidence DESC)")
        con.execute(
            """CREATE TABLE IF NOT EXISTS active_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_key TEXT NOT NULL UNIQUE,
                rule_text TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0.0,
                source_candidate_id INTEGER NOT NULL DEFAULT 0,
                reuse_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL DEFAULT ''
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_active_rules_conf ON active_rules(confidence DESC)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_active_rules_status ON active_rules(status)")
        con.commit()
    finally:
        con.close()


def upsert_investor_style_answer(key: str, answer: str) -> bool:
    ensure_portfolio_memory_schema()
    k = str(key or "").strip().lower()[:80]
    v = str(answer or "").strip()[:2000]
    if not k or not v:
        return False
    now = dt.datetime.now().isoformat()

    def _write() -> bool:
        con = _conn()
        try:
            row = con.execute("SELECT key FROM investor_style_memory WHERE key=? LIMIT 1", (k,)).fetchone()
            if row:
                con.execute("UPDATE investor_style_memory SET answer=?, updated_at=? WHERE key=?", (v, now, k))
            else:
                con.execute(
                    "INSERT INTO investor_style_memory (key, answer, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (k, v, now, now),
                )
            con.commit()
            return True
        finally:
            con.close()

    ok = bool(sqlite_retry(_write))
    if ok and pg_enabled():
        try:
            upsert_investor_style_memory_pg(k, v)
        except Exception:
            pass
    return ok


def summarize_investor_style_memory(limit: int = 24) -> str:
    if pg_enabled():
        try:
            txt = summarize_investor_style_memory_pg(limit=limit)
            if txt:
                return txt
        except Exception:
            if strict_postgres_mode():
                return ""
        if strict_postgres_mode():
            return ""
    ensure_portfolio_memory_schema()
    con = _conn()
    try:
        rows = con.execute(
            """SELECT key, answer
               FROM investor_style_memory
               WHERE COALESCE(answer, '') <> ''
               ORDER BY updated_at DESC
               LIMIT ?""",
            (max(1, min(200, int(limit or 24))),),
        ).fetchall()
        out: list[str] = []
        for r in rows:
            k = str(r["key"] or "").strip()
            a = str(r["answer"] or "").strip()
            if not k or not a:
                continue
            out.append(f"- {k}: {a[:180]}")
        return "\n".join(out)
    finally:
        con.close()


def _memory_key_from_text(bucket: str, text: str) -> str:
    base = f"{str(bucket or '').strip().lower()}::{str(text or '').strip().lower()}"
    h = hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()
    return h[:40]


def upsert_compact_memory(
    bucket: str,
    value: str,
    *,
    source: str = "chat_turn",
    reliability: float = 0.7,
    memory_key: str = "",
    status: str = "active",
    conflict_of: str = "",
) -> dict[str, Any]:
    ensure_portfolio_memory_schema()
    b = str(bucket or "preference").strip().lower()[:32] or "preference"
    v = str(value or "").strip()[:2000]
    if not v:
        return {"ok": False, "error": "value_required"}
    k = str(memory_key or "").strip().lower()[:64] or _memory_key_from_text(b, v)
    now = dt.datetime.now().isoformat(timespec="seconds")
    rel = max(0.0, min(1.0, float(reliability or 0.0)))
    st = str(status or "active").strip().lower()
    if st not in {"active", "archived", "pending_confirmation"}:
        st = "active"
    cf_of = str(conflict_of or "").strip()[:64]

    def _write() -> dict[str, Any]:
        con = _conn()
        try:
            row = con.execute("SELECT id, reuse_count FROM memory_compact WHERE memory_key = ? LIMIT 1", (k,)).fetchone()
            if row:
                rid = int(row["id"] or 0)
                reuse = int(row["reuse_count"] or 0)
                con.execute(
                    """UPDATE memory_compact
                       SET bucket=?, value=?, source=?, reliability=?, status=?, conflict_of=?,
                           reuse_count=?, updated_at=?
                       WHERE id=?""",
                    (b, v, str(source or "chat_turn")[:64], rel, st, cf_of, reuse + 1, now, rid),
                )
                con.commit()
                return {"ok": True, "id": rid, "memory_key": k, "updated": True}
            cur = con.execute(
                """INSERT INTO memory_compact
                   (memory_key, bucket, value, source, reliability, reuse_count, status, conflict_of, created_at, updated_at, last_used_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)""",
                (k, b, v, str(source or "chat_turn")[:64], rel, st, cf_of, now, now, now),
            )
            con.commit()
            return {"ok": True, "id": int(cur.lastrowid or 0), "memory_key": k, "updated": False}
        finally:
            con.close()

    return dict(sqlite_retry(_write))


def remember_compact_memory(text: str, bucket: str = "preference", source: str = "chat_turn", reliability: float = 0.85) -> dict[str, Any]:
    t = str(text or "").strip()
    if not t:
        return {"ok": False, "error": "text_required"}
    return upsert_compact_memory(bucket=bucket, value=t, source=source, reliability=reliability)


def forget_compact_memory(query: str, limit: int = 10) -> dict[str, Any]:
    ensure_portfolio_memory_schema()
    q = str(query or "").strip()
    if not q:
        return {"ok": False, "archived": 0, "error": "query_required"}
    like = "%" + q[:120].lower() + "%"
    now = dt.datetime.now().isoformat(timespec="seconds")

    def _write() -> dict[str, Any]:
        con = _conn()
        try:
            rows = con.execute(
                """SELECT id
                   FROM memory_compact
                   WHERE status='active'
                     AND (LOWER(value) LIKE ? OR LOWER(memory_key) LIKE ? OR LOWER(bucket) LIKE ?)
                   ORDER BY updated_at DESC
                   LIMIT ?""",
                (like, like, like, max(1, min(200, int(limit or 10)))),
            ).fetchall()
            ids = [int(r["id"] or 0) for r in rows if int(r["id"] or 0) > 0]
            for rid in ids:
                con.execute("UPDATE memory_compact SET status='archived', updated_at=? WHERE id=?", (now, rid))
            con.commit()
            return {"ok": True, "archived": len(ids)}
        finally:
            con.close()

    return dict(sqlite_retry(_write))


def _memory_freshness_weight(updated_at: str) -> float:
    try:
        ts = dt.datetime.fromisoformat(str(updated_at or "").replace("Z", ""))
        age_days = max(0.0, (dt.datetime.now() - ts).total_seconds() / 86400.0)
        if age_days <= 7:
            return 1.0
        if age_days <= 30:
            return 0.85
        if age_days <= 90:
            return 0.65
        return 0.45
    except Exception:
        return 0.5


def list_compact_memories(query: str = "", bucket: str = "", limit: int = 30, include_archived: bool = False) -> list[dict[str, Any]]:
    ensure_portfolio_memory_schema()
    q = str(query or "").strip().lower()
    b = str(bucket or "").strip().lower()
    lim = max(1, min(300, int(limit or 30)))
    con = _conn()
    try:
        clauses = ["1=1"]
        vals: list[Any] = []
        if not include_archived:
            clauses.append("status='active'")
        if b:
            clauses.append("bucket=?")
            vals.append(b[:32])
        if q:
            like = "%" + q[:120] + "%"
            clauses.append("(LOWER(value) LIKE ? OR LOWER(memory_key) LIKE ?)")
            vals.extend([like, like])
        sql = (
            "SELECT id, memory_key, bucket, value, source, reliability, reuse_count, status, conflict_of, created_at, updated_at, last_used_at "
            "FROM memory_compact WHERE " + " AND ".join(clauses) + " ORDER BY updated_at DESC LIMIT ?"
        )
        vals.append(lim)
        rows = con.execute(sql, tuple(vals)).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            rel = float(r["reliability"] or 0.0)
            reuse = int(r["reuse_count"] or 0)
            score = rel * _memory_freshness_weight(str(r["updated_at"] or "")) * (1.0 + min(2.0, reuse / 4.0))
            out.append(
                {
                    "id": int(r["id"] or 0),
                    "memory_key": str(r["memory_key"] or ""),
                    "bucket": str(r["bucket"] or ""),
                    "value": str(r["value"] or ""),
                    "source": str(r["source"] or ""),
                    "reliability": rel,
                    "reuse_count": reuse,
                    "status": str(r["status"] or ""),
                    "conflict_of": str(r["conflict_of"] or ""),
                    "created_at": str(r["created_at"] or ""),
                    "updated_at": str(r["updated_at"] or ""),
                    "last_used_at": str(r["last_used_at"] or ""),
                    "score": float(score),
                }
            )
        out.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
        return out
    finally:
        con.close()


def summarize_compact_memory_for_prompt(query: str = "", limit: int = 16) -> str:
    rows = list_compact_memories(query=query, bucket="", limit=max(1, min(80, int(limit or 16))) * 2, include_archived=False)
    out: list[str] = []
    for r in rows[: max(1, min(80, int(limit or 16)))]:
        out.append(
            f"- [{str(r.get('bucket') or 'memory')}] {str(r.get('value') or '')[:180]} "
            f"(score={float(r.get('score') or 0.0):.2f})"
        )
    return "\n".join(out)


def compact_compact_memory(max_keep_active: int = 600, stale_days: int = 120) -> dict[str, int]:
    ensure_portfolio_memory_schema()
    now = dt.datetime.now()
    rows = list_compact_memories(query="", bucket="", limit=5000, include_archived=False)
    archived = 0
    # 1) Archive stale low-value memories.
    stale_cut = now - dt.timedelta(days=max(30, int(stale_days or 120)))
    stale_ids: list[int] = []
    for r in rows:
        try:
            upd = dt.datetime.fromisoformat(str(r.get("updated_at") or "").replace("Z", ""))
        except Exception:
            upd = now
        if upd < stale_cut and float(r.get("reliability") or 0.0) < 0.75 and int(r.get("reuse_count") or 0) <= 1:
            rid = int(r.get("id") or 0)
            if rid > 0:
                stale_ids.append(rid)
    # 2) Keep top-N by score, archive the tail.
    ranked = sorted(rows, key=lambda x: float(x.get("score") or 0.0), reverse=True)
    keep = max(100, int(max_keep_active or 600))
    tail_ids = [int(r.get("id") or 0) for r in ranked[keep:] if int(r.get("id") or 0) > 0]
    target_ids = sorted(set(stale_ids + tail_ids))
    if not target_ids:
        return {"archived": 0, "active": len(rows)}

    ts = now.isoformat(timespec="seconds")

    def _write() -> int:
        con = _conn()
        try:
            cnt = 0
            for rid in target_ids:
                cur = con.execute(
                    "UPDATE memory_compact SET status='archived', updated_at=? WHERE id=? AND status='active'",
                    (ts, rid),
                )
                cnt += int(cur.rowcount or 0)
            con.commit()
            return cnt
        finally:
            con.close()

    archived = int(sqlite_retry(_write))
    active_after = max(0, len(rows) - archived)
    return {"archived": archived, "active": active_after}


def record_learning_event(event_type: str, payload: dict[str, Any] | None = None, source: str = "chat") -> bool:
    ensure_portfolio_memory_schema()
    et = str(event_type or "").strip().lower()[:64]
    if not et:
        return False
    now = dt.datetime.now().isoformat(timespec="seconds")
    pj = json.dumps(payload or {}, ensure_ascii=True)

    def _write() -> bool:
        con = _conn()
        try:
            con.execute(
                "INSERT INTO learning_events (created_at, event_type, source, payload_json) VALUES (?, ?, ?, ?)",
                (now, et, str(source or "chat")[:32], pj),
            )
            con.commit()
            return True
        finally:
            con.close()

    return bool(sqlite_retry(_write))


def _normalize_rule_text(text: str) -> str:
    s = re.sub(r"\s+", " ", str(text or "").strip())
    s = re.sub(r"^[\-\*\d\.\)\( ]+", "", s).strip()
    return s[:400]


def _rule_key(text: str, source_pattern: str) -> str:
    raw = f"{str(source_pattern or '').strip().lower()}::{_normalize_rule_text(text).lower()}"
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:40]


def _rule_confidence(support: int, accepts: int, rejects: int) -> float:
    s = max(0, int(support or 0))
    a = max(0, int(accepts or 0))
    rj = max(0, int(rejects or 0))
    support_score = min(1.0, float(s) / 8.0)
    # Slight optimistic prior when there are no explicit rejects yet.
    agree = float(a + 2) / float(a + rj + 2)
    penalty = min(0.5, float(rj) / 10.0)
    return max(0.0, min(1.0, (0.65 * support_score) + (0.45 * agree) - penalty))


def _upsert_rule_candidate(
    rule_text: str,
    source_pattern: str,
    delta_support: int = 1,
    delta_accept: int = 0,
    delta_reject: int = 0,
) -> dict[str, Any]:
    ensure_portfolio_memory_schema()
    rt = _normalize_rule_text(rule_text)
    sp = str(source_pattern or "").strip().lower()[:48] or "chat_pattern"
    if not rt:
        return {"ok": False, "error": "empty_rule_text"}
    rk = _rule_key(rt, sp)
    now = dt.datetime.now().isoformat(timespec="seconds")

    def _write() -> dict[str, Any]:
        con = _conn()
        try:
            row = con.execute(
                """SELECT id, support_count, accept_count, reject_count
                   FROM rule_candidates WHERE rule_key=? LIMIT 1""",
                (rk,),
            ).fetchone()
            if row:
                rid = int(row["id"] or 0)
                s = int(row["support_count"] or 0) + int(delta_support or 0)
                a = int(row["accept_count"] or 0) + int(delta_accept or 0)
                rj = int(row["reject_count"] or 0) + int(delta_reject or 0)
                conf = _rule_confidence(s, a, rj)
                st = "candidate"
                if conf >= 0.78 and s >= 4 and rj <= 1:
                    st = "ready"
                elif conf < 0.35 and rj >= 3:
                    st = "rejected"
                con.execute(
                    """UPDATE rule_candidates
                       SET rule_text=?, source_pattern=?, support_count=?, accept_count=?, reject_count=?,
                           confidence=?, status=?, updated_at=?, last_evaluated_at=?
                       WHERE id=?""",
                    (rt, sp, s, a, rj, conf, st, now, now, rid),
                )
                con.commit()
                return {"ok": True, "id": rid, "rule_key": rk, "confidence": conf, "status": st, "updated": True}
            s = max(0, int(delta_support or 0))
            a = max(0, int(delta_accept or 0))
            rj = max(0, int(delta_reject or 0))
            conf = _rule_confidence(s, a, rj)
            st = "candidate"
            if conf >= 0.78 and s >= 4 and rj <= 1:
                st = "ready"
            cur = con.execute(
                """INSERT INTO rule_candidates
                   (rule_key, rule_text, source_pattern, support_count, accept_count, reject_count, confidence, status, created_at, updated_at, last_evaluated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (rk, rt, sp, s, a, rj, conf, st, now, now, now),
            )
            con.commit()
            return {"ok": True, "id": int(cur.lastrowid or 0), "rule_key": rk, "confidence": conf, "status": st, "updated": False}
        finally:
            con.close()

    return dict(sqlite_retry(_write))


def list_rule_candidates(limit: int = 40) -> list[dict[str, Any]]:
    ensure_portfolio_memory_schema()
    con = _conn()
    try:
        rows = con.execute(
            """SELECT id, rule_key, rule_text, source_pattern, support_count, accept_count, reject_count, confidence, status, updated_at
               FROM rule_candidates
               ORDER BY confidence DESC, support_count DESC, updated_at DESC
               LIMIT ?""",
            (max(1, min(400, int(limit or 40))),),
        ).fetchall()
        return [
            {
                "id": int(r["id"] or 0),
                "rule_key": str(r["rule_key"] or ""),
                "rule_text": str(r["rule_text"] or ""),
                "source_pattern": str(r["source_pattern"] or ""),
                "support_count": int(r["support_count"] or 0),
                "accept_count": int(r["accept_count"] or 0),
                "reject_count": int(r["reject_count"] or 0),
                "confidence": float(r["confidence"] or 0.0),
                "status": str(r["status"] or ""),
                "updated_at": str(r["updated_at"] or ""),
            }
            for r in rows
        ]
    finally:
        con.close()


def promote_rule_candidate(rule_key: str) -> dict[str, Any]:
    ensure_portfolio_memory_schema()
    rk = str(rule_key or "").strip().lower()[:64]
    if not rk:
        return {"ok": False, "error": "rule_key_required"}
    now = dt.datetime.now().isoformat(timespec="seconds")

    def _write() -> dict[str, Any]:
        con = _conn()
        try:
            row = con.execute(
                "SELECT id, rule_text, confidence FROM rule_candidates WHERE rule_key=? LIMIT 1",
                (rk,),
            ).fetchone()
            if not row:
                return {"ok": False, "error": "rule_not_found"}
            cid = int(row["id"] or 0)
            txt = _normalize_rule_text(str(row["rule_text"] or ""))
            conf = float(row["confidence"] or 0.0)
            ex = con.execute("SELECT id, reuse_count FROM active_rules WHERE rule_key=? LIMIT 1", (rk,)).fetchone()
            if ex:
                rid = int(ex["id"] or 0)
                reuse = int(ex["reuse_count"] or 0)
                con.execute(
                    """UPDATE active_rules
                       SET rule_text=?, confidence=?, source_candidate_id=?, status='active', reuse_count=?, updated_at=?
                       WHERE id=?""",
                    (txt, conf, cid, reuse + 1, now, rid),
                )
            else:
                con.execute(
                    """INSERT INTO active_rules
                       (rule_key, rule_text, confidence, source_candidate_id, reuse_count, status, created_at, updated_at, last_used_at)
                       VALUES (?, ?, ?, ?, 1, 'active', ?, ?, ?)""",
                    (rk, txt, conf, cid, now, now, now),
                )
            con.execute(
                "UPDATE rule_candidates SET status='active', updated_at=?, last_evaluated_at=? WHERE id=?",
                (now, now, cid),
            )
            con.commit()
            return {"ok": True, "rule_key": rk, "confidence": conf}
        finally:
            con.close()

    return dict(sqlite_retry(_write))


def list_active_rules(limit: int = 30) -> list[dict[str, Any]]:
    ensure_portfolio_memory_schema()
    con = _conn()
    try:
        rows = con.execute(
            """SELECT id, rule_key, rule_text, confidence, source_candidate_id, reuse_count, status, updated_at
               FROM active_rules
               WHERE status='active'
               ORDER BY confidence DESC, reuse_count DESC, updated_at DESC
               LIMIT ?""",
            (max(1, min(200, int(limit or 30))),),
        ).fetchall()
        return [
            {
                "id": int(r["id"] or 0),
                "rule_key": str(r["rule_key"] or ""),
                "rule_text": str(r["rule_text"] or ""),
                "confidence": float(r["confidence"] or 0.0),
                "source_candidate_id": int(r["source_candidate_id"] or 0),
                "reuse_count": int(r["reuse_count"] or 0),
                "status": str(r["status"] or ""),
                "updated_at": str(r["updated_at"] or ""),
            }
            for r in rows
        ]
    finally:
        con.close()


def summarize_active_rules_for_prompt(query: str = "", limit: int = 10) -> str:
    q = str(query or "").strip().lower()
    rows = list_active_rules(limit=max(1, min(120, int(limit or 10) * 3)))
    scored: list[tuple[float, dict[str, Any]]] = []
    q_words = set(re.findall(r"[a-z0-9]{3,}", q))
    for r in rows:
        txt = str(r.get("rule_text") or "")
        base = float(r.get("confidence") or 0.0)
        if q_words:
            tw = set(re.findall(r"[a-z0-9]{3,}", txt.lower()))
            inter = len(q_words & tw)
            uni = max(1, len(q_words | tw))
            rel = float(inter) / float(uni)
            base += 0.4 * rel
        scored.append((base, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    out: list[str] = []
    for _s, r in scored[: max(1, min(60, int(limit or 10)))]:
        out.append(f"- {str(r.get('rule_text') or '')[:220]} (conf={float(r.get('confidence') or 0.0):.2f})")
    return "\n".join(out)


def learn_rules_from_memory_compact(limit_memories: int = 250) -> dict[str, int]:
    ensure_portfolio_memory_schema()
    rows = list_compact_memories(query="", bucket="", limit=max(20, min(2000, int(limit_memories or 250))), include_archived=False)
    created_or_updated = 0
    for r in rows:
        bucket = str(r.get("bucket") or "")
        txt = _normalize_rule_text(str(r.get("value") or ""))
        if not txt or len(txt) < 12:
            continue
        if bucket not in {"preference", "process_rule", "thesis", "evidence_fact"}:
            continue
        src = f"memory_{bucket}"
        res = _upsert_rule_candidate(txt, src, delta_support=1, delta_accept=0, delta_reject=0)
        if res.get("ok"):
            created_or_updated += 1
    return {"candidates_touched": int(created_or_updated)}


def learn_from_chat_turn(text: str, source: str = "chat") -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {"ok": False, "error": "empty_text"}
    record_learning_event("chat_turn", {"text": raw[:400]}, source=source)
    touched = learn_rules_from_memory_compact(limit_memories=200)
    low = raw.lower()
    if "good rule" in low or "use this rule" in low:
        _upsert_rule_candidate(raw, "explicit_accept", delta_support=1, delta_accept=1, delta_reject=0)
    if "bad rule" in low or "don't use this rule" in low or "do not use this rule" in low:
        _upsert_rule_candidate(raw, "explicit_reject", delta_support=1, delta_accept=0, delta_reject=1)
    return {"ok": True, **touched}


def run_learning_cycle() -> dict[str, Any]:
    ensure_portfolio_memory_schema()
    now = dt.datetime.now().isoformat(timespec="seconds")
    touched = learn_rules_from_memory_compact(limit_memories=500)
    candidates = list_rule_candidates(limit=400)
    promoted = 0
    for c in candidates:
        if str(c.get("status") or "") in {"ready", "active"} and float(c.get("confidence") or 0.0) >= 0.78 and int(c.get("support_count") or 0) >= 4:
            out = promote_rule_candidate(str(c.get("rule_key") or ""))
            if out.get("ok"):
                promoted += 1
    record_learning_event(
        "learning_cycle",
        {
            "at": now,
            "candidates_touched": int(touched.get("candidates_touched") or 0),
            "promoted": int(promoted),
            "active_rules": len(list_active_rules(limit=200)),
        },
        source="automation",
    )
    return {
        "ok": True,
        "at": now,
        "candidates_touched": int(touched.get("candidates_touched") or 0),
        "promoted": int(promoted),
        "active_rules": len(list_active_rules(limit=200)),
    }


def learn_compact_memory_from_text(text: str, source: str = "chat_turn") -> list[dict[str, Any]]:
    raw = str(text or "").strip()
    low = raw.lower()
    if not raw:
        return []
    saved: list[dict[str, Any]] = []
    # Explicit memory commands are highest quality.
    m = re.search(r"^\s*(?:remember this|remember)\s*[:\-]\s*(.+)$", raw, flags=re.I)
    if m:
        res = remember_compact_memory(m.group(1).strip(), bucket="process_rule", source=source, reliability=0.96)
        if res.get("ok"):
            saved.append(res)
        return saved

    # Compact preference extraction from stable phrasing.
    pref_patterns: list[tuple[str, str]] = [
        (r"\b(i (?:prefer|want))\s+(.+)$", "preference"),
        (r"\b(don't|do not|hate)\s+(.+)$", "preference"),
        (r"\b(always|never)\s+(.+)$", "process_rule"),
        (r"\bmy rule is\s*[:\-]?\s*(.+)$", "process_rule"),
    ]
    for pat, bucket in pref_patterns:
        mm = re.search(pat, raw, flags=re.I)
        if not mm:
            continue
        val = str(mm.group(0) or "").strip()
        if len(val) < 10:
            continue
        res = remember_compact_memory(val, bucket=bucket, source=source, reliability=0.82)
        if res.get("ok"):
            saved.append(res)
    return saved


def learn_investor_style_from_answer(text: str) -> list[dict[str, str]]:
    ensure_portfolio_memory_schema()
    raw = str(text or "").strip()
    low = raw.lower()
    if not raw:
        return []
    saved: list[dict[str, str]] = []

    m = re.search(r"\bpriority\s*=\s*([a-z0-9 _-]{2,40})", low)
    if m:
        v = str(m.group(1) or "").strip()
        if upsert_investor_style_answer("compare_priority", v):
            saved.append({"key": "compare_priority", "value": v})
    m = re.search(r"\bhorizon\s*=\s*([a-z0-9 _-]{2,40})", low)
    if m:
        v = str(m.group(1) or "").strip()
        if upsert_investor_style_answer("compare_horizon", v):
            saved.append({"key": "compare_horizon", "value": v})
    m = re.search(r"\brisk\s*=\s*([a-z0-9 _-]{2,40})", low)
    if m:
        v = str(m.group(1) or "").strip()
        if upsert_investor_style_answer("compare_risk", v):
            saved.append({"key": "compare_risk", "value": v})

    if "mistake" in low and len(raw) >= 16:
        if upsert_investor_style_answer("mistakes_log_latest", raw):
            saved.append({"key": "mistakes_log_latest", "value": raw[:120]})
    if pg_enabled() and saved:
        for it in saved:
            try:
                upsert_investor_style_memory_pg(str(it.get("key") or ""), str(it.get("value") or ""))
            except Exception:
                pass
    return saved


def _extract_report_fact_lines(text: str, limit: int = 16) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for ln in str(text or "").splitlines():
        s = str(ln or "").strip()
        if not s:
            continue
        if s.startswith("#"):
            s = s.lstrip("#").strip()
        elif s.startswith(("- ", "* ")):
            s = s[2:].strip()
        elif re.match(r"^\d+\.\s+", s):
            s = re.sub(r"^\d+\.\s+", "", s)
        else:
            if not re.search(r"\b(earnings|guidance|margin|revenue|cash flow|risk|invalidation|beat|miss|downgrade|upgrade|calendar|results?)\b", s, flags=re.I):
                continue
        if len(s) < 18:
            continue
        key = re.sub(r"\s+", " ", s.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(s[:320])
        if len(out) >= max(1, int(limit)):
            break
    return out


def _is_official_report_kind(kind: str) -> bool:
    k = str(kind or "").strip()
    return k in {
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


def _is_official_report_row(row: dict[str, Any]) -> bool:
    kind = str(row.get("kind") or "").strip()
    if _is_official_report_kind(kind):
        return True
    meta = f"{str(row.get('name') or '')} {str(row.get('title') or '')} {kind}".lower()
    # SEC filing forms or explicit sec tags in report naming.
    return bool(re.search(r"\b(10-k|10-q|8-k|20-f|40-f|sec|filing)\b", meta))


def ingest_recent_report_facts(limit_reports: int = 20, facts_per_report: int = 12) -> dict[str, int]:
    ensure_portfolio_memory_schema()
    rows = list_reports(limit=max(1, min(200, int(limit_reports or 20))))
    now = dt.datetime.now().isoformat(timespec="seconds")
    inserted = 0
    scanned = 0

    def _write() -> None:
        nonlocal inserted, scanned
        con = _conn()
        try:
            for r in rows:
                name = str(r.get("name") or "").strip()
                if not name:
                    continue
                if not _is_official_report_row(r):
                    continue
                scanned += 1
                txt, err = read_report_file(name, max_chars=120_000)
                if err or not txt:
                    continue
                kind = str(r.get("kind") or "").strip()
                modified = str(r.get("modified") or "").strip()
                date_tag = modified[:10] if modified else dt.date.today().isoformat()
                ticker_hits = [x.strip().upper() for x in str(r.get("ticker_hits") or "").split(",") if x.strip()]
                fact_lines = _extract_report_fact_lines(txt, limit=max(1, int(facts_per_report or 12)))
                for line in fact_lines:
                    tickers = ticker_hits or []
                    if not tickers:
                        m = re.findall(r"\b[A-Z]{1,6}\b", line.upper())
                        tickers = [_safe_ticker(x) for x in m if _safe_ticker(x)]
                    if not tickers:
                        tickers = [""]
                    imp = 40
                    if re.search(r"\b(risk|warning|downgrade|miss|liquidity|invalidation)\b", line, flags=re.I):
                        imp += 35
                    if re.search(r"\b(earnings|guidance|results?|calendar)\b", line, flags=re.I):
                        imp += 20
                    for tk in tickers[:3]:
                        hash_src = f"{name}|{tk}|{line}"
                        fh = hashlib.sha1(hash_src.encode("utf-8", errors="ignore")).hexdigest()
                        cur = con.execute(
                            """INSERT OR IGNORE INTO report_facts
                               (report_name, report_kind, report_modified, fact_date, ticker, fact_text, importance, source, fact_hash, created_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, 'report_ingest', ?, ?)""",
                            (name, kind, modified, date_tag, str(tk or ""), line, int(max(1, min(100, imp))), fh, now),
                        )
                        if int(cur.rowcount or 0) > 0:
                            inserted += 1
            con.execute(
                """INSERT INTO report_fact_ingest_state (state_key, state_value, updated_at)
                   VALUES ('last_ingest_at', ?, ?)
                   ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value, updated_at=excluded.updated_at""",
                (now, now),
            )
            con.commit()
        finally:
            con.close()

    sqlite_retry(_write)
    return {"inserted": int(inserted), "scanned": int(scanned)}


def query_report_facts(
    query: str = "",
    tickers: list[str] | None = None,
    limit: int = 12,
    official_only: bool = True,
) -> list[dict[str, Any]]:
    if pg_enabled():
        try:
            out_pg = query_report_facts_pg(query=query, tickers=tickers, limit=limit, official_only=official_only)
            if out_pg:
                return out_pg
        except Exception:
            if strict_postgres_mode():
                return []
        if strict_postgres_mode():
            return []
    ensure_portfolio_memory_schema()
    lim = max(1, min(100, int(limit or 12)))
    tks = [str(t or "").strip().upper() for t in (tickers or []) if str(t or "").strip()]
    con = _conn()
    try:
        q = str(query or "").strip()
        clauses = ["1=1"]
        vals: list[Any] = []
        if tks:
            clauses.append("ticker IN (" + ",".join("?" for _ in tks) + ")")
            vals.extend(tks)
        if q:
            like = "%" + q[:120] + "%"
            clauses.append("(fact_text LIKE ? OR report_name LIKE ? OR report_kind LIKE ?)")
            vals.extend([like, like, like])
        if bool(official_only):
            marks = ",".join("?" for _ in sorted({
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
            }))
            allowed_kinds = sorted({
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
            })
            clauses.append(
                "(report_kind IN (" + marks + ") OR report_name LIKE ? OR report_name LIKE ? OR report_name LIKE ? OR report_name LIKE ? OR report_name LIKE ? OR report_name LIKE ?)"
            )
            vals.extend(allowed_kinds)
            vals.extend(["%10-k%", "%10-q%", "%8-k%", "%20-f%", "%40-f%", "%sec%"])
        sql = (
            "SELECT report_name, report_kind, fact_date, ticker, fact_text, importance, created_at "
            "FROM report_facts WHERE " + " AND ".join(clauses) + " "
            "ORDER BY importance DESC, fact_date DESC, id DESC LIMIT ?"
        )
        vals.append(lim)
        rows = con.execute(sql, tuple(vals)).fetchall()
        return [
            {
                "report_name": str(r["report_name"] or ""),
                "report_kind": str(r["report_kind"] or ""),
                "fact_date": str(r["fact_date"] or ""),
                "ticker": str(r["ticker"] or "").strip().upper(),
                "fact_text": str(r["fact_text"] or ""),
                "importance": int(r["importance"] or 0),
                "created_at": str(r["created_at"] or ""),
            }
            for r in rows
        ]
    finally:
        con.close()


def _slice_10k_risk_section(text: str) -> str:
    txt = str(text or "")
    if not txt:
        return ""
    low = txt.lower()
    start = re.search(r"(item\s*1a[\.:]?\s*risk\s*factors?)", low, flags=re.I)
    if not start:
        # Fallback if report is normalized without item numbers.
        start = re.search(r"\brisk\s*factors?\b", low, flags=re.I)
    if not start:
        return ""
    sidx = int(start.start())
    tail = low[sidx + 20 :]
    end_rel = re.search(r"\b(item\s*1b|item\s*2|unresolved\s*staff\s*comments?)\b", tail, flags=re.I)
    eidx = (sidx + 20 + int(end_rel.start())) if end_rel else len(txt)
    sec = txt[sidx:eidx]
    return sec[:180_000]


def _risk_theme(line: str) -> str:
    l = str(line or "").lower()
    if re.search(r"\b(cyber|data breach|security)\b", l):
        return "Cyber/Data"
    if re.search(r"\b(regulat|compliance|legal|litigation|antitrust|law)\b", l):
        return "Regulatory/Legal"
    if re.search(r"\b(compet|pricing pressure|market share)\b", l):
        return "Competition"
    if re.search(r"\b(macro|recession|inflation|interest rate|currency|fx)\b", l):
        return "Macro/Financial"
    if re.search(r"\b(supply chain|vendor|supplier|operations?|execution)\b", l):
        return "Operations"
    if re.search(r"\b(customer concentration|churn|demand|retention|sales cycle)\b", l):
        return "Customer/Demand"
    return "General"


def _extract_10k_risk_lines(section_text: str, per_report_limit: int = 20) -> list[str]:
    txt = str(section_text or "").strip()
    if not txt:
        return []
    out: list[str] = []
    seen: set[str] = set()
    buf: list[str] = []
    for raw in txt.splitlines():
        s = str(raw or "").strip()
        if not s:
            if buf:
                para = " ".join(buf).strip()
                buf = []
                if len(para) >= 60:
                    key = re.sub(r"\s+", " ", para.lower())
                    if key not in seen:
                        seen.add(key)
                        out.append(para[:360])
            continue
        if s.startswith("#"):
            continue
        if re.match(r"^item\s*\d+[a-z]?\b", s, flags=re.I):
            continue
        if s.startswith(("- ", "* ")):
            s = s[2:].strip()
        elif re.match(r"^\d+\.\s+", s):
            s = re.sub(r"^\d+\.\s+", "", s)
        buf.append(s)
    if buf:
        para = " ".join(buf).strip()
        if len(para) >= 60:
            key = re.sub(r"\s+", " ", para.lower())
            if key not in seen:
                out.append(para[:360])
    return out[: max(1, int(per_report_limit or 20))]


def query_10k_risk_map(
    query: str = "",
    tickers: list[str] | None = None,
    limit_reports: int = 80,
    per_ticker_limit: int = 6,
) -> list[dict[str, Any]]:
    q = str(query or "").strip().lower()
    wanted = [_safe_ticker(t) for t in (tickers or []) if _safe_ticker(t)]
    wanted_set = set(wanted)
    reports = list_reports(limit=max(10, min(300, int(limit_reports or 80))))
    con = _conn()
    industry_map: dict[str, str] = {}
    try:
        rows = con.execute("SELECT ticker, industry FROM company_profile_cache").fetchall()
        for r in rows:
            tk = _safe_ticker(str(r["ticker"] or ""))
            if tk:
                industry_map[tk] = str(r["industry"] or "").strip() or "Unknown"
    except Exception:
        pass
    finally:
        con.close()

    per_ticker_count: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for rp in reports:
        if not _is_official_report_row(rp):
            continue
        kind = str(rp.get("kind") or "").strip()
        name = str(rp.get("name") or "").strip()
        title = str(rp.get("title") or "").strip()
        low_meta = f"{kind} {name} {title}".lower()
        if "10-k" not in low_meta and "annual" not in low_meta:
            continue
        txt, err = read_report_file(name, max_chars=200_000)
        if err or not txt:
            continue
        sec = _slice_10k_risk_section(txt)
        if not sec:
            continue
        lines = _extract_10k_risk_lines(sec, per_report_limit=24)
        if not lines:
            continue
        tks = [x.strip().upper() for x in str(rp.get("ticker_hits") or "").split(",") if x.strip()]
        if not tks:
            tks = [_safe_ticker(x) for x in re.findall(r"\b[A-Z]{1,5}\b", f"{name} {title}") if _safe_ticker(x)]
        tks = [t for t in tks if _safe_ticker(t)]
        if not tks:
            continue
        for tk in tks[:3]:
            if wanted_set and tk not in wanted_set:
                continue
            if q and tk and tk.lower() not in q and "all" not in q and "industry" not in q and "map" not in q:
                # keep broad query behavior unless user is specific.
                pass
            for line in lines:
                if per_ticker_count.get(tk, 0) >= max(1, int(per_ticker_limit or 6)):
                    break
                key = (tk, re.sub(r"\s+", " ", line.lower())[:260])
                if key in seen:
                    continue
                seen.add(key)
                per_ticker_count[tk] = per_ticker_count.get(tk, 0) + 1
                out.append(
                    {
                        "ticker": tk,
                        "industry": industry_map.get(tk, "Unknown"),
                        "theme": _risk_theme(line),
                        "risk_text": line,
                        "report_name": name,
                        "report_kind": kind,
                        "fact_date": str(rp.get("modified") or "")[:10],
                    }
                )
    out.sort(key=lambda r: (str(r.get("industry") or ""), str(r.get("ticker") or ""), str(r.get("theme") or "")))
    return out


def record_portfolio_transaction(
    ticker: str,
    action: str,
    shares: float = 0.0,
    price: float = 0.0,
    note: str = "",
    source: str = "app",
    meta: dict[str, Any] | None = None,
    timestamp: str = "",
) -> None:
    t = str(ticker or "").strip().upper()[:16]
    a = str(action or "").strip().lower()[:32]
    if not t or not a:
        return
    created = str(timestamp or "").strip() or dt.datetime.now().isoformat()
    if core_backend() == "postgres":
        _ = insert_portfolio_transaction_pg(
            created_at=created,
            ticker=t,
            action=a,
            shares=float(shares or 0.0),
            price=float(price or 0.0),
            note=str(note or "")[:2000],
            source=str(source or "app")[:64],
            meta_json=(meta or {}),
        )
        return
    def _write() -> None:
        con = _conn()
        try:
            con.execute(
                """INSERT INTO portfolio_transactions
                   (created_at, ticker, action, shares, price, note, source, meta_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    created,
                    t,
                    a,
                    float(shares or 0.0),
                    float(price or 0.0),
                    str(note or "")[:2000],
                    str(source or "app")[:64],
                    json.dumps(meta or {}, ensure_ascii=True),
                ),
            )
            con.commit()
        finally:
            con.close()
    sqlite_retry(_write)


def record_decision(
    action: str,
    reason: str = "",
    ticker: str = "",
    confidence: float = 0.0,
    source: str = "app",
    trace_id: str = "",
    quantity: float = 0.0,
    price: float = 0.0,
    timestamp: str = "",
    reasoning: str = "",
) -> None:
    def _write() -> None:
        con = _conn()
        try:
            now = dt.datetime.now().isoformat()
            ts = str(timestamp or "").strip() or now
            rs = str(reasoning or "").strip() or str(reason or "").strip()
            con.execute(
                """INSERT INTO decision_log
                   (created_at, timestamp, ticker, action, reason, reasoning, confidence, source, trace_id, quantity, price)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    now,
                    ts,
                    str(ticker or "").strip().upper()[:16],
                    str(action or "").strip().lower()[:64],
                    str(reason or "")[:2000],
                    rs[:2000],
                    float(confidence or 0.0),
                    str(source or "app")[:64],
                    str(trace_id or "")[:120],
                    float(quantity or 0.0),
                    float(price or 0.0),
                ),
            )
            con.commit()
        finally:
            con.close()
    sqlite_retry(_write)


def upsert_watchlist_thesis(
    ticker: str,
    thesis: str = "",
    pick_method: str = "",
    triggers: str = "",
    invalidation: str = "",
    status: str = "active",
    conviction_rating: int = 0,
    time_horizon: str = "",
    thesis_summary: str = "",
    invalidation_criteria: str = "",
    strategy_tag: str = "",
    pattern_learnable: int = -1,
) -> None:
    t = str(ticker or "").strip().upper()[:16]
    if not t:
        return
    now = dt.datetime.now().isoformat()
    con = _conn()
    try:
        cur = con.execute("SELECT ticker FROM watchlist_thesis WHERE ticker = ? LIMIT 1", (t,)).fetchone()
        if cur:
            con.execute(
                """UPDATE watchlist_thesis
                   SET thesis = COALESCE(NULLIF(?, ''), thesis),
                       thesis_summary = COALESCE(NULLIF(?, ''), thesis_summary),
                       pick_method = COALESCE(NULLIF(?, ''), pick_method),
                       triggers = COALESCE(NULLIF(?, ''), triggers),
                       invalidation = COALESCE(NULLIF(?, ''), invalidation),
                       conviction_rating = CASE WHEN ? > 0 THEN ? ELSE conviction_rating END,
                       time_horizon = COALESCE(NULLIF(?, ''), time_horizon),
                       invalidation_criteria = COALESCE(NULLIF(?, ''), invalidation_criteria),
                       strategy_tag = COALESCE(NULLIF(?, ''), strategy_tag),
                       pattern_learnable = CASE WHEN ? IN (0,1) THEN ? ELSE pattern_learnable END,
                       status = COALESCE(NULLIF(?, ''), status),
                       updated_at = ?
                   WHERE ticker = ?""",
                (
                    str(thesis or "")[:3000],
                    str((thesis_summary or thesis) or "")[:3000],
                    str(pick_method or "")[:500],
                    str(triggers or "")[:1000],
                    str(invalidation or "")[:1000],
                    int(conviction_rating or 0),
                    int(conviction_rating or 0),
                    str(time_horizon or "")[:120],
                    str((invalidation_criteria or invalidation) or "")[:1000],
                    str(strategy_tag or "")[:16].upper(),
                    int(pattern_learnable),
                    int(pattern_learnable),
                    str(status or "")[:32],
                    now,
                    t,
                ),
            )
        else:
            con.execute(
                """INSERT INTO watchlist_thesis
                   (ticker, thesis, thesis_summary, pick_method, triggers, invalidation, conviction_rating, time_horizon, invalidation_criteria, strategy_tag, pattern_learnable, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
    finally:
        con.close()
    if pg_enabled():
        try:
            upsert_watchlist_thesis_pg(
                ticker=t,
                thesis=str(thesis or "")[:3000],
                thesis_summary=str((thesis_summary or thesis) or "")[:3000],
                pick_method=str(pick_method or "")[:500],
                triggers=str(triggers or "")[:1000],
                invalidation=str(invalidation or "")[:1000],
                conviction_rating=int(conviction_rating or 0),
                time_horizon=str(time_horizon or "")[:120],
                invalidation_criteria=str((invalidation_criteria or invalidation) or "")[:1000],
                strategy_tag=str(strategy_tag or "CORE")[:16].upper(),
                pattern_learnable=(1 if int(pattern_learnable) != 0 else 0),
                status=str(status or "active")[:32],
            )
        except Exception:
            pass


def list_recent_portfolio_transactions(limit: int = 20, ticker: str = "") -> list[dict[str, Any]]:
    if pg_enabled():
        try:
            rows_pg = list_recent_portfolio_transactions_pg(limit=limit, ticker=ticker)
            if rows_pg:
                return rows_pg
        except Exception:
            if strict_postgres_mode():
                return []
        if strict_postgres_mode():
            return []
    lim = max(1, min(200, int(limit or 20)))
    t = str(ticker or "").strip().upper()[:16]
    con = _conn()
    try:
        if t:
            rows = con.execute(
                """SELECT created_at, ticker, action, shares, price, note, source
                   FROM portfolio_transactions
                   WHERE ticker = ?
                   ORDER BY id DESC LIMIT ?""",
                (t, lim),
            ).fetchall()
        else:
            rows = con.execute(
                """SELECT created_at, ticker, action, shares, price, note, source
                   FROM portfolio_transactions
                   ORDER BY id DESC LIMIT ?""",
                (lim,),
            ).fetchall()
        return [
            {
                "created_at": str(r["created_at"] or ""),
                "ticker": str(r["ticker"] or ""),
                "action": str(r["action"] or ""),
                "shares": float(r["shares"] or 0.0),
                "price": float(r["price"] or 0.0),
                "note": str(r["note"] or ""),
                "source": str(r["source"] or ""),
            }
            for r in rows
        ]
    finally:
        con.close()


def list_portfolio_transactions(
    limit: int = 100,
    ticker: str = "",
    year: int = 0,
) -> list[dict[str, Any]]:
    if pg_enabled():
        try:
            rows_pg = list_portfolio_transactions_pg(limit=limit, ticker=ticker, year=year)
            if rows_pg:
                return rows_pg
        except Exception:
            if strict_postgres_mode():
                return []
        if strict_postgres_mode():
            return []
    lim = max(1, min(2000, int(limit or 100)))
    t = str(ticker or "").strip().upper()[:16]
    y = int(year or 0)
    clauses: list[str] = []
    vals: list[Any] = []
    if t:
        clauses.append("ticker = ?")
        vals.append(t)
    if y >= 1900:
        # Handle mixed timestamp formats from broker imports.
        clauses.append("(CAST(substr(created_at, 1, 4) AS INTEGER) = ? OR created_at LIKE ?)")
        vals.append(y)
        vals.append(f"%{y:04d}%")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    con = _conn()
    try:
        rows = con.execute(
            f"""SELECT created_at, ticker, action, shares, price, note, source
                FROM portfolio_transactions
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?""",
            tuple(vals + [lim]),
        ).fetchall()
        return [
            {
                "created_at": str(r["created_at"] or ""),
                "ticker": str(r["ticker"] or ""),
                "action": str(r["action"] or ""),
                "shares": float(r["shares"] or 0.0),
                "price": float(r["price"] or 0.0),
                "note": str(r["note"] or ""),
                "source": str(r["source"] or ""),
            }
            for r in rows
        ]
    finally:
        con.close()


def get_position_history(ticker: str, limit: int = 40) -> list[dict[str, Any]]:
    t = str(ticker or "").strip().upper()[:16]
    if not t:
        return []
    return list_recent_portfolio_transactions(limit=limit, ticker=t)


def get_watchlist_rationale(ticker: str, limit: int = 6) -> dict[str, Any]:
    t = str(ticker or "").strip().upper()[:16]
    out: dict[str, Any] = {"ticker": t, "thesis": {}, "decisions": []}
    if not t:
        return out
    con = _conn()
    try:
        th = con.execute(
            """SELECT thesis, thesis_summary, conviction_rating, time_horizon, invalidation_criteria,
                      pick_method, triggers, invalidation, strategy_tag, pattern_learnable, status, updated_at
               FROM watchlist_thesis WHERE ticker = ? LIMIT 1""",
            (t,),
        ).fetchone()
        if th:
            out["thesis"] = {
                "thesis": str(th["thesis"] or ""),
                "thesis_summary": str(th["thesis_summary"] or ""),
                "conviction_rating": int(th["conviction_rating"] or 0),
                "time_horizon": str(th["time_horizon"] or ""),
                "invalidation_criteria": str(th["invalidation_criteria"] or ""),
                "pick_method": str(th["pick_method"] or ""),
                "triggers": str(th["triggers"] or ""),
                "invalidation": str(th["invalidation"] or ""),
                "strategy_tag": str(th["strategy_tag"] or "CORE"),
                "pattern_learnable": int(th["pattern_learnable"] or 1),
                "status": str(th["status"] or ""),
                "updated_at": str(th["updated_at"] or ""),
            }
        lim = max(1, min(50, int(limit or 6)))
        rows = con.execute(
            """SELECT created_at, action, reason, reasoning, confidence, source, quantity, price
               FROM decision_log
               WHERE ticker = ? AND action LIKE 'watchlist%'
               ORDER BY id DESC LIMIT ?""",
            (t, lim),
        ).fetchall()
        out["decisions"] = [
            {
                "created_at": str(r["created_at"] or ""),
                "action": str(r["action"] or ""),
                "reason": str(r["reason"] or ""),
                "reasoning": str(r["reasoning"] or ""),
                "confidence": float(r["confidence"] or 0.0),
                "source": str(r["source"] or ""),
                "quantity": float(r["quantity"] or 0.0),
                "price": float(r["price"] or 0.0),
            }
            for r in rows
        ]
        return out
    finally:
        con.close()


def get_holdings(limit: int = 500) -> list[dict[str, Any]]:
    try:
        from app.services.dashboard_service import home_snapshot  # local import
        home = home_snapshot()
    except Exception:
        home = {}
    rows = list((home or {}).get("portfolio", []) or [])
    out: list[dict[str, Any]] = []
    for r in rows[: max(1, min(2000, int(limit or 500)))]:
        out.append(
            {
                "ticker": str(r.get("ticker") or "").strip().upper(),
                "name": str(r.get("name") or "").strip(),
                "shares": _to_float(r.get("shares"), 0.0),
                "cost": _to_float(r.get("cost"), 0.0),
                "day_pct": r.get("day_pct"),
                "industry": str(r.get("industry") or "").strip(),
            }
        )
    return out


def save_thesis(
    ticker: str,
    thesis: str,
    conviction: int = 0,
    time_horizon: str = "",
    invalidation_criteria: str = "",
    strategy_tag: str = "CORE",
) -> dict[str, Any]:
    t = str(ticker or "").strip().upper()[:16]
    if not t:
        return {"ok": False, "error": "ticker_required"}
    upsert_watchlist_thesis(
        ticker=t,
        thesis=thesis,
        thesis_summary=thesis,
        conviction_rating=max(0, min(10, int(conviction or 0))),
        time_horizon=time_horizon,
        invalidation_criteria=invalidation_criteria,
        strategy_tag=str(strategy_tag or "CORE").upper(),
        pattern_learnable=0 if str(strategy_tag or "").upper() == "LEGACY" else 1,
        status="active",
    )
    record_decision(
        ticker=t,
        action="thesis_update",
        reason=thesis,
        reasoning=thesis,
        confidence=0.95 if conviction else 0.85,
        source="interview_mode",
    )
    return {"ok": True, "ticker": t}


PROFILE_QUEUE_PREFIX = "PROFILE::"
PROFILE_QUESTIONS: list[tuple[str, str]] = [
    (
        "investing_style",
        "How would you describe your investing style in plain words (quality, growth, value, and your real edge)?",
    ),
    (
        "time_horizon",
        "What is your normal holding period, and what makes you exit earlier than planned?",
    ),
    (
        "position_sizing",
        "How do you size positions: starter size, max size, and exact add/trim rules?",
    ),
    (
        "risk_limits",
        "What are your hard risk limits I should never let you break (drawdown, concentration, cash buffer)?",
    ),
    (
        "strengths",
        "Where are you strongest as an investor? Give me 2-3 strengths I should lean on.",
    ),
    (
        "weaknesses_blindspots",
        "Where do you usually slip? Tell me the blind spots I should catch early.",
    ),
    (
        "mistakes_top10",
        "List your 10 biggest investing mistakes (short bullets are fine: ticker/setup + why each was a mistake).",
    ),
    (
        "mistakes_why_happened",
        "For the biggest mistakes, what caused them, which signal you ignored, and which bias was in control?",
    ),
    (
        "mistakes_avoidability",
        "For each major mistake, was it avoidable, and what rule could have prevented it?",
    ),
    (
        "mistakes_lessons",
        "What rules did you add after those mistakes, and which mistake are you still most likely to repeat?",
    ),
    (
        "decision_checklist",
        "Before you buy or add, what must be true every single time?",
    ),
    (
        "sell_discipline",
        "What should trigger a trim or full sell for you (invalidation, valuation/size, and management red flags)?",
    ),
]


def _profile_queue_id(key: str) -> str:
    return PROFILE_QUEUE_PREFIX + str(key or "").strip().lower()


def _profile_question_by_step(step: int) -> tuple[str, str]:
    s = int(step or 0)
    if s < 0 or s >= len(PROFILE_QUESTIONS):
        s = 0
    return PROFILE_QUESTIONS[s]


def _profile_question_by_key(key: str) -> tuple[str, str]:
    k = str(key or "").strip().lower()
    for item_key, item_q in PROFILE_QUESTIONS:
        if item_key == k:
            return item_key, item_q
    return PROFILE_QUESTIONS[0]


def _is_single_question_text(question: str) -> bool:
    q = str(question or "").strip()
    if not q:
        return False
    low = q.lower()
    if low.count("investor profile") > 1:
        return False
    if q.count("?") > 2:
        return False
    headers = re.findall(r"^\s{0,3}#{1,6}\s+", q, flags=re.M)
    if len(headers) > 1:
        return False
    if re.search(r"\n\s*(?:-|\*|\d+\.)\s+", q):
        return False
    return True


def _sanitize_question_text(question: str) -> str:
    q = str(question or "").strip()
    if not q:
        return ""
    # Hard split if multiple profile blocks were accidentally merged.
    if "Investor Profile -" in q:
        parts = [p.strip() for p in re.split(r"\n\s*(?=Investor Profile -)", q) if p.strip()]
        if parts:
            q = parts[0]
    # Remove trailing second blocks after blank lines.
    if "\n\n" in q:
        q = q.split("\n\n", 1)[0].strip()
    # Prefer a single line question if present.
    q_lines = [ln.strip() for ln in q.splitlines() if ln.strip()]
    if len(q_lines) > 1:
        q = q_lines[0]
    return q


def _get_profile_question_override(con: sqlite3.Connection, key: str) -> str:
    k = str(key or "").strip().lower()
    if not k:
        return ""
    row = con.execute(
        "SELECT COALESCE(question, '') AS question FROM investor_question_overrides WHERE key = ? LIMIT 1",
        (k,),
    ).fetchone()
    if not row:
        return ""
    q = _sanitize_question_text(str(row["question"] or ""))
    return q if _is_single_question_text(q) else ""


def _set_profile_question_override(con: sqlite3.Connection, key: str, question: str) -> None:
    now = dt.datetime.now().isoformat()
    k = str(key or "").strip().lower()
    q = _sanitize_question_text(question)
    if not k or not q or not _is_single_question_text(q):
        return
    row = con.execute("SELECT key FROM investor_question_overrides WHERE key = ? LIMIT 1", (k,)).fetchone()
    if row:
        con.execute("UPDATE investor_question_overrides SET question=?, updated_at=? WHERE key=?", (q, now, k))
    else:
        con.execute(
            "INSERT INTO investor_question_overrides (key, question, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (k, q, now, now),
        )


def _extract_question_override_text(answer: str) -> str:
    txt = str(answer or "").strip()
    if not txt:
        return ""
    low = txt.lower()
    if "not this" in low and "but" in low:
        tail = txt[low.find("but") + 3 :].strip(" :.-")
        if len(tail) >= 10:
            tail = re.sub(r"^(better\s+question|ask\s+instead|use\s+this\s+question|ask\s+this)\s*:\s*", "", tail, flags=re.I)
            return tail.strip()
    for marker in ("ask instead:", "better question:", "use this question:", "ask this:"):
        idx = low.find(marker)
        if idx >= 0:
            tail = txt[idx + len(marker) :].strip(" :.-")
            if len(tail) >= 10:
                return tail
    return ""


def _is_profile_row(queue_ticker: str) -> bool:
    return str(queue_ticker or "").strip().upper().startswith(PROFILE_QUEUE_PREFIX)


def _profile_key_from_queue_ticker(queue_ticker: str) -> str:
    raw = str(queue_ticker or "").strip()
    if not _is_profile_row(raw):
        return ""
    return raw[len(PROFILE_QUEUE_PREFIX):].strip().lower()


def _set_profile_answer(con: sqlite3.Connection, key: str, answer: str) -> None:
    now = dt.datetime.now().isoformat()
    k = str(key or "").strip().lower()
    if not k:
        return
    row = con.execute("SELECT key FROM investor_style_memory WHERE key = ? LIMIT 1", (k,)).fetchone()
    if row:
        con.execute("UPDATE investor_style_memory SET answer=?, updated_at=? WHERE key=?", (str(answer or ""), now, k))
    else:
        con.execute(
            "INSERT INTO investor_style_memory (key, answer, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (k, str(answer or ""), now, now),
        )


def _interview_question(step: int, ticker: str) -> str:
    s = int(step or 0)
    if s <= 0:
        return f"For {ticker}, when did we start the position, around what price, and what was the original thesis?"
    if s == 1:
        return f"For {ticker}, is this a LONG TERM hold, a TRADE, or a LEGACY position?"
    return f"For {ticker}, what is the invalidation/exit rule, and do you have a target range?"


def _queue_question_text(queue_ticker: str, step: int, last_question: str) -> str:
    tk = str(queue_ticker or "").strip().upper()
    s = int(step or 0)
    lq = _sanitize_question_text(last_question)
    if _is_profile_row(tk):
        pkey = _profile_key_from_queue_ticker(tk)
        _k, default_q = _profile_question_by_key(pkey)
        if _is_single_question_text(lq):
            return lq
        return default_q
    return _interview_question(s, tk)


def start_portfolio_interview(session_id: str = "") -> dict[str, Any]:
    ensure_portfolio_memory_schema()
    holdings = get_holdings(limit=1000)
    now = dt.datetime.now().isoformat()
    sid = str(session_id or "").strip()[:64] or ("intv_" + now.replace(":", "").replace("-", "").replace("T", "")[:14])
    con = _conn()
    try:
        queued = 0
        for idx, item in enumerate(PROFILE_QUESTIONS):
            key, default_question = item
            question = _get_profile_question_override(con, key) or default_question
            qid = _profile_queue_id(key)
            a = con.execute(
                "SELECT COALESCE(answer, '') AS answer FROM investor_style_memory WHERE key = ? LIMIT 1",
                (key,),
            ).fetchone()
            if a and str(a["answer"] or "").strip():
                continue
            cur = con.execute("SELECT status FROM portfolio_interview_queue WHERE ticker = ? LIMIT 1", (qid,)).fetchone()
            if cur:
                if str(cur[0] or "").strip().lower() == "done":
                    continue
                con.execute(
                    "UPDATE portfolio_interview_queue SET status='pending', step=?, last_question=?, session_id=?, updated_at=? WHERE ticker=?",
                    (idx, question, sid, now, qid),
                )
            else:
                con.execute(
                    """INSERT INTO portfolio_interview_queue (ticker, status, step, last_question, session_id, completed_at, created_at, updated_at)
                       VALUES (?, 'pending', ?, ?, ?, '', ?, ?)""",
                    (qid, idx, question, sid, now, now),
                )
            queued += 1

        for h in holdings:
            tk = str(h.get("ticker") or "").strip().upper()
            if not tk:
                continue
            th = con.execute(
                """SELECT COALESCE(thesis_summary,''), COALESCE(thesis,'')
                   FROM watchlist_thesis WHERE ticker = ? LIMIT 1""",
                (tk,),
            ).fetchone()
            has_thesis = bool(th and (str(th[0] or "").strip() or str(th[1] or "").strip()))
            if has_thesis:
                continue
            cur = con.execute("SELECT status, step FROM portfolio_interview_queue WHERE ticker = ? LIMIT 1", (tk,)).fetchone()
            if cur:
                if str(cur[0] or "").strip().lower() == "done":
                    continue
                cur_step = int((cur[1] or 0))
                con.execute(
                    "UPDATE portfolio_interview_queue SET status='pending', last_question=?, session_id=?, updated_at=? WHERE ticker=?",
                    (_interview_question(cur_step, tk), sid, now, tk),
                )
            else:
                con.execute(
                    """INSERT INTO portfolio_interview_queue (ticker, status, step, last_question, session_id, completed_at, created_at, updated_at)
                       VALUES (?, 'pending', 0, ?, ?, '', ?, ?)""",
                    (tk, _interview_question(0, tk), sid, now, now),
                )
            queued += 1
        con.commit()
        row = con.execute(
            """SELECT ticker, step, last_question FROM portfolio_interview_queue
               WHERE status='pending'
               ORDER BY CASE WHEN ticker LIKE 'PROFILE::%' THEN 0 ELSE 1 END, id ASC
               LIMIT 1"""
        ).fetchone()
        if not row:
            return {"ok": True, "queued": 0, "session_id": sid, "next": {}}
        tk = str(row["ticker"] or "").strip().upper()
        step = int(row["step"] or 0)
        q = _queue_question_text(tk, step, str(row["last_question"] or ""))
        return {"ok": True, "queued": queued, "session_id": sid, "next": {"ticker": tk, "step": step, "question": q}}
    finally:
        con.close()


def _parse_approx_trade_answer(answer: str) -> tuple[str, float, str]:
    txt = str(answer or "").strip()
    low = txt.lower()
    # Approx date
    date_s = ""
    m_year = re.search(r"\b(20\d{2}|19\d{2})\b", txt)
    if m_year:
        date_s = f"{m_year.group(1)}-01-01"
    else:
        m_date = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", txt)
        if m_date:
            date_s = m_date.group(1)
    # Approx price
    price = 0.0
    m_price = re.search(r"\$?\s*(\d+(?:\.\d+)?)", txt)
    if m_price:
        price = _to_float(m_price.group(1), 0.0)
    # Reason
    reason = txt
    m_reason = re.search(r"\b(?:because|for|thesis)\b[:\s-]*(.+)$", txt, flags=re.I)
    if m_reason:
        reason = str(m_reason.group(1) or "").strip()
    if not reason:
        reason = low
    return date_s, price, reason


def backfill_trade_history(
    ticker: str,
    approx_date: str,
    approx_price: float,
    reason: str,
    action: str = "buy",
    quantity: float = 0.0,
) -> dict[str, Any]:
    t = str(ticker or "").strip().upper()[:16]
    if not t:
        return {"ok": False, "error": "ticker_required"}
    a = str(action or "buy").strip().lower()
    if a not in {"buy", "sell", "trim", "add"}:
        a = "buy"
    qty = float(quantity or 0.0)
    if qty <= 0:
        qty = 1.0
    px = float(approx_price or 0.0)
    ts = str(approx_date or "").strip() or dt.date.today().isoformat()
    rs = str(reason or "").strip() or "Interview backfill"
    record_portfolio_transaction(
        ticker=t,
        action=a,
        shares=qty,
        price=px,
        note=rs,
        source="interview_backfill",
        meta={"approximate": True},
    )
    record_decision(
        ticker=t,
        action=a,
        reason=rs,
        reasoning=rs,
        quantity=qty,
        price=px,
        timestamp=ts,
        confidence=0.75,
        source="interview_backfill",
    )
    return {"ok": True, "ticker": t, "action": a}


def submit_portfolio_interview_answer(answer: str, ticker: str = "") -> dict[str, Any]:
    ans = str(answer or "").strip()
    if not ans:
        return {"ok": False, "error": "answer_required"}
    con = _conn()
    try:
        row = None
        if ticker:
            row = con.execute(
                """SELECT id, ticker, step FROM portfolio_interview_queue
                   WHERE ticker = ? AND status='pending' LIMIT 1""",
                (str(ticker).strip().upper(),),
            ).fetchone()
        if row is None:
            row = con.execute(
                """SELECT id, ticker, step FROM portfolio_interview_queue
                   WHERE status='pending'
                   ORDER BY CASE WHEN ticker LIKE 'PROFILE::%' THEN 0 ELSE 1 END, id ASC
                   LIMIT 1"""
            ).fetchone()
        if row is None:
            return {"ok": False, "error": "no_pending_interview"}
        qid = int(row["id"])
        tk = str(row["ticker"] or "").strip().upper()
        step = int(row["step"] or 0)

        if _is_profile_row(tk):
            pkey = _profile_key_from_queue_ticker(tk)
            override_q = _extract_question_override_text(ans)
            if override_q:
                _set_profile_question_override(con, pkey, override_q)
                now = dt.datetime.now().isoformat()
                con.execute(
                    "UPDATE portfolio_interview_queue SET last_question=?, updated_at=? WHERE id=?",
                    (override_q, now, qid),
                )
                con.commit()
                return {
                    "ok": True,
                    "completed": "",
                    "next": {"ticker": tk, "step": step, "question": override_q},
                    "override_saved": True,
                }
            _set_profile_answer(con, pkey, ans)
            now = dt.datetime.now().isoformat()
            con.execute(
                "UPDATE portfolio_interview_queue SET status='done', completed_at=?, updated_at=? WHERE id=?",
                (now, now, qid),
            )
            con.commit()
            record_decision(
                ticker="",
                action="interview_profile",
                reason=f"{pkey}: {ans}",
                reasoning=ans,
                confidence=0.9,
                source="interview_mode",
            )
            nxt = con.execute(
                """SELECT ticker, step, last_question FROM portfolio_interview_queue
                   WHERE status='pending'
                   ORDER BY CASE WHEN ticker LIKE 'PROFILE::%' THEN 0 ELSE 1 END, id ASC
                   LIMIT 1"""
            ).fetchone()
            if nxt is None:
                return {"ok": True, "completed": "PROFILE", "next": {}}
            return {
                "ok": True,
                "completed": "PROFILE",
                "next": {
                    "ticker": str(nxt["ticker"] or "").strip().upper(),
                    "step": int(nxt["step"] or 0),
                    "question": _queue_question_text(
                        str(nxt["ticker"] or "").strip().upper(),
                        int(nxt["step"] or 0),
                        str(nxt["last_question"] or "").strip(),
                    ),
                },
            }

        th = con.execute(
            """SELECT thesis_summary, conviction_rating, time_horizon, invalidation_criteria
               FROM watchlist_thesis WHERE ticker = ? LIMIT 1""",
            (tk,),
        ).fetchone()
        cur_thesis = str((th["thesis_summary"] if th else "") or "")
        cur_conv = int((th["conviction_rating"] if th else 0) or 0)
        cur_hor = str((th["time_horizon"] if th else "") or "")
        cur_inv = str((th["invalidation_criteria"] if th else "") or "")

        if step <= 0:
            cur_thesis = ans
            adate, aprice, areason = _parse_approx_trade_answer(ans)
            backfill_trade_history(
                ticker=tk,
                approx_date=adate,
                approx_price=aprice,
                reason=areason,
                action="buy",
                quantity=1.0,
            )
        elif step == 1:
            up = ans.upper()
            if "LEGACY" in up:
                cur_hor = "LEGACY"
                upsert_watchlist_thesis(
                    ticker=tk,
                    strategy_tag="LEGACY",
                    pattern_learnable=0,
                    status="active",
                )
            elif "TRADE" in up:
                cur_hor = "TRADE"
                upsert_watchlist_thesis(
                    ticker=tk,
                    strategy_tag="TRADE",
                    pattern_learnable=1,
                    status="active",
                )
            else:
                cur_hor = "LONG_TERM"
                upsert_watchlist_thesis(
                    ticker=tk,
                    strategy_tag="LONG_TERM",
                    pattern_learnable=1,
                    status="active",
                )
        else:
            cur_inv = ans
        upsert_watchlist_thesis(
            ticker=tk,
            thesis=cur_thesis,
            thesis_summary=cur_thesis,
            conviction_rating=cur_conv,
            time_horizon=cur_hor,
            invalidation_criteria=cur_inv,
            status="active",
        )
        record_decision(
            ticker=tk,
            action="interview_answer",
            reason=ans,
            reasoning=ans,
            confidence=0.9,
            source="interview_mode",
        )
        now = dt.datetime.now().isoformat()
        if step >= 2:
            con.execute(
                "UPDATE portfolio_interview_queue SET status='done', step=3, completed_at=?, updated_at=? WHERE id=?",
                (now, now, qid),
            )
        else:
            nstep = step + 1
            nq = _interview_question(nstep, tk)
            con.execute(
                "UPDATE portfolio_interview_queue SET step=?, last_question=?, updated_at=? WHERE id=?",
                (nstep, nq, now, qid),
            )
        con.commit()
        nxt = con.execute(
            """SELECT ticker, step, last_question FROM portfolio_interview_queue
               WHERE status='pending'
               ORDER BY CASE WHEN ticker LIKE 'PROFILE::%' THEN 0 ELSE 1 END, id ASC
               LIMIT 1"""
        ).fetchone()
        if nxt is None:
            return {"ok": True, "completed": tk, "next": {}}
        return {
            "ok": True,
            "completed": tk if step >= 2 else "",
            "next": {
                "ticker": str(nxt["ticker"] or "").strip().upper(),
                "step": int(nxt["step"] or 0),
                "question": _queue_question_text(
                    str(nxt["ticker"] or "").strip().upper(),
                    int(nxt["step"] or 0),
                    str(nxt["last_question"] or "").strip(),
                ),
            },
        }
    finally:
        con.close()


def import_portfolio_history_csv(csv_text: str, source: str = "broker_csv") -> dict[str, Any]:
    txt = str(csv_text or "").strip()
    if not txt:
        return {"ok": False, "inserted": 0, "error": "empty_csv"}
    ensure_portfolio_memory_schema()
    f = io.StringIO(txt)
    reader = csv.DictReader(f)
    inserted = 0
    for row in reader:
        if not isinstance(row, dict):
            continue
        norm = {str(k or "").strip().lower(): str(v or "").strip() for k, v in row.items()}
        ticker = str(norm.get("ticker") or norm.get("symbol") or "").strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,15}", ticker):
            continue
        action = str(norm.get("action") or norm.get("side") or "").strip().lower()
        if action not in {"buy", "sell", "trim", "add"}:
            action = "buy"
        qty = _to_float(norm.get("quantity") or norm.get("qty") or norm.get("shares"), 0.0)
        price = _to_float(norm.get("price") or norm.get("avg_price") or norm.get("fill_price"), 0.0)
        ts_raw = str(norm.get("timestamp") or norm.get("date") or norm.get("time") or "").strip()
        ts = _normalize_import_timestamp(ts_raw) or ts_raw
        reason = str(norm.get("reason") or norm.get("reasoning") or norm.get("note") or "").strip()
        if qty <= 0:
            continue
        record_portfolio_transaction(
            ticker=ticker,
            action=action,
            shares=qty,
            price=price,
            note=reason,
            source=source,
            meta={"imported": True},
            timestamp=ts,
        )
        record_decision(
            ticker=ticker,
            action=action,
            reason=reason or f"Imported {action} history",
            reasoning=reason or f"Imported {action} history",
            quantity=qty,
            price=price,
            timestamp=ts,
            confidence=0.8,
            source=source,
        )
        inserted += 1
    return {"ok": True, "inserted": inserted}


def get_interview_progress() -> dict[str, Any]:
    con = _conn()
    try:
        row = con.execute(
            """SELECT
                 SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done_cnt,
                 SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) AS pending_cnt,
                 COUNT(*) AS total_cnt
               FROM portfolio_interview_queue"""
        ).fetchone()
        done = int((row["done_cnt"] if row else 0) or 0)
        pending = int((row["pending_cnt"] if row else 0) or 0)
        total = int((row["total_cnt"] if row else 0) or 0)
        reviewed = done
        if pending > 0:
            reviewed = max(done, total - pending)
        return {"reviewed": reviewed, "total": total, "done": done, "pending": pending}
    finally:
        con.close()


def get_pending_interview_question() -> dict[str, Any]:
    ensure_portfolio_memory_schema()
    con = _conn()
    try:
        row = con.execute(
            """SELECT ticker, step, last_question
               FROM portfolio_interview_queue
               WHERE status='pending'
               ORDER BY CASE WHEN ticker LIKE 'PROFILE::%' THEN 0 ELSE 1 END, id ASC
               LIMIT 1"""
        ).fetchone()
        if not row:
            return {"ok": True, "pending": False, "next": {}}
        tk = str(row["ticker"] or "").strip().upper()
        return {
            "ok": True,
            "pending": True,
            "next": {
                "ticker": tk,
                "step": int(row["step"] or 0),
                "question": _queue_question_text(tk, int(row["step"] or 0), str(row["last_question"] or "").strip()),
            },
        }
    finally:
        con.close()


def get_missing_profile_questions(limit: int = 3) -> list[dict[str, str]]:
    ensure_portfolio_memory_schema()
    lim = max(1, min(int(limit or 3), 10))
    con = _conn()
    try:
        out: list[dict[str, str]] = []
        for key, default_question in PROFILE_QUESTIONS:
            question = _get_profile_question_override(con, key) or default_question
            row = con.execute(
                "SELECT COALESCE(answer, '') AS answer FROM investor_style_memory WHERE key = ? LIMIT 1",
                (key,),
            ).fetchone()
            if row and str(row["answer"] or "").strip():
                continue
            out.append({"key": key, "question": question})
            if len(out) >= lim:
                break
        return out
    finally:
        con.close()


def _has_thesis_for_ticker(con: sqlite3.Connection, ticker: str) -> bool:
    tk = str(ticker or "").strip().upper()
    if not tk:
        return False
    row = con.execute(
        """SELECT COALESCE(thesis_summary,''), COALESCE(thesis,'')
           FROM watchlist_thesis WHERE ticker = ? LIMIT 1""",
        (tk,),
    ).fetchone()
    if not row:
        return False
    return bool(str(row[0] or "").strip() or str(row[1] or "").strip())


def _portfolio_watchlist_sets() -> tuple[set[str], set[str]]:
    portfolio = _read_portfolio_tickers_file()
    watchlist = _read_watchlist_tickers_file()
    return portfolio, watchlist


def _earnings_relevant_hits(today: dt.date | None = None) -> list[dict[str, str]]:
    td = today or dt.date.today()
    earnings_path = _latest_report_path(("earnings_radar_",))
    appendix_path = _latest_report_path(("terminal_appendix_",))
    earnings_txt = _read_text_file(earnings_path)
    appendix_txt = _read_text_file(appendix_path)
    primary = appendix_txt if _is_stale(earnings_path, max_age_hours=36) and appendix_txt else (earnings_txt or appendix_txt)
    rows = _extract_earnings_week_rows(primary, today=td, row_limit=24)
    if not rows and primary is not appendix_txt:
        rows = _extract_earnings_week_rows(appendix_txt, today=td, row_limit=24)
    if not rows:
        return []
    pset, wset = _portfolio_watchlist_sets()
    all_set = pset | wset
    out: list[dict[str, str]] = []
    for r in rows:
        tk = str(r.get("symbol") or "").strip().upper()
        if not tk or tk not in all_set:
            continue
        d = str(r.get("date") or "").strip()
        scope = "portfolio" if tk in pset else "watchlist"
        out.append(
            {
                "date": d,
                "ticker": tk,
                "scope": scope,
                "company": str(r.get("company") or "-").strip(),
                "time": str(r.get("time") or "-").strip(),
            }
        )
    return out


def _read_portfolio_tickers_file() -> set[str]:
    p = ROOT / "data" / "portfolio.csv"
    out: set[str] = set()
    if not p.exists():
        return out
    try:
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = [x.strip() for x in str(ln or "").split(",")]
            if not parts:
                continue
            tk = _safe_ticker(parts[0] if len(parts) >= 1 else "")
            if tk:
                out.add(tk)
    except Exception:
        return set()
    return out


def _read_watchlist_tickers_file() -> set[str]:
    p = ROOT / "data" / "my_watchlist.txt"
    out: set[str] = set()
    if not p.exists():
        return out
    try:
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = str(ln or "").strip()
            if not s or s.startswith("#"):
                continue
            parts = [x.strip() for x in s.split(",")]
            tk = _safe_ticker(parts[0] if parts else "")
            if tk:
                out.add(tk)
    except Exception:
        return set()
    return out


def get_proactive_gap_prompt(user_name: str = "Arda") -> dict[str, Any]:
    # Dynamic gap-analysis loop is handled in daily_operator (LLM-generated question, no fixed question templates).
    try:
        from app.services.daily_operator import get_dynamic_gap_prompt

        return get_dynamic_gap_prompt(user_name=user_name)
    except Exception:
        pass
    ensure_portfolio_memory_schema()
    name = str(user_name or "").strip() or "Arda"
    pending = get_pending_interview_question()
    if bool(pending.get("pending")):
        return {"ok": True, "needs_attention": False, "prompt_id": "", "message": "", "kind": "none"}

    # 1) Earnings alerts for portfolio/watchlist names (highest urgency).
    td = dt.date.today()
    hits = _earnings_relevant_hits(today=td)
    if hits:
        today_hits = [h for h in hits if str(h.get("date") or "") == td.isoformat()]
        tomorrow_hits = [h for h in hits if str(h.get("date") or "") == (td + dt.timedelta(days=1)).isoformat()]
        selected = today_hits if today_hits else tomorrow_hits
        if selected:
            selected = selected[:4]
            lines: list[str] = []
            p_tks: list[str] = []
            w_tks: list[str] = []
            for h in selected:
                tk = str(h.get("ticker") or "").strip().upper()
                sc = str(h.get("scope") or "").strip().lower()
                tm = str(h.get("time") or "-").strip()
                lines.append(f"- {tk} ({sc}, {tm})")
                if sc == "portfolio":
                    p_tks.append(tk)
                else:
                    w_tks.append(tk)
            bucket = "today" if today_hits else "tomorrow"
            uniq = sorted({str(x.get("ticker") or "").strip().upper() for x in selected if str(x.get("ticker") or "").strip()})
            return {
                "ok": True,
                "needs_attention": True,
                "prompt_id": f"earnings:{bucket}:{','.join(uniq)}:{td.isoformat()}",
                "message": (
                    f"{name}, earnings alert: these names are reporting {bucket}.\n"
                    + "\n".join(lines)
                    + "\nLet's capture notes right after the print: what changed vs thesis, guidance, and risk?"
                ),
                "kind": "earnings_alert",
                "portfolio_tickers": sorted(set(p_tks)),
                "watchlist_tickers": sorted(set(w_tks)),
            }

    # 1.5) High-impact report facts from latest ingested daily/earnings reports.
    try:
        facts = query_report_facts(query="", tickers=list((_portfolio_watchlist_sets()[0] | _portfolio_watchlist_sets()[1])), limit=20)
    except Exception:
        facts = []
    hi = [f for f in facts if int(f.get("importance") or 0) >= 70][:3]
    if hi:
        lines = []
        tickers: list[str] = []
        for f in hi:
            tk = str(f.get("ticker") or "").strip().upper()
            if tk:
                tickers.append(tk)
            lines.append(f"- {tk + ': ' if tk else ''}{str(f.get('fact_text') or '')[:130]}")
        return {
            "ok": True,
            "needs_attention": True,
            "prompt_id": f"report_facts:{dt.date.today().isoformat()}:{','.join(sorted(set(tickers))[:6])}",
            "message": (
                f"{name}, here are the highest-impact items from fresh reports:\n"
                + "\n".join(lines)
                + "\nShould I convert these into structured thesis/risk notes?"
            ),
            "kind": "report_fact_alert",
            "tickers": sorted(set(tickers)),
        }

    # 2) Profile gaps.
    missing_profile = get_missing_profile_questions(limit=1)
    if missing_profile:
        q = missing_profile[0]
        key = str(q.get("key") or "").strip()
        question = str(q.get("question") or "").strip()
        return {
            "ok": True,
            "needs_attention": True,
            "prompt_id": f"profile:{key}",
            "message": f"{name}, quick check-in. I still don't have this part of your playbook.\n{question}",
            "kind": "profile_gap",
            "missing_key": key,
        }

    con = _conn()
    try:
        # 3) Holdings with missing thesis memory.
        portfolio_set, _watchlist_set = _portfolio_watchlist_sets()
        for tk in sorted(portfolio_set):
            if not tk:
                continue
            if _has_thesis_for_ticker(con, tk):
                continue
            return {
                "ok": True,
                "needs_attention": True,
                "prompt_id": f"thesis:{tk}",
                "message": (
                    f"{name}, I’m missing the story for {tk}. "
                    f"Why do we own it, is it LONG TERM / TRADE / LEGACY, "
                    f"and what would make us exit?"
                ),
                "kind": "thesis_gap",
                "ticker": tk,
            }

        # 4) Comparison gap: ask explicit reason for owning one peer and not the other.
        peer_pairs = [
            ("HUBS", "CRM", "CRM software"),
            ("GOOGL", "META", "digital advertising"),
            ("MSFT", "ORCL", "enterprise software"),
        ]
        held = set(portfolio_set)
        for own, alt, theme in peer_pairs:
            if own in held and alt not in held:
                return {
                    "ok": True,
                    "needs_attention": True,
                    "prompt_id": f"peer_gap:{own}:{alt}",
                    "message": (
                        f"{name}, one decision gap I want to document: we own {own} but not {alt} in {theme}. "
                        f"What rule explains that choice?"
                    ),
                    "kind": "peer_gap",
                    "owned": own,
                    "not_owned": alt,
                }
            if alt in held and own not in held:
                return {
                    "ok": True,
                    "needs_attention": True,
                    "prompt_id": f"peer_gap:{alt}:{own}",
                    "message": (
                        f"{name}, one decision gap I want to document: we own {alt} but not {own} in {theme}. "
                        f"What rule explains that choice?"
                    ),
                    "kind": "peer_gap",
                    "owned": alt,
                    "not_owned": own,
                }
    finally:
        con.close()

    return {
        "ok": True,
        "needs_attention": False,
        "prompt_id": "",
        "message": "",
        "kind": "none",
    }


def get_live_portfolio_summary() -> dict[str, Any]:
    rows = get_holdings(limit=1000)
    tickers = [str(r.get("ticker") or "").strip().upper() for r in rows if str(r.get("ticker") or "").strip()]
    qmap: dict[str, dict[str, float]] = {}
    quote_source = "none"
    quote_asof = dt.datetime.now().isoformat(timespec="seconds")
    try:
        import yfinance as yf  # type: ignore

        quote_source = "yfinance_fast_info"
        for t in sorted(set(tickers)):
            if not t:
                continue
            last = 0.0
            prev = 0.0
            try:
                tk = yf.Ticker(t)
                fi = tk.fast_info or {}
                last = _to_float(fi.get("last_price"), 0.0)
                prev = _to_float(fi.get("previous_close"), 0.0)
                if last <= 0:
                    last = _to_float(fi.get("regular_market_price"), 0.0)
                if prev <= 0:
                    prev = _to_float(fi.get("regular_market_previous_close"), 0.0)
                if last <= 0 or prev <= 0:
                    info = tk.info or {}
                    if last <= 0:
                        last = _to_float(info.get("currentPrice"), 0.0)
                    if last <= 0:
                        last = _to_float(info.get("regularMarketPrice"), 0.0)
                    if prev <= 0:
                        prev = _to_float(info.get("previousClose"), 0.0)
                    if prev <= 0:
                        prev = _to_float(info.get("regularMarketPreviousClose"), 0.0)
                if last > 0 and prev > 0:
                    qmap[t] = {"last": float(last), "prev": float(prev)}
            except Exception:
                continue
    except Exception:
        quote_source = "unavailable"

    cost_basis = 0.0
    tracked_prev = 0.0
    tracked_value = 0.0
    move = 0.0
    movers: list[dict[str, Any]] = []
    missing = 0
    for r in rows:
        t = str(r.get("ticker") or "").strip().upper()
        sh = _to_float(r.get("shares"), 0.0)
        cost = _to_float(r.get("cost"), 0.0)
        if sh <= 0:
            continue
        if cost > 0:
            cost_basis += sh * cost
        q = qmap.get(t) or {}
        last = _to_float(q.get("last"), 0.0)
        prev = _to_float(q.get("prev"), 0.0)
        if last > 0 and prev > 0:
            pos_now = sh * last
            pos_prev = sh * prev
            tracked_value += pos_now
            tracked_prev += pos_prev
            move += (pos_now - pos_prev)
            d = ((last - prev) / abs(prev) * 100.0) if prev else 0.0
            movers.append({"ticker": t, "day_pct": d})
        else:
            missing += 1
    movers.sort(key=lambda x: float(x.get("day_pct") or 0.0), reverse=True)
    day_pct = (move / tracked_prev * 100.0) if tracked_prev > 0 else 0.0
    alerts: list[str] = []
    if abs(day_pct) >= 2.0:
        alerts.append(f"Portfolio move is significant: {day_pct:+.2f}% today.")
    if movers and abs(float(movers[0].get("day_pct") or 0.0)) >= 5.0:
        alerts.append(f"Top mover: {movers[0]['ticker']} {float(movers[0]['day_pct']):+.2f}%.")
    if len(movers) > 1 and abs(float(movers[-1].get("day_pct") or 0.0)) >= 5.0:
        alerts.append(f"Lagging mover: {movers[-1]['ticker']} {float(movers[-1]['day_pct']):+.2f}%.")
    if missing > 0:
        alerts.append(f"{missing} holding(s) missing day move data.")
    coverage_pct = (len(movers) / max(1, len(rows))) * 100.0
    if coverage_pct < 80.0:
        alerts.append(f"Quote coverage low: {coverage_pct:.0f}% of holdings.")
    return {
        "asof": quote_asof,
        "quote_source": quote_source,
        "holdings_count": len(rows),
        "covered_holdings": len(movers),
        "coverage_pct": coverage_pct,
        # Denominator for day move % is previous-close marked value for covered holdings.
        "tracked_basis": tracked_prev,
        "tracked_prev_close_value": tracked_prev,
        "tracked_market_value": tracked_value,
        "cost_basis": cost_basis,
        "day_change_usd": move,
        "day_change_pct": day_pct,
        "est_total": tracked_value if tracked_value > 0 else (tracked_prev + move),
        "best": movers[0] if movers else {},
        "worst": movers[-1] if movers else {},
        "alerts": alerts,
    }


def build_watcher_events(max_events: int = 8) -> list[str]:
    s = get_live_portfolio_summary()
    events: list[str] = []
    for a in list(s.get("alerts") or []):
        if str(a or "").strip():
            events.append(str(a))
    if not events and s.get("holdings_count", 0):
        events.append(
            f"Portfolio quiet: {float(s.get('day_change_pct') or 0.0):+.2f}% "
            f"({float(s.get('day_change_usd') or 0.0):+.0f} USD)."
        )
    tx = list_recent_portfolio_transactions(limit=3)
    if tx:
        latest = tx[0]
        events.append(
            f"Latest portfolio action: {latest.get('action')} {latest.get('ticker')} "
            f"{float(latest.get('shares') or 0.0):g} @ {float(latest.get('price') or 0.0):.2f}."
        )
    return events[: max(1, int(max_events))]


def _report_ingest_due(min_hours: int = 6) -> bool:
    ensure_portfolio_memory_schema()
    con = _conn()
    try:
        row = con.execute(
            "SELECT state_value FROM report_fact_ingest_state WHERE state_key='last_ingest_at' LIMIT 1"
        ).fetchone()
        if not row:
            return True
        raw = str(row["state_value"] or "").strip()
        if not raw:
            return True
        try:
            last = dt.datetime.fromisoformat(raw.replace("Z", ""))
        except Exception:
            return True
        return (dt.datetime.now() - last).total_seconds() >= float(max(1, int(min_hours))) * 3600.0
    finally:
        con.close()


def _top_runtime_tickers(limit: int = 10) -> list[str]:
    rows = get_holdings(limit=max(1, min(100, int(limit or 10))))
    out: list[str] = []
    for r in rows:
        tk = str(r.get("ticker") or "").strip().upper()
        if not tk:
            continue
        if tk not in out:
            out.append(tk)
        if len(out) >= limit:
            break
    return out


def inject_runtime_context(context: dict[str, Any] | None, query: str = "") -> dict[str, Any]:
    ctx: dict[str, Any] = dict(context or {})
    ttl_sec = max(10, min(120, int(float(os.environ.get("RUNTIME_CONTEXT_CACHE_TTL_SEC", "45")))))
    path = str(ctx.get("current_path") or "").strip()
    q_norm = re.sub(r"\s+", " ", str(query or "").strip().lower())[:180]
    cache_key = f"{path}|{q_norm}"
    now_ts = time.time()
    with _RUNTIME_CACHE_LOCK:
        hit = _RUNTIME_CACHE.get(cache_key)
        if hit and (now_ts - float(hit[0])) <= float(ttl_sec):
            cached_ctx = dict(ctx)
            cached_ctx["runtime"] = json.loads(json.dumps(hit[1], ensure_ascii=True))
            return cached_ctx
    try:
        if _report_ingest_due(min_hours=6):
            ingest_recent_report_facts(limit_reports=24, facts_per_report=12)
    except Exception:
        pass
    map_query = f"{path} {query}".strip()
    page_hits = retrieve_app_knowledge(map_query, limit=3) if map_query else []
    page_hint = page_hits[0] if page_hits else {}
    ctx["runtime"] = {
        "asof": dt.datetime.now().isoformat(timespec="seconds"),
        "current_path": path,
        "page_hint": {
            "title": str(page_hint.get("title") or ""),
            "kind": str(page_hint.get("kind") or ""),
            "content": str(page_hint.get("content") or ""),
        },
        "portfolio_live": get_live_portfolio_summary(),
        "watcher_events": build_watcher_events(max_events=8),
        "recent_transactions": list_recent_portfolio_transactions(limit=8),
        "report_facts": query_report_facts(
            query=str(query or ""),
            tickers=_top_runtime_tickers(limit=10),
            limit=10,
        ),
    }
    with _RUNTIME_CACHE_LOCK:
        _RUNTIME_CACHE[cache_key] = (now_ts, json.loads(json.dumps(ctx["runtime"], ensure_ascii=True)))
        if len(_RUNTIME_CACHE) > 256:
            # Trim oldest entries to keep memory bounded.
            olds = sorted(_RUNTIME_CACHE.items(), key=lambda kv: kv[1][0])[:64]
            for k, _v in olds:
                _RUNTIME_CACHE.pop(k, None)
    return ctx


def get_morning_brief(limit_holdings: int = 5) -> dict[str, Any]:
    ensure_portfolio_memory_schema()
    live = get_live_portfolio_summary()
    holds = get_holdings(limit=max(1, min(20, int(limit_holdings or 5))))
    holds = sorted(holds, key=lambda r: float(r.get("shares") or 0.0), reverse=True)[:5]
    top_tickers = [str(h.get("ticker") or "").strip().upper() for h in holds if str(h.get("ticker") or "").strip()]

    bullets: list[str] = []
    if int(live.get("holdings_count") or 0) > 0:
        bullets.append(
            f"Portfolio pulse: {float(live.get('day_change_pct') or 0.0):+.2f}% "
            f"({float(live.get('day_change_usd') or 0.0):+.0f} USD) across {int(live.get('holdings_count') or 0)} holdings."
        )

    for side in ("best", "worst"):
        mv = live.get(side) if isinstance(live.get(side), dict) else {}
        tk = str(mv.get("ticker") or "").strip().upper()
        dp = float(mv.get("day_pct") or 0.0)
        if tk and abs(dp) >= 5.0:
            tag = "Strong mover" if dp > 0 else "Risk mover"
            bullets.append(f"{tag}: {tk} is {dp:+.2f}% today.")

    if top_tickers:
        marks = ",".join("?" for _ in top_tickers)
        con = _conn()
        try:
            rows = con.execute(
                f"""SELECT ticker, title, summary, created_at
                    FROM intel_feed
                    WHERE ticker IN ({marks})
                    ORDER BY id DESC
                    LIMIT 20""",
                tuple(top_tickers),
            ).fetchall()
        except Exception:
            rows = []
        finally:
            con.close()
        seen: set[str] = set()
        for r in rows:
            tk = str(r["ticker"] or "").strip().upper()
            if not tk or tk in seen:
                continue
            seen.add(tk)
            title = str(r["title"] or "").strip()
            summ = str(r["summary"] or "").strip()
            line = title or summ
            if line:
                bullets.append(f"News ({tk}): {line[:120]}")
            if len(bullets) >= 3:
                break

    if not bullets:
        bullets = ["No critical anomalies detected in the latest scan."]

    return {
        "ok": True,
        "asof": dt.datetime.now().isoformat(timespec="seconds"),
        "top_holdings": top_tickers,
        "bullets": bullets[:3],
    }


def save_morning_brief_snapshot(limit_holdings: int = 5, source: str = "scheduler") -> dict[str, Any]:
    ensure_portfolio_memory_schema()
    try:
        ingest_recent_report_facts(limit_reports=30, facts_per_report=12)
    except Exception:
        pass
    brief = get_morning_brief(limit_holdings=limit_holdings)
    day = dt.date.today().isoformat()
    now = dt.datetime.now().isoformat(timespec="seconds")
    payload = json.dumps(brief, ensure_ascii=False)
    src = str(source or "scheduler").strip()[:40] or "scheduler"

    def _write() -> bool:
        con = _conn()
        try:
            con.execute(
                """INSERT INTO morning_briefs (brief_day, created_at, source, brief_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(brief_day) DO UPDATE SET
                     created_at=excluded.created_at,
                     source=excluded.source,
                     brief_json=excluded.brief_json""",
                (day, now, src, payload),
            )
            con.commit()
            return True
        finally:
            con.close()

    _ = sqlite_retry(_write)
    return brief


def get_cached_morning_brief(day: str | None = None) -> dict[str, Any]:
    ensure_portfolio_memory_schema()
    target_day = str(day or dt.date.today().isoformat()).strip()
    con = _conn()
    try:
        row = con.execute(
            """SELECT brief_json
               FROM morning_briefs
               WHERE brief_day = ?
               ORDER BY id DESC
               LIMIT 1""",
            (target_day,),
        ).fetchone()
        if not row:
            return {}
        raw = str(row["brief_json"] or "").strip()
        if not raw:
            return {}
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}
    finally:
        con.close()
