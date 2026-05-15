#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "onyx_brain.db"


def _connect(db_path: str | Path = DB_PATH) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    return conn


def _has_col(conn: sqlite3.Connection, table: str, col: str) -> bool:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(str(r[1]) == col for r in rows)
    except Exception:
        return False


def init_db(db_path: str | Path = DB_PATH) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS holdings (
                ticker TEXT PRIMARY KEY,
                shares REAL NOT NULL DEFAULT 0,
                avg_cost REAL NOT NULL DEFAULT 0,
                thesis_text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Active'
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('News','Earnings','Insider')),
                content TEXT NOT NULL,
                date TEXT NOT NULL,
                source_url TEXT NOT NULL DEFAULT '',
                evidence_hash TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('Green','Red')),
                reasoning TEXT NOT NULL,
                evidence_summary TEXT NOT NULL DEFAULT '',
                timestamp TEXT NOT NULL,
                confidence REAL,
                contradiction_score REAL,
                evidence_hash TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS signal_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt TEXT NOT NULL,
                response TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        if not _has_col(conn, "evidence", "evidence_hash"):
            conn.execute("ALTER TABLE evidence ADD COLUMN evidence_hash TEXT")
        if not _has_col(conn, "signals", "confidence"):
            conn.execute("ALTER TABLE signals ADD COLUMN confidence REAL")
        if not _has_col(conn, "signals", "contradiction_score"):
            conn.execute("ALTER TABLE signals ADD COLUMN contradiction_score REAL")
        if not _has_col(conn, "signals", "evidence_hash"):
            conn.execute("ALTER TABLE signals ADD COLUMN evidence_hash TEXT")
        if not _has_col(conn, "signals", "evidence_summary"):
            conn.execute("ALTER TABLE signals ADD COLUMN evidence_summary TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_hash ON evidence(evidence_hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_evidence_ticker_date ON evidence(ticker, date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_ticker_id ON signals(ticker, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_holdings_status ON holdings(status)")
        conn.commit()
    finally:
        conn.close()


def _norm_ticker(ticker: str) -> str:
    t = (ticker or "").strip().upper()
    if not t:
        raise ValueError("ticker is required")
    return t


def _ev_hash(ticker: str, ev_type: str, content: str, source_url: str, date: str) -> str:
    base = "||".join([ticker, ev_type, content.strip().lower(), (source_url or "").strip().lower(), date[:19]])
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()


def add_portfolio_item(
    ticker: str,
    thesis: str,
    shares: float | None = None,
    avg_cost: float | None = None,
    status: str = "Active",
    db_path: str | Path = DB_PATH,
) -> None:
    t = _norm_ticker(ticker)
    th = (thesis or "").strip()
    st = (status or "Active").strip() or "Active"
    if not th:
        raise ValueError("thesis is required")
    init_db(db_path)
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT shares, avg_cost FROM holdings WHERE ticker = ? LIMIT 1", (t,)).fetchone()
        sh = float(shares) if shares is not None else (float(row["shares"]) if row else 0.0)
        ac = float(avg_cost) if avg_cost is not None else (float(row["avg_cost"]) if row else 0.0)
        conn.execute(
            """INSERT INTO holdings (ticker, shares, avg_cost, thesis_text, status)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET
                 shares=excluded.shares,
                 avg_cost=excluded.avg_cost,
                 thesis_text=excluded.thesis_text,
                 status=excluded.status""",
            (t, sh, ac, th, st),
        )
        conn.commit()
    finally:
        conn.close()


