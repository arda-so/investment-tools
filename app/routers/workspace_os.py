"""workspace_os.py — Day view + Ticker timeline + Unified record API."""
from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.services.workspace_os_service import (
    action_dismiss,
    action_done,
    action_delete,
    create_record,
    delete_record,
    get_activity_feed,
    get_calendar_data,
    get_day_view,
    get_sidebar_data,
    get_ticker_ai_context,
    get_ticker_timeline,
    list_records,
    pin_record,
    update_record_status,
    create_project,
    list_projects,
    update_project,
    delete_project,
    list_links,
    create_link,
    update_link,
    delete_link,
    get_home_activity,
    get_inbox_items,
    triage_record,
    get_references,
    get_tasks,
)

from app.services.postgres_core_service import list_todos_pg, list_watchlist_thesis_pg
from app.services.google_workspace_service import google_status
from app.services.company_lookup_service import company_name_map
from app.services.financial_lab_service import (
    get_lab_context,
    score_buffett_checklist,
    save_case_study,
    list_case_studies,
    delete_case_study,
    simulate_scenario,
    diff_risk_factors
)

router = APIRouter()
LOGGER = logging.getLogger(__name__)


def _templates(request: Request):
    return request.app.state.templates


# ── API: Home data (todos + sidebar stats for home space) ─────────────────────

@router.get("/workspace/home-data")
def api_home_data(request: Request):
    sidebar = get_sidebar_data()
    todos = list_todos_pg(open_only=True, limit=50)
    gst = google_status()
    return JSONResponse({
        "todos": todos,
        "open_tasks": sidebar.get("open_tasks", 0),
        "portfolio_count": len(sidebar.get("portfolio", [])),
        "watchlist_count": len(sidebar.get("watchlist", [])),
        "google_connected": gst.get("connected") == "1",
    })


@router.get("/workspace/home-activity")
def api_home_activity(request: Request, limit: int = 20):
    """Activity feed filtered to workspace-only items (user records, todos, notes)."""
    lim = max(1, min(100, int(limit or 20)))
    return JSONResponse({"items": get_home_activity(limit=lim)})


# ── Page: Day view ────────────────────────────────────────────────────────────

@router.get("/today", response_class=HTMLResponse)
@router.get("/day", response_class=HTMLResponse)
def day_view(request: Request):
    view = get_day_view()
    today = dt.date.today()
    weekday = today.strftime("%A")
    date_str = today.strftime("%B %-d, %Y")
    return _templates(request).TemplateResponse(
        "workspace_os_day.html",
        {
            "request": request,
            "focus": view["focus"],
            "agent_flagged": view["agent_flagged"],
            "backlog": view["backlog"],
            "weekday": weekday,
            "date_str": date_str,
            "hour": dt.datetime.now().hour,
            "auto_open_ticker": "",
        },
    )


# ── Page: Ticker timeline ─────────────────────────────────────────────────────

@router.get("/workspace/ticker/{ticker}", response_class=HTMLResponse)
@router.get("/ticker/{ticker}", response_class=HTMLResponse)
def ticker_timeline_page(request: Request, ticker: str):
    """Render the unified workspace shell with the ticker auto-opened inline."""
    tk = str(ticker or "").strip().upper()
    if not tk:
        return RedirectResponse(url="/today", status_code=302)

    # Render the same workspace shell as /today, with auto_open_ticker set.
    # The JS init() will call openTicker(tk) to load the ticker inline.
    view = get_day_view()
    today = dt.date.today()
    return _templates(request).TemplateResponse(
        "workspace_os_day.html",
        {
            "request": request,
            "focus": view["focus"],
            "agent_flagged": view["agent_flagged"],
            "backlog": view["backlog"],
            "weekday": today.strftime("%A"),
            "date_str": today.strftime("%B %-d, %Y"),
            "hour": dt.datetime.now().hour,
            "auto_open_ticker": tk,
        },
    )


# ── API: Ticker hub JSON (for workspace inline rendering) ─────────────────────

@router.get("/api/ticker-hub/{ticker}")
def api_ticker_hub(request: Request, ticker: str):
    """Return full ticker hub data as JSON for inline workspace rendering."""
    tk = str(ticker or "").strip().upper()
    if not tk:
        return JSONResponse({"error": "Missing ticker"}, status_code=400)

    from app.services.ticker_hub_service import get_ticker_hub
    hub = get_ticker_hub(tk)

    # Thesis
    thesis: dict = {}
    try:
        theses = list_watchlist_thesis_pg(limit=200)
        for t in theses:
            if str(t.get("ticker") or "").upper() == tk:
                thesis = dict(t)
                break
    except Exception:
        pass

    # Company name
    cname = tk
    try:
        nm = company_name_map()
        cname = nm.get(tk, tk)
    except Exception:
        pass
    if hub.get("profile"):
        cname = hub["profile"].get("name") or hub["profile"].get("company_name") or cname

    # Research notes
    research_notes: list = []
    try:
        from app.services.research_notes_service import list_notes
        research_notes = list_notes(tk)
    except Exception:
        pass

    return JSONResponse({
        "ticker": tk,
        "company_name": cname,
        "hub": hub,
        "thesis": thesis,
        "research_notes": research_notes,
    })


# ── API: SEC Hub Smart Feed ───────────────────────────────────────────────────

@router.get("/api/sec-hub/feed")
def api_sec_hub_feed(limit: int = 60):
    """Cross-portfolio SEC filing feed — recent filings across all held + watchlist tickers."""
    from app.services.portfolio_memory_service import get_holdings
    from app.services.watchlist_service import read_watchlist_rows
    from app.services.postgres_core_service import pg_connect

    # Gather tickers
    held = set()
    watchlist = set()
    try:
        for h in (get_holdings() or []):
            tk = str(h.get("ticker") or "").strip().upper()
            if tk:
                held.add(tk)
    except Exception:
        pass
    try:
        for w in (read_watchlist_rows() or []):
            tk = str(w.get("ticker") or "").strip().upper()
            if tk:
                watchlist.add(tk)
    except Exception:
        pass

    all_tickers = sorted(held | watchlist)
    if not all_tickers:
        return JSONResponse({"ok": True, "filings": [], "held": [], "watchlist": []})

    con = pg_connect()
    if con is None:
        return JSONResponse({"ok": False, "error": "Database unavailable"}, status_code=500)

    try:
        cur = con.cursor()
        # Fetch recent filings across all tickers
        marks = ",".join(["%s"] * len(all_tickers))
        cur.execute(
            f"""SELECT f.ticker, f.form, f.date, f.accession, f.doc_url, f.path,
                       (f.content IS NOT NULL AND f.content != '') AS has_content
                FROM filings_core f
                WHERE UPPER(f.ticker) IN ({marks})
                ORDER BY f.date DESC, f.id DESC
                LIMIT %s""",
            (*all_tickers, min(int(limit), 120)),
        )
        filings = []
        for r in cur.fetchall() or []:
            tk = str(r[0] or "").upper()
            filings.append({
                "ticker": tk,
                "form": str(r[1] or ""),
                "date": str(r[2] or ""),
                "accession": str(r[3] or ""),
                "doc_url": str(r[4] or ""),
                "path": str(r[5] or ""),
                "has_local": "1" if r[6] else "0",
                "is_held": tk in held,
                "is_watchlist": tk in watchlist and tk not in held,
            })

        # Get company names
        names = {}
        try:
            nm = company_name_map(list(all_tickers))
            names = nm
        except Exception:
            pass

        # Get AI summaries from proposals (reuse existing data, no new LLM calls)
        summaries = {}
        try:
            cur.execute(
                f"""SELECT DISTINCT ON (source_event_key) source_event_key, ticker, thesis_summary
                    FROM action_proposals_core
                    WHERE UPPER(ticker) IN ({marks})
                      AND thesis_summary IS NOT NULL AND thesis_summary != ''
                    ORDER BY source_event_key, created_at DESC""",
                all_tickers,
            )
            for r in cur.fetchall() or []:
                key = str(r[0] or "")
                summaries[key] = str(r[2] or "")[:200]
        except Exception:
            pass

        # Attach names and summaries
        for f in filings:
            f["company_name"] = names.get(f["ticker"], f["ticker"])
            # Try to match summary by source_event_key pattern
            acc_key = f"sec_filing_{f['ticker']}_{f['accession']}"
            f["ai_summary"] = summaries.get(acc_key, "")

        return JSONResponse({
            "ok": True,
            "filings": filings,
            "held": sorted(held),
            "watchlist": sorted(watchlist - held),
        })

    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    finally:
        con.close()


# ── API: Ticker search / autocomplete ─────────────────────────────────────────

@router.get("/api/ticker-search")
def api_ticker_search(q: str = ""):
    """Search tickers and company names for autocomplete. Returns up to 12 matches."""
    query = str(q or "").strip().upper()
    if len(query) < 1:
        return JSONResponse({"results": []})
    try:
        nm = company_name_map()  # {TICKER: "Company Name", ...}
    except Exception:
        nm = {}
    # Also pull portfolio + watchlist tickers for priority
    try:
        from app.services.portfolio_memory_service import get_holdings
        held = {h["ticker"] for h in (get_holdings() or []) if h.get("ticker")}
    except Exception:
        held = set()
    results = []
    query_lower = query.lower()
    for tk, name in nm.items():
        tk_upper = tk.upper()
        name_lower = (name or "").lower()
        # Match on ticker prefix OR company name contains query
        if tk_upper.startswith(query) or query_lower in name_lower:
            results.append({"ticker": tk_upper, "name": name or tk_upper, "held": tk_upper in held})
    # Sort: exact ticker match first, then held tickers, then alphabetical
    results.sort(key=lambda r: (0 if r["ticker"] == query else 1, 0 if r["held"] else 1, r["ticker"]))
    return JSONResponse({"results": results[:12]})

