"""autonomic_service.py — Layer 4: Autonomic Nervous System.

Background intelligence that watches and pushes alerts without being asked.

Features:
  1. Thesis Watchdog       — detects when market data conflicts with thinking chain premises
  2. Auto-Connector        — suggests links between new objects and existing research
  3. Question Resolver     — matches open questions with new filings/data
  4. Thinking Decay        — nudges when chains have new relevant data but haven't been touched
  5. Cascade Alerts        — connects AI proposals to thinking chain reasoning
  6. Morning Brief Builder — builds overnight intelligence digest
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from typing import Any

from app.services.postgres_core_service import pg_connect, pg_enabled

LOGGER = logging.getLogger(__name__)

# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS autonomic_alerts_core (
    id          BIGSERIAL PRIMARY KEY,
    alert_type  TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    severity    TEXT NOT NULL DEFAULT 'info',
    obj_type    TEXT NOT NULL DEFAULT '',
    obj_id      TEXT NOT NULL DEFAULT '',
    ticker      TEXT NOT NULL DEFAULT '',
    dedupe_key  TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'unread',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_aa_status ON autonomic_alerts_core(status);
CREATE INDEX IF NOT EXISTS idx_aa_type ON autonomic_alerts_core(alert_type);
CREATE INDEX IF NOT EXISTS idx_aa_created ON autonomic_alerts_core(created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_aa_dedupe ON autonomic_alerts_core(dedupe_key)
    WHERE dedupe_key != '';
"""


def ensure_autonomic_schema() -> None:
    if not pg_enabled():
        return
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        for stmt in _DDL.strip().split(";"):
            s = stmt.strip()
            if s:
                cur.execute(s)
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row_to_dict(cur_description, row) -> dict:
    cols = [d[0] for d in cur_description]
    d: dict = {}
    for k, v in zip(cols, row):
        if isinstance(v, (dt.date, dt.datetime)):
            d[k] = v.isoformat()
        else:
            d[k] = v
    return d


def _dedupe(alert_type: str, *parts: str) -> str:
    raw = f"{alert_type}|{'|'.join(str(p) for p in parts)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _create_alert(
    alert_type: str,
    title: str,
    body: str = "",
    severity: str = "info",
    obj_type: str = "",
    obj_id: str = "",
    ticker: str = "",
    dedupe_key: str = "",
) -> int:
    """Insert an alert. Returns alert id, 0 on duplicate or failure."""
    if not pg_enabled():
        return 0
    con = pg_connect()
    if con is None:
        return 0
    try:
        cur = con.cursor()
        cur.execute(
            """INSERT INTO autonomic_alerts_core
               (alert_type, title, body, severity, obj_type, obj_id, ticker, dedupe_key)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT DO NOTHING
               RETURNING id""",
            (alert_type, title[:500], body[:2000], severity, obj_type, obj_id,
             ticker.upper(), dedupe_key),
        )
        row = cur.fetchone()
        con.commit()
        return int(row[0]) if row else 0
    except Exception as exc:
        LOGGER.debug("_create_alert failed: %s", exc)
        try:
            con.rollback()
        except Exception:
            pass
        return 0
    finally:
        con.close()


def list_alerts(
    status: str = "unread",
    limit: int = 30,
    alert_type: str = "",
) -> list[dict]:
    """Fetch alerts, newest first."""
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        if alert_type:
            cur.execute(
                "SELECT * FROM autonomic_alerts_core WHERE status=%s AND alert_type=%s "
                "ORDER BY created_at DESC LIMIT %s",
                (status, alert_type, limit),
            )
        else:
            cur.execute(
                "SELECT * FROM autonomic_alerts_core WHERE status=%s "
                "ORDER BY created_at DESC LIMIT %s",
                (status, limit),
            )
        return [_row_to_dict(cur.description, r) for r in (cur.fetchall() or [])]
    except Exception:
        return []
    finally:
        con.close()


def dismiss_alert(alert_id: int) -> bool:
    if not pg_enabled():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(
            "UPDATE autonomic_alerts_core SET status='dismissed' WHERE id=%s",
            (alert_id,),
        )
        con.commit()
        return int(cur.rowcount or 0) > 0
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def dismiss_all_alerts() -> bool:
    if not pg_enabled():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("UPDATE autonomic_alerts_core SET status='dismissed' WHERE status='unread'")
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


