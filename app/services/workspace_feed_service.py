from __future__ import annotations

import datetime as dt
import json
from typing import Any

from app.services.postgres_core_service import pg_connect


CHANNELS: list[dict[str, str]] = [
    {"id": "all", "label": "All"},
    {"id": "morning-brief", "label": "Morning Brief"},
    {"id": "alerts", "label": "Alerts"},
    {"id": "filings", "label": "Filings"},
    {"id": "portfolio", "label": "Portfolio"},
    {"id": "notes", "label": "Notes"},
    {"id": "agent-runs", "label": "Agent Runs"},
    {"id": "ai-agent", "label": "AI Agent"},
]


def list_workspace_channels() -> list[dict[str, str]]:
    return list(CHANNELS)


def ensure_workspace_schema() -> None:
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS workspace_messages_core (
                id BIGSERIAL PRIMARY KEY,
                channel TEXT NOT NULL DEFAULT 'ai-agent',
                role TEXT NOT NULL DEFAULT 'user',
                message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_workspace_messages_core_channel_created ON workspace_messages_core(channel, created_at DESC)")
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


def clear_workspace_channel_history(channel: str = "ai-agent") -> int:
    """Delete all messages for a channel. Returns number of rows deleted."""
    ch = str(channel or "ai-agent").strip().lower() or "ai-agent"
    con = pg_connect()
    if con is None:
        return 0
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM workspace_messages_core WHERE channel=%s", (ch,))
        n = cur.rowcount or 0
        con.commit()
        return int(n)
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return 0
    finally:
        con.close()


def add_workspace_message(channel: str, role: str, message: str) -> None:
    ch = str(channel or "ai-agent").strip().lower() or "ai-agent"
    rl = str(role or "user").strip().lower() or "user"
    msg = str(message or "").strip()
    if not msg:
        return
    ensure_workspace_schema()
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        cur.execute(
            """
            INSERT INTO workspace_messages_core(channel, role, message, created_at)
            VALUES (%s,%s,%s,%s)
            """,
            (ch, rl, msg[:8000], dt.datetime.now().isoformat()),
        )
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


def _table_exists(cur: Any, table: str) -> bool:
    try:
        cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
        row = cur.fetchone()
        return bool(row and row[0])
    except Exception:
        return False


def _norm_time(raw: str) -> str:
    s = str(raw or "").strip()
    if not s:
        return dt.datetime.now().isoformat()
    return s


def get_workspace_context() -> dict[str, Any]:
    """Lightweight context snapshot for the workspace right panel."""
    ctx: dict[str, Any] = {
        "open_proposals": 0,
        "breach_alerts": 0,
        "last_run_status": "",
        "last_run_at": "",
        "last_run_agent": "",
        "holdings": [],
    }
    con = pg_connect()
    if con is None:
        return ctx
    try:
        cur = con.cursor()
        if _table_exists(cur, "action_proposals_core"):
            cur.execute("SELECT COUNT(*) FROM action_proposals_core WHERE COALESCE(status,'open')='open'")
            row = cur.fetchone()
            ctx["open_proposals"] = int((row or [0])[0] or 0)
        if _table_exists(cur, "thesis_breach_alerts_core"):
            cur.execute("SELECT COUNT(*) FROM thesis_breach_alerts_core WHERE COALESCE(status,'open')='open'")
            row = cur.fetchone()
            ctx["breach_alerts"] = int((row or [0])[0] or 0)
        if _table_exists(cur, "agent_runs_core"):
            cur.execute(
                "SELECT agent_name, status, started_at FROM agent_runs_core ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
            if row:
                ctx["last_run_agent"] = str(row[0] or "")
                ctx["last_run_status"] = str(row[1] or "")
                ctx["last_run_at"] = str(row[2] or "")[:16]
    except Exception:
        pass
    finally:
        con.close()
    # Holdings from portfolio service
    try:
        from app.services.portfolio_memory_service import get_holdings
        holdings = get_holdings(limit=10)
        ctx["holdings"] = [
            {"ticker": str(h.get("ticker") or ""), "weight": str(h.get("weight") or h.get("pct_weight") or "")}
            for h in (holdings or [])
        ]
    except Exception:
        pass
    return ctx


def load_workspace_feed(channel: str = "all", limit: int = 50) -> list[dict[str, Any]]:
    ch = str(channel or "all").strip().lower() or "all"
    lim = max(1, min(int(limit or 50), 200))
    ensure_workspace_schema()
    con = pg_connect()
    if con is None:
        return []
    items: list[dict[str, Any]] = []
    try:
        cur = con.cursor()

        if ch in {"all", "morning-brief"} and _table_exists(cur, "morning_briefs_core"):
            cur.execute(
                """
                SELECT brief_day, created_at, source, brief_json::text
                FROM morning_briefs_core
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (min(lim, 12),),
            )
            for row in cur.fetchall() or []:
                brief_day = str(row[0] or "")
                created_at = _norm_time(str(row[1] or ""))
                source = str(row[2] or "")
                try:
                    payload = json.loads(str(row[3] or "{}"))
                except Exception:
                    payload = {}
                bullets = payload.get("points") if isinstance(payload, dict) else []
                if not isinstance(bullets, list):
                    bullets = []
                text = "\n".join([f"- {str(x)}" for x in bullets[:6]]) or "Morning brief generated."
                items.append(
                    {
                        "channel": "morning-brief",
                        "kind": "morning_brief",
                        "title": f"Morning Brief · {brief_day or 'Today'}",
                        "text": text,
                        "meta": source,
                        "ts": created_at,
                    }
                )

        if ch in {"all", "alerts"} and _table_exists(cur, "thesis_breach_alerts_core"):
            cur.execute(
                """
                SELECT id, ticker, breach_type, breach_detail, severity, detected_at
                FROM thesis_breach_alerts_core
                WHERE COALESCE(status,'open')='open'
                ORDER BY detected_at DESC
                LIMIT %s
                """,
                (min(lim, 50),),
            )
            for row in cur.fetchall() or []:
                items.append(
                    {
                        "channel": "alerts",
                        "kind": "thesis_breach",
                        "id": int(row[0] or 0),
                        "title": f"Thesis Breach · {str(row[1] or '').upper()}",
                        "text": str(row[3] or row[2] or "Potential thesis breach."),
                        "meta": f"{str(row[4] or 'medium').upper()} · {str(row[2] or '')}",
                        "ticker": str(row[1] or "").upper(),
                        "ts": _norm_time(str(row[5] or "")),
                        "actions": [
                            {"label": "Dismiss", "method": "POST", "url": f"/api/agent/breach-alerts/{int(row[0] or 0)}/dismiss"}
                        ],
                    }
                )

        if ch in {"all", "alerts"} and _table_exists(cur, "portfolio_cascade_alerts_core"):
            cur.execute(
                """
                SELECT id, trigger_ticker, affected_ticker, effect_summary, severity, detected_at
                FROM portfolio_cascade_alerts_core
                WHERE COALESCE(status,'open')='open'
                ORDER BY detected_at DESC
                LIMIT %s
                """,
                (min(lim, 50),),
            )
            for row in cur.fetchall() or []:
                items.append(
                    {
                        "channel": "alerts",
                        "kind": "cascade_alert",
                        "id": int(row[0] or 0),
                        "title": f"Cascade Alert · {str(row[1] or '').upper()} → {str(row[2] or '').upper()}",
                        "text": str(row[3] or "Potential portfolio cascade impact."),
                        "meta": str(row[4] or "medium").upper(),
                        "ticker": str(row[2] or "").upper(),
                        "ts": _norm_time(str(row[5] or "")),
                        "actions": [
                            {"label": "Dismiss", "method": "POST", "url": f"/api/agent/cascade-alerts/{int(row[0] or 0)}/dismiss"}
                        ],
                    }
                )

        if ch in {"all", "filings"} and _table_exists(cur, "filings_core"):
            cur.execute(
                """
                SELECT ticker, form, date, accession, downloaded_at
                FROM filings_core
                ORDER BY downloaded_at DESC
                LIMIT %s
                """,
                (min(lim, 80),),
            )
            for row in cur.fetchall() or []:
                tk = str(row[0] or "").upper()
                fm = str(row[1] or "")
                fd = str(row[2] or "")
                acc = str(row[3] or "")
                items.append(
                    {
                        "channel": "filings",
                        "kind": "filing",
                        "title": f"New Filing · {tk} {fm}",
                        "text": f"Filed {fd} · Accession {acc}",
                        "meta": fm,
                        "ticker": tk,
                        "ts": _norm_time(str(row[4] or fd)),
                        "actions": [
                            {"label": "Open SEC", "method": "GET", "url": f"/company_file/sec?t={tk}"},
                            {"label": "Analyze", "method": "GET", "url": f"/company_file?t={tk}"},
                        ],
                    }
                )

        if ch in {"all", "portfolio"} and _table_exists(cur, "action_proposals_core"):
            cur.execute(
                """
                SELECT id, ticker, title, status, confidence_score, created_at
                FROM action_proposals_core
                WHERE COALESCE(status,'') IN ('open','pending','review')
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (min(lim, 80),),
            )
            for row in cur.fetchall() or []:
                items.append(
                    {
                        "channel": "portfolio",
                        "kind": "proposal",
                        "id": int(row[0] or 0),
                        "title": str(row[2] or "Portfolio Proposal"),
                        "text": f"Ticker {str(row[1] or '').upper()} · Status {str(row[3] or '').upper()}",
                        "meta": f"Confidence {float(row[4] or 0) * 100:.0f}%",
                        "ticker": str(row[1] or "").upper(),
                        "ts": _norm_time(str(row[5] or "")),
                        "actions": [
                            {"label": "Execute", "method": "POST", "url": f"/dashboard/proposals/{int(row[0] or 0)}/execute"},
                            {"label": "Reject", "method": "POST", "url": f"/dashboard/proposals/{int(row[0] or 0)}/reject"},
                        ],
                    }
                )

        if ch in {"all", "notes"} and _table_exists(cur, "investor_annotations_core"):
            cur.execute(
                """
                SELECT entity_id, annotation_type, content, created_at
                FROM investor_annotations_core
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (min(lim, 80),),
            )
            for row in cur.fetchall() or []:
                ent = str(row[0] or "").upper()
                items.append(
                    {
                        "channel": "notes",
                        "kind": "note",
                        "title": f"Annotation · {ent or 'GLOBAL'}",
                        "text": str(row[2] or "")[:350],
                        "meta": str(row[1] or "note"),
                        "ticker": ent,
                        "ts": _norm_time(str(row[3] or "")),
                    }
                )

        if ch in {"all", "agent-runs"} and _table_exists(cur, "agent_runs_core"):
            # Auto-timeout any run stuck in 'running' for > 5 minutes
            try:
                cur.execute(
                    """
                    UPDATE agent_runs_core
                    SET status='timeout', finished_at=started_at,
                        error_text='Auto-timeout: no completion received', updated_at=NOW()::text
                    WHERE status='running'
                      AND started_at < (NOW() - INTERVAL '5 minutes')::text
                    """
                )
            except Exception:
                pass

            # In #All channel: only show completed/failed/timeout/error runs (not raw RUNNING noise)
            # In #Agent Runs channel: show everything including in-progress
            status_filter = "AND status != 'running'" if ch == "all" else ""
            cur.execute(
                f"""
                SELECT agent_name, trigger_type, status, started_at, finished_at, duration_ms
                FROM agent_runs_core
                {status_filter}
                ORDER BY started_at DESC
                LIMIT %s
                """,
                (min(lim, 80),),
            )
            for row in cur.fetchall() or []:
                status = str(row[2] or "unknown").upper()
                dur = float(row[5] or 0.0)
                items.append(
                    {
                        "channel": "agent-runs",
                        "kind": "agent_run",
                        "title": f"{str(row[0] or 'Agent')} · {status}",
                        "text": f"Trigger: {str(row[1] or '')} · Duration: {dur/1000.0:.1f}s",
                        "meta": str(row[4] or ""),
                        "ts": _norm_time(str(row[3] or "")),
                    }
                )
        if ch in {"all", "ai-agent"} and _table_exists(cur, "workspace_messages_core"):
            cur.execute(
                """
                SELECT id, channel, role, message, created_at
                FROM workspace_messages_core
                WHERE channel='ai-agent'
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (min(lim, 80),),
            )
            for row in cur.fetchall() or []:
                role = str(row[2] or "user").lower()
                items.append(
                    {
                        "channel": "ai-agent",
                        "kind": "chat_message",
                        "id": int(row[0] or 0),
                        "title": "AI Agent" if role == "assistant" else "You",
                        "text": str(row[3] or ""),
                        "meta": role,
                        "ts": _norm_time(str(row[4] or "")),
                    }
                )
    except Exception:
        return []
    finally:
        con.close()

    items.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
    if ch == "all":
        return items[:lim]
    return [it for it in items if str(it.get("channel") or "") == ch][:lim]
