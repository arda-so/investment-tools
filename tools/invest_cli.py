#!/usr/bin/env python3
"""
invest_cli.py — Whitelisted command registry + dispatcher for the headless agent worker.

No eval, no shell=True, no subprocess.  Pure Python function dispatch only.

Usage (from another module):
    from tools.invest_cli import execute
    result = execute("> invest_app get_financials --ticker AAPL")

Usage (self-test):
    python tools/invest_cli.py --selftest
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# Service imports (lazy — no explicit DB init needed at module load)
# ---------------------------------------------------------------------------
from app.services.postgres_core_service import (
    add_investor_note_pg,
    add_workspace_journal_note_pg,
    get_company_intel_pg,
    get_mini_statements_pg,
    get_price_metrics_pg,
    list_recent_notes_pg,
    list_watchlist_thesis_pg,
    pg_connect,
)
from app.services.portfolio_memory_service import get_holdings
from app.core.filing_text import read_filing_text_any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_ticker(raw: str) -> str:
    return re.sub(r"[^A-Z0-9.\-]", "", str(raw or "").strip().upper())[:16]


def _cap(val: Any, n: int) -> str:
    return str(val or "")[:n]


def _dict_to_text(d: dict, indent: int = 0) -> str:
    lines = []
    pad = " " * indent
    for k, v in (d or {}).items():
        if isinstance(v, dict):
            lines.append(f"{pad}{k}:")
            lines.append(_dict_to_text(v, indent + 2))
        elif isinstance(v, list):
            lines.append(f"{pad}{k}: [{', '.join(str(x) for x in v[:10])}]")
        else:
            lines.append(f"{pad}{k}: {v}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------

def _cmd_fetch_filings(ticker: str, limit: str = "5") -> str:
    tk = _safe_ticker(ticker)
    if not tk:
        return "[Error]: ticker is required."
    lim = max(1, min(50, int(limit or 5)))
    con = pg_connect()
    if con is None:
        return "[Error]: DB unavailable."
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT form_type, filed_at, accession_no, local_path
            FROM filings_core
            WHERE ticker = %s
            ORDER BY filed_at DESC
            LIMIT %s
            """,
            (tk, lim),
        )
        rows = cur.fetchall() or []
        if not rows:
            return f"No filings found for {tk}."
        lines = [f"Recent filings for {tk}:"]
        for r in rows:
            form = str(r[0] or "")
            date = str(r[1] or "")[:10]
            acc = str(r[2] or "")
            path = str(r[3] or "")
            lines.append(f"  {form}  {date}  accession={acc}  path={path}")
        return "\n".join(lines)
    except Exception as exc:
        return f"[Error]: {exc}"
    finally:
        con.close()


def _cmd_get_financials(ticker: str) -> str:
    tk = _safe_ticker(ticker)
    if not tk:
        return "[Error]: ticker is required."
    data = get_mini_statements_pg(tk)
    if not data:
        return f"No financial data found for {tk}."
    return f"Financials for {tk}:\n{_dict_to_text(data)}"


def _cmd_get_intel(ticker: str) -> str:
    tk = _safe_ticker(ticker)
    if not tk:
        return "[Error]: ticker is required."
    data = get_company_intel_pg(tk)
    if not data:
        return f"No intel data found for {tk}."
    return f"Intel for {tk}:\n{_dict_to_text(data)}"


def _cmd_get_price(ticker: str) -> str:
    tk = _safe_ticker(ticker)
    if not tk:
        return "[Error]: ticker is required."
    data = get_price_metrics_pg(tk)
    if not data:
        return f"No price metrics found for {tk}."
    return f"Price metrics for {tk}:\n{_dict_to_text(data)}"


def _cmd_list_holdings(limit: str = "50") -> str:
    lim = max(1, min(50, int(limit or 50)))
    holdings = get_holdings(limit=lim)
    if not holdings:
        return "No holdings found."
    lines = ["Holdings:"]
    for h in holdings[:lim]:
        tk = str(h.get("ticker") or "")
        weight = str(h.get("weight") or h.get("pct_weight") or "")
        pl = str(h.get("unrealized_pnl") or h.get("pnl") or "")
        lines.append(f"  {tk:<8} weight={weight}  P&L={pl}")
    return "\n".join(lines)


