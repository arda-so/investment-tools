from __future__ import annotations

import datetime as dt
import re
import urllib.parse

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from app.core.date import parse_datetime_flexible
from app.core.proposal_text import clean_task_text, is_ai_task_text, is_system_log_text
from app.core.ticker_infer import infer_ticker_explicit
from app.services.memory_engine import OnyxMemory
from app.services.postgres_core_service import (
    delete_todo_pg,
    delete_workspace_journal_note_pg,
    list_todos_pg,
    set_task_company_pg,
    update_todo_pg,
    update_workspace_journal_note_pg,
)
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
    snooze_task,
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


def _infer_ticker_from_text(text: str) -> str:
    return infer_ticker_explicit(text, max_len=5)


def _safe_day(day: str) -> str:
    d = str(day or "").strip()
    try:
        return dt.date.fromisoformat(d).isoformat()
    except Exception:
        return dt.date.today().isoformat()


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
    show_ai: bool = False,
):
    templates = request.app.state.templates
    d = _safe_day(day)
    daily = get_daily_note(d)
    open_tasks_raw = list_tasks(open_only=True, limit=500)
    done_tasks_raw = list_tasks(open_only=False, limit=120)
    open_tasks: list[dict[str, object]] = []
    company_tasks: list[dict[str, object]] = []
    inbox_tasks: list[dict[str, object]] = []
    inbox_notes: list[dict[str, object]] = []
    done_tasks: list[dict[str, object]] = []
    for r in open_tasks_raw:
        row = dict(r)
        tk = str(row.get("ticker") or "").strip().upper()
        if not tk:
            tk = _infer_ticker_from_text(str(row.get("task") or ""))
        row["_link_ticker"] = tk
        open_tasks.append(row)
        cat = str(row.get("category") or "").strip().lower()
        if not tk:
            inbox_tasks.append(row)
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
    human_notes = [n for n in notes if not _is_system_log_note(n)]
    system_logs = [n for n in notes if _is_system_log_note(n)]
    timeline_events: list[dict[str, object]] = []
    draft_notes = [
        n
        for n in notes
        if str(n.get("status") or "").strip().lower() == "pending"
        and str(n.get("source_table") or "") == "investor_notes"
    ]
    company_notes = [n for n in human_notes if str(n.get("ticker") or "").strip()]
    general_notes = [n for n in human_notes if not str(n.get("ticker") or "").strip()]
    inbox_notes = list(general_notes)
    inbox_events: list[dict[str, object]] = []
    for r in inbox_tasks:
        inbox_events.append(
            {
                "kind": "task",
                "id": int(r.get("id") or 0),
                "title": clean_task_text(str(r.get("task") or "Task"))[:140],
                "created_at": str(r.get("created_at") or ""),
                "ticker": str(r.get("_link_ticker") or ""),
                "source_table": "todos",
                "due_date": str(r.get("due_date") or ""),
                "priority": str(r.get("priority") or "P2"),
            }
        )
    for n in inbox_notes:
        inbox_events.append(
            {
                "kind": "note",
                "id": int(n.get("id") or 0),
                "title": str(n.get("text") or "")[:180],
                "created_at": str(n.get("date") or ""),
                "ticker": str(n.get("ticker") or "").strip().upper(),
                "source_table": str(n.get("source_table") or "investor_notes"),
            }
        )
    inbox_events.sort(
        key=lambda e: parse_datetime_flexible(str(e.get("created_at") or "")) or dt.datetime.min,
        reverse=True,
    )
    for r in open_tasks:
        is_ai_task = _is_ai_task_row(r)
        title = clean_task_text(str(r.get("task") or "Task"))[:140]
        timeline_events.append(
            {
                "kind": "log" if is_ai_task else "task",
                "icon": "🤖" if is_ai_task else "✅",
                "created_at": str(r.get("created_at") or ""),
                "is_ai": is_ai_task,
                "ticker": str(r.get("_link_ticker") or ""),
                "title": title or "Task",
                "id": int(r.get("id") or 0),
                "source_table": "todos",
                "text": (
                    f"AI follow-up • {str(r.get('status') or 'open')}"
                    if is_ai_task
                    else f"Task • {str(r.get('status') or 'open')}"
                ),
            }
        )
    for r in done_tasks:
        is_ai_task = _is_ai_task_row(r)
        title = clean_task_text(str(r.get("task") or "Task"))[:140]
        timeline_events.append(
            {
                "kind": "log" if is_ai_task else "task",
                "icon": "🤖" if is_ai_task else "✅",
                "created_at": str(r.get("created_at") or ""),
                "is_ai": is_ai_task,
                "ticker": str(r.get("_link_ticker") or ""),
                "title": title or "Task",
                "id": int(r.get("id") or 0),
                "source_table": "todos",
                "text": (
                    f"AI follow-up • {str(r.get('status') or 'done')}"
                    if is_ai_task
                    else f"Task • {str(r.get('status') or 'done')}"
                ),
            }
        )
    for n in human_notes:
        timeline_events.append(
            {
                "kind": "note",
                "icon": "📝",
                "created_at": str(n.get("date") or ""),
                "is_ai": False,
                "ticker": str(n.get("_link_ticker") or ""),
                "title": str(n.get("tag") or "Note"),
                "text": str(n.get("text") or ""),
                "id": int(n.get("id") or 0),
                "source_table": str(n.get("source_table") or "investor_notes"),
            }
        )
    for n in system_logs:
        timeline_events.append(
            {
                "kind": "log",
                "icon": "🤖",
                "created_at": str(n.get("date") or ""),
                "is_ai": True,
                "ticker": str(n.get("_link_ticker") or ""),
                "title": str(n.get("tag") or "System"),
                "text": str(n.get("text") or ""),
                "id": int(n.get("id") or 0),
                "source_table": str(n.get("source_table") or "investor_notes"),
            }
        )
    timeline_events.sort(
        key=lambda e: parse_datetime_flexible(str(e.get("created_at") or "")) or dt.datetime.min,
        reverse=True,
    )
    qn = str(query or "").strip().lower()
    if qn:
        toks = [t for t in re.split(r"\s+", qn) if t]

        def _hit(*parts: object) -> bool:
            txt = " ".join(str(x or "") for x in parts).strip().lower()
            if not txt:
                return False
            if qn in txt:
                return True
            return bool(toks) and all(t in txt for t in toks)

        open_tasks = [r for r in open_tasks if _hit(r.get("task"), r.get("_link_ticker"), r.get("status"), r.get("priority"))]
        done_tasks = [r for r in done_tasks if _hit(r.get("task"), r.get("_link_ticker"), r.get("status"), r.get("priority"))]
        company_tasks = [r for r in company_tasks if _hit(r.get("task"), r.get("_link_ticker"), r.get("status"), r.get("priority"))]
        inbox_tasks = [r for r in inbox_tasks if _hit(r.get("task"), r.get("_link_ticker"), r.get("status"), r.get("priority"))]
        human_notes = [n for n in human_notes if _hit(n.get("text"), n.get("ticker"), n.get("tag"), n.get("kind"))]
        system_logs = [n for n in system_logs if _hit(n.get("text"), n.get("ticker"), n.get("tag"), n.get("kind"))]
        notes = [n for n in notes if _hit(n.get("text"), n.get("ticker"), n.get("tag"), n.get("kind"))]
        draft_notes = [n for n in draft_notes if _hit(n.get("text"), n.get("ticker"), n.get("tag"), n.get("kind"))]
        company_notes = [n for n in company_notes if _hit(n.get("text"), n.get("ticker"), n.get("tag"), n.get("kind"))]
        general_notes = [n for n in general_notes if _hit(n.get("text"), n.get("ticker"), n.get("tag"), n.get("kind"))]
        inbox_notes = [n for n in inbox_notes if _hit(n.get("text"), n.get("ticker"), n.get("tag"), n.get("kind"))]
        inbox_events = [e for e in inbox_events if _hit(e.get("title"), e.get("ticker"), e.get("kind"), e.get("created_at"))]
        timeline_events = [
            e
            for e in timeline_events
            if _hit(e.get("title"), e.get("text"), e.get("ticker"), e.get("kind"), e.get("created_at"))
        ]

    timeline_events_visible = timeline_events if bool(show_ai) else [e for e in timeline_events if not bool(e.get("is_ai"))]
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
            "inbox_tasks": inbox_tasks,
            "inbox_notes": inbox_notes,
            "inbox_events": inbox_events,
            "done_tasks": done_tasks,
            "notes": notes,
            "human_notes": human_notes,
            "system_logs": system_logs,
            "draft_notes": draft_notes,
            "company_notes": company_notes,
            "general_notes": general_notes,
            "timeline_events": timeline_events,
            "timeline_events_visible": timeline_events_visible,
            "show_ai": "1" if bool(show_ai) else "0",
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


