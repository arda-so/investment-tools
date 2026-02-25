from __future__ import annotations

import json
import re
from pathlib import Path

from app.core.config import ROOT
from app.core.db import core_conn
from app.core.normalize import normalize_text
from app.services.postgres_core_service import pg_connect, pg_enabled


KNOWLEDGE_PATH = ROOT / "data" / "app_knowledge.json"


def _tokens(text: str) -> set[str]:
    stop = {
        "the", "and", "for", "with", "that", "this", "from", "into", "what", "when", "where", "how",
        "is", "are", "was", "were", "you", "your", "our", "app", "application", "please", "show",
    }
    out: set[str] = set()
    for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_/-]{2,}", normalize_text(text, keep_extra="/_-")):
        if w in stop:
            continue
        out.add(w)
    return out


def ensure_app_knowledge_map() -> None:
    if KNOWLEDGE_PATH.exists():
        return
    items = [
        {
            "id": "route_dashboard",
            "kind": "route",
            "title": "Dashboard",
            "tags": ["dashboard", "home", "briefing", "macro"],
            "content": "Route /dashboard renders daily market briefing, indices, lows scanner, earnings calendar, and live wire.",
        },
        {
            "id": "route_organizer",
            "kind": "route",
            "title": "Organizer Workspace",
            "tags": ["organizer", "daily log", "tasks", "notes", "approvals"],
            "content": "Route /organizer is the command center for daily log, tasks hub, notes hub, and approval queue.",
        },
        {
            "id": "route_reports",
            "kind": "route",
            "title": "Reports Library",
            "tags": ["reports", "library", "daily brief", "deep dive", "view"],
            "content": "Routes /reports and /reports/view expose generated reports, filters, and full report content.",
        },
        {
            "id": "route_company_file",
            "kind": "route",
            "title": "Company File",
            "tags": ["company", "profile", "sec", "filings", "thesis"],
            "content": "Route /company_file?t=TICKER shows company profile, notes, tasks, reminders, and SEC links.",
        },
        {
            "id": "route_company_sec",
            "kind": "route",
            "title": "Company SEC Page",
            "tags": ["sec", "filings", "10-k", "10-q", "8-k"],
            "content": "Route /company_file/sec?t=TICKER&form=10-K filters SEC filings by form and opens local/remote documents.",
        },
        {
            "id": "route_ai_command",
            "kind": "api",
            "title": "Unified AI Command",
            "tags": ["ai", "command", "orchestrator", "intent"],
            "content": "POST /ai/command accepts query + context and returns intent, status, message, confidence, redirect, citations, traces.",
        },
        {
            "id": "route_observability",
            "kind": "route",
            "title": "Observability",
            "tags": ["observability", "system events", "status", "latency"],
            "content": "Route /observability shows last 50 system_events with service, status, latency, and message.",
        },
        {
            "id": "workflow_daily_log",
            "kind": "workflow",
            "title": "Daily Log Workflow",
            "tags": ["daily log", "append", "save", "organizer"],
            "content": "AI intent add_daily_log appends text to today's daily note and redirects to /organizer?day=YYYY-MM-DD.",
        },
        {
            "id": "workflow_sec_sync",
            "kind": "workflow",
            "title": "SEC Sync",
            "tags": ["sec sync", "automation", "filings"],
            "content": "POST /company_file/sec-sync runs single-ticker sync. POST /company_file/sec-sync-my runs portfolio/watchlist SEC sync.",
        },
        {
            "id": "workflow_approvals",
            "kind": "workflow",
            "title": "Drafts and Approvals",
            "tags": ["draft", "approval", "queue", "risk"],
            "content": "AI writes pending drafts for notes; high-risk actions enter ai_action_queue and require approve/reject.",
        },
        {
            "id": "table_filings",
            "kind": "table",
            "title": "filings table",
            "tags": ["filings", "sec", "10-k", "10-q"],
            "content": "filings columns include ticker, form, date, accession, doc_url, path, downloaded_at.",
        },
        {
            "id": "table_todos",
            "kind": "table",
            "title": "todos table",
            "tags": ["tasks", "todo", "organizer"],
            "content": "todos stores task text, status, priority, due_date, ticker, category for organizer and company workflows.",
        },
        {
            "id": "table_notes",
            "kind": "table",
            "title": "investor_notes table",
            "tags": ["notes", "draft", "ai"],
            "content": "investor_notes stores general notes and AI drafts including status, created_by, ai_confidence, ai_reasoning, trace_id.",
        },
    ]
    KNOWLEDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    KNOWLEDGE_PATH.write_text(json.dumps({"items": items}, ensure_ascii=True, indent=2), encoding="utf-8")


