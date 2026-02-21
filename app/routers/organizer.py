from __future__ import annotations

import datetime as dt
import difflib
import re
import sqlite3
import urllib.parse
from collections import Counter

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from app.core.config import CORE_DB_PATH
from app.core.sqlite_hardening import connect_sqlite, sqlite_retry
from app.services.memory_engine import OnyxMemory
from app.services.postgres_core_service import core_backend, delete_todo_pg, list_todos_pg
from app.services.google_workspace_service import (
    build_connect_url,
    complete_connect,
    disconnect_google,
    get_priority_emails,
    get_today_calendar_events,
    google_status,
    send_email,
)

from app.services.organizer_service import (
    approve_note_draft,
    add_task,
    add_general_note,
    close_day,
    discard_note_draft,
    get_daily_note,
    list_action_log,
    list_action_queue,
    list_recent_notes,
    list_tasks,
    recall,
    resolve_action_queue,
    save_daily_note,
    suggest_ir_emails,
    toggle_task,
)


router = APIRouter()

EMAIL_PRESETS: list[dict[str, str]] = [
    {
        "id": "ir_intro",
        "label": "IR Intro",
        "subject": "Investor questions on recent filings and capital allocation",
        "body": (
            "Hello Investor Relations,\n\n"
            "I am a long-term investor and reviewed your recent filings.\n"
            "I would appreciate clarification on the following points:\n"
            "1) [Key operating KPI trend and management view]\n"
            "2) [Capital allocation priority over the next 12-24 months]\n"
            "3) [Any non-obvious risk factor management is monitoring]\n\n"
            "If these are addressed in an existing deck or call transcript, a pointer would be very helpful.\n\n"
            "Thank you for your time.\n"
            "Best regards,\n"
            "[Your Name]"
        ),
    },
    {
        "id": "meeting_request",
        "label": "Meeting Request",
        "subject": "Request for a brief investor relations call",
        "body": (
            "Hello Investor Relations,\n\n"
            "I am a long-term shareholder and would appreciate a short call to better understand current priorities.\n"
            "Could you please clarify:\n"
            "1) Execution priorities for the next 2-4 quarters\n"
            "2) Key assumptions behind current guidance\n"
            "3) Capital allocation framework at current valuation levels\n\n"
            "I am available on [Option 1] or [Option 2], but I can adapt to your schedule.\n\n"
            "Thank you for your time.\n"
            "Best regards,\n"
            "[Your Name]"
        ),
    },
    {
        "id": "follow_up",
        "label": "Follow-up",
        "subject": "Follow-up on investor relations inquiry",
        "body": (
            "Hello Investor Relations,\n\n"
            "Following up on my previous inquiry regarding [topic], sent on [date].\n"
            "A short clarification on the points below would be appreciated:\n"
            "1) [Point]\n"
            "2) [Point]\n\n"
            "Thank you again for your time.\n\n"
            "Best regards,\n"
            "[Your Name]"
        ),
    },
]


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(s or "").lower())).strip()