# ── API: SEC Hub JSON (for workspace inline rendering) ────────────────────────

@router.get("/api/sec-hub")
def api_sec_hub(request: Request, ticker: str = ""):
    """Return SEC filing data as JSON for inline workspace rendering."""
    tk = str(ticker or "").strip().upper()
    if not tk:
        return JSONResponse({"ok": False, "error": "Missing ticker"}, status_code=400)
    try:
        from app.services.company_file_service import company_detail
        detail = company_detail(tk)
        if not detail:
            return JSONResponse({"ok": True, "ticker": tk, "filings": [], "filing_groups": []})
        filing_groups = detail.get("filing_groups") or []
        filings = detail.get("filings") or []
        return JSONResponse({
            "ok": True,
            "ticker": tk,
            "name": detail.get("name") or tk,
            "filings": filings,
            "filing_groups": [
                {"key": g.get("key", ""), "label": g.get("label", ""), "count": g.get("count", 0),
                 "rows": [{"date": r.get("date",""), "form": r.get("form",""), "accession": r.get("accession",""),
                           "file_name": r.get("file_name",""), "has_local": r.get("has_local","0"),
                           "path": r.get("path",""), "doc_url": r.get("doc_url","")}
                          for r in (g.get("rows") or [])]}
                for g in filing_groups
            ],
        })
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


# ── API: Create record ────────────────────────────────────────────────────────

@router.post("/workspace/record")
def api_create_record(
    request: Request,
    kind: str = Form("action"),
    domain: str = Form("work"),
    title: str = Form(""),
    body: str = Form(""),
    ticker: str = Form(""),
    priority: str = Form("normal"),
    due_date: str = Form(""),
    source: str = Form("manual"),
    created_by: str = Form("user"),
    sentiment: str = Form("neutral"),
    pinned: str = Form("false"),
    source_ref_id: str = Form(""),
    url: str = Form(""),
    emoji: str = Form(""),
    project_space: str = Form("general"),
    project_id: str = Form(""),
):
    rec_id = create_record(
        kind=kind,
        domain=domain,
        title=title,
        body=body,
        ticker=ticker,
        priority=priority,
        due_date=due_date or None,
        source=source,
        created_by=created_by,
        sentiment=sentiment,
        pinned=str(pinned).lower() in ("1", "true", "yes"),
        source_ref_id=source_ref_id,
        url=url,
        emoji=emoji,
        project_space=project_space,
        project_id=int(project_id) if project_id.strip().isdigit() else None,
    )
    if rec_id <= 0:
        return JSONResponse(
            {
                "ok": False,
                "id": 0,
                "error": "Could not save record. Check database/backend configuration.",
            },
            status_code=500,
        )
    return JSONResponse({"ok": True, "id": rec_id})


# ── API: Update status ────────────────────────────────────────────────────────

@router.post("/workspace/record/{rec_id}/status")
def api_update_status(
    request: Request,
    rec_id: int,
    status: str = Form(...),
    approved_by: str = Form(""),
):
    ok = update_record_status(rec_id, status, approved_by=approved_by)
    return JSONResponse({"ok": ok})


# ── API: Delete ───────────────────────────────────────────────────────────────

@router.post("/workspace/record/{rec_id}/delete")
def api_delete_record(request: Request, rec_id: int):
    ok = delete_record(rec_id)
    if not ok:
        return JSONResponse({"ok": False, "error": "Record not found."}, status_code=404)
    return JSONResponse({"ok": True})


# ── API: Pin toggle ───────────────────────────────────────────────────────────

@router.post("/workspace/record/{rec_id}/pin")
def api_pin_record(
    request: Request,
    rec_id: int,
    pinned: str = Form("true"),
):
    p = str(pinned).lower() not in ("0", "false", "no")
    ok = pin_record(rec_id, p)
    return JSONResponse({"ok": ok, "pinned": p})


# ── API: List records ─────────────────────────────────────────────────────────

@router.get("/workspace/records")
def api_list_records(
    request: Request,
    kind: str = "",
    domain: str = "",
    status: str = "",
    ticker: str = "",
    project_space: str = "",
    project_id: int = 0,
    limit: int = 50,
):
    rows = list_records(
        kind=kind or None,
        domain=domain or None,
        status=status or None,
        ticker=ticker or None,
        project_space=project_space or None,
        project_id=project_id or None,
        limit=limit,
    )
    return JSONResponse({"records": rows})


# ── API: Ticker records ───────────────────────────────────────────────────────

@router.get("/workspace/ticker/{ticker}/records")
def api_ticker_records(request: Request, ticker: str, limit: int = 50):
    tk = str(ticker or "").strip().upper()
    rows = get_ticker_timeline(tk, limit=limit)
    return JSONResponse({"ticker": tk, "records": rows})


# ── API: Unified action (done/dismiss/delete across all sources) ──────────────

@router.post("/workspace/item-action")
def api_item_action(
    request: Request,
    source: str = Form(...),   # todo | proposal | record
    id: int = Form(...),
    action: str = Form(...),   # done | dismiss | delete
):
    src = str(source or "").strip().lower()
    act = str(action or "").strip().lower()
    ok = False
    if act == "done":
        ok = action_done(src, id)
    elif act == "dismiss":
        ok = action_dismiss(src, id)
    elif act == "delete":
        ok = action_delete(src, id)
    return JSONResponse({"ok": ok})


# ── API: Ticker AI context (cached) ──────────────────────────────────────────

@router.get("/workspace/ticker/{ticker}/ai-context")
def api_ticker_ai_context(request: Request, ticker: str):
    tk = str(ticker or "").strip().upper()
    insight = get_ticker_ai_context(tk)
    return JSONResponse({"ticker": tk, "insight": insight})


# ── API: Sidebar data ─────────────────────────────────────────────────────────

@router.get("/api/sidebar-data")
def api_sidebar_data(request: Request):
    return JSONResponse(get_sidebar_data())


@router.get("/api/space-portfolio")
def api_space_portfolio(request: Request):
    """Rich portfolio data for the Today workspace Portfolio space."""
    from app.routers.dashboard import (
        dashboard_snapshot, _my_companies_metrics, _load_thesis_map,
        _safe_ticker, _to_float,
    )
    snap = dashboard_snapshot()
    home = snap.get("home", {}) or {}
    metrics = _my_companies_metrics(home)
    portfolio_rows = list(metrics.get("portfolio_rows") or [])
    thesis_map = _load_thesis_map()
    holdings = []
    for r in portfolio_rows:
        t = _safe_ticker(r.get("ticker", ""))
        if not t:
            continue
        th = thesis_map.get(t, {})
        holdings.append({
            "ticker": t,
            "name": str(r.get("name") or ""),
            "shares": r.get("shares"),
            "cost": _to_float(r.get("cost_num"), 0),
            "price": _to_float(r.get("price_live"), 0),
            "day_pct": _to_float(r.get("day_pct"), 0),
            "value": _to_float(r.get("value_usd"), 0),
            "weight_pct": _to_float(r.get("weight_pct"), 0),
            "thesis": str(th.get("thesis") or ""),
            "target_price": _to_float(th.get("target_price"), 0),
            "invalidation": str(th.get("invalidation_criteria") or ""),
        })
    # Also fetch earnings map for portfolio tickers
    tickers = [h["ticker"] for h in holdings if h.get("ticker")]
    earnings_map: dict = {}
    try:
        from app.routers.dashboard import _load_earnings_map
        earnings_map = _load_earnings_map(tickers)
    except Exception:
        pass

    return JSONResponse({
        "holdings": holdings,
        "aum": _to_float(metrics.get("aum_usd"), 0),
        "stock_value": _to_float(metrics.get("stock_value_usd"), 0),
        "day_pnl": _to_float(metrics.get("aum_day_pnl_usd"), 0),
        "day_pct": _to_float(metrics.get("aum_day_pct"), 0),
        "earnings_map": {k: v[:4] for k, v in earnings_map.items()},  # last 4 per ticker
    })


@router.get("/api/quarterly-reviews")
def api_list_quarterly_reviews(request: Request):
    """Return quarterly reviews as JSON for the Portfolio space."""
    try:
        from app.routers.dashboard import _load_quarterly_reviews
        reviews = _load_quarterly_reviews()
    except Exception:
        reviews = []
    return JSONResponse({"reviews": reviews})


