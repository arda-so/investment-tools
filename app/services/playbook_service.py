"""playbook_service.py — Playbooks & Routines engine.

Tables:
  playbooks_core         — playbook definitions (name, steps JSON, schedule)
  playbook_runs_core     — each execution of a playbook
  playbook_step_notes_core — notes/findings per step per run
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any

from app.services.postgres_core_service import pg_connect, pg_enabled

LOGGER = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS playbooks_core (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    emoji       TEXT NOT NULL DEFAULT '📖',
    description TEXT NOT NULL DEFAULT '',
    purpose     TEXT NOT NULL DEFAULT '',
    steps       JSONB NOT NULL DEFAULT '[]'::jsonb,
    schedule    TEXT NOT NULL DEFAULT '',
    workbench   JSONB NOT NULL DEFAULT '{"links":[],"workspace_note":""}'::jsonb,
    guardrails  JSONB NOT NULL DEFAULT '[]'::jsonb,
    output_config JSONB NOT NULL DEFAULT '{"decision_type":"pass_fail","pass_action":"","fail_action":""}'::jsonb,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pb_status ON playbooks_core(status);

CREATE TABLE IF NOT EXISTS playbook_runs_core (
    id          BIGSERIAL PRIMARY KEY,
    playbook_id BIGINT NOT NULL REFERENCES playbooks_core(id) ON DELETE CASCADE,
    ticker      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'in_progress',
    current_step INTEGER NOT NULL DEFAULT 0,
    decision    TEXT NOT NULL DEFAULT '',
    decision_notes TEXT NOT NULL DEFAULT '',
    next_action TEXT NOT NULL DEFAULT '',
    started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_pbrun_pb ON playbook_runs_core(playbook_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_pbrun_status ON playbook_runs_core(status);

CREATE TABLE IF NOT EXISTS playbook_step_notes_core (
    id          BIGSERIAL PRIMARY KEY,
    run_id      BIGINT NOT NULL REFERENCES playbook_runs_core(id) ON DELETE CASCADE,
    step_index  INTEGER NOT NULL DEFAULT 0,
    notes       TEXT NOT NULL DEFAULT '',
    checked_items JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pbsn_run ON playbook_step_notes_core(run_id, step_index);
"""

# ── Value Hunt seed playbook ─────────────────────────────────────────────────
_VALUE_HUNT_STEPS = [
    {
        "title": "THE SCREENER",
        "subtitle": "Finding the Lows",
        "items": [
            "Open Finviz New Lows or TradingView All Time Lows",
            "Kill List: Delete companies with declining industries, massive debt, lying management",
            "Find ONE company that is down 52 weeks but has a business model you like"
        ],
        "output_field": "ticker",
        "output_label": "Survivor Ticker"
    },
    {
        "title": "THE SCUTTLEBUTT",
        "subtitle": "The Reality Check",
        "items": [
            "Product: Why are customers leaving? Or are they? (Reddit, YouTube, App Store)",
            "Competitor: Is the rival eating their lunch, or whole industry down?",
            "Vibe: Check Glassdoor — morale dropping as fast as stock price?"
        ],
        "output_field": "notes",
        "output_label": "Findings"
    },
    {
        "title": "THE BEAR HUNT",
        "subtitle": "Why is it low?",
        "items": [
            "Search \"[Ticker] Short Thesis\"",
            "Is it Noise? (Temporary earnings miss, macro fear)",
            "Is it Signal? (Business model is broken)",
            "If you can't disprove the Bear Case with evidence, walk away"
        ],
        "output_field": "notes",
        "output_label": "Bear Case Analysis"
    },
    {
        "title": "THE FILING",
        "subtitle": "Organize",
        "items": [
            "Create research page: [TICKER] - New Low Research",
            "Fill in basic metrics: Margins, Debt, ROIC",
            "Set the Strike Price: If it's a Good House, what price makes the risk worth it?"
        ],
        "output_field": "notes",
        "output_label": "Summary & Strike Price"
    }
]

_DEFAULT_WORKBENCH = {"links": [], "workspace_note": ""}
_DEFAULT_OUTPUT_CONFIG = {
    "decision_type": "pass_fail",
    "pass_action": "",
    "fail_action": "",
}
_ALLOWED_OUTPUT_FIELDS = {"", "notes", "ticker"}
_ALLOWED_DECISIONS = {"", "pass", "fail"}