def _company_alias_rows() -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    con = connect_sqlite(str(CORE_DB_PATH), row_factory=True)
    try:
        for r in con.execute("SELECT ticker, name FROM company_profile_cache").fetchall():
            t = str(r["ticker"] or "").strip().upper()
            nm = str(r["name"] or "").strip()
            if not t or not nm:
                continue
            n = _norm_text(nm)
            if not n:
                continue
            rows.append((n, t))
            # Common compact aliases: remove legal suffixes and web/domain tokens.
            short = re.sub(
                r"\b(incorporated|inc|corporation|corp|company|co|limited|ltd|plc|holdings|holding|group|class|the|com)\b",
                " ",
                n,
            )
            short = re.sub(r"\s+", " ", short).strip()
            if short and short != n:
                rows.append((short, t))
            # Strong single-word alias from first token (e.g., salesforce.com -> salesforce).
            parts = [p for p in short.split(" ") if p]
            if parts and len(parts[0]) >= 4:
                rows.append((parts[0], t))
            # Two-word alias if available (helps names like "bank of america").
            if len(parts) >= 2:
                rows.append((f"{parts[0]} {parts[1]}", t))
    finally:
        con.close()
    # Deduplicate by alias+ticker while preserving order.
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for a, t in rows:
        k = (a, t)
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def _infer_ticker_from_text(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    s = _norm_text(raw)
    if not s:
        return ""
    padded = f" {s} "
    aliases = _company_alias_rows()

    # 1) Exact alias containment.
    for alias, tk in aliases:
        if len(alias) < 4:
            continue
        if f" {alias} " in padded:
            return tk

    # 2) Fuzzy single-word match for minor typos (e.g., saleforce -> salesforce).
    words = [w for w in s.split(" ") if len(w) >= 6]
    if not words:
        return ""
    one_word_aliases = [a for a, _t in aliases if " " not in a and len(a) >= 6]
    alias_to_ticker = {a: t for a, t in aliases}
    for w in words[:10]:
        m = difflib.get_close_matches(w, one_word_aliases, n=1, cutoff=0.90)
        if m:
            return str(alias_to_ticker.get(m[0]) or "")
    # 3) Prefix fallback for minor truncation/typo around brand token.
    for w in words[:10]:
        for a in one_word_aliases[:3000]:
            if a.startswith(w[: max(4, min(len(w), 7))]) or w.startswith(a[: max(4, min(len(a), 7))]):
                tk = str(alias_to_ticker.get(a) or "")
                if tk:
                    return tk
    return ""


def _safe_day(day: str) -> str:
    d = str(day or "").strip()
    try:
        return dt.date.fromisoformat(d).isoformat()
    except Exception:
        return dt.date.today().isoformat()


def _due_from_cadence(cadence: str) -> str:
    c = str(cadence or "").strip().lower()
    today = dt.date.today()
    if c in {"day", "daily", "today"}:
        d = today
    elif c in {"week", "weekly"}:
        d = today + dt.timedelta(days=7)
    elif c in {"biweek", "biweekly", "2weeks", "two_weeks"}:
        d = today + dt.timedelta(days=14)
    else:
        return ""
    return d.isoformat()


def _is_hx(request: Request) -> bool:
    return str(request.headers.get("HX-Request") or "").strip().lower() == "true"


def _memory_rows(
    q: str = "",
    ticker: str = "",
    source: str = "",
    limit: int = 120,
) -> tuple[list[dict], int, bool]:
    mem = OnyxMemory()
    if not mem.available:
        return [], 0, False
    lim = max(20, min(500, int(limit or 120)))
    tq = str(q or "").strip()
    tk = str(ticker or "").strip().upper()
    src = str(source or "").strip().lower()

    rows = mem.recall(tq, n_results=min(lim, 60)) if tq else mem.browse(limit=lim)
    if tk:
        rows = [r for r in rows if str((r.get("metadata") or {}).get("ticker") or "").strip().upper() == tk]
    if src:
        rows = [r for r in rows if str((r.get("metadata") or {}).get("source_type") or "").strip().lower() == src]
    return rows[:lim], mem.count(), True


def _ctx(
    request: Request,
    message: str = "",
    query: str = "",
    day: str = "",
    recall_q: str = "",
    partial: bool = False,
    ir_ticker: str = "",
    ir_company: str = "",
    ir_domain: str = "",
):
    templates = request.app.state.templates
    d = _safe_day(day)
    daily = get_daily_note(d)
    open_tasks_raw = list_tasks(open_only=True, limit=500)
    done_tasks_raw = list_tasks(open_only=False, limit=120)
    open_tasks: list[dict[str, object]] = []
    company_tasks: list[dict[str, object]] = []
    quick_todos: list[dict[str, object]] = []
    done_tasks: list[dict[str, object]] = []
    for r in open_tasks_raw:
        row = dict(r)
        tk = str(row.get("ticker") or "").strip().upper()
        if not tk:
            tk = _infer_ticker_from_text(str(row.get("task") or ""))
        row["_link_ticker"] = tk
        open_tasks.append(row)
        cat = str(row.get("category") or "").strip().lower()
        if cat == "quick":
            quick_todos.append(row)
        elif cat == "company" or tk:
            company_tasks.append(row)
    for r in done_tasks_raw:
        row = dict(r)
        tk = str(row.get("ticker") or "").strip().upper()
        if not tk:
            tk = _infer_ticker_from_text(str(row.get("task") or ""))
        row["_link_ticker"] = tk
        done_tasks.append(row)
    notes = list_recent_notes(limit=220)
    draft_notes = [n for n in notes if str(n.get("status") or "").strip().lower() == "pending" and str(n.get("source_table") or "") == "investor_notes"]
    company_notes = [n for n in notes if str(n.get("ticker") or "").strip()]
    general_notes = [n for n in notes if not str(n.get("ticker") or "").strip()]
    pending_actions = list_action_queue(limit=30)
    ans, sources = recall(recall_q, limit=8) if recall_q else ("", [])
    g_status = google_status()
    events = []
    emails = []
    if g_status.get("connected") == "1":
        try:
            events = get_today_calendar_events(limit=20)
        except Exception:
            events = []
        try:
            emails = get_priority_emails(limit=8)
        except Exception:
            emails = []
    if not events:
        events = [
            {"time": "10:00", "title": "Earnings Call"},
            {"time": "14:00", "title": "Deep Work"},
        ]
    ir_suggestions = suggest_ir_emails(ticker=ir_ticker, company=ir_company, domain=ir_domain, limit=12)
    return templates.TemplateResponse(
        "components/organizer_body.html" if partial else "organizer.html",
        {
            "request": request,
            "message": message,
            "query": query,
            "day": d,
            "daily": daily,
            "open_tasks": open_tasks,
            "company_tasks": company_tasks,
            "quick_todos": quick_todos,
            "done_tasks": done_tasks,
            "notes": notes,
            "draft_notes": draft_notes,
            "company_notes": company_notes,
            "general_notes": general_notes,
            "pending_actions": pending_actions,
            "recall_query": recall_q,
            "recall_answer": ans,
            "recall_sources": sources,
            "google_status": g_status,
            "calendar_events": events,
            "priority_emails": emails,
            "email_presets": EMAIL_PRESETS,
            "email_prefill": {"to": "", "subject": "", "body": "", "ticker": ir_ticker, "company": ir_company, "domain": ir_domain},
            "ir_suggestions": ir_suggestions,
        },
    )


def _with_link_ticker(row: dict) -> dict:
    out = dict(row)
    tk = str(out.get("ticker") or "").strip().upper()
    if not tk:
        tk = _infer_ticker_from_text(str(out.get("task") or out.get("text") or ""))
    out["_link_ticker"] = tk
    return out


def _company_summary(rows: list[dict]) -> list[dict[str, object]]:
    c: Counter[str] = Counter()
    for r in rows:
        tk = str(r.get("_link_ticker") or "").strip().upper()
        if tk:
            c[tk] += 1
    return [{"ticker": tk, "count": n} for tk, n in c.most_common(24)]


def _daily_history(limit: int = 120) -> list[dict[str, str]]:
    con = connect_sqlite(str(CORE_DB_PATH), row_factory=True)
    out: list[dict[str, str]] = []
    try:
        rows = con.execute(
            "SELECT day, updated_at, locked FROM daily_notes ORDER BY day DESC LIMIT ?",
            (max(1, min(1000, int(limit))),),
        ).fetchall()
        for r in rows:
            day = str(r["day"] or "")
            tags = [
                str(x["ticker"] or "").strip().upper()
                for x in con.execute(
                    "SELECT ticker FROM daily_note_tags WHERE day = ? ORDER BY ticker ASC LIMIT 12",
                    (day,),
                ).fetchall()
                if str(x["ticker"] or "").strip()
            ]
            out.append(
                {
                    "day": day,
                    "updated_at": str(r["updated_at"] or ""),
                    "locked": "1" if int(r["locked"] or 0) == 1 else "0",
                    "tags": ", ".join(tags),
                }
            )
    finally:
        con.close()
    return out


def _task_words(text: str, limit: int = 4) -> list[str]:
    words = [w.strip().lower() for w in re.findall(r"[A-Za-z]{5,}", str(text or ""))]
    out: list[str] = []
    seen: set[str] = set()
    for w in words:
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= max(1, min(10, int(limit))):
            break
    return out


def _task_detail(todo_id: int) -> dict | None:
    if int(todo_id or 0) <= 0:
        return None
    if core_backend() == "postgres":
        rows_all = list_todos_pg(open_only=True, limit=3000) + list_todos_pg(open_only=False, limit=3000)
        row = next((r for r in rows_all if int(r.get("id") or 0) == int(todo_id)), None)
        if not row:
            return None
        task = _with_link_ticker(dict(row))
        tk = str(task.get("_link_ticker") or "").strip().upper()
        related_tasks: list[dict] = []
        if tk:
            related_tasks = [_with_link_ticker(dict(r)) for r in rows_all if int(r.get("id") or 0) != int(todo_id) and str(r.get("ticker") or "").strip().upper() == tk][:40]
        else:
            kws = _task_words(str(task.get("task") or ""), limit=3)
            if kws:
                related_tasks = [
                    _with_link_ticker(dict(r))
                    for r in rows_all
                    if int(r.get("id") or 0) != int(todo_id) and any(w in str(r.get("task") or "").lower() for w in kws)
                ][:30]
        notes_all = [_with_link_ticker(dict(n)) for n in list_recent_notes(limit=1200)]
        if tk:
            related_notes = [n for n in notes_all if str(n.get("_link_ticker") or "").strip().upper() == tk][:80]
        else:
            kws = _task_words(str(task.get("task") or ""), limit=3)
            related_notes = [n for n in notes_all if any(w in str(n.get("text") or "").lower() for w in kws)][:80] if kws else notes_all[:40]
        return {
            "task": task,
            "related_tasks": related_tasks,
            "related_notes": related_notes,
            "daily_mentions": [],
        }
    con = connect_sqlite(str(CORE_DB_PATH), row_factory=True)
    try:
        row = con.execute(
            "SELECT id, task, status, priority, due_date, ticker, category, created_at FROM todos WHERE id = ?",
            (int(todo_id),),
        ).fetchone()
        if not row:
            return None
        task = _with_link_ticker(dict(row))
        tk = str(task.get("_link_ticker") or "").strip().upper()
        related_tasks: list[dict] = []
        if tk:
            rel_rows = con.execute(
                """SELECT id, task, status, priority, due_date, ticker, category, created_at
                   FROM todos
                   WHERE id <> ? AND (UPPER(COALESCE(ticker,'')) = ?)
                   ORDER BY id DESC
                   LIMIT 40""",
                (int(todo_id), tk),
            ).fetchall()
            related_tasks = [_with_link_ticker(dict(r)) for r in rel_rows]
        else:
            kws = _task_words(str(task.get("task") or ""), limit=3)
            if kws:
                sql = (
                    "SELECT id, task, status, priority, due_date, ticker, category, created_at "
                    "FROM todos WHERE id <> ? AND ("
                    + " OR ".join(["LOWER(task) LIKE ?"] * len(kws))
                    + ") ORDER BY id DESC LIMIT 30"
                )
                vals: list[object] = [int(todo_id)] + [f"%{w}%" for w in kws]
                rel_rows = con.execute(sql, vals).fetchall()
                related_tasks = [_with_link_ticker(dict(r)) for r in rel_rows]
        notes_all = [_with_link_ticker(dict(n)) for n in list_recent_notes(limit=1200)]
        if tk:
            related_notes = [n for n in notes_all if str(n.get("_link_ticker") or "").strip().upper() == tk][:80]
        else:
            kws = _task_words(str(task.get("task") or ""), limit=3)
            if kws:
                related_notes = [n for n in notes_all if any(w in str(n.get("text") or "").lower() for w in kws)][:80]
            else:
                related_notes = notes_all[:40]
        daily_mentions: list[dict[str, str]] = []
        if tk:
            for r in con.execute(
                "SELECT day FROM daily_note_tags WHERE ticker = ? ORDER BY day DESC LIMIT 60",
                (tk,),
            ).fetchall():
                day = str(r["day"] or "")
                up = con.execute("SELECT updated_at FROM daily_notes WHERE day = ? LIMIT 1", (day,)).fetchone()
                daily_mentions.append({"day": day, "updated_at": str((up["updated_at"] if up else "") or "")})
        return {
            "task": task,
            "related_tasks": related_tasks,
            "related_notes": related_notes,
            "daily_mentions": daily_mentions,
        }
    finally:
        con.close()


@router.get("/organizer")
def organizer_page(
    request: Request,
    msg: str = "",
    q: str = "",
    day: str = "",
    rq: str = "",
    ir_to: str = "",
    ir_subject: str = "",
    ir_body: str = "",
    ir_ticker: str = "",
    ir_company: str = "",
    ir_domain: str = "",
):
    res = _ctx(
        request,
        message=msg,
        query=q,
        day=day,
        recall_q=rq,
        ir_ticker=ir_ticker,
        ir_company=ir_company,
        ir_domain=ir_domain,
    )
    try:
        res.context["email_prefill"] = {
            "to": str(ir_to or "").strip(),
            "subject": str(ir_subject or "").strip(),
            "body": str(ir_body or "").strip(),
            "ticker": str(ir_ticker or "").strip().upper(),
            "company": str(ir_company or "").strip(),
            "domain": str(ir_domain or "").strip(),
        }
    except Exception:
        pass
    return res


@router.get("/organizer/file/tasks")
def organizer_tasks_file(
    request: Request,
    day: str = "",
):
    templates = request.app.state.templates
    d = _safe_day(day)
    open_rows_raw = list_tasks(open_only=True, limit=1200)
    done_rows_raw = list_tasks(open_only=False, limit=800)
    open_rows = [_with_link_ticker(dict(r)) for r in open_rows_raw if str(dict(r).get("category") or "").strip().lower() == "company" or str(dict(r).get("ticker") or "").strip()]
    done_rows = [_with_link_ticker(dict(r)) for r in done_rows_raw if str(dict(r).get("category") or "").strip().lower() == "company" or str(dict(r).get("ticker") or "").strip()]
    companies = _company_summary(open_rows + done_rows)
    return templates.TemplateResponse(
        "organizer_tasks_file.html",
        {
            "request": request,
            "day": d,
            "open_rows": open_rows,
            "done_rows": done_rows,
            "companies": companies,
        },
    )


@router.get("/organizer/file/todos")
def organizer_todos_file(
    request: Request,
    day: str = "",
):
    templates = request.app.state.templates
    d = _safe_day(day)
    open_rows_raw = list_tasks(open_only=True, limit=1200)
    done_rows_raw = list_tasks(open_only=False, limit=800)
    open_rows = [_with_link_ticker(dict(r)) for r in open_rows_raw if str(dict(r).get("category") or "").strip().lower() == "quick"]
    done_rows = [_with_link_ticker(dict(r)) for r in done_rows_raw if str(dict(r).get("category") or "").strip().lower() == "quick"]
    companies = _company_summary(open_rows + done_rows)
    return templates.TemplateResponse(
        "organizer_todos_file.html",
        {
            "request": request,
            "day": d,
            "open_rows": open_rows,
            "done_rows": done_rows,
            "companies": companies,
        },
    )


@router.get("/organizer/file/notes")
def organizer_notes_file(
    request: Request,
    day: str = "",
    msg: str = "",
):
    templates = request.app.state.templates
    d = _safe_day(day)
    rows = [_with_link_ticker(dict(r)) for r in list_recent_notes(limit=900)]
    companies = _company_summary(rows)
    return templates.TemplateResponse(
        "organizer_notes_file.html",
        {
            "request": request,
            "day": d,
            "message": msg,
            "rows": rows,
            "companies": companies,
        },
    )


@router.get("/organizer/file/daily-log")
def organizer_daily_file(
    request: Request,
    day: str = "",
    msg: str = "",
):
    templates = request.app.state.templates
    d = _safe_day(day)
    daily = get_daily_note(d)
    history = _daily_history(limit=240)
    return templates.TemplateResponse(
        "organizer_daily_file.html",
        {
            "request": request,
            "day": d,
            "message": msg,
            "daily": daily,
            "history": history,
        },
    )


@router.get("/organizer/file/task/{todo_id}")
def organizer_task_file(
    request: Request,
    todo_id: int,
    day: str = "",
    msg: str = "",
):
    templates = request.app.state.templates
    d = _safe_day(day)
    detail = _task_detail(int(todo_id))
    if not detail:
        return RedirectResponse(
            url=f"/organizer/file/tasks?day={urllib.parse.quote(d)}",
            status_code=303,
        )
    return templates.TemplateResponse(
        "organizer_task_file.html",
        {
            "request": request,
            "day": d,
            "message": msg,
            "detail": detail,
        },
    )


@router.post("/organizer/file/task/toggle")
def organizer_task_file_toggle(
    todo_id: int = Form(...),
    day: str = Form(""),
):
    d = _safe_day(day)
    _ = toggle_task(int(todo_id))
    return RedirectResponse(
        url=f"/organizer/file/task/{int(todo_id)}?day={urllib.parse.quote(d)}&msg={urllib.parse.quote('Task updated.')}",
        status_code=303,
    )


@router.post("/organizer/file/task/delete")
def organizer_task_file_delete(
    todo_id: int = Form(...),
    day: str = Form(""),
):
    d = _safe_day(day)
    if core_backend() == "postgres":
        _ = delete_todo_pg(int(todo_id))
        return RedirectResponse(
            url=f"/organizer/file/tasks?day={urllib.parse.quote(d)}&msg={urllib.parse.quote('Task deleted.')}",
            status_code=303,
        )
    def _delete() -> None:
        con = connect_sqlite(str(CORE_DB_PATH), row_factory=True)
        try:
            con.execute("DELETE FROM todos WHERE id = ?", (int(todo_id),))
            con.commit()
        finally:
            con.close()
    sqlite_retry(_delete)
    return RedirectResponse(
        url=f"/organizer/file/tasks?day={urllib.parse.quote(d)}&msg={urllib.parse.quote('Task deleted.')}",
        status_code=303,
    )


@router.post("/organizer/file/daily-save")
def organizer_daily_file_save(
    day: str = Form(""),
    content: str = Form(""),
):
    d = _safe_day(day)
    ok = save_daily_note(d, content)
    msg = "Daily note saved." if ok else "This day is locked."
    return RedirectResponse(
        url=f"/organizer/file/daily-log?day={urllib.parse.quote(d)}&msg={urllib.parse.quote(msg)}",
        status_code=303,
    )


@router.post("/organizer/file/day-close")
def organizer_daily_file_close(
    day: str = Form(""),
    content: str = Form(""),
):
    d = _safe_day(day)
    _ = save_daily_note(d, content)
    _ = close_day(d)
    next_day = (dt.date.fromisoformat(d) + dt.timedelta(days=1)).isoformat()
    return RedirectResponse(
        url=f"/organizer/file/daily-log?day={urllib.parse.quote(next_day)}&msg={urllib.parse.quote('Day archived.')}",
        status_code=303,
    )


@router.get("/organizer/memory")
def organizer_memory_page(
    request: Request,
    msg: str = "",
    q: str = "",
    ticker: str = "",
    source: str = "",
    limit: int = 120,
):
    templates = request.app.state.templates
    rows, total, available = _memory_rows(q=q, ticker=ticker, source=source, limit=limit)
    return templates.TemplateResponse(
        "organizer_memory.html",
        {
            "request": request,
            "message": msg,
            "query": q,
            "ticker": ticker.strip().upper(),
            "source": source.strip().lower(),
            "limit": max(20, min(500, int(limit or 120))),
            "rows": rows,
            "total": total,
            "available": available,
        },
    )


@router.get("/organizer/approvals")
def organizer_approvals_page(
    request: Request,
    msg: str = "",
    day: str = "",
):
    templates = request.app.state.templates
    d = _safe_day(day)
    pending = list_action_queue(limit=400)
    logs = list_action_log(limit=200)
    pending_count = len(pending)
    avg_conf = (sum(float(x.get("confidence") or 0.0) for x in pending) / pending_count) if pending_count else 0.0
    high_risk_count = 0
    for r in pending:
        t = str(r.get("tool_name") or "").lower()
        p = str(r.get("params_json") or "").lower()
        if "delete" in t or "delete" in p or "update thesis" in p:
            high_risk_count += 1
    return templates.TemplateResponse(
        "organizer_approvals.html",
        {
            "request": request,
            "message": msg,
            "day": d,
            "pending": pending,
            "logs": logs,
            "pending_count": pending_count,
            "avg_conf": avg_conf,
            "high_risk_count": high_risk_count,
        },
    )


@router.post("/organizer/memory/delete")
def organizer_memory_delete(
    request: Request,
    memory_id: str = Form(""),
    q: str = Form(""),
    ticker: str = Form(""),
    source: str = Form(""),
    limit: int = Form(120),
):
    mem = OnyxMemory()
    removed = mem.delete_ids([memory_id]) if mem.available else 0
    msg = "Memory deleted." if removed > 0 else "Could not delete memory."
    url = (
        "/organizer/memory?"
        + urllib.parse.urlencode(
            {
                "msg": msg,
                "q": q,
                "ticker": ticker.strip().upper(),
                "source": source.strip().lower(),
                "limit": max(20, min(500, int(limit or 120))),
            }
        )
    )
    return RedirectResponse(url=url, status_code=303)


@router.post("/organizer/memory/delete-selected")
def organizer_memory_delete_selected(
    memory_ids: list[str] = Form(default=[]),
    q: str = Form(""),
    ticker: str = Form(""),
    source: str = Form(""),
    limit: int = Form(120),
):
    mem = OnyxMemory()
    removed = mem.delete_ids(memory_ids) if mem.available else 0
    msg = f"Deleted {removed} memory rows." if removed > 0 else "No memories deleted."
    url = (
        "/organizer/memory?"
        + urllib.parse.urlencode(
            {
                "msg": msg,
                "q": q,
                "ticker": ticker.strip().upper(),
                "source": source.strip().lower(),
                "limit": max(20, min(500, int(limit or 120))),
            }
        )
    )
    return RedirectResponse(url=url, status_code=303)


@router.post("/organizer/memory/delete-filtered")
def organizer_memory_delete_filtered(
    q: str = Form(""),
    ticker: str = Form(""),
    source: str = Form(""),
    limit: int = Form(120),
):
    mem = OnyxMemory()
    removed = 0
    if mem.available:
        rows, _total, _ok = _memory_rows(q=q, ticker=ticker, source=source, limit=limit)
        ids = [str(r.get("id") or "").strip() for r in rows if str(r.get("id") or "").strip()]
        removed = mem.delete_ids(ids)
    msg = f"Deleted {removed} filtered memory rows." if removed > 0 else "No filtered memories deleted."
    url = (
        "/organizer/memory?"
        + urllib.parse.urlencode(
            {
                "msg": msg,
                "q": q,
                "ticker": ticker.strip().upper(),
                "source": source.strip().lower(),
                "limit": max(20, min(500, int(limit or 120))),
            }
        )
    )
    return RedirectResponse(url=url, status_code=303)


@router.post("/organizer/daily-save")
def organizer_daily_save(
    request: Request,
    day: str = Form(""),
    content: str = Form(""),
    q: str = Form(""),
):
    d = _safe_day(day)
    ok = save_daily_note(d, content)
    msg = "Daily note saved." if ok else "This day is locked."
    if _is_hx(request):
        return _ctx(request, message=msg, query=q, day=d, partial=True)
    return RedirectResponse(url=f"/organizer?day={urllib.parse.quote(d)}&q={urllib.parse.quote(q)}&msg={urllib.parse.quote(msg)}", status_code=303)


@router.post("/organizer/day-close")
def organizer_day_close(
    request: Request,
    day: str = Form(""),
    content: str = Form(""),
    q: str = Form(""),
):
    d = _safe_day(day)
    _ = save_daily_note(d, content)
    _ = close_day(d)
    next_day = (dt.date.fromisoformat(d) + dt.timedelta(days=1)).isoformat()
    if _is_hx(request):
        return _ctx(request, message="Day archived.", query=q, day=next_day, partial=True)
    return RedirectResponse(
        url=f"/organizer?day={urllib.parse.quote(next_day)}&q={urllib.parse.quote(q)}&msg={urllib.parse.quote('Day archived.')}",
        status_code=303,
    )


@router.post("/organizer/recall")
def organizer_recall_post(
    request: Request,
    day: str = Form(""),
    q: str = Form(""),
    question: str = Form(""),
):
    d = _safe_day(day)
    if _is_hx(request):
        return _ctx(request, query=q, day=d, recall_q=question, partial=True)
    return RedirectResponse(
        url=f"/organizer?day={urllib.parse.quote(d)}&q={urllib.parse.quote(q)}&rq={urllib.parse.quote(question)}",
        status_code=303,
    )


@router.post("/organizer/todo-toggle")
def organizer_toggle_todo(
    request: Request,
    todo_id: int = Form(...),
    day: str = Form(""),
    q: str = Form(""),
):
    d = _safe_day(day)
    _ = toggle_task(todo_id)
    if _is_hx(request):
        return _ctx(request, message="Task updated.", query=q, day=d, partial=True)
    return RedirectResponse(
        url=f"/organizer?day={urllib.parse.quote(d)}&q={urllib.parse.quote(q)}&msg={urllib.parse.quote('Task updated.')}",
        status_code=303,
    )


@router.post("/organizer/task-add")
def organizer_task_add(
    request: Request,
    task: str = Form(""),
    ticker: str = Form(""),
    priority: str = Form("P2"),
    cadence: str = Form("week"),
    kind: str = Form("company"),
    q: str = Form(""),
    day: str = Form(""),
):
    txt = str(task or "").strip()
    tk = str(ticker or "").strip().upper()
    kd = str(kind or "company").strip().lower()
    if kd not in {"company", "quick"}:
        kd = "company"
    due = _due_from_cadence(cadence) if kd == "quick" else ""
    ok = add_task(task=txt, ticker=tk, category=kd, priority=priority, due_date=due)
    msg = "Task added." if ok else "Could not add task."
    d = _safe_day(day)
    if _is_hx(request):
        return _ctx(request, message=msg, query=q, day=d, partial=True)
    return RedirectResponse(
        url=f"/organizer?day={urllib.parse.quote(d)}&q={urllib.parse.quote(q)}&msg={urllib.parse.quote(msg)}",
        status_code=303,
    )


@router.post("/organizer/note-add")
def organizer_note_add(
    request: Request,
    note: str = Form(""),
    ticker: str = Form(""),
    q: str = Form(""),
    day: str = Form(""),
):
    txt = str(note or "").strip()
    tk = str(ticker or "").strip().upper()
    scope = "company_note" if tk else "organizer_note"
    ok = add_general_note(txt, scope=scope, ticker=tk, tags="organizer")
    msg = "Note saved." if ok else "Could not save note."
    d = _safe_day(day)
    if _is_hx(request):
        return _ctx(request, message=msg, query=q, day=d, partial=True)
    return RedirectResponse(
        url=f"/organizer?day={urllib.parse.quote(d)}&q={urllib.parse.quote(q)}&msg={urllib.parse.quote(msg)}",
        status_code=303,
    )


@router.post("/organizer/draft-note/keep")
def organizer_draft_note_keep(
    request: Request,
    note_id: int = Form(...),
    q: str = Form(""),
    day: str = Form(""),
    return_to: str = Form("organizer"),
):
    d = _safe_day(day)
    ok = approve_note_draft(int(note_id))
    msg = "Draft approved." if ok else "Could not approve draft."
    if _is_hx(request):
        return _ctx(request, message=msg, query=q, day=d, partial=True)
    if str(return_to or "").strip() == "notes_file":
        return RedirectResponse(
            url=f"/organizer/file/notes?day={urllib.parse.quote(d)}&msg={urllib.parse.quote(msg)}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/organizer?day={urllib.parse.quote(d)}&q={urllib.parse.quote(q)}&msg={urllib.parse.quote(msg)}",
        status_code=303,
    )


@router.post("/organizer/draft-note/discard")
def organizer_draft_note_discard(
    request: Request,
    note_id: int = Form(...),
    q: str = Form(""),
    day: str = Form(""),
    return_to: str = Form("organizer"),
):
    d = _safe_day(day)
    ok = discard_note_draft(int(note_id))
    msg = "Draft discarded." if ok else "Could not discard draft."
    if _is_hx(request):
        return _ctx(request, message=msg, query=q, day=d, partial=True)
    if str(return_to or "").strip() == "notes_file":
        return RedirectResponse(
            url=f"/organizer/file/notes?day={urllib.parse.quote(d)}&msg={urllib.parse.quote(msg)}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/organizer?day={urllib.parse.quote(d)}&q={urllib.parse.quote(q)}&msg={urllib.parse.quote(msg)}",
        status_code=303,
    )


@router.post("/organizer/action-queue/resolve")
def organizer_action_queue_resolve(
    request: Request,
    action_id: int = Form(...),
    decision: str = Form(""),
    q: str = Form(""),
    day: str = Form(""),
    return_to: str = Form("organizer"),
):
    d = _safe_day(day)
    ok = resolve_action_queue(int(action_id), decision)
    msg = "Action decision saved." if ok else "Could not update action."
    if _is_hx(request):
        return _ctx(request, message=msg, query=q, day=d, partial=True)
    if str(return_to or "").strip() == "approvals":
        return RedirectResponse(
            url=f"/organizer/approvals?day={urllib.parse.quote(d)}&msg={urllib.parse.quote(msg)}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/organizer?day={urllib.parse.quote(d)}&q={urllib.parse.quote(q)}&msg={urllib.parse.quote(msg)}",
        status_code=303,
    )


@router.get("/organizer/google/connect")
def organizer_google_connect():
    st = google_status()
    if st.get("connected") == "1":
        return RedirectResponse(url="/organizer?msg=" + urllib.parse.quote("Google already connected."), status_code=303)
    try:
        url, _state = build_connect_url()
        return RedirectResponse(url=url, status_code=303)
    except Exception as exc:
        msg = f"Google connect unavailable: {type(exc).__name__}"
        return RedirectResponse(url="/organizer?msg=" + urllib.parse.quote(msg), status_code=303)


@router.get("/organizer/google/callback")
def organizer_google_callback(code: str = "", state: str = "", error: str = ""):
    if error:
        return RedirectResponse(url="/organizer?msg=" + urllib.parse.quote(f"Google auth cancelled: {error}"), status_code=303)
    ok, msg = complete_connect(code=code, state=state)
    return RedirectResponse(url="/organizer?msg=" + urllib.parse.quote(msg), status_code=303)


@router.post("/organizer/google/disconnect")
def organizer_google_disconnect():
    disconnect_google()
    return RedirectResponse(url="/organizer?msg=" + urllib.parse.quote("Google disconnected."), status_code=303)


@router.post("/organizer/google/send-email")
def organizer_google_send_email(
    to_email: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
    confirm_send: str = Form(""),
):
    if str(confirm_send or "").strip() != "1":
        return RedirectResponse(url="/organizer?msg=" + urllib.parse.quote("Send cancelled (not confirmed)."), status_code=303)
    ok, msg = send_email(to_email=to_email, subject=subject, body=body)
    if ok:
        add_general_note(
            f"Email sent to {to_email.strip()}\nSubject: {subject.strip()}\n\n{body.strip()[:1200]}",
            scope="email",
            tags="email,sent",
        )
    return RedirectResponse(url="/organizer?msg=" + urllib.parse.quote(msg), status_code=303)
