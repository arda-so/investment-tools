from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections import Counter, defaultdict
from typing import Any

from app.core.db import core_conn as _conn, sqlite_retry
from app.core.normalize import normalize_text as _norm
from app.services.portfolio_memory_service import get_holdings, query_report_facts
from app.services.postgres_core_service import core_backend, pg_connect

try:
    from tools.llm_engine import ask_ai
except Exception:
    ask_ai = None  # type: ignore[assignment]


def ensure_ai_insight_schema() -> None:
    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is None:
            return
        try:
            cur = con_pg.cursor()
            cur.execute(
                """CREATE TABLE IF NOT EXISTS ai_meta_suggestions_core (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL,
                    category TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    detail_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                    priority DOUBLE PRECISION NOT NULL DEFAULT 0.0,
                    source TEXT NOT NULL DEFAULT 'heuristic',
                    status TEXT NOT NULL DEFAULT 'open'
                )"""
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_meta_suggestions_core_created ON ai_meta_suggestions_core(created_at DESC)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ai_meta_suggestions_core_status ON ai_meta_suggestions_core(status, priority DESC)")
            con_pg.commit()
            return
        except Exception:
            try:
                con_pg.rollback()
            except Exception:
                pass
            return
        finally:
            con_pg.close()

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


def _recent_quality_rows(days: int = 30) -> list[dict[str, Any]]:
    ensure_ai_insight_schema()
    since = (dt.datetime.now() - dt.timedelta(days=max(1, int(days or 30)))).isoformat()

    if core_backend() == "postgres":
        try:
            con_pg = pg_connect()
            if con_pg is not None:
                try:
                    cur = con_pg.cursor()
                    cur.execute(
                        """SELECT created_at, event_type, query, detail_json
                           FROM ai_quality_log_core
                           WHERE created_at >= %s
                           ORDER BY id DESC
                           LIMIT 6000""",
                        (since,),
                    )
                    cols = [d[0] for d in cur.description] if cur.description else []
                    return [dict(zip(cols, r)) for r in cur.fetchall()]
                finally:
                    con_pg.close()
        except Exception:
            pass
        return []

    con = _conn()
    try:
        rows = con.execute(
            """SELECT created_at, event_type, query, detail_json
               FROM ai_quality_log
               WHERE created_at >= ?
               ORDER BY id DESC
               LIMIT 6000""",
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _build_missing_tool_suggestions(rows: list[dict[str, Any]], min_count: int = 4) -> list[dict[str, Any]]:
    """LLM-driven: analyze blocked/gated queries to identify missing capabilities."""
    gated_events = {"action_confidence_gate", "needs_clarification", "risk_veto_review", "risk_veto_blocked"}
    q_counts: Counter[str] = Counter()
    for r in rows:
        ev = str(r["event_type"] or "").strip().lower()
        if ev not in gated_events:
            continue
        qn = _norm(str(r["query"] or ""))
        if qn:
            q_counts[qn] += 1

    # Collect queries that hit gates repeatedly
    frequent = [(qn, c) for qn, c in q_counts.most_common(30) if c >= int(min_count or 4)]
    if not frequent or ask_ai is None:
        return []

    formatted = "\n".join(f"- \"{qn}\" (blocked {c} times)" for qn, c in frequent[:20])
    prompt = (
        "You are an AI product intelligence engine for an investment assistant app.\n"
        "Analyze these blocked/gated user queries — these are things users tried to do but the system couldn't handle.\n\n"
        f"Blocked queries:\n{formatted}\n\n"
        "Identify what tools, features, or capabilities are missing.\n"
        "Think about: what was the user trying to accomplish? What tool would solve it?\n\n"
        "Return strict JSON only:\n"
        '{"suggestions": [{"tool_name": "snake_case_name", "title": "Missing Tool: ...", '
        '"summary": "What it should do and why users need it", "confidence": 0.0, "blocked_count": 0}]}\n\n'
        "Rules:\n"
        "- Only suggest for clear patterns (not one-off queries)\n"
        "- tool_name: plausible function name (snake_case)\n"
        "- confidence: 0.0-1.0 based on pattern clarity and user need\n"
        "- blocked_count: total times this capability was requested\n"
        "- Group similar queries into one suggestion\n"
        "- Maximum 8 suggestions, ordered by importance"
    )
    try:
        raw = str(ask_ai(prompt, "AI product intelligence. JSON only.", mode="fast", json_mode=True, temperature=0.0) or "").strip()
        if not raw:
            return []
        parsed = json.loads(raw)
        suggestions = parsed.get("suggestions") or []
        out: list[dict[str, Any]] = []
        for s in suggestions[:8]:
            title = str(s.get("title") or "").strip()
            summary = str(s.get("summary") or "").strip()
            tool_name = str(s.get("tool_name") or "").strip()
            conf = float(s.get("confidence") or 0.0)
            bc = int(s.get("blocked_count") or 0)
            if not title or not summary or conf < 0.5:
                continue
            out.append({
                "category": "missing_tool",
                "title": title[:200],
                "summary": summary[:500],
                "priority": min(99.0, 60.0 + (conf * 30.0)),
                "detail": {"suggested_tool": tool_name, "blocked_count": bc, "confidence": conf},
                "source": "llm_intent_gap_analysis",
            })
        return out
    except Exception:
        return []


def _build_behavioral_friction_suggestions(rows: list[dict[str, Any]], min_repeats: int = 3) -> list[dict[str, Any]]:
    """LLM-driven: analyze repeated friction events to identify UX improvement opportunities."""
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

    frequent = [(qn, c, dict(q_event_counts[qn])) for qn, c in q_counts.most_common(30) if c >= int(min_repeats or 3)]
    if not frequent or ask_ai is None:
        return []

    formatted = "\n".join(
        f"- \"{qn}\" (hit friction {c} times — breakdown: {evts})"
        for qn, c, evts in frequent[:20]
    )
    prompt = (
        "You are an AI UX intelligence engine for an investment assistant app.\n"
        "Analyze these repeated friction events — these are actions where users repeatedly hit gates, "
        "clarification requests, or review steps.\n\n"
        f"Friction events:\n{formatted}\n\n"
        "Identify patterns where the UX is causing unnecessary friction and suggest improvements.\n"
        "Think about: Why does this keep happening? What would eliminate the friction?\n\n"
        "Return strict JSON only:\n"
        '{"suggestions": [{"title": "High Friction: ...", '
        '"summary": "What causes friction and the recommended fix", '
        '"suggested_fix": "snake_case_fix_name", "confidence": 0.0, "affected_queries": 0}]}\n\n'
        "Rules:\n"
        "- Focus on patterns where users repeatedly hit the same wall\n"
        "- Suggest concrete fixes: dedicated widget, shortcut, auto-complete, better defaults, etc.\n"
        "- confidence: 0.0-1.0 based on pattern clarity\n"
        "- affected_queries: total friction events for this pattern\n"
        "- Group similar friction patterns together\n"
        "- Maximum 8 suggestions, ordered by severity"
    )
    try:
        raw = str(ask_ai(prompt, "AI UX intelligence. JSON only.", mode="fast", json_mode=True, temperature=0.0) or "").strip()
        if not raw:
            return []
        parsed = json.loads(raw)
        suggestions = parsed.get("suggestions") or []
        out: list[dict[str, Any]] = []
        for s in suggestions[:8]:
            title = str(s.get("title") or "").strip()
            summary = str(s.get("summary") or "").strip()
            fix = str(s.get("suggested_fix") or "").strip()
            conf = float(s.get("confidence") or 0.0)
            aq = int(s.get("affected_queries") or 0)
            if not title or not summary or conf < 0.5:
                continue
            out.append({
                "category": "behavioral_friction",
                "title": title[:200],
                "summary": summary[:500],
                "priority": min(98.0, 55.0 + (conf * 35.0)),
                "detail": {"suggested_fix": fix, "affected_queries": aq, "confidence": conf},
                "source": "llm_friction_tracker",
            })
        return out
    except Exception:
        return []


def _build_portfolio_blindspot_suggestions() -> list[dict[str, Any]]:
    """LLM-driven: reason over holdings + filing data to detect any portfolio blindspot."""
    holds = get_holdings(limit=1200)
    if not holds:
        return []

    # Build portfolio summary for the LLM
    inds: Counter[str] = Counter()
    tickers_by_ind: dict[str, list[str]] = defaultdict(list)
    total = 0.0
    for h in holds:
        ind = str(h.get("industry") or "Unknown").strip() or "Unknown"
        tk = str(h.get("ticker") or "").strip()
        sh = float(h.get("shares") or 0.0)
        if sh <= 0:
            continue
        inds[ind] += sh
        total += sh
        if tk:
            tickers_by_ind[ind].append(tk)
    if total <= 0:
        return []

    portfolio_lines = []
    for ind, sh in inds.most_common(15):
        pct = (sh / total) * 100.0
        tks = ", ".join(tickers_by_ind.get(ind, [])[:8])
        portfolio_lines.append(f"- {ind}: {pct:.1f}% ({tks})")
    portfolio_summary = "\n".join(portfolio_lines)

    # Get recent filing signals
    try:
        fact_rows = query_report_facts(query="", tickers=None, limit=100, official_only=True)
    except Exception:
        fact_rows = []
    filing_excerpts = "\n".join(
        f"- [{str((r or {}).get('ticker') or '?')}] {str((r or {}).get('fact_text') or '')[:200]}"
        for r in fact_rows[:30]
    ) or "(no recent filing data)"

    if ask_ai is None:
        return []

    prompt = (
        "You are an AI portfolio risk intelligence engine.\n"
        "Analyze this portfolio composition and recent SEC filing signals to identify blindspots, "
        "hidden risks, concentration dangers, cause-and-effect chains, and secondary effects the investor may be missing.\n\n"
        f"Portfolio composition:\n{portfolio_summary}\n\n"
        f"Recent SEC filing signals:\n{filing_excerpts}\n\n"
        "Think deeply:\n"
        "- Concentration risk: is there over-exposure to any sector/theme?\n"
        "- Correlated risk: do positions move together in a downturn?\n"
        "- Supply chain: are holdings exposed to the same supply chain?\n"
        "- Regulatory: pending regulation that affects multiple holdings?\n"
        "- Macro: interest rate, currency, or geopolitical exposure?\n"
        "- Missing hedges: what risks are completely unhedged?\n"
        "- Secondary effects: if X happens, what cascades through the portfolio?\n\n"
        "Return strict JSON only:\n"
        '{"suggestions": [{"title": "Blindspot: ...", '
        '"summary": "Clear explanation of the risk with cause-effect reasoning", '
        '"risk_type": "concentration|correlation|supply_chain|regulatory|macro|missing_hedge|secondary_effect", '
        '"affected_tickers": ["TK1", "TK2"], "confidence": 0.0, "severity": "low|medium|high|critical"}]}\n\n'
        "Rules:\n"
        "- Be specific — name the actual stocks and industries affected\n"
        "- Explain the cause-effect chain (if X then Y then Z)\n"
        "- confidence: 0.0-1.0\n"
        "- Maximum 8 suggestions, ordered by severity"
    )
    try:
        raw = str(ask_ai(prompt, "Portfolio risk intelligence. JSON only.", mode="smart", json_mode=True, temperature=0.1) or "").strip()
        if not raw:
            return []
        parsed = json.loads(raw)
        suggestions = parsed.get("suggestions") or []
        severity_map = {"critical": 95.0, "high": 80.0, "medium": 65.0, "low": 50.0}
        out: list[dict[str, Any]] = []
        for s in suggestions[:8]:
            title = str(s.get("title") or "").strip()
            summary = str(s.get("summary") or "").strip()
            conf = float(s.get("confidence") or 0.0)
            sev = str(s.get("severity") or "medium").strip().lower()
            risk_type = str(s.get("risk_type") or "").strip()
            tickers = s.get("affected_tickers") or []
            if not title or not summary or conf < 0.4:
                continue
            base_pri = severity_map.get(sev, 65.0)
            out.append({
                "category": "portfolio_blindspot",
                "title": title[:200],
                "summary": summary[:600],
                "priority": min(99.0, base_pri + (conf * 10.0)),
                "detail": {
                    "risk_type": risk_type,
                    "affected_tickers": tickers[:10] if isinstance(tickers, list) else [],
                    "confidence": conf,
                    "severity": sev,
                },
                "source": "llm_portfolio_gap_analysis",
            })
        return out
    except Exception:
        return []


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

    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is None:
            return {"ok": False, "created": 0, "items": [], "error": "pg_unavailable"}
        try:
            cur = con_pg.cursor()
            for s in sugg:
                title = str(s.get("title") or "").strip()
                category = str(s.get("category") or "").strip()
                summary = str(s.get("summary") or "").strip()
                if not title or not category or not summary:
                    continue
                cur.execute(
                    """SELECT 1 FROM ai_meta_suggestions_core
                       WHERE title=%s AND created_at>=NOW() - INTERVAL '7 days'
                       LIMIT 1""",
                    (title,),
                )
                if cur.fetchone():
                    continue
                cur.execute(
                    """INSERT INTO ai_meta_suggestions_core
                       (created_at, category, title, summary, detail_json, priority, source, status)
                       VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, 'open')""",
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
            con_pg.commit()
            return {"ok": True, "created": created, "items": sugg}
        except Exception as exc:
            try:
                con_pg.rollback()
            except Exception:
                pass
            return {"ok": False, "created": created, "items": sugg, "error": str(exc)}
        finally:
            con_pg.close()

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
    if core_backend() == "postgres":
        con_pg = pg_connect()
        if con_pg is None:
            return []
        try:
            cur = con_pg.cursor()
            cur.execute(
                """SELECT id, created_at, category, title, summary, detail_json::text, priority, source, status
                   FROM ai_meta_suggestions_core
                   WHERE status=%s
                   ORDER BY priority DESC, id DESC
                   LIMIT %s""",
                (st, max(1, min(300, int(limit or 30)))),
            )
            rows = cur.fetchall() or []
        finally:
            con_pg.close()
        out: list[dict[str, Any]] = []
        for r in rows:
            d: dict[str, Any] = {}
            try:
                d = json.loads(str(r[5] or "{}"))
            except Exception:
                d = {}
            out.append(
                {
                    "id": int(r[0] or 0),
                    "created_at": str(r[1] or ""),
                    "category": str(r[2] or ""),
                    "title": str(r[3] or ""),
                    "summary": str(r[4] or ""),
                    "detail": d if isinstance(d, dict) else {},
                    "priority": float(r[6] or 0.0),
                    "source": str(r[7] or ""),
                    "status": str(r[8] or ""),
                }
            )
        return out

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
