"""
Phase 3.1: SEC EDGAR Poller Service
--------------------------------------
Auto-ingests new SEC filings for a broad signal universe and feeds them into
the event pipeline without manual trigger.

Scope:
  - portfolio + watchlist tickers  → held tickers
  - signal_universe_core tickers   → universe-only (not held)

Flow:
  poll_and_ingest_tickers()
    → for each scope ticker (held + signal universe):
        - fetch filing metadata only (fast, no markdown yet)
        - skip already-seen accessions (dedupe via sec_edgar_poll_state_core)
        - for new filings only: download markdown, save to filing_docs/, upsert filings_core
        - if HELD ticker:
            create_event(type='sec_filing') → proposals + monitor pipeline
        - if UNIVERSE-ONLY ticker:
            create_event(type='universe_signal') → cascade analysis only
            LLM filters noise: only surfaces alerts with real connections to held positions
"""

from __future__ import annotations

import datetime as dt
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from app.core import cloud_files
from app.core.ticker import safe_ticker as _safe_ticker
from app.services.events_service import create_event, process_event
from app.services.postgres_core_service import core_backend, pg_connect

# Material forms to poll (Form 4 excluded — high volume, low signal for background polling)
POLL_FORMS = ("8-K", "10-Q", "10-K", "6-K", "20-F")
# How many recent filings to check per ticker per poll run
PER_TICKER_LIMIT = 8

# Key tickers to monitor for cross-portfolio signals even if not held.
# Seeded into signal_universe_core on first run; user can add/remove at runtime.
DEFAULT_SIGNAL_UNIVERSE: dict[str, tuple[str, str]] = {
    "TSM":   ("TSMC — dominant chip foundry; supply chain for AAPL/NVDA/AMD/QCOM", "supplier"),
    "NVDA":  ("NVIDIA — AI/GPU bellwether, data center capex proxy", "sector"),
    "ASML":  ("ASML — semiconductor lithography monopoly; gates entire chip supply", "supplier"),
    "INTC":  ("Intel — semiconductor competitor + foundry alternative to TSMC", "competitor"),
    "MSFT":  ("Microsoft — cloud/enterprise spending proxy", "sector"),
    "AMZN":  ("Amazon — cloud + consumer spending proxy", "sector"),
    "GOOGL": ("Alphabet — digital ad + cloud proxy", "sector"),
    "META":  ("Meta — digital advertising spending proxy", "sector"),
    "JPM":   ("JPMorgan — credit conditions + rate sensitivity proxy", "macro"),
    "COST":  ("Costco — consumer spending health proxy", "macro"),
    "WMT":   ("Walmart — consumer spending + supply chain health", "macro"),
    "CAT":   ("Caterpillar — industrial capex + China exposure proxy", "macro"),
}

def _now() -> str:
    return dt.datetime.now().isoformat()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_poller_schema() -> None:
    """Create poll state + signal universe tables."""
    if core_backend() != "postgres":
        return
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sec_edgar_poll_state_core (
                ticker TEXT NOT NULL,
                accession TEXT NOT NULL,
                form TEXT NOT NULL DEFAULT '',
                filing_date TEXT NOT NULL DEFAULT '',
                filing_id BIGINT NOT NULL DEFAULT 0,
                processed_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (ticker, accession)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_sec_poll_state_ticker ON sec_edgar_poll_state_core(ticker)"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_universe_core (
                ticker TEXT PRIMARY KEY,
                reason TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'macro',
                added_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        con.commit()
        # Seed defaults if table is empty
        cur.execute("SELECT COUNT(*) FROM signal_universe_core")
        if (cur.fetchone() or [0])[0] == 0:
            now = _now()
            for tk, (reason, category) in DEFAULT_SIGNAL_UNIVERSE.items():
                cur.execute(
                    "INSERT INTO signal_universe_core(ticker, reason, category, added_at) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT(ticker) DO NOTHING",
                    (tk, reason, category, now),
                )
            con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


def get_signal_universe_tickers() -> set[str]:
    """Return all tickers in the signal universe (monitored but not necessarily held)."""
    con = pg_connect()
    if con is None:
        return set()
    try:
        cur = con.cursor()
        cur.execute("SELECT ticker FROM signal_universe_core")
        return {str(r[0]) for r in (cur.fetchall() or [])}
    except Exception:
        return set()
    finally:
        con.close()


def add_to_signal_universe(ticker: str, reason: str = "", category: str = "macro") -> bool:
    """Add a ticker to the signal universe. Returns True on success."""
    tk = _safe_ticker(ticker)
    if not tk:
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO signal_universe_core(ticker, reason, category, added_at) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT(ticker) DO UPDATE SET reason=EXCLUDED.reason, category=EXCLUDED.category",
            (tk, str(reason or "")[:300], str(category or "macro")[:30], _now()),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def remove_from_signal_universe(ticker: str) -> bool:
    """Remove a ticker from the signal universe."""
    tk = _safe_ticker(ticker)
    if not tk:
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM signal_universe_core WHERE ticker=%s", (tk,))
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def _has_seen(ticker: str, accession: str) -> bool:
    """Return True if this (ticker, accession) was already processed."""
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT 1 FROM sec_edgar_poll_state_core WHERE ticker=%s AND accession=%s LIMIT 1",
            (str(ticker or "").upper(), str(accession or "")),
        )
        return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        con.close()