@router.get("/api/space-watchlist")
def api_space_watchlist(request: Request):
    """Rich watchlist data for the Today workspace Watchlist space."""
    from app.routers.dashboard import (
        _read_watchlist_rows, _last_quote_map, _company_name_map,
        _load_thesis_map, _safe_ticker, _to_float, _pick_day_pct,
        dashboard_snapshot,
    )
    snap = dashboard_snapshot()
    home = snap.get("home", {}) or {}
    watchlist_rows = _read_watchlist_rows()
    if not watchlist_rows:
        watchlist_rows = list(home.get("watchlist", []) or [])
    wl_tickers = [_safe_ticker(r.get("ticker", "")) for r in watchlist_rows if _safe_ticker(r.get("ticker", ""))]
    quote_map = _last_quote_map(wl_tickers)
    name_map = _company_name_map(wl_tickers)
    thesis_map = _load_thesis_map()
    items = []
    for r in watchlist_rows:
        t = _safe_ticker(r.get("ticker", ""))
        if not t:
            continue
        q = quote_map.get(t, {}) or {}
        live_px = _to_float(q.get("price"), 0) or _to_float(r.get("price_now"), 0)
        day_pct = _to_float(_pick_day_pct(q.get("day_pct"), r.get("day_pct")), 0)
        th = thesis_map.get(t, {})
        items.append({
            "ticker": t,
            "name": str(name_map.get(t) or ""),
            "price": live_px,
            "day_pct": day_pct,
            "added_at": str(r.get("added_at") or ""),
            "thesis": str(th.get("thesis") or ""),
            "target_price": _to_float(th.get("target_price"), 0),
            "status": str(th.get("status") or "active"),
            "invalidation": str(th.get("invalidation_criteria") or ""),
        })
    return JSONResponse({"items": items})


# ── API: Activity feed ────────────────────────────────────────────────────────

@router.get("/workspace/activity")
def api_activity_feed(request: Request, limit: int = 60):
    lim = max(1, min(200, int(limit or 60)))
    return JSONResponse({"items": get_activity_feed(limit=lim)})


# ── API: Inbox ───────────────────────────────────────────────────────────────

@router.get("/workspace/inbox")
def api_inbox(request: Request, limit: int = 50):
    items = get_inbox_items(limit=limit)
    return JSONResponse({"items": items})


# ── API: Triage record ───────────────────────────────────────────────────────

@router.post("/workspace/record/{rec_id}/triage")
def api_triage_record(
    request: Request,
    rec_id: int,
    kind: str = Form("task"),
):
    ok = triage_record(rec_id, kind)
    return JSONResponse({"ok": ok})


# ── API: References ──────────────────────────────────────────────────────────

@router.get("/workspace/references")
def api_references(request: Request, ticker: str = "", limit: int = 50):
    items = get_references(ticker=ticker or None, limit=limit)
    return JSONResponse({"items": items})


# ── API: Tasks ───────────────────────────────────────────────────────────────

@router.get("/workspace/tasks")
def api_tasks(request: Request, status: str = "open", limit: int = 100):
    items = get_tasks(status=status, limit=limit)
    return JSONResponse({"items": items})


# ── API: Playbooks ────────────────────────────────────────────────────────────

@router.get("/workspace/playbooks")
def api_list_playbooks(request: Request):
    from app.services.playbook_service import list_playbooks
    return JSONResponse({"playbooks": list_playbooks()})


@router.get("/workspace/playbooks/today")
def api_todays_playbooks(request: Request):
    from app.services.playbook_service import get_todays_playbooks
    return JSONResponse({"playbooks": get_todays_playbooks()})


@router.get("/workspace/playbook/{pb_id}")
def api_get_playbook(request: Request, pb_id: int):
    from app.services.playbook_service import get_playbook
    pb = get_playbook(pb_id)
    if not pb:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    return JSONResponse({"playbook": pb})


@router.post("/workspace/playbook")
def api_create_playbook(
    request: Request,
    name: str = Form(""),
    emoji: str = Form("📖"),
    description: str = Form(""),
    purpose: str = Form(""),
    steps_json: str = Form("[]"),
    schedule: str = Form(""),
    workbench_json: str = Form('{"links":[],"workspace_note":""}'),
    guardrails_json: str = Form("[]"),
    output_config_json: str = Form('{"decision_type":"pass_fail","pass_action":"","fail_action":""}'),
):
    import json as _json
    if not name.strip():
        return JSONResponse({"ok": False, "error": "Name required"}, status_code=400)
    try:
        steps = _json.loads(steps_json)
    except Exception:
        steps = []
    try:
        workbench = _json.loads(workbench_json)
    except Exception:
        workbench = {"links": [], "workspace_note": ""}
    try:
        guardrails = _json.loads(guardrails_json)
    except Exception:
        guardrails = []
    try:
        output_config = _json.loads(output_config_json)
    except Exception:
        output_config = {"decision_type": "pass_fail", "pass_action": "", "fail_action": ""}
    from app.services.playbook_service import create_playbook
    pb_id = create_playbook(
        name=name,
        emoji=emoji,
        description=description,
        purpose=purpose,
        steps=steps,
        schedule=schedule,
        workbench=workbench,
        guardrails=guardrails,
        output_config=output_config,
    )
    return JSONResponse({"ok": pb_id > 0, "id": pb_id})


@router.post("/workspace/playbook/{pb_id}/update")
def api_update_playbook(
    request: Request,
    pb_id: int,
    name: str = Form(""),
    emoji: str = Form(""),
    description: str = Form(""),
    purpose: str = Form(""),
    steps_json: str = Form(""),
    schedule: str = Form(""),
    workbench_json: str = Form(""),
    guardrails_json: str = Form(""),
    output_config_json: str = Form(""),
):
    import json as _json
    fields: dict = {}
    if name.strip():
        fields["name"] = name.strip()
    if emoji.strip():
        fields["emoji"] = emoji.strip()
    if description is not None and description != "":
        fields["description"] = description
    if purpose is not None and purpose != "":
        fields["purpose"] = purpose
    if steps_json.strip():
        try:
            fields["steps"] = _json.loads(steps_json)
        except Exception:
            pass
    if schedule is not None and schedule != "":
        fields["schedule"] = schedule
    if workbench_json.strip():
        try:
            fields["workbench"] = _json.loads(workbench_json)
        except Exception:
            pass
    if guardrails_json.strip():
        try:
            fields["guardrails"] = _json.loads(guardrails_json)
        except Exception:
            pass
    if output_config_json.strip():
        try:
            fields["output_config"] = _json.loads(output_config_json)
        except Exception:
            pass
    from app.services.playbook_service import update_playbook
    ok = update_playbook(pb_id, **fields)
    return JSONResponse({"ok": ok})


@router.post("/workspace/playbook/{pb_id}/delete")
def api_delete_playbook(request: Request, pb_id: int):
    from app.services.playbook_service import delete_playbook
    return JSONResponse({"ok": delete_playbook(pb_id)})


@router.post("/workspace/playbook/{pb_id}/start")
def api_start_run(request: Request, pb_id: int):
    from app.services.playbook_service import start_run
    run_id = start_run(pb_id)
    return JSONResponse({"ok": run_id > 0, "run_id": run_id})


@router.get("/workspace/playbook-run/{run_id}")
def api_get_run(request: Request, run_id: int):
    from app.services.playbook_service import get_run, get_playbook
    run = get_run(run_id)
    if not run:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    pb = get_playbook(run["playbook_id"])
    return JSONResponse({"run": run, "playbook": pb})


@router.post("/workspace/playbook-run/{run_id}/step-notes")
async def api_save_step_notes(request: Request, run_id: int):
    import json as _json
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
        step_index = int(body.get("step_index", 0))
        notes = str(body.get("notes", ""))
        checked = body.get("checked_items", [])
    else:
        form = await request.form()
        step_index = int(form.get("step_index", 0))
        notes = str(form.get("notes", ""))
        try:
            checked = _json.loads(form.get("checked_json", "[]"))
        except Exception:
            checked = []
    from app.services.playbook_service import save_step_notes
    ok = save_step_notes(run_id, step_index, notes, checked)
    return JSONResponse({"ok": ok})


@router.post("/workspace/playbook-run/{run_id}/advance")
async def api_advance_run(request: Request, run_id: int):
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
        step = int(body.get("step", 0))
        ticker = str(body.get("ticker", ""))
    else:
        form = await request.form()
        step = int(form.get("step", 0))
        ticker = str(form.get("ticker", ""))
    from app.services.playbook_service import update_run
    fields: dict = {"current_step": step}
    if ticker.strip():
        fields["ticker"] = ticker.strip().upper()
    ok = update_run(run_id, **fields)
    return JSONResponse({"ok": ok})


@router.post("/workspace/playbook-run/{run_id}/complete")
async def api_complete_run(request: Request, run_id: int):
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
        ticker = str(body.get("ticker", ""))
        decision = str(body.get("decision", ""))
        decision_notes = str(body.get("decision_notes", ""))
        next_action = str(body.get("next_action", ""))
    else:
        form = await request.form()
        ticker = str(form.get("ticker", ""))
        decision = str(form.get("decision", ""))
        decision_notes = str(form.get("decision_notes", ""))
        next_action = str(form.get("next_action", ""))
    from app.services.playbook_service import complete_run
    ok = complete_run(
        run_id,
        ticker=ticker,
        decision=decision,
        decision_notes=decision_notes,
        next_action=next_action,
    )
    return JSONResponse({"ok": ok})


@router.get("/workspace/playbook/{pb_id}/runs")
def api_list_runs(request: Request, pb_id: int, limit: int = 20):
    from app.services.playbook_service import list_runs
    return JSONResponse({"runs": list_runs(pb_id, limit=limit)})


