#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.core.ticker import normalize_ticker as _normalize_ticker

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CORE_DB = DATA_DIR / "core.db"
PORTFOLIO_CSV = DATA_DIR / "portfolio.csv"
WATCHLIST_TXT = DATA_DIR / "my_watchlist.txt"
SYNC_STATE_JSON = DATA_DIR / "sec_sync_state.json"


def _load_portfolio() -> list[str]:
    out: list[str] = []
    if not PORTFOLIO_CSV.exists():
        return out
    with PORTFOLIO_CSV.open("r", encoding="utf-8", errors="ignore", newline="") as fh:
        for row in csv.reader(fh):
            if not row:
                continue
            t = _normalize_ticker(row[0] if row else "")
            if t:
                out.append(t)
    return out


def _load_watchlist() -> list[str]:
    out: list[str] = []
    if not WATCHLIST_TXT.exists():
        return out
    for ln in WATCHLIST_TXT.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        t = _normalize_ticker(s.split(",", 1)[0])
        if t:
            out.append(t)
    return out


def _load_blue_chips(db_path: Path) -> list[str]:
    out: list[str] = []
    if not db_path.exists():
        return out
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        con.execute(
            """CREATE TABLE IF NOT EXISTS blue_chips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL UNIQUE,
                added_at TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT ''
            )"""
        )
        rows = con.execute("SELECT ticker FROM blue_chips ORDER BY added_at DESC, ticker ASC").fetchall()
        for r in rows:
            t = _normalize_ticker(str(r["ticker"] or ""))
            if t:
                out.append(t)
    except Exception:
        return []
    finally:
        con.close()
    return out


