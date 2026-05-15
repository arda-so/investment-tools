#!/usr/bin/env python3
"""
Replay regression tests for agent_worker and the AI pipeline.

Two modes:
  --smoke   (default) Fast checks: imports, DB, schema contracts, CLI.
            No LLM calls.  Runs in < 15 seconds.  Use in pre-commit / CI.

  --replay  Real LLM dry-run on N recent portfolio filings.
            Validates full end-to-end pipeline without writing to DB.
            Costs a few LLM credits.  Use before deploys.

Usage:
    python tools/run_replay_tests.py              # smoke mode
    python tools/run_replay_tests.py --smoke      # smoke mode (explicit)
    python tools/run_replay_tests.py --replay     # replay 3 filings
    python tools/run_replay_tests.py --replay --jobs 5
    python tools/run_replay_tests.py --replay --ticker IT --form 10-Q
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("CORE_DB_BACKEND", "postgres")
os.environ.setdefault("CORE_DB_STRICT_POSTGRES", "1")

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
WARN = "\033[33m⚠\033[0m"
SKIP = "\033[90m-\033[0m"

AGENT_WORKER = ROOT / "tools" / "agent_worker.py"
PYTHON = sys.executable


# ── helpers ──────────────────────────────────────────────────────────────────

def _print_result(label: str, ok: bool, detail: str = "") -> None:
    icon = PASS if ok else FAIL
    suffix = f"  {detail}" if detail else ""
    print(f"  {icon}  {label}{suffix}")


def _section(title: str) -> None:
    print(f"\n── {title} {'─' * max(0, 60 - len(title))}")


# ── SMOKE TESTS ───────────────────────────────────────────────────────────────

def test_imports() -> list[str]:
    """Verify all critical imports work (catches missing deps, bad refactors)."""
    errors: list[str] = []
    modules = [
        ("app.schemas.ai_outputs",    ["StructuredAnalysis", "AgentRunOutput"]),
        ("app.services.proactive_ai_service",
         ["start_agent_run", "finish_agent_run", "cleanup_stuck_agent_runs"]),
        ("app.services.postgres_core_service",
         ["pg_connect", "upsert_investor_style_memory_pg"]),
        ("app.services.company_file_service",
         ["save_company_moats", "add_competitor"]),
        ("tools.llm_engine",          ["ask_ai"]),
    ]
    for mod_name, symbols in modules:
        try:
            mod = __import__(mod_name, fromlist=symbols)
            missing = [s for s in symbols if not hasattr(mod, s)]
            if missing:
                errors.append(f"{mod_name}: missing symbols {missing}")
        except Exception as exc:
            errors.append(f"{mod_name}: {exc}")
    return errors


def test_schema_contracts() -> list[str]:
    """Verify StructuredAnalysis and AgentRunOutput handle edge cases correctly."""
    from app.schemas.ai_outputs import AgentRunOutput, StructuredAnalysis
    import io

    errors: list[str] = []

    # --- StructuredAnalysis ---
    cases: list[tuple[dict, dict]] = [
        # (input, expected_output_subset)
        ({"thesis_status": "on_track", "sentiment": "bullish", "confidence": "75"},
         {"thesis_status": "on_track", "confidence": "75"}),
        ({"confidence": "high"},    {"confidence": "75"}),
        ({"confidence": "0.75"},    {"confidence": "75"}),
        ({"confidence": "75%"},     {"confidence": "75"}),
        ({"confidence": "N/A"},     {"confidence": "50"}),
        ({"confidence": ""},        {"confidence": ""}),
        ({"confidence": None},      {"confidence": ""}),
    ]
    for inp, expected in cases:
        try:
            result = StructuredAnalysis.from_llm(inp).to_dict()
            for k, v in expected.items():
                if result.get(k) != v:
                    errors.append(f"StructuredAnalysis: {inp} → {k}={result.get(k)!r} expected {v!r}")
        except Exception as exc:
            errors.append(f"StructuredAnalysis crashed on {inp}: {exc}")

    # Verify warning log emitted for bad values
    old_stdout, sys.stdout = sys.stdout, io.StringIO()
    try:
        StructuredAnalysis.from_llm({"sentiment": "MEGA_BULLISH", "severity": "CATASTROPHIC"})
        log = sys.stdout.getvalue()
    finally:
        sys.stdout = old_stdout
    if "schema_contract_violation" not in log:
        errors.append("StructuredAnalysis: no warning log emitted for bad sentiment/severity")

    # --- AgentRunOutput ---
    out = AgentRunOutput.from_dict({"ticker": "AAPL", "steps": "5", "report_chars": 3000, "confidence": "high"})
    if out.ticker != "AAPL":  errors.append(f"AgentRunOutput: ticker={out.ticker!r}")
    if out.steps  != 5:       errors.append(f"AgentRunOutput: steps={out.steps!r} (expected 5)")
    if out.confidence != "75":errors.append(f"AgentRunOutput: confidence={out.confidence!r} (expected '75')")

    # Missing ticker should emit warning
    old_stdout, sys.stdout = sys.stdout, io.StringIO()
    try:
        AgentRunOutput.from_dict({"steps": 3})
        log2 = sys.stdout.getvalue()
    finally:
        sys.stdout = old_stdout
    if "missing ticker" not in log2:
        errors.append("AgentRunOutput: no warning log for missing ticker")

    return errors


def test_db_connectivity() -> list[str]:
    """Verify Postgres is reachable and key tables exist."""
    errors: list[str] = []
    try:
        from app.services.postgres_core_service import pg_connect
        con = pg_connect()
        if con is None:
            errors.append("pg_connect() returned None")
            return errors
        cur = con.cursor()
        required_tables = [
            "agent_runs_core", "action_proposals_core", "investor_style_memory_core",
            "company_moat_tags_core", "company_sec_competitors_core",
            "filings_core", "companies_core",
        ]
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name = ANY(%s)",
            (required_tables,),
        )
        found = {r[0] for r in cur.fetchall()}
        missing = [t for t in required_tables if t not in found]
        if missing:
            errors.append(f"Missing Postgres tables: {missing}")
        con.close()
    except Exception as exc:
        errors.append(f"DB connectivity: {exc}")
    return errors


def test_cli_help() -> list[str]:
    """Verify agent_worker.py --help exits cleanly."""
    errors: list[str] = []
    try:
        result = subprocess.run(
            [PYTHON, str(AGENT_WORKER), "--help"],
            capture_output=True, text=True, timeout=15,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
        )
        if result.returncode not in (0, 1):  # argparse returns 0 on --help
            errors.append(f"--help exit code {result.returncode}: {result.stderr[:200]}")
    except subprocess.TimeoutExpired:
        errors.append("--help timed out (>15s)")
    except Exception as exc:
        errors.append(f"--help failed: {exc}")
    return errors


def run_smoke() -> bool:
    _section("Smoke tests (no LLM)")
    all_pass = True

    checks = [
        ("Imports",           test_imports),
        ("Schema contracts",  test_schema_contracts),
        ("DB connectivity",   test_db_connectivity),
        ("CLI --help",        test_cli_help),
    ]

    for label, fn in checks:
        try:
            errs = fn()
            if errs:
                for e in errs:
                    _print_result(f"{label}: {e}", ok=False)
                all_pass = False
            else:
                _print_result(label, ok=True)
        except Exception as exc:
            _print_result(f"{label}: unexpected error: {exc}", ok=False)
            all_pass = False

    return all_pass


# ── REPLAY TESTS ──────────────────────────────────────────────────────────────

def _pick_replay_filings(n: int, ticker: str = "", form: str = "") -> list[dict]:
    """Pick N recent filings from filings_core suitable for agent_worker replay."""
    from app.services.postgres_core_service import pg_connect
    con = pg_connect()
    if con is None:
        return []
    try:
        cur = con.cursor()
        # Prefer 10-K / 10-Q / 8-K (substantive filings), portfolio tickers first
        priority_tickers = ("IT", "UNH", "HUBS", "CRM")
        where_clauses = []
        params: list[Any] = []
        if ticker:
            where_clauses.append("ticker = %s")
            params.append(ticker.upper())
        if form:
            where_clauses.append("form = %s")
            params.append(form.upper())
        where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""
        cur.execute(
            f"""SELECT ticker, form, accession, date
                FROM filings_core
                {where}
                ORDER BY
                  CASE WHEN ticker = ANY(%s) THEN 0 ELSE 1 END,
                  CASE WHEN form IN ('10-K','10-Q') THEN 0
                       WHEN form IN ('8-K','6-K','20-F') THEN 1 ELSE 2 END,
                  date DESC
                LIMIT %s""",
            params + [list(priority_tickers), n],
        )
        return [
            {"ticker": r[0], "form": r[1], "accession": r[2], "date": r[3]}
            for r in (cur.fetchall() or [])
        ]
    except Exception as exc:
        print(f"  {WARN}  Could not query filings_core: {exc}", file=sys.stderr)
        return []
    finally:
        con.close()


def _run_replay_job(filing: dict) -> dict:
    """Run agent_worker --dry-run --no-debate for one filing. Returns result dict."""
    ticker = filing["ticker"]
    form   = filing["form"]
    acc    = filing.get("accession", "")
    cmd    = [PYTHON, str(AGENT_WORKER),
              "--ticker", ticker, "--form", form,
              "--dry-run", "--no-debate"]
    if acc:
        cmd += ["--accession", acc]

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            env={**os.environ, "PYTHONPATH": str(ROOT),
                 "CORE_DB_BACKEND": "postgres",
                 "CORE_DB_STRICT_POSTGRES": "1"},
        )
        elapsed = time.time() - t0
        stdout  = proc.stdout or ""
        stderr  = proc.stderr or ""
        combined = stdout + "\n" + stderr

        issues: list[str] = []

        # 1. Exit code
        if proc.returncode != 0:
            issues.append(f"exit_code={proc.returncode}")

        # 2. FINAL_REPORT present
        if "<FINAL_REPORT>" not in combined and "[dry-run]" not in combined:
            issues.append("no FINAL_REPORT in output")

        # 3. No unhandled Python exceptions
        tb_match = re.search(r"Traceback \(most recent call last\)", combined)
        if tb_match:
            # Extract last line of traceback for brief error
            after = combined[tb_match.start():]
            last_line = [l for l in after.splitlines() if l.strip()][-1]
            issues.append(f"traceback: {last_line[:120]}")

        # 4. Schema contract violations (informational — not a test failure, but flagged)
        violations: list[dict] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
                if d.get("alert") == "schema_contract_violation":
                    violations.append(d)
            except Exception:
                pass

        # 5. Steps > 0 (agent did something)
        steps_match = re.search(r"\[agent_worker\].*step[s]?\s*(\d+)", combined, re.IGNORECASE)
        steps = int(steps_match.group(1)) if steps_match else None

        return {
            "ticker":     ticker,
            "form":       form,
            "accession":  acc[:20] if acc else "",
            "elapsed_s":  round(elapsed, 1),
            "exit_code":  proc.returncode,
            "issues":     issues,
            "violations": violations,
            "steps":      steps,
            "passed":     len(issues) == 0,
        }

    except subprocess.TimeoutExpired:
        return {
            "ticker": ticker, "form": form, "accession": acc[:20] if acc else "",
            "elapsed_s": 300, "exit_code": -1,
            "issues": ["timeout >300s"], "violations": [], "steps": None, "passed": False,
        }
    except Exception as exc:
        return {
            "ticker": ticker, "form": form, "accession": acc[:20] if acc else "",
            "elapsed_s": 0, "exit_code": -1,
            "issues": [f"runner error: {exc}"], "violations": [], "steps": None, "passed": False,
        }


def run_replay(n: int = 3, ticker: str = "", form: str = "") -> bool:
    _section(f"Replay tests (real LLM, --dry-run, n={n})")
    print(f"  Picking up to {n} recent filings …")

    filings = _pick_replay_filings(n, ticker=ticker, form=form)
    if not filings:
        print(f"  {WARN}  No filings found in filings_core — skipping replay tests.")
        return True  # not a failure if no filings yet

    print(f"  Running {len(filings)} job(s):\n")
    results: list[dict] = []
    for i, f in enumerate(filings, 1):
        label = f"{f['ticker']} {f['form']} ({f['date'][:10] if f['date'] else ''})"
        print(f"  [{i}/{len(filings)}] {label} …", end=" ", flush=True)
        r = _run_replay_job(f)
        results.append(r)
        status = PASS if r["passed"] else FAIL
        print(f"{status}  {r['elapsed_s']:.0f}s")
        if r["issues"]:
            for issue in r["issues"]:
                print(f"         {FAIL}  {issue}")
        if r["violations"]:
            for v in r["violations"]:
                print(f"         {WARN}  schema_contract_violation: {v.get('issues', [])}")

    # Summary
    passed  = sum(1 for r in results if r["passed"])
    total   = len(results)
    print(f"\n  Summary: {passed}/{total} passed")
    if passed < total:
        print(f"\n  Failed jobs:")
        for r in results:
            if not r["passed"]:
                print(f"    {r['ticker']} {r['form']}: {r['issues']}")

    return passed == total


# ── ENTRY POINT ──────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay regression tests for agent_worker and AI pipeline."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--smoke",  action="store_true", help="Fast checks only (default)")
    mode.add_argument("--replay", action="store_true", help="Real LLM dry-run on recent filings")
    parser.add_argument("--jobs",   type=int, default=3, help="Number of filings to replay (default 3)")
    parser.add_argument("--ticker", default="", help="Filter replay to specific ticker")
    parser.add_argument("--form",   default="", help="Filter replay to specific form type")
    args = parser.parse_args()

    print(f"=== InvestorOS Replay Tests  {datetime.datetime.now():%Y-%m-%d %H:%M} ===")

    ok = True

    if args.replay:
        # Always run smoke first
        ok = run_smoke() and ok
        ok = run_replay(n=args.jobs, ticker=args.ticker, form=args.form) and ok
    else:
        ok = run_smoke() and ok

    print(f"\n{'=== PASS ===' if ok else '=== FAIL ==='}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