def _safe_query(query: str, params: tuple = ()) -> list[dict]:
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(query, params)
        return [_row_to_dict(cur.description, r) for r in (cur.fetchall() or [])]
    except Exception as exc:
        # Keep runtime resilient, but surface query failures for diagnosis.
        LOGGER.warning(
            "autonomic query failed: %s | sql=%s",
            str(exc),
            " ".join(str(query).strip().split())[:220],
        )
        try:
            con.rollback()
        except Exception:
            pass
        return []
    finally:
        con.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 1: THESIS WATCHDOG
# ═══════════════════════════════════════════════════════════════════════════════

# Patterns that indicate a price/value premise in thinking chain content
_PRICE_PATTERNS = [
    # "oil above $90", "oil stays above 90", "BTC at 50000"
    re.compile(r"(?:above|over|at|hits?|reaches?|stays?\s+above)\s+\$?([\d,]+(?:\.\d+)?)", re.I),
    # "oil below $60", "drops below 60"
    re.compile(r"(?:below|under|drops?\s+(?:below|to)|falls?\s+(?:below|to))\s+\$?([\d,]+(?:\.\d+)?)", re.I),
]

# Map common commodity/asset words to yfinance tickers
_ASSET_KEYWORDS = {
    "oil": "CL=F", "crude": "CL=F", "wti": "CL=F", "brent": "BZ=F",
    "gold": "GC=F", "silver": "SI=F", "copper": "HG=F",
    "bitcoin": "BTC-USD", "btc": "BTC-USD", "ethereum": "ETH-USD", "eth": "ETH-USD",
    "vix": "^VIX", "volatility": "^VIX",
    "10-year": "^TNX", "10 year": "^TNX", "treasury": "^TNX",
    "s&p": "^GSPC", "sp500": "^GSPC", "s&p 500": "^GSPC",
    "nasdaq": "^IXIC", "dow": "^DJI",
    "dollar": "DX-Y.NYB", "dxy": "DX-Y.NYB",
    "natural gas": "NG=F", "nat gas": "NG=F",
}


def _extract_premises(chain: dict) -> list[dict]:
    """Extract price premises from a chain's nodes and notes."""
    premises = []
    text_sources = []
    if chain.get("notes"):
        text_sources.append(("notes", chain["notes"]))
    for node in (chain.get("nodes") or []):
        if node.get("content"):
            text_sources.append(("node", node["content"]))
        for child in (node.get("children") or []):
            if child.get("content"):
                text_sources.append(("node", child["content"]))

    for source_type, text in text_sources:
        text_lower = text.lower()
        # Find asset references
        asset_ticker = ""
        asset_name = ""
        for keyword, yticker in _ASSET_KEYWORDS.items():
            if keyword in text_lower:
                asset_ticker = yticker
                asset_name = keyword
                break

        if not asset_ticker:
            # Check for stock tickers like $AAPL
            ticker_match = re.search(r'\$([A-Z]{1,5})\b', text)
            if ticker_match:
                asset_ticker = ticker_match.group(1)
                asset_name = asset_ticker

        if not asset_ticker:
            continue

        # Find price conditions
        for pat in _PRICE_PATTERNS:
            m = pat.search(text)
            if m:
                try:
                    price_val = float(m.group(1).replace(",", ""))
                except (ValueError, IndexError):
                    continue
                direction = "above" if any(w in pat.pattern for w in ["above", "over", "hits", "reaches", "stays"]) else "below"
                premises.append({
                    "asset": asset_name,
                    "ticker": asset_ticker,
                    "direction": direction,
                    "price": price_val,
                    "text": text[:200],
                    "chain_id": chain.get("id"),
                    "chain_title": chain.get("title", ""),
                })
    return premises