def _cmd_get_thesis(ticker: str) -> str:
    tk = _safe_ticker(ticker)
    if not tk:
        return "[Error]: ticker is required."
    rows = list_watchlist_thesis_pg(limit=300)
    row = next((r for r in rows if str(r.get("ticker") or "").strip().upper() == tk), None)
    if not row:
        return f"No thesis found for {tk}."
    lines = [f"Thesis for {tk}:"]
    for key in ("thesis_summary", "time_horizon", "invalidation_criteria", "strategy_tag"):
        val = str(row.get(key) or "")
        if val:
            lines.append(f"  {key}: {val}")
    return "\n".join(lines)


def _cmd_list_notes(ticker: str = "", limit: str = "10") -> str:
    lim = max(1, min(50, int(limit or 10)))
    con = pg_connect()
    if con is None:
        return "[Error]: DB unavailable."
    try:
        cur = con.cursor()
        if ticker:
            tk = _safe_ticker(ticker)
            cur.execute(
                """
                SELECT entity_id, content, created_at
                FROM investor_annotations_core
                WHERE annotation_type IN ('note', 'log')
                  AND entity_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (tk, lim),
            )
        else:
            cur.execute(
                """
                SELECT entity_id, content, created_at
                FROM investor_annotations_core
                WHERE annotation_type IN ('note', 'log')
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (lim,),
            )
        rows = cur.fetchall() or []
        if not rows:
            label = f" for {_safe_ticker(ticker)}" if ticker else ""
            return f"No notes found{label}."
        lines = [f"Notes ({len(rows)}):"]
        for r in rows:
            tk_label = str(r[0] or "")
            date = str(r[2] or "")[:10]
            text = str(r[1] or "")[:200]
            lines.append(f"  [{date}] {tk_label}: {text}")
        return "\n".join(lines)
    except Exception as exc:
        return f"[Error]: {exc}"
    finally:
        con.close()


_CHUNK_SIZE = 6000
_MAX_OFFSET = 36000  # cap at 6 chunks (36KB total readable)


def _cmd_read_filing(ticker: str, form: str, accession: str = "", offset: str = "0") -> str:
    tk = _safe_ticker(ticker)
    if not tk:
        return "[Error]: ticker is required."
    form_safe = re.sub(r"[^A-Za-z0-9\-/]", "", str(form or ""))[:20]
    if not form_safe:
        return "[Error]: form is required."
    try:
        off = max(0, min(_MAX_OFFSET, int(offset or 0)))
    except (ValueError, TypeError):
        off = 0

    # Look up local_path from DB
    con = pg_connect()
    path = ""
    if con is not None:
        try:
            cur = con.cursor()
            if accession:
                acc_safe = re.sub(r"[^A-Za-z0-9\-]", "", str(accession or ""))[:40]
                cur.execute(
                    "SELECT local_path FROM filings_core WHERE ticker=%s AND form_type=%s AND accession_no=%s LIMIT 1",
                    (tk, form_safe, acc_safe),
                )
            else:
                cur.execute(
                    "SELECT local_path FROM filings_core WHERE ticker=%s AND form_type=%s ORDER BY filed_at DESC LIMIT 1",
                    (tk, form_safe),
                )
            row = cur.fetchone()
            if row:
                path = str(row[0] or "")
        except Exception:
            pass
        finally:
            con.close()

    if not path:
        return f"[Error]: No filing path found for {tk} {form_safe}."

    try:
        # Read enough to serve the requested chunk
        full = read_filing_text_any(path, max_chars=off + _CHUNK_SIZE)
        if not full:
            return f"[Error]: Filing file empty or unreadable: {path}"
        chunk = full[off: off + _CHUNK_SIZE]
        if not chunk:
            return f"Filing text ({tk} {form_safe}): [offset {off} is past end of available content ({len(full)} chars total)]"
        total_hint = f" [chars {off}–{off + len(chunk)} of ~{len(full)}+]"
        next_hint = f" [use --offset {off + _CHUNK_SIZE} to read next chunk]" if len(chunk) == _CHUNK_SIZE else " [end of content]"
        return f"Filing text ({tk} {form_safe}){total_hint}{next_hint}:\n{chunk}"
    except Exception as exc:
        return f"[Error]: Could not read filing: {exc}"