# ── API: Research Threads & Projects ──────────────────────────────────────────

@router.get("/workspace/threads")
def api_list_threads(request: Request, status: str = "active", thread_type: str = ""):
    from app.services.research_thread_service import list_threads
    return JSONResponse({"threads": list_threads(status=status, thread_type=thread_type)})


@router.get("/workspace/thread/{thread_id}")
def api_get_thread(request: Request, thread_id: int):
    from app.services.research_thread_service import get_thread
    t = get_thread(thread_id)
    if not t:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    return JSONResponse({"thread": t})


@router.post("/workspace/thread")
async def api_create_thread(request: Request):
    import json as _json
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)
        if "tags" in body:
            try:
                body["tags"] = _json.loads(body["tags"])
            except Exception:
                body["tags"] = []
    from app.services.research_thread_service import create_thread
    title = str(body.get("title", "")).strip()
    if not title:
        return JSONResponse({"ok": False, "error": "Title required"}, status_code=400)
    thread_id = create_thread(
        title=title,
        thread_type=str(body.get("thread_type", "research")),
        emoji=str(body.get("emoji", "")),
        ticker=str(body.get("ticker", "")),
        thesis=str(body.get("thesis", "")),
        priority=str(body.get("priority", "normal")),
        tags=body.get("tags", []),
    )
    return JSONResponse({"ok": thread_id > 0, "id": thread_id})


@router.post("/workspace/thread/{thread_id}/update")
async def api_update_thread(request: Request, thread_id: int):
    import json as _json
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)
        if "tags" in body:
            try:
                body["tags"] = _json.loads(body["tags"])
            except Exception:
                body["tags"] = []
    from app.services.research_thread_service import update_thread
    ok = update_thread(thread_id, **body)
    return JSONResponse({"ok": ok})


@router.post("/workspace/thread/{thread_id}/delete")
def api_delete_thread(request: Request, thread_id: int):
    from app.services.research_thread_service import delete_thread
    return JSONResponse({"ok": delete_thread(thread_id)})


@router.post("/workspace/thread/{thread_id}/session/start")
def api_start_session(request: Request, thread_id: int):
    from app.services.research_thread_service import start_session
    session_id = start_session(thread_id)
    return JSONResponse({"ok": session_id > 0, "session_id": session_id})


@router.post("/workspace/session/{session_id}/end")
async def api_end_session(request: Request, session_id: int):
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)
    from app.services.research_thread_service import end_session
    ok = end_session(
        session_id,
        summary=str(body.get("summary", "")),
        where_left_off=str(body.get("where_left_off", "")),
    )
    return JSONResponse({"ok": ok})


@router.post("/workspace/thread/{thread_id}/entry")
async def api_add_entry(request: Request, thread_id: int):
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)
    from app.services.research_thread_service import add_entry
    entry_id = add_entry(
        thread_id,
        kind=str(body.get("kind", "note")),
        content=str(body.get("content", "")),
        session_id=int(body["session_id"]) if body.get("session_id") else None,
        source_url=str(body.get("source_url", "")),
    )
    return JSONResponse({"ok": entry_id > 0, "id": entry_id})


@router.post("/workspace/entry/{entry_id}/update")
async def api_update_entry(request: Request, entry_id: int):
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)
    from app.services.research_thread_service import update_entry
    ok = update_entry(entry_id, **body)
    return JSONResponse({"ok": ok})


@router.post("/workspace/entry/{entry_id}/delete")
def api_delete_entry(request: Request, entry_id: int):
    from app.services.research_thread_service import delete_entry
    return JSONResponse({"ok": delete_entry(entry_id)})


@router.get("/workspace/resume-threads")
def api_resume_threads(request: Request, limit: int = 5):
    from app.services.research_thread_service import get_resume_threads
    return JSONResponse({"threads": get_resume_threads(limit=limit)})


# ── API: Work Graph (links between objects) ──────────────────────────────────

@router.get("/workspace/object-links")
def api_get_links(request: Request, obj_type: str = "", obj_id: str = "", filter_type: str = ""):
    from app.services.work_graph_service import get_links, resolve_linked_objects
    if not obj_type or not obj_id:
        return JSONResponse({"links": []})
    links = get_links(obj_type, obj_id, filter_type=filter_type)
    links = resolve_linked_objects(links)
    return JSONResponse({"links": links})


@router.post("/workspace/object-link")
async def api_add_link(request: Request):
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)
    from app.services.work_graph_service import add_link
    a_type = str(body.get("a_type", ""))
    a_id = str(body.get("a_id", ""))
    b_type = str(body.get("b_type", ""))
    b_id = str(body.get("b_id", ""))
    relation = str(body.get("relation", "related"))
    if not a_type or not a_id or not b_type or not b_id:
        return JSONResponse({"ok": False, "error": "Missing fields"}, status_code=400)
    link_id = add_link(a_type, a_id, b_type, b_id, relation)
    return JSONResponse({"ok": link_id > 0, "id": link_id})


@router.post("/workspace/object-link/{link_id}/delete")
def api_delete_link(request: Request, link_id: int):
    from app.services.work_graph_service import remove_link_by_id
    return JSONResponse({"ok": remove_link_by_id(link_id)})


@router.get("/workspace/search-linkable")
def api_search_linkable(request: Request, q: str = "", obj_type: str = ""):
    """Search for objects to link to. Returns matching records, threads, playbooks."""
    results: list[dict] = []
    if not q.strip():
        return JSONResponse({"results": results})
    from app.services.postgres_core_service import pg_connect, pg_enabled
    if not pg_enabled():
        return JSONResponse({"results": results})
    con = pg_connect()
    if con is None:
        return JSONResponse({"results": results})
    try:
        cur = con.cursor()
        pattern = f"%{q.strip()}%"
        # Search records
        if not obj_type or obj_type == "record":
            cur.execute(
                """SELECT id, title, kind, ticker FROM investment_records_core
                   WHERE (title ILIKE %s OR ticker ILIKE %s) AND status != 'deleted'
                   ORDER BY updated_at DESC LIMIT 10""",
                (pattern, pattern),
            )
            for row in (cur.fetchall() or []):
                kind_icons = {"task": "\u2705", "note": "\U0001f4dd", "question": "\u2753",
                              "reference": "\U0001f4ce", "inbox": "\U0001f4ec"}
                results.append({"type": "record", "id": str(row[0]),
                                "title": row[1], "emoji": kind_icons.get(row[2], "\U0001f4dd"),
                                "subtitle": f"${row[3]}" if row[3] else row[2]})
        # Search threads
        if not obj_type or obj_type == "thread":
            cur.execute(
                """SELECT id, title, emoji, ticker, thread_type FROM research_threads_core
                   WHERE (title ILIKE %s OR ticker ILIKE %s) AND status != 'archived'
                   ORDER BY updated_at DESC LIMIT 10""",
                (pattern, pattern),
            )
            for row in (cur.fetchall() or []):
                results.append({"type": "thread", "id": str(row[0]),
                                "title": row[1], "emoji": row[2] or "\U0001f52c",
                                "subtitle": f"${row[3]}" if row[3] else row[4]})
        # Search playbooks
        if not obj_type or obj_type == "playbook":
            cur.execute(
                """SELECT id, name, emoji FROM playbooks_core
                   WHERE name ILIKE %s AND status='active'
                   ORDER BY name LIMIT 10""",
                (pattern,),
            )
            for row in (cur.fetchall() or []):
                results.append({"type": "playbook", "id": str(row[0]),
                                "title": row[1], "emoji": row[2] or "\U0001f4d6",
                                "subtitle": "playbook"})
        return JSONResponse({"results": results})
    except Exception as exc:
        LOGGER.warning("search_linkable failed: %s", str(exc))
        return JSONResponse({"results": []})
    finally:
        con.close()


# ── API: AI Canvas ───────────────────────────────────────────────────────────

@router.post("/workspace/thread/{thread_id}/canvas")
def api_generate_canvas(request: Request, thread_id: int):
    from app.services.research_thread_service import generate_canvas
    canvas = generate_canvas(thread_id)
    return JSONResponse({"ok": bool(canvas), "canvas": canvas})


@router.post("/workspace/thread/{thread_id}/canvas/save")
async def api_save_canvas(request: Request, thread_id: int):
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)
    from app.services.research_thread_service import update_thread
    ok = update_thread(thread_id, canvas_markdown=str(body.get("canvas_markdown", "")))
    return JSONResponse({"ok": ok})


# ── API: Smart automation (overdue, stale, blocked) ──────────────────────────

@router.get("/workspace/overdue")
def api_overdue_tasks(request: Request, limit: int = 10):
    from app.services.workspace_os_service import get_overdue_tasks
    return JSONResponse({"items": get_overdue_tasks(limit=limit)})


@router.get("/workspace/stale")
def api_stale_records(request: Request, days: int = 14, limit: int = 10):
    from app.services.workspace_os_service import get_stale_records
    return JSONResponse({"items": get_stale_records(days=days, limit=limit)})


@router.get("/workspace/stale-threads")
def api_stale_threads(request: Request, days: int = 7, limit: int = 5):
    from app.services.research_thread_service import get_stale_threads
    return JSONResponse({"threads": get_stale_threads(days=days, limit=limit)})


@router.get("/workspace/blocked")
def api_blocked_items(request: Request, limit: int = 10):
    from app.services.workspace_os_service import get_blocked_items
    return JSONResponse({"items": get_blocked_items(limit=limit)})


