from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
import csv
import io

from app.core import cloud_files
from app.core.normalize import normalize_text as _norm
from app.services.company_lookup_service import company_name_map
from app.services.postgres_core_service import (
    approve_investor_note_draft_pg,
    add_investor_note_pg,
    add_todo_pg,
    close_day_pg,
    delete_notes_pg,
    discard_investor_note_draft_pg,
    enqueue_action_pg,
    get_daily_note_pg,
    list_action_log_pg,
    list_action_queue_pg,
    list_recent_notes_pg,
    list_todos_pg,
    resolve_action_queue_pg,
    save_daily_note_pg,
    snooze_todo_pg,
    toggle_todo_pg,
    update_todo_status_pg,
)

ENTITY_ALIASES = {
    "google": "GOOGL",
    "alphabet": "GOOGL",
    "facebook": "META",
    "meta": "META",
    "tesla": "TSLA",
    "amazon": "AMZN",
    "apple": "AAPL",
    "microsoft": "MSFT",
    "netflix": "NFLX",
    "nvidia": "NVDA",
}
INVALID_TICKER_WORDS = {
    "BUY", "SELL", "HOLD", "WATCH", "READ", "WRITE", "ADD", "NOTE", "TASK",
    "OPEN", "SHOW", "MY", "ME", "LIST", "UPDATE", "DELETE", "REPORT",
}


def ensure_schema() -> None:
    # Postgres-only runtime: schema managed in postgres_core_service.
    return


def _extract_tickers(text: str, limit: int = 50) -> list[str]:
    found = re.findall(r"\$([A-Za-z]{1,6})\b|\b([A-Z]{2,5})\b", text or "")
    out: list[str] = []
    seen: set[str] = set()
    stop = {"THE", "AND", "FOR", "WITH", "THIS", "THAT", "FROM", "NOTE", "IDEA", "BOOK"}
    for pair in found:
        raw = (pair[0] or pair[1] or "").upper().strip()
        if not raw or raw in stop or raw in seen:
            continue
        seen.add(raw)
        out.append(raw)
        if len(out) >= max(1, min(200, int(limit))):
            break
    return out


def _is_known_ticker(ticker: str) -> bool:
    tk = str(ticker or "").strip().upper()
    if not tk:
        return False
    if tk in set(ENTITY_ALIASES.values()):
        return True
    try:
        return bool(company_name_map([tk]).get(tk))
    except Exception:
        return False


def _resolve_ticker_from_text(text: str, current_ticker: str = "") -> str:
    s = str(text or "")
    if not s:
        return ""
    # Strict policy for organizer capture: only explicit $TICKER binds scope.
    # This prevents accidental links from natural language words.
    m = re.search(r"\$([A-Za-z]{1,5})\b", s)
    if m:
        tk = str(m.group(1) or "").strip().upper()
        if tk and tk not in INVALID_TICKER_WORDS and _is_known_ticker(tk):
            return tk
    return ""


@dataclass
class DailyNoteView:
    day: str
    content: str
    updated_at: str
    locked: bool
    archived_at: str
    tags: list[str]