def set_holding_status(ticker: str, status: str = "Inactive", db_path: str | Path = DB_PATH) -> bool:
    t = _norm_ticker(ticker)
    st = (status or "Inactive").strip() or "Inactive"
    init_db(db_path)
    conn = _connect(db_path)
    try:
        cur = conn.execute("UPDATE holdings SET status = ? WHERE ticker = ?", (st, t))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def log_new_evidence(
    ticker: str,
    text: str,
    ev_type: str = "News",
    source_url: str = "",
    date: str = "",
    db_path: str | Path = DB_PATH,
) -> int:
    t = _norm_ticker(ticker)
    c = (text or "").strip()
    et = (ev_type or "News").strip().title()
    if et not in {"News", "Earnings", "Insider"}:
        et = "News"
    if not c:
        raise ValueError("evidence text is required")
    init_db(db_path)
    conn = _connect(db_path)
    try:
        d = (date or "").strip() or dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        h = _ev_hash(t, et, c, source_url, d)
        cur = conn.execute(
            """INSERT OR IGNORE INTO evidence (ticker, type, content, date, source_url, evidence_hash)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (t, et, c, d, (source_url or "").strip(), h),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _ollama_models() -> list[str]:
    cmd = ["curl", "-sS", "--max-time", "8", "http://127.0.0.1:11434/api/tags"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        return []
    try:
        obj = json.loads(p.stdout or "{}")
    except Exception:
        return []
    out: list[str] = []
    for m in (obj.get("models") or []):
        n = str((m or {}).get("name") or "").strip()
        if n:
            out.append(n)
    return out


def _pick_ollama_model(preferred: str = "gpt-oss:20b") -> str:
    models = [m.lower() for m in _ollama_models()]
    order = [preferred, "gpt-oss:20b", "llama3:latest", "llama3", "llama3.1"]
    for cand in order:
        cl = cand.lower()
        if cl in models:
            # return the exact advertised name for case/format stability
            for raw in _ollama_models():
                if raw.lower() == cl:
                    return raw
            return cand
    return "llama3:latest"


def _ollama_chat(prompt: str, model: str = "gpt-oss:20b", timeout: int = 45) -> str:
    chosen = _pick_ollama_model(model)
    chat_payload = json.dumps(
        {"model": chosen, "messages": [{"role": "user", "content": prompt}], "stream": False},
        ensure_ascii=True,
    )
    gen_payload = json.dumps(
        {"model": chosen, "prompt": prompt, "stream": False},
        ensure_ascii=True,
    )

    def _post(url: str, payload: str) -> tuple[int, str]:
        cmd = [
            "curl",
            "-sS",
            "--max-time",
            str(timeout),
            "-X",
            "POST",
            url,
            "-H",
            "Content-Type: application/json",
            "-d",
            payload,
            "-w",
            "\n__HTTP__:%{http_code}",
        ]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or p.stdout or "ollama_call_failed").strip()[:200])
        raw = p.stdout or ""
        marker = "\n__HTTP__:"
        if marker not in raw:
            return 0, raw
        body, code = raw.rsplit(marker, 1)
        try:
            return int(code.strip()), body
        except Exception:
            return 0, body

    # Primary: /api/chat ; fallback: /api/generate for older/newer Ollama variants.
    code, body = _post("http://127.0.0.1:11434/api/chat", chat_payload)
    if code == 404:
        code, body = _post("http://127.0.0.1:11434/api/generate", gen_payload)
    if code and code >= 400:
        raise RuntimeError(f"ollama_http_{code}")
    try:
        obj = json.loads(body or "{}")
    except Exception as e:
        raise RuntimeError(f"ollama_json_parse_failed: {str(e)[:120]}")
    if isinstance(obj, dict) and obj.get("error"):
        raise RuntimeError(str(obj.get("error"))[:200])
    msg = str((obj.get("message") or {}).get("content") or "").strip()
    if not msg:
        msg = str(obj.get("response") or "").strip()
    if not msg:
        raise RuntimeError("ollama_empty_response")
    return msg


def _parse_confidence(text: str) -> float | None:
    m = re.search(r"confidence\s*[:=]\s*(\d{1,3})", text or "", flags=re.IGNORECASE)
    if not m:
        return None
    try:
        v = float(m.group(1))
        return max(0.0, min(100.0, v))
    except Exception:
        return None


def _thesis_profile(thesis: str) -> dict[str, bool]:
    txt = (thesis or "").lower()
    insider_keys = [
        "insider",
        "alignment",
        "skin in the game",
        "management buying",
        "management selling",
        "insider buying",
        "insider selling",
        "founder-led",
        "founder alignment",
    ]
    return {
        "insider_relevant": any(k in txt for k in insider_keys),
    }


def run_thesis_check(ticker: str, db_path: str | Path = DB_PATH, model: str = "gpt-oss:20b") -> dict[str, str | float]:
    t = _norm_ticker(ticker)
    init_db(db_path)
    conn = _connect(db_path)
    try:
        row = conn.execute("SELECT thesis_text FROM holdings WHERE ticker = ? LIMIT 1", (t,)).fetchone()
        thesis = str((row["thesis_text"] if row else "") or "").strip()
        if not thesis:
            raise ValueError(f"No thesis found for {t} in holdings table.")

        profile = _thesis_profile(thesis)
        insider_relevant = bool(profile.get("insider_relevant"))

        raw_rows = conn.execute(
            "SELECT type, content, date, source_url, evidence_hash FROM evidence WHERE ticker = ? ORDER BY id DESC LIMIT 20",
            (t,),
        ).fetchall()
        if insider_relevant:
            ev_rows = raw_rows[:5]
        else:
            # By default, thesis validation should be driven by thesis-relevant operating evidence,
            # not insider flow noise.
            non_insider = [r for r in raw_rows if str(r["type"] or "") != "Insider"]
            ev_rows = non_insider[:5]

        ev_lines = [
            f"- [{str(r['type'])}] {str(r['date'])}: {str(r['content'])} (src: {str(r['source_url'])})"
            for r in ev_rows
        ]
        evidence_text = "\n".join(ev_lines) if ev_lines else "- No recent evidence."
        evidence_summary = " | ".join(
            f"[{str(r['type'])}] {str(r['date'])}: {str(r['content'])[:120]}"
            for r in ev_rows[:3]
        )
        combo_hash = hashlib.sha256(
            ("||".join(sorted(str(r["evidence_hash"] or "") for r in ev_rows)) + "||" + t).encode("utf-8", errors="ignore")
        ).hexdigest()

        prompt = ""
        if not ev_rows:
            ai_answer = (
                "STATUS: GREEN\n\n"
                "• No thesis-relevant evidence rows found for this ticker yet.\n"
                "• Insider-only flow is not used unless the thesis is explicitly management/insider-based.\n"
                "• Keep monitoring earnings/news evidence before changing thesis status."
            )
        else:
            prompt = (
                f"My thesis for {t} is: {thesis}\n"
                f"Here is the recent thesis-relevant evidence:\n{evidence_text}\n\n"
                "Task: Decide whether this evidence breaks the thesis claims.\n"
                "Rules:\n"
                "- Evaluate only evidence directly tied to thesis claims.\n"
                "- Do NOT mark RED from insider flow unless thesis is explicitly insider/management-alignment based.\n"
                "- If evidence is mixed or not thesis-breaking, prefer GREEN and explain monitoring points.\n\n"
                "Output format:\n"
                "First line exactly one of: STATUS: RED | STATUS: GREEN.\n"
                "Then 3 short bullets and optional line: Confidence: <0-100>."
            )
            ai_answer = _ollama_chat(prompt, model=model)
        upper = ai_answer.upper()
        status = "Red" if "STATUS: RED" in upper else "Green"
        contradiction_score = 80.0 if status == "Red" else 20.0
        confidence = _parse_confidence(ai_answer)
        ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """INSERT INTO signals (ticker, status, reasoning, evidence_summary, timestamp, confidence, contradiction_score, evidence_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (t, status, ai_answer or "STATUS: GREEN", evidence_summary, ts, confidence, contradiction_score, combo_hash),
        )
        conn.execute(
            "INSERT INTO signal_audit (ticker, model, prompt, response, created_at) VALUES (?, ?, ?, ?, ?)",
            (t, model, prompt, ai_answer or "", ts),
        )
        conn.commit()
        return {
            "ticker": t,
            "status": status,
            "reasoning": ai_answer or "STATUS: GREEN",
            "timestamp": ts,
            "confidence": (confidence if confidence is not None else -1.0),
            "contradiction_score": contradiction_score,
        }
    finally:
        conn.close()


def run_thesis_check_if_due(
    ticker: str,
    min_interval_seconds: int = 21600,
    db_path: str | Path = DB_PATH,
    model: str = "gpt-oss:20b",
) -> dict[str, str | float]:
    t = _norm_ticker(ticker)
    init_db(db_path)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, timestamp FROM signals WHERE ticker = ? ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()
        if row:
            ts = str(row["timestamp"] or "")
            try:
                dt_last = dt.datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                age = (dt.datetime.now() - dt_last).total_seconds()
                if age < float(min_interval_seconds):
                    sig = get_latest_signal(t, db_path=db_path)
                    sig["skipped"] = "1"
                    return sig
            except Exception:
                pass
    finally:
        conn.close()
    return run_thesis_check(t, db_path=db_path, model=model)


def run_thesis_check_all_active(
    db_path: str | Path = DB_PATH,
    model: str = "gpt-oss:20b",
    min_interval_seconds: int = 21600,
) -> list[dict[str, str | float]]:
    init_db(db_path)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT ticker FROM holdings WHERE status = 'Active' AND thesis_text IS NOT NULL AND trim(thesis_text) != '' ORDER BY ticker"
        ).fetchall()
        tickers = [str(r["ticker"] or "").strip().upper() for r in rows if str(r["ticker"] or "").strip()]
    finally:
        conn.close()
    out: list[dict[str, str | float]] = []
    for t in tickers:
        try:
            out.append(run_thesis_check_if_due(t, min_interval_seconds=min_interval_seconds, db_path=db_path, model=model))
        except Exception as e:
            ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            msg = f"Thesis check unavailable: {str(e)[:140]}"
            out.append({"ticker": t, "status": "Unavailable", "reasoning": msg, "timestamp": ts})
    return out


