"""ticker_hub_service.py — Unified ticker intelligence hub.

Gathers everything the system knows about a single ticker from all tables
and returns it as a single dict for display or AI context injection.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

_FILING_DOCS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "filing_docs")

from app.services.postgres_core_service import pg_connect, pg_enabled

LOGGER = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _row_to_dict(cur_description, row) -> dict:
    """Convert a DB row + cursor.description into a plain dict, serialising dates."""
    cols = [d[0] for d in cur_description]
    d: dict = {}
    for k, v in zip(cols, row):
        if isinstance(v, (dt.date, dt.datetime)):
            d[k] = v.isoformat()
        else:
            d[k] = v
    return d


def _safe_query(query: str, params: tuple = (), *, limit: int = 0) -> list[dict]:
    """Execute *query* and return rows as dicts.  Returns [] on any failure."""
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(query, params)
        rows = cur.fetchall() or []
        return [_row_to_dict(cur.description, r) for r in rows]
    except Exception as exc:
        LOGGER.debug("_safe_query failed (%s): %s", query[:60], exc)
        try:
            con.rollback()
        except Exception:
            pass
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass


def _safe_query_one(query: str, params: tuple = ()) -> dict | None:
    rows = _safe_query(query, params, limit=1)
    return rows[0] if rows else None


# ── Insider trades from Form 4 events ────────────────────────────────────────

def _insider_trades_from_files(ticker: str) -> list[dict]:
    """Parse downloaded Form 4 filing text files from filing_docs/ for display."""
    import glob
    import re

    pattern = os.path.join(_FILING_DOCS_DIR, f"{ticker.upper()}_4_*.txt")
    files = sorted(glob.glob(pattern), reverse=True)[:20]
    if not files:
        return []

    # Common address fragments to strip from names
    _ADDR = re.compile(
        r"(?:C/O\b|ONE |TWO |\d{2,5} |P\.?O\.? BOX).*$", re.I
    )

    trades: list[dict] = []
    for fpath in files:
        try:
            text = open(fpath, "r", errors="replace").read(8000)
        except Exception:
            continue

        # ── Extract filer name ──
        name = ""
        # Format A (table): "Reporting Person*\n NameADDRESS"
        m = re.search(r"Reporting Person\*?\s*\n\s*(.+?)(?:ONE |C/O |TWO |\d{3,})", text)
        if m:
            name = m.group(1).strip()
        if not name:
            # Format B (plain text): name on line after "Reporting Person*"
            m = re.search(r"Reporting Person\*?\s*\n([A-Z][a-zA-Z .'-]+)", text)
            if m:
                name = m.group(1).strip()
        # Clean any remaining address fragments
        name = _ADDR.sub("", name).strip().rstrip(",")

        # ── Extract title ──
        title = ""
        # Format A (table): "XDirectorOfficer (give title below)10% OwnerOther..."
        m = re.search(r"Officer \(give title below\).*?Other[^\n]*\n[^|]*", text)
        if not m:
            m = re.search(r"Officer \(give title below\)\s*\n\s*\n?\s*Other[^\n]*\n\s*\n?(.+)", text)
            if m:
                title = m.group(1).strip()
        if not title:
            m = re.search(r"Officer \(give title below\).*?(?:10%|Other)[^\n]*?([A-Z][A-Za-z &,/]+(?:Officer|President|CEO|CFO|VP|Director|Counsel|Secretary|Controller)[A-Za-z &,/]*)", text)
            if m:
                title = m.group(1).strip()
        if not title:
            # Fallback: look for common C-suite titles anywhere near top
            m = re.search(r"((?:Chief .{3,30} Officer|EVP[^|]{0,40}|SVP[^|]{0,40}|President[^|]{0,20}|General Counsel|Secretary|Controller|Treasurer))", text[:2000])
            if m:
                title = m.group(1).strip()

        # ── Relationship ──
        is_director = bool(re.search(r"X\s*Director|Director\s*\n\s*\n?\s*X", text))
        is_officer = bool(title) or bool(re.search(r"X\s*Officer|Officer.*\n\s*X", text))

        # ── Transaction date ──
        tx_date = ""
        m = re.search(r"Earliest Transaction.*?(\d{2}/\d{2}/\d{4})", text, re.S)
        if m:
            parts = m.group(1).split("/")
            if len(parts) == 3:
                tx_date = f"{parts[2]}-{parts[0]}-{parts[1]}"

        # ── Transaction code ──
        # Table format: "| M |" or "| S |" in pipe-delimited rows
        codes_table = re.findall(r"\|\s*([SPMFAX])\s*\|", text)
        # Plain format: standalone code on a line
        codes_plain = re.findall(r"^\s*([SPMFAX])\s*$", text, re.M)
        codes = codes_table or codes_plain
        tx_code = ""
        for c in codes:
            if c in ("S", "P"):
                tx_code = c
                break
        if not tx_code:
            for c in codes:
                if c in ("M", "F"):
                    tx_code = c
                    break

        # ── 10b5-1 plan ──
        is_10b5 = bool(re.search(r"10b5-1", text, re.I))

        # ── Shares ──
        shares = 0
        # Table: "1,255 | A |" or "1,255 | D |"
        share_matches = re.findall(r"(\d{1,3}(?:,\d{3})*)\s*\|\s*[AD]\s*\|", text)
        if not share_matches:
            share_matches = re.findall(r"(\d{1,3}(?:,\d{3})*)\s*\n\s*[AD]\s*\n", text)
        for sm in share_matches:
            v = int(sm.replace(",", ""))
            if v > shares:
                shares = v

        # ── Build label ──
        if tx_code == "P":
            tx_label = "BUY"
        elif tx_code == "S":
            tx_label = "SELL"
        elif tx_code in ("M", "F"):
            tx_label = "EXERCISE"
        else:
            tx_label = "OTHER"

        # Clean title of stray form text
        if title and re.search(r"\d\.\s*Date|Transaction|Earliest", title):
            title = ""
        role = title or ("Director" if is_director else "")
        summary_parts = [p for p in [name, role, "10b5-1" if is_10b5 else ""] if p]

        trades.append({
            "owner": name[:80] or "Unknown",
            "title": role[:80],
            "date": tx_date[:10],
            "tx_type": tx_label,
            "net_shares": shares if tx_label == "BUY" else -shares,
            "est_value": 0,
            "signal_summary": " · ".join(summary_parts),
        })

    trades.sort(key=lambda t: (t.get("date") or ""), reverse=True)
    return trades[:12]


# ── Timeline builder ─────────────────────────────────────────────────────────

def _extract_date(d: dict, *keys: str) -> str:
    """Return the first non-empty ISO date string found among *keys*."""
    for k in keys:
        v = d.get(k)
        if v:
            if isinstance(v, (dt.date, dt.datetime)):
                return v.isoformat()
            return str(v)
    return ""


def _build_timeline(
    proposals: list[dict],
    filings: list[dict],
    research_threads: list[dict],
    records: list[dict],
    earnings: list[dict],
    thinking_chains: list[dict],
    limit: int = 30,
) -> list[dict]:
    """Merge items from every source into a single date-sorted timeline."""
    entries: list[dict] = []

    for p in proposals:
        entries.append({
            "type": "proposal",
            "title": p.get("headline") or p.get("action_type") or "Proposal",
            "date": _extract_date(p, "created_at"),
            "detail": (p.get("reasoning") or "")[:200],
            "emoji": "\U0001f4a1",
        })

    for f in filings:
        entries.append({
            "type": "filing",
            "title": f"SEC {f.get('form', '')} Filing",
            "date": _extract_date(f, "filing_date", "processed_at"),
            "detail": f.get("accession", ""),
            "emoji": "\U0001f4c4",
        })

    for t in research_threads:
        entries.append({
            "type": "thread",
            "title": t.get("title") or "Research Thread",
            "date": _extract_date(t, "updated_at", "created_at"),
            "detail": (t.get("thesis") or "")[:200],
            "emoji": t.get("emoji") or "\U0001f52c",
        })

    for r in records:
        entries.append({
            "type": "record",
            "title": r.get("title") or "Record",
            "date": _extract_date(r, "created_at"),
            "detail": (r.get("body") or r.get("content") or "")[:200],
            "emoji": {
                "task": "\u2705",
                "note": "\U0001f4dd",
                "question": "\u2753",
                "reference": "\U0001f4ce",
                "inbox": "\U0001f4ec",
            }.get(r.get("kind", ""), "\U0001f4dd"),
        })

    for e in earnings:
        entries.append({
            "type": "earning",
            "title": f"Earnings — {e.get('quarter', '')} {e.get('year', '')}".strip(),
            "date": _extract_date(e, "created_at"),
            "detail": (e.get("summary") or e.get("guidance_direction") or "")[:200],
            "emoji": "\U0001f4ca",
        })

    for c in thinking_chains:
        entries.append({
            "type": "chain",
            "title": c.get("title") or "Thinking Chain",
            "date": _extract_date(c, "updated_at", "created_at"),
            "detail": (c.get("notes") or "")[:200],
            "emoji": c.get("emoji") or "\U0001f4a1",
        })

    # Sort descending by date (empty dates sink to the bottom)
    entries.sort(key=lambda e: e["date"] or "0000", reverse=True)
    return entries[:limit]


# ── Main entry point ─────────────────────────────────────────────────────────

def get_ticker_hub(ticker: str) -> dict:
    """Gather everything the system knows about *ticker* across all tables.

    Returns a dict with keys: profile, position, research_threads,
    thinking_chains, proposals, earnings, records, filings, work_graph,
    timeline.  Each sub-section degrades gracefully on failure.

    All independent DB queries run in parallel via ThreadPoolExecutor.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    ticker = (ticker or "").strip().upper()
    if not ticker:
        return {"error": "empty ticker"}

    # ── Helper: run query with error handling ─────────────────────────────
    def _q(label, sql, params):
        try:
            return (label, _safe_query(sql, params))
        except Exception as exc:
            LOGGER.debug("ticker_hub %s failed: %s", label, exc)
            return (label, [])

    def _q1(label, sql, params):
        try:
            return (label, _safe_query_one(sql, params))
        except Exception as exc:
            LOGGER.debug("ticker_hub %s failed: %s", label, exc)
            return (label, None)

    def _fetch_company_detail():
        try:
            from app.services.company_file_service import company_detail as _cd
            return ("company", _cd(ticker) or {})
        except Exception as exc:
            LOGGER.debug("ticker_hub company_detail failed: %s", exc)
            return ("company", {})

    def _fetch_profile():
        try:
            row = _safe_query_one(
                "SELECT * FROM companies WHERE UPPER(ticker) = %s", (ticker,)
            )
            if not row:
                row = _safe_query_one(
                    "SELECT * FROM companies_core WHERE UPPER(ticker) = %s", (ticker,)
                )
            return ("profile", row or {})
        except Exception as exc:
            LOGGER.debug("ticker_hub profile failed: %s", exc)
            return ("profile", {})

    def _fetch_thinking_chains():
        try:
            from app.services.work_graph_service import get_links
            chains: list[dict] = []
            links = get_links("ticker", ticker, filter_type="chain")
            if links:
                chain_ids = [lk.get("other_id") for lk in links if lk.get("other_type") == "chain"]
                if chain_ids:
                    placeholders = ",".join(["%s"] * len(chain_ids))
                    chains = _safe_query(
                        f"SELECT * FROM thinking_chains_core WHERE id IN ({placeholders}) "
                        "ORDER BY updated_at DESC",
                        tuple(int(cid) for cid in chain_ids),
                    )
            tag_chains = _safe_query(
                "SELECT * FROM thinking_chains_core "
                "WHERE UPPER(tags) LIKE %s AND status != 'deleted' "
                "ORDER BY updated_at DESC LIMIT 20",
                (f"%{ticker}%",),
            )
            if tag_chains:
                seen_ids = {c.get("id") for c in chains}
                for tc in tag_chains:
                    if tc.get("id") not in seen_ids:
                        chains.append(tc)
            return ("thinking_chains", chains)
        except Exception as exc:
            LOGGER.debug("ticker_hub thinking_chains failed: %s", exc)
            return ("thinking_chains", [])

    def _fetch_filings():
        try:
            # Merge both sources: poll_state (recent) + filings_core (full archive)
            seen_acc: set = set()
            merged: list[dict] = []

            poll_rows = _safe_query(
                "SELECT ticker, form, filing_date, accession, processed_at "
                "FROM sec_edgar_poll_state_core "
                "WHERE UPPER(ticker) = %s ORDER BY filing_date DESC LIMIT 50",
                (ticker,),
            )
            for r in poll_rows:
                acc = r.get("accession", "")
                if acc and acc not in seen_acc:
                    seen_acc.add(acc)
                    merged.append(r)

            core_rows = _safe_query(
                "SELECT ticker, form, date AS filing_date, accession, "
                "downloaded_at AS processed_at "
                "FROM filings_core "
                "WHERE UPPER(ticker) = %s ORDER BY date DESC LIMIT 100",
                (ticker,),
            )
            for r in core_rows:
                acc = r.get("accession", "")
                if acc and acc not in seen_acc:
                    seen_acc.add(acc)
                    merged.append(r)

            # Sort by date descending
            merged.sort(key=lambda x: x.get("filing_date") or "0000", reverse=True)
            return ("filings", merged)
        except Exception as exc:
            LOGGER.debug("ticker_hub filings failed: %s", exc)
            return ("filings", [])

    def _fetch_work_graph():
        try:
            from app.services.work_graph_service import get_links, resolve_linked_objects
            raw_links = get_links("ticker", ticker)
            return ("work_graph", resolve_linked_objects(raw_links) if raw_links else [])
        except Exception as exc:
            LOGGER.debug("ticker_hub work_graph failed: %s", exc)
            return ("work_graph", [])

    # ── Launch all independent queries in parallel ────────────────────────
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = [
            ex.submit(_fetch_profile),
            ex.submit(_q1, "position", "SELECT * FROM portfolio_positions_core WHERE UPPER(ticker) = %s", (ticker,)),
            ex.submit(_q, "research_threads", "SELECT * FROM research_threads_core WHERE UPPER(ticker) = %s AND status != 'deleted' ORDER BY updated_at DESC", (ticker,)),
            ex.submit(_fetch_thinking_chains),
            ex.submit(_q, "proposals", "SELECT * FROM action_proposals_core WHERE UPPER(ticker) = %s ORDER BY created_at DESC LIMIT 20", (ticker,)),
            ex.submit(_q, "earnings", "SELECT * FROM earnings_analysis_core WHERE UPPER(ticker) = %s ORDER BY created_at DESC LIMIT 10", (ticker,)),
            ex.submit(_q, "records", "SELECT * FROM investment_records_core WHERE UPPER(ticker) = %s AND status != 'deleted' ORDER BY created_at DESC LIMIT 30", (ticker,)),
            ex.submit(_fetch_filings),
            ex.submit(_fetch_work_graph),
            ex.submit(_fetch_company_detail),
            ex.submit(_q, "transactions", "SELECT * FROM portfolio_transactions_core WHERE UPPER(ticker) = %s ORDER BY trade_date DESC LIMIT 30", (ticker,)),
            ex.submit(_q, "decisions", "SELECT * FROM decision_log_core WHERE UPPER(ticker) = %s ORDER BY created_at DESC LIMIT 20", (ticker,)),
            ex.submit(_q, "cascades", "SELECT * FROM portfolio_cascade_alerts_core WHERE UPPER(ticker) = %s ORDER BY created_at DESC LIMIT 20", (ticker,)),
            ex.submit(_q, "thesis_breaches", "SELECT * FROM thesis_breach_alerts_core WHERE UPPER(ticker) = %s ORDER BY created_at DESC LIMIT 20", (ticker,)),
            ex.submit(_q, "case_studies", "SELECT * FROM lab_case_studies_core WHERE UPPER(ticker) = %s ORDER BY created_at DESC LIMIT 10", (ticker,)),
            ex.submit(_q, "playbook_runs", "SELECT * FROM playbook_runs_core WHERE UPPER(ticker) = %s ORDER BY created_at DESC LIMIT 10", (ticker,)),
        ]
        for fut in as_completed(futures):
            label, data = fut.result()
            results[label] = data

    profile = results.get("profile") or {}
    position = results.get("position")
    research_threads = results.get("research_threads") or []
    thinking_chains = results.get("thinking_chains") or []
    proposals = results.get("proposals") or []
    earnings = results.get("earnings") or []
    records = results.get("records") or []
    filings = results.get("filings") or []
    work_graph = results.get("work_graph") or []
    company = results.get("company") or {}
    transactions = results.get("transactions") or []
    decisions = results.get("decisions") or []
    cascades = results.get("cascades") or []
    thesis_breaches = results.get("thesis_breaches") or []
    case_studies = results.get("case_studies") or []
    playbook_runs = results.get("playbook_runs") or []

    # ── Dependent: timeline (needs proposals, filings, etc.) ──────────────
    timeline: list[dict] = []
    try:
        timeline = _build_timeline(
            proposals=proposals,
            filings=filings,
            research_threads=research_threads,
            records=records,
            earnings=earnings,
            thinking_chains=thinking_chains,
            limit=30,
        )
    except Exception as exc:
        LOGGER.debug("ticker_hub timeline failed: %s", exc)

    # ── Dependent: debates (needs proposal IDs) ───────────────────────────
    debates: list[dict] = []
    try:
        proposal_ids = [p.get("id") for p in proposals if p.get("id")]
        if proposal_ids:
            placeholders = ",".join(["%s"] * len(proposal_ids))
            debates = _safe_query(
                f"SELECT * FROM proposal_debate_artifacts "
                f"WHERE proposal_id IN ({placeholders}) ORDER BY created_at DESC",
                tuple(int(pid) for pid in proposal_ids),
            )
    except Exception as exc:
        LOGGER.debug("ticker_hub debates failed: %s", exc)

    # Merge company_detail data into the hub for template access
    moats = company.get("moats") or []
    competitors = company.get("competitors") or []
    similar_companies = company.get("similar_companies") or []
    supply_chain = company.get("supply_chain") or {}
    notes = company.get("notes") or []
    tasks = company.get("tasks") or []
    reminders = company.get("reminders") or []
    mini_statements = company.get("mini_statements") or {}
    price_metrics = company.get("price_metrics") or {}
    financial_deltas = company.get("financial_deltas") or {}
    revenue_segments = company.get("revenue_segments") or {}
    insider_trades = company.get("insider_trades") or []
    # Fallback: if intel has no insider trades, parse downloaded Form 4 files
    if not insider_trades:
        try:
            insider_trades = _insider_trades_from_files(ticker)
        except Exception as exc:
            LOGGER.debug("ticker_hub insider_trades_from_files failed: %s", exc)
    active_proposal = company.get("active_proposal") or {}
    data_coverage = company.get("data_coverage") or {}
    earnings_analysis_detailed = company.get("earnings_analysis") or []
    filing_groups = company.get("filing_groups") or []
    moat_options = company.get("moat_options") or []
    buyback = company.get("buyback") or {}
    earnings_calls = company.get("earnings_calls") or []
    earnings_releases = company.get("earnings_releases") or []
    earnings_sec_brief = company.get("earnings_sec_brief") or {}
    quarterly_signals = company.get("quarterly_signals") or []
    current_price = company.get("current_price") or ""

    # Enrich profile with company_detail fields if profile was sparse
    if company:
        if not profile.get("name"):
            profile["name"] = company.get("name") or ""
        if not profile.get("industry"):
            profile["industry"] = company.get("industry") or ""
        if not profile.get("country"):
            profile["country"] = company.get("country") or ""
        profile["market_cap"] = company.get("market_cap") or profile.get("market_cap") or ""
        profile["current_price"] = company.get("current_price") or ""

    return {
        "ticker": ticker,
        "profile": profile,
        "position": position,
        "research_threads": research_threads,
        "thinking_chains": thinking_chains,
        "proposals": proposals,
        "earnings": earnings,
        "records": records,
        "filings": filings,
        "work_graph": work_graph,
        "timeline": timeline,
        # ── New: company_detail data ──
        "moats": moats,
        "moat_options": moat_options,
        "competitors": competitors,
        "similar_companies": similar_companies,
        "supply_chain": supply_chain,
        "notes": notes,
        "tasks": tasks,
        "reminders": reminders,
        "mini_statements": mini_statements,
        "price_metrics": price_metrics,
        "financial_deltas": financial_deltas,
        "revenue_segments": revenue_segments,
        "insider_trades": insider_trades,
        "active_proposal": active_proposal,
        "data_coverage": data_coverage,
        "earnings_analysis_detailed": earnings_analysis_detailed,
        "filing_groups": filing_groups,
        "buyback": buyback,
        "earnings_calls": earnings_calls,
        "earnings_releases": earnings_releases,
        "earnings_sec_brief": earnings_sec_brief,
        "quarterly_signals": quarterly_signals,
        "current_price": current_price,
        # ── New: additional tables ──
        "transactions": transactions,
        "decisions": decisions,
        "cascades": cascades,
        "thesis_breaches": thesis_breaches,
        "debates": debates,
        "case_studies": case_studies,
        "playbook_runs": playbook_runs,
    }
