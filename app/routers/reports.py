from __future__ import annotations

import datetime as dt
import urllib.parse

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.date import parse_datetime_flexible
from app.services.reports_service import list_reports, mark_report_read, mark_reports_read, read_report_file, report_intelligence


router = APIRouter()


def _parse_modified(value: str) -> dt.datetime:
    out = parse_datetime_flexible(
        value,
        formats=("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"),
    )
    return out or dt.datetime.fromtimestamp(0)


def _group_label(mod: dt.datetime, now: dt.datetime) -> str:
    today = now.date()
    if mod.date() == today:
        return "Today"
    if mod.date() == (today - dt.timedelta(days=1)):
        return "Yesterday"
    if mod.date() >= (today - dt.timedelta(days=7)):
        return "This Week"
    return "Older"


def _priority_reads(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    preferred = [
        "Daily Brief",
        "Deep Dive",
        "Red Flag Alert",
        "L2 Digest",
        "Morning Intelligence",
        "Earnings Radar",
    ]
    icon_map = {
        "Daily Brief": "☕",
        "Deep Dive": "🔬",
        "Red Flag Alert": "🚨",
        "L2 Digest": "🧭",
        "Morning Intelligence": "🌅",
        "Earnings Radar": "📅",
    }
    top_map: dict[str, dict[str, str]] = {}
    for r in rows:
        k = str(r.get("kind") or "")
        if k and k not in top_map:
            top_map[k] = r
    out: list[dict[str, str]] = []
    for k in preferred:
        r = top_map.get(k)
        if not r:
            continue
        out.append(
            {
                "kind": k,
                "name": str(r.get("name") or ""),
                "title": str(r.get("title") or r.get("name") or "Report"),
                "score": int(str(r.get("priority_score") or "0") or "0"),
                "unread": str(r.get("unread") or "0"),
                "icon": icon_map.get(k, "📰"),
                "modified_short": _parse_modified(str(r.get("modified") or "")).strftime("%H:%M"),
            }
        )
        if len(out) >= 4:
            break
    return out


@router.get("/report")
def report_alias():
    return RedirectResponse(url="/reports", status_code=303)


@router.get("/reports/")
def reports_alias_slash():
    return RedirectResponse(url="/reports", status_code=303)


@router.get("/reports")
def reports_page(request: Request, q: str = "", kind: str = "", msg: str = ""):
    templates = request.app.state.templates
    view_mode = str(request.query_params.get("view") or "").strip().lower()
    if view_mode not in {"morning", "full"}:
        view_mode = "full"
    portfolio_only = str(request.query_params.get("portfolio_only") or "").strip() in {"1", "true", "yes", "on"}
    all_rows = list_reports(limit=500)
    rows = list(all_rows)
    ql = str(q or "").strip().lower()
    kd = str(kind or "").strip()
    if kd:
        rows = [r for r in rows if str(r.get("kind") or "") == kd]
    if ql:
        rows = [r for r in rows if ql in str(r.get("name") or "").lower() or ql in str(r.get("kind") or "").lower()]
    if portfolio_only:
        rows = [r for r in rows if str(r.get("ticker_hits") or "").strip()]
    kinds = sorted({str(r.get("kind") or "Other") for r in all_rows})
    try:
        # Keep /reports fast: do not block page render on external AI calls.
        intel = report_intelligence(rows if rows else all_rows, allow_ai=False)
    except Exception:
        intel = {"read_now": [], "new_items": [], "headlines": []}
    seq: list[dict[str, str]] = []
    seen: set[str] = set()
    for r in list(intel.get("read_now") or []) + list(intel.get("new_items") or []):
        name = str((r or {}).get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        seq.append(
            {
                "name": name,
                "kind": str((r or {}).get("kind") or ""),
                "title": str((r or {}).get("title") or ""),
            }
        )
        if len(seq) >= 8:
            break
    now = dt.datetime.now()
    grouped: dict[str, list[dict[str, str]]] = {"Today": [], "Yesterday": [], "This Week": [], "Older": []}
    for r in rows:
        mod = _parse_modified(str(r.get("modified") or ""))
        score = int(str(r.get("priority_score") or "0") or "0")
        bucket = _group_label(mod, now)
        grouped[bucket].append(
            {
                **r,
                "time_short": mod.strftime("%H:%M"),
                "day_num": mod.strftime("%d"),
                "month_short": mod.strftime("%b").upper(),
                "is_hot": "1" if score > 10 else "0",
            }
        )
    return templates.TemplateResponse(
        "reports.html",
        {
            "request": request,
            "message": msg,
            "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "view_mode": view_mode,
            "query": q,
            "kind": kd,
            "portfolio_only": "1" if portfolio_only else "0",
            "rows": rows,
            "kinds": kinds,
            "intel": intel,
            "morning_sequence": seq,
            "priority_reads": _priority_reads(rows if rows else all_rows),
            "grouped_rows": grouped,
        },
    )


@router.post("/reports/mark-all-read")
def reports_mark_all_read(
    q: str = Form(""),
    kind: str = Form(""),
    portfolio_only: str = Form("0"),
):
    rows = list_reports(limit=500)
    ql = str(q or "").strip().lower()
    kd = str(kind or "").strip()
    po = str(portfolio_only or "").strip() in {"1", "true", "yes", "on"}
    if kd:
        rows = [r for r in rows if str(r.get("kind") or "") == kd]
    if ql:
        rows = [r for r in rows if ql in str(r.get("name") or "").lower() or ql in str(r.get("kind") or "").lower()]
    if po:
        rows = [r for r in rows if str(r.get("ticker_hits") or "").strip()]
    n = mark_reports_read([str(r.get("name") or "") for r in rows])
    params = {"msg": f"Marked {n} report(s) as reviewed.", "q": q, "kind": kd}
    if po:
        params["portfolio_only"] = "1"
    return RedirectResponse(url="/reports?" + urllib.parse.urlencode(params), status_code=303)


@router.get("/api/reports")
def api_reports_list(q: str = "", kind: str = ""):
    """JSON list of all reports — powers the inline reports viewer."""
    rows = list_reports(limit=500)
    ql = str(q or "").strip().lower()
    kd = str(kind or "").strip()
    if kd:
        rows = [r for r in rows if str(r.get("kind") or "") == kd]
    if ql:
        rows = [r for r in rows if ql in str(r.get("name") or "").lower() or ql in str(r.get("kind") or "").lower()]
    now = dt.datetime.now()
    for r in rows:
        mod = _parse_modified(str(r.get("modified") or ""))
        r["group"] = _group_label(mod, now)
        r["time_short"] = mod.strftime("%H:%M")
    kinds = sorted({str(r.get("kind") or "Other") for r in rows})
    return JSONResponse({"ok": True, "reports": rows, "kinds": kinds})


@router.get("/api/report/{name:path}/content")
def api_report_content(name: str = ""):
    """Return report content as JSON — powers the slide panel viewer."""
    mark_report_read(name)
    meta = {}
    for r in list_reports(limit=2000):
        if str(r.get("name") or "") == str(name or ""):
            meta = r
            break
    txt, err = read_report_file(name)
    if err:
        return JSONResponse({"ok": False, "error": err})
    return JSONResponse({
        "ok": True,
        "name": name,
        "title": str(meta.get("title") or "Report"),
        "kind": str(meta.get("kind") or "Report"),
        "modified": str(meta.get("modified") or ""),
        "content": txt,
    })


@router.get("/reports/view")
def reports_view(request: Request, name: str = ""):
    templates = request.app.state.templates
    if name:
        mark_report_read(name)
    meta = {}
    for r in list_reports(limit=2000):
        if str(r.get("name") or "") == str(name or ""):
            meta = r
            break
    txt, err = read_report_file(name)
    return templates.TemplateResponse(
        "report_view.html",
        {
            "request": request,
            "name": name,
            "title": str(meta.get("title") or "Report"),
            "kind": str(meta.get("kind") or "Report"),
            "modified": str(meta.get("modified") or ""),
            "error": err,
            "content": txt,
        },
    )
