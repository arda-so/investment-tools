#!/usr/bin/env python3
"""
agent_worker.py — Headless agentic loop for autonomous investment analysis.

Default mode: Phase 4.1 multi-agent debate (bull analyst + bear analyst + moderator).
Fallback:     Single-perspective loop (--no-debate flag).

Triggered by SEC filing events or nightly watch mode.  Writes to:
  - investor_annotations_core     (note + journal via _persist_outputs)
  - thesis_breach_alerts_core     (structured breach signal)
  - portfolio_cascade_alerts_core (cross-portfolio impact)
  - proposal_outcomes_core        (T+7/30/90 outcome baseline)

Works identically on local (launchd) and cloud (Cloud Run job).

Usage:
  python tools/agent_worker.py --watch [--days 1] [--dry-run] [--no-debate]
  python tools/agent_worker.py --ticker AAPL --form 10-Q [--accession ...] [--dry-run]
  python tools/agent_worker.py --event-uid ev_abc123def456
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import invest_cli
from tools.llm_engine import ask_ai
from app.services.postgres_core_service import (
    add_investor_note_pg,
    add_workspace_journal_note_pg,
    ensure_postgres_core_schema,
    get_price_metrics_pg,
    list_investor_style_memory_pg,
    list_watchlist_thesis_pg,
    pg_connect,
)
from app.services.portfolio_memory_service import get_holdings
from app.services.proactive_ai_service import (
    analyze_universe_signal_for_portfolio,
    cleanup_stuck_agent_runs,
    finish_agent_run,
    start_agent_run,
)
from app.core.filing_text import read_filing_text_any

# ---------------------------------------------------------------------------
# Startup guard — ensure all tables exist (safe on repeated calls)
# ---------------------------------------------------------------------------
try:
    ensure_postgres_core_schema()
except Exception:
    pass  # will fail again at query time with a clearer error

# ---------------------------------------------------------------------------
# Tuning constants (all overridable via env vars)
# ---------------------------------------------------------------------------

MAX_STEPS: int = 8           # single-perspective hard cap
DEBATE_STEPS: int = 4        # steps per bull/bear perspective
MAX_TOOL_OUTPUT: int = 2000  # chars per individual command result
MAX_PROMPT_CHARS: int = 14_000

WATCH_DELAY_SEC: float = float(os.getenv("AGENT_WORKER_DELAY_SEC", "5"))

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

def _build_system_prompt(today: str, rules: str = "") -> str:
    """Single-perspective (--no-debate) system prompt."""
    return f"""\
You are an autonomous investment analysis agent. Today is {today}.

You have access to these READ-ONLY data commands:

> invest_app fetch_filings --ticker AAPL [--limit 5]
> invest_app get_financials --ticker AAPL
> invest_app get_intel --ticker AAPL
> invest_app get_price --ticker AAPL
> invest_app list_holdings
> invest_app get_thesis --ticker AAPL
> invest_app list_notes --ticker AAPL --limit 10
> invest_app read_filing --ticker AAPL --form 10-Q [--accession 0001193125-26-012345] [--offset 6000]

Rules:
- Gather data with commands before drawing conclusions.
- Maximum {MAX_STEPS} commands per session.
- Do NOT guess numbers — state clearly when data is unavailable.
- Keep analysis focused: 3-5 bullet points.
- The system writes your report to the database — do not call post_note or post_journal.
- For long filings (10-K, 10-Q), use --offset 6000, 12000, 18000 to read successive chunks.
- When done, output your analysis in <FINAL_REPORT>...</FINAL_REPORT> followed immediately
  by a <STRUCTURED>...</STRUCTURED> block (see required fields below).

Required <STRUCTURED> fields:
  sentiment: bullish|neutral|bearish
  thesis_status: confirmed|watch|breach_suspected|breach_confirmed
  severity: low|medium|high
  key_metric: <single most important number or trend>
  breach_type: <short label if thesis_status is breach_*, else "none">
  breach_detail: <1-2 sentences if breach, else "none">
  actual_values: <key numbers observed, e.g. "revenue +4% vs +12% prior, margin -180bps">
  cascade_tickers: <comma-separated portfolio tickers that may be affected, or "none">
  confidence: <integer 0-100>
{rules}"""


def _build_bull_prompt(ticker: str, today: str, rules: str = "") -> str:
    return f"""\
You are a bull-case analyst. Today is {today}.

Your ONLY job: find evidence that the investment thesis for {ticker} HOLDS or is STRENGTHENING.

Look for: revenue beats, margin expansion, raised guidance, competitive wins, market share
gains, strong free cash flow, insider buying, credible management commentary.

Commands available (max 4):
> invest_app fetch_filings --ticker {ticker} --limit 3
> invest_app get_financials --ticker {ticker}
> invest_app get_price --ticker {ticker}
> invest_app get_intel --ticker {ticker}
> invest_app get_thesis --ticker {ticker}
> invest_app read_filing --ticker {ticker} --form 10-Q [--offset 6000]

Gather data, then present your bull case in <BULL_CASE>...</BULL_CASE> (3-5 bullets max).
Do NOT present bear arguments.
{rules}"""


def _build_bear_prompt(ticker: str, today: str, rules: str = "") -> str:
    return f"""\
You are a bear-case analyst. Today is {today}.

Your ONLY job: stress-test the investment thesis for {ticker}. Find where it is WEAKENING.

Look for: revenue misses, margin compression, guidance cuts, competition encroachment,
customer concentration, rising debt, insider selling, management credibility issues.

Commands available (max 4):
> invest_app fetch_filings --ticker {ticker} --limit 3
> invest_app get_financials --ticker {ticker}
> invest_app get_price --ticker {ticker}
> invest_app get_intel --ticker {ticker}
> invest_app get_thesis --ticker {ticker}
> invest_app read_filing --ticker {ticker} --form 10-Q [--offset 6000]

Gather data, then present your bear case in <BEAR_CASE>...</BEAR_CASE> (3-5 bullets max).
Do NOT present bull arguments.
{rules}"""


def _build_moderator_prompt(
    ticker: str, thesis: str, bull_case: str, bear_case: str, today: str, rules: str = ""
) -> str:
    return f"""\
You are the senior portfolio manager. Today is {today}.
Two analysts have reviewed the same SEC filing for {ticker}.

INVESTMENT THESIS:
{thesis[:400]}

BULL CASE:
{bull_case[:1500]}

BEAR CASE:
{bear_case[:1500]}

Weigh both sides against the original thesis and produce the final verdict.

You MUST output both blocks:

