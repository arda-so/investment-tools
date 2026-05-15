"""cognitive_engine_service.py — Phase 4: Cognitive Investment Intelligence.

Six interconnected features that transform the system from a filing cabinet
into a thinking partner:

  1. Assumption Graph        — explicit premises per position with measurable conditions
  2. Conviction Tracker      — score per position, auto-updated by events
  3. Conditional Triggers    — user-defined "if X then alert me" rules
  4. Portfolio Coherence Map  — shared assumptions across positions
  5. Living Synthesis        — auto-generated ticker summary, refreshed on material change
  6. Signal Preference Learning — tracks dismiss/act ratios to reduce noise
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import time
from typing import Any

from app.services.postgres_core_service import pg_connect, pg_enabled

LOGGER = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# SCHEMA
# ═══════════════════════════════════════════════════════════════════════════════

_DDL = """
-- 1. Assumption Graph
CREATE TABLE IF NOT EXISTS position_assumptions_core (
    id                  BIGSERIAL PRIMARY KEY,
    ticker              TEXT NOT NULL,
    assumption_text     TEXT NOT NULL,
    condition_type      TEXT NOT NULL DEFAULT 'qualitative',
    measurable_metric   TEXT NOT NULL DEFAULT '',
    threshold_operator  TEXT NOT NULL DEFAULT '>=',
    threshold_value     DOUBLE PRECISION,
    current_value       DOUBLE PRECISION,
    status              TEXT NOT NULL DEFAULT 'active',
    importance          TEXT NOT NULL DEFAULT 'core',
    source_chain_id     BIGINT,
    source_node_id      BIGINT,
    breached_at         TIMESTAMPTZ,
    last_checked_at     TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pa_ticker ON position_assumptions_core(UPPER(ticker));
CREATE INDEX IF NOT EXISTS idx_pa_status ON position_assumptions_core(status);

CREATE TABLE IF NOT EXISTS assumption_check_log_core (
    id              BIGSERIAL PRIMARY KEY,
    assumption_id   BIGINT NOT NULL,
    old_status      TEXT NOT NULL DEFAULT '',
    new_status      TEXT NOT NULL DEFAULT '',
    trigger_value   DOUBLE PRECISION,
    notes           TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_acl_assumption ON assumption_check_log_core(assumption_id);

-- 2. Conviction Tracker
CREATE TABLE IF NOT EXISTS conviction_scores_core (
    id              BIGSERIAL PRIMARY KEY,
    ticker          TEXT NOT NULL UNIQUE,
    score           DOUBLE PRECISION NOT NULL DEFAULT 50.0,
    previous_score  DOUBLE PRECISION NOT NULL DEFAULT 50.0,
    components      JSONB NOT NULL DEFAULT '{}',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cs_ticker ON conviction_scores_core(UPPER(ticker));

CREATE TABLE IF NOT EXISTS conviction_history_core (
    id              BIGSERIAL PRIMARY KEY,
    ticker          TEXT NOT NULL,
    score           DOUBLE PRECISION NOT NULL,
    delta           DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    trigger_event   TEXT NOT NULL DEFAULT '',
    trigger_type    TEXT NOT NULL DEFAULT '',
    notes           TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ch_ticker ON conviction_history_core(UPPER(ticker));
CREATE INDEX IF NOT EXISTS idx_ch_created ON conviction_history_core(created_at DESC);

-- 3. Conditional Triggers
CREATE TABLE IF NOT EXISTS conditional_triggers_core (
    id              BIGSERIAL PRIMARY KEY,
    ticker          TEXT NOT NULL DEFAULT '',
    trigger_name    TEXT NOT NULL,
    trigger_type    TEXT NOT NULL DEFAULT 'price_below',
    condition_text  TEXT NOT NULL DEFAULT '',
    threshold_value DOUBLE PRECISION,
    comparison_op   TEXT NOT NULL DEFAULT '<',
    action_text     TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'active',
    last_checked_at TIMESTAMPTZ,
    fired_at        TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ,
    cooldown_hours  INTEGER NOT NULL DEFAULT 24,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ct_status ON conditional_triggers_core(status);
CREATE INDEX IF NOT EXISTS idx_ct_ticker ON conditional_triggers_core(UPPER(ticker));

-- 4. Portfolio Coherence (computed, minimal storage)
CREATE TABLE IF NOT EXISTS shared_assumptions_core (
    id              BIGSERIAL PRIMARY KEY,
    theme           TEXT NOT NULL,
    assumption_ids  JSONB NOT NULL DEFAULT '[]',
    tickers         JSONB NOT NULL DEFAULT '[]',
    risk_note       TEXT NOT NULL DEFAULT '',
    concentration   DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sa_theme ON shared_assumptions_core(theme);

-- 5. Living Synthesis
CREATE TABLE IF NOT EXISTS ticker_synthesis_core (
    id              BIGSERIAL PRIMARY KEY,
    ticker          TEXT NOT NULL UNIQUE,
    synthesis_text  TEXT NOT NULL DEFAULT '',
    data_hash       TEXT NOT NULL DEFAULT '',
    section_scores  JSONB NOT NULL DEFAULT '{}',
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stale           BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_ts_ticker ON ticker_synthesis_core(UPPER(ticker));

-- 6. Signal Preference Learning
CREATE TABLE IF NOT EXISTS signal_preferences_core (
    id              BIGSERIAL PRIMARY KEY,
    signal_type     TEXT NOT NULL UNIQUE,
    total_shown     INTEGER NOT NULL DEFAULT 0,
    total_acted     INTEGER NOT NULL DEFAULT 0,
    total_dismissed INTEGER NOT NULL DEFAULT 0,
    act_ratio       DOUBLE PRECISION NOT NULL DEFAULT 0.5,
    weight_mult     DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sp_type ON signal_preferences_core(signal_type);

CREATE TABLE IF NOT EXISTS signal_action_log_core (
    id              BIGSERIAL PRIMARY KEY,
    signal_type     TEXT NOT NULL,
    signal_id       BIGINT,
    action          TEXT NOT NULL DEFAULT 'shown',
    ticker          TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sal_type ON signal_action_log_core(signal_type);
CREATE INDEX IF NOT EXISTS idx_sal_created ON signal_action_log_core(created_at DESC);
"""


def ensure_cognitive_schema() -> None:
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
        LOGGER.info("cognitive_engine_service: schema ensured")
    except Exception as exc:
        LOGGER.debug("cognitive_engine_service schema error: %s", exc)
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _row_to_dict(desc, row) -> dict:
    cols = [d[0] for d in desc]
    d: dict = {}
    for k, v in zip(cols, row):
        if isinstance(v, (dt.date, dt.datetime)):
            d[k] = v.isoformat()
        else:
            d[k] = v
    return d


def _q(query: str, params: tuple = ()) -> list[dict]:
    """Execute query, return rows as dicts."""
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
        LOGGER.debug("_q failed (%s): %s", query[:60], exc)
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


def _exec(query: str, params: tuple = ()) -> bool:
    """Execute a write query. Returns True on success."""
    if not pg_enabled():
        return False
    con = pg_connect()
    if con is None:
        return False
    try:
        cur = con.cursor()
        cur.execute(query, params)
        con.commit()
        return True
    except Exception as exc:
        LOGGER.debug("_exec failed (%s): %s", query[:60], exc)
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        try:
            con.close()
        except Exception:
            pass


def _exec_returning(query: str, params: tuple = ()) -> int | None:
    """Execute INSERT ... RETURNING id. Returns the id or None."""
    if not pg_enabled():
        return None
    con = pg_connect()
    if con is None:
        return None
    try:
        cur = con.cursor()
        cur.execute(query, params)
        row = cur.fetchone()
        con.commit()
        return int(row[0]) if row else None
    except Exception as exc:
        LOGGER.debug("_exec_returning failed: %s", exc)
        try:
            con.rollback()
        except Exception:
            pass
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# FEATURE 1: ASSUMPTION GRAPH
# ═══════════════════════════════════════════════════════════════════════════════

def create_assumption(
    ticker: str,
    assumption_text: str,
    condition_type: str = "qualitative",
    measurable_metric: str = "",
    threshold_operator: str = ">=",
    threshold_value: float | None = None,
    importance: str = "core",
    source_chain_id: int | None = None,
    source_node_id: int | None = None,
) -> int | None:
    """Create an explicit assumption for a position."""
    tk = str(ticker or "").strip().upper()
    if not tk or not assumption_text:
        return None
    return _exec_returning(
        """INSERT INTO position_assumptions_core
           (ticker, assumption_text, condition_type, measurable_metric,
            threshold_operator, threshold_value, importance,
            source_chain_id, source_node_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
           RETURNING id""",
        (tk, assumption_text.strip(), condition_type, measurable_metric,
         threshold_operator, threshold_value, importance,
         source_chain_id, source_node_id),
    )


def list_assumptions(ticker: str = "", status: str = "", limit: int = 50) -> list[dict]:
    """List assumptions, optionally filtered by ticker and/or status."""
    clauses = []
    params: list = []
    if ticker:
        clauses.append("UPPER(ticker) = %s")
        params.append(ticker.strip().upper())
    if status:
        clauses.append("status = %s")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return _q(
        f"SELECT * FROM position_assumptions_core{where} ORDER BY created_at DESC LIMIT %s",
        tuple(params) + (limit,),
    )


def update_assumption(assumption_id: int, **kwargs) -> bool:
    """Update fields on an assumption."""
    allowed = {
        "assumption_text", "condition_type", "measurable_metric",
        "threshold_operator", "threshold_value", "importance", "status",
    }
    sets = []
    params: list = []
    for k, v in kwargs.items():
        if k in allowed:
            sets.append(f"{k} = %s")
            params.append(v)
    if not sets:
        return False
    sets.append("updated_at = NOW()")
    params.append(assumption_id)
    return _exec(
        f"UPDATE position_assumptions_core SET {', '.join(sets)} WHERE id = %s",
        tuple(params),
    )


def delete_assumption(assumption_id: int) -> bool:
    return _exec("DELETE FROM position_assumptions_core WHERE id = %s", (assumption_id,))


def check_assumptions() -> int:
    """Run automated checks on all measurable assumptions. Returns count of breaches detected."""
    assumptions = list_assumptions(status="active")
    breaches = 0
    for a in assumptions:
        ctype = str(a.get("condition_type") or "").strip()
        if ctype == "qualitative":
            continue  # Can't auto-check qualitative assumptions

        metric = str(a.get("measurable_metric") or "").strip()
        threshold = a.get("threshold_value")
        op = str(a.get("threshold_operator") or ">=").strip()
        if not metric or threshold is None:
            continue

        current_val = _fetch_metric_value(a.get("ticker", ""), metric)
        if current_val is None:
            continue

        # Update current_value
        _exec(
            "UPDATE position_assumptions_core SET current_value = %s, last_checked_at = NOW() WHERE id = %s",
            (current_val, a["id"]),
        )

        # Check condition
        breached = False
        if op == ">=" and current_val < threshold:
            breached = True
        elif op == "<=" and current_val > threshold:
            breached = True
        elif op == ">" and current_val <= threshold:
            breached = True
        elif op == "<" and current_val >= threshold:
            breached = True
        elif op == "=" and abs(current_val - threshold) > 0.01:
            breached = True

        old_status = str(a.get("status") or "active")
        new_status = "breached" if breached else "active"

        if old_status != new_status:
            _exec(
                "UPDATE position_assumptions_core SET status = %s, "
                + ("breached_at = NOW(), " if breached else "breached_at = NULL, ")
                + "updated_at = NOW() WHERE id = %s",
                (new_status, a["id"]),
            )
            # Log the change
            _exec(
                "INSERT INTO assumption_check_log_core (assumption_id, old_status, new_status, trigger_value, notes) "
                "VALUES (%s, %s, %s, %s, %s)",
                (a["id"], old_status, new_status, current_val,
                 f"{metric} = {current_val} vs threshold {op} {threshold}"),
            )
            if breached:
                breaches += 1
                # Create autonomic alert
                _create_breach_alert(a, current_val)

    return breaches


def _fetch_metric_value(ticker: str, metric: str) -> float | None:
    """Fetch a live metric value for assumption checking."""
    metric_lower = metric.lower().strip()

    # Price-based metrics — use yfinance
    if metric_lower in ("price", "stock_price", "share_price"):
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            info = t.info or {}
            return float(info.get("currentPrice") or info.get("regularMarketPrice") or 0) or None
        except Exception:
            return None

    # Macro metrics — use cached macro snapshot
    _MACRO_MAP = {
        "oil": "CL=F", "oil_price": "CL=F", "crude": "CL=F",
        "gold": "GC=F", "gold_price": "GC=F",
        "vix": "^VIX",
        "10y_yield": "^TNX", "10y": "^TNX", "treasury_yield": "^TNX",
        "dxy": "DX-Y.NYB", "dollar": "DX-Y.NYB",
        "btc": "BTC-USD", "bitcoin": "BTC-USD",
        "spy": "SPY", "sp500": "^GSPC", "s&p500": "^GSPC",
    }
    yf_ticker = _MACRO_MAP.get(metric_lower)
    if yf_ticker:
        try:
            import yfinance as yf
            t = yf.Ticker(yf_ticker)
            info = t.info or {}
            return float(info.get("regularMarketPrice") or info.get("currentPrice") or 0) or None
        except Exception:
            return None

    # Financial metrics from mini_statements_core
    _FIN_MAP = {
        "revenue": "revenue", "gross_profit": "gross_profit",
        "fcf": "free_cash_flow", "free_cash_flow": "free_cash_flow",
        "debt": "total_debt", "total_debt": "total_debt",
        "cash": "cash", "total_cash": "cash",
        "operating_cash_flow": "operating_cash_flow", "ocf": "operating_cash_flow",
    }
    fin_key = _FIN_MAP.get(metric_lower)
    if fin_key:
        try:
            from app.services.mini_statements_service import get_mini_statements
            ms = get_mini_statements(ticker) or {}
            vals = list(ms.get(fin_key) or [])
            return float(vals[-1]) if vals else None
        except Exception:
            return None

    return None


def _create_breach_alert(assumption: dict, current_val: float) -> None:
    """Create an autonomic alert for a breached assumption."""
    try:
        from app.services.autonomic_service import _create_alert
        ticker = str(assumption.get("ticker") or "").upper()
        _create_alert(
            alert_type="assumption_breach",
            title=f"Assumption breached: {assumption.get('assumption_text', '')[:80]}",
            body=(
                f"Your assumption for ${ticker} has been violated.\n"
                f"Condition: {assumption.get('measurable_metric')} "
                f"{assumption.get('threshold_operator')} {assumption.get('threshold_value')}\n"
                f"Current value: {current_val}\n"
                f"Importance: {assumption.get('importance', 'core')}"
            ),
            severity="warning" if assumption.get("importance") == "core" else "info",
            obj_type="assumption",
            obj_id=str(assumption.get("id", "")),
            ticker=ticker,
        )
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# FEATURE 2: CONVICTION TRACKER
# ═══════════════════════════════════════════════════════════════════════════════

def get_conviction(ticker: str) -> dict | None:
    """Get current conviction score for a ticker."""
    rows = _q(
        "SELECT * FROM conviction_scores_core WHERE UPPER(ticker) = %s",
        (ticker.strip().upper(),),
    )
    return rows[0] if rows else None


def list_convictions(limit: int = 50) -> list[dict]:
    """List all conviction scores, highest first."""
    return _q("SELECT * FROM conviction_scores_core ORDER BY score DESC LIMIT %s", (limit,))


def get_conviction_history(ticker: str, limit: int = 30) -> list[dict]:
    return _q(
        "SELECT * FROM conviction_history_core WHERE UPPER(ticker) = %s "
        "ORDER BY created_at DESC LIMIT %s",
        (ticker.strip().upper(), limit),
    )


def compute_conviction(ticker: str) -> dict:
    """Compute conviction score from multiple signals. Returns components dict."""
    tk = ticker.strip().upper()
    components: dict[str, float] = {}

    # 1. Assumption health (0-100): % of active (non-breached) assumptions
    assumptions = list_assumptions(ticker=tk)
    if assumptions:
        active = sum(1 for a in assumptions if a.get("status") == "active")
        total = len(assumptions)
        components["assumption_health"] = round((active / total) * 100, 1) if total else 50.0
    else:
        components["assumption_health"] = 50.0  # neutral if no assumptions

    # 2. Debate pass rate (0-100): % of proposals that passed debate
    proposals = _q(
        "SELECT status FROM action_proposals_core WHERE UPPER(ticker) = %s "
        "ORDER BY created_at DESC LIMIT 20",
        (tk,),
    )
    if proposals:
        passed = sum(1 for p in proposals if p.get("status") not in ("debate_rejected", "rejected"))
        components["debate_pass_rate"] = round((passed / len(proposals)) * 100, 1)
    else:
        components["debate_pass_rate"] = 50.0

    # 3. Earnings trajectory (0-100): guidance direction trend
    earnings = _q(
        "SELECT guidance_direction FROM earnings_analysis_core WHERE UPPER(ticker) = %s "
        "ORDER BY created_at DESC LIMIT 4",
        (tk,),
    )
    if earnings:
        direction_scores = {"raised": 80, "maintained": 50, "lowered": 20}
        avg = sum(direction_scores.get(e.get("guidance_direction", ""), 50) for e in earnings) / len(earnings)
        components["earnings_trajectory"] = round(avg, 1)
    else:
        components["earnings_trajectory"] = 50.0

    # 4. Research activity (0-100): recency of research engagement
    threads = _q(
        "SELECT updated_at FROM research_threads_core WHERE UPPER(ticker) = %s "
        "AND status = 'active' ORDER BY updated_at DESC LIMIT 1",
        (tk,),
    )
    if threads and threads[0].get("updated_at"):
        try:
            last = dt.datetime.fromisoformat(str(threads[0]["updated_at"]))
            days_ago = (dt.datetime.now(dt.timezone.utc) - last.replace(tzinfo=dt.timezone.utc)).days
            components["research_freshness"] = max(0.0, min(100.0, 100.0 - (days_ago * 2.0)))
        except Exception:
            components["research_freshness"] = 50.0
    else:
        components["research_freshness"] = 30.0  # no active research = lower conviction

    # 5. Signal-to-noise (act ratio for this ticker)
    signal_prefs = _q(
        "SELECT act_ratio FROM signal_preferences_core WHERE signal_type = %s",
        (f"ticker_{tk}",),
    )
    if signal_prefs:
        components["signal_engagement"] = round(float(signal_prefs[0].get("act_ratio", 0.5)) * 100, 1)
    else:
        components["signal_engagement"] = 50.0

    # Weighted average
    weights = {
        "assumption_health": 0.30,
        "debate_pass_rate": 0.20,
        "earnings_trajectory": 0.20,
        "research_freshness": 0.15,
        "signal_engagement": 0.15,
    }
    score = sum(components.get(k, 50.0) * w for k, w in weights.items())
    score = round(max(0.0, min(100.0, score)), 1)

    return {"score": score, "components": components}


def update_conviction(ticker: str, trigger_event: str = "", trigger_type: str = "") -> dict:
    """Recompute and store conviction score. Returns the new score dict."""
    tk = ticker.strip().upper()
    result = compute_conviction(tk)
    score = result["score"]
    components = result["components"]

    existing = get_conviction(tk)
    prev_score = float(existing["score"]) if existing else 50.0
    delta = round(score - prev_score, 1)

    if existing:
        _exec(
            "UPDATE conviction_scores_core SET score = %s, previous_score = %s, "
            "components = %s, updated_at = NOW() WHERE UPPER(ticker) = %s",
            (score, prev_score, json.dumps(components), tk),
        )
    else:
        _exec(
            "INSERT INTO conviction_scores_core (ticker, score, previous_score, components) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (ticker) DO UPDATE SET "
            "score = EXCLUDED.score, previous_score = conviction_scores_core.score, "
            "components = EXCLUDED.components, updated_at = NOW()",
            (tk, score, prev_score, json.dumps(components)),
        )

    # Log history if score actually changed
    if abs(delta) >= 0.5:
        _exec(
            "INSERT INTO conviction_history_core (ticker, score, delta, trigger_event, trigger_type) "
            "VALUES (%s, %s, %s, %s, %s)",
            (tk, score, delta, trigger_event[:200], trigger_type[:50]),
        )

    return {"ticker": tk, "score": score, "previous_score": prev_score, "delta": delta, "components": components}


def update_all_convictions(trigger_event: str = "periodic_sweep") -> list[dict]:
    """Recompute conviction for all held tickers."""
    results = []
    try:
        tickers = _q("SELECT DISTINCT UPPER(ticker) AS ticker FROM portfolio_positions_core")
        for row in tickers:
            tk = str(row.get("ticker") or "").strip()
            if tk:
                r = update_conviction(tk, trigger_event=trigger_event, trigger_type="sweep")
                results.append(r)
    except Exception as exc:
        LOGGER.debug("update_all_convictions failed: %s", exc)
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# FEATURE 3: CONDITIONAL TRIGGERS
# ═══════════════════════════════════════════════════════════════════════════════

def create_trigger(
    ticker: str,
    trigger_name: str,
    trigger_type: str = "price_below",
    condition_text: str = "",
    threshold_value: float | None = None,
    comparison_op: str = "<",
    action_text: str = "",
    expires_at: str | None = None,
    cooldown_hours: int = 24,
) -> int | None:
    """Create a conditional trigger rule."""
    tk = str(ticker or "").strip().upper()
    exp = None
    if expires_at:
        try:
            exp = dt.datetime.fromisoformat(expires_at)
        except Exception:
            pass
    return _exec_returning(
        """INSERT INTO conditional_triggers_core
           (ticker, trigger_name, trigger_type, condition_text, threshold_value,
            comparison_op, action_text, expires_at, cooldown_hours)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (tk, trigger_name.strip(), trigger_type, condition_text.strip(),
         threshold_value, comparison_op, action_text.strip(), exp, cooldown_hours),
    )


def list_triggers(ticker: str = "", status: str = "active", limit: int = 50) -> list[dict]:
    clauses = ["status = %s"]
    params: list = [status]
    if ticker:
        clauses.append("UPPER(ticker) = %s")
        params.append(ticker.strip().upper())
    where = " WHERE " + " AND ".join(clauses)
    return _q(
        f"SELECT * FROM conditional_triggers_core{where} ORDER BY created_at DESC LIMIT %s",
        tuple(params) + (limit,),
    )


def delete_trigger(trigger_id: int) -> bool:
    return _exec("DELETE FROM conditional_triggers_core WHERE id = %s", (trigger_id,))


def disable_trigger(trigger_id: int) -> bool:
    return _exec(
        "UPDATE conditional_triggers_core SET status = 'disabled' WHERE id = %s",
        (trigger_id,),
    )


def check_triggers() -> int:
    """Evaluate all active triggers. Returns count fired."""
    triggers = list_triggers(status="active", limit=200)
    fired = 0
    now = dt.datetime.now(dt.timezone.utc)

    for t in triggers:
        # Check expiry
        exp = t.get("expires_at")
        if exp:
            try:
                exp_dt = dt.datetime.fromisoformat(str(exp))
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=dt.timezone.utc)
                if now > exp_dt:
                    _exec("UPDATE conditional_triggers_core SET status = 'expired' WHERE id = %s", (t["id"],))
                    continue
            except Exception:
                pass

        # Check cooldown
        last_fired = t.get("fired_at")
        cooldown = int(t.get("cooldown_hours") or 24)
        if last_fired:
            try:
                lf_dt = dt.datetime.fromisoformat(str(last_fired))
                if lf_dt.tzinfo is None:
                    lf_dt = lf_dt.replace(tzinfo=dt.timezone.utc)
                if (now - lf_dt).total_seconds() < cooldown * 3600:
                    continue
            except Exception:
                pass

        ttype = str(t.get("trigger_type") or "").strip()
        ticker = str(t.get("ticker") or "").strip().upper()
        threshold = t.get("threshold_value")
        op = str(t.get("comparison_op") or "<").strip()

        if ttype in ("price_below", "price_above") and ticker and threshold is not None:
            current = _fetch_metric_value(ticker, "price")
            if current is None:
                continue
            should_fire = _compare(current, op, threshold)
        elif ttype == "metric_check" and ticker:
            metric = str(t.get("condition_text") or "").strip()
            if not metric:
                continue
            current = _fetch_metric_value(ticker, metric)
            if current is None:
                continue
            should_fire = _compare(current, op, threshold or 0)
        elif ttype == "conviction_below" and ticker and threshold is not None:
            conv = get_conviction(ticker)
            if not conv:
                continue
            current = float(conv.get("score", 50))
            should_fire = current < threshold
        else:
            continue

        # Update check timestamp
        _exec(
            "UPDATE conditional_triggers_core SET last_checked_at = NOW() WHERE id = %s",
            (t["id"],),
        )

        if should_fire:
            fired += 1
            _exec(
                "UPDATE conditional_triggers_core SET status = 'fired', fired_at = NOW() WHERE id = %s",
                (t["id"],),
            )
            # Create alert
            try:
                from app.services.autonomic_service import _create_alert
                _create_alert(
                    alert_type="trigger_fired",
                    title=f"Trigger fired: {t.get('trigger_name', 'Rule')}",
                    body=(
                        f"Condition met for ${ticker}: {t.get('condition_text', '')}\n"
                        f"Action: {t.get('action_text', 'Review position')}"
                    ),
                    severity="warning",
                    obj_type="trigger",
                    obj_id=str(t["id"]),
                    ticker=ticker,
                )
            except Exception:
                pass

    return fired


def _compare(value: float, op: str, threshold: float) -> bool:
    if op == "<":
        return value < threshold
    elif op == "<=":
        return value <= threshold
    elif op == ">":
        return value > threshold
    elif op == ">=":
        return value >= threshold
    elif op == "=":
        return abs(value - threshold) < 0.01
    return False


# ═══════════════════════════════════════════════════════════════════════════════
# FEATURE 4: PORTFOLIO COHERENCE MAP
# ═══════════════════════════════════════════════════════════════════════════════

def compute_portfolio_coherence() -> dict:
    """Analyze shared assumptions across positions and identify concentration risk."""
    all_assumptions = list_assumptions(status="active", limit=500)
    if not all_assumptions:
        return {"themes": [], "concentration_risks": [], "contradictions": []}

    # Group assumptions by semantic theme using keyword extraction
    theme_map: dict[str, list[dict]] = {}
    for a in all_assumptions:
        text = str(a.get("assumption_text") or "").lower()
        themes = _extract_themes(text)
        for theme in themes:
            theme_map.setdefault(theme, []).append(a)

    # Find themes shared across 2+ tickers
    shared_themes: list[dict] = []
    for theme, assumptions in theme_map.items():
        tickers = list(set(str(a.get("ticker", "")) for a in assumptions))
        if len(tickers) >= 2:
            shared_themes.append({
                "theme": theme,
                "tickers": tickers,
                "assumption_count": len(assumptions),
                "assumptions": [{"id": a["id"], "ticker": a["ticker"],
                                 "text": a.get("assumption_text", "")} for a in assumptions],
                "concentration": round(len(tickers) / max(1, len(set(
                    a["ticker"] for a in all_assumptions
                ))) * 100, 1),
            })

    # Sort by concentration (most shared first)
    shared_themes.sort(key=lambda x: x["assumption_count"], reverse=True)

    # Detect contradictions (bullish X + bearish related Y)
    contradictions = _detect_contradictions(all_assumptions)

    # Store results
    _exec("DELETE FROM shared_assumptions_core WHERE 1=1")
    for st in shared_themes[:20]:
        _exec(
            "INSERT INTO shared_assumptions_core (theme, assumption_ids, tickers, concentration, risk_note) "
            "VALUES (%s, %s, %s, %s, %s)",
            (st["theme"],
             json.dumps([a["id"] for a in st["assumptions"]]),
             json.dumps(st["tickers"]),
             st["concentration"],
             f"{len(st['tickers'])} positions depend on '{st['theme']}'"),
        )

    return {
        "themes": shared_themes[:20],
        "concentration_risks": [t for t in shared_themes if t["concentration"] >= 30],
        "contradictions": contradictions,
    }


_THEME_KEYWORDS = {
    "ai_spending": ["ai", "artificial intelligence", "machine learning", "gpu", "data center"],
    "interest_rates": ["interest rate", "fed", "monetary policy", "rate hike", "rate cut", "yield"],
    "china_risk": ["china", "chinese", "tariff", "trade war", "geopolitical"],
    "oil_energy": ["oil", "energy", "crude", "opec", "natural gas"],
    "consumer_strength": ["consumer", "spending", "retail", "demand"],
    "semiconductor": ["semiconductor", "chip", "fab", "foundry", "wafer"],
    "cloud_growth": ["cloud", "aws", "azure", "saas", "subscription"],
    "ev_auto": ["ev", "electric vehicle", "automotive", "battery"],
    "inflation": ["inflation", "cpi", "pricing power", "cost pressure"],
    "dollar_strength": ["dollar", "usd", "currency", "fx", "dxy"],
    "margin_expansion": ["margin", "profitability", "cost reduction", "efficiency"],
    "capex_cycle": ["capex", "capital expenditure", "investment cycle", "capacity"],
    "regulation": ["regulation", "antitrust", "compliance", "government"],
    "supply_chain": ["supply chain", "logistics", "shortage", "inventory"],
}


def _extract_themes(text: str) -> list[str]:
    themes = []
    for theme, keywords in _THEME_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            themes.append(theme)
    return themes


def _detect_contradictions(assumptions: list[dict]) -> list[dict]:
    """Find assumptions that may contradict each other."""
    _OPPOSITES = {
        "oil_energy": "consumer_strength",  # High oil hurts consumers
        "interest_rates": "cloud_growth",    # High rates hurt growth
    }
    contradictions = []
    ticker_themes: dict[str, set[str]] = {}
    for a in assumptions:
        tk = str(a.get("ticker", ""))
        text = str(a.get("assumption_text", "")).lower()
        themes = _extract_themes(text)
        ticker_themes.setdefault(tk, set()).update(themes)

    # Check if portfolio contains opposing themes
    all_themes = set()
    for themes in ticker_themes.values():
        all_themes.update(themes)

    for t1, opposite in _OPPOSITES.items():
        if t1 in all_themes and opposite in all_themes:
            t1_tickers = [tk for tk, th in ticker_themes.items() if t1 in th]
            t2_tickers = [tk for tk, th in ticker_themes.items() if opposite in th]
            contradictions.append({
                "theme_1": t1,
                "theme_2": opposite,
                "tickers_1": t1_tickers,
                "tickers_2": t2_tickers,
                "note": f"'{t1}' assumption conflicts with '{opposite}' assumption across positions",
            })

    return contradictions


def get_coherence_map() -> list[dict]:
    """Retrieve stored coherence results."""
    return _q("SELECT * FROM shared_assumptions_core ORDER BY concentration DESC")


# ═══════════════════════════════════════════════════════════════════════════════
# FEATURE 5: LIVING SYNTHESIS
# ═══════════════════════════════════════════════════════════════════════════════

def get_synthesis(ticker: str) -> dict | None:
    rows = _q(
        "SELECT * FROM ticker_synthesis_core WHERE UPPER(ticker) = %s",
        (ticker.strip().upper(),),
    )
    return rows[0] if rows else None


def _compute_data_hash(ticker: str) -> str:
    """Hash of key data to detect when synthesis needs refresh."""
    tk = ticker.strip().upper()
    parts = []
    # Count of proposals, filings, earnings, records, assumptions
    for table, col in [
        ("action_proposals_core", "ticker"),
        ("sec_edgar_poll_state_core", "ticker"),
        ("earnings_analysis_core", "ticker"),
        ("investment_records_core", "ticker"),
        ("position_assumptions_core", "ticker"),
    ]:
        rows = _q(f"SELECT COUNT(*) AS cnt FROM {table} WHERE UPPER({col}) = %s", (tk,))
        parts.append(str(rows[0].get("cnt", 0)) if rows else "0")

    # Latest conviction score
    conv = get_conviction(tk)
    parts.append(str(conv.get("score", 0)) if conv else "0")

    return hashlib.md5("|".join(parts).encode()).hexdigest()


def generate_synthesis(ticker: str) -> dict:
    """Generate or refresh a living synthesis for a ticker using LLM."""
    tk = ticker.strip().upper()
    new_hash = _compute_data_hash(tk)

    existing = get_synthesis(tk)
    if existing and existing.get("data_hash") == new_hash and not existing.get("stale"):
        return existing  # No material change

    # Gather context for synthesis
    from app.services.ticker_hub_service import get_ticker_hub
    hub = get_ticker_hub(tk)

    # Build synthesis prompt context
    sections: dict[str, str] = {}

    # Position
    pos = hub.get("position")
    if pos:
        sections["position"] = f"Holds {pos.get('shares', 0)} shares at avg ${pos.get('avg_cost', 0)}"

    # Assumptions
    assumptions = list_assumptions(ticker=tk)
    if assumptions:
        parts = []
        for a in assumptions[:8]:
            status_icon = "BREACHED" if a.get("status") == "breached" else "OK"
            parts.append(f"[{status_icon}] {a.get('assumption_text', '')}")
        sections["assumptions"] = "\n".join(parts)

    # Conviction
    conv = get_conviction(tk)
    if conv:
        sections["conviction"] = f"Score: {conv.get('score', 50)}/100"

    # Recent proposals
    proposals = (hub.get("proposals") or [])[:5]
    if proposals:
        parts = [f"- {p.get('headline', p.get('action_type', 'Proposal'))} (conf: {p.get('confidence', 0)}, status: {p.get('status', '')})" for p in proposals]
        sections["recent_signals"] = "\n".join(parts)

    # Earnings
    earnings = (hub.get("earnings") or [])[:3]
    if earnings:
        parts = [f"- {e.get('quarter', '')} {e.get('year', '')}: guidance {e.get('guidance_direction', 'n/a')}" for e in earnings]
        sections["earnings_trend"] = "\n".join(parts)

    # Research summary
    threads = (hub.get("research_threads") or [])[:3]
    if threads:
        parts = [f"- {t.get('title', 'Thread')} ({t.get('status', '')})" for t in threads]
        sections["active_research"] = "\n".join(parts)

    # Build synthesis text (non-LLM version — structured summary)
    synthesis_parts = [f"# ${tk} — Living Synthesis", f"*Generated: {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}*", ""]

    if sections.get("position"):
        synthesis_parts.append(f"**Position:** {sections['position']}")
    if sections.get("conviction"):
        synthesis_parts.append(f"**Conviction:** {sections['conviction']}")
    if sections.get("assumptions"):
        synthesis_parts.append(f"\n**Key Assumptions:**\n{sections['assumptions']}")
    if sections.get("earnings_trend"):
        synthesis_parts.append(f"\n**Earnings Trend:**\n{sections['earnings_trend']}")
    if sections.get("recent_signals"):
        synthesis_parts.append(f"\n**Recent AI Signals:**\n{sections['recent_signals']}")
    if sections.get("active_research"):
        synthesis_parts.append(f"\n**Active Research:**\n{sections['active_research']}")

    # Breached assumptions warning
    breached = [a for a in assumptions if a.get("status") == "breached"]
    if breached:
        synthesis_parts.append(f"\n**WARNINGS:** {len(breached)} assumption(s) breached:")
        for b in breached:
            synthesis_parts.append(f"- {b.get('assumption_text', '')}")

    synthesis_text = "\n".join(synthesis_parts)

    # Store
    if existing:
        _exec(
            "UPDATE ticker_synthesis_core SET synthesis_text = %s, data_hash = %s, "
            "section_scores = %s, generated_at = NOW(), stale = FALSE WHERE UPPER(ticker) = %s",
            (synthesis_text, new_hash, json.dumps(sections), tk),
        )
    else:
        _exec(
            "INSERT INTO ticker_synthesis_core (ticker, synthesis_text, data_hash, section_scores) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (ticker) DO UPDATE SET "
            "synthesis_text = EXCLUDED.synthesis_text, data_hash = EXCLUDED.data_hash, "
            "section_scores = EXCLUDED.section_scores, generated_at = NOW(), stale = FALSE",
            (tk, synthesis_text, new_hash, json.dumps(sections)),
        )

    return {"ticker": tk, "synthesis_text": synthesis_text, "data_hash": new_hash,
            "section_scores": sections, "generated_at": dt.datetime.now().isoformat()}


def mark_synthesis_stale(ticker: str) -> bool:
    return _exec(
        "UPDATE ticker_synthesis_core SET stale = TRUE WHERE UPPER(ticker) = %s",
        (ticker.strip().upper(),),
    )


def refresh_all_syntheses() -> int:
    """Regenerate syntheses for all held tickers. Returns count refreshed."""
    count = 0
    try:
        tickers = _q("SELECT DISTINCT UPPER(ticker) AS ticker FROM portfolio_positions_core")
        for row in tickers:
            tk = str(row.get("ticker") or "").strip()
            if tk:
                generate_synthesis(tk)
                count += 1
    except Exception as exc:
        LOGGER.debug("refresh_all_syntheses failed: %s", exc)
    return count


# ═══════════════════════════════════════════════════════════════════════════════
# FEATURE 6: SIGNAL PREFERENCE LEARNING
# ═══════════════════════════════════════════════════════════════════════════════

def record_signal_action(signal_type: str, action: str, signal_id: int | None = None, ticker: str = "") -> bool:
    """Record a user action on a signal (shown/acted/dismissed)."""
    if not signal_type or action not in ("shown", "acted", "dismissed"):
        return False

    # Log individual action
    _exec(
        "INSERT INTO signal_action_log_core (signal_type, signal_id, action, ticker) VALUES (%s,%s,%s,%s)",
        (signal_type, signal_id, action, ticker.strip().upper()),
    )

    # Update aggregate
    if action == "shown":
        _exec(
            "INSERT INTO signal_preferences_core (signal_type, total_shown) VALUES (%s, 1) "
            "ON CONFLICT (signal_type) DO UPDATE SET total_shown = signal_preferences_core.total_shown + 1, "
            "updated_at = NOW()",
            (signal_type,),
        )
    elif action == "acted":
        _exec(
            "INSERT INTO signal_preferences_core (signal_type, total_acted) VALUES (%s, 1) "
            "ON CONFLICT (signal_type) DO UPDATE SET total_acted = signal_preferences_core.total_acted + 1, "
            "updated_at = NOW()",
            (signal_type,),
        )
    elif action == "dismissed":
        _exec(
            "INSERT INTO signal_preferences_core (signal_type, total_dismissed) VALUES (%s, 1) "
            "ON CONFLICT (signal_type) DO UPDATE SET total_dismissed = signal_preferences_core.total_dismissed + 1, "
            "updated_at = NOW()",
            (signal_type,),
        )

    # Recompute act_ratio and weight
    _recompute_signal_weight(signal_type)
    return True


def _recompute_signal_weight(signal_type: str) -> None:
    """Recompute act_ratio and weight multiplier for a signal type."""
    rows = _q("SELECT * FROM signal_preferences_core WHERE signal_type = %s", (signal_type,))
    if not rows:
        return
    r = rows[0]
    shown = int(r.get("total_shown", 0))
    acted = int(r.get("total_acted", 0))
    dismissed = int(r.get("total_dismissed", 0))

    # Act ratio: proportion of shown signals that were acted upon
    total_decisions = acted + dismissed
    if total_decisions > 0:
        act_ratio = round(acted / total_decisions, 3)
    else:
        act_ratio = 0.5  # neutral default

    # Weight multiplier: signals with high act ratio get boosted, low get suppressed
    # Range: 0.3 (heavily dismissed) to 2.0 (always acted upon)
    # Requires at least 5 decisions before adjusting
    if total_decisions >= 5:
        weight = max(0.3, min(2.0, 0.5 + act_ratio * 1.5))
    else:
        weight = 1.0

    _exec(
        "UPDATE signal_preferences_core SET act_ratio = %s, weight_mult = %s, updated_at = NOW() "
        "WHERE signal_type = %s",
        (act_ratio, round(weight, 2), signal_type),
    )


def get_signal_preferences(limit: int = 30) -> list[dict]:
    return _q("SELECT * FROM signal_preferences_core ORDER BY updated_at DESC LIMIT %s", (limit,))


def get_signal_weight(signal_type: str) -> float:
    """Get the weight multiplier for a signal type. Returns 1.0 if unknown."""
    rows = _q("SELECT weight_mult FROM signal_preferences_core WHERE signal_type = %s", (signal_type,))
    return float(rows[0].get("weight_mult", 1.0)) if rows else 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# MASTER SWEEP — runs all cognitive features
# ═══════════════════════════════════════════════════════════════════════════════

def run_cognitive_sweep() -> dict:
    """Run all cognitive engine features. Called by background thread."""
    results: dict[str, Any] = {}

    try:
        results["assumptions_breached"] = check_assumptions()
    except Exception as exc:
        LOGGER.debug("check_assumptions failed: %s", exc)
        results["assumptions_breached"] = -1

    try:
        results["triggers_fired"] = check_triggers()
    except Exception as exc:
        LOGGER.debug("check_triggers failed: %s", exc)
        results["triggers_fired"] = -1

    try:
        conviction_results = update_all_convictions(trigger_event="cognitive_sweep")
        results["convictions_updated"] = len(conviction_results)
        moved = [r for r in conviction_results if abs(r.get("delta", 0)) >= 5]
        results["conviction_moves"] = len(moved)
    except Exception as exc:
        LOGGER.debug("update_all_convictions failed: %s", exc)
        results["convictions_updated"] = -1

    try:
        coherence = compute_portfolio_coherence()
        results["shared_themes"] = len(coherence.get("themes", []))
        results["concentration_risks"] = len(coherence.get("concentration_risks", []))
        results["contradictions"] = len(coherence.get("contradictions", []))
    except Exception as exc:
        LOGGER.debug("compute_portfolio_coherence failed: %s", exc)
        results["shared_themes"] = -1

    try:
        results["syntheses_refreshed"] = refresh_all_syntheses()
    except Exception as exc:
        LOGGER.debug("refresh_all_syntheses failed: %s", exc)
        results["syntheses_refreshed"] = -1

    LOGGER.info("cognitive_sweep results: %s", results)
    return results