def _cmd_post_note(ticker: str, text: str) -> str:
    tk = _safe_ticker(ticker)
    if not tk:
        return "[Error]: ticker is required."
    note_text = _cap(text, 4000).strip()
    if not note_text:
        return "[Error]: text is required."
    ok = add_investor_note_pg(
        scope="agent_worker",
        ticker=tk,
        sentiment="neutral",
        note=note_text,
        tags="ai,sec",
        status="approved",
        created_by="ai",
        ai_confidence=0.85,
    )
    if ok:
        return f"Note posted for {tk} ({len(note_text)} chars)."
    return "[Error]: Failed to post note (DB write returned False)."


def _cmd_post_journal(ticker: str, text: str, action: str = "agent_worker") -> str:
    tk = _safe_ticker(ticker)
    note_text = _cap(text, 4000).strip()
    if not note_text:
        return "[Error]: text is required."
    action_safe = _cap(action, 80) or "agent_worker"
    ok = add_workspace_journal_note_pg(
        ticker=tk,
        note=note_text,
        action=action_safe,
        emotion="Calm",
        created_by="ai",
    )
    if ok:
        return f"Journal entry posted ({len(note_text)} chars)."
    return "[Error]: Failed to post journal entry (DB write returned False)."


# ---------------------------------------------------------------------------
# Command registries
# ---------------------------------------------------------------------------

# Read-only commands exposed to the LLM agent.
# post_note / post_journal are intentionally excluded: the agent must not
# call them directly; _persist_outputs() in agent_worker.py is the sole
# write path (ensures correct dedup tagging and dry-run support).
COMMAND_REGISTRY: dict[str, tuple[Callable, list[str]]] = {
    "fetch_filings":  (_cmd_fetch_filings,  ["ticker"]),
    "get_financials": (_cmd_get_financials,  ["ticker"]),
    "get_intel":      (_cmd_get_intel,       ["ticker"]),
    "get_price":      (_cmd_get_price,       ["ticker"]),
    "list_holdings":  (_cmd_list_holdings,   []),
    "get_thesis":     (_cmd_get_thesis,      ["ticker"]),
    "list_notes":     (_cmd_list_notes,      []),
    "read_filing":    (_cmd_read_filing,     ["ticker", "form"]),
}

# Write commands available for selftest and manual use, but NOT advertised
# to the LLM system prompt.
WRITE_REGISTRY: dict[str, tuple[Callable, list[str]]] = {
    "post_note":    (_cmd_post_note,    ["ticker", "text"]),
    "post_journal": (_cmd_post_journal, ["ticker", "text"]),
}

# Combined registry used only by the selftest / manual execute path.
_ALL_REGISTRY: dict[str, tuple[Callable, list[str]]] = {**COMMAND_REGISTRY, **WRITE_REGISTRY}

# ---------------------------------------------------------------------------
# Argument parser (no shlex / no shell)
# ---------------------------------------------------------------------------

_ARG_RE = re.compile(
    r"--(?P<key>[a-zA-Z_][a-zA-Z0-9_-]*)\s+"
    r'(?:"(?P<qval>[^"\\]*(?:\\.[^"\\]*)*)"|(?P<val>\S+))'
)