def run_thesis_watchdog() -> int:
    """Check thinking chain premises against live market data. Returns alert count."""
    alerts_created = 0
    try:
        from app.services.web_search_service import get_live_macro_snapshot
        snapshot = get_live_macro_snapshot(timeout_sec=3.0)
        if not snapshot:
            return 0

        # Build price lookup from snapshot
        price_map: dict[str, float] = {}
        for name, data in snapshot.items():
            sym = data.get("symbol", "")
            price = data.get("price")
            if sym and price:
                price_map[sym] = float(price)

        # Get all active thinking chains
        from app.services.thinking_service import list_chains, get_chain
        chains = list_chains(status="active")
        for chain_summary in chains:
            chain = get_chain(chain_summary["id"])
            if not chain:
                continue

            premises = _extract_premises(chain)
            for p in premises:
                ticker = p["ticker"]
                # Try to get current price
                current = price_map.get(ticker)
                if current is None:
                    # Try fetching individually
                    try:
                        import yfinance as yf
                        t = yf.Ticker(ticker)
                        fi = t.fast_info
                        current = fi.get("lastPrice") or fi.get("last_price")
                    except Exception:
                        continue
                if current is None:
                    continue

                # Check if premise is violated
                violated = False
                if p["direction"] == "above" and current < p["price"] * 0.95:
                    violated = True
                elif p["direction"] == "below" and current > p["price"] * 1.05:
                    violated = True

                if violated:
                    title = f"Premise may be invalidated — {p['asset']} is ${current:.2f}"
                    body = (
                        f"Your thinking chain \"{p['chain_title']}\" (Chain #{p['chain_id']}) "
                        f"assumes {p['asset']} stays {p['direction']} ${p['price']:.0f}, "
                        f"but it's currently ${current:.2f}."
                    )
                    dk = _dedupe("thesis_watchdog", str(p["chain_id"]), p["ticker"],
                                 p["direction"], str(int(p["price"])),
                                 dt.date.today().isoformat())
                    aid = _create_alert(
                        alert_type="thesis_watchdog",
                        title=title,
                        body=body,
                        severity="warning",
                        obj_type="chain",
                        obj_id=str(p["chain_id"]),
                        ticker=p["ticker"],
                        dedupe_key=dk,
                    )
                    if aid:
                        alerts_created += 1
    except Exception as exc:
        LOGGER.debug("thesis_watchdog error: %s", exc)
    return alerts_created


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 2: AUTO-CONNECTOR
# ═══════════════════════════════════════════════════════════════════════════════

def run_auto_connector() -> int:
    """Scan recent chains/threads and suggest cross-links. Returns alert count."""
    alerts_created = 0
    try:
        from app.services.thinking_service import list_chains
        from app.services.research_thread_service import list_threads
        from app.services.work_graph_service import get_links

        chains = list_chains(status="active")
        threads = list_threads(status="active")

        if not chains or not threads:
            return 0

        # For each chain, check if any thread tags/tickers overlap
        for chain in chains:
            chain_tags = set(
                t.strip().upper()
                for t in (chain.get("tags") or "").split(",")
                if t.strip()
            )
            chain_title_words = set(
                w.upper() for w in re.findall(r'[A-Za-z]{3,}', chain.get("title") or "")
            )
            chain_notes_words = set(
                w.upper() for w in re.findall(r'[A-Za-z]{3,}', chain.get("notes") or "")
            )
            chain_words = chain_tags | chain_title_words | chain_notes_words

            # Check existing links for this chain
            existing_links = get_links("chain", str(chain["id"]))
            linked_thread_ids = {
                lk.get("other_id") for lk in existing_links
                if lk.get("other_type") == "thread"
            }

            for thread in threads:
                if str(thread.get("id")) in linked_thread_ids:
                    continue  # Already linked

                thread_ticker = (thread.get("ticker") or "").upper()
                thread_title_words = set(
                    w.upper() for w in re.findall(r'[A-Za-z]{3,}', thread.get("title") or "")
                )
                thread_thesis_words = set(
                    w.upper() for w in re.findall(r'[A-Za-z]{3,}', thread.get("thesis") or "")
                )
                thread_words = thread_title_words | thread_thesis_words
                if thread_ticker:
                    thread_words.add(thread_ticker)

                # Find overlap
                overlap = chain_words & thread_words
                # Filter out very common words
                common = {"THE", "AND", "FOR", "WITH", "THIS", "THAT", "FROM", "WILL",
                          "NOT", "ARE", "HAS", "HAVE", "BEEN", "BUT", "CAN", "ALL",
                          "MAY", "ABOUT", "ALSO", "COULD", "WOULD", "SHOULD", "INTO",
                          "MORE", "THAN", "WHEN", "HOW", "WHY", "WHAT", "NEW", "ONE"}
                meaningful = overlap - common
                if len(meaningful) >= 2 or (thread_ticker and thread_ticker in chain_tags):
                    match_words = ", ".join(sorted(meaningful)[:5])
                    title = f"Chain \"{chain.get('title','')}\" may relate to Thread \"{thread.get('title','')}\""
                    body = f"Shared concepts: {match_words}. Consider linking them."
                    dk = _dedupe("auto_connector", str(chain["id"]), str(thread["id"]))
                    aid = _create_alert(
                        alert_type="auto_connector",
                        title=title[:500],
                        body=body,
                        severity="info",
                        obj_type="chain",
                        obj_id=str(chain["id"]),
                        ticker=thread_ticker,
                        dedupe_key=dk,
                    )
                    if aid:
                        alerts_created += 1
    except Exception as exc:
        LOGGER.debug("auto_connector error: %s", exc)
    return alerts_created


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 3: QUESTION RESOLVER
# ═══════════════════════════════════════════════════════════════════════════════