<FINAL_REPORT>
3-5 bullet points: net verdict, key data points, what changed vs. thesis, what to watch.
</FINAL_REPORT>
<STRUCTURED>
sentiment: bullish|neutral|bearish
thesis_status: confirmed|watch|breach_suspected|breach_confirmed
severity: low|medium|high
key_metric: <single most important number or trend>
breach_type: <short label if breach, else "none">
breach_detail: <1-2 sentences if breach, else "none">
actual_values: <key numbers, e.g. "revenue +4% vs +12% prior, margin -180bps">
cascade_tickers: <comma-separated portfolio tickers affected, or "none">
confidence: <integer 0-100>
</STRUCTURED>
{rules}"""


# ---------------------------------------------------------------------------
# Investor context enrichment
# ---------------------------------------------------------------------------

def _fetch_outcome_history(ticker: str) -> str:
    """
    Query proposal_outcomes_core for this ticker and return a plain-text
    summary of past agent analyses and their measured outcomes.

    Only includes rows with status='measured' so pending rows aren't shown
    as evidence.  Returns "" if no history yet.
    """
    try:
        con = pg_connect()
        if con is None:
            return ""
        try:
            cur = con.cursor()
            cur.execute(
                """SELECT stance_at_proposal, measurement_window,
                          baseline_price, outcome_price, return_pct,
                          direction_correct, reasoning_summary, created_at
                   FROM proposal_outcomes_core
                   WHERE ticker=%s AND status='measured'
                   ORDER BY created_at DESC
                   LIMIT 10""",
                (ticker,),
            )
            rows = cur.fetchall() or []
        finally:
            con.close()

        if not rows:
            return ""

        lines = [f"\nPAST AGENT ANALYSES FOR {ticker} (measured outcomes):"]
        correct = sum(1 for r in rows if r[5] is True)
        total   = len(rows)
        lines.append(f"  Hit rate: {correct}/{total} correct ({int(correct/total*100)}%)")

        for r in rows[:6]:  # show at most 6 rows — keeps context tight
            stance   = str(r[0] or "?")
            window   = str(r[1] or "?")
            base     = float(r[2] or 0)
            outcome  = float(r[3] or 0)
            ret_pct  = float(r[4] or 0)
            correct_ = r[5]
            summary  = str(r[6] or "")[:120]
            date_s   = str(r[7] or "")[:10]
            result   = "CORRECT" if correct_ is True else ("INCORRECT" if correct_ is False else "PENDING")
            sign     = "+" if ret_pct >= 0 else ""
            lines.append(
                f"  {date_s}  stance={stance}  {window}: "
                f"${base:.2f}→${outcome:.2f} ({sign}{ret_pct:.1f}%)  [{result}]"
                + (f"  // {summary}" if summary else "")
            )

        # Highlight systematic bias if visible
        if total >= 3:
            wrong_bull = sum(1 for r in rows if r[0] in ("bullish","buy") and r[5] is False)
            wrong_bear = sum(1 for r in rows if r[0] in ("bearish","sell") and r[5] is False)
            if wrong_bull >= 2:
                lines.append(f"  ⚠ Pattern: over-bullish on {ticker} — {wrong_bull} incorrect bullish calls.")
            if wrong_bear >= 2:
                lines.append(f"  ⚠ Pattern: over-bearish on {ticker} — {wrong_bear} incorrect bearish calls.")

        return "\n".join(lines)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Feature 1: Agent memory across runs
# ---------------------------------------------------------------------------

def _ensure_agent_memory_table() -> None:
    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agent_memory_core (
                id BIGSERIAL PRIMARY KEY,
                ticker TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_memory_core_ticker "
            "ON agent_memory_core(ticker, updated_at DESC)"
        )
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


def _load_cross_portfolio_memories(current_ticker: str) -> str:
    """Return the most recent agent memory summary for each OTHER portfolio holding."""
    try:
        holdings = get_holdings(limit=20)
        other_tickers = [
            str(h.get("ticker") or "").strip().upper()
            for h in holdings
            if str(h.get("ticker") or "").strip().upper() not in {"", current_ticker}
        ]
        if not other_tickers:
            return ""
        con = pg_connect()
        if con is None:
            return ""
        lines = ["\nCROSS-PORTFOLIO CONTEXT (recent analyses of your other holdings):"]
        try:
            cur = con.cursor()
            for tk in other_tickers[:4]:
                cur.execute(
                    "SELECT summary, updated_at FROM agent_memory_core "
                    "WHERE ticker=%s ORDER BY updated_at DESC LIMIT 1",
                    (tk,),
                )
                row = cur.fetchone()
                if row:
                    date = str(row[1] or "")[:10]
                    lines.append(f"  [{tk} · {date}] {str(row[0] or '')[:250]}")
        finally:
            con.close()
        return "\n".join(lines) if len(lines) > 1 else ""
    except Exception:
        return ""


def _load_agent_memory(ticker: str) -> str:
    """Return the last 3 distilled memories for this ticker."""
    try:
        con = pg_connect()
        if con is None:
            return ""
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT summary, updated_at FROM agent_memory_core "
                "WHERE ticker=%s ORDER BY updated_at DESC LIMIT 3",
                (ticker,),
            )
            rows = cur.fetchall() or []
        finally:
            con.close()
        if not rows:
            return ""
        lines = [f"\nAGENT MEMORY FOR {ticker} (from past analyses):"]
        for r in rows:
            date = str(r[1] or "")[:10]
            lines.append(f"  [{date}] {str(r[0] or '')[:300]}")
        return "\n".join(lines)
    except Exception:
        return ""


def _save_agent_memory(ticker: str, report: str, structured: dict) -> None:
    """Distill 2-3 sentence memory from this analysis via LLM, then save to Postgres."""
    _ensure_agent_memory_table()
    try:
        sentiment  = structured.get("sentiment", "neutral")
        status     = structured.get("thesis_status", "")
        key_metric = structured.get("key_metric", "")
        prompt = (
            f"You just completed an investment analysis of {ticker}.\n\n"
            f"REPORT:\n{report[:1500]}\n\n"
            f"Distill this into 2-3 sentences of memory for future analyses. "
            f"Focus on: key conclusion, main data points observed, and what to watch next. "
            f"Be specific with numbers. Start with the ticker symbol.\n"
            f"Output ONLY the memory sentences, nothing else."
        )
        memory = ask_ai(prompt, context="", mode="fast").strip()
        if not memory or len(memory) < 20:
            memory = f"{ticker}: {sentiment} — {status}. Key metric: {key_metric}."
        con = pg_connect()
        if con is None:
            return
        try:
            cur = con.cursor()
            now = datetime.datetime.now().isoformat()
            cur.execute(
                "INSERT INTO agent_memory_core (ticker, summary, updated_at, created_at) "
                "VALUES (%s, %s, %s, %s)",
                (ticker, memory[:600], now, now),
            )
            con.commit()
            print(f"[agent_worker] Memory saved for {ticker}: {memory[:80]}…")
        finally:
            con.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Feature 2: Reflexion policy injection
# ---------------------------------------------------------------------------

def _load_active_reflexion_rules() -> str:
    """Load active reflexion policy rules from Postgres. Returns formatted string."""
    try:
        con = pg_connect()
        if con is None:
            return ""
        try:
            cur = con.cursor()
            cur.execute(
                "SELECT policy_json::text FROM reflexion_policy_versions_core "
                "WHERE is_active=1 ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()
        finally:
            con.close()
        if not row:
            return ""
        try:
            policy = json.loads(str(row[0] or "{}"))
        except Exception:
            return ""
        rules: list[str] = []
        for key in ("rules", "guidelines", "principles", "lessons", "policy"):
            val = policy.get(key)
            if isinstance(val, list) and val:
                rules = [str(r) for r in val]
                break
            elif isinstance(val, str) and val.strip():
                rules = [val.strip()]
                break
        if not rules:
            return ""
        lines = ["\n[LEARNED RULES — reflexion policy, apply to this analysis]:"]
        for r in rules[:8]:
            lines.append(f"  - {r[:200]}")
        return "\n".join(lines)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Feature 3: Macro context injection
# ---------------------------------------------------------------------------

def _load_macro_context() -> str:
    """Read latest macro watchdog JSON and return a compact 3-line context."""
    _macro_paths = [
        ROOT / "reports" / ".terminal_inputs" / "macro_watchdog_latest.json",
        ROOT / "reports" / "macro_watchdog_latest.json",
        ROOT / "data" / "cache" / "macro_watchdog_latest.json",
    ]
    payload: dict = {}
    for p in _macro_paths:
        if p.exists():
            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
                break
            except Exception:
                continue
    if not payload:
        return ""
    try:
        asof     = str(payload.get("asof_utc") or "")[:16]
        series   = payload.get("series") or {}
        all_s    = series.get("all_items") or {}
        core_s   = series.get("core") or {}
        surprise = payload.get("cpi_surprise") or {}
        lines = [f"\nMACRO ENVIRONMENT (as of {asof} UTC):"]
        all_yoy = all_s.get("yoy_pct")
        all_mom = all_s.get("mom_pct")
        core_yoy = core_s.get("yoy_pct")
        if all_yoy is not None:
            mom_str = f" | MoM: {all_mom:+.2f}%" if all_mom is not None else ""
            lines.append(f"  CPI YoY: {all_yoy:+.2f}%{mom_str}")
        if core_yoy is not None:
            lines.append(f"  Core CPI YoY: {core_yoy:+.2f}%")
        vs_cons = surprise.get("core_mom_vs_consensus")
        if vs_cons is not None:
            direction = "above" if vs_cons > 0 else "below"
            lines.append(f"  Core CPI vs consensus: {abs(vs_cons):.2f}pp {direction} expectations")
        return "\n".join(lines)
    except Exception:
        return ""


def _build_investor_context(ticker: str) -> str:
    lines: list[str] = []
    try:
        style = list_investor_style_memory_pg(limit=10)
        if style:
            lines.append("Investor profile:")
            for s in style[:5]:
                k = str(s.get("key") or "").strip()
                v = str(s.get("answer") or "").strip()[:120]
                if k and v:
                    lines.append(f"  {k}: {v}")
    except Exception:
        pass
    try:
        theses = list_watchlist_thesis_pg(limit=300)
        row = next(
            (r for r in theses if str(r.get("ticker") or "").upper() == ticker), None
        )
        if row:
            lines.append(f"\nInvestment thesis for {ticker}:")
            if row.get("thesis_summary"):
                lines.append(f"  Summary: {str(row['thesis_summary'])[:250]}")
            if row.get("time_horizon"):
                lines.append(f"  Time horizon: {row['time_horizon']}")
            if row.get("invalidation_criteria"):
                lines.append(f"  Invalidation: {str(row['invalidation_criteria'])[:250]}")
    except Exception:
        pass
    # Inject past outcome history so the agent can calibrate its own confidence
    outcome_history = _fetch_outcome_history(ticker)
    if outcome_history:
        lines.append(outcome_history)
    # Inject agent memory from past analyses
    memory = _load_agent_memory(ticker)
    if memory:
        lines.append(memory)
    # Inject cross-portfolio context from other holdings
    cross_memory = _load_cross_portfolio_memories(ticker)
    if cross_memory:
        lines.append(cross_memory)
    # Inject macro environment context
    macro = _load_macro_context()
    if macro:
        lines.append(macro)
    return "\n".join(lines)


def _get_thesis_text(ticker: str) -> str:
    try:
        theses = list_watchlist_thesis_pg(limit=300)
        row = next(
            (r for r in theses if str(r.get("ticker") or "").upper() == ticker), None
        )
        return str(row.get("thesis_summary") or "No thesis on file.") if row else "No thesis on file."
    except Exception:
        return "No thesis on file."


# ---------------------------------------------------------------------------
# Trigger message
# ---------------------------------------------------------------------------

def _build_trigger_message(event: dict) -> str:
    # Earnings prep (or any other mode) can override the full trigger message
    override = event.get("_override_trigger")
    if override:
        return str(override)
    ticker = str(event.get("ticker") or "").strip().upper()
    form = event.get("form", "")
    date = event.get("filing_date", "")
    ctx = _build_investor_context(ticker)
    ctx_section = f"\n\n{ctx}\n" if ctx else ""
    return (
        f"A new SEC filing was detected.{ctx_section}\n"
        f"Ticker: {ticker} | Form: {form} | Date: {date}\n\n"
        f"1. Fetch the filing and financial data for {ticker}.\n"
        f"2. Compare current figures against historical trend.\n"
        f"3. Check alignment with the investment thesis.\n"
        f"4. Identify signals that change the outlook.\n"
        f"Begin by fetching data, then output <FINAL_REPORT> + <STRUCTURED> when done."
    )


# ---------------------------------------------------------------------------
# Prompt management
# ---------------------------------------------------------------------------

def _truncate_tool_output(text: str) -> str:
    if len(text) <= MAX_TOOL_OUTPUT:
        return text
    return text[:MAX_TOOL_OUTPUT] + f"\n[...{len(text) - MAX_TOOL_OUTPUT} chars truncated]"


def _messages_to_prompt(messages: list[dict]) -> str:
    def _fmt(m: dict) -> str:
        role = m.get("role", "user")
        content = m.get("content", "")
        tag = {"system": "[SYSTEM]", "assistant": "[ASSISTANT]"}.get(role, "[USER]")
        return f"{tag}\n{content}"

    parts = [_fmt(m) for m in messages]
    full = "\n\n".join(parts)
    if len(full) <= MAX_PROMPT_CHARS:
        return full
    if len(parts) <= 4:
        return full[:MAX_PROMPT_CHARS]
    core = parts[:2]
    tail = parts[-2:]
    compressed = [
        p[:300] + f"\n[...{len(p)-300} chars omitted]" if len(p) > 300 else p
        for p in parts[2:-2]
    ]
    return "\n\n".join(core + compressed + tail)


# ---------------------------------------------------------------------------
# Command extraction + report parsing
# ---------------------------------------------------------------------------

_CMD_RE = re.compile(r">\s*invest_app\s+[^\n\r]+", re.MULTILINE)


def _extract_commands(text: str) -> list[str]:
    return [m.group(0).strip() for m in _CMD_RE.finditer(text)]


def _extract_report(text: str) -> str:
    m = re.search(r"<FINAL_REPORT>(.*?)</FINAL_REPORT>", text, re.DOTALL)
    return m.group(1).strip() if m else text.strip()


def _extract_case(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # Fallback: everything after the opening tag
    m2 = re.search(rf"<{tag}>(.+)", text, re.DOTALL)
    return m2.group(1).strip()[:2000] if m2 else text.strip()[:2000]


def _parse_structured(text: str) -> dict[str, str]:
    """
    Extract <STRUCTURED>...</STRUCTURED> from LLM output.
    Returns a dict of field -> value.  Safe: never raises.
    """
    m = re.search(r"<STRUCTURED>(.*?)</STRUCTURED>", text, re.DOTALL)
    if not m:
        return {}
    result: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            result[k.strip().lower().replace(" ", "_")] = v.strip()
    return result


# ---------------------------------------------------------------------------
# Database writes — three tables
# ---------------------------------------------------------------------------

def _write_thesis_breach(
    ticker: str, structured: dict, report: str, dry_run: bool
) -> None:
    """Write to thesis_breach_alerts_core if structured output signals a breach."""
    status = structured.get("thesis_status", "")
    if "breach" not in status:
        return

    breach_type = (structured.get("breach_type") or "unspecified")[:60]
    if not breach_type or breach_type == "none":
        breach_type = "unspecified"
    severity     = (structured.get("severity") or "medium")[:16]
    breach_detail = (structured.get("breach_detail") or "")[:600]
    actual_values = (structured.get("actual_values") or "")[:500]

    try:
        theses = list_watchlist_thesis_pg(limit=300)
        row = next((r for r in theses if str(r.get("ticker") or "").upper() == ticker), None)
        thesis_text  = str(row.get("thesis_summary") or "")[:1000] if row else ""
        invalidation = str(row.get("invalidation_criteria") or "")[:500] if row else ""
    except Exception:
        thesis_text = invalidation = ""

    if dry_run:
        print(f"[dry-run] thesis_breach_alerts_core ← {ticker} {breach_type} severity={severity}")
        return

    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        now = datetime.datetime.now().isoformat()
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=7)).isoformat()
        cur.execute(
            "SELECT id FROM thesis_breach_alerts_core "
            "WHERE ticker=%s AND breach_type=%s AND detected_at>=%s AND status='open' LIMIT 1",
            (ticker, breach_type, cutoff),
        )
        if cur.fetchone():
            return  # dedup: already open
        cur.execute(
            """INSERT INTO thesis_breach_alerts_core
               (id, ticker, thesis_id, breach_type, breach_detail, severity, detected_at,
                financial_context, thesis_text, invalidation_criteria, actual_values, status)
               VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM thesis_breach_alerts_core),
                       %s,0,%s,%s,%s,%s,%s,%s,%s,%s,'open')""",
            (ticker, breach_type, breach_detail, severity, now,
             report[:2000], thesis_text, invalidation, actual_values),
        )
        con.commit()
        print(f"[agent_worker] Thesis breach recorded: {ticker} {breach_type} ({severity})")
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


def _write_cascade_alerts(ticker: str, structured: dict, dry_run: bool) -> None:
    """Write to portfolio_cascade_alerts_core for each flagged affected ticker."""
    cascade_raw = structured.get("cascade_tickers") or "none"
    affected = [
        t.strip().upper()
        for t in cascade_raw.split(",")
        if t.strip() and t.strip().lower() != "none" and t.strip().upper() != ticker
    ]
    if not affected:
        return

    key_metric = (structured.get("key_metric") or "")[:500]
    severity   = (structured.get("severity") or "medium")[:16]
    try:
        confidence = min(1.0, max(0.0, float(structured.get("confidence") or 50) / 100.0))
    except (ValueError, TypeError):
        confidence = 0.5

    if dry_run:
        print(f"[dry-run] portfolio_cascade_alerts_core ← {ticker} → {affected}")
        return

    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        now    = datetime.datetime.now().isoformat()
        cutoff = (datetime.datetime.now() - datetime.timedelta(days=3)).isoformat()
        created = 0
        for atk in affected:
            cur.execute(
                "SELECT id FROM portfolio_cascade_alerts_core "
                "WHERE trigger_ticker=%s AND affected_ticker=%s AND detected_at>=%s AND status='open' LIMIT 1",
                (ticker, atk, cutoff),
            )
            if cur.fetchone():
                continue
            cur.execute(
                """INSERT INTO portfolio_cascade_alerts_core
                   (id, trigger_ticker, trigger_signal, trigger_proposal_id, affected_ticker,
                    effect_type, effect_summary, relationship, confidence, severity, detected_at, status)
                   VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM portfolio_cascade_alerts_core),
                           %s,%s,0,%s,'filing_signal',%s,'portfolio_holding',%s,%s,%s,'open')""",
                (ticker, key_metric, atk,
                 f"Agent worker analysis of {ticker} filing flagged potential impact on {atk}.",
                 confidence, severity, now),
            )
            created += 1
        con.commit()
        if created:
            print(f"[agent_worker] Cascade alerts: {ticker} → {affected} ({created} written)")
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


def _write_outcome_baseline(ticker: str, structured: dict, dry_run: bool) -> None:
    """Capture price + stance baseline for T+7/30/90 outcome tracking."""
    try:
        pm = get_price_metrics_pg(ticker)
        baseline = 0.0
        for field in ("ytd_end_px", "m12_end_px", "y5_end_px"):
            v = float((pm or {}).get(field) or 0.0)
            if v > 0:
                baseline = v
                break
    except Exception:
        baseline = 0.0

    if baseline <= 0:
        return  # no price = no useful baseline

    stance     = (structured.get("sentiment") or "neutral")[:12]
    confidence = (structured.get("confidence") or "0")[:16]
    key_metric = (structured.get("key_metric") or "")[:500]

    if dry_run:
        print(f"[dry-run] proposal_outcomes_core ← {ticker} @ {baseline:.2f}")
        return

    con = pg_connect()
    if con is None:
        return
    try:
        cur = con.cursor()
        now = datetime.datetime.now().isoformat()
        for window in ("7d", "30d", "90d"):
            cur.execute(
                """INSERT INTO proposal_outcomes_core
                   (id, proposal_id, ticker, outcome_type, created_at, measurement_window,
                    baseline_price, confidence_at_proposal, stance_at_proposal, reasoning_summary, status)
                   VALUES ((SELECT COALESCE(MAX(id),0)+1 FROM proposal_outcomes_core),
                           0,%s,'price',%s,%s,%s,%s,%s,%s,'pending')""",
                (ticker, now, window, baseline, confidence, stance, key_metric),
            )
        con.commit()
        print(f"[agent_worker] Outcome baseline: {ticker} @ {baseline:.2f} stance={stance}")
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Output persistence (sole note/journal write path)
# ---------------------------------------------------------------------------

def _format_sources(commands: list[str]) -> str:
    """
    Deduplicate commands and format as a compact SOURCES block.
    Groups by command type (get_financials, fetch_filings, etc.) for readability.
    """
    if not commands:
        return ""
    seen: dict[str, list[str]] = {}
    _cmd_re = re.compile(r"invest_app\s+(\S+)")
    _ticker_re = re.compile(r"--ticker\s+(\S+)", re.IGNORECASE)
    for cmd in commands:
        m = _cmd_re.search(cmd)
        if not m:
            continue
        cmd_name = m.group(1)
        t = _ticker_re.search(cmd)
        ticker_label = t.group(1).upper() if t else ""
        entry = f"{ticker_label} ({cmd_name})" if ticker_label else cmd_name
        seen.setdefault(cmd_name, [])
        if entry not in seen[cmd_name]:
            seen[cmd_name].append(entry)
    lines = ["", "SOURCES:"]
    for cmd_name, entries in seen.items():
        lines.append(f"  • {', '.join(entries)}")
    return "\n".join(lines)


def _persist_outputs(
    ticker: str,
    report: str,
    event: dict,
    dry_run: bool = False,
    commands: list[str] | None = None,
) -> None:
    today      = datetime.date.today().isoformat()
    accession  = str(event.get("accession") or "").strip()
    form       = str(event.get("form") or "").strip()
    sources    = _format_sources(commands or [])
    body       = f"[accession={accession}]\n{report}" if accession else report
    note_text  = (body + sources)[:4000]
    summary    = next((ln.strip() for ln in report.splitlines() if ln.strip()), f"{ticker} {form} analyzed.")
    journal    = f"[agent_worker] {today} — {summary[:200]}"

    if dry_run:
        print(f"[dry-run] investor_annotations_core ← note {ticker} ({len(note_text)} chars)")
        if sources:
            print(sources)
        print(f"[dry-run] investor_annotations_core ← journal: {journal[:100]}")
        return

    add_investor_note_pg(
        scope="agent_worker", ticker=ticker, sentiment="neutral",
        note=note_text, tags="ai,sec", status="approved",
        created_by="ai", ai_confidence=0.85,
    )
    add_workspace_journal_note_pg(
        ticker=ticker, note=journal[:4000],
        action="agent_worker", emotion="Calm", created_by="ai",
    )


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

def _log_run(result: dict, event: dict) -> None:
    log_path = ROOT / "logs" / "agent_worker_runs.jsonl"
    try:
        log_path.parent.mkdir(exist_ok=True)
        entry = {
            "ts":            datetime.datetime.now().isoformat(timespec="seconds"),
            "run_uid":       result.get("run_uid", ""),
            "ticker":        event.get("ticker", ""),
            "form":          event.get("form", ""),
            "accession":     event.get("accession", ""),
            "mode":          result.get("mode", "single"),
            "status":        result.get("status", ""),
            "steps":         result.get("steps", 0),
            "commands":      result.get("commands", []),
            "report_chars":  len(result.get("report", "")),
            "cross_portfolio": result.get("cross_portfolio", ""),
            "structured":    result.get("structured", {}),
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Phase 4.1: Multi-agent debate
# ---------------------------------------------------------------------------

def _run_perspective(event: dict, role: str, today: str, rules: str = "") -> tuple[str, list[str]]:
    """
    Run bull or bear single-perspective loop (DEBATE_STEPS max).
    Returns (case_text, commands_run).
    """
    ticker   = str(event.get("ticker") or "").strip().upper()
    end_tag  = "BULL_CASE" if role == "bull" else "BEAR_CASE"
    sys_prompt = _build_bull_prompt(ticker, today, rules) if role == "bull" else _build_bear_prompt(ticker, today, rules)

    messages: list[dict] = [
        {"role": "system", "content": sys_prompt},
        {"role": "user",
         "content": f"Analyze the {event.get('form','')} filing for {ticker} "
                    f"(filed {event.get('filing_date', today)})."},
    ]
    commands_run: list[str] = []

    for _ in range(DEBATE_STEPS):
        response = ask_ai(_messages_to_prompt(messages), context="", mode="smart", temperature=0.3)
        messages.append({"role": "assistant", "content": response})

        if f"<{end_tag}>" in response:
            return _extract_case(response, end_tag), commands_run

        commands = _extract_commands(response)
        if not commands:
            messages.append({
                "role": "user",
                "content": f"No commands found. Either run a data command or "
                           f"output your {role} case in <{end_tag}>...</{end_tag}>.",
            })
            continue

        results = []
        for cmd in commands:
            commands_run.append(cmd)
            results.append(f"[Result for `{cmd}`]:\n{_truncate_tool_output(invest_cli.execute(cmd))}")
        messages.append({"role": "user", "content": "\n\n".join(results)})

    # Fallback: return whatever the last assistant message said
    last = next((m["content"] for m in reversed(messages) if m["role"] == "assistant"), "")
    return (last[:2000] or f"[{role} analysis incomplete]"), commands_run


def _run_debate(event: dict, dry_run: bool = False) -> dict:
    """
    Full Phase 4.1 debate: bull perspective → bear perspective → moderator synthesis.
    Writes structured output to all three intelligence tables.
    """
    ticker = str(event.get("ticker") or "").strip().upper()
    today  = datetime.date.today().isoformat()
    rules  = _load_active_reflexion_rules()

    try:
        print(f"[agent_worker] [bull]  {ticker} …")
        bull_case, bull_cmds = _run_perspective(event, "bull", today, rules)

        time.sleep(min(WATCH_DELAY_SEC, 3))  # brief pause between LLM calls

        print(f"[agent_worker] [bear]  {ticker} …")
        bear_case, bear_cmds = _run_perspective(event, "bear", today, rules)

        print(f"[agent_worker] [mod]   {ticker} …")
        thesis    = _get_thesis_text(ticker)
        mod_resp  = ask_ai(
            _build_moderator_prompt(ticker, thesis, bull_case, bear_case, today, rules),
            context="", mode="smart", temperature=0.1,
        )

        report     = _extract_report(mod_resp)
        structured = _parse_structured(mod_resp)
        cross      = structured.get("cascade_tickers", "")
        all_cmds   = bull_cmds + bear_cmds

        _write_thesis_breach(ticker, structured, report, dry_run)
        _write_cascade_alerts(ticker, structured, dry_run)
        _write_outcome_baseline(ticker, structured, dry_run)
        _persist_outputs(ticker, report, event, dry_run=dry_run, commands=all_cmds)
        if not dry_run:
            _save_agent_memory(ticker, report, structured)

        result = {
            "mode":          "debate",
            "status":        "ok",
            "report":        report,
            "structured":    structured,
            "bull_case":     bull_case,
            "bear_case":     bear_case,
            "steps":         DEBATE_STEPS * 2 + 1,
            "commands":      all_cmds,
            "cross_portfolio": cross,
        }
        _log_run(result, event)
        return result

    except Exception as exc:
        result = {
            "mode": "debate", "status": "error",
            "report": f"Debate error: {exc}",
            "steps": 0, "commands": [], "cross_portfolio": "", "structured": {},
        }
        _log_run(result, event)
        return result


# ---------------------------------------------------------------------------
# Single-perspective loop (--no-debate fallback)
# ---------------------------------------------------------------------------

def _run_single(event: dict, max_steps: int = MAX_STEPS, dry_run: bool = False) -> dict:
    """Original single-perspective agentic loop."""
    ticker = str(event.get("ticker") or "").strip().upper()
    today  = datetime.date.today().isoformat()

    rules = _load_active_reflexion_rules()
    messages: list[dict] = [
        {"role": "system",  "content": _build_system_prompt(today, rules)},
        {"role": "user",    "content": _build_trigger_message(event)},
    ]
    commands_run: list[str] = []

    try:
        for step in range(max_steps):
            response = ask_ai(_messages_to_prompt(messages), context="", mode="smart", temperature=0.2)
            messages.append({"role": "assistant", "content": response})

            if "<FINAL_REPORT>" in response:
                report     = _extract_report(response)
                structured = _parse_structured(response)
                cross      = structured.get("cascade_tickers", "")

                _write_thesis_breach(ticker, structured, report, dry_run)
                _write_cascade_alerts(ticker, structured, dry_run)
                _write_outcome_baseline(ticker, structured, dry_run)
                _persist_outputs(ticker, report, event, dry_run=dry_run, commands=commands_run)
                if not dry_run:
                    _save_agent_memory(ticker, report, structured)

                result = {
                    "mode": "single", "status": "ok", "report": report,
                    "structured": structured, "steps": step + 1,
                    "commands": commands_run, "cross_portfolio": cross,
                }
                _log_run(result, event)
                return result

            commands = _extract_commands(response)
            if not commands:
                messages.append({
                    "role": "user",
                    "content": "No commands found. Either issue a data command or "
                               "output <FINAL_REPORT>...</FINAL_REPORT> + <STRUCTURED>...</STRUCTURED>.",
                })
                continue

            results = []
            for cmd in commands:
                raw = invest_cli.execute(cmd)
                commands_run.append(cmd)
                results.append(f"[Result for `{cmd}`]:\n{_truncate_tool_output(raw)}")
            messages.append({"role": "user", "content": "\n\n".join(results)})

    except Exception as exc:
        result = {
            "mode": "single", "status": "error",
            "report": f"Agent loop error: {exc}",
            "steps": len(commands_run), "commands": commands_run,
            "cross_portfolio": "", "structured": {},
        }
        _log_run(result, event)
        return result

    result = {
        "mode": "single", "status": "no_report", "report": "",
        "structured": {}, "steps": max_steps,
        "commands": commands_run, "cross_portfolio": "",
    }
    _log_run(result, event)
    return result


# ---------------------------------------------------------------------------
# Public run() entry point
# ---------------------------------------------------------------------------

def run(
    event:     dict,
    max_steps: int  = MAX_STEPS,
    dry_run:   bool = False,
    debate:    bool = True,
) -> dict:
    """
    Analyse a single SEC filing event.

    event keys: type, ticker, form, accession, filing_path, filing_date

    Returns: {status, report, structured, steps, commands, cross_portfolio, mode, run_uid}
    """
    ticker = str(event.get("ticker") or "").strip().upper()
    mode   = "debate" if debate else "single"

    # Register run in agent_runs_core (visible on dashboard Agent panel)
    run_uid = ""
    if not dry_run:
        try:
            run_uid = start_agent_run(
                agent_name=f"agent_worker/{mode}",
                trigger_type=str(event.get("type") or "sec_filing"),
                input_payload={
                    "ticker": ticker,
                    "form":   event.get("form", ""),
                    "accession": event.get("accession", ""),
                },
            )
        except Exception:
            pass

    result = _run_debate(event, dry_run=dry_run) if debate else _run_single(event, max_steps=max_steps, dry_run=dry_run)
    result["run_uid"] = run_uid

    if not dry_run and run_uid:
        try:
            finish_agent_run(
                run_uid=run_uid,
                status=result.get("status", "finished"),
                output_payload={
                    "ticker":       ticker,
                    "steps":        result.get("steps", 0),
                    "report_chars": len(result.get("report", "")),
                    "thesis_status": result.get("structured", {}).get("thesis_status", ""),
                    "sentiment":    result.get("structured", {}).get("sentiment", ""),
                    "confidence":   result.get("structured", {}).get("confidence", ""),
                },
                error_text=result.get("report", "")[:500] if result.get("status") == "error" else "",
            )
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# Interactive / chat query mode  (called from the dashboard top bar / AI Agent channel)
# ---------------------------------------------------------------------------

_QUERY_SYSTEM_PROMPT = f"""\
You are an autonomous investment analysis agent with access to real portfolio data.

