from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
from typing import Any

from app.core.config import ROOT
from app.core.db import core_conn as _conn, sqlite_retry
from app.core.date import parse_datetime_flexible
from app.core.ticker import safe_ticker as _safe_ticker
from app.services.postgres_core_service import (
    list_investor_style_memory_pg,
    list_watchlist_thesis_pg,
    pg_connect,
    pg_enabled,
    strict_postgres_mode,
    upsert_investor_style_memory_pg,
    upsert_watchlist_thesis_pg,
)
from app.services.watchlist_service import read_watchlist_tickers
from tools.llm_engine import ask_ai


SYSTEM_PROMPT = """You are an elite, highly conversational investment Chief of Staff. You are chatting casually with your boss (the user).
Review their current portfolio, active theses, the rules you have learned about them so far, their recent trading activity, and what they just added to their watchlist.
Find ONE blind spot, risk, or strategic shift in their profile.

Your task is to ask ONE natural, conversational question to help uncover this missing context.
- DO NOT sound like a robot, a survey, or a customer service agent.
- DO NOT use overly formal jargon.
- Keep it to 1-2 sentences maximum.

Here are examples of the exact tone and style you should use depending on what you find in the data:

SCENARIO A: The user is heavily concentrated in one sector.
GOOD: 'Hey, I notice we hold 40% in UNH right now. Are we comfortable with that much Healthcare exposure, or should we set a max sector limit?'
BAD: 'Please define your maximum risk tolerance for the Healthcare sector.'

SCENARIO B: The user holds very few total stocks.
GOOD: 'I see we only have 3 stocks in the portfolio right now. Are we running a highly concentrated conviction strategy, or just waiting for better setups to deploy cash?'
BAD: 'Please confirm if your strategy is classified as Value Investor or Concentrated.'

SCENARIO C: The user bought a stock that immediately dropped.
GOOD: 'Looks like we caught a falling knife with that recent AMD buy. Was the thesis to buy the dip, or did we just mistime the entry?'
BAD: 'Transaction logged. Please explain the negative variance on your recent AMD purchase.'

SCENARIO D: The user hasn't made a trade in months.
GOOD: 'I notice we haven't added anything to the portfolio in the last 3 months. Are we strictly holding long-term right now, or what would actually change your thesis to make you buy more of [Top Holding]?'
BAD: 'System detects 90 days of trading inactivity. Please confirm if your strategy is Long Term.'

SCENARIO E: The user's portfolio significantly outperformed the S&P 500 over the last 6 months.
GOOD: 'We’ve crushed the S&P 500 over the last 6 months. The market mostly rode the AI wave, but what do you think gave our portfolio that extra edge? Was it our Industrials, or just better stock picking?'
BAD: 'Your portfolio alpha is positive against the S&P 500 benchmark. Please specify the factors contributing to your outperformance.'

SCENARIO F: The user adds a stock to their watchlist from a sector they don't currently own.
GOOD: 'I see you just added Progressive (PGR) to the watchlist, but we don't own any insurance names right now. What caught your eye—are they trading at a rare valuation discount, or is it a play on rising premiums?'
BAD: 'You have added a Financials sector asset to your watchlist. Please input your thesis for this sector.'

If their profile is perfectly airtight with no blind spots or missing context, return the exact word 'NONE'."""


