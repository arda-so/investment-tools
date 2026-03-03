from __future__ import annotations

import datetime as dt
import html
import mimetypes
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, JSONResponse

from app.core import cloud_files
from app.core.filing_text import normalize_filing_rel_path, read_filing_text_any
from app.core.http import is_hx_request
from app.services.company_file_service import (
    add_company_note,
    add_company_reminder,
    add_company_task,
    add_competitor,
    company_detail,
    index_filters,
    list_companies,
    list_filters,
    moat_filters,
    remove_competitor,
    remove_supply_chain_link,
    save_company_moats,
    safe_resolve_filing_path,
    add_supply_chain_link,
    toggle_company_reminder,
    toggle_company_task,
    update_competitor,
    update_company_note,
    update_company_reminder,
    update_company_task,
    delete_company_note,
    delete_company_reminder,
    delete_company_task,
)
from app.services.sec_sync_state import load_sec_sync_state, set_ticker_sync_state
from app.services.google_workspace_service import send_email
from app.services.organizer_service import add_general_note, suggest_ir_emails
from app.services.portfolio_memory_service import get_holdings
from app.services.price_metrics_service import refresh_price_metrics
from app.services.mini_statements_service import refresh_mini_statements, fetch_historical_financials, compute_financial_deltas
from app.services.company_intel_service import refresh_company_intel
from app.services.postgres_core_service import insert_earnings_call_ingest_run_pg
from app.services.sec_ingest_pipeline_service import ingest_sec_facts_for_ticker
from app.services.sec_edgar_poller_service import poll_ticker, poll_and_ingest_tickers


router = APIRouter()
_SYNC_LOCK = threading.Lock()
_SEC_SYNC_STATE: dict[str, dict[str, str]] = {}


def _parse_ticker_list(raw: str) -> list[str]:
    parts = re.split(r"[\s,;]+", str(raw or "").upper())
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        tk = re.sub(r"[^A-Z0-9.\-]", "", p).strip()
        if not tk or tk in seen:
            continue
        seen.add(tk)
        out.append(tk)
    return out


def _parse_show_ai_flag(raw: object) -> bool | None:
    if raw is None:
        return None
    val = str(raw).strip().lower()
    if val in {"1", "true", "yes", "on"}:
        return True
    if val in {"0", "false", "no", "off"}:
        return False
    return None


def _resolve_show_ai(request: Request, fallback: bool = False) -> bool:
    direct = _parse_show_ai_flag(request.query_params.get("show_ai"))
    if direct is not None:
        return direct

    hx_current = request.headers.get("HX-Current-URL", "")
    if hx_current:
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(hx_current).query)
            parsed = _parse_show_ai_flag((q.get("show_ai") or [None])[0])
            if parsed is not None:
                return parsed
        except Exception:
            pass

    referer = request.headers.get("Referer", "")
    if referer:
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(referer).query)
            parsed = _parse_show_ai_flag((q.get("show_ai") or [None])[0])
            if parsed is not None:
                return parsed
        except Exception:
            pass
    return bool(fallback)


def _portfolio_context_for_ticker(ticker: str) -> dict[str, object]:
    tk = str(ticker or "").strip().upper()
    if not tk:
        return {"is_held": False, "shares": 0.0, "avg_cost": 0.0, "position_cost": 0.0}
    shares = 0.0
    avg_cost = 0.0
    try:
        for r in get_holdings(limit=3000):
            if str(r.get("ticker") or "").strip().upper() != tk:
                continue
            shares = float(r.get("shares") or 0.0)
            avg_cost = float(r.get("cost") or 0.0)
            break
    except Exception:
        shares = 0.0
        avg_cost = 0.0
    pos_cost = shares * avg_cost
    return {
        "is_held": bool(shares > 0),
        "shares": float(shares),
        "avg_cost": float(avg_cost),
        "position_cost": float(pos_cost),
    }


def _render_company_detail(request: Request, ticker: str, message: str = "", show_ai: bool | None = None):
    templates = request.app.state.templates
    show_ai_flag = _resolve_show_ai(request, fallback=bool(show_ai))
    detail = company_detail(ticker)
    if detail:
        ev = list(detail.get("timeline_events") or [])
        detail["timeline_events_visible"] = ev if show_ai_flag else [x for x in ev if not bool(x.get("is_ai"))]
        detail["show_ai"] = "1" if show_ai_flag else "0"
    portfolio_context = _portfolio_context_for_ticker(ticker)
    ir_suggestions = suggest_ir_emails(ticker=str(ticker or "").strip().upper(), company=str((detail or {}).get("name") or ""), limit=8) if detail else []
    if not detail:
        if is_hx_request(request):
            return templates.TemplateResponse(
                "components/company_detail_body.html",
                {"request": request, "message": "Company not found.", "detail": {"ticker": ticker, "name": ticker, "country": "-", "industry": "Unknown", "market_cap": "-", "moat_options": [], "moat_keys": [], "moats": [], "competitors": [], "notes": [], "tasks": [], "reminders": [], "filings": [], "timeline_events": [], "timeline_events_visible": [], "show_ai": "0"}, "ir_suggestions": [], "portfolio_context": portfolio_context},
            )
        return RedirectResponse(url="/company_file?msg=" + urllib.parse.quote("Company not found."), status_code=303)
    if is_hx_request(request):
        return templates.TemplateResponse(
            "components/company_detail_body.html",
            {"request": request, "message": message, "detail": detail, "ir_suggestions": ir_suggestions, "portfolio_context": portfolio_context},
        )
    return templates.TemplateResponse(
        "company_detail.html",
        {"request": request, "message": message, "detail": detail, "ir_suggestions": ir_suggestions, "portfolio_context": portfolio_context},
    )


def _back_to_ticker(ticker: str, msg: str = "") -> RedirectResponse:
    t = str(ticker or "").strip().upper()
    url = f"/company_file?t={urllib.parse.quote(t)}"
    if msg:
        url += "&msg=" + urllib.parse.quote(msg)
    return RedirectResponse(url=url, status_code=303)


def _derive_sec_primary_doc_url(doc_url: str, accession: str, file_name: str) -> str:
    u = str(doc_url or "").strip()
    acc = re.sub(r"[^0-9]", "", str(accession or ""))
    fn = str(file_name or "").strip()
    if not u or not acc or not fn or "/" in fn:
        return ""
    try:
        pu = urllib.parse.urlsplit(u)
        m = re.search(r"(/Archives/edgar/data/\d+)/", str(pu.path or ""), flags=re.IGNORECASE)
        if not m:
            return ""
        base = m.group(1).rstrip("/")
        new_path = f"{base}/{acc}/{fn}"
        return urllib.parse.urlunsplit((pu.scheme or "https", pu.netloc, new_path, "", ""))
    except Exception:
        return ""