def _load_items() -> list[dict[str, str]]:
    ensure_app_knowledge_map()
    try:
        obj = json.loads(KNOWLEDGE_PATH.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []
    raw = obj.get("items") if isinstance(obj, dict) else []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        out.append(
            {
                "id": str(r.get("id") or "").strip(),
                "kind": str(r.get("kind") or "").strip(),
                "title": str(r.get("title") or "").strip(),
                "tags": ",".join(r.get("tags") or []) if isinstance(r.get("tags"), list) else str(r.get("tags") or ""),
                "content": str(r.get("content") or "").strip(),
            }
        )
    return out


def _schema_items(limit_tables: int = 20) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if pg_enabled():
        con_pg = pg_connect()
        if con_pg is None:
            return out
        try:
            cur = con_pg.cursor()
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema='public'
                ORDER BY table_name ASC
                LIMIT %s
                """,
                (max(1, int(limit_tables)),),
            )
            tables = [str(r[0] or "").strip() for r in (cur.fetchall() or []) if str(r[0] or "").strip()]
            for nm in tables:
                cur.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema='public' AND table_name=%s
                    ORDER BY ordinal_position ASC
                    """,
                    (nm,),
                )
                col_names = [str(c[0] or "").strip() for c in (cur.fetchall() or []) if str(c[0] or "").strip()]
                out.append(
                    {
                        "id": f"schema_{nm}",
                        "kind": "schema",
                        "title": f"table {nm}",
                        "tags": "schema,table,db",
                        "content": f"Table {nm} columns: {', '.join(col_names[:24])}",
                    }
                )
        except Exception:
            return out
        finally:
            con_pg.close()
        return out
    try:
        con = core_conn()
        try:
            tables = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name ASC"
            ).fetchall()
            for t in tables[: max(1, int(limit_tables))]:
                nm = str(t["name"] or "").strip()
                if not nm:
                    continue
                cols = con.execute(f"PRAGMA table_info({nm})").fetchall()
                col_names = [str(c["name"] or "").strip() for c in cols if str(c["name"] or "").strip()]
                out.append(
                    {
                        "id": f"schema_{nm}",
                        "kind": "schema",
                        "title": f"table {nm}",
                        "tags": "schema,table,db",
                        "content": f"Table {nm} columns: {', '.join(col_names[:24])}",
                    }
                )
        finally:
            con.close()
    except Exception:
        return out
    return out


def retrieve_app_knowledge(query: str, limit: int = 8) -> list[dict[str, str]]:
    q_toks = _tokens(query)
    items = _load_items() + _schema_items()
    scored: list[tuple[int, dict[str, str]]] = []
    for it in items:
        hay = " ".join([it.get("title", ""), it.get("tags", ""), it.get("content", "")])
        h_toks = _tokens(hay)
        score = len(q_toks & h_toks)
        if score <= 0:
            continue
        scored.append((score, it))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [it for _s, it in scored[: max(1, min(30, int(limit)))]]


def format_app_knowledge(entries: list[dict[str, str]]) -> str:
    if not entries:
        return "(no app knowledge hits)"
    lines: list[str] = []
    for e in entries:
        lines.append(f"- [{e.get('kind','')}] {e.get('title','')}: {e.get('content','')}")
    return "\n".join(lines)