def run_question_resolver() -> int:
    """Match open research questions with recent filings/events. Returns alert count."""
    alerts_created = 0
    try:
        # Get open questions from thread entries
        questions = _safe_query(
            "SELECT te.id, te.content, te.thread_id, rt.ticker, rt.title AS thread_title "
            "FROM thread_entries_core te "
            "JOIN research_threads_core rt ON rt.id = te.thread_id "
            "WHERE te.kind = 'question' AND te.status = 'open' AND rt.status = 'active' "
            "ORDER BY te.created_at DESC LIMIT 50"
        )

        if not questions:
            return 0

        # Get recent filings (last 7 days)
        recent_filings = _safe_query(
            "SELECT ticker, form, accession, filing_date "
            "FROM sec_edgar_poll_state_core "
            "WHERE COALESCE(NULLIF(processed_at,''), '1970-01-01T00:00:00+00:00')::timestamptz > NOW() - INTERVAL '7 days' "
            "ORDER BY processed_at DESC LIMIT 100"
        )

        # Get recent events
        recent_events = _safe_query(
            "SELECT ticker, event_type, payload_json, occurred_at "
            "FROM events_core "
            "WHERE COALESCE(NULLIF(occurred_at,''), '1970-01-01T00:00:00+00:00')::timestamptz > NOW() - INTERVAL '7 days' "
            "ORDER BY occurred_at DESC LIMIT 100"
        )

        for q in questions:
            q_ticker = (q.get("ticker") or "").upper()
            q_content = (q.get("content") or "").lower()
            if not q_content or len(q_content) < 10:
                continue

            # Extract key phrases from question (3+ char words)
            q_words = set(
                w for w in re.findall(r'[a-z]{3,}', q_content)
                if w not in {"the", "and", "for", "with", "this", "that", "from",
                            "will", "not", "are", "has", "have", "been", "what",
                            "how", "why", "when", "does", "can", "any"}
            )

            # Check events for semantic overlap first.
            for ev in recent_events:
                ev_ticker = (ev.get("ticker") or "").upper()
                if q_ticker and ev_ticker != q_ticker:
                    continue
                payload = ev.get("payload_json")
                ev_text = str(payload or "").lower()
                matched = q_words & set(re.findall(r'[a-z]{3,}', ev_text))
                if len(matched) >= 3:
                    title = f"Open question in \"{q.get('thread_title','')}\" may be answered"
                    body = (
                        f"Your question: \"{q.get('content','')[:150]}\"\n"
                        f"Recent event for ${ev_ticker or q_ticker}: {ev.get('event_type','event')} "
                        f"mentions related concepts: {', '.join(sorted(matched)[:5])}."
                    )
                    dk = _dedupe(
                        "question_resolver_event",
                        str(q.get("id")),
                        str(ev.get("occurred_at", "")),
                        str(ev.get("event_type", "")),
                    )
                    aid = _create_alert(
                        alert_type="question_resolver",
                        title=title,
                        body=body,
                        severity="info",
                        obj_type="entry",
                        obj_id=str(q.get("id")),
                        ticker=ev_ticker or q_ticker,
                        dedupe_key=dk,
                    )
                    if aid:
                        alerts_created += 1
                    break

            # Check filings for ticker-level relevance.
            for f in recent_filings:
                f_ticker = (f.get("ticker") or "").upper()
                if q_ticker and f_ticker != q_ticker:
                    continue
                if q_ticker and f_ticker == q_ticker:
                    title = f"Open question in \"{q.get('thread_title','')}\" may be answered"
                    body = (
                        f"Your question: \"{q.get('content','')[:150]}\"\n"
                        f"New {f.get('form','')} filing for ${f_ticker} "
                        f"(filed {f.get('filing_date','')}) is available for review."
                    )
                    dk = _dedupe("question_resolver", str(q.get("id")),
                                 f.get("accession", ""))
                    aid = _create_alert(
                        alert_type="question_resolver",
                        title=title,
                        body=body,
                        severity="info",
                        obj_type="entry",
                        obj_id=str(q.get("id")),
                        ticker=f_ticker,
                        dedupe_key=dk,
                    )
                    if aid:
                        alerts_created += 1
                    break  # One match per question is enough

    except Exception as exc:
        LOGGER.debug("question_resolver error: %s", exc)
    return alerts_created


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 4: THINKING DECAY DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

