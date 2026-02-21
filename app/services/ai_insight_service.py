from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from collections import Counter, defaultdict
from typing import Any

from app.core.config import CORE_DB_PATH
from app.core.sqlite_hardening import connect_sqlite, sqlite_retry
from app.services.portfolio_memory_service import get_holdings, query_report_facts


def _conn() -> sqlite3.Connection:
    return connect_sqlite(str(CORE_DB_PATH), row_factory=True)


def ensure_ai_insight_schema() -> None:
    def _write() -> None:
        con = _conn()
        try:
            con.execute(
                """CREATE TABLE IF NOT EXISTS ai_meta_suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    priority REAL NOT NULL DEFAULT 0.0,
                    source TEXT NOT NULL DEFAULT 'heuristic',
                    status TEXT NOT NULL DEFAULT 'open'
                )"""
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_ai_meta_suggestions_created ON ai_meta_suggestions(created_at DESC)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_ai_meta_suggestions_status ON ai_meta_suggestions(status, priority DESC)")
            con.commit()
        finally:
            con.close()

    sqlite_retry(_write)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", str(text or "").lower())).strip()


def _recent_quality_rows(days: int = 30) -> list[sqlite3.Row]:
    ensure_ai_insight_schema()
    since = (dt.datetime.now() - dt.timedelta(days=max(1, int(days or 30)))).isoformat()
    con = _conn()
    try:
        return con.execute(
            """SELECT created_at, event_type, query, detail_json
               FROM ai_quality_log
               WHERE created_at >= ?
               ORDER BY id DESC
               LIMIT 6000""",
            (since,),
        ).fetchall()
    finally:
        con.close()


def _build_missing_tool_suggestions(rows: list[sqlite3.Row], min_count: int = 4) -> list[dict[str, Any]]:
    gated_events = {"action_confidence_gate", "needs_clarification", "risk_veto_review", "risk_veto_blocked"}
    q_counts: Counter[str] = Counter()
    for r in rows:
        ev = str(r["event_type"] or "").strip().lower()
        if ev not in gated_events:
            continue
        qn = _norm(str(r["query"] or ""))
        if qn:
            q_counts[qn] += 1

    out: list[dict[str, Any]] = []
    for qn, c in q_counts.most_common(80):
        if c < int(min_count or 4):
            continue
        title = ""
        summary = ""
        tool_name = ""
        if any(k in qn for k in {"option", "greek", "delta", "gamma", "theta", "vega"}):
            tool_name = "calculate_options_greeks"
            title = "Missing Tool: Options Greeks"
            summary = f"Detected {c} blocked intents around options/greeks. Add `{tool_name}` tool."
        elif "blue chip" in qn and any(k in qn for k in {"add", "remove", "rebalance"}):
            tool_name = "rebalance_blue_chips"
            title = "Missing Flow: Blue Chip Rebalancing"
            summary = f"Detected {c} blocked blue-chip management intents. Add `{tool_name}` action flow."
        elif any(k in qn for k in {"tax loss", "harvest"}):
            tool_name = "tax_loss_harvest"
            title = "Missing Tool: Tax-Loss Harvest"
            summary = f"Detected {c} blocked tax-harvest intents. Add `{tool_name}` tool."
        elif any(k in qn for k in {"supply chain exposure", "supplier risk"}):
            tool_name = "supply_chain_exposure_scan"
            title = "Missing Tool: Supply Chain Exposure"
            summary = f"Detected {c} blocked supply-chain exposure queries. Add `{tool_name}` tool."
        if not title:
            continue
        out.append(
            {
                "category": "missing_tool",
                "title": title,
                "summary": summary,
                "priority": min(99.0, 60.0 + (c * 2.5)),
                "detail": {"count": c, "example_query": qn, "suggested_tool": tool_name},
                "source": "intent_gap_analysis",
            }
        )
    return out[:10]


def _build_behavioral_friction_suggestions(rows: list[sqlite3.Row], min_repeats: int = 3) -> list[dict[str, Any]]:
    friction_events = {"needs_clarification", "action_confidence_gate", "risk_veto_review"}
    q_counts: Counter[str] = Counter()
    q_event_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for r in rows:
        qn = _norm(str(r["query"] or ""))
        ev = str(r["event_type"] or "").strip().lower()
        if not qn or ev not in friction_events:
            continue
        q_counts[qn] += 1
        q_event_counts[qn][ev] += 1

    out: list[dict[str, Any]] = []
    for qn, c in q_counts.most_common(60):
        if c < int(min_repeats or 3):
            continue
        if "blue chip" in qn:
            out.append(
                {
                    "category": "behavioral_friction",
                    "title": "High Friction: Blue Chips Flow",
                    "summary": f"`manage_blue_chips` style requests need ~{c} clarification/review turns. Build a dedicated widget.",
                    "priority": min(98.0, 55.0 + (c * 4.0)),
                    "detail": {"query": qn, "count": c, "events": dict(q_event_counts[qn]), "suggested_fix": "dedicated_blue_chip_widget"},
                    "source": "friction_tracker",
                }
            )
        elif any(k in qn for k in {"watchlist", "portfolio"}) and c >= 4:
            out.append(
                {
                    "category": "behavioral_friction",
                    "title": "High Friction: Repeated Portfolio/Watchlist Commands",
                    "summary": f"Detected repeated command retries ({c}) for similar request. Improve disambiguation or add one-click action chip.",
                    "priority": min(95.0, 50.0 + (c * 3.0)),
                    "detail": {"query": qn, "count": c, "events": dict(q_event_counts[qn]), "suggested_fix": "intent_shortcuts_widget"},
                    "source": "friction_tracker",
                }
            )
    return out[:10]


def _build_portfolio_blindspot_suggestions() -> list[dict[str, Any]]:
    holds = get_holdings(limit=1200)
    inds: Counter[str] = Counter()
    total = 0.0
    for h in holds:
        ind = str(h.get("industry") or "Unknown").strip() or "Unknown"
        sh = float(h.get("shares") or 0.0)
        if sh <= 0:
            continue
        inds[ind] += sh
        total += sh
    if total <= 0:
        return []
    top_ind, top_sh = inds.most_common(1)[0]
    conc_pct = (float(top_sh) / total) * 100.0

    try:
        rows = query_report_facts(query="", tickers=None, limit=100, official_only=True)
    except Exception:
        rows = []
    txt = " ".join(str((r or {}).get("fact_text") or "") for r in rows).lower()
    supply_hits = sum(1 for k in ["supply chain", "supplier", "logistics", "shortage"] if k in txt)
    reg_hits = sum(1 for k in ["regulatory", "antitrust", "compliance", "litigation"] if k in txt)

    out: list[dict[str, Any]] = []
    if conc_pct >= 50.0 and supply_hits >= 2:
        out.append(
            {
                "category": "portfolio_blindspot",
                "title": "Blindspot: Concentration vs Supply Chain Risk",
                "summary": f"Portfolio concentration is high in `{top_ind}` ({conc_pct:.1f}%). Recent filings show rising supply-chain signals. Add ontology node `SUPPLY_CHAIN_EXPOSURE`.",
                "priority": min(99.0, 70.0 + ((conc_pct - 50.0) * 0.8)),
                "detail": {"top_industry": top_ind, "concentration_pct": round(conc_pct, 2), "supply_chain_signal_hits": supply_hits},
                "source": "portfolio_gap_analysis",
            }
        )
    if conc_pct >= 55.0 and reg_hits >= 2:
        out.append(
            {
                "category": "portfolio_blindspot",
                "title": "Blindspot: Concentration vs Regulatory Risk",
                "summary": f"High portfolio concentration in `{top_ind}` ({conc_pct:.1f}%) while regulatory pressure signals are increasing.",
                "priority": min(98.0, 66.0 + ((conc_pct - 50.0) * 0.7)),
                "detail": {"top_industry": top_ind, "concentration_pct": round(conc_pct, 2), "regulatory_signal_hits": reg_hits},
                "source": "portfolio_gap_analysis",
            }
        )
    return out[:8]


def run_ai_meta_suggestions(days: int = 30) -> dict[str, Any]:
    ensure_ai_insight_schema()
    rows = _recent_quality_rows(days=days)
    sugg = []
    sugg.extend(_build_missing_tool_suggestions(rows, min_count=4))
    sugg.extend(_build_behavioral_friction_suggestions(rows, min_repeats=3))
    sugg.extend(_build_portfolio_blindspot_suggestions())
    if not sugg:
        return {"ok": True, "created": 0, "items": []}

    created = 0
    now = dt.datetime.now().isoformat()

    def _write() -> None:
        nonlocal created
        con = _conn()
        try:
            for s in sugg:
                title = str(s.get("title") or "").strip()
                category = str(s.get("category") or "").strip()
                summary = str(s.get("summary") or "").strip()
                if not title or not category or not summary:
                    continue
                # De-dup recent same title
                r = con.execute(
                    """SELECT 1 FROM ai_meta_suggestions
                       WHERE title=? AND created_at>=?
                       LIMIT 1""",
                    (title, (dt.datetime.now() - dt.timedelta(days=7)).isoformat()),
                ).fetchone()
                if r:
                    continue
                con.execute(
                    """INSERT INTO ai_meta_suggestions
                       (created_at, category, title, summary, detail_json, priority, source, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'open')""",
                    (
                        now,
                        category[:80],
                        title[:240],
                        summary[:1200],
                        json.dumps(dict(s.get("detail") or {}), ensure_ascii=True),
                        float(s.get("priority") or 0.0),
                        str(s.get("source") or "heuristic")[:80],
                    ),
                )
                created += 1
            con.commit()
        finally:
            con.close()

    sqlite_retry(_write)
    return {"ok": True, "created": created, "items": sugg}


def list_ai_meta_suggestions(limit: int = 30, status: str = "open") -> list[dict[str, Any]]:
    ensure_ai_insight_schema()
    st = str(status or "open").strip().lower()
    con = _conn()
    try:
        rows = con.execute(
            """SELECT id, created_at, category, title, summary, detail_json, priority, source, status
               FROM ai_meta_suggestions
               WHERE status=?
               ORDER BY priority DESC, id DESC
               LIMIT ?""",
            (st, max(1, min(300, int(limit or 30)))),
        ).fetchall()
    finally:
        con.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = {}
        try:
            d = json.loads(str(r["detail_json"] or "{}"))
        except Exception:
            d = {}
        out.append(
            {
                "id": int(r["id"] or 0),
                "created_at": str(r["created_at"] or ""),
                "category": str(r["category"] or ""),
                "title": str(r["title"] or ""),
                "summary": str(r["summary"] or ""),
                "detail": d if isinstance(d, dict) else {},
                "priority": float(r["priority"] or 0.0),
                "source": str(r["source"] or ""),
                "status": str(r["status"] or ""),
            }
        )
    return out