# ── API: Structured intake forms ─────────────────────────────────────────────

@router.post("/workspace/intake/research-candidate")
async def api_intake_research(request: Request):
    """Structured intake: new research candidate."""
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)
    ticker = str(body.get("ticker", "")).strip().upper()
    reason = str(body.get("reason", "")).strip()
    source_info = str(body.get("source", "")).strip()
    priority = str(body.get("priority", "normal")).strip()

    # Create a record in inbox
    rec_id = create_record(
        kind="inbox",
        title=f"Research Candidate: ${ticker}" if ticker else "Research Candidate",
        body=f"Reason: {reason}\nSource: {source_info}" if reason else "",
        ticker=ticker,
        priority=priority,
        source="intake_form",
        created_by="user",
        emoji="\U0001f52c",
    )
    return JSONResponse({"ok": rec_id > 0, "id": rec_id})


@router.post("/workspace/intake/project-idea")
async def api_intake_project(request: Request):
    """Structured intake: new project idea."""
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)
    title = str(body.get("title", "")).strip()
    description = str(body.get("description", "")).strip()
    goal = str(body.get("goal", "")).strip()
    priority = str(body.get("priority", "normal")).strip()

    rec_id = create_record(
        kind="inbox",
        title=f"Project Idea: {title}" if title else "Project Idea",
        body=f"Goal: {goal}\n\n{description}" if goal else description,
        priority=priority,
        source="intake_form",
        created_by="user",
        emoji="\U0001f680",
    )
    return JSONResponse({"ok": rec_id > 0, "id": rec_id})


@router.post("/workspace/intake/bookmark")
async def api_intake_bookmark(request: Request):
    """Structured intake: bookmark/link worth reviewing."""
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)
    url = str(body.get("url", "")).strip()
    title = str(body.get("title", "")).strip() or url
    notes = str(body.get("notes", "")).strip()
    ticker = str(body.get("ticker", "")).strip().upper()

    rec_id = create_record(
        kind="inbox",
        title=title,
        body=notes,
        ticker=ticker,
        url=url,
        source="intake_form",
        created_by="user",
        emoji="\U0001f517",
    )
    return JSONResponse({"ok": rec_id > 0, "id": rec_id})


# ── API: Today briefing ─────────────────────────────────────────────────────

@router.get("/workspace/today-briefing")
def api_today_briefing(request: Request):
    """Gather all data needed for the smart Today briefing."""
    import datetime as _dt
    today_iso = _dt.date.today().isoformat()
    DAYS = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    day_name = DAYS[_dt.date.today().weekday()]

    # Routines
    from app.services.playbook_service import get_todays_playbooks
    routines = get_todays_playbooks()
    routines_total = len(routines)
    routines_done = 0
    routines_in_progress = 0
    for pb in routines:
        lr = pb.get("last_run")
        if lr and (lr.get("started_at") or "")[:10] == today_iso:
            if lr.get("status") == "completed":
                routines_done += 1
            elif lr.get("status") == "in_progress":
                routines_in_progress += 1

    # Research threads
    from app.services.research_thread_service import list_threads, get_resume_threads, get_stale_threads
    active_threads = list_threads(status="active")
    resume_threads = get_resume_threads(limit=5)
    stale_threads = get_stale_threads(days=7, limit=5)
    open_questions = sum(t.get("open_questions", 0) for t in active_threads)
    open_blockers = sum(t.get("open_blockers", 0) for t in active_threads)

    # Tasks
    from app.services.workspace_os_service import get_overdue_tasks, get_stale_records, get_blocked_items
    overdue = get_overdue_tasks(limit=10)
    stale_records = get_stale_records(days=14, limit=10)
    blocked = get_blocked_items(limit=10)

    # Focus + backlog counts
    view = get_day_view()
    focus_items = view.get("focus", [])
    agent_items = view.get("agent_flagged", [])
    backlog_items = view.get("backlog", [])

    # Inbox
    from app.services.workspace_os_service import get_inbox_items
    inbox_items = get_inbox_items(limit=10)

    # Build briefing text
    parts = []

    # Routines line
    if routines_total > 0:
        if routines_done == routines_total:
            parts.append(f"All {routines_total} routines completed today.")
        elif routines_in_progress > 0:
            parts.append(f"{routines_total} routines today — {routines_done} done, {routines_in_progress} in progress.")
        else:
            first_routine = routines[0].get("name", "your routine") if routines else ""
            parts.append(f"{routines_total} routine{'s' if routines_total != 1 else ''} today — start with {first_routine}.")

    # Urgent items
    if overdue:
        parts.append(f"{len(overdue)} overdue task{'s' if len(overdue) != 1 else ''} need{'s' if len(overdue) == 1 else ''} attention.")
    if blocked:
        parts.append(f"{len(blocked)} item{'s' if len(blocked) != 1 else ''} blocked.")
    if inbox_items:
        parts.append(f"{len(inbox_items)} item{'s' if len(inbox_items) != 1 else ''} in inbox to triage.")

    # Research
    if active_threads:
        thread_parts = [f"{len(active_threads)} active research thread{'s' if len(active_threads) != 1 else ''}"]
        if open_questions:
            thread_parts.append(f"{open_questions} open question{'s' if open_questions != 1 else ''}")
        parts.append(" with ".join(thread_parts) + ".")
    if resume_threads:
        first_resume = resume_threads[0]
        left_off = first_resume.get("where_left_off", "")
        if left_off:
            parts.append(f"Continue {first_resume.get('title','')}: \"{left_off[:80]}\"")

    # Stale
    stale_total = len(stale_threads) + len(stale_records)
    if stale_total:
        parts.append(f"{stale_total} item{'s' if stale_total != 1 else ''} going stale.")

    # Backlog
    if backlog_items and not parts:
        parts.append(f"{len(backlog_items)} items in your backlog whenever you're ready.")

    # All clear
    all_clear = (routines_done == routines_total and not overdue and not blocked
                 and not inbox_items and not focus_items)

    briefing_text = " ".join(parts) if parts else "You're all clear. No urgent items today."

    # Autonomic intelligence brief
    autonomic_brief = {}
    try:
        from app.services.autonomic_service import build_morning_brief
        autonomic_brief = build_morning_brief()
        if autonomic_brief.get("headline") and autonomic_brief.get("overnight_alerts", 0) > 0:
            parts.insert(0, "\U0001f9e0 " + autonomic_brief["headline"])
            briefing_text = " ".join(parts) if parts else briefing_text
    except Exception:
        pass

    return JSONResponse({
        "briefing_text": briefing_text,
        "all_clear": all_clear,
        "day_name": day_name,
        "date": today_iso,
        "routines": {"total": routines_total, "done": routines_done, "in_progress": routines_in_progress, "items": routines},
        "overdue": overdue,
        "blocked": blocked,
        "inbox": inbox_items,
        "focus": focus_items,
        "agent_signals": agent_items,
        "resume_threads": resume_threads,
        "stale_threads": stale_threads,
        "stale_records": stale_records,
        "backlog": backlog_items,
        "autonomic": autonomic_brief,
        "stats": {
            "active_threads": len(active_threads),
            "open_questions": open_questions,
            "open_blockers": open_blockers,
            "overdue_count": len(overdue),
            "inbox_count": len(inbox_items),
            "backlog_count": len(backlog_items),
            "autonomic_alerts": autonomic_brief.get("overnight_alerts", 0),
        }
    })


# ── API: Autonomic alerts ─────────────────────────────────────────────────────

@router.get("/workspace/alerts")
def api_list_alerts(request: Request, status: str = "unread", limit: int = 30, alert_type: str = ""):
    from app.services.autonomic_service import list_alerts
    return JSONResponse({"alerts": list_alerts(status=status, limit=limit, alert_type=alert_type)})


@router.post("/workspace/alerts/{alert_id}/dismiss")
def api_dismiss_alert(alert_id: int):
    from app.services.autonomic_service import dismiss_alert
    ok = dismiss_alert(alert_id)
    return JSONResponse({"ok": ok})


@router.post("/workspace/alerts/dismiss-all")
def api_dismiss_all_alerts():
    from app.services.autonomic_service import dismiss_all_alerts
    ok = dismiss_all_alerts()
    return JSONResponse({"ok": ok})


@router.post("/workspace/autonomic/sweep")
def api_trigger_sweep():
    """Manually trigger an autonomic sweep (for testing)."""
    from app.services.autonomic_service import run_autonomic_sweep
    results = run_autonomic_sweep()
    return JSONResponse({"ok": True, "results": results})


# ── API: Calendar data ────────────────────────────────────────────────────────

@router.get("/workspace/calendar-data")
def api_calendar_data(
    request: Request,
    project_space: str = "",
    year: int = 0,
    month: int = 0,
):
    import datetime as _dt
    now = _dt.date.today()
    y = int(year) if year else now.year
    m = int(month) if month else now.month
    items = get_calendar_data(
        project_space=project_space or None,
        year=y,
        month=m,
    )
    return JSONResponse({"items": items, "year": y, "month": m})


# ── API: Projects ─────────────────────────────────────────────────────────────

@router.get("/workspace/projects")
def api_list_projects(request: Request):
    return JSONResponse({"projects": list_projects()})