_DECAY_DAYS = 30  # chains untouched for 30+ days


def run_thinking_decay() -> int:
    """Detect chains that have gone stale while new data arrived. Returns alert count."""
    alerts_created = 0
    try:
        # Find chains not updated in DECAY_DAYS
        stale_chains = _safe_query(
            "SELECT id, title, tags, updated_at, chain_type "
            "FROM thinking_chains_core "
            "WHERE status = 'active' AND updated_at < NOW() - INTERVAL '%s days' "
            "ORDER BY updated_at ASC LIMIT 30",
            (_DECAY_DAYS,),
        )

        if not stale_chains:
            return 0

        for chain in stale_chains:
            chain_tags = [
                t.strip().upper()
                for t in (chain.get("tags") or "").split(",")
                if t.strip()
            ]
            chain_updated = chain.get("updated_at", "")

            # Check for tickers in tags
            tickers_in_chain = [
                t for t in chain_tags
                if re.match(r'^[A-Z]{1,5}$', t)
            ]

            new_data_count = 0
            data_sources = []

            for tk in tickers_in_chain:
                # Count new filings since chain was last updated
                filings = _safe_query(
                    "SELECT COUNT(*) as cnt FROM sec_edgar_poll_state_core "
                    "WHERE UPPER(ticker) = %s AND processed_at > %s",
                    (tk, chain_updated),
                )
                fc = int(filings[0].get("cnt", 0)) if filings else 0
                if fc:
                    new_data_count += fc
                    data_sources.append(f"{fc} filing{'s' if fc>1 else ''} for ${tk}")

                # Count new earnings
                earnings = _safe_query(
                    "SELECT COUNT(*) as cnt FROM earnings_analysis_core "
                    "WHERE UPPER(ticker) = %s "
                    "AND COALESCE(NULLIF(analyzed_at,''), '1970-01-01T00:00:00+00:00')::timestamptz > %s::timestamptz",
                    (tk, chain_updated),
                )
                ec = int(earnings[0].get("cnt", 0)) if earnings else 0
                if ec:
                    new_data_count += ec
                    data_sources.append(f"{ec} earnings report{'s' if ec>1 else ''} for ${tk}")

                # Count new proposals
                proposals = _safe_query(
                    "SELECT COUNT(*) as cnt FROM action_proposals_core "
                    "WHERE UPPER(ticker) = %s AND created_at > %s",
                    (tk, chain_updated),
                )
                pc = int(proposals[0].get("cnt", 0)) if proposals else 0
                if pc:
                    new_data_count += pc
                    data_sources.append(f"{pc} AI proposal{'s' if pc>1 else ''} for ${tk}")

            if new_data_count >= 2:
                days_stale = (dt.datetime.now(dt.timezone.utc) -
                             dt.datetime.fromisoformat(chain_updated.replace('Z', '+00:00'))).days \
                    if chain_updated else _DECAY_DAYS

                title = f"Chain \"{chain.get('title','')}\" has {new_data_count} new data points"
                body = (
                    f"Your thinking chain hasn't been updated in {days_stale} days, "
                    f"but there's new data: {'; '.join(data_sources)}. "
                    f"Consider revisiting your reasoning."
                )
                dk = _dedupe("thinking_decay", str(chain["id"]),
                             dt.date.today().isoformat())
                aid = _create_alert(
                    alert_type="thinking_decay",
                    title=title,
                    body=body,
                    severity="info",
                    obj_type="chain",
                    obj_id=str(chain["id"]),
                    dedupe_key=dk,
                )
                if aid:
                    alerts_created += 1

            elif not tickers_in_chain and (dt.datetime.now(dt.timezone.utc) -
                    dt.datetime.fromisoformat(
                        chain_updated.replace('Z', '+00:00'))).days >= 60 if chain_updated else True:
                # Chain with no ticker tags and 60+ days stale
                title = f"Chain \"{chain.get('title','')}\" is going stale"
                body = f"This {chain.get('chain_type','chain')} hasn't been touched in a while. Archive it or add new thinking."
                dk = _dedupe("thinking_decay_generic", str(chain["id"]),
                             dt.date.today().isocalendar()[1])  # weekly dedupe
                aid = _create_alert(
                    alert_type="thinking_decay",
                    title=title,
                    body=body,
                    severity="low",
                    obj_type="chain",
                    obj_id=str(chain["id"]),
                    dedupe_key=dk,
                )
                if aid:
                    alerts_created += 1

    except Exception as exc:
        LOGGER.debug("thinking_decay error: %s", exc)
    return alerts_created


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 5: SECOND-ORDER CASCADE ALERTS
# ═══════════════════════════════════════════════════════════════════════════════