Available data commands (use these to gather facts before answering):
> invest_app list_holdings
> invest_app get_financials --ticker AAPL
> invest_app get_price --ticker AAPL
> invest_app get_intel --ticker AAPL
> invest_app get_thesis --ticker AAPL
> invest_app fetch_filings --ticker AAPL [--limit 5]
> invest_app read_filing --ticker AAPL --form 10-Q [--accession X] [--offset 6000]
> invest_app list_notes --ticker AAPL --limit 10

Rules:
- Maximum {MAX_STEPS} commands per session.
- Do NOT guess numbers — use commands to fetch real data first.
- Be concise: 3-5 sentences or bullet points in your final answer.
- When done, wrap your answer in <FINAL_REPORT>...</FINAL_REPORT> tags.
- Do NOT call post_note or post_journal.
"""

def run_query(
    query: str,
    ticker: str = "",
    max_steps: int = 6,
    dry_run: bool = False,
) -> dict:
    """
    Run the agent loop for a free-form investment question from the dashboard.

    Returns: {status, report, steps, commands, run_uid}
    """
    from tools.llm_engine import ask_ai as _ask_ai
    from app.services.proactive_ai_service import start_agent_run, finish_agent_run

    query = str(query or "").strip()
    ticker = str(ticker or "").strip().upper()
    if not query:
        return {"status": "error", "report": "Empty query.", "steps": 0, "commands": []}

    run_uid = ""
    if not dry_run:
        try:
            run_uid = start_agent_run(
                agent_name="chat_query",
                trigger_type="manual",
                input_payload={"query": query, "ticker": ticker},
            )
        except Exception:
            pass

    ticker_hint = f"\nThe user is asking about ticker: {ticker}." if ticker else ""
    messages = [
        {"role": "system", "content": _QUERY_SYSTEM_PROMPT},
        {"role": "user", "content": f"User question: {query}{ticker_hint}\n\nFetch the data you need, then answer."},
    ]
    commands_run: list[str] = []

    report = ""
    status = "no_report"

    for step in range(max_steps):
        full_prompt = "\n\n".join(
            f"[{m['role'].upper()}]: {m['content']}" for m in messages
        )
        response = _ask_ai(full_prompt, context="", mode="smart")
        messages.append({"role": "assistant", "content": response})

        if "<FINAL_REPORT>" in response:
            m = re.search(r"<FINAL_REPORT>(.*?)</FINAL_REPORT>", response, re.DOTALL)
            report = m.group(1).strip() if m else response.strip()
            status = "ok"
            break

        commands = _extract_commands(response)
        if not commands:
            # No commands and no report — treat response as the answer
            report = response.strip()
            status = "ok"
            break

        results = []
        for cmd in commands[:4]:
            out = invest_cli.execute(cmd) if not dry_run else "[dry-run]"
            commands_run.append(cmd)
            results.append(f"[Result for `{cmd}`]:\n{_truncate_tool_output(out)}")
        messages.append({"role": "user", "content": "\n\n".join(results)})

    if not dry_run and run_uid:
        try:
            finish_agent_run(
                run_uid=run_uid,
                status=status,
                output_payload={"query": query, "steps": step + 1, "report_chars": len(report)},
            )
        except Exception:
            pass

    return {
        "status": status,
        "report": report,
        "steps": step + 1,
        "commands": commands_run,
        "run_uid": run_uid,
    }


# ---------------------------------------------------------------------------
# Watch mode
# ---------------------------------------------------------------------------

def _query_recent_filings(tickers: set[str], lookback_days: int) -> list[dict]:
    if not tickers:
        return []
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        cur.execute(
            """SELECT ticker, form_type, accession_no, local_path, filed_at
               FROM filings_core
               WHERE ticker = ANY(%s)
                 AND filed_at >= NOW() - %s * INTERVAL '1 day'
               ORDER BY filed_at DESC""",
            (list(tickers), int(lookback_days)),
        )
        return [
            {"ticker": str(r[0] or ""), "form": str(r[1] or ""),
             "accession": str(r[2] or ""), "path": str(r[3] or ""),
             "date": str(r[4] or "")[:10]}
            for r in (cur.fetchall() or [])
        ]
    except Exception:
        return []
    finally:
        con.close()


def _accessions_already_analyzed() -> set[str]:
    con = pg_connect()
    if con is None:
        return set()
    try:
        cur = con.cursor()
        cur.execute(
            """SELECT content FROM investor_annotations_core
               WHERE annotation_type='note' AND category='agent_worker'
                 AND content LIKE '[accession=%'
               ORDER BY created_at DESC LIMIT 500"""
        )
        found: set[str] = set()
        acc_re = re.compile(r"\[accession=([^\]]+)\]")
        for (content,) in (cur.fetchall() or []):
            m = acc_re.search(str(content or ""))
            if m:
                found.add(m.group(1).strip())
        return found
    except Exception:
        return set()
    finally:
        con.close()


def run_watch(
    lookback_days: int  = 1,
    dry_run:       bool = False,
    debate:        bool = True,
) -> list[dict]:
    """Process new filings for held tickers not yet analyzed."""
    holdings = get_holdings(limit=500)
    held = {str(h.get("ticker") or "").strip().upper() for h in holdings if h.get("ticker")}
    if not held:
        print("[agent_worker] No holdings — nothing to watch.")
        return []

    recent = _query_recent_filings(held, lookback_days)
    done   = _accessions_already_analyzed()
    results: list[dict] = []

    for i, filing in enumerate(recent):
        acc = filing["accession"]
        if acc and acc in done:
            print(f"[agent_worker] Skip {filing['ticker']} {filing['form']} (already analyzed).")
            continue

        if i > 0 and WATCH_DELAY_SEC > 0:
            print(f"[agent_worker] Waiting {WATCH_DELAY_SEC}s …")
            time.sleep(WATCH_DELAY_SEC)

        mode_label = "debate" if debate else "single"
        dry_label  = " [dry-run]" if dry_run else ""
        print(f"[agent_worker]{dry_label} [{mode_label}] {filing['ticker']} {filing['form']} {acc}")

        result = run(
            event={
                "type": "sec_filing", "ticker": filing["ticker"],
                "form": filing["form"],   "accession": acc,
                "filing_path": filing["path"], "filing_date": filing["date"],
            },
            dry_run=dry_run,
            debate=debate,
        )
        result["filing"] = filing
        results.append(result)

        print(f"[agent_worker]   → status={result.get('status')}  steps={result.get('steps')}")
        structured = result.get("structured", {})
        if structured.get("thesis_status"):
            print(f"[agent_worker]   → thesis={structured['thesis_status']}  sentiment={structured.get('sentiment','?')}")

    print(f"[agent_worker] Watch done. Processed {len(results)} filing(s).")
    return results


# ---------------------------------------------------------------------------
# Universe watch mode
# ---------------------------------------------------------------------------

def _accessions_universe_already_processed() -> set[str]:
    """Return accession numbers already processed by universe_watch (logged in annotations)."""
    con = pg_connect()
    if con is None:
        return set()
    try:
        cur = con.cursor()
        cur.execute(
            """SELECT content FROM investor_annotations_core
               WHERE annotation_type='note' AND category='universe_watch'
                 AND content LIKE '[accession=%'
               ORDER BY created_at DESC LIMIT 1000"""
        )
        found: set[str] = set()
        acc_re = re.compile(r"\[accession=([^\]]+)\]")
        for (content,) in (cur.fetchall() or []):
            m = acc_re.search(str(content or ""))
            if m:
                found.add(m.group(1).strip())
        return found
    except Exception:
        return set()
    finally:
        con.close()


def _log_universe_processed(ticker: str, accession: str, cascades: int, dry_run: bool) -> None:
    """Write a stub note so we never reprocess the same filing."""
    if dry_run or not accession:
        return
    try:
        add_investor_note_pg(
            scope="entity",
            ticker=ticker,
            sentiment="neutral",
            note=f"[accession={accession}] Universe watch: {cascades} cascade(s) surfaced.",
            tags="universe_watch,agent_worker",
            status="active",
            created_by="universe_watch",
            ai_confidence=0.0,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Earnings preparation mode
# ---------------------------------------------------------------------------

def _build_earnings_prep_message(ticker: str, event_date: str, days_until: int, eps_estimate: str) -> str:
    ctx = _build_investor_context(ticker)
    ctx_section = f"\n\n{ctx}\n" if ctx else ""
    return (
        f"UPCOMING EARNINGS ALERT — {ticker} reports in {days_until} day(s) on {event_date}.{ctx_section}\n"
        f"EPS Estimate: {eps_estimate or 'not available'}\n\n"
        f"Your task: prepare a pre-earnings brief.\n"
        f"1. Fetch the most recent filing and financial data for {ticker}.\n"
        f"2. Identify the key metrics the market is focused on this quarter.\n"
        f"3. Compare current guidance vs. thesis expectations.\n"
        f"4. Highlight what a BEAT vs MISS would mean for the thesis.\n"
        f"5. List 2-3 specific things to watch in the earnings call.\n"
        f"Begin by fetching data, then output <FINAL_REPORT> + <STRUCTURED> when done."
    )


def _earnings_prep_already_done(ticker: str) -> bool:
    """Return True if a prep note for this ticker was written in the last 7 days."""
    try:
        con = pg_connect()
        if con is None:
            return False
        try:
            cur = con.cursor()
            cutoff = (datetime.datetime.now() - datetime.timedelta(days=7)).isoformat()
            cur.execute(
                """SELECT id FROM investor_annotations_core
                   WHERE entity_id=%s AND tags LIKE '%%earnings_prep%%' AND created_at >= %s LIMIT 1""",
                (ticker, cutoff),
            )
            return bool(cur.fetchone())
        finally:
            con.close()
    except Exception:
        return False


def run_earnings_prep(days_ahead: int = 3, dry_run: bool = False, debate: bool = True) -> list[dict]:
    """
    Find held tickers with earnings in the next `days_ahead` days.
    Run the agent in earnings-prep mode for each — writes a pre-earnings brief note.
    Skips tickers already prepped in the last 7 days.
    """
    from app.services.postgres_core_service import list_earnings_calendar_snapshot_pg

    today     = datetime.date.today()
    week_end  = (today + datetime.timedelta(days=days_ahead)).isoformat()
    week_start = today.isoformat()

    # Held tickers
    holdings  = get_holdings(limit=500)
    held      = {str(h.get("ticker") or "").strip().upper() for h in holdings if h.get("ticker")}
    if not held:
        print("[earnings_prep] No holdings — nothing to prep.")
        return []

    # Upcoming earnings
    cal_rows = list_earnings_calendar_snapshot_pg(week_start=week_start, week_end=week_end, limit=200)
    upcoming = [r for r in cal_rows if str(r.get("symbol") or "").upper() in held]

    if not upcoming:
        print(f"[earnings_prep] No earnings in next {days_ahead} days for held tickers.")
        return []

    results = []
    for cal in upcoming:
        ticker     = str(cal.get("symbol") or "").strip().upper()
        event_date = str(cal.get("event_date") or "")
        eps_est    = str(cal.get("eps_estimate") or "")

        if _earnings_prep_already_done(ticker):
            print(f"[earnings_prep] {ticker} — already prepped, skipping.")
            continue

        try:
            days_until = (datetime.date.fromisoformat(event_date) - today).days
        except Exception:
            days_until = days_ahead

        print(f"[earnings_prep] Running prep for {ticker} (earnings {event_date}, {days_until}d away)…")

        event = {
            "type":        "earnings_prep",
            "ticker":      ticker,
            "form":        "10-Q",
            "accession":   "",
            "filing_path": "",
            "filing_date": today.isoformat(),
        }

        # Override trigger message for earnings prep context
        # We monkey-patch _build_trigger_message behaviour via the event dict
        event["_override_trigger"] = _build_earnings_prep_message(ticker, event_date, days_until, eps_est)

        result = run(event, dry_run=dry_run, debate=debate)
        results.append({"ticker": ticker, "event_date": event_date, **result})

        # Tag the note as earnings_prep (update last annotation row)
        if not dry_run and result.get("status") == "ok":
            try:
                con = pg_connect()
                if con:
                    try:
                        cur = con.cursor()
                        cur.execute(
                            """UPDATE investor_annotations_core SET tags='ai,sec,earnings_prep'
                               WHERE entity_id=%s AND created_by='ai'
                               ORDER BY created_at DESC LIMIT 1""",
                            (ticker,),
                        )
                        con.commit()
                    finally:
                        con.close()
            except Exception:
                pass

        time.sleep(WATCH_DELAY_SEC)

    return results


def run_universe_watch(
    lookback_days: int = 1,
    dry_run:       bool = False,
    forms:         list[str] | None = None,
) -> list[dict]:
    """
    Scan recent filings from WATCHLIST tickers (non-held) for signals that
    affect our held positions.  Calls analyze_universe_signal_for_portfolio()
    which writes cascade alerts directly — no LLM loop needed.

    Returns a list of result dicts: {ticker, accession, cascades, relevant, ok}
    """
    # 1. Held tickers (for the cascade relevance check)
    holdings = get_holdings(limit=500)
    held = [str(h.get("ticker") or "").strip().upper() for h in holdings if h.get("ticker")]
    if not held:
        print("[universe_watch] No holdings — nothing to correlate against.")
        return []

    # 2. Watchlist tickers (the filing sources)
    watchlist_rows = list_watchlist_thesis_pg(limit=500)
    watchlist = {str(r.get("ticker") or "").strip().upper() for r in watchlist_rows if r.get("ticker")}
    # Remove held tickers — run_watch() already handles those
    universe = watchlist - set(held)
    if not universe:
        print("[universe_watch] No non-held watchlist tickers to scan.")
        return []

    # 3. Interesting form types only (8-K and 10-Q/10-K by default)
    target_forms = set(f.upper() for f in (forms or ["8-K", "10-Q", "10-K"]))

    # 4. Recent filings from universe tickers
    recent = _query_recent_filings(universe, lookback_days)
    recent = [f for f in recent if str(f.get("form") or "").upper() in target_forms]
    if not recent:
        print(f"[universe_watch] No recent {target_forms} filings for {len(universe)} watchlist tickers.")
        return []

    # 5. Skip already-processed accessions
    done = _accessions_universe_already_processed()
    results: list[dict] = []

    for i, filing in enumerate(recent):
        acc = filing["accession"]
        tk  = filing["ticker"]

        if acc and acc in done:
            print(f"[universe_watch] Skip {tk} {filing['form']} (already processed).")
            continue

        # Rate-limit between calls
        if i > 0 and WATCH_DELAY_SEC > 0:
            print(f"[universe_watch] Waiting {WATCH_DELAY_SEC}s …")
            time.sleep(WATCH_DELAY_SEC)

        dry_label = " [dry-run]" if dry_run else ""
        print(f"[universe_watch]{dry_label} {tk} {filing['form']} {acc}")

        # 6. Read filing text (up to 6000 chars)
        text = ""
        path = filing.get("path") or ""
        if path:
            try:
                text = read_filing_text_any(path, max_chars=6000) or ""
            except Exception:
                text = ""

        if not text:
            print(f"[universe_watch]   → No filing text for {tk}, skipping.")
            results.append({"ticker": tk, "accession": acc, "cascades": 0, "relevant": False, "ok": False, "error": "no_text"})
            continue

        # 7. Run the signal detector
        if dry_run:
            print(f"[universe_watch]   → [dry-run] Would call analyze_universe_signal_for_portfolio({tk})")
            results.append({"ticker": tk, "accession": acc, "cascades": 0, "relevant": False, "ok": True, "dry_run": True})
            continue

        out = analyze_universe_signal_for_portfolio(
            universe_ticker=tk,
            filing_text=text,
            filing_form=filing["form"],
            held_tickers=held,
        )

        cascades = int(out.get("cascades") or 0)
        relevant = bool(out.get("relevant"))
        ok       = bool(out.get("ok", True))

        print(f"[universe_watch]   → relevant={relevant}  cascades={cascades}  ok={ok}")

        # 8. Log that we processed this accession (prevents reprocessing)
        _log_universe_processed(tk, acc, cascades, dry_run)

        results.append({
            "ticker": tk, "accession": acc,
            "cascades": cascades, "relevant": relevant, "ok": ok,
        })

    relevant_count = sum(1 for r in results if r.get("relevant"))
    total_cascades = sum(int(r.get("cascades") or 0) for r in results)
    print(
        f"[universe_watch] Done. Scanned {len(results)} filing(s). "
        f"Relevant: {relevant_count}. Cascade alerts written: {total_cascades}."
    )
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Headless investment analysis agent (Phase 4.1 debate by default).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python tools/agent_worker.py --watch --days 1
  python tools/agent_worker.py --watch --days 7 --dry-run
  python tools/agent_worker.py --universe-watch --days 1
  python tools/agent_worker.py --universe-watch --days 7 --dry-run
  python tools/agent_worker.py --ticker IT --form 10-Q --dry-run
  python tools/agent_worker.py --ticker IT --form 10-Q --no-debate
  python tools/agent_worker.py --ticker AAPL --form 10-Q --accession 0001193125-26-012345
  python tools/agent_worker.py --earnings-prep --days-ahead 3
  python tools/agent_worker.py --earnings-prep --days-ahead 5 --dry-run
""",
    )
    p.add_argument("--watch",          action="store_true", help="Scan all held tickers for new filings.")
    p.add_argument("--universe-watch", action="store_true", dest="universe_watch",
                   help="Scan watchlist (non-held) tickers for signals that affect held positions.")
    p.add_argument("--watch-all",      action="store_true", dest="watch_all",
                   help="Run --watch then --universe-watch in sequence (used by Cloud Run job).")
    p.add_argument("--days",      type=int, default=1,   metavar="N", help="Look-back window for --watch/--universe-watch.")
    p.add_argument("--ticker",    metavar="TICKER")
    p.add_argument("--form",      metavar="FORM",  default="")
    p.add_argument("--accession", metavar="ACC",   default="")
    p.add_argument("--filing-date", metavar="YYYY-MM-DD", default="")
    p.add_argument("--dry-run",       action="store_true", help="Full run, no DB writes.")
    p.add_argument("--no-debate",     action="store_true", help="Single-perspective mode (faster/cheaper).")
    p.add_argument("--json",          action="store_true", dest="output_json", help="Output JSON.")
    p.add_argument("--event-uid",     metavar="UID", default="",
                   help="(stub) Future: look up filing event by UID.")
    p.add_argument("--earnings-prep", action="store_true", dest="earnings_prep",
                   help="Run pre-earnings brief for held tickers with upcoming earnings.")
    p.add_argument("--days-ahead",    type=int, default=3, dest="days_ahead",
                   help="Days ahead to scan for upcoming earnings (default 3, used with --earnings-prep).")
    return p