def get_latest_signal(ticker: str, db_path: str | Path = DB_PATH) -> dict[str, str]:
    t = _norm_ticker(ticker)
    init_db(db_path)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT ticker, status, reasoning, evidence_summary, timestamp, confidence, contradiction_score FROM signals WHERE ticker = ? ORDER BY id DESC LIMIT 1",
            (t,),
        ).fetchone()
        if not row:
            return {
                "ticker": t,
                "status": "-",
                "reasoning": "No signal yet.",
                "evidence_summary": "-",
                "timestamp": "-",
                "confidence": "-",
                "contradiction_score": "-",
            }
        return {
            "ticker": str(row["ticker"]),
            "status": str(row["status"]),
            "reasoning": str(row["reasoning"]),
            "evidence_summary": str(row["evidence_summary"] or ""),
            "timestamp": str(row["timestamp"]),
            "confidence": (f"{float(row['confidence']):.0f}" if row["confidence"] is not None else "-"),
            "contradiction_score": (f"{float(row['contradiction_score']):.0f}" if row["contradiction_score"] is not None else "-"),
        }
    finally:
        conn.close()


def get_signals_for_tickers(tickers: list[str], db_path: str | Path = DB_PATH) -> dict[str, dict[str, str]]:
    init_db(db_path)
    out: dict[str, dict[str, str]] = {}
    uniq = sorted({(t or "").strip().upper() for t in tickers if (t or "").strip()})
    if not uniq:
        return out
    conn = _connect(db_path)
    try:
        q_marks = ",".join("?" for _ in uniq)
        rows = conn.execute(
            f"""
            SELECT s.ticker, s.status, s.reasoning, s.evidence_summary, s.timestamp, s.confidence, s.contradiction_score
            FROM signals s
            INNER JOIN (
              SELECT ticker, MAX(id) AS max_id
              FROM signals
              WHERE ticker IN ({q_marks})
              GROUP BY ticker
            ) latest ON latest.ticker = s.ticker AND latest.max_id = s.id
            """,
            uniq,
        ).fetchall()
        for r in rows:
            t = str(r["ticker"] or "")
            out[t] = {
                "ticker": t,
                "status": str(r["status"] or "-"),
                "reasoning": str(r["reasoning"] or ""),
                "evidence_summary": str(r["evidence_summary"] or ""),
                "timestamp": str(r["timestamp"] or "-"),
                "confidence": (f"{float(r['confidence']):.0f}" if r["confidence"] is not None else "-"),
                "contradiction_score": (f"{float(r['contradiction_score']):.0f}" if r["contradiction_score"] is not None else "-"),
            }
    finally:
        conn.close()
    return out


def get_dashboard_signals(db_path: str | Path = DB_PATH) -> list[dict[str, str]]:
    init_db(db_path)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT h.ticker,
                   COALESCE(s.status, '-') AS status,
                   COALESCE(s.reasoning, 'No thesis signal yet.') AS reasoning,
                   COALESCE(s.timestamp, '-') AS timestamp
            FROM holdings h
            LEFT JOIN (
                SELECT s1.ticker, s1.status, s1.reasoning, s1.timestamp
                FROM signals s1
                INNER JOIN (
                    SELECT ticker, MAX(id) AS max_id
                    FROM signals
                    GROUP BY ticker
                ) latest ON latest.ticker = s1.ticker AND latest.max_id = s1.id
            ) s ON s.ticker = h.ticker
            WHERE h.status = 'Active'
            ORDER BY h.ticker ASC
            """
        ).fetchall()
        return [
            {
                "ticker": str(r["ticker"]),
                "status": str(r["status"]),
                "reasoning": str(r["reasoning"]),
                "timestamp": str(r["timestamp"]),
            }
            for r in rows
        ]
    finally:
        conn.close()