def _normalize_workbench(workbench: Any) -> dict[str, Any]:
    data = workbench if isinstance(workbench, dict) else {}
    links = []
    for link in data.get("links") or []:
        if not isinstance(link, dict):
            continue
        label = str(link.get("label") or "").strip()
        url = str(link.get("url") or "").strip()
        if not label and not url:
            continue
        links.append({"label": label[:120], "url": url[:1000]})
    return {
        "links": links,
        "workspace_note": str(data.get("workspace_note") or "").strip()[:2000],
    }


def _normalize_guardrails(guardrails: Any) -> list[str]:
    if not isinstance(guardrails, list):
        return []
    return [str(rule or "").strip()[:300] for rule in guardrails if str(rule or "").strip()]


def _normalize_steps(steps: Any) -> list[dict[str, Any]]:
    if not isinstance(steps, list):
        return []
    out: list[dict[str, Any]] = []
    for raw in steps:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()[:200]
        if not title:
            continue
        subtitle = str(raw.get("subtitle") or "").strip()[:400]
        items = [
            str(item or "").strip()[:400]
            for item in (raw.get("items") or [])
            if str(item or "").strip()
        ]
        output_field = str(raw.get("output_field") or "").strip().lower()
        if output_field not in _ALLOWED_OUTPUT_FIELDS:
            output_field = ""
        output_label = str(raw.get("output_label") or "").strip()[:120]
        out.append(
            {
                "title": title,
                "subtitle": subtitle,
                "items": items,
                "output_field": output_field,
                "output_label": output_label,
            }
        )
    return out


def _normalize_output_config(output_config: Any) -> dict[str, str]:
    data = output_config if isinstance(output_config, dict) else {}
    decision_type = "pass_fail"
    if str(data.get("decision_type") or "").strip().lower() == "pass_fail":
        decision_type = "pass_fail"
    return {
        "decision_type": decision_type,
        "pass_action": str(data.get("pass_action") or "").strip()[:2000],
        "fail_action": str(data.get("fail_action") or "").strip()[:2000],
    }


def _normalize_playbook(pb: dict[str, Any]) -> dict[str, Any]:
    pb["purpose"] = str(pb.get("purpose") or pb.get("description") or "").strip()
    pb["workbench"] = _normalize_workbench(pb.get("workbench"))
    pb["guardrails"] = _normalize_guardrails(pb.get("guardrails"))
    pb["steps"] = _normalize_steps(pb.get("steps"))
    pb["output_config"] = _normalize_output_config(pb.get("output_config"))
    pb["schedule"] = str(pb.get("schedule") or "").strip().lower()[:100]
    return pb


def _serialize_playbook_fields(fields: dict[str, Any]) -> dict[str, Any]:
    serialized = dict(fields)
    if "purpose" in serialized:
        serialized["purpose"] = str(serialized["purpose"] or "").strip()[:2000]
    if "description" in serialized:
        serialized["description"] = str(serialized["description"] or "").strip()[:2000]
    if "name" in serialized:
        serialized["name"] = str(serialized["name"] or "").strip()[:200]
    if "emoji" in serialized:
        serialized["emoji"] = str(serialized["emoji"] or "📖").strip()[:10]
    if "schedule" in serialized:
        serialized["schedule"] = str(serialized["schedule"] or "").strip().lower()[:100]
    if "steps" in serialized:
        serialized["steps"] = json.dumps(_normalize_steps(serialized["steps"]))
    if "workbench" in serialized:
        serialized["workbench"] = json.dumps(_normalize_workbench(serialized["workbench"]))
    if "guardrails" in serialized:
        serialized["guardrails"] = json.dumps(_normalize_guardrails(serialized["guardrails"]))
    if "output_config" in serialized:
        serialized["output_config"] = json.dumps(_normalize_output_config(serialized["output_config"]))
    if "status" in serialized:
        serialized["status"] = str(serialized["status"] or "").strip()[:40]
    return serialized