def main() -> None:
    parser = _build_parser()
    args   = parser.parse_args()
    debate = not args.no_debate

    # Clean up any runs left stuck in 'running' from previous crashed workers
    try:
        cleaned = cleanup_stuck_agent_runs(stale_minutes=60)
        if cleaned:
            print(f"[agent_worker] Cleaned up {cleaned} stuck agent run(s)", file=sys.stderr)
    except Exception:
        pass

    if args.event_uid:
        print("[agent_worker] --event-uid not yet implemented. Use --ticker --form --accession.", file=sys.stderr)
        sys.exit(1)

    if args.watch_all:
        held_results = run_watch(lookback_days=max(1, args.days), dry_run=args.dry_run, debate=debate)
        uni_results  = run_universe_watch(lookback_days=max(1, args.days), dry_run=args.dry_run)
        combined = {"held": held_results, "universe": uni_results}
        if args.output_json:
            print(json.dumps(combined, indent=2, default=str))
        sys.exit(0)

    if args.universe_watch:
        results = run_universe_watch(lookback_days=max(1, args.days), dry_run=args.dry_run)
        if args.output_json:
            print(json.dumps(results, indent=2, default=str))
        sys.exit(0)

    if args.earnings_prep:
        results = run_earnings_prep(days_ahead=max(1, args.days_ahead), dry_run=args.dry_run, debate=debate)
        if args.output_json:
            print(json.dumps(results, indent=2, default=str))
        sys.exit(0)

    if args.watch:
        results = run_watch(lookback_days=max(1, args.days), dry_run=args.dry_run, debate=debate)
        if args.output_json:
            print(json.dumps(results, indent=2, default=str))
        sys.exit(0)

    if not args.ticker:
        parser.error("Either --watch or --ticker is required.")

    event: dict[str, Any] = {
        "type":        "manual",
        "ticker":      args.ticker.strip().upper(),
        "form":        args.form.strip(),
        "accession":   args.accession.strip(),
        "filing_path": "",
        "filing_date": args.filing_date or datetime.date.today().isoformat(),
    }

    result = run(event, dry_run=args.dry_run, debate=debate)

    if args.output_json:
        print(json.dumps(result, indent=2, default=str))
    else:
        dry_label = "  [DRY RUN]" if args.dry_run else ""
        mode      = result.get("mode", "?")
        print(f"\nMode   : {mode}{dry_label}")
        print(f"Status : {result.get('status')}")
        print(f"Steps  : {result.get('steps')}")
        s = result.get("structured", {})
        if s:
            print(f"Sentiment     : {s.get('sentiment','?')}")
            print(f"Thesis status : {s.get('thesis_status','?')}")
            print(f"Confidence    : {s.get('confidence','?')}")
        report = result.get("report", "")
        if report:
            print(f"\n--- REPORT ---\n{report}\n--- END ---")
        else:
            print("\n[No report generated]")
        if result.get("mode") == "debate":
            print(f"\n--- BULL ---\n{result.get('bull_case','')[:500]}")
            print(f"\n--- BEAR ---\n{result.get('bear_case','')[:500]}")

    sys.exit(0 if result.get("status") == "ok" else 1)


if __name__ == "__main__":
    main()