def run_cascade_alerts() -> int:
    """Connect recent AI proposals to thinking chain reasoning. Returns alert count."""
    alerts_created = 0
    try:
        # Get proposals from last 24h
        recent_proposals = _safe_query(
            "SELECT id, ticker, kind, title, reasoning_json, confidence, created_at "
            "FROM action_proposals_core "
            "WHERE COALESCE(NULLIF(created_at,''), '1970-01-01T00:00:00+00:00')::timestamptz > NOW() - INTERVAL '24 hours' "
            "ORDER BY created_at DESC LIMIT 30"
        )

        if not recent_proposals:
            return 0

        # Get all active chains with their tags
        from app.services.thinking_service import list_chains
        chains = list_chains(status="active")

        for proposal in recent_proposals:
            p_ticker = (proposal.get("ticker") or "").upper()
            raw_reasoning = proposal.get("reasoning_json")
            if isinstance(raw_reasoning, dict):
                p_reasoning = str(raw_reasoning.get("summary") or raw_reasoning.get("actionable_proposal") or raw_reasoning).lower()
            else:
                p_reasoning = str(raw_reasoning or "").lower()
            p_headline = proposal.get("title") or proposal.get("kind") or "Proposal"

            for chain in chains:
                chain_tags = [
                    t.strip().upper()
                    for t in (chain.get("tags") or "").split(",")
                    if t.strip()
                ]
                chain_title = (chain.get("title") or "").lower()
                chain_notes = (chain.get("notes") or "").lower()
                chain_text = chain_title + " " + chain_notes

                # Direct ticker match
                if p_ticker and p_ticker in chain_tags:
                    title = f"AI signal on ${p_ticker} aligns with your Chain \"{chain.get('title','')}\""
                    body = (
                        f"New proposal: {p_headline}\n"
                        f"Reasoning: {str(raw_reasoning or '')[:200]}\n"
                        f"Your chain tags include ${p_ticker} — check if this confirms or challenges your thesis."
                    )
                    dk = _dedupe("cascade_alert", str(proposal["id"]),
                                 str(chain["id"]))
                    aid = _create_alert(
                        alert_type="cascade_alert",
                        title=title,
                        body=body,
                        severity="info",
                        obj_type="proposal",
                        obj_id=str(proposal["id"]),
                        ticker=p_ticker,
                        dedupe_key=dk,
                    )
                    if aid:
                        alerts_created += 1
                    continue

                # Concept overlap (check if proposal reasoning mentions chain concepts)
                chain_key_words = set(
                    w for w in re.findall(r'[a-z]{4,}', chain_text)
                    if w not in {"this", "that", "with", "from", "will", "have",
                                "been", "about", "could", "would", "should", "their",
                                "than", "more", "when", "also", "into"}
                )
                p_words = set(re.findall(r'[a-z]{4,}', p_reasoning))
                overlap = chain_key_words & p_words
                if len(overlap) >= 4:
                    title = f"AI signal on ${p_ticker} may connect to Chain \"{chain.get('title','')}\""
                    body = (
                        f"Proposal: {p_headline}\n"
                        f"Shared concepts with your thinking: {', '.join(sorted(overlap)[:6])}."
                    )
                    dk = _dedupe("cascade_concept", str(proposal["id"]),
                                 str(chain["id"]))
                    aid = _create_alert(
                        alert_type="cascade_alert",
                        title=title,
                        body=body,
                        severity="low",
                        obj_type="proposal",
                        obj_id=str(proposal["id"]),
                        ticker=p_ticker,
                        dedupe_key=dk,
                    )
                    if aid:
                        alerts_created += 1

    except Exception as exc:
        LOGGER.debug("cascade_alerts error: %s", exc)
    return alerts_created