def ensure_playbook_schema() -> None:
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
        cur.execute("ALTER TABLE playbooks_core ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT ''")
        cur.execute(
            """ALTER TABLE playbooks_core
               ADD COLUMN IF NOT EXISTS workbench JSONB NOT NULL
               DEFAULT '{"links":[],"workspace_note":""}'::jsonb"""
        )
        cur.execute(
            """ALTER TABLE playbooks_core
               ADD COLUMN IF NOT EXISTS guardrails JSONB NOT NULL
               DEFAULT '[]'::jsonb"""
        )
        cur.execute(
            """ALTER TABLE playbooks_core
               ADD COLUMN IF NOT EXISTS output_config JSONB NOT NULL
               DEFAULT '{"decision_type":"pass_fail","pass_action":"","fail_action":""}'::jsonb"""
        )
        cur.execute("ALTER TABLE playbook_runs_core ADD COLUMN IF NOT EXISTS decision TEXT NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE playbook_runs_core ADD COLUMN IF NOT EXISTS decision_notes TEXT NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE playbook_runs_core ADD COLUMN IF NOT EXISTS next_action TEXT NOT NULL DEFAULT ''")
        con.commit()

        # Seed Value Hunt playbook if table is empty
        try:
            cur.execute("SELECT COUNT(*) FROM playbooks_core")
            row = cur.fetchone()
            if row and int(row[0]) == 0:
                cur.execute(
                    """INSERT INTO playbooks_core
                       (name, emoji, description, purpose, steps, schedule, workbench, guardrails, output_config)
                       VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)""",
                    (
                        "Value Hunt & Scuttlebutt",
                        "🔍",
                        "Weekly deep-value screening: find new lows, scuttlebutt check, bear case analysis, and file the research.",
                        "Find deep-value stocks at 52-week lows, validate with scuttlebutt, and decide Pass or Fail.",
                        json.dumps(_VALUE_HUNT_STEPS),
                        "saturday",
                        json.dumps(
                            {
                                "links": [
                                    {
                                        "label": "Finviz New Lows",
                                        "url": "https://finviz.com/screener.ashx?v=111&f=ta_highlow52w_nl",
                                    },
                                    {"label": "ROIC.ai", "url": "https://roic.ai"},
                                    {
                                        "label": "SEC EDGAR",
                                        "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany",
                                    },
                                ],
                                "workspace_note": "Open a new research page for the ticker",
                            }
                        ),
                        json.dumps(
                            [
                                "Single-company focus per session",
                                "Do not skim — read fully",
                                "If you can't disprove the bear case, walk away",
                            ]
                        ),
                        json.dumps(
                            {
                                "decision_type": "pass_fail",
                                "pass_action": "Set Strike Price, add to Idea List, then Watchlist",
                                "fail_action": "Delete from inbox, move on",
                            }
                        ),
                    ),
                )
                con.commit()
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


def _row_to_dict(cur_description, row) -> dict:
    cols = [d[0] for d in cur_description]
    d: dict = {}
    for k, v in zip(cols, row):
        if isinstance(v, (dt.date, dt.datetime)):
            d[k] = v.isoformat()
        else:
            d[k] = v
    return d


# ── Playbook CRUD ────────────────────────────────────────────────────────────

def list_playbooks(status: str = "active") -> list[dict]:
    ensure_playbook_schema()
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT * FROM playbooks_core WHERE status=%s ORDER BY created_at ASC",
            (status,),
        )
        playbooks = [_normalize_playbook(_row_to_dict(cur.description, r)) for r in (cur.fetchall() or [])]
        # Attach last run info
        for pb in playbooks:
            try:
                cur.execute(
                    """SELECT id, status, started_at, completed_at, ticker
                       FROM playbook_runs_core
                       WHERE playbook_id=%s ORDER BY started_at DESC LIMIT 1""",
                    (pb["id"],),
                )
                row = cur.fetchone()
                if row:
                    pb["last_run"] = _row_to_dict(cur.description, row)
                else:
                    pb["last_run"] = None
            except Exception:
                pb["last_run"] = None
        return playbooks
    except Exception as exc:
        LOGGER.warning("list_playbooks failed: %s", str(exc))
        return []
    finally:
        con.close()


def get_playbook(pb_id: int) -> dict | None:
    ensure_playbook_schema()
    if not pg_enabled():
        return None
    con = pg_connect()
    if con is None:
        return None
    try:
        cur = con.cursor()
        cur.execute("SELECT * FROM playbooks_core WHERE id=%s", (pb_id,))
        row = cur.fetchone()
        if not row:
            return None
        return _normalize_playbook(_row_to_dict(cur.description, row))
    except Exception:
        return None
    finally:
        con.close()