def ensure_daily_operator_schema() -> None:
    def _write() -> None:
        con = _conn()
        try:
            con.execute(
                """CREATE TABLE IF NOT EXISTS daily_operator_state (
                    state_key TEXT PRIMARY KEY,
                    state_value TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )"""
            )
            con.execute(
                """CREATE TABLE IF NOT EXISTS daily_operator_gap_prompts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    prompt_id TEXT NOT NULL UNIQUE,
                    user_name TEXT NOT NULL DEFAULT '',
                    prompt_text TEXT NOT NULL DEFAULT '',
                    context_json TEXT NOT NULL DEFAULT '{}',
                    context_fingerprint TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'open',
                    answered_at TEXT NOT NULL DEFAULT '',
                    answer_text TEXT NOT NULL DEFAULT '',
                    resolved_json TEXT NOT NULL DEFAULT '{}'
                )"""
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_daily_gap_status ON daily_operator_gap_prompts(status, id DESC)")
            con.execute(
                """CREATE TABLE IF NOT EXISTS daily_operator_gap_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    saved_count INTEGER NOT NULL DEFAULT 0,
                    prompt_id TEXT NOT NULL DEFAULT '',
                    details_json TEXT NOT NULL DEFAULT '{}'
                )"""
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_daily_gap_audit_created ON daily_operator_gap_audit(id DESC)")
            con.commit()
        finally:
            con.close()

    if not (pg_enabled() and strict_postgres_mode()):
        try:
            sqlite_retry(_write)
        except sqlite3.OperationalError:
            # Avoid blocking chat/dashboard paths under transient DB write locks.
            return
        except RuntimeError:
            # Strict-postgres guard can forbid sqlite in some runtime paths.
            pass
    if pg_enabled():
        con_pg = pg_connect()
        if con_pg is not None:
            try:
                cur = con_pg.cursor()
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS daily_operator_gap_audit_core (
                        id BIGSERIAL PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        reason TEXT NOT NULL DEFAULT '',
                        saved_count INTEGER NOT NULL DEFAULT 0,
                        prompt_id TEXT NOT NULL DEFAULT '',
                        details_json JSONB NOT NULL DEFAULT '{}'::jsonb
                    )
                    """
                )
                cur.execute("CREATE INDEX IF NOT EXISTS idx_daily_operator_gap_audit_core_created ON daily_operator_gap_audit_core(id DESC)")
                con_pg.commit()
            except Exception:
                try:
                    con_pg.rollback()
                except Exception:
                    pass
            finally:
                con_pg.close()


def _stale_hours() -> int:
    try:
        return max(1, int(str(os.environ.get("DAILY_GAP_STALE_HOURS", "48")).strip()))
    except Exception:
        return 48


def _state_get_sqlite(con: sqlite3.Connection, key: str) -> str:
    row = con.execute("SELECT state_value FROM daily_operator_state WHERE state_key=? LIMIT 1", (str(key),)).fetchone()
    return str((row[0] if row else "") or "")


def _state_set_sqlite(con: sqlite3.Connection, key: str, value: str) -> None:
    now = dt.datetime.now().isoformat()
    con.execute(
        "INSERT INTO daily_operator_state(state_key, state_value, updated_at) VALUES(?, ?, ?) "
        "ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value, updated_at=excluded.updated_at",
        (str(key), str(value or ""), now),
    )


def _state_get(key: str) -> str:
    if pg_enabled():
        con_pg = pg_connect()
        if con_pg is not None:
            try:
                cur = con_pg.cursor()
                cur.execute("SELECT state_value FROM daily_operator_state_core WHERE state_key=%s LIMIT 1", (str(key),))
                row = cur.fetchone()
                return str((row[0] if row else "") or "")
            except Exception:
                if strict_postgres_mode():
                    return ""
            finally:
                con_pg.close()
    con = _conn()
    try:
        return _state_get_sqlite(con, key)
    finally:
        con.close()


def _state_set(key: str, value: str) -> None:
    now = dt.datetime.now().isoformat()
    wrote_pg = False
    if pg_enabled():
        con_pg = pg_connect()
        if con_pg is not None:
            try:
                cur = con_pg.cursor()
                cur.execute(
                    "INSERT INTO daily_operator_state_core(state_key, state_value, updated_at) VALUES(%s, %s, %s) "
                    "ON CONFLICT(state_key) DO UPDATE SET state_value=EXCLUDED.state_value, updated_at=EXCLUDED.updated_at",
                    (str(key), str(value or ""), now),
                )
                con_pg.commit()
                wrote_pg = True
            except Exception:
                try:
                    con_pg.rollback()
                except Exception:
                    pass
                if strict_postgres_mode():
                    return
            finally:
                con_pg.close()
    if wrote_pg and strict_postgres_mode():
        return
    con = _conn()
    try:
        _state_set_sqlite(con, key, value)
        con.commit()
    finally:
        con.close()


def _expire_stale_open_prompts() -> None:
    cutoff_iso = (dt.datetime.now() - dt.timedelta(hours=_stale_hours())).isoformat()
    if pg_enabled():
        con_pg = pg_connect()
        if con_pg is not None:
            try:
                cur = con_pg.cursor()
                cur.execute(
                    "UPDATE daily_operator_gap_prompts_core "
                    "SET status='expired', updated_at=%s "
                    "WHERE status='open' AND created_at < %s",
                    (dt.datetime.now().isoformat(), cutoff_iso),
                )
                con_pg.commit()
            except Exception:
                try:
                    con_pg.rollback()
                except Exception:
                    pass
            finally:
                con_pg.close()
    if strict_postgres_mode() and pg_enabled():
        return
    con = _conn()
    try:
        con.execute(
            "UPDATE daily_operator_gap_prompts "
            "SET status='expired', updated_at=? "
            "WHERE status='open' AND created_at < ?",
            (dt.datetime.now().isoformat(), cutoff_iso),
        )
        con.commit()
    finally:
        con.close()


def _insert_open_prompt(prompt_id: str, user_name: str, prompt_text: str, context_json: str, context_fingerprint: str) -> None:
    now = dt.datetime.now().isoformat()
    _expire_stale_open_prompts()
    wrote_pg = False
    if pg_enabled():
        con_pg = pg_connect()
        if con_pg is not None:
            try:
                cur = con_pg.cursor()
                cur.execute(
                    "INSERT INTO daily_operator_gap_prompts_core "
                    "(created_at, updated_at, prompt_id, user_name, prompt_text, context_json, context_fingerprint, status, answered_at, answer_text, resolved_json) "
                    "VALUES(%s, %s, %s, %s, %s, %s::jsonb, %s, 'open', '', '', '{}'::jsonb) "
                    "ON CONFLICT (prompt_id) DO UPDATE SET "
                    "updated_at=EXCLUDED.updated_at, user_name=EXCLUDED.user_name, prompt_text=EXCLUDED.prompt_text, "
                    "context_json=EXCLUDED.context_json, context_fingerprint=EXCLUDED.context_fingerprint, "
                    "status='open', answered_at='', answer_text='', resolved_json='{}'::jsonb",
                    (now, now, prompt_id, user_name, prompt_text, context_json, context_fingerprint),
                )
                con_pg.commit()
                wrote_pg = True
            except Exception:
                try:
                    con_pg.rollback()
                except Exception:
                    pass
                if strict_postgres_mode():
                    return
            finally:
                con_pg.close()
    if wrote_pg and strict_postgres_mode():
        return
    con = _conn()
    try:
        con.execute(
            "INSERT OR REPLACE INTO daily_operator_gap_prompts "
            "(created_at, updated_at, prompt_id, user_name, prompt_text, context_json, context_fingerprint, status, answered_at, answer_text, resolved_json) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, 'open', '', '', '{}')",
            (now, now, prompt_id, user_name, prompt_text, context_json, context_fingerprint),
        )
        con.commit()
    finally:
        con.close()


def _latest_open_prompt() -> dict[str, Any]:
    _expire_stale_open_prompts()
    if pg_enabled():
        con_pg = pg_connect()
        if con_pg is not None:
            try:
                cur = con_pg.cursor()
                cur.execute(
                    "SELECT id, prompt_id, prompt_text, context_json FROM daily_operator_gap_prompts_core "
                    "WHERE status='open' ORDER BY id DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row:
                    return {
                        "id": int(row[0] or 0),
                        "prompt_id": str(row[1] or ""),
                        "prompt_text": str(row[2] or ""),
                        "context_json": str(row[3] or "{}"),
                    }
                return {}
            except Exception:
                if strict_postgres_mode():
                    return {}
            finally:
                con_pg.close()
    con = _conn()
    try:
        row = con.execute(
            """SELECT id, prompt_id, prompt_text, context_json
               FROM daily_operator_gap_prompts
               WHERE status='open'
               ORDER BY id DESC
               LIMIT 1"""
        ).fetchone()
        if not row:
            return {}
        return {
            "id": int(row["id"] or 0),
            "prompt_id": str(row["prompt_id"] or ""),
            "prompt_text": str(row["prompt_text"] or ""),
            "context_json": str(row["context_json"] or "{}"),
        }
    finally:
        con.close()


def _mark_prompt_answered(row_id: int, answer_text: str, resolved_obj: dict[str, Any]) -> None:
    now = dt.datetime.now().isoformat()
    payload = json.dumps(resolved_obj or {}, ensure_ascii=True)
    wrote_pg = False
    if pg_enabled():
        con_pg = pg_connect()
        if con_pg is not None:
            try:
                cur = con_pg.cursor()
                cur.execute(
                    "UPDATE daily_operator_gap_prompts_core "
                    "SET status='answered', answered_at=%s, answer_text=%s, resolved_json=%s::jsonb, updated_at=%s "
                    "WHERE id=%s",
                    (now, str(answer_text or "")[:4000], payload, now, int(row_id or 0)),
                )
                con_pg.commit()
                wrote_pg = True
            except Exception:
                try:
                    con_pg.rollback()
                except Exception:
                    pass
                if strict_postgres_mode():
                    return
            finally:
                con_pg.close()
    if wrote_pg and strict_postgres_mode():
        return
    con = _conn()
    try:
        con.execute(
            "UPDATE daily_operator_gap_prompts SET status='answered', answered_at=?, answer_text=?, resolved_json=?, updated_at=? WHERE id=?",
            (now, str(answer_text or "")[:4000], payload, now, int(row_id or 0)),
        )
        con.commit()
    finally:
        con.close()


def _log_gap_audit(reason: str, saved_count: int = 0, prompt_id: str = "", details: dict[str, Any] | None = None) -> None:
    now = dt.datetime.now().isoformat()
    detail_json = json.dumps(details or {}, ensure_ascii=True)
    if pg_enabled():
        con_pg = pg_connect()
        if con_pg is not None:
            try:
                cur = con_pg.cursor()
                cur.execute(
                    "INSERT INTO daily_operator_gap_audit_core(created_at, reason, saved_count, prompt_id, details_json) "
                    "VALUES(%s, %s, %s, %s, %s::jsonb)",
                    (now, str(reason or "")[:80], int(saved_count or 0), str(prompt_id or "")[:120], detail_json),
                )
                con_pg.commit()
            except Exception:
                try:
                    con_pg.rollback()
                except Exception:
                    pass
            finally:
                con_pg.close()
    if strict_postgres_mode() and pg_enabled():
        return
    con = _conn()
    try:
        con.execute(
            "INSERT INTO daily_operator_gap_audit(created_at, reason, saved_count, prompt_id, details_json) VALUES(?, ?, ?, ?, ?)",
            (now, str(reason or "")[:80], int(saved_count or 0), str(prompt_id or "")[:120], detail_json),
        )
        con.commit()
    finally:
        con.close()


def _read_portfolio_csv() -> list[dict[str, Any]]:
    p = ROOT / "data" / "portfolio.csv"
    if not p.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            parts = [x.strip() for x in str(ln or "").split(",")]
            tk = _safe_ticker(parts[0] if parts else "")
            if not tk:
                continue
            sh = 0.0
            cost = 0.0
            try:
                sh = float(parts[1] if len(parts) > 1 else 0.0)
            except Exception:
                sh = 0.0
            try:
                cost = float(parts[2] if len(parts) > 2 else 0.0)
            except Exception:
                cost = 0.0
            out.append({"ticker": tk, "shares": sh, "cost": cost})
    except Exception:
        return []
    return out


def _six_month_perf_vs_benchmark(portfolio_rows: list[dict[str, Any]], benchmark: str = "SPY") -> str:
    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return "unavailable"
    if not portfolio_rows:
        return "unavailable"
    weights: dict[str, float] = {}
    total = 0.0
    for r in portfolio_rows:
        tk = _safe_ticker(str(r.get("ticker") or ""))
        sh = float(r.get("shares") or 0.0)
        cost = float(r.get("cost") or 0.0)
        val = max(0.0, sh * max(0.0, cost))
        if not tk or val <= 0:
            continue
        weights[tk] = weights.get(tk, 0.0) + val
        total += val
    if total <= 0:
        return "unavailable"
    six_month_port = 0.0
    for tk, val in weights.items():
        try:
            hist = yf.Ticker(tk).history(period="6mo", interval="1d")
            if hist is None or len(hist.index) < 2:
                continue
            p0 = float(hist["Close"].iloc[0] or 0.0)
            p1 = float(hist["Close"].iloc[-1] or 0.0)
            if p0 <= 0:
                continue
            rtn = (p1 - p0) / p0
            six_month_port += (val / total) * rtn
        except Exception:
            continue
    try:
        bh = yf.Ticker(str(benchmark)).history(period="6mo", interval="1d")
        b0 = float(bh["Close"].iloc[0] or 0.0)
        b1 = float(bh["Close"].iloc[-1] or 0.0)
        if b0 <= 0:
            return "unavailable"
        b_rtn = (b1 - b0) / b0
    except Exception:
        return "unavailable"
    alpha = six_month_port - b_rtn
    return f"portfolio_6m={six_month_port*100:.2f}% benchmark_6m={b_rtn*100:.2f}% alpha={alpha*100:+.2f}%"


def _build_context_payload() -> dict[str, Any]:
    ensure_daily_operator_schema()
    portfolio_rows = _read_portfolio_csv()
    watchlist_file = read_watchlist_tickers()
    if pg_enabled():
        try:
            wt_rows = list_watchlist_thesis_pg(limit=300)
            style_rows = list_investor_style_memory_pg(limit=300)
            tx_recent = []
            tx_count = 0
            tx_tickers = set()
            tx_min = ""
            tx_max = ""
            from app.services.portfolio_memory_service import list_portfolio_transactions
            tx_rows = list_portfolio_transactions(limit=2000, ticker="", year=0)
            for r in tx_rows:
                tx_count += 1
                tk = _safe_ticker(str(r.get("ticker") or ""))
                if tk:
                    tx_tickers.add(tk)
                ca = str(r.get("created_at") or "")
                if ca:
                    if not tx_min or ca < tx_min:
                        tx_min = ca
                    if not tx_max or ca > tx_max:
                        tx_max = ca
                if len(tx_recent) < 20:
                    tx_recent.append(
                        {
                            "created_at": ca,
                            "ticker": tk,
                            "action": str(r.get("action") or "").lower()[:12],
                            "shares": float(r.get("shares") or 0.0),
                            "price": float(r.get("price") or 0.0),
                        }
                    )
            days_since_trade = "unknown"
            if tx_max:
                t0 = parse_datetime_flexible(str(tx_max))
                if t0 is not None:
                    days_since_trade = int(max(0.0, (dt.datetime.now() - t0).total_seconds() / 86400.0))
                else:
                    days_since_trade = "unknown"
            watchlist_rows = []
            for r in wt_rows:
                tk = _safe_ticker(str((r or {}).get("ticker") or ""))
                if not tk:
                    continue
                watchlist_rows.append(
                    {
                        "ticker": tk,
                        "thesis_summary": str((r or {}).get("thesis_summary") or "")[:260],
                        "time_horizon": str((r or {}).get("time_horizon") or "")[:64],
                        "invalidation_criteria": str((r or {}).get("invalidation_criteria") or "")[:260],
                        "strategy_tag": str((r or {}).get("strategy_tag") or "")[:32],
                        "created_at": str((r or {}).get("created_at") or ""),
                        "updated_at": str((r or {}).get("updated_at") or ""),
                    }
                )
            added_recent = []
            for r in sorted(watchlist_rows, key=lambda x: str(x.get("created_at") or ""), reverse=True):
                tk = str(r.get("ticker") or "")
                if tk and tk not in added_recent:
                    added_recent.append(tk)
                if len(added_recent) >= 8:
                    break
            style_mem = [
                {
                    "key": str((r or {}).get("key") or "")[:80],
                    "answer": str((r or {}).get("answer") or "")[:300],
                    "updated_at": str((r or {}).get("updated_at") or ""),
                }
                for r in style_rows
                if str((r or {}).get("key") or "").strip() and str((r or {}).get("answer") or "").strip()
            ]
            portfolio_live = {"holdings_count": len(portfolio_rows), "positions": portfolio_rows[:80]}
            return {
                "portfolio_live": portfolio_live,
                "portfolio_transaction_history": {
                    "transactions_count": tx_count,
                    "distinct_tickers_traded": len(tx_tickers),
                    "first_trade_at": tx_min,
                    "last_trade_at": tx_max,
                    "recent_trades": tx_recent[:12],
                },
                "watchlist_thesis": watchlist_rows[:120],
                "investor_style_memory": style_mem[:120],
                "days_since_last_trade": days_since_trade,
                "six_month_performance_vs_benchmark": _six_month_perf_vs_benchmark(portfolio_rows, benchmark="SPY"),
                "recently_added_to_watchlist": added_recent if added_recent else watchlist_file[:8],
            }
        except Exception:
            if strict_postgres_mode():
                return {
                    "portfolio_live": {"holdings_count": len(portfolio_rows), "positions": portfolio_rows[:80]},
                    "portfolio_transaction_history": {
                        "transactions_count": 0,
                        "distinct_tickers_traded": 0,
                        "first_trade_at": "",
                        "last_trade_at": "",
                        "recent_trades": [],
                    },
                    "watchlist_thesis": [],
                    "investor_style_memory": [],
                    "days_since_last_trade": "unknown",
                    "six_month_performance_vs_benchmark": "unavailable",
                    "recently_added_to_watchlist": watchlist_file[:8],
                }
    con = _conn()
    try:
        wt_rows = con.execute(
            """SELECT ticker, COALESCE(thesis_summary,'') AS thesis_summary, COALESCE(time_horizon,'') AS time_horizon,
                      COALESCE(invalidation_criteria,'') AS invalidation_criteria, COALESCE(strategy_tag,'') AS strategy_tag,
                      COALESCE(created_at,'') AS created_at, COALESCE(updated_at,'') AS updated_at
               FROM watchlist_thesis
               ORDER BY updated_at DESC
               LIMIT 300"""
        ).fetchall()
        style_rows = con.execute(
            """SELECT COALESCE(key,'') AS key, COALESCE(answer,'') AS answer, COALESCE(updated_at,'') AS updated_at
               FROM investor_style_memory
               ORDER BY updated_at DESC
               LIMIT 300"""
        ).fetchall()
        tx = con.execute(
            "SELECT COALESCE(created_at,'') AS created_at FROM portfolio_transactions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        tx_stats = con.execute(
            """SELECT
                 COUNT(*) AS cnt,
                 COUNT(DISTINCT COALESCE(ticker,'')) AS ticker_cnt,
                 COALESCE(MIN(created_at),'') AS min_ts,
                 COALESCE(MAX(created_at),'') AS max_ts
               FROM portfolio_transactions"""
        ).fetchone()
        tx_recent_rows = con.execute(
            """SELECT COALESCE(created_at,'') AS created_at, COALESCE(ticker,'') AS ticker,
                      COALESCE(action,'') AS action, COALESCE(shares,0) AS shares, COALESCE(price,0) AS price
               FROM portfolio_transactions
               ORDER BY id DESC
               LIMIT 20"""
        ).fetchall()
        days_since_trade = None
        if tx and str(tx["created_at"] or "").strip():
            t0 = parse_datetime_flexible(str(tx["created_at"]))
            if t0 is not None:
                days_since_trade = int(max(0.0, (dt.datetime.now() - t0).total_seconds() / 86400.0))
            else:
                days_since_trade = None
        tx_recent = [
            {
                "created_at": str(r["created_at"] or ""),
                "ticker": _safe_ticker(str(r["ticker"] or "")),
                "action": str(r["action"] or "").lower()[:12],
                "shares": float(r["shares"] or 0.0),
                "price": float(r["price"] or 0.0),
            }
            for r in tx_recent_rows
            if _safe_ticker(str(r["ticker"] or ""))
        ]
        watchlist_rows = []
        for r in wt_rows:
            tk = _safe_ticker(str(r["ticker"] or ""))
            if not tk:
                continue
            watchlist_rows.append(
                {
                    "ticker": tk,
                    "thesis_summary": str(r["thesis_summary"] or "")[:260],
                    "time_horizon": str(r["time_horizon"] or "")[:64],
                    "invalidation_criteria": str(r["invalidation_criteria"] or "")[:260],
                    "strategy_tag": str(r["strategy_tag"] or "")[:32],
                    "created_at": str(r["created_at"] or ""),
                    "updated_at": str(r["updated_at"] or ""),
                }
            )
        added_recent = []
        for r in sorted(watchlist_rows, key=lambda x: str(x.get("created_at") or ""), reverse=True):
            tk = str(r.get("ticker") or "")
            if tk and tk not in added_recent:
                added_recent.append(tk)
            if len(added_recent) >= 8:
                break
        style_mem = [
            {"key": str(r["key"] or "")[:80], "answer": str(r["answer"] or "")[:300], "updated_at": str(r["updated_at"] or "")}
            for r in style_rows
            if str(r["key"] or "").strip() and str(r["answer"] or "").strip()
        ]
        portfolio_live = {"holdings_count": len(portfolio_rows), "positions": portfolio_rows[:80]}
        return {
            "portfolio_live": portfolio_live,
            "portfolio_transaction_history": {
                "transactions_count": int((tx_stats["cnt"] if tx_stats else 0) or 0),
                "distinct_tickers_traded": int((tx_stats["ticker_cnt"] if tx_stats else 0) or 0),
                "first_trade_at": str((tx_stats["min_ts"] if tx_stats else "") or ""),
                "last_trade_at": str((tx_stats["max_ts"] if tx_stats else "") or ""),
                "recent_trades": tx_recent[:12],
            },
            "watchlist_thesis": watchlist_rows[:120],
            "investor_style_memory": style_mem[:120],
            "days_since_last_trade": days_since_trade if days_since_trade is not None else "unknown",
            "six_month_performance_vs_benchmark": _six_month_perf_vs_benchmark(portfolio_rows, benchmark="SPY"),
            "recently_added_to_watchlist": added_recent if added_recent else watchlist_file[:8],
        }
    finally:
        con.close()


def _context_fingerprint(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()


def _clean_question(txt: str) -> str:
    t = str(txt or "").strip()
    t = re.sub(r"^\s*['\"`]+|['\"`]+\s*$", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > 420:
        t = t[:420].rstrip() + "…"
    return t


def get_dynamic_gap_prompt(user_name: str = "Arda") -> dict[str, Any]:
    ensure_daily_operator_schema()
    name = str(user_name or "").strip() or "Arda"
    payload = _build_context_payload()
    fp = _context_fingerprint(payload)
    today = dt.date.today().isoformat()
    try:
        last_fp = _state_get("daily_gap_context_fp")
        last_day = _state_get("daily_gap_prompt_day")
        should_ask = bool(last_fp != fp or last_day != today)
        if not should_ask:
            return {"ok": True, "needs_attention": False, "prompt_id": "", "message": "", "kind": "none"}
        user_prompt = (
            "Review this live user context and return exactly one conversational question (1-2 sentences), "
            "or 'NONE' if no blind spot exists.\n\n"
            + json.dumps(payload, ensure_ascii=True, sort_keys=True)
        )
        txt = str(
            ask_ai(
                user_prompt,
                SYSTEM_PROMPT,
                mode="smart",
                json_mode=False,
                temperature=1.0,
            )
            or ""
        ).strip()
        q = _clean_question(txt)
        _state_set("daily_gap_context_fp", fp)
        _state_set("daily_gap_prompt_day", today)
        if not q or q.upper() == "NONE":
            return {"ok": True, "needs_attention": False, "prompt_id": "", "message": "", "kind": "none"}
        prompt_id = f"gap:{today}:{fp[:12]}"
        _insert_open_prompt(
            prompt_id=prompt_id,
            user_name=name,
            prompt_text=q,
            context_json=json.dumps(payload, ensure_ascii=True),
            context_fingerprint=fp,
        )
        return {
            "ok": True,
            "needs_attention": True,
            "prompt_id": prompt_id,
            "message": q,
            "kind": "proactive_gap_check",
        }
    except Exception:
        return {"ok": True, "needs_attention": False, "prompt_id": "", "message": "", "kind": "none"}


def _upsert_style_kv(con: sqlite3.Connection | None, key: str, value: str) -> bool:
    k = str(key or "").strip().lower()[:80]
    v = str(value or "").strip()[:2000]
    if not k or not v:
        return False
    now = dt.datetime.now().isoformat()
    ok_sql = True
    if not strict_postgres_mode():
        own_con = False
        con_sql = con
        if con_sql is None:
            con_sql = _conn()
            own_con = True
        try:
            row = con_sql.execute("SELECT key FROM investor_style_memory WHERE key=? LIMIT 1", (k,)).fetchone()
            if row:
                con_sql.execute("UPDATE investor_style_memory SET answer=?, updated_at=? WHERE key=?", (v, now, k))
            else:
                con_sql.execute(
                    "INSERT INTO investor_style_memory (key, answer, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (k, v, now, now),
                )
            if own_con:
                con_sql.commit()
        finally:
            if own_con:
                con_sql.close()
    ok_pg = True
    if pg_enabled():
        ok_pg = bool(upsert_investor_style_memory_pg(k, v))
    return bool(ok_sql and ok_pg)


def _upsert_watchlist_kv(
    con: sqlite3.Connection | None,
    ticker: str,
    thesis_summary: str = "",
    time_horizon: str = "",
    invalidation_criteria: str = "",
    strategy_tag: str = "",
) -> bool:
    tk = _safe_ticker(ticker)
    if not tk:
        return False
    ts = str(thesis_summary or "").strip()[:2000]
    th = ts[:300]
    hz = str(time_horizon or "").strip()[:64]
    inv = str(invalidation_criteria or "").strip()[:1200]
    st = str(strategy_tag or "").strip().upper()[:32]
    if not (ts or hz or inv or st):
        return False
    now = dt.datetime.now().isoformat()
    ok_sql = True
    if not strict_postgres_mode():
        own_con = False
        con_sql = con
        if con_sql is None:
            con_sql = _conn()
            own_con = True
        try:
            row = con_sql.execute("SELECT ticker FROM watchlist_thesis WHERE ticker=? LIMIT 1", (tk,)).fetchone()
            if row:
                con_sql.execute(
                    """UPDATE watchlist_thesis
                       SET thesis=CASE WHEN ?<>'' THEN ? ELSE thesis END,
                           thesis_summary=CASE WHEN ?<>'' THEN ? ELSE thesis_summary END,
                           time_horizon=CASE WHEN ?<>'' THEN ? ELSE time_horizon END,
                           invalidation_criteria=CASE WHEN ?<>'' THEN ? ELSE invalidation_criteria END,
                           strategy_tag=CASE WHEN ?<>'' THEN ? ELSE strategy_tag END,
                           updated_at=?
                       WHERE ticker=?""",
                    (ts, ts, th, th, hz, hz, inv, inv, st, st, now, tk),
                )
            else:
                con_sql.execute(
                    """INSERT INTO watchlist_thesis
                       (ticker, thesis, thesis_summary, conviction_rating, time_horizon, invalidation_criteria,
                        pick_method, triggers, invalidation, strategy_tag, pattern_learnable, status, created_at, updated_at)
                       VALUES (?, ?, ?, 0, ?, ?, 'daily_operator', '', '', ?, 1, 'active', ?, ?)""",
                    (tk, ts, th, hz, inv, st or "CORE", now, now),
                )
            if own_con:
                con_sql.commit()
        finally:
            if own_con:
                con_sql.close()
    ok_pg = True
    if pg_enabled():
        ok_pg = bool(
            upsert_watchlist_thesis_pg(
                ticker=tk,
                thesis=ts,
                thesis_summary=th,
                time_horizon=hz,
                invalidation_criteria=inv,
                strategy_tag=(st or "CORE"),
                pattern_learnable=1,
                status="active",
            )
        )
    return bool(ok_sql and ok_pg)


def resolve_dynamic_gap_answer(user_text: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    ensure_daily_operator_schema()
    ans = str(user_text or "").strip()
    if len(ans) < 8:
        _log_gap_audit("too_short", saved_count=0, details={"len": len(ans)})
        return {"ok": True, "saved": 0, "skipped": "too_short"}
    ctx = dict(context or {})
    has_proactive_hint = False
    if str(ctx.get("last_intent") or "").strip().lower() == "proactive_gap_check":
        has_proactive_hint = True
    for s in list(ctx.get("recent_signals") or []):
        if isinstance(s, dict) and str(s.get("intent") or "").strip().lower() == "proactive_gap_check":
            has_proactive_hint = True
            break
    row = _latest_open_prompt()
    if not row and not has_proactive_hint:
        _log_gap_audit("no_open_gap_prompt", saved_count=0, details={"has_proactive_hint": False})
        return {"ok": True, "saved": 0, "skipped": "no_open_gap_prompt"}
    if row and not has_proactive_hint:
        _log_gap_audit("no_proactive_hint", saved_count=0, prompt_id=str(row.get("prompt_id") or ""))
        return {"ok": True, "saved": 0, "skipped": "no_proactive_hint"}
    prompt_text = str(row.get("prompt_text") or "").strip() if row else ""
    context_json = str(row.get("context_json") or "{}").strip() if row else ""
    if not context_json:
        context_json = "{}"
    if not prompt_text and has_proactive_hint:
        prompt_text = "No explicit prompt row found; parse for durable investing profile updates."
    con: sqlite3.Connection | None = None
    try:
        parser_prompt = (
            "Parse the user's answer into strict key-value updates for investment memory.\n"
            "Return STRICT JSON only with this schema:\n"
            '{"investor_style_memory":[{"key":"...","value":"..."}],'
            '"watchlist_thesis":[{"ticker":"...","thesis_summary":"...","time_horizon":"...","invalidation_criteria":"...","strategy_tag":"..."}],'
            '"should_store":true|false}\n'
            "Rules:\n"
            "- Use only explicit information from the user's answer.\n"
            "- Keep keys snake_case and concise.\n"
            "- If no durable rule/thesis is present, return should_store=false with empty arrays.\n\n"
            f"Prompt asked:\n{prompt_text}\n\nContext JSON:\n{context_json}\n\nUser answer:\n{ans}"
        )
        raw = str(
            ask_ai(
                parser_prompt,
                "You extract strict investment memory key-values. JSON only.",
                mode="smart",
                json_mode=True,
                temperature=1.0,
            )
            or ""
        ).strip()
        obj = json.loads(raw) if raw else {}
        if not isinstance(obj, dict):
            _log_gap_audit("bad_parser_output", saved_count=0, prompt_id=str(row.get("prompt_id") or ""), details={"raw_head": raw[:220]})
            return {"ok": True, "saved": 0, "skipped": "bad_parser_output"}
        prompt_id = str(row.get("prompt_id") or "")
        if not bool(obj.get("should_store")):
            if row and int(row.get("id") or 0) > 0:
                _mark_prompt_answered(int(row.get("id") or 0), ans, obj)
            _log_gap_audit("no_durable_updates", saved_count=0, prompt_id=prompt_id)
            return {"ok": True, "saved": 0, "skipped": "no_durable_updates"}
        saved = 0
        if not strict_postgres_mode():
            con = _conn()
        for it in list(obj.get("investor_style_memory") or []):
            if not isinstance(it, dict):
                continue
            if _upsert_style_kv(con, str(it.get("key") or ""), str(it.get("value") or "")):
                saved += 1
        for it in list(obj.get("watchlist_thesis") or []):
            if not isinstance(it, dict):
                continue
            if _upsert_watchlist_kv(
                con,
                ticker=str(it.get("ticker") or ""),
                thesis_summary=str(it.get("thesis_summary") or ""),
                time_horizon=str(it.get("time_horizon") or ""),
                invalidation_criteria=str(it.get("invalidation_criteria") or ""),
                strategy_tag=str(it.get("strategy_tag") or ""),
            ):
                saved += 1
        if con is not None:
            con.commit()
            con.close()
        if row and int(row.get("id") or 0) > 0:
            _mark_prompt_answered(int(row.get("id") or 0), ans, obj)
        _log_gap_audit("saved", saved_count=int(saved), prompt_id=prompt_id)
        return {"ok": True, "saved": int(saved), "prompt_id": prompt_id}
    except sqlite3.OperationalError:
        _log_gap_audit("db_locked", saved_count=0, prompt_id=str(row.get("prompt_id") or ""))
        return {"ok": True, "saved": 0, "skipped": "db_locked"}
    except Exception as e:
        _log_gap_audit("resolve_error", saved_count=0, prompt_id=str(row.get("prompt_id") or ""), details={"error": str(e)[:220]})
        return {"ok": True, "saved": 0, "skipped": "resolve_error"}
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def get_dynamic_gap_audit() -> dict[str, Any]:
    ensure_daily_operator_schema()
    latest: dict[str, Any] = {}
    resolved: dict[str, Any] = {}
    if pg_enabled():
        con_pg = pg_connect()
        if con_pg is not None:
            try:
                cur = con_pg.cursor()
                cur.execute(
                    "SELECT id, created_at, updated_at, prompt_id, user_name, prompt_text, status, answered_at, answer_text, resolved_json "
                    "FROM daily_operator_gap_prompts_core ORDER BY id DESC LIMIT 1"
                )
                row = cur.fetchone()
                if row:
                    try:
                        resolved = dict(json.loads(str(row[9] or "{}")))
                    except Exception:
                        resolved = {}
                    latest = {
                        "id": int(row[0] or 0),
                        "created_at": str(row[1] or ""),
                        "updated_at": str(row[2] or ""),
                        "prompt_id": str(row[3] or ""),
                        "user_name": str(row[4] or ""),
                        "prompt_text": str(row[5] or ""),
                        "status": str(row[6] or ""),
                        "answered_at": str(row[7] or ""),
                        "answer_text": str(row[8] or ""),
                        "resolved_json": resolved,
                    }
            except Exception:
                if strict_postgres_mode():
                    return {"ok": True, "latest": {}, "persisted": {"investor_style_memory": [], "watchlist_thesis": []}}
            finally:
                con_pg.close()
    if not latest:
        con = _conn()
        try:
            row = con.execute(
                """SELECT id, created_at, updated_at, prompt_id, user_name, prompt_text, status, answered_at, answer_text, resolved_json
                   FROM daily_operator_gap_prompts
                   ORDER BY id DESC
                   LIMIT 1"""
            ).fetchone()
            if not row:
                return {"ok": True, "latest": {}, "persisted": {"investor_style_memory": [], "watchlist_thesis": []}}
            try:
                resolved = dict(json.loads(str(row["resolved_json"] or "{}")))
            except Exception:
                resolved = {}
            latest = {
                "id": int(row["id"] or 0),
                "created_at": str(row["created_at"] or ""),
                "updated_at": str(row["updated_at"] or ""),
                "prompt_id": str(row["prompt_id"] or ""),
                "user_name": str(row["user_name"] or ""),
                "prompt_text": str(row["prompt_text"] or ""),
                "status": str(row["status"] or ""),
                "answered_at": str(row["answered_at"] or ""),
                "answer_text": str(row["answer_text"] or ""),
                "resolved_json": resolved,
            }
        except sqlite3.OperationalError:
            return {"ok": True, "latest": {}, "persisted": {"investor_style_memory": [], "watchlist_thesis": []}, "note": "db_locked"}
        finally:
            con.close()
    style_lookup = {str((r or {}).get("key") or "").strip().lower(): r for r in list_investor_style_memory_pg(limit=300)} if pg_enabled() else {}
    thesis_lookup = {str((r or {}).get("ticker") or "").strip().upper(): r for r in list_watchlist_thesis_pg(limit=300)} if pg_enabled() else {}
    style_rows: list[dict[str, str]] = []
    for it in list(resolved.get("investor_style_memory") or []):
        if not isinstance(it, dict):
            continue
        k = str(it.get("key") or "").strip().lower()
        if not k:
            continue
        row = style_lookup.get(k)
        if row:
            style_rows.append(
                {
                    "key": str((row or {}).get("key") or ""),
                    "answer": str((row or {}).get("answer") or ""),
                    "updated_at": str((row or {}).get("updated_at") or ""),
                }
            )
    thesis_rows: list[dict[str, str]] = []
    for it in list(resolved.get("watchlist_thesis") or []):
        if not isinstance(it, dict):
            continue
        tk = _safe_ticker(str(it.get("ticker") or ""))
        if not tk:
            continue
        row = thesis_lookup.get(tk)
        if row:
            thesis_rows.append(
                {
                    "ticker": str((row or {}).get("ticker") or ""),
                    "thesis_summary": str((row or {}).get("thesis_summary") or ""),
                    "time_horizon": str((row or {}).get("time_horizon") or ""),
                    "invalidation_criteria": str((row or {}).get("invalidation_criteria") or ""),
                    "strategy_tag": str((row or {}).get("strategy_tag") or ""),
                    "updated_at": str((row or {}).get("updated_at") or ""),
                }
            )
    return {
        "ok": True,
        "latest": latest,
        "persisted": {
            "investor_style_memory": style_rows,
            "watchlist_thesis": thesis_rows,
        },
    }