def _mark_seen(ticker: str, accession: str, form: str, filing_date: str, filing_id: int) -> None:
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO sec_edgar_poll_state_core(ticker, accession, form, filing_date, filing_id, processed_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (ticker, accession) DO NOTHING
            """,
            (
                str(ticker or "").upper(),
                str(accession or ""),
                str(form or ""),
                str(filing_date or ""),
                int(filing_id or 0),
                _now(),
            ),
        )
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Filing download via edgartools
# ---------------------------------------------------------------------------

def _set_edgar_identity() -> None:
    try:
        import edgar
        edgar.set_identity("InvestorOS research@investoros.local")
    except Exception:
        pass


def _fetch_filing_metadata(ticker: str, forms: tuple[str, ...], limit: int = PER_TICKER_LIMIT) -> list[dict[str, Any]]:
    """
    Fetch recent filing *metadata* only (no markdown download).
    Returns list of {accession, form, filing_date, period, doc_url, _obj}.
    _obj is the raw edgartools Filing object; call _obj.markdown() only when needed.
    """
    _set_edgar_identity()
    try:
        import edgar
        company = edgar.Company(ticker)
        all_filings = list(company.get_filings(form=list(forms)))
        out: list[dict[str, Any]] = []
        for f in all_filings[:limit]:
            accession = str(getattr(f, "accession_number", "") or "").strip()
            if not accession:
                continue
            out.append({
                "accession": accession,
                "form": str(getattr(f, "form", "") or "").strip().upper(),
                "filing_date": str(getattr(f, "filing_date", "") or "").strip(),
                "period": str(getattr(f, "period_of_report", "") or "").strip(),
                "doc_url": str(getattr(f, "homepage_url", "") or "").strip(),
                "_obj": f,  # raw object — call f.markdown() only for new filings
            })
        return out
    except Exception:
        return []


def _download_markdown(filing_obj: Any) -> str:
    """Download and return filing text as markdown. Returns '' on failure."""
    try:
        return str(filing_obj.markdown() or "").strip()[:80000]
    except Exception:
        return ""


def _save_filing_text(ticker: str, form: str, accession: str, text: str) -> str:
    """
    Save filing text and return canonical runtime path.
    Path is stored as repo-relative `filing_docs/...` so cloud/local both resolve.
    Returns "" on failure.
    """
    if not text:
        return ""
    try:
        # Sanitize accession for filename: replace / with - and strip special chars
        safe_acc = re.sub(r"[^A-Za-z0-9\-]", "", str(accession or "").replace("/", "-"))[:40]
        fname = f"{ticker}_{form}_{safe_acc}.txt"
        rel_path = f"filing_docs/{fname}"
        ok = cloud_files.write_text(rel_path, text)
        return rel_path if ok else ""
    except Exception:
        return ""


def _upsert_filing_core(
    ticker: str,
    form: str,
    filing_date: str,
    accession: str,
    doc_url: str,
    path: str,
    content: str = "",
) -> int:
    """
    Insert into filings_core if accession not seen for this ticker.
    Returns the filing_id (existing or new), or 0 on failure.
    content is stored in the DB so Cloud Run can read it without GCS.
    """
    tk = _safe_ticker(ticker)
    if not tk or not accession:
        return 0
    if core_backend() != "postgres":
        return 0
    con = pg_connect()
    if con is None:
        return 0
    try:
        cur = con.cursor()
        # Check if this accession already exists for this ticker
        cur.execute(
            "SELECT id FROM filings_core WHERE ticker=%s AND accession=%s LIMIT 1",
            (tk, str(accession or "")),
        )
        existing = cur.fetchone()
        if existing:
            filing_id = int(existing[0] or 0)
            # Back-fill content if we have it now and row is empty
            if content and filing_id:
                cur.execute(
                    "UPDATE filings_core SET content=%s WHERE id=%s AND content=''",
                    (str(content), filing_id),
                )
                con.commit()
            return filing_id
        # Generate new ID
        cur.execute("SELECT COALESCE(MAX(id),0)+1 FROM filings_core")
        new_id = int((cur.fetchone() or [1])[0] or 1)
        now = _now()
        cur.execute(
            """
            INSERT INTO filings_core(id, ticker, form, date, accession, doc_url, path, downloaded_at, content)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(id) DO NOTHING
            """,
            (
                new_id,
                tk,
                str(form or ""),
                str(filing_date or ""),
                str(accession or "")[:120],
                str(doc_url or "")[:500],
                str(path or "")[:500],
                now,
                str(content or ""),
            ),
        )
        con.commit()
        return new_id
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return 0
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Main poll entry points
# ---------------------------------------------------------------------------

def poll_ticker(
    ticker: str,
    forms: tuple[str, ...] = POLL_FORMS,
    is_held: bool = True,
) -> dict[str, Any]:
    """
    Poll one ticker for new SEC filings.

    is_held=True  → normal proposal pipeline (sec_filing event)
    is_held=False → cascade-only analysis (universe_signal event); no proposals generated
    """
    ensure_poller_schema()
    tk = _safe_ticker(ticker)
    if not tk:
        return {"ok": False, "error": "invalid_ticker"}

    # Phase 1: fetch metadata only (fast — no markdown download yet)
    metadata = _fetch_filing_metadata(tk, forms)
    if not metadata:
        return {"ok": True, "ticker": tk, "new_filings": 0, "events_created": 0}

    # Phase 2: identify truly new filings (skip markdown for already-seen)
    new_meta = [fi for fi in metadata if not _has_seen(tk, str(fi.get("accession") or ""))]
    if not new_meta:
        return {"ok": True, "ticker": tk, "new_filings": 0, "events_created": 0}

    # Phase 3: for each new filing — download markdown, save, upsert, create event
    event_ids: list[int] = []
    new_filings = 0
    event_type = "sec_filing" if is_held else "universe_signal"

    for fi in new_meta:
        accession = str(fi.get("accession") or "").strip()
        form = str(fi.get("form") or "").strip()
        filing_date = str(fi.get("filing_date") or "").strip()
        doc_url = str(fi.get("doc_url") or "").strip()

        md_text = _download_markdown(fi["_obj"])
        path = _save_filing_text(tk, form, accession, md_text) if md_text else ""

        filing_id = _upsert_filing_core(
            ticker=tk, form=form, filing_date=filing_date,
            accession=accession, doc_url=doc_url, path=path,
            content=md_text or "",
        )
        if filing_id <= 0:
            continue

        new_filings += 1
        _mark_seen(tk, accession, form, filing_date, filing_id)

        ev = create_event(
            source="sec_edgar", ticker=tk, event_type=event_type,
            occurred_at=filing_date or _now(),
            payload={"filing_id": filing_id, "form": form, "accession": accession,
                     "filing_date": filing_date, "doc_url": doc_url, "path": path},
            dedupe_key=f"{event_type}:{tk}:{accession}",
        )
        ev_id = int(ev.get("id") or 0)
        if ev_id > 0:
            event_ids.append(ev_id)

    # Phase 4: process all new events in parallel (capped at 4 concurrent LLM chains)
    events_created = 0
    if event_ids:
        max_workers = min(len(event_ids), 4)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(process_event, str(eid)): eid for eid in event_ids}
            for fut in as_completed(futures):
                try:
                    fut.result()
                    events_created += 1
                except Exception:
                    pass

    return {
        "ok": True,
        "ticker": tk,
        "is_held": is_held,
        "new_filings": new_filings,
        "events_created": events_created,
    }


def poll_and_ingest_tickers(
    tickers: list[str] | None = None,
    forms: tuple[str, ...] = POLL_FORMS,
) -> dict[str, Any]:
    """
    Main entry point for the SEC poll background loop.
    If tickers=None, polls portfolio + watchlist (held) AND signal_universe_core (universe-only).
    Universe-only filings route to cascade analysis instead of proposal generation.
    """
    ensure_poller_schema()

    if tickers is None:
        from app.services.proactive_ai_service import _read_scope_tickers
        portfolio, watchlist, _bluechips = _read_scope_tickers()
        held = set(portfolio) | set(watchlist)
        universe = get_signal_universe_tickers()
        # Universe-only = in signal universe but NOT already in held (avoid double-processing)
        universe_only = universe - held
        scope_held = sorted(held)
        scope_universe = sorted(universe_only)
    else:
        # Explicit list: treat all as held
        scope_held = [_safe_ticker(t) for t in tickers if _safe_ticker(t)]
        scope_universe = []

    if not scope_held and not scope_universe:
        return {"ok": True, "tickers_checked": 0, "new_filings": 0, "events_created": 0}

    total_new = 0
    total_events = 0
    errors: list[str] = []

    for tk in scope_held:
        try:
            result = poll_ticker(tk, forms=forms, is_held=True)
            total_new += int(result.get("new_filings") or 0)
            total_events += int(result.get("events_created") or 0)
        except Exception as exc:
            errors.append(f"{tk}:{str(exc)[:60]}")

    for tk in scope_universe:
        try:
            result = poll_ticker(tk, forms=forms, is_held=False)
            total_new += int(result.get("new_filings") or 0)
            total_events += int(result.get("events_created") or 0)
        except Exception as exc:
            errors.append(f"universe:{tk}:{str(exc)[:60]}")

    return {
        "ok": True,
        "tickers_checked": len(scope_held) + len(scope_universe),
        "held_checked": len(scope_held),
        "universe_checked": len(scope_universe),
        "new_filings": total_new,
        "events_created": total_events,
        "errors": errors[:10],
        "polled_at": _now(),
    }


# ---------------------------------------------------------------------------
# Form 4 — smart insider trade filtering
# ---------------------------------------------------------------------------

_EXECUTIVE_TITLES = re.compile(
    r"\b(chief\s+executive|ceo|chief\s+financial|cfo|chief\s+operating|coo|"
    r"president|chairman|executive\s+vice\s+president|evp|svp|senior\s+vice\s+president)\b",
    re.I,
)
_PLAN_10B5 = re.compile(r"\b10b5[-\s]?1\b", re.I)
_TX_OPEN_MARKET = re.compile(r"\btransaction\s+code[:\s]+([A-Z])\b", re.I)
_SHARES_PRICE = re.compile(
    r"([\d,]+)\s+shares?\s+(?:at|@)\s+\$?([\d.]+)", re.I
)


def _is_significant_insider_trade(
    markdown_text: str,
    threshold_usd: float = 500_000,
) -> dict[str, Any]:
    """
    Parse Form 4 markdown and determine if this is a significant insider trade.
    Returns {significant: bool, reason: str, tx_type: str, filer_title: str, est_value: float}.
    Filters IN: CEO/CFO/President + open-market sale (S) or purchase (P) + value > threshold.
    Filters OUT: option exercises (F/M/X), 10b5-1 plans, small transactions.
    """
    text = str(markdown_text or "")
    low = text.lower()

    # Is this a 10b5-1 plan? If so, skip (pre-scheduled, not informative)
    if _PLAN_10B5.search(text):
        return {"significant": False, "reason": "10b5-1_plan"}

    # Check transaction code
    tx_match = _TX_OPEN_MARKET.search(text)
    tx_code = tx_match.group(1).upper() if tx_match else ""
    # S = sale, P = open-market purchase; skip grants/options (F, M, X, G, A, D, etc.)
    if tx_code not in ("S", "P", ""):
        return {"significant": False, "reason": f"non_open_market_tx_{tx_code}"}

    # Is filer an executive?
    exec_match = _EXECUTIVE_TITLES.search(text)
    filer_title = exec_match.group(0).strip() if exec_match else ""
    if not filer_title:
        return {"significant": False, "reason": "non_executive_filer"}

    # Estimate transaction value
    est_value = 0.0
    for m in _SHARES_PRICE.finditer(text):
        try:
            shares = float(m.group(1).replace(",", ""))
            price = float(m.group(2))
            est_value = max(est_value, shares * price)
        except Exception:
            pass

    if est_value < threshold_usd and est_value > 0:
        return {"significant": False, "reason": f"below_threshold_{est_value:.0f}"}

    return {
        "significant": True,
        "reason": "executive_open_market_trade",
        "tx_type": tx_code or "S",
        "filer_title": filer_title,
        "est_value": round(est_value, 2),
    }


def poll_form4_for_tickers(
    tickers: list[str] | None = None,
    threshold_usd: float = 500_000,
    limit_per_ticker: int = 10,
) -> dict[str, Any]:
    """
    Daily Form 4 poll — separate from the main 6-hour cycle.
    Only surfaces significant insider trades: executive + open-market + above threshold + not 10b5-1.
    """
    ensure_poller_schema()

    if tickers is None:
        from app.services.proactive_ai_service import _read_scope_tickers
        portfolio, watchlist, _ = _read_scope_tickers()
        scope = sorted(set(portfolio) | set(watchlist))
    else:
        scope = [_safe_ticker(t) for t in tickers if _safe_ticker(t)]

    if not scope:
        return {"ok": True, "tickers_checked": 0, "signals_created": 0}

    total_signals = 0
    errors: list[str] = []

    for tk in scope:
        try:
            metadata = _fetch_filing_metadata(tk, ("4",), limit=limit_per_ticker)
            for fi in metadata:
                accession = str(fi.get("accession") or "").strip()
                if not accession or _has_seen(tk, accession):
                    continue
                filing_date = str(fi.get("filing_date") or "").strip()
                md_text = _download_markdown(fi["_obj"])
                if not md_text:
                    _mark_seen(tk, accession, "4", filing_date, 0)
                    continue
                check = _is_significant_insider_trade(md_text, threshold_usd)
                if not check.get("significant"):
                    # Mark seen so we don't re-check
                    _mark_seen(tk, accession, "4", filing_date, 0)
                    continue
                # Significant trade — save, create event
                path = _save_filing_text(tk, "4", accession, md_text)
                filing_id = _upsert_filing_core(
                    ticker=tk, form="4", filing_date=filing_date,
                    accession=accession, doc_url=str(fi.get("doc_url") or ""), path=path,
                    content=md_text,
                )
                _mark_seen(tk, accession, "4", filing_date, filing_id or 0)
                signal_summary = (
                    f"Form 4: {check['filer_title']} {check['tx_type']} "
                    f"~${check['est_value']:,.0f} on {filing_date}"
                )
                ev = create_event(
                    source="sec_edgar", ticker=tk, event_type="insider_trade_signal",
                    occurred_at=filing_date or _now(),
                    payload={
                        "filing_id": filing_id or 0,
                        "form": "4",
                        "accession": accession,
                        "tx_type": check["tx_type"],
                        "filer_title": check["filer_title"],
                        "est_value": check["est_value"],
                        "path": path,
                        "signal_summary": signal_summary,
                    },
                    dedupe_key=f"form4:{tk}:{accession}",
                )
                ev_id = int(ev.get("id") or 0)
                if ev_id > 0:
                    try:
                        process_event(str(ev_id))
                        total_signals += 1
                    except Exception:
                        pass
        except Exception as exc:
            errors.append(f"{tk}:{str(exc)[:60]}")

    return {
        "ok": True,
        "tickers_checked": len(scope),
        "signals_created": total_signals,
        "errors": errors[:10],
        "polled_at": _now(),
    }
