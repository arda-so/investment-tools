"""organizer.py — POST action endpoints only.

GET /organizer redirects to /day (the unified Organizer surface).
All POST sub-routes remain for backwards compatibility with quick-capture,
existing data mutations, memory management, and Google OAuth.
"""
from __future__ import annotations

import datetime as dt
import functools
import re
import urllib.parse

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
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
    google_status,
    send_email,
)
from app.services.organizer_service import (
    approve_note_draft,
    add_task,
    add_general_note,
    close_day,
    discard_note_draft,
    list_action_log,
    list_action_queue,
    list_recent_notes,
    recall,
    resolve_action_queue,
    save_daily_note,
    toggle_task,
    snooze_task,
)


router = APIRouter()


@functools.lru_cache(maxsize=1)
def _workspace_create_record_fn():
    # Lazy-load once to avoid request-path repeated import locking while staying cycle-safe.
    from app.services.workspace_os_service import create_record

    return create_record


def _safe_day(day: str) -> str:
    d = str(day or "").strip()
    try:
        return dt.date.fromisoformat(d).isoformat()
    except Exception:
        return dt.date.today().isoformat()


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


# ── GET /organizer → /day ─────────────────────────────────────────────────────

@router.get("/organizer")
@router.get("/organizer/")
def organizer_page(request: Request):
    return RedirectResponse(url="/day", status_code=302)


# ── Sub-pages (kept as standalone views) ─────────────────────────────────────

@router.get("/organizer/notes-export")
def organizer_notes_export(limit: int = 500, include_system: int = 1):
    from app.core.proposal_text import is_system_log_text
    lim = max(1, min(int(limit or 500), 5000))
    rows = list_recent_notes(limit=lim)
    if int(include_system or 1) == 0:
        rows = [r for r in rows if not is_system_log_text(str(r.get("text") or "") + str(r.get("tag") or ""))]
    return JSONResponse({"ok": True, "count": len(rows), "items": rows})


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
    return templates.TemplateResponse(
        "organizer_approvals.html",
        {
            "request": request,
            "message": msg,
            "day": d,
            "pending": pending,
            "pending_count": len(pending),
            "logs": logs,
        },
    )


# ── Memory management ─────────────────────────────────────────────────────────

@router.post("/organizer/memory/delete")
def organizer_memory_delete(
    memory_id: str = Form(""),
    q: str = Form(""),
    ticker: str = Form(""),
    source: str = Form(""),
    limit: int = Form(120),
):
    mem = OnyxMemory()
    removed = mem.delete_ids([memory_id]) if mem.available else 0
    msg = "Memory deleted." if removed > 0 else "Could not delete memory."
    url = "/organizer/memory?" + urllib.parse.urlencode({
        "msg": msg, "q": q,
        "ticker": ticker.strip().upper(),
        "source": source.strip().lower(),
        "limit": max(20, min(500, int(limit or 120))),
    })
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
    url = "/organizer/memory?" + urllib.parse.urlencode({
        "msg": msg, "q": q,
        "ticker": ticker.strip().upper(),
        "source": source.strip().lower(),
        "limit": max(20, min(500, int(limit or 120))),
    })
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
    url = "/organizer/memory?" + urllib.parse.urlencode({
        "msg": msg, "q": q,
        "ticker": ticker.strip().upper(),
        "source": source.strip().lower(),
        "limit": max(20, min(500, int(limit or 120))),
    })
    return RedirectResponse(url=url, status_code=303)


# ── Daily note / day close ────────────────────────────────────────────────────

@router.post("/organizer/daily-save")
def organizer_daily_save(day: str = Form(""), content: str = Form("")):
    save_daily_note(_safe_day(day), content)
    return RedirectResponse(url="/day", status_code=303)


@router.post("/organizer/day-close")
def organizer_day_close(day: str = Form(""), content: str = Form("")):
    d = _safe_day(day)
    save_daily_note(d, content)
    close_day(d)
    return RedirectResponse(url="/day", status_code=303)


# ── Task actions ──────────────────────────────────────────────────────────────

@router.post("/organizer/todo-toggle")
def organizer_toggle_todo(todo_id: int = Form(...)):
    toggle_task(todo_id)
    return JSONResponse({"ok": True})


@router.post("/organizer/task-snooze")
def organizer_task_snooze(todo_id: int = Form(...), snooze_until: str = Form("")):
    ok = snooze_task(int(todo_id), str(snooze_until or "").strip())
    return JSONResponse({"ok": ok})


@router.post("/organizer/inbox/task-update")
def organizer_inbox_task_update(
    todo_id: int = Form(...),
    ticker: str = Form(""),
    due_date: str = Form(""),
):
    rid = int(todo_id or 0)
    tk = str(ticker or "").strip().upper()
    dd = str(due_date or "").strip()
    ok = set_task_company_pg(rid, ticker=tk, due_date=dd) if rid > 0 else False
    return JSONResponse({"ok": ok})


@router.post("/organizer/task-add")
def organizer_task_add(
    task: str = Form(""),
    ticker: str = Form(""),
    priority: str = Form("P2"),
    kind: str = Form("general"),
):
    txt = str(task or "").strip()
    tk = str(ticker or "").strip().upper()
    kd = str(kind or "general").strip().lower()
    if kd not in {"company", "general"}:
        kd = "general"
    ok = add_task(task=txt, ticker=tk, category=kd, priority=priority, due_date="")
    return JSONResponse({"ok": ok})