def _is_synthetic_cached_name(file_name: str, ticker: str) -> bool:
    fn = str(file_name or "").strip()
    tk = str(ticker or "").strip().upper()
    if not fn:
        return True
    up = fn.upper()
    if tk and up.startswith(f"{tk}_"):
        return True
    return bool(re.match(r"^[A-Z0-9.\-]+_(10-K|10-Q|8-K|20-F|6-K|DEF ?14A)_[0-9\-]+\.TXT$", up))


def _fetch_sec_primary_from_index(doc_url: str, desired_form: str = "") -> str:
    u = str(doc_url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return ""
    if not u.lower().endswith("-index.html"):
        return ""
    try:
        req = urllib.request.Request(
            u,
            headers={
                "User-Agent": "InvestorOS SEC Open/1.0 (research@investoros.local)",
                "Accept": "text/html, */*",
            },
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        return ""
    if not raw:
        return ""
    block = raw
    m = re.search(r"(?is)Document Format Files.*?<table.*?</table>", raw)
    if m:
        block = m.group(0)
    def _sec_ix_doc_target(u_full: str) -> str:
        try:
            pu = urllib.parse.urlsplit(str(u_full or ""))
            q = urllib.parse.parse_qs(str(pu.query or ""))
            d = str((q.get("doc") or [""])[0] or "").strip()
            if not d:
                return ""
            return urllib.parse.urljoin("https://www.sec.gov", d)
        except Exception:
            return ""

    form_norm = re.sub(r"\s+", "", str(desired_form or "").upper())
    rows = re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", block)
    if form_norm and rows:
        for row in rows:
            cells = re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)
            if len(cells) < 4:
                continue
            type_text = re.sub(r"(?is)<[^>]+>", " ", str(cells[3] or ""))
            type_norm = re.sub(r"\s+", "", type_text).upper()
            if type_norm != form_norm:
                continue
            doc_cell = str(cells[2] or "")
            for href in re.findall(r'(?is)href="([^"]+)"', doc_cell):
                h = str(href or "").strip()
                if not h or h.lower().startswith("javascript:"):
                    continue
                abs_u = urllib.parse.urljoin(u, h)
                low = abs_u.lower()
                if "ix?doc=" in low:
                    tgt = _sec_ix_doc_target(abs_u)
                    if tgt:
                        return tgt
                    continue
                if low.endswith((".htm", ".html", ".xhtml", ".txt", ".xml")):
                    return abs_u
    for href in re.findall(r'(?is)href="([^"]+)"', block):
        h = str(href or "").strip()
        if not h or h.lower().startswith("javascript:"):
            continue
        abs_u = urllib.parse.urljoin(u, h)
        low = abs_u.lower()
        if "ix?doc=" in low:
            tgt = _sec_ix_doc_target(abs_u)
            if tgt:
                return tgt
            continue
        if low.endswith((".htm", ".html", ".xhtml", ".txt", ".xml")):
            return abs_u
    return ""


def _apply_sec_open_links(detail: dict, ticker: str, prefer_sec_direct_open: bool) -> None:
    tk = urllib.parse.quote(str(ticker or "").strip().upper())
    groups = list(detail.get("filing_groups") or [])
    for g in groups:
        rows = list(g.get("rows") or [])
        for r in rows:
            pth = str(r.get("path") or "").strip()
            doc_url = str(r.get("doc_url") or "").strip()
            file_name = str(r.get("file_name") or "").strip()
            accession = str(r.get("accession") or "").strip()
            if prefer_sec_direct_open and doc_url:
                r["open_href"] = (
                    "/company_file/sec-open?doc="
                    + urllib.parse.quote(doc_url, safe="")
                    + "&t="
                    + tk
                    + "&acc="
                    + urllib.parse.quote(accession, safe="")
                    + "&fn="
                    + urllib.parse.quote(file_name, safe="")
                    + "&form="
                    + urllib.parse.quote(str(r.get("form") or ""), safe="")
                )
                r["open_external"] = "0"
                r["open_label"] = "Open"
            elif pth:
                r["open_href"] = (
                    "/filing?path="
                    + urllib.parse.quote(pth, safe="")
                    + "&doc="
                    + urllib.parse.quote(doc_url, safe="")
                    + "&t="
                    + tk
                    + "&mode=original"
                )
                r["open_external"] = "0"
                r["open_label"] = "Open"
            elif doc_url:
                r["open_href"] = doc_url
                r["open_external"] = "1"
                r["open_label"] = "Open SEC"
            else:
                r["open_href"] = ""
                r["open_external"] = "0"
                r["open_label"] = "N/A"


@router.get("/company_file/sec-open")
def company_sec_open(doc: str = "", t: str = "", acc: str = "", fn: str = "", form: str = ""):
    doc_url = str(doc or "").strip()
    if not doc_url.lower().startswith(("http://", "https://")):
        return _back_to_ticker(str(t or ""), "SEC document URL is missing.")
    ticker = str(t or "").strip().upper()
    file_name = str(fn or "").strip()
    target = ""
    if file_name and not _is_synthetic_cached_name(file_name, ticker):
        target = _derive_sec_primary_doc_url(doc_url, str(acc or "").strip(), file_name)
    if not target:
        target = _fetch_sec_primary_from_index(doc_url, desired_form=form)
    if not target:
        target = doc_url
    return RedirectResponse(url=target, status_code=302)


def _sync_python_bin() -> str:
    root = Path(__file__).resolve().parents[2]
    venv_py = root / ".venv-memory" / "bin" / "python"
    if venv_py.exists() and os.access(str(venv_py), os.X_OK):
        return str(venv_py)
    sys_py = Path("/Library/Frameworks/Python.framework/Versions/3.14/bin/python3")
    if sys_py.exists() and os.access(str(sys_py), os.X_OK):
        return str(sys_py)
    return "python3"


def _sync_ticker_worker(ticker: str, full_backfill: bool = False) -> None:
    t = str(ticker or "").strip().upper()
    if not t:
        return
    ok = False
    msg = "SEC filing sync failed."
    try:
        # Manual sync should backfill much deeper than incremental background poll.
        # Normal manual sync is deep by default; full_backfill pushes even further.
        if full_backfill:
            manual_limit = max(200, min(5000, int(os.getenv("SEC_MANUAL_SYNC_FULL_LIMIT", "3000"))))
        else:
            manual_limit = max(20, min(1000, int(os.getenv("SEC_MANUAL_INCREMENTAL_LIMIT", "120"))))
        out = poll_ticker(t, is_held=True, per_ticker_limit=manual_limit)
        ok = bool(out.get("ok"))
        if ok:
            msg = (
                f"SEC filing sync complete ({'full_backfill' if full_backfill else 'incremental'}, limit={manual_limit}). "
                f"new_filings={int(out.get('new_filings') or 0)} "
                f"events={int(out.get('events_created') or 0)}"
            )
        else:
            msg = f"SEC filing sync failed: {str(out.get('error') or 'unknown_error')[:180]}"
    except Exception as exc:
        msg = f"SEC filing sync failed: {type(exc).__name__}: {str(exc)[:150]}"
    with _SYNC_LOCK:
        _SEC_SYNC_STATE[t] = {
            "running": "0",
            "last": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "result": "ok" if ok else "failed",
            "message": msg,
        }
        set_ticker_sync_state(t, _SEC_SYNC_STATE[t])


def _sync_my_companies_worker() -> None:
    key = "MY_COMPANIES_BATCH"
    ok = False
    msg = "My companies SEC sync failed."
    try:
        out = poll_and_ingest_tickers()
        ok = bool(out.get("ok"))
        if ok:
            msg = (
                f"My companies SEC sync complete. checked={int(out.get('tickers_checked') or 0)} "
                f"new_filings={int(out.get('new_filings') or 0)} events={int(out.get('events_created') or 0)}"
            )
        else:
            msg = f"My companies SEC sync failed: {str(out.get('error') or 'unknown_error')[:180]}"
    except Exception as exc:
        msg = f"My companies SEC sync failed: {type(exc).__name__}: {str(exc)[:150]}"
    with _SYNC_LOCK:
        _SEC_SYNC_STATE[key] = {
            "running": "0",
            "last": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "result": "ok" if ok else "failed",
            "message": msg,
        }


def _sync_ticker_list_worker(tickers: list[str]) -> None:
    key = "SEC_LIST_BATCH"
    done = 0
    ok_count = 0
    fail_count = 0
    for tk in list(tickers or []):
        _sync_ticker_worker(tk)
        done += 1
        st = (_SEC_SYNC_STATE.get(tk) or {}).get("result") or ""
        if str(st) == "ok":
            ok_count += 1
        else:
            fail_count += 1
        with _SYNC_LOCK:
            _SEC_SYNC_STATE[key] = {
                "running": "1",
                "last": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "result": "running",
                "message": f"Processed {done}/{len(tickers)} tickers (ok={ok_count}, failed={fail_count})",
            }
    with _SYNC_LOCK:
        _SEC_SYNC_STATE[key] = {
            "running": "0",
            "last": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "result": "ok" if fail_count == 0 else "partial",
            "message": f"Batch complete. total={len(tickers)} ok={ok_count} failed={fail_count}",
        }


def _is_transient_sync_error(err: str) -> bool:
    s = str(err or "").lower()
    signals = (
        "failed to resolve",
        "name resolution",
        "temporary failure",
        "no route to host",
        "connection reset",
        "connection aborted",
        "read timed out",
        "timed out",
        "502",
        "503",
        "504",
        "too many requests",
        "rate limit",
    )
    return any(k in s for k in signals)


def _run_step_with_retries(
    step: list[str],
    *,
    root: Path,
    fh,
    timeout: int,
    retries: int,
    base_delay: float = 2.0,
) -> tuple[bool, str]:
    max_attempts = max(1, int(retries) + 1)
    last_err = ""
    for attempt in range(1, max_attempts + 1):
        fh.write("$ " + " ".join(step) + f"  [attempt {attempt}/{max_attempts}]\n")
        try:
            p = subprocess.run(step, cwd=str(root), capture_output=True, text=True, timeout=timeout)
            if p.stdout:
                fh.write(p.stdout + "\n")
            if p.stderr:
                fh.write(p.stderr + "\n")
            err = (p.stderr.strip() or p.stdout.strip() or f"exit={p.returncode}")[:600]
            if p.returncode == 0:
                return True, ""
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            fh.write(f"ERROR: {err}\n")
        last_err = err
        if attempt >= max_attempts:
            break
        if not _is_transient_sync_error(err):
            break
        delay = max(1.0, float(base_delay) * (2 ** (attempt - 1)))
        fh.write(f"Retrying in {delay:.1f}s due to transient error.\n")
        time.sleep(delay)
    return False, last_err


@router.get("/company_file/sec")
def company_sec_page(request: Request, t: str = "", msg: str = "", form: str = ""):
    templates = request.app.state.templates
    ticker = str(t or "").strip().upper()
    if not ticker:
        return RedirectResponse(url="/company_file?msg=" + urllib.parse.quote("Ticker is required."), status_code=303)
    detail = company_detail(ticker)
    if not detail:
        return RedirectResponse(url="/company_file?msg=" + urllib.parse.quote("Company not found."), status_code=303)
    selected_form = str(form or "").strip().upper()
    if selected_form:
        proxy_forms = {"DEF 14A", "DEFA14A", "DEFA14C", "DEF 14C", "PRE 14A", "PRE 14C", "PREM14A", "PREC14A"}
        selected_forms = {selected_form}
        if selected_form == "PROXY":
            selected_forms = proxy_forms
        groups = []
        total = 0
        for g in list(detail.get("filing_groups") or []):
            rows = [r for r in list(g.get("rows") or []) if str(r.get("form") or "").strip().upper() in selected_forms]
            if not rows:
                continue
            groups.append(
                {
                    "key": g.get("key"),
                    "label": g.get("label"),
                    "count": len(rows),
                    "rows": rows,
                }
            )
            total += len(rows)
        detail["filing_groups"] = groups
        detail["filings"] = [r for g in groups for r in g.get("rows", [])]
    prefer_sec_direct_open = str(os.getenv("APP_ENV") or "").strip().lower() == "cloud"
    _apply_sec_open_links(detail, ticker, prefer_sec_direct_open)
    with _SYNC_LOCK:
        state_file = load_sec_sync_state()
        current = _SEC_SYNC_STATE.get(ticker) or state_file.get(ticker) or {}
        sync_state = {
            "running": str(current.get("running") or "0"),
            "last": str(current.get("last") or ""),
            "result": str(current.get("result") or ""),
            "message": str(current.get("message") or ""),
        }
    return templates.TemplateResponse(
        "company_sec.html",
        {
            "request": request,
            "message": msg,
            "detail": detail,
            "sync_state": sync_state,
            "selected_form": selected_form,
        },
    )


@router.post("/company_file/sec-sync")
def company_sec_sync(ticker: str = Form(""), background: int = Form(1)):
    t = str(ticker or "").strip().upper()
    if not t:
        return RedirectResponse(url="/company_file?msg=" + urllib.parse.quote("Ticker is required."), status_code=303)
    run_inline = int(background or 0) == 0
    if run_inline and str(os.getenv("APP_ENV") or "").strip().lower() == "cloud":
        run_inline = False
    if run_inline:
        with _SYNC_LOCK:
            _SEC_SYNC_STATE[t] = {
                "running": "1",
                "last": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "result": "running",
                "message": "Sync started...",
            }
            set_ticker_sync_state(t, _SEC_SYNC_STATE[t])
        _sync_ticker_worker(t, full_backfill=False)
        with _SYNC_LOCK:
            current = _SEC_SYNC_STATE.get(t) or {}
        final_msg = str(current.get("message") or "SEC sync finished.")
        return RedirectResponse(
            url="/company_file/sec?t=" + urllib.parse.quote(t) + "&msg=" + urllib.parse.quote(final_msg),
            status_code=303,
        )
    start = False
    with _SYNC_LOCK:
        state_file = load_sec_sync_state()
        st = _SEC_SYNC_STATE.get(t) or state_file.get(t) or {"running": "0", "last": "", "result": "", "message": ""}
        if st.get("running") != "1":
            _SEC_SYNC_STATE[t] = {
                "running": "1",
                "last": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "result": "running",
                "message": "Sync started...",
            }
            set_ticker_sync_state(t, _SEC_SYNC_STATE[t])
            start = True
    if start:
        th = threading.Thread(target=_sync_ticker_worker, args=(t, False), daemon=True)
        th.start()
        msg = "SEC incremental sync started. Refresh this page in ~20-90 seconds."
    else:
        msg = "Sync already running."
    return RedirectResponse(
        url="/company_file/sec?t=" + urllib.parse.quote(t) + "&msg=" + urllib.parse.quote(msg),
        status_code=303,
    )


@router.post("/company_file/sec-sync-full")
def company_sec_sync_full(ticker: str = Form(""), background: int = Form(1)):
    t = str(ticker or "").strip().upper()
    if not t:
        return RedirectResponse(url="/company_file?msg=" + urllib.parse.quote("Ticker is required."), status_code=303)
    run_inline = int(background or 0) == 0
    if run_inline and str(os.getenv("APP_ENV") or "").strip().lower() == "cloud":
        run_inline = False
    if run_inline:
        with _SYNC_LOCK:
            _SEC_SYNC_STATE[t] = {
                "running": "1",
                "last": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "result": "running",
                "message": "Full backfill started...",
            }
            set_ticker_sync_state(t, _SEC_SYNC_STATE[t])
        _sync_ticker_worker(t, full_backfill=True)
        with _SYNC_LOCK:
            current = _SEC_SYNC_STATE.get(t) or {}
        final_msg = str(current.get("message") or "SEC full backfill finished.")
        return RedirectResponse(
            url="/company_file/sec?t=" + urllib.parse.quote(t) + "&msg=" + urllib.parse.quote(final_msg),
            status_code=303,
        )
    start = False
    with _SYNC_LOCK:
        state_file = load_sec_sync_state()
        st = _SEC_SYNC_STATE.get(t) or state_file.get(t) or {"running": "0", "last": "", "result": "", "message": ""}
        if st.get("running") != "1":
            _SEC_SYNC_STATE[t] = {
                "running": "1",
                "last": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "result": "running",
                "message": "Full backfill started...",
            }
            set_ticker_sync_state(t, _SEC_SYNC_STATE[t])
            start = True
    if start:
        th = threading.Thread(target=_sync_ticker_worker, args=(t, True), daemon=True)
        th.start()
        msg = "SEC full backfill started. Refresh this page in ~1-5 minutes."
    else:
        msg = "Sync already running."
    return RedirectResponse(
        url="/company_file/sec?t=" + urllib.parse.quote(t) + "&msg=" + urllib.parse.quote(msg),
        status_code=303,
    )


@router.post("/company_file/sec-sync-my")
def company_sec_sync_my(return_to: str = "", background: int = Form(1)):
    key = "MY_COMPANIES_BATCH"
    run_inline = int(background or 0) == 0
    if run_inline and str(os.getenv("APP_ENV") or "").strip().lower() == "cloud":
        run_inline = False
    if run_inline:
        with _SYNC_LOCK:
            _SEC_SYNC_STATE[key] = {
                "running": "1",
                "last": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "result": "running",
                "message": "Batch sync started...",
            }
        _sync_my_companies_worker()
        with _SYNC_LOCK:
            current = _SEC_SYNC_STATE.get(key) or {}
        msg = str(current.get("message") or "Portfolio + watchlist SEC sync finished.")
        rt = str(return_to or "").strip().lower()
        base = "/my_universe?tab=all"
        if rt in {"my_universe", "/my_universe"}:
            base = "/my_universe?tab=all"
        sep = "&" if "?" in base else "?"
        return RedirectResponse(url=base + sep + "msg=" + urllib.parse.quote(msg), status_code=303)
    start = False
    with _SYNC_LOCK:
        st = _SEC_SYNC_STATE.get(key, {"running": "0"})
        if st.get("running") != "1":
            _SEC_SYNC_STATE[key] = {
                "running": "1",
                "last": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "result": "running",
                "message": "Batch sync started...",
            }
            start = True
    if start:
        th = threading.Thread(target=_sync_my_companies_worker, daemon=True)
        th.start()
        msg = "Portfolio + watchlist SEC sync started. Refresh in ~1-5 minutes."
    else:
        msg = "A batch sync is already running."
    rt = str(return_to or "").strip().lower()
    base = "/my_universe?tab=all"
    if rt in {"my_universe", "/my_universe"}:
        base = "/my_universe?tab=all"
    sep = "&" if "?" in base else "?"
    return RedirectResponse(url=base + sep + "msg=" + urllib.parse.quote(msg), status_code=303)


@router.get("/company_file/sec-sync-my/status")
def company_sec_sync_my_status():
    key = "MY_COMPANIES_BATCH"
    with _SYNC_LOCK:
        st = _SEC_SYNC_STATE.get(key) or {}
    return JSONResponse(
        {
            "ok": True,
            "running": str(st.get("running") or "0") == "1",
            "last": str(st.get("last") or ""),
            "result": str(st.get("result") or ""),
            "message": str(st.get("message") or ""),
        }
    )


@router.post("/company_file/sec-sync-list")
def company_sec_sync_list(tickers: str = Form(""), background: int = Form(1)):
    parsed = _parse_ticker_list(tickers)
    if not parsed:
        return JSONResponse({"ok": False, "error": "tickers_required"}, status_code=400)
    key = "SEC_LIST_BATCH"
    run_inline = int(background or 0) == 0
    if run_inline and str(os.getenv("APP_ENV") or "").strip().lower() == "cloud":
        run_inline = False
    with _SYNC_LOCK:
        st = _SEC_SYNC_STATE.get(key, {"running": "0"})
        if st.get("running") == "1":
            return JSONResponse({"ok": True, "started": False, "running": True, "message": str(st.get("message") or "Batch already running.")})
        _SEC_SYNC_STATE[key] = {
            "running": "1",
            "last": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "result": "running",
            "message": f"Starting batch for {len(parsed)} tickers...",
        }
    if run_inline:
        _sync_ticker_list_worker(parsed)
        with _SYNC_LOCK:
            final = dict(_SEC_SYNC_STATE.get(key) or {})
        return JSONResponse({"ok": True, "started": True, "running": False, "tickers": parsed, "state": final})
    th = threading.Thread(target=_sync_ticker_list_worker, args=(parsed,), daemon=True)
    th.start()
    return JSONResponse({"ok": True, "started": True, "running": True, "tickers": parsed, "message": f"Batch started for {len(parsed)} tickers."})


@router.get("/company_file/sec-sync-list/status")
def company_sec_sync_list_status(tickers: str = ""):
    key = "SEC_LIST_BATCH"
    parsed = _parse_ticker_list(tickers)
    with _SYNC_LOCK:
        batch = dict(_SEC_SYNC_STATE.get(key) or {})
    state_file = load_sec_sync_state()
    items: dict[str, dict[str, str]] = {}
    for tk in parsed[:200]:
        cur = (_SEC_SYNC_STATE.get(tk) or state_file.get(tk) or {})
        items[tk] = {
            "running": str(cur.get("running") or "0"),
            "last": str(cur.get("last") or ""),
            "result": str(cur.get("result") or ""),
            "message": str(cur.get("message") or ""),
        }
    return JSONResponse(
        {
            "ok": True,
            "batch": {
                "running": str(batch.get("running") or "0") == "1",
                "last": str(batch.get("last") or ""),
                "result": str(batch.get("result") or ""),
                "message": str(batch.get("message") or ""),
            },
            "tickers": items,
        }
    )


@router.post("/company_file/price-metrics-refresh")
def company_price_metrics_refresh(
    request: Request,
    ticker: str = Form(""),
):
    tk = str(ticker or "").strip().upper()
    if not tk:
        msg = "Ticker is required."
        if is_hx_request(request):
            return _render_company_detail(request, ticker=tk, message=msg)
        return _back_to_ticker(tk, msg)
    row = refresh_price_metrics(tk)
    msg = "Price metrics refreshed." if row else "Could not refresh price metrics."
    if is_hx_request(request):
        return _render_company_detail(request, ticker=tk, message=msg)
    return _back_to_ticker(tk, msg)


@router.post("/company_file/mini-statements-refresh")
def company_mini_statements_refresh(
    request: Request,
    ticker: str = Form(""),
):
    tk = str(ticker or "").strip().upper()
    if not tk:
        msg = "Ticker is required."
        if is_hx_request(request):
            return _render_company_detail(request, ticker=tk, message=msg)
        return _back_to_ticker(tk, msg)
    row = refresh_mini_statements(tk)
    if row:
        msg = "Mini statements refreshed."
    else:
        probe = fetch_historical_financials(ticker=tk, metric="all", years=5, refresh=True)
        err = str((probe or {}).get("error") or "").strip().lower()
        if err == "financials_unavailable":
            msg = "No provider statement data available for this ticker right now."
        elif err == "ticker_required":
            msg = "Ticker is required."
        elif err == "unsupported_metric":
            msg = "Requested metric is not supported."
        else:
            msg = "Could not refresh mini statements right now."
    if is_hx_request(request):
        return _render_company_detail(request, ticker=tk, message=msg)
    return _back_to_ticker(tk, msg)


@router.post("/company_file/intel-refresh")
def company_intel_refresh(
    request: Request,
    ticker: str = Form(""),
):
    tk = str(ticker or "").strip().upper()
    if not tk:
        msg = "Ticker is required."
        if is_hx_request(request):
            return _render_company_detail(request, ticker=tk, message=msg)
        return _back_to_ticker(tk, msg)
    row = refresh_company_intel(tk)
    status = dict(row.get("status") or {})
    ok = (
        bool((row.get("revenue_segments") or {}).get("product") or (row.get("revenue_segments") or {}).get("geography"))
        or bool((row.get("buyback") or {}).get("quarters"))
        or bool(row.get("insider_trades"))
    )
    if ok:
        msg = "Company intel refreshed."
    else:
        details: list[str] = []
        err = str(status.get("error") or "").strip()
        seg_s = str(status.get("revenue_segments") or "").strip()
        buy_s = str(status.get("buybacks") or "").strip()
        ins_s = str(status.get("insider") or "").strip()
        if err:
            details.append(f"error={err}")
        if seg_s:
            details.append(f"revenue_segments={seg_s}")
        if buy_s:
            details.append(f"buybacks={buy_s}")
        if ins_s:
            details.append(f"insider={ins_s}")
        msg = "Could not refresh company intel right now."
        if details:
            msg = f"Could not refresh company intel. {'; '.join(details)}"
    if is_hx_request(request):
        return _render_company_detail(request, ticker=tk, message=msg)
    return _back_to_ticker(tk, msg)


@router.post("/company_file/transcripts-refresh")
def company_transcripts_refresh(
    request: Request,
    ticker: str = Form(""),
):
    tk = str(ticker or "").strip().upper()
    if not tk:
        msg = "Ticker is required."
        if is_hx_request(request):
            return _render_company_detail(request, ticker=tk, message=msg)
        return _back_to_ticker(tk, msg)
    run_key = dt.datetime.now().strftime("%Y%m%dT%H%M%S%f")
    sec_key = f"{tk}:sec_refresh:{run_key}"
    queued_sec = insert_earnings_call_ingest_run_pg(
        ticker=tk,
        event_datetime="",
        idempotency_key=sec_key,
        stage="sec_refresh",
        status="queued",
        attempt=0,
        error_text="",
        detail={"requested_by": "company_file_ui"},
        next_retry_at="",
    )
    audio_enabled = str(os.getenv("ENABLE_IR_AUDIO_TRANSCRIBE", "1")).strip().lower() in {"1", "true", "yes", "on"}
    queued_audio = False
    if audio_enabled:
        audio_key = f"{tk}:ir_audio_transcribe:{run_key}"
        queued_audio = insert_earnings_call_ingest_run_pg(
            ticker=tk,
            event_datetime="",
            idempotency_key=audio_key,
            stage="ir_audio_transcribe",
            status="queued",
            attempt=0,
            error_text="",
            detail={"requested_by": "company_file_ui"},
            next_retry_at="",
        )
    if queued_sec and queued_audio:
        msg = "Transcript refresh queued (SEC + IR audio->text). Background worker will process shortly."
    elif queued_sec:
        msg = "SEC transcript refresh queued."
        if audio_enabled:
            msg = "SEC transcript refresh queued, but IR audio->text queueing failed."
    elif queued_audio:
        msg = "IR audio->text queued, but SEC transcript refresh queueing failed."
    else:
        msg = "Could not queue transcript refresh jobs."
    if is_hx_request(request):
        return _render_company_detail(request, ticker=tk, message=msg)
    return _back_to_ticker(tk, msg)


@router.post("/company_file/sec-facts-refresh")
def company_sec_facts_refresh(
    request: Request,
    ticker: str = Form(""),
):
    tk = str(ticker or "").strip().upper()
    if not tk:
        msg = "Ticker is required."
        if is_hx_request(request):
            return _render_company_detail(request, ticker=tk, message=msg)
        return _back_to_ticker(tk, msg)
    out = ingest_sec_facts_for_ticker(tk, max_filings=24)
    if bool(out.get("ok")):
        msg = (
            f"SEC facts refreshed. selected={int(out.get('selected') or 0)} "
            f"processed={int(out.get('processed') or 0)} chunks={int(out.get('chunks') or 0)}"
        )
    else:
        msg = f"Could not refresh SEC facts: {str(out.get('error') or 'unknown_error')}"
    if is_hx_request(request):
        return _render_company_detail(request, ticker=tk, message=msg)
    return _back_to_ticker(tk, msg)


@router.get("/api/company/financial-deltas")
def api_company_financial_deltas(ticker: str = "", years: int = 5):
    tk = str(ticker or "").strip().upper()
    if not tk:
        return JSONResponse({"ok": False, "error": "ticker_required"}, status_code=400)
    out = compute_financial_deltas(ticker=tk, years=years)
    return JSONResponse(out, status_code=(200 if bool(out.get("ok")) else 404))


@router.get("/filing")
def filing_view(path: str = "", doc: str = "", t: str = "", mode: str = "reader"):
    rel = normalize_filing_rel_path(path)
    if not rel:
        return HTMLResponse("<html><body>Filing path not available.</body></html>", status_code=404)
    p = safe_resolve_filing_path(path)

    raw_q = urllib.parse.quote(str(path), safe="")
    doc_s = str(doc or "").strip()
    doc_q = urllib.parse.quote(doc_s, safe="")
    doc_ok = doc_s.lower().startswith(("http://", "https://"))
    back = f"/company_file?t={urllib.parse.quote(str(t or '').strip().upper())}" if str(t or "").strip() else "/company_file"
    raw_href = f"/filing_raw?path={raw_q}"
    original_href = f"/filing?path={raw_q}&doc={doc_q}&t={urllib.parse.quote(str(t or '').strip().upper())}&mode=original"
    official_href = f"/filing?path={raw_q}&doc={doc_q}&t={urllib.parse.quote(str(t or '').strip().upper())}&mode=official"
    toolbar = (
        "<div class='bar'>"
        f"<a class='btn' href='{back}'>Back</a>"
        + (f"<a class='btn' href='{official_href}'>Official</a>" if doc_ok else "")
        + f"<a class='btn' href='{original_href}'>Original</a>"
        + f"<a class='btn' href='{raw_href}' target='_blank' rel='noopener noreferrer'>Raw File</a>"
        + (f"<a class='btn' href='{html.escape(doc_s, quote=True)}' target='_blank' rel='noopener noreferrer'>Open SEC</a>" if doc_ok else "")
        + "</div>"
    )
    base_style = (
        "<style>body{margin:0;background:#eaf0f4;color:#2f4358;font-family:'Avenir Next','Helvetica Neue',sans-serif;}"
        ".wrap{max-width:1280px;margin:0 auto;padding:12px;} .bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px;}"
        ".btn{border:1px solid #c8d3dd;background:#edf2f6;color:#2f4358;border-radius:8px;padding:6px 10px;text-decoration:none;font-size:12px;font-weight:700;}"
        "iframe{width:100%;height:88vh;border:1px solid #c8d3dd;border-radius:10px;background:#fff;}"
        ".paper{background:#f8fafc;border:1px solid #d3dce5;border-radius:10px;padding:18px;line-height:1.52;font-size:15px;}"
        "pre{white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;line-height:1.35;}</style>"
    )

    ext = (p.suffix.lower() if p is not None else Path(rel).suffix.lower())
    if ext == ".pdf":
        body = (
            "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
            + base_style
            + "</head><body><div class='wrap'>"
            + toolbar
            + f"<iframe src='{raw_href}'></iframe>"
            + "</div></body></html>"
        )
        return HTMLResponse(body)

    if p is not None:
        txt = p.read_text(encoding="utf-8", errors="ignore")
    else:
        txt = read_filing_text_any(rel)
    if not txt:
        return HTMLResponse("<html><body>Filing content not available.</body></html>", status_code=404)
    mode_norm = str(mode or "original").strip().lower()
    if mode_norm == "reader":
        mode_norm = "original"
    if mode_norm not in {"original", "official"}:
        mode_norm = "original"
    if mode_norm == "official" and doc_ok:
        # SEC pages commonly deny cross-origin iframe embedding.
        # Redirect to the official URL to guarantee proper render.
        return RedirectResponse(url=doc_s, status_code=307)

    looks_html = ("<html" in txt[:4000].lower()) or ("<!doctype html" in txt[:4000].lower()) or ("<body" in txt[:4000].lower())
    if looks_html and mode_norm == "original":
        cleaned = re.sub(r"(?is)<script[^>]*>.*?</script>", "", txt[:1200000])
        body = (
            "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
            + base_style
            + "</head><body><div class='wrap'>"
            + toolbar
            + f"<iframe srcdoc='{html.escape(cleaned, quote=True)}'></iframe>"
            + "</div></body></html>"
        )
        return HTMLResponse(body)

    reader = txt
    if looks_html:
        reader = re.sub(r"(?is)<script[^>]*>.*?</script>", "", reader)
        reader = re.sub(r"(?is)<style[^>]*>.*?</style>", "", reader)
        reader = re.sub(r"(?is)</?(meta|link|head|title)[^>]*>", "", reader)
    payload = reader if looks_html else f"<pre>{html.escape(reader[:500000])}</pre>"
    body = (
        "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        + base_style
        + "</head><body><div class='wrap'>"
        + toolbar
        + f"<div class='paper'>{payload}</div>"
        + "</div></body></html>"
    )
    return HTMLResponse(body)


@router.get("/filing_raw")
def filing_raw(path: str = ""):
    rel = normalize_filing_rel_path(path)
    if not rel:
        return Response(status_code=404)
    p = safe_resolve_filing_path(path)
    if p is None and not cloud_files.exists(rel):
        return Response(status_code=404)
    if p is not None:
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        return FileResponse(str(p), media_type=ctype, filename=p.name)
    txt = cloud_files.read_text(rel)
    if not txt:
        return Response(status_code=404)
    ctype = mimetypes.guess_type(rel)[0] or "text/plain; charset=utf-8"
    return Response(content=txt, media_type=ctype)


@router.post("/company_file/ir-email-send")
def company_ir_email_send(
    ticker: str = Form(""),
    to_email: str = Form(""),
    subject: str = Form(""),
    body: str = Form(""),
):
    t = str(ticker or "").strip().upper()
    ok, msg = send_email(to_email=to_email, subject=subject, body=body)
    if ok and t:
        add_general_note(
            f"Email sent to {to_email.strip()}\nSubject: {subject.strip()}\n\n{body.strip()[:1200]}",
            scope="email",
            ticker=t,
            tags="email,sent,company_ir",
        )
    return RedirectResponse(
        url="/company_file?t=" + urllib.parse.quote(t) + "&msg=" + urllib.parse.quote(msg),
        status_code=303,
    )


@router.get("/company_file")
def company_file_page(
    request: Request,
    t: str = "",
    q: str = "",
    industry: str = "",
    index: str = "",
    list: str = "",
    moat: str = "",
    market: str = "",
    size: str = "",
    scope: str = "all",
    sort: str = "mcap_desc",
    page: int = 1,
    page_size: int = 80,
    msg: str = "",
    show_ai: int = 0,
):
    templates = request.app.state.templates
    ticker = str(t or "").strip().upper()
    if ticker:
        return _render_company_detail(request, ticker=ticker, message=msg, show_ai=(int(show_ai or 0) == 1))

    data = list_companies(
        query=q,
        industry=industry,
        index_key=index,
        list_name=list,
        moat_key=moat,
        market=market,
        size=size,
        scope=scope,
        sort=sort,
        page=page,
        page_size=page_size,
    )
    return templates.TemplateResponse(
        "company_file.html",
        {
            "request": request,
            "message": msg,
            "query": q,
            "industry": industry,
            "index_key": index,
            "list_name": list,
            "moat_key": moat,
            "market_key": market,
            "size_key": size,
            "scope": data["scope"],
            "sort": data["sort"],
            "rows": data["rows"],
            "total": data["total"],
            "page": data["page"],
            "pages": data["pages"],
            "page_size": data["page_size"],
            "industries": data["industries"],
            "index_filters": index_filters(),
            "list_filters": list_filters(),
            "moat_filters": moat_filters(),
        },
    )


@router.post("/company_file/moat-save")
def company_file_moat_save(
    request: Request,
    ticker: str = Form(""),
    moat_keys: list[str] = Form(default=[]),
):
    ok = save_company_moats(ticker, moat_keys or [])
    msg = "Moat updated." if ok else "Could not save moat tags."
    if is_hx_request(request):
        return _render_company_detail(request, ticker=ticker, message=msg)
    return _back_to_ticker(ticker, msg)


@router.post("/company_file/competitor-add")
def company_file_competitor_add(
    request: Request,
    ticker: str = Form(""),
    competitor_ticker: str = Form(""),
    competitor_name: str = Form(""),
    evidence: str = Form(""),
):
    ok = add_competitor(ticker, competitor_ticker, competitor_name, evidence)
    msg = "Competitor added." if ok else "Could not add competitor."
    if is_hx_request(request):
        return _render_company_detail(request, ticker=ticker, message=msg)
    return _back_to_ticker(ticker, msg)


@router.post("/company_file/competitor-update")
def company_file_competitor_update(
    request: Request,
    ticker: str = Form(""),
    row_id: int = Form(0),
    competitor_ticker: str = Form(""),
    competitor_name: str = Form(""),
    evidence: str = Form(""),
):
    ok = update_competitor(row_id, competitor_ticker, competitor_name, evidence)
    msg = "Competitor updated." if ok else "Could not update competitor."
    if is_hx_request(request):
        return _render_company_detail(request, ticker=ticker, message=msg)
    return _back_to_ticker(ticker, msg)


@router.post("/company_file/competitor-remove")
def company_file_competitor_remove(
    request: Request,
    ticker: str = Form(""),
    row_id: int = Form(0),
):
    ok = remove_competitor(row_id)
    msg = "Competitor removed." if ok else "Could not remove competitor."
    if is_hx_request(request):
        return _render_company_detail(request, ticker=ticker, message=msg)
    return _back_to_ticker(ticker, msg)


@router.post("/company_file/supply-chain-add")
def company_file_supply_chain_add(
    request: Request,
    ticker: str = Form(""),
    counterparty_ticker: str = Form(""),
    counterparty_name: str = Form(""),
    relationship_type: str = Form("supplier"),
    evidence: str = Form(""),
    confidence: float = Form(0.7),
):
    ok = add_supply_chain_link(
        anchor_ticker=ticker,
        counterparty_ticker=counterparty_ticker,
        counterparty_name=counterparty_name,
        relationship_type=relationship_type,
        evidence=evidence,
        confidence=confidence,
    )
    msg = "Supply chain link added." if ok else "Could not add supply chain link."
    if is_hx_request(request):
        return _render_company_detail(request, ticker=ticker, message=msg)
    return _back_to_ticker(ticker, msg)


@router.post("/company_file/supply-chain-remove")
def company_file_supply_chain_remove(
    request: Request,
    ticker: str = Form(""),
    row_id: int = Form(0),
):
    ok = remove_supply_chain_link(row_id=row_id)
    msg = "Supply chain link removed." if ok else "Could not remove supply chain link."
    if is_hx_request(request):
        return _render_company_detail(request, ticker=ticker, message=msg)
    return _back_to_ticker(ticker, msg)


@router.post("/company_file/note-add")
def company_file_note_add(
    request: Request,
    ticker: str = Form(""),
    note: str = Form(""),
):
    ok = add_company_note(ticker=ticker, note=note, action="Note", emotion="Calm")
    msg = "Note saved." if ok else "Could not save note."
    if is_hx_request(request):
        return _render_company_detail(request, ticker=ticker, message=msg)
    return _back_to_ticker(ticker, msg)


@router.post("/company_file/omnibox-add")
def company_file_omnibox_add(
    request: Request,
    ticker: str = Form(""),
    entry: str = Form(""),
    show_ai: str = Form("0"),
):
    tk = str(ticker or "").strip().upper()
    raw = str(entry or "").strip()
    if not tk or not raw:
        msg = "Empty input." if raw == "" else "Ticker is required."
        if is_hx_request(request):
            return _render_company_detail(request, ticker=tk, message=msg, show_ai=(str(show_ai or "0").strip() == "1"))
        return _back_to_ticker(tk, msg)

    force_note = bool(re.search(r"(^|\\s)#note\\b", raw, flags=re.I))
    clean = re.sub(r"(^|\\s)#task\\b", " ", raw, flags=re.I)
    clean = re.sub(r"(^|\\s)#note\\b", " ", clean, flags=re.I)
    clean = re.sub(r"\\s+", " ", clean).strip()
    content = clean or raw
    if force_note:
        ok = add_company_note(ticker=tk, note=content, action="Note", emotion="Calm")
        msg = f"Note saved to {tk} workspace." if ok else "Could not save note."
    else:
        ok = add_company_task(ticker=tk, task=content, due_date="", priority="P2")
        msg = f"Task added to {tk} workspace." if ok else "Could not add task."
    if is_hx_request(request):
        return _render_company_detail(request, ticker=tk, message=msg, show_ai=(str(show_ai or "0").strip() == "1"))
    return _back_to_ticker(tk, msg)


@router.post("/company_file/note-update")
def company_file_note_update(
    request: Request,
    ticker: str = Form(""),
    note_id: int = Form(0),
    note: str = Form(""),
):
    ok = update_company_note(note_id=note_id, note=note)
    msg = "Note updated." if ok else "Could not update note."
    if is_hx_request(request):
        return _render_company_detail(request, ticker=ticker, message=msg)
    return _back_to_ticker(ticker, msg)


@router.post("/company_file/note-delete")
def company_file_note_delete(
    request: Request,
    ticker: str = Form(""),
    note_id: int = Form(0),
):
    ok = delete_company_note(note_id=note_id)
    msg = "Note deleted." if ok else "Could not delete note."
    if is_hx_request(request):
        return _render_company_detail(request, ticker=ticker, message=msg)
    return _back_to_ticker(ticker, msg)


@router.post("/company_file/task-add")
def company_file_task_add(
    request: Request,
    ticker: str = Form(""),
    task: str = Form(""),
    due_date: str = Form(""),
    priority: str = Form("P2"),
):
    ok = add_company_task(ticker=ticker, task=task, due_date=due_date, priority=priority)
    msg = "Task added." if ok else "Could not add task."
    if is_hx_request(request):
        return _render_company_detail(request, ticker=ticker, message=msg)
    return _back_to_ticker(ticker, msg)


@router.post("/company_file/task-toggle")
def company_file_task_toggle(
    request: Request,
    ticker: str = Form(""),
    todo_id: int = Form(0),
):
    ok = toggle_company_task(todo_id)
    msg = "Task updated." if ok else "Could not update task."
    if is_hx_request(request):
        return _render_company_detail(request, ticker=ticker, message=msg)
    return _back_to_ticker(ticker, msg)


@router.post("/company_file/task-delete")
def company_file_task_delete(
    request: Request,
    ticker: str = Form(""),
    todo_id: int = Form(0),
):
    ok = delete_company_task(todo_id)
    msg = "Task deleted." if ok else "Could not delete task."
    if is_hx_request(request):
        return _render_company_detail(request, ticker=ticker, message=msg)
    return _back_to_ticker(ticker, msg)


@router.post("/company_file/task-update")
def company_file_task_update(
    request: Request,
    ticker: str = Form(""),
    todo_id: int = Form(0),
    task: str = Form(""),
    due_date: str = Form(""),
    priority: str = Form("P2"),
):
    ok = update_company_task(todo_id=todo_id, task=task, due_date=due_date, priority=priority)
    msg = "Task updated." if ok else "Could not update task."
    if is_hx_request(request):
        return _render_company_detail(request, ticker=ticker, message=msg)
    return _back_to_ticker(ticker, msg)


@router.post("/company_file/reminder-add")
def company_file_reminder_add(
    request: Request,
    ticker: str = Form(""),
    remind_at: str = Form(""),
    note: str = Form(""),
):
    ok = add_company_reminder(ticker=ticker, remind_at=remind_at, note=note)
    msg = "Reminder saved." if ok else "Could not save reminder."
    if is_hx_request(request):
        return _render_company_detail(request, ticker=ticker, message=msg)
    return _back_to_ticker(ticker, msg)


@router.post("/company_file/reminder-toggle")
def company_file_reminder_toggle(
    request: Request,
    ticker: str = Form(""),
    reminder_id: int = Form(0),
):
    ok = toggle_company_reminder(reminder_id)
    msg = "Reminder updated." if ok else "Could not update reminder."
    if is_hx_request(request):
        return _render_company_detail(request, ticker=ticker, message=msg)
    return _back_to_ticker(ticker, msg)


@router.post("/company_file/reminder-update")
def company_file_reminder_update(
    request: Request,
    ticker: str = Form(""),
    reminder_id: int = Form(0),
    remind_at: str = Form(""),
    note: str = Form(""),
):
    ok = update_company_reminder(reminder_id=reminder_id, remind_at=remind_at, note=note)
    msg = "Reminder updated." if ok else "Could not update reminder."
    if is_hx_request(request):
        return _render_company_detail(request, ticker=ticker, message=msg)
    return _back_to_ticker(ticker, msg)


@router.post("/api/agent/analyze")
async def api_agent_analyze(request: Request):
    """
    Manually trigger the agent worker to analyse a ticker.
    Runs in a background thread so the response returns immediately.
    Body: {"ticker": "AAPL", "form": "10-Q"}  (form is optional)
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    ticker = str(payload.get("ticker") or "").strip().upper()
    if not ticker:
        return JSONResponse({"ok": False, "error": "ticker_required"}, status_code=400)

    form = str(payload.get("form") or "10-Q").strip().upper()

    def _run():
        try:
            import sys
            from pathlib import Path
            ROOT = Path(__file__).resolve().parents[2]
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            from tools.agent_worker import run as agent_run
            event = {
                "type": "manual",
                "ticker": ticker,
                "form": form,
                "accession": "",
                "filing_path": "",
                "filing_date": dt.date.today().isoformat(),
            }
            agent_run(event, dry_run=False, debate=True)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).error("api_agent_analyze background error: %s", exc)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return JSONResponse({"ok": True, "ticker": ticker, "message": f"Agent analysis started for {ticker}. Results will appear in the Activity notes."})


@router.post("/company_file/reminder-delete")
def company_file_reminder_delete(
    request: Request,
    ticker: str = Form(""),
    reminder_id: int = Form(0),
):
    ok = delete_company_reminder(reminder_id=reminder_id)
    msg = "Reminder deleted." if ok else "Could not delete reminder."
    if is_hx_request(request):
        return _render_company_detail(request, ticker=ticker, message=msg)
    return _back_to_ticker(ticker, msg)


@router.post("/api/admin/ir-registry")
async def admin_upsert_ir_registry(request: Request):
    """Admin endpoint: upsert an IR source registry entry in Postgres.
    Accepts JSON body with ticker + registry fields.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    ticker = str(body.get("ticker") or "").strip().upper()
    if not ticker:
        return JSONResponse({"ok": False, "error": "ticker_required"}, status_code=400)
    try:
        from app.services.postgres_core_service import upsert_ir_source_registry_pg
        ok = upsert_ir_source_registry_pg(
            ticker=ticker,
            ir_home_url=str(body.get("ir_home_url") or "").strip(),
            provider=str(body.get("provider") or "").strip(),
            rss_url=str(body.get("rss_url") or "").strip(),
            last_good_audio_pattern=str(body.get("last_good_audio_pattern") or "").strip(),
            last_good_event_url=str(body.get("last_good_event_url") or "").strip(),
            active=bool(body.get("active", True)),
            meta=body.get("meta") or {},
        )
        return JSONResponse({"ok": ok, "ticker": ticker})
    except Exception as exc:
        import logging
        logging.getLogger(__name__).error("admin_upsert_ir_registry error: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