def get_daily_note(day: str) -> DailyNoteView:
    d = str(day or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        d = dt.date.today().isoformat()
    obj = get_daily_note_pg(d)
    return DailyNoteView(
        day=str(obj.get("day") or d),
        content=str(obj.get("content") or ""),
        updated_at=str(obj.get("updated_at") or ""),
        locked=bool(obj.get("locked") or False),
        archived_at=str(obj.get("archived_at") or ""),
        tags=[str(x or "").strip().upper() for x in list(obj.get("tags") or []) if str(x or "").strip()],
    )


def save_daily_note(day: str, content: str) -> bool:
    d = str(day or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        d = dt.date.today().isoformat()
    txt = str(content or "").strip()
    ok = save_daily_note_pg(d, txt, tags=_extract_tickers(txt, limit=60))
    if ok:
        try:
            from app.services.agent_service import memorize_user_note

            memorize_user_note(
                txt[:120000],
                ticker="",
                source_type="daily_note",
                source_id=f"daily_notes:{d}",
            )
        except Exception:
            pass
    return ok


def close_day(day: str) -> bool:
    d = str(day or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        return False
    return close_day_pg(d)


def list_tasks(open_only: bool = True, limit: int = 400) -> list[dict[str, object]]:
    return list_todos_pg(open_only=open_only, limit=limit)


def toggle_task(todo_id: int) -> bool:
    if todo_id <= 0:
        return False
    return toggle_todo_pg(todo_id)


def complete_task(todo_id: int) -> bool:
    rid = int(todo_id or 0)
    if rid <= 0:
        return False
    rows = list_todos_pg(open_only=False, limit=2000)
    row = next((r for r in rows if int(r.get("id") or 0) == rid), None)
    if row is None:
        return False
    status = str(row.get("status") or "open").strip().lower()
    if status in {"done", "archived"}:
        return True
    return update_todo_status_pg(rid, "done")


def complete_task_by_text(task_text: str) -> dict[str, object]:
    txt = str(task_text or "").strip()
    if not txt:
        return {"ok": False, "error": "task_text_required"}
    rows = list_todos_pg(open_only=False, limit=2000)
    row = next((r for r in rows if str(r.get("task") or "").strip().lower() == txt.lower()), None)
    if row is None:
        row = next(
            (
                r
                for r in rows
                if str(r.get("status") or "").strip().lower() == "open"
                and txt.lower() in str(r.get("task") or "").lower()
            ),
            None,
        )
    if not row:
        return {"ok": False, "error": "not_found"}
    rid = int(row.get("id") or 0)
    status = str(row.get("status") or "open").strip().lower()
    if status not in {"done", "archived"}:
        status = "done"
        _ = update_todo_status_pg(rid, status)
    return {"ok": True, "id": rid, "task": str(row.get("task") or ""), "status": status}


def list_recent_notes(limit: int = 200) -> list[dict[str, str]]:
    return list_recent_notes_pg(limit=limit)


def add_general_note(
    note: str,
    *,
    scope: str = "organizer",
    ticker: str = "",
    tags: str = "log",
    status: str = "approved",
    created_by: str = "human",
    ai_confidence: float = 0.0,
    ai_reasoning: str = "",
    trace_id: str = "",
) -> bool:
    txt = str(note or "").strip()
    if not txt:
        return False
    st = str(status or "approved").strip().lower()
    if st not in {"pending", "approved", "rejected"}:
        st = "approved"
    cb = str(created_by or "human").strip().lower()
    if cb not in {"human", "ai"}:
        cb = "human"
    now = dt.datetime.now().isoformat()
    inferred_ticker = str(ticker or "").strip().upper()[:16]
    if not inferred_ticker:
        inferred_ticker = _resolve_ticker_from_text(txt)
    return add_investor_note_pg(
        scope=str(scope or "organizer")[:40],
        ticker=inferred_ticker,
        sentiment="neutral",
        note=txt[:4000],
        tags=str(tags or "log")[:200],
        status=st,
        created_by=cb,
        ai_confidence=float(ai_confidence or 0.0),
        ai_reasoning=str(ai_reasoning or "")[:3000],
        trace_id=str(trace_id or "")[:120],
    )


def approve_note_draft(note_id: int) -> bool:
    rid = int(note_id or 0)
    if rid <= 0:
        return False
    # Draft normalization currently optional in pg path; keep ticker untouched.
    return approve_investor_note_draft_pg(rid)


def discard_note_draft(note_id: int) -> bool:
    rid = int(note_id or 0)
    if rid <= 0:
        return False
    return discard_investor_note_draft_pg(rid)


def delete_notes(
    *,
    ticker: str = "",
    text_contains: str = "",
    created_by: str = "",
    include_company_journal: bool = True,
) -> dict[str, int]:
    tk = str(ticker or "").strip().upper()[:16]
    txt = str(text_contains or "").strip()
    cb = str(created_by or "").strip().lower()
    if cb not in {"", "ai", "human"}:
        cb = ""
    return delete_notes_pg(
        ticker=tk,
        text_contains=txt,
        created_by=cb,
        include_company_journal=include_company_journal,
    )


def enqueue_action(
    tool_name: str,
    params_json: str,
    reasoning: str,
    confidence: float,
    trace_id: str = "",
) -> bool:
    nm = str(tool_name or "").strip()
    if not nm:
        return False
    return enqueue_action_pg(nm, params_json, reasoning, confidence, trace_id)


def list_action_queue(limit: int = 120) -> list[dict[str, str]]:
    return list_action_queue_pg(limit=limit)


def list_action_log(limit: int = 120) -> list[dict[str, str]]:
    return list_action_log_pg(limit=limit)


def resolve_action_queue(action_id: int, decision: str) -> bool:
    rid = int(action_id or 0)
    dec = str(decision or "").strip().lower()
    if rid <= 0 or dec not in {"approved", "rejected"}:
        return False
    return resolve_action_queue_pg(rid, dec)


def add_task(
    task: str,
    *,
    ticker: str = "",
    category: str = "general",
    priority: str = "P2",
    due_date: str = "",
) -> bool:
    txt = str(task or "").strip()
    if not txt:
        return False
    tk = str(ticker or "").strip().upper()[:16]
    if not tk:
        m = re.search(r"\$([A-Za-z]{1,5})\b", txt)
        if not m:
            m = re.search(r"\b(?:company|ticker)\s+\$?([A-Za-z]{1,5})\b", txt, flags=re.I)
        cand = str(m.group(1) if m else "").strip().upper()
        tk = cand if (cand and _is_known_ticker(cand)) else ""
    cat = str(category or "general").strip().lower()
    if cat not in {"company", "general"}:
        cat = "general"
    if tk and cat == "general":
        cat = "company"
    pr = str(priority or "P2").strip().upper()
    if pr not in {"P1", "P2", "P3"}:
        pr = "P2"
    dd = str(due_date or "").strip()
    if dd and not re.match(r"^\d{4}-\d{2}-\d{2}$", dd):
        dd = ""
    return add_todo_pg(task=txt[:4000], ticker=tk, category=cat, priority=pr, due_date=dd)


def snooze_task(todo_id: int, until_date: str) -> bool:
    rid = int(todo_id or 0)
    if rid <= 0:
        return False
    su = str(until_date or "").strip()
    if not su:
        return False
    return snooze_todo_pg(rid, su)


def suggest_ir_emails(ticker: str = "", company: str = "", domain: str = "", limit: int = 10) -> list[dict[str, str]]:
    t = str(ticker or "").strip().upper()
    dm = str(domain or "").strip().lower().replace("https://", "").replace("http://", "").strip("/")
    email_re = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(email: str, confidence: str, source: str) -> None:
        e = str(email or "").strip().lower()
        if not e or e in seen:
            return
        seen.add(e)
        out.append({"email": e, "confidence": confidence, "source": source})

    # 1) User-maintained override file (highest trust)
    try:
        csv_txt = cloud_files.read_text("data/ir_contacts.csv")
        if csv_txt:
            fh = io.StringIO(csv_txt)
            rd = csv.DictReader(fh)
            for r in rd:
                tk = str((r.get("ticker") or "")).strip().upper()
                em = str((r.get("email") or "")).strip()
                if t and tk != t:
                    continue
                if em:
                    add(em, "high", "ir_contacts.csv")
    except Exception:
        pass

    # 2) Extract from existing notes for this ticker (high confidence)
    if t:
        for n in list_recent_notes_pg(limit=600):
            tk = str(n.get("ticker") or "").strip().upper()
            if tk and tk != t:
                continue
            txt = str(n.get("text") or "")
            for em in email_re.findall(txt):
                add(em, "high", "notes")

    # 3) If a domain is provided, generate common IR aliases (medium confidence)
    if dm and "." in dm:
        aliases = [
            "ir",
            "investorrelations",
            "investor.relations",
            "investors",
            "investor",
            "irteam",
            "corpcomm",
            "media",
            "press",
        ]
        for a in aliases:
            add(f"{a}@{dm}", "medium", "pattern")

    # 4) If we already have a high confidence email, use its domain for extra medium suggestions.
    if not dm:
        for row in out:
            if row.get("confidence") == "high":
                em = str(row.get("email") or "")
                if "@" in em:
                    dm2 = em.split("@", 1)[1]
                    for a in ("ir", "investorrelations", "investors"):
                        add(f"{a}@{dm2}", "medium", "pattern-from-known-domain")
                break

    # 5) If still nothing, infer likely domain from company name (low confidence).
    if not out and company:
        raw = str(company or "").strip().lower()
        # Remove common legal/company suffixes.
        raw = re.sub(
            r"\b(inc|inc\.|corp|corporation|company|co|ltd|limited|plc|group|holdings|sa|ag|nv|llc|the)\b",
            " ",
            raw,
        )
        tokens = [re.sub(r"[^a-z0-9]", "", x) for x in raw.split()]
        tokens = [x for x in tokens if len(x) >= 2]
        guesses: list[str] = []
        if tokens:
            guesses.append(tokens[0] + ".com")
            if len(tokens) >= 2:
                guesses.append(tokens[0] + tokens[1] + ".com")
            guesses.append("".join(tokens[:3]) + ".com")
        if t and len(t) >= 2:
            guesses.append(t.lower() + ".com")
        for g in guesses:
            if "." not in g or len(g) < 6:
                continue
            for a in ("ir", "investorrelations", "investors"):
                add(f"{a}@{g}", "low", "pattern-from-company-name")

    # Prefer high confidence first, then medium.
    rank = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda x: (rank.get(str(x.get("confidence")), 9), str(x.get("email") or "")))
    return out[: max(1, min(30, int(limit or 10)))]


def recall(question: str, limit: int = 10) -> tuple[str, list[dict[str, str]]]:
    q = str(question or "").strip()
    if not q:
        return "", []
    toks = [
        x
        for x in re.findall(r"[a-z0-9]{3,}", q.lower())
        if x not in {"what", "when", "this", "that", "with", "from", "about", "your"}
    ]
    pool = list_recent_notes(limit=500)
    dn = get_daily_note(dt.date.today().isoformat())
    if dn.content:
        pool.insert(
            0,
            {"date": dn.day, "kind": "Daily", "ticker": ",".join(dn.tags), "tag": "daily", "text": dn.content},
        )
    scored: list[tuple[int, dict[str, str]]] = []
    for row in pool:
        hay = f"{row.get('date','')} {row.get('ticker','')} {row.get('tag','')} {row.get('text','')}".lower()
        s = sum(1 for t in toks if t in hay)
        if s > 0:
            scored.append((s, row))
    scored.sort(key=lambda x: (-x[0], str(x[1].get("date") or "")), reverse=False)
    top = [r for _s, r in scored[: max(1, min(25, limit * 2))]]
    if not top:
        return "No matching memory found yet.", []
    lines = [f"- {r.get('date','-')} | {r.get('ticker','-')} | {str(r.get('text',''))[:220]}" for r in top[:limit]]
    return "Best matches:\n" + "\n".join(lines), top[:limit]