# ═══════════════════════════════════════════════════════════════════════════════
# Feature 6: MORNING INTELLIGENCE BRIEF
# ═══════════════════════════════════════════════════════════════════════════════

def build_morning_brief() -> dict:
    """Build overnight intelligence digest. Returns summary dict for Today page."""
    brief: dict = {
        "overnight_alerts": 0,
        "thesis_conflicts": 0,
        "new_connections": 0,
        "questions_answered": 0,
        "decaying_chains": 0,
        "cascade_hits": 0,
        "headline": "",
        "items": [],
    }
    try:
        # Count unread alerts by type
        counts = _safe_query(
            "SELECT alert_type, COUNT(*) as cnt FROM autonomic_alerts_core "
            "WHERE status = 'unread' GROUP BY alert_type"
        )
        for c in counts:
            at = c.get("alert_type", "")
            cnt = int(c.get("cnt", 0))
            brief["overnight_alerts"] += cnt
            if at == "thesis_watchdog":
                brief["thesis_conflicts"] = cnt
            elif at == "auto_connector":
                brief["new_connections"] = cnt
            elif at == "question_resolver":
                brief["questions_answered"] = cnt
            elif at == "thinking_decay":
                brief["decaying_chains"] = cnt
            elif at == "cascade_alert":
                brief["cascade_hits"] = cnt

        # Build headline
        parts = []
        if brief["thesis_conflicts"]:
            parts.append(f"{brief['thesis_conflicts']} thesis conflict{'s' if brief['thesis_conflicts']>1 else ''}")
        if brief["questions_answered"]:
            parts.append(f"{brief['questions_answered']} question{'s' if brief['questions_answered']>1 else ''} may have answers")
        if brief["cascade_hits"]:
            parts.append(f"{brief['cascade_hits']} signal{'s' if brief['cascade_hits']>1 else ''} connect to your thinking")
        if brief["decaying_chains"]:
            parts.append(f"{brief['decaying_chains']} chain{'s' if brief['decaying_chains']>1 else ''} need attention")
        if brief["new_connections"]:
            parts.append(f"{brief['new_connections']} suggested connection{'s' if brief['new_connections']>1 else ''}")

        if parts:
            brief["headline"] = "; ".join(parts) + "."
        else:
            brief["headline"] = "No overnight intelligence alerts."

        # Get top alerts for display
        brief["items"] = list_alerts(status="unread", limit=10)

    except Exception as exc:
        LOGGER.debug("morning_brief error: %s", exc)
    return brief


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN SWEEP — runs all features in sequence
# ═══════════════════════════════════════════════════════════════════════════════

def run_autonomic_sweep() -> dict:
    """Run all autonomic features. Called by background thread."""
    results = {
        "thesis_watchdog": 0,
        "auto_connector": 0,
        "question_resolver": 0,
        "thinking_decay": 0,
        "cascade_alerts": 0,
        "total": 0,
    }
    try:
        results["thesis_watchdog"] = run_thesis_watchdog()
    except Exception as exc:
        LOGGER.warning("thesis_watchdog sweep failed: %s", exc)

    try:
        results["auto_connector"] = run_auto_connector()
    except Exception as exc:
        LOGGER.warning("auto_connector sweep failed: %s", exc)

    try:
        results["question_resolver"] = run_question_resolver()
    except Exception as exc:
        LOGGER.warning("question_resolver sweep failed: %s", exc)

    try:
        results["thinking_decay"] = run_thinking_decay()
    except Exception as exc:
        LOGGER.warning("thinking_decay sweep failed: %s", exc)

    try:
        results["cascade_alerts"] = run_cascade_alerts()
    except Exception as exc:
        LOGGER.warning("cascade_alerts sweep failed: %s", exc)

    results["total"] = sum(v for v in results.values())
    LOGGER.info("Autonomic sweep complete: %s", results)
    return results