@router.post("/organizer/omnibox-add")
def organizer_omnibox_add(
    request: Request,
    entry: str = Form(""),
    q: str = Form(""),
    day: str = Form(""),
):
    raw = str(entry or "").strip()
    if not raw:
        return RedirectResponse(url="/day", status_code=303)
    m = re.search(r"\$([A-Za-z]{1,5})\b", raw)
    tk = str(m.group(1) if m else "").strip().upper()
    force_note = bool(re.search(r"(^|\s)#note\b", raw, flags=re.I))
    clean = re.sub(r"(^|\s)#task\b", " ", raw, flags=re.I)
    clean = re.sub(r"(^|\s)#note\b", " ", clean, flags=re.I)
    clean = re.sub(r"\s+", " ", clean).strip()
    if force_note:
        scope = "company_note" if tk else "organizer_note"
        add_general_note(clean or raw, scope=scope, ticker=tk, tags="organizer")
    else:
        kind = "company" if tk else "general"
        add_task(task=clean or raw, ticker=tk, category=kind, priority="P2", due_date="")
    # Dual-write to unified investment_records_core
    try:
        _ws_create = _workspace_create_record_fn()
        _ws_create(
            kind="action",
            domain="work",
            title=clean or raw,
            ticker=tk,
            source="manual",
            created_by="user",
        )
    except Exception:
        pass
    return RedirectResponse(url="/day", status_code=303)


@router.post("/organizer/item-delete")
@router.post("/organizer/item-delete/")
def organizer_item_delete(
    item_id: int = Form(...),
    kind: str = Form(""),
    source_table: str = Form(""),
):
    rid = int(item_id or 0)
    k = str(kind or "").strip().lower()
    ok = False
    if rid > 0 and k == "task":
        ok = delete_todo_pg(rid)
    elif rid > 0 and k in {"note", "log"}:
        ok = delete_workspace_journal_note_pg(rid)
    return JSONResponse({"ok": ok})


@router.post("/organizer/item-edit")
@router.post("/organizer/item-edit/")
def organizer_item_edit(
    item_id: int = Form(...),
    kind: str = Form(""),
    new_text: str = Form(""),
):
    rid = int(item_id or 0)
    k = str(kind or "").strip().lower()
    txt = str(new_text or "").strip()
    ok = False
    if rid > 0 and txt:
        if k == "task":
            ok = update_todo_pg(rid, task=txt, due_date="", priority="")
        elif k in {"note", "log"}:
            ok = update_workspace_journal_note_pg(rid, txt)
    return JSONResponse({"ok": ok})


@router.post("/organizer/note-add")
def organizer_note_add(note: str = Form(""), ticker: str = Form("")):
    txt = str(note or "").strip()
    tk = str(ticker or "").strip().upper()
    scope = "company_note" if tk else "organizer_note"
    ok = add_general_note(txt, scope=scope, ticker=tk, tags="organizer")
    return JSONResponse({"ok": ok})


# ── Draft notes ───────────────────────────────────────────────────────────────

@router.post("/organizer/draft-note/keep")
def organizer_draft_note_keep(note_id: int = Form(...)):
    ok = approve_note_draft(int(note_id))
    return JSONResponse({"ok": ok})


@router.post("/organizer/draft-note/discard")
def organizer_draft_note_discard(note_id: int = Form(...)):
    ok = discard_note_draft(int(note_id))
    return JSONResponse({"ok": ok})


# ── Action queue ──────────────────────────────────────────────────────────────

@router.post("/organizer/action-queue/resolve")
def organizer_action_queue_resolve(
    action_id: int = Form(...),
    decision: str = Form(""),
    return_to: str = Form("organizer"),
):
    ok = resolve_action_queue(int(action_id), decision)
    if str(return_to or "").strip() == "approvals":
        return RedirectResponse(url="/organizer/approvals", status_code=303)
    return JSONResponse({"ok": ok})


# ── Google OAuth ──────────────────────────────────────────────────────────────

@router.get("/organizer/google/connect")
def organizer_google_connect():
    st = google_status()
    if st.get("connected") == "1":
        return RedirectResponse(url="/day", status_code=303)
    try:
        url, _state = build_connect_url()
        return RedirectResponse(url=url, status_code=303)
    except Exception as exc:
        return RedirectResponse(url="/day", status_code=303)


@router.get("/organizer/google/callback")
def organizer_google_callback(code: str = "", state: str = "", error: str = ""):
    complete_connect(code=code, state=state)
    return RedirectResponse(url="/day", status_code=303)


@router.post("/organizer/google/disconnect")
def organizer_google_disconnect():
    disconnect_google()
    return RedirectResponse(url="/day", status_code=303)


@router.post("/organizer/google/send-email")
def organizer_google_send_email(
    to_email: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
    confirm_send: str = Form(""),
):
    if str(confirm_send or "").strip() != "1":
        return RedirectResponse(url="/day", status_code=303)
    ok, msg = send_email(to_email=to_email, subject=subject, body=body)
    if ok:
        add_general_note(
            f"Email sent to {to_email.strip()}\nSubject: {subject.strip()}\n\n{body.strip()[:1200]}",
            scope="email",
            tags="email,sent",
        )
    return RedirectResponse(url="/day", status_code=303)


# ── Recall (kept for any external callers) ────────────────────────────────────

@router.post("/organizer/recall")
def organizer_recall_post(question: str = Form("")):
    ans, sources = recall(question, limit=8) if question else ("", [])
    return JSONResponse({"answer": ans, "sources": sources})