def _parse(command_line: str) -> tuple[str, dict[str, str]]:
    """
    Parse "> invest_app <cmd> [--key value ...]" safely.
    Returns (cmd_name, kwargs_dict).
    Raises ValueError on malformed input.
    """
    line = command_line.strip()
    # Strip leading "> invest_app" prefix (case-insensitive for robustness)
    line = re.sub(r"^>\s*invest_app\s*", "", line, flags=re.IGNORECASE).strip()

    # Extract command name (first token, no dashes)
    parts = line.split(None, 1)
    if not parts:
        raise ValueError("Empty command.")
    cmd = parts[0].strip().lower()
    remainder = parts[1] if len(parts) > 1 else ""

    kwargs: dict[str, str] = {}
    for m in _ARG_RE.finditer(remainder):
        key = m.group("key").replace("-", "_").lower()
        val = m.group("qval") if m.group("qval") is not None else m.group("val")
        kwargs[key] = val

    return cmd, kwargs


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def execute(command_line: str) -> str:
    """
    Execute a single "> invest_app <cmd> [--key value ...]" command.
    Only commands in COMMAND_REGISTRY (read-only) are accepted.
    Returns plain-text result or an [Error]: ... message.
    """
    try:
        cmd, kwargs = _parse(command_line)
    except ValueError as exc:
        return f"[Error]: Parse failure: {exc}"

    if cmd not in COMMAND_REGISTRY:
        allowed = sorted(COMMAND_REGISTRY)
        return f"[Error]: Unknown command '{cmd}'. Allowed: {allowed}"

    fn, required = COMMAND_REGISTRY[cmd]
    missing = [r for r in required if r not in kwargs]
    if missing:
        return f"[Error]: Missing required args for '{cmd}': {missing}"

    try:
        return str(fn(**kwargs))
    except TypeError as exc:
        return f"[Error]: Bad args for '{cmd}': {exc}"
    except Exception as exc:
        return f"[Error]: {exc}"


def execute_write(command_line: str) -> str:
    """
    Execute a write command (post_note / post_journal) from the manual selftest path.
    Not called by the LLM agent loop.
    """
    try:
        cmd, kwargs = _parse(command_line)
    except ValueError as exc:
        return f"[Error]: Parse failure: {exc}"

    if cmd not in _ALL_REGISTRY:
        allowed = sorted(_ALL_REGISTRY)
        return f"[Error]: Unknown command '{cmd}'. Allowed: {allowed}"

    fn, required = _ALL_REGISTRY[cmd]
    missing = [r for r in required if r not in kwargs]
    if missing:
        return f"[Error]: Missing required args for '{cmd}': {missing}"

    try:
        return str(fn(**kwargs))
    except TypeError as exc:
        return f"[Error]: Bad args for '{cmd}': {exc}"
    except Exception as exc:
        return f"[Error]: {exc}"


# ---------------------------------------------------------------------------
# Self-test mode
# ---------------------------------------------------------------------------

def _selftest() -> None:
    print("=== invest_cli self-test ===\n")
    read_cases = [
        "> invest_app list_holdings",
        "> invest_app list_notes --limit 3",
        "> invest_app list_notes --ticker IT --limit 5",
        "> invest_app get_thesis --ticker IT",
        "> invest_app get_financials --ticker IT",
        "> invest_app get_price --ticker IT",
        "> invest_app get_intel --ticker IT",
        "> invest_app fetch_filings --ticker IT --limit 3",
    ]
    error_cases = [
        "> invest_app unknown_cmd",
        "> invest_app get_financials",
        # post_note is now in WRITE_REGISTRY, not COMMAND_REGISTRY
        "> invest_app post_note --text missing_ticker",
    ]
    write_cases = [
        # These go through execute_write, not execute
        # Uncomment carefully — they write to DB:
        # "> invest_app post_note --ticker IT --text \"selftest note\"",
        # "> invest_app post_journal --ticker IT --text \"selftest journal\"",
    ]
    all_cases = [("read", c, execute) for c in read_cases]
    all_cases += [("error", c, execute) for c in error_cases]
    all_cases += [("write", c, execute_write) for c in write_cases]
    for kind, cmd_line, fn in all_cases:
        print(f"[{kind}] {cmd_line}")
        result = fn(cmd_line)
        print(result[:500])
        print()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        print("Usage: python tools/invest_cli.py --selftest")