def _is_system_log_note(row: dict[str, object]) -> bool:
    return is_system_log_text(
        tag=str(row.get("tag") or ""),
        text=str(row.get("text") or ""),
        title=str(row.get("title") or ""),
        created_by=str(row.get("created_by") or "human"),
    )


def _is_ai_task_row(row: dict[str, object]) -> bool:
    return is_ai_task_text(
        task=str(row.get("task") or ""),
        category=str(row.get("category") or ""),
    )


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
    show_ai: int = 0,
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
        show_ai=int(show_ai or 0) == 1,
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


@router.post("/organizer/task-snooze")
def organizer_task_snooze(
    request: Request,
    todo_id: int = Form(...),
    snooze_until: str = Form(""),
    day: str = Form(""),
    q: str = Form(""),
):
    d = _safe_day(day)
    ok = snooze_task(int(todo_id), str(snooze_until or "").strip())
    msg = "Task snoozed." if ok else "Could not snooze task."
    if _is_hx(request):
        return _ctx(request, message=msg, query=q, day=d, partial=True)
    return RedirectResponse(
        url=f"/organizer?day={urllib.parse.quote(d)}&q={urllib.parse.quote(q)}&msg={urllib.parse.quote(msg)}",
        status_code=303,
    )


@router.post("/organizer/inbox/task-update")
def organizer_inbox_task_update(
    request: Request,
    todo_id: int = Form(...),
    ticker: str = Form(""),
    due_date: str = Form(""),
    day: str = Form(""),
    q: str = Form(""),
):
    d = _safe_day(day)
    rid = int(todo_id or 0)
    tk = str(ticker or "").strip().upper()
    dd = str(due_date or "").strip()
    ok = False
    if rid > 0:
        rows = list_todos_pg(open_only=True, limit=3000) + list_todos_pg(open_only=False, limit=3000)
        row = next((r for r in rows if int(r.get("id") or 0) == rid), None)
        if row is not None:
            ok = set_task_company_pg(rid, ticker=tk, due_date=dd)
    msg = "Inbox task updated." if ok else "Could not update inbox task."
    if _is_hx(request):
        return _ctx(request, message=msg, query=q, day=d, partial=True)
    return RedirectResponse(
        url=f"/organizer?day={urllib.parse.quote(d)}&q={urllib.parse.quote(q)}&msg={urllib.parse.quote(msg)}",
        status_code=303,
    )