def _load_sync_state() -> dict[str, dict[str, str]]:
    try:
        if not SYNC_STATE_JSON.exists():
            return {}
        obj = json.loads(SYNC_STATE_JSON.read_text(encoding="utf-8", errors="ignore"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _write_sync_state(state: dict[str, dict[str, str]]) -> None:
    SYNC_STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=str(SYNC_STATE_JSON.parent), encoding="utf-8") as tmp:
        tmp.write(payload)
        tmp_path = Path(tmp.name)
    tmp_path.replace(SYNC_STATE_JSON)


def _set_state(ticker: str, *, running: str, result: str, message: str) -> None:
    t = _normalize_ticker(ticker)
    if not t:
        return
    state = _load_sync_state()
    state[t] = {
        "running": str(running),
        "last": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "result": str(result),
        "message": str(message),
    }
    _write_sync_state(state)


def _filing_count_map(tickers: list[str], db_path: Path) -> dict[str, int]:
    out = {t: 0 for t in tickers}
    if not tickers or not db_path.exists():
        return out
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    marks = ",".join("?" for _ in tickers)
    try:
        rows = con.execute(
            f"SELECT ticker, COUNT(*) AS c FROM filings WHERE ticker IN ({marks}) GROUP BY ticker",
            tuple(tickers),
        ).fetchall()
        for r in rows:
            t = _normalize_ticker(str(r["ticker"] or ""))
            if t:
                out[t] = int(r["c"] or 0)
    except Exception:
        pass
    finally:
        con.close()
    return out


def _max_filing_id(db_path: Path) -> int:
    if not db_path.exists():
        return 0
    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute("SELECT COALESCE(MAX(id),0) FROM filings").fetchone()
        return int((row[0] if row else 0) or 0)
    except Exception:
        return 0
    finally:
        con.close()


def _filing_ids_between(db_path: Path, start_id: int, end_id: int, tickers: list[str]) -> list[int]:
    if not db_path.exists() or int(end_id) <= int(start_id):
        return []
    tks = [_normalize_ticker(t) for t in tickers if _normalize_ticker(t)]
    if not tks:
        return []
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        marks = ",".join("?" for _ in tks)
        rows = con.execute(
            f"""SELECT id
                FROM filings
                WHERE id > ? AND id <= ? AND ticker IN ({marks})
                ORDER BY id ASC""",
            tuple([int(start_id), int(end_id)] + tks),
        ).fetchall()
        return [int(r["id"] or 0) for r in rows if int(r["id"] or 0) > 0]
    except Exception:
        return []
    finally:
        con.close()


def _trigger_sec_ingest_api(api_url: str, filing_ids: list[int], timeout_sec: int = 8) -> tuple[bool, str]:
    ids = [int(x) for x in filing_ids if int(x) > 0]
    if not ids:
        return True, "No new filing IDs to ingest."
    payload = json.dumps({"filing_ids": ids}, ensure_ascii=True).encode("utf-8")
    req = urllib.request.Request(
        str(api_url).strip(),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=max(1, int(timeout_sec))) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            return (200 <= int(resp.status) < 300), body[:400]
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="ignore")
        except Exception:
            body = str(exc)
        return False, f"HTTPError {exc.code}: {body[:260]}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _trigger_sec_ingest_local(filing_ids: list[int]) -> tuple[bool, str]:
    ids = [int(x) for x in filing_ids if int(x) > 0]
    if not ids:
        return True, "No new filing IDs to ingest."
    try:
        from app.services.sec_ingest_pipeline_service import process_new_filings_pipeline

        out = process_new_filings_pipeline(ids)
        ok = bool((out or {}).get("ok"))
        if not ok:
            return False, json.dumps({"ok": False, "error": str((out or {}).get("error") or "local_ingest_failed")}, ensure_ascii=True)
        return True, json.dumps(
            {
                "ok": True,
                "processed": int((out or {}).get("processed") or 0),
                "chunks": int((out or {}).get("chunks") or 0),
                "entities": int((out or {}).get("entities") or 0),
                "relationships": int((out or {}).get("relationships") or 0),
            },
            ensure_ascii=True,
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _trigger_sec_ingest_with_fallback(api_url: str, filing_ids: list[int], timeout_sec: int = 8, batch_size: int = 120) -> tuple[bool, str]:
    ids = [int(x) for x in filing_ids if int(x) > 0]
    if not ids:
        return True, "No new filing IDs to ingest."
    step = max(20, min(400, int(batch_size or 120)))
    msgs: list[str] = []
    failed = False
    for i in range(0, len(ids), step):
        batch = ids[i : i + step]
        ok, msg = _trigger_sec_ingest_api(api_url, batch, timeout_sec=timeout_sec)
        if ok:
            msgs.append(f"api:batch{i//step+1}=ok")
            continue
        msgs.append(f"api:batch{i//step+1}=failed")
        lok, lmsg = _trigger_sec_ingest_local(batch)
        if lok:
            msgs.append(f"local:batch{i//step+1}=ok")
        else:
            failed = True
            msgs.append(f"local:batch{i//step+1}=failed:{lmsg[:80]}")
    return (not failed), " | ".join(msgs)[:1200]


def _run(step: list[str], *, timeout: int, log_file: Path | None = None) -> tuple[bool, str]:
    return _run_with_retries(step, timeout=timeout, log_file=log_file, retries=0)


def _is_transient_error(err: str) -> bool:
    s = str(err or "").lower()
    signals = (
        "failed to resolve",
        "name resolution",
        "temporary failure",
        "no route to host",
        "connection reset",
        "connection aborted",
        "read timed out",
        "timed out",
        "502",
        "503",
        "504",
        "too many requests",
        "rate limit",
    )
    return any(k in s for k in signals)


def _run_with_retries(
    step: list[str],
    *,
    timeout: int,
    log_file: Path | None,
    retries: int,
    base_delay: float = 2.0,
) -> tuple[bool, str]:
    max_attempts = max(1, int(retries) + 1)
    last_err = ""
    for attempt in range(1, max_attempts + 1):
        try:
            p = subprocess.run(step, cwd=str(ROOT), capture_output=True, text=True, timeout=timeout)
            err = (p.stderr.strip() or p.stdout.strip() or f"exit={p.returncode}")[:600]
            ok = p.returncode == 0
        except Exception as exc:
            p = None
            ok = False
            err = f"{type(exc).__name__}: {exc}"
        last_err = err
        if log_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write(f"$ {' '.join(step)}  [attempt {attempt}/{max_attempts}]\n")
                if p and p.stdout:
                    fh.write(p.stdout + "\n")
                if p and p.stderr:
                    fh.write(p.stderr + "\n")
                if not ok:
                    fh.write(f"ERROR: {err}\n")
        if ok:
            return True, ""
        if attempt >= max_attempts:
            break
        if not _is_transient_error(err):
            break
        delay = max(1.0, float(base_delay) * (2 ** (attempt - 1)))
        if log_file:
            with log_file.open("a", encoding="utf-8") as fh:
                fh.write(f"Retrying in {delay:.1f}s due to transient error.\n")
        time.sleep(delay)
    return False, last_err


def _python_bin() -> str:
    venv_py = ROOT / ".venv-memory" / "bin" / "python"
    if venv_py.exists():
        return str(venv_py)
    sys_py = Path("/Library/Frameworks/Python.framework/Versions/3.14/bin/python3")
    if sys_py.exists():
        return str(sys_py)
    return str(Path(sys.executable))


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync SEC filings for portfolio/watchlist companies.")
    parser.add_argument("--days", type=int, default=7, help="Incremental update lookback window (default: 7)")
    parser.add_argument("--full-empty-limit", type=int, default=6, help="Run --full for up to N empty tickers per run")
    parser.add_argument("--retries", type=int, default=3, help="Max retries for transient SEC/network failures")
    parser.add_argument("--db", default=str(CORE_DB), help="SQLite DB path (default: data/core.db)")
    parser.add_argument("--log", default=str(ROOT / "logs" / "v2_sec_nightly.log"), help="Log file path")
    parser.add_argument(
        "--post-ingest-api",
        default="http://127.0.0.1:8766/api/sec/ingest-new-filings",
        help="FastAPI endpoint to enqueue post-download SEC ingest pipeline",
    )
    args = parser.parse_args()

    db_path = Path(str(args.db)).expanduser().resolve()
    log_file = Path(str(args.log)).expanduser().resolve()
    tickers = sorted(set(_load_portfolio() + _load_watchlist() + _load_blue_chips(db_path)))
    if not tickers:
        print("No portfolio/watchlist tickers found. Nothing to sync.")
        return 0

    py = _python_bin()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] Sync start. companies={len(tickers)}")
    pre_max_id = _max_filing_id(db_path)

    ok_watch = 0
    retries = max(0, int(args.retries))
    for t in tickers:
        _set_state(t, running="1", result="running", message="Nightly sync started...")
        success, err = _run_with_retries(
            [py, "research_agent.py", "watch", t],
            timeout=300,
            log_file=log_file,
            retries=retries,
        )
        if success:
            ok_watch += 1
        else:
            _set_state(t, running="1", result="failed", message=f"Watch step failed: {err}")

    joined = ",".join(tickers)
    update_ok, update_err = _run_with_retries(
        [py, "research_agent.py", "update", "--ticker", joined, "--days", str(max(1, int(args.days)))],
        timeout=7200,
        log_file=log_file,
        retries=retries,
    )

    post_counts = _filing_count_map(tickers, db_path)
    empty = [t for t in tickers if int(post_counts.get(t, 0)) <= 0]
    full_limit = max(0, int(args.full_empty_limit))
    full_targets = empty[:full_limit]
    full_ok = 0
    for t in full_targets:
        _set_state(t, running="1", result="running", message="Repair sync (--full) started...")
        success, err = _run_with_retries(
            [py, "research_agent.py", "update", "--ticker", t, "--full"],
            timeout=7200,
            log_file=log_file,
            retries=retries,
        )
        if success:
            full_ok += 1
        else:
            _set_state(t, running="0", result="failed", message=f"Full sync failed: {err}")

    final_counts = _filing_count_map(tickers, db_path)
    still_empty = [t for t in tickers if int(final_counts.get(t, 0)) <= 0]
    for t in tickers:
        c = int(final_counts.get(t, 0))
        if c > 0:
            _set_state(t, running="0", result="ok", message=f"Synced. filings={c}")
        else:
            _set_state(t, running="0", result="empty", message="No filings found yet.")

    print(
        "Sync complete. "
        f"watch_ok={ok_watch}/{len(tickers)} "
        f"incremental={'ok' if update_ok else 'failed'} "
        f"full_ok={full_ok}/{len(full_targets)} "
        f"remaining_empty={len(still_empty)}"
    )
    post_max_id = _max_filing_id(db_path)
    new_ids = _filing_ids_between(db_path, pre_max_id, post_max_id, tickers)
    trig_ok, trig_msg = _trigger_sec_ingest_with_fallback(str(args.post_ingest_api), new_ids, timeout_sec=10, batch_size=120)
    print(
        "Post-ingest trigger: "
        f"{'ok' if trig_ok else 'failed'} "
        f"new_filing_ids={len(new_ids)} "
        f"message={trig_msg}"
    )
    if not update_ok:
        print(f"Incremental update error: {update_err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