def create_playbook(
    name: str,
    emoji: str = "📖",
    description: str = "",
    purpose: str = "",
    steps: list | None = None,
    schedule: str = "",
    workbench: dict | None = None,
    guardrails: list | None = None,
    output_config: dict | None = None,
) -> int:
    ensure_playbook_schema()
    if not pg_enabled():
        return 0
    con = pg_connect()
    if con is None:
        return 0
    try:
        cur = con.cursor()
        payload = _serialize_playbook_fields(
            {
                "name": name,
                "emoji": emoji,
                "description": description,
                "purpose": purpose,
                "steps": steps or [],
                "schedule": schedule,
                "workbench": workbench or _DEFAULT_WORKBENCH,
                "guardrails": guardrails or [],
                "output_config": output_config or _DEFAULT_OUTPUT_CONFIG,
            }
        )
        cur.execute(
            """INSERT INTO playbooks_core
               (name, emoji, description, purpose, steps, schedule, workbench, guardrails, output_config)
               VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb, %s::jsonb)
               RETURNING id""",
            (
                payload["name"],
                payload["emoji"],
                payload["description"],
                payload["purpose"],
                payload["steps"],
                payload["schedule"],
                payload["workbench"],
                payload["guardrails"],
                payload["output_config"],
            ),
        )
        row = cur.fetchone()
        con.commit()
        return int(row[0]) if row else 0
    except Exception as exc:
        LOGGER.warning("create_playbook failed: %s", str(exc))
        try:
            con.rollback()
        except Exception:
            pass
        return 0
    finally:
        con.close()


def update_playbook(pb_id: int, **fields) -> bool:
    allowed = {
        "name", "emoji", "description", "purpose", "steps", "schedule",
        "workbench", "guardrails", "output_config", "status"
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates or not pg_enabled():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        updates = _serialize_playbook_fields(updates)
        set_parts = ", ".join(f"{k}=%s" for k in updates)
        vals = list(updates.values()) + [pb_id]
        cur.execute(
            f"UPDATE playbooks_core SET {set_parts}, updated_at=NOW() WHERE id=%s",
            vals,
        )
        con.commit()
        return True
    except Exception as exc:
        LOGGER.warning("update_playbook failed: %s", str(exc))
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def delete_playbook(pb_id: int) -> bool:
    if not pg_enabled():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute("DELETE FROM playbooks_core WHERE id=%s", (pb_id,))
        con.commit()
        return True
    except Exception as exc:
        LOGGER.warning("delete_playbook failed: %s", str(exc))
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


# ── Runs ─────────────────────────────────────────────────────────────────────

def start_run(playbook_id: int) -> int:
    """Start a new playbook run. Returns run_id."""
    ensure_playbook_schema()
    if not pg_enabled():
        return 0
    con = pg_connect()
    if con is None:
        return 0
    try:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO playbook_runs_core (playbook_id) VALUES (%s) RETURNING id",
            (playbook_id,),
        )
        row = cur.fetchone()
        con.commit()
        return int(row[0]) if row else 0
    except Exception as exc:
        LOGGER.warning("start_run failed: %s", str(exc))
        try:
            con.rollback()
        except Exception:
            pass
        return 0
    finally:
        con.close()


def get_run(run_id: int) -> dict | None:
    ensure_playbook_schema()
    if not pg_enabled():
        return None
    con = pg_connect()
    if con is None:
        return None
    try:
        cur = con.cursor()
        cur.execute("SELECT * FROM playbook_runs_core WHERE id=%s", (run_id,))
        row = cur.fetchone()
        if not row:
            return None
        run = _row_to_dict(cur.description, row)
        # Attach step notes
        cur.execute(
            "SELECT * FROM playbook_step_notes_core WHERE run_id=%s ORDER BY step_index",
            (run_id,),
        )
        run["step_notes"] = [_row_to_dict(cur.description, r) for r in (cur.fetchall() or [])]
        return run
    except Exception:
        return None
    finally:
        con.close()