@router.post("/organizer/task-add")
def organizer_task_add(
    request: Request,
    task: str = Form(""),
    ticker: str = Form(""),
    priority: str = Form("P2"),
    kind: str = Form("general"),
    q: str = Form(""),
    day: str = Form(""),
):
    txt = str(task or "").strip()
    tk = str(ticker or "").strip().upper()
    kd = str(kind or "general").strip().lower()
    if kd not in {"company", "general"}:
        kd = "general"
    ok = add_task(task=txt, ticker=tk, category=kd, priority=priority, due_date="")
    msg = "Task added." if ok else "Could not add task."
    d = _safe_day(day)
    if _is_hx(request):
        return _ctx(request, message=msg, query=q, day=d, partial=True)
    return RedirectResponse(
        url=f"/organizer?day={urllib.parse.quote(d)}&q={urllib.parse.quote(q)}&msg={urllib.parse.quote(msg)}",
        status_code=303,
    )


@router.post("/organizer/omnibox-add")
def organizer_omnibox_add(
    request: Request,
    entry: str = Form(""),
    q: str = Form(""),
    day: str = Form(""),
):
    d = _safe_day(day)
    raw = str(entry or "").strip()
    if not raw:
        msg = "Empty input."
        if _is_hx(request):
            return _ctx(request, message=msg, query=q, day=d, partial=True)
        return RedirectResponse(
            url=f"/organizer?day={urllib.parse.quote(d)}&q={urllib.parse.quote(q)}&msg={urllib.parse.quote(msg)}",
            status_code=303,
        )
    m = re.search(r"\$([A-Za-z]{1,5})\b", raw)
    tk = str(m.group(1) if m else "").strip().upper()
    force_note = bool(re.search(r"(^|\\s)#note\\b", raw, flags=re.I))
    clean = re.sub(r"(^|\\s)#task\\b", " ", raw, flags=re.I)
    clean = re.sub(r"(^|\\s)#note\\b", " ", clean, flags=re.I)
    clean = re.sub(r"\\s+", " ", clean).strip()
    ok = False
    if force_note:
        scope = "company_note" if tk else "organizer_note"
        ok = add_general_note(clean or raw, scope=scope, ticker=tk, tags="organizer")
        msg = f"Note saved to {tk} workspace." if (ok and tk) else ("Note saved to Inbox." if ok else "Could not save note.")
    else:
        kind = "company" if tk else "general"
        ok = add_task(task=clean or raw, ticker=tk, category=kind, priority="P2", due_date="")
        msg = f"Task added to {tk} workspace." if (ok and tk) else ("Task added to Inbox." if ok else "Could not add task.")
    if _is_hx(request):
        return _ctx(request, message=msg, query=q, day=d, partial=True)
    return RedirectResponse(
        url=f"/organizer?day={urllib.parse.quote(d)}&q={urllib.parse.quote(q)}&msg={urllib.parse.quote(msg)}",
        status_code=303,
    )