@router.get("/workspace/projects-all")
def api_list_all_projects(request: Request):
    """Diagnostic: list ALL projects regardless of status."""
    from app.services.postgres_core_service import pg_connect
    con = pg_connect()
    if con is None:
        return JSONResponse({"error": "db"}, status_code=500)
    try:
        cur = con.cursor()
        cur.execute(
            """SELECT id, name, emoji, status, created_at
               FROM workspace_projects_core ORDER BY id"""
        )
        rows = []
        for r in cur.fetchall():
            rows.append({
                "id": r[0], "name": str(r[1] or ""), "emoji": str(r[2] or ""),
                "status": str(r[3] or ""), "created_at": str(r[4] or ""),
            })
        return JSONResponse({"all_projects": rows, "count": len(rows)})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    finally:
        con.close()


@router.post("/workspace/project")
def api_create_project(
    request: Request,
    name: str = Form(""),
    emoji: str = Form("🎯"),
    description: str = Form(""),
    color: str = Form("#6366f1"),
):
    if not name.strip():
        return JSONResponse({"ok": False, "error": "Name is required."}, status_code=400)
    proj_id = create_project(name=name.strip(), emoji=emoji, description=description, color=color)
    if proj_id <= 0:
        return JSONResponse({"ok": False, "error": "Could not save project."}, status_code=500)
    return JSONResponse({"ok": True, "id": proj_id})


@router.post("/workspace/project/{proj_id}/update")
def api_update_project(
    request: Request,
    proj_id: int,
    name: str = Form(""),
    emoji: str = Form(""),
    description: str = Form(""),
    color: str = Form(""),
):
    fields: dict = {}
    if name.strip():
        fields["name"] = name.strip()
    if emoji.strip():
        fields["emoji"] = emoji.strip()
    if description is not None:
        fields["description"] = description
    if color.strip():
        fields["color"] = color.strip()
    ok = update_project(proj_id, **fields)
    return JSONResponse({"ok": ok})


@router.post("/workspace/project/{proj_id}/delete")
def api_delete_project(request: Request, proj_id: int):
    ok = delete_project(proj_id)
    return JSONResponse({"ok": ok})


# ── API: Hub links ────────────────────────────────────────────────────────────

@router.get("/workspace/links")
def api_list_links(request: Request):
    return JSONResponse({"links": list_links()})


@router.post("/workspace/link")
def api_create_link(
    request: Request,
    title: str = Form(""),
    url: str = Form(""),
    category: str = Form("General"),
    description: str = Form(""),
    pinned: str = Form("false"),
):
    if not title.strip() or not url.strip():
        return JSONResponse({"ok": False, "error": "Title and URL are required."}, status_code=400)
    link_id = create_link(
        title=title.strip(),
        url=url.strip(),
        category=category.strip() or "General",
        description=description,
        pinned=str(pinned).lower() in ("1", "true", "yes"),
    )
    if link_id <= 0:
        return JSONResponse({"ok": False, "error": "Could not save link."}, status_code=500)
    return JSONResponse({"ok": True, "id": link_id})


@router.post("/workspace/link/{link_id}/update")
def api_update_link(
    request: Request,
    link_id: int,
    title: str = Form(""),
    url: str = Form(""),
    category: str = Form(""),
    description: str = Form(""),
):
    fields: dict = {}
    if title.strip():
        fields["title"] = title.strip()
    if url.strip():
        fields["url"] = url.strip()
    if category.strip():
        fields["category"] = category.strip()
    if description is not None:
        fields["description"] = description
    ok = update_link(link_id, **fields)
    return JSONResponse({"ok": ok})


@router.post("/workspace/link/{link_id}/delete")
def api_delete_link(request: Request, link_id: int):
    ok = delete_link(link_id)
    return JSONResponse({"ok": ok})


# ── Financial Lab ────────────────────────────────────────────────────────────

@router.get("/lab/{ticker}", response_class=HTMLResponse)
def lab_page(request: Request, ticker: str):
    tk = str(ticker or "").strip().upper()
    ctx = get_lab_context(tk)
    checklist = score_buffett_checklist(tk, ctx)

    # Company name
    company_name = tk
    try:
        nm = company_name_map()
        company_name = nm.get(tk, tk)
    except Exception:
        pass

    return _templates(request).TemplateResponse(
        "financial_lab.html",
        {
            "request": request,
            "ticker": tk,
            "company_name": company_name,
            "ctx": ctx,
            "checklist": checklist,
        },
    )


@router.get("/api/lab/{ticker}/context")
def api_lab_context(request: Request, ticker: str):
    tk = str(ticker or "").strip().upper()
    return JSONResponse(get_lab_context(tk))


@router.get("/api/lab/{ticker}/checklist")
def api_lab_checklist(request: Request, ticker: str):
    tk = str(ticker or "").strip().upper()
    return JSONResponse(score_buffett_checklist(tk))


@router.post("/api/lab/{ticker}/case-study")
def api_save_case_study(
    request: Request,
    ticker: str,
    title: str = Form(""),
    module: str = Form("dcf"),
    params_json: str = Form("{}"),
    result_json: str = Form("{}"),
    notes: str = Form(""),
):
    tk = str(ticker or "").strip().upper()
    if not title.strip():
        return JSONResponse({"ok": False, "error": "Title is required."}, status_code=400)
    import json
    try:
        params = json.loads(params_json)
    except Exception:
        params = {}
    try:
        result = json.loads(result_json)
    except Exception:
        result = {}
    case_id = save_case_study(tk, title.strip(), module, params, result, notes)
    if case_id <= 0:
        return JSONResponse({"ok": False, "error": "Could not save."}, status_code=500)
    return JSONResponse({"ok": True, "id": case_id})


@router.get("/api/lab/{ticker}/case-studies")
def api_list_case_studies(request: Request, ticker: str):
    tk = str(ticker or "").strip().upper()
    return JSONResponse({"cases": list_case_studies(tk)})


@router.delete("/api/lab/{ticker}/case-study/{case_id}")
def api_delete_case_study(request: Request, ticker: str, case_id: int):
    ok = delete_case_study(case_id)
    return JSONResponse({"ok": ok})


@router.post("/api/lab/{ticker}/simulate-scenario")
def api_simulate_scenario(
    request: Request,
    ticker: str,
    scenario: str = Form(""),
):
    tk = str(ticker or "").strip().upper()
    if not scenario.strip():
        return JSONResponse({"ok": False, "error": "Scenario is required."}, status_code=400)
    result = simulate_scenario(tk, scenario.strip())
    status = 200 if result.get("ok") else 500
    return JSONResponse(result, status_code=status)


@router.get("/api/lab/{ticker}/risk-diff")
def api_lab_risk_diff(request: Request, ticker: str):
    tk = str(ticker or "").strip().upper()
    return JSONResponse(diff_risk_factors(tk))


# ═══════════════════════════════════════════════════════════════════════════════
#  THINKING LAB — chains, quotes, bookmarks, books, mental models
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/workspace/thinking")
def api_list_chains(request: Request, chain_type: str = "", status: str = "active"):
    from app.services.thinking_service import list_chains
    ct = chain_type.strip() or None
    chains = list_chains(chain_type=ct, status=status)
    return JSONResponse({"chains": chains})


@router.get("/workspace/thinking/{chain_id}")
def api_get_chain(request: Request, chain_id: int):
    from app.services.thinking_service import get_chain
    chain = get_chain(chain_id)
    if not chain:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse({"chain": chain})


@router.post("/workspace/thinking")
async def api_create_chain(request: Request):
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        data = await request.json()
    else:
        form = await request.form()
        data = dict(form)
    from app.services.thinking_service import create_chain
    chain_id = create_chain(
        chain_type=str(data.get("chain_type", "chain")),
        title=str(data.get("title", "")),
        emoji=str(data.get("emoji", "💡")),
        tags=str(data.get("tags", "")),
        source=str(data.get("source", "")),
        notes=str(data.get("notes", "")),
    )
    if chain_id:
        return JSONResponse({"ok": True, "id": chain_id})
    return JSONResponse({"ok": False, "error": "Failed to create"}, status_code=500)


@router.put("/workspace/thinking/{chain_id}")
async def api_update_chain(request: Request, chain_id: int):
    data = await request.json()
    from app.services.thinking_service import update_chain
    ok = update_chain(chain_id, **data)
    return JSONResponse({"ok": ok})


@router.delete("/workspace/thinking/{chain_id}")
def api_delete_chain(request: Request, chain_id: int):
    from app.services.thinking_service import delete_chain
    ok = delete_chain(chain_id)
    return JSONResponse({"ok": ok})


@router.post("/workspace/thinking/{chain_id}/archive")
def api_archive_chain(request: Request, chain_id: int):
    from app.services.thinking_service import archive_chain
    ok = archive_chain(chain_id)
    return JSONResponse({"ok": ok})