def update_run(run_id: int, **fields) -> bool:
    allowed = {"status", "current_step", "ticker", "completed_at", "decision", "decision_notes", "next_action"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates or not pg_enabled():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        if "ticker" in updates:
            updates["ticker"] = str(updates["ticker"] or "").strip().upper()[:20]
        if "decision" in updates:
            decision = str(updates["decision"] or "").strip().lower()
            updates["decision"] = decision if decision in _ALLOWED_DECISIONS else ""
        if "decision_notes" in updates:
            updates["decision_notes"] = str(updates["decision_notes"] or "").strip()[:4000]
        if "next_action" in updates:
            updates["next_action"] = str(updates["next_action"] or "").strip()[:4000]
        set_parts = ", ".join(f"{k}=%s" for k in updates)
        vals = list(updates.values()) + [run_id]
        cur.execute(f"UPDATE playbook_runs_core SET {set_parts} WHERE id=%s", vals)
        con.commit()
        return True
    except Exception as exc:
        LOGGER.warning("update_run failed: %s", str(exc))
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def complete_run(
    run_id: int,
    ticker: str = "",
    decision: str = "",
    decision_notes: str = "",
    next_action: str = "",
) -> bool:
    return update_run(
        run_id,
        status="completed",
        ticker=ticker,
        decision=decision,
        decision_notes=decision_notes,
        next_action=next_action,
        completed_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )


def list_runs(playbook_id: int, limit: int = 20) -> list[dict]:
    ensure_playbook_schema()
    if not pg_enabled():
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            """SELECT * FROM playbook_runs_core
               WHERE playbook_id=%s ORDER BY started_at DESC LIMIT %s""",
            (playbook_id, max(1, min(100, limit))),
        )
        return [_row_to_dict(cur.description, r) for r in (cur.fetchall() or [])]
    except Exception:
        return []
    finally:
        con.close()


# ── Step notes ───────────────────────────────────────────────────────────────

def save_step_notes(run_id: int, step_index: int, notes: str, checked_items: list | None = None) -> bool:
    if not pg_enabled():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        checked = json.dumps(checked_items or [])
        # Upsert
        cur.execute(
            """INSERT INTO playbook_step_notes_core (run_id, step_index, notes, checked_items, updated_at)
               VALUES (%s, %s, %s, %s::jsonb, NOW())
               ON CONFLICT (run_id, step_index) DO UPDATE
               SET notes=EXCLUDED.notes, checked_items=EXCLUDED.checked_items, updated_at=NOW()""",
            (run_id, step_index, str(notes or ""), checked),
        )
        con.commit()
        return True
    except Exception as exc:
        # ON CONFLICT needs a unique index — add it if missing and retry
        try:
            con.rollback()
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_pbsn_run_step ON playbook_step_notes_core(run_id, step_index)"
            )
            con.commit()
            cur.execute(
                """INSERT INTO playbook_step_notes_core (run_id, step_index, notes, checked_items, updated_at)
                   VALUES (%s, %s, %s, %s::jsonb, NOW())
                   ON CONFLICT (run_id, step_index) DO UPDATE
                   SET notes=EXCLUDED.notes, checked_items=EXCLUDED.checked_items, updated_at=NOW()""",
                (run_id, step_index, str(notes or ""), checked),
            )
            con.commit()
            return True
        except Exception as exc2:
            LOGGER.warning("save_step_notes failed: %s", str(exc2))
            try:
                con.rollback()
            except Exception:
                pass
            return False
    finally:
        con.close()


# ── Schedule helpers ─────────────────────────────────────────────────────────

def get_todays_playbooks() -> list[dict]:
    """Return playbooks scheduled for today."""
    all_pbs = list_playbooks(status="active")
    today = dt.date.today()
    day_name = today.strftime("%A").lower()  # monday, tuesday, etc.
    day_short = today.strftime("%a").lower()  # mon, tue, etc.

    result = []
    for pb in all_pbs:
        sched = str(pb.get("schedule") or "").strip().lower()
        base = sched.split("@", 1)[0]
        if not sched:
            continue
        match = False
        if base == "daily":
            match = True
        elif base == "weekday" and day_name not in ("saturday", "sunday"):
            match = True
        elif base == "weekend" and day_name in ("saturday", "sunday"):
            match = True
        elif day_name in base.split(",") or day_short in base:
            match = True
        if match:
            result.append(pb)
    return result