@router.post("/organizer/item-delete")
@router.post("/organizer/item-delete/")
def organizer_item_delete(
    request: Request,
    item_id: int = Form(...),
    kind: str = Form(""),
    source_table: str = Form(""),
    q: str = Form(""),
    day: str = Form(""),
):
    d = _safe_day(day)
    rid = int(item_id or 0)
    k = str(kind or "").strip().lower()
    st = str(source_table or "").strip().lower()
    ok = False
    if rid > 0 and k == "task":
        ok = delete_todo_pg(rid)
    elif rid > 0 and k in {"note", "log"}:
        ok = delete_workspace_journal_note_pg(rid)
    msg = "Deleted." if ok else "Could not delete."
    if _is_hx(request):
        return _ctx(request, message=msg, query=q, day=d, partial=True)
    return RedirectResponse(
        url=f"/organizer?day={urllib.parse.quote(d)}&q={urllib.parse.quote(q)}&msg={urllib.parse.quote(msg)}",
        status_code=303,
    )


@router.post("/organizer/item-edit")
@router.post("/organizer/item-edit/")
def organizer_item_edit(
    request: Request,
    item_id: int = Form(...),
    kind: str = Form(""),
    source_table: str = Form(""),
    new_text: str = Form(""),
    q: str = Form(""),
    day: str = Form(""),
):
    d = _safe_day(day)
    rid = int(item_id or 0)
    k = str(kind or "").strip().lower()
    st = str(source_table or "").strip().lower()
    txt = str(new_text or "").strip()
    ok = False
    if rid > 0 and txt:
        if k == "task":
            ok = update_todo_pg(rid, task=txt, due_date="", priority="")
        elif k in {"note", "log"}:
            ok = update_workspace_journal_note_pg(rid, txt)
    msg = "Updated." if ok else "Could not update."
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
            url=f"/organizer?day={urllib.parse.quote(d)}&q={urllib.parse.quote(q)}&msg={urllib.parse.quote(msg)}&show_ai=1",
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
            url=f"/organizer?day={urllib.parse.quote(d)}&q={urllib.parse.quote(q)}&msg={urllib.parse.quote(msg)}&show_ai=1",
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