@router.post("/workspace/thinking/{chain_id}/node")
async def api_add_node(request: Request, chain_id: int):
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        data = await request.json()
    else:
        form = await request.form()
        data = dict(form)
    from app.services.thinking_service import add_node
    parent_id = data.get("parent_node_id")
    if parent_id is not None and str(parent_id).strip():
        parent_id = int(parent_id)
    else:
        parent_id = None
    sort_order = data.get("sort_order")
    if sort_order is not None and str(sort_order).strip():
        sort_order = int(sort_order)
    else:
        sort_order = None
    node_id = add_node(
        chain_id=chain_id,
        parent_node_id=parent_id,
        node_type=str(data.get("node_type", "idea")),
        content=str(data.get("content", "")),
        source_url=str(data.get("source_url", "")),
        sort_order=sort_order,
    )
    if node_id:
        return JSONResponse({"ok": True, "id": node_id})
    return JSONResponse({"ok": False, "error": "Failed to add node"}, status_code=500)


@router.put("/workspace/thinking-node/{node_id}")
async def api_update_node(request: Request, node_id: int):
    data = await request.json()
    from app.services.thinking_service import update_node
    ok = update_node(
        node_id,
        content=data.get("content"),
        node_type=data.get("node_type"),
        sort_order=data.get("sort_order"),
        source_url=data.get("source_url"),
    )
    return JSONResponse({"ok": ok})


@router.delete("/workspace/thinking-node/{node_id}")
def api_delete_node(request: Request, node_id: int):
    from app.services.thinking_service import delete_node
    ok = delete_node(node_id)
    return JSONResponse({"ok": ok})


@router.get("/workspace/thinking-node/{node_id}/lineage")
def api_node_lineage(request: Request, node_id: int):
    """Get ancestor lineage from root to this node (the 'aisle view' within a chain)."""
    from app.services.thinking_service import get_node_ancestors
    ancestors = get_node_ancestors(node_id)
    return JSONResponse({"lineage": ancestors})


@router.get("/workspace/thinking/{chain_id}/leaves")
def api_chain_leaves(request: Request, chain_id: int):
    """Get all leaf nodes (final decisions/conclusions) in a chain."""
    from app.services.thinking_service import get_all_leaf_nodes
    leaves = get_all_leaf_nodes(chain_id)
    return JSONResponse({"leaves": leaves})


# ── API: Cognitive Engine endpoints (Phase 4) ─────────────────────────────────

# -- Assumptions --
@router.get("/api/assumptions")
def api_list_assumptions(request: Request, ticker: str = "", status: str = ""):
    from app.services.cognitive_engine_service import list_assumptions
    return JSONResponse(list_assumptions(ticker=ticker, status=status))

@router.post("/api/assumption")
def api_create_assumption(
    request: Request,
    ticker: str = Form(""),
    assumption_text: str = Form(""),
    condition_type: str = Form("qualitative"),
    measurable_metric: str = Form(""),
    threshold_operator: str = Form(">="),
    threshold_value: str = Form(""),
    importance: str = Form("core"),
):
    from app.services.cognitive_engine_service import create_assumption
    tv = float(threshold_value) if threshold_value else None
    aid = create_assumption(
        ticker=ticker, assumption_text=assumption_text,
        condition_type=condition_type, measurable_metric=measurable_metric,
        threshold_operator=threshold_operator, threshold_value=tv,
        importance=importance,
    )
    return JSONResponse({"ok": bool(aid), "id": aid})

@router.post("/api/assumption/{aid}/update")
def api_update_assumption(request: Request, aid: int, status: str = Form("")):
    from app.services.cognitive_engine_service import update_assumption
    ok = update_assumption(aid, status=status) if status else False
    return JSONResponse({"ok": ok})

@router.post("/api/assumption/{aid}/delete")
def api_delete_assumption(request: Request, aid: int):
    from app.services.cognitive_engine_service import delete_assumption
    return JSONResponse({"ok": delete_assumption(aid)})

# -- Conviction --
@router.get("/api/conviction")
def api_list_convictions(request: Request):
    from app.services.cognitive_engine_service import list_convictions
    return JSONResponse(list_convictions())

@router.get("/api/conviction/{ticker}")
def api_get_conviction(request: Request, ticker: str):
    from app.services.cognitive_engine_service import get_conviction, get_conviction_history
    conv = get_conviction(ticker)
    history = get_conviction_history(ticker, limit=20)
    return JSONResponse({"current": conv, "history": history})

@router.post("/api/conviction/{ticker}/refresh")
def api_refresh_conviction(request: Request, ticker: str):
    from app.services.cognitive_engine_service import update_conviction
    result = update_conviction(ticker, trigger_event="manual_refresh", trigger_type="user")
    return JSONResponse(result)

# -- Triggers --
@router.get("/api/triggers")
def api_list_triggers(request: Request, ticker: str = "", status: str = "active"):
    from app.services.cognitive_engine_service import list_triggers
    return JSONResponse(list_triggers(ticker=ticker, status=status))

@router.post("/api/trigger")
def api_create_trigger(
    request: Request,
    ticker: str = Form(""),
    trigger_name: str = Form(""),
    trigger_type: str = Form("price_below"),
    condition_text: str = Form(""),
    threshold_value: str = Form(""),
    comparison_op: str = Form("<"),
    action_text: str = Form(""),
    cooldown_hours: int = Form(24),
):
    from app.services.cognitive_engine_service import create_trigger
    tv = float(threshold_value) if threshold_value else None
    tid = create_trigger(
        ticker=ticker, trigger_name=trigger_name, trigger_type=trigger_type,
        condition_text=condition_text, threshold_value=tv,
        comparison_op=comparison_op, action_text=action_text,
        cooldown_hours=cooldown_hours,
    )
    return JSONResponse({"ok": bool(tid), "id": tid})

@router.post("/api/trigger/{tid}/delete")
def api_delete_trigger(request: Request, tid: int):
    from app.services.cognitive_engine_service import delete_trigger
    return JSONResponse({"ok": delete_trigger(tid)})

@router.post("/api/trigger/{tid}/disable")
def api_disable_trigger(request: Request, tid: int):
    from app.services.cognitive_engine_service import disable_trigger
    return JSONResponse({"ok": disable_trigger(tid)})

# -- Coherence --
@router.get("/api/coherence")
def api_portfolio_coherence(request: Request):
    from app.services.cognitive_engine_service import compute_portfolio_coherence
    return JSONResponse(compute_portfolio_coherence())

# -- Synthesis --
@router.get("/api/synthesis/{ticker}")
def api_get_synthesis(request: Request, ticker: str):
    from app.services.cognitive_engine_service import generate_synthesis
    return JSONResponse(generate_synthesis(ticker))

# -- Signal preferences --
@router.get("/api/signal-preferences")
def api_signal_preferences(request: Request):
    from app.services.cognitive_engine_service import get_signal_preferences
    return JSONResponse(get_signal_preferences())

@router.post("/api/signal-action")
def api_record_signal_action(
    request: Request,
    signal_type: str = Form(""),
    action: str = Form(""),
    signal_id: str = Form(""),
    ticker: str = Form(""),
):
    from app.services.cognitive_engine_service import record_signal_action
    sid = int(signal_id) if signal_id else None
    ok = record_signal_action(signal_type=signal_type, action=action, signal_id=sid, ticker=ticker)
    return JSONResponse({"ok": ok})


# ── API: Context Panel endpoints (Layer 3) ────────────────────────────────────

@router.get("/api/context-panel/{ticker}")
def api_context_panel_ticker(request: Request, ticker: str):
    """Return lightweight context data for the universal context panel."""
    tk = str(ticker or "").strip().upper()
    if not tk:
        return JSONResponse({"error": "empty ticker"}, status_code=400)
    from app.services.ticker_hub_service import get_ticker_hub
    hub = get_ticker_hub(tk)
    # Fetch thesis
    thesis_text = ""
    try:
        from app.services.portfolio_memory_service import list_watchlist_thesis_pg
        for t in list_watchlist_thesis_pg(limit=200):
            if str(t.get("ticker") or "").upper() == tk:
                thesis_text = str(t.get("thesis") or "")
                break
    except Exception:
        pass
    return JSONResponse({
        "ticker": tk,
        "position": hub.get("position"),
        "thesis": thesis_text,
        "proposals": (hub.get("proposals") or [])[:5],
        "research_threads": (hub.get("research_threads") or [])[:4],
        "thinking_chains": (hub.get("thinking_chains") or [])[:4],
        "cascades": (hub.get("cascades") or [])[:3],
        "work_graph": (hub.get("work_graph") or [])[:6],
    })


@router.get("/api/context-panel/portfolio")
def api_context_panel_portfolio(request: Request):
    """Return portfolio-level context for day/dashboard views."""
    from app.services.ticker_hub_service import _safe_query
    recent_proposals = _safe_query(
        "SELECT ticker, headline, action_type, created_at, confidence "
        "FROM action_proposals_core ORDER BY created_at DESC LIMIT 8",
    )
    active_threads = _safe_query(
        "SELECT id, ticker, title, status, updated_at "
        "FROM research_threads_core WHERE status = 'active' "
        "ORDER BY updated_at DESC LIMIT 6",
    )
    return JSONResponse({
        "recent_proposals": recent_proposals,
        "active_threads": active_threads,
    })


@router.get("/workspace/object-graph")
def api_object_graph(request: Request, obj_type: str = "", obj_id: str = ""):
    """BFS traversal of work graph — cross-object lineage map."""
    from app.services.work_graph_service import get_object_graph
    if not obj_type or not obj_id:
        return JSONResponse({"error": "obj_type and obj_id required"}, status_code=400)
    graph = get_object_graph(obj_type, obj_id, max_depth=4)
    return JSONResponse(graph)


# ── Research Notes (Research Canvas) ──────────────────────────────────────────

@router.get("/api/research-notes/{ticker}")
def api_list_research_notes(ticker: str):
    from app.services.research_notes_service import list_notes
    import datetime as _dt
    notes = list_notes(ticker)
    for n in notes:
        for k, v in n.items():
            if isinstance(v, _dt.datetime):
                n[k] = v.isoformat()
    return JSONResponse({"ok": True, "notes": notes})


@router.post("/api/research-note")
def api_create_research_note(request: Request, ticker: str = Form(""), note: str = Form(""), sentiment: str = Form("neutral")):
    from app.services.research_notes_service import create_note
    import datetime as _dt
    if not ticker or not note.strip():
        return JSONResponse({"ok": False, "error": "ticker and note required"}, status_code=400)
    created = create_note(ticker, note, sentiment)
    for k, v in created.items():
        if isinstance(v, _dt.datetime):
            created[k] = v.isoformat()
    return JSONResponse({"ok": True, "note": created})


@router.post("/api/research-note/{note_id}/update")
def api_update_research_note(note_id: int, request: Request, pinned: str = Form(None), sentiment: str = Form(None)):
    from app.services.research_notes_service import update_note
    kwargs = {}
    if pinned is not None:
        kwargs["pinned"] = pinned.lower() in ("true", "1", "yes")
    if sentiment is not None:
        kwargs["sentiment"] = sentiment
    ok = update_note(note_id, **kwargs)
    return JSONResponse({"ok": ok})


@router.post("/api/research-note/{note_id}/delete")
def api_delete_research_note(note_id: int):
    from app.services.research_notes_service import delete_note
    ok = delete_note(note_id)
    return JSONResponse({"ok": ok})


@router.get("/api/research-canvas/{ticker}")
def api_research_canvas(ticker: str):
    """Aggregated research canvas data for a ticker — parallel fetches."""
    import datetime as _dt
    import re
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from app.services.ticker_hub_service import _safe_query
    from app.services.research_notes_service import list_notes

    tk = ticker.strip().upper()
    if not tk:
        return JSONResponse({"ok": False, "error": "ticker required"}, status_code=400)

    def _ser(rows):
        out = []
        for r in (rows or []):
            row = dict(r)
            for k, v in row.items():
                if isinstance(v, _dt.datetime):
                    row[k] = v.isoformat()
            out.append(row)
        return out

    # ── Parallel fetch all data sources ──
    results = {}

    def _fetch_notes():
        return _ser(list_notes(tk))

    def _fetch_thesis():
        rows = _safe_query(
            "SELECT ticker, rationale, notes, thesis FROM watchlist_core "
            "WHERE UPPER(ticker) = %s LIMIT 1", (tk,))
        if rows:
            t = rows[0]
            return {"thesis": t.get("thesis") or "", "rationale": t.get("rationale") or "", "notes": t.get("notes") or ""}
        return {}

    def _fetch_threads():
        return _ser(_safe_query(
            "SELECT id, title, thesis, status, emoji, created_at, updated_at "
            "FROM research_threads_core WHERE UPPER(ticker) = %s AND status != 'deleted' "
            "ORDER BY updated_at DESC LIMIT 5", (tk,)))

    def _fetch_chains():
        return _ser(_safe_query(
            "SELECT c.id, c.title, c.chain_type, c.emoji, c.notes, c.tags, c.created_at, c.updated_at, "
            "COUNT(n.id) as node_count "
            "FROM thinking_chains_core c "
            "LEFT JOIN thinking_nodes_core n ON n.chain_id = c.id "
            "WHERE (UPPER(c.tags) LIKE %s OR UPPER(c.title) LIKE %s) AND c.status != 'deleted' "
            "GROUP BY c.id ORDER BY c.updated_at DESC LIMIT 5",
            (f"%{tk}%", f"%{tk}%")))

    def _fetch_earnings():
        return _ser(_safe_query(
            "SELECT period, revenue_actual, eps_actual, guidance_direction, "
            "management_tone, key_metrics, created_at "
            "FROM earnings_analysis_core WHERE UPPER(ticker) = %s "
            "ORDER BY created_at DESC LIMIT 4", (tk,)))

    def _fetch_insider():
        return _ser(_safe_query(
            "SELECT created_at, headline, summary FROM events_core "
            "WHERE event_type = 'insider_trade_signal' AND UPPER(ticker) = %s "
            "ORDER BY created_at DESC LIMIT 6", (tk,)))

    def _fetch_filings():
        return _ser(_safe_query(
            "SELECT form_type, filed_date, headline, accession_number, created_at "
            "FROM events_core WHERE event_type = 'sec_filing' AND UPPER(ticker) = %s "
            "ORDER BY created_at DESC LIMIT 6", (tk,)))

    def _fetch_live():
        # Use cached macro snapshot first, fallback to single yfinance call with timeout
        try:
            from app.services.web_search_service import get_live_macro_snapshot
            snap = get_live_macro_snapshot(timeout_sec=1.0)
            if snap and tk in snap:
                s = snap[tk]
                return {"price": s.get("price", 0), "change_pct": s.get("change_pct", 0)}
        except Exception:
            pass
        # Quick yfinance fallback (2s timeout)
        try:
            import yfinance as yf
            t = yf.Ticker(tk)
            info = t.fast_info
            price = round(float(info.get("lastPrice", 0)), 2)
            prev = float(info.get("previousClose", 0))
            chg = round(((price - prev) / prev) * 100, 2) if prev > 0 else 0
            return {"price": price, "change_pct": chg}
        except Exception:
            return {}

    def _fetch_wl_tickers():
        rows = _safe_query("SELECT DISTINCT UPPER(ticker) as ticker FROM watchlist_core")
        return set(r["ticker"] for r in (rows or []))

    # Run all fetches in parallel
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {
            pool.submit(_fetch_notes): "notes",
            pool.submit(_fetch_thesis): "thesis",
            pool.submit(_fetch_threads): "threads",
            pool.submit(_fetch_chains): "chains",
            pool.submit(_fetch_earnings): "earnings",
            pool.submit(_fetch_insider): "insider",
            pool.submit(_fetch_filings): "filings",
            pool.submit(_fetch_live): "live",
            pool.submit(_fetch_wl_tickers): "wl_set",
        }
        for fut in as_completed(futs):
            key = futs[fut]
            try:
                results[key] = fut.result()
            except Exception:
                results[key] = [] if key not in ("thesis", "live", "wl_set") else ({} if key != "wl_set" else set())

    notes = results.get("notes", [])
    thesis_data = results.get("thesis", {})
    threads = results.get("threads", [])
    chains = results.get("chains", [])
    earnings = results.get("earnings", [])
    insider = results.get("insider", [])
    filings = results.get("filings", [])
    live = results.get("live", {})
    wl_set = results.get("wl_set", set())

    # Fetch thread entries (depends on threads result)
    all_entries = []
    open_questions = []
    findings = []
    if threads:
        thread_ids = [t["id"] for t in threads if t.get("id")]
        if thread_ids:
            placeholders = ",".join(["%s"] * len(thread_ids))
            raw_entries = _ser(_safe_query(
                f"SELECT id, thread_id, kind, content, status, source_url, created_at "
                f"FROM thread_entries_core WHERE thread_id IN ({placeholders}) ORDER BY created_at DESC",
                tuple(thread_ids)
            ))
            thread_map = {t["id"]: t.get("title", "") for t in threads}
            for e in raw_entries:
                e["thread_title"] = thread_map.get(e.get("thread_id"), "")
                all_entries.append(e)
                if e.get("kind") == "question" and e.get("status") != "resolved":
                    open_questions.append(e)
                if e.get("kind") == "finding":
                    findings.append(e)

    # Connected tickers
    all_text = " ".join([n.get("note", "") for n in notes])
    all_text += " ".join([e.get("content", "") for e in all_entries])
    all_text += " ".join([c.get("notes", "") + " " + c.get("title", "") for c in chains])
    mentioned = set(re.findall(r'\$([A-Z]{1,5})\b', all_text))
    for word in re.findall(r'\b([A-Z]{2,5})\b', all_text):
        if word in wl_set and word != tk:
            mentioned.add(word)
    mentioned.discard(tk)

    # Build timeline
    timeline = []
    for n in notes:
        timeline.append({"type": "note", "date": n.get("created_at", ""), "sentiment": n.get("sentiment", ""),
                         "text": n.get("note", "")[:200], "id": n.get("id")})
    for e in all_entries:
        timeline.append({"type": e.get("kind", "note"), "date": e.get("created_at", ""),
                         "text": e.get("content", "")[:200], "thread": e.get("thread_title", ""), "id": e.get("id")})
    timeline.sort(key=lambda x: x.get("date", ""), reverse=True)

    return JSONResponse({
        "ok": True,
        "ticker": tk,
        "thesis": thesis_data,
        "notes": notes,
        "supports": [n for n in notes if n.get("sentiment") == "supports"],
        "challenges": [n for n in notes if n.get("sentiment") == "challenges"],
        "open_questions": open_questions,
        "findings": findings,
        "threads": threads,
        "chains": chains,
        "earnings": earnings,
        "insider_trades": insider,
        "filings": filings,
        "connections": sorted(list(mentioned)),
        "live": live,
        "timeline": timeline[:30],
    })
