#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import csv
import concurrent.futures
import datetime as dt
import glob
import hashlib
import html
import json
import mimetypes
import os
import re
import sqlite3
import shutil
import secrets
import subprocess
import sys
from tools.terminal.ui import fmt_money, fmt_pct, fmt_money_ccy
import threading
import time
import uuid
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tools.terminal_file_utils import (
    get_latest_file as _file_get_latest_file,
    latest as _file_latest,
    latest_any as _file_latest_any,
    mtime,
    read_cash_balances,
    read_lines,
    read_watchlist_entries,
    to_float,
    write_cash_balances,
)

try:
    import yfinance as yf
except Exception:
    yf = None

ROOT = Path("/Users/solmaz/Investment_Tools")
REPORTS = ROOT / "reports"
INPUTS = REPORTS / ".terminal_inputs"
DATA = ROOT / "data"
LOGS = ROOT / "logs"
SETTINGS_FILE = DATA / "user_settings.json"
CASH_BALANCES_FILE = DATA / "cash_balances.csv"
FX_CACHE_FILE = DATA / "fx_rates_cache.json"
FRONTEND_DIST = ROOT / "frontend" / "dist"
APP_VERSION = "Beta v2"
LEGACY_DB_PATH = DATA / "research.db"
CORE_DB_PATH = DATA / "core.db"
FILINGS_DB_PATH = DATA / "filings.db"
CACHE_DB_PATH = DATA / "cache.db"
DB_SPLIT_STATE: dict[str, bool] = {"ready": False}
FAST_MODE_DEFAULT = os.getenv("ONYX_FAST_MODE", "1").strip().lower() not in {"0", "false", "no", "off"}
SWR_NONBLOCK_DEFAULT = os.getenv("ONYX_SWR_NONBLOCK", "1").strip().lower() not in {"0", "false", "no", "off"}
SIGNAL_BLOCKING_BOOT_DEFAULT = os.getenv("ONYX_SIGNAL_BLOCKING_BOOT", "0").strip().lower() in {"1", "true", "yes", "on"}

FINANCIAL_INTEL_PROMPT = """
You are a Wall Street Equity Analyst. I will give you the raw text from {TICKER}'s latest filing (8-K Earnings or 10-Q).
You will also be given structured_financial_data_json when available.
Do NOT extract/calculate new numbers from raw filing text.
If structured_financial_data_json is missing for a quantitative request, output exactly: Data not available in structured filings.

YOUR GOAL:
Ignore legal warnings. Extract the Financial Narrative and Operational Reality.

EXTRACT THESE 3 INSIGHTS:
1. The "Quality" Check:
   - Did Revenue grow from volume (good) or just price hikes (weak)?
   - Are Margins expanding or compressing? (Cite the specific reason: e.g., "Cloud costs" or "Headcount reduction").
2. The "Future" Signal (Guidance):
   - Did they raise or lower the outlook?
   - What is the specific reason given? (e.g., "Weakness in Europe" or "AI Demand").
3. The "Capex" Reality:
   - Are they burning cash? Where is the money going? (e.g., "Heavy AI infrastructure spend").

INPUT TEXT:
{raw_filing_text}

OUTPUT FORMAT (JSON):
{
  "headline": "A short, punchy title (e.g., 'Margins Compressed by AI Spend despite Rev Beat')",
  "performance_summary": "2 sentences on Revenue/EPS context.",
  "margin_story": "Why are they more/less profitable this quarter?",
  "guidance_tone": "Bullish" | "Bearish" | "Cautious",
  "key_quote": "The most important sentence management said."
}
"""

# Ensure root-level modules are importable when running tools/terminal_app.py
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from tools.llm_engine import ask_ai as _hybrid_ask_ai
except Exception:
    _hybrid_ask_ai = None

try:
    from tools.llm_engine import AIEngine as _HybridAIEngine
except Exception:
    _HybridAIEngine = None

try:
    from tools.gemini_reader import synthesize_reports as _gemini_synthesize_reports
except Exception:
    _gemini_synthesize_reports = None

try:
    import tools.gemini_reader as gemini
except Exception:
    gemini = None

try:
    import tools.news_wire as news_wire
except Exception:
    news_wire = None

try:
    import tools.sec_watchdog as sec_watchdog
except Exception:
    sec_watchdog = None

try:
    from modules.sec_filing_analyzer import SECFilingAnalyzer as _SECFilingAnalyzer
    from modules.sec_filing_analyzer import ExtractionError as _SECExtractionError
except Exception:
    _SECFilingAnalyzer = None
    _SECExtractionError = RuntimeError

try:
    from modules.sec_mda_analyzer import SECMDAAnalyzer as _SECMDAAnalyzer
except Exception:
    _SECMDAAnalyzer = None

try:
    from modules.sec_full_lens_analyzer import SECFullLensAnalyzer as _SECFullLensAnalyzer
except Exception:
    _SECFullLensAnalyzer = None

try:
    from onyx_db import (
        init_db as _onyx_init_db,
        add_portfolio_item as _onyx_add_portfolio_item,
        set_holding_status as _onyx_set_holding_status,
        log_new_evidence as _onyx_log_new_evidence,
        get_latest_signal as _onyx_get_latest_signal,
        get_signals_for_tickers as _onyx_get_signals_for_tickers,
        run_thesis_check_if_due as _onyx_run_thesis_check_if_due,
        run_thesis_check_all_active as _onyx_run_thesis_check_all_active,
    )
except Exception:
    _onyx_init_db = None
    _onyx_add_portfolio_item = None
    _onyx_set_holding_status = None
    _onyx_log_new_evidence = None
    _onyx_get_latest_signal = None
    _onyx_get_signals_for_tickers = None
    _onyx_run_thesis_check_if_due = None
    _onyx_run_thesis_check_all_active = None

JOBS = {
    "daily": ["./bin/run_terminal_daily"],
    "weekly": ["./bin/run_terminal_weekly"],
    "monthly": ["./bin/run_terminal_monthly"],
    "l2": ["./bin/run_l2_digest"],
    "thesis_checks": ["./bin/run_onyx_thesis_checks"],
    "all": ["./bin/run_terminal_daily", "./bin/run_terminal_weekly", "./bin/run_terminal_monthly", "./bin/run_quarterly"],
    "beta": ["python3 tools/build_beta_dashboard.py"],
}

RUN_STATE: dict[str, dict[str, str | bool]] = {
    k: {"running": False, "last": "never", "result": "-", "log": f"manual_webapp_{k}.log"} for k in JOBS
}
COMPANY_SYNC_STATE: dict[str, dict[str, str | bool]] = {}
INSIDER_SYNC_STATE: dict[str, dict[str, str | bool]] = {}
LOCK = threading.Lock()
QUOTE_CACHE: dict[str, object] = {"ts": 0.0, "data": {}}
QUOTE_REFRESH: dict[str, object] = {"running": False}
PORT_INTEL_CACHE: dict[str, dict[str, object]] = {}
PORT_INTEL_REFRESH: dict[str, object] = {"running": False}
PORT_PROFILE_CACHE: dict[str, dict[str, object]] = {}
PORT_PROFILE_REFRESH: dict[str, object] = {"running": False}
EARNINGS_FEED_CACHE: dict[str, object] = {"key": "", "map": {}}
COMPANY_NAME_CACHE: dict[str, object] = {"ts": 0.0, "map": {}}
SIGNAL_CACHE: dict[str, dict[str, object]] = {
    "alerts": {"key": "", "data": []},
    "upcoming": {"key": "", "data": []},
    "week": {"key": "", "data": []},
}
SIGNAL_REFRESH: dict[str, bool] = {"alerts": False, "upcoming": False, "week": False}
ONYX_CACHE: dict[str, dict[str, object]] = {}
CHAT_HISTORY: list[dict[str, str]] = []
EVIDENCE_REFRESH: dict[str, bool] = {}
DEEP_DIVE_CACHE: dict[str, dict[str, object]] = {}
AI_SUMMARY_CACHE: dict[str, dict[str, object]] = {}
AI_SUMMARY_REFRESH: dict[str, bool] = {}
L2_CACHE: dict[str, dict[str, object]] = {}
L2_REFRESH: dict[str, bool] = {"running": False}
L2_SCHED: dict[str, bool] = {"started": False}
ONYX_SCHED: dict[str, bool] = {"started": False}
SEC_RISK_SCHED: dict[str, bool] = {"started": False}
INTEL_PREWARM_SCHED: dict[str, bool] = {"started": False}
INTEL_FEED_SCHED: dict[str, bool] = {"started": False}
FAST_INTEL_FEED_SCHED: dict[str, bool] = {"started": False}
INTEL24_SNAP_SCHED: dict[str, bool] = {"started": False}
WORKSPACE_SNAPSHOT_SCHED: dict[str, bool] = {"started": False}
ONYX_UNIFIED_SCHED: dict[str, bool] = {"started": False}
COMPANY_PROFILE_ENRICH_SCHED: dict[str, bool] = {"started": False}
SEC_RISK_DIFF_CACHE: dict[str, dict[str, object]] = {}
MDA_DIFF_CACHE: dict[str, dict[str, object]] = {}
TENK_LENS_CACHE: dict[str, dict[str, object]] = {}
TENK_LENS_INFLIGHT: set[str] = set()
EARNINGS_AUTO_REFRESH_STATE: dict[str, object] = {"last_date": "", "running": False}
HOME_DASHBOARD_CACHE: dict[str, dict[str, object]] = {}
HOME_DASHBOARD_REFRESH: dict[str, bool] = {}
GOOGLE_OAUTH_STATE: dict[str, dict[str, object]] = {}
GOOGLE_OAUTH_LOCK = threading.Lock()
GOOGLE_CAL_CACHE: dict[str, object] = {"ts": 0.0, "day": "", "items": [], "status": ""}

MOAT_OPTIONS: list[tuple[str, str]] = [
    ("network_effect", "Network Effect"),
    ("switching_costs", "Switching Costs"),
    ("cost_advantage", "Cost Advantage"),
    ("intangible_assets", "Intangible Assets"),
    ("efficient_scale", "Efficient Scale"),
    ("brand", "Brand"),
    ("distribution", "Distribution"),
    ("regulatory_license", "Regulatory / License"),
    ("ip_patents", "IP / Patents"),
    ("ecosystem_lockin", "Ecosystem Lock-in"),
]
MOAT_LABEL_MAP: dict[str, str] = {k: v for k, v in MOAT_OPTIONS}


HEAVY_ANALYSIS_HARD_OFF = True


def _heavy_analysis_enabled() -> bool:
    # Hard gate for expensive legacy analysis paths (SEC risk diff, MD&A diff, deep dive).
    # Keep disabled unless you intentionally flip HEAVY_ANALYSIS_HARD_OFF to False.
    if HEAVY_ANALYSIS_HARD_OFF:
        return False
    return os.getenv("ONYX_HEAVY_ANALYSIS_ON", "0").strip().lower() in {"1", "true", "yes", "on"}


def latest(pattern: str) -> str:
    return _file_latest(ROOT, pattern)


def latest_any(patterns: list[str]) -> str:
    return _file_latest_any(ROOT, patterns)


def get_latest_file(pattern: str) -> str:
    return _file_get_latest_file(ROOT, pattern)


def _latest_earnings_file() -> str:
    return latest_any(
        [
            "reports/.terminal_inputs/earnings_radar_*.md",
            "reports/legacy_archive/bulk/earnings_radar_*.md",
            "reports/earnings_radar_*.md",
        ]
    )


def _read_settings() -> dict[str, object]:
    try:
        if not SETTINGS_FILE.exists():
            return {}
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8", errors="ignore"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _write_settings(data: dict[str, object]) -> None:
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
    except Exception:
        pass


def _preferred_base_currency() -> str:
    raw = str(_read_settings().get("base_currency") or "USD").upper().strip()
    return raw if re.fullmatch(r"[A-Z]{3}", raw) else "USD"


def _set_preferred_base_currency(ccy: str) -> None:
    cur = (ccy or "").upper().strip()
    if not re.fullmatch(r"[A-Z]{3}", cur):
        return
    data = _read_settings()
    data["base_currency"] = cur
    _write_settings(data)


def _workspace_recent_watchlist_days() -> int:
    data = _read_settings()
    try:
        raw = int(str(data.get("workspace_recent_watchlist_days", "30")).strip())
    except Exception:
        raw = 30
    return max(1, min(90, raw))


def _set_workspace_recent_watchlist_days(days: int) -> int:
    val = max(1, min(90, int(days)))
    data = _read_settings()
    data["workspace_recent_watchlist_days"] = val
    _write_settings(data)
    return val


def _load_fx_cache_file() -> dict[str, dict[str, object]]:
    try:
        if not FX_CACHE_FILE.exists():
            return {}
        raw = json.loads(FX_CACHE_FILE.read_text(encoding="utf-8", errors="ignore"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save_fx_cache_file(data: dict[str, dict[str, object]]) -> None:
    try:
        FX_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        FX_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
    except Exception:
        pass


def _fx_rates(base_ccy: str, ttl_seconds: int = 1800) -> dict[str, float]:
    base = (base_ccy or "USD").upper().strip()
    if not re.fullmatch(r"[A-Z]{3}", base):
        base = "USD"
    ck = f"fx:rates:{base}"
    cached = _cache_get(ck, ttl_seconds=ttl_seconds)
    if isinstance(cached, dict):
        out = {k.upper(): float(v) for k, v in cached.items() if isinstance(k, str)}
        out[base] = 1.0
        return out
    try:
        url = "https://api.frankfurter.app/latest?" + urllib.parse.urlencode({"from": base})
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        rates = payload.get("rates") if isinstance(payload, dict) else None
        if isinstance(rates, dict):
            out = {}
            for k, v in rates.items():
                try:
                    out[str(k).upper()] = float(v)
                except Exception:
                    continue
            out[base] = 1.0
            _cache_put(ck, out)
            disk = _load_fx_cache_file()
            disk[base] = {"ts": time.time(), "rates": out}
            _save_fx_cache_file(disk)
            return out
    except Exception:
        pass
    disk = _load_fx_cache_file()
    fallback = disk.get(base) if isinstance(disk, dict) else None
    if isinstance(fallback, dict):
        rates = fallback.get("rates")
        if isinstance(rates, dict):
            out = {}
            for k, v in rates.items():
                try:
                    out[str(k).upper()] = float(v)
                except Exception:
                    continue
            out[base] = 1.0
            return out
    return {base: 1.0}


def fx_convert(amount: float, from_ccy: str, to_ccy: str) -> float | None:
    try:
        amt = float(amount)
    except Exception:
        return None
    src = (from_ccy or "").upper().strip()
    dst = (to_ccy or "").upper().strip()
    if not re.fullmatch(r"[A-Z]{3}", src) or not re.fullmatch(r"[A-Z]{3}", dst):
        return None
    if src == dst:
        return amt
    r_dst = _fx_rates(dst)
    if src in r_dst and float(r_dst[src]) > 0:
        return amt / float(r_dst[src])
    r_src = _fx_rates(src)
    if dst in r_src and float(r_src[dst]) > 0:
        return amt * float(r_src[dst])
    return None


def _normalize_company_key(s: str) -> str:
    x = (s or "").lower()
    x = re.sub(r"[^a-z0-9 ]+", " ", x)
    x = re.sub(r"\s+", " ", x).strip()
    for suf in (" inc", " corp", " corporation", " ltd", " plc", " llc", " holdings", " company", " co"):
        if x.endswith(suf):
            x = x[: -len(suf)].strip()
    return x


def _company_name_map(ttl_seconds: int = 900) -> dict[str, str]:
    now = time.time()
    with LOCK:
        ts = float(COMPANY_NAME_CACHE.get("ts", 0.0))
        mp = COMPANY_NAME_CACHE.get("map", {})
        if isinstance(mp, dict) and (now - ts) <= ttl_seconds and mp:
            return dict(mp)
    out: dict[str, str] = {}
    try:
        conn = sqlite3.connect(str(FILINGS_DB_PATH if FILINGS_DB_PATH.exists() else LEGACY_DB_PATH))
        rows = conn.execute("SELECT ticker, name FROM companies WHERE ticker IS NOT NULL AND name IS NOT NULL").fetchall()
        conn.close()
        for t, n in rows:
            tu = str(t or "").strip().upper()
            nk = _normalize_company_key(str(n or ""))
            if tu and nk and nk not in out:
                out[nk] = tu
    except Exception:
        pass
    with LOCK:
        COMPANY_NAME_CACHE["ts"] = now
        COMPANY_NAME_CACHE["map"] = dict(out)
    return out


def resolve_ticker_input(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    # Trust explicit ticker-like symbols, including global suffixes (e.g., AIR.PA, 005930.KS).
    if re.fullmatch(r"[A-Za-z0-9.\-]{1,14}", s) and re.search(r"[A-Za-z]", s):
        return s.upper()
    m = re.search(r"\$([A-Za-z]{1,6})\b", s)
    if m:
        return m.group(1).upper()

    q = _normalize_company_key(s)
    if not q:
        return ""
    aliases = {
        "microsoft": "MSFT",
        "apple": "AAPL",
        "alphabet": "GOOGL",
        "google": "GOOGL",
        "amazon": "AMZN",
        "meta": "META",
        "facebook": "META",
        "nvidia": "NVDA",
        "tesla": "TSLA",
        "salesforce": "CRM",
        "hubspot": "HUBS",
        "gartner": "IT",
        "adobe": "ADBE",
        "molina": "MOH",
        "molina healthcare": "MOH",
    }
    if q in aliases:
        return aliases[q]

    cmap = _company_name_map()
    if q in cmap:
        return cmap[q]

    cands = [(name, t) for name, t in cmap.items() if q in name or name in q]
    if len(cands) == 1:
        return cands[0][1]
    if cands:
        cands.sort(key=lambda x: len(x[0]))
        return cands[0][1]
    # Last pass: allow lowercase ticker entry (e.g. "msft"), but only if it looks like a real quote.
    if re.fullmatch(r"[A-Za-z]{1,6}", s):
        cand = s.upper()
        qd = fetch_quote_single(cand)
        if isinstance(qd.get("price"), float):
            return cand
    return ""


def fetch_quote_single(ticker: str) -> dict[str, float | None]:
    if yf is None:
        return {"price": None, "prev_close": None, "day_pct": None}
    try:
        tk = yf.Ticker(ticker)
        fi = getattr(tk, "fast_info", None) or {}
        price = fi.get("last_price")
        prev = fi.get("previous_close") or fi.get("previousClose")
        if price is None:
            h = tk.history(period="2d", interval="1d")
            if not h.empty:
                price = float(h["Close"].dropna().iloc[-1])
                if len(h["Close"].dropna()) > 1:
                    prev = float(h["Close"].dropna().iloc[-2])
        price_f = float(price) if price is not None else None
        prev_f = float(prev) if prev is not None else None
        day_pct = ((price_f - prev_f) / prev_f * 100.0) if (price_f is not None and prev_f not in (None, 0.0)) else None
        return {"price": price_f, "prev_close": prev_f, "day_pct": day_pct}
    except Exception:
        return {"price": None, "prev_close": None, "day_pct": None}


def _quote_empty() -> dict[str, float | None]:
    return {"price": None, "prev_close": None, "day_pct": None}


def _to_float_obj(v: object) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _extract_date_obj(v: object) -> dt.date | None:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    for attr in ("to_pydatetime", "date"):
        fn = getattr(v, attr, None)
        if callable(fn):
            try:
                vv = fn()
                if isinstance(vv, dt.datetime):
                    return vv.date()
                if isinstance(vv, dt.date):
                    return vv
            except Exception:
                pass
    s = str(v).strip()
    if not s:
        return None
    s = s.replace("Z", "").replace("T", " ")
    if len(s) >= 10:
        try:
            return dt.datetime.strptime(s[:10], "%Y-%m-%d").date()
        except Exception:
            pass
    try:
        return dt.datetime.fromisoformat(s[:19]).date()
    except Exception:
        return None


def _parse_feed_date_text(s: str) -> dt.date | None:
    x = (s or "").strip().lower()
    if not x:
        return None
    today = dt.date.today()
    if x.startswith("today"):
        return today
    if x.startswith("tomorrow"):
        return today + dt.timedelta(days=1)
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", x)
    if m:
        try:
            return dt.datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except Exception:
            return None
    return None


def _earnings_days_map_from_feed() -> dict[str, int]:
    earnings = _latest_earnings_file()
    key = f"{_file_sig(earnings)}:earn_map_v1"
    with LOCK:
        if str(EARNINGS_FEED_CACHE.get("key") or "") == key and isinstance(EARNINGS_FEED_CACHE.get("map"), dict):
            return dict(EARNINGS_FEED_CACHE.get("map") or {})
    if not earnings:
        return {}
    rows = run_cmd(["python3", "tools/earnings_watch_rank.py", "--file", earnings, "--limit", "500", "--scope", "upcoming"])
    if not rows:
        rows = run_cmd(["python3", "tools/earnings_watch_rank.py", "--file", earnings, "--limit", "500", "--scope", "week"])
    today = dt.date.today()
    out: dict[str, int] = {}
    for ln in rows:
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) < 3:
            continue
        ticker = parts[1].upper().strip()
        d = _parse_feed_date_text(parts[2])
        if not ticker or d is None:
            continue
        dd = int((d - today).days)
        if dd < 0:
            continue
        prev = out.get(ticker)
        if prev is None or dd < prev:
            out[ticker] = dd
    with LOCK:
        EARNINGS_FEED_CACHE["key"] = key
        EARNINGS_FEED_CACHE["map"] = dict(out)
    return out


def _watchlist_earnings_event_map(limit: int = 500) -> dict[str, dict[str, object]]:
    earnings = _latest_earnings_file()
    if not earnings:
        return {}
    rows = run_cmd(["python3", "tools/earnings_watch_rank.py", "--file", earnings, "--limit", str(limit), "--scope", "upcoming"])
    rows += run_cmd(["python3", "tools/earnings_watch_rank.py", "--file", earnings, "--limit", str(limit), "--scope", "week"])
    today = dt.date.today()
    out: dict[str, dict[str, object]] = {}
    for ln in rows:
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) < 5:
            continue
        status = parts[0].upper().strip()
        ticker = parts[1].upper().strip()
        d = _parse_feed_date_text(parts[2])
        eps_part = parts[4].replace("EPS", "").strip()
        left, right = "", ""
        if "vs" in eps_part:
            left, right = [x.strip() for x in eps_part.split("vs", 1)]
        actual = _parse_eps_num(left)
        est = _parse_eps_num(right)
        dd = int((d - today).days) if d is not None else None
        row = out.get(ticker, {})
        if status == "UPCOMING":
            prev = row.get("next_days")
            if isinstance(dd, int) and dd >= 0 and (not isinstance(prev, int) or dd < prev):
                row["next_days"] = dd
                row["est_eps"] = est
                row["next_date"] = parts[2]
        elif status == "REPORTED":
            # Keep the most recent reported snapshot by date when available.
            row["last_report_date"] = parts[2]
            row["last_actual_eps"] = actual
            row["last_est_eps"] = est
            if actual is not None and est not in (None, 0.0):
                row["last_surprise_pct"] = (actual - est) / abs(est) * 100.0
                row["last_verdict"] = "BEAT" if actual >= est else "MISS"
            else:
                row["last_surprise_pct"] = None
                row["last_verdict"] = "REPORTED"
        out[ticker] = row
    return out


def _extract_next_earnings_days(tk: object) -> int | None:
    today = dt.date.today()
    dates: list[dt.date] = []
    try:
        cal = getattr(tk, "calendar", None)
        if cal is not None:
            idx = getattr(cal, "index", None)
            cols = getattr(cal, "columns", None)
            if idx is not None and "Earnings Date" in idx:
                row = cal.loc["Earnings Date"]
                vals = row.values.tolist() if hasattr(row, "values") else [row]
                for v in vals:
                    d = _extract_date_obj(v)
                    if d:
                        dates.append(d)
            elif cols is not None and "Earnings Date" in cols:
                col = cal["Earnings Date"]
                vals = col.values.tolist() if hasattr(col, "values") else [col]
                for v in vals:
                    d = _extract_date_obj(v)
                    if d:
                        dates.append(d)
    except Exception:
        pass
    if not dates:
        try:
            if hasattr(tk, "get_earnings_dates"):
                df = tk.get_earnings_dates(limit=6)
                idx = getattr(df, "index", None)
                if idx is not None:
                    for x in list(idx):
                        d = _extract_date_obj(x)
                        if d:
                            dates.append(d)
        except Exception:
            pass
    if not dates:
        # Fallback: pull from info timestamps when calendar endpoints are empty.
        try:
            info = getattr(tk, "info", None) or {}
            for k in ("earningsTimestamp", "earningsTimestampStart", "earningsTimestampEnd"):
                ts = info.get(k)
                try:
                    if ts is not None:
                        d = dt.datetime.fromtimestamp(int(ts)).date()
                        dates.append(d)
                except Exception:
                    pass
            nd = info.get("nextEarningsDate")
            d2 = _extract_date_obj(nd)
            if d2:
                dates.append(d2)
        except Exception:
            pass
    future = sorted({d for d in dates if d >= today})
    if not future:
        return None
    return int((future[0] - today).days)


def _fmt_date_ymd(val: object) -> str | None:
    try:
        if val is None:
            return None
        if isinstance(val, dt.datetime):
            return val.date().isoformat()
        if isinstance(val, dt.date):
            return val.isoformat()
        if isinstance(val, (int, float)):
            return dt.datetime.fromtimestamp(float(val)).date().isoformat()
        s = str(val).strip()
        if not s:
            return None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return s
        try:
            return dt.datetime.fromisoformat(s[:19]).date().isoformat()
        except Exception:
            return None
    except Exception:
        return None


def _sec_dividend_check_label(ticker: str) -> str:
    t = (ticker or "").strip().upper()
    if not t:
        return "none"
    try:
        conn = research_db()
        try:
            rows = conn.execute(
                """
                SELECT form, date, path
                FROM filings
                WHERE ticker = ? AND form IN ('10-K','10-Q','8-K','20-F')
                ORDER BY date DESC
                LIMIT 3
                """,
                (t,),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return "none"
        for r in rows:
            txt = _read_latest_10k_text(str(r["path"] or ""), max_chars=220000)
            low = (txt or "").lower()
            if any(k in low for k in ("dividend", "dividends", "cash dividend", "declared dividend")):
                return "sec"
        return "none"
    except Exception:
        return "none"


def _earnings_override_map(path: Path) -> dict[str, dt.date]:
    out: dict[str, dt.date] = {}
    if not path.exists():
        return out
    try:
        for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            parts = [p.strip() for p in s.split(",")]
            if len(parts) < 2:
                continue
            t = parts[0].upper()
            try:
                d = dt.datetime.strptime(parts[1], "%Y-%m-%d").date()
            except Exception:
                continue
            out[t] = d
    except Exception:
        return {}
    return out


def fetch_portfolio_intel_single(ticker: str) -> dict[str, float | int | str | None]:
    out: dict[str, float | int | str | None] = {
        "earn_days": None,
        "target_mean": None,
        "earn_source": "-",
        "dividend_rate": None,
        "dividend_yield_pct": None,
        "ex_div_date": None,
        "pay_div_date": None,
        "fifty_two_low": None,
        "fifty_two_high": None,
        "near_low_pct": None,
        "div_source_conf": "none",
    }
    t = (ticker or "").strip().upper()
    ov = _earnings_override_map(DATA / "earnings_overrides.csv")
    if t in ov:
        dd = int((ov[t] - dt.date.today()).days)
        if dd >= 0:
            out["earn_days"] = dd
            out["earn_source"] = "override"
    if t:
        try:
            emap = _earnings_days_map_from_feed()
            if t in emap:
                out["earn_days"] = int(emap[t])
                out["earn_source"] = "feed"
        except Exception:
            pass
    if yf is None:
        return out
    try:
        tk = yf.Ticker(ticker)
        if out["earn_days"] is None:
            out["earn_days"] = _extract_next_earnings_days(tk)
            if isinstance(out["earn_days"], int):
                out["earn_source"] = "yahoo"
        info = {}
        fi = {}
        try:
            info = getattr(tk, "info", None) or {}
        except Exception:
            info = {}
        try:
            fi = getattr(tk, "fast_info", None) or {}
        except Exception:
            fi = {}
        try:
            pt = tk.get_analyst_price_targets()
            if isinstance(pt, dict):
                out["target_mean"] = _to_float_obj(pt.get("mean") or pt.get("targetMeanPrice"))
        except Exception:
            pass
        if out["target_mean"] is None:
            try:
                fi = getattr(tk, "fast_info", None) or {}
                out["target_mean"] = _to_float_obj(fi.get("target_mean_price") or fi.get("targetMeanPrice"))
            except Exception:
                pass
        if out["target_mean"] is None:
            try:
                out["target_mean"] = _to_float_obj(info.get("targetMeanPrice"))
            except Exception:
                pass
        # Dividends (Yahoo)
        d_rate = _to_float_obj(info.get("dividendRate")) or _to_float_obj(fi.get("dividend_rate"))
        d_yield = _to_float_obj(info.get("dividendYield")) or _to_float_obj(fi.get("dividend_yield"))
        px_now = _to_float_obj(info.get("currentPrice") or info.get("regularMarketPrice") or fi.get("last_price"))
        implied_yield = ((d_rate / px_now) * 100.0) if (isinstance(d_rate, float) and isinstance(px_now, float) and px_now > 0) else None
        # Normalize vendor inconsistency: some feeds return fraction (0.009), some return percent (0.9).
        if isinstance(d_yield, float):
            if d_yield <= 0.50:
                d_yield = d_yield * 100.0
            # If parsed yield is extreme or far from implied, trust implied when available.
            if isinstance(implied_yield, float):
                if d_yield > 25.0 or abs(d_yield - implied_yield) > 8.0:
                    d_yield = implied_yield
        elif isinstance(implied_yield, float):
            d_yield = implied_yield
        out["dividend_rate"] = d_rate
        out["dividend_yield_pct"] = d_yield
        exd = _fmt_date_ymd(info.get("exDividendDate"))
        payd = _fmt_date_ymd(info.get("dividendDate") or info.get("lastDividendDate"))
        try:
            cal = getattr(tk, "calendar", None)
            if cal is not None and hasattr(cal, "index"):
                if "Ex-Dividend Date" in cal.index:
                    exd = exd or _fmt_date_ymd(cal.loc["Ex-Dividend Date"].values[0])
                if "Dividend Date" in cal.index:
                    payd = payd or _fmt_date_ymd(cal.loc["Dividend Date"].values[0])
        except Exception:
            pass
        out["ex_div_date"] = exd
        out["pay_div_date"] = payd
        # 52-week range / proximity
        low52 = _to_float_obj(info.get("fiftyTwoWeekLow")) or _to_float_obj(fi.get("year_low"))
        high52 = _to_float_obj(info.get("fiftyTwoWeekHigh")) or _to_float_obj(fi.get("year_high"))
        out["fifty_two_low"] = low52
        out["fifty_two_high"] = high52
        if isinstance(px_now, float) and isinstance(low52, float) and low52 > 0:
            out["near_low_pct"] = ((px_now - low52) / low52) * 100.0
        # SEC cross-check label
        sec_chk = _sec_dividend_check_label(t)
        if sec_chk == "sec" and (d_rate is not None or d_yield is not None or exd or payd):
            out["div_source_conf"] = "yahoo+sec"
        elif sec_chk == "sec":
            out["div_source_conf"] = "sec-only"
        elif (d_rate is not None or d_yield is not None or exd or payd):
            out["div_source_conf"] = "yahoo-only"
        else:
            out["div_source_conf"] = "none"
    except Exception:
        return out
    return out


def get_portfolio_intel(tickers: list[str], ttl_seconds: int = 21600) -> dict[str, dict[str, float | int | str | None]]:
    uniq = sorted({x.upper().strip() for x in tickers if x.strip()})
    if not uniq:
        return {}
    now = time.time()
    with LOCK:
        snapshot = dict(PORT_INTEL_CACHE)
    out: dict[str, dict[str, float | int | str | None]] = {}
    needs_refresh: list[str] = []
    sync_missing: list[str] = []
    for t in uniq:
        cell = snapshot.get(t)
        if cell and isinstance(cell.get("data"), dict):
            out[t] = dict(cell.get("data", {}))
            age_ok = (now - float(cell.get("ts", 0.0))) <= ttl_seconds
            if not age_ok:
                needs_refresh.append(t)
            # If earnings date is missing, force an immediate refresh candidate.
            if out[t].get("earn_days") is None:
                sync_missing.append(t)
        else:
            out[t] = {
                "earn_days": None,
                "target_mean": None,
                "earn_source": "-",
                "dividend_rate": None,
                "dividend_yield_pct": None,
                "ex_div_date": None,
                "pay_div_date": None,
                "fifty_two_low": None,
                "fifty_two_high": None,
                "near_low_pct": None,
                "div_source_conf": "none",
            }
            sync_missing.append(t)
    if SWR_NONBLOCK_DEFAULT:
        refresh = sorted(set(needs_refresh + sync_missing))
        if refresh:
            _start_port_intel_refresh(refresh)
        return out
    # Legacy blocking fallback (disabled by default).
    for t in sync_missing[:8]:
        data = fetch_portfolio_intel_single(t)
        out[t] = dict(data)
        with LOCK:
            PORT_INTEL_CACHE[t] = {"ts": now, "data": dict(data)}
    rest = [t for t in needs_refresh + sync_missing if t not in set(sync_missing[:8])]
    if rest:
        _start_port_intel_refresh(rest)
    return out


def fetch_portfolio_profile_single(ticker: str) -> dict[str, str | None]:
    out: dict[str, str | None] = {"sector": None, "industry": None, "name": None, "country": None, "region": None}
    if yf is None:
        return out
    try:
        tk = yf.Ticker(ticker)
        info = getattr(tk, "info", None) or {}
        out["sector"] = str(info.get("sector") or "").strip() or None
        out["industry"] = str(info.get("industry") or "").strip() or None
        out["name"] = str(info.get("shortName") or info.get("longName") or "").strip() or None
        out["country"] = str(info.get("country") or "").strip() or None
        out["region"] = str(info.get("region") or info.get("exchange") or "").strip() or None
    except Exception:
        return out
    return out


def _load_profile_cache_rows(tickers: list[str]) -> dict[str, dict[str, str | None]]:
    uniq = sorted({str(t or "").strip().upper() for t in tickers if str(t or "").strip()})
    if not uniq:
        return {}
    conn = memory_db()
    try:
        placeholders = ",".join("?" for _ in uniq)
        rows = conn.execute(
            f"""SELECT ticker, name, country, industry, sector
                FROM company_profile_cache
                WHERE ticker IN ({placeholders})""",
            tuple(uniq),
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, dict[str, str | None]] = {}
    for r in rows:
        t = str(r["ticker"] or "").strip().upper()
        if not t:
            continue
        out[t] = {
            "name": str(r["name"] or "").strip() or None,
            "country": str(r["country"] or "").strip() or None,
            "industry": str(r["industry"] or "").strip() or None,
            "sector": str(r["sector"] or "").strip() or None,
            "region": None,
        }
    return out


def _save_profile_cache_row(ticker: str, data: dict[str, str | None]) -> None:
    t = str(ticker or "").strip().upper()
    if not t:
        return
    name = str(data.get("name") or "").strip()
    country = str(data.get("country") or "").strip()
    industry = str(data.get("industry") or "").strip()
    sector = str(data.get("sector") or "").strip()
    if not (name or country or industry or sector):
        return
    conn = memory_db()
    try:
        conn.execute(
            """INSERT INTO company_profile_cache (ticker, name, country, industry, sector, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET
                 name=excluded.name,
                 country=excluded.country,
                 industry=excluded.industry,
                 sector=excluded.sector,
                 updated_at=excluded.updated_at""",
            (t, name, country, industry, sector, dt.datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _port_profile_refresh_worker(tickers: list[str]) -> None:
    now = time.time()
    try:
        for t in tickers:
            data = fetch_portfolio_profile_single(t)
            _save_profile_cache_row(t, data)
            with LOCK:
                PORT_PROFILE_CACHE[t] = {"ts": now, "data": dict(data)}
    finally:
        with LOCK:
            PORT_PROFILE_REFRESH["running"] = False


def _start_port_profile_refresh(tickers: list[str]) -> None:
    uniq = sorted({x.upper().strip() for x in tickers if x.strip()})
    if not uniq:
        return
    with LOCK:
        if bool(PORT_PROFILE_REFRESH.get("running")):
            return
        PORT_PROFILE_REFRESH["running"] = True
    th = threading.Thread(target=_port_profile_refresh_worker, args=(uniq,), daemon=True)
    th.start()


def get_portfolio_profiles(tickers: list[str], ttl_seconds: int = 86400) -> dict[str, dict[str, str | None]]:
    uniq = sorted({x.upper().strip() for x in tickers if x.strip()})
    if not uniq:
        return {}
    now = time.time()
    with LOCK:
        snapshot = dict(PORT_PROFILE_CACHE)
    persisted = _load_profile_cache_rows(uniq)
    out: dict[str, dict[str, str | None]] = {}
    needs_refresh: list[str] = []
    sync_missing: list[str] = []

    def _is_profile_incomplete(p: dict[str, str | None]) -> bool:
        name_v = str(p.get("name") or "").strip()
        industry_v = str(p.get("industry") or "").strip()
        country_v = str(p.get("country") or "").strip()
        return (not name_v) or (not industry_v) or (not country_v)

    for t in uniq:
        cell = snapshot.get(t)
        if cell and isinstance(cell.get("data"), dict):
            out[t] = dict(cell.get("data", {}))
            age_ok = (now - float(cell.get("ts", 0.0))) <= ttl_seconds
            if not age_ok:
                needs_refresh.append(t)
            if _is_profile_incomplete(out[t]):
                sync_missing.append(t)
        elif t in persisted:
            out[t] = dict(persisted.get(t, {}))
            if _is_profile_incomplete(out[t]):
                sync_missing.append(t)
        else:
            out[t] = {"sector": None, "industry": None, "name": None, "country": None, "region": None}
            sync_missing.append(t)
    if SWR_NONBLOCK_DEFAULT:
        refresh = sorted(set(needs_refresh + sync_missing))
        if refresh:
            _start_port_profile_refresh(refresh)
        return out
    # Legacy blocking fallback (disabled by default).
    for t in sync_missing[:10]:
        prof = fetch_portfolio_profile_single(t)
        out[t] = dict(prof)
        with LOCK:
            PORT_PROFILE_CACHE[t] = {"ts": now, "data": dict(prof)}
    rest = [t for t in sync_missing if t not in set(sync_missing[:10])]
    if needs_refresh or rest:
        _start_port_profile_refresh(needs_refresh + rest)
    return out


def _company_profile_enrich_state_path() -> Path:
    return DATA / "company_profile_enrich_scheduler_state.json"


def _company_profile_enrich_last_ts() -> float:
    p = _company_profile_enrich_state_path()
    if not p.exists():
        return 0.0
    try:
        obj = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        return float(obj.get("last_ts") or 0.0)
    except Exception:
        return 0.0


def _company_profile_enrich_save_state(
    tried: int,
    updated: int,
    remaining: int,
    sample: list[str],
) -> None:
    p = _company_profile_enrich_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    obj = {
        "last_ts": time.time(),
        "last_run": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tried": int(tried),
        "updated": int(updated),
        "remaining": int(remaining),
        "sample": [str(x).strip().upper() for x in sample if str(x).strip()][:20],
    }
    p.write_text(json.dumps(obj, ensure_ascii=True, indent=2), encoding="utf-8")


def _seed_profile_cache_from_local_sources(tickers: list[str]) -> None:
    uniq = sorted({str(t or "").strip().upper() for t in tickers if str(t or "").strip()})
    if not uniq:
        return
    now_iso = dt.datetime.now().isoformat()
    conn = memory_db()
    try:
        # Seed names from local companies table.
        placeholders = ",".join("?" for _ in uniq)
        rows = conn.execute(
            f"SELECT ticker, name FROM companies WHERE ticker IN ({placeholders})",
            tuple(uniq),
        ).fetchall()
        for r in rows:
            t = str(r["ticker"] or "").strip().upper()
            n = str(r["name"] or "").strip()
            if not t or not n:
                continue
            conn.execute(
                """INSERT INTO company_profile_cache (ticker, name, country, industry, sector, updated_at)
                   VALUES (?, ?, '', '', '', ?)
                   ON CONFLICT(ticker) DO UPDATE SET
                     name=CASE WHEN trim(company_profile_cache.name)='' THEN excluded.name ELSE company_profile_cache.name END,
                     updated_at=excluded.updated_at""",
                (t, n, now_iso),
            )

        # Seed names from local report tables (earnings/market scanner).
        reports: list[Path] = []
        for base in (REPORTS / ".terminal_inputs", REPORTS / "legacy_archive" / "bulk"):
            if base.exists():
                reports.extend(sorted(base.glob("earnings_radar_*.md"), reverse=True)[:10])
                reports.extend(sorted(base.glob("market_scanner_*.md"), reverse=True)[:10])
        pat = re.compile(r"^\|\s*(?:\d{4}-\d{2}-\d{2}\s*\|\s*)?([A-Z][A-Z0-9.\-]{0,9})\s*\|\s*([^|]+?)\s*\|", re.M)
        hits: dict[str, str] = {}
        u_set = set(uniq)
        for fp in reports:
            try:
                txt = fp.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for m in pat.finditer(txt):
                t = str(m.group(1) or "").strip().upper()
                n = str(m.group(2) or "").strip()
                if not t or t not in u_set or not n or n.upper() == t:
                    continue
                hits.setdefault(t, n)
        for t, n in hits.items():
            conn.execute(
                """INSERT INTO company_profile_cache (ticker, name, country, industry, sector, updated_at)
                   VALUES (?, ?, '', '', '', ?)
                   ON CONFLICT(ticker) DO UPDATE SET
                     name=CASE WHEN trim(company_profile_cache.name)='' THEN excluded.name ELSE company_profile_cache.name END,
                     updated_at=excluded.updated_at""",
                (t, n, now_iso),
            )
        conn.commit()
    finally:
        conn.close()


def _finnhub_profile_single(ticker: str) -> dict[str, str | None]:
    t = str(ticker or "").strip().upper()
    token = (os.getenv("FINNHUB_API_KEY", "") or os.getenv("FINNHUB_TOKEN", "")).strip()
    if not t or not token:
        return {"name": None, "country": None, "industry": None, "sector": None, "region": None}
    try:
        proc = subprocess.run(
            ["curl", "-sS", "--max-time", "10", f"https://finnhub.io/api/v1/stock/profile2?symbol={t}&token={token}"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=12,
        )
        if int(proc.returncode) != 0:
            return {"name": None, "country": None, "industry": None, "sector": None, "region": None}
        obj = json.loads(str(proc.stdout or "{}"))
        if not isinstance(obj, dict):
            return {"name": None, "country": None, "industry": None, "sector": None, "region": None}
        return {
            "name": str(obj.get("name") or "").strip() or None,
            "country": str(obj.get("country") or "").strip() or None,
            "industry": str(obj.get("finnhubIndustry") or "").strip() or None,
            "sector": None,
            "region": None,
        }
    except Exception:
        return {"name": None, "country": None, "industry": None, "sector": None, "region": None}


def _run_company_profile_enrich_once() -> None:
    universe = _company_file_universe(limit=5000)
    if not universe:
        _company_profile_enrich_save_state(tried=0, updated=0, remaining=0, sample=[])
        return

    # Always seed from local sources first so we improve even when network is unavailable.
    _seed_profile_cache_from_local_sources(universe)

    conn = memory_db()
    try:
        placeholders = ",".join("?" for _ in universe)
        rows = conn.execute(
            f"""SELECT ticker, name, industry, country
                FROM company_profile_cache
                WHERE ticker IN ({placeholders})""",
            tuple(universe),
        ).fetchall()
    finally:
        conn.close()

    have: dict[str, tuple[str, str, str]] = {
        str(r["ticker"] or "").strip().upper(): (
            str(r["name"] or "").strip(),
            str(r["industry"] or "").strip(),
            str(r["country"] or "").strip(),
        )
        for r in rows
    }
    candidates = [
        t
        for t in sorted({str(x or "").strip().upper() for x in universe if str(x or "").strip()})
        if (not have.get(t, ("", "", ""))[0]) or (not have.get(t, ("", "", ""))[1])
    ]
    batch = max(20, min(400, int(float(os.getenv("ONYX_COMPANY_PROFILE_ENRICH_BATCH", "120").strip() or "120"))))
    picked = candidates[:batch]

    tried = 0
    updated = 0
    use_finnhub = os.getenv("ONYX_COMPANY_PROFILE_USE_FINNHUB", "1").strip().lower() not in {"0", "false", "no", "off"}
    for t in picked:
        tried += 1
        data = fetch_portfolio_profile_single(t)
        # Yahoo path may fail in this environment; fallback to Finnhub when enabled.
        if use_finnhub and not (data.get("name") or data.get("industry") or data.get("country")):
            data = _finnhub_profile_single(t)
        if data.get("name") or data.get("industry") or data.get("country") or data.get("sector"):
            _save_profile_cache_row(t, data)
            with LOCK:
                PORT_PROFILE_CACHE[t] = {"ts": time.time(), "data": dict(data)}
            updated += 1
        time.sleep(0.03)

    remaining = max(0, len(candidates) - len(picked))
    _company_profile_enrich_save_state(tried=tried, updated=updated, remaining=remaining, sample=picked[:12])


def _company_profile_enrich_worker(interval_seconds: int = 1800) -> None:
    while True:
        try:
            last = _company_profile_enrich_last_ts()
            now = time.time()
            if (now - last) >= max(600, int(interval_seconds)):
                _run_company_profile_enrich_once()
        except Exception:
            pass
        time.sleep(90)


def start_company_profile_enrich_scheduler(interval_seconds: int = 1800) -> None:
    with LOCK:
        if bool(COMPANY_PROFILE_ENRICH_SCHED.get("started")):
            return
        enabled = os.getenv("ONYX_COMPANY_PROFILE_ENRICH_ON", "1").strip().lower() not in {"0", "false", "no", "off"}
        if not enabled:
            return
        COMPANY_PROFILE_ENRICH_SCHED["started"] = True
    th = threading.Thread(target=_company_profile_enrich_worker, args=(max(600, int(interval_seconds)),), daemon=True)
    th.start()


def _port_intel_refresh_worker(tickers: list[str]) -> None:
    now = time.time()
    try:
        for t in tickers:
            data = fetch_portfolio_intel_single(t)
            with LOCK:
                PORT_INTEL_CACHE[t] = {"ts": now, "data": dict(data)}
    finally:
        with LOCK:
            PORT_INTEL_REFRESH["running"] = False


def _start_port_intel_refresh(tickers: list[str]) -> None:
    uniq = sorted({x.upper().strip() for x in tickers if x.strip()})
    if not uniq:
        return
    with LOCK:
        if bool(PORT_INTEL_REFRESH.get("running")):
            return
        PORT_INTEL_REFRESH["running"] = True
    th = threading.Thread(target=_port_intel_refresh_worker, args=(uniq,), daemon=True)
    th.start()


def _quote_refresh_worker(tickers: list[str]) -> None:
    fresh: dict[str, dict[str, float | None]] = {}
    try:
        for t in tickers:
            fresh[t] = fetch_quote_single(t)
    finally:
        now = time.time()
        with LOCK:
            cache_data = QUOTE_CACHE.get("data", {})
            merged = dict(cache_data) if isinstance(cache_data, dict) else {}
            merged.update(fresh)
            QUOTE_CACHE["ts"] = now
            QUOTE_CACHE["data"] = merged
            QUOTE_REFRESH["running"] = False


def _start_quote_refresh(tickers: list[str]) -> None:
    if not tickers:
        return
    with LOCK:
        if bool(QUOTE_REFRESH.get("running")):
            return
        QUOTE_REFRESH["running"] = True
    th = threading.Thread(target=_quote_refresh_worker, args=(tickers,), daemon=True)
    th.start()


def get_live_quotes(tickers: list[str], ttl_seconds: int = 60) -> dict[str, dict[str, float | None]]:
    uniq = sorted({t.upper().strip() for t in tickers if t.strip()})
    if not uniq:
        return {}
    now = time.time()
    with LOCK:
        cache_ts = float(QUOTE_CACHE.get("ts", 0.0))
        cache_data = QUOTE_CACHE.get("data", {})
        if isinstance(cache_data, dict) and (now - cache_ts) <= ttl_seconds and all(t in cache_data for t in uniq):
            return {t: cache_data[t] for t in uniq}
        stale = (now - cache_ts) > ttl_seconds
        snapshot = dict(cache_data) if isinstance(cache_data, dict) else {}

    # Return immediately from cache (or N/A placeholders), refresh in background.
    out = {t: snapshot.get(t, _quote_empty()) for t in uniq}
    if stale or any(t not in snapshot for t in uniq):
        _start_quote_refresh(uniq)
    return out


def run_cmd(args: list[str]) -> list[str]:
    try:
        out = subprocess.check_output(args, text=True, cwd=str(ROOT), stderr=subprocess.DEVNULL)
        return [ln.strip() for ln in out.splitlines() if ln.strip()]
    except Exception:
        return []


def _file_sig(path_s: str) -> str:
    if not path_s:
        return ""
    try:
        mt = os.path.getmtime(path_s)
        sz = os.path.getsize(path_s)
        return f"{path_s}:{int(mt)}:{sz}"
    except Exception:
        return ""


def _cached_rank(kind: str, key: str, args: list[str]) -> list[str]:
    if not key:
        return []
    stale: list[str] = []
    should_block_fetch = False
    with LOCK:
        cell = SIGNAL_CACHE.get(kind, {})
        if cell.get("key") == key and isinstance(cell.get("data"), list):
            return list(cell.get("data", []))
        if isinstance(cell.get("data"), list):
            stale = list(cell.get("data", []))
        # Optional blocking boot fetch; default is non-blocking for fast first paint.
        if SIGNAL_BLOCKING_BOOT_DEFAULT and not stale:
            should_block_fetch = True
        running = bool(SIGNAL_REFRESH.get(kind, False))
        if not running and not should_block_fetch:
            SIGNAL_REFRESH[kind] = True
            th = threading.Thread(target=_signal_refresh_worker, args=(kind, key, args), daemon=True)
            th.start()
    if should_block_fetch:
        data = run_cmd(args)
        with LOCK:
            SIGNAL_CACHE[kind] = {"key": key, "data": list(data)}
            SIGNAL_REFRESH[kind] = False
        return data
    return stale


def _signal_refresh_worker(kind: str, key: str, args: list[str]) -> None:
    data = run_cmd(args)
    with LOCK:
        SIGNAL_CACHE[kind] = {"key": key, "data": list(data)}
        SIGNAL_REFRESH[kind] = False


def get_macro_market_snapshot(ttl_seconds: int = 300) -> dict[str, object]:
    symbols: list[tuple[str, str, str]] = [
        ("S&P 500", "^GSPC", "Indexes"),
        ("Nasdaq", "^IXIC", "Indexes"),
        ("Dow", "^DJI", "Indexes"),
        ("Russell 2000", "^RUT", "Indexes"),
        ("VIX", "^VIX", "Risk"),
        ("US 10Y", "^TNX", "Rates"),
        ("Gold", "GC=F", "Commodities"),
        ("Silver", "SI=F", "Commodities"),
        ("Crude Oil", "CL=F", "Commodities"),
        ("Nat Gas", "NG=F", "Commodities"),
        ("Copper", "HG=F", "Commodities"),
    ]
    key = "macro:v1:" + "|".join(f"{n}:{t}" for n, t, _ in symbols)
    cached = _cache_get(key, ttl_seconds=ttl_seconds)
    if isinstance(cached, dict):
        return cached
    quotes = get_live_quotes([t for _, t, _ in symbols], ttl_seconds=60)

    def _fred_latest_pair(series_id: str) -> tuple[float | None, float | None]:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={urllib.parse.quote(series_id)}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            vals: list[float] = []
            for ln in raw.splitlines()[1:]:
                parts = ln.split(",", 1)
                if len(parts) != 2:
                    continue
                v = parts[1].strip()
                if not v or v == ".":
                    continue
                try:
                    vals.append(float(v))
                except Exception:
                    continue
            if not vals:
                return None, None
            last = vals[-1]
            prev = vals[-2] if len(vals) > 1 else None
            return last, prev
        except Exception:
            return None, None

    net_ok_cached = _cache_get("net:quick", ttl_seconds=120)
    if isinstance(net_ok_cached, bool):
        net_ok = net_ok_cached
    else:
        net_ok = False
        try:
            req = urllib.request.Request("https://fred.stlouisfed.org", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=1.0):
                net_ok = True
        except Exception:
            net_ok = False
        _cache_put("net:quick", net_ok)

    fred_map = {
        "S&P 500": "SP500",
        "Nasdaq": "NASDAQCOM",
        "Dow": "DJIA",
        "US 10Y": "DGS10",
        "VIX": "VIXCLS",
        "Gold": "GOLDAMGBD228NLBM",
        "Silver": "SLVPRUSD",
        "Crude Oil": "DCOILWTICO",
        "Nat Gas": "DHHNGSP",
        "Copper": "PCOPPUSDM",
    }
    rows: list[dict[str, object]] = []
    src_notes: list[str] = []
    for name, ticker, grp in symbols:
        q = quotes.get(ticker, {})
        px = q.get("price")
        day = q.get("day_pct")
        source = "Yahoo"
        if net_ok and ((not isinstance(px, float)) or (not isinstance(day, float))):
            sid = fred_map.get(name)
            if sid:
                last, prev = _fred_latest_pair(sid)
                if isinstance(last, float):
                    if not isinstance(px, float):
                        px = last
                    if not isinstance(day, float) and isinstance(prev, float) and prev != 0:
                        day = (last - prev) / abs(prev) * 100.0
                    source = "FRED"
        src_notes.append(f"{name}:{source}")
        rows.append(
            {
                "group": grp,
                "name": name,
                "ticker": ticker,
                "price": px if isinstance(px, float) else None,
                "day": day if isinstance(day, float) else None,
                "source": source,
            }
        )
    out = {
        "rows": rows,
        "asof": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_mix": ", ".join(src_notes),
    }
    _cache_put(key, out)
    return out


def run_cmd_raw(args: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True)
    return p.returncode, p.stdout or "", p.stderr or ""


def read_portfolio_tickers() -> list[str]:
    out: list[str] = []
    p = DATA / "portfolio.csv"
    if not p.exists():
        return out
    for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        t = s.split(",", 1)[0].strip().upper()
        if t:
            out.append(t)
    return sorted(set(out))


def read_watchlist_tickers() -> list[str]:
    out: set[str] = set()
    for wl in [DATA / "my_watchlist.txt"]:
        if not wl.exists():
            continue
        for ln in wl.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            t = s.split(",", 1)[0].strip().upper()
            if t:
                out.add(t)
    return sorted(out)


def _mode_universe(mode: str) -> list[str]:
    portfolio = read_portfolio_tickers()
    watchlist = read_watchlist_tickers()
    if mode == "portfolio":
        return sorted(set(portfolio))
    if mode == "watchlist":
        return sorted(set(watchlist))
    return sorted(set(portfolio + watchlist))


def quick_local_answer(question: str, mode: str) -> str:
    q = (question or "").strip()
    if not q:
        return "Please enter a question."
    universe = _mode_universe(mode)
    if not universe:
        return "No tickers found in your selected scope yet. Add watchlist/portfolio symbols first."

    q_tokens = set(re.findall(r"\b[A-Z][A-Z0-9.\-]{0,6}\b", q.upper()))
    focus = [t for t in universe if t in q_tokens]
    if not focus:
        focus = universe[:5]

    red_flag = latest("reports/.terminal_inputs/red_flag_alert_*.txt")
    earnings = _latest_earnings_file()
    alerts = run_cmd(["python3", "tools/red_flag_rank.py", "--file", red_flag, "--limit", "120"]) if red_flag else []
    week_events = run_cmd(["python3", "tools/earnings_watch_rank.py", "--file", earnings, "--limit", "120", "--scope", "week"]) if earnings else []
    upcoming = run_cmd(["python3", "tools/earnings_watch_rank.py", "--file", earnings, "--limit", "120", "--scope", "upcoming"]) if earnings else []

    portfolio_rows = {r[0].upper(): r for r in read_portfolio_rows(DATA / "portfolio.csv") if r and r[0]}
    watch_rows = {e["ticker"].upper(): e for e in read_watchlist_entries(DATA / "my_watchlist.txt")}
    quotes = get_live_quotes(focus)

    lines: list[str] = []
    lines.append(f"Scope: {mode} | Focus: {', '.join(focus)}")
    lines.append("")
    lines.append("Snapshot")
    for t in focus[:6]:
        qd = quotes.get(t, {})
        now = qd.get("price")
        day = qd.get("day_pct")
        row = portfolio_rows.get(t)
        if row:
            shares = to_float(row[1]) or 0.0
            cost = to_float(row[2])
            pnl_pct = ((float(now) - cost) / cost * 100.0) if (now is not None and cost not in (None, 0.0)) else None
            lines.append(f"- {t}: now {fmt_money(now if isinstance(now, float) else None)}, day {fmt_pct(day if isinstance(day, float) else None)}, pos {shares:g} sh @ {fmt_money(cost)} ({fmt_pct(pnl_pct)})")
        else:
            add_px = to_float(watch_rows.get(t, {}).get("added_price", ""))
            since_add = ((float(now) - add_px) / add_px * 100.0) if (now is not None and add_px not in (None, 0.0)) else None
            lines.append(f"- {t}: now {fmt_money(now if isinstance(now, float) else None)}, day {fmt_pct(day if isinstance(day, float) else None)}, since add {fmt_pct(since_add)}")

    def _hit(rows: list[str], ticker: str) -> list[str]:
        out = []
        for r in rows:
            if f"| {ticker} |" in r:
                out.append(r)
            if len(out) >= 2:
                break
        return out

    lines.append("")
    lines.append("Signals")
    any_signal = False
    for t in focus[:5]:
        a = _hit(alerts, t)
        e = _hit(week_events + upcoming, t)
        if not a and not e:
            continue
        any_signal = True
        lines.append(f"- {t}:")
        for r in a:
            lines.append(f"  alert: {r}")
        for r in e:
            lines.append(f"  earnings: {r}")
    if not any_signal:
        lines.append("- No high-priority alert/earnings lines matched your focus tickers.")

    lines.append("")
    lines.append("Notes")
    note_rows = []
    for t in focus[:3]:
        note_rows.extend(list_investor_notes(limit=2, ticker=t))
    if note_rows:
        for r in note_rows[:4]:
            lines.append(f"- {(r['ticker'] or 'PORTFOLIO')}: {(r['created_at'] or '')[:16].replace('T',' ')} | {(r['note'] or '')[:120]}")
    else:
        lines.append("- No recent ticker notes in local DB for this focus set.")

    lines.append("")
    lines.append("For deeper reasoning/catalyst framing, switch chat speed to Deep.")
    return "\n".join(lines)


def ask_agent_from_dashboard(question: str, mode: str, speed: str = "quick") -> str:
    mode = (mode or "full").strip().lower()
    speed = (speed or "quick").strip().lower()
    q = (question or "").strip()
    if not q:
        return "Please enter a question."

    universe = _mode_universe(mode)
    if mode == "portfolio":
        ctx = f"Focus strictly on my portfolio tickers: {', '.join(universe) if universe else 'none'}."
    elif mode == "watchlist":
        ctx = f"Focus strictly on my watchlist tickers: {', '.join(universe) if universe else 'none'}."
    elif mode == "global":
        ctx = "Answer globally using live market/public sources. You are not limited to my holdings."
    else:
        ctx = f"Use my full universe (portfolio + watchlist): {', '.join(universe) if universe else 'none'}."

    if speed == "quick":
        if mode == "global":
            return quick_global_answer(q)
        return quick_local_answer(q, mode)

    if mode == "global":
        # Global deep mode bypasses internal watchlist-scoped agent.
        live_ctx = _global_live_snapshot(q)
        if _hybrid_ask_ai is None:
            return live_ctx + "\n\nAI reasoning unavailable (llm_engine not loaded)."
        try:
            return _hybrid_ask_ai(
                f"Question:\n{q}\n\nLive data snapshot:\n{live_ctx}",
                "You are a global financial assistant. Use ONLY the provided live snapshot as evidence. "
                "Return: (1) direct answer sentence, (2) 3 evidence bullets, (3) confidence 0-100. "
                "If evidence is thin, explicitly say: 'Insufficient live evidence in snapshot.'",
            )
        except Exception as e:
            return f"Global deep AI failed: {str(e)[:160]}"

    l2_ctx = ""
    if mode in {"portfolio", "watchlist", "full"}:
        l2 = get_l2_snapshot("portfolio", ttl_seconds=900)
        rows = list(l2.get("rows") or [])
        if rows:
            parts = []
            for r in rows[:8]:
                parts.append(
                    f"{r.get('ticker')}: prio={r.get('priority')}, total={r.get('total')}, "
                    f"conflict={r.get('conflict')}, urgency={r.get('urgency')}, thesis={r.get('thesis_sentiment')}"
                )
            l2_ctx = "L2 Synthesis Snapshot:\n" + "\n".join(parts)
    prompt = f"{ctx}\n\n{l2_ctx}\n\nQuestion:\n{q}" if l2_ctx else f"{ctx}\n\nQuestion:\n{q}"
    cmd = ["python3", "research_agent.py", "ask", prompt]
    try:
        p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=150)
    except subprocess.TimeoutExpired:
        return "Agent timed out. Please try a shorter question."
    if p.returncode != 0:
        err = (p.stderr or "").strip()
        return f"Agent error: {err[:400] if err else 'unknown error'}"
    ans = (p.stdout or "").strip()
    return ans or "No response."


def _infer_ticker_from_question(question: str) -> str:
    q = (question or "").strip()
    if not q:
        return ""
    m = re.search(r"\$([A-Za-z]{1,6})\b", q)
    if m:
        return m.group(1).upper()
    qu = q.upper()
    # Strong pattern first: "PDD stock", "PDD ticker", etc.
    m2 = re.search(r"\b([A-Z]{1,6})\s+(?:STOCK|SHARE|SHARES|TICKER|COMPANY|REVENUE|EARNINGS|NEWS)\b", qu)
    if m2:
        return m2.group(1).upper()
    # Try explicit upper-case symbols with filters.
    tokens = re.findall(r"\b[A-Z]{1,6}\b", qu)
    stop = {
        "A", "AN", "AND", "ARE", "AS", "AT", "BE", "BUT", "BY", "CAN", "COULD", "DAY", "DO", "DOWN",
        "FOR", "FROM", "GET", "HAS", "HAVE", "HOW", "I", "IF", "IN", "INTO", "IS", "IT", "ITS",
        "LLM", "ME", "MY", "NEWS", "NO", "NOT", "OF", "ON", "OR", "OUR", "PLEASE", "SHOULD", "SO",
        "STOCK", "TELL", "THAT", "THE", "THEY", "THIS", "TO", "TODAY", "UP", "WANT", "WHAT", "WHEN",
        "WHICH", "WHO", "WHY", "WITH", "YOU", "YOUR", "AI", "GDP", "CPI", "ETF", "CEO", "CFO", "YOY", "SEC", "USA",
    }
    finance_words = {"STOCK", "SHARE", "SHARES", "TICKER", "REVENUE", "EARNINGS", "PRICE", "NEWS", "DOWN", "UP"}
    has_finance_context = any(w in qu for w in finance_words)
    known_tickers = set(_mode_universe("full"))
    cmap = _company_name_map()
    known_tickers.update(str(v).upper() for v in cmap.values())
    for t in tokens:
        if t in stop:
            continue
        if t in known_tickers:
            return t
        if has_finance_context and len(t) <= 5:
            return t
    # Try company-name mapping from local DB aliases.
    words = re.findall(r"[A-Za-z][A-Za-z&\.\-]{1,}", q)
    for n in (3, 2, 1):
        for i in range(0, max(0, len(words) - n + 1)):
            phrase = " ".join(words[i : i + n])
            key = _normalize_company_key(phrase)
            if not key:
                continue
            aliases = {
                "microsoft": "MSFT",
                "apple": "AAPL",
                "alphabet": "GOOGL",
                "google": "GOOGL",
                "amazon": "AMZN",
                "meta": "META",
                "facebook": "META",
                "nvidia": "NVDA",
                "tesla": "TSLA",
                "salesforce": "CRM",
                "hubspot": "HUBS",
                "gartner": "IT",
                "adobe": "ADBE",
            }
            if key in aliases:
                return aliases[key]
            cmap = _company_name_map()
            if key in cmap:
                return str(cmap[key]).upper()
    return ""


def _live_revenue_snapshot(ticker: str) -> str:
    if yf is None:
        return "Revenue data unavailable (yfinance not loaded)."
    try:
        tk = yf.Ticker(ticker)
        fin = getattr(tk, "financials", None)
        if fin is None or not hasattr(fin, "index") or not hasattr(fin, "columns") or len(getattr(fin, "columns", [])) == 0:
            return "Revenue data unavailable."
        row = None
        for k in ["Total Revenue", "Revenue", "Operating Revenue"]:
            if k in fin.index:
                row = k
                break
        if row is None:
            return "Revenue row not found in latest filings."
        cols = list(fin.columns)[:2]
        rev0 = _to_num(fin.at[row, cols[0]]) if len(cols) > 0 else None
        rev1 = _to_num(fin.at[row, cols[1]]) if len(cols) > 1 else None
        if rev0 is None:
            return "Revenue data unavailable."
        if rev1 not in (None, 0.0):
            yoy = (rev0 - rev1) / abs(rev1) * 100.0
            return f"Latest annual revenue: {_fmt_big(rev0)} (YoY: {yoy:+.2f}%)."
        return f"Latest annual revenue: {_fmt_big(rev0)}."
    except Exception as e:
        return f"Revenue lookup failed: {str(e)[:120]}"


def _fetch_google_news_headlines(query: str, limit: int = 5) -> list[str]:
    q = (query or "").strip()
    if not q:
        return []
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
        out: list[str] = []
        for item in root.findall(".//item")[:limit]:
            title = (item.findtext("title") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            if title:
                out.append(f"- {title}" + (f" ({pub[:16]})" if pub else ""))
        return out
    except Exception:
        return []


def _fetch_reuters_business_headlines(limit: int = 5) -> list[str]:
    feeds = [
        "https://feeds.reuters.com/reuters/businessNews",
        "https://feeds.reuters.com/reuters/worldNews",
    ]
    out: list[str] = []
    for url in feeds:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw = resp.read()
            root = ET.fromstring(raw)
            for item in root.findall(".//item"):
                title = (item.findtext("title") or "").strip()
                pub = (item.findtext("pubDate") or "").strip()
                if title:
                    line = f"- {title}" + (f" ({pub[:16]})" if pub else "")
                    if line not in out:
                        out.append(line)
                if len(out) >= limit:
                    return out
        except Exception:
            continue
    return out


def _live_price_context(ticker: str) -> str:
    q = get_live_quotes([ticker]).get(ticker, {})
    px = q.get("price")
    day = q.get("day_pct")
    base = f"Price now: {fmt_money(px if isinstance(px, float) else None)} | Day: {fmt_pct(day if isinstance(day, float) else None)}"
    if yf is None:
        return base
    try:
        tk = yf.Ticker(ticker)
        info = getattr(tk, "info", None) or {}
        post_px = _to_num(info.get("postMarketPrice"))
        post_chg = _to_num(info.get("postMarketChangePercent"))
        if post_px is not None:
            if post_chg is not None:
                return base + f" | After-hours: ${post_px:,.2f} ({post_chg:+.2f}%)"
            return base + f" | After-hours: ${post_px:,.2f}"
        pre_px = _to_num(info.get("preMarketPrice"))
        pre_chg = _to_num(info.get("preMarketChangePercent"))
        if pre_px is not None:
            if pre_chg is not None:
                return base + f" | Pre-market: ${pre_px:,.2f} ({pre_chg:+.2f}%)"
            return base + f" | Pre-market: ${pre_px:,.2f}"
    except Exception:
        pass
    return base


def _live_news_snapshot(ticker: str, limit: int = 4) -> str:
    out: list[str] = []
    if yf is not None:
        try:
            tk = yf.Ticker(ticker)
            rows = getattr(tk, "news", None) or []
            for r in rows[:limit]:
                title = str(r.get("title") or "").strip()
                pub = str(r.get("publisher") or "").strip()
                ts = r.get("providerPublishTime")
                d = ""
                try:
                    if ts:
                        d = dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
                except Exception:
                    d = ""
                if title:
                    out.append(f"- {title}" + (f" ({pub}, {d})" if pub or d else ""))
        except Exception:
            pass
    if len(out) < limit:
        extra = _fetch_google_news_headlines(f"{ticker} stock", limit=limit)
        for ln in extra:
            if ln not in out:
                out.append(ln)
            if len(out) >= limit:
                break
    return "\n".join(out) if out else "No recent headlines returned right now."


def _is_weather_query(question: str) -> bool:
    q = (question or "").strip().lower()
    if not q:
        return False
    keys = ["weather", "temperature", "temp", "rain", "snow", "forecast", "wind", "humidity"]
    return any(k in q for k in keys)


def _extract_location_from_question(question: str) -> str:
    q = (question or "").strip()
    m = re.search(r"\bin\s+([A-Za-z][A-Za-z .,'-]{1,60})$", q, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip(" ?!.,")
    # Fallback: remove common weather words and keep last meaningful chunk.
    clean = re.sub(r"\b(how|what|is|the|weather|like|today|now|forecast|temperature|in)\b", " ", q, flags=re.IGNORECASE)
    clean = re.sub(r"\s+", " ", clean).strip(" ?!.,")
    return clean if clean else "Ireland"


def _live_weather_snapshot(question: str) -> str:
    loc = _extract_location_from_question(question)
    cache_key = f"weather:{loc.lower()}"
    cached = _cache_get(cache_key, ttl_seconds=600)
    if isinstance(cached, str) and cached.strip():
        return cached
    try:
        g_url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode({"name": loc, "count": 1, "language": "en", "format": "json"})
        g_req = urllib.request.Request(g_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(g_req, timeout=3.0) as resp:
            g_raw = resp.read().decode("utf-8", errors="ignore")
        g_obj = json.loads(g_raw or "{}")
        rows = g_obj.get("results") or []
        if not rows:
            return f"Weather lookup failed: location not found for '{loc}'."
        row = rows[0]
        lat = row.get("latitude")
        lon = row.get("longitude")
        name = str(row.get("name") or loc)
        admin = str(row.get("admin1") or row.get("country") or "").strip()
        tz = str(row.get("timezone") or "auto")
        if lat is None or lon is None:
            return f"Weather lookup failed: coordinates unavailable for '{loc}'."

        w_url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(
            {
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,wind_speed_10m",
                "hourly": "temperature_2m,precipitation_probability",
                "forecast_days": 1,
                "timezone": tz,
            }
        )
        w_req = urllib.request.Request(w_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(w_req, timeout=3.0) as resp:
            w_raw = resp.read().decode("utf-8", errors="ignore")
        w_obj = json.loads(w_raw or "{}")
        cur = w_obj.get("current") or {}
        units = w_obj.get("current_units") or {}
        temp = cur.get("temperature_2m")
        hum = cur.get("relative_humidity_2m")
        feels = cur.get("apparent_temperature")
        rain = cur.get("precipitation")
        wind = cur.get("wind_speed_10m")
        tstamp = str(cur.get("time") or "-")

        temp_u = str(units.get("temperature_2m") or "C")
        wind_u = str(units.get("wind_speed_10m") or "km/h")
        rain_u = str(units.get("precipitation") or "mm")

        out = (
            f"Live weather for {name}" + (f", {admin}" if admin else "") + "\n"
            f"As of {tstamp} ({tz})\n"
            f"Temperature: {temp}{temp_u} | Feels like: {feels}{temp_u}\n"
            f"Humidity: {hum}% | Wind: {wind} {wind_u} | Precipitation: {rain} {rain_u}\n"
            "Source: Open-Meteo Geocoding + Forecast API."
        )
        _cache_put(cache_key, out)
        return out
    except Exception as e:
        return f"Weather live data unavailable right now: {str(e)[:140]}"


def _is_macro_query(question: str) -> bool:
    q = (question or "").lower()
    keys = [
        "cpi", "inflation", "fed funds", "interest rate", "rates", "unemployment",
        "jobs", "payroll", "nfp", "macro", "economy", "treasury yield", "10y",
    ]
    return any(k in q for k in keys)


def _fred_latest_pair_fast(series_id: str) -> tuple[float | None, float | None]:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={urllib.parse.quote(series_id)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        vals: list[float] = []
        for ln in raw.splitlines()[1:]:
            parts = ln.split(",", 1)
            if len(parts) != 2:
                continue
            v = parts[1].strip()
            if not v or v == ".":
                continue
            try:
                vals.append(float(v))
            except Exception:
                continue
        if not vals:
            return None, None
        last = vals[-1]
        prev = vals[-2] if len(vals) > 1 else None
        return last, prev
    except Exception:
        return None, None


def _live_macro_snapshot(question: str) -> str:
    key = "macro:snapshot:v1"
    cached = _cache_get(key, ttl_seconds=600)
    if isinstance(cached, str) and cached.strip():
        return cached
    series = {
        "CPI YoY proxy (CPIAUCSL)": "CPIAUCSL",
        "Fed Funds (FEDFUNDS)": "FEDFUNDS",
        "Unemployment (UNRATE)": "UNRATE",
        "US 10Y Yield (DGS10)": "DGS10",
        "Payrolls (PAYEMS)": "PAYEMS",
    }
    lines = ["US Macro Snapshot (official series)"]
    ok = 0
    for label, sid in series.items():
        last, prev = _fred_latest_pair_fast(sid)
        if isinstance(last, float):
            ok += 1
            if isinstance(prev, float) and prev != 0:
                chg = (last - prev) / abs(prev) * 100.0
                lines.append(f"- {label}: {last:.2f} (vs prior: {chg:+.2f}%)")
            else:
                lines.append(f"- {label}: {last:.2f}")
        else:
            lines.append(f"- {label}: unavailable")
    lines.append("Source: FRED (Federal Reserve Economic Data).")
    if ok == 0:
        lines.append("No live macro series could be fetched right now.")
    out = "\n".join(lines)
    _cache_put(key, out)
    return out


def _is_filing_query(question: str) -> bool:
    q = (question or "").lower()
    keys = ["10-k", "10q", "10-q", "8-k", "filing", "sec", "risk factor", "md&a", "mda", "annual report"]
    return any(k in q for k in keys)


def _live_sec_filing_snapshot(question: str) -> str:
    t = _infer_ticker_from_question(question)
    if not t:
        return "SEC filings route: please include a ticker (example: 'Summarize CRM 10-K risks')."
    conn = research_db()
    try:
        rows = conn.execute(
            "SELECT form, date, path FROM filings WHERE ticker = ? ORDER BY date DESC LIMIT 6",
            (t,),
        ).fetchall()
        if not rows:
            return f"SEC filings route: no local filings found for {t}."
        lines = [f"SEC local filing snapshot | Ticker: {t}"]
        for r in rows[:4]:
            form = str(r["form"] or "-")
            d = str(r["date"] or "-")
            p = str(r["path"] or "")
            lines.append(f"- {form} | {d} | {Path(p).name if p else '-'}")
        # Pull key filing references even when recent rows are mostly Form 4.
        key_rows = conn.execute(
            """SELECT form, date, path FROM filings
               WHERE ticker = ? AND form IN ('10-K','10-Q','8-K')
               ORDER BY date DESC LIMIT 3""",
            (t,),
        ).fetchall()
        if key_rows:
            lines.append("Key filings:")
            for r in key_rows:
                lines.append(f"- {str(r['form'] or '-')} | {str(r['date'] or '-')} | {Path(str(r['path'] or '')).name}")
        # Pull 10-K/10-Q snippet for direct evidence.
        kq = next((r for r in key_rows if str(r["form"] or "") in {"10-K", "10-Q"}), None)
        if kq:
            txt = _read_filing_text(str(kq["path"] or ""), max_chars=120000)
            if txt:
                low = txt.lower()
                risk_idx = low.find("risk factors")
                if risk_idx >= 0:
                    snippet = re.sub(r"\s+", " ", txt[risk_idx:risk_idx + 500]).strip()
                    lines.append(f"Risk Factors snippet: {snippet[:320]}...")
                biz_idx = low.find("item 1")
                if biz_idx >= 0:
                    snippet2 = re.sub(r"\s+", " ", txt[biz_idx:biz_idx + 500]).strip()
                    lines.append(f"Business snippet: {snippet2[:320]}...")
        lines.append("Source: SEC local filings database/cache.")
        return "\n".join(lines)
    finally:
        conn.close()


def _is_stock_move_query(question: str) -> bool:
    q = (question or "").lower()
    return ("why" in q or "what happened" in q) and any(k in q for k in ["stock", "up", "down", "after hours", "premarket", "pre-market"])


def _live_stock_move_snapshot(question: str) -> str:
    t = _infer_ticker_from_question(question)
    if not t:
        return "Stock-move route: include a ticker (example: 'Why is PDD stock down today?')."
    lines = [f"Stock move evidence chain | Ticker: {t}", _live_price_context(t)]
    lines.append("Recent headlines:")
    lines.append(_live_news_snapshot(t, limit=5))
    # Earnings context from local ranked feed.
    earn = _latest_earnings_file()
    if earn:
        rows = run_cmd(["python3", "tools/earnings_watch_rank.py", "--file", earn, "--limit", "80", "--scope", "week"])
        hits = [r for r in rows if f"| {t} |" in r][:3]
        if hits:
            lines.append("Earnings context:")
            lines.extend(f"- {h}" for h in hits)
    # Filing context from local SEC cache.
    conn = research_db()
    try:
        f = conn.execute(
            "SELECT form, date FROM filings WHERE ticker = ? ORDER BY date DESC LIMIT 3",
            (t,),
        ).fetchall()
        if f:
            lines.append("Latest SEC filings:")
            for r in f:
                lines.append(f"- {str(r['form'] or '-')} | {str(r['date'] or '-')}")
    finally:
        conn.close()
    lines.append("Source: Yahoo Finance price/news + Google News RSS + local earnings feed + SEC local filings.")
    return "\n".join(lines)


def _global_live_snapshot(question: str) -> str:
    q = (question or "").strip()
    if not q:
        return "Please enter a question."
    if _is_weather_query(q):
        return _live_weather_snapshot(q)
    if _is_macro_query(q):
        return _live_macro_snapshot(q)
    if _is_filing_query(q):
        return _live_sec_filing_snapshot(q)
    if _is_stock_move_query(q):
        return _live_stock_move_snapshot(q)
    t = _infer_ticker_from_question(q)
    ql = q.lower()
    wants_news = any(k in ql for k in ["news", "headline", "what happened", "what is this about"])
    wants_revenue = "revenue" in ql
    if t:
        lines = [f"Global mode | Ticker: {t}"]
        lines.append(_live_price_context(t))
        if wants_revenue:
            lines.append(_live_revenue_snapshot(t))
        if wants_news:
            lines.append("Recent headlines:")
            lines.append(_live_news_snapshot(t, limit=5))
        if not wants_news and not wants_revenue:
            # Default quick pack for any ticker question.
            lines.append(_live_revenue_snapshot(t))
            lines.append("Recent headlines:")
            lines.append(_live_news_snapshot(t, limit=3))
        lines.append("Other market headlines:")
        reu = _fetch_reuters_business_headlines(limit=3)
        lines.append("\n".join(reu) if reu else "Reuters feed unavailable right now.")
        lines.append("Source: Yahoo Finance + Google News RSS + Reuters RSS.")
        return "\n".join(lines)
    # No ticker inferred: still provide live web context for real global chat.
    lines = ["Global mode | No ticker inferred"]
    lines.append(f"Question context: {q}")
    lines.append("Query-matched headlines:")
    gg = _fetch_google_news_headlines(q, limit=6)
    lines.append("\n".join(gg) if gg else "No Google News headlines returned right now.")
    lines.append("Top macro/business headlines:")
    reu = _fetch_reuters_business_headlines(limit=6)
    lines.append("\n".join(reu) if reu else "Reuters feed unavailable right now.")
    lines.append("Source: Google News RSS + Reuters RSS (+ Yahoo when ticker is inferred).")
    return "\n".join(lines)


def quick_global_answer(question: str) -> str:
    q = (question or "").strip()
    if _is_weather_query(q):
        return _live_weather_snapshot(q)
    if _is_macro_query(q):
        return _live_macro_snapshot(q)
    if _is_filing_query(q):
        return _live_sec_filing_snapshot(q)
    if _is_stock_move_query(q):
        return _live_stock_move_snapshot(q)
    snap = _global_live_snapshot(q)
    if _hybrid_ask_ai is None:
        return snap
    ql = q.lower()
    wants_reasoning = any(k in ql for k in ["why", "what happened", "explain", "about", "summarize", "reason"])
    if not wants_reasoning:
        return snap
    try:
        return _hybrid_ask_ai(
            f"Question:\n{q}\n\nLive source snapshot:\n{snap}",
            "You are a financial assistant. Use ONLY evidence from the provided live snapshot. "
            "Return: (1) direct answer sentence, (2) 3 bullet drivers with evidence lines, (3) confidence 0-100. "
            "If evidence is missing, state exactly: 'Insufficient live evidence in snapshot.'",
        )
    except Exception as e:
        return snap + f"\n\nAI reasoning unavailable: {str(e)[:160]}"


def _chat_style_system(style: str) -> str:
    s = (style or "analyst").strip().lower()
    if s == "concise":
        return "Answer in 4-6 short lines. Be direct, no fluff."
    if s == "deep":
        return "Answer as a senior buy-side analyst. Include drivers, risks, and what to verify next."
    return "Answer clearly like an institutional analyst with practical detail."


def _chat_history_block(limit: int = 6) -> str:
    rows = CHAT_HISTORY[-limit:]
    if not rows:
        return "No prior turns."
    out: list[str] = []
    for r in rows:
        q = str(r.get("q", "")).strip()
        a = str(r.get("a", "")).strip()
        if not q or not a:
            continue
        out.append(f"User: {q}\nAssistant: {a[:500]}")
    return "\n\n".join(out) if out else "No prior turns."


def _ask_llm_with_provider(
    prompt: str,
    system: str,
    provider: str = "auto",
    model_override: str = "",
    strict_provider: bool = False,
) -> tuple[str, str, str]:
    if _HybridAIEngine is None:
        raise RuntimeError("llm_engine unavailable")
    pref = (provider or "auto").strip().lower()
    mo = (model_override or "").strip()
    fb = pref if (strict_provider and pref != "auto") else None
    eng = _HybridAIEngine(
        provider_override=(None if pref == "auto" else pref),
        fallback_override=fb,
        model_override=(mo or None),
    )
    return eng.ask_ai_with_meta(prompt, system)


def _ai_cached_reliable_summary(
    cache_key: str,
    source_text: str,
    system: str,
    ttl_seconds: int = 900,
    provider: str = "openai",
    model_override: str = "",
    strict_provider: bool = False,
) -> str:
    key = (cache_key or "").strip()
    if not key or not source_text.strip():
        return ""
    now = time.time()
    stale = ""
    start_new = False
    with LOCK:
        cell = AI_SUMMARY_CACHE.get(key)
        if cell and (now - float(cell.get("ts", 0.0))) <= ttl_seconds:
            txt = str(cell.get("text") or "").strip()
            if txt:
                return txt
        if cell:
            stale = str(cell.get("text") or "").strip()
        running = bool(AI_SUMMARY_REFRESH.get(key, False))
        if not running:
            AI_SUMMARY_REFRESH[key] = True
            start_new = True

    def _worker() -> None:
        try:
            if _hybrid_ask_ai is None and _HybridAIEngine is None:
                return
            timeout_s = max(8, int(float(os.getenv("ONYX_AI_SUMMARY_TIMEOUT", "25").strip() or "25")))
            out = ""
            ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            try:
                pref = (provider or "openai").strip().lower()
                mo = (model_override or "").strip()
                # Prefer requested provider/model; fallback remains fast path.
                if _HybridAIEngine is not None:
                    use_requested = pref in {"openai", "ollama", "anthropic"}
                    use_openai_default = (not use_requested) and bool(os.getenv("OPENAI_API_KEY", "").strip())
                    if use_requested or use_openai_default:
                        req_provider = pref if use_requested else "openai"
                        req_model = mo or ("gpt-4o-mini" if req_provider == "openai" else "")
                        fut = ex.submit(
                            _ask_llm_with_provider,
                            source_text[:120000],
                            system,
                            req_provider,
                            req_model,
                            bool(strict_provider),
                        )
                        try:
                            val = fut.result(timeout=min(timeout_s, 20))
                            if isinstance(val, tuple) and len(val) >= 1:
                                out = str(val[0] or "").strip()
                        except Exception:
                            out = ""
                            try:
                                fut.cancel()
                            except Exception:
                                pass

                if not out and _HybridAIEngine is not None and os.getenv("OPENAI_API_KEY", "").strip():
                    # Legacy default path: strict OpenAI for reliability.
                    fut = ex.submit(
                        _ask_llm_with_provider,
                        source_text[:120000],
                        system,
                        "openai",
                        "gpt-4o-mini",
                        True,
                    )
                    try:
                        val = fut.result(timeout=min(timeout_s, 20))
                        if isinstance(val, tuple) and len(val) >= 1:
                            out = str(val[0] or "").strip()
                    except Exception:
                        out = ""
                        try:
                            fut.cancel()
                        except Exception:
                            pass

                if not out and _hybrid_ask_ai is not None:
                    # Dashboard PM blocks should stay responsive; use fast mode for quick synthesis.
                    fut = ex.submit(_hybrid_ask_ai, source_text[:120000], system, "fast", False)
                    out = str(fut.result(timeout=timeout_s) or "").strip()
            except Exception:
                out = "Insufficient evidence in provided inputs."
                try:
                    fut.cancel()  # type: ignore[name-defined]
                except Exception:
                    pass
            finally:
                try:
                    ex.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
            if not out:
                return
            with LOCK:
                AI_SUMMARY_CACHE[key] = {"ts": time.time(), "text": out}
        except Exception:
            pass
        finally:
            with LOCK:
                AI_SUMMARY_REFRESH[key] = False

    if start_new:
        th = threading.Thread(target=_worker, daemon=True)
        th.start()
    return stale


def _daily_brief_ai_summary(daily_path: str, briefing_lines: list[str]) -> str:
    sig = _file_sig(daily_path)
    if not sig or not briefing_lines:
        return ""
    src = "\n".join(f"- {ln}" for ln in briefing_lines[:12])
    system = (
        "You are a buy-side macro analyst. Use ONLY the provided daily brief lines. "
        "Do not add external facts, dates, or tickers. "
        "Return exactly 3 concise bullets labeled 1), 2), 3). "
        "If evidence is thin, write: Insufficient evidence in provided brief."
    )
    return _ai_cached_reliable_summary(f"brief:{sig}", src, system, ttl_seconds=1200)


def _risk_cards_ai_summary(source_rows: list[str], source_key: str) -> str:
    if not source_rows:
        return ""
    src = "\n".join(f"- {r}" for r in source_rows[:18])
    system = (
        "You are a risk officer. Use ONLY these provided risk lines from SEC-derived alerts and Yahoo market metrics. "
        "Do not introduce new claims. "
        "Return exactly: Headline: <one line> then 3 bullets labeled 1), 2), 3). "
        "If evidence is weak, say: Insufficient evidence in provided risk inputs."
    )
    return _ai_cached_reliable_summary(f"risk:{source_key}", src, system, ttl_seconds=900)


def _earnings_cards_ai_summary(source_rows: list[str], source_key: str) -> str:
    if not source_rows:
        return ""
    src = "\n".join(f"- {r}" for r in source_rows[:24])
    system = (
        "You are an earnings event analyst. Use ONLY provided earnings rows and card fields. "
        "Do not add external facts. "
        "Return exactly: Headline: <one line> then 3 bullets labeled 1), 2), 3). "
        "If evidence is weak, say: Insufficient evidence in provided earnings inputs."
    )
    return _ai_cached_reliable_summary(
        f"earn:{source_key}",
        src,
        system,
        ttl_seconds=21600,
        provider="ollama",
        model_override="gemma3:27b",
        strict_provider=True,
    )


def _pm_take_confidence_label(ai_text: str, source_count: int) -> str:
    txt = (ai_text or "").lower()
    if "insufficient evidence" in txt:
        return "low"
    if source_count >= 8:
        return "high"
    if source_count >= 4:
        return "medium"
    return "low"


def _extract_pm_take_line(ai_text: str) -> str:
    for raw in str(ai_text or "").splitlines():
        s = raw.strip()
        if not s:
            continue
        s = re.sub(r"^\d+\)\s*", "", s)
        s = re.sub(r"^-\s*", "", s)
        if s.lower().startswith("headline:"):
            s = s.split(":", 1)[1].strip()
        if s:
            return s
    return "No AI summary available."


def _first_n_intel_lines(ai_text: str, n: int = 3) -> list[str]:
    out: list[str] = []
    for raw in str(ai_text or "").splitlines():
        s = raw.strip()
        if not s:
            continue
        s = re.sub(r"^\d+\)\s*", "", s)
        s = re.sub(r"^[-*]\s*", "", s)
        if s.lower().startswith("headline:"):
            s = s.split(":", 1)[1].strip()
        if not s:
            continue
        out.append(s)
        if len(out) >= n:
            break
    return out


def _render_pm_take_block(title: str, ai_text: str, source_count: int, section_key: str) -> str:
    txt = (ai_text or "").strip()
    if not txt:
        return ""
    lead = _extract_pm_take_line(txt)
    conf = _pm_take_confidence_label(txt, source_count)
    sid = re.sub(r"[^a-z0-9_-]", "", (section_key or "pm").lower()) or "pm"
    return (
        "<div class='focus-box pm-box'>"
        f"<div class='focus-title'>{html.escape(title)}</div>"
        f"<div class='pm-line'>{html.escape(lead)}</div>"
        f"<div class='pm-meta'>confidence: {html.escape(conf)}</div>"
        f"<details class='pm-detail'><summary>Expand</summary>"
        f"<pre class='chart' id='pm-{html.escape(sid)}' style='white-space:pre-wrap;margin-top:6px;'>{html.escape(txt)}</pre>"
        "</details></div>"
    )


def _deep_dive_news_block(ticker: str, max_items: int = 10) -> tuple[str, int, list[str]]:
    t = resolve_ticker_input(ticker)
    if not t:
        return "", 0, []
    if yf is None:
        return "yfinance unavailable.", 0, []
    lines: list[str] = []
    headlines: list[str] = []
    try:
        tk = yf.Ticker(t)
        rows = getattr(tk, "news", None) or []
        for idx, row in enumerate(rows[:max_items], start=1):
            title = str(row.get("title") or "").strip()
            summary = str(row.get("summary") or row.get("content") or "").strip()
            pub = str(row.get("publisher") or "").strip()
            ts = row.get("providerPublishTime")
            d = ""
            try:
                if ts:
                    d = dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
            except Exception:
                d = ""
            if not title and not summary:
                continue
            if title:
                headlines.append(title)
            head = f"[{idx}] {title}" if title else f"[{idx}] Headline unavailable"
            if pub or d:
                head += f" ({pub}{', ' if pub and d else ''}{d})"
            lines.append(head)
            if summary:
                lines.append(f"Summary: {summary}")
            lines.append("")
    except Exception as e:
        return f"News fetch failed: {str(e)[:160]}", 0, []
    # Add broader web context to avoid stale/single-source narrative.
    web_lines: list[str] = []
    try:
        gg = _fetch_google_news_headlines(f"{t} stock AI subscription model coding", limit=6)
        if gg:
            web_lines.append("Google News (query-matched):")
            web_lines.extend(gg)
    except Exception:
        pass
    try:
        reu = _fetch_reuters_business_headlines(limit=4)
        if reu:
            web_lines.append("Reuters business headlines:")
            web_lines.extend(reu)
    except Exception:
        pass
    if web_lines:
        lines.append("")
        lines.append("Broader market context:")
        lines.extend(web_lines)

    text = "\n".join(lines).strip()
    return text, len([ln for ln in lines if ln.startswith("[")]), headlines[:5]


def _ollama_models() -> list[str]:
    url = "http://localhost:11434/api/tags"
    try:
        req = urllib.request.Request(url, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        out: list[str] = []
        for m in (payload.get("models") or []):
            name = str((m or {}).get("name") or "").strip().lower()
            if name:
                out.append(name)
        return out
    except Exception:
        return []


def _is_ollama_up(timeout: float = 2.0) -> bool:
    try:
        req = urllib.request.Request("http://localhost:11434/api/tags", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            _ = resp.read(64)
        return True
    except Exception:
        return False


def chat_health_snapshot() -> dict[str, object]:
    return {
        "ok": True,
        "api": "ok",
        "llm_engine_loaded": _hybrid_ask_ai is not None,
        "openai_key_set": bool(os.getenv("OPENAI_API_KEY", "").strip()),
        "anthropic_key_set": bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
        "ollama_up": _is_ollama_up(timeout=2.0),
        "ts": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _pick_deep_dive_model() -> str:
    models = _ollama_models()
    preferred = ["gemma3:27b", "gpt-oss:20b", "llama3:latest", "llama3", "llama3.1:latest", "llama3.1"]
    for p in preferred:
        if p.lower() in models:
            return p
    # Avoid hard-failing on missing llama3.1; use the most common local default.
    return "llama3:latest"


def _deep_dive_model_chain() -> list[str]:
    primary = _pick_deep_dive_model()
    chain: list[str] = [primary]
    # Fast fallback chain for timeout resilience.
    for m in ["gemma3:27b", "llama3:latest", "llama3", "gpt-oss:20b", "llama3.1:latest", "llama3.1"]:
        if m not in chain:
            chain.append(m)
    return chain


def _ollama_chat_once(messages: list[dict[str, str]], model: str, timeout: int = 90) -> str:
    url = "http://localhost:11434/api/chat"
    payload = {"model": model, "messages": messages, "stream": False}
    data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        obj = json.loads(resp.read().decode("utf-8", errors="ignore"))
    msg = obj.get("message") if isinstance(obj, dict) else {}
    content = str((msg or {}).get("content") or "").strip()
    if content:
        return content
    return "No model response returned."


def perform_deep_dive(ticker: str) -> dict[str, str | int]:
    t = resolve_ticker_input(ticker)
    if not t:
        return {"ok": 0, "ticker": "", "analysis": "Ticker is required.", "model": "-", "news_count": 0}
    if not _heavy_analysis_enabled():
        return {
            "ok": 0,
            "ticker": t,
            "analysis": "Deep dive analysis is disabled by configuration (ONYX_HEAVY_ANALYSIS_ON=0).",
            "model": "disabled",
            "news_count": 0,
            "headlines": [],
        }
    now = time.time()
    with LOCK:
        c = DEEP_DIVE_CACHE.get(t)
        if c and (now - float(c.get("ts", 0.0))) <= 900:
            cached = c.get("result")
            if isinstance(cached, dict):
                return dict(cached)
    news_text, news_count, top_headlines = _deep_dive_news_block(t, max_items=10)
    if news_count <= 0:
        result: dict[str, str | int] = {
            "ok": 0,
            "ticker": t,
            "analysis": "No recent Yahoo Finance news items found for deep dive.",
            "model": "-",
            "news_count": 0,
            "headlines": [],
        }
        with LOCK:
            DEEP_DIVE_CACHE[t] = {"ts": now, "result": dict(result)}
        return result
    system_prompt = (
        "You are a skeptical hedge fund analyst. "
        "Use only the provided news pack. Be specific and non-generic. "
        "Every risk must cite a concrete trigger from the pack (company/event/number/date). "
        "If evidence is weak, explicitly say 'insufficient direct evidence'. "
        "For SEC quantitative questions without structured_financial_data_json, say exactly: Data not available in structured filings."
    )
    user_prompt = (
        f"Ticker: {t}\n"
        "Policy marker: structured_financial_data_json\n"
        "Fallback marker: Data not available in structured filings.\n"
        "Analyze this recent news pack and return exactly this format:\n"
        "Sentiment: <Bullish/Bearish>\n"
        "Top 3 Risks:\n"
        "1) <Risk> | Trigger: <specific phrase/event>\n"
        "2) <Risk> | Trigger: <specific phrase/event>\n"
        "3) <Risk> | Trigger: <specific phrase/event>\n"
        "Top 2 Upside Catalysts:\n"
        "1) <Catalyst> | Trigger: <specific phrase/event>\n"
        "2) <Catalyst> | Trigger: <specific phrase/event>\n"
        "Verdict: <one sentence with explicit condition to be wrong>\n\n"
        f"News:\n{news_text[:120000]}"
    )
    messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
    errs: list[str] = []
    used_model = "-"
    for idx, model in enumerate(_deep_dive_model_chain()):
        try:
            # First model gets a bit longer; fallback models stay fast.
            timeout_s = 90 if idx == 0 else 45
            analysis = _ollama_chat_once(messages, model=model, timeout=timeout_s)
            result = {
                "ok": 1,
                "ticker": t,
                "analysis": analysis,
                "model": model,
                "news_count": news_count,
                "headlines": top_headlines,
            }
            with LOCK:
                DEEP_DIVE_CACHE[t] = {"ts": now, "result": dict(result)}
            return result
        except Exception as e:
            used_model = model
            errs.append(f"{model}: {str(e)[:120]}")
            continue

    # Do not poison cache for long with failures; keep short-lived warning only.
    result = {
        "ok": 0,
        "ticker": t,
        "analysis": f"Deep dive AI failed after retries: {' | '.join(errs[:3])}",
        "model": used_model or "-",
        "news_count": news_count,
        "headlines": top_headlines,
    }
    with LOCK:
        DEEP_DIVE_CACHE[t] = {"ts": now - 840, "result": dict(result)}
    return result


def _sec_diff_keyword_score(text: str, change_type: str) -> tuple[int, str]:
    low = (text or "").lower()
    hits: list[str] = []
    score = 2 if change_type == "modified" else 3
    rules: list[tuple[str, int, str]] = [
        ("material weakness", 4, "internal controls risk"),
        ("impairment", 3, "asset quality pressure"),
        ("goodwill", 2, "acquisition/value risk"),
        ("litigation", 3, "legal/regulatory risk"),
        ("investigation", 3, "regulatory overhang"),
        ("subpoena", 3, "regulatory overhang"),
        ("covenant", 4, "balance-sheet constraint"),
        ("liquidity", 3, "funding/liquidity risk"),
        ("debt", 2, "leverage risk"),
        ("default", 5, "credit event risk"),
        ("cyber", 3, "cybersecurity risk"),
        ("breach", 4, "security incident risk"),
        ("supply chain", 2, "execution risk"),
        ("restructuring", 2, "operational stress"),
        ("tariff", 2, "policy/cost pressure"),
    ]
    for kw, pts, label in rules:
        if kw in low:
            score += pts
            hits.append(label)
    score = max(1, min(10, score))
    why = ", ".join(sorted(set(hits))) if hits else "new/changed risk language in Item 1A"
    return score, why


def _is_table_noise_text(text: str) -> bool:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if not s:
        return True
    low = s.lower()
    if len(s) > 650:
        return True
    table_hits = 0
    for kw in (
        "fiscal year ended",
        "% of total revenues",
        "table of contents",
        "in millions",
        "as of january",
        "subscription and support",
        "cost of revenues",
        "total operating expenses",
    ):
        if kw in low:
            table_hits += 1
    nums = len(re.findall(r"\$?\d[\d,]*(?:\.\d+)?", s))
    pcts = len(re.findall(r"\d+\s*%", s))
    if table_hits >= 2:
        return True
    if nums >= 10 and pcts >= 3:
        return True
    return False


def _compact_signal_text(text: str, max_chars: int = 220) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if not s:
        return ""
    # Prefer first sentence-like boundary.
    parts = re.split(r"(?<=[\.\!\?;])\s+", s)
    if parts and parts[0]:
        s = parts[0].strip()
    if len(s) > max_chars:
        s = s[: max_chars - 1].rstrip() + "…"
    return s


def _clean_ranked_changes(rows: list[dict[str, object]], top_n: int = 3) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    cleaned: list[dict[str, object]] = []
    for r in (rows or []):
        txt = str(r.get("risk_text") or "").strip()
        if not txt or _is_table_noise_text(txt):
            continue
        c = dict(r)
        c["risk_text"] = _compact_signal_text(txt, max_chars=240)
        why = _compact_signal_text(str(c.get("why_it_matters") or ""), max_chars=120)
        c["why_it_matters"] = why
        cleaned.append(c)
    cleaned.sort(key=lambda x: _to_int(x.get("severity_score"), 0), reverse=True)
    return cleaned, cleaned[:top_n]


def _sec_diff_structured_fallback(diff_list: list[dict[str, str]]) -> dict[str, object]:
    ranked: list[dict[str, object]] = []
    for row in diff_list[:180]:
        txt = re.sub(r"\s+", " ", str(row.get("text") or "")).strip()
        if not txt:
            continue
        ctype = str(row.get("type") or "added").strip().lower()
        if ctype not in {"added", "modified"}:
            ctype = "added"
        sev, why = _sec_diff_keyword_score(txt, ctype)
        ranked.append(
            {
                "severity_score": sev,
                "change_type": ctype,
                "risk_text": txt,
                "why_it_matters": why,
            }
        )
    ranked.sort(key=lambda x: _to_int(x.get("severity_score"), 0), reverse=True)
    top = ranked[:3]
    lines = ["Top Material Risk Increases (Fallback Ranking):", ""]
    for i, row in enumerate(top, 1):
        lines.append(
            f"{i}. [Severity {_to_int(row.get('severity_score'), 0)}/10] "
            f"({row.get('change_type')}) {row.get('risk_text')}"
        )
        lines.append(f"   Why it matters: {row.get('why_it_matters')}")
    if len(ranked) > 3:
        lines.append("")
        lines.append(f"Additional scored risks detected: {len(ranked) - 3}")
    return {
        "ranked_changes": ranked,
        "top_3": top,
        "additional_count": max(0, len(ranked) - 3),
        "report_text": "\n".join(lines).strip(),
    }


def _sec_file_fingerprint(path: Path) -> str:
    try:
        st = path.stat()
        return f"{path.name}:{int(st.st_mtime)}:{int(st.st_size)}"
    except Exception:
        return path.name


def _sec_filing_pair_hash(ticker: str, files: list[Path]) -> str:
    t = (ticker or "").strip().upper()
    a = _sec_file_fingerprint(files[0]) if len(files) > 0 else "-"
    b = _sec_file_fingerprint(files[1]) if len(files) > 1 else "-"
    raw = f"{t}|{a}|{b}"
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def perform_sec_risk_diff(ticker: str) -> dict[str, object]:
    t = resolve_ticker_input(ticker)
    if not t:
        return {"ok": False, "error": "ticker_required"}
    if not _heavy_analysis_enabled():
        return {"ok": False, "ticker": t, "error": "disabled", "detail": "SEC risk diff is disabled by configuration."}
    if _SECFilingAnalyzer is None:
        return {
            "ok": False,
            "ticker": t,
            "error": "sec_filing_analyzer_unavailable",
            "detail": "modules/sec_filing_analyzer.py or dependencies are not available.",
        }

    try:
        now = time.time()
        analyzer = _SECFilingAnalyzer(download_dir=str(DATA / "sec_filings"))
        files = analyzer.fetch_latest_10ks(t)
        if len(files) < 2:
            return {"ok": False, "ticker": t, "error": "insufficient_filings", "detail": "Need at least 2 latest 10-K filings."}

        pair_hash = _sec_filing_pair_hash(t, files)
        ck = f"{t}:{pair_hash}:mda_v2"
        with LOCK:
            c = SEC_RISK_DIFF_CACHE.get(ck)
            if c:
                res = c.get("result")
                if isinstance(res, dict):
                    return dict(res)

        new_path = str(files[0])
        old_path = str(files[1])
        new_html = analyzer.load_filing_text(files[0])
        old_html = analyzer.load_filing_text(files[1])
        new_risk = analyzer.extract_risk_factors(new_html)
        old_risk = analyzer.extract_risk_factors(old_html)
        diff_list = analyzer.calculate_sentence_diff(old_risk, new_risk)

        # Keep request responsive: bound LLM runtime and fallback to deterministic ranking.
        ai_input = diff_list[:120]
        structured: dict[str, object]
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(analyzer.analyze_diff_with_llama_structured, ai_input)
            structured = fut.result(timeout=6)
        except Exception:
            try:
                fut.cancel()
            except Exception:
                pass
            structured = _sec_diff_structured_fallback(ai_input)
        finally:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

        result: dict[str, object] = {
            "ok": True,
            "ticker": t,
            "pair_hash": pair_hash,
            "last_run": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "filings": {"new": new_path, "old": old_path},
            "diff_count": len(diff_list),
            "ranked_changes": list(structured.get("ranked_changes") or []),
            "top_3": list(structured.get("top_3") or []),
            "additional_count": _to_int(structured.get("additional_count"), 0),
            "report_text": str(structured.get("report_text") or "").strip(),
        }
        with LOCK:
            SEC_RISK_DIFF_CACHE[ck] = {"ts": now, "result": dict(result)}
        return result
    except _SECExtractionError as e:
        return {"ok": False, "ticker": t, "error": "extraction_failed", "detail": str(e)[:260]}
    except Exception as e:
        return {"ok": False, "ticker": t, "error": "sec_risk_diff_failed", "detail": str(e)[:260]}


def sec_risk_html(ticker: str) -> str:
    t = resolve_ticker_input(ticker)
    if not t:
        return dashboard_html("Ticker is required for SEC Risk Diff.")
    res = perform_sec_risk_diff(t)
    if not bool(res.get("ok")):
        err = str(res.get("detail") or res.get("error") or "unknown_error")
        return (
            "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<style>body{margin:0;background:#0b1014;color:#e7eef6;font-family:'Avenir Next','Helvetica Neue',sans-serif;}"
            ".wrap{max-width:980px;margin:0 auto;padding:18px;} .card{background:#111a22;border:1px solid #2a3f50;border-radius:12px;padding:14px;}"
            "a{color:#9fd3ff;}</style></head><body><div class='wrap'>"
            f"<div class='card'><h1>SEC Risk Diff: {html.escape(t)}</h1><a href='/universe?tab=all'>Back My Companies</a> | <a href='/'>Back Dashboard</a>"
            f"<p style='margin-top:10px;color:#ffb6bf;'>Failed: {html.escape(err)}</p></div></div></body></html>"
        )

    top = res.get("top_3") if isinstance(res.get("top_3"), list) else []
    lis = "".join(
        "<li>"
        f"<strong>Severity {_to_int((x or {}).get('severity_score'), 0)}/10</strong> "
        f"({html.escape(str((x or {}).get('change_type') or '-'))}) "
        f"{html.escape(str((x or {}).get('risk_text') or ''))}"
        + (
            f"<br><span style='color:#9ab0c0;'>Why: {html.escape(str((x or {}).get('why_it_matters') or ''))}</span>"
            if str((x or {}).get("why_it_matters") or "").strip()
            else ""
        )
        + "</li>"
        for x in top[:3]
    ) or "<li>No ranked items returned.</li>"
    report_text = html.escape(str(res.get("report_text") or ""))
    diff_count = _to_int(res.get("diff_count"), 0)
    extra = _to_int(res.get("additional_count"), 0)
    last_run = str(res.get("last_run") or "-")
    return (
        "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<style>body{margin:0;background:#0b1014;color:#e7eef6;font-family:'Avenir Next','Helvetica Neue',sans-serif;}"
        ".wrap{max-width:980px;margin:0 auto;padding:18px;} .card{background:#111a22;border:1px solid #2a3f50;border-radius:12px;padding:14px;margin-bottom:10px;}"
        ".muted{color:#9ab0c0;} .btn{display:inline-block;border:1px solid #2e5c7b;border-radius:8px;padding:6px 10px;background:#1a3d56;color:#e7eef6;text-decoration:none;}"
        "pre{white-space:pre-wrap;background:#0a131b;border:1px solid #203647;border-radius:8px;padding:10px;} li{margin:8px 0;}</style></head><body><div class='wrap'>"
        f"<div class='card'><h1>SEC Risk Diff: {html.escape(t)}</h1>"
        f"<div class='muted'>Diff sentences: {diff_count} | Additional scored risks: {extra} | Last run: {html.escape(last_run)}</div>"
        f"<div style='margin-top:8px;'><a class='btn' href='/universe?tab=all'>Back My Companies</a> <a class='btn' href='/'>Back Dashboard</a></div></div>"
        f"<div class='card'><h2>Top 3 Material Risks</h2><ul>{lis}</ul></div>"
        f"<div class='card'><h2>Full AI Report</h2><pre>{report_text}</pre></div>"
        "</div></body></html>"
    )


def perform_mda_diff(ticker: str) -> dict[str, object]:
    t = resolve_ticker_input(ticker)
    if not t:
        return {"ok": False, "error": "ticker_required"}
    if not _heavy_analysis_enabled():
        return {"ok": False, "ticker": t, "error": "disabled", "detail": "MD&A diff is disabled by configuration."}
    if _SECMDAAnalyzer is None:
        return {
            "ok": False,
            "ticker": t,
            "error": "sec_mda_analyzer_unavailable",
            "detail": "modules/sec_mda_analyzer.py is not available.",
        }
    try:
        now = time.time()
        analyzer = _SECMDAAnalyzer(download_dir=str(DATA / "sec_filings"))
        files = analyzer.fetch_latest_10ks(t)
        if len(files) < 2:
            return {"ok": False, "ticker": t, "error": "insufficient_filings", "detail": "Need at least 2 latest 10-K filings."}
        pair_hash = _sec_filing_pair_hash(t, files)
        ck = f"{t}:{pair_hash}"
        with LOCK:
            c = MDA_DIFF_CACHE.get(ck)
            if c:
                res = c.get("result")
                if isinstance(res, dict):
                    return dict(res)
        new_html = analyzer.load_filing_text(files[0])
        old_html = analyzer.load_filing_text(files[1])
        new_mda = analyzer.extract_mda(new_html)
        old_mda = analyzer.extract_mda(old_html)
        diff_list = analyzer.calculate_sentence_diff(old_mda, new_mda)
        ai_input = diff_list[:140]
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(analyzer.analyze_diff_with_llama_structured, ai_input)
            structured = fut.result(timeout=18)
        except Exception:
            try:
                fut.cancel()
            except Exception:
                pass
            structured = _sec_diff_structured_fallback(ai_input)
        finally:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        ranked_clean, top_clean = _clean_ranked_changes(list(structured.get("ranked_changes") or []), top_n=3)
        result: dict[str, object] = {
            "ok": True,
            "ticker": t,
            "pair_hash": pair_hash,
            "last_run": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "filings": {"new": str(files[0]), "old": str(files[1])},
            "diff_count": len(diff_list),
            "ranked_changes": ranked_clean,
            "top_3": top_clean,
            "additional_count": max(0, len(ranked_clean) - len(top_clean)),
            "report_text": str(structured.get("report_text") or "").strip(),
        }
        with LOCK:
            MDA_DIFF_CACHE[ck] = {"ts": now, "result": dict(result)}
        return result
    except _SECExtractionError as e:
        return {"ok": False, "ticker": t, "error": "extraction_failed", "detail": str(e)[:260]}
    except Exception as e:
        return {"ok": False, "ticker": t, "error": "mda_diff_failed", "detail": str(e)[:260]}


def mda_diff_html(ticker: str) -> str:
    t = resolve_ticker_input(ticker)
    if not t:
        return dashboard_html("Ticker is required for MD&A Diff.")
    res = perform_mda_diff(t)
    if not bool(res.get("ok")):
        err = str(res.get("detail") or res.get("error") or "unknown_error")
        return (
            "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<style>body{margin:0;background:#0b1014;color:#e7eef6;font-family:'Avenir Next','Helvetica Neue',sans-serif;}"
            ".wrap{max-width:980px;margin:0 auto;padding:18px;} .card{background:#111a22;border:1px solid #2a3f50;border-radius:12px;padding:14px;}"
            "a{color:#9fd3ff;}</style></head><body><div class='wrap'>"
            f"<div class='card'><h1>MD&A Intelligence: {html.escape(t)}</h1><a href='/universe?tab=all'>Back My Companies</a> | <a href='/'>Back Dashboard</a>"
            f"<p style='margin-top:10px;color:#ffb6bf;'>Failed: {html.escape(err)}</p></div></div></body></html>"
        )
    top = res.get("top_3") if isinstance(res.get("top_3"), list) else []
    lis = "".join(
        "<article class='sig-card'>"
        f"<div class='sig-head'><span class='sev'>S{_to_int((x or {}).get('severity_score'), 0)}</span>"
        f"<span class='typ'>{html.escape(str((x or {}).get('change_type') or '-'))}</span></div>"
        f"<div class='sig-txt'>{html.escape(_compact_signal_text(str((x or {}).get('risk_text') or ''), max_chars=260))}</div>"
        + (
            f"<div class='sig-why'>Why: {html.escape(_compact_signal_text(str((x or {}).get('why_it_matters') or ''), max_chars=140))}</div>"
            if str((x or {}).get("why_it_matters") or "").strip()
            else ""
        )
        + "</article>"
        for x in top[:3]
    ) or "<div class='muted'>No ranked items returned.</div>"
    report_text = html.escape(str(res.get("report_text") or ""))
    diff_count = _to_int(res.get("diff_count"), 0)
    extra = _to_int(res.get("additional_count"), 0)
    last_run = str(res.get("last_run") or "-")
    return (
        "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<style>body{margin:0;background:#0b1014;color:#e7eef6;font-family:'Avenir Next','Helvetica Neue',sans-serif;}"
        ".wrap{max-width:1080px;margin:0 auto;padding:18px;} .card{background:#111a22;border:1px solid #2a3f50;border-radius:12px;padding:14px;margin-bottom:10px;}"
        ".muted{color:#9ab0c0;} .btn{display:inline-block;border:1px solid #2e5c7b;border-radius:8px;padding:6px 10px;background:#1a3d56;color:#e7eef6;text-decoration:none;}"
        ".kpis{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:8px;}"
        ".kpi{border:1px solid #2a4659;background:#0f1a23;border-radius:10px;padding:10px;}.kpi b{display:block;font-size:18px;}"
        ".sig-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;}"
        ".sig-card{border:1px solid #2a4a61;background:#0f1c27;border-radius:10px;padding:10px;}"
        ".sig-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;}"
        ".sev{display:inline-block;border:1px solid #3f6a86;background:#133248;border-radius:999px;padding:2px 8px;font-weight:700;font-size:11px;}"
        ".typ{font-size:11px;color:#9ec6e0;}"
        ".sig-txt{font-size:13px;line-height:1.35;color:#e8f1f9;}"
        ".sig-why{margin-top:6px;font-size:12px;color:#a8c1d3;}"
        "pre{white-space:pre-wrap;background:#0a131b;border:1px solid #203647;border-radius:8px;padding:10px;}"
        "@media(max-width:980px){.sig-grid{grid-template-columns:1fr;} .kpis{grid-template-columns:1fr;}}</style></head><body><div class='wrap'>"
        f"<div class='card'><h1>MD&A Intelligence: {html.escape(t)}</h1>"
        f"<div class='muted'>Institutional summary view (table-noise filtered)</div>"
        f"<div class='kpis'><div class='kpi'><span class='muted'>Diff Sentences</span><b>{diff_count}</b></div><div class='kpi'><span class='muted'>Material Signals</span><b>{len(top[:3])}</b></div><div class='kpi'><span class='muted'>Additional</span><b>{extra}</b></div></div>"
        f"<div class='muted' style='margin-top:6px;'>Last run: {html.escape(last_run)}</div>"
        f"<div style='margin-top:8px;'><a class='btn' href='/universe?tab=all'>Back My Companies</a> <a class='btn' href='/'>Back Dashboard</a></div></div>"
        f"<div class='card'><h2>Top 3 MD&A Signals</h2><div class='sig-grid'>{lis}</div></div>"
        f"<div class='card'><details><summary>Full AI Report</summary><pre>{report_text}</pre></details></div>"
        "</div></body></html>"
    )


def _mda_sections_from_result(res: dict[str, object]) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {
        "what_changed": [],
        "revenue": [],
        "margin": [],
        "competition": [],
        "guidance": [],
        "accountability": [],
        "earnings_quality": [],
        "capital_allocation": [],
        "clarity": [],
    }
    rows = res.get("ranked_changes") if isinstance(res.get("ranked_changes"), list) else []
    if not rows and isinstance(res.get("top_3"), list):
        rows = res.get("top_3")
    for row in rows[:24]:
        x = row if isinstance(row, dict) else {}
        trig = re.sub(r"\s+", " ", str(x.get("risk_text") or "")).strip()
        why = re.sub(r"\s+", " ", str(x.get("why_it_matters") or "")).strip()
        ctype = str(x.get("change_type") or "").strip()
        if not trig:
            continue
        line = f"{trig}" + (f" — {why}" if why else "")
        low = f"{trig} {why} {ctype}".lower()

        sections["what_changed"].append(line)
        if any(k in low for k in ("revenue", "sales", "demand", "volume", "pricing")):
            sections["revenue"].append(line)
        if any(k in low for k in ("margin", "cost", "gross", "operating", "tailwind", "headwind")):
            sections["margin"].append(line)
        if any(k in low for k in ("competition", "competitor", "market share", "pricing power", "moat")):
            sections["competition"].append(line)
        if any(k in low for k in ("guidance", "outlook", "forecast", "raise", "cut")):
            sections["guidance"].append(line)
        if any(k in low for k in ("accountability", "mistake", "we made", "weather", "currency", "macro headwind", "blame")):
            sections["accountability"].append(line)
        if any(k in low for k in ("adjusted ebitda", "non-gaap", "gaap", "free cash flow", "owner earnings")):
            sections["earnings_quality"].append(line)
        if any(k in low for k in ("buyback", "repurchase", "dilution", "capital", "acquisition", "roi", "return on")):
            sections["capital_allocation"].append(line)
        if any(k in low for k in ("synergies", "ecosystem", "optimization", "paradigm", "strategic initiative", "clarity")):
            sections["clarity"].append(line)

    # Deduplicate and cap each section.
    out: dict[str, list[str]] = {}
    for k, vals in sections.items():
        seen: set[str] = set()
        cleaned: list[str] = []
        for v in vals:
            vv = re.sub(r"\s+", " ", str(v or "")).strip()
            if not vv:
                continue
            kk = vv.lower()
            if kk in seen:
                continue
            seen.add(kk)
            cleaned.append(vv)
            if len(cleaned) >= 3:
                break
        out[k] = cleaned
    return out


def _mda_overview_card_html(ticker: str) -> str:
    res = perform_mda_diff(ticker)
    if not bool(res.get("ok")):
        err = html.escape(str(res.get("detail") or res.get("error") or "mda_unavailable"))
        return (
            "<section class='card c12'>"
            "<h2>MD&A Intelligence</h2>"
            f"<div class='muted'>Failed to load MD&A analysis: {err}. "
            f"<a class='btn' href='/mda_diff?t={html.escape(ticker)}'>Open MD&A Diff</a></div>"
            "</section>"
        )

    sections = _mda_sections_from_result(res)
    last_run = html.escape(str(res.get("last_run") or "-"))
    diff_count = _to_int(res.get("diff_count"), 0)

    def _lis(key: str, empty: str) -> str:
        rows = sections.get(key) or []
        if not rows:
            return f"<li class='muted'>{html.escape(empty)}</li>"
        return "".join(f"<li>{html.escape(x)}</li>" for x in rows)

    return (
        "<section class='card c12'>"
        "<h2>MD&A Intelligence</h2>"
        f"<div class='muted'>Diff sentences: {diff_count} | Last run: {last_run} | Buffett lens: accountability, earnings quality, capital allocation, competition, guidance, clarity.</div>"
        "<div class='mda-grid'>"
        f"<div class='mda-box'><h3>What Changed</h3><ul>{_lis('what_changed', 'No material MD&A changes detected.')}</ul></div>"
        f"<div class='mda-box'><h3>Why Revenue Moved</h3><ul>{_lis('revenue', 'No explicit revenue driver extracted.')}</ul></div>"
        f"<div class='mda-box'><h3>Margin Pressure / Tailwinds</h3><ul>{_lis('margin', 'No clear margin signal extracted.')}</ul></div>"
        f"<div class='mda-box'><h3>Competition Mentions</h3><ul>{_lis('competition', 'No explicit competition wording detected.')}</ul></div>"
        f"<div class='mda-box'><h3>Management Guidance Shift</h3><ul>{_lis('guidance', 'No guidance shift detected in changed text.')}</ul></div>"
        f"<div class='mda-box'><h3>Accountability Check</h3><ul>{_lis('accountability', 'No clear ownership-vs-blame signal found.')}</ul></div>"
        f"<div class='mda-box'><h3>Earnings Quality (GAAP vs Adjusted)</h3><ul>{_lis('earnings_quality', 'No explicit GAAP/Adjusted/FCF quality cue found.')}</ul></div>"
        f"<div class='mda-box'><h3>Capital Allocation Signals</h3><ul>{_lis('capital_allocation', 'No clear buyback/dilution/ROI signal found.')}</ul></div>"
        f"<div class='mda-box'><h3>Clarity vs Jargon</h3><ul>{_lis('clarity', 'No obvious jargon-heavy update detected.')}</ul></div>"
        "</div>"
        f"<div class='actions'><a class='btn' href='/mda_diff?t={html.escape(ticker)}'>Open Full MD&A Diff</a></div>"
        "</section>"
    )


def perform_10k_full_lens(ticker: str, force: bool = False, cached_only: bool = False) -> dict[str, object]:
    t = resolve_ticker_input(ticker)
    if not t:
        return {"ok": False, "error": "ticker_required"}
    if _SECFilingAnalyzer is None:
        return {
            "ok": False,
            "ticker": t,
            "error": "sec_filing_analyzer_unavailable",
            "detail": "modules/sec_filing_analyzer.py is not available.",
        }
    try:
        # Fast path for UI rendering: do not fetch/download, return cache-only view.
        if cached_only and not force:
            with LOCK:
                keys = [
                    k
                    for k in TENK_LENS_CACHE.keys()
                    if str(k).startswith(f"{t}:") and str(k).endswith(":v2")
                ]
                if keys:
                    # Return newest cached result for this ticker.
                    k = sorted(keys, key=lambda x: float(TENK_LENS_CACHE.get(x, {}).get("ts", 0.0)), reverse=True)[0]
                    c = TENK_LENS_CACHE.get(k, {})
                    if isinstance(c.get("result"), dict):
                        return dict(c["result"])
            return {
                "ok": False,
                "ticker": t,
                "error": "not_cached",
                "detail": "Run 10-K Lens once to generate section intelligence.",
            }

        analyzer = _SECFilingAnalyzer(download_dir=str(DATA / "sec_filings"))

        # Prefer local filing DB paths first to keep UI snappy and avoid network fetch delays.
        files: list[Path] = []
        try:
            conn = research_db()
            rows = conn.execute(
                "SELECT path FROM filings WHERE ticker = ? AND form = '10-K' ORDER BY date DESC LIMIT 2",
                (t,),
            ).fetchall()
            conn.close()
            for r in rows:
                p = Path(str(r["path"] or ""))
                if not p.is_absolute():
                    p = ROOT / p
                if p.exists() and p.is_file():
                    files.append(p)
        except Exception:
            files = []
        if len(files) < 2:
            try:
                files = analyzer.fetch_latest_10ks(t)
            except Exception:
                files = []

        pair_hash = _sec_filing_pair_hash(t, files) if len(files) >= 2 else f"{t}:latest"
        ck = f"{t}:{pair_hash}:v2"
        if not force:
            with LOCK:
                c = TENK_LENS_CACHE.get(ck)
                if c and isinstance(c.get("result"), dict):
                    return dict(c["result"])
            if cached_only:
                return {
                    "ok": False,
                    "ticker": t,
                    "error": "not_cached",
                    "detail": "Run 10-K Lens once to generate section intelligence.",
                }

        def _build_segments_from_report(report: dict[str, object]) -> dict[str, dict[str, str]]:
            biz = dict(report.get("business") or {})
            mda = dict(report.get("mda_moat") or {})
            rf = dict(report.get("risk_factors") or {})
            fin = dict(report.get("financial_notes") or {})
            gov = dict(report.get("proxy_governance") or {})

            top3 = list(rf.get("top_3") or [])
            top_line = "No material incremental risk extracted."
            if top3:
                r0 = dict(top3[0] or {})
                sev = r0.get("severity_score")
                txt = str(r0.get("risk_text") or "").strip()
                top_line = f"Severity {sev}/10: {txt}" if txt else f"Severity {sev}/10 risk change detected."

            concentration = str(fin.get("concentration") or "").strip()
            debt_risk = str(fin.get("debt_risk") or "").strip()
            revenue_flag = str(fin.get("revenue_flag") or "").strip()
            evidence = str(fin.get("evidence") or "").strip()

            incentive = str(gov.get("incentive_metric") or "").strip()
            red_flags = str(gov.get("red_flags") or "").strip()
            align_score = gov.get("alignment_score")

            return {
                "business": {
                    "what_it_does": str(biz.get("summary") or ""),
                    "customers_value": str(biz.get("customers") or ""),
                    "product_mix": str(biz.get("summary") or ""),
                    "competition_status": str(biz.get("moat_source") or ""),
                    "management_signal": "Business model and moat narrative refreshed from latest filing.",
                    "green_flag": "Core model, customer base, and moat source extracted.",
                    "red_flag": "If language is generic, confirm against full Item 1 text.",
                },
                "mda": {
                    "why_revenue_up_down": str(mda.get("revenue_driver") or ""),
                    "margin_drivers": str(mda.get("margin_driver") or ""),
                    "guidance_shift": str(mda.get("guidance_shift") or ""),
                    "accountability": "Management accountability requires explicit wording review in full MD&A.",
                    "green_flag": "Revenue/margin/guidance fields extracted.",
                    "red_flag": "If blank, re-run after fresh filing sync.",
                },
                "risk": {
                    "top_exposure": top_line,
                    "concentration_risk": concentration or "No concentration risk extracted.",
                    "dependency_risk": debt_risk or "No explicit dependency/debt distress extracted.",
                    "policy_regulatory_risk": "Check top risk triggers for legal/regulatory mentions.",
                    "green_flag": "Severity-ranked risk deltas available.",
                    "red_flag": "High severity score requires immediate thesis re-check.",
                },
                "financials": {
                    "income_statement_signal": f"Revenue recognition: {revenue_flag or 'N/A'}",
                    "balance_sheet_signal": f"Debt risk: {debt_risk or 'N/A'}",
                    "cashflow_signal": evidence or "Forensic evidence not provided.",
                    "working_capital_signal": concentration or "Working-capital concentration not explicit.",
                    "green_flag": "Forensic accounting checks completed.",
                    "red_flag": "High debt/concentration flags can invalidate equity case.",
                },
                "notes": {
                    "debt_terms_covenants": debt_risk or "Debt/covenant risk not explicit.",
                    "revenue_recognition_detail": revenue_flag or "Revenue recognition classification unavailable.",
                    "customer_supplier_concentration": concentration or "No concentration disclosed in extracted notes.",
                    "legal_contingency": evidence or "No specific legal contingency extracted.",
                    "green_flag": "Notes were scanned for accounting/debt/concentration cues.",
                    "red_flag": "Always confirm with full Note disclosures in filing.",
                },
                "proxy": {
                    "ownership_alignment": f"Alignment score: {align_score if align_score is not None else 'N/A'}/10",
                    "incentive_design": incentive or "Incentive metric not extracted.",
                    "vesting_horizon": "Check full DEF 14A grants table for vesting horizon details.",
                    "capital_behavior_risk": red_flags or "No obvious governance red flag extracted.",
                    "green_flag": "Governance and incentive extraction completed.",
                    "red_flag": "EPS-heavy incentives may encourage financial engineering over quality.",
                },
            }

        def _quick_fallback_result() -> dict[str, object] | None:
            if len(files) < 1:
                return None
            try:
                latest_html = analyzer.load_filing_text(files[0])
                latest_text = analyzer.parser.clean_html(latest_html)
                biz = analyzer.parser.extract_section(latest_text, "BUSINESS", max_chars=26000) or ""
                mda = analyzer.parser.extract_section(latest_text, "MD&A", max_chars=26000) or ""
                risk = analyzer.parser.extract_section(latest_text, "RISK", max_chars=26000) or ""
                notes = analyzer.parser.extract_section(latest_text, "NOTES", max_chars=26000) or ""
                # Upgrade low-quality slices with robust local extractors (still deterministic).
                if len(biz) < 500:
                    try:
                        biz = analyzer.extract_business_section(latest_html)
                    except Exception:
                        pass
                if len(mda) < 500:
                    try:
                        mda = analyzer._extract_item7_mda(latest_html)
                    except Exception:
                        pass
                if len(risk) < 500:
                    try:
                        risk = analyzer.extract_risk_factors(latest_html)
                    except Exception:
                        pass
                if len(notes) < 700:
                    try:
                        notes = analyzer.extract_financial_notes(latest_html)
                    except Exception:
                        pass
                sections = {
                    "business": biz,
                    "mda": mda,
                    "risk": risk,
                    "financials": notes,
                    "notes": notes,
                }
                proxy_text = ""
                try:
                    conn = research_db()
                    prow = conn.execute(
                        "SELECT path FROM filings WHERE ticker = ? AND form IN ('DEF 14A','DEF14A') ORDER BY date DESC LIMIT 1",
                        (t,),
                    ).fetchone()
                    conn.close()
                    if prow:
                        pp = Path(str(prow["path"] or ""))
                        if not pp.is_absolute():
                            pp = ROOT / pp
                        if pp.exists() and pp.is_file():
                            proxy_text = analyzer.load_filing_text(pp)
                except Exception:
                    proxy_text = ""
                segs = _tenk_lens_fallback_sections(sections, proxy_text)
                return {
                    "ok": True,
                    "ticker": t,
                    "pair_hash": pair_hash,
                    "last_run": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "source": "fallback_quick",
                    "filings": {
                        "new": str(files[0]) if files else "",
                        "old": str(files[1]) if len(files) > 1 else "",
                        "proxy": "",
                    },
                    "segments": segs,
                    "raw_report": {},
                }
            except Exception:
                return None

        # Force refresh is served immediately from quick fallback, then upgraded async.
        if force:
            fb = _quick_fallback_result()
            if fb:
                with LOCK:
                    TENK_LENS_CACHE[ck] = {"ts": time.time(), "result": dict(fb)}
                    start_bg = ck not in TENK_LENS_INFLIGHT
                    if start_bg:
                        TENK_LENS_INFLIGHT.add(ck)
                if start_bg:
                    def _bg_refresh() -> None:
                        try:
                            report_bg = analyzer.run_full_deep_dive(t)
                            segs_bg = _build_segments_from_report(report_bg if isinstance(report_bg, dict) else {})
                            filings_bg = dict((report_bg or {}).get("files") or {})
                            result_bg: dict[str, object] = {
                                "ok": True,
                                "ticker": t,
                                "pair_hash": pair_hash,
                                "last_run": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "source": "full_ai",
                                "filings": {
                                    "new": str(filings_bg.get("latest_10k") or (str(files[0]) if files else "")),
                                    "old": str(filings_bg.get("prior_10k") or (str(files[1]) if len(files) > 1 else "")),
                                    "proxy": str(filings_bg.get("latest_proxy") or ""),
                                },
                                "segments": segs_bg,
                                "raw_report": report_bg if isinstance(report_bg, dict) else {},
                            }
                            with LOCK:
                                TENK_LENS_CACHE[ck] = {"ts": time.time(), "result": dict(result_bg)}
                        except Exception:
                            pass
                        finally:
                            with LOCK:
                                TENK_LENS_INFLIGHT.discard(ck)
                    threading.Thread(target=_bg_refresh, daemon=True).start()
                return fb

        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(analyzer.run_full_deep_dive, t)
            report = fut.result(timeout=8)
        except Exception:
            try:
                fut.cancel()
            except Exception:
                pass
            fb = _quick_fallback_result()
            if fb:
                with LOCK:
                    TENK_LENS_CACHE[ck] = {"ts": time.time(), "result": dict(fb)}
                return fb
            return {
                "ok": False,
                "ticker": t,
                "error": "tenk_lens_failed",
                "detail": "Deep-dive timed out and fallback extraction was unavailable.",
            }
        finally:
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
        segs = _build_segments_from_report(report if isinstance(report, dict) else {})
        filings = dict((report or {}).get("files") or {})
        result: dict[str, object] = {
            "ok": True,
            "ticker": t,
            "pair_hash": pair_hash,
            "last_run": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "filings": {
                "new": str(filings.get("latest_10k") or (str(files[0]) if files else "")),
                "old": str(filings.get("prior_10k") or (str(files[1]) if len(files) > 1 else "")),
                "proxy": str(filings.get("latest_proxy") or ""),
            },
            "segments": segs,
            "raw_report": report if isinstance(report, dict) else {},
        }
        with LOCK:
            TENK_LENS_CACHE[ck] = {"ts": time.time(), "result": dict(result)}
        return result
    except Exception as e:
        return {"ok": False, "ticker": t, "error": "tenk_lens_failed", "detail": str(e)[:260]}


def _field(v: object) -> str:
    s = re.sub(r"\s+", " ", str(v or "")).strip()
    return s if s else "Not clearly disclosed in provided text."


def _slice_sentence(text: str, max_chars: int = 180) -> str:
    s = re.sub(r"\s+", " ", (text or "")).strip()
    if not s:
        return "Not clearly disclosed in provided text."
    parts = re.split(r"(?<=[\.\;\:])\s+", s)
    if parts and parts[0]:
        out = parts[0].strip()
    else:
        out = s
    # Skip trivial numeric fragments like "4" / "11" that often appear near Item headers.
    if len(out) < 24 or re.fullmatch(r"[0-9\W]{1,16}", out):
        for p in parts[1:]:
            cand = p.strip()
            if len(cand) >= 24 and not re.fullmatch(r"[0-9\W]{1,16}", cand):
                out = cand
                break
        if len(out) < 24:
            out = s
    if len(out) > max_chars:
        out = out[: max_chars - 1].rstrip() + "..."
    return out


def _contains_any(text: str, words: list[str]) -> bool:
    low = (text or "").lower()
    return any(w in low for w in words)


def _tenk_lens_fallback_sections(sections: dict[str, str], proxy_text: str) -> dict[str, dict[str, str]]:
    biz = sections.get("business") or ""
    mda = sections.get("mda") or ""
    risk = sections.get("risk") or ""
    fin = sections.get("financials") or ""
    notes = sections.get("notes") or ""
    proxy = proxy_text or ""

    def _meaningful_line(*cands: str) -> str:
        for c in cands:
            s = _slice_sentence(c)
            alpha_words = re.findall(r"[A-Za-z]{3,}", s)
            if len(alpha_words) >= 4:
                return s
        return "Not clearly disclosed in provided text."

    return {
        "business": {
            "what_it_does": _meaningful_line(biz, mda, risk),
            "customers_value": _meaningful_line(biz[200:1200], mda[200:1200]),
            "product_mix": _meaningful_line(biz[800:2000], mda[800:2000]),
            "competition_status": "Competition mentioned." if _contains_any(biz, ["competition", "competitor", "market share"]) else "Competition specifics not clearly disclosed.",
            "management_signal": "Clear business framing." if len(biz) > 1000 else "Limited business disclosure extracted.",
            "red_flag": "Complex narrative with low specificity." if _contains_any(biz, ["synergy", "ecosystem", "optimization"]) else "No obvious business red flag in fallback scan.",
            "green_flag": "Core business model disclosed in Item 1.",
        },
        "mda": {
            "why_revenue_up_down": _meaningful_line(mda, biz, fin),
            "margin_drivers": "Margin drivers referenced." if _contains_any(mda, ["margin", "cost", "pricing", "volume"]) else "Margin drivers not explicit in extracted text.",
            "guidance_shift": "Guidance/outlook language present." if _contains_any(mda, ["guidance", "outlook", "expect"]) else "No clear guidance shift detected.",
            "accountability": "Management acknowledges internal drivers." if _contains_any(mda, ["we made", "execution", "mistake"]) else "Tone appears neutral/defensive in fallback scan.",
            "red_flag": "Blame-heavy language risk." if _contains_any(mda, ["macro headwind", "weather", "currency"]) else "No obvious accountability red flag from fallback scan.",
            "green_flag": "MD&A includes operational explanation for period changes.",
        },
        "risk": {
            "top_exposure": _meaningful_line(risk, notes, fin),
            "concentration_risk": "Concentration wording present." if _contains_any(risk, ["single", "concentration", "major customer"]) else "No explicit concentration line in fallback scan.",
            "dependency_risk": "Supplier/partner dependency present." if _contains_any(risk, ["supplier", "partner", "platform dependency"]) else "Dependency wording not explicit in fallback scan.",
            "policy_regulatory_risk": "Regulatory risk discussed." if _contains_any(risk, ["regulation", "government", "compliance"]) else "Policy/regulatory terms limited in fallback scan.",
            "red_flag": "High structural risk density in Item 1A." if len(risk) > 50000 else "No extreme density red flag from fallback scan.",
            "green_flag": "Risk disclosure appears comprehensive.",
        },
        "financials": {
            "income_statement_signal": "Revenue/profit discussion present." if _contains_any(fin, ["revenue", "net income", "operating income"]) else "Income statement signal not explicit.",
            "balance_sheet_signal": "Debt/cash signal present." if _contains_any(fin, ["debt", "cash", "liabilities"]) else "Balance sheet signal not explicit.",
            "cashflow_signal": "Cash flow commentary present." if _contains_any(fin, ["cash flow", "operating cash", "capex"]) else "Cash flow signal not explicit.",
            "working_capital_signal": "Working capital/inventory/receivable language present." if _contains_any(fin, ["working capital", "inventory", "receivable"]) else "Working-capital signal limited.",
            "red_flag": "Financial statement explanation appears thin." if len(fin) < 4000 else "No immediate financial red flag in fallback scan.",
            "green_flag": "Item 8 financial discussion extracted.",
        },
        "notes": {
            "debt_terms_covenants": "Debt/covenant notes present." if _contains_any(notes, ["debt", "covenant", "maturity"]) else "Debt/covenant detail not clearly extracted.",
            "revenue_recognition_detail": "Revenue recognition note present." if _contains_any(notes, ["revenue recognition", "performance obligation"]) else "Revenue-recognition detail not clearly extracted.",
            "customer_supplier_concentration": "Concentration note present." if _contains_any(notes, ["customer", "supplier", "concentration"]) else "Customer/supplier concentration not explicit in extracted notes.",
            "legal_contingency": "Legal contingency note present." if _contains_any(notes, ["legal", "contingency", "litigation"]) else "Legal contingency detail not explicit in extracted notes.",
            "red_flag": "Note disclosure may be incomplete in extracted subset.",
            "green_flag": "Notes subset captured key accounting/debt/legal anchors.",
        },
        "proxy": {
            "ownership_alignment": "Ownership language present." if _contains_any(proxy, ["beneficial ownership", "ownership", "common stock"]) else "Ownership alignment not clearly disclosed in extracted proxy.",
            "incentive_design": "Compensation plan language present." if _contains_any(proxy, ["compensation", "incentive", "bonus"]) else "Incentive design not clearly extracted.",
            "vesting_horizon": "Vesting terms referenced." if _contains_any(proxy, ["vesting", "RSU", "performance stock"]) else "Vesting horizon not explicit in extracted proxy.",
            "capital_behavior_risk": "Short-term EPS incentive risk." if _contains_any(proxy, ["eps", "annual bonus"]) else "Capital behavior risk not explicit in extracted proxy.",
            "red_flag": "Proxy extraction unavailable or partial." if not proxy else "No obvious proxy red flag in fallback scan.",
            "green_flag": "Proxy incentives/ownership data present." if proxy else "Proxy filing not available in local cache.",
        },
    }


def _segment_list(seg: dict[str, object], keys: list[tuple[str, str]]) -> str:
    rows = "".join(f"<li><span class='muted'>{html.escape(lbl)}:</span> {html.escape(_field(seg.get(k)))}</li>" for k, lbl in keys)
    return f"<ul>{rows}</ul>"


def _tenk_lens_overview_card_html(ticker: str, run_now: bool = False) -> str:
    t = resolve_ticker_input(ticker)
    if not t:
        return ""
    res = perform_10k_full_lens(t, force=run_now, cached_only=(not run_now))
    if not bool(res.get("ok")):
        err = html.escape(str(res.get("detail") or res.get("error") or "unavailable"))
        run_href = f"/company?t={html.escape(t)}&tab=overview&lens=1"
        placeholder = (
            "<div class='mda-grid'>"
            "<div class='mda-box'><h3>Business Section</h3><ul><li class='muted'>Not generated yet.</li></ul></div>"
            "<div class='mda-box'><h3>MD&A</h3><ul><li class='muted'>Not generated yet.</li></ul></div>"
            "<div class='mda-box'><h3>Risk Factors</h3><ul><li class='muted'>Not generated yet.</li></ul></div>"
            "<div class='mda-box'><h3>Financials</h3><ul><li class='muted'>Not generated yet.</li></ul></div>"
            "<div class='mda-box'><h3>Read the Notes</h3><ul><li class='muted'>Not generated yet.</li></ul></div>"
            "<div class='mda-box'><h3>Proxy / Ownership / Incentives</h3><ul><li class='muted'>Not generated yet.</li></ul></div>"
            "</div>"
        )
        return (
            "<section class='card c12'>"
            "<h2>10-K Owner Lens</h2>"
            f"<div class='muted'>Not ready: {err}</div>"
            f"{placeholder}"
            f"<div class='actions'><a class='btn' href='{run_href}'>Run 10-K Lens</a></div>"
            "</section>"
        )

    segs = dict(res.get("segments") or {})
    last_run = html.escape(str(res.get("last_run") or "-"))
    run_href = f"/company?t={html.escape(t)}&tab=overview&lens=1"

    business = dict(segs.get("business") or {})
    mda = dict(segs.get("mda") or {})
    risk = dict(segs.get("risk") or {})
    financials = dict(segs.get("financials") or {})
    notes = dict(segs.get("notes") or {})
    proxy = dict(segs.get("proxy") or {})

    return (
        "<section class='card c12'>"
        "<h2>10-K Owner Lens</h2>"
        f"<div class='muted'>Last run: {last_run} | Sections: Business, MD&A, Risk, Financials, Notes, Proxy/Incentives.</div>"
        "<div class='mda-grid'>"
        f"<div class='mda-box'><h3>Business Section</h3>{_segment_list(business, [('what_it_does','What it does'),('customers_value','Customer value'),('product_mix','Product/service mix'),('competition_status','Competition status'),('management_signal','Management signal'),('green_flag','Green flag'),('red_flag','Red flag')])}</div>"
        f"<div class='mda-box'><h3>MD&A</h3>{_segment_list(mda, [('why_revenue_up_down','Revenue up/down why'),('margin_drivers','Margin drivers'),('guidance_shift','Guidance shift'),('accountability','Accountability tone'),('green_flag','Green flag'),('red_flag','Red flag')])}</div>"
        f"<div class='mda-box'><h3>Risk Factors</h3>{_segment_list(risk, [('top_exposure','Top exposure'),('concentration_risk','Concentration risk'),('dependency_risk','Dependency risk'),('policy_regulatory_risk','Policy/regulatory risk'),('green_flag','Green flag'),('red_flag','Red flag')])}</div>"
        f"<div class='mda-box'><h3>Financials</h3>{_segment_list(financials, [('income_statement_signal','Income statement'),('balance_sheet_signal','Balance sheet'),('cashflow_signal','Cash flow'),('working_capital_signal','Working capital'),('green_flag','Green flag'),('red_flag','Red flag')])}</div>"
        f"<div class='mda-box'><h3>Read the Notes</h3>{_segment_list(notes, [('debt_terms_covenants','Debt/covenants'),('revenue_recognition_detail','Revenue recognition'),('customer_supplier_concentration','Customer/supplier concentration'),('legal_contingency','Legal contingencies'),('green_flag','Green flag'),('red_flag','Red flag')])}</div>"
        f"<div class='mda-box'><h3>Proxy / Ownership / Incentives</h3>{_segment_list(proxy, [('ownership_alignment','Ownership alignment'),('incentive_design','Incentive design'),('vesting_horizon','Vesting horizon'),('capital_behavior_risk','Capital behavior risk'),('green_flag','Green flag'),('red_flag','Red flag')])}</div>"
        "</div>"
        f"<div class='actions'><a class='btn' href='{run_href}'>Refresh 10-K Lens</a></div>"
        "</section>"
    )


def tenk_segment_html(ticker: str, segment: str = "business", force: bool = False) -> str:
    t = resolve_ticker_input(ticker)
    seg = (segment or "business").strip().lower()
    allowed = {"business", "mda", "risk", "financials", "notes", "proxy"}
    if seg not in allowed:
        seg = "business"
    if not t:
        return dashboard_html("Ticker is required.")

    res = perform_10k_full_lens(t, force=force, cached_only=(not force))
    title_map = {
        "business": "Business Section",
        "mda": "MD&A",
        "risk": "Risk Factors",
        "financials": "Financials",
        "notes": "Read the Notes",
        "proxy": "Proxy / Ownership / Incentives",
    }
    title = title_map.get(seg, seg.title())
    nav = " ".join(
        f"<a class='btn' href='/tenk_segment?t={html.escape(t)}&s={k}'>{html.escape(v)}</a>"
        for k, v in title_map.items()
    )
    if not bool(res.get("ok")):
        err = html.escape(str(res.get("detail") or res.get("error") or "unavailable"))
        run_href = f"/tenk_segment?t={html.escape(t)}&s={seg}&run=1"
        return (
            "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<style>body{margin:0;background:radial-gradient(980px 460px at 0% 0%, #ff7a5938 0%, transparent 62%),radial-gradient(900px 420px at 100% 0%, #2f5f8a4a 0%, transparent 64%),#0f2033;color:#eef4fb;font-family:'Avenir Next','Helvetica Neue',sans-serif;}"
            ".wrap{max-width:1100px;margin:0 auto;padding:18px;} .card{background:linear-gradient(180deg,#182d45,#122539);border:1px solid #355574;border-radius:12px;padding:14px;margin-bottom:10px;}"
            ".btn{display:inline-block;border:1px solid #ff9f87;border-radius:8px;padding:6px 10px;background:#ff7a59;color:#14263b;text-decoration:none;margin-right:6px;margin-bottom:6px;font-weight:700;}"
            ".muted{color:#a9bfd6;} li{margin:8px 0;}</style></head><body><div class='wrap'>"
            f"<div class='card'><h1>{html.escape(t)} — {html.escape(title)}</h1><a class='btn' href='/universe?tab=all'>Back My Companies</a><a class='btn' href='/company?t={html.escape(t)}&tab=overview'>Company Overview</a><a class='btn' href='{run_href}'>Run 10-K Lens</a><div class='muted' style='margin-top:8px;'>Not ready: {err}</div></div>"
            f"<div class='card'><h2>Sections</h2>{nav}</div>"
            "</div></body></html>"
        )

    segs = dict(res.get("segments") or {})
    data = dict(segs.get(seg) or {})
    last_run = html.escape(str(res.get("last_run") or "-"))
    rows = "".join(
        f"<tr><th>{html.escape(str(k).replace('_', ' ').title())}</th><td>{html.escape(_field(v))}</td></tr>"
        for k, v in data.items()
    ) or "<tr><th>Info</th><td>No extracted fields.</td></tr>"
    run_href = f"/tenk_segment?t={html.escape(t)}&s={seg}&run=1"
    return (
        "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<style>body{margin:0;background:radial-gradient(980px 460px at 0% 0%, #ff7a5938 0%, transparent 62%),radial-gradient(900px 420px at 100% 0%, #2f5f8a4a 0%, transparent 64%),#0f2033;color:#eef4fb;font-family:'Avenir Next','Helvetica Neue',sans-serif;}"
        ".wrap{max-width:1100px;margin:0 auto;padding:18px;} .card{background:linear-gradient(180deg,#182d45,#122539);border:1px solid #355574;border-radius:12px;padding:14px;margin-bottom:10px;}"
        ".btn{display:inline-block;border:1px solid #ff9f87;border-radius:8px;padding:6px 10px;background:#ff7a59;color:#14263b;text-decoration:none;margin-right:6px;margin-bottom:6px;font-weight:700;}"
        ".muted{color:#a9bfd6;} table{width:100%;border-collapse:collapse;} th,td{border-bottom:1px solid #365977;padding:8px;vertical-align:top;text-align:left;} th{width:280px;color:#d5e6f7;}</style></head><body><div class='wrap'>"
        f"<div class='card'><h1>{html.escape(t)} — {html.escape(title)}</h1><a class='btn' href='/universe?tab=all'>Back My Companies</a><a class='btn' href='/company?t={html.escape(t)}&tab=overview'>Company Overview</a><a class='btn' href='{run_href}'>Refresh Lens</a><div class='muted' style='margin-top:8px;'>Last run: {last_run}</div></div>"
        f"<div class='card'><h2>Sections</h2>{nav}</div>"
        f"<div class='card'><table>{rows}</table></div>"
        "</div></body></html>"
    )


def render_list(items: list[str], empty: str) -> str:
    if not items:
        return f"<li class='muted'>{html.escape(empty)}</li>"
    return "".join(f"<li>{html.escape(i)}</li>" for i in items)


def _ensure_memory_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS investor_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            ticker TEXT,
            sentiment TEXT DEFAULT 'neutral',
            note TEXT NOT NULL,
            tags TEXT,
            created_at TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS stock_thesis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            sentiment TEXT NOT NULL,
            content TEXT NOT NULL,
            date TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS scratchpad (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            content TEXT NOT NULL,
            last_updated TEXT NOT NULL
        )"""
    )
    # Lightweight schema migration for task priority/due-date and pinned notes.
    def _has_col(table: str, col: str) -> bool:
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
            return any(str(r[1]) == col for r in rows)
        except Exception:
            return False

    if not _has_col("todos", "priority"):
        conn.execute("ALTER TABLE todos ADD COLUMN priority TEXT NOT NULL DEFAULT 'P2'")
    if not _has_col("todos", "due_date"):
        conn.execute("ALTER TABLE todos ADD COLUMN due_date TEXT DEFAULT ''")
    if not _has_col("todos", "ticker"):
        conn.execute("ALTER TABLE todos ADD COLUMN ticker TEXT NOT NULL DEFAULT ''")
    if not _has_col("todos", "category"):
        conn.execute("ALTER TABLE todos ADD COLUMN category TEXT NOT NULL DEFAULT 'general'")
    if not _has_col("scratchpad", "pinned"):
        conn.execute("ALTER TABLE scratchpad ADD COLUMN pinned TEXT NOT NULL DEFAULT ''")
    conn.execute("INSERT OR IGNORE INTO scratchpad (id, content, last_updated) VALUES (1, '', ?)", (dt.datetime.now().isoformat(),))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS l2_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_ts TEXT NOT NULL,
            scope TEXT NOT NULL,
            summary TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS l2_insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            support_score INTEGER NOT NULL DEFAULT 0,
            conflict_score INTEGER NOT NULL DEFAULT 0,
            novelty_score INTEGER NOT NULL DEFAULT 0,
            urgency_score INTEGER NOT NULL DEFAULT 0,
            total_score INTEGER NOT NULL DEFAULT 0,
            priority TEXT NOT NULL DEFAULT 'LOW',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES l2_runs(id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS intel_feed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            ticker TEXT NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            severity INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            unique_key TEXT NOT NULL UNIQUE
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS intel24_snapshot (
            ticker TEXT PRIMARY KEY,
            asof TEXT NOT NULL,
            day_pct REAL,
            insider_txt TEXT NOT NULL,
            sec_txt TEXT NOT NULL,
            happened TEXT NOT NULL,
            suggestion TEXT NOT NULL,
            event_score INTEGER NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'scheduler'
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS workspace_companies (
            ticker TEXT PRIMARY KEY,
            stage TEXT NOT NULL DEFAULT 'Inbox',
            conviction INTEGER NOT NULL DEFAULT 6,
            thesis TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS workspace_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            action TEXT NOT NULL,
            emotion TEXT NOT NULL,
            note TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS workspace_profiles (
            ticker TEXT PRIMARY KEY,
            why_wrong TEXT NOT NULL DEFAULT '',
            score_growth INTEGER NOT NULL DEFAULT 5,
            score_margin INTEGER NOT NULL DEFAULT 5,
            score_capital INTEGER NOT NULL DEFAULT 5,
            score_valuation INTEGER NOT NULL DEFAULT 5,
            score_risk INTEGER NOT NULL DEFAULT 5,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS workspace_thesis_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            snapshot_month TEXT NOT NULL,
            thesis TEXT NOT NULL DEFAULT '',
            why_wrong TEXT NOT NULL DEFAULT '',
            score_total INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(ticker, snapshot_month)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS workspace_mortems (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            mortem_type TEXT NOT NULL,
            trigger_txt TEXT NOT NULL DEFAULT '',
            hypothesis TEXT NOT NULL DEFAULT '',
            outcome TEXT NOT NULL DEFAULT '',
            lessons TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS workspace_valuation (
            ticker TEXT PRIMARY KEY,
            intrinsic_market_cap_b REAL,
            intrinsic_price REAL,
            strike_starter REAL,
            strike_add REAL,
            strike_aggressive REAL,
            invalidation_trigger TEXT NOT NULL DEFAULT '',
            confidence INTEGER NOT NULL DEFAULT 50,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS workspace_ritual_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ritual_key TEXT NOT NULL,
            title TEXT NOT NULL,
            due_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT '',
            UNIQUE(ritual_key, due_date)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS company_lists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS company_list_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id INTEGER NOT NULL,
            ticker TEXT NOT NULL,
            added_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            UNIQUE(list_id, ticker),
            FOREIGN KEY(list_id) REFERENCES company_lists(id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS company_todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            task TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_todos_ticker_status_due ON todos(ticker, status, due_date, id)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS company_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            remind_at TEXT NOT NULL,
            note TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS company_profile_cache (
            ticker TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            country TEXT NOT NULL DEFAULT '',
            industry TEXT NOT NULL DEFAULT '',
            sector TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS company_moat_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            moat_key TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            UNIQUE(ticker, moat_key)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_company_moat_key_ticker ON company_moat_tags(moat_key, ticker)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS company_sec_competitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            competitor_ticker TEXT NOT NULL,
            competitor_name TEXT NOT NULL DEFAULT '',
            source_form TEXT NOT NULL DEFAULT '',
            source_date TEXT NOT NULL DEFAULT '',
            source_path TEXT NOT NULL DEFAULT '',
            evidence TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0.0,
            status TEXT NOT NULL DEFAULT 'active',
            updated_at TEXT NOT NULL,
            UNIQUE(ticker, competitor_ticker)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_company_sec_comp_ticker_status ON company_sec_competitors(ticker, status)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS company_sec_competitor_runs (
            ticker TEXT PRIMARY KEY,
            last_run TEXT NOT NULL,
            source_form TEXT NOT NULL DEFAULT '',
            source_date TEXT NOT NULL DEFAULT '',
            source_path TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'idle',
            note TEXT NOT NULL DEFAULT ''
        )"""
    )
    # One-time idempotent migration: keep legacy company_todos visible in the unified todos list.
    conn.execute(
        """INSERT INTO todos (task, status, created_at, priority, due_date, ticker, category)
           SELECT ct.task,
                  CASE WHEN LOWER(COALESCE(ct.status, 'open')) = 'done' THEN 'done' ELSE 'open' END,
                  COALESCE(ct.created_at, ?),
                  'P2',
                  '',
                  UPPER(COALESCE(ct.ticker, '')),
                  'company'
           FROM company_todos ct
           WHERE NOT EXISTS (
             SELECT 1
             FROM todos t
             WHERE t.task = ct.task
               AND UPPER(COALESCE(t.ticker, '')) = UPPER(COALESCE(ct.ticker, ''))
               AND COALESCE(t.created_at, '') = COALESCE(ct.created_at, '')
           )""",
        (dt.datetime.now().isoformat(),),
    )


def _ensure_db_split_ready() -> None:
    with LOCK:
        if bool(DB_SPLIT_STATE.get("ready")):
            return
    DATA.mkdir(parents=True, exist_ok=True)
    # One-time safe bootstrap from existing single-db layout.
    if LEGACY_DB_PATH.exists():
        try:
            if not CORE_DB_PATH.exists():
                shutil.copy2(str(LEGACY_DB_PATH), str(CORE_DB_PATH))
        except Exception:
            pass
        try:
            if not FILINGS_DB_PATH.exists():
                shutil.copy2(str(LEGACY_DB_PATH), str(FILINGS_DB_PATH))
        except Exception:
            pass
    # Ensure core schema exists.
    conn_core = sqlite3.connect(str(CORE_DB_PATH))
    conn_core.row_factory = sqlite3.Row
    _ensure_memory_schema(conn_core)
    conn_core.close()
    # Ensure cache schema exists.
    conn_cache = sqlite3.connect(str(CACHE_DB_PATH))
    conn_cache.execute(
        """CREATE TABLE IF NOT EXISTS cache_entries (
            key TEXT PRIMARY KEY,
            ts REAL NOT NULL,
            payload TEXT NOT NULL
        )"""
    )
    conn_cache.commit()
    conn_cache.close()
    with LOCK:
        DB_SPLIT_STATE["ready"] = True


def note_db() -> sqlite3.Connection:
    _ensure_db_split_ready()
    db_path = CORE_DB_PATH
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    _ensure_memory_schema(conn)
    return conn


def memory_db() -> sqlite3.Connection:
    _ensure_db_split_ready()
    db_path = CORE_DB_PATH
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    _ensure_memory_schema(conn)
    return conn


def list_todos(limit: int = 40, ticker: str = "", include_archived: bool = False) -> list[sqlite3.Row]:
    t = resolve_ticker_input(ticker)
    conn = memory_db()
    where_parts: list[str] = []
    args: list[object] = []
    if not include_archived:
        where_parts.append("status != 'archived'")
    if t:
        where_parts.append("ticker = ?")
        args.append(t)
    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    rows = conn.execute(
        f"""SELECT id, task, status, created_at, priority, due_date, ticker, category
            FROM todos
            {where_sql}
            ORDER BY
              CASE WHEN status='open' THEN 0 ELSE 1 END,
              CASE priority WHEN 'P1' THEN 0 WHEN 'P2' THEN 1 ELSE 2 END,
              CASE WHEN due_date IS NULL OR due_date='' THEN 1 ELSE 0 END,
              due_date ASC,
              created_at DESC
            LIMIT ?""",
        tuple(args + [max(1, min(5000, int(limit)))]),
    ).fetchall()
    conn.close()
    return rows


def insert_intel_feed(
    ticker: str,
    category: str,
    title: str,
    summary: str,
    detail: str,
    severity: int,
    source: str,
    model: str,
    unique_key: str,
) -> bool:
    ukey = (unique_key or "").strip()
    if not ukey:
        return False
    conn = memory_db()
    try:
        sev_num = int(severity)
    except Exception:
        sev_num = 0
    cur = conn.execute(
        """INSERT OR IGNORE INTO intel_feed
           (created_at, ticker, category, title, summary, detail, severity, source, model, unique_key)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            dt.datetime.now().isoformat(),
            (ticker or "").strip().upper(),
            (category or "").strip().upper() or "GENERAL",
            (title or "").strip()[:220],
            (summary or "").strip()[:2200],
            (detail or "").strip()[:8000],
            max(0, min(100, sev_num)),
            (source or "").strip()[:120],
            (model or "").strip()[:120],
            ukey[:180],
        ),
    )
    conn.commit()
    added = int(cur.rowcount) > 0
    conn.close()
    return added


def list_intel_feed(limit: int = 10) -> list[sqlite3.Row]:
    lim = max(1, min(50, int(limit)))
    conn = memory_db()
    rows = conn.execute(
        """SELECT id, created_at, ticker, category, title, summary, detail, severity, source, model
           FROM intel_feed
           ORDER BY id DESC
           LIMIT ?""",
        (lim,),
    ).fetchall()
    conn.close()
    return rows


def latest_intel_feed_id() -> int:
    conn = memory_db()
    row = conn.execute("SELECT MAX(id) AS mx FROM intel_feed").fetchone()
    conn.close()
    try:
        return int((row["mx"] if row else 0) or 0)
    except Exception:
        return 0


def save_intel24_snapshot(rows: list[dict[str, object]], source: str = "scheduler") -> None:
    if not rows:
        return
    conn = memory_db()
    asof = dt.datetime.now().isoformat()
    for r in rows:
        t = str(r.get("ticker") or "").strip().upper()
        if not t:
            continue
        dp = r.get("day_pct")
        day_pct = float(dp) if isinstance(dp, (int, float)) else None
        conn.execute(
            """INSERT INTO intel24_snapshot (ticker, asof, day_pct, insider_txt, sec_txt, happened, suggestion, event_score, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET
                 asof=excluded.asof,
                 day_pct=excluded.day_pct,
                 insider_txt=excluded.insider_txt,
                 sec_txt=excluded.sec_txt,
                 happened=excluded.happened,
                 suggestion=excluded.suggestion,
                 event_score=excluded.event_score,
                 source=excluded.source""",
            (
                t,
                asof,
                day_pct,
                str(r.get("insider_txt") or "No notable insider trades"),
                str(r.get("sec_txt") or "-"),
                str(r.get("happened") or "No material change detected."),
                str(r.get("suggestion") or "Monitor only."),
                _to_int(r.get("event_score"), 0),
                str(source or "scheduler"),
            ),
        )
    conn.commit()
    conn.close()


def load_intel24_snapshot_map(max_age_seconds: int = 2400) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    conn = memory_db()
    rows = conn.execute(
        """SELECT ticker, asof, day_pct, insider_txt, sec_txt, happened, suggestion, event_score, source
           FROM intel24_snapshot"""
    ).fetchall()
    conn.close()
    now = dt.datetime.now()
    ttl = max(60, int(max_age_seconds))
    for r in rows:
        t = str(r["ticker"] or "").strip().upper()
        if not t:
            continue
        asof_dt = _parse_iso_datetime(str(r["asof"] or ""))
        if asof_dt is None:
            continue
        if (now - asof_dt).total_seconds() > ttl:
            continue
        out[t] = {
            "ticker": t,
            "asof": str(r["asof"] or ""),
            "day_pct": (float(r["day_pct"]) if isinstance(r["day_pct"], (int, float)) else None),
            "insider_txt": str(r["insider_txt"] or ""),
            "sec_txt": str(r["sec_txt"] or ""),
            "happened": str(r["happened"] or ""),
            "suggestion": str(r["suggestion"] or ""),
            "event_score": _to_int(r["event_score"], 0),
            "source": str(r["source"] or ""),
        }
    return out


def _workspace_stage_for_ticker(ticker: str) -> str:
    t = (ticker or "").strip().upper()
    if not t:
        return "Inbox"
    in_port = any((str(r[0] or "").strip().upper() == t) for r in read_portfolio_rows(DATA / "portfolio.csv"))
    if in_port:
        return "Portfolio"
    in_watch = t in {str(x or "").strip().upper() for x in read_watchlist_tickers()}
    if in_watch:
        return "Watchlist"
    return "Inbox"


def _is_tracked_holding_ticker(ticker: str) -> bool:
    t = (ticker or "").strip().upper()
    if not t:
        return False
    if any((str(r[0] or "").strip().upper() == t) for r in read_portfolio_rows(DATA / "portfolio.csv")):
        return True
    return t in {str(x or "").strip().upper() for x in read_watchlist_tickers()}


def sync_workspace_companies() -> None:
    tickers: set[str] = set()
    for r in read_portfolio_rows(DATA / "portfolio.csv"):
        t = str(r[0] or "").strip().upper()
        if t:
            tickers.add(t)
    for t in read_watchlist_tickers():
        u = str(t or "").strip().upper()
        if u:
            tickers.add(u)
    if not tickers:
        return
    conn = memory_db()
    now_s = dt.datetime.now().isoformat()
    for t in sorted(tickers):
        row = conn.execute("SELECT ticker, stage FROM workspace_companies WHERE ticker = ?", (t,)).fetchone()
        auto_stage = _workspace_stage_for_ticker(t)
        if not row:
            conn.execute(
                "INSERT INTO workspace_companies (ticker, stage, conviction, thesis, updated_at) VALUES (?, ?, ?, '', ?)",
                (t, auto_stage, 6, now_s),
            )
            continue
        cur_stage = str(row["stage"] or "Inbox").strip()
        if cur_stage in {"Portfolio", "Watchlist"} and cur_stage != auto_stage and auto_stage in {"Portfolio", "Watchlist"}:
            conn.execute(
                "UPDATE workspace_companies SET stage = ?, updated_at = ? WHERE ticker = ?",
                (auto_stage, now_s, t),
            )
    conn.commit()
    conn.close()


def list_workspace_companies(limit: int = 400) -> list[sqlite3.Row]:
    sync_workspace_companies()
    conn = memory_db()
    rows = conn.execute(
        """SELECT ticker, stage, conviction, thesis, updated_at
           FROM workspace_companies
           ORDER BY
             CASE stage WHEN 'Portfolio' THEN 0 WHEN 'Watchlist' THEN 1 WHEN 'Deep Dive' THEN 2 ELSE 3 END,
             ticker ASC
           LIMIT ?""",
        (max(1, min(2000, int(limit))),),
    ).fetchall()
    conn.close()
    return rows


def save_workspace_company(ticker: str, stage: str, conviction: int, thesis: str) -> bool:
    t = resolve_ticker_input(ticker)
    if not t:
        return False
    st = (stage or "Inbox").strip()
    if st not in {"Inbox", "Deep Dive", "Watchlist", "Portfolio"}:
        st = _workspace_stage_for_ticker(t)
    cv = max(1, min(10, int(conviction)))
    th = (thesis or "").strip()
    now_s = dt.datetime.now().isoformat()
    conn = memory_db()
    conn.execute(
        """INSERT INTO workspace_companies (ticker, stage, conviction, thesis, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(ticker) DO UPDATE SET
             stage=excluded.stage,
             conviction=excluded.conviction,
             thesis=excluded.thesis,
             updated_at=excluded.updated_at""",
        (t, st, cv, th, now_s),
    )
    conn.commit()
    conn.close()
    return True


def list_workspace_journal(ticker: str, limit: int = 120) -> list[sqlite3.Row]:
    t = resolve_ticker_input(ticker)
    if not t:
        return []
    conn = memory_db()
    rows = conn.execute(
        """SELECT id, ticker, action, emotion, note, created_at
           FROM workspace_journal
           WHERE ticker = ?
           ORDER BY id DESC
           LIMIT ?""",
        (t, max(1, min(500, int(limit)))),
    ).fetchall()
    conn.close()
    return rows


def list_workspace_journal_all(limit: int = 240) -> list[sqlite3.Row]:
    conn = memory_db()
    rows = conn.execute(
        """SELECT id, ticker, action, emotion, note, created_at
           FROM workspace_journal
           ORDER BY id DESC
           LIMIT ?""",
        (max(1, min(1200, int(limit))),),
    ).fetchall()
    conn.close()
    return rows


def add_workspace_journal(ticker: str, action: str, emotion: str, note: str) -> int:
    t = resolve_ticker_input(ticker)
    if not t:
        return 0
    act = (action or "Note").strip().title()
    emo = (emotion or "Calm").strip().title()
    if act not in {"Buy", "Sell", "Note", "Mistake", "Lesson"}:
        act = "Note"
    if emo not in {"Calm", "Excited", "Anxious"}:
        emo = "Calm"
    txt = (note or "").strip()
    if not txt:
        return 0
    conn = memory_db()
    cur = conn.execute(
        "INSERT INTO workspace_journal (ticker, action, emotion, note, created_at) VALUES (?, ?, ?, ?, ?)",
        (t, act, emo, txt[:4000], dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    rid = int(cur.lastrowid)
    conn.close()
    return rid


def log_watchlist_event(ticker: str, event: str, reason: str = "") -> int:
    t = resolve_ticker_input(ticker)
    if not t:
        return 0
    ev = (event or "").strip().lower()
    if ev not in {"added", "removed"}:
        ev = "added"
    why = (reason or "").strip()
    if not why:
        why = "No reason provided."
    note = f"WATCHLIST_{ev.upper()}: {t} | Reason: {why}"
    return add_workspace_journal(t, "Note", "Calm", note)


def log_portfolio_event(ticker: str, event: str, detail: str = "") -> int:
    t = resolve_ticker_input(ticker)
    if not t:
        return 0
    ev = (event or "").strip().lower()
    if ev not in {"added", "updated", "removed"}:
        ev = "updated"
    d = (detail or "").strip() or "No detail."
    note = f"PORTFOLIO_{ev.upper()}: {t} | Detail: {d}"
    return add_workspace_journal(t, "Note", "Calm", note)


def list_company_todos(ticker: str, limit: int = 300) -> list[sqlite3.Row]:
    t = resolve_ticker_input(ticker)
    if not t:
        return []
    return list_todos(limit=max(1, min(2000, int(limit))), ticker=t, include_archived=False)


def add_company_todo(ticker: str, task: str) -> int:
    t = resolve_ticker_input(ticker)
    txt = (task or "").strip()
    if not t or not txt:
        return 0
    return add_todo(task=txt[:800], priority="P2", due_date="", ticker=t, category="company")


def toggle_company_todo(todo_id: int) -> bool:
    return toggle_todo(int(todo_id))


def delete_company_todo(todo_id: int) -> bool:
    return delete_todo(int(todo_id))


def list_company_reminders(ticker: str, limit: int = 200) -> list[sqlite3.Row]:
    t = resolve_ticker_input(ticker)
    if not t:
        return []
    conn = memory_db()
    rows = conn.execute(
        """SELECT id, ticker, remind_at, note, status, created_at
           FROM company_reminders
           WHERE ticker = ?
           ORDER BY CASE WHEN status='open' THEN 0 ELSE 1 END, remind_at ASC, id DESC
           LIMIT ?""",
        (t, max(1, min(1000, int(limit)))),
    ).fetchall()
    conn.close()
    return rows


def list_company_reminders_all(limit: int = 400) -> list[sqlite3.Row]:
    conn = memory_db()
    rows = conn.execute(
        """SELECT id, ticker, remind_at, note, status, created_at
           FROM company_reminders
           ORDER BY CASE WHEN status='open' THEN 0 ELSE 1 END, remind_at ASC, id DESC
           LIMIT ?""",
        (max(1, min(5000, int(limit))),),
    ).fetchall()
    conn.close()
    return rows


def add_company_reminder(ticker: str, remind_at: str, note: str) -> int:
    t = resolve_ticker_input(ticker)
    txt = (note or "").strip()
    ra_raw = (remind_at or "").strip()
    if not t or not txt:
        return 0
    ra = ra_raw.replace("T", " ").strip()
    if len(ra) == 16:
        ra = ra + ":00"
    if not re.match(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}(:\d{2})?)?$", ra):
        ra = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = memory_db()
    cur = conn.execute(
        "INSERT INTO company_reminders (ticker, remind_at, note, status, created_at) VALUES (?, ?, ?, 'open', ?)",
        (t, ra, txt[:1000], dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    rid = int(cur.lastrowid)
    conn.close()
    return rid


def toggle_company_reminder(reminder_id: int) -> bool:
    if int(reminder_id) <= 0:
        return False
    conn = memory_db()
    row = conn.execute("SELECT status FROM company_reminders WHERE id = ?", (int(reminder_id),)).fetchone()
    if not row:
        conn.close()
        return False
    status = str(row["status"] or "open").strip().lower()
    new_status = "done" if status != "done" else "open"
    conn.execute("UPDATE company_reminders SET status = ? WHERE id = ?", (new_status, int(reminder_id)))
    conn.commit()
    conn.close()
    return True


def list_watchlist_history(ticker: str = "", order: str = "desc", limit: int = 5000) -> list[sqlite3.Row]:
    t = resolve_ticker_input(ticker) if ticker else ""
    o = "ASC" if str(order or "").strip().lower() == "asc" else "DESC"
    lim = max(50, min(20000, int(limit)))
    conn = memory_db()
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workspace_journal_created ON workspace_journal(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_workspace_journal_ticker ON workspace_journal(ticker)")
        if t:
            rows = conn.execute(
                f"""SELECT id, ticker, action, emotion, note, created_at
                    FROM workspace_journal
                    WHERE ticker = ? AND note LIKE 'WATCHLIST_%'
                    ORDER BY created_at {o}, id {o}
                    LIMIT ?""",
                (t, lim),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT id, ticker, action, emotion, note, created_at
                    FROM workspace_journal
                    WHERE note LIKE 'WATCHLIST_%'
                    ORDER BY created_at {o}, id {o}
                    LIMIT ?""",
                (lim,),
            ).fetchall()
        return rows
    finally:
        conn.close()


def watchlist_history_html(ticker: str = "", order: str = "desc", limit: int = 5000) -> str:
    t = resolve_ticker_input(ticker) if ticker else ""
    ordv = "asc" if str(order or "").strip().lower() == "asc" else "desc"
    lim = max(50, min(20000, int(limit)))
    rows = list_watchlist_history(ticker=t, order=ordv, limit=lim)
    body_rows = "".join(
        (
            "<tr>"
            f"<td>{html.escape(str(r['created_at'] or ''))}</td>"
            f"<td>{html.escape(str(r['ticker'] or ''))}</td>"
            f"<td>{html.escape('Added' if 'WATCHLIST_ADDED' in str(r['note'] or '') else ('Removed' if 'WATCHLIST_REMOVED' in str(r['note'] or '') else 'Event'))}</td>"
            f"<td>{html.escape(str(r['note'] or ''))}</td>"
            f"<td><a class='btn' href='/company_file?t={html.escape(str(r['ticker'] or ''))}'>Open File</a></td>"
            "</tr>"
        )
        for r in rows
    ) or "<tr><td colspan='5' class='muted'>No watchlist history found.</td></tr>"
    return (
        "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Watchlist History</title>"
        "<style>body{margin:0;background:#eaf0f4;color:#2f4358;font-family:'Avenir Next','Helvetica Neue',sans-serif;}"
        ".wrap{max-width:1280px;margin:0 auto;padding:14px;} .card{background:#f1f5f8;border:1px solid #d6dee6;border-radius:10px;padding:12px;margin-bottom:10px;}"
        ".btn{border:1px solid #c8d3dd;background:#edf2f6;color:#2f4358;border-radius:8px;padding:6px 10px;text-decoration:none;font-size:12px;font-weight:700;}"
        "input,select{width:100%;box-sizing:border-box;border:1px solid #c8d3dd;border-radius:8px;background:#edf2f6;color:#2f4358;padding:8px;}"
        "button{border:1px solid #ff7a59;background:#ff7a59;color:#fff;border-radius:8px;padding:8px 10px;font-weight:700;cursor:pointer;}"
        "table{width:100%;border-collapse:collapse;} th,td{border-bottom:1px solid #d6dee6;padding:8px;text-align:left;vertical-align:top;} .muted{color:#4f6780;}</style>"
        "</head><body><div class='wrap'>"
        "<div class='card'><a class='btn' href='/'>Home</a> <a class='btn' href='/company_lists'>Lists</a> <a class='btn' href='/indices'>Index Library</a></div>"
        "<div class='card'><h2 style='margin:0 0 8px 0;'>Watchlist History</h2><div class='muted'>Persistent, timestamped add/remove history.</div>"
        "<form method='get' action='/watchlist_history' style='display:grid;grid-template-columns:1fr 1fr 1fr auto;gap:8px;margin-top:8px;'>"
        f"<input name='ticker' value='{html.escape(t)}' placeholder='Optional ticker (e.g. CRM)'>"
        "<select name='order'><option value='desc' " + ("selected" if ordv == "desc" else "") + ">Newest First</option><option value='asc' " + ("selected" if ordv == "asc" else "") + ">Oldest First</option></select>"
        f"<input name='limit' value='{lim}' placeholder='Limit (max 20000)'>"
        "<button type='submit'>Apply</button>"
        "</form></div>"
        "<div class='card'><table><thead><tr><th>Date</th><th>Ticker</th><th>Event</th><th>Details</th><th>Open</th></tr></thead><tbody>"
        f"{body_rows}</tbody></table></div></div></body></html>"
    )


def _company_file_universe(limit: int = 2000) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for t in read_portfolio_tickers() + read_watchlist_tickers():
        u = resolve_ticker_input(t)
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    try:
        for r in list_workspace_companies(limit=limit):
            u = resolve_ticker_input(str(r["ticker"] or ""))
            if u and u not in seen:
                seen.add(u)
                out.append(u)
    except Exception:
        pass
    try:
        conn = memory_db()
        rows = conn.execute("SELECT DISTINCT ticker FROM company_list_items ORDER BY ticker ASC LIMIT ?", (max(100, min(10000, int(limit))),)).fetchall()
        conn.close()
        for r in rows:
            u = resolve_ticker_input(str(r["ticker"] or ""))
            if u and u not in seen:
                seen.add(u)
                out.append(u)
    except Exception:
        pass
    return out[: max(50, min(5000, int(limit)))]


def _company_name_db_map(tickers: list[str]) -> dict[str, str]:
    uniq = sorted({str(t or "").strip().upper() for t in tickers if str(t or "").strip()})
    if not uniq:
        return {}
    out: dict[str, str] = {}
    try:
        conn = sqlite3.connect(str(FILINGS_DB_PATH if FILINGS_DB_PATH.exists() else LEGACY_DB_PATH))
        placeholders = ",".join("?" for _ in uniq)
        rows = conn.execute(
            f"SELECT ticker, name FROM companies WHERE ticker IN ({placeholders})",
            tuple(uniq),
        ).fetchall()
        conn.close()
        for t, n in rows:
            tu = str(t or "").strip().upper()
            nm = str(n or "").strip()
            if tu and nm:
                out[tu] = nm
    except Exception:
        return out
    return out


def _company_db_profile_map(tickers: list[str]) -> dict[str, dict[str, str]]:
    uniq = sorted({str(t or "").strip().upper() for t in tickers if str(t or "").strip()})
    if not uniq:
        return {}
    out: dict[str, dict[str, str]] = {}
    try:
        conn = sqlite3.connect(str(FILINGS_DB_PATH if FILINGS_DB_PATH.exists() else LEGACY_DB_PATH))
        placeholders = ",".join("?" for _ in uniq)
        rows = conn.execute(
            f"SELECT ticker, name FROM companies WHERE ticker IN ({placeholders})",
            tuple(uniq),
        ).fetchall()
        conn.close()
        for t, n in rows:
            tu = str(t or "").strip().upper()
            if not tu:
                continue
            out[tu] = {
                "name": str(n or "").strip(),
                "country": "",
                "industry": "",
            }
    except Exception:
        return out
    return out


def _company_name_is_placeholder(name: str, ticker: str) -> bool:
    n = str(name or "").strip()
    t = str(ticker or "").strip().upper()
    if not n:
        return True
    nu = n.upper()
    if t and (nu == t or nu == f"{t}."):
        return True
    if len(n) <= 2:
        return True
    return False


def _prettify_company_name(name: str, ticker: str) -> str:
    raw = str(name or "").strip()
    if _company_name_is_placeholder(raw, ticker):
        return ""
    # If fully upper-case words, make it more readable.
    alpha = "".join(ch for ch in raw if ch.isalpha())
    if alpha and alpha.isupper() and " " in raw:
        return raw.title()
    return raw


def _company_file_search_results(
    query: str,
    limit: int = 80,
    offset: int = 0,
    sort_by: str = "name_asc",
    industry_filter: str = "",
    industry_mode: str = "include",
    universe_tickers: list[str] | None = None,
) -> tuple[list[dict[str, str]], int, list[str]]:
    q = (query or "").strip().lower()
    seed = universe_tickers if universe_tickers is not None else _company_file_universe(limit=2500)
    base: list[str] = []
    seen: set[str] = set()
    for x in seed:
        t = resolve_ticker_input(x)
        if t and t not in seen:
            seen.add(t)
            base.append(t)
    lim = max(10, min(300, int(limit)))
    off = max(0, int(offset))
    profiles = get_portfolio_profiles(base, ttl_seconds=86400 * 7) if base else {}
    db_profiles = _company_db_profile_map(base)
    normalized: list[dict[str, str]] = []
    for t in base:
        prof = profiles.get(t, {}) or {}
        db_prof = db_profiles.get(t, {}) or {}
        name = _prettify_company_name(str(prof.get("name") or ""), t)
        if not name:
            name = _prettify_company_name(str(db_prof.get("name") or ""), t)
        if not name:
            name = ""
        country = str(prof.get("country") or db_prof.get("country") or "-").strip() or "-"
        industry = str(prof.get("industry") or db_prof.get("industry") or "Unknown").strip() or "Unknown"
        normalized.append({"ticker": t, "name": name, "country": country, "industry": industry})
    industry_options = sorted({str(r.get("industry") or "Unknown").strip() or "Unknown" for r in normalized})
    ind = str(industry_filter or "").strip()
    mode = "exclude" if str(industry_mode or "").strip().lower() == "exclude" else "include"
    if ind:
        ind_l = ind.lower()
        if mode == "exclude":
            normalized = [r for r in normalized if ind_l not in str(r.get("industry") or "").lower()]
        else:
            normalized = [r for r in normalized if ind_l in str(r.get("industry") or "").lower()]
    if q:
        normalized = [
            r
            for r in normalized
            if q in str(r.get("ticker") or "").lower()
            or q in str(r.get("name") or "").lower()
            or q in str(r.get("country") or "").lower()
            or q in str(r.get("industry") or "").lower()
        ]
    if sort_by not in {"name_asc", "mcap_desc", "mcap_asc"}:
        sort_by = "name_asc"
    if sort_by == "name_asc":
        def _name_sort_key(r: dict[str, str]) -> tuple[int, int, str, str]:
            tkr = str(r.get("ticker") or "").strip().upper()
            nm = str(r.get("name") or "").strip()
            indv = str(r.get("industry") or "").strip().lower()
            name_missing = 1 if _company_name_is_placeholder(nm, tkr) else 0
            industry_missing = 1 if indv in {"", "unknown", "-", "n/a", "na"} else 0
            return (name_missing, industry_missing, nm.lower(), tkr.lower())
        normalized.sort(key=_name_sort_key)
    else:
        for r in normalized:
            tkr = str(r.get("ticker") or "").upper().strip()
            r["_mcap_cached"] = _market_cap_cached_only(tkr)
        normalized.sort(
            key=lambda r: _parse_mcap_value(str(r.get("_mcap_cached") or "-")),
            reverse=(sort_by == "mcap_desc"),
        )
    total = len(normalized)
    page = normalized[off:off + lim]
    # Resolve missing details for current page synchronously to reduce Unknown/- values.
    missing_on_page = [
        str(r.get("ticker") or "").upper().strip()
        for r in page
        if (
            not str(r.get("name") or "").strip()
            or str(r.get("industry") or "").strip().lower() in {"", "unknown"}
            or str(r.get("country") or "").strip() in {"", "-"}
        )
    ]
    for t in missing_on_page:
        if not t:
            continue
        p = fetch_portfolio_profile_single(t)
        _save_profile_cache_row(t, p)
        fresh_name = _prettify_company_name(str(p.get("name") or ""), t)
        with LOCK:
            PORT_PROFILE_CACHE[t] = {"ts": time.time(), "data": dict(p)}
        for r in page:
            if str(r.get("ticker") or "").upper().strip() == t:
                if fresh_name:
                    r["name"] = fresh_name
                p_ind = str(p.get("industry") or "").strip()
                p_country = str(p.get("country") or "").strip()
                if p_ind:
                    r["industry"] = p_ind
                if p_country:
                    r["country"] = p_country
                break
    # Final fallback: never show "Name unavailable"; use ticker if no official name found.
    for r in page:
        if not str(r.get("name") or "").strip():
            r["name"] = str(r.get("ticker") or "Unknown")
    for i, r in enumerate(page):
        t = str(r.get("ticker") or "").upper()
        mcap = _market_cap_cached_only(t)
        if (not mcap or mcap == "-") and i < 20:
            mcap = _market_cap_for_ticker(t)
        r["mcap"] = str(mcap or "-")
    return page, total, industry_options


def _quick_capture_widget_html(return_to: str = "/", current_ticker: str = "") -> str:
    rt = str(return_to or "/").strip()
    if not rt.startswith("/"):
        rt = "/"
    tk = resolve_ticker_input(current_ticker) or ""
    return (
        "<details class='qc-widget'>"
        "<summary>Quick Capture</summary>"
        "<form method='post' action='/quick_capture' class='qc-form'>"
        f"<input type='hidden' name='return_to' value='{html.escape(rt)}'>"
        f"<input name='ticker' value='{html.escape(tk)}' placeholder='Ticker (optional, e.g. AAPL)'>"
        "<textarea name='text' rows='4' maxlength='2000' placeholder='Write a quick note or task...'></textarea>"
        "<div class='qc-row'>"
        "<button type='submit' name='mode' value='note'>Save Note</button>"
        "<button type='submit' name='mode' value='task'>Save Task</button>"
        "</div>"
        "<div class='qc-help'>If ticker is set, note appears in that company file. Prefix task with <code>!</code> for quick auto-hide on done.</div>"
        "</form>"
        "</details>"
        "<style>"
        ".qc-widget{position:fixed;right:16px;bottom:16px;width:min(360px,92vw);z-index:9999;background:#f1f5f8;border:1px solid #d6dee6;border-radius:12px;box-shadow:0 8px 24px rgba(0,0,0,.12);}"
        ".qc-widget>summary{cursor:pointer;list-style:none;padding:10px 12px;font-weight:700;color:#2f4358;}"
        ".qc-widget[open]>summary{border-bottom:1px solid #d6dee6;}"
        ".qc-form{padding:10px;display:grid;gap:8px;}"
        ".qc-form input,.qc-form textarea{width:100%;box-sizing:border-box;border:1px solid #c8d3dd;border-radius:8px;background:#edf2f6;color:#2f4358;padding:8px;}"
        ".qc-row{display:flex;gap:8px;}"
        ".qc-row button{flex:1;border:1px solid #ff7a59;background:#ff7a59;color:#fff;border-radius:8px;padding:8px 10px;font-weight:700;cursor:pointer;}"
        ".qc-help{font-size:11px;color:#4f6780;line-height:1.35;}"
        "</style>"
    )


def _ensure_daily_note_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS daily_notes (
            day TEXT PRIMARY KEY,
            content TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            locked INTEGER NOT NULL DEFAULT 0,
            archived_at TEXT NOT NULL DEFAULT ''
        )"""
    )
    cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(daily_notes)").fetchall()}
    if "locked" not in cols:
        conn.execute("ALTER TABLE daily_notes ADD COLUMN locked INTEGER NOT NULL DEFAULT 0")
    if "archived_at" not in cols:
        conn.execute("ALTER TABLE daily_notes ADD COLUMN archived_at TEXT NOT NULL DEFAULT ''")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS daily_note_tags (
            day TEXT NOT NULL,
            ticker TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(day, ticker)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_note_tags_ticker_day ON daily_note_tags(ticker, day)")


def _extract_tickers_from_text(text: str, limit: int = 40) -> list[str]:
    raw = str(text or "")
    out: set[str] = set()
    for t in re.findall(r"\$([A-Za-z]{1,6})\b", raw):
        u = resolve_ticker_input(t)
        if u:
            out.add(u)
    stop = {"THE", "AND", "FOR", "WITH", "THIS", "THAT", "FROM", "NOTE", "IDEA", "BOOK"}
    for t in re.findall(r"\b([A-Z]{2,5})\b", raw):
        if t in stop:
            continue
        u = resolve_ticker_input(t)
        if u:
            out.add(u)
    low_norm = _normalize_company_key(raw)
    name_map = _company_name_map(ttl_seconds=900)
    for nk, tk in name_map.items():
        if len(nk) < 4:
            continue
        if nk in low_norm:
            u = resolve_ticker_input(tk)
            if u:
                out.add(u)
        if len(out) >= max(3, int(limit)):
            break
    return sorted(out)[: max(1, min(120, int(limit)))]


def get_daily_note_entry(day: str) -> dict[str, str | int]:
    d = str(day or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        d = dt.date.today().isoformat()
    conn = memory_db()
    try:
        _ensure_daily_note_schema(conn)
        row = conn.execute("SELECT content, updated_at, locked, archived_at FROM daily_notes WHERE day = ?", (d,)).fetchone()
        tags = conn.execute("SELECT ticker FROM daily_note_tags WHERE day = ? ORDER BY ticker ASC", (d,)).fetchall()
    finally:
        conn.close()
    return {
        "day": d,
        "content": str((row["content"] if row else "") or ""),
        "updated_at": str((row["updated_at"] if row else "") or ""),
        "locked": int((row["locked"] if row else 0) or 0),
        "archived_at": str((row["archived_at"] if row else "") or ""),
        "tickers": ",".join(str(r["ticker"] or "").strip().upper() for r in tags if str(r["ticker"] or "").strip()),
    }


def get_daily_note(day: str) -> str:
    return str(get_daily_note_entry(day).get("content") or "")


def save_daily_note(day: str, content: str, force: bool = False) -> bool:
    d = str(day or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        d = dt.date.today().isoformat()
    txt = str(content or "").strip()
    conn = memory_db()
    try:
        _ensure_daily_note_schema(conn)
        row = conn.execute("SELECT locked FROM daily_notes WHERE day = ?", (d,)).fetchone()
        if row and int(row["locked"] or 0) == 1 and not force:
            return False
        now_s = dt.datetime.now().isoformat()
        conn.execute(
            """INSERT INTO daily_notes (day, content, updated_at, locked, archived_at)
               VALUES (?, ?, ?, 0, '')
               ON CONFLICT(day) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at""",
            (d, txt[:120000], now_s),
        )
        tickers = _extract_tickers_from_text(txt, limit=60)
        conn.execute("DELETE FROM daily_note_tags WHERE day = ?", (d,))
        for tk in tickers:
            conn.execute(
                "INSERT OR IGNORE INTO daily_note_tags (day, ticker, created_at) VALUES (?, ?, ?)",
                (d, tk, now_s),
            )
        conn.commit()
        return True
    finally:
        conn.close()


def close_daily_note(day: str) -> bool:
    d = str(day or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        return False
    conn = memory_db()
    try:
        _ensure_daily_note_schema(conn)
        cur = conn.execute(
            "UPDATE daily_notes SET locked = 1, archived_at = ?, updated_at = ? WHERE day = ?",
            (dt.datetime.now().isoformat(), dt.datetime.now().isoformat(), d),
        )
        conn.commit()
        return bool(cur.rowcount > 0)
    finally:
        conn.close()


def _organizer_memory_rows(limit: int = 800) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for r in list_investor_notes(limit=max(1, min(1000, int(limit)))):
        rows.append(
            {
                "date": str(r["created_at"] or ""),
                "ticker": str(r["ticker"] or "").strip().upper(),
                "source": "general_note",
                "text": str(r["note"] or ""),
            }
        )
    for r in list_workspace_journal_all(limit=max(1, min(1000, int(limit)))):
        rows.append(
            {
                "date": str(r["created_at"] or ""),
                "ticker": str(r["ticker"] or "").strip().upper(),
                "source": "company_note",
                "text": str(r["note"] or ""),
            }
        )
    try:
        conn = memory_db()
        trows = conn.execute(
            "SELECT ticker, sentiment, content, date FROM stock_thesis ORDER BY date DESC LIMIT ?",
            (max(20, min(1000, int(limit))),),
        ).fetchall()
        conn.close()
        for r in trows:
            rows.append(
                {
                    "date": str(r["date"] or ""),
                    "ticker": str(r["ticker"] or "").strip().upper(),
                    "source": f"thesis:{str(r['sentiment'] or '').strip().lower()}",
                    "text": str(r["content"] or ""),
                }
            )
    except Exception:
        pass
    try:
        conn = memory_db()
        _ensure_daily_note_schema(conn)
        drows = conn.execute(
            "SELECT day, content, updated_at FROM daily_notes ORDER BY day DESC LIMIT ?",
            (max(20, min(1200, int(limit))),),
        ).fetchall()
        for r in drows:
            day = str(r["day"] or "")
            txt = str(r["content"] or "")
            if not txt:
                continue
            tags = conn.execute("SELECT ticker FROM daily_note_tags WHERE day = ? ORDER BY ticker ASC", (day,)).fetchall()
            tag_list = [str(x["ticker"] or "").strip().upper() for x in tags if str(x["ticker"] or "").strip()]
            if tag_list:
                for tk in tag_list:
                    rows.append(
                        {
                            "date": day,
                            "ticker": tk,
                            "source": "daily_note",
                            "text": txt,
                        }
                    )
            else:
                rows.append(
                    {
                        "date": day,
                        "ticker": "",
                        "source": "daily_note",
                        "text": txt,
                    }
                )
        conn.close()
    except Exception:
        pass
    rows.sort(key=lambda x: str(x.get("date") or ""), reverse=True)
    return rows[: max(50, min(2500, int(limit)))]


def organizer_recall_answer(question: str, limit: int = 8) -> tuple[str, list[dict[str, str]]]:
    q = str(question or "").strip()
    if not q:
        return "", []
    toks = [x for x in re.findall(r"[a-z0-9]{3,}", q.lower()) if x not in {"what", "when", "that", "this", "from", "with", "about", "have", "your"}]
    pool = _organizer_memory_rows(limit=1200)
    scored: list[tuple[int, dict[str, str]]] = []
    for r in pool:
        txt = f"{r.get('ticker','')} {r.get('source','')} {r.get('text','')}".lower()
        score = sum(1 for t in toks if t in txt)
        if score > 0:
            scored.append((score, r))
    scored.sort(key=lambda x: (-x[0], str(x[1].get("date") or "")), reverse=False)
    top = [r for _s, r in scored[: max(1, min(20, int(limit) * 2))]]
    if not top:
        return "No matching memory found yet.", []
    if _hybrid_ask_ai is not None:
        snippets = []
        for i, r in enumerate(top[:12], start=1):
            snippets.append(
                f"[{i}] date={r.get('date','-')} ticker={r.get('ticker','-')} source={r.get('source','-')} text={str(r.get('text',''))[:500]}"
            )
        system = (
            "You are a memory recall assistant. Use ONLY the provided snippets. "
            "Answer in 2-4 bullets and cite snippet date(s) inline."
        )
        prompt = "Question:\n" + q + "\n\nMemory snippets:\n" + "\n".join(snippets)
        try:
            ans = str((_hybrid_ask_ai(prompt, system) or "")).strip()
            if ans:
                return ans[:2400], top[:12]
        except Exception:
            pass
    lines = []
    for r in top[: max(3, min(8, int(limit)))]:
        lines.append(
            f"- {str(r.get('date') or '-')} | {str(r.get('ticker') or '-')} | {str(r.get('text') or '')[:220]}"
        )
    return "Best matches:\n" + "\n".join(lines), top[:8]


def _google_calendar_client_id() -> str:
    return str(os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")).strip()


def _google_calendar_redirect_uri() -> str:
    v = str(os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "")).strip()
    return v or "http://127.0.0.1:8765/organizer/google/callback"


def _google_calendar_scope() -> str:
    return "https://www.googleapis.com/auth/calendar.readonly"


def _google_calendar_token_path() -> Path:
    return DATA / "google_calendar_token.json"


def _google_calendar_enabled() -> bool:
    return bool(_google_calendar_client_id())


def _safe_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _google_load_token() -> dict[str, object]:
    p = _google_calendar_token_path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _google_save_token(data: dict[str, object]) -> None:
    if not isinstance(data, dict):
        return
    _safe_write_json(_google_calendar_token_path(), data)


def _google_disconnect() -> None:
    p = _google_calendar_token_path()
    try:
        if p.exists():
            p.unlink()
    except Exception:
        pass


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _google_oauth_authorize_url(return_to: str = "/organizer") -> str:
    client_id = _google_calendar_client_id()
    redirect_uri = _google_calendar_redirect_uri()
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url_no_pad(hashlib.sha256(verifier.encode("utf-8")).digest())
    now = time.time()
    with GOOGLE_OAUTH_LOCK:
        GOOGLE_OAUTH_STATE[state] = {
            "verifier": verifier,
            "created_at": now,
            "return_to": return_to if str(return_to or "").startswith("/") else "/organizer",
        }
        # Keep memory small and avoid stale state replay.
        stale = [k for k, v in GOOGLE_OAUTH_STATE.items() if float(v.get("created_at") or 0.0) < (now - 900)]
        for k in stale:
            GOOGLE_OAUTH_STATE.pop(k, None)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": _google_calendar_scope(),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)


def _google_exchange_code(code: str, state: str) -> tuple[bool, str]:
    c = str(code or "").strip()
    st = str(state or "").strip()
    if not c or not st:
        return False, "Missing OAuth code/state."
    with GOOGLE_OAUTH_LOCK:
        saved = dict(GOOGLE_OAUTH_STATE.pop(st, {}) or {})
    verifier = str(saved.get("verifier") or "")
    created_at = float(saved.get("created_at") or 0.0)
    if not verifier or created_at <= 0 or (time.time() - created_at) > 900:
        return False, "Expired or invalid OAuth state. Please reconnect."
    client_id = _google_calendar_client_id()
    redirect_uri = _google_calendar_redirect_uri()
    payload = urllib.parse.urlencode(
        {
            "code": c,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=18) as r:
            body = json.loads(r.read().decode("utf-8", errors="ignore") or "{}")
    except Exception as e:
        return False, f"Google token exchange failed: {str(e)[:180]}"
    if not isinstance(body, dict):
        return False, "Google token response was invalid."
    access_token = str(body.get("access_token") or "").strip()
    if not access_token:
        return False, "Google did not return an access token."
    old = _google_load_token()
    refresh_token = str(body.get("refresh_token") or old.get("refresh_token") or "").strip()
    expires_in = int(float(body.get("expires_in") or 3600))
    token_data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "scope": str(body.get("scope") or _google_calendar_scope()),
        "token_type": str(body.get("token_type") or "Bearer"),
        "expires_at": int(time.time()) + max(60, expires_in - 30),
        "updated_at": dt.datetime.now().isoformat(),
    }
    _google_save_token(token_data)
    return True, "Calendar connected."


def _google_refresh_access_token() -> tuple[bool, str]:
    tok = _google_load_token()
    refresh_token = str(tok.get("refresh_token") or "").strip()
    client_id = _google_calendar_client_id()
    if not refresh_token or not client_id:
        return False, "Calendar is not connected."
    payload = urllib.parse.urlencode(
        {
            "refresh_token": refresh_token,
            "client_id": client_id,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=18) as r:
            body = json.loads(r.read().decode("utf-8", errors="ignore") or "{}")
    except Exception as e:
        return False, f"Google token refresh failed: {str(e)[:180]}"
    if not isinstance(body, dict):
        return False, "Google token refresh response was invalid."
    access_token = str(body.get("access_token") or "").strip()
    if not access_token:
        return False, "Google token refresh returned no access token."
    expires_in = int(float(body.get("expires_in") or 3600))
    tok["access_token"] = access_token
    tok["token_type"] = str(body.get("token_type") or tok.get("token_type") or "Bearer")
    tok["scope"] = str(body.get("scope") or tok.get("scope") or _google_calendar_scope())
    tok["expires_at"] = int(time.time()) + max(60, expires_in - 30)
    tok["updated_at"] = dt.datetime.now().isoformat()
    _google_save_token(tok)
    return True, "OK"


def _google_access_token() -> tuple[str, str]:
    if not _google_calendar_enabled():
        return "", "Google Calendar is not configured. Set GOOGLE_OAUTH_CLIENT_ID."
    tok = _google_load_token()
    access = str(tok.get("access_token") or "").strip()
    exp = int(float(tok.get("expires_at") or 0))
    if access and exp > int(time.time() + 30):
        return access, "OK"
    ok, msg = _google_refresh_access_token()
    if not ok:
        return "", msg
    tok2 = _google_load_token()
    return str(tok2.get("access_token") or "").strip(), "OK"


def _google_list_events(day_s: str, max_results: int = 20) -> tuple[list[dict[str, str]], str]:
    if not _google_calendar_enabled():
        return [], "not_configured"
    d = str(day_s or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        d = dt.date.today().isoformat()
    now = time.time()
    if (
        str(GOOGLE_CAL_CACHE.get("day") or "") == d
        and (now - float(GOOGLE_CAL_CACHE.get("ts") or 0.0)) <= 60.0
    ):
        rows = GOOGLE_CAL_CACHE.get("items")
        status = str(GOOGLE_CAL_CACHE.get("status") or "unknown")
        if isinstance(rows, list):
            out = [x for x in rows if isinstance(x, dict)]
            return out, status
    access, status = _google_access_token()
    if not access:
        GOOGLE_CAL_CACHE.update({"ts": now, "day": d, "items": [], "status": "disconnected"})
        return [], status
    start = f"{d}T00:00:00Z"
    end = f"{d}T23:59:59Z"
    params = urllib.parse.urlencode(
        {
            "timeMin": start,
            "timeMax": end,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": str(max(1, min(80, int(max_results)))),
        }
    )
    url = "https://www.googleapis.com/calendar/v3/calendars/primary/events?" + params
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = json.loads(r.read().decode("utf-8", errors="ignore") or "{}")
    except Exception as e:
        msg = f"Calendar fetch failed: {str(e)[:180]}"
        GOOGLE_CAL_CACHE.update({"ts": now, "day": d, "items": [], "status": msg})
        return [], msg
    items = body.get("items") if isinstance(body, dict) else []
    rows: list[dict[str, str]] = []
    if isinstance(items, list):
        for it in items[: max(1, min(80, int(max_results)))]:
            if not isinstance(it, dict):
                continue
            start_obj = it.get("start") if isinstance(it.get("start"), dict) else {}
            when = str((start_obj.get("dateTime") if isinstance(start_obj, dict) else "") or (start_obj.get("date") if isinstance(start_obj, dict) else "") or "-")
            rows.append(
                {
                    "when": when,
                    "title": str(it.get("summary") or "(No title)"),
                    "location": str(it.get("location") or ""),
                }
            )
    GOOGLE_CAL_CACHE.update({"ts": now, "day": d, "items": rows, "status": "connected"})
    return rows, "connected"


def organizer_html(message: str = "", query: str = "", day: str = "", recall_query: str = "") -> str:
    msg_html = f"<div class='flash'>{html.escape(message)}</div>" if message else ""
    q = str(query or "").strip().lower()
    today_s = dt.date.today().isoformat()
    day_s = str(day or today_s).strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day_s):
        day_s = today_s
    daily_entry = get_daily_note_entry(day_s)
    daily_note = str(daily_entry.get("content") or "")
    daily_locked = int(daily_entry.get("locked") or 0) == 1
    daily_tags_txt = str(daily_entry.get("tickers") or "")
    daily_tags = [x for x in daily_tags_txt.split(",") if x.strip()]
    recall_q = str(recall_query or "").strip()
    recall_a, recall_rows = organizer_recall_answer(recall_q, limit=8) if recall_q else ("", [])
    gcal_token = _google_load_token()
    gcal_connected = bool(str(gcal_token.get("refresh_token") or gcal_token.get("access_token") or "").strip())
    gcal_events, gcal_state = _google_list_events(day_s, max_results=14) if gcal_connected else ([], "disconnected")

    todos = list_todos(limit=1200, include_archived=False)
    notes_general = list_investor_notes(limit=500)
    notes_company = list_workspace_journal_all(limit=500)
    rituals_open = list_workspace_ritual_tasks(limit=240, status="open")
    reminders = list_company_reminders_all(limit=600)

    def _matches(row_txt: str) -> bool:
        return (not q) or (q in row_txt.lower())

    open_tasks = [r for r in todos if str(r["status"] or "").lower() == "open"]
    done_tasks = [r for r in todos if str(r["status"] or "").lower() == "done"][:120]
    near_s = (dt.date.today() + dt.timedelta(days=3)).isoformat()

    pri_rank = {"P1": 0, "P2": 1, "P3": 2}
    def _task_key(r: sqlite3.Row) -> tuple[int, int, str, int]:
        p = str(r["priority"] or "P2").upper()
        due = str(r["due_date"] or "")
        due_rank = 0 if (due and due <= today_s) else (1 if due else 2)
        return (pri_rank.get(p, 1), due_rank, due or "9999-12-31", -int(r["id"]))
    open_tasks.sort(key=_task_key)
    open_tasks = [r for r in open_tasks if _matches(f"{r['task']} {r['ticker']} {r['category']}")]
    today_focus = open_tasks[:7]
    backlog = open_tasks[7:400]
    done_tasks = [r for r in done_tasks if _matches(f"{r['task']} {r['ticker']} {r['category']}")]

    today_rituals = [r for r in rituals_open if str(r["due_date"] or "") == today_s]
    upcoming_rituals = [r for r in rituals_open if today_s < str(r["due_date"] or "") <= near_s][:80]
    open_reminders = [r for r in reminders if str(r["status"] or "").lower() == "open"]
    open_reminders = [r for r in open_reminders if _matches(f"{r['ticker']} {r['note']} {r['remind_at']}")][:180]

    note_rows: list[tuple[str, str, str, str, str]] = []
    for r in notes_general:
        note_rows.append((str(r["created_at"] or ""), "General", str(r["ticker"] or "").strip().upper(), str(r["scope"] or "general"), str(r["note"] or "")))
    for r in notes_company:
        note_rows.append((str(r["created_at"] or ""), "Company", str(r["ticker"] or "").strip().upper(), str(r["action"] or "note"), str(r["note"] or "")))
    note_rows = [x for x in note_rows if _matches(" ".join(x))]
    note_rows.sort(key=lambda x: x[0], reverse=True)
    note_rows = note_rows[:250]

    def _task_rows_html(rows: list[sqlite3.Row], include_actions: bool) -> str:
        if not rows:
            col = "5" if include_actions else "4"
            return f"<tr><td colspan='{col}' class='muted'>No items.</td></tr>"
        out: list[str] = []
        for r in rows:
            tk = str(r["ticker"] or "").strip().upper()
            tk_cell = f"<a href='/company_file?t={html.escape(tk)}'>{html.escape(tk)}</a>" if tk else "-"
            row = (
                "<tr>"
                f"<td>{html.escape(str(r['task'] or ''))}</td>"
                f"<td>{tk_cell}</td>"
                f"<td>{html.escape(str(r['category'] or 'general'))}</td>"
                f"<td>{html.escape(str(r['due_date'] or '-'))}</td>"
            )
            if include_actions:
                row += (
                    "<td>"
                    "<form method='post' action='/todo/toggle' style='display:inline;margin-right:6px;'>"
                    f"<input type='hidden' name='id' value='{int(r['id'])}'>"
                    "<input type='hidden' name='source' value='organizer'>"
                    "<button type='submit'>Done</button></form>"
                    "<form method='post' action='/todo/delete' style='display:inline;'>"
                    f"<input type='hidden' name='id' value='{int(r['id'])}'>"
                    "<input type='hidden' name='source' value='organizer'>"
                    "<button type='submit'>Delete</button></form>"
                    "</td>"
                )
            row += "</tr>"
            out.append(row)
        return "".join(out)

    note_html = "".join(
        f"<tr><td>{html.escape(ts or '-')}</td><td>{html.escape(kind)}</td><td>{html.escape(tk or '-')}</td><td>{html.escape(tag)}</td><td>{html.escape(txt)}</td></tr>"
        for ts, kind, tk, tag, txt in note_rows
    ) or "<tr><td colspan='5' class='muted'>No notes yet.</td></tr>"
    ritual_today_html = "".join(
        f"<tr><td>{html.escape(str(r['due_date'] or today_s))}</td><td>{html.escape(str(r['title'] or '-'))}</td></tr>"
        for r in today_rituals
    ) or "<tr><td colspan='2' class='muted'>No rituals due today.</td></tr>"
    ritual_upcoming_html = "".join(
        f"<tr><td>{html.escape(str(r['due_date'] or '-'))}</td><td>{html.escape(str(r['title'] or '-'))}</td></tr>"
        for r in upcoming_rituals
    ) or "<tr><td colspan='2' class='muted'>No rituals in next 3 days.</td></tr>"
    reminders_html = "".join(
        f"<tr><td>{html.escape(str(r['remind_at'] or '-'))}</td><td>{html.escape(str(r['ticker'] or '-'))}</td><td>{html.escape(str(r['note'] or ''))}</td></tr>"
        for r in open_reminders
    ) or "<tr><td colspan='3' class='muted'>No open reminders.</td></tr>"
    cal_events_html = "".join(
        f"<tr><td>{html.escape(str(r.get('when') or '-'))}</td><td>{html.escape(str(r.get('title') or '-'))}</td><td>{html.escape(str(r.get('location') or '-'))}</td></tr>"
        for r in gcal_events
    ) or "<tr><td colspan='3' class='muted'>No events found for this day.</td></tr>"
    recall_sources_html = "".join(
        f"<tr><td>{html.escape(str(r.get('date') or '-'))}</td><td>{html.escape(str(r.get('ticker') or '-'))}</td><td>{html.escape(str(r.get('source') or '-'))}</td><td>{html.escape(str(r.get('text') or '')[:320])}</td></tr>"
        for r in recall_rows[:12]
    ) if recall_rows else ""
    if not _google_calendar_enabled():
        cal_status_html = "<div class='muted' style='margin:6px 0;'>Calendar not configured yet. Add <code>GOOGLE_OAUTH_CLIENT_ID</code> in <code>.env</code>.</div>"
        cal_action_html = ""
    elif not gcal_connected:
        cal_status_html = "<div class='muted' style='margin:6px 0;'>Calendar disconnected.</div>"
        cal_action_html = (
            "<a class='btn' href='/organizer/google/connect?return_to=/organizer'>Connect Google Calendar</a>"
        )
    else:
        state_label = "Connected" if gcal_state == "connected" else html.escape(gcal_state)
        cal_status_html = f"<div class='muted' style='margin:6px 0;'>Status: {state_label}</div>"
        cal_action_html = (
            "<form method='post' action='/organizer/google/disconnect' style='display:inline;'>"
            f"<input type='hidden' name='day' value='{html.escape(day_s)}'>"
            f"<input type='hidden' name='q' value='{html.escape(str(query or ''))}'>"
            "<button type='submit' style='background:#edf2f6;color:#2f4358;border:1px solid #c8d3dd;'>Disconnect</button>"
            "</form>"
        )

    return (
        "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Organizer</title>"
        "<style>body{margin:0;background:#eaf0f4;color:#2f4358;font-family:'Avenir Next','Helvetica Neue',sans-serif;}"
        ".wrap{max-width:1260px;margin:0 auto;padding:14px;} .card{background:#f1f5f8;border:1px solid #d6dee6;border-radius:10px;padding:12px;margin-bottom:10px;}"
        ".btn{border:1px solid #c8d3dd;background:#edf2f6;color:#2f4358;border-radius:8px;padding:6px 10px;text-decoration:none;font-size:12px;font-weight:700;}"
        "input,textarea,select{width:100%;box-sizing:border-box;border:1px solid #c8d3dd;border-radius:8px;background:#edf2f6;color:#2f4358;padding:8px;}"
        "button{border:1px solid #ff7a59;background:#ff7a59;color:#fff;border-radius:8px;padding:8px 10px;font-weight:700;cursor:pointer;}"
        "table{width:100%;border-collapse:collapse;} th,td{border-bottom:1px solid #d6dee6;padding:8px;text-align:left;vertical-align:top;} .muted{color:#4f6780;} .flash{margin-bottom:10px;padding:8px;border:1px solid #9fbad0;border-radius:8px;background:#edf2f6;}"
        ".grid{display:grid;grid-template-columns:1fr;gap:10px;} .stats{display:flex;gap:8px;flex-wrap:wrap;} .chip{padding:4px 10px;border-radius:999px;border:1px solid #c8d3dd;background:#edf2f6;font-size:12px;font-weight:700;} @media(min-width:1020px){.grid{grid-template-columns:1fr 1fr;}}</style>"
        "</head><body><div class='wrap'>"
        "<div class='card'><a class='btn' href='/'>Home</a> <a class='btn' href='/company_file'>Company Files</a> <strong style='margin-left:8px;'>Organizer</strong></div>"
        f"{msg_html}"
        "<div class='card'><h3 style='margin:0 0 8px 0;'>Today Focus</h3>"
        f"<div class='stats'><span class='chip'>Open Tasks: {len(open_tasks)}</span><span class='chip'>Today Focus: {len(today_focus)}</span><span class='chip'>Notes: {len(note_rows)}</span><span class='chip'>Open Reminders: {len(open_reminders)}</span></div>"
        "<form method='get' action='/organizer' style='display:grid;grid-template-columns:1fr auto;gap:8px;margin-top:8px;'>"
        f"<input name='q' value='{html.escape(str(query or ''))}' placeholder='Search tasks, notes, tickers...'>"
        "<button type='submit'>Search</button></form></div>"
        "<div class='grid'>"
        "<div class='card'><h3 style='margin:0 0 8px 0;'>Daily Note</h3>"
        "<form method='post' action='/organizer/daily_save'>"
        f"<input type='hidden' name='q' value='{html.escape(str(query or ''))}'>"
        f"<input name='day' value='{html.escape(day_s)}' type='date' style='max-width:220px;margin-bottom:8px;'>"
        f"<textarea name='content' rows='10' placeholder='Fresh page for the day...' {'readonly' if daily_locked else ''}>{html.escape(daily_note)}</textarea>"
        + (
            f"<div class='muted' style='margin-top:6px;'>Auto tags: {', '.join(html.escape(x) for x in daily_tags)}</div>"
            if daily_tags
            else ""
        )
        + (
            "<div class='muted' style='margin-top:6px;'>This day is locked and archived.</div>"
            if daily_locked
            else ""
        )
        + "<div style='margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;'>"
        + ("" if daily_locked else "<button type='submit'>Save Daily Note</button>")
        + (
            ""
            if daily_locked
            else "<button type='submit' formaction='/organizer/day_close' formmethod='post'>End Day</button>"
        )
        + "</div>"
        "</form></div>"
        "<div class='card'><h3 style='margin:0 0 8px 0;'>AI Recall</h3>"
        "<form method='post' action='/organizer/recall' style='display:grid;grid-template-columns:1fr auto;gap:8px;'>"
        f"<input type='hidden' name='q' value='{html.escape(str(query or ''))}'>"
        f"<input type='hidden' name='day' value='{html.escape(day_s)}'>"
        f"<input id='recallQuestion' name='question' value='{html.escape(recall_q)}' placeholder='Why did I buy Company X?'>"
        "<button type='submit'>Recall</button></form>"
        + (f"<div class='card' style='margin-top:8px;'><pre style='white-space:pre-wrap;margin:0;'>{html.escape(recall_a)}</pre></div>" if recall_q else "")
        + (f"<details style='margin-top:8px;'><summary>Sources</summary><table><thead><tr><th>Date</th><th>Ticker</th><th>Source</th><th>Snippet</th></tr></thead><tbody>{recall_sources_html}</tbody></table></details>" if recall_sources_html else "")
        + "</div>"
        "</div>"
        "<div class='card'><h3 style='margin:0 0 8px 0;'>Quick Add</h3>"
        "<form method='post' action='/quick_capture' style='display:grid;grid-template-columns:150px 1fr auto auto;gap:8px;'>"
        f"<input type='hidden' name='return_to' value='/organizer?q={urllib.parse.quote(str(query or ''))}&day={urllib.parse.quote(day_s)}'>"
        "<input name='ticker' placeholder='Ticker (optional)'>"
        "<input name='text' placeholder='Write note or task'>"
        "<button type='submit' name='mode' value='note'>Save Note</button>"
        "<button type='submit' name='mode' value='task'>Save Task</button>"
        "</form></div>"
        "<div class='card'><h3 style='margin:0 0 8px 0;'>Priority Tasks</h3><table><thead><tr><th>Task</th><th>Ticker</th><th>Type</th><th>Due</th><th>Action</th></tr></thead><tbody>"
        f"{_task_rows_html(today_focus, include_actions=True)}</tbody></table></div>"
        "<div class='grid'>"
        "<div class='card'><h3 style='margin:0 0 8px 0;'>Backlog</h3><table><thead><tr><th>Task</th><th>Ticker</th><th>Type</th><th>Due</th><th>Action</th></tr></thead><tbody>"
        f"{_task_rows_html(backlog, include_actions=True)}</tbody></table></div>"
        "<div class='card'><h3 style='margin:0 0 8px 0;'>Schedule</h3>"
        "<div class='muted' style='margin-bottom:6px;'>Google Calendar</div>"
        f"{cal_status_html}"
        f"{cal_action_html}"
        "<div class='muted' style='margin:10px 0 6px 0;'>Calendar events</div>"
        f"<table><thead><tr><th>When</th><th>Title</th><th>Location</th></tr></thead><tbody>{cal_events_html}</tbody></table>"
        "<div class='muted' style='margin-bottom:6px;'>Today rituals</div>"
        f"<table><thead><tr><th>Date</th><th>Item</th></tr></thead><tbody>{ritual_today_html}</tbody></table>"
        "<div class='muted' style='margin:10px 0 6px 0;'>Next 3 days rituals</div>"
        f"<table><thead><tr><th>Date</th><th>Item</th></tr></thead><tbody>{ritual_upcoming_html}</tbody></table>"
        "<div class='muted' style='margin:10px 0 6px 0;'>Open reminders</div>"
        f"<table><thead><tr><th>When</th><th>Ticker</th><th>Reminder</th></tr></thead><tbody>{reminders_html}</tbody></table></div>"
        "</div>"
        "<div class='card'><h3 style='margin:0 0 8px 0;'>All Notes</h3><table><thead><tr><th>Date</th><th>Kind</th><th>Ticker</th><th>Tag</th><th>Note</th></tr></thead><tbody>"
        f"{note_html}</tbody></table></div>"
        "<div class='card'><details><summary style='cursor:pointer;font-weight:700;'>Done Tasks (Recent)</summary><table><thead><tr><th>Task</th><th>Ticker</th><th>Type</th><th>Due</th></tr></thead><tbody>"
        f"{_task_rows_html(done_tasks, include_actions=False)}</tbody></table></details></div>"
        "<script>(function(){document.addEventListener('keydown',function(e){if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();var d=document.getElementById('recallOverlay');if(d){d.style.display='block';var i=document.getElementById('recallOverlayInput');if(i){i.focus();i.select();}}}});var c=document.getElementById('recallOverlayClose');if(c){c.addEventListener('click',function(){var d=document.getElementById('recallOverlay');if(d)d.style.display='none';});}})();</script>"
        "<div id='recallOverlay' style='display:none;position:fixed;inset:0;background:rgba(0,0,0,.35);z-index:10000;'>"
        "<div style='max-width:760px;margin:12vh auto;background:#f1f5f8;border:1px solid #d6dee6;border-radius:12px;padding:12px;'>"
        "<div style='display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;'><strong>AI Recall</strong><button id='recallOverlayClose' type='button' style='background:#edf2f6;color:#2f4358;border:1px solid #c8d3dd;'>Close</button></div>"
        "<form method='post' action='/organizer/recall' style='display:grid;grid-template-columns:1fr auto;gap:8px;'>"
        f"<input type='hidden' name='q' value='{html.escape(str(query or ''))}'>"
        f"<input type='hidden' name='day' value='{html.escape(day_s)}'>"
        "<input id='recallOverlayInput' name='question' placeholder='Ask memory: Why did I buy Company X?'>"
        "<button type='submit'>Recall</button></form>"
        "</div></div>"
        + _quick_capture_widget_html(return_to=f"/organizer?q={urllib.parse.quote(str(query or ''))}&day={urllib.parse.quote(day_s)}", current_ticker="")
        + "</div></body></html>"
    )


def company_file_html(
    ticker: str,
    message: str = "",
    query: str = "",
    timeline_filter: str = "all",
    page: int = 1,
    page_size: int = 80,
    sort_by: str = "mcap_desc",
    industry_filter: str = "",
    industry_mode: str = "include",
    index_filter: str = "",
    list_filter: str = "",
    moat_filter: str = "",
) -> str:
    t = resolve_ticker_input(ticker)
    q = (query or "").strip()
    tf = (timeline_filter or "all").strip().lower()
    if tf not in {"all", "notes", "watchlist", "portfolio"}:
        tf = "all"
    ps = max(20, min(300, int(page_size)))
    pg = max(1, int(page))
    if sort_by not in {"name_asc", "mcap_desc", "mcap_asc"}:
        sort_by = "name_asc"
    ind = str(industry_filter or "").strip()
    ind_mode = "exclude" if str(industry_mode or "").strip().lower() == "exclude" else "include"
    idx_filter = str(index_filter or "").strip().lower()
    idx_map = {str(x.get("id") or ""): x for x in _index_presets()}
    if idx_filter not in idx_map:
        idx_filter = ""
    list_rows = list_company_lists(limit=300)
    list_names = [str(r["name"] or "") for r in list_rows if str(r["name"] or "").strip()]
    list_name_set = {_norm_list_name(x) for x in list_names}
    lst_filter = _norm_list_name(list_filter)
    if lst_filter and lst_filter not in list_name_set:
        lst_filter = ""
    moat_filter_key = _normalize_moat_key(moat_filter)
    base_universe = _company_file_universe(limit=5000)
    idx_tickers = _company_file_index_tickers(idx_filter) if idx_filter else []
    lst_tickers = _company_file_list_tickers(lst_filter) if lst_filter else []
    moat_filter_tickers = moat_tickers(moat_filter_key, limit=10000) if moat_filter_key else []
    if idx_filter:
        idx_set = set(idx_tickers)
        base_universe = [x for x in base_universe if x in idx_set]
    if lst_filter:
        lst_set = set(lst_tickers)
        base_universe = [x for x in base_universe if x in lst_set]
    if moat_filter_key:
        moat_set = set(moat_filter_tickers)
        base_universe = [x for x in base_universe if x in moat_set]
    if not base_universe and (idx_filter or lst_filter or moat_filter_key):
        if idx_filter and lst_filter and moat_filter_key:
            base_universe = sorted(set(idx_tickers).intersection(set(lst_tickers)).intersection(set(moat_filter_tickers)))
        elif idx_filter and lst_filter:
            base_universe = sorted(set(idx_tickers).intersection(set(lst_tickers)))
        elif idx_filter and moat_filter_key:
            base_universe = sorted(set(idx_tickers).intersection(set(moat_filter_tickers)))
        elif lst_filter and moat_filter_key:
            base_universe = sorted(set(lst_tickers).intersection(set(moat_filter_tickers)))
        elif idx_filter:
            base_universe = list(idx_tickers)
        elif moat_filter_key:
            base_universe = list(moat_filter_tickers)
        else:
            base_universe = list(lst_tickers)
    msg_html = f"<div class='flash'>{html.escape(message)}</div>" if message else ""

    def _company_file_href(
        *,
        qv: str | None = None,
        sort_v: str | None = None,
        ind_v: str | None = None,
        ind_mode_v: str | None = None,
        page_v: int | None = None,
        page_size_v: int | None = None,
        idx_v: str | None = None,
        list_v: str | None = None,
        moat_v: str | None = None,
    ) -> str:
        params: list[tuple[str, str]] = [
            ("q", q if qv is None else qv),
            ("sort", sort_by if sort_v is None else sort_v),
            ("industry", ind if ind_v is None else ind_v),
            ("industry_mode", ind_mode if ind_mode_v is None else ind_mode_v),
            ("page", str(pg if page_v is None else page_v)),
            ("page_size", str(ps if page_size_v is None else page_size_v)),
        ]
        chosen_idx = idx_filter if idx_v is None else idx_v
        chosen_list = lst_filter if list_v is None else list_v
        chosen_moat = moat_filter_key if moat_v is None else _normalize_moat_key(moat_v or "")
        if chosen_idx:
            params.append(("index", chosen_idx))
        if chosen_list:
            params.append(("list", chosen_list))
        if chosen_moat:
            params.append(("moat", chosen_moat))
        return "/company_file?" + urllib.parse.urlencode(params)

    search_rows: list[dict[str, str]] = []
    search_total = 0
    industry_options: list[str] = []
    if not t or bool(q):
        search_rows, search_total, industry_options = _company_file_search_results(
            q,
            limit=ps,
            offset=(pg - 1) * ps,
            sort_by=sort_by,
            industry_filter=ind,
            industry_mode=ind_mode,
            universe_tickers=base_universe,
        )
    pg_max = max(1, (search_total + ps - 1) // ps) if search_total > 0 else 1
    if pg > pg_max:
        pg = pg_max
        if not t or bool(q):
            search_rows, search_total, industry_options = _company_file_search_results(
                q,
                limit=ps,
                offset=(pg - 1) * ps,
                sort_by=sort_by,
                industry_filter=ind,
                industry_mode=ind_mode,
                universe_tickers=base_universe,
            )
    prev_href = _company_file_href(page_v=pg - 1) if (pg > 1 and (not t or bool(q))) else ""
    next_href = _company_file_href(page_v=pg + 1) if (pg < pg_max and (not t or bool(q))) else ""
    search_rows_html: list[str] = []
    search_db_names = _company_name_db_map([str(r.get("ticker") or "").upper().strip() for r in search_rows])
    for r in search_rows:
        row_ticker = str(r.get("ticker") or "")
        row_name = str(r.get("name") or "").strip()
        if _company_name_is_placeholder(row_name, row_ticker):
            db_name = _prettify_company_name(str(search_db_names.get(row_ticker.upper().strip()) or ""), row_ticker)
            if db_name:
                row_name = db_name
        if not row_name:
            row_name = row_ticker or "-"
        row_country = str(r.get("country") or "-")
        row_industry = str(r.get("industry") or "Unknown")
        row_mcap = str(r.get("mcap") or "-")
        row_href = (
            f"/company_file?t={urllib.parse.quote(row_ticker)}&q={urllib.parse.quote(q)}"
            f"&sort={urllib.parse.quote(sort_by)}&industry={urllib.parse.quote(ind)}&industry_mode={urllib.parse.quote(ind_mode)}"
            f"&index={urllib.parse.quote(idx_filter)}&list={urllib.parse.quote(lst_filter)}&moat={urllib.parse.quote(moat_filter_key)}"
        )
        search_rows_html.append(
            "<tr>"
            f"<td><a class='co-name' href='{html.escape(row_href)}'>{html.escape(row_name)}</a><div><span class='tk-chip'>{html.escape(row_ticker or '-')}</span></div></td>"
            f"<td>{html.escape(row_country)}</td>"
            f"<td>{html.escape(row_industry)}</td>"
            f"<td>{html.escape(row_mcap)}</td>"
            "</tr>"
        )
    search_html = "".join(search_rows_html) or "<tr><td colspan='4' class='muted'>Type a company name or ticker and click Search.</td></tr>"
    if not t:
        page_links: list[str] = []
        mcap_next_sort = "mcap_asc" if sort_by == "mcap_desc" else "mcap_desc"
        mcap_arrow = "↓" if sort_by == "mcap_desc" else ("↑" if sort_by == "mcap_asc" else "")
        mcap_hdr_href = _company_file_href(sort_v=mcap_next_sort, page_v=1)
        ind_mode_next = "exclude" if ind_mode == "include" else "include"
        ind_mode_label = "Include" if ind_mode == "include" else "Exclude"
        ind_arrow = "↓" if ind_mode == "include" else "↑"
        industry_hdr_href = _company_file_href(ind_mode_v=ind_mode_next, page_v=1)
        if pg_max > 1:
            start_pg = max(1, pg - 2)
            end_pg = min(pg_max, pg + 2)
            if start_pg > 1:
                page_links.append(f"<a class='btn' href='{html.escape(_company_file_href(page_v=1))}'>1</a>")
                if start_pg > 2:
                    page_links.append("<span class='muted'>...</span>")
            for pno in range(start_pg, end_pg + 1):
                if pno == pg:
                    page_links.append(f"<span class='btn' style='background:#ff7a59;color:#fff;border-color:#ff7a59;'>{pno}</span>")
                else:
                    page_links.append(f"<a class='btn' href='{html.escape(_company_file_href(page_v=pno))}'>{pno}</a>")
            if end_pg < pg_max:
                if end_pg < pg_max - 1:
                    page_links.append("<span class='muted'>...</span>")
                page_links.append(f"<a class='btn' href='{html.escape(_company_file_href(page_v=pg_max))}'>{pg_max}</a>")
        index_chips = [f"<a class='btn' href='{html.escape(_company_file_href(idx_v='', page_v=1))}'>All Indices</a>"]
        hidden_index_labels: list[str] = []
        for x in _index_presets():
            xid = str(x.get("id") or "").strip().lower()
            if not xid:
                continue
            count = len(_company_file_index_tickers(xid))
            active = xid == idx_filter
            if count < 10 and not active:
                hidden_index_labels.append(str(x.get("name") or xid))
                continue
            label = f"{str(x.get('name') or xid)} ({count})"
            style = " style='background:#ff7a59;color:#fff;border-color:#ff7a59;'" if active else ""
            index_chips.append(f"<a class='btn' href='{html.escape(_company_file_href(idx_v=xid, page_v=1))}'{style}>{html.escape(label)}</a>")
        list_chips = [f"<a class='btn' href='{html.escape(_company_file_href(list_v='', page_v=1))}'>All Lists</a>"]
        for r in list_rows[:20]:
            lname = str(r["name"] or "").strip()
            if not lname:
                continue
            active = lname == lst_filter
            label = f"{lname} ({int(r['item_count'] or 0)})"
            style = " style='background:#ff7a59;color:#fff;border-color:#ff7a59;'" if active else ""
            list_chips.append(f"<a class='btn' href='{html.escape(_company_file_href(list_v=lname, page_v=1))}'{style}>{html.escape(label)}</a>")
        moat_counts_map = moat_counts()
        moat_chips = [f"<a class='btn' href='{html.escape(_company_file_href(moat_v='', page_v=1))}'>All Moats</a>"]
        for mk, label in MOAT_OPTIONS:
            cnt = int(moat_counts_map.get(mk, 0) or 0)
            if cnt <= 0 and mk != moat_filter_key:
                continue
            active = mk == moat_filter_key
            style = " style='background:#ff7a59;color:#fff;border-color:#ff7a59;'" if active else ""
            moat_chips.append(
                f"<a class='btn' href='{html.escape(_company_file_href(moat_v=mk, page_v=1))}'{style}>{html.escape(label)} ({cnt})</a>"
            )
        filter_note = "All companies"
        if idx_filter and idx_filter in idx_map:
            filter_note = f"Index: {str((idx_map.get(idx_filter) or {}).get('name') or idx_filter)}"
        if lst_filter:
            filter_note += f" | List: {lst_filter}"
        if moat_filter_key:
            filter_note += f" | Moat: {MOAT_LABEL_MAP.get(moat_filter_key, moat_filter_key)}"
        industry_opts_html = (
            "<option value=''>All industries</option>"
            + "".join(
                f"<option value='{html.escape(x)}' {'selected' if x == ind else ''}>{html.escape(x)}</option>"
                for x in industry_options
            )
        )
        return (
            "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>Company File Search</title>"
            "<style>body{margin:0;background:#eaf0f4;color:#2f4358;font-family:'Avenir Next','Helvetica Neue',sans-serif;}"
            ".wrap{max-width:980px;margin:0 auto;padding:14px;} .card{background:#f1f5f8;border:1px solid #d6dee6;border-radius:10px;padding:12px;margin-bottom:10px;}"
            ".btn{border:1px solid #c8d3dd;background:#edf2f6;color:#2f4358;border-radius:8px;padding:6px 10px;text-decoration:none;font-size:12px;font-weight:700;}"
            "input,select{width:100%;box-sizing:border-box;border:1px solid #c8d3dd;border-radius:8px;background:#edf2f6;color:#2f4358;padding:8px;}"
            "button{border:1px solid #ff7a59;background:#ff7a59;color:#fff;border-radius:8px;padding:8px 10px;font-weight:700;cursor:pointer;}"
            "table{width:100%;border-collapse:collapse;} th,td{border-bottom:1px solid #d6dee6;padding:10px;text-align:left;vertical-align:top;} .muted{color:#4f6780;} .co-name{font-weight:700;} .tk-chip{display:inline-block;margin-top:4px;padding:2px 8px;border:1px solid #c8d3dd;border-radius:999px;background:#edf2f6;font-size:11px;color:#35506a;} .flash{margin-bottom:10px;padding:8px;border:1px solid #9fbad0;border-radius:8px;background:#edf2f6;}</style>"
            "</head><body><div class='wrap'>"
            "<div class='card'><a class='btn' href='/'>Home</a></div>"
            f"{msg_html}"
            "<div class='card'><h3 style='margin:0 0 8px 0;'>Quick Filters</h3>"
            "<div class='muted' style='margin-bottom:6px;'>Indices</div>"
            f"<div style='display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;'>{''.join(index_chips)}</div>"
            + (
                f"<div class='muted' style='margin-bottom:8px;'>Temporarily hidden (insufficient source data): {html.escape(', '.join(hidden_index_labels[:6]))}</div>"
                if hidden_index_labels
                else ""
            )
            +
            "<div class='muted' style='margin-bottom:6px;'>Saved Lists</div>"
            f"<div style='display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;'>{''.join(list_chips)}</div>"
            "<div class='muted' style='margin-bottom:6px;'>Moats</div>"
            f"<div style='display:flex;gap:6px;flex-wrap:wrap;'>{''.join(moat_chips)}</div>"
            "</div>"
            "<div class='card'><h2 style='margin:0 0 8px 0;'>Company File Search</h2>"
            "<form method='get' action='/company_file' style='display:grid;grid-template-columns:1fr 180px 260px auto;gap:8px;'>"
            f"<input name='q' value='{html.escape(q)}' placeholder='Search ticker or company name (e.g. AAPL or Apple)'>"
            "<select name='industry_mode'>"
            + ("<option value='include' selected>Include</option>" if ind_mode == "include" else "<option value='include'>Include</option>")
            + ("<option value='exclude' selected>Exclude</option>" if ind_mode == "exclude" else "<option value='exclude'>Exclude</option>")
            + "</select>"
            f"<select name='industry'>{industry_opts_html}</select>"
            f"<input type='hidden' name='sort' value='{html.escape(sort_by)}'><input type='hidden' name='page' value='1'><input type='hidden' name='page_size' value='{ps}'><input type='hidden' name='index' value='{html.escape(idx_filter)}'><input type='hidden' name='list' value='{html.escape(lst_filter)}'><input type='hidden' name='moat' value='{html.escape(moat_filter_key)}'>"
            "<button type='submit'>Search</button></form></div>"
            + (
                "<div class='card'>"
                f"<div class='muted'>{html.escape(filter_note)} | Showing {0 if search_total == 0 else ((pg-1)*ps)+1}-{min(pg*ps, search_total)} of {search_total} companies | Page {pg}/{pg_max}</div>"
                + (f"<a class='btn' href='{html.escape(prev_href)}' style='margin-right:6px;'>Prev</a>" if prev_href else "")
                + (f"<a class='btn' href='{html.escape(next_href)}'>Next</a>" if next_href else "")
                + ("<div style='margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;'>" + "".join(page_links) + "</div>" if page_links else "")
                + "</div>"
            )
            +
            "<div class='card'><div class='muted' style='margin-bottom:8px;'>Pick industry in the top filter, then click Industry header to toggle Include/Exclude.</div><table><thead><tr><th>Company</th><th>Country</th>"
            f"<th><a href='{html.escape(industry_hdr_href)}' style='text-decoration:none;color:inherit;'>Industry {html.escape(ind_mode_label)} {html.escape(ind_arrow)}</a></th>"
            f"<th><a href='{html.escape(mcap_hdr_href)}' style='text-decoration:none;color:inherit;'>Market Cap {html.escape(mcap_arrow)}</a></th>"
            "</tr></thead><tbody>"
            f"{search_html}</tbody></table></div>"
            + _quick_capture_widget_html(return_to=_company_file_href(), current_ticker="")
            + "</div></body></html>"
        )
    profiles = get_portfolio_profiles([t]) or {}
    p = profiles.get(t, {}) or {}
    db_name = (_company_name_db_map([t]).get(t, "") if t else "").strip()
    company_name = _prettify_company_name(str(p.get("name") or ""), t) or _prettify_company_name(db_name, t) or "Name unavailable"
    conn = memory_db()
    row = conn.execute("SELECT updated_at FROM workspace_companies WHERE ticker = ?", (t,)).fetchone()
    conn.close()
    updated_at = str((row["updated_at"] if row else "") or "")
    notes = list_workspace_journal(t, limit=300)
    msg_html = f"<div class='flash'>{html.escape(message)}</div>" if message else ""
    in_watch = t in {str(x or "").strip().upper() for x in read_watchlist_tickers()}
    in_port = any((str(r[0] or "").strip().upper() == t) for r in read_portfolio_rows(DATA / "portfolio.csv"))
    status_txt = ("Portfolio" if in_port else "") + (" + Watchlist" if (in_port and in_watch) else ("Watchlist" if in_watch else "Not in active lists"))
    country_txt = str(p.get("country") or "-").strip() or "-"
    industry_txt = str(p.get("industry") or "Unknown").strip() or "Unknown"
    industry_href = (
        f"/company_file?q=&sort=name_asc&industry={urllib.parse.quote(industry_txt)}"
        "&industry_mode=include&page=1&page_size=80"
    )
    mcap_txt = _market_cap_cached_only(t)
    if mcap_txt == "-":
        mcap_txt = _market_cap_for_ticker(t)
    reminders = list_company_reminders(t, limit=240)
    todos = list_company_todos(t, limit=300)
    sec_comp_run = get_company_sec_competitor_run(t)
    sec_comp_rows = list_company_sec_competitors(t, include_hidden=True, limit=120)

    notes_html = "".join(
        f"<tr><td>{html.escape(str(r['created_at'] or ''))}</td><td>{html.escape(str(r['note'] or ''))}</td></tr>"
        for r in notes
    ) or "<tr><td colspan='2' class='muted'>No notes yet.</td></tr>"
    reminders_html = "".join(
        (
            "<tr>"
            f"<td>{html.escape(str(r['remind_at'] or '-'))}</td>"
            f"<td>{html.escape(str(r['note'] or '-'))}</td>"
            f"<td>{html.escape(str(r['status'] or 'open'))}</td>"
            f"<td><form method='post' action='/company_file/reminder_toggle' style='display:inline;'>"
            f"<input type='hidden' name='ticker' value='{html.escape(t)}'>"
            f"<input type='hidden' name='id' value='{int(r['id'])}'>"
            "<button type='submit'>Toggle</button></form></td>"
            "</tr>"
        )
        for r in reminders
    ) or "<tr><td colspan='4' class='muted'>No reminders yet.</td></tr>"
    todos_html = "".join(
        (
            "<tr>"
            f"<td>{html.escape(str(r['task'] or '-'))}</td>"
            f"<td>{html.escape(str(r['due_date'] or '-'))}</td>"
            f"<td>{html.escape(str(r['status'] or 'open'))}</td>"
            "<td>"
            f"<form method='post' action='/company_file/todo_toggle' style='display:inline;margin-right:6px;'>"
            f"<input type='hidden' name='ticker' value='{html.escape(t)}'>"
            f"<input type='hidden' name='id' value='{int(r['id'])}'>"
            "<button type='submit'>Toggle</button></form>"
            f"<form method='post' action='/company_file/todo_delete' style='display:inline;'>"
            f"<input type='hidden' name='ticker' value='{html.escape(t)}'>"
            f"<input type='hidden' name='id' value='{int(r['id'])}'>"
            "<button type='submit'>Delete</button></form>"
            "</td>"
            "</tr>"
        )
        for r in todos
    ) or "<tr><td colspan='4' class='muted'>No tasks yet.</td></tr>"
    company_moat_keys = list_company_moat_keys(t)
    current_moat_chip_html = "".join(
        f"<a href='/company_file?moat={urllib.parse.quote(mk)}' style='display:inline-block;margin:0 6px 6px 0;padding:4px 10px;border-radius:999px;border:1px solid #ff7a59;background:#fff1ec;color:#b5472f;font-weight:700;text-decoration:none;'>{html.escape(MOAT_LABEL_MAP.get(mk, mk))}</a>"
        for mk in sorted(company_moat_keys)
    ) or "<span class='muted'>No moat tags yet.</span>"
    moat_input_html = "".join(
        (
            "<label style='display:flex;align-items:center;gap:8px;padding:4px 0;'>"
            f"<input type='checkbox' name='moat' value='{html.escape(mk)}' style='width:auto;' {'checked' if mk in company_moat_keys else ''}>"
            f"<span>{html.escape(label)}</span>"
            "</label>"
        )
        for mk, label in MOAT_OPTIONS
    )
    sec_comp_rows_html = "".join(
        (
            "<tr>"
            "<td>"
            f"<form method='post' action='/company_file/competitor_update' style='display:grid;grid-template-columns:120px 1fr auto auto;gap:6px;align-items:center;'>"
            f"<input type='hidden' name='ticker' value='{html.escape(t)}'>"
            f"<input type='hidden' name='id' value='{int(r['id'])}'>"
            f"<input name='competitor_ticker' value='{html.escape(str(r['competitor_ticker'] or ''))}' placeholder='Ticker'>"
            f"<input name='competitor_name' value='{html.escape(str(r['competitor_name'] or ''))}' placeholder='Company name'>"
            "<button type='submit'>Save</button>"
            "</form>"
            "</td>"
            "<td style='white-space:nowrap;'>"
            f"<form method='post' action='/company_file/competitor_remove' style='display:inline;'>"
            f"<input type='hidden' name='ticker' value='{html.escape(t)}'>"
            f"<input type='hidden' name='id' value='{int(r['id'])}'>"
            "<button type='submit'>Remove</button>"
            "</form>"
            "</td>"
            "</tr>"
        )
        for r in sec_comp_rows
    ) or "<tr><td colspan='2' class='muted'>No competitors yet.</td></tr>"
    sec_comp_note = html.escape(str(sec_comp_run.get("note") or ""))

    return (
        "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(company_name)} File</title>"
        "<style>body{margin:0;background:#eaf0f4;color:#2f4358;font-family:'Avenir Next','Helvetica Neue',sans-serif;}"
        ".wrap{max-width:1200px;margin:0 auto;padding:14px;} .card{background:#f1f5f8;border:1px solid #d6dee6;border-radius:10px;padding:12px;margin-bottom:10px;}"
        ".btn{border:1px solid #c8d3dd;background:#edf2f6;color:#2f4358;border-radius:8px;padding:6px 10px;text-decoration:none;font-size:12px;font-weight:700;}"
        "input,textarea,select{width:100%;box-sizing:border-box;border:1px solid #c8d3dd;border-radius:8px;background:#edf2f6;color:#2f4358;padding:8px;}"
        "button{border:1px solid #ff7a59;background:#ff7a59;color:#fff;border-radius:8px;padding:8px 10px;font-weight:700;cursor:pointer;}"
        "table{width:100%;border-collapse:collapse;} th,td{border-bottom:1px solid #d6dee6;padding:8px;text-align:left;vertical-align:top;} .flash{margin-bottom:10px;padding:8px;border:1px solid #9fbad0;border-radius:8px;background:#edf2f6;} .muted{color:#4f6780;}</style>"
        "</head><body><div class='wrap'>"
        f"<div class='card'><a class='btn' href='/'>Home</a> <a class='btn' href='/company?t={urllib.parse.quote(t)}'>Company</a> <strong style='margin-left:8px;'>{html.escape(company_name)} File</strong></div>"
        "<div class='card'><form method='get' action='/company_file' style='display:grid;grid-template-columns:1fr auto;gap:8px;'>"
        f"<input name='q' value='{html.escape(q)}' placeholder='Search ticker or company name'>"
        "<button type='submit'>Search</button></form></div>"
        f"{msg_html}"
        f"<div class='card'><h2 style='margin:0;'>{html.escape(company_name)}</h2>"
        f"<div class='muted'>Ticker: {html.escape(t)} | Country: {html.escape(country_txt)} | Market Cap: {html.escape(mcap_txt)}</div>"
        f"<div class='muted'>{html.escape(str(p.get('sector') or 'Unknown'))} | "
        f"<a href='{html.escape(industry_href)}' style='display:inline-block;padding:2px 8px;border-radius:999px;border:1px solid #ff7a59;background:#fff1ec;color:#b5472f;font-weight:700;text-decoration:none;'>{html.escape(industry_txt)}</a></div>"
        f"<div class='muted'>Status: {html.escape(status_txt)} | Last update: {html.escape(updated_at or '-')}</div>"
        f"<div style='margin-top:8px;'><a class='btn' href='/company?t={urllib.parse.quote(t)}'>SEC Filings</a></div></div>"
        "<div class='card'><h3 style='margin:0 0 8px 0;'>Moat</h3>"
        "<div class='muted' style='margin-bottom:8px;'>Tag moats from your own research. Click any tag to see all companies with the same moat.</div>"
        f"<div style='margin-bottom:8px;'>{current_moat_chip_html}</div>"
        "<form method='post' action='/company_file/moat_save'>"
        f"<input type='hidden' name='ticker' value='{html.escape(t)}'>"
        f"<div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:4px 12px;'>{moat_input_html}</div>"
        "<div style='margin-top:8px;'><button type='submit'>Save Moat Tags</button></div>"
        "</form></div>"
        "<div class='card'><h3 style='margin:0 0 8px 0;'>Competitors <span class='muted' style='font-size:12px;'>(based on SEC)</span></h3>"
        "<form method='post' action='/company_file/competitor_add' style='display:grid;grid-template-columns:120px 1fr auto;gap:6px;margin-bottom:8px;'>"
        f"<input type='hidden' name='ticker' value='{html.escape(t)}'>"
        "<input name='competitor_ticker' placeholder='Ticker'>"
        "<input name='competitor_name' placeholder='Company name (optional)'>"
        "<button type='submit'>Add</button></form>"
        + "<form method='post' action='/company_file/competitors_refresh' style='margin-bottom:8px;'>"
        f"<input type='hidden' name='ticker' value='{html.escape(t)}'>"
        "<button type='submit'>Refresh</button></form>"
        + (f"<div class='muted' style='margin-bottom:8px;'>{sec_comp_note}</div>" if sec_comp_note else "")
        + f"<table><thead><tr><th>Competitor</th><th>Action</th></tr></thead><tbody>{sec_comp_rows_html}</tbody></table>"
        "</div>"
        "<div class='card'><h3 style='margin:0 0 8px 0;'>Tasks</h3>"
        "<form method='post' action='/company_file/todo_add' style='display:grid;grid-template-columns:1fr auto;gap:8px;margin-bottom:8px;'>"
        f"<input type='hidden' name='ticker' value='{html.escape(t)}'>"
        "<input name='task' placeholder='Add task (prefix ! for quick auto-hide when done)'>"
        "<button type='submit'>Add Task</button></form>"
        "<div class='muted' style='margin-bottom:8px;'>Quick task example: <code>! buy gasoline</code></div>"
        f"<table><thead><tr><th>Task</th><th>Due</th><th>Status</th><th>Action</th></tr></thead><tbody>{todos_html}</tbody></table>"
        "</div>"
        "<div class='card'><h3 style='margin:0 0 8px 0;'>Company Notes</h3>"
        "<form method='post' action='/company_file/note' style='display:grid;grid-template-columns:1fr auto;gap:8px;margin-bottom:8px;'>"
        f"<input type='hidden' name='ticker' value='{html.escape(t)}'>"
        "<input name='note' placeholder='Add note'>"
        "<button type='submit'>Add Note</button></form>"
        f"<table><thead><tr><th>Date</th><th>Note</th></tr></thead><tbody>{notes_html}</tbody></table></div>"
        "<div class='card'><h3 style='margin:0 0 8px 0;'>Reminders</h3>"
        "<form method='post' action='/company_file/reminder_add' style='display:grid;grid-template-columns:1fr 1fr auto;gap:8px;margin-bottom:8px;'>"
        f"<input type='hidden' name='ticker' value='{html.escape(t)}'>"
        "<input name='note' placeholder='Reminder note (e.g., check earnings call transcript)'>"
        "<input name='remind_at' type='datetime-local'>"
        "<button type='submit'>Add Reminder</button></form>"
        f"<table><thead><tr><th>Remind At</th><th>Note</th><th>Status</th><th>Action</th></tr></thead><tbody>{reminders_html}</tbody></table></div>"
        + _quick_capture_widget_html(return_to=f"/company_file?t={urllib.parse.quote(t)}", current_ticker=t)
        + "</div></body></html>"
    )


def _norm_list_name(name: str) -> str:
    n = re.sub(r"\s+", " ", str(name or "").strip())
    n = re.sub(r"[^A-Za-z0-9 _\-/()&+.]", "", n)
    return n[:80].strip()


def create_company_list(name: str) -> tuple[bool, str]:
    n = _norm_list_name(name)
    if not n:
        return False, "List name is required."
    now_s = dt.datetime.now().isoformat()
    conn = memory_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO company_lists (name, created_at, updated_at) VALUES (?, ?, ?)",
            (n, now_s, now_s),
        )
        conn.commit()
    finally:
        conn.close()
    return True, n


def list_company_lists(limit: int = 200) -> list[sqlite3.Row]:
    conn = memory_db()
    rows = conn.execute(
        """SELECT l.name, l.created_at, l.updated_at, COUNT(i.id) AS item_count
           FROM company_lists l
           LEFT JOIN company_list_items i ON i.list_id = l.id
           GROUP BY l.id, l.name, l.created_at, l.updated_at
           ORDER BY l.updated_at DESC, l.name ASC
           LIMIT ?""",
        (max(1, min(1000, int(limit))),),
    ).fetchall()
    conn.close()
    return rows


def add_company_to_list(name: str, ticker: str, source: str = "") -> tuple[bool, str]:
    ok, norm = create_company_list(name)
    if not ok:
        return False, norm
    t = resolve_ticker_input(ticker)
    if not t:
        return False, "Ticker is required."
    conn = memory_db()
    try:
        row = conn.execute("SELECT id FROM company_lists WHERE name = ?", (norm,)).fetchone()
        if not row:
            return False, "List not found."
        lid = int(row["id"])
        now_s = dt.datetime.now().isoformat()
        conn.execute(
            """INSERT OR IGNORE INTO company_list_items (list_id, ticker, added_at, source)
               VALUES (?, ?, ?, ?)""",
            (lid, t, now_s, (source or "").strip()[:60]),
        )
        conn.execute("UPDATE company_lists SET updated_at = ? WHERE id = ?", (now_s, lid))
        conn.commit()
    finally:
        conn.close()
    return True, t


def remove_company_from_list(name: str, ticker: str) -> bool:
    n = _norm_list_name(name)
    t = resolve_ticker_input(ticker)
    if not n or not t:
        return False
    conn = memory_db()
    try:
        row = conn.execute("SELECT id FROM company_lists WHERE name = ?", (n,)).fetchone()
        if not row:
            return False
        lid = int(row["id"])
        cur = conn.execute("DELETE FROM company_list_items WHERE list_id = ? AND ticker = ?", (lid, t))
        conn.execute("UPDATE company_lists SET updated_at = ? WHERE id = ?", (dt.datetime.now().isoformat(), lid))
        conn.commit()
        return int(cur.rowcount) > 0
    finally:
        conn.close()


def delete_company_list(name: str) -> bool:
    n = _norm_list_name(name)
    if not n:
        return False
    conn = memory_db()
    try:
        row = conn.execute("SELECT id FROM company_lists WHERE name = ?", (n,)).fetchone()
        if not row:
            return False
        lid = int(row["id"])
        conn.execute("DELETE FROM company_list_items WHERE list_id = ?", (lid,))
        cur = conn.execute("DELETE FROM company_lists WHERE id = ?", (lid,))
        conn.commit()
        return int(cur.rowcount) > 0
    finally:
        conn.close()


def list_company_list_items(name: str, limit: int = 2000) -> list[sqlite3.Row]:
    n = _norm_list_name(name)
    if not n:
        return []
    conn = memory_db()
    rows = conn.execute(
        """SELECT i.ticker, i.added_at, i.source
           FROM company_list_items i
           JOIN company_lists l ON l.id = i.list_id
           WHERE l.name = ?
           ORDER BY i.ticker ASC
           LIMIT ?""",
        (n, max(1, min(5000, int(limit)))),
    ).fetchall()
    conn.close()
    return rows


def _normalize_moat_key(raw: str) -> str:
    k = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    return k if k in MOAT_LABEL_MAP else ""


def list_company_moat_keys(ticker: str) -> set[str]:
    t = resolve_ticker_input(ticker)
    if not t:
        return set()
    conn = memory_db()
    rows = conn.execute(
        "SELECT moat_key FROM company_moat_tags WHERE ticker = ? ORDER BY moat_key ASC",
        (t,),
    ).fetchall()
    conn.close()
    out: set[str] = set()
    for r in rows:
        mk = _normalize_moat_key(str(r["moat_key"] or ""))
        if mk:
            out.add(mk)
    return out


def save_company_moat_keys(ticker: str, moat_keys: list[str]) -> int:
    t = resolve_ticker_input(ticker)
    if not t:
        return 0
    norm = sorted({_normalize_moat_key(x) for x in moat_keys if _normalize_moat_key(x)})
    now_s = dt.datetime.now().isoformat()
    conn = memory_db()
    try:
        conn.execute("DELETE FROM company_moat_tags WHERE ticker = ?", (t,))
        for mk in norm:
            conn.execute(
                """INSERT OR IGNORE INTO company_moat_tags (ticker, moat_key, updated_at, note)
                   VALUES (?, ?, ?, '')""",
                (t, mk, now_s),
            )
        conn.commit()
    finally:
        conn.close()
    return len(norm)


def moat_tickers(moat_key: str, limit: int = 5000) -> list[str]:
    mk = _normalize_moat_key(moat_key)
    if not mk:
        return []
    conn = memory_db()
    rows = conn.execute(
        """SELECT DISTINCT ticker
           FROM company_moat_tags
           WHERE moat_key = ?
           ORDER BY ticker ASC
           LIMIT ?""",
        (mk, max(1, min(20000, int(limit)))),
    ).fetchall()
    conn.close()
    return [resolve_ticker_input(str(r["ticker"] or "")) or "" for r in rows if str(r["ticker"] or "").strip()]


def moat_counts() -> dict[str, int]:
    conn = memory_db()
    rows = conn.execute(
        """SELECT moat_key, COUNT(DISTINCT ticker) AS n
           FROM company_moat_tags
           GROUP BY moat_key"""
    ).fetchall()
    conn.close()
    out: dict[str, int] = {}
    for r in rows:
        mk = _normalize_moat_key(str(r["moat_key"] or ""))
        if mk:
            out[mk] = int(r["n"] or 0)
    return out


def _competitor_aliases(company_name: str) -> list[str]:
    raw = re.sub(r"\s+", " ", str(company_name or "").strip())
    if not raw:
        return []
    aliases: list[str] = [raw]
    base = re.sub(
        r"\b(incorporated|inc|corp|corporation|company|co|limited|ltd|plc|holdings|holding|group)\b\.?",
        "",
        raw,
        flags=re.IGNORECASE,
    )
    base = re.sub(r"[\.,]", " ", base)
    base = re.sub(r"\s+", " ", base).strip()
    if len(base) >= 5 and base.lower() != raw.lower():
        aliases.append(base)
    out: list[str] = []
    seen: set[str] = set()
    for a in aliases:
        ak = a.lower().strip()
        if len(ak) < 4:
            continue
        if len(re.findall(r"[a-z0-9]+", ak)) < 2:
            continue
        if ak in {"company", "group", "holdings", "holdings inc"}:
            continue
        if ak not in seen:
            seen.add(ak)
            out.append(a)
    return out


def _sec_competition_windows(text: str, max_windows: int = 120) -> list[str]:
    if not text:
        return []
    src = re.sub(r"\s+", " ", text)
    marks = list(re.finditer(r"\b(compet\w+|rival\w*|market\s+share|pricing\s+pressure)\b", src, flags=re.IGNORECASE))
    out: list[str] = []
    for m in marks[: max(20, int(max_windows))]:
        a = max(0, m.start() - 220)
        b = min(len(src), m.end() + 220)
        chunk = src[a:b].strip()
        if len(chunk) >= 30:
            out.append(chunk)
    if not out:
        sents = re.split(r"(?<=[\.\!\?])\s+", src)
        for s in sents:
            low = s.lower()
            if any(k in low for k in ("compet", "rival", "market share", "pricing pressure")):
                out.append(s.strip()[:720])
                if len(out) >= 50:
                    break
    return out[: max(20, int(max_windows))]


def _sec_competitor_filing_row(ticker: str) -> sqlite3.Row | None:
    t = resolve_ticker_input(ticker)
    if not t:
        return None
    conn = research_db()
    try:
        return conn.execute(
            """SELECT form, date, path
               FROM filings
               WHERE ticker = ? AND form IN ('10-K','20-F','10-Q')
               ORDER BY date DESC
               LIMIT 1""",
            (t,),
        ).fetchone()
    finally:
        conn.close()


def list_company_sec_competitors(ticker: str, include_hidden: bool = False, limit: int = 120) -> list[sqlite3.Row]:
    t = resolve_ticker_input(ticker)
    if not t:
        return []
    conn = memory_db()
    try:
        if include_hidden:
            rows = conn.execute(
                """SELECT id, ticker, competitor_ticker, competitor_name, source_form, source_date, source_path,
                          evidence, confidence, status, updated_at
                   FROM company_sec_competitors
                   WHERE ticker = ?
                   ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, confidence DESC, competitor_ticker ASC
                   LIMIT ?""",
                (t, max(1, min(500, int(limit)))),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, ticker, competitor_ticker, competitor_name, source_form, source_date, source_path,
                          evidence, confidence, status, updated_at
                   FROM company_sec_competitors
                   WHERE ticker = ? AND status = 'active'
                   ORDER BY confidence DESC, competitor_ticker ASC
                   LIMIT ?""",
                (t, max(1, min(500, int(limit)))),
            ).fetchall()
        return rows
    finally:
        conn.close()


def get_company_sec_competitor_run(ticker: str) -> dict[str, str]:
    t = resolve_ticker_input(ticker)
    if not t:
        return {"last_run": "", "source_form": "", "source_date": "", "source_path": "", "status": "idle", "note": ""}
    conn = memory_db()
    try:
        row = conn.execute(
            """SELECT last_run, source_form, source_date, source_path, status, note
               FROM company_sec_competitor_runs
               WHERE ticker = ?""",
            (t,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"last_run": "", "source_form": "", "source_date": "", "source_path": "", "status": "idle", "note": ""}
    return {
        "last_run": str(row["last_run"] or ""),
        "source_form": str(row["source_form"] or ""),
        "source_date": str(row["source_date"] or ""),
        "source_path": str(row["source_path"] or ""),
        "status": str(row["status"] or "idle"),
        "note": str(row["note"] or ""),
    }


def refresh_company_sec_competitors(ticker: str) -> dict[str, str | int]:
    t = resolve_ticker_input(ticker)
    if not t:
        return {"ok": 0, "count": 0, "message": "Ticker is required."}
    filing_row = _sec_competitor_filing_row(t)
    if not filing_row:
        return {"ok": 0, "count": 0, "message": "No SEC 10-K / 20-F / 10-Q filing found locally."}
    source_form = str(filing_row["form"] or "")
    source_date = str(filing_row["date"] or "")
    source_path = str(filing_row["path"] or "")
    filing_text = _read_filing_text(source_path, max_chars=900000)
    if not filing_text:
        return {"ok": 0, "count": 0, "message": "Filing text is unavailable for this company."}

    business_blk = _extract_item_block(
        filing_text,
        r"\bitem\s+1\.?\s+business\b",
        (r"\bitem\s+1a\.?\s+risk\s+factors\b", r"\bitem\s+1b\.?\b", r"\bitem\s+2\.?\b"),
        max_chars=260000,
    )
    risk_blk = _extract_item_block(
        filing_text,
        r"\bitem\s+1a\.?\s+risk\s+factors\b",
        (r"\bitem\s+1b\.?\b", r"\bitem\s+2\.?\b"),
        max_chars=240000,
    )
    mda_blk = _extract_item_block(
        filing_text,
        r"\bitem\s+7\.?\s+(management['’]?\s+s\s+discussion|management|md&a|management’s)\b|\bitem\s+7\.?\b",
        (r"\bitem\s+7a\.?\b", r"\bitem\s+8\.?\b"),
        max_chars=220000,
    )
    comp_source_text = " ".join(x for x in [business_blk, risk_blk, mda_blk] if x.strip()) or filing_text[:280000]
    windows = _sec_competition_windows(comp_source_text, max_windows=140)
    window_text = "\n".join(windows)
    if not window_text.strip():
        window_text = comp_source_text[:120000]

    conn_r = research_db()
    try:
        company_rows = conn_r.execute(
            "SELECT ticker, name FROM companies WHERE ticker != ? AND name IS NOT NULL AND TRIM(name) != ''",
            (t,),
        ).fetchall()
    finally:
        conn_r.close()
    candidates: list[tuple[str, str, list[str]]] = []
    for r in company_rows:
        ct = resolve_ticker_input(str(r["ticker"] or ""))
        nm = str(r["name"] or "").strip()
        if not ct or not nm:
            continue
        aliases = _competitor_aliases(nm)
        if not aliases:
            continue
        candidates.append((ct, nm, aliases))

    hits: list[tuple[str, str, str, float]] = []
    for ct, nm, aliases in candidates:
        best_ev = ""
        best_score = 0.0
        for w in windows:
            lw = w.lower()
            if not any(k in lw for k in ("compet", "rival", "market share", "pricing pressure")):
                continue
            for alias in aliases:
                pat = r"\b" + re.escape(alias) + r"\b"
                if re.search(pat, w, flags=re.IGNORECASE):
                    score = 0.72 + (0.06 if len(alias) >= 12 else 0.0)
                    if score > best_score:
                        best_score = score
                        best_ev = w[:560]
                    break
            if best_score >= 0.72:
                break
        if best_score > 0.0:
            hits.append((ct, nm, best_ev, best_score))

    # Deduplicate and keep strongest rows first.
    dedup: dict[str, tuple[str, str, str, float]] = {}
    for ct, nm, ev, sc in hits:
        prev = dedup.get(ct)
        if prev is None or sc > prev[3]:
            dedup[ct] = (ct, nm, ev, sc)
    top_rows = sorted(dedup.values(), key=lambda x: (-x[3], x[0]))[:24]

    now_s = dt.datetime.now().isoformat()
    conn = memory_db()
    try:
        conn.execute("DELETE FROM company_sec_competitors WHERE ticker = ?", (t,))
        for ct, nm, ev, sc in top_rows:
            conn.execute(
                """INSERT INTO company_sec_competitors
                   (ticker, competitor_ticker, competitor_name, source_form, source_date, source_path, evidence, confidence, status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
                (t, ct, nm, source_form, source_date, source_path, ev, float(sc), now_s),
            )
        note = f"SEC filing scanned ({source_form} {source_date})."
        if not top_rows:
            note = f"SEC filing scanned ({source_form} {source_date}); no high-confidence competitor mentions detected."
        conn.execute(
            """INSERT INTO company_sec_competitor_runs
               (ticker, last_run, source_form, source_date, source_path, status, note)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(ticker) DO UPDATE SET
                 last_run=excluded.last_run,
                 source_form=excluded.source_form,
                 source_date=excluded.source_date,
                 source_path=excluded.source_path,
                 status=excluded.status,
                 note=excluded.note""",
            (t, now_s, source_form, source_date, source_path, "ok", note),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": 1,
        "count": len(top_rows),
        "message": f"SEC refresh done: {len(top_rows)} competitors detected.",
        "source_form": source_form,
        "source_date": source_date,
    }


def set_company_sec_competitor_status(comp_id: int, status: str) -> bool:
    st = "hidden" if str(status or "").strip().lower() == "hidden" else "active"
    conn = memory_db()
    try:
        cur = conn.execute(
            "UPDATE company_sec_competitors SET status = ?, updated_at = ? WHERE id = ?",
            (st, dt.datetime.now().isoformat(), int(comp_id)),
        )
        conn.commit()
        return bool(cur.rowcount > 0)
    finally:
        conn.close()


def upsert_company_sec_competitor_manual(ticker: str, competitor_ticker: str, competitor_name: str) -> tuple[bool, str]:
    t = resolve_ticker_input(ticker)
    ct = resolve_ticker_input(competitor_ticker)
    if not t:
        return False, "Ticker is required."
    if not ct:
        return False, "Competitor ticker is required."
    if ct == t:
        return False, "Competitor cannot be the same ticker."
    cname = str(competitor_name or "").strip()
    if not cname:
        cname = str(_company_name_db_map([ct]).get(ct, "") or "").strip() or ct
    now_s = dt.datetime.now().isoformat()
    conn = memory_db()
    try:
        conn.execute(
            """INSERT INTO company_sec_competitors
               (ticker, competitor_ticker, competitor_name, source_form, source_date, source_path, evidence, confidence, status, updated_at)
               VALUES (?, ?, ?, 'manual', ?, '', '', 1.0, 'active', ?)
               ON CONFLICT(ticker, competitor_ticker) DO UPDATE SET
                 competitor_name=excluded.competitor_name,
                 source_form='manual',
                 source_date=excluded.source_date,
                 source_path='',
                 evidence='',
                 confidence=1.0,
                 status='active',
                 updated_at=excluded.updated_at""",
            (t, ct, cname, dt.date.today().isoformat(), now_s),
        )
        conn.commit()
    finally:
        conn.close()
    return True, "Competitor saved."


def update_company_sec_competitor_manual(comp_id: int, competitor_ticker: str, competitor_name: str) -> tuple[bool, str]:
    ct = resolve_ticker_input(competitor_ticker)
    if not ct:
        return False, "Competitor ticker is required."
    cname = str(competitor_name or "").strip() or ct
    now_s = dt.datetime.now().isoformat()
    conn = memory_db()
    try:
        row = conn.execute("SELECT ticker FROM company_sec_competitors WHERE id = ?", (int(comp_id),)).fetchone()
        if not row:
            return False, "Competitor row not found."
        host_ticker = resolve_ticker_input(str(row["ticker"] or ""))
        if ct == host_ticker:
            return False, "Competitor cannot be the same ticker."
        conn.execute(
            """UPDATE company_sec_competitors
               SET competitor_ticker = ?, competitor_name = ?, source_form = 'manual', source_date = ?, source_path = '',
                   evidence = '', confidence = 1.0, status = 'active', updated_at = ?
               WHERE id = ?""",
            (ct, cname, dt.date.today().isoformat(), now_s, int(comp_id)),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        return False, "This competitor already exists."
    finally:
        conn.close()
    return True, "Competitor updated."


def delete_company_sec_competitor(comp_id: int) -> bool:
    conn = memory_db()
    try:
        cur = conn.execute("DELETE FROM company_sec_competitors WHERE id = ?", (int(comp_id),))
        conn.commit()
        return bool(cur.rowcount > 0)
    finally:
        conn.close()


def _company_file_list_tickers(name: str) -> list[str]:
    n = _norm_list_name(name)
    if not n:
        return []
    rows = list_company_list_items(n, limit=5000)
    return sorted({t for t in (resolve_ticker_input(str(r["ticker"] or "")) for r in rows) if t})


def _company_file_index_tickers(index_id: str) -> list[str]:
    idx = str(index_id or "").strip().lower()
    if not idx:
        return []
    rows, _mode, _note = _load_index_tickers(idx, refresh=False, allow_network=False)
    if not rows:
        rows, _mode, _note = _load_index_tickers(idx, refresh=False, allow_network=True)
    def _normalize_index_symbol(raw: str) -> str:
        s = _clean_symbol(raw)
        if not s:
            return ""
        if idx == "hangseng":
            m = re.fullmatch(r"\d{1,5}", s)
            if m:
                return f"{int(s):04d}.HK"
        if idx == "sse50":
            if re.fullmatch(r"\d{6}", s):
                return f"{s}.SS"
        if idx == "kospi200":
            if re.fullmatch(r"\d{6}", s):
                return f"{s}.KS"
        if idx == "nikkei225":
            if re.fullmatch(r"\d{4}", s):
                return f"{s}.T"
        return resolve_ticker_input(s)

    return sorted({t for t in (_normalize_index_symbol(x) for x in rows) if t})


def _market_cap_for_ticker(ticker: str) -> str:
    t = (ticker or "").strip().upper()
    if not t:
        return "-"
    ck = f"mcap:t:{t}"
    cached = _cache_get(ck, ttl_seconds=86400)
    if isinstance(cached, str) and cached.strip():
        return cached
    out = "-"
    if yf is not None:
        try:
            tk = yf.Ticker(t)
            fi = getattr(tk, "fast_info", None) or {}
            info = getattr(tk, "info", None) or {}
            v = fi.get("marketCap") or fi.get("market_cap") or info.get("marketCap")
            out = _fmt_big(v)
        except Exception:
            out = "-"
    _cache_put(ck, out)
    return out


INDEX_CACHE_DIR = DATA / "index_lists"


def _read_ticker_file(path: Path) -> list[str]:
    out: list[str] = []
    if not path.exists():
        return out
    for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        s = (ln or "").strip()
        if not s or s.startswith("#"):
            continue
        t = re.sub(r"[^A-Za-z0-9.\-]", "", s.upper())
        if t:
            out.append(t)
    seen: set[str] = set()
    uniq: list[str] = []
    for t in out:
        if t in seen:
            continue
        seen.add(t)
        uniq.append(t)
    return uniq


def _write_ticker_file(path: Path, tickers: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Auto-cached index constituents.", f"# Updated: {dt.datetime.now().isoformat()}"] + sorted(set(tickers))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fetch_text_url(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; OnyxTerminal/1.0; +https://127.0.0.1)",
            "Accept": "text/html,application/xhtml+xml,application/xml,text/csv,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=max(4, min(45, int(timeout)))) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="ignore")


def _clean_symbol(raw: str) -> str:
    s = html.unescape(str(raw or "")).strip()
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\[[0-9]+\]", "", s)
    s = s.replace("\xa0", " ").strip().upper()
    if ":" in s:
        # Prefer the token after exchange prefixes like "NYSE:MMM" or "SEHK:0005".
        parts = [p.strip() for p in s.split(":") if p.strip()]
        if parts:
            right = parts[-1]
            left = parts[0]
            if re.search(r"[0-9]", right) or "." in right or (len(right) <= 8 and re.search(r"[A-Z]", right)):
                s = right
            else:
                s = left
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[^A-Z0-9.\-]", "", s)
    if not s:
        return ""
    if len(s) > 14:
        return ""
    if not re.search(r"[A-Z0-9]", s):
        return ""
    return s


def _extract_wiki_tickers(page_html: str) -> list[str]:
    if not page_html:
        return []
    tables = re.findall(r"(?is)<table[^>]*class=\"[^\"]*wikitable[^\"]*\"[^>]*>.*?</table>", page_html)
    best: list[str] = []
    for tb in tables:
        headers = [re.sub(r"\s+", " ", re.sub(r"(?is)<[^>]+>", " ", h)).strip().lower() for h in re.findall(r"(?is)<th[^>]*>(.*?)</th>", tb)]
        if not headers:
            continue
        col = -1
        for i, h in enumerate(headers):
            if any(k in h for k in ("symbol", "ticker", "epic", "code", "ric")):
                col = i
                break
        if col < 0:
            continue
        rows = re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", tb)
        got: list[str] = []
        for rw in rows[1:]:
            cells = re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", rw)
            if len(cells) <= col:
                continue
            t = _clean_symbol(cells[col])
            if t:
                got.append(t)
        uniq = sorted(set(got))
        if len(uniq) > len(best):
            best = uniq
    return best


def _extract_ishares_csv_tickers(csv_text: str) -> list[str]:
    if not csv_text:
        return []
    lines = [ln for ln in str(csv_text).splitlines() if ln.strip()]
    start = -1
    for i, ln in enumerate(lines):
        if "ticker" in ln.lower() and "name" in ln.lower():
            start = i
            break
    if start < 0:
        return []
    rows: list[str] = []
    reader = csv.DictReader(lines[start:])
    for row in reader:
        v = row.get("Ticker") or row.get("ticker") or ""
        t = _clean_symbol(v)
        if t:
            rows.append(t)
    return sorted(set(rows))


def _index_presets() -> list[dict[str, str]]:
    return [
        {"id": "sp500", "name": "S&P 500", "region": "US", "kind": "wiki", "url": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "file": str(INDEX_CACHE_DIR / "sp500.txt")},
        {"id": "russell1000", "name": "Russell 1000", "region": "US", "kind": "csv", "url": "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund", "file": str(INDEX_CACHE_DIR / "russell1000.txt")},
        {"id": "russell2000", "name": "Russell 2000", "region": "US", "kind": "csv", "url": "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund", "file": str(INDEX_CACHE_DIR / "russell2000.txt")},
        {"id": "stoxx600", "name": "STOXX Europe 600", "region": "Europe", "kind": "wiki", "url": "https://en.wikipedia.org/wiki/STOXX_Europe_600", "file": str(INDEX_CACHE_DIR / "stoxx600.txt")},
        {"id": "ftse100", "name": "FTSE 100", "region": "Europe", "kind": "wiki", "url": "https://en.wikipedia.org/wiki/FTSE_100_Index", "file": str(INDEX_CACHE_DIR / "ftse100.txt")},
        {"id": "dax40", "name": "DAX 40", "region": "Europe", "kind": "wiki", "url": "https://en.wikipedia.org/wiki/DAX", "file": str(INDEX_CACHE_DIR / "dax40.txt")},
        {"id": "cac40", "name": "CAC 40", "region": "Europe", "kind": "wiki", "url": "https://en.wikipedia.org/wiki/CAC_40", "file": str(INDEX_CACHE_DIR / "cac40.txt")},
        {"id": "nikkei225", "name": "Nikkei 225", "region": "Asia", "kind": "wiki", "url": "https://en.wikipedia.org/wiki/Nikkei_225", "file": str(INDEX_CACHE_DIR / "nikkei225.txt")},
        {"id": "hangseng", "name": "Hang Seng", "region": "Asia", "kind": "wiki", "url": "https://en.wikipedia.org/wiki/Hang_Seng_Index", "file": str(INDEX_CACHE_DIR / "hangseng.txt")},
        {"id": "sse50", "name": "SSE 50", "region": "Asia", "kind": "wiki", "url": "https://en.wikipedia.org/wiki/SSE_50_Index", "file": str(INDEX_CACHE_DIR / "sse50.txt")},
        {"id": "kospi200", "name": "KOSPI 200", "region": "Asia", "kind": "wiki", "url": "https://en.wikipedia.org/wiki/KOSPI_200", "file": str(INDEX_CACHE_DIR / "kospi200.txt")},
        {"id": "tsx60", "name": "S&P/TSX 60", "region": "Canada", "kind": "wiki", "url": "https://en.wikipedia.org/wiki/S%26P/TSX_60", "file": str(INDEX_CACHE_DIR / "tsx60.txt")},
        {"id": "asx200", "name": "S&P/ASX 200", "region": "Australia", "kind": "wiki", "url": "https://en.wikipedia.org/wiki/S%26P/ASX_200", "file": str(INDEX_CACHE_DIR / "asx200.txt")},
    ]


def _load_index_tickers(index_id: str, refresh: bool = False, allow_network: bool = False) -> tuple[list[str], str, str]:
    idx = next((x for x in _index_presets() if x["id"] == index_id), None)
    if not idx:
        return [], "missing", "Unknown index."
    fpath = Path(str(idx["file"]))
    if fpath.exists():
        rows = _read_ticker_file(fpath)
        if rows:
            if not refresh:
                return rows, "cache", f"Loaded cached list ({len(rows)})."
    if allow_network or refresh:
        try:
            txt = _fetch_text_url(str(idx["url"]), timeout=12)
            if str(idx["kind"]) == "csv":
                rows = _extract_ishares_csv_tickers(txt)
            else:
                rows = _extract_wiki_tickers(txt)
            if rows:
                _write_ticker_file(fpath, rows)
                return rows, "live", f"Fetched from source ({len(rows)})."
        except Exception:
            pass
    fallback = _read_ticker_file(fpath) if fpath.exists() else []
    if fallback:
        return fallback, "cache_stale", f"Source unavailable; using cached list ({len(fallback)})."
    return [], "empty", "No cache yet. Click Refresh for first load."


def indices_html(message: str = "") -> str:
    msg_html = f"<div class='flash'>{html.escape(message)}</div>" if message else ""
    cards = []
    for x in _index_presets():
        rows, _mode, note = _load_index_tickers(x["id"], refresh=False, allow_network=False)
        cards.append(
            "<tr>"
            f"<td><a href='/index?i={urllib.parse.quote(x['id'])}'>{html.escape(x['name'])}</a></td>"
            f"<td>{html.escape(x['region'])}</td>"
            f"<td>{len(rows)}</td>"
            f"<td>{html.escape(note)}</td>"
            "<td>"
            f"<form method='post' action='/company_list/import_index' style='display:inline;'><input type='hidden' name='index_id' value='{html.escape(x['id'])}'><input type='hidden' name='name' value='{html.escape(x['name'])}'><button type='submit' style='margin-left:6px;'>Import To List</button></form> "
            f"<a class='btn' href='/index?i={urllib.parse.quote(x['id'])}&refresh=1'>Refresh</a>"
            "</td>"
            "</tr>"
        )
    rows_html = "".join(cards) or "<tr><td colspan='5' class='muted'>No index presets configured.</td></tr>"
    return (
        "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Indices</title>"
        "<style>body{margin:0;background:#eaf0f4;color:#2f4358;font-family:'Avenir Next','Helvetica Neue',sans-serif;}"
        ".wrap{max-width:1120px;margin:0 auto;padding:14px;} .card{background:#f1f5f8;border:1px solid #d6dee6;border-radius:10px;padding:12px;margin-bottom:10px;}"
        ".btn{border:1px solid #c8d3dd;background:#edf2f6;color:#2f4358;border-radius:8px;padding:6px 10px;text-decoration:none;font-size:12px;font-weight:700;}"
        "button{border:1px solid #ff7a59;background:#ff7a59;color:#fff;border-radius:8px;padding:8px 10px;font-weight:700;cursor:pointer;}"
        "table{width:100%;border-collapse:collapse;} th,td{border-bottom:1px solid #d6dee6;padding:8px;text-align:left;vertical-align:top;} .flash{margin-bottom:10px;padding:8px;border:1px solid #9fbad0;border-radius:8px;background:#edf2f6;} .muted{color:#4f6780;}</style>"
        "</head><body><div class='wrap'>"
        "<div class='card'><a class='btn' href='/'>Home</a> <a class='btn' href='/company_lists'>Company Lists</a></div>"
        f"{msg_html}"
        "<div class='card'><h2 style='margin:0 0 8px 0;'>Indices</h2><div class='muted'>Open index constituents and import into persistent Company Lists.</div>"
        "<form method='get' action='/indices/industry' style='display:grid;grid-template-columns:1fr auto;gap:8px;margin-top:8px;'>"
        "<input name='industry' placeholder='Industry or sector (e.g. Financial Services)'>"
        "<button type='submit'>Explore Industry Across All Indices</button>"
        "</form></div>"
        "<div class='card'><table><thead><tr><th>Index</th><th>Region</th><th>Companies</th><th>Status</th><th>Actions</th></tr></thead><tbody>"
        f"{rows_html}</tbody></table></div></div></body></html>"
    )


def _market_cap_cached_only(ticker: str) -> str:
    t = (ticker or "").strip().upper()
    if not t:
        return "-"
    ck = f"mcap:t:{t}"
    v = _cache_get(ck, ttl_seconds=86400 * 14)
    if isinstance(v, str) and v.strip():
        return v
    return "-"


MCAP_BACKFILL_STATE: dict[str, bool] = {"running": False}


def _mcap_backfill_worker(tickers: list[str]) -> None:
    try:
        for t in tickers:
            _market_cap_for_ticker(t)
    finally:
        with LOCK:
            MCAP_BACKFILL_STATE["running"] = False


def _start_mcap_backfill(tickers: list[str]) -> None:
    uniq = [str(t or "").strip().upper() for t in tickers if str(t or "").strip()]
    uniq = sorted(set(uniq))
    if not uniq:
        return
    with LOCK:
        if bool(MCAP_BACKFILL_STATE.get("running")):
            return
        MCAP_BACKFILL_STATE["running"] = True
    th = threading.Thread(target=_mcap_backfill_worker, args=(uniq,), daemon=True)
    th.start()


def _market_caps_for_page(tickers: list[str], sync_limit: int = 12) -> dict[str, str]:
    uniq = [str(t or "").strip().upper() for t in tickers if str(t or "").strip()]
    out: dict[str, str] = {}
    missing: list[str] = []
    for t in uniq:
        m = _market_cap_cached_only(t)
        out[t] = m
        if m == "-":
            missing.append(t)
    lim = max(0, min(40, int(sync_limit)))
    for t in missing[:lim]:
        out[t] = _market_cap_for_ticker(t)
    rest = [t for t in missing[lim:] if out.get(t, "-") == "-"]
    if rest:
        _start_mcap_backfill(rest)
    return out


def index_industry_html(industry: str = "", query: str = "", sort_by: str = "ticker_asc", page: int = 1, page_size: int = 120) -> str:
    needle = (industry or "").strip()
    if not needle:
        return indices_html("Industry filter is required.")
    all_tickers: set[str] = set()
    for x in _index_presets():
        rows, _mode, _note = _load_index_tickers(x["id"], refresh=False, allow_network=False)
        all_tickers.update({str(t or "").upper().strip() for t in rows if str(t or "").strip()})
    tickers = sorted(all_tickers)
    profiles = get_portfolio_profiles(tickers) if tickers else {}
    nd = needle.lower()
    q = (query or "").strip().lower()
    picked: list[str] = []
    for t in tickers:
        p = profiles.get(t, {}) or {}
        sec = str(p.get("sector") or "Unknown")
        ind = str(p.get("industry") or "Unknown")
        if nd not in sec.lower() and nd not in ind.lower():
            continue
        if q and q not in t.lower() and q not in str(p.get("name") or "").lower() and q not in ind.lower() and q not in sec.lower():
            continue
        picked.append(t)
    if sort_by not in {"mcap_desc", "ticker_asc"}:
        sort_by = "ticker_asc"
    if sort_by == "ticker_asc":
        picked = sorted(picked)
    else:
        picked = sorted(picked, key=lambda t: _parse_mcap_value(_market_cap_cached_only(t)), reverse=True)
    total = len(picked)
    ps = max(25, min(300, int(page_size)))
    pg_max = max(1, (total + ps - 1) // ps)
    pg = max(1, min(int(page), pg_max))
    start = (pg - 1) * ps
    end = min(total, start + ps)
    page_rows = picked[start:end]
    mcap_map = _market_caps_for_page(page_rows, sync_limit=40)
    rows_html = "".join(
        (
            "<tr>"
            f"<td><a href='/company_file?t={html.escape(t)}'>{html.escape(t)}</a></td>"
            f"<td><a href='/company_file?t={html.escape(t)}'>{html.escape(str((profiles.get(t, {}) or {}).get('name') or t))}</a></td>"
            f"<td>{html.escape(str((profiles.get(t, {}) or {}).get('sector') or 'Unknown'))}</td>"
            f"<td>{html.escape(str((profiles.get(t, {}) or {}).get('industry') or 'Unknown'))}</td>"
            f"<td>{html.escape(str(mcap_map.get(t) or '-'))}</td>"
            f"<td><form method='post' action='/watchlist/add' style='display:inline;'><input type='hidden' name='ticker' value='{html.escape(t)}'><input type='hidden' name='return_to' value='/indices/industry?industry={urllib.parse.quote(needle)}'><button type='submit'>Add Watchlist</button></form></td>"
            "</tr>"
        )
        for t in page_rows
    ) or "<tr><td colspan='6' class='muted'>No companies matched this industry filter in current index datasets.</td></tr>"
    prev_href = (
        f"/indices/industry?industry={urllib.parse.quote(needle)}&q={urllib.parse.quote(query)}&sort={urllib.parse.quote(sort_by)}&page={pg-1}&page_size={ps}"
        if pg > 1
        else ""
    )
    next_href = (
        f"/indices/industry?industry={urllib.parse.quote(needle)}&q={urllib.parse.quote(query)}&sort={urllib.parse.quote(sort_by)}&page={pg+1}&page_size={ps}"
        if pg < pg_max
        else ""
    )
    return (
        "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>Industry: {html.escape(needle)}</title>"
        "<style>body{margin:0;background:#eaf0f4;color:#2f4358;font-family:'Avenir Next','Helvetica Neue',sans-serif;}"
        ".wrap{max-width:1240px;margin:0 auto;padding:14px;} .card{background:#f1f5f8;border:1px solid #d6dee6;border-radius:10px;padding:12px;margin-bottom:10px;}"
        ".btn{border:1px solid #c8d3dd;background:#edf2f6;color:#2f4358;border-radius:8px;padding:6px 10px;text-decoration:none;font-size:12px;font-weight:700;}"
        "input,select{width:100%;box-sizing:border-box;border:1px solid #c8d3dd;border-radius:8px;background:#edf2f6;color:#2f4358;padding:8px;}"
        "button{border:1px solid #ff7a59;background:#ff7a59;color:#fff;border-radius:8px;padding:8px 10px;font-weight:700;cursor:pointer;}"
        "table{width:100%;border-collapse:collapse;} th,td{border-bottom:1px solid #d6dee6;padding:8px;text-align:left;vertical-align:top;} .muted{color:#4f6780;}</style>"
        "</head><body><div class='wrap'>"
        "<div class='card'><a class='btn' href='/'>Home</a> <a class='btn' href='/indices'>Indices</a> <a class='btn' href='/company_lists'>Company Lists</a></div>"
        f"<div class='card'><h2 style='margin:0;'>Industry Filter: {html.escape(needle)}</h2><div class='muted'>Cross-index and cross-country company view.</div></div>"
        "<div class='card'><form method='get' action='/indices/industry' style='display:grid;grid-template-columns:1fr 1fr 1fr 1fr auto;gap:8px;'>"
        f"<input name='industry' value='{html.escape(needle)}' placeholder='Industry or sector (e.g. Financial)'>"
        f"<input name='q' value='{html.escape(query)}' placeholder='Filter ticker/company'>"
        "<select name='sort'><option value='ticker_asc' " + ("selected" if sort_by == "ticker_asc" else "") + ">Sort: Ticker A-Z</option><option value='mcap_desc' " + ("selected" if sort_by == "mcap_desc" else "") + ">Sort: Market Cap ↓ (cached)</option></select>"
        f"<input name='page_size' value='{ps}' placeholder='Rows/page'>"
        "<button type='submit'>Apply</button></form></div>"
        + (
            "<div class='card'>"
            f"<div class='muted'>Showing {start+1 if total else 0}-{end} of {total} | Page {pg}/{pg_max}</div>"
            + (f"<a class='btn' href='{html.escape(prev_href)}' style='margin-right:6px;'>Prev</a>" if prev_href else "")
            + (f"<a class='btn' href='{html.escape(next_href)}'>Next</a>" if next_href else "")
            + "</div>"
        )
        + "<div class='card'><table><thead><tr><th>Ticker</th><th>Company</th><th>Sector</th><th>Industry</th><th>Market Cap</th><th>Actions</th></tr></thead><tbody>"
        f"{rows_html}</tbody></table></div></div></body></html>"
    )


def index_detail_html(index_id: str, query: str = "", sort_by: str = "ticker_asc", refresh: bool = False, page: int = 1, page_size: int = 120) -> str:
    idx = next((x for x in _index_presets() if x["id"] == index_id), None)
    if not idx:
        return indices_html("Unknown index.")
    tickers, _mode, note = _load_index_tickers(index_id=index_id, refresh=refresh, allow_network=refresh)
    q = (query or "").strip().lower()
    profiles = get_portfolio_profiles(tickers) if tickers else {}
    f_tickers = [t for t in tickers if (not q or q in t.lower() or q in str((profiles.get(t, {}) or {}).get("name") or "").lower() or q in str((profiles.get(t, {}) or {}).get("industry") or "").lower())]
    if sort_by not in {"mcap_desc", "ticker_asc"}:
        sort_by = "ticker_asc"
    if sort_by == "ticker_asc":
        f_tickers = sorted(f_tickers)
    else:
        f_tickers = sorted(f_tickers, key=lambda t: _parse_mcap_value(_market_cap_cached_only(t)), reverse=True)
    total = len(f_tickers)
    ps = max(25, min(300, int(page_size)))
    pg_max = max(1, (total + ps - 1) // ps)
    pg = max(1, min(int(page), pg_max))
    start = (pg - 1) * ps
    end = min(total, start + ps)
    page_rows = f_tickers[start:end]
    mcap_map = _market_caps_for_page(page_rows, sync_limit=40)
    rows_html = "".join(
        (
            "<tr>"
            f"<td><a href='/company_file?t={html.escape(t)}'>{html.escape(t)}</a></td>"
            f"<td><a href='/company_file?t={html.escape(t)}'>{html.escape(str((profiles.get(t, {}) or {}).get('name') or t))}</a></td>"
            f"<td><a href='/indices/industry?industry={urllib.parse.quote(str((profiles.get(t, {}) or {}).get('industry') or 'Unknown'))}'>{html.escape(str((profiles.get(t, {}) or {}).get('industry') or 'Unknown'))}</a></td>"
            f"<td>{html.escape(str(mcap_map.get(t) or '-'))}</td>"
            f"<td><form method='post' action='/watchlist/add' style='display:inline;'><input type='hidden' name='ticker' value='{html.escape(t)}'><input type='hidden' name='return_to' value='/index?i={urllib.parse.quote(index_id)}'><button type='submit'>Add Watchlist</button></form> "
            f"<form method='post' action='/company_list/add' style='display:inline;'><input type='hidden' name='name' value='{html.escape(str(idx['name']))}'><input type='hidden' name='ticker' value='{html.escape(t)}'><input type='hidden' name='source' value='index:{html.escape(index_id)}'><button type='submit' style='margin-left:6px;'>Add To List</button></form></td>"
            "</tr>"
        )
        for t in page_rows
    ) or "<tr><td colspan='5' class='muted'>No companies loaded for this index.</td></tr>"
    prev_href = (
        f"/index?i={urllib.parse.quote(index_id)}&q={urllib.parse.quote(query)}&sort={urllib.parse.quote(sort_by)}&page={pg-1}&page_size={ps}"
        if pg > 1
        else ""
    )
    next_href = (
        f"/index?i={urllib.parse.quote(index_id)}&q={urllib.parse.quote(query)}&sort={urllib.parse.quote(sort_by)}&page={pg+1}&page_size={ps}"
        if pg < pg_max
        else ""
    )
    return (
        "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(str(idx['name']))}</title>"
        "<style>body{margin:0;background:#eaf0f4;color:#2f4358;font-family:'Avenir Next','Helvetica Neue',sans-serif;}"
        ".wrap{max-width:1240px;margin:0 auto;padding:14px;} .card{background:#f1f5f8;border:1px solid #d6dee6;border-radius:10px;padding:12px;margin-bottom:10px;}"
        ".btn{border:1px solid #c8d3dd;background:#edf2f6;color:#2f4358;border-radius:8px;padding:6px 10px;text-decoration:none;font-size:12px;font-weight:700;}"
        "input,select{width:100%;box-sizing:border-box;border:1px solid #c8d3dd;border-radius:8px;background:#edf2f6;color:#2f4358;padding:8px;}"
        "button{border:1px solid #ff7a59;background:#ff7a59;color:#fff;border-radius:8px;padding:8px 10px;font-weight:700;cursor:pointer;}"
        "table{width:100%;border-collapse:collapse;} th,td{border-bottom:1px solid #d6dee6;padding:8px;text-align:left;vertical-align:top;} .muted{color:#4f6780;}</style>"
        "</head><body><div class='wrap'>"
        f"<div class='card'><a class='btn' href='/'>Home</a> <a class='btn' href='/indices'>Indices</a> <a class='btn' href='/company_list?name={urllib.parse.quote(str(idx['name']))}'>Open Saved List</a></div>"
        f"<div class='card'><h2 style='margin:0 0 6px 0;'>{html.escape(str(idx['name']))}</h2><div class='muted'>{html.escape(note)}</div>"
        "<form method='post' action='/company_list/import_index' style='display:grid;grid-template-columns:1fr auto auto;gap:8px;margin-top:8px;'>"
        f"<input type='hidden' name='index_id' value='{html.escape(index_id)}'>"
        f"<input name='name' value='{html.escape(str(idx['name']))}' placeholder='List name'>"
        "<button type='submit'>Import Entire Index To List</button>"
        "</form></div>"
        "<div class='card'><form method='get' action='/index' style='display:grid;grid-template-columns:1fr 1fr 1fr auto auto;gap:8px;'>"
        f"<input type='hidden' name='i' value='{html.escape(index_id)}'>"
        f"<input name='q' value='{html.escape(query)}' placeholder='Filter by ticker/company/industry'>"
        "<select name='sort'><option value='ticker_asc' " + ("selected" if sort_by == "ticker_asc" else "") + ">Sort: Ticker A-Z</option><option value='mcap_desc' " + ("selected" if sort_by == "mcap_desc" else "") + ">Sort: Market Cap ↓ (cached)</option></select>"
        f"<input name='page_size' value='{ps}' placeholder='Rows/page'>"
        "<button type='submit'>Filter</button>"
        "</form></div>"
        + (
            "<div class='card'>"
            f"<div class='muted'>Showing {start+1 if total else 0}-{end} of {total} | Page {pg}/{pg_max}</div>"
            + (f"<a class='btn' href='{html.escape(prev_href)}' style='margin-right:6px;'>Prev</a>" if prev_href else "")
            + (f"<a class='btn' href='{html.escape(next_href)}'>Next</a>" if next_href else "")
            + "</div>"
        )
        + "<div class='card'><table><thead><tr><th>Ticker</th><th>Company</th><th>Industry (click)</th><th>Market Cap</th><th>Actions</th></tr></thead><tbody>"
        f"{rows_html}</tbody></table></div></div></body></html>"
    )


def company_lists_html(message: str = "") -> str:
    rows = list_company_lists(limit=300)
    msg_html = f"<div class='flash'>{html.escape(message)}</div>" if message else ""
    idx_cards = []
    for x in _index_presets():
        items, _mode, note = _load_index_tickers(x["id"], refresh=False, allow_network=False)
        idx_cards.append(
            "<tr>"
            f"<td><a href='/index?i={urllib.parse.quote(x['id'])}'>{html.escape(x['name'])}</a></td>"
            f"<td>{html.escape(x['region'])}</td>"
            f"<td>{len(items)}</td>"
            f"<td>{html.escape(note)}</td>"
            "<td>"
            f"<form method='post' action='/company_list/import_index' style='display:inline;'><input type='hidden' name='index_id' value='{html.escape(x['id'])}'><input type='hidden' name='name' value='{html.escape(x['name'])}'><button type='submit' style='margin-left:6px;'>Import</button></form>"
            "</td>"
            "</tr>"
        )
    idx_html = "".join(idx_cards) or "<tr><td colspan='5' class='muted'>No index presets.</td></tr>"
    cards = "".join(
        (
            "<tr>"
            f"<td><a href='/company_list?name={urllib.parse.quote(str(r['name'] or ''), safe='')}'>{html.escape(str(r['name'] or ''))}</a></td>"
            f"<td>{int(r['item_count'] or 0)}</td>"
            f"<td>{html.escape(str(r['updated_at'] or '')[:19].replace('T',' '))}</td>"
            f"<td><a class='btn' href='/company_list?name={urllib.parse.quote(str(r['name'] or ''), safe='')}'>Open</a> "
            f"<form method='post' action='/company_lists/delete' style='display:inline;'><input type='hidden' name='name' value='{html.escape(str(r['name'] or ''))}'><button type='submit' style='margin-left:6px;'>Delete</button></form></td>"
            "</tr>"
        )
        for r in rows
    ) or "<tr><td colspan='4' class='muted'>No lists yet.</td></tr>"
    return (
        "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Company Lists</title>"
        "<style>body{margin:0;background:#eaf0f4;color:#2f4358;font-family:'Avenir Next','Helvetica Neue',sans-serif;}"
        ".wrap{max-width:1000px;margin:0 auto;padding:14px;} .card{background:#f1f5f8;border:1px solid #d6dee6;border-radius:10px;padding:12px;margin-bottom:10px;}"
        ".btn{border:1px solid #c8d3dd;background:#edf2f6;color:#2f4358;border-radius:8px;padding:6px 10px;text-decoration:none;font-size:12px;font-weight:700;}"
        "input{width:100%;box-sizing:border-box;border:1px solid #c8d3dd;border-radius:8px;background:#edf2f6;color:#2f4358;padding:8px;}"
        "button{border:1px solid #ff7a59;background:#ff7a59;color:#fff;border-radius:8px;padding:8px 10px;font-weight:700;cursor:pointer;}"
        "table{width:100%;border-collapse:collapse;} th,td{border-bottom:1px solid #d6dee6;padding:8px;text-align:left;} .flash{margin-bottom:10px;padding:8px;border:1px solid #9fbad0;border-radius:8px;background:#edf2f6;} .muted{color:#4f6780;}</style>"
        "</head><body><div class='wrap'>"
        "<div class='card'><a class='btn' href='/'>Home</a> <a class='btn' href='/watchlist_history'>History</a></div>"
        f"{msg_html}"
        "<div class='card'><h2 style='margin:0 0 8px 0;'>Lists</h2><div class='muted'>Your custom saved lists. Use Index Library to import S&P 500, Russell 2000, EU/Asia lists.</div>"
        "<form method='post' action='/company_lists/create' style='display:grid;grid-template-columns:1fr auto;gap:8px;'>"
        "<input name='name' placeholder='Create list name (e.g. AI Infra)'>"
        "<button type='submit'>Create List</button>"
        "</form></div>"
        "<div class='card'><h3 style='margin:0 0 8px 0;'>Index Library</h3><table><thead><tr><th>Index</th><th>Region</th><th>Companies</th><th>Status</th><th>Actions</th></tr></thead><tbody>"
        f"{idx_html}</tbody></table></div>"
        "<div class='card'><table><thead><tr><th>List</th><th>Companies</th><th>Updated</th><th>Open</th></tr></thead><tbody>"
        f"{cards}</tbody></table></div>"
        "</div></body></html>"
    )


def company_list_html(name: str, query: str = "", sort_by: str = "mcap_desc") -> str:
    n = _norm_list_name(name)
    if not n:
        return company_lists_html("List name is required.")
    items = list_company_list_items(n, limit=3000)
    tickers = sorted({str(r["ticker"] or "").upper().strip() for r in items if str(r["ticker"] or "").strip()})
    profiles = get_portfolio_profiles(tickers) if tickers else {}
    rep = get_reported_earnings_snapshot(limit=260)
    mcap_map = {str(r.get("ticker") or "").upper().strip(): str(r.get("mcap_txt") or "-") for r in list(rep.get("parsed") or [])}
    q = (query or "").strip().lower()
    f_tickers = [t for t in tickers if (not q or q in t.lower() or q in str((profiles.get(t, {}) or {}).get("name") or "").lower() or q in str((profiles.get(t, {}) or {}).get("industry") or "").lower())]
    if sort_by not in {"mcap_desc", "ticker_asc", "recent_added"}:
        sort_by = "mcap_desc"
    added_map = {str(r["ticker"] or "").upper().strip(): str(r["added_at"] or "") for r in items}
    if sort_by == "ticker_asc":
        f_tickers = sorted(f_tickers)
    elif sort_by == "recent_added":
        f_tickers = sorted(f_tickers, key=lambda t: added_map.get(t, ""), reverse=True)
    else:
        def _mcap_num_for_t(t: str) -> float:
            s = str(mcap_map.get(t) or _market_cap_for_ticker(t) or "")
            return _parse_mcap_value(s)
        f_tickers = sorted(f_tickers, key=_mcap_num_for_t, reverse=True)
    notes_all = list_workspace_journal_all(limit=2500)
    note_rows = [r for r in notes_all if str(r["ticker"] or "").upper().strip() in set(f_tickers)]
    note_rows = [r for r in note_rows if (not q or q in str(r["ticker"] or "").lower() or q in str(r["note"] or "").lower() or q in str(r["created_at"] or "").lower())]

    rows_html = "".join(
        (
            "<tr>"
            f"<td><a href='/company_file?t={html.escape(t)}'>{html.escape(t)}</a></td>"
            f"<td><a href='/company_file?t={html.escape(t)}'>{html.escape(str((profiles.get(t, {}) or {}).get('name') or t))}</a></td>"
            f"<td>{html.escape(str((profiles.get(t, {}) or {}).get('industry') or 'Unknown'))}</td>"
            f"<td>{html.escape(mcap_map.get(t) or _market_cap_for_ticker(t))}</td>"
            f"<td><form method='post' action='/company_list/remove' style='display:inline;'><input type='hidden' name='name' value='{html.escape(n)}'><input type='hidden' name='ticker' value='{html.escape(t)}'><button type='submit'>Remove</button></form></td>"
            "</tr>"
        )
        for t in f_tickers
    ) or "<tr><td colspan='5' class='muted'>No companies in this list.</td></tr>"

    notes_html = "".join(
        (
            "<tr>"
            f"<td>{html.escape(str(r['ticker'] or ''))}</td>"
            f"<td>{html.escape(str(r['created_at'] or '')[:19])}</td>"
            f"<td>{html.escape(str(r['action'] or ''))}</td>"
            f"<td>{html.escape(str(r['emotion'] or ''))}</td>"
            f"<td>{html.escape(str(r['note'] or '')[:260])}</td>"
            "</tr>"
        )
        for r in note_rows[:500]
    ) or "<tr><td colspan='5' class='muted'>No notes yet for companies in this list.</td></tr>"

    return (
        "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(n)} - Company List</title>"
        "<style>body{margin:0;background:#eaf0f4;color:#2f4358;font-family:'Avenir Next','Helvetica Neue',sans-serif;}"
        ".wrap{max-width:1200px;margin:0 auto;padding:14px;} .card{background:#f1f5f8;border:1px solid #d6dee6;border-radius:10px;padding:12px;margin-bottom:10px;}"
        ".btn{border:1px solid #c8d3dd;background:#edf2f6;color:#2f4358;border-radius:8px;padding:6px 10px;text-decoration:none;font-size:12px;font-weight:700;}"
        "input{width:100%;box-sizing:border-box;border:1px solid #c8d3dd;border-radius:8px;background:#edf2f6;color:#2f4358;padding:8px;}"
        "button{border:1px solid #ff7a59;background:#ff7a59;color:#fff;border-radius:8px;padding:8px 10px;font-weight:700;cursor:pointer;}"
        "table{width:100%;border-collapse:collapse;} th,td{border-bottom:1px solid #d6dee6;padding:8px;text-align:left;vertical-align:top;} .muted{color:#4f6780;}</style>"
        "</head><body><div class='wrap'>"
        f"<div class='card'><a class='btn' href='/'>Home</a> <a class='btn' href='/company_lists'>All Lists</a> <strong style='margin-left:8px;'>{html.escape(n)}</strong></div>"
        "<div class='card'><form method='get' action='/company_list' style='display:grid;grid-template-columns:1fr 1fr 1fr auto;gap:8px;'>"
        f"<input type='hidden' name='name' value='{html.escape(n)}'>"
        f"<input name='q' value='{html.escape(query)}' placeholder='Filter by ticker/name/industry/note/date'>"
        "<select name='sort'><option value='mcap_desc' " + ("selected" if sort_by == "mcap_desc" else "") + ">Sort: Market Cap ↓</option><option value='ticker_asc' " + ("selected" if sort_by == "ticker_asc" else "") + ">Sort: Ticker A-Z</option><option value='recent_added' " + ("selected" if sort_by == "recent_added" else "") + ">Sort: Recently Added</option></select>"
        "<button type='submit'>Filter</button>"
        "</form></div>"
        "<div class='card'><form method='post' action='/company_list/add' style='display:grid;grid-template-columns:1fr 1fr auto;gap:8px;'>"
        f"<input type='hidden' name='name' value='{html.escape(n)}'>"
        "<input name='ticker' placeholder='Add ticker (e.g. CRM)'>"
        "<input name='source' placeholder='Source (optional)' value='manual'>"
        "<button type='submit'>Add Company</button>"
        "</form></div>"
        "<div class='card'><h3 style='margin:0 0 8px 0;'>Companies</h3>"
        "<table><thead><tr><th>Ticker</th><th>Company</th><th>Industry</th><th>Market Cap</th><th>Actions</th></tr></thead><tbody>"
        f"{rows_html}</tbody></table></div>"
        "<div class='card'><h3 style='margin:0 0 8px 0;'>All Notes (Shared Company Source)</h3>"
        "<table><thead><tr><th>Ticker</th><th>Date</th><th>Action</th><th>Emotion</th><th>Note</th></tr></thead><tbody>"
        f"{notes_html}</tbody></table></div>"
        "</div></body></html>"
    )


def _clamp_score(value: object, default: int = 5) -> int:
    try:
        n = int(value)
    except Exception:
        n = int(default)
    return max(0, min(10, n))


def _workspace_stale_days(*timestamps: str) -> int:
    best: dt.datetime | None = None
    for ts in timestamps:
        d = _parse_iso_datetime(str(ts or ""))
        if d is None:
            continue
        if best is None or d > best:
            best = d
    if best is None:
        return 999
    return max(0, int((dt.datetime.now() - best).total_seconds() // 86400))


def get_workspace_profile(ticker: str) -> dict[str, object]:
    t = resolve_ticker_input(ticker)
    if not t:
        return {
            "ticker": "",
            "why_wrong": "",
            "score_growth": 5,
            "score_margin": 5,
            "score_capital": 5,
            "score_valuation": 5,
            "score_risk": 5,
            "updated_at": "",
        }
    conn = memory_db()
    row = conn.execute(
        """SELECT ticker, why_wrong, score_growth, score_margin, score_capital, score_valuation, score_risk, updated_at
           FROM workspace_profiles
           WHERE ticker = ?""",
        (t,),
    ).fetchone()
    conn.close()
    if not row:
        return {
            "ticker": t,
            "why_wrong": "",
            "score_growth": 5,
            "score_margin": 5,
            "score_capital": 5,
            "score_valuation": 5,
            "score_risk": 5,
            "updated_at": "",
        }
    return {
        "ticker": t,
        "why_wrong": str(row["why_wrong"] or ""),
        "score_growth": _to_int(row["score_growth"], 5),
        "score_margin": _to_int(row["score_margin"], 5),
        "score_capital": _to_int(row["score_capital"], 5),
        "score_valuation": _to_int(row["score_valuation"], 5),
        "score_risk": _to_int(row["score_risk"], 5),
        "updated_at": str(row["updated_at"] or ""),
    }


def save_workspace_profile(
    ticker: str,
    why_wrong: str,
    score_growth: object,
    score_margin: object,
    score_capital: object,
    score_valuation: object,
    score_risk: object,
) -> bool:
    t = resolve_ticker_input(ticker)
    if not t:
        return False
    ww = (why_wrong or "").strip()
    if not ww:
        return False
    now_s = dt.datetime.now().isoformat()
    conn = memory_db()
    conn.execute(
        """INSERT INTO workspace_profiles
           (ticker, why_wrong, score_growth, score_margin, score_capital, score_valuation, score_risk, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(ticker) DO UPDATE SET
             why_wrong=excluded.why_wrong,
             score_growth=excluded.score_growth,
             score_margin=excluded.score_margin,
             score_capital=excluded.score_capital,
             score_valuation=excluded.score_valuation,
             score_risk=excluded.score_risk,
             updated_at=excluded.updated_at""",
        (
            t,
            ww[:8000],
            _clamp_score(score_growth, 5),
            _clamp_score(score_margin, 5),
            _clamp_score(score_capital, 5),
            _clamp_score(score_valuation, 5),
            _clamp_score(score_risk, 5),
            now_s,
        ),
    )
    conn.commit()
    conn.close()
    return True


def list_workspace_profiles_map(tickers: list[str]) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    cleaned: list[str] = []
    for t in tickers:
        u = resolve_ticker_input(t)
        if u:
            cleaned.append(u)
    if not cleaned:
        return out
    uniq = sorted(set(cleaned))
    conn = memory_db()
    marks = ",".join("?" for _ in uniq)
    rows = conn.execute(
        f"""SELECT ticker, why_wrong, score_growth, score_margin, score_capital, score_valuation, score_risk, updated_at
            FROM workspace_profiles
            WHERE ticker IN ({marks})""",
        tuple(uniq),
    ).fetchall()
    conn.close()
    for r in rows:
        t = str(r["ticker"] or "").strip().upper()
        if not t:
            continue
        out[t] = {
            "why_wrong": str(r["why_wrong"] or ""),
            "score_growth": _to_int(r["score_growth"], 5),
            "score_margin": _to_int(r["score_margin"], 5),
            "score_capital": _to_int(r["score_capital"], 5),
            "score_valuation": _to_int(r["score_valuation"], 5),
            "score_risk": _to_int(r["score_risk"], 5),
            "updated_at": str(r["updated_at"] or ""),
        }
    return out


def save_workspace_thesis_snapshot(ticker: str, snapshot_month: str = "") -> bool:
    t = resolve_ticker_input(ticker)
    if not t:
        return False
    month = (snapshot_month or "").strip()
    if not re.match(r"^\d{4}-\d{2}$", month):
        month = dt.date.today().strftime("%Y-%m")
    conn = memory_db()
    c_row = conn.execute(
        "SELECT thesis FROM workspace_companies WHERE ticker = ?",
        (t,),
    ).fetchone()
    p_row = conn.execute(
        """SELECT why_wrong, score_growth, score_margin, score_capital, score_valuation, score_risk
           FROM workspace_profiles WHERE ticker = ?""",
        (t,),
    ).fetchone()
    thesis = str((c_row["thesis"] if c_row else "") or "")
    why_wrong = str((p_row["why_wrong"] if p_row else "") or "")
    score_total = 0
    if p_row:
        score_total = (
            _to_int(p_row["score_growth"], 0)
            + _to_int(p_row["score_margin"], 0)
            + _to_int(p_row["score_capital"], 0)
            + _to_int(p_row["score_valuation"], 0)
            + _to_int(p_row["score_risk"], 0)
        )
    conn.execute(
        """INSERT INTO workspace_thesis_snapshots
           (ticker, snapshot_month, thesis, why_wrong, score_total, created_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(ticker, snapshot_month) DO UPDATE SET
             thesis=excluded.thesis,
             why_wrong=excluded.why_wrong,
             score_total=excluded.score_total,
             created_at=excluded.created_at""",
        (t, month, thesis[:12000], why_wrong[:8000], max(0, min(50, score_total)), dt.datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    return True


def save_workspace_thesis_snapshot_if_missing(ticker: str, snapshot_month: str = "") -> bool:
    t = resolve_ticker_input(ticker)
    if not t:
        return False
    month = (snapshot_month or "").strip()
    if not re.match(r"^\d{4}-\d{2}$", month):
        month = dt.date.today().strftime("%Y-%m")
    conn = memory_db()
    c_row = conn.execute(
        "SELECT thesis FROM workspace_companies WHERE ticker = ?",
        (t,),
    ).fetchone()
    p_row = conn.execute(
        """SELECT why_wrong, score_growth, score_margin, score_capital, score_valuation, score_risk
           FROM workspace_profiles WHERE ticker = ?""",
        (t,),
    ).fetchone()
    thesis = str((c_row["thesis"] if c_row else "") or "")
    why_wrong = str((p_row["why_wrong"] if p_row else "") or "")
    score_total = 0
    if p_row:
        score_total = (
            _to_int(p_row["score_growth"], 0)
            + _to_int(p_row["score_margin"], 0)
            + _to_int(p_row["score_capital"], 0)
            + _to_int(p_row["score_valuation"], 0)
            + _to_int(p_row["score_risk"], 0)
        )
    cur = conn.execute(
        """INSERT OR IGNORE INTO workspace_thesis_snapshots
           (ticker, snapshot_month, thesis, why_wrong, score_total, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (t, month, thesis[:12000], why_wrong[:8000], max(0, min(50, score_total)), dt.datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    return int(cur.rowcount) > 0


def list_workspace_thesis_snapshots(ticker: str, limit: int = 24) -> list[sqlite3.Row]:
    t = resolve_ticker_input(ticker)
    if not t:
        return []
    conn = memory_db()
    rows = conn.execute(
        """SELECT id, ticker, snapshot_month, thesis, why_wrong, score_total, created_at
           FROM workspace_thesis_snapshots
           WHERE ticker = ?
           ORDER BY snapshot_month DESC, id DESC
           LIMIT ?""",
        (t, max(1, min(120, int(limit)))),
    ).fetchall()
    conn.close()
    return rows


def add_workspace_mortem(
    ticker: str,
    mortem_type: str,
    trigger_txt: str,
    hypothesis: str,
    outcome: str,
    lessons: str,
) -> int:
    t = resolve_ticker_input(ticker)
    if not t:
        return 0
    mt = (mortem_type or "premortem").strip().lower()
    if mt not in {"premortem", "postmortem"}:
        mt = "premortem"
    trig = (trigger_txt or "").strip()
    hypo = (hypothesis or "").strip()
    outc = (outcome or "").strip()
    les = (lessons or "").strip()
    if not trig and not hypo and not outc and not les:
        return 0
    conn = memory_db()
    cur = conn.execute(
        """INSERT INTO workspace_mortems
           (ticker, mortem_type, trigger_txt, hypothesis, outcome, lessons, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (t, mt, trig[:1500], hypo[:3000], outc[:3000], les[:3000], dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    rid = int(cur.lastrowid)
    conn.close()
    return rid


def list_workspace_mortems(ticker: str, limit: int = 60) -> list[sqlite3.Row]:
    t = resolve_ticker_input(ticker)
    if not t:
        return []
    conn = memory_db()
    rows = conn.execute(
        """SELECT id, ticker, mortem_type, trigger_txt, hypothesis, outcome, lessons, created_at
           FROM workspace_mortems
           WHERE ticker = ?
           ORDER BY id DESC
           LIMIT ?""",
        (t, max(1, min(200, int(limit)))),
    ).fetchall()
    conn.close()
    return rows


def workspace_weekly_review(days: int = 7, limit: int = 24) -> list[dict[str, object]]:
    d = max(1, min(30, int(days)))
    cutoff = (dt.datetime.now() - dt.timedelta(days=d)).strftime("%Y-%m-%d %H:%M:%S")
    conn = memory_db()
    rows = conn.execute(
        """SELECT ticker,
                  SUM(CASE WHEN action='Mistake' THEN 1 ELSE 0 END) AS mistakes,
                  SUM(CASE WHEN action='Lesson' THEN 1 ELSE 0 END) AS lessons,
                  SUM(CASE WHEN action='Buy' THEN 1 ELSE 0 END) AS buys,
                  SUM(CASE WHEN action='Sell' THEN 1 ELSE 0 END) AS sells,
                  SUM(CASE WHEN action='Note' THEN 1 ELSE 0 END) AS notes,
                  COUNT(*) AS total_logs
           FROM workspace_journal
           WHERE created_at >= ?
           GROUP BY ticker
           ORDER BY mistakes DESC, lessons DESC, total_logs DESC, ticker ASC
           LIMIT ?""",
        (cutoff, max(1, min(100, int(limit)))),
    ).fetchall()
    conn.close()
    out: list[dict[str, object]] = []
    for r in rows:
        out.append(
            {
                "ticker": str(r["ticker"] or ""),
                "mistakes": _to_int(r["mistakes"], 0),
                "lessons": _to_int(r["lessons"], 0),
                "buys": _to_int(r["buys"], 0),
                "sells": _to_int(r["sells"], 0),
                "notes": _to_int(r["notes"], 0),
                "total_logs": _to_int(r["total_logs"], 0),
            }
        )
    return out


def _safe_float(v: object) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _safe_date(v: str) -> dt.date | None:
    s = (v or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None


def get_workspace_valuation(ticker: str) -> dict[str, object]:
    t = resolve_ticker_input(ticker)
    if not t:
        return {
            "ticker": "",
            "intrinsic_market_cap_b": None,
            "intrinsic_price": None,
            "strike_starter": None,
            "strike_add": None,
            "strike_aggressive": None,
            "invalidation_trigger": "",
            "confidence": 50,
            "updated_at": "",
        }
    conn = memory_db()
    row = conn.execute(
        """SELECT ticker, intrinsic_market_cap_b, intrinsic_price, strike_starter, strike_add, strike_aggressive,
                  invalidation_trigger, confidence, updated_at
           FROM workspace_valuation WHERE ticker = ?""",
        (t,),
    ).fetchone()
    conn.close()
    if not row:
        return {
            "ticker": t,
            "intrinsic_market_cap_b": None,
            "intrinsic_price": None,
            "strike_starter": None,
            "strike_add": None,
            "strike_aggressive": None,
            "invalidation_trigger": "",
            "confidence": 50,
            "updated_at": "",
        }
    return {
        "ticker": t,
        "intrinsic_market_cap_b": _safe_float(row["intrinsic_market_cap_b"]),
        "intrinsic_price": _safe_float(row["intrinsic_price"]),
        "strike_starter": _safe_float(row["strike_starter"]),
        "strike_add": _safe_float(row["strike_add"]),
        "strike_aggressive": _safe_float(row["strike_aggressive"]),
        "invalidation_trigger": str(row["invalidation_trigger"] or ""),
        "confidence": max(0, min(100, _to_int(row["confidence"], 50))),
        "updated_at": str(row["updated_at"] or ""),
    }


def save_workspace_valuation(
    ticker: str,
    intrinsic_market_cap_b: object,
    intrinsic_price: object,
    strike_starter: object,
    strike_add: object,
    strike_aggressive: object,
    invalidation_trigger: str,
    confidence: object,
) -> bool:
    t = resolve_ticker_input(ticker)
    if not t:
        return False
    conf = max(0, min(100, _to_int(confidence, 50)))
    inv = (invalidation_trigger or "").strip()
    conn = memory_db()
    conn.execute(
        """INSERT INTO workspace_valuation
           (ticker, intrinsic_market_cap_b, intrinsic_price, strike_starter, strike_add, strike_aggressive, invalidation_trigger, confidence, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(ticker) DO UPDATE SET
             intrinsic_market_cap_b=excluded.intrinsic_market_cap_b,
             intrinsic_price=excluded.intrinsic_price,
             strike_starter=excluded.strike_starter,
             strike_add=excluded.strike_add,
             strike_aggressive=excluded.strike_aggressive,
             invalidation_trigger=excluded.invalidation_trigger,
             confidence=excluded.confidence,
             updated_at=excluded.updated_at""",
        (
            t,
            _safe_float(intrinsic_market_cap_b),
            _safe_float(intrinsic_price),
            _safe_float(strike_starter),
            _safe_float(strike_add),
            _safe_float(strike_aggressive),
            inv[:4000],
            conf,
            dt.datetime.now().isoformat(),
        ),
    )
    conn.commit()
    conn.close()
    return True


def list_workspace_valuation_map(tickers: list[str]) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    cleaned: list[str] = []
    for t in tickers:
        u = resolve_ticker_input(t)
        if u:
            cleaned.append(u)
    if not cleaned:
        return out
    uniq = sorted(set(cleaned))
    conn = memory_db()
    marks = ",".join("?" for _ in uniq)
    rows = conn.execute(
        f"""SELECT ticker, intrinsic_market_cap_b, intrinsic_price, strike_starter, strike_add, strike_aggressive,
                   invalidation_trigger, confidence, updated_at
            FROM workspace_valuation
            WHERE ticker IN ({marks})""",
        tuple(uniq),
    ).fetchall()
    conn.close()
    for r in rows:
        t = str(r["ticker"] or "").strip().upper()
        if not t:
            continue
        out[t] = {
            "intrinsic_market_cap_b": _safe_float(r["intrinsic_market_cap_b"]),
            "intrinsic_price": _safe_float(r["intrinsic_price"]),
            "strike_starter": _safe_float(r["strike_starter"]),
            "strike_add": _safe_float(r["strike_add"]),
            "strike_aggressive": _safe_float(r["strike_aggressive"]),
            "invalidation_trigger": str(r["invalidation_trigger"] or ""),
            "confidence": max(0, min(100, _to_int(r["confidence"], 50))),
            "updated_at": str(r["updated_at"] or ""),
        }
    return out


def _ritual_templates_for_date(d: dt.date) -> list[tuple[str, str]]:
    w = d.weekday()  # Mon=0
    out: list[tuple[str, str]] = [
        ("daily_am_brief", "Daily AM brief + portfolio deltas"),
        ("daily_pm_recap", "Daily PM recap + update action queue"),
    ]
    if w <= 3:
        out.append(("weekday_deep_work", "Deep work block: 10-K / 10-Q / transcript"))
    if w == 4:
        out.append(("friday_13f", "Friday 13F smart-money scan"))
    if w == 5:
        out.append(("saturday_thesis", "Saturday thesis health check (owner mindset)"))
    return out


def ensure_workspace_ritual_tasks(days_ahead: int = 10) -> None:
    n = max(3, min(30, int(days_ahead)))
    conn = memory_db()
    now_s = dt.datetime.now().isoformat()
    today = dt.date.today()
    for i in range(n):
        d = today + dt.timedelta(days=i)
        due = d.isoformat()
        for key, title in _ritual_templates_for_date(d):
            conn.execute(
                """INSERT OR IGNORE INTO workspace_ritual_tasks
                   (ritual_key, title, due_date, status, notes, created_at, completed_at)
                   VALUES (?, ?, ?, 'open', '', ?, '')""",
                (key, title, due, now_s),
            )
    conn.commit()
    conn.close()


def list_workspace_ritual_tasks(limit: int = 120, status: str = "open") -> list[sqlite3.Row]:
    ensure_workspace_ritual_tasks(days_ahead=10)
    st = (status or "open").strip().lower()
    if st not in {"open", "done", "all"}:
        st = "open"
    conn = memory_db()
    if st == "all":
        rows = conn.execute(
            """SELECT id, ritual_key, title, due_date, status, notes, created_at, completed_at
               FROM workspace_ritual_tasks
               ORDER BY due_date ASC, id ASC
               LIMIT ?""",
            (max(1, min(500, int(limit))),),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, ritual_key, title, due_date, status, notes, created_at, completed_at
               FROM workspace_ritual_tasks
               WHERE status = ?
               ORDER BY due_date ASC, id ASC
               LIMIT ?""",
            (st, max(1, min(500, int(limit)))),
        ).fetchall()
    conn.close()
    return rows


def complete_workspace_ritual_task(task_id: int, note: str = "") -> bool:
    conn = memory_db()
    cur = conn.execute(
        "UPDATE workspace_ritual_tasks SET status='done', notes=?, completed_at=? WHERE id=?",
        ((note or "").strip()[:1200], dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(task_id)),
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def workspace_today_payload() -> dict[str, object]:
    rows = list_workspace_companies(limit=600)
    tickers = [str(r["ticker"] or "").strip().upper() for r in rows if str(r["ticker"] or "").strip()]
    quotes = get_live_quotes(tickers, ttl_seconds=120) if tickers else {}
    snap = load_intel24_snapshot_map(max_age_seconds=3600)
    profiles = list_workspace_profiles_map(tickers)
    vals = list_workspace_valuation_map(tickers)
    priorities: list[dict[str, object]] = []
    for r in rows:
        t = str(r["ticker"] or "").strip().upper()
        if not t:
            continue
        s = snap.get(t, {}) if isinstance(snap, dict) else {}
        p = profiles.get(t, {}) if isinstance(profiles, dict) else {}
        v = vals.get(t, {}) if isinstance(vals, dict) else {}
        q = quotes.get(t, {}) if isinstance(quotes, dict) else {}
        day = _safe_float(q.get("day_pct"))
        px = _safe_float(q.get("price"))
        ev = _to_int(s.get("event_score"), 0)
        stale = _workspace_stale_days(str(r["updated_at"] or ""), str(p.get("updated_at") or ""))
        reason: list[str] = []
        score = 0
        if ev >= 60:
            score += 4
            reason.append(f"high signal {ev}")
        elif ev >= 40:
            score += 2
            reason.append(f"signal {ev}")
        if stale >= 90:
            score += 3
            reason.append(f"stale {stale}d")
        elif stale >= 60:
            score += 2
            reason.append(f"aging {stale}d")
        if isinstance(day, float) and abs(day) >= 5:
            score += 2
            reason.append(f"move {day:+.2f}%")
        starter = _safe_float(v.get("strike_starter"))
        add_px = _safe_float(v.get("strike_add"))
        aggr = _safe_float(v.get("strike_aggressive"))
        if isinstance(px, float):
            if isinstance(aggr, float) and px <= aggr:
                score += 4
                reason.append("<= aggressive strike")
            elif isinstance(add_px, float) and px <= add_px:
                score += 3
                reason.append("<= add strike")
            elif isinstance(starter, float) and px <= starter:
                score += 2
                reason.append("<= starter strike")
        if score > 0:
            priorities.append(
                {
                    "ticker": t,
                    "score": score,
                    "reason": "; ".join(reason),
                    "suggestion": str(s.get("suggestion") or "Review"),
                }
            )
    priorities.sort(key=lambda x: int(x.get("score") or 0), reverse=True)
    rituals = list_workspace_ritual_tasks(limit=80, status="open")
    today = dt.date.today().isoformat()
    today_rituals = [r for r in rituals if str(r["due_date"] or "") == today]
    week_end = (dt.date.today() + dt.timedelta(days=7)).isoformat()
    week_rituals = [r for r in rituals if today <= str(r["due_date"] or "") <= week_end]
    return {
        "today": [
            {
                "ticker": str(x["ticker"]),
                "score": int(x["score"]),
                "reason": str(x["reason"]),
                "suggestion": str(x["suggestion"]),
            }
            for x in priorities[:3]
        ],
        "must_week": [
            {
                "id": int(r["id"]),
                "title": str(r["title"] or ""),
                "due_date": str(r["due_date"] or ""),
            }
            for r in week_rituals[:8]
        ],
        "blocking": [
            {
                "ticker": str(x["ticker"]),
                "reason": str(x["reason"]),
            }
            for x in priorities
            if ("stale" in str(x.get("reason") or "") or "aging" in str(x.get("reason") or ""))
        ][:6],
        "ritual_today": [
            {
                "id": int(r["id"]),
                "ritual_key": str(r["ritual_key"] or ""),
                "title": str(r["title"] or ""),
                "due_date": str(r["due_date"] or ""),
            }
            for r in today_rituals[:8]
        ],
    }


def _latest_existing(paths: list[Path]) -> str:
    for p in paths:
        if p.exists():
            try:
                return p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
    return ""


def workspace_13f_digest() -> dict[str, object]:
    # Pull from local generated reports if present.
    cand = sorted((REPORTS.glob("signal_tracker_*.md")), reverse=True)
    txt = _latest_existing(cand[:5]) if cand else ""
    lines: list[str] = []
    if txt:
        for ln in txt.splitlines():
            s = ln.strip().lstrip("-* ").strip()
            if not s:
                continue
            low = s.lower()
            if ("insider" in low or "smart money" in low or "52-week" in low or "5-year low" in low) and len(s) > 18:
                lines.append(_compact_signal_text(s, max_chars=220))
            if len(lines) >= 6:
                break
    if not lines:
        lines = [
            "No local 13F/smart-money digest detected yet.",
            "Run your Friday data refresh and this card will auto-populate.",
        ]
    return {"ok": True, "asof": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "items": lines}


def workspace_saturday_review_cards() -> list[dict[str, object]]:
    recent_watchlist_days = _workspace_recent_watchlist_days()
    rows = list_workspace_companies(limit=600)
    by_ticker = {str(r["ticker"] or "").strip().upper(): r for r in rows if str(r["ticker"] or "").strip()}
    port = [r for r in rows if str(r["stage"] or "") == "Portfolio"]

    # Include recently added watchlist names so new ideas are forced into weekly review.
    recent_watch: list[str] = []
    wl = read_watchlist_entries(DATA / "my_watchlist.txt")
    today = dt.date.today()
    for e in wl:
        t = str(e.get("ticker") or "").strip().upper()
        if not t:
            continue
        d = _safe_date(str(e.get("added_at") or ""))
        if d is None:
            continue
        if 0 <= (today - d).days <= recent_watchlist_days:
            recent_watch.append(t)

    tickers = [str(r["ticker"] or "").strip().upper() for r in port if str(r["ticker"] or "").strip()]
    for t in recent_watch:
        if t not in tickers:
            tickers.append(t)
    vals = list_workspace_valuation_map(tickers)
    profs = list_workspace_profiles_map(tickers)
    out: list[dict[str, object]] = []
    for t in tickers:
        if not t:
            continue
        r = by_ticker.get(t)
        v = vals.get(t, {})
        p = profs.get(t, {})
        stage = str((r["stage"] if r else "Watchlist") or "Watchlist")
        conviction = _to_int((r["conviction"] if r else 5), 5)
        thesis_updated = str((r["updated_at"] if r else "") or "")
        added_days = None
        for e in wl:
            if str(e.get("ticker") or "").strip().upper() == t:
                d = _safe_date(str(e.get("added_at") or ""))
                if d is not None:
                    added_days = max(0, (today - d).days)
                break
        out.append(
            {
                "ticker": t,
                "stage": stage,
                "recent_watchlist": bool(stage == "Watchlist" and (added_days is not None and added_days <= recent_watchlist_days)),
                "watch_added_days": added_days,
                "conviction": conviction,
                "thesis_updated_at": thesis_updated,
                "profile_updated_at": str(p.get("updated_at") or ""),
                "intrinsic_price": _safe_float(v.get("intrinsic_price")),
                "starter": _safe_float(v.get("strike_starter")),
                "add": _safe_float(v.get("strike_add")),
                "aggressive": _safe_float(v.get("strike_aggressive")),
                "invalidation": str(v.get("invalidation_trigger") or ""),
                "confidence": _to_int(v.get("confidence"), 50),
            }
        )
    out.sort(
        key=lambda x: (
            0 if str(x.get("stage") or "") == "Portfolio" else 1,
            0 if bool(x.get("recent_watchlist")) else 1,
            str(x.get("ticker") or ""),
        )
    )
    return out


def add_todo(task: str, priority: str = "P2", due_date: str = "", ticker: str = "", category: str = "general") -> int:
    txt = (task or "").strip()
    if not txt:
        return 0
    quick_prefix = False
    for pfx in ("! ", "!quick ", "[quick] ", "quick: "):
        if txt.lower().startswith(pfx):
            txt = txt[len(pfx) :].strip()
            quick_prefix = True
            break
    if not txt:
        return 0
    p = (priority or "P2").strip().upper()
    if p not in {"P1", "P2", "P3"}:
        p = "P2"
    due = (due_date or "").strip()
    if due and not re.match(r"^\d{4}-\d{2}-\d{2}$", due):
        due = ""
    t = resolve_ticker_input(ticker)
    cat = (category or "general").strip().lower()
    if cat not in {"general", "company", "project", "admin", "quick"}:
        cat = "general"
    if quick_prefix and not t:
        cat = "quick"
    if t and cat == "general":
        cat = "company"
    conn = memory_db()
    cur = conn.execute(
        """INSERT INTO todos (task, status, created_at, priority, due_date, ticker, category)
           VALUES (?, 'open', ?, ?, ?, ?, ?)""",
        (txt[:1000], dt.datetime.now().isoformat(), p, due, t or "", cat),
    )
    conn.commit()
    tid = int(cur.lastrowid)
    conn.close()
    return tid


def toggle_todo(todo_id: int) -> bool:
    conn = memory_db()
    row = conn.execute("SELECT status, category FROM todos WHERE id = ?", (todo_id,)).fetchone()
    if not row:
        conn.close()
        return False
    cur_status = str(row["status"] or "open").strip().lower()
    cat = str(row["category"] or "general").strip().lower()
    if cur_status == "open":
        new_status = "archived" if cat == "quick" else "done"
    else:
        new_status = "open"
    conn.execute("UPDATE todos SET status = ? WHERE id = ?", (new_status, todo_id))
    conn.commit()
    conn.close()
    return True


def update_todo(
    todo_id: int,
    task: str,
    priority: str = "P2",
    due_date: str = "",
    ticker: str | None = None,
    category: str | None = None,
) -> bool:
    txt = (task or "").strip()
    if not txt:
        return False
    p = (priority or "P2").strip().upper()
    if p not in {"P1", "P2", "P3"}:
        p = "P2"
    due = (due_date or "").strip()
    if due and not re.match(r"^\d{4}-\d{2}-\d{2}$", due):
        due = ""
    conn = memory_db()
    sets = ["task = ?", "priority = ?", "due_date = ?"]
    vals: list[object] = [txt, p, due]
    if ticker is not None:
        vals.append(resolve_ticker_input(ticker) or "")
        sets.append("ticker = ?")
    if category is not None:
        cat = (category or "general").strip().lower()
        if cat not in {"general", "company", "project", "admin", "quick"}:
            cat = "general"
        sets.append("category = ?")
        vals.append(cat)
    vals.append(todo_id)
    cur = conn.execute(
        f"UPDATE todos SET {', '.join(sets)} WHERE id = ?",
        tuple(vals),
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def archive_done_todos() -> int:
    conn = memory_db()
    cur = conn.execute("UPDATE todos SET status = 'archived' WHERE status = 'done'")
    conn.commit()
    n = int(cur.rowcount)
    conn.close()
    return n


def delete_todo(todo_id: int) -> bool:
    conn = memory_db()
    cur = conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def carry_forward_todo(todo_id: int, ticker: str = "", note: str = "") -> tuple[bool, int]:
    conn = memory_db()
    row = conn.execute(
        "SELECT task, status, priority, due_date FROM todos WHERE id = ?",
        (todo_id,),
    ).fetchone()
    conn.close()
    if not row:
        return False, 0
    task = str(row["task"] or "").strip()
    if not task:
        return False, 0
    status = str(row["status"] or "open").strip().lower()
    priority = str(row["priority"] or "P2").strip().upper()
    due = str(row["due_date"] or "").strip()
    if priority not in {"P1", "P2", "P3"}:
        priority = "P2"
    if due and not re.match(r"^\d{4}-\d{2}-\d{2}$", due):
        due = ""
    if status != "open":
        # Reopen if it was accidentally closed before carrying forward.
        toggle_todo(todo_id)
    next_due = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    _ = update_todo(todo_id, task=task, priority=priority, due_date=(due or next_due))

    t = resolve_ticker_input(ticker)
    extra = (note or "").strip() or "Not done yet. Carrying forward."
    scope = "portfolio" if t and any((str(r[0] or "").strip().upper() == t) for r in read_portfolio_rows(DATA / "portfolio.csv")) else "watchlist"
    if not t:
        scope = "general"
    nid = add_note_row(
        scope=scope,
        ticker=t,
        sentiment="watch",
        note=f"[Carry Forward] {task} | {extra}",
        tags="workspace,carry_forward",
    )
    return True, int(nid)


def get_scratchpad() -> str:
    conn = memory_db()
    row = conn.execute("SELECT content FROM scratchpad WHERE id = 1").fetchone()
    conn.close()
    return str(row["content"] or "") if row else ""


def get_scratchpad_pinned() -> str:
    conn = memory_db()
    row = conn.execute("SELECT pinned FROM scratchpad WHERE id = 1").fetchone()
    conn.close()
    return str(row["pinned"] or "") if row else ""


def save_scratchpad(content: str, pinned: str | None = None) -> None:
    conn = memory_db()
    if pinned is None:
        conn.execute(
            "UPDATE scratchpad SET content = ?, last_updated = ? WHERE id = 1",
            (content, dt.datetime.now().isoformat()),
        )
    else:
        conn.execute(
            "UPDATE scratchpad SET content = ?, pinned = ?, last_updated = ? WHERE id = 1",
            (content, pinned, dt.datetime.now().isoformat()),
        )
    conn.commit()
    conn.close()


def _decision_log_path() -> Path:
    return DATA / "decision_log.json"


def _detect_sentiment_tag(text: str) -> str:
    s = (text or "").lower()
    if re.search(r"\bbullish\b", s):
        return "BULLISH"
    if re.search(r"\bbearish\b", s):
        return "BEARISH"
    return ""


def _extract_tickers_from_log(text: str) -> list[str]:
    found = re.findall(r"\$([A-Za-z]{1,6})\b", text or "")
    out: list[str] = []
    seen: set[str] = set()
    for t in found:
        tu = t.upper()
        if tu not in seen:
            seen.add(tu)
            out.append(tu)
    return out


def _load_decision_log() -> list[dict[str, object]]:
    path = _decision_log_path()
    legacy = DATA / "decision_log.txt"
    rows: list[dict[str, object]] = []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        rows.append(item)
        except Exception:
            rows = []
    elif legacy.exists():
        # One-time migration from legacy TSV to JSON.
        for ln in legacy.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not ln.strip():
                continue
            parts = ln.split("\t", 2)
            if len(parts) == 3:
                ts, _, txt = parts
            elif len(parts) == 2:
                ts, txt = parts
            else:
                ts, txt = "", ln
            clean = re.sub(r"\s+", " ", txt).strip()
            if not clean:
                continue
            rows.append(
                {
                    "id": uuid.uuid4().hex[:12],
                    "timestamp": (ts or "").strip() or dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "text": clean,
                    "sentiment": _detect_sentiment_tag(clean),
                    "tags": [f"${t}" for t in _extract_tickers_from_log(clean)],
                }
            )
        if rows:
            _save_decision_log(rows)
    return rows


def _save_decision_log(rows: list[dict[str, object]]) -> None:
    path = _decision_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=True, indent=2), encoding="utf-8")


def save_or_update_decision_log(text: str, edit_id: str = "") -> tuple[str, str]:
    raw = (text or "").strip()
    if not raw:
        return "", ""
    clean = re.sub(r"\s+", " ", raw).strip()
    if not clean:
        return "", ""
    ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sent = _detect_sentiment_tag(clean)
    tags = [f"${t}" for t in _extract_tickers_from_log(clean)]
    rows = _load_decision_log()
    eid = (edit_id or "").strip()
    if eid:
        for r in rows:
            if str(r.get("id", "")) == eid:
                r["timestamp"] = ts
                r["text"] = clean
                r["sentiment"] = sent
                r["tags"] = tags
                _save_decision_log(rows)
                invalidate_l2_cache(start_async=True)
                return "updated", eid
    new_id = uuid.uuid4().hex[:12]
    rows.append({"id": new_id, "timestamp": ts, "text": clean, "sentiment": sent, "tags": tags})
    _save_decision_log(rows)
    invalidate_l2_cache(start_async=True)
    return "created", new_id


def delete_decision_log(entry_id: str) -> bool:
    eid = (entry_id or "").strip()
    if not eid:
        return False
    rows = _load_decision_log()
    out = [r for r in rows if str(r.get("id", "")) != eid]
    if len(out) == len(rows):
        return False
    _save_decision_log(out)
    invalidate_l2_cache(start_async=True)
    return True


def list_decision_log(limit: int = 300) -> list[dict[str, str]]:
    rows = _load_decision_log()
    out: list[dict[str, str]] = []
    for r in reversed(rows):
        txt = str(r.get("text", "")).strip()
        if not txt:
            continue
        out.append(
            {
                "id": str(r.get("id", "")).strip(),
                "timestamp": str(r.get("timestamp", "")).strip(),
                "sentiment": str(r.get("sentiment", "")).strip(),
                "text": txt,
                "tags": ",".join([str(x) for x in (r.get("tags") or [])]),
            }
        )
        if len(out) >= limit:
            break
    return out


def _render_log_text_with_tickers(text: str) -> str:
    s = html.escape(text or "")

    def repl(m: re.Match[str]) -> str:
        raw = m.group(0) or ""
        t = (m.group(1) or "").upper()
        return f"<a class='tkr' href='/company?t={html.escape(t)}&tab=overview'>{html.escape(raw)}</a>"

    return re.sub(r"\$([A-Za-z]{1,6})\b", repl, s)


def list_stock_thesis(ticker: str, limit: int = 8) -> list[sqlite3.Row]:
    conn = memory_db()
    rows = conn.execute(
        "SELECT id, ticker, sentiment, content, date FROM stock_thesis WHERE ticker = ? ORDER BY date DESC LIMIT ?",
        (ticker.upper().strip(), limit),
    ).fetchall()
    conn.close()
    return rows


_THESIS_METRICS = [
    "Revenue Growth",
    "Gross Margin",
    "ROIC",
    "Debt/EBITDA",
    "Free Cash Flow",
    "PE Ratio",
]


def _ai_route_thesis_source(thesis_text: str) -> tuple[str, str]:
    prompt = (
        "Classify the user's thesis into exactly one source type.\n"
        f"Thesis: '{thesis_text}'\n"
        "Types:\n"
        "- FINANCIAL: margins, growth, debt, cash flow, valuation\n"
        "- SENTIMENT: brand, regulation, competitor narrative, market sentiment\n"
        "- INSIDER: management alignment, founder behavior, insider activity\n"
        "Return ONLY one word: FINANCIAL or SENTIMENT or INSIDER."
    )
    system_message = (
        "You are a strict router. Output exactly one token from this set: "
        "FINANCIAL, SENTIMENT, INSIDER."
    )
    try:
        if _hybrid_ask_ai is None:
            raise RuntimeError("llm_engine unavailable")
        raw = (_hybrid_ask_ai(prompt, system_message) or "").strip().upper()
    except Exception as e:
        return "FINANCIAL", f"AI route fallback: {str(e)[:120]}"
    if "SENTIMENT" in raw:
        return "SENTIMENT", ""
    if "INSIDER" in raw:
        return "INSIDER", ""
    if "FINANCIAL" in raw:
        return "FINANCIAL", ""
    return "FINANCIAL", f"AI route fallback: unsupported='{raw[:40]}'"


def _normalize_metric_name(raw: str) -> str | None:
    s = (raw or "").strip().lower()
    if ("revenue" in s and "growth" in s) or s == "revenue growth":
        return "Revenue Growth"
    if "gross" in s and "margin" in s:
        return "Gross Margin"
    if "roic" in s:
        return "ROIC"
    if "debt" in s and "ebitda" in s:
        return "Debt/EBITDA"
    if "free" in s and "cash" in s:
        return "Free Cash Flow"
    if "p/e" in s or "pe ratio" in s or (s == "pe") or ("forward pe" in s):
        return "PE Ratio"
    return None


def _ai_select_metric(thesis_text: str) -> tuple[str | None, str]:
    prompt = (
        f"The user believes: '{thesis_text}'. "
        "What ONE specific financial metric from yfinance best proves or disproves this? "
        "(Choose from: Revenue Growth, Gross Margin, ROIC, Debt/EBITDA, Free Cash Flow, or PE Ratio). "
        "Return ONLY the metric name."
    )
    system_message = (
        "You are a strict financial metric classifier. "
        "Return exactly one metric name from this list only: "
        "Revenue Growth, Gross Margin, ROIC, Debt/EBITDA, Free Cash Flow, PE Ratio."
    )
    try:
        if _hybrid_ask_ai is None:
            raise RuntimeError("llm_engine unavailable")
        raw = _hybrid_ask_ai(prompt, system_message)
    except Exception as e:
        return None, f"AI unavailable: {str(e)[:120]}"
    metric = _normalize_metric_name(raw)
    if metric is None:
        return None, f"AI returned unsupported metric: {str(raw)[:80]}"
    return metric, ""


def _as_pct(v: float | None) -> str:
    return f"{v * 100.0:.2f}%" if v is not None else "N/A"


def _metric_value_check(ticker: str, metric: str) -> str:
    if yf is None:
        return "N/A (yfinance not loaded)"
    tk = yf.Ticker(ticker)
    fi = getattr(tk, "fast_info", None) or {}
    info = getattr(tk, "info", None) or {}
    fin = getattr(tk, "financials", None)
    bal = getattr(tk, "balance_sheet", None)

    def _arrow(delta: float) -> str:
        return "↑" if delta > 0 else ("↓" if delta < 0 else "→")

    def _pick_row(df: object, keys: list[str]) -> str | None:
        if df is None or not hasattr(df, "index"):
            return None
        idx = getattr(df, "index")
        for k in keys:
            if k in idx:
                return k
        return None

    def _latest_two(df: object, row_name: str) -> tuple[float | None, float | None]:
        if df is None or not hasattr(df, "columns") or not hasattr(df, "at"):
            return None, None
        cols = list(getattr(df, "columns"))
        if len(cols) < 2:
            return None, None
        v0 = _to_num(df.at[row_name, cols[0]]) if row_name in getattr(df, "index") else None
        v1 = _to_num(df.at[row_name, cols[1]]) if row_name in getattr(df, "index") else None
        return v0, v1

    if metric == "Revenue Growth":
        rev_row = _pick_row(fin, ["Total Revenue", "Revenue"])
        if rev_row:
            rev0, rev1 = _latest_two(fin, rev_row)
            if rev0 is not None and rev1 not in (None, 0.0):
                yoy = (rev0 - rev1) / abs(rev1) * 100.0
                return f"{_fmt_big(rev0)} (Checking Trend... {_arrow(yoy)} {yoy:+.2f}% vs prior year)"
        rg = _to_num(info.get("revenueGrowth"))
        return f"{rg * 100.0:.2f}% (YoY revenue growth)" if rg is not None else "N/A"

    if metric == "Gross Margin":
        gp_row = _pick_row(fin, ["Gross Profit"])
        rev_row = _pick_row(fin, ["Total Revenue", "Revenue"])
        if gp_row and rev_row:
            gp0, gp1 = _latest_two(fin, gp_row)
            rev0, rev1 = _latest_two(fin, rev_row)
            if gp0 is not None and gp1 is not None and rev0 not in (None, 0.0) and rev1 not in (None, 0.0):
                m0 = gp0 / rev0 * 100.0
                m1 = gp1 / rev1 * 100.0
                bps = (m0 - m1) * 100.0
                return f"{m0:.2f}% (Checking Trend... {_arrow(bps)} {bps:+.0f} bps vs prior year)"
        gm = _to_num(info.get("grossMargins"))
        return f"{gm * 100.0:.2f}% (gross margin)" if gm is not None else "N/A"

    if metric == "ROIC":
        ebit_row = _pick_row(fin, ["EBIT", "Operating Income"])
        ta_row = _pick_row(bal, ["Total Assets"])
        cl_row = _pick_row(bal, ["Current Liabilities"])
        if ebit_row and ta_row and cl_row:
            e0, e1 = _latest_two(fin, ebit_row)
            ta0, ta1 = _latest_two(bal, ta_row)
            cl0, cl1 = _latest_two(bal, cl_row)
            if (
                e0 is not None and e1 is not None and ta0 is not None and ta1 is not None
                and cl0 is not None and cl1 is not None and (ta0 - cl0) != 0 and (ta1 - cl1) != 0
            ):
                r0 = (e0 / (ta0 - cl0)) * 100.0
                r1 = (e1 / (ta1 - cl1)) * 100.0
                bps = (r0 - r1) * 100.0
                return f"{r0:.2f}% (Checking Trend... {_arrow(bps)} {bps:+.0f} bps vs prior year)"
        roic = _to_num(info.get("returnOnInvestedCapital"))
        if roic is not None:
            return f"{roic * 100.0:.2f}% (trailing ROIC)"
        return "N/A"

    if metric == "Debt/EBITDA":
        debt = _to_num(info.get("totalDebt")) or _to_num(fi.get("totalDebt"))
        ebitda = _to_num(info.get("ebitda")) or _to_num(fi.get("ebitda"))
        if debt is not None and ebitda not in (None, 0.0):
            return f"{debt / ebitda:.2f}x (Debt/EBITDA)"
        return "N/A"

    if metric == "Free Cash Flow":
        fcf = _to_num(info.get("freeCashflow")) or _to_num(fi.get("freeCashFlow"))
        if fcf is not None:
            return f"{_fmt_big(fcf)} (TTM free cash flow)"
        cf = getattr(tk, "cashflow", None)
        if cf is not None and hasattr(cf, "index") and hasattr(cf, "columns"):
            cols = list(cf.columns)
            if cols and "Free Cash Flow" in cf.index:
                f0 = _to_num(cf.at["Free Cash Flow", cols[0]])
                if f0 is not None:
                    return f"{_fmt_big(f0)} (latest annual free cash flow)"
        return "N/A"

    if metric == "PE Ratio":
        pe = _to_num(info.get("forwardPE")) or _to_num(fi.get("forwardPE")) or _to_num(info.get("trailingPE"))
        return f"{pe:.2f}x (forward PE)" if pe is not None else "N/A"

    return "N/A"


def _thesis_keywords(thesis_text: str, limit: int = 4) -> list[str]:
    stop = {
        "the",
        "and",
        "that",
        "this",
        "from",
        "with",
        "will",
        "they",
        "them",
        "their",
        "have",
        "been",
        "into",
        "about",
        "because",
        "always",
        "needs",
        "need",
        "very",
        "just",
        "great",
    }
    toks = [t.lower() for t in re.findall(r"[A-Za-z]{4,}", thesis_text or "")]
    out: list[str] = []
    seen: set[str] = set()
    for t in toks:
        if t in stop or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= limit:
            break
    return out


def _news_check(ticker: str, thesis_text: str) -> str:
    kw = _thesis_keywords(thesis_text, limit=3)
    q = f"{ticker} {' '.join(kw)}"
    rss_url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(q)
    try:
        with urllib.request.urlopen(rss_url, timeout=12) as resp:
            xml_bytes = resp.read()
        root = ET.fromstring(xml_bytes)
        items = root.findall(".//item")
        titles = []
        for it in items[:8]:
            t = (it.findtext("title") or "").strip()
            if t:
                titles.append(t)
        if not titles:
            return "Found 0 recent articles for this thesis route."
        return f"Found {len(titles)} recent articles. Top headline: {titles[0]}"
    except Exception as e:
        return f"News lookup unavailable ({str(e)[:90]})."


def _insider_check_short(ticker: str) -> str:
    d = _sec_insider_details(ticker, months=3, meaningful_usd=100000.0)
    events = [e for e in d.get("events", []) if isinstance(e, dict)]
    cs = [e for e in events if "Officer" in str(e.get("role", ""))]
    buy_v = sum(float(_to_num(e.get("value")) or 0.0) for e in cs if str(e.get("code")) == "P")
    sell_v = sum(float(_to_num(e.get("value")) or 0.0) for e in cs if str(e.get("code")) == "S")
    net = buy_v - sell_v
    side = "BUY" if net > 0 else ("SELL" if net < 0 else "NEUTRAL")
    return f"Net Insider Activity (last 3 months, C-suite): {side} ({fmt_money(net)}; buys={fmt_money(buy_v)}, sells={fmt_money(sell_v)})."


def _thesis_fact_check(ticker: str, content: str) -> list[str]:
    route, route_note = _ai_route_thesis_source(content)
    arg_map = {
        "Revenue Growth": "Stability / Demand durability",
        "Gross Margin": "Moat / Pricing power",
        "ROIC": "Moat / Capital efficiency",
        "Debt/EBITDA": "Balance-sheet discipline",
        "Free Cash Flow": "Cash generation quality",
        "PE Ratio": "Valuation expectations",
    }
    if route == "SENTIMENT":
        news_line = _news_check(ticker, content)
        note = f" ({route_note})" if route_note else ""
        return [
            f"🤖 AI ROUTER: Classified thesis as SENTIMENT{note}.",
            f"📰 NEWS CHECK: {news_line}",
        ]
    if route == "INSIDER":
        insider_line = _insider_check_short(ticker)
        note = f" ({route_note})" if route_note else ""
        return [
            f"🤖 AI ROUTER: Classified thesis as INSIDER{note}.",
            f"👥 INSIDER CHECK: {insider_line}",
        ]

    metric, err = _ai_select_metric(content)
    if metric is None:
        return [
            "🤖 AI ROUTER: Classified thesis as FINANCIAL.",
            f"📊 DATA CHECK: N/A ({err or 'AI metric classifier did not return a supported metric.'})",
        ]
    value = _metric_value_check(ticker, metric)
    note = f" ({route_note})" if route_note else ""
    return [
        f"🤖 AI ROUTER: Classified thesis as FINANCIAL{note}.",
        f"📊 DATA CHECK: {metric} is {value}.",
        f"🤖 AI INTERPRETATION: You are arguing for {arg_map.get(metric, 'Business quality')} (metric: {metric}).",
    ]


def _heuristic_polish_text(raw: str) -> str:
    s = " ".join((raw or "").split())
    if not s:
        return ""
    if s and s[0].isalpha():
        s = s[0].upper() + s[1:]
    if s[-1] not in ".!?":
        s += "."
    return s


def _polish_thesis_text(ticker: str, sentiment: str, content: str) -> tuple[str, bool]:
    raw = (content or "").strip()
    if not raw:
        return "", False
    if _hybrid_ask_ai is None:
        return _heuristic_polish_text(raw), False
    ctx = (
        "You are a buy-side writing editor for institutional equity notes. "
        "Rewrite for grammar, clarity, and complete unfinished sentences. "
        "Do not add new facts, numbers, claims, or sources. Preserve original meaning and sentiment."
    )
    prompt = (
        f"Ticker: {ticker}\n"
        f"Sentiment: {sentiment}\n"
        f"Raw thesis text:\n{raw}\n\n"
        "Format in Wall Street note style using this exact structure and concise bullets:\n"
        "Thesis: <one-line core claim>\n\n"
        "Why It Can Work:\n"
        "- <bullet 1>\n"
        "- <bullet 2>\n\n"
        "Key Monitors:\n"
        "- <metric or trigger>\n"
        "- <metric or trigger>\n\n"
        "Risk / Disconfirming Signal:\n"
        "- <what would weaken the thesis>\n\n"
        "Rules:\n"
        "- Keep total length <= 140 words.\n"
        "- Use plain text only.\n"
        "- Keep only information inferable from user text.\n"
        "- Return only the final polished note."
    )
    try:
        out = (_hybrid_ask_ai(prompt, ctx) or "").strip()
        if not out:
            return _heuristic_polish_text(raw), False
        return out, True
    except Exception:
        return _heuristic_polish_text(raw), False


def save_stock_thesis(ticker: str, sentiment: str, content: str) -> int:
    ticker = ticker.upper().strip()
    base, _polished = _polish_thesis_text(ticker, sentiment, content)
    if not base:
        base = content.strip()
    fact_lines = _thesis_fact_check(ticker, base)
    full = base
    if fact_lines:
        full += "\n\n[Fact Check]\n" + "\n".join(f"- {ln}" for ln in fact_lines)
    conn = memory_db()
    cur = conn.execute(
        "INSERT INTO stock_thesis (ticker, sentiment, content, date) VALUES (?, ?, ?, ?)",
        (ticker, sentiment, full, dt.datetime.now().isoformat()),
    )
    conn.commit()
    tid = int(cur.lastrowid)
    conn.close()
    invalidate_l2_cache(start_async=True)
    return tid


def delete_stock_thesis(thesis_id: int, ticker: str = "") -> bool:
    if thesis_id <= 0:
        return False
    conn = memory_db()
    try:
        if ticker:
            cur = conn.execute(
                "DELETE FROM stock_thesis WHERE id = ? AND ticker = ?",
                (thesis_id, ticker.upper().strip()),
            )
        else:
            cur = conn.execute("DELETE FROM stock_thesis WHERE id = ?", (thesis_id,))
        conn.commit()
        ok = cur.rowcount > 0
    finally:
        conn.close()
    if ok:
        invalidate_l2_cache(start_async=True)
    return ok


def compare_latest_thesis(ticker: str) -> str:
    rows = list_stock_thesis(ticker, limit=2)
    if len(rows) < 2:
        return "First thesis saved."
    latest = str(rows[0]["content"] or "")
    prev = str(rows[1]["content"] or "")
    delta_len = len(latest) - len(prev)
    if abs(delta_len) <= 20:
        return "Minor edit vs previous."
    return f"Updated vs previous ({delta_len:+d} chars)."


def _theme_flags(text: str) -> set[str]:
    s = (text or "").lower()
    flags: set[str] = set()
    if any(k in s for k in ["margin", "pricing", "moat"]):
        flags.add("margin")
    if any(k in s for k in ["growth", "revenue", "demand", "retention"]):
        flags.add("growth")
    if any(k in s for k in ["debt", "leverage", "balance sheet", "liquidity"]):
        flags.add("debt")
    if any(k in s for k in ["insider", "ceo", "cfo", "management", "founder", "board"]):
        flags.add("management")
    if any(k in s for k in ["regulation", "legal", "antitrust", "compliance"]):
        flags.add("regulation")
    if any(k in s for k in ["earnings", "guide", "guidance", "quarter"]):
        flags.add("earnings")
    return flags


def _latest_thesis_map(tickers: list[str]) -> dict[str, dict[str, object]]:
    conn = memory_db()
    out: dict[str, dict[str, object]] = {}
    try:
        for t in tickers:
            row = conn.execute(
                "SELECT sentiment, content, date FROM stock_thesis WHERE ticker = ? ORDER BY date DESC LIMIT 1",
                (t,),
            ).fetchone()
            if not row:
                continue
            txt = str(row["content"] or "")
            out[t] = {
                "sentiment": str(row["sentiment"] or "").strip().lower(),
                "content": txt,
                "date": str(row["date"] or ""),
                "themes": sorted(_theme_flags(txt)),
            }
    finally:
        conn.close()
    return out


def _decision_log_map(limit: int = 300) -> dict[str, list[dict[str, str]]]:
    rows = list_decision_log(limit=limit)
    out: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        txt = str(r.get("text") or "")
        tags = re.findall(r"\$([A-Za-z]{1,6})\b", txt)
        for t in tags:
            tu = t.upper()
            out.setdefault(tu, []).append(
                {
                    "timestamp": str(r.get("timestamp") or ""),
                    "sentiment": str(r.get("sentiment") or ""),
                    "text": txt,
                }
            )
    return out


def _ticker_alert_hits_map(limit: int = 400) -> dict[str, list[str]]:
    red_flag = latest("reports/.terminal_inputs/red_flag_alert_*.txt")
    rows = run_cmd(["python3", "tools/red_flag_rank.py", "--file", red_flag, "--limit", str(limit)]) if red_flag else []
    out: dict[str, list[str]] = {}
    for ln in rows:
        t = extract_ticker_from_signal(ln)
        if not t:
            continue
        out.setdefault(t, []).append(ln)
    return out


def _l2_priority(total: int, urgency: int, conflict: int) -> str:
    if urgency >= 70 or total >= 75 or conflict >= 70:
        return "HIGH"
    if urgency >= 45 or total >= 50 or conflict >= 45:
        return "MEDIUM"
    return "LOW"


def _score_clip(v: int) -> int:
    return max(0, min(100, int(v)))


def _l2_build_ticker_insight(
    ticker: str,
    thesis: dict[str, object] | None,
    decision_rows: list[dict[str, str]],
    quotes: dict[str, dict[str, float | None]],
    intel: dict[str, dict[str, float | int | str | None]],
    weight_map: dict[str, float],
    alert_hits: list[str],
    macro: dict[str, object],
) -> dict[str, object]:
    t = ticker.upper()
    q = quotes.get(t, {})
    day = _to_num(q.get("day_pct"))
    wt = float(weight_map.get(t, 0.0))
    earn_days = intel.get(t, {}).get("earn_days")
    sec = _sec_snapshot(t)
    sec_dil = _to_num(sec.get("dilution_pct"))
    insider = _insider_skin_signal(t)
    insider_v = str(insider.get("verdict") or "").upper()

    themes = set(thesis.get("themes") or []) if thesis else set()
    thesis_sent = str(thesis.get("sentiment") or "").lower() if thesis else ""

    support = 0
    conflict = 0
    novelty = 0
    urgency = 0
    evidence: list[str] = []

    if thesis:
        support += 10
        evidence.append("Thesis present.")
    else:
        novelty += 15
        evidence.append("No current thesis found for this holding.")

    if decision_rows:
        support += 8
        evidence.append(f"Decision-log context present ({len(decision_rows)} entries).")
    else:
        novelty += 8
        evidence.append("No recent decision-log entry tagged to ticker.")

    if isinstance(sec_dil, float):
        if sec_dil > 0:
            conflict += min(35, int(sec_dil * 2))
            evidence.append(f"SEC dilution signal: +{sec_dil:.2f}% shares vs prior filing.")
            if "debt" in themes or thesis_sent == "bullish":
                conflict += 10
        else:
            support += 8
            evidence.append("SEC dilution signal: stable/non-increasing shares.")

    if insider_v == "BULLISH":
        support += 16
        evidence.append("Insider skin-in-game verdict is BULLISH.")
        if thesis_sent == "bullish":
            support += 6
    elif insider_v == "BEARISH":
        conflict += 16
        evidence.append("Insider skin-in-game verdict is BEARISH.")
        if thesis_sent == "bullish":
            conflict += 8

    if alert_hits:
        conflict += min(35, len(alert_hits) * 10)
        novelty += min(20, len(alert_hits) * 4)
        evidence.append(f"Red-flag alerts detected: {len(alert_hits)}.")

    if isinstance(earn_days, int):
        if earn_days <= 2:
            urgency += 50
            evidence.append(f"Earnings in {earn_days} day(s).")
        elif earn_days <= 7:
            urgency += 30
            evidence.append(f"Earnings in {earn_days} day(s).")
        elif earn_days <= 14:
            urgency += 15

    if isinstance(day, float):
        if abs(day) >= 5.0:
            urgency += 25
            novelty += 10
            evidence.append(f"Large daily move: {day:+.2f}%.")
        elif abs(day) >= 3.0:
            urgency += 12

    if wt >= 35.0:
        urgency += 30
        evidence.append(f"Concentration risk: {wt:.1f}% weight.")
    elif wt >= 20.0:
        urgency += 18
        evidence.append(f"High position weight: {wt:.1f}%.")

    # Light macro pressure cue from market pulse.
    rows = list(macro.get("rows") or [])
    vix_day = None
    for r in rows:
        if str((r or {}).get("name") or "") == "VIX":
            vix_day = _to_num((r or {}).get("day"))
            break
    if isinstance(vix_day, float) and vix_day > 3.0:
        urgency += 10
        evidence.append("Macro pressure: VIX rising >3% today.")

    support = _score_clip(support)
    conflict = _score_clip(conflict)
    novelty = _score_clip(novelty)
    urgency = _score_clip(urgency)
    total = _score_clip(int(0.35 * conflict + 0.30 * urgency + 0.20 * novelty + 0.15 * support))
    priority = _l2_priority(total, urgency, conflict)
    return {
        "ticker": t,
        "support": support,
        "conflict": conflict,
        "novelty": novelty,
        "urgency": urgency,
        "total": total,
        "priority": priority,
        "weight": wt,
        "earn_days": earn_days if isinstance(earn_days, int) else None,
        "thesis_sentiment": thesis_sent or "-",
        "evidence": evidence[:8],
    }


def _save_l2_run(rows: list[dict[str, object]], scope: str, summary: str) -> None:
    conn = memory_db()
    try:
        cur = conn.execute(
            "INSERT INTO l2_runs (run_ts, scope, summary) VALUES (?, ?, ?)",
            (dt.datetime.now().isoformat(), scope, summary),
        )
        run_id = int(cur.lastrowid)
        for r in rows:
            conn.execute(
                """INSERT INTO l2_insights (
                    run_id, ticker, support_score, conflict_score, novelty_score, urgency_score,
                    total_score, priority, evidence_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    str(r.get("ticker") or ""),
                    int(r.get("support") or 0),
                    int(r.get("conflict") or 0),
                    int(r.get("novelty") or 0),
                    int(r.get("urgency") or 0),
                    int(r.get("total") or 0),
                    str(r.get("priority") or "LOW"),
                    json.dumps({"evidence": r.get("evidence") or []}, ensure_ascii=True),
                    dt.datetime.now().isoformat(),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _previous_l2_scores() -> dict[str, dict[str, int]]:
    conn = memory_db()
    out: dict[str, dict[str, int]] = {}
    try:
        rid = conn.execute("SELECT id FROM l2_runs ORDER BY id DESC LIMIT 1 OFFSET 1").fetchone()
        if not rid:
            return {}
        rows = conn.execute(
            "SELECT ticker, total_score, conflict_score, urgency_score FROM l2_insights WHERE run_id = ?",
            (int(rid["id"]),),
        ).fetchall()
        for r in rows:
            t = str(r["ticker"] or "")
            out[t] = {
                "total": int(r["total_score"] or 0),
                "conflict": int(r["conflict_score"] or 0),
                "urgency": int(r["urgency_score"] or 0),
            }
    finally:
        conn.close()
    return out


def _compute_l2_snapshot(scope: str = "portfolio") -> dict[str, object]:
    port_rows = read_portfolio_rows(DATA / "portfolio.csv")
    tickers = [str(r[0] or "").upper().strip() for r in port_rows if r and str(r[0] or "").strip()]
    tickers = sorted(set(tickers))
    if not tickers:
        return {
            "rows": [],
            "contradictions": [],
            "queue": [],
            "drift": [],
            "asof": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "summary": "No portfolio holdings found for L2 synthesis.",
        }

    # Position weights
    quotes = get_live_quotes(tickers)
    pos_vals: dict[str, float] = {}
    for r in port_rows:
        t = str(r[0] or "").upper().strip()
        sh = to_float(str(r[1] or "")) or 0.0
        if not t or sh <= 0:
            continue
        nowp = _to_num((quotes.get(t, {}) or {}).get("price"))
        costp = to_float(str(r[2] or ""))
        px = float(nowp) if isinstance(nowp, float) else (float(costp) if costp not in (None, 0.0) else 0.0)
        if px > 0:
            pos_vals[t] = sh * px
    tot = sum(pos_vals.values())
    weight_map = {t: (v / tot * 100.0 if tot > 0 else 0.0) for t, v in pos_vals.items()}

    thesis_map = _latest_thesis_map(tickers)
    dlog_map = _decision_log_map(limit=500)
    alert_map = _ticker_alert_hits_map(limit=500)
    intel = get_portfolio_intel(tickers)
    macro = get_macro_market_snapshot(ttl_seconds=300)

    rows: list[dict[str, object]] = []
    for t in tickers:
        row = _l2_build_ticker_insight(
            ticker=t,
            thesis=thesis_map.get(t),
            decision_rows=dlog_map.get(t, []),
            quotes=quotes,
            intel=intel,
            weight_map=weight_map,
            alert_hits=alert_map.get(t, []),
            macro=macro,
        )
        rows.append(row)
    rows.sort(key=lambda r: (0 if str(r.get("priority")) == "HIGH" else 1 if str(r.get("priority")) == "MEDIUM" else 2, -int(r.get("total") or 0)))

    contradictions = [
        r for r in rows
        if str(r.get("thesis_sentiment") or "") == "bullish" and int(r.get("conflict") or 0) >= 45
    ][:8]

    queue = [
        r for r in rows
        if int(r.get("urgency") or 0) >= 45 or str(r.get("priority") or "") == "HIGH"
    ][:10]

    prev = _previous_l2_scores()
    drift: list[dict[str, object]] = []
    for r in rows:
        t = str(r.get("ticker") or "")
        p = prev.get(t)
        if not p:
            continue
        dtot = int(r.get("total") or 0) - int(p.get("total") or 0)
        dconf = int(r.get("conflict") or 0) - int(p.get("conflict") or 0)
        durg = int(r.get("urgency") or 0) - int(p.get("urgency") or 0)
        if abs(dtot) >= 8 or abs(dconf) >= 8 or abs(durg) >= 8:
            drift.append({"ticker": t, "delta_total": dtot, "delta_conflict": dconf, "delta_urgency": durg})
    drift.sort(key=lambda x: abs(int(x.get("delta_total") or 0)), reverse=True)

    high_n = sum(1 for r in rows if str(r.get("priority")) == "HIGH")
    med_n = sum(1 for r in rows if str(r.get("priority")) == "MEDIUM")
    summary = f"L2 run: {len(rows)} holdings analyzed | HIGH={high_n}, MEDIUM={med_n}."
    _save_l2_run(rows, scope=scope, summary=summary)
    return {
        "rows": rows,
        "contradictions": contradictions,
        "queue": queue,
        "drift": drift[:10],
        "asof": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "summary": summary,
    }


def _l2_refresh_worker(scope: str) -> None:
    try:
        snap = _compute_l2_snapshot(scope=scope)
        with LOCK:
            L2_CACHE[scope] = {"ts": time.time(), "data": snap}
    except Exception:
        pass
    finally:
        with LOCK:
            L2_REFRESH["running"] = False


def get_l2_snapshot(scope: str = "portfolio", ttl_seconds: int = 900) -> dict[str, object]:
    sc = (scope or "portfolio").strip().lower()
    now = time.time()
    stale: dict[str, object] | None = None
    start = False
    with LOCK:
        cell = L2_CACHE.get(sc)
        if cell and (now - float(cell.get("ts", 0.0))) <= ttl_seconds:
            data = cell.get("data")
            if isinstance(data, dict):
                return dict(data)
        if cell and isinstance(cell.get("data"), dict):
            stale = dict(cell.get("data") or {})
        if not bool(L2_REFRESH.get("running")):
            L2_REFRESH["running"] = True
            start = True
    if start:
        th = threading.Thread(target=_l2_refresh_worker, args=(sc,), daemon=True)
        th.start()
    if stale:
        return stale
    return _compute_l2_snapshot(scope=sc)


def invalidate_l2_cache(start_async: bool = True) -> None:
    with LOCK:
        L2_CACHE.clear()
        if not start_async or bool(L2_REFRESH.get("running")):
            return
        L2_REFRESH["running"] = True
    th = threading.Thread(target=_l2_refresh_worker, args=("portfolio",), daemon=True)
    th.start()


def export_memory_markdown() -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = REPORTS / f"memory_export_{stamp}.md"
    todos = list_todos(limit=500)
    scratch = get_scratchpad()
    pinned = get_scratchpad_pinned()
    conn = memory_db()
    thesis = conn.execute(
        "SELECT ticker, sentiment, content, date FROM stock_thesis ORDER BY date DESC LIMIT 500"
    ).fetchall()
    notes = conn.execute(
        "SELECT scope, ticker, sentiment, note, tags, created_at FROM investor_notes ORDER BY created_at DESC LIMIT 500"
    ).fetchall()
    conn.close()

    lines: list[str] = []
    lines.append(f"# Memory Export ({stamp})")
    lines.append("")
    lines.append("## To-Dos")
    if todos:
        for r in todos:
            lines.append(f"- [{ 'x' if str(r['status']) == 'done' else ' ' }] #{int(r['id'])} {str(r['task'])}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## Scratchpad")
    lines.append("### Pinned Notes")
    lines.append(pinned if pinned.strip() else "(empty)")
    lines.append("")
    lines.append("### Scratch")
    lines.append(scratch if scratch.strip() else "(empty)")
    lines.append("")
    lines.append("## Stock Thesis")
    if thesis:
        for r in thesis:
            lines.append(f"### {str(r['ticker'])} | {str(r['sentiment'])} | {str(r['date'])[:16].replace('T', ' ')}")
            lines.append(str(r["content"] or ""))
            lines.append("")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## Investor Notes")
    if notes:
        for r in notes:
            ts = str(r["created_at"] or "")[:16].replace("T", " ")
            lines.append(f"- {ts} | {str(r['scope'])} | {str(r['ticker'] or '-') } | {str(r['sentiment'])} | {str(r['note'])}")
    else:
        lines.append("- none")

    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(out)


def list_investor_notes(limit: int = 20, scope: str = "", ticker: str = "") -> list[sqlite3.Row]:
    conn = note_db()
    q = "SELECT id, scope, ticker, sentiment, note, tags, created_at FROM investor_notes WHERE 1=1"
    params: list[object] = []
    if scope:
        q += " AND scope = ?"
        params.append(scope)
    if ticker:
        q += " AND ticker = ?"
        params.append(ticker.upper())
    q += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return rows


def get_note(note_id: int) -> sqlite3.Row | None:
    conn = note_db()
    row = conn.execute(
        "SELECT id, scope, ticker, sentiment, note, tags, created_at FROM investor_notes WHERE id = ?",
        (note_id,),
    ).fetchone()
    conn.close()
    return row


def add_note_row(scope: str, ticker: str, sentiment: str, note: str, tags: str) -> int:
    conn = note_db()
    cur = conn.execute(
        "INSERT INTO investor_notes (scope, ticker, sentiment, note, tags, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            scope,
            (ticker.upper().strip() if ticker.strip() else None),
            sentiment,
            note,
            tags.strip(),
            dt.datetime.now().isoformat(),
        ),
    )
    conn.commit()
    new_id = int(cur.lastrowid)
    conn.close()
    return new_id


def update_note_row(note_id: int, scope: str, ticker: str, sentiment: str, note: str, tags: str) -> bool:
    conn = note_db()
    cur = conn.execute(
        "UPDATE investor_notes SET scope = ?, ticker = ?, sentiment = ?, note = ?, tags = ? WHERE id = ?",
        (
            scope,
            (ticker.upper().strip() if ticker.strip() else None),
            sentiment,
            note,
            tags.strip(),
            note_id,
        ),
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def research_db() -> sqlite3.Connection:
    _ensure_db_split_ready()
    db_path = FILINGS_DB_PATH if FILINGS_DB_PATH.exists() else LEGACY_DB_PATH
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def queue_company_sync(ticker: str) -> None:
    t = ticker.upper().strip()
    if not t:
        return
    if not _is_tracked_holding_ticker(t):
        with LOCK:
            COMPANY_SYNC_STATE[t] = {
                "running": False,
                "last": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "result": "skipped",
                "message": "Skipped: filing sync is only for portfolio/watchlist companies.",
            }
        return
    queue_insider_refresh(t, months=18)
    with LOCK:
        st = COMPANY_SYNC_STATE.get(t, {"running": False, "last": "never", "result": "-", "message": ""})
        if st.get("running"):
            return
        COMPANY_SYNC_STATE[t] = {
            "running": True,
            "last": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "result": "running",
            "message": "Sync started",
        }
    thread = threading.Thread(target=sync_company_data, args=(t,), daemon=True)
    thread.start()


def sync_company_data(ticker: str) -> None:
    log_path = LOGS / f"manual_company_sync_{ticker}.log"
    LOGS.mkdir(parents=True, exist_ok=True)
    steps = [
        ["python3", "research_agent.py", "init"],
        ["python3", "research_agent.py", "watch", ticker],
        ["python3", "research_agent.py", "update", "--ticker", ticker, "--days", "45"],
    ]
    ok = True
    msg = "Sync complete"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"\n=== {dt.datetime.now().isoformat()} sync {ticker} ===\n")
        for step in steps:
            fh.write(f"$ {' '.join(step)}\n")
            rc, out, err = run_cmd_raw(step)
            if out:
                fh.write(out + "\n")
            if err:
                fh.write(err + "\n")
            if rc != 0:
                ok = False
                msg = f"Step failed: {' '.join(step)}"
                break
    with LOCK:
        COMPANY_SYNC_STATE[ticker] = {
            "running": False,
            "last": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "result": "ok" if ok else "failed",
            "message": msg,
        }


def queue_insider_refresh(ticker: str, months: int = 18) -> None:
    t = ticker.upper().strip()
    if not t:
        return
    key = f"{t}:{months}"
    with LOCK:
        st = INSIDER_SYNC_STATE.get(key, {"running": False, "last": "never", "result": "-", "message": ""})
        if st.get("running"):
            return
        INSIDER_SYNC_STATE[key] = {
            "running": True,
            "last": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "result": "running",
            "message": "Insider refresh started",
        }
    th = threading.Thread(target=run_insider_refresh, args=(t, months, key), daemon=True)
    th.start()


def run_insider_refresh(ticker: str, months: int, key: str) -> None:
    log_path = LOGS / f"manual_insider_sync_{ticker}.log"
    LOGS.mkdir(parents=True, exist_ok=True)
    step = ["python3", "tools/insider_refresh.py", "--ticker", ticker, "--months", str(months)]
    ok = True
    msg = "Insider refresh complete"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"\n=== {dt.datetime.now().isoformat()} insider sync {ticker} ({months}m) ===\n")
        fh.write(f"$ {' '.join(step)}\n")
        rc, out, err = run_cmd_raw(step)
        if out:
            fh.write(out + "\n")
        if err:
            fh.write(err + "\n")
        if rc != 0:
            ok = False
            msg = "Insider refresh failed"
    with LOCK:
        INSIDER_SYNC_STATE[key] = {
            "running": False,
            "last": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "result": "ok" if ok else "failed",
            "message": msg,
        }


def get_company_snapshot(ticker: str) -> dict[str, object]:
    t = ticker.upper().strip()
    conn = research_db()
    data: dict[str, object] = {"ticker": t}
    try:
        c = conn.execute("SELECT ticker, name, cik FROM companies WHERE ticker = ?", (t,)).fetchone()
        data["company"] = c
        filings = conn.execute(
            "SELECT form, date, accession, path, doc_url FROM filings WHERE ticker = ? ORDER BY date DESC",
            (t,),
        ).fetchall()
        data["filings"] = filings
        changes = conn.execute(
            """SELECT c.section_name, c.change_type, c.summary, c.detected_at, f.form, f.date
               FROM changes c JOIN filings f ON c.filing_id = f.id
               WHERE c.ticker = ?
               ORDER BY c.detected_at DESC""",
            (t,),
        ).fetchall()
        data["changes"] = changes
        transcript_hits = conn.execute(
            """SELECT f.form, f.date, substr(s.content, 1, 280) AS excerpt
               FROM sections s
               JOIN filings f ON s.filing_id = f.id
               WHERE f.ticker = ?
                 AND lower(s.content) LIKE '%transcript%'
               ORDER BY f.date DESC LIMIT 8""",
            (t,),
        ).fetchall()
        data["transcripts"] = transcript_hits
    finally:
        conn.close()

    red_flag = latest("reports/.terminal_inputs/red_flag_alert_*.txt")
    earnings = _latest_earnings_file()
    alerts = run_cmd(["python3", "tools/red_flag_rank.py", "--file", red_flag, "--limit", "300"]) if red_flag else []
    data["alert_hits"] = [a for a in alerts if f"| {t} |" in a][:15]
    upcoming = run_cmd(["python3", "tools/earnings_watch_rank.py", "--file", earnings, "--limit", "300", "--scope", "upcoming"]) if earnings else []
    week_events = run_cmd(["python3", "tools/earnings_watch_rank.py", "--file", earnings, "--limit", "300", "--scope", "week"]) if earnings else []
    data["earn_hits"] = [e for e in (upcoming + week_events) if f"| {t} |" in e][:15]
    data["notes"] = list_investor_notes(limit=20, ticker=t)
    with LOCK:
        data["sync"] = COMPANY_SYNC_STATE.get(t, {"running": False, "last": "never", "result": "-", "message": ""})
    return data


def reported_earnings_lines(limit: int = 60) -> list[str]:
    earnings = _latest_earnings_file()
    if not earnings:
        return []
    lim = max(1, int(limit))
    out: list[str] = []

    # Source of truth: earnings ranking script, guarded by timeout so homepage stays responsive.
    try:
        p = subprocess.run(
            ["python3", "tools/earnings_watch_rank.py", "--file", earnings, "--limit", str(lim), "--scope", "week"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=6,
        )
        if p.returncode == 0:
            for ln in (p.stdout or "").splitlines():
                s = ln.strip().lstrip("-").strip()
                if "REPORTED |" in s:
                    out.append(s)
                    if len(out) >= lim:
                        break
            if out:
                return out
    except Exception:
        pass

    # Fallback: direct file parse.
    try:
        txt = Path(earnings).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return out
    for ln in txt.splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.startswith("-"):
            s = s.lstrip("-").strip()
        if "REPORTED |" in s:
            out.append(s)
            if len(out) >= lim:
                break
    return out


def _eps_est_map_from_earnings(path: str) -> dict[str, float]:
    out: dict[str, float] = {}
    if not path:
        return out
    try:
        txt = Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return out
    for ln in txt.splitlines():
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 5:
            continue
        sym = re.sub(r"[^A-Za-z0-9.\-]", "", cells[1]).upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", sym or ""):
            continue
        est = _parse_eps_num(cells[4])
        if isinstance(est, float):
            out[sym] = est
    return out


def _parse_date_in_text(s: str) -> dt.date | None:
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", str(s or ""))
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except Exception:
        return None


def _parse_reported_row_line(
    ln: str,
    est_map: dict[str, float],
    portfolio: set[str],
    watchlist: set[str],
) -> dict[str, object] | None:
    s = (ln or "").strip()
    if s.startswith("-"):
        s = s.lstrip("-").strip()
    parts = [p.strip() for p in s.split("|")]
    if len(parts) < 5:
        return None
    status = parts[0].upper().strip()
    if status != "REPORTED":
        return None
    t = parts[1].upper().strip()
    date_txt = parts[2]
    mcap_txt = parts[3]
    eps_txt = parts[4].replace("EPS", "").strip()
    left = right = ""
    if "vs" in eps_txt:
        left, right = [x.strip() for x in eps_txt.split("vs", 1)]
    actual = _parse_eps_num(left)
    est = _parse_eps_num(right)
    if est is None:
        est = est_map.get(t)
    surprise = None
    verdict = "REPORTED"
    if actual is not None and est not in (None, 0.0):
        surprise = (actual - est) / abs(est) * 100.0
        verdict = "BEAT" if actual >= est else "MISS"
    mcap_num = _to_num(re.sub(r"[^0-9.\-]", "", mcap_txt))
    in_scope = "portfolio" if t in portfolio else ("watchlist" if t in watchlist else "-")
    score = 0
    if in_scope == "portfolio":
        score += 100
    elif in_scope == "watchlist":
        score += 60
    if isinstance(mcap_num, float):
        if mcap_num >= 300_000_000_000:
            score += 35
        elif mcap_num >= 100_000_000_000:
            score += 25
        elif mcap_num >= 30_000_000_000:
            score += 15
        else:
            score += 8
    if isinstance(surprise, float):
        a = abs(surprise)
        if a >= 30:
            score += 35
        elif a >= 15:
            score += 25
        elif a >= 8:
            score += 15
        elif a >= 3:
            score += 8
        if surprise < 0 and a >= 8:
            score += 10
    elif isinstance(mcap_num, float) and mcap_num >= 200_000_000_000:
        score += 10
    if score >= 95:
        imp = "CRITICAL"
    elif score >= 65:
        imp = "HIGH"
    elif score >= 40:
        imp = "MEDIUM"
    else:
        imp = "LOW"
    d = _parse_date_in_text(date_txt)
    return {
        "ticker": t,
        "in_scope": in_scope,
        "date_txt": date_txt,
        "date_obj": d,
        "mcap_txt": mcap_txt.replace("mcap", "").strip(),
        "mcap_num": mcap_num,
        "actual": actual,
        "est": est,
        "surprise": surprise,
        "verdict": verdict,
        "importance": imp,
        "score": score,
        "raw": ln,
    }


def get_reported_earnings_snapshot(limit: int = 140) -> dict[str, object]:
    earnings_path = _latest_earnings_file()
    portfolio = set(read_portfolio_tickers())
    watchlist = set(read_watchlist_tickers())
    sig_src = "-"
    if earnings_path:
        try:
            st = os.stat(earnings_path)
            sig_src = f"{earnings_path}:{int(st.st_mtime)}:{int(st.st_size)}"
        except Exception:
            sig_src = str(earnings_path)
    p_sig = ",".join(sorted(portfolio))
    w_sig = ",".join(sorted(watchlist))
    ck = f"earnings_snapshot:v2:{limit}:{sig_src}:{p_sig}:{w_sig}"
    cached = _cache_get(ck, ttl_seconds=600)
    if isinstance(cached, dict):
        try:
            c = dict(cached)
            # Restore date objects used by sorting/filter paths.
            for k in ("parsed", "holdings_rows", "this_week_rows"):
                rows = c.get(k)
                if isinstance(rows, list):
                    for r in rows:
                        if isinstance(r, dict):
                            raw_d = r.get("date_obj")
                            if isinstance(raw_d, str):
                                try:
                                    r["date_obj"] = dt.datetime.strptime(raw_d[:10], "%Y-%m-%d").date()
                                except Exception:
                                    r["date_obj"] = None
            c["portfolio"] = set(c.get("portfolio") or [])
            c["watchlist"] = set(c.get("watchlist") or [])
            return c
        except Exception:
            pass

    lines = reported_earnings_lines(limit=limit)
    est_map = _eps_est_map_from_earnings(earnings_path)

    parsed: list[dict[str, object]] = []
    for ln in lines:
        row = _parse_reported_row_line(ln, est_map, portfolio, watchlist)
        if row:
            parsed.append(row)
    parsed.sort(key=lambda r: (int(r.get("score") or 0), float(r.get("mcap_num") or 0.0)), reverse=True)

    holdings_rows = [r for r in parsed if str(r.get("in_scope")) in {"portfolio", "watchlist"}]
    holdings_rows.sort(key=lambda r: (0 if str(r.get("in_scope")) == "portfolio" else 1, -int(r.get("score") or 0)))

    today = dt.date.today()
    week_start = today - dt.timedelta(days=today.weekday())
    week_end = week_start + dt.timedelta(days=6)
    this_week_rows = [
        r
        for r in parsed
        if isinstance(r.get("date_obj"), dt.date) and week_start <= r["date_obj"] <= week_end
    ]
    this_week_rows.sort(key=lambda r: (r["date_obj"], int(r.get("score") or 0)), reverse=True)

    freshness = {"label": "-", "stamp": "-", "age_hours": None, "note": "Data freshness unknown."}
    age_hours: float | None = None
    if earnings_path:
        try:
            ts = dt.datetime.fromtimestamp(os.path.getmtime(earnings_path))
            age_hours = (dt.datetime.now() - ts).total_seconds() / 3600.0
            stamp = ts.strftime("%Y-%m-%d %H:%M")
            if age_hours >= 12:
                label, note = "STALE", f"Data is {age_hours:.1f}h old. Run daily refresh."
            elif age_hours >= 6:
                label, note = "AGING", f"Data is {age_hours:.1f}h old. Consider refresh."
            else:
                label, note = "FRESH", f"Data is {age_hours:.1f}h old."
            freshness = {"label": label, "stamp": stamp, "age_hours": age_hours, "note": note}
        except Exception:
            pass

    # Keep reported earnings intelligence up to date daily by auto-running the daily refresh job
    # once per date when the earnings feed is stale or missing.
    today_key = dt.date.today().isoformat()
    stale_or_missing = (not earnings_path) or (isinstance(age_hours, float) and age_hours >= 20.0)
    if stale_or_missing:
        with LOCK:
            already_today = str(EARNINGS_AUTO_REFRESH_STATE.get("last_date") or "") == today_key
            busy = bool(EARNINGS_AUTO_REFRESH_STATE.get("running"))
            if (not already_today) and (not busy):
                EARNINGS_AUTO_REFRESH_STATE["running"] = True
                EARNINGS_AUTO_REFRESH_STATE["last_date"] = today_key

                def _refresh_daily_job() -> None:
                    try:
                        run_job("daily")
                    finally:
                        with LOCK:
                            EARNINGS_AUTO_REFRESH_STATE["running"] = False

                threading.Thread(target=_refresh_daily_job, daemon=True).start()

    out = {
        "lines": lines,
        "parsed": parsed,
        "holdings_rows": holdings_rows,
        "this_week_rows": this_week_rows,
        "portfolio": portfolio,
        "watchlist": watchlist,
        "freshness": freshness,
        "earnings_path": earnings_path,
    }
    try:
        to_cache = {
            "lines": lines,
            "parsed": [{**r, "date_obj": (r.get("date_obj").isoformat() if isinstance(r.get("date_obj"), dt.date) else None)} for r in parsed],
            "holdings_rows": [{**r, "date_obj": (r.get("date_obj").isoformat() if isinstance(r.get("date_obj"), dt.date) else None)} for r in holdings_rows],
            "this_week_rows": [{**r, "date_obj": (r.get("date_obj").isoformat() if isinstance(r.get("date_obj"), dt.date) else None)} for r in this_week_rows],
            "portfolio": sorted(portfolio),
            "watchlist": sorted(watchlist),
            "freshness": freshness,
            "earnings_path": earnings_path,
        }
        _cache_put(ck, to_cache)
    except Exception:
        pass
    return out


def earnings_industry_html(sector: str = "") -> str:
    sec_target = (sector or "").strip()
    if not sec_target:
        return dashboard_html("Sector is required.")
    snap = get_reported_earnings_snapshot(limit=220)
    rows = list(snap.get("this_week_rows") or [])
    if not rows:
        rows = list(snap.get("parsed") or [])
    tickers = [str(r.get("ticker") or "").upper().strip() for r in rows if str(r.get("ticker") or "").strip()]
    profiles = get_portfolio_profiles(tickers) if tickers else {}

    def _sec_of(t: str) -> str:
        return str((profiles.get(t, {}) or {}).get("sector") or "Unknown").strip() or "Unknown"

    def _ind_of(t: str) -> str:
        return str((profiles.get(t, {}) or {}).get("industry") or "Unknown").strip() or "Unknown"

    def _name_of(t: str) -> str:
        return str((profiles.get(t, {}) or {}).get("name") or t).strip() or t

    frows = [r for r in rows if _sec_of(str(r.get("ticker") or "").upper().strip()).lower() == sec_target.lower()]
    frows.sort(key=lambda r: (str(r.get("date_txt") or ""), float(r.get("mcap_num") or 0.0)), reverse=True)

    def _d(r: dict[str, object]) -> str:
        dtxt = str(r.get("date_txt") or "").strip()
        m = re.search(r"\b\d{4}-\d{2}-\d{2}\b", dtxt)
        return m.group(0) if m else (dtxt[:10] if len(dtxt) >= 10 else (dtxt or "-"))

    def _eps(r: dict[str, object]) -> str:
        a = r.get("actual")
        e = r.get("est")
        if isinstance(a, float) and isinstance(e, float):
            return f"{a:.2f} vs {e:.2f}"
        return "-"

    def _verdict(r: dict[str, object]) -> str:
        v = str(r.get("verdict") or "").upper().strip()
        return v if v in {"BEAT", "MISS"} else "REPORTED"

    default_list = f"{sec_target} List"
    cards = "".join(
        (
            "<article class='row'>"
            f"<div class='t'><a href='/company?t={html.escape(str(r.get('ticker') or '-'))}'>{html.escape(str(r.get('ticker') or '-'))}</a>"
            f"<span class='name'>{html.escape(_name_of(str(r.get('ticker') or '').upper().strip()))}</span></div>"
            f"<div class='m'>Date: {html.escape(_d(r))}</div>"
            f"<div class='m'>EPS: {html.escape(_eps(r))}</div>"
            f"<div class='m'>Verdict: <span class='v {'beat' if _verdict(r) == 'BEAT' else ('miss' if _verdict(r) == 'MISS' else '')}'>{html.escape(_verdict(r))}</span></div>"
            f"<div class='m'>Surprise: {html.escape(fmt_pct(float(r.get('surprise'))) if isinstance(r.get('surprise'), float) else '-')}</div>"
            f"<div class='m'>MCap: {html.escape(str(r.get('mcap_txt') or '-'))}</div>"
            f"<div class='m'>Industry: {html.escape(_ind_of(str(r.get('ticker') or '').upper().strip()))}</div>"
            f"<div><a class='btn' href='/company?t={html.escape(str(r.get('ticker') or '-'))}&tab=overview'>Open</a>"
            f"<form method='post' action='/company_list/add' style='display:inline;margin-left:6px;'><input type='hidden' name='name' value='{html.escape(default_list)}'><input type='hidden' name='ticker' value='{html.escape(str(r.get('ticker') or '-'))}'><input type='hidden' name='source' value='earnings_sector'><button type='submit' class='btn'>Add</button></form></div>"
            "</article>"
        )
        for r in frows
    ) or "<div class='muted'>No reported companies in this sector for current snapshot.</div>"

    return (
        "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Earnings Sector List</title>"
        "<style>"
        "body{margin:0;background:#eaf0f4;color:#2f4358;font-family:'Avenir Next','Helvetica Neue',sans-serif;}"
        ".wrap{max-width:1100px;margin:0 auto;padding:14px;}"
        ".card{background:#f1f5f8;border:1px solid #d6dee6;border-radius:10px;padding:12px;}"
        ".bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;}"
        ".btn{border:1px solid #c8d3dd;background:#edf2f6;color:#2f4358;border-radius:8px;padding:6px 10px;text-decoration:none;font-size:12px;font-weight:700;}"
        ".grid{display:grid;gap:8px;margin-top:8px;}"
        ".row{display:grid;grid-template-columns:1.4fr 1fr 1fr 1fr 1fr 1fr 1.2fr auto;gap:8px;align-items:center;border:1px solid #cfd8e1;background:#eaf1f6;border-radius:10px;padding:9px;}"
        ".t a{font-weight:800;color:#1f3a52;text-decoration:none;} .name{margin-left:8px;color:#4f6780;font-size:12px;}"
        ".m{font-size:12px;color:#355066;} .v{font-weight:800;} .v.beat{color:#008f7e;} .v.miss{color:#b62e4a;}"
        ".muted{color:#4f6780;} @media (max-width:980px){.row{grid-template-columns:1fr;}}"
        "</style></head><body><div class='wrap'>"
        "<div class='bar'>"
        "<a class='btn' href='/'>Home</a>"
        "<a class='btn' href='javascript:history.back()'>Back</a>"
        "</div>"
        f"<div class='card'><h2 style='margin:0 0 6px 0;'>Reported Companies in {html.escape(sec_target)}</h2>"
        f"<form method='post' action='/company_list/import_sector' style='display:grid;grid-template-columns:1fr auto auto;gap:8px;margin:8px 0;'>"
        f"<input type='hidden' name='sector' value='{html.escape(sec_target)}'>"
        f"<input name='name' value='{html.escape(default_list)}' placeholder='List name'>"
        "<button type='submit'>Add All To List</button>"
        "<a class='btn' href='/company_lists'>Open Lists</a>"
        "</form>"
        "<div class='muted'>Current earnings snapshot (this week, fallback to latest parsed).</div>"
        f"<div class='grid'>{cards}</div></div>"
        "</div></body></html>"
    )


def extract_ticker_from_signal(line: str) -> str:
    s = line.strip().lstrip("-").strip()
    m_earn = re.search(r"^(?:UPCOMING|REPORTED)\s*\|\s*([A-Z][A-Z0-9.\-]+)\s*\|", s)
    if m_earn:
        return m_earn.group(1)
    m_alert = re.search(r"^[A-Z]+\(\d+\)\s+([A-Z][A-Z0-9.\-]+)\s*\|", s)
    if m_alert:
        return m_alert.group(1)
    return ""


def render_signal_list(items: list[str], empty: str, link_mode: str = "memo") -> str:
    if not items:
        return f"<li class='muted'>{html.escape(empty)}</li>"
    out: list[str] = []
    for row in items:
        row = re.sub(r"\s*\[source:[^\]]+\]\s*", " ", str(row), flags=re.IGNORECASE).strip()
        row = re.sub(r"\s+", " ", row)
        t = extract_ticker_from_signal(row)
        safe = html.escape(row)
        if t:
            route = f"/company?t={html.escape(t)}&tab=overview" if link_mode in {"deepdive", "memo"} else f"/ticker?t={html.escape(t)}"
            safe_t = html.escape(t)
            safe = safe.replace(safe_t, f"<a href='{route}'>{safe_t}</a>", 1)
        out.append(f"<li>{safe}</li>")
    return "".join(out)


def _parse_eps_num(s: str) -> float | None:
    t = (s or "").strip()
    if not t:
        return None
    neg = False
    if t.startswith("(") and t.endswith(")"):
        neg = True
        t = t[1:-1]
    t = t.replace("$", "").replace(",", "").strip()
    try:
        v = float(t)
        return -v if neg else v
    except Exception:
        return None


def _extract_eps_pair_from_text(text: str) -> tuple[float | None, float | None]:
    s = str(text or "")
    # Common forms:
    # "EPS $1.23 vs $1.10", "EPS: 1.23 vs 1.10", "reported EPS of $1.23 ... estimate $1.10"
    pats = [
        r"eps[^0-9\-()]*([\-]?\(?\$?\d+(?:\.\d+)?\)?)\s*(?:vs|versus)\s*([\-]?\(?\$?\d+(?:\.\d+)?\)?)",
        r"reported eps[^0-9\-()]*([\-]?\(?\$?\d+(?:\.\d+)?\)?)\D+estimate[^0-9\-()]*([\-]?\(?\$?\d+(?:\.\d+)?\)?)",
        r"actual[^0-9\-()]*([\-]?\(?\$?\d+(?:\.\d+)?\)?)\D+estimate[^0-9\-()]*([\-]?\(?\$?\d+(?:\.\d+)?\)?)",
    ]
    for pat in pats:
        m = re.search(pat, s, flags=re.IGNORECASE)
        if not m:
            continue
        a = _parse_eps_num(m.group(1))
        e = _parse_eps_num(m.group(2))
        if a is not None and e is not None:
            return a, e
    return None, None


def _llm_extract_eps_pair(text: str) -> tuple[float | None, float | None]:
    if _hybrid_ask_ai is None:
        return None, None
    src = str(text or "").strip()
    if not src:
        return None, None
    prompt = (
        "Extract EPS actual and EPS estimate from this earnings text. "
        "Return ONLY compact JSON like {\"actual\":1.23,\"estimate\":1.10}. "
        f"Text: {src[:1200]}"
    )
    ctx = "You are a strict parser. No explanation. JSON only."
    try:
        raw = (_hybrid_ask_ai(prompt, ctx) or "").strip()
        if not raw:
            return None, None
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        blob = m.group(0) if m else raw
        obj = json.loads(blob)
        return _to_num(obj.get("actual")), _to_num(obj.get("estimate"))
    except Exception:
        return None, None


def _guidance_tag(text: str) -> str:
    s = str(text or "").lower()
    if any(k in s for k in ["raised outlook", "raise guidance", "guidance raised", "increased guidance", "raised forecast"]):
        return "RAISED"
    if any(k in s for k in ["cut guidance", "guidance cut", "lowered outlook", "reduced guidance", "trimmed outlook"]):
        return "CUT"
    if any(k in s for k in ["reiterated guidance", "maintained guidance", "in-line guidance", "guidance unchanged"]):
        return "MAINTAINED"
    return "N/A"


def _earnings_move_why(ticker: str, row_text: str, surprise: float | None, guidance: str) -> str:
    base = []
    if surprise is not None:
        base.append(f"EPS surprise {surprise:+.1f}%")
    if guidance != "N/A":
        base.append(f"guidance {guidance.lower()}")
    default = (" / ".join(base) + ".") if base else "No clear move driver in parsed row."
    if _hybrid_ask_ai is None:
        return default
    key = f"earn_why:{ticker}:{hashlib.sha256((row_text + str(surprise) + guidance).encode('utf-8', errors='ignore')).hexdigest()[:24]}"
    cached = _cache_get(key, ttl_seconds=1800)
    if isinstance(cached, str) and cached.strip():
        return cached.strip()
    prompt = (
        f"Ticker: {ticker}\n"
        f"Earnings row: {row_text[:1200]}\n"
        f"Parsed surprise: {('n/a' if surprise is None else f'{surprise:+.1f}%')}\n"
        f"Guidance tag: {guidance}\n\n"
        "Write exactly one sentence explaining likely move driver using only this evidence."
    )
    ctx = "You are an earnings analyst. One sentence only. No external facts."
    try:
        out = (_hybrid_ask_ai(prompt, ctx) or "").strip()
        if out:
            _cache_put(key, out)
            return out
    except Exception:
        pass
    return default


def _safe_pct(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:+.1f}%"


def _price_trend_stats(ticker: str) -> dict[str, float | None]:
    t = (ticker or "").strip().upper()
    if not t or yf is None:
        return {"ret5": None, "ret21": None, "rsi14": None, "vol20": None}
    ck = f"trend:{t}"
    cached = _cache_get(ck, ttl_seconds=1800)
    if isinstance(cached, dict):
        return {
            "ret5": _to_num(cached.get("ret5")),
            "ret21": _to_num(cached.get("ret21")),
            "rsi14": _to_num(cached.get("rsi14")),
            "vol20": _to_num(cached.get("vol20")),
        }
    ret5 = ret21 = rsi14 = vol20 = None
    try:
        hist = yf.Ticker(t).history(period="3mo", interval="1d")
        close = []
        if hist is not None and hasattr(hist, "columns") and "Close" in list(hist.columns):
            close = [float(x) for x in list(hist["Close"]) if _to_num(x) is not None]
        if len(close) >= 22:
            c0 = close[-1]
            c5 = close[-6]
            c21 = close[-22]
            if c5 not in (0.0, None):
                ret5 = (c0 / c5 - 1.0) * 100.0
            if c21 not in (0.0, None):
                ret21 = (c0 / c21 - 1.0) * 100.0
            rets = []
            for i in range(1, len(close)):
                p0 = close[i - 1]
                p1 = close[i]
                if p0:
                    rets.append((p1 / p0) - 1.0)
            if len(rets) >= 20:
                tail = rets[-20:]
                mu = sum(tail) / len(tail)
                var = sum((x - mu) ** 2 for x in tail) / max(1, len(tail) - 1)
                vol20 = (var ** 0.5) * 100.0
            if len(rets) >= 14:
                tail = rets[-14:]
                gains = [x for x in tail if x > 0]
                losses = [-x for x in tail if x < 0]
                avg_g = (sum(gains) / 14.0) if gains else 0.0
                avg_l = (sum(losses) / 14.0) if losses else 0.0
                if avg_l == 0:
                    rsi14 = 100.0
                else:
                    rs = avg_g / avg_l
                    rsi14 = 100.0 - (100.0 / (1.0 + rs))
    except Exception:
        pass
    out = {"ret5": ret5, "ret21": ret21, "rsi14": rsi14, "vol20": vol20}
    _cache_put(ck, out)
    return out


def _pair_corr(t1: str, t2: str) -> float | None:
    if yf is None:
        return None
    a = _price_trend_series(t1)
    b = _price_trend_series(t2)
    if not a or not b:
        return None
    common = sorted(set(a.keys()) & set(b.keys()))
    if len(common) < 15:
        return None
    x = [a[d] for d in common]
    y = [b[d] for d in common]
    mx = sum(x) / len(x)
    my = sum(y) / len(y)
    vx = sum((v - mx) ** 2 for v in x)
    vy = sum((v - my) ** 2 for v in y)
    if vx <= 0 or vy <= 0:
        return None
    cov = sum((x[i] - mx) * (y[i] - my) for i in range(len(x)))
    return cov / (vx ** 0.5 * vy ** 0.5)


def _price_trend_series(ticker: str) -> dict[str, float]:
    t = (ticker or "").strip().upper()
    if not t or yf is None:
        return {}
    ck = f"ret_series:{t}"
    cached = _cache_get(ck, ttl_seconds=3600)
    if isinstance(cached, dict):
        out = {str(k): _to_num(v) for k, v in cached.items()}
        return {k: float(v) for k, v in out.items() if v is not None}
    out: dict[str, float] = {}
    try:
        hist = yf.Ticker(t).history(period="3mo", interval="1d")
        if hist is not None and hasattr(hist, "index") and "Close" in list(hist.columns):
            idx = list(hist.index)
            close = [float(x) for x in list(hist["Close"]) if _to_num(x) is not None]
            if len(idx) == len(close) and len(close) >= 2:
                for i in range(1, len(close)):
                    p0 = close[i - 1]
                    p1 = close[i]
                    if p0:
                        d = str(getattr(idx[i], "date", lambda: idx[i])())
                        out[d] = (p1 / p0) - 1.0
    except Exception:
        out = {}
    _cache_put(ck, out)
    return out


def generate_risk_cards(
    portfolio: list[str],
    portfolio_rows: list[tuple[str, str, str, str]],
    quotes: dict[str, dict[str, float | None]],
    intel: dict[str, dict[str, float | int | str | None]],
) -> list[str]:
    tks = [t.upper().strip() for t in portfolio if t.strip()]
    if not tks:
        return []
    pmap = {(r[0] or "").upper().strip(): r for r in portfolio_rows if r and (r[0] or "").strip()}
    vals: dict[str, float] = {}
    for t in tks:
        row = pmap.get(t)
        if not row:
            continue
        sh = to_float(row[1]) or 0.0
        if sh <= 0:
            continue
        nowp = quotes.get(t, {}).get("price")
        cb = to_float(row[2])
        px = float(nowp) if isinstance(nowp, float) else (float(cb) if cb not in (None, 0.0) else None)
        if px is None:
            continue
        vals[t] = sh * px
    tot = sum(vals.values())
    out: list[str] = []
    profiles = get_portfolio_profiles(tks)
    # 1) Sector exposure risk
    sector_val: dict[str, float] = {}
    sector_names: dict[str, list[str]] = {}
    for t, v in vals.items():
        sec = str((profiles.get(t, {}) or {}).get("sector") or "Unknown").strip() or "Unknown"
        sector_val[sec] = sector_val.get(sec, 0.0) + v
        sector_names.setdefault(sec, []).append(t)
    for sec, v in sorted(sector_val.items(), key=lambda kv: kv[1], reverse=True)[:2]:
        if tot > 0:
            wt = v / tot * 100.0
            names = ", ".join(sorted(sector_names.get(sec, []))[:4])
            if wt >= 45.0:
                out.append(
                    f"RISK(92) {sorted(sector_names.get(sec, ['-']))[0]} | Sector Risk: {wt:.1f}% of portfolio in {sec} ({names}). A sector rotation can hit all legs together."
                )
    # 1b) Correlation cluster risk
    if len(tks) >= 3:
        high_pairs = 0
        pair_samples: list[str] = []
        for i in range(len(tks)):
            for j in range(i + 1, len(tks)):
                c = _pair_corr(tks[i], tks[j])
                if c is not None and c >= 0.70:
                    high_pairs += 1
                    if len(pair_samples) < 3:
                        pair_samples.append(f"{tks[i]}/{tks[j]} {c:.2f}")
        if high_pairs >= 2:
            seed = tks[0]
            out.append(
                f"RISK(86) {seed} | Correlation Cluster: {high_pairs} high-correlation pairs in holdings ({'; '.join(pair_samples)}). Diversification is weaker than it looks."
            )
    # 2) Falling knife alerts
    for t in tks:
        day = quotes.get(t, {}).get("day_pct")
        st = _price_trend_stats(t)
        ret5 = _to_num(st.get("ret5"))
        ret21 = _to_num(st.get("ret21"))
        rsi = _to_num(st.get("rsi14"))
        if isinstance(day, float) and day <= -3.0 and isinstance(ret5, float) and ret5 < 0:
            month = _safe_pct(ret21)
            rsi_txt = f"{rsi:.0f}" if isinstance(rsi, float) else "n/a"
            out.append(
                f"RISK(84) {t} | Falling Knife: {t} is {day:+.2f}% today, 5D {ret5:+.1f}%, 1M {month}. RSI {rsi_txt}. Watch for bounce vs stop discipline."
            )
    # 3) Binary earnings event risk (<5d)
    for t in tks:
        d = intel.get(t, {}).get("earn_days")
        if isinstance(d, int) and d < 5:
            st = _price_trend_stats(t)
            vol20 = _to_num(st.get("vol20"))
            imp = None
            if isinstance(vol20, float):
                imp = max(3.0, min(15.0, 1.6 * vol20))
            imp_txt = f"+/- {imp:.1f}% (proxy)" if isinstance(imp, float) else "move proxy unavailable"
            out.append(
                f"RISK(88) {t} | Binary Event Risk: earnings in {d} day(s). Expected move {imp_txt}. Decide hedge/size before print."
            )
    # 4) Onyx thesis contradiction overlay
    if _onyx_get_signals_for_tickers is not None:
        try:
            sigs = _onyx_get_signals_for_tickers(tks)
            for t in tks:
                s = str((sigs.get(t, {}) or {}).get("status") or "").lower()
                if s == "red":
                    out.append(f"RISK(82) {t} | Thesis Check is RED. Re-underwrite assumptions before adding risk.")
        except Exception:
            pass
    return out[:10]


def _fmt_cap_short(v: object) -> str:
    n = _to_num(v)
    if n is None:
        return "-"
    a = abs(n)
    if a >= 1_000_000_000_000:
        return f"${n / 1_000_000_000_000:.0f}T"
    if a >= 1_000_000_000:
        return f"${n / 1_000_000_000:.0f}B"
    if a >= 1_000_000:
        return f"${n / 1_000_000:.0f}M"
    return f"${n:,.0f}"


def _earnings_time_label(date_s: str, tm: str) -> str:
    d = (date_s or "").strip()
    today = dt.date.today().isoformat()
    day_txt = "Today" if d == today else d
    raw = (tm or "").strip().lower()
    if "after" in raw:
        when = "Post-Mkt"
    elif "pre" in raw:
        when = "Pre-Mkt"
    elif raw:
        when = raw.title()
    else:
        when = "Time N/A"
    return f"{day_txt}, {when}"


def _fetch_recent_earnings_actual_est(ticker: str) -> tuple[float | None, float | None]:
    ck = f"earn_actual:{ticker}"
    cached = _cache_get(ck, ttl_seconds=600)
    if isinstance(cached, dict):
        return _to_num(cached.get("actual")), _to_num(cached.get("est"))
    actual = est = None
    if yf is not None:
        try:
            tk = yf.Ticker(ticker)
            df = None
            if hasattr(tk, "get_earnings_dates"):
                df = tk.get_earnings_dates(limit=6)
            if df is not None and hasattr(df, "columns") and hasattr(df, "iloc") and len(df) > 0:
                cols = {str(c).lower(): c for c in list(df.columns)}
                a_col = cols.get("reported eps") or cols.get("eps actual")
                e_col = cols.get("eps estimate")
                if a_col is not None:
                    actual = _to_num(df.iloc[0][a_col])
                if e_col is not None:
                    est = _to_num(df.iloc[0][e_col])
        except Exception:
            pass
    _cache_put(ck, {"actual": actual, "est": est})
    return actual, est


def _recent_reported_earnings_snapshot(ticker: str, max_days: int = 10) -> dict[str, object]:
    ck = f"earn_recent:{ticker}:{max_days}"
    cached = _cache_get(ck, ttl_seconds=7200)
    if isinstance(cached, dict):
        return dict(cached)
    out: dict[str, object] = {}
    if yf is None:
        return out
    try:
        tk = yf.Ticker(ticker)
        df = None
        if hasattr(tk, "get_earnings_dates"):
            df = tk.get_earnings_dates(limit=8)
        if df is None or not hasattr(df, "columns") or not hasattr(df, "iloc") or len(df) <= 0:
            _cache_put(ck, out)
            return out
        cols = {str(c).lower(): c for c in list(df.columns)}
        a_col = cols.get("reported eps") or cols.get("eps actual")
        e_col = cols.get("eps estimate")
        today = dt.date.today()
        for i in range(min(len(df), 8)):
            d = _extract_date_obj(df.index[i]) if hasattr(df, "index") else None
            if d is None or d > today:
                continue
            days_ago = (today - d).days
            if days_ago < 0 or days_ago > max_days:
                continue
            actual = _to_num(df.iloc[i][a_col]) if a_col is not None else None
            if actual is None:
                continue
            est = _to_num(df.iloc[i][e_col]) if e_col is not None else None
            surprise = ((actual - est) / abs(est) * 100.0) if est not in (None, 0.0) else None
            verdict = "REPORTED"
            if isinstance(surprise, float):
                verdict = "BEAT" if surprise >= 0 else "MISS"
            out = {
                "date": d.isoformat(),
                "days_ago": days_ago,
                "actual": actual,
                "est": est,
                "surprise_pct": surprise,
                "verdict": verdict,
            }
            break
    except Exception:
        out = {}
    _cache_put(ck, out)
    return out


def get_earnings_cards(
    rows: list[str],
    max_cards: int = 12,
    focus_universe: set[str] | None = None,
    include_all: bool = False,
) -> list[dict[str, object]]:
    cards: list[dict[str, object]] = []
    for ln in rows:
        s = ln.strip().lstrip("-").strip()
        parts = [p.strip() for p in s.split("|")]
        # Expected: STATUS | TICKER | DATE TIME | mcap $... | EPS $x vs $y
        if len(parts) < 5:
            continue
        status = parts[0].upper()
        ticker = parts[1].upper()
        if focus_universe and (not include_all) and ticker not in focus_universe:
            continue
        dt_part = parts[2]
        mcap_part = parts[3].replace("mcap", "").strip()
        eps_part = parts[4].replace("EPS", "").strip()
        date_s = ""
        tm = ""
        if " " in dt_part:
            date_s, tm = dt_part.split(" ", 1)
        else:
            date_s = dt_part
        left = eps_part
        right = ""
        if " vs " in eps_part:
            left, right = [x.strip() for x in eps_part.split(" vs ", 1)]
        # Parsed from ranked line "EPS $actual vs $estimate" when available.
        actual_line = _parse_eps_num(left)
        estimate_line = _parse_eps_num(right)

        card: dict[str, object] = {
            "ticker": ticker,
            "status": status,
            "when": _earnings_time_label(date_s, tm),
            "mcap": _fmt_cap_short(mcap_part),
            "deepdive": f"/deepdive?t={ticker}",
            "klass": "upcoming",
            "label": "UPCOMING",
            "headline": f"Est: {left or '-'}",
            "sub": "",
        }

        if status == "REPORTED":
            actual, estimate = actual_line, estimate_line
            if actual is None or estimate is None:
                pa, pe = _extract_eps_pair_from_text(s)
                actual = actual if actual is not None else pa
                estimate = estimate if estimate is not None else pe
            if actual is None or estimate is None:
                ya, ye = _fetch_recent_earnings_actual_est(ticker)
                actual = actual if actual is not None else ya
                estimate = estimate if estimate is not None else ye
            if actual is None or estimate is None:
                la, le = _llm_extract_eps_pair(s)
                actual = actual if actual is not None else la
                estimate = estimate if estimate is not None else le
            surprise = None
            if actual is not None and estimate not in (None, 0.0):
                surprise = (actual - estimate) / abs(estimate) * 100.0
            guidance = _guidance_tag(s)
            why_line = _earnings_move_why(ticker, s, surprise, guidance)
            if surprise is not None:
                beat = actual >= estimate
                card["klass"] = "beat" if beat else "miss"
                card["label"] = f"{'BEAT' if beat else 'MISS'} ({surprise:+.1f}%)"
                card["headline"] = f"EPS: {actual:.2f}"
                card["sub"] = f"Est: {estimate:.2f}"
            else:
                card["klass"] = "reported"
                card["label"] = "REPORTED"
                card["headline"] = f"EPS: {left or '-'}"
                card["sub"] = "Estimate unavailable (fallback parse did not resolve)."
            card["guidance"] = guidance
            card["why"] = why_line
        else:
            card["guidance"] = "N/A"
            card["why"] = ""
        cards.append(card)
        if len(cards) >= max_cards:
            break
    return cards


def markdown_to_html(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    in_list = False
    in_code = False
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip("\n")
        if line.strip().startswith("```"):
            if not in_code:
                out.append("<pre><code>")
            else:
                out.append("</code></pre>")
            in_code = not in_code
            i += 1
            continue
        if in_code:
            out.append(html.escape(line))
            i += 1
            continue
        s = line.strip()
        if not s:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<div class='sp'></div>")
            i += 1
            continue
        # Markdown table block:
        # | col | col |
        # | --- | --- |
        def _is_table_row(x: str) -> bool:
            return x.startswith("|") and x.count("|") >= 2
        if _is_table_row(s) and (i + 1) < len(lines):
            sep = lines[i + 1].strip()
            if re.fullmatch(r"\|?[\s:\-|]+\|?", sep or ""):
                if in_list:
                    out.append("</ul>")
                    in_list = False
                head = [c.strip() for c in s.strip("|").split("|")]
                rows: list[list[str]] = []
                i += 2
                while i < len(lines):
                    rs = lines[i].strip()
                    if not _is_table_row(rs):
                        break
                    rows.append([c.strip() for c in rs.strip("|").split("|")])
                    i += 1
                thead = "".join(f"<th>{html.escape(c)}</th>" for c in head)
                tbody_rows: list[str] = []
                for r in rows:
                    cells = r + ([""] * max(0, len(head) - len(r)))
                    tbody_rows.append("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in cells[: len(head)]) + "</tr>")
                out.append("<div class='tbl-wrap'><table><thead><tr>" + thead + "</tr></thead><tbody>" + "".join(tbody_rows) + "</tbody></table></div>")
                continue
        if s.startswith("### "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h3>{html.escape(s[4:])}</h3>")
            i += 1
            continue
        if s.startswith("## "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h2>{html.escape(s[3:])}</h2>")
            i += 1
            continue
        if s.startswith("# "):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(f"<h1>{html.escape(s[2:])}</h1>")
            i += 1
            continue
        if s.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{html.escape(s[2:])}</li>")
            i += 1
            continue
        if s.startswith("---"):
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<hr>")
            i += 1
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        out.append(f"<p>{html.escape(s)}</p>")
        i += 1
    if in_list:
        out.append("</ul>")
    if in_code:
        out.append("</code></pre>")
    return "\n".join(out)


def latest_reports_map() -> dict[str, str]:
    return {
        "daily": latest("reports/terminal_daily_brief_*.md"),
        "appendix": latest("reports/terminal_appendix_*.md"),
        "weekly": latest("reports/terminal_weekly_outlook_*.md"),
        "monthly": latest("reports/terminal_monthly_ic_memo_*.md"),
        "quarterly": latest("reports/quarterly_report_*.md"),
        "l2": latest("reports/l2_digest_*.md"),
    }


def latest_deep_dive_file(ticker: str) -> str:
    if not ticker:
        return ""
    return latest(f"reports/deep_dive_{ticker.upper()}_*.md")


def file_age_minutes(path: str) -> float:
    if not path:
        return 1e9
    try:
        mt = os.path.getmtime(path)
    except Exception:
        return 1e9
    return max(0.0, (dt.datetime.now().timestamp() - mt) / 60.0)


def generate_deep_dive(ticker: str, force: bool = False, max_age_minutes: int = 180) -> tuple[bool, str]:
    ticker = ticker.upper().strip()
    if not ticker:
        return False, ""
    existing = latest_deep_dive_file(ticker)
    if existing and (not force) and file_age_minutes(existing) <= max_age_minutes:
        return False, existing
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    out = REPORTS / f"deep_dive_{ticker}_{stamp}.md"
    # Primary path: fresh Deep Dive synthesis from latest 24h news + AI model.
    try:
        dd = perform_deep_dive(ticker)
        if bool(dd.get("ok")):
            model = str(dd.get("model") or "-")
            analysis = str(dd.get("analysis") or "").strip()
            hls = dd.get("headlines") if isinstance(dd.get("headlines"), list) else []
            hls_txt = "\n".join(f"- {str(h)}" for h in hls[:8] if str(h).strip())
            sec_ctx = _live_sec_filing_snapshot(ticker)
            rev_ctx = _live_revenue_snapshot(ticker)
            px_ctx = _live_price_context(ticker)
            filing_ref = sec_ctx if sec_ctx.strip() else "Filing reference not found in current local SEC snapshot."
            recent_perf = rev_ctx if rev_ctx.strip() else "Recent performance details not found in current filings/market snapshot."
            market_resp = px_ctx if px_ctx.strip() else "Market response details not found in current quote/news snapshot."
            md = (
                f"# {ticker} Deep Dive\n"
                f"_Generated: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Model: {model}_\n\n"
                "## Filing Reference\n"
                f"{filing_ref}\n\n"
                "## Recent Performance\n"
                f"{recent_perf}\n\n"
                "## Market Response\n"
                f"{market_resp}\n\n"
                "## 24h Headline Pack\n"
                f"{(hls_txt or '- No headline titles captured.')}\n\n"
                "## AI Synthesis\n"
                f"{analysis}\n\n"
                "## Filing Context\n"
                f"{sec_ctx}\n"
            )
            out.write_text(md, encoding="utf-8")
            return True, str(out)
    except Exception:
        pass

    # Fallback path: legacy research agent generation.
    prompt = (
        f"Create a deep-dive investment research brief for {ticker} using the latest filings in my local context. "
        "Cover: business model, latest earnings context, key risks, red flags, debt/liquidity, management commentary changes, "
        "near-term catalysts, and a concise bull/base/bear framing. Use clear headings and bullet points."
    )
    cmd = ["python3", "research_agent.py", "ask", prompt]
    p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    if p.returncode == 0 and p.stdout.strip():
        out.write_text(p.stdout, encoding="utf-8")
        return True, str(out)
    existing = latest_deep_dive_file(ticker)
    if existing:
        return False, existing
    return False, ""


def read_beta_body() -> str:
    beta = latest("reports/terminal_beta_dashboard.html")
    if not beta:
        return "<div style='padding:12px;'>No Beta dashboard yet. Run Build Beta Dashboard.</div>"
    return Path(beta).read_text(encoding="utf-8", errors="ignore")


def _safe_read(path: str) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def _macro_watchdog_payload() -> dict[str, object]:
    paths = [
        INPUTS / "macro_watchdog_latest.json",
        REPORTS / "macro_watchdog_latest.json",
    ]
    for p in paths:
        if not p.exists():
            continue
        try:
            obj = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return {}


def _read_cpi_consensus_map() -> dict[str, dict[str, float]]:
    p = DATA / "cpi_consensus.json"
    if not p.exists():
        return {}
    try:
        obj = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    if not isinstance(obj, dict):
        return {}
    out: dict[str, dict[str, float]] = {}
    for k, v in obj.items():
        if not isinstance(k, str) or not re.match(r"^\d{4}-\d{2}$", k):
            continue
        if not isinstance(v, dict):
            continue
        row: dict[str, float] = {}
        for fld in ("all_items_mom_consensus", "core_mom_consensus"):
            try:
                if v.get(fld) is not None:
                    row[fld] = float(v.get(fld))
            except Exception:
                continue
        if row:
            out[k] = row
    return out


def _save_cpi_consensus_row(month_ym: str, all_mom: float | None, core_mom: float | None) -> tuple[bool, str]:
    if not re.match(r"^\d{4}-\d{2}$", month_ym or ""):
        return False, "Invalid month format. Use YYYY-MM."
    mp = _read_cpi_consensus_map()
    row: dict[str, float] = {}
    if all_mom is not None:
        row["all_items_mom_consensus"] = float(all_mom)
    if core_mom is not None:
        row["core_mom_consensus"] = float(core_mom)
    if not row:
        return False, "Enter at least one consensus value."
    mp[month_ym] = row
    p = DATA / "cpi_consensus.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(json.dumps(mp, ensure_ascii=True, indent=2), encoding="utf-8")
        return True, f"Saved CPI consensus for {month_ym}."
    except Exception as e:
        return False, f"Failed to save consensus: {str(e)[:120]}"


def _macro_watchdog_banner_html() -> str:
    payload = _macro_watchdog_payload()
    if not payload:
        return ""
    asof = str(payload.get("asof_utc") or "").strip()
    cpi_today = bool(payload.get("cpi_release_today"))
    fresh_mins = int(payload.get("freshness_minutes") or (90 if cpi_today else 240))
    age = None
    if asof:
        try:
            base = asof.replace(" UTC", "")
            ts = dt.datetime.strptime(base[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
            age = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 60.0
        except Exception:
            age = None
    headline = str(payload.get("headline") or "-").strip()
    surprise = payload.get("cpi_surprise") if isinstance(payload.get("cpi_surprise"), dict) else {}
    # Only show macro freshness banner on CPI release day.
    if not cpi_today:
        return ""
    def _fmt_pct(v: object) -> str:
        try:
            f = float(v)  # type: ignore[arg-type]
            return f"{f:+.2f}%"
        except Exception:
            return "-"
    surprise_line = ""
    show_surprise = cpi_today
    if isinstance(surprise, dict) and show_surprise:
        month = str(surprise.get("month") or "-")
        all_act = _fmt_pct(surprise.get("all_items_mom_actual"))
        all_pri = _fmt_pct(surprise.get("all_items_mom_prior"))
        all_vs = _fmt_pct(surprise.get("all_items_mom_vs_consensus"))
        core_act = _fmt_pct(surprise.get("core_mom_actual"))
        core_pri = _fmt_pct(surprise.get("core_mom_prior"))
        core_vs = _fmt_pct(surprise.get("core_mom_vs_consensus"))
        surprise_line = (
            f" <span class='muted'>| CPI Surprise {html.escape(month)}: "
            f"All MoM {html.escape(all_act)} (prior {html.escape(all_pri)}, vs est {html.escape(all_vs)}) ; "
            f"Core MoM {html.escape(core_act)} (prior {html.escape(core_pri)}, vs est {html.escape(core_vs)})</span>"
        )
    cls = "ok"
    state = "fresh"
    if age is None:
        cls = "warn"
        state = "unknown"
    elif age > fresh_mins:
        cls = "warn"
        state = "stale"
    cpi_tag = "CPI day" if cpi_today else "normal day"
    age_txt = f"{int(age)}m old" if isinstance(age, float) else "age unknown"
    return (
        f"<div class='macro-banner {cls}'>"
        f"<strong>Macro Freshness ({cpi_tag}):</strong> {html.escape(headline)} "
        f"<span class='muted'>| {html.escape(state)} | {html.escape(age_txt)} | as of {html.escape(asof or '-')}</span>"
        f"{surprise_line}"
        "</div>"
    )


def _pick_first_existing(paths: list[str]) -> str:
    for p in paths:
        if p and Path(p).exists():
            return p
    return ""


def _resolve_exec_report_inputs() -> dict[str, str]:
    rpt = latest_reports_map()
    daily_candidates = [
        latest("morning_intelligence_*.md"),
        latest("reports/morning_intelligence_*.md"),
        latest("reports/.terminal_inputs/morning_intelligence_*.md"),
        rpt.get("daily", ""),
    ]
    weekly_candidates = [
        latest("terminal_weekly_outlook*.md"),
        latest("reports/terminal_weekly_outlook*.md"),
        rpt.get("weekly", ""),
    ]
    monthly_candidates = [
        latest("terminal_monthly_memo*.md"),
        latest("terminal_monthly_ic_memo*.md"),
        latest("reports/terminal_monthly_memo*.md"),
        latest("reports/terminal_monthly_ic_memo*.md"),
        rpt.get("monthly", ""),
    ]
    quarterly_candidates = [
        latest("quarterly_report*.md"),
        latest("reports/quarterly_report*.md"),
        rpt.get("quarterly", ""),
    ]
    return {
        "daily": _pick_first_existing(daily_candidates),
        "weekly": _pick_first_existing(weekly_candidates),
        "monthly": _pick_first_existing(monthly_candidates),
        "quarterly": _pick_first_existing(quarterly_candidates),
    }


def build_intelligence_packet(ttl_seconds: int = 900) -> str:
    """Build a report-event intelligence packet (not portfolio-based)."""
    sections: list[str] = []
    rpt = latest_reports_map()
    bundle = _report_intel_bundle(rpt)
    per_report = dict(bundle.get("per_report") or {})
    overlap = list(bundle.get("overlap") or [])
    misses = list(bundle.get("misses") or [])
    actions = list(bundle.get("actions") or [])
    deltas = dict(bundle.get("deltas") or {})
    truth_stack = dict(bundle.get("truth_stack") or {})
    change_detection = list(bundle.get("change_detection") or [])

    sections.append("## TIMEFRAME INTELLIGENCE")
    for k in ["daily", "appendix", "weekly", "monthly", "quarterly", "l2"]:
        info = dict(per_report.get(k) or {})
        intel_text = str(info.get("intel") or "").strip()
        stance = str(info.get("stance") or "Neutral")
        topics = list(info.get("topics") or [])
        if not intel_text:
            continue
        sections.append(f"  {k.upper()}: stance={stance} topics={','.join(topics[:5])}")
        sections.append(f"    {intel_text[:350]}")

    sections.append("")
    sections.append("## CROSS-REPORT MAP")
    sections.append(f"  Overlap: {', '.join(overlap) if overlap else 'none'}")
    sections.append(f"  Cross-report confirmed theme: {truth_stack.get('confirmed_by', '-')}")
    sections.append(f"  Cross-report conflict: {truth_stack.get('conflicts_with', '-')}")
    sections.append(f"  Missing proof: {truth_stack.get('missing_proof', '-')}")
    for x in change_detection[:5]:
        sections.append(f"  Change: {x}")
    for k, v in deltas.items():
        sections.append(f"  Delta {k}: {v}")
    for m in misses[:8]:
        sections.append(f"  Gap: {m}")

    sections.append("")
    sections.append("## EVENT QUEUE (IMPORTANCE-RANKED)")
    pri = {"high": 0, "medium": 1, "low": 2}
    ordered = sorted(
        actions,
        key=lambda a: (
            pri.get(str(a.get("confidence") or "medium").strip().lower(), 1),
            0 if str(a.get("source") or "") == "must_read_filings" else 1,
            str(a.get("text") or ""),
        ),
    )
    for a in ordered[:24]:
        sections.append(
            f"  [{str(a.get('confidence') or 'medium').upper()}] "
            f"{str(a.get('source') or '-')} | {str(a.get('text') or '')}"
        )

    return "\n".join(sections)


def _build_synthesis_prompt(packet: str) -> tuple[str, str]:
    """Return (user_prompt, system_prompt) for the executive synthesis LLM call."""
    system = (
        "You are a CIO synthesis engine focused on report-event intelligence (not portfolio positions). "
        "You receive a structured packet from Daily, Appendix, Weekly, Monthly, and Quarterly reports plus derived event queue. "
        "Prioritize like a Buffett-style long-term investor: business durability, balance-sheet risk, "
        "capital allocation quality, and management signaling. "
        "Rules: "
        "1) Use ONLY the data provided in the packet. Never invent facts or reference external events. "
        "2) Every action item MUST reference a specific ticker/event and a specific data point from the packet. "
        "3) If a data point is missing or unclear, say so explicitly. "
        "4) Be direct and specific — no generic market commentary."
    )

    user_prompt = (
        f"{packet}\n\n"
        "---\n"
        "Based on ALL the intelligence above, produce the following structured synthesis:\n\n"
        "**MARKET RISK SUMMARY** (2-3 sentences)\n"
        "Overall risk posture from report evidence only. Mention dominant regime and highest-impact stress point.\n\n"
        "**TOP 3 ACTION ITEMS** (numbered, verb-first)\n"
        "The 3 most important report-derived items today, ranked by importance and urgency. "
        "Each MUST cite ticker/event + packet evidence.\n\n"
        "**QUALITY WATCHLIST (TOP 3)**\n"
        "Name 3 report events/tickers worth long-term tracking and why (durability/solvency/capital-allocation lens).\n\n"
        "**SIGNAL CONVERGENCE** (1 sentence)\n"
        "The one theme connecting daily/appendix/weekly/monthly/quarterly signals.\n\n"
        "Keep total output under 500 words. Be specific, not generic."
    )
    return user_prompt, system


def generate_executive_synthesis(ttl_seconds: int = 1800) -> str:
    inp = _resolve_exec_report_inputs()
    sig = "|".join(f"{k}:{_file_sig(v)}" for k, v in inp.items())
    key = f"exec_synth_v3:{hashlib.sha256(sig.encode('utf-8', errors='ignore')).hexdigest()[:24]}"
    cached = _cache_get(key, ttl_seconds=ttl_seconds)
    if isinstance(cached, str) and cached.strip():
        return cached.strip()

    has_content = any(_safe_read(inp.get(k, "")) for k in ("daily", "weekly", "monthly", "quarterly"))
    if not has_content:
        return "Executive synthesis unavailable: report inputs not found."

    with LOCK:
        running = bool(AI_SUMMARY_REFRESH.get(key, False))
        if not running:
            AI_SUMMARY_REFRESH[key] = True

            def _worker() -> None:
                try:
                    # Step 1: Build report-event intelligence packet
                    packet = build_intelligence_packet(ttl_seconds=900)
                    if not packet or "TIMEFRAME INTELLIGENCE" not in packet:
                        _cache_put(key, "Executive synthesis unavailable: report-event packet could not be built.")
                        return

                    # Step 2: Build report-event prompt
                    user_prompt, system_prompt = _build_synthesis_prompt(packet)

                    out = ""

                    # Step 3 — Primary: gpt-4o
                    try:
                        txt, provider, model = _ask_llm_with_provider(
                            user_prompt[:120000], system_prompt, provider="openai", model_override="gpt-4o"
                        )
                        out = (txt or "").strip()
                        if out:
                            out += f"\n\n_Generated by {model} via {provider}_"
                    except Exception:
                        out = ""

                    # Step 4 — Fallback 1: Gemini Flash
                    if not out and _gemini_synthesize_reports is not None:
                        try:
                            out = _gemini_synthesize_reports(
                                "", "", "", "", intelligence_packet=packet
                            ).strip()
                            if out:
                                out += "\n\n_Generated by Gemini Flash (fallback)_"
                        except Exception:
                            out = ""

                    # Step 5 — Fallback 2: Ollama local
                    if not out:
                        try:
                            txt, _prov, _mdl = _ask_llm_with_provider(
                                user_prompt[:120000], system_prompt, provider="ollama"
                            )
                            out = (txt or "").strip()
                            if out:
                                out += "\n\n_Generated by local Ollama (fallback)_"
                        except Exception:
                            out = ""

                    if not out:
                        out = "Executive synthesis unavailable right now. Click Run Executive Synthesis to retry."

                    _cache_put(key, out)
                except Exception as e:
                    _cache_put(key, f"Executive synthesis failed: {str(e)[:220]}")
                finally:
                    with LOCK:
                        AI_SUMMARY_REFRESH[key] = False

            th = threading.Thread(target=_worker, daemon=True)
            th.start()

    return "Executive synthesis is generating... reload in a few seconds."


def _report_topics(text: str) -> set[str]:
    s = (text or "").lower()
    out: set[str] = set()
    topic_map = {
        "rates": ["rates", "yield", "treasury", "fed"],
        "inflation": ["inflation", "cpi", "ppi"],
        "guidance": ["guidance", "outlook", "raised outlook", "cut guidance"],
        "margins": ["margin", "gross margin", "operating margin"],
        "consumer": ["consumer", "demand", "retail", "spend"],
        "labor": ["labor", "wage", "hiring", "employment"],
        "credit": ["credit", "default", "liquidity", "debt", "covenant"],
        "regulation": ["regulation", "antitrust", "investigation", "subpoena"],
        "ai": ["ai", "artificial intelligence", "automation"],
    }
    for k, words in topic_map.items():
        if any(w in s for w in words):
            out.add(k)
    return out


def _extract_must_read_lines(text: str, limit: int = 20) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        m = re.search(r"\*\*([A-Z][A-Z0-9.\-]{0,9})\*\*.*?\bscore\s+(\d+)\b.*?keywords:\s*(.+)$", s)
        if not m:
            continue
        out.append(
            {
                "ticker": m.group(1).strip().upper(),
                "score": int(m.group(2)),
                "raw": s,
                "keywords": m.group(3).strip(),
            }
        )
        if len(out) >= limit:
            break
    return out


def _extract_appendix_earnings_rows(text: str, limit: int = 200) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s.startswith("| 20"):
            continue
        parts = [p.strip() for p in s.strip("|").split("|")]
        if len(parts) < 7:
            continue
        ticker = parts[1].upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker):
            continue
        out.append(
            {
                "date": parts[0],
                "ticker": ticker,
                "company": parts[2],
                "time": parts[3],
                "eps_est": parts[4],
                "last_eps": parts[5],
                "mcap": parts[6],
            }
        )
        if len(out) >= limit:
            break
    return out


def _parse_mcap_value(s: str) -> float:
    t = str(s or "").strip().upper().replace(",", "").replace("$", "")
    if not t:
        return 0.0
    mul = 1.0
    if t.endswith("T"):
        mul = 1_000_000_000_000.0
        t = t[:-1]
    elif t.endswith("B"):
        mul = 1_000_000_000.0
        t = t[:-1]
    elif t.endswith("M"):
        mul = 1_000_000.0
        t = t[:-1]
    try:
        return float(t) * mul
    except Exception:
        return 0.0


def _appendix_portfolio_intel_text(text: str) -> str:
    earn_rows = _extract_appendix_earnings_rows(text or "", limit=500)
    must_read = _extract_must_read_lines(text or "", limit=40)
    portfolio = {t.upper() for t in read_portfolio_tickers()}
    watchlist = {t.upper() for t in read_watchlist_tickers()}
    holdings = portfolio | watchlist

    today = dt.date.today()
    hold_events: list[tuple[dt.date, dict[str, str], str]] = []
    all_events: list[tuple[dt.date, dict[str, str], str]] = []
    for r in earn_rows:
        dtx = str(r.get("date") or "").strip()
        try:
            d = dt.datetime.strptime(dtx[:10], "%Y-%m-%d").date()
        except Exception:
            continue
        if d < (today - dt.timedelta(days=2)):
            continue
        tk = str(r.get("ticker") or "").upper()
        tm = str(r.get("time") or "").strip().lower()
        all_events.append((d, r, tk))
        if tk in holdings:
            hold_events.append((d, r, tk))

    hold_events.sort(key=lambda x: x[0])
    all_events.sort(key=lambda x: (x[0], -_parse_mcap_value(x[1].get("mcap", ""))))
    hold_must = [m for m in must_read if str(m.get("ticker") or "").upper() in holdings]

    upcoming_7 = [x for x in hold_events if (x[0] - today).days <= 7]
    upcoming_14 = [x for x in hold_events if (x[0] - today).days <= 14]

    signal = "Neutral"
    if upcoming_7 or hold_must:
        signal = "Event-heavy"
    elif upcoming_14:
        signal = "Catalyst build"

    if holdings and not hold_events and not hold_must:
        impact = "No direct holdings rows detected in the appendix parse."
        decision = "Use appendix as market read-through only; no holdings-specific action today."
    else:
        hold_bits: list[str] = []
        for d, r, tk in upcoming_14[:4]:
            hold_bits.append(f"{tk} ({(d - today).days}d)")
        must_bits = ", ".join(str(m.get("ticker") or "-") for m in hold_must[:3]) if hold_must else ""
        impact = (
            f"Holdings catalysts: {', '.join(hold_bits)}."
            if hold_bits
            else "No holdings earnings catalyst in next 14 days."
        )
        if must_bits:
            impact += f" Elevated filing-risk names in your scope: {must_bits}."
        decision = (
            "Pre-define post-earnings checklist for nearest holdings catalysts and update risk notes after print."
            if hold_bits
            else "Prioritize non-holdings read-through names by market cap and transfer only confirmed themes to watchlist."
        )

    top_universe = sorted(all_events, key=lambda x: _parse_mcap_value(x[1].get("mcap", "")), reverse=True)[:3]
    top_universe_txt = ", ".join(str(x[2]) for x in top_universe) if top_universe else "none"
    why = (
        f"Appendix parsed {len(earn_rows)} earnings rows and {len(must_read)} must-read filing flags. "
        f"Highest-impact universe prints: {top_universe_txt}."
    )

    if len(earn_rows) >= 30:
        conf = "High"
    elif len(earn_rows) >= 8:
        conf = "Medium"
    else:
        conf = "Low"

    return (
        f"Signal: {signal}.\n"
        f"Why it matters: {why}\n"
        f"Portfolio impact: {impact}\n"
        f"Decision now: {decision}\n"
        f"Confidence: {conf}"
    )


def _appendix_highlight_lines(text: str, limit: int = 36) -> list[str]:
    lines: list[str] = []
    added = set()
    raw = (text or "").splitlines()
    # 1) High-level section counters / context.
    for ln in raw:
        s = ln.strip()
        if not s:
            continue
        if any(k in s.lower() for k in [
            "universe sizes:",
            "combined deduped universe:",
            "universe earnings this week:",
            "reporting today/tomorrow",
        ]):
            if s.lower() not in added:
                lines.append(s)
                added.add(s.lower())
        if len(lines) >= 8:
            break
    # 2) Must-read filing risk rows.
    for m in _extract_must_read_lines(text, limit=12):
        row = str(m.get("raw") or "").strip()
        if row and row.lower() not in added:
            lines.append(row)
            added.add(row.lower())
        if len(lines) >= limit:
            return lines[:limit]
    # 3) Earnings rows (largest market cap first, then earliest date).
    erows = _extract_appendix_earnings_rows(text, limit=500)
    if erows:
        ranked = sorted(
            erows,
            key=lambda r: (-_parse_mcap_value(r.get("mcap", "")), str(r.get("date") or "")),
        )
        for r in ranked[:18]:
            s = (
                f"{r.get('ticker','-')} | {r.get('date','-')} {r.get('time','-')} | "
                f"Est {r.get('eps_est','-')} | LY {r.get('last_eps','-')} | MCAP {r.get('mcap','-')}"
            )
            if s.lower() not in added:
                lines.append(s)
                added.add(s.lower())
            if len(lines) >= limit:
                return lines[:limit]
    return lines[:limit]


def _appendix_global_intel_text(text: str, sig: str) -> str:
    full_text = (text or "").strip()
    if not full_text:
        return "Signal: Neutral.\nWhy it matters: Appendix has no readable content.\nPortfolio impact: Not applicable.\nDecision now: Regenerate appendix report.\nConfidence: Low"
    # Keep a compact helper block for deterministic fallback and signal grounding.
    lines = _appendix_highlight_lines(full_text, limit=50)
    helper = "\n".join(f"- {x}" for x in lines) if lines else "-"
    erows = _extract_appendix_earnings_rows(full_text, limit=800)
    top_earn = sorted(
        erows,
        key=lambda r: (-_parse_mcap_value(r.get("mcap", "")), str(r.get("date") or "")),
    )[:10]
    top_earn_txt = "\n".join(
        f"- {r.get('ticker','-')} | {r.get('date','-')} {r.get('time','-')} | "
        f"Est {r.get('eps_est','-')} | LY {r.get('last_eps','-')} | MCAP {r.get('mcap','-')}"
        for r in top_earn
    ) or "- none"
    cross_lines = []
    for ln in full_text.splitlines():
        s = ln.strip()
        if not s:
            continue
        low = s.lower()
        if "insider selling + near 52-week low" in low or ("**" in s and " | " in s and "score " in low):
            cross_lines.append(s)
        if len(cross_lines) >= 8:
            break
    cross_txt = "\n".join(f"- {x}" for x in cross_lines) if cross_lines else "- none"
    system = (
        "You are a Chief Investment Officer writing concise market intelligence from a terminal appendix report. "
        "This is NOT portfolio-specific. Read the full appendix text and produce a practical summary. "
        "Prioritize importance the way a Buffett-style long-term investor would: "
        "(1) durability of cash flows and business quality, "
        "(2) balance-sheet and solvency risk, "
        "(3) management behavior / insider signaling, "
        "(4) near-term earnings only when they change long-term thesis. "
        "Return exactly 5 lines with labels:\n"
        "Signal:\nWhy it matters:\nPortfolio impact:\nDecision now:\nConfidence:\n"
        "Each line must be concrete and include at least 1 named ticker/event where relevant. "
        "Avoid generic wording like 'monitor reactions' without naming what to monitor. "
        "For 'Portfolio impact' describe market-wide read-through only (not user holdings). "
        "No mention of data sources or model names."
    )
    # ChatGPT-style summary first; cache key is tied to report signature so updates auto-refresh.
    ck = f"appendix_global_intel:v4:{sig}"
    cached = _cache_get(ck, ttl_seconds=3600)
    if isinstance(cached, str) and cached.strip():
        return cached.strip()
    out = ""
    try:
        prompt = (
            "FULL APPENDIX REPORT:\n"
            f"{full_text[:200000]}\n\n"
            "HELPER HIGHLIGHTS (optional cross-check):\n"
            f"{helper}\n\n"
            "TOP EARNINGS BY IMPORTANCE (MCAP+TIMING):\n"
            f"{top_earn_txt}\n\n"
            "CROSS-SIGNAL FLAGS:\n"
            f"{cross_txt}"
        )
        txt, _p, _m = _ask_llm_with_provider(
            prompt, system, provider="openai", model_override="gpt-4o", strict_provider=True
        )
        out = (txt or "").strip()
    except Exception:
        out = ""
    if out:
        _cache_put(ck, out)
        return out
    # Deterministic fallback if LLM unavailable.
    mrows = _extract_must_read_lines(full_text, limit=20)
    sig_lbl = "Event-heavy" if len(erows) >= 20 else ("Watchful" if len(erows) >= 5 else "Neutral")
    top_eps = ", ".join(str(r.get("ticker") or "-") for r in sorted(erows, key=lambda r: -_parse_mcap_value(r.get("mcap", "")))[:3]) or "none"
    top_risk = ", ".join(str(r.get("ticker") or "-") for r in sorted(mrows, key=lambda r: int(r.get("score") or 0), reverse=True)[:3]) or "none"
    return (
        f"Signal: {sig_lbl}.\n"
        f"Why it matters: Appendix contains {len(erows)} earnings events and {len(mrows)} filing-risk signals.\n"
        f"Portfolio impact: Broad market read-through; largest upcoming prints are {top_eps}.\n"
        f"Decision now: Track top-cap earnings and cross-check any high-risk filing names ({top_risk}). GPT-4o was unavailable, so fallback logic was used.\n"
        "Confidence: Medium"
    )


def _previous_report_path(kind: str, current_path: str) -> str:
    pats = {
        "daily": "reports/terminal_daily_brief_*.md",
        "appendix": "reports/terminal_appendix_*.md",
        "weekly": "reports/terminal_weekly_outlook_*.md",
        "monthly": "reports/terminal_monthly_ic_memo_*.md",
        "quarterly": "reports/quarterly_report_*_brief.md",
        "l2": "reports/l2_digest_*.md",
    }
    pat = pats.get((kind or "").strip().lower())
    if not pat:
        return ""
    files = sorted(glob.glob(str(ROOT / pat)), key=os.path.getmtime, reverse=True)
    cur = str(current_path or "")
    for f in files:
        if cur and os.path.abspath(f) == os.path.abspath(cur):
            continue
        return f
    return ""


def _report_delta_line(kind: str, current_path: str) -> str:
    cur = _safe_read(current_path)
    prev_path = _previous_report_path(kind, current_path)
    prev = _safe_read(prev_path)
    if not cur or not prev:
        return "No prior report to compare."
    cur_topics = _report_topics(cur)
    prev_topics = _report_topics(prev)
    add = sorted(cur_topics - prev_topics)
    rem = sorted(prev_topics - cur_topics)
    dlen = len(cur) - len(prev)
    parts = [f"Size delta {dlen:+d} chars"]
    if add:
        parts.append("new themes: " + ", ".join(add[:3]))
    if rem:
        parts.append("faded themes: " + ", ".join(rem[:3]))
    if len(parts) == 1:
        parts.append("topic mix broadly stable")
    return " | ".join(parts)


def _confidence_from_text(text: str, default_level: str = "medium") -> str:
    s = (text or "").lower()
    if any(k in s for k in ["insufficient", "unavailable", "n/a"]):
        return "low"
    d = (default_level or "medium").lower()
    if d in {"high", "medium", "low"}:
        return d
    return "medium"


def _stance_label(text: str) -> str:
    s = (text or "").lower()
    pos = sum(
        1
        for w in [
            "strong",
            "improving",
            "raised",
            "growth",
            "beat",
            "resilient",
            "stabilizing",
            "opportunity",
        ]
        if w in s
    )
    neg = sum(
        1
        for w in [
            "risk",
            "default",
            "cut",
            "decline",
            "weak",
            "stress",
            "restructuring",
            "impairment",
            "litigation",
            "pressure",
        ]
        if w in s
    )
    score = pos - neg
    if score >= 2:
        return "Bullish"
    if score <= -2:
        return "Bearish"
    return "Neutral"


def _topic_diff(kind: str, current_path: str) -> tuple[set[str], set[str]]:
    cur = _safe_read(current_path)
    prev = _safe_read(_previous_report_path(kind, current_path))
    if not cur or not prev:
        return set(), set()
    cur_t = _report_topics(cur)
    prev_t = _report_topics(prev)
    return (cur_t - prev_t, prev_t - cur_t)


def _report_intel_bundle(rpt: dict[str, str]) -> dict[str, object]:
    texts = {k: _safe_read(v) for k, v in rpt.items()}
    labels = {
        "daily": "Daily Brief",
        "appendix": "Appendix",
        "weekly": "Weekly Outlook",
        "monthly": "Monthly IC Memo",
        "quarterly": "Quarterly Report",
        "l2": "L2 Synthesis Digest",
    }
    per_report: dict[str, dict[str, object]] = {}
    stances: dict[str, str] = {}
    for k, path in rpt.items():
        intel = _report_intelligence_text(k, path, text=texts.get(k, ""))
        key_lines = _report_key_lines(path, limit=7)
        st = _stance_label(texts.get(k, ""))
        stances[k] = st
        engine = ("GPT-4o" if k == "appendix" else "Llama (Ollama)")
        low_intel = str(intel or "").lower()
        if "fallback" in low_intel or "unavailable" in low_intel:
            ai_status = "fallback"
        else:
            ai_status = "ok"
        per_report[k] = {
            "label": labels.get(k, k.title()),
            "path": path,
            "intel": intel,
            "key_lines": key_lines,
            "topics": sorted(_report_topics(texts.get(k, ""))),
            "stance": st,
            "engine": engine,
            "ai_status": ai_status,
        }

    # Cross-report overlap and missing coverage.
    w = set(per_report.get("weekly", {}).get("topics", []))
    m = set(per_report.get("monthly", {}).get("topics", []))
    overlap = sorted(w & m)
    misses: list[str] = []
    if not overlap:
        misses.append("Weekly and Monthly have weak thematic overlap; align narrative.")
    elif len(overlap) <= 2:
        misses.append("Weekly/Monthly overlap is thin; may indicate narrative drift.")
    if rpt.get("quarterly"):
        try:
            age_days = int((time.time() - os.path.getmtime(rpt["quarterly"])) / 86400)
            if age_days > 21:
                misses.append(f"Quarterly brief is stale ({age_days}d old).")
        except Exception:
            pass

    # Report-derived action queue from Appendix + cross-report gaps.
    appendix_text = texts.get("appendix", "")
    earn_rows = _extract_appendix_earnings_rows(appendix_text, limit=300)
    must_read = _extract_must_read_lines(appendix_text, limit=30)
    actions: list[dict[str, str]] = []

    # Highest-risk filings from appendix golden list (report-wide, importance-ranked).
    ranked_must = sorted(must_read, key=lambda x: int(x.get("score") or 0), reverse=True)
    for row in ranked_must[:10]:
        sc = int(row.get("score") or 0)
        conf = "high" if sc >= 16 else ("medium" if sc >= 13 else "low")
        actions.append(
            {
                "scope": "report",
                "source": "must_read_filings",
                "confidence": conf,
                "text": f"{row['ticker']}: filing risk score {row['score']} | {row['keywords']}.",
            }
        )

    # Earliest earnings rows from appendix.
    def _earn_sort_key(r: dict[str, str]) -> tuple[str, str]:
        d = str(r.get("date") or "9999-12-31")
        t = str(r.get("ticker") or "ZZZ")
        return (d, t)

    today = dt.date.today()
    fresh_rows: list[dict[str, str]] = []
    for r in earn_rows:
        dtx = str(r.get("date") or "").strip()
        try:
            d = dt.datetime.strptime(dtx[:10], "%Y-%m-%d").date()
            # Keep recent + upcoming events, avoid stale historical queue noise.
            if d >= (today - dt.timedelta(days=2)):
                fresh_rows.append(r)
        except Exception:
            continue
    if not fresh_rows:
        # Fallback: if parsing fails, still keep upcoming-style ordering from raw rows.
        fresh_rows = list(earn_rows)

    def _earn_priority_key(r: dict[str, str]) -> tuple[int, str, float, str]:
        dtx = str(r.get("date") or "").strip()
        date_rank = 999
        if dtx:
            try:
                d = dt.datetime.strptime(dtx[:10], "%Y-%m-%d").date()
                date_rank = abs((d - today).days)
            except Exception:
                date_rank = 999
        mcap = -_parse_mcap_value(str(r.get("mcap") or ""))
        t = str(r.get("ticker") or "ZZZ")
        # Closer date first, then larger market cap, then ticker.
        return (date_rank, dtx or "9999-12-31", mcap, t)

    ranked_rows = sorted(fresh_rows, key=_earn_priority_key)[:16]
    for r in ranked_rows:
        t = str(r.get("ticker") or "-")
        mcap_val = _parse_mcap_value(str(r.get("mcap") or ""))
        conf = "high" if mcap_val >= 100_000_000_000 else "medium"
        actions.append(
            {
                "scope": "report",
                "source": "appendix_earnings",
                "confidence": conf,
                "text": f"{t}: earnings {r.get('date','-')} {r.get('time','-')} | Est {r.get('eps_est','-')} | review guidance + segment commentary.",
            }
        )

    if not earn_rows:
        misses.append("Appendix earnings table not parsed; improve report generation format.")
    if not must_read:
        misses.append("Appendix missing parsable must-read filing rows.")
    if not earn_rows:
        misses.append("Appendix missing parsable earnings table rows.")
    # De-duplicate actions by canonical text (case-insensitive).
    seen_actions: set[str] = set()
    dedup_actions: list[dict[str, str]] = []
    for a in actions:
        key = re.sub(r"\s+", " ", str(a.get("text") or "").strip().lower())
        if not key or key in seen_actions:
            continue
        seen_actions.add(key)
        dedup_actions.append(a)
    actions = dedup_actions
    actions.sort(key=lambda a: (0 if str(a.get("source")) == "must_read_filings" else 1, str(a.get("text") or "")))
    deltas = {k: _report_delta_line(k, str(rpt.get(k) or "")) for k in ["daily", "appendix", "weekly", "monthly", "quarterly", "l2"]}

    # Truth Stack
    available = [k for k in ["daily", "weekly", "monthly", "quarterly"] if str(rpt.get(k) or "")]
    commons: set[str] = set()
    if available:
        commons = set(per_report.get(available[0], {}).get("topics", []))
        for k in available[1:]:
            commons &= set(per_report.get(k, {}).get("topics", []))
    common_topic = sorted(commons)[0] if commons else ""
    confirmed_by = (
        f"Theme '{common_topic}' confirmed across {', '.join(k.title() for k in available)}."
        if common_topic
        else "No single theme is confirmed across all timeframes."
    )
    d_st = stances.get("daily", "Neutral")
    m_st = stances.get("monthly", "Neutral")
    conflicts_with = (
        f"Daily stance {d_st} conflicts with Monthly stance {m_st}."
        if d_st != m_st and "Neutral" not in {d_st, m_st}
        else "No hard Daily-vs-Monthly stance conflict detected."
    )
    q_topics = set(per_report.get("quarterly", {}).get("topics", []))
    d_topics = set(per_report.get("daily", {}).get("topics", []))
    missing = sorted(q_topics - d_topics)
    missing_proof = (
        f"Missing daily proof for quarterly themes: {', '.join(missing[:3])}."
        if missing
        else "Daily report reflects key quarterly themes."
    )
    truth_stack = {
        "confirmed_by": confirmed_by,
        "conflicts_with": conflicts_with,
        "missing_proof": missing_proof,
    }

    # Change Detection
    q_add, q_rem = _topic_diff("quarterly", str(rpt.get("quarterly") or ""))
    m_add, m_rem = _topic_diff("monthly", str(rpt.get("monthly") or ""))
    risk_topics = {"credit", "regulation", "guidance", "margins", "consumer"}
    new_risk_set = sorted((q_add | m_add) & risk_topics)
    resolved_set = sorted((q_rem | m_rem) & risk_topics)
    if new_risk_set:
        new_risk = f"New Risk: {new_risk_set[0]} appeared vs prior cycle."
    else:
        new_risk = "New Risk: none detected vs prior cycle."
    if resolved_set:
        risk_resolved = f"Risk Resolved: {resolved_set[0]} faded vs prior cycle."
    else:
        risk_resolved = "Risk Resolved: none clearly resolved."
    m_prev = _safe_read(_previous_report_path("monthly", str(rpt.get("monthly") or "")))
    m_prev_st = _stance_label(m_prev) if m_prev else "N/A"
    m_cur_st = stances.get("monthly", "Neutral")
    if m_prev_st != "N/A" and m_prev_st != m_cur_st:
        narrative_flip = f"Narrative Flip: Monthly stance changed {m_prev_st} -> {m_cur_st}."
    else:
        narrative_flip = "Narrative Flip: no major monthly stance flip."
    change_detection = [new_risk, risk_resolved, narrative_flip]

    return {
        "per_report": per_report,
        "overlap": overlap,
        "misses": misses[:6],
        "actions": actions[:18],
        "deltas": deltas,
        "truth_stack": truth_stack,
        "change_detection": change_detection,
    }


def _clear_report_intel_cache() -> None:
    prefixes = (
        "report_intel:",
        "report_intel_llama:",
        "report_intel_llama:v2:",
        "report_intel_llama:v3:",
        "report_intel:v3:",
        "appendix_global_intel:",
        "exec_synth:",
        "exec_synth_v",
        "macro_right_ai:",
        "brief:",
        "risk:",
        "earn:",
    )
    with LOCK:
        for k in list(ONYX_CACHE.keys()):
            if any(str(k).startswith(p) for p in prefixes):
                ONYX_CACHE.pop(k, None)
        for k in list(AI_SUMMARY_CACHE.keys()):
            if any(str(k).startswith(p) for p in prefixes):
                AI_SUMMARY_CACHE.pop(k, None)
        for k in list(AI_SUMMARY_REFRESH.keys()):
            if any(str(k).startswith(p) for p in prefixes):
                AI_SUMMARY_REFRESH.pop(k, None)


def report_studio_html(message: str = "", run_exec: bool = False) -> str:
    rpt = latest_reports_map()
    bundle = _report_intel_bundle(rpt)
    exec_synth = generate_executive_synthesis(ttl_seconds=1800)
    exec_manual = ""
    if run_exec:
        try:
            packet = build_intelligence_packet(ttl_seconds=300)
            user_prompt, system_prompt = _build_synthesis_prompt(packet)
            exec_manual = ""

            # Primary: gpt-4o
            try:
                txt, provider, model = _ask_llm_with_provider(
                    user_prompt[:120000], system_prompt, provider="openai", model_override="gpt-4o"
                )
                exec_manual = (txt or "").strip()
                if exec_manual:
                    exec_manual += f"\n\n_Generated by {model} via {provider}_"
            except Exception:
                exec_manual = ""

            # Fallback 1: Gemini Flash
            if not exec_manual and gemini is not None:
                try:
                    exec_manual = (gemini.synthesize_reports(
                        "", "", "", "", intelligence_packet=packet
                    ) or "").strip()
                    if exec_manual:
                        exec_manual += "\n\n_Generated by Gemini Flash (fallback)_"
                except Exception:
                    exec_manual = ""

            # Fallback 2: Ollama local
            if not exec_manual:
                try:
                    txt, _prov, _mdl = _ask_llm_with_provider(
                        user_prompt[:120000], system_prompt, provider="ollama"
                    )
                    exec_manual = (txt or "").strip()
                    if exec_manual:
                        exec_manual += "\n\n_Generated by local Ollama (fallback)_"
                except Exception:
                    exec_manual = ""

            if not exec_manual:
                exec_manual = "Executive synthesis returned an empty response."
        except Exception as e:
            exec_manual = f"Executive synthesis failed: {str(e)[:220]}"
    per_report = dict(bundle.get("per_report") or {})
    overlap = list(bundle.get("overlap") or [])
    misses = list(bundle.get("misses") or [])
    actions = list(bundle.get("actions") or [])
    deltas = dict(bundle.get("deltas") or {})
    truth_stack = dict(bundle.get("truth_stack") or {})
    change_detection = list(bundle.get("change_detection") or [])
    msg_html = f"<div class='msg'>{html.escape(message)}</div>" if message else ""
    overlap_html = ", ".join(html.escape(x) for x in overlap) if overlap else "none"

    def _clean_ui_text(s: str, max_len: int = 260) -> str:
        x = str(s or "")
        x = re.sub(r"\*\*([^*]+)\*\*", r"\1", x)
        x = re.sub(r"`([^`]+)`", r"\1", x)
        x = re.sub(r"\b[1-4]\)\s*", "", x)
        x = re.sub(r"\b(regime/stance|key context|key risk|key opportunity)\s*:\s*", "", x, flags=re.IGNORECASE)
        x = re.sub(r"\bsources?\s*:[^|.\n]*", "", x, flags=re.IGNORECASE)
        x = re.sub(r"\s+", " ", x).strip(" |.-")
        if len(x) > max_len:
            x = x[: max_len - 1].rstrip() + "..."
        return x

    def _extract_intel_field(block: str, label: str) -> str:
        m = re.search(rf"{re.escape(label)}\s*(.+?)(?:\n|$)", str(block or ""), flags=re.IGNORECASE)
        return (m.group(1).strip() if m else "")

    def _row_metric(label: str, value: str) -> str:
        return (
            "<div class='metric'>"
            f"<div class='metric-label'>{html.escape(label)}</div>"
            f"<div class='metric-val'>{html.escape(value)}</div>"
            "</div>"
        )

    truth_items = [
        ("Confirmed by", str(truth_stack.get("confirmed_by") or "-")),
        ("Conflicts with", str(truth_stack.get("conflicts_with") or "-")),
        ("Missing proof", str(truth_stack.get("missing_proof") or "-")),
    ]
    truth_metrics_html = "".join(_row_metric(k, v) for k, v in truth_items)

    change_list_html = "".join(f"<li>{html.escape(str(x))}</li>" for x in change_detection) or "<li class='muted'>No change-detection output yet.</li>"
    delta_html = "".join(f"<li><strong>{html.escape(k.title())}</strong>: {html.escape(str(v))}</li>" for k, v in deltas.items()) or "<li class='muted'>No delta comparison available.</li>"

    def _parse_action_row(a: dict[str, str]) -> tuple[str, str, str, str]:
        txt = str(a.get("text") or "")
        t_m = re.match(r"\s*([A-Z][A-Z0-9.\-]{0,6})\s*:", txt)
        if not t_m:
            t_m = re.search(r"\b([A-Z][A-Z0-9.\-]{0,6})\b", txt)
        d_m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", txt)
        ex_m = re.search(r"\b20\d{2}-\d{2}-\d{2}\s+([^|]{2,60})\s+\|\s*Est\b", txt)
        if not ex_m:
            ex_m = re.search(r"\|\s*Est\s+([^|]{2,80})", txt)
        ticker = t_m.group(1) if t_m else "-"
        date_txt = d_m.group(1) if d_m else "-"
        executive = (ex_m.group(1).strip() if ex_m else "-")
        if executive.lower() in {"pre market", "after hours", "post market", "not supplied"}:
            executive = "-"
        action_item = txt
        action_item = re.sub(r"^\s*[A-Z][A-Z0-9.\-]{0,6}\s*:\s*", "", action_item)
        action_item = re.sub(r"\|\s*Est\s*", " | Est ", action_item)
        action_item = re.sub(r"\s+", " ", action_item).strip()
        action_item = _clean_ui_text(action_item, max_len=170)
        action_item = action_item[:220] + ("..." if len(action_item) > 220 else "")
        return ticker, date_txt, executive, action_item

    action_rows = [_parse_action_row(dict(a)) for a in actions]
    action_table_rows = "".join(
        f"<tr><td>{html.escape(t)}</td><td>{html.escape(d)}</td><td>{html.escape(e)}</td><td>{html.escape(i)}</td></tr>"
        for t, d, e, i in action_rows
    ) or "<tr><td colspan='4' class='muted'>No action items generated yet.</td></tr>"

    snapshot_cards: list[str] = []
    for k in ["daily", "appendix", "weekly", "monthly", "quarterly", "l2"]:
        info = dict(per_report.get(k) or {})
        label = str(info.get("label") or k.title())
        path = str(info.get("path") or "")
        intel = str(info.get("intel") or "No intelligence generated yet.")
        stance = str(info.get("stance") or "Neutral")
        topics = [str(x) for x in list(info.get("topics") or [])]
        conf = _extract_intel_field(intel, "Confidence:")
        conf = conf.title() if conf else "Medium"
        if conf not in {"High", "Medium", "Low"}:
            conf = "Medium"
        engine = str(info.get("engine") or "AI")
        ai_status = str(info.get("ai_status") or "ok").strip().lower()
        ai_chip = "AI: GPT-4o OK" if (engine == "GPT-4o" and ai_status == "ok") else ("AI: Fallback" if ai_status != "ok" else "AI: OK")
        m_ctx = re.search(r"Why it matters:\s*(.+?)(?:\n|$)", intel, flags=re.IGNORECASE)
        if m_ctx:
            top = _clean_ui_text(m_ctx.group(1), max_len=210)
        else:
            top = _clean_ui_text(" ".join(intel.split()), max_len=210)
        if not top:
            top = "No context extracted yet."
        # Prefer intelligence fields over raw report lines.
        ai_points: list[str] = []
        for fld in ["Signal:", "Why it matters:", "Portfolio impact:", "Decision now:"]:
            v = _extract_intel_field(intel, fld)
            c = _clean_ui_text(v, max_len=200)
            if c:
                ai_points.append(c)
        if m_ctx and ai_points and ai_points[0] == top:
            ai_points = ai_points[1:]
        lines_raw = [str(x) for x in list(info.get("key_lines") or [])]
        lines: list[str] = []
        for ln in lines_raw:
            low = ln.lower()
            if "source:" in low or "sources:" in low:
                continue
            c = _clean_ui_text(ln, max_len=180)
            if c:
                lines.append(c)
            if len(lines) >= 2:
                break
        point_rows = ai_points[:2] if ai_points else lines
        points_html = "".join(f"<li>{html.escape(x)}</li>" for x in point_rows) or "<li class='muted'>No key points extracted.</li>"
        topic_txt = ", ".join(topics[:4]) if topics else "none"
        link = f"<a class='btn sm' href='/report?kind={k}'>Open Full</a>" if path else "<span class='muted'>Missing</span>"
        snapshot_cards.append(
            "<article class='snap-card'>"
            f"<div class='snap-head'><h4>{html.escape(label)}</h4><span class='muted'>{html.escape(mtime(path))}</span></div>"
            f"<div class='snap-badges'><span class='chip'>{html.escape(stance)}</span><span class='chip'>{html.escape(topic_txt)}</span><span class='chip conf-{conf.lower()}'>{html.escape(conf)} confidence</span><span class='chip'>{html.escape(engine)}</span><span class='chip ai-{html.escape(ai_status)}'>{html.escape(ai_chip)}</span></div>"
            f"<div class='snap-top'>{html.escape(top)}</div>"
            f"<ul class='snap-points'>{points_html}</ul>"
            f"<div class='snap-actions'>{link}</div>"
            "</article>"
        )
    snapshot_html = "".join(snapshot_cards)

    synth_to_show = exec_manual.strip() if exec_manual.strip() else exec_synth.strip()

    archive_cards: list[str] = []
    for k in ["daily", "appendix", "weekly", "monthly", "quarterly"]:
        info = dict(per_report.get(k) or {})
        label = str(info.get("label") or k.title())
        path = str(info.get("path") or "")
        content = _safe_read(path) if path else ""
        open_link = f"<a class='btn sm' href='/report?kind={k}'>Open Full</a>" if path else "<span class='muted'>Missing</span>"
        body = html.escape(content[:220000]) if content else "No report content found."
        archive_cards.append(
            "<details class='exp'>"
            f"<summary>📄 View Full {html.escape(label)} <span class='muted'>({html.escape(mtime(path))})</span></summary>"
            f"<div class='exp-head'>{open_link}</div>"
            f"<pre>{body}</pre>"
            "</details>"
        )

    return f"""<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Report Studio</title>
<style>
  :root {{ --bg:#F5F8FA; --panel:#FFFFFF; --line:#E1E6EB; --text:#33475B; --muted:#516F90; }}
  body {{ margin:0; font-family:"Avenir Next","Helvetica Neue",sans-serif; background:var(--bg); color:var(--text); }}
  .wrap {{ max-width:1160px; margin:0 auto; padding:18px; }}
  .card {{ background:#FFFFFF; border:1px solid var(--line); border-radius:12px; padding:14px; margin-bottom:12px; box-shadow:0 2px 5px rgba(0,0,0,.05); }}
  .btn {{ background:#FF7A59; color:#FFFFFF; border:1px solid #FF7A59; border-radius:8px; padding:7px 12px; text-decoration:none; display:inline-block; line-height:1.25; font-weight:700; }}
  .btn.sm {{ padding:4px 8px; font-size:12px; }}
  .muted {{ color:var(--muted); }}
  .msg {{ margin-bottom:10px; background:#FFFFFF; border:1px solid #E1E6EB; border-radius:8px; padding:8px; }}
  .hero {{ border:1px solid #FF7A59; box-shadow:none; }}
  .hero h2 {{ margin:0 0 8px 0; color:#33475B; }}
  .gold-box {{ white-space:pre-wrap; margin-top:8px; background:#FFFFFF; border:1px solid #E1E6EB; border-radius:8px; padding:10px; color:#33475B; line-height:1.45; }}
  .pulse {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
  .metric {{ border:1px solid #E1E6EB; background:#FFFFFF; border-radius:8px; padding:8px; margin-bottom:8px; }}
  .metric-label {{ font-size:11px; text-transform:uppercase; letter-spacing:.5px; color:#516F90; }}
  .metric-val {{ margin-top:4px; font-size:13px; color:#33475B; }}
  .compact-list {{ margin:0; padding-left:18px; }}
  .compact-list li {{ margin:5px 0; line-height:1.35; }}
  table {{ width:100%; border-collapse:collapse; }}
  th, td {{ border-bottom:1px solid #E1E6EB; padding:8px 6px; font-size:12px; text-align:left; vertical-align:top; }}
  th {{ color:#33475B; font-size:11px; text-transform:uppercase; letter-spacing:.5px; }}
  .snap-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }}
  .snap-card {{ border:1px solid #E1E6EB; background:#FFFFFF; border-radius:10px; padding:10px; }}
  .snap-head {{ display:flex; justify-content:space-between; gap:8px; align-items:flex-start; }}
  .snap-head h4 {{ margin:0; font-size:14px; }}
  .snap-badges {{ display:flex; gap:6px; margin-top:6px; flex-wrap:wrap; }}
  .chip {{ display:inline-block; border:1px solid #E1E6EB; border-radius:999px; padding:2px 8px; font-size:10px; color:#516F90; background:#FFFFFF; }}
  .conf-high {{ border-color:#00BDA5; color:#00BDA5; background:#E5F8F6; }}
  .conf-medium {{ border-color:#E1E6EB; color:#516F90; background:#FFFFFF; }}
  .conf-low {{ border-color:#D93B59; color:#D93B59; background:#FBEAEF; }}
  .ai-ok {{ border-color:#00BDA5; color:#00BDA5; background:#E5F8F6; }}
  .ai-fallback {{ border-color:#D93B59; color:#D93B59; background:#FBEAEF; }}
  .snap-top {{ margin-top:8px; font-size:13px; line-height:1.45; color:#33475B; }}
  .snap-points {{ margin:8px 0 0 18px; padding:0; }}
  .snap-points li {{ margin:4px 0; line-height:1.35; color:#516F90; font-size:12px; }}
  .snap-actions {{ margin-top:8px; }}
  .exp {{ border:1px solid #2e5168; border-radius:10px; background:#0f1a22; margin:10px 0; overflow:hidden; }}
  .exp summary {{ cursor:pointer; list-style:none; padding:10px 12px; font-weight:700; }}
  .exp summary::-webkit-details-marker {{ display:none; }}
  .exp-head {{ padding:0 12px 8px 12px; }}
  .exp pre {{ margin:0; background:#0b1218; border-top:1px solid #223442; padding:12px; overflow:auto; max-height:420px; }}
  @media (max-width:980px) {{ .pulse {{ grid-template-columns:1fr; }} .snap-grid {{ grid-template-columns:1fr; }} }}
</style></head><body><div class='wrap'>
  <div class='card'><h1>Report Studio — Clean Cockpit</h1><div class='muted'>Investor Terminal App [{APP_VERSION}]</div><a class='btn' href='/'>Back to Dashboard</a> <a class='btn' href='/reports?refresh=1'>Refresh Insights</a></div>
  {msg_html}

  <div class='card hero'>
    <div style='margin-bottom:8px;'><a class='btn' href='/reports?run_exec=1'>✨ Run Executive Synthesis</a></div>
    <h2>Executive Synthesis</h2>
    <div class='gold-box'>{html.escape(synth_to_show)}</div>
  </div>

  <div class='pulse'>
    <div class='card'>
      <h3 style='margin-top:0;'>Truth Stack</h3>
      {truth_metrics_html}
      <div class='muted'>Theme overlap: {overlap_html}</div>
    </div>
    <div class='card'>
      <h3 style='margin-top:0;'>Change Detection</h3>
      <ul class='compact-list'>{change_list_html}</ul>
      <ul class='compact-list'>{delta_html}</ul>
    </div>
  </div>

  <div class='card'>
    <h3 style='margin-top:0;'>Report Snapshot (One Page)</h3>
    <div class='snap-grid'>{snapshot_html}</div>
  </div>

  <div class='card'>
    <h3 style='margin-top:0;'>Report-Derived Action Queue</h3>
    <table>
      <thead><tr><th>Ticker</th><th>Date</th><th>Executive</th><th>Action Item</th></tr></thead>
      <tbody>{action_table_rows}</tbody>
    </table>
  </div>

  <div class='card'>
    <h3 style='margin-top:0;'>Archive</h3>
    {''.join(archive_cards)}
  </div>
</div></body></html>"""


def report_reader_html(kind: str, view: str = "paper") -> str:
    rpt = latest_reports_map()
    labels = {
        "daily": "Daily Brief",
        "appendix": "Appendix",
        "weekly": "Weekly Outlook",
        "monthly": "Monthly IC Memo",
        "quarterly": "Quarterly Report",
        "l2": "L2 Synthesis Digest",
    }
    if kind not in rpt:
        return report_studio_html("Unknown report type.")
    path = rpt.get(kind, "")
    if not path:
        return report_studio_html(f"{labels[kind]} is missing.")
    view_mode = "dark" if str(view or "").strip().lower() == "dark" else "paper"
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    intel = _report_intelligence_block(kind, path, text)
    rendered = markdown_to_html(text)
    bg = "radial-gradient(1200px 500px at -5% -5%, #273d50 0%, transparent 60%), #0a0f14" if view_mode == "dark" else "#e7edf2"
    top_bg = "linear-gradient(180deg,#1a2936,#121c24)" if view_mode == "dark" else "#f7fafc"
    top_line = "#2a3f50" if view_mode == "dark" else "#cfd9e3"
    paper_bg = "#0f171e" if view_mode == "dark" else "#ffffff"
    paper_line = "#2a3f50" if view_mode == "dark" else "#d6e0ea"
    text_color = "#e6edf4" if view_mode == "dark" else "#22384a"
    muted = "#97aebe" if view_mode == "dark" else "#5e7386"
    accent_bg = "#1a3d56" if view_mode == "dark" else "#edf3f8"
    accent_line = "#2e5c7b" if view_mode == "dark" else "#b7c8d8"
    toc_bg = "#0f2330" if view_mode == "dark" else "#f1f6fb"
    intel_bg = "#10202c" if view_mode == "dark" else "#eef5fb"
    intel_line = "#2f536b" if view_mode == "dark" else "#c9dceb"
    view_switch = "paper" if view_mode == "dark" else "dark"
    return f"""<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>{labels[kind]}</title>
<style>
  :root {{ --line:{top_line}; --text:{text_color}; --muted:{muted}; }}
  body {{ margin:0; color:var(--text); background:{bg}; font-family:"Avenir Next","Helvetica Neue",sans-serif; }}
  .wrap {{ max-width:1160px; margin:0 auto; padding:20px; }}
  .top {{ position:sticky; top:0; z-index:10; background:{top_bg}; border:1px solid var(--line); border-radius:12px; padding:14px; margin-bottom:12px; }}
  .paper {{ background:{paper_bg}; border:1px solid {paper_line}; border-radius:12px; padding:24px 28px; line-height:1.72; font-size:15px; color:{text_color}; box-shadow:0 1px 4px rgba(15,28,42,.08); }}
  .paper > * {{ max-width:92ch; }}
  h1,h2,h3 {{ margin:18px 0 10px; line-height:1.25; }} h1:first-child {{ margin-top:0; }}
  h1 {{ font-size:28px; }} h2 {{ font-size:22px; border-bottom:1px solid #223746; padding-bottom:4px; }} h3 {{ font-size:18px; }}
  p {{ margin:10px 0; }} ul {{ margin:10px 0 10px 22px; }} li {{ margin:6px 0; }}
  pre {{ overflow:auto; background:{'#0b1218' if view_mode == 'dark' else '#f8fbff'}; border:1px solid {'#223442' if view_mode == 'dark' else '#d5e2ee'}; border-radius:8px; padding:12px; line-height:1.45; font-size:13px; }}
  .tbl-wrap {{ overflow:auto; margin:10px 0; border:1px solid {'#2a3f50' if view_mode == 'dark' else '#d6e0ea'}; border-radius:8px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th,td {{ border-bottom:1px solid {'#243746' if view_mode == 'dark' else '#e1e8ef'}; padding:8px; text-align:left; vertical-align:top; }}
  th {{ background:{'#13222d' if view_mode == 'dark' else '#f4f8fc'}; }}
  hr {{ border:0; border-top:1px solid var(--line); margin:14px 0; }}
  .sp {{ height:6px; }}
  .btn {{ background:{accent_bg}; color:{text_color}; border:1px solid {accent_line}; border-radius:8px; padding:7px 12px; text-decoration:none; margin-right:6px; }}
  .muted {{ color:var(--muted); }}
  .intel {{ background:{intel_bg}; border:1px solid {intel_line}; border-radius:10px; padding:12px; margin-top:10px; }}
  .intel h3 {{ margin:0 0 6px 0; font-size:14px; }}
  .toc {{ margin-top:8px; display:flex; gap:8px; flex-wrap:wrap; }}
  .toc a {{ color:{text_color}; font-size:12px; border:1px solid {accent_line}; border-radius:999px; padding:3px 9px; text-decoration:none; background:{toc_bg}; }}
</style></head><body><div class='wrap'>
  <div class='top'>
    <h1>{labels[kind]}</h1>
    <div class='muted'>File: {html.escape(Path(path).name)} | Updated: {mtime(path)}</div>
    <div style='margin-top:8px;'><a class='btn' href='/reports'>All Reports</a><a class='btn' href='/'>Dashboard</a><a class='btn' href='/report?kind={kind}&view={view_switch}'>{'Paper View' if view_mode == 'dark' else 'Dark View'}</a><button class='btn' id='readBtn' type='button'>Read Aloud</button><button class='btn' id='stopBtn' type='button'>Stop</button></div>
    <div class='toc'><a href='#intelBox'>Intelligence</a><a href='#reportBody'>Report Body</a></div>
    <div class='intel' id='intelBox'><h3>Intelligence Layer</h3><pre style='white-space:pre-wrap;margin:0;'>{html.escape(intel)}</pre></div>
  </div>
  <div class='paper' id='reportBody'>{rendered}</div>
<script>
(() => {{
  const readBtn = document.getElementById('readBtn');
  const stopBtn = document.getElementById('stopBtn');
  const intel = document.getElementById('intelBox');
  const paper = document.querySelector('.paper');
  function getText() {{
    const i = intel ? intel.innerText : '';
    const p = paper ? paper.innerText : '';
    return (i + "\\n\\n" + p).slice(0, 12000);
  }}
  readBtn && readBtn.addEventListener('click', () => {{
    if (!('speechSynthesis' in window)) {{
      alert('Read aloud is not supported in this browser.');
      return;
    }}
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(getText());
    u.rate = 1.0;
    u.pitch = 1.0;
    window.speechSynthesis.speak(u);
  }});
  stopBtn && stopBtn.addEventListener('click', () => {{
    if ('speechSynthesis' in window) window.speechSynthesis.cancel();
  }});
}})();
</script>
</div></body></html>"""


def _report_key_lines(path: str, limit: int = 8) -> list[str]:
    if not path:
        return []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return []
    out: list[str] = []
    # Avoid boilerplate and source markers; prefer informational lines.
    skip_contains = [
        "this is the consolidated front page",
        "detailed data sections that support the daily brief",
        "strategic weekly synthesis and positioning context",
        "investment-committee level monthly memo",
        "*source:",
        "source mode:",
        "table of contents",
        "sources:",
        "source:",
    ]
    priority_terms = [
        "risk",
        "guidance",
        "default",
        "covenant",
        "impairment",
        "earnings",
        "eps",
        "revenue",
        "margin",
        "yield",
        "inflation",
        "rates",
        "credit",
        "regulation",
        "outlook",
    ]
    backup: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#"):
            continue
        if s.startswith("|---"):
            continue
        if s.startswith("- "):
            s = s[2:].strip()
        low = s.lower()
        if any(x in low for x in skip_contains):
            continue
        # Keep a clean backup pool for narrative-heavy reports.
        if len(s) >= 22 and len(s) <= 260 and not s.startswith("|"):
            backup.append(s)
        # Prefer lines with meaningful signal (numbers/keywords), not plain prose headers.
        has_num = bool(re.search(r"[$%]|[0-9]{2,}", s))
        has_kw = any(k in low for k in priority_terms)
        if not (has_num or has_kw):
            continue
        if len(s) > 260:
            s = s[:257].rstrip() + "..."
        out.append(s)
        if len(out) >= limit:
            break
    if len(out) < min(4, limit):
        seen = {x.lower() for x in out}
        for s in backup:
            k = s.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(s)
            if len(out) >= limit:
                break
    return out


def _report_heuristic_summary(kind: str, text: str, lines: list[str]) -> str:
    label = {
        "daily": "Daily Brief",
        "appendix": "Appendix",
        "weekly": "Weekly Outlook",
        "monthly": "Monthly IC Memo",
        "quarterly": "Quarterly Report",
        "l2": "L2 Synthesis Digest",
    }.get((kind or "").strip().lower(), "Report")
    body = (text or "").strip()
    stance = _stance_label(body)
    risk_terms = ("risk", "default", "covenant", "impairment", "litigation", "cut guidance", "restructuring")
    opp_terms = ("raised", "beat", "growth", "improving", "stabilizing", "opportunity", "upside")
    risk_line = next((x for x in lines if any(t in x.lower() for t in risk_terms)), "")
    opp_line = next((x for x in lines if any(t in x.lower() for t in opp_terms)), "")
    top_line = lines[0] if lines else f"{label} updated."
    if not risk_line:
        risk_line = "No explicit high-severity risk line was parsed from the latest extract."
    if not opp_line:
        opp_line = "No clear upside catalyst line was parsed from the latest extract."
    impact = "Monitor for second-order effects before changing position size."
    decision = "No immediate trade required; track next catalyst and guidance language."
    confidence = "Medium"
    low = (text or "").lower()
    if any(x in low for x in ("default", "covenant", "liquidity", "debt")):
        impact = "Refinancing/liquidity risk can compress multiples and raise downside volatility."
        decision = "Prioritize balance-sheet checks and avoid adding until debt signals stabilize."
        confidence = "High"
    elif any(x in low for x in ("beat", "raised", "growth", "improving")):
        impact = "Positive execution signals can support revisions and momentum continuation."
        decision = "Review whether the move is revision-driven or one-off before adding."
        confidence = "Medium"
    elif (kind or "").strip().lower() == "l2":
        impact = "Current signal set implies low urgency; risk/return is driven by weight concentration."
        decision = "Use L2 as triage: act only on HIGH/MEDIUM conflicts, otherwise monitor."
        confidence = "Low"
    line_ref = "L1" if lines else "L0"
    return (
        f"Signal: {stance}. [{line_ref}]\n"
        f"Why it matters: {top_line} [{line_ref}]\n"
        f"Portfolio impact: {impact} [{line_ref}]\n"
        f"Decision now: {decision} [{line_ref}]\n"
        f"Confidence: {confidence}"
    )


def _normalize_report_intel_output(raw: str, evidence_count: int) -> str:
    txt = str(raw or "").strip()
    if not txt:
        return ""
    wanted = ["Signal", "Why it matters", "Portfolio impact", "Decision now", "Confidence"]
    vals: dict[str, str] = {}
    for ln in txt.splitlines():
        m = re.match(r"^\s*(Signal|Why it matters|Portfolio impact|Decision now|Confidence)\s*:\s*(.+?)\s*$", ln, flags=re.IGNORECASE)
        if not m:
            continue
        key = str(m.group(1) or "").strip().lower()
        key = {
            "signal": "Signal",
            "why it matters": "Why it matters",
            "portfolio impact": "Portfolio impact",
            "decision now": "Decision now",
            "confidence": "Confidence",
        }.get(key, "")
        if key and key not in vals:
            vals[key] = str(m.group(2) or "").strip()
    if any(not vals.get(k, "").strip() for k in wanted):
        return ""

    max_ref = max(1, int(evidence_count or 0))

    def _fix_refs(s: str) -> str:
        refs = re.findall(r"\[L(\d{1,3})\]", s)
        if not refs:
            return f"{s} [L1]"
        out = s
        for r in refs:
            try:
                x = int(r)
            except Exception:
                x = 1
            if x < 1:
                x = 1
            if x > max_ref:
                x = max_ref
            out = re.sub(rf"\[L{re.escape(r)}\]", f"[L{x}]", out, count=1)
        return out

    for k in ["Signal", "Why it matters", "Portfolio impact", "Decision now"]:
        vals[k] = _fix_refs(vals[k]).strip()

    conf_raw = vals["Confidence"].strip().lower()
    if "high" in conf_raw:
        vals["Confidence"] = "High"
    elif "low" in conf_raw:
        vals["Confidence"] = "Low"
    else:
        vals["Confidence"] = "Medium"

    return "\n".join(f"{k}: {vals[k]}" for k in wanted)


def _report_intelligence_text(kind: str, path: str, text: str = "") -> str:
    k = (kind or "").strip().lower()
    if not path:
        return "Missing report."
    sig = _file_sig(path)
    if not sig:
        return "Report unavailable."
    labels = {
        "daily": "Daily Brief",
        "appendix": "Appendix",
        "weekly": "Weekly Outlook",
        "monthly": "Monthly IC Memo",
        "quarterly": "Quarterly Report",
        "l2": "L2 Synthesis Digest",
    }
    if k == "appendix":
        base = text or _safe_read(path)
        if base.strip():
            return _appendix_global_intel_text(base, sig=sig)
    lines = _report_key_lines(path, limit=12)
    if not lines:
        return "No readable lines found."
    src = "\n".join(f"[L{i + 1}] {x}" for i, x in enumerate(lines))
    system = (
        f"You are a PM decision-support analyst reading a {labels.get(k, 'report')}. "
        "Use ONLY provided evidence lines and extract decision value. "
        "Return exactly 5 lines with these labels and no extra text: "
        "Signal:, Why it matters:, Portfolio impact:, Decision now:, Confidence:. "
        "Confidence must be one of: High, Medium, Low. "
        "In the first 4 lines, include at least one evidence tag like [L3]. "
        "Do not mention the word 'source'. Avoid generic statements."
    )
    # Llama-first path for report intelligence, but never block page render.
    ck = f"report_intel_llama:v3:{k}:{sig}"
    cached = _cache_get(ck, ttl_seconds=3600)
    if isinstance(cached, str) and cached.strip():
        return cached.strip()
    with LOCK:
        running = bool(AI_SUMMARY_REFRESH.get(ck, False))
        if not running:
            AI_SUMMARY_REFRESH[ck] = True

            def _worker() -> None:
                try:
                    ai, _prov, _mdl = _ask_llm_with_provider(src[:120000], system, provider="ollama")
                    txt = _normalize_report_intel_output(ai or "", evidence_count=len(lines))
                    if txt:
                        _cache_put(ck, txt)
                except Exception:
                    pass
                finally:
                    with LOCK:
                        AI_SUMMARY_REFRESH[ck] = False

            th = threading.Thread(target=_worker, daemon=True)
            th.start()
    # Fallback to existing cached async summary path.
    ai2 = _ai_cached_reliable_summary(
        cache_key=f"report_intel:v3:{k}:{sig}",
        source_text=src,
        system=system,
        ttl_seconds=3600,
    )
    if ai2:
        txt2 = _normalize_report_intel_output(ai2, evidence_count=len(lines))
        if txt2:
            return txt2
    # fallback while AI cache warms (real context, not boilerplate)
    return _report_heuristic_summary(k, text or _safe_read(path), lines)


def _report_intelligence_preview(kind: str, path: str) -> str:
    txt = _report_intelligence_text(kind, path)
    one = " ".join(txt.split())
    return (one[:180] + "...") if len(one) > 183 else one


def _report_intelligence_block(kind: str, path: str, text: str) -> str:
    return _report_intelligence_text(kind, path, text=text)


def generate_l2_digest_report(force_recompute: bool = True) -> str:
    snap = get_l2_snapshot("portfolio", ttl_seconds=(0 if force_recompute else 900))
    rows = list(snap.get("rows") or [])
    contradictions = list(snap.get("contradictions") or [])
    queue = list(snap.get("queue") or [])
    drift = list(snap.get("drift") or [])
    asof = str(snap.get("asof") or dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M")
    out = REPORTS / f"l2_digest_{stamp}.md"
    lines: list[str] = []
    lines.append(f"# L2 Synthesis Digest ({asof})")
    lines.append("")
    lines.append(str(snap.get("summary") or ""))
    lines.append("")
    lines.append("## Top Holdings Triage")
    if rows:
        lines.append("| Ticker | Priority | Total | Conflict | Urgency | Support | Novelty | Weight | Earnings |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for r in rows[:15]:
            ed = r.get("earn_days")
            eds = str(ed) if isinstance(ed, int) else "-"
            lines.append(
                f"| {r.get('ticker')} | {r.get('priority')} | {int(r.get('total') or 0)} | "
                f"{int(r.get('conflict') or 0)} | {int(r.get('urgency') or 0)} | "
                f"{int(r.get('support') or 0)} | {int(r.get('novelty') or 0)} | "
                f"{float(r.get('weight') or 0.0):.1f}% | {eds} |"
            )
    else:
        lines.append("- No holdings available.")
    lines.append("")
    lines.append("## Portfolio Contradictions")
    if contradictions:
        for r in contradictions[:10]:
            lines.append(f"- {r.get('ticker')}: bullish thesis vs conflict={int(r.get('conflict') or 0)}.")
    else:
        lines.append("- No major contradictions detected.")
    lines.append("")
    lines.append("## 48h Action Queue")
    if queue:
        for r in queue[:10]:
            lines.append(
                f"- {r.get('ticker')}: priority={r.get('priority')}, urgency={int(r.get('urgency') or 0)}, "
                f"earnings_in={r.get('earn_days') if isinstance(r.get('earn_days'), int) else '-'}d."
            )
    else:
        lines.append("- No urgent 48h actions.")
    lines.append("")
    lines.append("## Signal Drift (vs previous run)")
    if drift:
        for d in drift[:12]:
            lines.append(
                f"- {d.get('ticker')}: Δtotal={int(d.get('delta_total') or 0):+d}, "
                f"Δconflict={int(d.get('delta_conflict') or 0):+d}, Δurgency={int(d.get('delta_urgency') or 0):+d}."
            )
    else:
        lines.append("- No material drift.")
    lines.append("")
    lines.append("## Notes")
    lines.append("- Engine uses thesis + decision log + SEC + insider + earnings timing + portfolio concentration.")
    lines.append("- Scores are deterministic risk/action triage, not valuation advice.")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(out)


def l2_html(ticker: str = "", message: str = "") -> str:
    t = resolve_ticker_input(ticker or "")
    snap = get_l2_snapshot("portfolio", ttl_seconds=900)
    rows = list(snap.get("rows") or [])
    idx = {str(r.get("ticker") or ""): r for r in rows}
    sel = idx.get(t) if t else None
    msg_html = f"<div class='msg'>{html.escape(message)}</div>" if message else ""
    asof = str(snap.get("asof") or "-")
    summary = str(snap.get("summary") or "")
    table_rows = "".join(
        f"<tr class='row-link' onclick=\"window.location='/l2?t={html.escape(str(r.get('ticker') or ''), quote=True)}#brain';\">"
        f"<td><a href='/l2?t={html.escape(str(r.get('ticker') or ''))}#brain'>{html.escape(str(r.get('ticker') or '-'))}</a></td>"
        f"<td>{html.escape(str(r.get('priority') or '-'))}</td>"
        f"<td>{int(r.get('total') or 0)}</td>"
        f"<td>{int(r.get('conflict') or 0)}</td>"
        f"<td>{int(r.get('urgency') or 0)}</td>"
        f"<td>{int(r.get('support') or 0)}</td>"
        f"<td>{int(r.get('novelty') or 0)}</td>"
        f"<td>{float(r.get('weight') or 0.0):.1f}%</td>"
        f"<td>{('In ' + str(r.get('earn_days')) + 'd') if isinstance(r.get('earn_days'), int) else '-'}</td></tr>"
        for r in rows
    ) or "<tr><td colspan='9' class='muted'>No holdings found.</td></tr>"
    detail_html = "<div class='muted'>Select a ticker from the table for drill-down evidence.</div>"
    brain_html = "<div class='muted'>Select a ticker row to view Brain Synthesis.</div>"
    if sel:
        ev = list(sel.get("evidence") or [])
        ev_html = "".join(f"<li>{html.escape(str(x))}</li>" for x in ev) or "<li class='muted'>No evidence rows.</li>"
        th = _latest_thesis_map([t]).get(t, {})
        drows = _decision_log_map(limit=400).get(t, [])
        dlog_html = "".join(
            f"<li>{html.escape(str(r.get('timestamp') or '-'))} | {html.escape(str(r.get('text') or ''))}</li>"
            for r in drows[:6]
        ) or "<li class='muted'>No tagged decision-log rows.</li>"
        thesis_txt = str(th.get("content") or "").strip()
        sig = _onyx_get_latest_signal(t) if _onyx_get_latest_signal is not None else {}
        sig_reason = str((sig or {}).get("reasoning") or "").strip()
        sig_ev = str((sig or {}).get("evidence_summary") or "").strip()
        if not sig_ev:
            sig_ev = "No evidence summary saved yet."
        # Make reasoning plain-English by removing boilerplate and confidence/status tokens.
        plain_lines: list[str] = []
        for ln in sig_reason.splitlines():
            s = ln.strip()
            if not s:
                continue
            low = s.lower()
            if low.startswith("status:") or low.startswith("confidence:"):
                continue
            s = re.sub(r"^[\-\*\u2022]+\s*", "", s).strip()
            if s:
                plain_lines.append(s)
        plain_reason = " ".join(plain_lines) if plain_lines else "No reasoning text available."
        c = int(sel.get("conflict") or 0)
        u = int(sel.get("urgency") or 0)
        if c >= 70:
            c_label = "high"
        elif c >= 45:
            c_label = "medium"
        else:
            c_label = "low"
        if u >= 70:
            u_label = "high"
        elif u >= 45:
            u_label = "medium"
        else:
            u_label = "low"
        c_badge = f"<span class='lvl lvl-{c_label}'>{c_label.upper()}</span>"
        u_badge = f"<span class='lvl lvl-{u_label}'>{u_label.upper()}</span>"
        brain_html = (
            f"<div id='brain'><h2>Brain Synthesis: {html.escape(t)}</h2>"
            f"<div class='muted'>Why scores are elevated in plain English.</div>"
            f"<div class='brain-box'><strong>Conflict ({c}) {c_badge}:</strong> "
            f"{html.escape('This score is driven by contradictions between your thesis and recent evidence.' if c >= 45 else 'No major thesis contradiction detected.')}</div>"
            f"<div class='brain-box'><strong>Urgency ({u}) {u_badge}:</strong> "
            f"{html.escape('This score is driven by near-term catalysts (earnings/events) and risk timing.' if u >= 45 else 'No immediate timing pressure detected.')}</div>"
            f"<div class='brain-box'><strong>Signal Reasoning:</strong> {html.escape(plain_reason)}</div>"
            f"<div class='brain-box'><strong>Evidence Summary:</strong> {html.escape(sig_ev)}</div>"
            "</div>"
        )
        detail_html = (
            f"<h2>Drill-Down: {html.escape(t)}</h2>"
            f"<div class='muted'>Priority {html.escape(str(sel.get('priority') or '-'))} | "
            f"Total {int(sel.get('total') or 0)} | Conflict {int(sel.get('conflict') or 0)} | "
            f"Urgency {int(sel.get('urgency') or 0)}</div>"
            f"<h3>Evidence Lines</h3><ul>{ev_html}</ul>"
            f"<h3>Latest Thesis</h3><pre>{html.escape(thesis_txt[:2500] if thesis_txt else 'No thesis found.')}</pre>"
            f"<h3>Tagged Decision Log</h3><ul>{dlog_html}</ul>"
        )
    return f"""<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>L2 Brain</title>
<style>
  :root {{ --bg:#0b1014; --panel:#131d25; --line:#294051; --text:#e7eef6; --muted:#9ab0c0; }}
  body {{ margin:0; font-family:"Avenir Next","Helvetica Neue",sans-serif; background:radial-gradient(900px 420px at 0% 0%, #22374a 0%, transparent 62%), var(--bg); color:var(--text); }}
  .wrap {{ max-width:1280px; margin:0 auto; padding:18px; }}
  .card {{ background:linear-gradient(180deg,#1a2936,#121c24); border:1px solid var(--line); border-radius:12px; padding:14px; margin-bottom:12px; }}
  .btn {{ background:#1a3d56; color:#e7eef6; border:1px solid #2e5c7b; border-radius:8px; padding:7px 12px; text-decoration:none; }}
  table {{ width:100%; border-collapse:collapse; font-size:12px; }} th,td {{ border-bottom:1px solid var(--line); padding:7px; text-align:left; }} .muted {{ color:var(--muted); }}
  tr.row-link {{ cursor:pointer; }}
  tr.row-link:hover {{ background:#182734; }}
  .grid {{ display:grid; grid-template-columns:1.2fr .8fr; gap:12px; }} .msg {{ margin-bottom:10px; background:#1f3445; border:1px solid #31536c; border-radius:8px; padding:8px; }}
  .brain-box {{ margin-top:8px; border:1px solid #2e526a; border-radius:8px; background:#0f1b24; padding:8px; line-height:1.4; }}
  .lvl {{ display:inline-block; margin-left:6px; border:1px solid #3a6078; border-radius:999px; padding:1px 7px; font-size:10px; letter-spacing:.3px; }}
  .lvl-high {{ border-color:#8a4a4a; color:#ffd3d8; background:#321818; }}
  .lvl-medium {{ border-color:#8a7f3a; color:#f2e3a6; background:#352f14; }}
  .lvl-low {{ border-color:#2f7f54; color:#b7f0c8; background:#103320; }}
  pre {{ white-space:pre-wrap; background:#0f171e; border:1px solid var(--line); border-radius:8px; padding:8px; }}
  @media (max-width:980px) {{ .grid {{ grid-template-columns:1fr; }} }}
</style></head><body><div class='wrap'>
  <div class='card'><h1>L2 Brain</h1><div class='muted'>{html.escape(summary)} As of {html.escape(asof)}</div>
    <div style='margin-top:8px;'><a class='btn' href='/'>Back Dashboard</a> <a class='btn' href='/reports'>Report Studio</a> <a class='btn' href='/l2?refresh=1'>Refresh L2</a> <a class='btn' href='/l2/digest'>Write Digest Now</a></div>
  </div>
  {msg_html}
  <div class='grid'>
    <div class='card'>
      <h2>Thesis vs Reality (Portfolio)</h2>
      <table><thead><tr><th>Ticker</th><th>Priority</th><th>Total</th><th>Conflict</th><th>Urgency</th><th>Support</th><th>Novelty</th><th>Weight</th><th>Earnings</th></tr></thead><tbody>{table_rows}</tbody></table>
      <div style='margin-top:12px;'>{brain_html}</div>
    </div>
    <div class='card'>{detail_html}</div>
  </div>
</div></body></html>"""


def _l2_sched_state_path() -> Path:
    return DATA / "l2_scheduler_state.json"


def _l2_sched_last_ts() -> float:
    p = _l2_sched_state_path()
    if not p.exists():
        return 0.0
    try:
        obj = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        return float(obj.get("last_ts") or 0.0)
    except Exception:
        return 0.0


def _l2_sched_save_now(path: str) -> None:
    p = _l2_sched_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    obj = {
        "last_ts": time.time(),
        "last_run": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_file": Path(path).name if path else "",
    }
    p.write_text(json.dumps(obj, ensure_ascii=True, indent=2), encoding="utf-8")


def _l2_scheduler_worker(interval_seconds: int = 21600) -> None:
    while True:
        try:
            last = _l2_sched_last_ts()
            now = time.time()
            if (now - last) >= interval_seconds:
                out = generate_l2_digest_report(force_recompute=True)
                _l2_sched_save_now(out)
        except Exception:
            pass
        time.sleep(300)


def start_l2_scheduler(interval_seconds: int = 21600) -> None:
    with LOCK:
        if bool(L2_SCHED.get("started")):
            return
        L2_SCHED["started"] = True
    th = threading.Thread(target=_l2_scheduler_worker, args=(interval_seconds,), daemon=True)
    th.start()


def _latest_thesis_text_for_ticker(ticker: str) -> str:
    t = (ticker or "").strip().upper()
    if not t:
        return ""
    rows = list_stock_thesis(t, limit=1)
    if not rows:
        return ""
    return str(rows[0]["content"] or "").strip()


def _portfolio_position_for_ticker(ticker: str) -> tuple[float | None, float | None]:
    t = (ticker or "").strip().upper()
    if not t:
        return None, None
    for r in read_portfolio_rows(DATA / "portfolio.csv"):
        if str(r[0] or "").strip().upper() == t:
            return to_float(str(r[1] or "")), to_float(str(r[2] or ""))
    return None, None


def _onyx_sync_holding(ticker: str, thesis_hint: str = "") -> None:
    if _onyx_add_portfolio_item is None:
        return
    t = (ticker or "").strip().upper()
    if not t:
        return
    th = _latest_thesis_text_for_ticker(t) or (thesis_hint or "").strip() or "No thesis yet."
    sh, cb = _portfolio_position_for_ticker(t)
    try:
        _onyx_add_portfolio_item(ticker=t, thesis=th, shares=sh, avg_cost=cb, status="Active")
    except Exception:
        return


def _onyx_backfill_portfolio_holdings() -> None:
    if _onyx_add_portfolio_item is None:
        return
    for r in read_portfolio_rows(DATA / "portfolio.csv"):
        t = str(r[0] or "").strip().upper()
        if not t:
            continue
        notes = str(r[3] or "").strip()
        _onyx_sync_holding(t, thesis_hint=notes)


def _onyx_log_ticker_evidence(ticker: str) -> None:
    if _onyx_log_new_evidence is None:
        return
    t = (ticker or "").strip().upper()
    if not t:
        return
    try:
        red_flag = latest("reports/.terminal_inputs/red_flag_alert_*.txt")
        if red_flag:
            rows = run_cmd(["python3", "tools/red_flag_rank.py", "--file", red_flag, "--limit", "200"])
            for ln in rows:
                if f"| {t} |" in ln:
                    _onyx_log_new_evidence(ticker=t, text=ln, ev_type="News", source_url="local:red_flag_alert")
                    break
        earnings = _latest_earnings_file()
        if earnings:
            er = run_cmd(["python3", "tools/earnings_watch_rank.py", "--file", earnings, "--limit", "200", "--scope", "week"])
            for ln in er:
                if f"| {t} |" in ln:
                    _onyx_log_new_evidence(ticker=t, text=ln, ev_type="Earnings", source_url="local:earnings_feed")
                    break
        ins = _insider_skin_signal(t)
        verdict = str(ins.get("verdict") or "")
        detail = str(ins.get("detail") or "")
        if verdict or detail:
            _onyx_log_new_evidence(
                ticker=t,
                text=f"Insider signal {verdict}: {detail}",
                ev_type="Insider",
                source_url="local:sec_form4+yf",
            )
    except Exception:
        return


def _onyx_signal_badge_html(sig: dict[str, str] | None) -> str:
    if not sig:
        return "-"
    st = str(sig.get("status") or "-").strip()
    rs = str(sig.get("reasoning") or "")
    rs_attr = html.escape(rs[:1200], quote=True)
    if "Thesis check failed:" in rs or "Thesis check unavailable:" in rs:
        return f"<button type='button' class='thesis-signal-btn sig-na' data-status='N/A' data-reasoning='{rs_attr}'>N/A</button>"
    if st == "Green":
        return f"<button type='button' class='thesis-signal-btn sig-green' data-status='Green' data-reasoning='{rs_attr}'>Green</button>"
    if st == "Red":
        return f"<button type='button' class='thesis-signal-btn sig-red' data-status='Red' data-reasoning='{rs_attr}'>Red</button>"
    return "-"


def _start_onyx_scheduler(interval_seconds: int = 21600) -> None:
    if _onyx_run_thesis_check_all_active is None:
        return
    with LOCK:
        if bool(ONYX_SCHED.get("started")):
            return
        ONYX_SCHED["started"] = True

    def _worker() -> None:
        while True:
            try:
                _onyx_run_thesis_check_all_active(min_interval_seconds=interval_seconds)
            except Exception:
                pass
            time.sleep(300)

    th = threading.Thread(target=_worker, daemon=True)
    th.start()


def _sec_risk_sched_state_path() -> Path:
    return DATA / "sec_risk_scheduler_state.json"


def _sec_risk_sched_last_ts() -> float:
    p = _sec_risk_sched_state_path()
    if not p.exists():
        return 0.0
    try:
        obj = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        return float(obj.get("last_ts") or 0.0)
    except Exception:
        return 0.0


def _sec_risk_sched_save_now(tickers: list[str]) -> None:
    p = _sec_risk_sched_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    obj = {
        "last_ts": time.time(),
        "last_run": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tickers": tickers[:10],
    }
    p.write_text(json.dumps(obj, ensure_ascii=True, indent=2), encoding="utf-8")


def _top_portfolio_tickers_for_sec(limit: int = 3) -> list[str]:
    rows = read_portfolio_rows(DATA / "portfolio.csv")
    tickers = [str(r[0] or "").strip().upper() for r in rows if str(r[0] or "").strip()]
    if not tickers:
        return []
    q = fetch_quote_map(tickers)
    ranked: list[tuple[float, str]] = []
    for t, sh_s, _c, _n in rows:
        tu = str(t or "").strip().upper()
        sh = to_float(str(sh_s or "")) or 0.0
        px = to_float(str(q.get(tu, {}).get("price") if isinstance(q.get(tu, {}), dict) else None))
        if sh > 0 and px and px > 0:
            ranked.append((sh * px, tu))
        else:
            ranked.append((0.0, tu))
    ranked.sort(key=lambda x: x[0], reverse=True)
    out: list[str] = []
    seen: set[str] = set()
    for _v, t in ranked:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
        if len(out) >= limit:
            break
    return out


def _sec_risk_scheduler_worker(interval_seconds: int = 21600) -> None:
    while True:
        try:
            if not _heavy_analysis_enabled():
                time.sleep(300)
                continue
            last = _sec_risk_sched_last_ts()
            now = time.time()
            if (now - last) >= interval_seconds:
                ts = _top_portfolio_tickers_for_sec(limit=3)
                if ts:
                    for t in ts:
                        try:
                            perform_sec_risk_diff(t)
                        except Exception:
                            pass
                    _sec_risk_sched_save_now(ts)
        except Exception:
            pass
        time.sleep(300)


def start_sec_risk_scheduler(interval_seconds: int = 21600) -> None:
    with LOCK:
        if bool(SEC_RISK_SCHED.get("started")):
            return
        if not _heavy_analysis_enabled():
            return
        enabled = os.getenv("ONYX_SEC_RISK_ON", "0").strip().lower() not in {"0", "false", "no", "off"}
        if not enabled:
            return
        SEC_RISK_SCHED["started"] = True
    th = threading.Thread(target=_sec_risk_scheduler_worker, args=(interval_seconds,), daemon=True)
    th.start()


def _fast_intel_feed_sched_state_path() -> Path:
    return DATA / "fast_intel_feed_scheduler_state.json"


def _fast_intel_feed_sched_last_ts() -> float:
    p = _fast_intel_feed_sched_state_path()
    if not p.exists():
        return 0.0
    try:
        obj = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        return float(obj.get("last_ts") or 0.0)
    except Exception:
        return 0.0


def _fast_intel_feed_sched_save_now(tickers: list[str]) -> None:
    p = _fast_intel_feed_sched_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    obj = {
        "last_ts": time.time(),
        "last_run": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tickers": [str(t).strip().upper() for t in tickers if str(t).strip()][:30],
    }
    p.write_text(json.dumps(obj, ensure_ascii=True, indent=2), encoding="utf-8")


def _latest_valuation_price(ticker: str) -> float | None:
    t = (ticker or "").strip().upper()
    if not t:
        return None
    conn = research_db()
    try:
        row = conn.execute(
            """SELECT price
               FROM valuation_snapshots
               WHERE ticker = ?
               ORDER BY as_of DESC, id DESC
               LIMIT 1""",
            (t,),
        ).fetchone()
        if not row:
            return None
        return _to_float_obj(row["price"])
    except Exception:
        return None
    finally:
        conn.close()


def _quote_with_fallback(ticker: str) -> tuple[float | None, float | None, str]:
    t = (ticker or "").strip().upper()
    if not t:
        return None, None, "none"

    q_live = (get_live_quotes([t], ttl_seconds=120).get(t) or {})
    price = _to_float_obj(q_live.get("price"))
    day_pct = _to_float_obj(q_live.get("day_pct"))
    if price is not None or day_pct is not None:
        return price, day_pct, "yahoo_live"

    with LOCK:
        q_stale = dict((QUOTE_CACHE.get("data") or {})).get(t, {}) if isinstance(QUOTE_CACHE.get("data"), dict) else {}
    price = _to_float_obj((q_stale or {}).get("price"))
    day_pct = _to_float_obj((q_stale or {}).get("day_pct"))
    if price is not None or day_pct is not None:
        return price, day_pct, "quote_cache"

    snap = load_intel24_snapshot_map(max_age_seconds=172800).get(t, {})
    day_pct = _to_float_obj((snap or {}).get("day_pct"))
    price = _latest_valuation_price(t)
    if price is not None or day_pct is not None:
        return price, day_pct, "snapshot_fallback"

    return None, None, "unavailable"


def _fast_intel_from_market_snapshot(ticker: str, use_ai: bool = True) -> dict[str, str]:
    t = (ticker or "").strip().upper()
    if not t:
        return {"headline": "", "summary": "", "detail": "", "source_key": ""}
    price, day_pct, quote_src = _quote_with_fallback(t)
    earn = _recent_reported_earnings_snapshot(t, max_days=21)
    earn_verdict = str((earn or {}).get("verdict") or "N/A")
    earn_date = str((earn or {}).get("date") or "N/A")
    earn_surprise = str((earn or {}).get("surprise_pct") or "N/A")

    time_slot = int(time.time() // 1800)
    basis = f"{t}|{dt.date.today().isoformat()}|{time_slot}|{price}|{day_pct}|{quote_src}|{earn_verdict}|{earn_date}|{earn_surprise}"
    source_key = hashlib.sha256(basis.encode("utf-8", errors="ignore")).hexdigest()[:18]
    cache_key = f"fast_intel:{source_key}"
    cached = _cache_get(cache_key, ttl_seconds=2100)
    if isinstance(cached, dict):
        return {
            "headline": str(cached.get("headline") or ""),
            "summary": str(cached.get("summary") or ""),
            "detail": str(cached.get("detail") or ""),
            "source_key": source_key,
        }

    if quote_src == "unavailable":
        base_head = _compact_signal_text(f"{t}: Quote source unavailable", max_chars=140)
        base_sum = _compact_signal_text(
            f"Live quote unavailable right now. Recent earnings: {earn_verdict} ({earn_date}, surprise {earn_surprise}).",
            max_chars=260,
        )
    else:
        base_head = _compact_signal_text(f"{t}: {fmt_money(price)} ({fmt_pct(day_pct)})", max_chars=140)
        base_sum = _compact_signal_text(
            f"Day move {fmt_pct(day_pct)} at {fmt_money(price)}. Recent earnings: {earn_verdict} ({earn_date}, surprise {earn_surprise}).",
            max_chars=260,
        )
    base_detail = (
        f"Ticker: {t}\n"
        f"Price: {fmt_money(price)}\n"
        f"Day change: {fmt_pct(day_pct)}\n"
        f"Quote source: {quote_src}\n"
        f"Recent earnings verdict: {earn_verdict}\n"
        f"Earnings date: {earn_date}\n"
        f"Earnings surprise: {earn_surprise}\n"
    ).strip()

    if (not use_ai) or (_HybridAIEngine is None and _hybrid_ask_ai is None):
        out = {"headline": base_head, "summary": base_sum, "detail": base_detail}
        _cache_put(cache_key, out)
        return {**out, "source_key": source_key}

    try:
        prompt = (
            f"Ticker: {t}\n"
            f"Price: {fmt_money(price)}\n"
            f"Day change: {fmt_pct(day_pct)}\n"
            f"Recent earnings verdict: {earn_verdict}\n"
            f"Earnings date: {earn_date}\n"
            f"Earnings surprise: {earn_surprise}\n"
        )
        system = (
            "You are a sell-side desk analyst. Return exactly 2 lines only:\n"
            "Line1 = short headline (max 90 chars).\n"
            "Line2 = one-sentence update with only concrete facts from input."
        )
        if _HybridAIEngine is not None:
            eng = _HybridAIEngine(provider_override="ollama", fallback_override="none")
            raw, _, _ = eng.ask_ai_with_meta(prompt, system, mode="fast", json_mode=False)
        else:
            raw = _hybrid_ask_ai(prompt, system, "fast", False)
        lines = [re.sub(r"\s+", " ", ln).strip(" -•\t") for ln in str(raw or "").splitlines() if ln.strip()]
        if lines:
            lines[0] = re.sub(r"^line\s*1\s*:\s*", "", lines[0], flags=re.IGNORECASE)
        if len(lines) > 1:
            lines[1] = re.sub(r"^line\s*2\s*:\s*", "", lines[1], flags=re.IGNORECASE)
        head = _compact_signal_text((lines[0] if lines else base_head), max_chars=140)
        summ = _compact_signal_text((lines[1] if len(lines) > 1 else base_sum), max_chars=260)
        out = {"headline": head, "summary": summ, "detail": base_detail}
        _cache_put(cache_key, out)
        return {**out, "source_key": source_key}
    except Exception:
        out = {"headline": base_head, "summary": base_sum, "detail": base_detail}
        _cache_put(cache_key, out)
        return {**out, "source_key": source_key}


def _fast_intel_feed_worker(interval_seconds: int = 1800) -> None:
    while True:
        try:
            last = _fast_intel_feed_sched_last_ts()
            now = time.time()
            if (now - last) >= interval_seconds:
                max_t = max(1, min(20, int(float(os.getenv("ONYX_FAST_FEED_MAX_TICKERS", "8").strip() or "8"))))
                ai_tickers = max(0, min(max_t, int(float(os.getenv("ONYX_FAST_FEED_AI_TICKERS", "1").strip() or "1"))))
                prefer_filing = os.getenv("ONYX_FAST_FEED_PREFER_FILING", "1").strip().lower() not in {"0", "false", "no", "off"}
                tickers = _feed_target_tickers(limit=max_t)
                for i, t in enumerate(tickers):
                    try:
                        if prefer_filing:
                            fl = _filing_lite_intel_from_cache(t)
                            if str(fl.get("ok") or "0") == "1":
                                insert_intel_feed(
                                    ticker=t,
                                    category="FILING_INTEL",
                                    title=str(fl.get("headline") or f"{t}: filing intel"),
                                    summary=str(fl.get("summary") or "MD&A + financial statement update."),
                                    detail=str(fl.get("detail") or "")[:2200],
                                    severity=30,
                                    source="md&a+financials(cache)",
                                    model="local_parser",
                                    unique_key=f"filinglite:{t}:{str(fl.get('source_key') or '')}",
                                )
                                continue
                        fi = _fast_intel_from_market_snapshot(t, use_ai=(i < ai_tickers))
                        insert_intel_feed(
                            ticker=t,
                            category="FAST_INTEL",
                            title=str(fi.get("headline") or f"{t}: market snapshot"),
                            summary=str(fi.get("summary") or "Quick market update."),
                            detail=str(fi.get("detail") or "")[:2000],
                            severity=20,
                            source="quotes+earnings_quick",
                            model=os.getenv("OLLAMA_FAST_MODEL", "llama3:latest").strip() or "llama3:latest",
                            unique_key=f"fastintel:{t}:{str(fi.get('source_key') or '')}",
                        )
                    except Exception:
                        pass
                _fast_intel_feed_sched_save_now(tickers)
        except Exception:
            pass
        time.sleep(120)


def start_fast_intel_feed_scheduler(interval_seconds: int = 1800) -> None:
    with LOCK:
        if bool(FAST_INTEL_FEED_SCHED.get("started")):
            return
        enabled = os.getenv("ONYX_FAST_FEED_ON", "1").strip().lower() not in {"0", "false", "no", "off"}
        if not enabled:
            return
        FAST_INTEL_FEED_SCHED["started"] = True
    th = threading.Thread(target=_fast_intel_feed_worker, args=(max(600, int(interval_seconds)),), daemon=True)
    th.start()


def _intel_feed_sched_state_path() -> Path:
    return DATA / "intel_feed_scheduler_state.json"


def _intel_feed_sched_last_ts() -> float:
    p = _intel_feed_sched_state_path()
    if not p.exists():
        return 0.0
    try:
        obj = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        return float(obj.get("last_ts") or 0.0)
    except Exception:
        return 0.0


def _intel_feed_sched_save_now(tickers: list[str]) -> None:
    p = _intel_feed_sched_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    obj = {
        "last_ts": time.time(),
        "last_run": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tickers": [str(t).strip().upper() for t in tickers if str(t).strip()][:30],
    }
    p.write_text(json.dumps(obj, ensure_ascii=True, indent=2), encoding="utf-8")


def _feed_target_tickers(limit: int = 10) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for r in read_portfolio_rows(DATA / "portfolio.csv"):
        t = (r[0] or "").strip().upper()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    for t in read_watchlist_tickers():
        tu = (t or "").strip().upper()
        if tu and tu not in seen:
            seen.add(tu)
            out.append(tu)
    lim = max(1, min(30, int(limit)))
    return out[:lim]


def _first_nonempty_line(text: str, max_chars: int = 220) -> str:
    for ln in str(text or "").splitlines():
        s = re.sub(r"\s+", " ", ln).strip()
        if not s:
            continue
        if len(s) > max_chars:
            return s[: max_chars - 1].rstrip() + "…"
        return s
    return ""


def _latest_filing_for_financial_intel(ticker: str) -> dict[str, str]:
    t = (ticker or "").strip().upper()
    if not t:
        return {"form": "", "date": "", "path": ""}
    conn = research_db()
    try:
        row = conn.execute(
            """SELECT form, date, path
               FROM filings
               WHERE ticker = ? AND form IN ('10-Q','10-K')
               ORDER BY date DESC
               LIMIT 1""",
            (t,),
        ).fetchone()
        if not row:
            return {"form": "", "date": "", "path": ""}
        return {
            "form": str(row["form"] or ""),
            "date": str(row["date"] or ""),
            "path": str(row["path"] or ""),
        }
    except Exception:
        return {"form": "", "date": "", "path": ""}
    finally:
        conn.close()


def _extract_mda_financial_focus(text: str, max_chars: int = 9000) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if not s:
        return ""
    parts: list[str] = []
    # 10-K/10-Q style MD&A anchors.
    anchors = [
        r"management[’'`s]{0,2}\s+discussion\s+and\s+analysis",
        r"\bitem\s+7\.\s*management[’'`s]{0,2}\s+discussion",
        r"\bitem\s+2\.\s*management[’'`s]{0,2}\s+discussion",
        r"results\s+of\s+operations",
        r"liquidity\s+and\s+capital\s+resources",
        r"consolidated\s+statements?\s+of\s+operations",
        r"consolidated\s+statements?\s+of\s+income",
        r"consolidated\s+balance\s+sheets?",
        r"consolidated\s+statements?\s+of\s+cash\s+flows?",
    ]
    for a in anchors:
        m = re.search(a, s, flags=re.IGNORECASE)
        if not m:
            continue
        st = max(0, m.start() - 120)
        ed = min(len(s), st + 1800)
        parts.append(s[st:ed])
        if sum(len(x) for x in parts) >= max_chars:
            break
    if not parts:
        # Fallback: pull dense financial sentences only.
        chunks = re.split(r"(?<=[\.\;\:])\s+", s)
        kw = (
            "revenue", "gross margin", "operating margin", "operating income", "net income",
            "eps", "cash flow", "free cash flow", "capex", "balance sheet", "liquidity",
            "debt", "working capital"
        )
        for c in chunks:
            low = c.lower()
            if any(k in low for k in kw):
                parts.append(c.strip())
            if sum(len(x) for x in parts) >= max_chars:
                break
    out = " ".join(parts).strip()
    if len(out) > max_chars:
        out = out[:max_chars]
    return out


def _row_val(row: object, key: str, default: str = "") -> str:
    if isinstance(row, dict):
        return str(row.get(key) or default)
    try:
        return str(row[key] or default)  # type: ignore[index]
    except Exception:
        return default


def _best_sentence_by_keywords(text: str, keywords: tuple[str, ...], max_chars: int = 280) -> str:
    chunks = re.split(r"(?<=[\.\;\:])\s+", re.sub(r"\s+", " ", str(text or "")).strip())
    for c in chunks:
        s = (c or "").strip()
        if len(s) < 40:
            continue
        low = s.lower()
        if any(k in low for k in keywords):
            return s[:max_chars].rstrip() + ("..." if len(s) > max_chars else "")
    return "Not clearly disclosed in current Item 7 extraction."


def _item7_mda_summary_card_html(ticker: str, filings: list[object]) -> str:
    t = resolve_ticker_input(ticker)
    if not t:
        return ""
    form = "-"
    fdate = "-"
    path_s = ""
    for r in filings:
        fm = _row_val(r, "form", "").upper().strip()
        if fm == "10-K":
            form = fm
            fdate = _row_val(r, "date", "-")
            path_s = _row_val(r, "path", "")
            break
    if not path_s and filings:
        form = _row_val(filings[0], "form", "-")
        fdate = _row_val(filings[0], "date", "-")
        path_s = _row_val(filings[0], "path", "")
    txt = _read_filing_text(path_s, max_chars=450000) if path_s else ""
    focus = _extract_mda_financial_focus(txt, max_chars=14000) if txt else ""
    rev = _best_sentence_by_keywords(focus, ("revenue", "volume", "pricing", "demand"))
    margin = _best_sentence_by_keywords(focus, ("margin", "gross", "operating income", "cost", "expense"))
    cash = _best_sentence_by_keywords(focus, ("cash flow", "operating cash", "free cash flow", "capex"))
    bs = _best_sentence_by_keywords(focus, ("balance sheet", "liquidity", "debt", "working capital"))
    status = "Ready" if focus else "Pending (No Item 7/MD&A text extracted yet; run Resync Company.)"
    return (
        "<section class='card c12'>"
        "<h2>Management Discussion (Item 7) Summary</h2>"
        f"<div class='muted'>Source filing: {html.escape(form)} ({html.escape(fdate)}) | Status: {html.escape(status)}</div>"
        "<div class='mda-grid'>"
        f"<div class='mda-box'><h3>Revenue Driver</h3><ul><li>{html.escape(rev)}</li></ul></div>"
        f"<div class='mda-box'><h3>Margin Story</h3><ul><li>{html.escape(margin)}</li></ul></div>"
        f"<div class='mda-box'><h3>Cash Flow / Capex</h3><ul><li>{html.escape(cash)}</li></ul></div>"
        f"<div class='mda-box'><h3>Balance Sheet / Liquidity</h3><ul><li>{html.escape(bs)}</li></ul></div>"
        "</div>"
        "</section>"
    )


def _filing_lite_intel_from_cache(ticker: str) -> dict[str, str]:
    t = (ticker or "").strip().upper()
    if not t:
        return {"ok": "0", "headline": "", "summary": "", "detail": "", "source_key": ""}
    filing = _latest_filing_for_financial_intel(t)
    form = str(filing.get("form") or "")
    date_s = str(filing.get("date") or "")
    path_s = str(filing.get("path") or "")
    if not path_s:
        return {"ok": "0", "headline": "", "summary": "", "detail": "", "source_key": ""}
    text = _read_filing_text(path_s, max_chars=220000)
    focus = _extract_mda_financial_focus(text, max_chars=12000)
    if not focus:
        return {"ok": "0", "headline": "", "summary": "", "detail": "", "source_key": ""}
    rev = _best_sentence_by_keywords(focus, ("revenue", "volume", "pricing", "demand"))
    margin = _best_sentence_by_keywords(focus, ("margin", "gross", "operating income", "cost", "expense"))
    cfs = _best_sentence_by_keywords(focus, ("cash flow", "operating cash", "free cash flow", "capex"))
    bal = _best_sentence_by_keywords(focus, ("balance sheet", "liquidity", "debt", "working capital"))
    guidance = _best_sentence_by_keywords(focus, ("guidance", "outlook", "expect", "forecast"))
    inc = _best_sentence_by_keywords(focus, ("income", "earnings", "eps", "profit"))
    headline = _compact_signal_text(f"{t}: MD&A + Financials update", max_chars=140)
    summary = _compact_signal_text(
        " | ".join(x for x in [rev, margin, guidance, inc, cfs, bal] if x) or "MD&A/financial sections available from latest filing.",
        max_chars=260,
    )
    detail = (
        f"Revenue/MD&A: {rev or '-'}\n"
        f"Margin: {margin or '-'}\n"
        f"Guidance: {guidance or '-'}\n"
        f"Income statement: {inc or '-'}\n"
        f"Cash flow: {cfs or '-'}\n"
        f"Balance sheet: {bal or '-'}\n"
    )
    src = f"{form}:{date_s}:{Path(path_s).name}"
    return {"ok": "1", "headline": headline, "summary": summary, "detail": detail, "source_key": f"{t}:filinglite:{src}"}


def _extract_json_object(raw: str) -> dict[str, object] | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    m = re.search(r"\{.*\}", s, flags=re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        if isinstance(obj, dict):
            return obj
    except Exception:
        return None
    return None


def _financial_intel_from_latest_filing(ticker: str) -> dict[str, str]:
    t = (ticker or "").strip().upper()
    if not t:
        return {"ok": "0", "headline": "", "summary": "", "detail": "", "source_key": ""}
    filing = _latest_filing_for_financial_intel(t)
    form = str(filing.get("form") or "")
    date_s = str(filing.get("date") or "")
    path_s = str(filing.get("path") or "")
    if not path_s:
        return {
            "ok": "0",
            "headline": f"{t}: No recent 8-K/10-Q/10-K found",
            "summary": "Filing database has no recent periodic filing for this ticker.",
            "detail": "",
            "source_key": f"{t}:nofiling",
        }

    text = _read_filing_text(path_s, max_chars=120000)
    if not text:
        return {
            "ok": "0",
            "headline": f"{t}: Filing text unavailable",
            "summary": f"Latest filing ({form} {date_s}) could not be read.",
            "detail": "",
            "source_key": f"{t}:{form}:{date_s}:unreadable",
        }

    earn = _recent_reported_earnings_snapshot(t, max_days=21)
    earn_ctx = (
        f"Recent earnings snapshot: verdict={earn.get('verdict')} | "
        f"actual={earn.get('actual')} | est={earn.get('est')} | "
        f"surprise={earn.get('surprise_pct')} | date={earn.get('date')}"
        if earn
        else "Recent earnings snapshot: unavailable"
    )
    focused_text = _extract_mda_financial_focus(text, max_chars=9000) or text[:9000]
    raw_input = f"Ticker: {t}\nForm: {form}\nFiled: {date_s}\n{earn_ctx}\n\n{focused_text}"
    prompt = FINANCIAL_INTEL_PROMPT.replace("{TICKER}", t).replace("{raw_filing_text}", raw_input)
    source_key = f"{t}:{form}:{date_s}:{hashlib.sha256(raw_input.encode('utf-8', errors='ignore')).hexdigest()[:16]}"

    if _hybrid_ask_ai is None:
        headline = _first_nonempty_line(text, max_chars=120) or f"{t}: Filing updated"
        return {
            "ok": "0",
            "headline": headline,
            "summary": f"Latest filing {form} ({date_s}) captured. AI parser unavailable.",
            "detail": text[:1500],
            "source_key": source_key,
        }

    cache_key = f"financial_intel:{source_key}"
    cached = _cache_get(cache_key, ttl_seconds=21600)
    if isinstance(cached, dict):
        return {
            "ok": "1",
            "headline": str(cached.get("headline") or ""),
            "summary": str(cached.get("summary") or ""),
            "detail": str(cached.get("detail") or ""),
            "source_key": source_key,
        }

    try:
        raw = _hybrid_ask_ai(
            prompt,
            "Return strict JSON only, matching requested keys. No markdown.",
            "deep",
            False,
        )
        obj = _extract_json_object(str(raw or ""))
        if isinstance(obj, dict):
            headline = _compact_signal_text(str(obj.get("headline") or f"{t}: Filing intelligence"), max_chars=140)
            perf = str(obj.get("performance_summary") or "").strip()
            margin = str(obj.get("margin_story") or "").strip()
            tone = str(obj.get("guidance_tone") or "").strip()
            quote = str(obj.get("key_quote") or "").strip()
            summary = _compact_signal_text(
                " | ".join(x for x in [perf, margin, f"Guidance: {tone}" if tone else ""] if x),
                max_chars=260,
            )
            detail = (
                f"Form: {form} ({date_s})\n"
                f"Performance: {perf}\n"
                f"Margin story: {margin}\n"
                f"Guidance tone: {tone}\n"
                f"Key quote: {quote}\n"
            ).strip()
            packed = {"headline": headline, "summary": summary, "detail": detail}
            _cache_put(cache_key, packed)
            return {"ok": "1", "headline": headline, "summary": summary, "detail": detail, "source_key": source_key}
    except Exception:
        pass

    fallback_head = _first_nonempty_line(text, max_chars=120) or f"{t}: Filing intelligence updated"
    fallback_sum = _compact_signal_text(f"Latest filing {form} ({date_s}) processed; AI JSON parse fallback used.", max_chars=260)
    return {"ok": "0", "headline": fallback_head, "summary": fallback_sum, "detail": text[:1800], "source_key": source_key}


def _intel_feed_worker(interval_seconds: int = 14400) -> None:
    while True:
        try:
            last = _intel_feed_sched_last_ts()
            now = time.time()
            if (now - last) >= interval_seconds:
                max_t = max(1, min(30, int(float(os.getenv("ONYX_FEED_MAX_TICKERS", "10").strip() or "10"))))
                include_deep = _heavy_analysis_enabled() and (
                    os.getenv("ONYX_FEED_INCLUDE_DEEP_DIVE", "0").strip().lower() in {"1", "true", "yes", "on"}
                )
                tickers = _feed_target_tickers(limit=max_t)
                for t in tickers:
                    try:
                        fin = _financial_intel_from_latest_filing(t)
                        headline = _compact_signal_text(str(fin.get("headline") or f"{t}: Filing intelligence updated"), max_chars=140)
                        summary = _compact_signal_text(str(fin.get("summary") or "Latest filing and recent earnings context analyzed."), max_chars=260)
                        detail = str(fin.get("detail") or "")[:3000]
                        src_key = str(fin.get("source_key") or f"{t}:none")
                        insert_intel_feed(
                            ticker=t,
                            category="FINANCIAL_INTEL",
                            title=headline,
                            summary=summary,
                            detail=detail,
                            severity=35,
                            source="sec_filing+earnings",
                            model="gemma3:27b",
                            unique_key=f"finintel:{src_key}",
                        )
                    except Exception:
                        pass
                    if include_deep:
                        try:
                            deep = perform_deep_dive(t)
                            if int(deep.get("ok") or 0) == 1:
                                analysis = str(deep.get("analysis") or "")
                                title = _first_nonempty_line(analysis, max_chars=140) or f"{t}: Deep dive refreshed"
                                summary = _first_nonempty_line("\n".join(analysis.splitlines()[1:]), max_chars=260) or "News-driven deep dive updated."
                                model_used = str(deep.get("model") or "gemma3:27b")
                                day_key = dt.date.today().strftime("%Y-%m-%d")
                                insert_intel_feed(
                                    ticker=t,
                                    category="DEEP_DIVE",
                                    title=title,
                                    summary=summary,
                                    detail=analysis[:3000],
                                    severity=40,
                                    source="news_pack",
                                    model=model_used,
                                    unique_key=f"deep:{t}:{day_key}",
                                )
                        except Exception:
                            pass
                _intel_feed_sched_save_now(tickers)
        except Exception:
            pass
        time.sleep(300)


def start_intel_feed_scheduler(interval_seconds: int = 14400) -> None:
    with LOCK:
        if bool(INTEL_FEED_SCHED.get("started")):
            return
        enabled = os.getenv("ONYX_INTEL_FEED_ON", "1").strip().lower() not in {"0", "false", "no", "off"}
        if not enabled:
            return
        INTEL_FEED_SCHED["started"] = True
    th = threading.Thread(target=_intel_feed_worker, args=(interval_seconds,), daemon=True)
    th.start()


def _intel24_sched_state_path() -> Path:
    return DATA / "intel24_scheduler_state.json"


def _intel24_sched_last_ts() -> float:
    p = _intel24_sched_state_path()
    if not p.exists():
        return 0.0
    try:
        obj = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        return float(obj.get("last_ts") or 0.0)
    except Exception:
        return 0.0


def _intel24_sched_save_now(tickers: list[str], rows_n: int) -> None:
    p = _intel24_sched_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    obj = {
        "last_ts": time.time(),
        "last_run": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tickers": [str(t).strip().upper() for t in tickers if str(t).strip()][:50],
        "rows": int(rows_n),
    }
    p.write_text(json.dumps(obj, ensure_ascii=True, indent=2), encoding="utf-8")


def _intel24_snapshot_worker(interval_seconds: int = 900) -> None:
    while True:
        try:
            last = _intel24_sched_last_ts()
            now = time.time()
            if (now - last) >= interval_seconds:
                tickers = _feed_target_tickers(limit=max(4, min(50, int(float(os.getenv("ONYX_INTEL24_MAX_TICKERS", "30").strip() or "30")))))
                if tickers:
                    quotes = get_live_quotes(tickers, ttl_seconds=120)
                    rows = _build_last24_rows_for_tickers(tickers, quotes)
                    if rows:
                        save_intel24_snapshot(rows, source="scheduler")
                    _intel24_sched_save_now(tickers, len(rows))
        except Exception:
            pass
        time.sleep(120)


def start_intel24_snapshot_scheduler(interval_seconds: int = 900) -> None:
    with LOCK:
        if bool(INTEL24_SNAP_SCHED.get("started")):
            return
        enabled = os.getenv("ONYX_INTEL24_ON", "1").strip().lower() not in {"0", "false", "no", "off"}
        if not enabled:
            return
        INTEL24_SNAP_SCHED["started"] = True
    th = threading.Thread(target=_intel24_snapshot_worker, args=(max(300, int(interval_seconds)),), daemon=True)
    th.start()


def _workspace_snapshot_sched_state_path() -> Path:
    return DATA / "workspace_snapshot_scheduler_state.json"


def _workspace_snapshot_sched_last_ts() -> float:
    p = _workspace_snapshot_sched_state_path()
    if not p.exists():
        return 0.0
    try:
        obj = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        return float(obj.get("last_ts") or 0.0)
    except Exception:
        return 0.0


def _workspace_snapshot_sched_save(month: str, saved: int, tickers: list[str]) -> None:
    p = _workspace_snapshot_sched_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    obj = {
        "last_ts": time.time(),
        "last_run": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "month": str(month or ""),
        "saved_count": int(saved),
        "tickers": [str(t).strip().upper() for t in tickers if str(t).strip()][:60],
    }
    p.write_text(json.dumps(obj, ensure_ascii=True, indent=2), encoding="utf-8")


def _workspace_snapshot_target_tickers(limit: int = 80) -> list[str]:
    rows = list_workspace_companies(limit=max(10, min(2000, int(limit))))
    out: list[str] = []
    seen: set[str] = set()
    for r in rows:
        t = str(r["ticker"] or "").strip().upper()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out[: max(5, min(300, int(limit)))]


def _workspace_snapshot_scheduler_worker(interval_seconds: int = 21600) -> None:
    while True:
        try:
            last = _workspace_snapshot_sched_last_ts()
            now = time.time()
            if (now - last) >= interval_seconds:
                month = dt.date.today().strftime("%Y-%m")
                tickers = _workspace_snapshot_target_tickers(limit=max(10, int(float(os.getenv("ONYX_WORKSPACE_SNAPSHOT_MAX_TICKERS", "80").strip() or "80"))))
                saved = 0
                for t in tickers:
                    try:
                        if save_workspace_thesis_snapshot_if_missing(ticker=t, snapshot_month=month):
                            saved += 1
                    except Exception:
                        pass
                _workspace_snapshot_sched_save(month=month, saved=saved, tickers=tickers)
        except Exception:
            pass
        time.sleep(300)


def start_workspace_snapshot_scheduler(interval_seconds: int = 21600) -> None:
    with LOCK:
        if bool(WORKSPACE_SNAPSHOT_SCHED.get("started")):
            return
        enabled = os.getenv("ONYX_WORKSPACE_SNAPSHOT_ON", "1").strip().lower() not in {"0", "false", "no", "off"}
        if not enabled:
            return
        WORKSPACE_SNAPSHOT_SCHED["started"] = True
    th = threading.Thread(target=_workspace_snapshot_scheduler_worker, args=(max(3600, int(interval_seconds)),), daemon=True)
    th.start()


def _run_fast_intel_feed_once() -> None:
    max_t = max(1, min(20, int(float(os.getenv("ONYX_FAST_FEED_MAX_TICKERS", "8").strip() or "8"))))
    ai_tickers = max(0, min(max_t, int(float(os.getenv("ONYX_FAST_FEED_AI_TICKERS", "1").strip() or "1"))))
    tickers = _feed_target_tickers(limit=max_t)
    prefer_filing = os.getenv("ONYX_FAST_FEED_PREFER_FILING", "1").strip().lower() not in {"0", "false", "no", "off"}
    for i, t in enumerate(tickers):
        try:
            if prefer_filing:
                fl = _filing_lite_intel_from_cache(t)
                if str(fl.get("ok") or "0") == "1":
                    insert_intel_feed(
                        ticker=t,
                        category="FILING_INTEL",
                        title=str(fl.get("headline") or f"{t}: filing intel"),
                        summary=str(fl.get("summary") or "MD&A + financial statement update."),
                        detail=str(fl.get("detail") or "")[:2200],
                        severity=30,
                        source="md&a+financials(cache)",
                        model="local_parser",
                        unique_key=f"filinglite:{t}:{str(fl.get('source_key') or '')}",
                    )
                    continue
            fi = _fast_intel_from_market_snapshot(t, use_ai=(i < ai_tickers))
            insert_intel_feed(
                ticker=t,
                category="FAST_INTEL",
                title=str(fi.get("headline") or f"{t}: market snapshot"),
                summary=str(fi.get("summary") or "Quick market update."),
                detail=str(fi.get("detail") or "")[:2000],
                severity=20,
                source="quotes+earnings_quick",
                model=os.getenv("OLLAMA_FAST_MODEL", "llama3:latest").strip() or "llama3:latest",
                unique_key=f"fastintel:{t}:{str(fi.get('source_key') or '')}",
            )
        except Exception:
            pass
    _fast_intel_feed_sched_save_now(tickers)


def _run_financial_intel_feed_once() -> None:
    max_t = max(1, min(30, int(float(os.getenv("ONYX_FEED_MAX_TICKERS", "10").strip() or "10"))))
    include_deep = _heavy_analysis_enabled() and (
        os.getenv("ONYX_FEED_INCLUDE_DEEP_DIVE", "0").strip().lower() in {"1", "true", "yes", "on"}
    )
    tickers = _feed_target_tickers(limit=max_t)
    for t in tickers:
        try:
            fin = _financial_intel_from_latest_filing(t)
            headline = _compact_signal_text(str(fin.get("headline") or f"{t}: Filing intelligence updated"), max_chars=140)
            summary = _compact_signal_text(str(fin.get("summary") or "Latest filing and recent earnings context analyzed."), max_chars=260)
            detail = str(fin.get("detail") or "")[:3000]
            src_key = str(fin.get("source_key") or f"{t}:none")
            insert_intel_feed(
                ticker=t,
                category="FINANCIAL_INTEL",
                title=headline,
                summary=summary,
                detail=detail,
                severity=35,
                source="sec_filing+earnings",
                model="gemma3:27b",
                unique_key=f"finintel:{src_key}",
            )
        except Exception:
            pass
        if include_deep:
            try:
                deep = perform_deep_dive(t)
                if int(deep.get("ok") or 0) == 1:
                    analysis = str(deep.get("analysis") or "")
                    title = _first_nonempty_line(analysis, max_chars=140) or f"{t}: Deep dive refreshed"
                    summary = _first_nonempty_line("\n".join(analysis.splitlines()[1:]), max_chars=260) or "News-driven deep dive updated."
                    model_used = str(deep.get("model") or "gemma3:27b")
                    day_key = dt.date.today().strftime("%Y-%m-%d")
                    insert_intel_feed(
                        ticker=t,
                        category="DEEP_DIVE",
                        title=title,
                        summary=summary,
                        detail=analysis[:3000],
                        severity=40,
                        source="news_pack",
                        model=model_used,
                        unique_key=f"deep:{t}:{day_key}",
                    )
            except Exception:
                pass
    _intel_feed_sched_save_now(tickers)


def _run_intel24_snapshot_once() -> None:
    tickers = _feed_target_tickers(limit=max(4, min(50, int(float(os.getenv("ONYX_INTEL24_MAX_TICKERS", "30").strip() or "30")))))
    if not tickers:
        return
    quotes = get_live_quotes(tickers, ttl_seconds=120)
    rows = _build_last24_rows_for_tickers(tickers, quotes)
    if rows:
        save_intel24_snapshot(rows, source="scheduler")
    _intel24_sched_save_now(tickers, len(rows))


def _run_workspace_snapshot_once() -> None:
    month = dt.date.today().strftime("%Y-%m")
    tickers = _workspace_snapshot_target_tickers(limit=max(10, int(float(os.getenv("ONYX_WORKSPACE_SNAPSHOT_MAX_TICKERS", "80").strip() or "80"))))
    saved = 0
    for t in tickers:
        try:
            if save_workspace_thesis_snapshot_if_missing(ticker=t, snapshot_month=month):
                saved += 1
        except Exception:
            pass
    _workspace_snapshot_sched_save(month=month, saved=saved, tickers=tickers)


def _unified_scheduler_worker(loop_sleep_seconds: int = 60) -> None:
    while True:
        try:
            now = time.time()
            onyx_interval = max(1800, int(float(os.getenv("ONYX_THESIS_INTERVAL", "21600").strip() or "21600")))
            l2_interval = max(3600, int(float(os.getenv("ONYX_L2_INTERVAL", "21600").strip() or "21600")))
            sec_interval = max(3600, int(float(os.getenv("ONYX_SEC_RISK_INTERVAL", "21600").strip() or "21600")))
            fast_interval = max(600, int(float(os.getenv("ONYX_FAST_FEED_INTERVAL", "1800").strip() or "1800")))
            fin_interval = max(1800, int(float(os.getenv("ONYX_INTEL_FEED_INTERVAL", "14400").strip() or "14400")))
            intel24_interval = max(300, int(float(os.getenv("ONYX_INTEL24_INTERVAL", "900").strip() or "900")))
            ws_interval = max(3600, int(float(os.getenv("ONYX_WORKSPACE_SNAPSHOT_INTERVAL", "21600").strip() or "21600")))
            profile_interval = max(600, int(float(os.getenv("ONYX_COMPANY_PROFILE_ENRICH_INTERVAL", "1800").strip() or "1800")))

            if _onyx_run_thesis_check_all_active is not None:
                try:
                    _onyx_run_thesis_check_all_active(min_interval_seconds=onyx_interval)
                except Exception:
                    pass

            if (now - _l2_sched_last_ts()) >= l2_interval:
                try:
                    out = generate_l2_digest_report(force_recompute=True)
                    _l2_sched_save_now(out)
                except Exception:
                    pass

            sec_on = _heavy_analysis_enabled() and (
                os.getenv("ONYX_SEC_RISK_ON", "0").strip().lower() not in {"0", "false", "no", "off"}
            )
            if sec_on and (now - _sec_risk_sched_last_ts()) >= sec_interval:
                try:
                    ts = _top_portfolio_tickers_for_sec(limit=3)
                    if ts:
                        for t in ts:
                            try:
                                perform_sec_risk_diff(t)
                            except Exception:
                                pass
                        _sec_risk_sched_save_now(ts)
                except Exception:
                    pass

            fast_on = os.getenv("ONYX_FAST_FEED_ON", "1").strip().lower() not in {"0", "false", "no", "off"}
            if fast_on and (now - _fast_intel_feed_sched_last_ts()) >= fast_interval:
                _run_fast_intel_feed_once()

            fin_on = os.getenv("ONYX_INTEL_FEED_ON", "1").strip().lower() not in {"0", "false", "no", "off"}
            if fin_on and (now - _intel_feed_sched_last_ts()) >= fin_interval:
                _run_financial_intel_feed_once()

            intel24_on = os.getenv("ONYX_INTEL24_ON", "1").strip().lower() not in {"0", "false", "no", "off"}
            if intel24_on and (now - _intel24_sched_last_ts()) >= intel24_interval:
                _run_intel24_snapshot_once()

            ws_on = os.getenv("ONYX_WORKSPACE_SNAPSHOT_ON", "1").strip().lower() not in {"0", "false", "no", "off"}
            if ws_on and (now - _workspace_snapshot_sched_last_ts()) >= ws_interval:
                _run_workspace_snapshot_once()

            profile_on = os.getenv("ONYX_COMPANY_PROFILE_ENRICH_ON", "1").strip().lower() not in {"0", "false", "no", "off"}
            if profile_on and (now - _company_profile_enrich_last_ts()) >= profile_interval:
                _run_company_profile_enrich_once()
        except Exception:
            pass
        time.sleep(max(20, int(loop_sleep_seconds)))


def start_unified_scheduler() -> None:
    with LOCK:
        if bool(ONYX_UNIFIED_SCHED.get("started")):
            return
        ONYX_UNIFIED_SCHED["started"] = True
    th = threading.Thread(target=_unified_scheduler_worker, args=(60,), daemon=True)
    th.start()


def _startup_intel_prewarm_worker() -> None:
    # Precompute high-quality intelligence so first dashboard open is warm.
    try:
        time.sleep(1.5)
        daily = latest("reports/terminal_daily_brief_*.md")
        red_flag = latest("reports/.terminal_inputs/red_flag_alert_*.txt")
        earnings = _latest_earnings_file()

        portfolio_rows = read_portfolio_rows(DATA / "portfolio.csv")
        portfolio = []
        for r in portfolio_rows:
            t = (r[0] or "").strip().upper()
            if t:
                portfolio.append(t)
        portfolio = sorted(set(portfolio))
        watchlist = read_watchlist_tickers()
        holdings_universe = {t.upper() for t in (portfolio + watchlist) if t}

        # 1) Warm dashboard PM blocks.
        try:
            briefing = _extract_briefing_lines(daily, limit=7)
            if briefing:
                _ = _daily_brief_ai_summary(daily, briefing)
        except Exception:
            pass
        try:
            if portfolio:
                pquotes = get_live_quotes(portfolio)
                pintel = get_portfolio_intel(portfolio)
                risk_rows = generate_risk_cards(portfolio, portfolio_rows, pquotes, pintel)
                alerts = run_cmd(["python3", "tools/red_flag_rank.py", "--file", red_flag, "--limit", "120"]) if red_flag else []
                pset = {t.upper() for t in portfolio}
                sec_rows = [a for a in alerts if extract_ticker_from_signal(a) in pset][:3]
                for sr in sec_rows:
                    if sr not in risk_rows:
                        risk_rows.append(sr)
                if risk_rows:
                    risk_key = f"{_file_sig(red_flag)}:{'|'.join(sorted(pset))}:{len(risk_rows)}"
                    _ = _risk_cards_ai_summary([str(r) for r in risk_rows], risk_key)
        except Exception:
            pass
        try:
            week_events = run_cmd(["python3", "tools/earnings_watch_rank.py", "--file", earnings, "--limit", "120", "--scope", "week"]) if earnings else []
            earn_cards = get_earnings_cards(
                week_events[:60],
                max_cards=12,
                focus_universe=holdings_universe,
                include_all=False,
            )
            if earn_cards:
                earn_source_rows = []
                for c in earn_cards:
                    earn_source_rows.append(
                        " | ".join(
                            [
                                str(c.get("ticker", "-")),
                                str(c.get("when", "-")),
                                str(c.get("label", "-")),
                                str(c.get("headline", "-")),
                                str(c.get("sub", "-")),
                                str(c.get("mcap", "-")),
                            ]
                        )
                    )
                earn_key = f"{_file_sig(earnings)}:{len(week_events[:60])}:{'|'.join(str(c.get('ticker','')) for c in earn_cards)}"
                _ = _earnings_cards_ai_summary(earn_source_rows, earn_key)
        except Exception:
            pass
        try:
            _ = get_l2_snapshot("portfolio", ttl_seconds=900)
        except Exception:
            pass

        # 2) Warm heavy company intelligence for portfolio+watchlist.
        ordered: list[str] = []
        seen: set[str] = set()
        for t in portfolio + watchlist:
            u = str(t or "").strip().upper()
            if not u or u in seen:
                continue
            seen.add(u)
            ordered.append(u)
        max_t = max(1, int(float(os.getenv("ONYX_PREWARM_TICKERS", "8").strip() or "8")))
        if _heavy_analysis_enabled():
            for t in ordered[:max_t]:
                try:
                    perform_deep_dive(t)
                except Exception:
                    pass
                try:
                    perform_sec_risk_diff(t)
                except Exception:
                    pass
                try:
                    perform_mda_diff(t)
                except Exception:
                    pass
    except Exception:
        return


def start_startup_intel_prewarm() -> None:
    with LOCK:
        if bool(INTEL_PREWARM_SCHED.get("started")):
            return
        enabled = os.getenv("ONYX_PREWARM_ON_START", "1").strip().lower() not in {"0", "false", "no", "off"}
        if not enabled:
            return
        INTEL_PREWARM_SCHED["started"] = True
    th = threading.Thread(target=_startup_intel_prewarm_worker, daemon=True)
    th.start()


def read_portfolio_rows(path: Path) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    for ln in read_lines(path):
        parts = [p.strip() for p in ln.split(",", 3)]
        while len(parts) < 4:
            parts.append("")
        rows.append((parts[0], parts[1], parts[2], parts[3]))
    return rows


def write_portfolio_rows(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = []
    for (t, s, c, n) in rows:
        tu = (t or "").strip().upper()
        if not tu:
            continue
        out.append(f"{tu},{(s or '').strip()},{(c or '').strip()},{(n or '').strip()}")
    path.write_text(("\n".join(out) + "\n") if out else "", encoding="utf-8")


def write_watchlist_entries(path: Path, entries: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    comments: list[str] = []
    if path.exists():
        for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = ln.strip()
            if s.startswith("#"):
                comments.append(s)
    if not comments:
        comments = ["# TICKER,ADDED_AT,ADDED_PRICE"]
    lines = comments[:]
    seen: set[str] = set()
    for e in entries:
        t = (e.get("ticker") or "").strip().upper()
        if not t or t in seen:
            continue
        seen.add(t)
        added_at = (e.get("added_at") or "").strip()
        added_price = (e.get("added_price") or "").strip()
        lines.append(f"{t},{added_at},{added_price}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def quick_ticker_links(tickers: list[str]) -> str:
    if not tickers:
        return "<span class='muted'>none</span>"
    unique = []
    seen: set[str] = set()
    for t in tickers:
        tu = t.upper().strip()
        if tu and tu not in seen:
            seen.add(tu)
            unique.append(tu)
    return " ".join(f"<a class='btn' href='/company?t={html.escape(t)}'>{html.escape(t)}</a>" for t in unique[:20])


def _to_num(v: object) -> float | None:
    try:
        if v is None:
            return None
        s = str(v).strip().replace("$", "").replace(",", "")
        if not s:
            return None
        return float(s)
    except Exception:
        return None


def _to_int(v: object, default: int = 0) -> int:
    n = _to_num(v)
    if n is None:
        return default
    try:
        return int(n)
    except Exception:
        return default


def _fmt_big(v: object) -> str:
    n = _to_num(v)
    if n is None:
        return "-"
    a = abs(n)
    if a >= 1_000_000_000_000:
        return f"${n / 1_000_000_000_000:.2f}T"
    if a >= 1_000_000_000:
        return f"${n / 1_000_000_000:.2f}B"
    if a >= 1_000_000:
        return f"${n / 1_000_000:.2f}M"
    return f"${n:,.0f}"


def _sparkline(vals: list[float]) -> str:
    if not vals:
        return "-"
    ticks = "._-:=+*#%@"
    lo = min(vals)
    hi = max(vals)
    if hi <= lo:
        return ticks[len(ticks) // 2] * min(len(vals), 40)
    out: list[str] = []
    sample = vals[-40:]
    for x in sample:
        idx = int((x - lo) / (hi - lo) * (len(ticks) - 1))
        idx = max(0, min(idx, len(ticks) - 1))
        out.append(ticks[idx])
    return "".join(out)


def _cache_get(key: str, ttl_seconds: int = 300) -> object | None:
    _ensure_db_split_ready()
    now = time.time()
    with LOCK:
        row = ONYX_CACHE.get(key)
        if not row:
            row = None
        if row:
            ts = float(row.get("ts", 0.0))
            if now - ts <= ttl_seconds:
                return row.get("data")
    try:
        conn = sqlite3.connect(str(CACHE_DB_PATH))
        db_row = conn.execute("SELECT ts, payload FROM cache_entries WHERE key = ?", (str(key),)).fetchone()
        conn.close()
        if not db_row:
            return None
        ts = float(db_row[0] or 0.0)
        if now - ts > ttl_seconds:
            return None
        data = json.loads(str(db_row[1] or "null"))
        with LOCK:
            ONYX_CACHE[str(key)] = {"ts": ts, "data": data}
        return data
    except Exception:
        return None


def _cache_put(key: str, data: object) -> None:
    _ensure_db_split_ready()
    ts_now = time.time()
    with LOCK:
        ONYX_CACHE[key] = {"ts": ts_now, "data": data}
    try:
        payload = json.dumps(data, ensure_ascii=True, default=str)
        conn = sqlite3.connect(str(CACHE_DB_PATH))
        conn.execute(
            "INSERT OR REPLACE INTO cache_entries (key, ts, payload) VALUES (?, ?, ?)",
            (str(key), float(ts_now), payload),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _extract_briefing_lines(path: str, limit: int = 6) -> list[str]:
    if not path:
        return ["No daily brief generated yet. Run Daily refresh."]
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return ["Daily brief file could not be read."]
    out: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        low = s.lower()
        if s.startswith("#") or s.startswith("---"):
            continue
        if "source:" in low or "sources:" in low or "source mode:" in low:
            continue
        if s.startswith("- "):
            s = s[2:].strip()
        # Keep dashboard rendering fast and scannable.
        if len(s) > 260:
            s = s[:257].rstrip() + "..."
        out.append(s)
        if len(out) >= limit:
            break
    return out or ["Daily brief is available, but no summary lines were detected."]


def _extract_report_lines(path: str, kind: str, limit: int = 6) -> list[str]:
    labels = {
        "daily": "Daily Brief",
        "appendix": "Appendix",
        "weekly": "Weekly Outlook",
        "monthly": "Monthly IC Memo",
        "quarterly": "Quarterly Report",
    }
    k = (kind or "daily").strip().lower()
    if not path:
        return [f"No {labels.get(k, 'report')} generated yet."]
    lines = _extract_briefing_lines(path, limit=limit)
    if lines and lines[0].startswith("No daily brief"):
        return [f"No {labels.get(k, 'report')} generated yet."]
    if lines and lines[0].startswith("Daily brief file could not be read"):
        return [f"{labels.get(k, 'Report')} file could not be read."]
    return lines


def _read_filing_text(path_s: str, max_chars: int = 800000) -> str:
    if not path_s:
        return ""
    p = Path(path_s)
    if not p.is_absolute():
        p = ROOT / path_s
    if not p.exists() or not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="ignore")[:max_chars]
    except Exception:
        return ""


def _parse_shares_outstanding(text: str) -> float | None:
    if not text:
        return None
    low = text.lower()
    patterns = [
        r"(\d[\d,]{5,})\s+shares\s+(?:of\s+common\s+stock\s+)?outstanding",
        r"shares\s+outstanding[^0-9]{0,60}(\d[\d,]{5,})",
    ]
    for pat in patterns:
        m = re.search(pat, low, flags=re.IGNORECASE)
        if not m:
            continue
        n = _to_num(m.group(1).replace(",", ""))
        if n is not None and n > 0:
            return n
    return None


def _sec_snapshot(ticker: str) -> dict[str, object]:
    ck = f"secsnap:{ticker}"
    cached = _cache_get(ck, ttl_seconds=900)
    if isinstance(cached, dict):
        return cached
    out: dict[str, object] = {
        "latest_form": "-",
        "latest_date": "-",
        "latest_path": "",
        "prev_form": "-",
        "prev_date": "-",
        "shares_latest": None,
        "shares_prev": None,
        "dilution_pct": None,
    }
    conn = research_db()
    try:
        rows = conn.execute(
            "SELECT form, date, path FROM filings WHERE ticker = ? AND form IN ('10-K','10-Q') ORDER BY date DESC LIMIT 2",
            (ticker,),
        ).fetchall()
        if rows:
            r0 = rows[0]
            out["latest_form"] = str(r0["form"] or "-")
            out["latest_date"] = str(r0["date"] or "-")
            out["latest_path"] = str(r0["path"] or "")
            txt0 = _read_filing_text(str(r0["path"] or ""))
            out["shares_latest"] = _parse_shares_outstanding(txt0)
        if len(rows) > 1:
            r1 = rows[1]
            out["prev_form"] = str(r1["form"] or "-")
            out["prev_date"] = str(r1["date"] or "-")
            txt1 = _read_filing_text(str(r1["path"] or ""))
            out["shares_prev"] = _parse_shares_outstanding(txt1)
        s0 = _to_num(out.get("shares_latest"))
        s1 = _to_num(out.get("shares_prev"))
        if s0 is not None and s1 not in (None, 0.0):
            out["dilution_pct"] = (s0 - s1) / s1 * 100.0
    finally:
        conn.close()
    _cache_put(ck, out)
    return out


def _sec_insider_signal(
    ticker: str,
    months: int = 18,
    meaningful_usd: float = 100000.0,
    include_directors: bool = True,
) -> dict[str, object]:
    ck = f"secinsider:{ticker}"
    cached = _cache_get(ck, ttl_seconds=900)
    if isinstance(cached, dict):
        return cached

    out: dict[str, object] = {
        "found": False,
        "buy_v": 0.0,
        "sell_v": 0.0,
        "buy_n": 0,
        "sell_n": 0,
        "buy_mean_n": 0,
        "sell_mean_n": 0,
        "events": 0,
        "note": f"No recent insider Form 4 open-market trades detected in last {months} months.",
    }
    details = _sec_insider_details(ticker, months=months, meaningful_usd=meaningful_usd)
    events = [e for e in details.get("events", []) if isinstance(e, dict)]
    if not include_directors:
        events = [e for e in events if "Director" not in str(e.get("role", ""))]

    buy_events = [e for e in events if str(e.get("code")) == "P"]
    sell_events = [e for e in events if str(e.get("code")) == "S"]
    buy_v = sum(float(_to_num(e.get("value")) or 0.0) for e in buy_events)
    sell_v = sum(float(_to_num(e.get("value")) or 0.0) for e in sell_events)
    buy_n = len(buy_events)
    sell_n = len(sell_events)
    buy_mean_n = sum(1 for e in buy_events if bool(e.get("meaningful")))
    sell_mean_n = sum(1 for e in sell_events if bool(e.get("meaningful")))
    csuite_events = sum(1 for e in events if "Officer" in str(e.get("role", "")))
    director_events = sum(1 for e in events if "Director" in str(e.get("role", "")))
    owner_events = sum(1 for e in events if "10% Owner" in str(e.get("role", "")))

    out["found"] = len(events) > 0
    out["buy_v"] = buy_v
    out["sell_v"] = sell_v
    out["buy_n"] = buy_n
    out["sell_n"] = sell_n
    out["buy_mean_n"] = buy_mean_n
    out["sell_mean_n"] = sell_mean_n
    out["events"] = len(events)
    out["csuite_events"] = csuite_events
    out["director_events"] = director_events
    out["owner_events"] = owner_events
    if events:
        out["note"] = (
            f"Parsed {len(events)} insider Form 4 open-market events in last {months} months "
            f"(officer={csuite_events}, directors={director_events}, 10% owners={owner_events}). "
            f"Meaningful (>=${meaningful_usd:,.0f}): buys={buy_mean_n}, sells={sell_mean_n}."
        )
    else:
        out["note"] = f"No qualifying insider Form 4 open-market events in last {months} months."

    _cache_put(ck, out)
    return out


def _parse_reporting_name_role(text: str) -> tuple[str, str]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    name = "Unknown Insider"
    for i, ln in enumerate(lines):
        if "Name and Address of Reporting Person" in ln:
            for cand in lines[i + 1 : i + 10]:
                if cand.startswith("("):
                    continue
                if re.fullmatch(r"[A-Z][A-Z0-9 .,'&\-]{2,80}", cand):
                    name = cand.title()
                    break
            break
    flat = re.sub(r"\s+", " ", text)
    flags: list[str] = []
    if re.search(r"\bX\s+Director\b", flat, flags=re.IGNORECASE):
        flags.append("Director")
    if re.search(r"\bX\s+10%\s*Owner\b", flat, flags=re.IGNORECASE):
        flags.append("10% Owner")
    if re.search(r"\bX\s+Officer\b", flat, flags=re.IGNORECASE):
        flags.append("Officer")
    if not flags:
        low = flat.lower()
        if "chief executive officer" in low or " ceo" in low or "chief financial officer" in low or " cfo" in low:
            flags.append("Officer")
        if "director" in low:
            flags.append("Director")
        if "10% owner" in low:
            flags.append("10% Owner")
    role = ", ".join(flags) if flags else "Insider"
    return name, role


def _sec_insider_details(ticker: str, months: int = 18, meaningful_usd: float = 100000.0) -> dict[str, object]:
    ck = f"secinsiderdetails:{ticker}:{months}:{int(meaningful_usd)}"
    cached = _cache_get(ck, ttl_seconds=900)
    if isinstance(cached, dict):
        return cached

    cutoff = (dt.datetime.now() - dt.timedelta(days=max(30, months * 30))).strftime("%Y-%m-%d")
    out: dict[str, object] = {
        "events": [],
        "buyers": [],
        "sellers": [],
        "cutoff": cutoff,
    }
    conn = research_db()
    try:
        rows = conn.execute(
            "SELECT date, path FROM filings WHERE ticker = ? AND form = '4' AND date >= ? ORDER BY date DESC LIMIT 96",
            (ticker, cutoff),
        ).fetchall()

        events: list[dict[str, object]] = []
        for r in rows:
            date_s = str(r["date"] or "")
            txt = _read_filing_text(str(r["path"] or ""), max_chars=500000)
            if not txt:
                continue
            name, role = _parse_reporting_name_role(txt)
            for m in re.finditer(r"\b([PS])\(\d+\)\s+([\d,]+)\s+[AD]\s+\$([\d,]+(?:\.\d+)?)", txt, flags=re.IGNORECASE):
                code = (m.group(1) or "").upper()
                sh = _to_num((m.group(2) or "").replace(",", "")) or 0.0
                pr = _to_num((m.group(3) or "").replace(",", "")) or 0.0
                if sh <= 0 or pr <= 0:
                    continue
                value = sh * pr
                events.append(
                    {
                        "date": date_s,
                        "name": name,
                        "role": role,
                        "code": code,
                        "shares": sh,
                        "price": pr,
                        "value": value,
                        "meaningful": value >= meaningful_usd,
                    }
                )

        def _aggregate(code: str) -> list[dict[str, object]]:
            by_person: dict[str, dict[str, object]] = {}
            for e in events:
                if e["code"] != code:
                    continue
                key = f"{e['name']}|{e['role']}"
                row = by_person.get(key)
                if row is None:
                    row = {
                        "name": e["name"],
                        "role": e["role"],
                        "trades": 0,
                        "shares": 0.0,
                        "value": 0.0,
                        "meaningful_trades": 0,
                        "last_date": e["date"],
                    }
                    by_person[key] = row
                row["trades"] = int(row["trades"]) + 1
                row["shares"] = float(row["shares"]) + float(e["shares"])
                row["value"] = float(row["value"]) + float(e["value"])
                if bool(e["meaningful"]):
                    row["meaningful_trades"] = int(row["meaningful_trades"]) + 1
                if str(e["date"]) > str(row["last_date"]):
                    row["last_date"] = str(e["date"])
            out_rows = []
            for row in by_person.values():
                sh = float(row["shares"]) or 0.0
                val = float(row["value"]) or 0.0
                avg = (val / sh) if sh > 0 else 0.0
                row["avg_price"] = avg
                out_rows.append(row)
            out_rows.sort(key=lambda x: float(x["value"]), reverse=True)
            return out_rows

        out["events"] = sorted(events, key=lambda x: (str(x["date"]), float(x["value"])), reverse=True)
        out["buyers"] = _aggregate("P")
        out["sellers"] = _aggregate("S")
    finally:
        conn.close()

    _cache_put(ck, out)
    return out


def _insider_skin_signal(ticker: str) -> dict[str, str]:
    ck = f"insider:{ticker}"
    cached = _cache_get(ck, ttl_seconds=900)
    if isinstance(cached, dict):
        return cached
    out = {
        "verdict": "No clear signal",
        "headline": "Insider activity data is limited for this ticker.",
        "detail": "Could not confirm open-market CEO/CFO behavior from available feed.",
    }

    # Primary source: local SEC Form 4 filings (C-suite + directors).
    sec = _sec_insider_signal(ticker, months=18, meaningful_usd=100000.0, include_directors=True)
    sec_events = int(_to_num(sec.get("events")) or 0)
    if sec_events > 0:
        buy_v = float(_to_num(sec.get("buy_v")) or 0.0)
        sell_v = float(_to_num(sec.get("sell_v")) or 0.0)
        buy_n = int(_to_num(sec.get("buy_n")) or 0)
        sell_n = int(_to_num(sec.get("sell_n")) or 0)
        if buy_v >= 100000 and buy_n > 0 and sell_n == 0:
            out["verdict"] = "High Conviction Buy"
        elif buy_v > sell_v and buy_n > 0:
            out["verdict"] = "Bullish Accumulation"
        elif sell_v > buy_v and sell_n > 0:
            out["verdict"] = "Bearish Distribution"
        else:
            out["verdict"] = "Mixed / Low Conviction"
        out["headline"] = f"Insider open-market flow (SEC Form 4): buys=${buy_v:,.0f} ({buy_n}) vs sells=${sell_v:,.0f} ({sell_n})."
        out["detail"] = f"{sec.get('note', '')} Net flow={(buy_v - sell_v):,.0f}. Source=SEC Form 4."
        _cache_put(ck, out)
        return out

    # Fallback source: yfinance insider endpoint.
    if yf is None:
        out["detail"] = f"{sec.get('note', '')} yfinance is unavailable in this environment."
        _cache_put(ck, out)
        return out
    try:
        tk = yf.Ticker(ticker)
        tx = None
        if hasattr(tk, "get_insider_transactions"):
            tx = tk.get_insider_transactions()
        if tx is None and hasattr(tk, "insider_transactions"):
            tx = tk.insider_transactions
        rows = []
        if tx is not None and hasattr(tx, "iterrows"):
            for _, row in tx.iterrows():
                rows.append(dict(row))
        buy_v = 0.0
        sell_v = 0.0
        buy_n = 0
        sell_n = 0
        for r in rows:
            role = f"{r.get('Position','')} {r.get('Insider','')} {r.get('Title','')}".upper()
            if ("CEO" not in role) and ("CFO" not in role) and ("DIRECTOR" not in role):
                continue
            action = f"{r.get('Transaction','')} {r.get('Text','')} {r.get('Type','')}".lower()
            if "open" not in action and "purchase" not in action and "sale" not in action and "buy" not in action and "sell" not in action:
                continue
            value = _to_num(r.get("Value"))
            if value is None:
                sh = _to_num(r.get("Shares")) or 0.0
                pr = _to_num(r.get("Price")) or 0.0
                value = sh * pr
            if value <= 0:
                continue
            if "purchase" in action or "buy" in action:
                buy_n += 1
                buy_v += value
            elif "sale" in action or "sell" in action:
                sell_n += 1
                sell_v += value
        if buy_v >= 100000 and buy_n > 0 and sell_n == 0:
            out["verdict"] = "High Conviction Buy"
        elif buy_v > sell_v and buy_n > 0:
            out["verdict"] = "Bullish Accumulation"
        elif sell_v > buy_v and sell_n > 0:
            out["verdict"] = "Bearish Distribution"
        else:
            out["verdict"] = "Mixed / Low Conviction"
        out["headline"] = f"Insider open-market flow (fallback feed): buys=${buy_v:,.0f} ({buy_n}) vs sells=${sell_v:,.0f} ({sell_n})."
        out["detail"] = f"{sec.get('note', '')} Conviction score weights size and direction. Net flow={(buy_v - sell_v):,.0f}. Source=fallback."
    except Exception as e:
        out["detail"] = f"{sec.get('note', '')} Insider parsing fallback error: {str(e)[:120]}"
    _cache_put(ck, out)
    return out


def _moat_health(ticker: str) -> dict[str, str]:
    ck = f"moat:{ticker}"
    cached = _cache_get(ck, ttl_seconds=900)
    if isinstance(cached, dict):
        return cached
    out = {"verdict": "Moat trend unclear", "headline": "Not enough margin/ROIC data.", "detail": "Add more historical financial statements."}
    if yf is None:
        _cache_put(ck, out)
        return out
    try:
        tk = yf.Ticker(ticker)
        qf = getattr(tk, "quarterly_financials", None)
        qb = getattr(tk, "quarterly_balance_sheet", None)
        margins: list[float] = []
        roics: list[float] = []
        if qf is not None and hasattr(qf, "columns") and hasattr(qf, "index"):
            for col in list(qf.columns)[:6]:
                rev = None
                op = None
                ni = None
                for rk in ["Total Revenue", "Revenue"]:
                    if rk in qf.index:
                        rev = _to_num(qf.at[rk, col])
                        if rev is not None:
                            break
                for ok in ["Operating Income", "EBIT"]:
                    if ok in qf.index:
                        op = _to_num(qf.at[ok, col])
                        if op is not None:
                            break
                if "Net Income" in qf.index:
                    ni = _to_num(qf.at["Net Income", col])
                if rev not in (None, 0.0) and op is not None:
                    margins.append(op / rev * 100.0)
                if qb is not None and hasattr(qb, "index"):
                    ta = _to_num(qb.at["Total Assets", col]) if "Total Assets" in qb.index else None
                    cl = _to_num(qb.at["Current Liabilities", col]) if "Current Liabilities" in qb.index else None
                    ic = (ta - cl) if (ta is not None and cl is not None) else None
                    if ni is not None and ic not in (None, 0.0):
                        roics.append(ni / ic * 100.0)
        m_now = margins[0] if margins else None
        m_old = margins[-1] if len(margins) > 1 else None
        r_now = roics[0] if roics else None
        r_old = roics[-1] if len(roics) > 1 else None
        if m_now is not None and m_old is not None and (m_now - m_old) > 1.0:
            out["verdict"] = "Moat strengthening"
        elif m_now is not None and m_old is not None and (m_now - m_old) < -1.0:
            out["verdict"] = "Moat weakening"
        else:
            out["verdict"] = "Moat stable / mixed"
        out["headline"] = f"Operating margin trend: {fmt_pct(m_now)} now vs {fmt_pct(m_old)} prior."
        out["detail"] = f"ROIC proxy: {fmt_pct(r_now)} now vs {fmt_pct(r_old)} prior."
    except Exception as e:
        out["detail"] = f"Moat parsing fallback: {str(e)[:120]}"
    _cache_put(ck, out)
    return out


def generate_bear_case(ticker: str) -> dict[str, object]:
    ck = f"bear:{ticker}"
    cached = _cache_get(ck, ttl_seconds=900)
    if isinstance(cached, dict):
        return cached

    out: dict[str, object] = {
        "headline": f"Why you should NOT buy {ticker}.",
        "verdict": "No structural deterioration detected",
        "points": [],
        "risk_count": 0,
    }
    if yf is None:
        out["points"] = ["Data source unavailable: yfinance is not loaded."]
        _cache_put(ck, out)
        return out

    try:
        tk = yf.Ticker(ticker)
        af = getattr(tk, "financials", None)
        ab = getattr(tk, "balance_sheet", None)

        points: list[str] = []

        # 1) Margin Trap: YoY decline in gross or operating margin.
        gm_old = gm_new = None
        om_old = om_new = None
        if af is not None and hasattr(af, "columns") and hasattr(af, "index"):
            cols = list(af.columns)[:2]
            if len(cols) >= 2:
                c_new, c_old = cols[0], cols[1]
                rev_new = rev_old = None
                for rk in ["Total Revenue", "Revenue"]:
                    if rk in af.index:
                        rev_new = _to_num(af.at[rk, c_new])
                        rev_old = _to_num(af.at[rk, c_old])
                        if rev_new is not None and rev_old is not None:
                            break
                gp_new = _to_num(af.at["Gross Profit", c_new]) if "Gross Profit" in af.index else None
                gp_old = _to_num(af.at["Gross Profit", c_old]) if "Gross Profit" in af.index else None
                op_new = op_old = None
                for ok in ["Operating Income", "EBIT"]:
                    if ok in af.index:
                        op_new = _to_num(af.at[ok, c_new])
                        op_old = _to_num(af.at[ok, c_old])
                        if op_new is not None and op_old is not None:
                            break
                if rev_new not in (None, 0.0) and rev_old not in (None, 0.0):
                    gm_new = (gp_new / rev_new * 100.0) if gp_new is not None else None
                    gm_old = (gp_old / rev_old * 100.0) if gp_old is not None else None
                    om_new = (op_new / rev_new * 100.0) if op_new is not None else None
                    om_old = (op_old / rev_old * 100.0) if op_old is not None else None
        if gm_new is not None and gm_old is not None and gm_new < gm_old:
            points.append(f"ALERT: Margin Trap - Gross margin fell YoY ({gm_old:.1f}% -> {gm_new:.1f}%).")
        if om_new is not None and om_old is not None and om_new < om_old:
            points.append(f"ALERT: Margin Trap - Operating margin fell YoY ({om_old:.1f}% -> {om_new:.1f}%).")

        # 2) Dilution Risk: shares outstanding YoY increase.
        sh_old = sh_new = None
        try:
            sh = tk.get_shares_full(start=(dt.datetime.now() - dt.timedelta(days=800)).strftime("%Y-%m-%d"))
            if sh is not None and hasattr(sh, "dropna"):
                s = sh.dropna()
                if hasattr(s, "__len__") and len(s) >= 2:
                    sh_new = _to_num(s.iloc[-1])
                    sh_old = _to_num(s.iloc[0])
        except Exception:
            pass
        if sh_new is not None and sh_old not in (None, 0.0) and sh_new > sh_old:
            dil = (sh_new - sh_old) / sh_old * 100.0
            points.append(f"ALERT: Dilution Risk - Shares outstanding increased YoY (+{dil:.2f}%). [source: market feed]")
        else:
            sec = _sec_snapshot(ticker)
            sec_dil = _to_num(sec.get("dilution_pct"))
            if sec_dil is not None and sec_dil > 0:
                points.append(f"ALERT: Dilution Risk - Shares outstanding increased between latest filings (+{sec_dil:.2f}%). [source: SEC]")

        # 3) Efficiency Fade: ROIC proxy trend down.
        roic_old = roic_new = None
        if af is not None and ab is not None and hasattr(af, "columns") and hasattr(ab, "columns"):
            fcols = list(af.columns)[:2]
            if len(fcols) >= 2:
                c_new, c_old = fcols[0], fcols[1]
                ni_new = _to_num(af.at["Net Income", c_new]) if "Net Income" in af.index else None
                ni_old = _to_num(af.at["Net Income", c_old]) if "Net Income" in af.index else None
                ta_new = _to_num(ab.at["Total Assets", c_new]) if "Total Assets" in ab.index else None
                ta_old = _to_num(ab.at["Total Assets", c_old]) if "Total Assets" in ab.index else None
                cl_new = _to_num(ab.at["Current Liabilities", c_new]) if "Current Liabilities" in ab.index else None
                cl_old = _to_num(ab.at["Current Liabilities", c_old]) if "Current Liabilities" in ab.index else None
                ic_new = (ta_new - cl_new) if (ta_new is not None and cl_new is not None) else None
                ic_old = (ta_old - cl_old) if (ta_old is not None and cl_old is not None) else None
                if ni_new is not None and ic_new not in (None, 0.0):
                    roic_new = ni_new / ic_new * 100.0
                if ni_old is not None and ic_old not in (None, 0.0):
                    roic_old = ni_old / ic_old * 100.0
        if roic_new is not None and roic_old is not None and roic_new < roic_old:
            points.append(f"ALERT: Efficiency Fade - ROIC proxy declined ({roic_old:.1f}% -> {roic_new:.1f}%).")

        # 4) Leverage Creep: long-term debt growing faster than EBITDA.
        debt_old = debt_new = None
        ebitda_old = ebitda_new = None
        if ab is not None and hasattr(ab, "columns"):
            bcols = list(ab.columns)[:2]
            if len(bcols) >= 2:
                c_new, c_old = bcols[0], bcols[1]
                for dk in ["Long Term Debt", "Long-Term Debt", "Long Term Debt And Capital Lease Obligation"]:
                    if dk in ab.index:
                        debt_new = _to_num(ab.at[dk, c_new])
                        debt_old = _to_num(ab.at[dk, c_old])
                        if debt_new is not None and debt_old is not None:
                            break
        if af is not None and hasattr(af, "columns"):
            fcols = list(af.columns)[:2]
            if len(fcols) >= 2:
                c_new, c_old = fcols[0], fcols[1]
                for ek in ["EBITDA", "Normalized EBITDA", "EBIT"]:
                    if ek in af.index:
                        ebitda_new = _to_num(af.at[ek, c_new])
                        ebitda_old = _to_num(af.at[ek, c_old])
                        if ebitda_new is not None and ebitda_old is not None:
                            break
        if debt_new not in (None,) and debt_old not in (None, 0.0) and ebitda_new not in (None,) and ebitda_old not in (None, 0.0):
            debt_g = (debt_new - debt_old) / abs(debt_old) * 100.0
            ebitda_g = (ebitda_new - ebitda_old) / abs(ebitda_old) * 100.0
            if debt_g > ebitda_g:
                points.append(
                    f"ALERT: Leverage Creep - Long-term debt growth ({debt_g:.1f}%) is outpacing EBITDA growth ({ebitda_g:.1f}%)."
                )

        risk_count = len(points)
        if risk_count > 2:
            verdict = "STRUCTURALLY WEAK"
        elif risk_count > 0:
            verdict = "Watch Structural Drift"
        else:
            verdict = "No structural deterioration detected"

        out["points"] = points or ["No margin, dilution, efficiency, or leverage deterioration detected from available annual data."]
        out["risk_count"] = risk_count
        out["verdict"] = verdict
    except Exception as e:
        out["points"] = [f"Bear-case engine fallback: {str(e)[:160]}"]
        out["verdict"] = "Analysis unavailable"
        out["risk_count"] = 0

    _cache_put(ck, out)
    return out


def _forensic_check(ticker: str) -> dict[str, str]:
    conn = research_db()
    out = {
        "verdict": "No major forensic alert",
        "headline": "No severe keyword concentration detected.",
        "detail": "Latest 10-K/10-Q scan is clean or unavailable.",
        "top_terms": "none",
    }
    try:
        row = conn.execute(
            "SELECT form, date, path FROM filings WHERE ticker = ? AND form IN ('10-K','10-Q') ORDER BY date DESC LIMIT 1",
            (ticker,),
        ).fetchone()
        if not row:
            return out
        path_s = str(row["path"] or "")
        p = Path(path_s)
        if not p.is_absolute():
            p = ROOT / path_s
        text = p.read_text(encoding="utf-8", errors="ignore")[:600000] if p.exists() else ""
        terms_path = DATA / "red_flag_terms.txt"
        terms = [ln.strip().lower() for ln in terms_path.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()] if terms_path.exists() else ["material weakness", "going concern", "restatement", "impairment", "liquidity", "covenant"]
        hits: list[tuple[str, int]] = []
        low = text.lower()
        for t in terms[:120]:
            c = low.count(t)
            if c > 0:
                hits.append((t, c))
        hits.sort(key=lambda x: x[1], reverse=True)
        top = hits[:5]
        score = sum(c for _, c in top)
        if score >= 25:
            out["verdict"] = "Elevated forensic risk"
        elif score >= 10:
            out["verdict"] = "Moderate forensic risk"
        else:
            out["verdict"] = "Low forensic risk"
        top3 = top[:3]
        top3_txt = ", ".join(k for k, _ in top3) if top3 else "none"
        out["top_terms"] = top3_txt
        out["headline"] = f"{row['form']} {row['date']} | score={score} | Found: {top3_txt}"
        out["detail"] = "Top term counts: " + (", ".join(f"{k} ({v})" for k, v in top) if top else "none")
    except Exception as e:
        out["detail"] = f"Forensic scan fallback: {str(e)[:120]}"
    finally:
        conn.close()
    return out


def get_focus_cards(ticker: str, ttl_seconds: int = 900) -> dict[str, object]:
    t = (ticker or "").strip().upper()
    if not t:
        return {"bear": {}, "insider": {}, "moat": {}, "forensic": {}}
    ck = f"focus_cards:{t}"
    cached = _cache_get(ck, ttl_seconds=ttl_seconds)
    if isinstance(cached, dict):
        return cached
    data = {
        "bear": dict(generate_bear_case(t)),
        "insider": dict(_insider_skin_signal(t)),
        "moat": dict(_moat_health(t)),
        "forensic": dict(_forensic_check(t)),
    }
    _cache_put(ck, data)
    return data


def _ticker_evidence(ticker: str) -> dict[str, str]:
    ck = f"evidence:{ticker}"
    cached = _cache_get(ck, ttl_seconds=300)
    if isinstance(cached, dict):
        return cached
    data = {
        "price": "-",
        "day": "-",
        "pe": "-",
        "mcap": "-",
        "chart": "-",
        "asof": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": "yfinance",
        "quality": "LOW",
        "quality_note": "Limited fields returned by provider.",
        "sec_latest": "-",
        "sec_prev": "-",
        "sec_dilution": "-",
    }

    # Evidence panel should be deterministic on first render: fetch sync for one ticker.
    q = fetch_quote_single(ticker)
    px = q.get("price")
    day = q.get("day_pct")
    data["price"] = fmt_money(px if isinstance(px, float) else None)
    data["day"] = fmt_pct(day if isinstance(day, float) else None)

    if yf is not None:
        try:
            tk = yf.Ticker(ticker)
            fi = getattr(tk, "fast_info", None) or {}
            info = getattr(tk, "info", None) or {}

            mcap = fi.get("marketCap") or fi.get("market_cap") or info.get("marketCap")
            data["mcap"] = _fmt_big(mcap)

            pe = fi.get("trailingPE") or fi.get("peRatio") or info.get("trailingPE") or info.get("forwardPE")
            data["pe"] = f"{float(pe):.2f}" if pe is not None else "-"
            hist = tk.history(period="1mo", interval="1d")
            closes: list[float] = []
            if hasattr(hist, "__getitem__"):
                series = hist["Close"]
                if hasattr(series, "dropna"):
                    series = series.dropna()
                if hasattr(series, "tolist"):
                    closes = [float(x) for x in series.tolist() if _to_num(x) is not None]
            if not closes:
                hist = tk.history(period="3mo", interval="1wk")
                if hasattr(hist, "__getitem__"):
                    series = hist["Close"]
                    if hasattr(series, "dropna"):
                        series = series.dropna()
                    if hasattr(series, "tolist"):
                        closes = [float(x) for x in series.tolist() if _to_num(x) is not None]
            data["chart"] = _sparkline(closes)
        except Exception:
            pass

    sec = _sec_snapshot(ticker)
    sec_latest = f"{sec.get('latest_form','-')} {sec.get('latest_date','-')}"
    sec_prev = f"{sec.get('prev_form','-')} {sec.get('prev_date','-')}"
    sec_d = _to_num(sec.get("dilution_pct"))
    data["sec_latest"] = sec_latest
    data["sec_prev"] = sec_prev
    data["sec_dilution"] = fmt_pct(sec_d) if sec_d is not None else "-"

    present = 0
    if data["price"] != "-":
        present += 1
    if data["day"] != "-":
        present += 1
    if data["pe"] != "-":
        present += 1
    if data["mcap"] != "-":
        present += 1
    if present >= 4:
        data["quality"] = "HIGH"
        data["quality_note"] = "All core evidence fields loaded."
    elif present >= 2:
        data["quality"] = "PARTIAL"
        data["quality_note"] = "Some fields missing from provider; refresh may help."
    else:
        data["quality"] = "LOW"
        data["quality_note"] = "Provider returned sparse data for this ticker right now."

    _cache_put(ck, data)
    return data


def _evidence_refresh_worker(ticker: str) -> None:
    try:
        _ticker_evidence(ticker)
    finally:
        with LOCK:
            EVIDENCE_REFRESH[ticker] = False


def _start_evidence_refresh(ticker: str) -> None:
    t = (ticker or "").strip().upper()
    if not t:
        return
    with LOCK:
        if bool(EVIDENCE_REFRESH.get(t, False)):
            return
        EVIDENCE_REFRESH[t] = True
    th = threading.Thread(target=_evidence_refresh_worker, args=(t,), daemon=True)
    th.start()


def get_ticker_evidence_fast(ticker: str, ttl_seconds: int = 240) -> dict[str, str]:
    t = (ticker or "").strip().upper()
    if not t:
        return {"price": "-", "day": "-", "pe": "-", "mcap": "-", "quality": "LOW", "quality_note": "Ticker required.", "source": "-", "sec_latest": "-", "sec_prev": "-", "sec_dilution": "-", "chart": "-", "asof": "-"}
    ck = f"evidence:{t}"
    cached = _cache_get(ck, ttl_seconds=ttl_seconds)
    if isinstance(cached, dict):
        return dict(cached)
    _start_evidence_refresh(t)
    return {
        "price": "-",
        "day": "-",
        "pe": "-",
        "mcap": "-",
        "quality": "LOADING",
        "quality_note": "Evidence is loading in background.",
        "source": "-",
        "sec_latest": "-",
        "sec_prev": "-",
        "sec_dilution": "-",
        "chart": "-",
        "asof": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def dashboard_html(
    message: str = "",
    focus_ticker: str = "",
    earnings_show_all: bool = False,
    news_mode: str = "general",
    reg_mode: str = "portfolio",
    deep_mode: bool = False,
) -> str:
    fast_mode = FAST_MODE_DEFAULT
    focus_ticker = (focus_ticker or "").strip().upper()
    daily = latest("reports/terminal_daily_brief_*.md")
    red_flag = latest("reports/.terminal_inputs/red_flag_alert_*.txt")
    earnings = _latest_earnings_file()
    weekly = latest("reports/terminal_weekly_outlook_*.md")
    monthly = latest("reports/terminal_monthly_ic_memo_*.md")

    alert_key = f"{_file_sig(red_flag)}:limit=32"
    earn_key_wk = f"{_file_sig(earnings)}:limit=16:scope=week"
    alerts = _cached_rank("alerts", alert_key, ["python3", "tools/red_flag_rank.py", "--file", red_flag, "--limit", "32"]) if red_flag else []
    week_events = _cached_rank("week", earn_key_wk, ["python3", "tools/earnings_watch_rank.py", "--file", earnings, "--limit", "16", "--scope", "week"]) if earnings else []

    watch_entries = read_watchlist_entries(DATA / "my_watchlist.txt")
    watchlist = []
    seen_w: set[str] = set()
    for e in watch_entries:
        t = e.get("ticker", "").upper().strip()
        if t and t not in seen_w:
            seen_w.add(t)
            watchlist.append(t)
    portfolio_rows = read_portfolio_rows(DATA / "portfolio.csv")
    portfolio = []
    for r in portfolio_rows:
        t = (r[0] or "").upper().strip()
        if t and t not in portfolio:
            portfolio.append(t)

    all_tickers = sorted(set(watchlist + portfolio))
    if focus_ticker and focus_ticker not in all_tickers:
        all_tickers.append(focus_ticker)
    sidebar_quotes = {} if fast_mode else (get_live_quotes(all_tickers, ttl_seconds=120) if all_tickers else {})
    news_mode = (news_mode or "general").strip().lower()
    if news_mode not in {"general", "company"}:
        news_mode = "general"
    reg_mode = (reg_mode or "portfolio").strip().lower()
    if reg_mode not in {"portfolio", "watchlist", "both"}:
        reg_mode = "portfolio"
    deep_mode = bool(deep_mode)
    deep_q = "&deep=1" if deep_mode and not focus_ticker else ""

    msg_html = f"<div class='flash'>{html.escape(message)}</div>" if message else ""
    quick_capture_html = _quick_capture_widget_html(
        return_to=(f"/?t={urllib.parse.quote(focus_ticker)}" if focus_ticker else "/"),
        current_ticker=focus_ticker,
    )
    try:
        _todos = list_todos(limit=500)
        open_tasks = sum(1 for r in _todos if str(r["status"] or "").lower() == "open")
    except Exception:
        open_tasks = 0
    top_nav = (
        "<a href='/organizer'>Organizer</a>"
        "<a href='/reports'>Reports</a>"
        "<a href='/company_file'>Company Files</a>"
        "<a href='/watchlist_history'>History</a>"
        "<a href='/universe'>My Companies</a>"
    )
    macro_banner_html = _macro_watchdog_banner_html()

    # Speed Engine: real-time market-moving news ticker.
    news_items: list[dict[str, str]] = []
    if news_wire is not None:
        if news_mode == "company":
            company_pool = [focus_ticker] if focus_ticker else (portfolio + watchlist)
            company_pool = [t for t in company_pool if t][:8]
            ck_news = f"news_wire:v2:company:{'|'.join(sorted(set(company_pool)))}"
        else:
            company_pool = []
            ck_news = "news_wire:v2:general"
        cached_news = _cache_get(ck_news, ttl_seconds=180)
        if isinstance(cached_news, list):
            news_items = [dict(x) for x in cached_news if isinstance(x, dict)]
        elif not fast_mode:
            try:
                if news_mode == "company" and company_pool:
                    news_items = news_wire.get_market_moving_news_for_tickers(
                        tickers=company_pool,
                        limit=10,
                        api_key=os.getenv("FINNHUB_API_KEY", "").strip(),
                        model="llama3",
                    )
                else:
                    news_items = news_wire.get_market_moving_news(
                        limit=10,
                        api_key=os.getenv("FINNHUB_API_KEY", "").strip(),
                        model="llama3",
                    )
                _cache_put(ck_news, news_items)
            except Exception:
                news_items = []
    n_general = f"/?nw=general&reg={reg_mode}" + (f"&t={focus_ticker}" if focus_ticker else "") + deep_q
    n_company = f"/?nw=company&reg={reg_mode}" + (f"&t={focus_ticker}" if focus_ticker else "") + deep_q
    if news_items:
        news_cells = []
        for n in news_items[:10]:
            h = str(n.get("headline") or "").strip()
            u = str(n.get("url") or "").strip()
            reason = str(n.get("reason") or "").strip()
            src = str(n.get("source") or "").strip()
            if not h:
                continue
            label = h if not reason else f"{h} ({reason})"
            if src:
                label = f"[{src}] {label}"
            if u:
                news_cells.append(f"<a target='_blank' rel='noopener noreferrer' href='{html.escape(u, quote=True)}'>{html.escape(label)}</a>")
            else:
                news_cells.append(f"<span>{html.escape(label)}</span>")
        joined = " <span class='sep'>•</span> ".join(news_cells) if news_cells else "No market-moving headlines yet."
        news_ticker_html = (
            "<section class='news-wire'>"
            "<div class='news-title'>News Wire</div>"
            f"<div class='news-ctrl'><a href='{html.escape(n_general)}' class='{'on' if news_mode == 'general' else ''}'>General</a><a href='{html.escape(n_company)}' class='{'on' if news_mode == 'company' else ''}'>Company</a></div>"
            f"<div class='news-marquee'><div class='news-track'>{joined}</div></div>"
            "</section>"
        )
    else:
        news_ticker_html = (
            "<section class='news-wire'>"
            "<div class='news-title'>News Wire</div>"
            f"<div class='news-ctrl'><a href='{html.escape(n_general)}' class='{'on' if news_mode == 'general' else ''}'>General</a><a href='{html.escape(n_company)}' class='{'on' if news_mode == 'company' else ''}'>Company</a></div>"
            "<div class='news-marquee'><div class='news-track muted'>No market-moving headlines right now.</div></div>"
            "</section>"
        )

    # Left sidebar: Library (with live price/day move)
    lib_portfolio = list(portfolio)
    lib_watchlist = list(watchlist)

    def _lib_item(t: str) -> str:
        q = sidebar_quotes.get(t, {}) if isinstance(sidebar_quotes, dict) else {}
        px = q.get("price")
        d = q.get("day_pct")
        px_txt = fmt_money(px if isinstance(px, float) else None)
        d_txt = fmt_pct(d if isinstance(d, float) else None)
        d_cls = "up" if isinstance(d, float) and d >= 0 else ("down" if isinstance(d, float) else "flat")
        return (
            f"<a class='ticker' href='/company_file?t={html.escape(t)}'>"
            f"<span class='tk'>{html.escape(t)}</span>"
            f"<span class='q'>{html.escape(px_txt)}</span>"
            f"<span class='d {d_cls}'>{html.escape(d_txt)}</span>"
            "</a>"
        )
    wl_items = "".join(_lib_item(t) for t in lib_watchlist) or "<div class='muted'>Empty</div>"
    pf_items = "".join(_lib_item(t) for t in lib_portfolio) or "<div class='muted'>Empty</div>"
    ws_link = f"/company_file?t={html.escape(focus_ticker)}" if focus_ticker else "/company_file"
    is_weekend = dt.datetime.now().weekday() >= 5
    portfolio_closed_note_html = "<div class='closed-note'>Market closed for portfolio.</div>" if is_weekend else ""
    chat_rows = CHAT_HISTORY[-8:]
    chat_html = "".join(
        f"<div class='chat-item'><div class='chat-meta'>{html.escape(str(c.get('ts','')))} | {html.escape(str(c.get('mode','full')))} | {html.escape(str(c.get('speed','quick')))} | {html.escape(str(c.get('provider','-')))} {html.escape(str(c.get('model','')))}</div>"
        f"<div><strong>Q:</strong> {html.escape(str(c.get('q','')))}</div>"
        f"<div><strong>A:</strong> {html.escape(str(c.get('a',''))[:1200])}</div>"
        f"<div class='muted'>Sources: {html.escape(str(c.get('sources','-')))}</div></div>"
        for c in reversed(chat_rows)
    ) or "<div class='muted'>No AI chat yet.</div>"

    def _intel_feed_section(limit: int = 10, ticker_filter: str = "") -> str:
        tf = (ticker_filter or "").strip().upper()
        rows = list_intel_feed(limit=30)
        if tf:
            rows = [r for r in rows if str(r["ticker"] or "").upper() == tf]
        lim = max(1, min(18, limit))
        # Prefer filing-derived intelligence over pure price-move snippets.
        rich = [r for r in rows if str(r["category"] or "").upper() in {"FILING_INTEL", "FINANCIAL_INTEL", "DEEP_DIVE"}]
        fast = [r for r in rows if str(r["category"] or "").upper() not in {"FILING_INTEL", "FINANCIAL_INTEL", "DEEP_DIVE"}]
        rows = (rich + fast)[:lim]
        if not rows:
            label = f"{tf}." if tf else "portfolio/watchlist."
            return (
                "<section class='feed-card'>"
                "<h2>Live Intel Feed</h2>"
                f"<div class='muted'>No new intelligence yet for {html.escape(label)} Background watcher is active.</div>"
                "</section>"
            )
        items = []
        for r in rows:
            rid = int(r["id"])
            created = str(r["created_at"] or "").replace("T", " ")[:19]
            tkr = str(r["ticker"] or "").upper()
            cat = str(r["category"] or "").upper()
            sev = _to_int(r["severity"], 0)
            model = str(r["model"] or "-")
            title = str(r["title"] or "")
            summary = str(r["summary"] or "")
            detail = str(r["detail"] or "")
            items.append(
                "<article class='intel-item'>"
                "<div class='intel-item-top'>"
                f"<a class='tk' href='/company_file?t={html.escape(tkr)}'>{html.escape(tkr)}</a>"
                f"<span class='chip'>{html.escape(cat)}</span>"
                f"<span class='sev'>S{sev}</span>"
                f"<span class='muted'>{html.escape(created)}</span>"
                f"<span class='muted'>#{rid}</span>"
                "</div>"
                f"<div class='intel-title'>{html.escape(title)}</div>"
                f"<div class='muted'>{html.escape(summary)}</div>"
                f"<details><summary>Evidence</summary><pre class='chart' style='white-space:pre-wrap;margin-top:6px;'>{html.escape(detail[:1400])}</pre></details>"
                f"<div class='muted'>Source: {html.escape(str(r['source'] or '-'))} | Model: {html.escape(model)}</div>"
                "</article>"
            )
        scope = f"for {tf}" if tf else "for portfolio and watchlist"
        return (
            "<section class='feed-card'>"
            "<h2>Live Intel Feed</h2>"
            f"<div class='muted'>Latest watcher intelligence {html.escape(scope)}.</div>"
            + "".join(items)
            + "</section>"
        )

    # Center feed
    feed_cards: list[str] = []
    if not focus_ticker:
        home_base = f"/?nw={urllib.parse.quote(news_mode)}&reg={urllib.parse.quote(reg_mode)}"
        home_expand = f"{home_base}&deep=1#home-intel"
        home_collapse = f"{home_base}#home-intel"
        feed_cards.append(
            "<section id='home-intel' class='feed-card'>"
            "<h2>Home Intelligence</h2>"
            + (
                "<div class='muted'>Collapsed for speed. Expand when you want full briefing and earnings intelligence.</div>"
                f"<div class='actions'><a href='{html.escape(home_expand)}'>Get Intelligence</a></div>"
                if not deep_mode
                else "<div class='muted'>Expanded view is on.</div>"
                f"<div class='actions'><a href='{html.escape(home_collapse)}'>Collapse</a></div>"
            )
            + "</section>"
        )
        if deep_mode:
            briefing = _extract_briefing_lines(daily, limit=7)
            brief_html = "".join(f"<li>{html.escape(x)}</li>" for x in briefing)
            brief_ai = "" if fast_mode else _daily_brief_ai_summary(daily, briefing)
            brief_ai_html = _render_pm_take_block("PM Take", brief_ai, len(briefing), "brief")
            feed_cards.append(
                "<section class='feed-card'><h2>Daily Briefing</h2>"
                "<div class='muted'>AI synthesis from latest macro brief.</div>"
                f"{brief_ai_html}<ul>{brief_html}</ul></section>"
            )

            rep = get_reported_earnings_snapshot(limit=140)
            rep_rows = list(rep.get("this_week_rows") or [])
            if not rep_rows:
                rep_rows = list(rep.get("parsed") or [])[:24]
            fr = dict(rep.get("freshness") or {})
            fr_label = str(fr.get("label") or "-")
            fr_note = str(fr.get("note") or "")
            fr_stamp = str(fr.get("stamp") or "-")

            rep_tickers = [str(r.get("ticker") or "").upper().strip() for r in rep_rows if str(r.get("ticker") or "").strip()]
            rep_profiles = get_portfolio_profiles(rep_tickers) if rep_tickers else {}

            def _simple_reported_txt(r: dict[str, object]) -> str:
                a = r.get("actual")
                e = r.get("est")
                if isinstance(a, float) and isinstance(e, float):
                    return f"EPS {a:.2f} vs {e:.2f}"
                return "-"

            def _simple_verdict_txt(r: dict[str, object]) -> str:
                v = str(r.get("verdict") or "").upper().strip()
                if v in {"BEAT", "MISS"}:
                    return v
                return "REPORTED"

            def _simple_report_date(r: dict[str, object]) -> str:
                dtxt = str(r.get("date_txt") or "").strip()
                m = re.search(r"\b\d{4}-\d{2}-\d{2}\b", dtxt)
                return m.group(0) if m else (dtxt[:10] if len(dtxt) >= 10 else (dtxt or "-"))

            rep_table = (
                "<div class='ei-list'>"
                + "".join(
                    (
                        "<article class='ei-row'>"
                        "<div class='ei-left'>"
                        f"<a class='ei-ticker' href='/company_file?t={html.escape(str(r.get('ticker') or '-'))}'>{html.escape(str(r.get('ticker') or '-'))}</a>"
                        f"<span class='ei-date'>{html.escape(_simple_report_date(r))}</span>"
                        f"<a class='ei-sector' href='/earnings_industry?sector={urllib.parse.quote(str((rep_profiles.get(str(r.get('ticker') or '').upper(), {}) or {}).get('sector') or 'Unknown'), safe='')}'>{html.escape(str((rep_profiles.get(str(r.get('ticker') or '').upper(), {}) or {}).get('sector') or 'Unknown'))}</a>"
                        "</div>"
                        "<div class='ei-mid'>"
                        f"<div class='ei-eps'>{html.escape(_simple_reported_txt(r))}</div>"
                        f"<div class='ei-surprise'>Surprise: {html.escape(fmt_pct(float(r.get('surprise'))) if isinstance(r.get('surprise'), float) else '-')}</div>"
                        "</div>"
                        "<div class='ei-right'>"
                        f"<span class='ei-verdict {'beat' if _simple_verdict_txt(r) == 'BEAT' else ('miss' if _simple_verdict_txt(r) == 'MISS' else 'rep')}'>{html.escape(_simple_verdict_txt(r))}</span>"
                        "</div>"
                        "</article>"
                    )
                    for r in rep_rows[:18]
                )
                + "</div>"
            ) if rep_rows else "<div class='muted'>No reported earnings intelligence rows yet.</div>"

            sector_stats: dict[str, dict[str, int]] = {}
            for r in rep_rows:
                t = str(r.get("ticker") or "").upper().strip()
                sec = str((rep_profiles.get(t, {}) or {}).get("sector") or "Unknown").strip() or "Unknown"
                bucket = sector_stats.setdefault(sec, {"beat": 0, "miss": 0, "n": 0})
                v = str(r.get("verdict") or "").upper().strip()
                if v == "BEAT":
                    bucket["beat"] += 1
                elif v == "MISS":
                    bucket["miss"] += 1
                bucket["n"] += 1

            tails: list[str] = []
            heads: list[str] = []
            for sec, s in sorted(sector_stats.items(), key=lambda kv: kv[1]["n"], reverse=True):
                n = s["n"]
                if n < 2:
                    continue
                beat_n = s["beat"]
                miss_n = s["miss"]
                line = f"{sec}: {beat_n} beat / {miss_n} miss ({n} reports)"
                if beat_n > miss_n:
                    tails.append(line)
                elif miss_n > beat_n:
                    heads.append(line)

            total_reports = len(rep_rows)
            total_beats = sum(1 for r in rep_rows if str(r.get("verdict") or "").upper().strip() == "BEAT")
            total_misses = sum(1 for r in rep_rows if str(r.get("verdict") or "").upper().strip() == "MISS")
            beat_rate = (float(total_beats) / float(total_reports) * 100.0) if total_reports > 0 else 0.0
            beat_rows = [r for r in rep_rows if str(r.get("verdict") or "").upper().strip() == "BEAT"]
            miss_rows = [r for r in rep_rows if str(r.get("verdict") or "").upper().strip() == "MISS"]
            beat_rows.sort(key=lambda x: float(x.get("surprise")) if isinstance(x.get("surprise"), float) else -1e9, reverse=True)
            miss_rows.sort(key=lambda x: float(x.get("surprise")) if isinstance(x.get("surprise"), float) else 1e9)
            top_beat = beat_rows[0] if beat_rows else None
            top_miss = miss_rows[0] if miss_rows else None
            top_sectors = sorted(sector_stats.items(), key=lambda kv: kv[1]["n"], reverse=True)[:3]
            top_sector_lines = [
                f"{sec}: {int(s['beat'])} beat / {int(s['miss'])} miss ({int(s['n'])} reports)"
                for sec, s in top_sectors
            ]
            top_headwind = heads[0] if heads else "No clear headwind sector yet."
            top_tailwind = tails[0] if tails else "No clear tailwind sector yet."

            pm_lines: list[str] = []
            pm_lines.append(
                f"Reported this week: {total_reports}; beats: {total_beats}; misses: {total_misses}; beat rate: {beat_rate:.1f}%."
            )
            pm_lines.append(f"Common pattern: headwind {top_headwind} | tailwind {top_tailwind}")
            if top_beat and top_miss:
                bt = str(top_beat.get("ticker") or "-")
                bs = top_beat.get("surprise")
                mt = str(top_miss.get("ticker") or "-")
                ms = top_miss.get("surprise")
                btxt = fmt_pct(float(bs)) if isinstance(bs, float) else "-"
                mtxt = fmt_pct(float(ms)) if isinstance(ms, float) else "-"
                pm_lines.append(f"Largest moves: beat {bt} ({btxt}) and miss {mt} ({mtxt}).")
            elif top_beat:
                bt = str(top_beat.get("ticker") or "-")
                bs = top_beat.get("surprise")
                btxt = fmt_pct(float(bs)) if isinstance(bs, float) else "-"
                pm_lines.append(f"Largest beat: {bt} ({btxt}).")
            elif top_miss:
                mt = str(top_miss.get("ticker") or "-")
                ms = top_miss.get("surprise")
                mtxt = fmt_pct(float(ms)) if isinstance(ms, float) else "-"
                pm_lines.append(f"Largest miss: {mt} ({mtxt}).")

            pm_take_fallback = "\n".join(f"{idx}) {line}" for idx, line in enumerate(pm_lines[:3], start=1))
            earn_src_rows = [
                " | ".join(
                    [
                        str(r.get("ticker") or "-"),
                        str(r.get("date_txt") or "-"),
                        str(r.get("verdict") or "-"),
                        str(r.get("actual") if isinstance(r.get("actual"), float) else "-"),
                        str(r.get("est") if isinstance(r.get("est"), float) else "-"),
                        str(r.get("surprise") if isinstance(r.get("surprise"), float) else "-"),
                        str((rep_profiles.get(str(r.get('ticker') or '').upper(), {}) or {}).get("sector") or "-"),
                    ]
                )
                for r in rep_rows[:40]
            ]
            earn_key = f"{fr_stamp}:{total_reports}:{total_beats}:{total_misses}:{'|'.join(sorted(rep_tickers)[:30])}"
            pm_take_text = _earnings_cards_ai_summary(earn_src_rows, earn_key).strip() or pm_take_fallback
            pm_take_text = (
                pm_take_text
                + "\n\nQuick Intel\n"
                + f"- Reported this week: {total_reports} | Beat: {total_beats} | Miss: {total_misses} | Beat rate: {beat_rate:.1f}%\n"
                + f"- Most active sectors: {', '.join(sec for sec, _s in top_sectors) if top_sectors else '-'}\n\n"
                + "Sector Concentration\n"
                + ("\n".join(f"- {x}" for x in top_sector_lines) if top_sector_lines else "- No sector grouping yet.")
                + "\n\nIndustry Tailwinds\n"
                + ("\n".join(f"- {x}" for x in tails[:6]) if tails else "- No clear sector tailwind yet.")
                + "\n\nIndustry Headwinds\n"
                + ("\n".join(f"- {x}" for x in heads[:6]) if heads else "- No clear sector headwind yet.")
            )
            pm_take_html = _render_pm_take_block("PM Take", pm_take_text, max(1, total_reports), "earnings_simple")
            feed_cards.append(
                "<section class='feed-card'>"
                "<h2>Earnings Intelligence</h2>"
                f"<div class='muted'>This week reported companies only. Freshness: {html.escape(fr_label)} | As of {html.escape(fr_stamp)} | {html.escape(fr_note)}</div>"
                "<div class='muted'>PM Take model: gemma3:27b (cached ~6h, not real-time).</div>"
                f"<div style='margin-top:8px;'>{pm_take_html}</div>"
                "<div style='margin-top:10px;'>"
                "<div class='focus-title'>Reported Companies</div>"
                f"{rep_table}"
                "</div>"
                "</section>"
            )
    else:
        feed_cards.append(
            f"<section class='feed-card'><h2>{html.escape(focus_ticker)}</h2>"
            f"<div class='muted'>Open full company file for notes, timeline, reminders, and filings.</div>"
            f"<div class='actions'><a href='/company_file?t={html.escape(focus_ticker)}'>Open Company File</a></div>"
            "</section>"
        )

    # Right sidebar: Evidence
    with LOCK:
        running_jobs = sum(1 for st in RUN_STATE.values() if bool(st.get("running")))
        total_jobs = len(RUN_STATE)
    reg_rows: list[dict[str, str]] = []
    if sec_watchdog is not None and not fast_mode:
        if reg_mode == "watchlist":
            reg_tickers = watchlist[:8]
        elif reg_mode == "both":
            reg_tickers = (portfolio + watchlist)[:10]
        else:
            reg_tickers = [focus_ticker] if focus_ticker else portfolio[:8]
        if reg_tickers:
            reg_key = f"sec_watchdog:v2:{reg_mode}:{'|'.join(sorted(set(reg_tickers)))}"
            reg_cached = _cache_get(reg_key, ttl_seconds=600)
            if isinstance(reg_cached, list):
                reg_rows = [dict(x) for x in reg_cached if isinstance(x, dict)]
            else:
                try:
                    reg_rows = sec_watchdog.get_material_filings(reg_tickers, per_ticker=3, model="llama3")
                    _cache_put(reg_key, reg_rows)
                except Exception:
                    reg_rows = []
    r_port = f"/?nw={news_mode}&reg=portfolio" + (f"&t={focus_ticker}" if focus_ticker else "") + deep_q
    r_watch = f"/?nw={news_mode}&reg=watchlist" + (f"&t={focus_ticker}" if focus_ticker else "") + deep_q
    r_both = f"/?nw={news_mode}&reg=both" + (f"&t={focus_ticker}" if focus_ticker else "") + deep_q
    reg_widget = (
        "<section class='right-card'><h3>🏛️ Regulatory Audit</h3>"
        f"<div class='news-ctrl' style='margin-bottom:6px;'><a href='{html.escape(r_port)}' class='{'on' if reg_mode == 'portfolio' else ''}'>Portfolio</a><a href='{html.escape(r_watch)}' class='{'on' if reg_mode == 'watchlist' else ''}'>Watchlist</a><a href='{html.escape(r_both)}' class='{'on' if reg_mode == 'both' else ''}'>Both</a></div>"
        + (
            "<ul class='reg-list'>"
            + "".join(
                "<li>"
                f"<div><strong>{html.escape(str(r.get('ticker') or '-'))}</strong> "
                f"<span class='chip'>{html.escape(str(r.get('form') or '-'))}</span></div>"
                f"<div class='muted'>{html.escape(str(r.get('updated') or '')[:19])}</div>"
                f"<div>{html.escape(str(r.get('ai_summary') or r.get('title') or '-'))}</div>"
                + (
                    f"<div><a target='_blank' rel='noopener noreferrer' href='{html.escape(str(r.get('link') or ''), quote=True)}'>Open filing</a></div>"
                    if str(r.get("link") or "").strip()
                    else ""
                )
                + "</li>"
                for r in reg_rows[:8]
            )
            + "</ul>"
            if reg_rows
            else "<div class='muted'>No new material filings in current pull.</div>"
        )
        + "</section>"
    )
    weekend_note_html = (
        "<div class='market-weekend-note'>Market closed.</div>"
        if is_weekend
        else ""
    )
    if focus_ticker:
        right_html = (
            f"<section id='lazy-evidence' class='right-card'><h3>Evidence: {html.escape(focus_ticker)}</h3>"
            f"<div class='muted'>Loading evidence...</div>"
            f"<div class='actions'><a href='/company?t={html.escape(focus_ticker)}'>Company</a><a href='/company?t={html.escape(focus_ticker)}'>SEC Filings</a><a href='/company?t={html.escape(focus_ticker)}'>Open Detail</a></div>"
            "</section>"
            f"{reg_widget}"
        )
    else:
        if fast_mode:
            right_html = (
                "<section class='right-card'><h3>Market Pulse</h3>"
                f"{weekend_note_html}"
                "<div class='muted'>Fast mode is on. Macro panel is minimized for speed.</div>"
                "</section>"
                f"{reg_widget}"
            )
        else:
            macro = get_macro_market_snapshot(ttl_seconds=300)
            macro_rows = list(macro.get("rows") or [])
            groups = {"Indexes": [], "Rates": [], "Risk": [], "Commodities": []}
            for r in macro_rows:
                g = str(r.get("group") or "")
                if g in groups:
                    groups[g].append(r)
            macro_source_lines = [
                f"{x.get('name')}: px={x.get('price')} day={x.get('day')}% src={x.get('source')}"
                for x in macro_rows
                if isinstance(x.get("price"), float) or isinstance(x.get("day"), float)
            ]
            macro_ai = ""
            if len(macro_source_lines) >= 4:
                macro_ai = _ai_cached_reliable_summary(
                    cache_key=f"macro_right_ai:{'|'.join(macro_source_lines)}",
                    source_text="\n".join(macro_source_lines),
                    system=(
                        "You are a macro strategist. Use ONLY provided metric lines. "
                        "Return exactly 3 bullets: regime risk-on/off, key pressure point, and one action cue. "
                        "No external facts."
                    ),
                    ttl_seconds=300,
                )
            def _mini_market_rows(rows: list[dict[str, object]]) -> str:
                blocks: list[str] = []
                for x in rows:
                    name = html.escape(str(x.get("name") or "-"))
                    px = f"{float(x.get('price')):,.2f}" if isinstance(x.get("price"), float) else "-"
                    d = x.get("day") if isinstance(x.get("day"), float) else None
                    d_txt = fmt_pct(d)
                    d_cls = "up" if isinstance(d, float) and d >= 0 else ("down" if isinstance(d, float) else "flat")
                    blocks.append(
                        "<div class='metric metric-pulse'>"
                        f"<span class='m-name'>{name}</span>"
                        "<span class='m-right'>"
                        f"<strong class='m-price'>{px}</strong>"
                        f"<span class='m-day {d_cls}'>{html.escape(d_txt)}</span>"
                        "</span>"
                        "</div>"
                    )
                return "".join(blocks)
            macro_ai_block = _render_pm_take_block("PM Take", macro_ai, len(macro_source_lines), "macro")
            right_html = (
                "<section class='right-card'><h3>Market Pulse</h3>"
                f"{weekend_note_html}"
                "<div class='focus-box'><div class='focus-title'>Indexes</div>"
                f"{_mini_market_rows(groups.get('Indexes') or []) or '<div class=\"muted\">No index data.</div>'}</div>"
                "<div class='focus-box'><div class='focus-title'>Rates / Risk</div>"
                f"{_mini_market_rows((groups.get('Rates') or []) + (groups.get('Risk') or [])) or '<div class=\"muted\">No rates/risk data.</div>'}</div>"
                "<div class='focus-box'><div class='focus-title'>Commodities</div>"
                f"{_mini_market_rows(groups.get('Commodities') or []) or '<div class=\"muted\">No commodities data.</div>'}</div>"
                f"{macro_ai_block}"
                f"<div class='muted' style='margin-top:8px;'>As of {html.escape(str(macro.get('asof') or '-'))}</div>"
                "</section>"
                f"{reg_widget}"
            )

    feed_html = "".join(feed_cards)
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""<!doctype html>
<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Investment Intelligence Platform</title>
<style>
  :root {{ --bg:#EAF0F4; --panel:#F2F5F8; --line:#D3DCE5; --text:#2F4358; --muted:#4F6780; --ink:#2F4358; --accent:#FF7A59; --success:#00BDA5; --risk:#D93B59; }}
  body {{ margin:0; color:var(--text); background:var(--bg); font-family:"Avenir Next","Helvetica Neue",sans-serif; }}
  a {{ color:var(--ink); text-decoration:none; }}
  .shell {{ max-width:1460px; margin:0 auto; padding:12px; }}
  .top {{ display:flex; justify-content:space-between; align-items:flex-end; gap:10px; margin-bottom:10px; padding:6px 2px; border-bottom:1px solid #d7e0e8; }}
  .top h1 {{ margin:0; font-size:22px; letter-spacing:.1px; }}
  .top .nav a {{ margin-left:8px; padding:5px 9px; border:1px solid #ff7a59; border-radius:7px; background:#ff7a59; color:#ffffff; font-size:12px; font-weight:700; }}
  .intel-badge {{ margin-left:6px; border:1px solid #4f2f36; border-radius:999px; background:#3a1b22; color:#ffdbe1; padding:1px 7px; font-size:10px; font-weight:700; }}
  .flash {{ margin-bottom:10px; padding:8px 10px; border:1px solid #355d78; border-radius:8px; background:#152637; }}
  .macro-banner {{ margin-bottom:10px; padding:8px 10px; border-radius:8px; font-size:12px; }}
  .macro-banner.ok {{ border:1px solid #2f7158; background:#133027; color:#d8f7e8; }}
  .macro-banner.warn {{ border:1px solid #8a4a4a; background:#331b1f; color:#ffd8dc; }}
  .news-wire {{ margin-bottom:10px; border:1px solid #cfd9e2; border-radius:8px; background:#f2f5f8; overflow:hidden; display:grid; grid-template-columns:92px 170px minmax(0,1fr); }}
  .news-title {{ padding:8px 10px; font-size:11px; text-transform:uppercase; letter-spacing:.6px; color:#33475b; border-right:1px solid #e1e6eb; }}
  .news-ctrl {{ display:flex; align-items:center; gap:6px; padding:6px 8px; border-right:1px solid #e1e6eb; }}
  .news-ctrl a {{ font-size:10px; border:1px solid #c8d3dd; border-radius:999px; padding:2px 8px; color:#4f6780; background:#edf2f6; }}
  .news-ctrl a.on {{ border-color:#ff7a59; color:#ffffff; background:#ff7a59; }}
  .news-marquee {{ position:relative; overflow:hidden; white-space:nowrap; }}
  .news-track {{ display:inline-block; padding:8px 12px; min-width:100%; animation: marquee 34s linear infinite; }}
  .news-track a, .news-track span {{ color:#33475b; font-size:12px; }}
  .news-track .sep {{ color:#9bb3c9; margin:0 10px; }}
  @keyframes marquee {{ 0% {{ transform: translateX(0); }} 100% {{ transform: translateX(-50%); }} }}
  .layout {{ display:grid; grid-template-columns:255px minmax(0,1fr) 320px; gap:10px; }}
  .col {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:10px; min-height:70vh; box-shadow:0 2px 5px rgba(0,0,0,0.05); }}
  .layout > aside.col {{ background:var(--panel); border-color:var(--line); color:var(--text); }}
  .layout > aside.col .title {{ color:#8ea9bc; }}
  .layout > aside.col .mini-hint,
  .layout > aside.col .muted {{ color:#516F90; }}
  .layout > aside.col a,
  .layout > aside.col .tk {{ color:#33475B; }}
  .title {{ font-size:11px; text-transform:uppercase; letter-spacing:.8px; color:#8ea9bc; margin-bottom:8px; }}
  .section-head {{ display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:4px; }}
  .section-head .title {{ margin:0; }}
  .expand-link {{ font-size:11px; color:#2f4358; padding:2px 7px; border:1px solid #c8d3dd; border-radius:999px; background:#edf2f6; text-transform:none; letter-spacing:0; white-space:nowrap; }}
  .mini-hint {{ font-size:11px; color:#516F90; margin-bottom:6px; }}
  .search input {{ width:100%; box-sizing:border-box; border-radius:8px; border:1px solid #c8d3dd; background:#edf2f6; color:var(--text); padding:10px; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }}
  textarea, select, .workbench input[type='text'] {{ width:100%; box-sizing:border-box; border-radius:8px; border:1px solid #9fbad0; background:#edf2f6; color:var(--text); padding:8px; }}
  button {{ border:1px solid #ff7a59; padding:6px 10px; border-radius:8px; background:#ff7a59; color:#ffffff; cursor:pointer; font-weight:700; }}
  .ticker {{ display:grid; grid-template-columns:56px 1fr 58px; gap:6px; align-items:center; padding:6px 8px; border-radius:8px; margin:4px 0; border:1px solid #c8dced; font-size:12px; background:#eef3f7; }}
  .ticker:hover {{ border-color:#9fbad0; background:#f0f7ff; }}
  .ticker .tk {{ font-weight:700; letter-spacing:.2px; }}
  .ticker .q {{ color:#516F90; text-align:right; font-size:11px; }}
  .ticker .d {{ text-align:right; font-size:11px; }}
  .ticker .d.up {{ color:#00BDA5; }}
  .ticker .d.down {{ color:#D93B59; }}
  .ticker .d.flat {{ color:#9fb2c4; }}
  .quick-add {{ margin:0 0 8px; }}
  .quick-line {{ display:grid; gap:4px; margin-bottom:4px; }}
  .quick-line.portfolio-top {{ grid-template-columns:minmax(0,1fr); }}
  .quick-line.portfolio-bottom {{ grid-template-columns:58px 74px 44px; }}
  .quick-line.watch {{ grid-template-columns:minmax(0,1fr) 44px; }}
  .quick-line input {{ width:100%; box-sizing:border-box; border-radius:7px; border:1px solid #9fbad0; background:#edf2f6; color:var(--text); padding:6px; font-size:11px; }}
  .quick-line button {{ padding:6px 0; border-radius:7px; }}
  .stack.lib-card {{ border:1px solid #c8dced; border-radius:10px; padding:8px; background:#edf2f6; }}
  .muted {{ color:var(--muted); }}
  .feed-card {{ background:#f1f5f8; border:1px solid #d6dee6; border-radius:8px; padding:10px; margin-bottom:8px; }}
  .feed-card h2,.feed-card h3 {{ margin:0 0 6px 0; }}
  .feed-card ul {{ margin:8px 0 0 18px; padding:0; }}
  .feed-card li {{ margin:4px 0; font-size:13px; }}
  .risk {{ border-color:#d93b59; background:#fff5f7; }}
  .redteam {{ border-color:#ffb24d; background:#fff8ee; }}
  .verdict {{ font-size:20px; font-weight:700; color:#33475b; margin:4px 0 8px; }}
  .verdict.red {{ color:#d93b59; }}
  .right-card {{ background:#f1f5f8; border:1px solid #d6dee6; border-radius:8px; padding:10px; }}
  .reg-list {{ margin:8px 0 0 16px; padding:0; }}
  .reg-list li {{ margin:8px 0; font-size:12px; line-height:1.35; }}
  .chip {{ display:inline-block; border:1px solid #b6d1e6; border-radius:999px; padding:1px 7px; font-size:10px; color:#355f7f; background:#edf5fc; }}
  .metric {{ display:flex; justify-content:space-between; gap:8px; padding:6px 0; border-bottom:1px solid #d7e4ee; }}
  .metric strong {{ color:#33475b; }}
  .metric-pulse {{ align-items:center; }}
  .metric-pulse .m-name {{ color:#33475b; font-size:12px; font-weight:700; }}
  .metric-pulse .m-right {{ display:flex; align-items:center; gap:8px; }}
  .metric-pulse .m-price {{ color:#1f2d3d; font-size:13px; font-weight:800; letter-spacing:.1px; }}
  .metric-pulse .m-day {{ display:inline-block; min-width:62px; text-align:center; border-radius:999px; padding:2px 8px; font-size:11px; font-weight:800; border:1px solid #b8c7d5; background:#f5f8fa; color:#486581; }}
  .metric-pulse .m-day.up {{ border-color:#00bda5; background:#e5f8f6; color:#008f7e; }}
  .metric-pulse .m-day.down {{ border-color:#d93b59; background:#fbeaef; color:#b62e4a; }}
  .metric-pulse .m-day.flat {{ border-color:#b8c7d5; background:#f5f8fa; color:#486581; }}
  .closed-note {{ margin:2px 0 6px; font-size:10px; line-height:1.2; color:#6B7F92; }}
  .market-weekend-note {{ margin:2px 0 6px; font-size:10px; line-height:1.2; color:#6B7F92; text-align:left; }}
  .focus-box {{ margin-top:8px; border:1px solid #c8dced; border-radius:8px; padding:8px; background:#edf2f6; }}
  .focus-title {{ font-size:11px; text-transform:uppercase; letter-spacing:.6px; color:#355f7f; margin-bottom:4px; }}
  .focus-box ul {{ margin:0 0 0 15px; padding:0; }}
  .focus-box li {{ margin:4px 0; color:#243b53; font-size:12px; }}
  .pm-box {{ margin:8px 0; }}
  .pm-line {{ color:#33475b; font-size:12px; line-height:1.35; font-weight:600; }}
  .pm-meta {{ margin-top:4px; color:#8fb3cf; font-size:11px; text-transform:uppercase; letter-spacing:.4px; }}
  .pm-detail {{ margin-top:6px; }}
  .pm-detail summary {{ cursor:pointer; color:#355f7f; font-size:11px; }}
  .chart {{ background:#eaf1f6; border:1px solid #c8dced; padding:8px; border-radius:8px; color:#17324a; overflow:auto; }}
  .actions {{ margin-top:8px; display:flex; flex-wrap:wrap; gap:8px; }}
  .actions a {{ border:1px solid #ff7a59; padding:6px 10px; border-radius:8px; background:#ff7a59; color:#ffffff; font-weight:700; }}
  .actions button {{ border:1px solid #ff7a59; padding:6px 10px; border-radius:8px; background:#ff7a59; color:#ffffff; font-weight:700; }}
  .stack {{ margin-bottom:12px; }}
  .workbench {{ margin-top:12px; border-top:1px solid #d7e4ee; padding-top:10px; }}
  .mini-btn {{ border:1px solid #9fbad0; background:#ffffff; color:#355f7f; border-radius:6px; font-size:11px; padding:1px 6px; cursor:pointer; }}
  .mini-btn.del-note {{ border-color:#d4a6af; color:#d93b59; background:#fff6f8; }}
  .chat-box {{ margin-top:10px; border-top:1px solid #d7e4ee; padding-top:10px; }}
  .chat-log {{ margin-top:8px; max-height:180px; overflow:auto; border:1px solid #c8dced; border-radius:8px; background:#edf2f6; padding:8px; }}
  .chat-item {{ border-bottom:1px dotted #d0e1ef; padding:5px 0; font-size:12px; line-height:1.35; color:#243b53; }}
  .chat-item:last-child {{ border-bottom:0; }}
  .chat-meta {{ color:#8fb3cf; font-size:11px; margin-bottom:2px; }}
  .chat-fab-wrap {{ position:fixed; right:14px; bottom:12px; width:360px; max-width:calc(100vw - 24px); z-index:9999; }}
  .chat-fab {{ background:#edf2f6; border:1px solid #d7e4ee; border-radius:12px; box-shadow:0 10px 24px rgba(31,58,86,.12); overflow:hidden; }}
  .chat-fab-head {{ display:flex; justify-content:space-between; align-items:center; gap:8px; padding:8px 10px; background:#f5f8fa; border-bottom:1px solid #d7e4ee; font-size:12px; color:#355f7f; }}
  .chat-fab-head .title {{ margin:0; color:#33475b; letter-spacing:.5px; }}
  .chat-fab-head button {{ padding:3px 8px; border-radius:6px; font-size:11px; }}
  .chat-fab-body {{ padding:8px; }}
  .chat-fab-body input,.chat-fab-body select {{ width:100%; box-sizing:border-box; border-radius:8px; border:1px solid #9fbad0; background:#edf2f6; color:var(--text); padding:7px; margin:4px 0; }}
  .chat-row {{ display:grid; grid-template-columns:1fr 1fr; gap:6px; }}
  .chat-actions {{ display:flex; gap:6px; margin-top:4px; }}
  .chat-actions button {{ flex:1; }}
  .chat-mini {{ position:fixed; right:14px; bottom:12px; z-index:9998; }}
  .chat-mini button {{ border:1px solid #ff7a59; border-radius:999px; background:#ff7a59; color:#ffffff; padding:8px 12px; font-size:12px; }}
  .deep-modal {{ position:fixed; inset:0; display:none; align-items:center; justify-content:center; background:rgba(30,47,66,.26); z-index:10030; }}
  .deep-modal.open {{ display:flex; }}
  .deep-modal-card {{ width:min(900px, calc(100vw - 28px)); max-height:calc(100vh - 28px); overflow:auto; background:#edf2f6; border:1px solid #d7e4ee; border-radius:12px; box-shadow:0 12px 28px rgba(31,58,86,.14); }}
  .deep-modal-head {{ display:flex; justify-content:space-between; align-items:center; gap:8px; padding:10px 12px; border-bottom:1px solid #d7e4ee; background:#f5f8fa; }}
  .deep-modal-head h3 {{ margin:0; font-size:14px; }}
  .deep-modal-head button {{ border:1px solid #9fbad0; padding:5px 9px; border-radius:7px; background:#ffffff; color:#355f7f; cursor:pointer; }}
  .deep-modal-body {{ padding:12px; }}
  .deep-status {{ color:#486581; font-size:12px; margin-bottom:8px; }}
  .deep-output {{ white-space:pre-wrap; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12px; line-height:1.4; border:1px solid #d7e4ee; border-radius:8px; padding:10px; background:#f8fbfe; color:#33475b; }}
  .deep-actions {{ margin-top:10px; display:flex; gap:8px; flex-wrap:wrap; }}
  .deep-actions a {{ border:1px solid #ff7a59; padding:6px 10px; border-radius:8px; background:#ff7a59; color:#ffffff; }}
  .thesis-item {{ border:1px solid #d7e4ee; border-radius:8px; padding:8px; margin-top:8px; background:#ffffff; }}
  .thesis-item pre {{ white-space:pre-wrap; font-family:inherit; margin:6px 0 0 0; }}
  .thesis-list {{ margin-top:8px; }}
  .row {{ margin:6px 0; }}
  .earnings-grid {{ margin-top:8px; display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); gap:8px; }}
  .earn-card {{ border:1px solid #d7e4ee; border-radius:10px; background:#ffffff; padding:8px; }}
  .earn-card header {{ display:flex; justify-content:space-between; align-items:flex-start; gap:6px; margin-bottom:6px; }}
  .earn-card .ticker {{ font-size:15px; font-weight:800; letter-spacing:.2px; }}
  .earn-card .when {{ font-size:11px; color:#486581; text-align:right; }}
  .earn-card .result .tag {{ display:inline-block; font-size:10px; padding:1px 6px; border-radius:999px; border:1px solid #b6d1e6; color:#355f7f; margin-bottom:4px; background:#edf5fc; }}
  .earn-card .headline {{ font-size:18px; font-weight:800; color:#33475b; line-height:1.1; }}
  .earn-card .sub {{ margin-top:4px; font-size:12px; color:#486581; line-height:1.25; }}
  .earn-card footer {{ margin-top:8px; padding-top:6px; border-top:1px solid #d7e4ee; display:flex; justify-content:space-between; align-items:center; color:#486581; font-size:12px; }}
  .earn-card footer a {{ border:1px solid #9fbad0; padding:3px 7px; border-radius:6px; background:#ffffff; color:#355f7f; font-size:12px; }}
  .earn-card.beat {{ border-color:#00bda5; background:#f0fffb; }}
  .earn-card.beat .tag {{ border-color:#00bda5; color:#008f7e; background:#e5f8f6; }}
  .earn-card.miss {{ border-color:#d93b59; background:#fff5f7; }}
  .earn-card.miss .tag {{ border-color:#d93b59; color:#b62e4a; background:#fbeaeF; }}
  .earn-card.upcoming,.earn-card.reported {{ border-color:#9fbad0; background:#ffffff; }}
  .earn-card.upcoming .tag,.earn-card.reported .tag {{ border-color:#b6d1e6; color:#355f7f; background:#edf5fc; }}
  .intel-item {{ border:1px solid #d7e4ee; border-radius:10px; background:#ffffff; padding:8px; margin-top:8px; }}
  .intel-item-top {{ display:flex; align-items:center; gap:7px; flex-wrap:wrap; margin-bottom:4px; }}
  .intel-item .chip {{ border:1px solid #b6d1e6; border-radius:999px; font-size:10px; padding:1px 7px; color:#355f7f; background:#edf5fc; }}
  .intel-item .sev {{ border:1px solid #d93b59; border-radius:999px; font-size:10px; padding:1px 7px; color:#b62e4a; background:#fbeaeF; }}
  .intel-item .intel-title {{ font-size:13px; line-height:1.35; color:#33475b; font-weight:700; margin-bottom:3px; }}
  .ei-list {{ margin-top:8px; display:grid; gap:8px; }}
  .ei-row {{ display:grid; grid-template-columns:2fr 2fr 1fr; gap:10px; align-items:center; border:1px solid #cfd8e1; border-radius:10px; background:#eaf1f6; padding:10px; }}
  .ei-left {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
  .ei-ticker {{ font-weight:800; font-size:15px; color:#1f3a52; }}
  .ei-date {{ font-size:11px; color:#486581; border:1px solid #c7d4df; background:#f2f6f9; border-radius:999px; padding:2px 8px; }}
  .ei-sector {{ font-size:11px; color:#355f7f; border:1px solid #b9d0e2; background:#edf5fc; border-radius:999px; padding:2px 8px; }}
  .ei-mid {{ min-width:0; }}
  .ei-eps {{ font-size:13px; font-weight:700; color:#2f4358; }}
  .ei-surprise {{ margin-top:2px; font-size:11px; color:#516f90; }}
  .ei-right {{ display:flex; justify-content:flex-end; align-items:center; gap:8px; }}
  .ei-verdict {{ display:inline-block; font-size:11px; font-weight:800; border-radius:999px; padding:2px 9px; border:1px solid #b9cad8; color:#486581; background:#f2f6f9; }}
  .ei-verdict.beat {{ border-color:#00bda5; color:#008f7e; background:#e5f8f6; }}
  .ei-verdict.miss {{ border-color:#d93b59; color:#b62e4a; background:#fbeaf0; }}
  .ei-open {{ border:1px solid #ff7a59; border-radius:8px; padding:4px 9px; background:#ff7a59; color:#fff; font-size:11px; font-weight:700; }}
  @media (max-width:980px) {{ .ei-row {{ grid-template-columns:1fr; }} .ei-right {{ justify-content:flex-start; }} }}
  @media (max-width:1200px) {{ .earnings-grid {{ grid-template-columns:repeat(2, minmax(0,1fr)); }} }}
  @media (max-width:900px) {{ .earnings-grid {{ grid-template-columns:1fr; }} }}
  @media (max-width:1080px) {{ .layout {{ grid-template-columns:1fr; }} .col {{ min-height:0; }} }}
</style></head>
<body><div class='shell'>
  <div class='top'><div><h1>Investment Intelligence Platform</h1><div class='muted'>Generated {generated} | Version {APP_VERSION}</div></div><div class='nav'>{top_nav}</div></div>
  {msg_html}
  {macro_banner_html}
  {news_ticker_html}
  <div class='layout'>
    <aside class='col'>
      <div class='title'>Library</div>
      <form class='search' method='get' action='/'><input name='t' placeholder='Load ticker or company (e.g. HUBS or Microsoft)' value='{html.escape(focus_ticker)}'></form>
      <div class='stack lib-card'>
        <div class='section-head'><div class='title'>My Portfolio</div><a class='expand-link' href='/universe?tab=portfolio'>View All</a></div>
        <div class='mini-hint'>All portfolio companies</div>
        {portfolio_closed_note_html}
        <form method='post' action='/portfolio/add' class='quick-add'>
          <input type='hidden' name='source' value='dashboard'>
          <input type='hidden' name='t' value='{html.escape(focus_ticker, quote=True)}'>
          <div class='quick-line portfolio-top'><input name='ticker' placeholder='Ticker (e.g. CRM)' required></div>
          <div class='quick-line portfolio-bottom'>
            <input name='shares' placeholder='Shares' value='1' required>
            <input name='cost_basis' placeholder='Cost'>
            <button type='submit'>Add</button>
          </div>
        </form>
        {pf_items}
      </div>
      <div class='stack lib-card'>
        <div class='section-head'><div class='title'>My Watchlist</div><a class='expand-link' href='/universe?tab=watchlist'>View All</a></div>
        <div class='mini-hint'>All watchlist companies</div>
        <form method='post' action='/watchlist/add' class='quick-add'>
          <input type='hidden' name='source' value='dashboard'>
          <input type='hidden' name='t' value='{html.escape(focus_ticker, quote=True)}'>
          <div class='quick-line watch'>
            <input name='ticker' placeholder='Ticker (e.g. ADBE)' required>
            <button type='submit'>Add</button>
          </div>
        </form>
        {wl_items}
      </div>
      <div class='stack'>
        <div class='title'>Company Files</div>
        <div class='mini-hint'>Notes, deep dive, to-do, timeline</div>
        <div class='actions'><a href='{ws_link}'>Open File</a></div>
      </div>
    </aside>
    <main class='col'>
      <div class='title'>Smart Feed</div>
      {feed_html}
    </main>
    <aside class='col'>
      <div class='title'>Evidence</div>
      {right_html}
    </aside>
  </div>
<div id='chatMini' class='chat-mini' style='display:none;'><button id='chatOpenBtn' type='button'>Ask AI</button></div>
<div id='chatFabWrap' class='chat-fab-wrap'>
  <div class='chat-fab'>
    <div class='chat-fab-head'><div class='title'>AI Assistant</div><button id='chatMinBtn' type='button'>Minimize</button></div>
    <div class='chat-fab-body'>
      <input id='chatQ' type='text' placeholder='Ask AI about portfolio, risk, catalysts...'>
      <div class='chat-row'>
        <select id='chatMode'>
          <option value='global' selected>global live</option>
          <option value='full'>full universe</option>
          <option value='portfolio'>portfolio only</option>
          <option value='watchlist'>watchlist only</option>
        </select>
        <select id='chatSpeed'>
          <option value='deep' selected>deep</option>
          <option value='quick'>quick</option>
        </select>
      </div>
      <div class='chat-row'>
        <select id='chatProvider'>
          <option value='auto' selected>provider: auto</option>
          <option value='openai'>provider: openai</option>
          <option value='ollama'>provider: ollama</option>
          <option value='anthropic'>provider: anthropic</option>
        </select>
        <select id='chatStyle'>
          <option value='analyst' selected>style: analyst</option>
          <option value='concise'>style: concise</option>
          <option value='deep'>style: deep-dive</option>
        </select>
      </div>
      <div class='chat-actions'><button id='chatAskBtn' type='button'>Ask AI</button><button id='chatClearBtn' type='button'>Clear</button></div>
      <div id='chatHealth' class='muted' style='margin-top:6px;'>Chat health: checking...</div>
      <div id='chatLog' class='chat-log'>{chat_html}</div>
    </div>
  </div>
</div>
<div id='deepDiveModal' class='deep-modal' aria-hidden='true'>
  <div class='deep-modal-card'>
    <div class='deep-modal-head'>
      <h3 id='deepDiveTitle'>Deep Dive</h3>
      <button id='deepDiveClose' type='button'>Close</button>
    </div>
    <div class='deep-modal-body'>
      <div id='deepDiveStatus' class='deep-status'></div>
      <pre id='deepDiveOutput' class='deep-output'></pre>
      <div class='deep-actions'>
        <a id='deepDiveOpenFull' href='#'>Open Full Report</a>
      </div>
    </div>
  </div>
</div>
<script>
  (function() {{
    var badge = document.getElementById('intelFeedBadge');
    var feedLink = document.getElementById('intelFeedLink');
    if (!badge) return;
    var seenKey = 'onyx_feed_seen_id';
    function toInt(v) {{
      var n = Number(v);
      return Number.isFinite(n) ? Math.max(0, Math.floor(n)) : 0;
    }}
    function markSeen(id) {{
      try {{
        localStorage.setItem(seenKey, String(toInt(id)));
      }} catch (_) {{}}
      badge.style.display = 'none';
    }}
    function getSeen() {{
      try {{
        return toInt(localStorage.getItem(seenKey) || '0');
      }} catch (_) {{
        return 0;
      }}
    }}
    function pollFeed() {{
      fetch('/api/feed?limit=1', {{ cache: 'no-store' }})
        .then(function(r) {{ return r.json(); }})
        .then(function(res) {{
          if (!res || !res.ok) return;
          var latestId = toInt(res.latest_id || 0);
          if (latestId <= 0) return;
          var seen = getSeen();
          if (seen <= 0) {{
            markSeen(latestId);
            return;
          }}
          if (latestId > seen) {{
            badge.style.display = 'inline-block';
          }} else {{
            badge.style.display = 'none';
          }}
        }})
        .catch(function() {{}});
    }}
    if (feedLink) {{
      feedLink.addEventListener('click', function() {{
        pollFeed();
        setTimeout(function() {{
          fetch('/api/feed?limit=1', {{ cache: 'no-store' }})
            .then(function(r) {{ return r.json(); }})
            .then(function(res) {{
              if (!res || !res.ok) return;
              markSeen(toInt(res.latest_id || 0));
            }})
            .catch(function() {{}});
        }}, 120);
      }});
    }}
    pollFeed();
    setInterval(pollFeed, 60000);
  }})();
  (function() {{
    var focusTicker = {json.dumps(focus_ticker)};
    if (!focusTicker) return;
    function esc(v) {{
      return String(v == null ? '' : v)
        .replace(/&/g,'&amp;')
        .replace(/</g,'&lt;')
        .replace(/>/g,'&gt;')
        .replace(/\"/g,'&quot;')
        .replace(/'/g,'&#39;');
    }}
    function showSecRiskPanel(htmlBody) {{
      var panel = document.getElementById('secRiskPanel');
      if (!panel) return;
      panel.style.display = 'block';
      panel.innerHTML = htmlBody;
    }}
    function showMdaDiffPanel(htmlBody) {{
      var panel = document.getElementById('mdaDiffPanel');
      if (!panel) return;
      panel.style.display = 'block';
      panel.innerHTML = htmlBody;
    }}
    function runSecRiskDiff(ticker) {{
      if (!ticker) return;
      showSecRiskPanel("<div class='focus-title'>SEC Risk Diff</div><div class='muted'>Analyzing latest two 10-K filings...</div>");
      fetch('/api/sec_risk_diff?t=' + encodeURIComponent(ticker), {{ cache: 'no-store' }})
        .then(function(r) {{ return r.json(); }})
        .then(function(res) {{
          if (!res || !res.ok) {{
            var detail = (res && (res.detail || res.error)) ? String(res.detail || res.error) : 'unknown_error';
            showSecRiskPanel("<div class='focus-title'>SEC Risk Diff</div><div class='muted'>Failed: " + esc(detail) + "</div>");
            return;
          }}
          var top = Array.isArray(res.top_3) ? res.top_3 : [];
          var rows = top.map(function(x, i) {{
            var sev = Number(x && x.severity_score || 0);
            var typ = String(x && x.change_type || '-');
            var txt = String(x && x.risk_text || '');
            var why = String(x && x.why_it_matters || '');
            return "<li><strong>#" + (i+1) + " | Sev " + esc(sev) + "/10</strong> (" + esc(typ) + ") " + esc(txt) + (why ? "<br><span class='muted'>Why: " + esc(why) + "</span>" : "") + "</li>";
          }}).join('');
          var extra = Number(res.additional_count || 0);
          var report = String(res.report_text || '');
          var block =
            "<div class='focus-title'>SEC Risk Diff</div>" +
            "<div class='muted'>Ticker: " + esc(String(res.ticker || ticker)) + " | diff sentences: " + esc(String(res.diff_count || 0)) + " | last run: " + esc(String(res.last_run || '-')) + "</div>" +
            (rows ? ("<ul>" + rows + "</ul>") : "<div class='muted'>No ranked changes returned.</div>") +
            (extra > 0 ? ("<div class='muted'>Additional scored risks: " + esc(String(extra)) + "</div>") : "") +
            (report ? ("<details style='margin-top:6px;'><summary class='muted'>Full AI Report</summary><pre class='chart' style='margin-top:6px;white-space:pre-wrap;'>" + esc(report) + "</pre></details>") : "");
          showSecRiskPanel(block);
        }})
        .catch(function(err) {{
          showSecRiskPanel("<div class='focus-title'>SEC Risk Diff</div><div class='muted'>Request failed: " + esc(String(err || 'request_failed')) + "</div>");
        }});
    }}
    function runMdaDiff(ticker) {{
      if (!ticker) return;
      showMdaDiffPanel("<div class='focus-title'>MD&A Intelligence</div><div class='muted'>Analyzing latest two 10-K MD&A sections...</div>");
      fetch('/api/mda_diff?t=' + encodeURIComponent(ticker), {{ cache: 'no-store' }})
        .then(function(r) {{ return r.json(); }})
        .then(function(res) {{
          if (!res || !res.ok) {{
            var detail = (res && (res.detail || res.error)) ? String(res.detail || res.error) : 'unknown_error';
            showMdaDiffPanel("<div class='focus-title'>MD&A Intelligence</div><div class='muted'>Failed: " + esc(detail) + "</div>");
            return;
          }}
          var top = Array.isArray(res.top_3) ? res.top_3 : [];
          var rows = top.map(function(x, i) {{
            var sev = Number(x && x.severity_score || 0);
            var typ = String(x && x.change_type || '-');
            var txt = String(x && x.risk_text || '');
            var why = String(x && x.why_it_matters || '');
            return "<li><strong>#" + (i+1) + " | Sev " + esc(sev) + "/10</strong> (" + esc(typ) + ") " + esc(txt) + (why ? "<br><span class='muted'>Why: " + esc(why) + "</span>" : "") + "</li>";
          }}).join('');
          var extra = Number(res.additional_count || 0);
          var report = String(res.report_text || '');
          var block =
            "<div class='focus-title'>MD&A Intelligence</div>" +
            "<div class='muted'>Ticker: " + esc(String(res.ticker || ticker)) + " | diff sentences: " + esc(String(res.diff_count || 0)) + " | last run: " + esc(String(res.last_run || '-')) + "</div>" +
            (rows ? ("<ul>" + rows + "</ul>") : "<div class='muted'>No ranked changes returned.</div>") +
            (extra > 0 ? ("<div class='muted'>Additional scored signals: " + esc(String(extra)) + "</div>") : "") +
            (report ? ("<details style='margin-top:6px;'><summary class='muted'>Full AI Report</summary><pre class='chart' style='margin-top:6px;white-space:pre-wrap;'>" + esc(report) + "</pre></details>") : "");
          showMdaDiffPanel(block);
        }})
        .catch(function(err) {{
          showMdaDiffPanel("<div class='focus-title'>MD&A Intelligence</div><div class='muted'>Request failed: " + esc(String(err || 'request_failed')) + "</div>");
        }});
    }}
    fetch('/api/evidence?t=' + encodeURIComponent(focusTicker))
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        if (!data || !data.ok || !data.evidence) return;
        var ev = data.evidence || {{}};
        var el = document.getElementById('lazy-evidence');
        if (!el) return;
        el.innerHTML =
          "<h3>Evidence: " + esc(focusTicker) + "</h3>" +
          "<div class='metric'><span>Price</span><strong>" + esc(ev.price || '-') + "</strong></div>" +
          "<div class='metric'><span>Day</span><strong>" + esc(ev.day || '-') + "</strong></div>" +
          "<div class='metric'><span>P/E</span><strong>" + esc(ev.pe || '-') + "</strong></div>" +
          "<div class='metric'><span>Market Cap</span><strong>" + esc(ev.mcap || '-') + "</strong></div>" +
          "<div class='metric'><span>Data Quality</span><strong>" + esc(ev.quality || '-') + "</strong></div>" +
          "<div class='muted'>" + esc(ev.quality_note || '') + "</div>" +
          "<div class='muted'>Source: " + esc(ev.source || '-') + "</div>" +
          "<hr style='border:0;border-top:1px solid #213749;margin:10px 0;'>" +
          "<div class='metric'><span>SEC Latest</span><strong>" + esc(ev.sec_latest || '-') + "</strong></div>" +
          "<div class='metric'><span>SEC Previous</span><strong>" + esc(ev.sec_prev || '-') + "</strong></div>" +
          "<div class='metric'><span>SEC Shares Delta</span><strong>" + esc(ev.sec_dilution || '-') + "</strong></div>" +
          "<div class='muted'>SEC verifies business deterioration inputs (dilution/leverage/margins), not live price ticks.</div>" +
          "<div class='muted'>Mini-chart (1M)</div><pre class='chart'>" + esc(ev.chart || '-') + "</pre>" +
          "<div class='muted'>As of " + esc(ev.asof || '-') + "</div>" +
          "<div class='actions'><a href='/company?t=" + encodeURIComponent(focusTicker) + "'>Company</a><a href='/company?t=" + encodeURIComponent(focusTicker) + "'>SEC Filings</a><a href='/company?t=" + encodeURIComponent(focusTicker) + "'>Open Detail</a></div>";
      }})
      .catch(function() {{}});
    document.addEventListener('click', function(e) {{
      var b = e.target && e.target.closest ? e.target.closest('a.sec-risk-btn') : null;
      if (b) {{
        e.preventDefault();
        var t = (b.getAttribute('data-ticker') || focusTicker || '').toUpperCase();
        runSecRiskDiff(t);
        return;
      }}
      var m = e.target && e.target.closest ? e.target.closest('a.mda-diff-btn') : null;
      if (m) {{
        e.preventDefault();
        var tm = (m.getAttribute('data-ticker') || focusTicker || '').toUpperCase();
        runMdaDiff(tm);
        return;
      }}
    }});
  }})();
  (function() {{
    var q = document.getElementById('chatQ');
    var mode = document.getElementById('chatMode');
    var speed = document.getElementById('chatSpeed');
    var provider = document.getElementById('chatProvider');
    var style = document.getElementById('chatStyle');
    var askBtn = document.getElementById('chatAskBtn');
    var clearBtn = document.getElementById('chatClearBtn');
    var health = document.getElementById('chatHealth');
    var log = document.getElementById('chatLog');
    var wrap = document.getElementById('chatFabWrap');
    var mini = document.getElementById('chatMini');
    var minBtn = document.getElementById('chatMinBtn');
    var openBtn = document.getElementById('chatOpenBtn');
    if (minBtn && openBtn && wrap && mini) {{
      var key = 'onyx_chat_minimized';
      function setMinimized(v) {{
        if (v) {{
          wrap.style.display = 'none';
          mini.style.display = 'block';
          try {{ localStorage.setItem(key, '1'); }} catch (_) {{}}
        }} else {{
          mini.style.display = 'none';
          wrap.style.display = 'block';
          try {{ localStorage.setItem(key, '0'); }} catch (_) {{}}
          if (q) q.focus();
        }}
      }}
      // Default: minimized unless user explicitly opened it before.
      var saved = '1';
      try {{
        var s = localStorage.getItem(key);
        if (s === '0' || s === '1') saved = s;
      }} catch (_) {{}}
      setMinimized(saved !== '0');
      minBtn.addEventListener('click', function() {{ setMinimized(true); }});
      openBtn.addEventListener('click', function() {{ setMinimized(false); }});
    }}
    function esc(v) {{
      return String(v == null ? '' : v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }}
    function appendItem(item) {{
      if (!log || !item) return;
      var row = document.createElement('div');
      row.className = 'chat-item';
      row.innerHTML =
        "<div class='chat-meta'>" + esc(item.ts || '') + " | " + esc(item.mode || 'full') + " | " + esc(item.speed || 'quick') + " | " + esc(item.provider || '-') + " " + esc(item.model || '') + "</div>" +
        "<div><strong>Q:</strong> " + esc(item.q || '') + "</div>" +
        "<div><strong>A:</strong> " + esc(item.a || '') + "</div>" +
        "<div class='muted'>Sources: " + esc(item.sources || '-') + "</div>";
      log.insertBefore(row, log.firstChild);
    }}
    function setHealth(txt) {{
      if (health) health.textContent = txt;
    }}
    function refreshHealth() {{
      fetch('/api/chat_health', {{ cache: 'no-store' }})
        .then(function(r) {{ return r.json(); }})
        .then(function(h) {{
          if (!h || !h.ok) {{
            setHealth('Chat health: FAIL');
            return;
          }}
          var parts = [];
          parts.push('api: ' + (h.api || 'fail'));
          parts.push('llm: ' + (h.llm_engine_loaded ? 'ok' : 'missing'));
          parts.push('openai-key: ' + (h.openai_key_set ? 'yes' : 'no'));
          parts.push('ollama: ' + (h.ollama_up ? 'up' : 'down'));
          setHealth('Chat health: ' + parts.join(' | '));
        }})
        .catch(function() {{ setHealth('Chat health: FAIL'); }});
    }}
    function postChat(payload, cb) {{
      var body = new URLSearchParams(payload);
      var controller = new AbortController();
      var timer = setTimeout(function() {{
        try {{ controller.abort(); }} catch (_) {{}}
      }}, 25000);
      fetch('/api/chat', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
        body: body.toString(),
        signal: controller.signal
      }})
      .then(function(r) {{
        clearTimeout(timer);
        if (!r.ok) throw new Error('http_' + r.status);
        return r.text();
      }})
      .then(function(txt) {{
        try {{
          cb(JSON.parse(txt || '{{}}'));
        }} catch (e) {{
          cb({{ok:false,error:'bad_json',raw:(txt || '').slice(0,300)}});
        }}
      }})
      .catch(function(err) {{
        clearTimeout(timer);
        var em = String(err && err.message ? err.message : 'request_failed');
        if (em === 'The operation was aborted.' || em === 'AbortError') em = 'request_timeout_25s';
        cb({{ok:false,error:em}});
      }});
    }}
    var asking = false;
    function submitAsk() {{
      if (!askBtn || asking) return;
      var qq = (q && q.value || '').trim();
      if (!qq) return;
      asking = true;
      setHealth('Chat health: sending...');
      askBtn.disabled = true;
      askBtn.textContent = 'Thinking...';
      postChat({{
        action:'ask',
        question:qq,
        mode:(mode && mode.value || 'full'),
        speed:(speed && speed.value || 'quick'),
        provider:(provider && provider.value || 'auto'),
        style:(style && style.value || 'analyst')
      }}, function(res) {{
        asking = false;
        askBtn.disabled = false;
        askBtn.textContent = 'Ask AI';
        if (res && res.ok && res.item) {{
          appendItem(res.item);
          if (q) q.value = '';
          refreshHealth();
          return;
        }}
        appendItem({{
          ts: new Date().toISOString().slice(0,19).replace('T',' '),
          mode:(mode && mode.value || 'full'),
          speed:(speed && speed.value || 'quick'),
          provider:(provider && provider.value || 'auto'),
          model:'-',
          q: qq,
          a: 'Chat request failed: ' + ((res && (res.error || res.raw)) ? String(res.error || res.raw) : 'unknown_error'),
          sources: '-'
        }});
        refreshHealth();
      }});
    }}
    if (askBtn) {{
      askBtn.addEventListener('click', submitAsk);
    }}
    if (clearBtn) {{
      clearBtn.addEventListener('click', function() {{
        postChat({{action:'clear'}}, function(res) {{
          if (res && res.ok && log) log.innerHTML = "<div class='muted'>No AI chat yet.</div>";
        }});
      }});
    }}
    if (q) {{
      q.addEventListener('keydown', function(e) {{
        if (e.key === 'Enter' || e.code === 'NumpadEnter') {{
          e.preventDefault();
          e.stopPropagation();
          submitAsk();
        }}
      }});
    }}
    refreshHealth();
    setInterval(refreshHealth, 30000);
  }})();
  (function() {{
    var modal = document.getElementById('deepDiveModal');
    var closeBtn = document.getElementById('deepDiveClose');
    var title = document.getElementById('deepDiveTitle');
    var status = document.getElementById('deepDiveStatus');
    var output = document.getElementById('deepDiveOutput');
    var openFull = document.getElementById('deepDiveOpenFull');
    function esc(v) {{
      return String(v == null ? '' : v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }}
    function setOpen(isOpen) {{
      if (!modal) return;
      if (isOpen) {{
        modal.classList.add('open');
        modal.setAttribute('aria-hidden', 'false');
      }} else {{
        modal.classList.remove('open');
        modal.setAttribute('aria-hidden', 'true');
      }}
    }}
    if (closeBtn) closeBtn.addEventListener('click', function() {{ setOpen(false); }});
    if (modal) {{
      modal.addEventListener('click', function(e) {{
        if (e.target === modal) setOpen(false);
      }});
    }}
    function parseTickerFromLink(a) {{
      var t = (a && a.getAttribute('data-ticker')) || '';
      if (t) return t.toUpperCase();
      var href = (a && a.getAttribute('href')) || '';
      try {{
        var u = new URL(href, window.location.origin);
        var p = u.searchParams.get('t') || '';
        if (p) return p.toUpperCase();
      }} catch (_) {{}}
      return '';
    }}
    function runDeepDive(ticker) {{
      if (!ticker) return;
      window.location.href = '/company?t=' + encodeURIComponent(ticker);
    }}
    document.addEventListener('click', function(e) {{
      var a = e.target && e.target.closest ? e.target.closest('a') : null;
      if (!a) return;
      var href = a.getAttribute('href') || '';
      var tagged = a.classList.contains('deep-dive-trigger');
      var deepLink = href.indexOf('/company?t=') === 0;
      if (!tagged && !deepLink) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      var ticker = parseTickerFromLink(a);
      if (!ticker) return;
      e.preventDefault();
      runDeepDive(ticker);
    }});
  }})();
</script>
{quick_capture_html}
</div></body></html>"""


def _dashboard_home_cache_key(
    earnings_show_all: bool,
    news_mode: str,
    reg_mode: str,
    deep_mode: bool,
) -> str:
    return (
        "home_dashboard:"
        f"earn={1 if bool(earnings_show_all) else 0}:"
        f"nw={str(news_mode or 'general').strip().lower()}:"
        f"reg={str(reg_mode or 'portfolio').strip().lower()}:"
        f"deep={1 if bool(deep_mode) else 0}"
    )


def _refresh_home_dashboard_cache_worker(
    key: str,
    earnings_show_all: bool,
    news_mode: str,
    reg_mode: str,
    deep_mode: bool,
) -> None:
    try:
        t0 = time.perf_counter()
        html_doc = dashboard_html(
            focus_ticker="",
            earnings_show_all=earnings_show_all,
            news_mode=news_mode,
            reg_mode=reg_mode,
            deep_mode=deep_mode,
        )
        render_ms = int((time.perf_counter() - t0) * 1000.0)
        with LOCK:
            HOME_DASHBOARD_CACHE[key] = {"ts": time.time(), "html": html_doc, "render_ms": render_ms}
    finally:
        with LOCK:
            HOME_DASHBOARD_REFRESH[key] = False


def dashboard_home_html_cached(
    earnings_show_all: bool,
    news_mode: str,
    reg_mode: str,
    deep_mode: bool,
    ttl_seconds: int = 20,
    return_meta: bool = False,
) -> str | tuple[str, dict[str, object]]:
    key = _dashboard_home_cache_key(
        earnings_show_all=earnings_show_all,
        news_mode=news_mode,
        reg_mode=reg_mode,
        deep_mode=deep_mode,
    )
    now = time.time()
    with LOCK:
        row = HOME_DASHBOARD_CACHE.get(key, {})
        html_doc = str(row.get("html") or "")
        ts = float(row.get("ts") or 0.0)
        render_ms = int(row.get("render_ms") or 0)
        age = now - ts if ts > 0 else 10_000.0
        if html_doc and age <= max(2, int(ttl_seconds)):
            meta = {"source": "cache_fresh", "age_ms": int(age * 1000.0), "render_ms": render_ms, "cached_at": ts}
            return (html_doc, meta) if return_meta else html_doc
        if html_doc:
            if not bool(HOME_DASHBOARD_REFRESH.get(key, False)):
                HOME_DASHBOARD_REFRESH[key] = True
                th = threading.Thread(
                    target=_refresh_home_dashboard_cache_worker,
                    args=(key, earnings_show_all, news_mode, reg_mode, deep_mode),
                    daemon=True,
                )
                th.start()
            meta = {"source": "cache_stale", "age_ms": int(age * 1000.0), "render_ms": render_ms, "cached_at": ts}
            return (html_doc, meta) if return_meta else html_doc
    # Cold miss: render once synchronously.
    t0 = time.perf_counter()
    fresh = dashboard_html(
        focus_ticker="",
        earnings_show_all=earnings_show_all,
        news_mode=news_mode,
        reg_mode=reg_mode,
        deep_mode=deep_mode,
    )
    render_ms = int((time.perf_counter() - t0) * 1000.0)
    ts_now = time.time()
    with LOCK:
        HOME_DASHBOARD_CACHE[key] = {"ts": ts_now, "html": fresh, "render_ms": render_ms}
    meta = {"source": "cold_render", "age_ms": 0, "render_ms": render_ms, "cached_at": ts_now}
    return (fresh, meta) if return_meta else fresh


def _inject_home_perf_badge(html_doc: str, meta: dict[str, object]) -> str:
    if not html_doc:
        return html_doc
    source = str(meta.get("source") or "-")
    age_ms = int(meta.get("age_ms") or 0)
    render_ms = int(meta.get("render_ms") or 0)
    cached_at = float(meta.get("cached_at") or 0.0)
    cached_txt = dt.datetime.fromtimestamp(cached_at).strftime("%H:%M:%S") if cached_at > 0 else "-"
    badge = (
        "<div style='position:fixed;right:10px;bottom:10px;z-index:10000;"
        "background:rgba(16,24,32,.90);color:#d7e4ee;border:1px solid #3a5368;"
        "border-radius:8px;padding:6px 8px;font:12px/1.3 ui-monospace,Menlo,Consolas,monospace;'>"
        f"home_perf src={html.escape(source)} age={age_ms}ms render={render_ms}ms cached={html.escape(cached_txt)}"
        "</div>"
    )
    if "</body>" in html_doc:
        return html_doc.replace("</body>", badge + "</body>", 1)
    return html_doc + badge


def investment_workspace_app_html(error_message: str = "") -> str:
    index_file = FRONTEND_DIST / "index.html"
    if index_file.exists():
        raw = index_file.read_text(encoding="utf-8", errors="ignore")
        # Rebase Vite asset URLs to this app route.
        themed = (
            raw.replace('src="/assets/', 'src="/investment_workspace_assets/')
            .replace('href="/assets/', 'href="/investment_workspace_assets/')
        )
        # Workspace-specific readability overrides (top bar + side panels).
        ws_css = """
<style id="workspace-light-override">
  body { background:#eaf0f4 !important; color:#2f4358 !important; }
  .workspace-root, .workspace-root main, .workspace-root section { background:transparent !important; color:#33475b !important; }
  .workspace-root header { background:#edf2f6 !important; border-bottom:1px solid #d3dce5 !important; color:#2f4358 !important; }
  .workspace-root aside { background:#eaf1f6 !important; border-right:1px solid #d3dce5 !important; color:#2f4358 !important; }
  .workspace-root .bg-black\\/20,
  .workspace-root .bg-black\\/30,
  .workspace-root .bg-black\\/40,
  .workspace-root .bg-black\\/80,
  .workspace-root .bg-white\\/5,
  .workspace-root .bg-white\\/10 { background:#edf2f6 !important; color:#2f4358 !important; }
  .workspace-root .border-white\\/10,
  .workspace-root .border-white\\/15,
  .workspace-root .border-white\\/20 { border-color:#d3dce5 !important; }
  .workspace-root .text-white,
  .workspace-root .text-neutral-100,
  .workspace-root .text-neutral-200,
  .workspace-root .text-neutral-300,
  .workspace-root .text-blue-200,
  .workspace-root .text-blue-300,
  .workspace-root .text-amber-300,
  .workspace-root .text-emerald-300 { color:#2f4358 !important; }
  .workspace-root .text-neutral-400,
  .workspace-root .text-neutral-500 { color:#4f6780 !important; }
  .workspace-root input, .workspace-root textarea, .workspace-root select {
    background:#edf2f6 !important; color:#2f4358 !important; border:1px solid #c8d6e2 !important;
  }
  /* Prevent the left workspace rail from getting stuck collapsed on notes tab. */
  .workspace-root aside.h-full.border-r.w-16 { width:16rem !important; }
  .workspace-root aside.h-full.border-r { min-width:16rem; }
  .ws-home-link {
    position: fixed;
    top: 56px;
    left: 12px;
    z-index: 99999;
    padding: 6px 10px;
    border-radius: 8px;
    border: 1px solid #d3dce5;
    background: #edf2f6;
    color: #2f4358;
    font-size: 12px;
    font-weight: 700;
    text-decoration: none;
    box-shadow: 0 2px 8px rgba(0,0,0,0.08);
  }
  .ws-home-link:hover { background:#e6edf3; }
</style>
<script id="workspace-sidebar-open-fix">
  (function () {
    try {
      localStorage.setItem("onyx.workspace.sidebar_open.v4", "1");
    } catch (_) {}
    function addHomeLink() {
      if (document.getElementById("wsHomeLink")) return;
      var a = document.createElement("a");
      a.id = "wsHomeLink";
      a.className = "ws-home-link";
      a.href = "/";
      a.textContent = "← Home";
      document.body.appendChild(a);
    }
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", addHomeLink);
    } else {
      addHomeLink();
    }
  })();
</script>
"""
        if "</head>" in themed and "workspace-light-override" not in themed:
            themed = themed.replace("</head>", ws_css + "\n</head>")
        return themed
    return dashboard_html(
        error_message
        or "Investment Workspace bundle not found. Build it with: "
        "cd frontend && npm install && npm run build"
    )


def _inject_classic_onyx_theme(content: str) -> str:
    if not content or 'id="onyx-classic-theme"' in content:
        return content
    css = """
<style id="onyx-classic-theme">
  :root{
    --onyx-bg:#0b1014;
    --onyx-panel:#131d25;
    --onyx-line:#294051;
    --onyx-text:#e7eef6;
    --onyx-muted:#9ab0c0;
    --onyx-link:#9fd3ff;
    --onyx-accent:#1a3d56;
    --onyx-accent-line:#2e5c7b;
    --onyx-up:#00bda5;
    --onyx-down:#d93b59;
  }
  html,body,#root{
    background:var(--onyx-bg) !important;
    color:var(--onyx-text) !important;
    font-family:"Avenir Next","Helvetica Neue",sans-serif !important;
  }
  a{ color:var(--onyx-link) !important; }
  body, .workspace-root, .workspace-root header, .workspace-root aside, .workspace-root main, .workspace-root section{
    background:var(--onyx-bg) !important;
    color:var(--onyx-text) !important;
  }
  .shell,.workspace-root{ background:transparent !important; color:var(--onyx-text) !important; }
  .top{ border-bottom-color:var(--onyx-line) !important; }
  .col,.card,.feed-card,.right-card,.focus-box,.chat-fab,.deep-modal-card,.thesis-item,.earn-card,.intel-item,.stack.lib-card,.news-wire,.chat-log,.chart,.paper,.kpi{
    background:var(--onyx-panel) !important;
    border:1px solid var(--onyx-line) !important;
    color:var(--onyx-text) !important;
    box-shadow:none !important;
  }
  .layout > aside.col,.workspace-root aside,.workspace-root header{
    background:var(--onyx-panel) !important;
    border-color:var(--onyx-line) !important;
  }
  .workspace-root [class*="bg-white"],
  .workspace-root [class*="bg-black/"],
  .workspace-root [class*="bg-neutral-"],
  .workspace-root [class*="bg-blue-"],
  .workspace-root [class*="bg-emerald-"],
  .workspace-root [class*="bg-amber-"],
  .workspace-root [class*="bg-red-"]{
    background:var(--onyx-panel) !important;
    color:var(--onyx-text) !important;
  }
  .workspace-root [class*="border-white"],
  .workspace-root [class*="border-neutral-"],
  .workspace-root [class*="border-blue-"],
  .workspace-root [class*="border-emerald-"],
  .workspace-root [class*="border-amber-"],
  .workspace-root [class*="border-red-"]{
    border-color:var(--onyx-line) !important;
  }
  .title,.section-title,.focus-title,h1,h2,h3,h4,h5,h6,strong{ color:var(--onyx-text) !important; }
  .muted,.mini-hint,.chat-meta,.pm-meta,.workspace-root .text-neutral-400,.workspace-root .text-neutral-500{ color:var(--onyx-muted) !important; }
  .workspace-root .text-neutral-100,
  .workspace-root .text-neutral-200,
  .workspace-root .text-neutral-300,
  .workspace-root .text-white,
  .workspace-root .text-blue-200,
  .workspace-root .text-blue-300,
  .workspace-root .text-amber-300,
  .workspace-root .text-emerald-300{
    color:var(--onyx-text) !important;
  }
  button,.btn,.actions a,.actions button,.top .nav a,.chat-mini button,.deep-actions a,.workspace-root button{
    background:var(--onyx-accent) !important;
    border:1px solid var(--onyx-accent-line) !important;
    color:var(--onyx-text) !important;
    border-radius:8px !important;
    box-shadow:none !important;
  }
  input,textarea,select,.workspace-root input,.workspace-root textarea,.workspace-root select{
    background:#0f1a23 !important;
    color:var(--onyx-text) !important;
    border:1px solid var(--onyx-line) !important;
  }
  .chip,.intel-badge,.tag,.workspace-root .ws-tab.ws-tab-active{
    background:#133248 !important;
    border:1px solid #3f6a86 !important;
    color:#cfe7f7 !important;
  }
  .workspace-root .ws-tab{
    background:#0f1a23 !important;
    border:1px solid var(--onyx-line) !important;
    color:var(--onyx-text) !important;
  }
  .warn-badge,.risk,.sev,.earn-card.miss .tag,.verdict.red{ color:var(--onyx-down) !important; border-color:var(--onyx-down) !important; }
  .ticker .d.up,.metric-pulse .m-day.up,.pct-text.up{ color:var(--onyx-up) !important; }
  .ticker .d.down,.metric-pulse .m-day.down,.pct-text.down{ color:var(--onyx-down) !important; }
</style>
"""
    if "</head>" in content:
        return content.replace("</head>", css + "</head>", 1)
    return css + content


def run_job(job: str) -> None:
    cmds = JOBS.get(job, [])
    if not cmds:
        return
    with LOCK:
        if RUN_STATE[job]["running"]:
            return
        RUN_STATE[job]["running"] = True
        RUN_STATE[job]["last"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        RUN_STATE[job]["result"] = "running"
    log_path = LOGS / str(RUN_STATE[job]["log"])
    LOGS.mkdir(parents=True, exist_ok=True)
    ok = True
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"\n=== {dt.datetime.now().isoformat()} {job} ===\n")
        for cmd in cmds:
            p = subprocess.run(cmd, shell=True, cwd=str(ROOT), stdout=fh, stderr=fh)
            if p.returncode != 0:
                ok = False
                break
    with LOCK:
        RUN_STATE[job]["running"] = False
        RUN_STATE[job]["result"] = "ok" if ok else "failed"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/workspace/"):
            body = json.dumps(
                {"ok": False, "error": "workspace_retired", "redirect": "/company_file"},
                ensure_ascii=True,
            ).encode("utf-8")
            self.send_response(410)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path.startswith("/investment_workspace_assets/"):
            rel = parsed.path.replace("/investment_workspace_assets/", "", 1)
            rel_path = Path(rel)
            if ".." in rel_path.parts or rel_path.is_absolute():
                self.send_response(400)
                self.end_headers()
                return
            target = FRONTEND_DIST / "assets" / rel_path
            if not target.exists() or not target.is_file():
                self.send_response(404)
                self.end_headers()
                return
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "public, max-age=3600")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/api/focus_cards":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or [""])[0])
            if not ticker:
                data = {"ok": False, "error": "ticker_required"}
            else:
                cards = get_focus_cards(ticker, ttl_seconds=900)
                data = {"ok": True, "ticker": ticker, "cards": cards}
            body = json.dumps(data, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/evidence":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or [""])[0])
            if not ticker:
                data = {"ok": False, "error": "ticker_required"}
            else:
                ev = get_ticker_evidence_fast(ticker, ttl_seconds=240)
                data = {"ok": True, "ticker": ticker, "evidence": ev}
            body = json.dumps(data, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/chat_health":
            data = chat_health_snapshot()
            body = json.dumps(data, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/sec_risk_diff":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or qs.get("ticker") or [""])[0])
            if not ticker:
                data = {"ok": False, "error": "ticker_required"}
            else:
                data = perform_sec_risk_diff(ticker)
            body = json.dumps(data, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/mda_diff":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or qs.get("ticker") or [""])[0])
            if not ticker:
                data = {"ok": False, "error": "ticker_required"}
            else:
                data = perform_mda_diff(ticker)
            body = json.dumps(data, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/feed":
            qs = parse_qs(parsed.query)
            try:
                limit = int((qs.get("limit") or ["10"])[0])
            except Exception:
                limit = 10
            rows = list_intel_feed(limit=max(1, min(50, limit)))
            items: list[dict[str, object]] = []
            for r in rows:
                items.append(
                    {
                        "id": int(r["id"]),
                        "created_at": str(r["created_at"] or ""),
                        "ticker": str(r["ticker"] or ""),
                        "category": str(r["category"] or ""),
                        "title": str(r["title"] or ""),
                        "summary": str(r["summary"] or ""),
                        "detail": str(r["detail"] or ""),
                        "severity": _to_int(r["severity"], 0),
                        "source": str(r["source"] or ""),
                        "model": str(r["model"] or ""),
                    }
                )
            data = {"ok": True, "latest_id": (items[0]["id"] if items else 0), "count": len(items), "items": items}
            body = json.dumps(data, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/workspace/companies":
            rows = list_workspace_companies(limit=600)
            tickers = [str(r["ticker"] or "").strip().upper() for r in rows if str(r["ticker"] or "").strip()]
            quotes = get_live_quotes(tickers, ttl_seconds=120) if tickers else {}
            snap = load_intel24_snapshot_map(max_age_seconds=3600)
            profiles = list_workspace_profiles_map(tickers)
            vals = list_workspace_valuation_map(tickers)
            items: list[dict[str, object]] = []
            for r in rows:
                t = str(r["ticker"] or "").strip().upper()
                q = quotes.get(t, {}) if isinstance(quotes, dict) else {}
                s = snap.get(t, {}) if isinstance(snap, dict) else {}
                p = profiles.get(t, {}) if isinstance(profiles, dict) else {}
                v = vals.get(t, {}) if isinstance(vals, dict) else {}
                stale_days = _workspace_stale_days(str(r["updated_at"] or ""), str(p.get("updated_at") or ""))
                stale_level = "fresh"
                if stale_days >= 90:
                    stale_level = "critical"
                elif stale_days >= 60:
                    stale_level = "warning"
                px = (float(q["price"]) if isinstance(q.get("price"), float) else None)
                strike_alert = ""
                starter = _safe_float(v.get("strike_starter"))
                add_px = _safe_float(v.get("strike_add"))
                aggr = _safe_float(v.get("strike_aggressive"))
                if isinstance(px, float):
                    if isinstance(aggr, float) and px <= aggr:
                        strike_alert = "aggressive"
                    elif isinstance(add_px, float) and px <= add_px:
                        strike_alert = "add"
                    elif isinstance(starter, float) and px <= starter:
                        strike_alert = "starter"
                items.append(
                    {
                        "ticker": t,
                        "stage": str(r["stage"] or "Inbox"),
                        "conviction": _to_int(r["conviction"], 6),
                        "thesis": str(r["thesis"] or ""),
                        "updated_at": str(r["updated_at"] or ""),
                        "price": px,
                        "day_pct": (float(q["day_pct"]) if isinstance(q.get("day_pct"), float) else None),
                        "event_score": _to_int(s.get("event_score"), 0),
                        "suggestion": str(s.get("suggestion") or ""),
                        "stale_days": stale_days,
                        "stale_level": stale_level,
                        "strike_alert": strike_alert,
                    }
                )
            body = json.dumps({"ok": True, "count": len(items), "items": items}, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/workspace/profile":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or qs.get("ticker") or [""])[0])
            if not ticker:
                data = {"ok": False, "error": "ticker_required"}
            else:
                prof = get_workspace_profile(ticker)
                data = {"ok": True, "ticker": ticker, "profile": prof}
            body = json.dumps(data, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/workspace/snapshots":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or qs.get("ticker") or [""])[0])
            if not ticker:
                data = {"ok": False, "error": "ticker_required", "items": []}
            else:
                rows = list_workspace_thesis_snapshots(ticker, limit=36)
                data = {
                    "ok": True,
                    "ticker": ticker,
                    "items": [
                        {
                            "id": int(r["id"]),
                            "snapshot_month": str(r["snapshot_month"] or ""),
                            "score_total": _to_int(r["score_total"], 0),
                            "thesis": str(r["thesis"] or ""),
                            "why_wrong": str(r["why_wrong"] or ""),
                            "created_at": str(r["created_at"] or ""),
                        }
                        for r in rows
                    ],
                }
            body = json.dumps(data, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/workspace/mortems":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or qs.get("ticker") or [""])[0])
            if not ticker:
                data = {"ok": False, "error": "ticker_required", "items": []}
            else:
                rows = list_workspace_mortems(ticker, limit=100)
                data = {
                    "ok": True,
                    "ticker": ticker,
                    "items": [
                        {
                            "id": int(r["id"]),
                            "ticker": str(r["ticker"] or ""),
                            "mortem_type": str(r["mortem_type"] or ""),
                            "trigger_txt": str(r["trigger_txt"] or ""),
                            "hypothesis": str(r["hypothesis"] or ""),
                            "outcome": str(r["outcome"] or ""),
                            "lessons": str(r["lessons"] or ""),
                            "created_at": str(r["created_at"] or ""),
                        }
                        for r in rows
                    ],
                }
            body = json.dumps(data, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/workspace/weekly_review":
            qs = parse_qs(parsed.query)
            try:
                days = int((qs.get("days") or ["7"])[0])
            except Exception:
                days = 7
            items = workspace_weekly_review(days=days, limit=40)
            data = {"ok": True, "days": max(1, min(30, int(days))), "items": items}
            body = json.dumps(data, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/workspace/history":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or qs.get("ticker") or [""])[0])
            scope = ((qs.get("scope") or ["all"])[0] or "all").strip().lower()
            try:
                limit = int((qs.get("limit") or ["200"])[0])
            except Exception:
                limit = 200
            lim = max(20, min(800, int(limit)))
            notes_rows = list_investor_notes(limit=lim, ticker=(ticker if scope == "ticker" and ticker else ""))
            if scope == "ticker" and ticker:
                j_rows = list_workspace_journal(ticker=ticker, limit=lim)
            else:
                j_rows = list_workspace_journal_all(limit=lim)
            done_todos = [r for r in list_todos(limit=500) if str(r["status"] or "").lower() == "done"][:80]
            data = {
                "ok": True,
                "scope": scope,
                "ticker": ticker,
                "notes": [
                    {
                        "id": int(r["id"]),
                        "scope": str(r["scope"] or ""),
                        "ticker": str(r["ticker"] or ""),
                        "sentiment": str(r["sentiment"] or ""),
                        "note": str(r["note"] or ""),
                        "tags": str(r["tags"] or ""),
                        "created_at": str(r["created_at"] or ""),
                    }
                    for r in notes_rows
                ],
                "journal": [
                    {
                        "id": int(r["id"]),
                        "ticker": str(r["ticker"] or ""),
                        "action": str(r["action"] or "Note"),
                        "emotion": str(r["emotion"] or "Calm"),
                        "note": str(r["note"] or ""),
                        "date": str(r["created_at"] or ""),
                    }
                    for r in j_rows
                ],
                "done_todos": [
                    {
                        "id": int(r["id"]),
                        "task": str(r["task"] or ""),
                        "status": str(r["status"] or ""),
                        "priority": str(r["priority"] or ""),
                        "due_date": str(r["due_date"] or ""),
                        "created_at": str(r["created_at"] or ""),
                    }
                    for r in done_todos
                ],
            }
            body = json.dumps(data, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/workspace/valuation":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or qs.get("ticker") or [""])[0])
            if not ticker:
                data = {"ok": False, "error": "ticker_required"}
            else:
                data = {"ok": True, "ticker": ticker, "valuation": get_workspace_valuation(ticker)}
            body = json.dumps(data, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/workspace/ritual/tasks":
            qs = parse_qs(parsed.query)
            status = ((qs.get("status") or ["open"])[0] or "open").strip().lower()
            rows = list_workspace_ritual_tasks(limit=200, status=status)
            data = {
                "ok": True,
                "items": [
                    {
                        "id": int(r["id"]),
                        "ritual_key": str(r["ritual_key"] or ""),
                        "title": str(r["title"] or ""),
                        "due_date": str(r["due_date"] or ""),
                        "status": str(r["status"] or ""),
                        "notes": str(r["notes"] or ""),
                        "created_at": str(r["created_at"] or ""),
                        "completed_at": str(r["completed_at"] or ""),
                    }
                    for r in rows
                ],
            }
            body = json.dumps(data, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/workspace/today":
            body = json.dumps({"ok": True, "payload": workspace_today_payload()}, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/workspace/13f_digest":
            body = json.dumps(workspace_13f_digest(), ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/workspace/saturday_review":
            body = json.dumps({"ok": True, "items": workspace_saturday_review_cards()}, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/workspace/settings":
            body = json.dumps(
                {
                    "ok": True,
                    "settings": {
                        "workspace_recent_watchlist_days": _workspace_recent_watchlist_days(),
                    },
                },
                ensure_ascii=True,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/workspace/journal":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or qs.get("ticker") or [""])[0])
            if not ticker:
                data = {"ok": False, "error": "ticker_required", "items": []}
            else:
                rows = list_workspace_journal(ticker, limit=160)
                data = {
                    "ok": True,
                    "ticker": ticker,
                    "items": [
                        {
                            "id": int(r["id"]),
                            "ticker": str(r["ticker"] or ""),
                            "action": str(r["action"] or "Note"),
                            "emotion": str(r["emotion"] or "Calm"),
                            "note": str(r["note"] or ""),
                            "date": str(r["created_at"] or ""),
                        }
                        for r in rows
                    ],
                }
            body = json.dumps(data, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/workspace/todos":
            rows = list_todos(limit=200)
            items = [
                {
                    "id": int(r["id"]),
                    "task": str(r["task"] or ""),
                    "status": str(r["status"] or "open"),
                    "priority": str(r["priority"] or "P2"),
                    "due_date": str(r["due_date"] or ""),
                    "created_at": str(r["created_at"] or ""),
                    "ticker": str(r["ticker"] or ""),
                    "category": str(r["category"] or "general"),
                }
                for r in rows
            ]
            body = json.dumps({"ok": True, "items": items}, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/workspace/scratchpad":
            body = json.dumps({"ok": True, "content": get_scratchpad()}, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or [""])[0])
            if ticker:
                self.send_response(302)
                self.send_header("Location", f"/company_file?t={urllib.parse.quote(ticker)}")
                self.end_headers()
                return
            show_all = ((qs.get("earnings") or [""])[0] or "").strip().lower() == "all"
            nw = ((qs.get("nw") or ["general"])[0] or "general").strip().lower()
            reg = ((qs.get("reg") or ["portfolio"])[0] or "portfolio").strip().lower()
            deep_mode = str((qs.get("deep") or ["0"])[0] or "0").strip() == "1"
            perf_mode = str((qs.get("perf") or ["0"])[0] or "0").strip() == "1"
            html_doc, perf_meta = dashboard_home_html_cached(
                earnings_show_all=show_all,
                news_mode=nw,
                reg_mode=reg,
                deep_mode=deep_mode,
                ttl_seconds=20,
                return_meta=True,
            )
            if perf_mode:
                html_doc = _inject_home_perf_badge(str(html_doc), dict(perf_meta))
            self._send_html(str(html_doc))
            return
        if parsed.path == "/organizer":
            qs = parse_qs(parsed.query)
            msg = str((qs.get("msg") or [""])[0] or "")
            q = str((qs.get("q") or [""])[0] or "")
            day = str((qs.get("day") or [""])[0] or "")
            rq = str((qs.get("rq") or [""])[0] or "")
            self._send_html(organizer_html(message=msg, query=q, day=day, recall_query=rq))
            return
        if parsed.path == "/organizer/google/connect":
            qs = parse_qs(parsed.query)
            return_to = str((qs.get("return_to") or ["/organizer"])[0] or "/organizer")
            if not _google_calendar_enabled():
                self.send_response(302)
                self.send_header("Location", "/organizer?msg=" + urllib.parse.quote("Set GOOGLE_OAUTH_CLIENT_ID in .env first."))
                self.end_headers()
                return
            auth_url = _google_oauth_authorize_url(return_to=return_to)
            self.send_response(302)
            self.send_header("Location", auth_url)
            self.end_headers()
            return
        if parsed.path == "/organizer/google/callback":
            qs = parse_qs(parsed.query)
            code = str((qs.get("code") or [""])[0] or "")
            state = str((qs.get("state") or [""])[0] or "")
            err = str((qs.get("error") or [""])[0] or "")
            if err:
                self.send_response(302)
                self.send_header("Location", "/organizer?msg=" + urllib.parse.quote(f"Google auth error: {err}"))
                self.end_headers()
                return
            ok, msg = _google_exchange_code(code=code, state=state)
            self.send_response(302)
            self.send_header("Location", "/organizer?msg=" + urllib.parse.quote(msg))
            self.end_headers()
            return
        if parsed.path == "/universe":
            qs = parse_qs(parsed.query)
            tab = (qs.get("tab") or ["portfolio"])[0].strip().lower()
            deep_mode = str((qs.get("deep") or ["0"])[0] or "0").strip() == "1"
            self._send_html(universe_html(tab=tab, deep_mode=deep_mode))
            return
        if parsed.path == "/workspace":
            self.send_response(302)
            self.send_header("Location", "/company_file")
            self.end_headers()
            return
        if parsed.path == "/company_lists":
            qs = parse_qs(parsed.query)
            msg = str((qs.get("msg") or [""])[0] or "")
            target = "/company_file"
            if msg:
                target += f"?msg={urllib.parse.quote(msg)}"
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return
        if parsed.path == "/watchlist_history":
            qs = parse_qs(parsed.query)
            ticker = str((qs.get("ticker") or [""])[0] or "").strip()
            order = str((qs.get("order") or ["desc"])[0] or "desc").strip().lower()
            lim_raw = str((qs.get("limit") or ["5000"])[0] or "5000").strip()
            try:
                lim = int(lim_raw)
            except Exception:
                lim = 5000
            self._send_html(watchlist_history_html(ticker=ticker, order=order, limit=lim))
            return
        if parsed.path == "/indices":
            self.send_response(302)
            self.send_header("Location", "/company_file")
            self.end_headers()
            return
        if parsed.path == "/index":
            qs = parse_qs(parsed.query)
            idx = str((qs.get("i") or [""])[0] or "").strip().lower()
            q = str((qs.get("q") or [""])[0] or "")
            sort_by = str((qs.get("sort") or ["ticker_asc"])[0] or "ticker_asc")
            refresh = str((qs.get("refresh") or ["0"])[0] or "0").strip() == "1"
            try:
                page = int(str((qs.get("page") or ["1"])[0] or "1").strip())
            except Exception:
                page = 1
            try:
                page_size = int(str((qs.get("page_size") or ["120"])[0] or "120").strip())
            except Exception:
                page_size = 120
            self._send_html(index_detail_html(index_id=idx, query=q, sort_by=sort_by, refresh=refresh, page=page, page_size=page_size))
            return
        if parsed.path == "/indices/industry":
            qs = parse_qs(parsed.query)
            industry = str((qs.get("industry") or [""])[0] or "")
            q = str((qs.get("q") or [""])[0] or "")
            sort_by = str((qs.get("sort") or ["ticker_asc"])[0] or "ticker_asc")
            try:
                page = int(str((qs.get("page") or ["1"])[0] or "1").strip())
            except Exception:
                page = 1
            try:
                page_size = int(str((qs.get("page_size") or ["120"])[0] or "120").strip())
            except Exception:
                page_size = 120
            self._send_html(index_industry_html(industry=industry, query=q, sort_by=sort_by, page=page, page_size=page_size))
            return
        if parsed.path == "/company_list":
            qs = parse_qs(parsed.query)
            name = str((qs.get("name") or [""])[0] or "")
            q = str((qs.get("q") or [""])[0] or "")
            sort_by = str((qs.get("sort") or ["mcap_desc"])[0] or "mcap_desc")
            self._send_html(company_list_html(name=name, query=q, sort_by=sort_by))
            return
        if parsed.path == "/investment_workspace":
            self.send_response(302)
            self.send_header("Location", "/company_file")
            self.end_headers()
            return
        if parsed.path == "/watchlist/edit":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or [""])[0])
            self._send_html(watchlist_edit_html(ticker))
            return
        if parsed.path == "/portfolio/edit":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or [""])[0])
            self._send_html(portfolio_edit_html(ticker))
            return
        if parsed.path == "/earnings_reported":
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if parsed.path == "/company":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or [""])[0])
            tab = (qs.get("tab") or ["overview"])[0].strip().lower()
            run_lens = ((qs.get("lens") or [""])[0] or "").strip() == "1"
            self._send_html(company_html(ticker, tab=tab, run_lens=run_lens))
            return
        if parsed.path == "/company_file":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or [""])[0])
            msg = str((qs.get("msg") or [""])[0] or "")
            q = str((qs.get("q") or [""])[0] or "")
            tl = str((qs.get("tl") or ["all"])[0] or "all")
            sort_by = str((qs.get("sort") or ["mcap_desc"])[0] or "mcap_desc")
            industry = str((qs.get("industry") or [""])[0] or "")
            industry_mode = str((qs.get("industry_mode") or ["include"])[0] or "include")
            index_id = str((qs.get("index") or [""])[0] or "")
            list_name = str((qs.get("list") or [""])[0] or "")
            moat = str((qs.get("moat") or [""])[0] or "")
            try:
                page = int(str((qs.get("page") or ["1"])[0] or "1").strip())
            except Exception:
                page = 1
            try:
                page_size = int(str((qs.get("page_size") or ["80"])[0] or "80").strip())
            except Exception:
                page_size = 80
            self._send_html(
                company_file_html(
                    ticker,
                    message=msg,
                    query=q,
                    timeline_filter=tl,
                    page=page,
                    page_size=page_size,
                    sort_by=sort_by,
                    industry_filter=industry,
                    industry_mode=industry_mode,
                    index_filter=index_id,
                    list_filter=list_name,
                    moat_filter=moat,
                )
            )
            return
        if parsed.path == "/earnings_industry":
            qs = parse_qs(parsed.query)
            sector = (qs.get("sector") or [""])[0]
            self._send_html(earnings_industry_html(sector))
            return
        if parsed.path == "/memo":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or [""])[0])
            self.send_response(302)
            self.send_header("Location", f"/company?t={urllib.parse.quote(ticker)}&tab=overview")
            self.end_headers()
            return
        if parsed.path == "/sec_risk":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or [""])[0])
            self._send_html(sec_risk_html(ticker))
            return
        if parsed.path == "/mda_diff":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or [""])[0])
            self._send_html(mda_diff_html(ticker))
            return
        if parsed.path == "/tenk_segment":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or [""])[0])
            self.send_response(302)
            self.send_header("Location", f"/company?t={urllib.parse.quote(ticker)}&tab=overview")
            self.end_headers()
            return
        if parsed.path == "/sync":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or [""])[0])
            tab = (qs.get("tab") or ["overview"])[0].strip().lower()
            run_lens = ((qs.get("lens") or [""])[0] or "").strip() == "1"
            if not ticker:
                self._send_html(dashboard_html("Ticker is required for sync."))
                return
            if not _is_tracked_holding_ticker(ticker):
                self._send_html(dashboard_html("Sync blocked: filings download is only for watchlist/portfolio companies.", focus_ticker=ticker))
                return
            queue_company_sync(ticker)
            self._send_html(company_html(ticker, tab=tab, run_lens=run_lens))
            return
        if parsed.path == "/filing":
            qs = parse_qs(parsed.query)
            path = (qs.get("path") or [""])[0]
            doc_url = (qs.get("doc") or [""])[0].strip()
            mode = ((qs.get("mode") or ["reader"])[0] or "reader").strip().lower()
            p = safe_resolve_file(path)
            if p is None:
                self._send_html(dashboard_html("Filing path not available."))
                return
            ext = p.suffix.lower()
            raw_href = f"/filing_raw?path={urllib.parse.quote(path, safe='')}"
            reader_href = f"/filing?path={urllib.parse.quote(path, safe='')}&doc={urllib.parse.quote(doc_url, safe='') if doc_url else ''}&mode=reader"
            original_href = f"/filing?path={urllib.parse.quote(path, safe='')}&doc={urllib.parse.quote(doc_url, safe='') if doc_url else ''}&mode=original"
            toolbar = (
                "<div class='bar'>"
                "<a class='btn' href='/'>Home</a>"
                "<a class='btn' href='javascript:history.back()'>Back</a>"
                f"<a class='btn' href='{reader_href}'>Reader</a>"
                f"<a class='btn' href='{original_href}'>Original</a>"
                f"<a class='btn' href='{raw_href}' target='_blank' rel='noopener noreferrer'>Raw File</a>"
                + (f"<a class='btn' href='{html.escape(doc_url, quote=True)}' target='_blank' rel='noopener noreferrer'>Official SEC</a>" if doc_url else "")
                + "</div>"
            )
            if ext == ".pdf":
                self._send_html(
                    "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
                    "<style>body{margin:0;background:#eaf0f4;color:#2f4358;font-family:'Avenir Next','Helvetica Neue',sans-serif;}"
                    ".wrap{max-width:1280px;margin:0 auto;padding:12px;} .bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px;}"
                    ".btn{border:1px solid #c8d3dd;background:#edf2f6;color:#2f4358;border-radius:8px;padding:6px 10px;text-decoration:none;font-size:12px;font-weight:700;}"
                    "iframe{width:100%;height:88vh;border:1px solid #c8d3dd;border-radius:10px;background:#fff;}</style></head>"
                    f"<body><div class='wrap'>{toolbar}<iframe src='{raw_href}'></iframe></div></body></html>"
                )
                return
            txt = p.read_text(encoding="utf-8", errors="ignore")
            looks_html = ("<html" in txt[:4000].lower()) or ("<!doctype html" in txt[:4000].lower()) or ("<body" in txt[:4000].lower())
            if looks_html and mode == "original":
                cleaned = re.sub(r"(?is)<script[^>]*>.*?</script>", "", txt[:1200000])
                self._send_html(
                    "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
                    "<style>body{margin:0;background:#eaf0f4;color:#2f4358;font-family:'Avenir Next','Helvetica Neue',sans-serif;}"
                    ".wrap{max-width:1280px;margin:0 auto;padding:12px;} .bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px;}"
                    ".btn{border:1px solid #c8d3dd;background:#edf2f6;color:#2f4358;border-radius:8px;padding:6px 10px;text-decoration:none;font-size:12px;font-weight:700;}"
                    "iframe{width:100%;height:88vh;border:1px solid #c8d3dd;border-radius:10px;background:#fff;}</style></head>"
                    f"<body><div class='wrap'>{toolbar}<iframe srcdoc='{html.escape(cleaned, quote=True)}'></iframe></div></body></html>"
                )
                return
            # Reader mode (default): cleaner typography while preserving structure.
            reader = txt
            if looks_html:
                reader = re.sub(r"(?is)<script[^>]*>.*?</script>", "", reader)
                reader = re.sub(r"(?is)<style[^>]*>.*?</style>", "", reader)
                reader = re.sub(r"(?is)</?(meta|link|head|title)[^>]*>", "", reader)
            self._send_html(
                "<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
                "<style>body{margin:0;background:#eaf0f4;color:#2f4358;font-family:Georgia,'Times New Roman',serif;}"
                ".wrap{max-width:980px;margin:0 auto;padding:14px;} .bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;}"
                ".btn{border:1px solid #c8d3dd;background:#edf2f6;color:#2f4358;border-radius:8px;padding:6px 10px;text-decoration:none;font-size:12px;font-family:'Avenir Next','Helvetica Neue',sans-serif;font-weight:700;}"
                ".paper{background:#f8fafc;border:1px solid #d3dce5;border-radius:10px;padding:18px;line-height:1.52;font-size:15px;}"
                "pre{white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;line-height:1.35;}</style></head>"
                f"<body><div class='wrap'>{toolbar}<div class='paper'>{reader if looks_html else f'<pre>{html.escape(reader[:500000])}</pre>'}</div></div></body></html>"
            )
            return
        if parsed.path == "/filing_raw":
            qs = parse_qs(parsed.query)
            path = (qs.get("path") or [""])[0]
            p = safe_resolve_file(path)
            if p is None:
                self.send_response(404)
                self.end_headers()
                return
            try:
                body = p.read_bytes()
            except Exception:
                self.send_response(500)
                self.end_headers()
                return
            ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/deepdive":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or [""])[0])
            refresh = (qs.get("refresh") or ["0"])[0].strip() in {"1", "true", "yes"}
            if not _heavy_analysis_enabled():
                self._send_html(dashboard_html("Deep dive automation is disabled by configuration."))
                return
            if not ticker:
                self._send_html(dashboard_html("Ticker is required for deep dive."))
                return
            ok, f = generate_deep_dive(ticker, force=refresh)
            if f:
                content = Path(f).read_text(encoding="utf-8", errors="ignore")
                rendered = markdown_to_html(content)
                status = "generated now" if ok else ("refreshed cache miss" if refresh else "opened cached/latest")
                self._send_html(
                    f"<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
                    f"<title>{html.escape(ticker)} Deep Dive</title>"
                    f"<style>body{{margin:0;font-family:'Avenir Next','Helvetica Neue',sans-serif;background:#0a0f14;color:#e7eef6;}}"
                    f".wrap{{max-width:980px;margin:0 auto;padding:20px;}}.card{{background:#111a22;border:1px solid #2a3f50;border-radius:12px;padding:16px;}}"
                    f"a{{color:#9fd3ff;}}</style></head><body><div class='wrap'>"
                    f"<div class='card'><h1>{html.escape(ticker)} Deep Dive</h1><div>Status: {status}</div><div>File: {html.escape(Path(f).name)}</div><a href='/'>Back</a> | <a href='/deepdive?t={html.escape(ticker)}'>Refresh Deep Dive</a></div>"
                    f"<div class='card' style='margin-top:12px;'>{rendered}</div></div></body></html>"
                )
            else:
                self._send_html(dashboard_html(f"Could not generate deep dive for {ticker}."))
            return
        if parsed.path == "/beta_embed":
            self._send_html(read_beta_body())
            return
        if parsed.path == "/reports":
            try:
                qs = parse_qs(parsed.query)
                run_exec = (qs.get("run_exec") or ["0"])[0].strip().lower() in {"1", "true", "yes"}
                refresh = (qs.get("refresh") or ["0"])[0].strip().lower() in {"1", "true", "yes"}
                if refresh:
                    _clear_report_intel_cache()
                self._send_html(report_studio_html(run_exec=run_exec))
            except Exception as e:
                self._send_html(report_studio_html(message=f"Report Studio fallback mode: {str(e)[:140]}"))
            return
        if parsed.path == "/l2" or parsed.path == "/l2/digest":
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return
        if parsed.path == "/report":
            qs = parse_qs(parsed.query)
            kind = (qs.get("kind") or [""])[0].strip().lower()
            view = (qs.get("view") or ["paper"])[0].strip().lower()
            try:
                self._send_html(report_reader_html(kind, view=view))
            except Exception as e:
                self._send_html(report_studio_html(message=f"Report reader error: {str(e)[:140]}"))
            return
        if parsed.path == "/ticker":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or [""])[0])
            self._send_html(ticker_html(ticker))
            return
        if parsed.path == "/insider":
            qs = parse_qs(parsed.query)
            ticker = resolve_ticker_input((qs.get("t") or [""])[0])
            meaningful = (qs.get("meaningful") or ["0"])[0].strip().lower() in {"1", "true", "yes", "y"}
            self._send_html(insider_summary_html(ticker, meaningful_only=meaningful))
            return
        if parsed.path == "/note/edit":
            qs = parse_qs(parsed.query)
            try:
                note_id = int((qs.get("id") or ["0"])[0])
            except Exception:
                note_id = 0
            self._send_html(note_edit_html(note_id))
            return
        if parsed.path == "/beta":
            beta = latest("reports/terminal_beta_dashboard.html")
            if not beta:
                self._send_html(dashboard_html("No Beta dashboard file yet. Run Build Beta first."))
                return
            self.send_response(302)
            self.send_header("Location", f"file://{beta}")
            self.end_headers()
            return
        if parsed.path == "/logs":
            qs = parse_qs(parsed.query)
            job = (qs.get("job") or ["daily"])[0]
            st = RUN_STATE.get(job)
            if not st:
                self._send_html(dashboard_html("Unknown job log."))
                return
            p = LOGS / str(st["log"])
            text = p.read_text(encoding="utf-8", errors="ignore")[-15000:] if p.exists() else ""
            self._send_html(
                f"<html><body style='font-family:monospace;background:#0b1014;color:#e7eef6;'>"
                f"<div style='padding:12px;'><a href='/'>Back</a> <h3>Log: {html.escape(job)}</h3><pre>{html.escape(text)}</pre></div></body></html>"
            )
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8", errors="ignore")
        form = parse_qs(raw)
        if parsed.path.startswith("/api/workspace/"):
            body = json.dumps(
                {"ok": False, "error": "workspace_retired", "redirect": "/company_file"},
                ensure_ascii=True,
            ).encode("utf-8")
            self.send_response(410)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/run":
            job = form.get("job", [""])[0]
            if job not in JOBS:
                self._send_html(dashboard_html("Unknown job."))
                return
            t = threading.Thread(target=run_job, args=(job,), daemon=True)
            t.start()
            self._send_html(dashboard_html(f"Started job: {job}"))
            return

        if parsed.path == "/chat":
            action = (form.get("action", ["ask"])[0] or "ask").strip().lower()
            if action == "clear":
                CHAT_HISTORY.clear()
                self._send_html(dashboard_html("Chat history cleared."))
                return
            q = (form.get("question", [""])[0] or "").strip()
            mode = (form.get("mode", ["full"])[0] or "full").strip().lower()
            speed = (form.get("speed", ["quick"])[0] or "quick").strip().lower()
            if mode not in {"full", "portfolio", "watchlist", "global"}:
                mode = "full"
            if speed not in {"quick", "deep"}:
                speed = "quick"
            if not q:
                self._send_html(dashboard_html("Please enter a question for Agent Chat."))
                return
            ans = ask_agent_from_dashboard(q, mode, speed)
            CHAT_HISTORY.append(
                {
                    "ts": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "mode": mode,
                    "speed": speed,
                    "q": q,
                    "a": ans,
                }
            )
            self._send_html(dashboard_html("Agent response added."))
            return

        if parsed.path == "/api/chat":
            action = (form.get("action", ["ask"])[0] or "ask").strip().lower()
            if action == "clear":
                CHAT_HISTORY.clear()
                body = json.dumps({"ok": True, "cleared": True}, ensure_ascii=True).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            q = (form.get("question", [""])[0] or "").strip()
            mode = (form.get("mode", ["full"])[0] or "full").strip().lower()
            speed = (form.get("speed", ["quick"])[0] or "quick").strip().lower()
            provider = (form.get("provider", ["auto"])[0] or "auto").strip().lower()
            style = (form.get("style", ["analyst"])[0] or "analyst").strip().lower()
            if mode not in {"full", "portfolio", "watchlist", "global"}:
                mode = "full"
            if speed not in {"quick", "deep"}:
                speed = "quick"
            if provider not in {"auto", "openai", "ollama", "anthropic"}:
                provider = "auto"
            if style not in {"analyst", "concise", "deep"}:
                style = "analyst"
            if not q:
                body = json.dumps({"ok": False, "error": "question_required"}, ensure_ascii=True).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            used_provider = "-"
            used_model = "-"
            sources: list[str] = []
            if mode == "global":
                live_ctx = _global_live_snapshot(q)
                weather_mode = _is_weather_query(q)
                macro_mode = _is_macro_query(q)
                filing_mode = _is_filing_query(q)
                move_mode = _is_stock_move_query(q)
                if weather_mode:
                    sources = ["Open-Meteo Geocoding API", "Open-Meteo Forecast API"]
                    ans = live_ctx
                    used_provider = "direct"
                    used_model = "none"
                elif macro_mode:
                    sources = ["FRED (Federal Reserve Economic Data)"]
                    if speed == "quick":
                        ans = live_ctx
                        used_provider = "direct"
                        used_model = "none"
                    else:
                        try:
                            ans, used_provider, used_model = _ask_llm_with_provider(
                                f"Question:\n{q}\n\nLive macro snapshot:\n{live_ctx}\n\nConversation memory:\n{_chat_history_block()}",
                                _chat_style_system(style) + " Use only provided macro snapshot.",
                                provider=provider,
                            )
                        except Exception:
                            ans = live_ctx
                elif filing_mode:
                    sources = ["SEC local filings database/cache"]
                    if speed == "quick":
                        ans = live_ctx
                        used_provider = "direct"
                        used_model = "none"
                    else:
                        try:
                            ans, used_provider, used_model = _ask_llm_with_provider(
                                f"Question:\n{q}\n\nSEC filing snapshot:\n{live_ctx}\n\nConversation memory:\n{_chat_history_block()}",
                                _chat_style_system(style) + " Use only provided SEC filing snapshot.",
                                provider=provider,
                            )
                        except Exception:
                            ans = live_ctx
                elif move_mode:
                    sources = ["Yahoo Finance", "Google News RSS", "Local earnings feed", "SEC local filings"]
                    if speed == "quick":
                        try:
                            ans, used_provider, used_model = _ask_llm_with_provider(
                                f"Question:\n{q}\n\nStock-move evidence chain:\n{live_ctx}",
                                "You are a market analyst. Use ONLY provided evidence chain. Return 1 direct answer + 3 evidence bullets + confidence.",
                                provider=provider,
                            )
                        except Exception:
                            ans = live_ctx
                    else:
                        try:
                            ans, used_provider, used_model = _ask_llm_with_provider(
                                f"Question:\n{q}\n\nStock-move evidence chain:\n{live_ctx}\n\nConversation memory:\n{_chat_history_block()}",
                                _chat_style_system(style) + " Use only provided stock-move evidence chain. Return direct answer + 3 bullets + confidence.",
                                provider=provider,
                            )
                        except Exception:
                            ans = live_ctx
                elif speed == "quick":
                    sources = ["Google News RSS", "Reuters RSS", "Yahoo Finance (when ticker inferred)", "SEC local filings"]
                    if _hybrid_ask_ai is None:
                        ans = live_ctx
                    else:
                        try:
                            ans, used_provider, used_model = _ask_llm_with_provider(
                                f"Question:\n{q}\n\nLive snapshot:\n{live_ctx}\n\nConversation memory:\n{_chat_history_block()}",
                                _chat_style_system(style)
                                + " Use only provided live snapshot as evidence. Return short actionable answer.",
                                provider=provider,
                            )
                        except Exception as e:
                            ans = live_ctx + f"\n\nAI unavailable: {str(e)[:160]}"
                            if provider in {"openai", "anthropic"}:
                                try:
                                    ans, used_provider, used_model = _ask_llm_with_provider(
                                        f"Question:\n{q}\n\nLive snapshot:\n{live_ctx}\n\nConversation memory:\n{_chat_history_block()}",
                                        _chat_style_system(style)
                                        + " Use only provided live snapshot as evidence. Return short actionable answer.",
                                        provider="ollama",
                                    )
                                    ans = ans + "\n\n(Fallback used: ollama)"
                                except Exception:
                                    pass
                else:
                    sources = ["Google News RSS", "Reuters RSS", "Yahoo Finance (when ticker inferred)", "SEC local filings"]
                    try:
                        ans, used_provider, used_model = _ask_llm_with_provider(
                            f"Question:\n{q}\n\nLive snapshot:\n{live_ctx}\n\nConversation memory:\n{_chat_history_block()}",
                            _chat_style_system(style)
                            + " Use only provided live snapshot as evidence. "
                            "Return direct answer + 3 evidence bullets + confidence.",
                            provider=provider,
                        )
                    except Exception as e:
                        ans = f"Global deep AI failed: {str(e)[:160]}"
                        if provider in {"openai", "anthropic"}:
                            try:
                                ans, used_provider, used_model = _ask_llm_with_provider(
                                    f"Question:\n{q}\n\nLive snapshot:\n{live_ctx}\n\nConversation memory:\n{_chat_history_block()}",
                                    _chat_style_system(style)
                                    + " Use only provided live snapshot as evidence. "
                                    "Return direct answer + 3 evidence bullets + confidence.",
                                    provider="ollama",
                                )
                                ans = ans + "\n\n(Fallback used: ollama)"
                            except Exception:
                                pass
            else:
                ans = ask_agent_from_dashboard(q, mode, speed)
            row = {
                "ts": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "mode": mode,
                "speed": speed,
                "style": style,
                "provider": used_provider if used_provider != "-" else provider,
                "model": used_model,
                "sources": ", ".join(sources),
                "q": q,
                "a": ans,
            }
            CHAT_HISTORY.append(row)
            body = json.dumps({"ok": True, "item": row}, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/deep_dive":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            if not _heavy_analysis_enabled():
                body = json.dumps(
                    {"ok": False, "ticker": ticker, "error": "disabled", "detail": "Deep dive automation is disabled by configuration."},
                    ensure_ascii=True,
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if not ticker:
                body = json.dumps({"ok": False, "error": "ticker_required"}, ensure_ascii=True).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            result = perform_deep_dive(ticker)
            body = json.dumps(result, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/workspace/company/save":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            stage = (form.get("stage", ["Inbox"])[0] or "Inbox").strip()
            thesis = (form.get("thesis", [""])[0] or "")
            try:
                conviction = int((form.get("conviction", ["6"])[0] or "6").strip())
            except Exception:
                conviction = 6
            if not ticker:
                body = json.dumps({"ok": False, "error": "ticker_required"}, ensure_ascii=True).encode("utf-8")
            else:
                ok = save_workspace_company(ticker=ticker, stage=stage, conviction=conviction, thesis=thesis)
                body = json.dumps({"ok": bool(ok), "ticker": ticker}, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/workspace/profile/save":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            why_wrong = (form.get("why_wrong", [""])[0] or "").strip()
            score_growth = (form.get("score_growth", ["5"])[0] or "5").strip()
            score_margin = (form.get("score_margin", ["5"])[0] or "5").strip()
            score_capital = (form.get("score_capital", ["5"])[0] or "5").strip()
            score_valuation = (form.get("score_valuation", ["5"])[0] or "5").strip()
            score_risk = (form.get("score_risk", ["5"])[0] or "5").strip()
            if not ticker:
                body = json.dumps({"ok": False, "error": "ticker_required"}, ensure_ascii=True).encode("utf-8")
            elif not why_wrong:
                body = json.dumps({"ok": False, "error": "why_wrong_required"}, ensure_ascii=True).encode("utf-8")
            else:
                ok = save_workspace_profile(
                    ticker=ticker,
                    why_wrong=why_wrong,
                    score_growth=score_growth,
                    score_margin=score_margin,
                    score_capital=score_capital,
                    score_valuation=score_valuation,
                    score_risk=score_risk,
                )
                body = json.dumps({"ok": bool(ok), "ticker": ticker}, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/workspace/valuation/save":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            intrinsic_market_cap_b = (form.get("intrinsic_market_cap_b", [""])[0] or "").strip()
            intrinsic_price = (form.get("intrinsic_price", [""])[0] or "").strip()
            strike_starter = (form.get("strike_starter", [""])[0] or "").strip()
            strike_add = (form.get("strike_add", [""])[0] or "").strip()
            strike_aggressive = (form.get("strike_aggressive", [""])[0] or "").strip()
            invalidation_trigger = (form.get("invalidation_trigger", [""])[0] or "").strip()
            confidence = (form.get("confidence", ["50"])[0] or "50").strip()
            if not ticker:
                body = json.dumps({"ok": False, "error": "ticker_required"}, ensure_ascii=True).encode("utf-8")
            else:
                ok = save_workspace_valuation(
                    ticker=ticker,
                    intrinsic_market_cap_b=intrinsic_market_cap_b,
                    intrinsic_price=intrinsic_price,
                    strike_starter=strike_starter,
                    strike_add=strike_add,
                    strike_aggressive=strike_aggressive,
                    invalidation_trigger=invalidation_trigger,
                    confidence=confidence,
                )
                body = json.dumps({"ok": bool(ok), "ticker": ticker}, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/workspace/ritual/complete":
            try:
                task_id = int((form.get("id", ["0"])[0] or "0").strip())
            except Exception:
                task_id = 0
            note = (form.get("note", [""])[0] or "").strip()
            ok = complete_workspace_ritual_task(task_id=task_id, note=note) if task_id > 0 else False
            body = json.dumps({"ok": bool(ok), "id": int(task_id)}, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/workspace/snapshot/save":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            snapshot_month = (form.get("snapshot_month", [""])[0] or "").strip()
            if not ticker:
                body = json.dumps({"ok": False, "error": "ticker_required"}, ensure_ascii=True).encode("utf-8")
            else:
                ok = save_workspace_thesis_snapshot(ticker=ticker, snapshot_month=snapshot_month)
                body = json.dumps({"ok": bool(ok), "ticker": ticker}, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/workspace/mortem/add":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            mortem_type = (form.get("mortem_type", ["premortem"])[0] or "premortem").strip()
            trigger_txt = (form.get("trigger_txt", [""])[0] or "").strip()
            hypothesis = (form.get("hypothesis", [""])[0] or "").strip()
            outcome = (form.get("outcome", [""])[0] or "").strip()
            lessons = (form.get("lessons", [""])[0] or "").strip()
            if not ticker:
                body = json.dumps({"ok": False, "error": "ticker_required"}, ensure_ascii=True).encode("utf-8")
            else:
                rid = add_workspace_mortem(
                    ticker=ticker,
                    mortem_type=mortem_type,
                    trigger_txt=trigger_txt,
                    hypothesis=hypothesis,
                    outcome=outcome,
                    lessons=lessons,
                )
                body = json.dumps({"ok": rid > 0, "id": rid, "ticker": ticker}, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/workspace/note/add":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            note = (form.get("note", [""])[0] or "").strip()
            sentiment = (form.get("sentiment", ["watch"])[0] or "watch").strip().lower()
            tags = (form.get("tags", ["workspace"])[0] or "workspace").strip()
            scope = (form.get("scope", ["portfolio"])[0] or "portfolio").strip().lower()
            if sentiment not in {"watch", "bullish", "bearish", "neutral"}:
                sentiment = "watch"
            if scope not in {"portfolio", "watchlist", "general"}:
                scope = "portfolio"
            if not note:
                body = json.dumps({"ok": False, "error": "note_required"}, ensure_ascii=True).encode("utf-8")
            else:
                rid = add_note_row(scope=scope, ticker=ticker, sentiment=sentiment, note=note, tags=tags)
                body = json.dumps({"ok": rid > 0, "id": rid, "ticker": ticker}, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/workspace/journal/add":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            action = (form.get("action", ["Note"])[0] or "Note").strip()
            emotion = (form.get("emotion", ["Calm"])[0] or "Calm").strip()
            note = (form.get("note", [""])[0] or "").strip()
            if not ticker:
                body = json.dumps({"ok": False, "error": "ticker_required"}, ensure_ascii=True).encode("utf-8")
            elif not note:
                body = json.dumps({"ok": False, "error": "note_required"}, ensure_ascii=True).encode("utf-8")
            else:
                rid = add_workspace_journal(ticker=ticker, action=action, emotion=emotion, note=note)
                body = json.dumps({"ok": rid > 0, "id": rid, "ticker": ticker}, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/memo/note":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            action = (form.get("action", ["Note"])[0] or "Note").strip()
            emotion = (form.get("emotion", ["Calm"])[0] or "Calm").strip()
            note = (form.get("note", [""])[0] or "").strip()
            if not ticker:
                msg = "Ticker is required."
            elif not note:
                msg = "Note text is required."
            else:
                rid = add_workspace_journal(ticker=ticker, action=action, emotion=emotion, note=note)
                msg = "Note saved to Company File." if rid > 0 else "Failed to save note."
            loc = f"/company_file?t={urllib.parse.quote(ticker or '')}&msg={urllib.parse.quote(msg)}"
            self.send_response(302)
            self.send_header("Location", loc)
            self.end_headers()
            return
        if parsed.path == "/api/workspace/todo/add":
            task = (form.get("task", [""])[0] or "").strip()
            priority = (form.get("priority", ["P2"])[0] or "P2").strip().upper()
            due_date = (form.get("due_date", [""])[0] or "").strip()
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            category = (form.get("category", ["general"])[0] or "general").strip().lower()
            if not task:
                body = json.dumps({"ok": False, "error": "task_required"}, ensure_ascii=True).encode("utf-8")
            else:
                tid = add_todo(task=task, priority=priority, due_date=due_date, ticker=ticker, category=category)
                body = json.dumps({"ok": tid > 0, "id": tid}, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/workspace/todo/toggle":
            try:
                todo_id = int((form.get("id", ["0"])[0] or "0").strip())
            except Exception:
                todo_id = 0
            ok = toggle_todo(todo_id) if todo_id > 0 else False
            body = json.dumps({"ok": ok, "id": todo_id}, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/workspace/todo/delete":
            try:
                todo_id = int((form.get("id", ["0"])[0] or "0").strip())
            except Exception:
                todo_id = 0
            ok = delete_todo(todo_id) if todo_id > 0 else False
            body = json.dumps({"ok": ok, "id": todo_id}, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/workspace/todo/carry":
            try:
                todo_id = int((form.get("id", ["0"])[0] or "0").strip())
            except Exception:
                todo_id = 0
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            note = (form.get("note", [""])[0] or "").strip()
            ok, note_id = carry_forward_todo(todo_id, ticker=ticker, note=note) if todo_id > 0 else (False, 0)
            body = json.dumps({"ok": ok, "id": todo_id, "note_id": int(note_id)}, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/workspace/scratchpad/save":
            content = (form.get("content", [""])[0] or "")
            save_scratchpad(content, pinned=None)
            body = json.dumps({"ok": True}, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/workspace/settings/save":
            raw_days = (form.get("workspace_recent_watchlist_days", ["30"])[0] or "30").strip()
            try:
                days = int(raw_days)
            except Exception:
                days = 30
            saved = _set_workspace_recent_watchlist_days(days)
            body = json.dumps(
                {
                    "ok": True,
                    "settings": {"workspace_recent_watchlist_days": saved},
                },
                ensure_ascii=True,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/sec_risk_diff":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            if not ticker:
                body = json.dumps({"ok": False, "error": "ticker_required"}, ensure_ascii=True).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            result = perform_sec_risk_diff(ticker)
            body = json.dumps(result, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/mda_diff":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            if not ticker:
                body = json.dumps({"ok": False, "error": "ticker_required"}, ensure_ascii=True).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            result = perform_mda_diff(ticker)
            body = json.dumps(result, ensure_ascii=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/company_lists/create":
            name = (form.get("name", [""])[0] or "").strip()
            ok, norm = create_company_list(name)
            msg = f"List ready: {norm}" if ok else norm
            self.send_response(302)
            self.send_header("Location", f"/company_file?msg={urllib.parse.quote(msg)}&list={urllib.parse.quote(norm if ok else '')}")
            self.end_headers()
            return
        if parsed.path == "/company_lists/delete":
            name = (form.get("name", [""])[0] or "").strip()
            ok = delete_company_list(name)
            msg = f"Deleted list: {name}" if ok else "List not found."
            self.send_response(302)
            self.send_header("Location", f"/company_file?msg={urllib.parse.quote(msg)}")
            self.end_headers()
            return
        if parsed.path == "/company_list/add":
            name = (form.get("name", [""])[0] or "").strip()
            ticker = (form.get("ticker", [""])[0] or "").strip()
            source = (form.get("source", ["manual"])[0] or "manual").strip()
            ok, msg = add_company_to_list(name=name, ticker=ticker, source=source)
            _ = msg
            self.send_response(302)
            self.send_header("Location", f"/company_list?name={urllib.parse.quote(_norm_list_name(name))}")
            self.end_headers()
            return
        if parsed.path == "/company_list/remove":
            name = (form.get("name", [""])[0] or "").strip()
            ticker = (form.get("ticker", [""])[0] or "").strip()
            _ = remove_company_from_list(name=name, ticker=ticker)
            self.send_response(302)
            self.send_header("Location", f"/company_list?name={urllib.parse.quote(_norm_list_name(name))}")
            self.end_headers()
            return
        if parsed.path == "/company_list/import_sector":
            name = (form.get("name", [""])[0] or "").strip()
            sector = (form.get("sector", [""])[0] or "").strip()
            final_name = _norm_list_name(name) or _norm_list_name(f"{sector} List")
            snap = get_reported_earnings_snapshot(limit=260)
            rows = list(snap.get("this_week_rows") or [])
            if not rows:
                rows = list(snap.get("parsed") or [])
            tickers = [str(r.get("ticker") or "").upper().strip() for r in rows if str(r.get("ticker") or "").strip()]
            prof = get_portfolio_profiles(tickers) if tickers else {}
            added = 0
            for t in sorted(set(tickers)):
                sec = str((prof.get(t, {}) or {}).get("sector") or "Unknown").strip() or "Unknown"
                if sec.lower() != sector.lower():
                    continue
                ok, _msg = add_company_to_list(name=final_name, ticker=t, source="earnings_sector")
                if ok:
                    added += 1
            self.send_response(302)
            self.send_header("Location", f"/company_list?name={urllib.parse.quote(final_name)}")
            self.end_headers()
            return
        if parsed.path == "/company_list/import_index":
            name = (form.get("name", [""])[0] or "").strip()
            index_id = (form.get("index_id", [""])[0] or "").strip().lower()
            idx = next((x for x in _index_presets() if x["id"] == index_id), None)
            if idx is None:
                self.send_response(302)
                self.send_header("Location", f"/company_file?msg={urllib.parse.quote('Unknown index.')}")
                self.end_headers()
                return
            list_name = _norm_list_name(name) or _norm_list_name(str(idx.get("name") or "Index List"))
            tickers, _mode, _note = _load_index_tickers(index_id=index_id, refresh=False)
            for t in tickers:
                add_company_to_list(name=list_name, ticker=t, source=f"index:{index_id}")
            self.send_response(302)
            self.send_header("Location", f"/company_list?name={urllib.parse.quote(list_name)}")
            self.end_headers()
            return
        if parsed.path == "/company_file/save":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            stage = (form.get("stage", ["Inbox"])[0] or "Inbox").strip()
            conviction = (form.get("conviction", ["6"])[0] or "6").strip()
            thesis = (form.get("thesis", [""])[0] or "").strip()
            try:
                cv = int(conviction)
            except Exception:
                cv = 6
            if not ticker:
                self._send_html(dashboard_html("Ticker is required."))
                return
            save_workspace_company(ticker=ticker, stage=stage, conviction=cv, thesis=thesis)
            self.send_response(302)
            self.send_header("Location", f"/company_file?t={urllib.parse.quote(ticker)}")
            self.end_headers()
            return
        if parsed.path == "/company_file/moat_save":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            moat_keys = [str(x or "") for x in form.get("moat", [])]
            if not ticker:
                self._send_html(dashboard_html("Ticker is required."))
                return
            count = save_company_moat_keys(ticker=ticker, moat_keys=moat_keys)
            msg = f"Saved moat tags ({count})."
            self.send_response(302)
            self.send_header("Location", f"/company_file?t={urllib.parse.quote(ticker)}&msg={urllib.parse.quote(msg)}")
            self.end_headers()
            return
        if parsed.path == "/company_file/competitors_refresh":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            if not ticker:
                self._send_html(dashboard_html("Ticker is required."))
                return
            res = refresh_company_sec_competitors(ticker)
            msg = str(res.get("message") or "SEC competitor refresh finished.")
            self.send_response(302)
            self.send_header("Location", f"/company_file?t={urllib.parse.quote(ticker)}&msg={urllib.parse.quote(msg)}")
            self.end_headers()
            return
        if parsed.path == "/company_file/competitor_add":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            competitor_ticker = str((form.get("competitor_ticker", [""])[0] or "").strip())
            competitor_name = str((form.get("competitor_name", [""])[0] or "").strip())
            ok, msg = upsert_company_sec_competitor_manual(ticker, competitor_ticker, competitor_name)
            self.send_response(302)
            self.send_header("Location", f"/company_file?t={urllib.parse.quote(ticker)}&msg={urllib.parse.quote(msg)}")
            self.end_headers()
            return
        if parsed.path == "/company_file/competitor_update":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            competitor_ticker = str((form.get("competitor_ticker", [""])[0] or "").strip())
            competitor_name = str((form.get("competitor_name", [""])[0] or "").strip())
            try:
                comp_id = int((form.get("id", ["0"])[0] or "0").strip())
            except Exception:
                comp_id = 0
            ok, msg = update_company_sec_competitor_manual(comp_id, competitor_ticker, competitor_name) if comp_id > 0 else (False, "Invalid competitor row.")
            self.send_response(302)
            self.send_header("Location", f"/company_file?t={urllib.parse.quote(ticker)}&msg={urllib.parse.quote(msg)}")
            self.end_headers()
            return
        if parsed.path == "/company_file/competitor_remove":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            try:
                comp_id = int((form.get("id", ["0"])[0] or "0").strip())
            except Exception:
                comp_id = 0
            msg = "Competitor removed." if (comp_id > 0 and delete_company_sec_competitor(comp_id)) else "Competitor not found."
            self.send_response(302)
            self.send_header("Location", f"/company_file?t={urllib.parse.quote(ticker)}&msg={urllib.parse.quote(msg)}")
            self.end_headers()
            return
        if parsed.path == "/company_file/competitor_status":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            try:
                comp_id = int((form.get("id", ["0"])[0] or "0").strip())
            except Exception:
                comp_id = 0
            status = str((form.get("status", ["active"])[0] or "active")).strip().lower()
            if comp_id > 0:
                set_company_sec_competitor_status(comp_id, status)
            self.send_response(302)
            self.send_header("Location", f"/company_file?t={urllib.parse.quote(ticker)}")
            self.end_headers()
            return
        if parsed.path == "/company_file/note":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            note = (form.get("note", [""])[0] or "").strip()
            if not ticker or not note:
                self._send_html(dashboard_html("Ticker and note are required."))
                return
            add_workspace_journal(ticker=ticker, action="Note", emotion="Calm", note=note)
            self.send_response(302)
            self.send_header("Location", f"/company_file?t={urllib.parse.quote(ticker)}")
            self.end_headers()
            return
        if parsed.path == "/company_file/deep_note":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            note = (form.get("note", [""])[0] or "").strip()
            if not ticker or not note:
                self._send_html(dashboard_html("Ticker and deep note are required."))
                return
            add_workspace_journal(ticker=ticker, action="Note", emotion="Calm", note=f"DEEP_DIVE_NOTE: {note}")
            self.send_response(302)
            self.send_header("Location", f"/company_file?t={urllib.parse.quote(ticker)}")
            self.end_headers()
            return
        if parsed.path == "/company_file/risk":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            if not ticker:
                self._send_html(dashboard_html("Ticker is required."))
                return
            why_wrong = (form.get("why_wrong", [""])[0] or "").strip()
            score_growth = (form.get("score_growth", ["5"])[0] or "5").strip()
            score_margin = (form.get("score_margin", ["5"])[0] or "5").strip()
            score_capital = (form.get("score_capital", ["5"])[0] or "5").strip()
            score_valuation = (form.get("score_valuation", ["5"])[0] or "5").strip()
            score_risk = (form.get("score_risk", ["5"])[0] or "5").strip()
            save_workspace_profile(
                ticker=ticker,
                why_wrong=why_wrong or "Risk register updated.",
                score_growth=score_growth,
                score_margin=score_margin,
                score_capital=score_capital,
                score_valuation=score_valuation,
                score_risk=score_risk,
            )
            add_workspace_journal(ticker=ticker, action="Note", emotion="Calm", note="RISK_REGISTER_UPDATED")
            self.send_response(302)
            self.send_header("Location", f"/company_file?t={urllib.parse.quote(ticker)}")
            self.end_headers()
            return
        if parsed.path == "/company_file/valuation":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            if not ticker:
                self._send_html(dashboard_html("Ticker is required."))
                return
            save_workspace_valuation(
                ticker=ticker,
                intrinsic_market_cap_b="",
                intrinsic_price=(form.get("intrinsic_price", [""])[0] or "").strip(),
                strike_starter=(form.get("strike_starter", [""])[0] or "").strip(),
                strike_add=(form.get("strike_add", [""])[0] or "").strip(),
                strike_aggressive=(form.get("strike_aggressive", [""])[0] or "").strip(),
                invalidation_trigger="",
                confidence=(form.get("confidence", ["50"])[0] or "50").strip(),
            )
            self.send_response(302)
            self.send_header("Location", f"/company_file?t={urllib.parse.quote(ticker)}")
            self.end_headers()
            return
        if parsed.path == "/company_file/todo_add":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            task = (form.get("task", [""])[0] or "").strip()
            if not ticker or not task:
                self._send_html(dashboard_html("Ticker and task are required."))
                return
            add_company_todo(ticker=ticker, task=task)
            self.send_response(302)
            self.send_header("Location", f"/company_file?t={urllib.parse.quote(ticker)}")
            self.end_headers()
            return
        if parsed.path == "/company_file/todo_toggle":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            try:
                tid = int((form.get("id", ["0"])[0] or "0").strip())
            except Exception:
                tid = 0
            if tid > 0:
                toggle_company_todo(tid)
            self.send_response(302)
            self.send_header("Location", f"/company_file?t={urllib.parse.quote(ticker)}")
            self.end_headers()
            return
        if parsed.path == "/company_file/todo_delete":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            try:
                tid = int((form.get("id", ["0"])[0] or "0").strip())
            except Exception:
                tid = 0
            if tid > 0:
                delete_company_todo(tid)
            self.send_response(302)
            self.send_header("Location", f"/company_file?t={urllib.parse.quote(ticker)}")
            self.end_headers()
            return
        if parsed.path == "/company_file/reminder_add":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            note = (form.get("note", [""])[0] or "").strip()
            remind_at = (form.get("remind_at", [""])[0] or "").strip()
            if ticker and note:
                add_company_reminder(ticker=ticker, remind_at=remind_at, note=note)
            self.send_response(302)
            self.send_header("Location", f"/company_file?t={urllib.parse.quote(ticker)}")
            self.end_headers()
            return
        if parsed.path == "/company_file/reminder_toggle":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            try:
                rid = int((form.get("id", ["0"])[0] or "0").strip())
            except Exception:
                rid = 0
            if rid > 0:
                toggle_company_reminder(rid)
            self.send_response(302)
            self.send_header("Location", f"/company_file?t={urllib.parse.quote(ticker)}")
            self.end_headers()
            return

        if parsed.path == "/quick_capture":
            mode = str((form.get("mode", ["note"])[0] or "note")).strip().lower()
            txt = str((form.get("text", [""])[0] or "")).strip()
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or "").strip())
            return_to = str((form.get("return_to", ["/"])[0] or "/")).strip()
            if not return_to.startswith("/"):
                return_to = "/"
            msg = ""
            if not txt:
                msg = "Please enter text."
            elif mode == "task":
                category = "company" if ticker else "general"
                tid = add_todo(task=txt, priority="P2", due_date="", ticker=ticker, category=category)
                msg = f"Task saved (#{tid})." if tid > 0 else "Task was not saved."
            else:
                if ticker:
                    rid = add_workspace_journal(ticker=ticker, action="Note", emotion="Calm", note=txt)
                    msg = "Company note saved." if rid > 0 else "Note was not saved."
                else:
                    nid = add_note_row(scope="general", ticker="", sentiment="neutral", note=txt, tags="quick_capture")
                    msg = f"Note saved (#{nid})." if nid > 0 else "Note was not saved."
            target = return_to
            if target.startswith("/company_file") or target.startswith("/organizer"):
                sep = "&" if "?" in target else "?"
                target = f"{target}{sep}msg={urllib.parse.quote(msg)}"
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return

        if parsed.path == "/organizer/daily_save":
            day = str((form.get("day", [""])[0] or "")).strip()
            content = str((form.get("content", [""])[0] or "")).strip()
            q = str((form.get("q", [""])[0] or "")).strip()
            _ = save_daily_note(day=day, content=content)
            d = day if re.match(r"^\d{4}-\d{2}-\d{2}$", day) else dt.date.today().isoformat()
            self.send_response(302)
            self.send_header("Location", f"/organizer?day={urllib.parse.quote(d)}&q={urllib.parse.quote(q)}&msg={urllib.parse.quote('Daily note saved.')}")
            self.end_headers()
            return

        if parsed.path == "/organizer/day_close":
            day = str((form.get("day", [""])[0] or "")).strip()
            content = str((form.get("content", [""])[0] or "")).strip()
            q = str((form.get("q", [""])[0] or "")).strip()
            d = day if re.match(r"^\d{4}-\d{2}-\d{2}$", day) else dt.date.today().isoformat()
            _ = save_daily_note(day=d, content=content)
            _ = close_daily_note(d)
            try:
                next_day = (dt.date.fromisoformat(d) + dt.timedelta(days=1)).isoformat()
            except Exception:
                next_day = dt.date.today().isoformat()
            self.send_response(302)
            self.send_header("Location", f"/organizer?day={urllib.parse.quote(next_day)}&q={urllib.parse.quote(q)}&msg={urllib.parse.quote('Day archived.')}")
            self.end_headers()
            return

        if parsed.path == "/organizer/google/disconnect":
            q = str((form.get("q", [""])[0] or "")).strip()
            day = str((form.get("day", [""])[0] or "")).strip()
            d = day if re.match(r"^\d{4}-\d{2}-\d{2}$", day) else dt.date.today().isoformat()
            _google_disconnect()
            GOOGLE_CAL_CACHE.update({"ts": 0.0, "day": "", "items": [], "status": ""})
            self.send_response(302)
            self.send_header("Location", f"/organizer?day={urllib.parse.quote(d)}&q={urllib.parse.quote(q)}&msg={urllib.parse.quote('Calendar disconnected.')}")
            self.end_headers()
            return

        if parsed.path == "/organizer/recall":
            question = str((form.get("question", [""])[0] or "")).strip()
            q = str((form.get("q", [""])[0] or "")).strip()
            day = str((form.get("day", [""])[0] or "")).strip()
            d = day if re.match(r"^\d{4}-\d{2}-\d{2}$", day) else dt.date.today().isoformat()
            self._send_html(organizer_html(query=q, day=d, recall_query=question))
            return

        if parsed.path == "/todo/add":
            task = (form.get("task", [""])[0] or "").strip()
            t = (form.get("t", [""])[0] or "").strip().upper()
            source = (form.get("source", [""])[0] or "").strip().lower()
            render = (lambda msg: dashboard_html(msg, focus_ticker=t))
            if not task:
                self._send_html(render("Task text is required."))
                return
            category = "company" if source == "company" or bool(resolve_ticker_input(t)) else "general"
            tid = add_todo(task, priority="P2", due_date="", ticker=t, category=category)
            self._send_html(render(f"Task added (ID #{tid})."))
            return

        if parsed.path == "/blotter/add":
            entry = (form.get("entry", [""])[0] or "").strip()
            edit_id = (form.get("edit_id", [""])[0] or "").strip()
            t = (form.get("t", [""])[0] or "").strip().upper()
            if not entry:
                self._send_html(dashboard_html("Log text is required.", focus_ticker=t))
                return
            mode, _ = save_or_update_decision_log(entry, edit_id=edit_id)
            if mode == "updated":
                self._send_html(dashboard_html("Decision updated.", focus_ticker=t))
            else:
                self._send_html(dashboard_html("Decision logged.", focus_ticker=t))
            return

        if parsed.path == "/blotter/delete":
            entry_id = (form.get("id", [""])[0] or "").strip()
            t = (form.get("t", [""])[0] or "").strip().upper()
            ok = delete_decision_log(entry_id)
            self._send_html(dashboard_html("Decision deleted." if ok else "Decision not found.", focus_ticker=t))
            return

        if parsed.path == "/todo/toggle":
            try:
                todo_id = int((form.get("id", ["0"])[0] or "0").strip())
            except Exception:
                todo_id = 0
            t = (form.get("t", [""])[0] or "").strip().upper()
            source = (form.get("source", [""])[0] or "").strip().lower()
            render = (lambda msg: organizer_html(msg)) if source == "organizer" else (lambda msg: dashboard_html(msg, focus_ticker=t))
            if todo_id <= 0:
                self._send_html(render("Invalid task ID."))
                return
            ok = toggle_todo(todo_id)
            self._send_html(render("Task updated." if ok else "Task not found."))
            return

        if parsed.path == "/todo/update":
            try:
                todo_id = int((form.get("id", ["0"])[0] or "0").strip())
            except Exception:
                todo_id = 0
            t = (form.get("t", [""])[0] or "").strip().upper()
            source = (form.get("source", [""])[0] or "").strip().lower()
            render = (lambda msg: organizer_html(msg)) if source == "organizer" else (lambda msg: dashboard_html(msg, focus_ticker=t))
            task = (form.get("task", [""])[0] or "").strip()
            priority = (form.get("priority", ["P2"])[0] or "P2").strip().upper()
            due_date = (form.get("due_date", [""])[0] or "").strip()
            if todo_id <= 0:
                self._send_html(render("Invalid task ID."))
                return
            if not task:
                self._send_html(render("Task text is required."))
                return
            ok = update_todo(todo_id, task, priority=priority, due_date=due_date)
            self._send_html(render("Task updated." if ok else "Task not found."))
            return

        if parsed.path == "/todo/delete":
            try:
                todo_id = int((form.get("id", ["0"])[0] or "0").strip())
            except Exception:
                todo_id = 0
            t = (form.get("t", [""])[0] or "").strip().upper()
            source = (form.get("source", [""])[0] or "").strip().lower()
            render = (lambda msg: organizer_html(msg)) if source == "organizer" else (lambda msg: dashboard_html(msg, focus_ticker=t))
            if todo_id <= 0:
                self._send_html(render("Invalid task ID."))
                return
            ok = delete_todo(todo_id)
            self._send_html(render("Task deleted." if ok else "Task not found."))
            return

        if parsed.path == "/todo/archive_done":
            t = (form.get("t", [""])[0] or "").strip().upper()
            source = (form.get("source", [""])[0] or "").strip().lower()
            render = (lambda msg: organizer_html(msg)) if source == "organizer" else (lambda msg: dashboard_html(msg, focus_ticker=t))
            n = archive_done_todos()
            self._send_html(render(f"Archived {n} completed task(s)."))
            return

        if parsed.path == "/scratchpad/save":
            content = (form.get("content", [""])[0] or "")
            t = (form.get("t", [""])[0] or "").strip().upper()
            source = (form.get("source", [""])[0] or "").strip().lower()
            render = (lambda msg: dashboard_html(msg, focus_ticker=t))
            pinned_vals = form.get("pinned")
            pinned = pinned_vals[0] if pinned_vals else None
            save_scratchpad(content, pinned=pinned)
            self._send_html(render("Notes saved."))
            return

        if parsed.path == "/cpi/consensus/save":
            t = (form.get("t", [""])[0] or "").strip().upper()
            source = (form.get("source", [""])[0] or "").strip().lower()
            render = (lambda msg: dashboard_html(msg, focus_ticker=t))
            month = (form.get("month", [""])[0] or "").strip()
            all_raw = (form.get("all_items_mom_consensus", [""])[0] or "").strip()
            core_raw = (form.get("core_mom_consensus", [""])[0] or "").strip()
            all_val = to_float(all_raw) if all_raw else None
            core_val = to_float(core_raw) if core_raw else None
            ok, msg = _save_cpi_consensus_row(month, all_val, core_val)
            if ok:
                try:
                    subprocess.run(
                        ["python3", "tools/macro_watchdog.py", "--write"],
                        cwd=str(ROOT),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=30,
                    )
                except Exception:
                    pass
            self._send_html(render(msg))
            return

        if parsed.path == "/memory/export":
            t = (form.get("t", [""])[0] or "").strip().upper()
            out = export_memory_markdown()
            self._send_html(dashboard_html(f"Memory exported: {Path(out).name}", focus_ticker=t))
            return

        if parsed.path == "/thesis/save":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or ""))
            self._send_html(dashboard_html("Smart Thesis Engine is disabled.", focus_ticker=ticker))
            return

        if parsed.path == "/thesis/delete":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or ""))
            self._send_html(dashboard_html("Smart Thesis Engine is disabled.", focus_ticker=ticker))
            return

        if parsed.path == "/settings/base_currency":
            base = (form.get("base_currency", ["USD"])[0] or "USD").strip().upper()
            source = (form.get("source", [""])[0] or "").strip().lower()
            tab = (form.get("tab", ["all"])[0] or "all").strip().lower()
            render = (lambda msg: universe_html(tab=tab, message=msg)) if source == "universe" else dashboard_html
            if not re.fullmatch(r"[A-Z]{3}", base):
                self._send_html(render("Base currency must be a 3-letter code (e.g. USD, EUR)."))
                return
            _set_preferred_base_currency(base)
            self._send_html(render(f"Base currency updated to {base}."))
            return

        if parsed.path == "/cash/add":
            source = (form.get("source", [""])[0] or "").strip().lower()
            tab = (form.get("tab", ["all"])[0] or "all").strip().lower()
            render = (lambda msg: universe_html(tab=tab, message=msg)) if source == "universe" else dashboard_html
            ccy = (form.get("currency", ["USD"])[0] or "USD").strip().upper()
            amount_raw = (form.get("amount", [""])[0] or "").strip()
            note = (form.get("note", [""])[0] or "").strip()
            amt = to_float(amount_raw)
            if not re.fullmatch(r"[A-Z]{3}", ccy):
                self._send_html(render("Cash currency must be a 3-letter code (e.g. EUR)."))
                return
            if amt is None:
                self._send_html(render("Cash amount is required."))
                return
            rows = read_cash_balances(CASH_BALANCES_FILE)
            rows.append({"currency": ccy, "amount": float(amt), "note": note})
            write_cash_balances(CASH_BALANCES_FILE, rows)
            self._send_html(render(f"Added cash balance: {ccy} {float(amt):,.2f}."))
            return

        if parsed.path == "/cash/update":
            source = (form.get("source", [""])[0] or "").strip().lower()
            tab = (form.get("tab", ["all"])[0] or "all").strip().lower()
            render = (lambda msg: universe_html(tab=tab, message=msg)) if source == "universe" else dashboard_html
            try:
                idx = int((form.get("cash_id", ["-1"])[0] or "-1").strip())
            except Exception:
                idx = -1
            amount_raw = (form.get("amount", [""])[0] or "").strip()
            note = (form.get("note", [""])[0] or "").strip()
            amt = to_float(amount_raw)
            rows = read_cash_balances(CASH_BALANCES_FILE)
            if idx < 0 or idx >= len(rows):
                self._send_html(render("Select a valid cash entry to edit."))
                return
            if amt is None:
                self._send_html(render("Enter a valid cash amount."))
                return
            if float(amt) == 0.0:
                removed = rows[idx]
                kept = [r for i, r in enumerate(rows) if i != idx]
                write_cash_balances(CASH_BALANCES_FILE, kept)
                self._send_html(
                    render(
                        f"Removed cash entry: {str(removed.get('currency') or 'USD')}."
                    )
                )
                return
            cur = str(rows[idx].get("currency") or "USD").upper().strip()
            rows[idx] = {"currency": cur, "amount": float(amt), "note": note}
            write_cash_balances(CASH_BALANCES_FILE, rows)
            self._send_html(render(f"Updated cash entry: {cur} {float(amt):,.2f}."))
            return

        if parsed.path == "/cash/remove":
            source = (form.get("source", [""])[0] or "").strip().lower()
            tab = (form.get("tab", ["all"])[0] or "all").strip().lower()
            render = (lambda msg: universe_html(tab=tab, message=msg)) if source == "universe" else dashboard_html
            try:
                idx = int((form.get("cash_id", ["-1"])[0] or "-1").strip())
            except Exception:
                idx = -1
            rows = read_cash_balances(CASH_BALANCES_FILE)
            if idx < 0 or idx >= len(rows):
                self._send_html(render("Select a valid cash balance to remove."))
                return
            removed = rows[idx]
            kept = [r for i, r in enumerate(rows) if i != idx]
            write_cash_balances(CASH_BALANCES_FILE, kept)
            self._send_html(
                render(
                    f"Removed cash balance: {str(removed.get('currency') or 'USD')} {float(removed.get('amount') or 0.0):,.2f}."
                )
            )
            return

        if parsed.path == "/list/remove":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or ""))
            list_type = ((form.get("list_type", ["watchlist"])[0] or "watchlist").strip().lower())
            reason = (form.get("reason", [""])[0] or "").strip()
            source = (form.get("source", [""])[0] or "").strip().lower()
            tab = (form.get("tab", ["all"])[0] or "all").strip().lower()
            render = (lambda msg: universe_html(tab=tab, message=msg)) if source == "universe" else dashboard_html
            if not ticker:
                self._send_html(render("Ticker is required."))
                return
            if list_type == "portfolio":
                path = DATA / "portfolio.csv"
                rows = read_portfolio_rows(path)
                kept = [r for r in rows if (r[0].upper() != ticker)]
                if len(kept) == len(rows):
                    self._send_html(render(f"{ticker} not found in portfolio."))
                    return
                write_portfolio_rows(path, kept)
                invalidate_l2_cache(start_async=True)
                if _onyx_set_holding_status is not None:
                    try:
                        _onyx_set_holding_status(ticker, status="Inactive")
                    except Exception:
                        pass
                log_portfolio_event(ticker, "removed", "Removed from portfolio via manage list.")
                self._send_html(render(f"Removed {ticker} from portfolio."))
                return
            # default: watchlist
            if not reason:
                self._send_html(render("Removal reason is required for watchlist history."))
                return
            path = DATA / "my_watchlist.txt"
            entries = read_watchlist_entries(path)
            kept_e = [e for e in entries if (e.get("ticker", "").upper() != ticker)]
            if len(kept_e) == len(entries):
                self._send_html(render(f"{ticker} not found in watchlist."))
                return
            write_watchlist_entries(path, kept_e)
            invalidate_l2_cache(start_async=True)
            if _onyx_set_holding_status is not None:
                try:
                    _onyx_set_holding_status(ticker, status="Inactive")
                except Exception:
                    pass
            log_watchlist_event(ticker, "removed", reason)
            self._send_html(render(f"Removed {ticker} from watchlist."))
            return

        if parsed.path == "/asset/add":
            list_type = ((form.get("list_type", ["portfolio"])[0] or "portfolio").strip().lower())
            asset_type = (form.get("asset_type", ["stock"])[0] or "stock").strip().lower()
            raw_ticker = (form.get("ticker", [""])[0] or "").strip()
            ticker = resolve_ticker_input(raw_ticker)
            shares = (form.get("shares", [""])[0] or "").strip()
            cost = (form.get("cost_basis", [""])[0] or "").strip()
            notes = (form.get("notes", [""])[0] or "").strip()
            source = (form.get("source", [""])[0] or "").strip().lower()
            tab = (form.get("tab", ["all"])[0] or "all").strip().lower()
            render = (lambda msg: universe_html(tab=tab, message=msg)) if source == "universe" else dashboard_html
            if list_type not in {"portfolio", "watchlist"}:
                list_type = "portfolio"
            if asset_type not in {"stock", "bond", "cash"}:
                asset_type = "stock"
            if not ticker:
                self._send_html(render(f"No ticker match for '{raw_ticker}'. Use exact ticker (e.g. MOH) or full company name."))
                return

            if list_type == "watchlist":
                f = DATA / "my_watchlist.txt"
                f.parent.mkdir(parents=True, exist_ok=True)
                if not f.exists():
                    f.write_text("", encoding="utf-8")
                cur = {e["ticker"].upper() for e in read_watchlist_entries(DATA / "my_watchlist.txt")}
                q = get_live_quotes([ticker]).get(ticker, {})
                now_p = q.get("price")
                now_px = f"{float(now_p):.4f}" if isinstance(now_p, float) else ""
                now_ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
                if ticker in cur:
                    queue_company_sync(ticker)
                    invalidate_l2_cache(start_async=True)
                    _onyx_sync_holding(ticker)
                    _onyx_log_ticker_evidence(ticker)
                    self._send_html(render(f"{ticker} already exists in watchlist."))
                    return
                with f.open("a", encoding="utf-8") as fh:
                    fh.write(f"{ticker},{now_ts},{now_px}\n")
                queue_company_sync(ticker)
                invalidate_l2_cache(start_async=True)
                _onyx_sync_holding(ticker)
                _onyx_log_ticker_evidence(ticker)
                log_watchlist_event(ticker, "added", "Added from asset/add watchlist.")
                self._send_html(render(f"Added {ticker} to watchlist. Company sync triggered."))
                return

            # portfolio
            new_shares = to_float(shares)
            new_cost = to_float(cost)
            if new_shares is None or new_shares <= 0:
                self._send_html(render("Amount/Shares must be a positive number."))
                return
            if asset_type == "cash":
                ccy = ticker.upper().strip()
                if not re.fullmatch(r"[A-Z]{3}", ccy):
                    self._send_html(render("For cash, enter 3-letter currency (e.g. USD, EUR)."))
                    return
                rows = read_cash_balances(CASH_BALANCES_FILE)
                rows.append({"currency": ccy, "amount": float(new_shares), "note": notes})
                write_cash_balances(CASH_BALANCES_FILE, rows)
                self._send_html(render(f"Added cash balance: {ccy} {float(new_shares):,.2f}."))
                return

            f = DATA / "portfolio.csv"
            f.parent.mkdir(parents=True, exist_ok=True)
            if not f.exists():
                f.write_text("", encoding="utf-8")
            other_rows: list[str] = []
            pos_rows: list[list[str]] = []
            for ln in read_lines(f):
                parts = [p.strip() for p in ln.split(",", 3)]
                while len(parts) < 4:
                    parts.append("")
                if parts[0].upper() == ticker:
                    pos_rows.append(parts[:4])
                else:
                    other_rows.append(ln)

            total_shares = 0.0
            weighted_cost_sum = 0.0
            weighted_shares = 0.0
            note_parts: list[str] = []
            for row in pos_rows:
                sh = to_float(row[1])
                cb = to_float(row[2])
                if sh is not None and sh > 0:
                    total_shares += sh
                    if cb is not None and cb > 0:
                        weighted_cost_sum += sh * cb
                        weighted_shares += sh
                if row[3]:
                    note_parts.append(row[3])
            total_shares += new_shares
            if new_cost is not None and new_cost > 0:
                weighted_cost_sum += new_shares * new_cost
                weighted_shares += new_shares
            if notes:
                note_parts.append(notes)
            avg_cost = (weighted_cost_sum / weighted_shares) if weighted_shares > 0 else None
            merged_notes = " | ".join([n for n in note_parts if n])
            shares_out = f"{int(total_shares)}" if float(total_shares).is_integer() else f"{total_shares:.4f}".rstrip("0").rstrip(".")
            cost_out = f"{avg_cost:.4f}".rstrip("0").rstrip(".") if avg_cost is not None else ""
            other_rows.append(f"{ticker},{shares_out},{cost_out},{merged_notes}")
            f.write_text("\n".join(other_rows) + "\n", encoding="utf-8")
            queue_company_sync(ticker)
            invalidate_l2_cache(start_async=True)
            _onyx_sync_holding(ticker, thesis_hint=notes)
            _onyx_log_ticker_evidence(ticker)
            log_portfolio_event(
                ticker,
                "added" if not pos_rows else "updated",
                f"Shares={shares_out} | AvgCost={cost_out or '-'}",
            )
            if _onyx_run_thesis_check_if_due is not None:
                try:
                    _onyx_run_thesis_check_if_due(ticker, min_interval_seconds=0)
                except Exception:
                    pass
            if not pos_rows:
                self._send_html(render(f"Updated portfolio: {ticker}. Company sync triggered."))
                return
            self._send_html(render(f"Updated portfolio: {ticker}."))
            return

        if parsed.path == "/watchlist/add":
            raw_ticker = (form.get("ticker", [""])[0] or "").strip()
            ticker = resolve_ticker_input(raw_ticker)
            source = (form.get("source", [""])[0] or "").strip().lower()
            tab = (form.get("tab", ["watchlist"])[0] or "watchlist").strip().lower()
            return_to = (form.get("return_to", [""])[0] or "").strip()
            render = (lambda msg: universe_html(tab=tab, message=msg)) if source == "universe" else dashboard_html
            if not ticker:
                self._send_html(render(f"No ticker match for '{raw_ticker}'. Use exact ticker (e.g. MOH) or full company name."))
                return
            f = DATA / "my_watchlist.txt"
            f.parent.mkdir(parents=True, exist_ok=True)
            if not f.exists():
                f.write_text("", encoding="utf-8")
            cur = {e["ticker"].upper() for e in read_watchlist_entries(DATA / "my_watchlist.txt")}
            q = get_live_quotes([ticker]).get(ticker, {})
            now_p = q.get("price")
            now_px = f"{float(now_p):.4f}" if isinstance(now_p, float) else ""
            now_ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
            if ticker in cur:
                queue_company_sync(ticker)
                invalidate_l2_cache(start_async=True)
                _onyx_sync_holding(ticker)
                _onyx_log_ticker_evidence(ticker)
                if return_to:
                    self.send_response(302)
                    self.send_header("Location", return_to)
                    self.end_headers()
                    return
                self._send_html(render(f"{ticker} already exists in watchlist. Company sync triggered."))
                return
            with f.open("a", encoding="utf-8") as fh:
                fh.write(f"{ticker},{now_ts},{now_px}\n")
            queue_company_sync(ticker)
            invalidate_l2_cache(start_async=True)
            _onyx_sync_holding(ticker)
            _onyx_log_ticker_evidence(ticker)
            log_watchlist_event(ticker, "added", "Added from UI action.")
            if return_to:
                self.send_response(302)
                self.send_header("Location", return_to)
                self.end_headers()
                return
            self._send_html(render(f"Added {ticker} to watchlist. Company sync triggered."))
            return

        if parsed.path == "/portfolio/add":
            asset_type = (form.get("asset_type", ["stock"])[0] or "stock").strip().lower()
            raw_ticker = (form.get("ticker", [""])[0] or "").strip()
            ticker = resolve_ticker_input(raw_ticker)
            shares = (form.get("shares", [""])[0] or "").strip()
            cost = (form.get("cost_basis", [""])[0] or "").strip()
            notes = (form.get("notes", [""])[0] or "").strip()
            source = (form.get("source", [""])[0] or "").strip().lower()
            tab = (form.get("tab", ["portfolio"])[0] or "portfolio").strip().lower()
            render = (lambda msg: universe_html(tab=tab, message=msg)) if source == "universe" else dashboard_html
            if asset_type not in {"stock", "bond", "cash"}:
                asset_type = "stock"
            if not ticker:
                self._send_html(render(f"No ticker match for '{raw_ticker}'. Use exact ticker (e.g. MOH) or full company name."))
                return
            new_shares = to_float(shares)
            new_cost = to_float(cost)
            if new_shares is None or new_shares <= 0:
                self._send_html(render("Amount/Shares must be a positive number."))
                return

            if asset_type == "cash":
                ccy = ticker.upper().strip()
                if not re.fullmatch(r"[A-Z]{3}", ccy):
                    self._send_html(render("For cash, enter a 3-letter currency (e.g. USD, EUR)."))
                    return
                rows = read_cash_balances(CASH_BALANCES_FILE)
                rows.append({"currency": ccy, "amount": float(new_shares), "note": notes})
                write_cash_balances(CASH_BALANCES_FILE, rows)
                self._send_html(render(f"Added cash balance: {ccy} {float(new_shares):,.2f}."))
                return

            f = DATA / "portfolio.csv"
            f.parent.mkdir(parents=True, exist_ok=True)
            if not f.exists():
                f.write_text("", encoding="utf-8")

            other_rows: list[str] = []
            pos_rows: list[list[str]] = []
            for ln in read_lines(f):
                parts = [p.strip() for p in ln.split(",", 3)]
                while len(parts) < 4:
                    parts.append("")
                if parts[0].upper() == ticker:
                    pos_rows.append(parts[:4])
                else:
                    other_rows.append(ln)

            total_shares = 0.0
            weighted_cost_sum = 0.0
            weighted_shares = 0.0
            note_parts: list[str] = []

            for row in pos_rows:
                sh = to_float(row[1])
                cb = to_float(row[2])
                if sh is not None and sh > 0:
                    total_shares += sh
                    if cb is not None and cb > 0:
                        weighted_cost_sum += sh * cb
                        weighted_shares += sh
                if row[3]:
                    note_parts.append(row[3])

            total_shares += new_shares
            if new_cost is not None and new_cost > 0:
                weighted_cost_sum += new_shares * new_cost
                weighted_shares += new_shares
            if notes:
                note_parts.append(notes)

            avg_cost = (weighted_cost_sum / weighted_shares) if weighted_shares > 0 else None
            merged_notes = " | ".join([n for n in note_parts if n])

            shares_out = f"{int(total_shares)}" if float(total_shares).is_integer() else f"{total_shares:.4f}".rstrip("0").rstrip(".")
            cost_out = f"{avg_cost:.4f}".rstrip("0").rstrip(".") if avg_cost is not None else ""

            other_rows.append(f"{ticker},{shares_out},{cost_out},{merged_notes}")
            f.write_text("\n".join(other_rows) + "\n", encoding="utf-8")
            queue_company_sync(ticker)
            invalidate_l2_cache(start_async=True)
            _onyx_sync_holding(ticker, thesis_hint=notes)
            _onyx_log_ticker_evidence(ticker)
            log_portfolio_event(
                ticker,
                "added" if not pos_rows else "updated",
                f"Shares={shares_out} | AvgCost={cost_out or '-'}",
            )
            if _onyx_run_thesis_check_if_due is not None:
                try:
                    _onyx_run_thesis_check_if_due(ticker, min_interval_seconds=0)
                except Exception:
                    pass
            if not pos_rows:
                self._send_html(render(f"Merged portfolio position for {ticker}: shares={shares_out}, avg_cost={cost_out or '-'}; company sync triggered."))
                return
            self._send_html(render(f"Merged portfolio position for {ticker}: shares={shares_out}, avg_cost={cost_out or '-'}; company sync triggered."))
            return

        if parsed.path == "/watchlist/update":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or ""))
            action = (form.get("action", ["save"])[0] or "save").strip().lower()
            reason = (form.get("reason", [""])[0] or "").strip()
            added_at = (form.get("added_at", [""])[0] or "").strip()
            added_price = (form.get("added_price", [""])[0] or "").strip()
            source = (form.get("source", [""])[0] or "").strip().lower()
            tab = (form.get("tab", ["watchlist"])[0] or "watchlist").strip().lower()
            focus_ticker = resolve_ticker_input((form.get("t", [""])[0] or ""))
            render = (lambda msg: dashboard_html(msg, focus_ticker=focus_ticker)) if source == "dashboard" else (lambda msg: universe_html(tab=tab, message=msg))
            path = DATA / "my_watchlist.txt"
            entries = read_watchlist_entries(path)
            if not ticker:
                self._send_html(render("Ticker is required."))
                return
            if action == "remove":
                if not reason:
                    self._send_html(watchlist_edit_html(ticker, "Removal reason is required."))
                    return
                entries = [e for e in entries if (e.get("ticker", "").upper() != ticker)]
                write_watchlist_entries(path, entries)
                invalidate_l2_cache(start_async=True)
                if _onyx_set_holding_status is not None:
                    try:
                        _onyx_set_holding_status(ticker, status="Inactive")
                    except Exception:
                        pass
                log_watchlist_event(ticker, "removed", reason)
                self._send_html(render(f"Removed {ticker} from watchlist."))
                return
            found = False
            for e in entries:
                if (e.get("ticker", "").upper() == ticker):
                    e["added_at"] = added_at
                    e["added_price"] = added_price
                    found = True
                    break
            if not found:
                entries.append({"ticker": ticker, "added_at": added_at, "added_price": added_price})
            write_watchlist_entries(path, entries)
            queue_company_sync(ticker)
            invalidate_l2_cache(start_async=True)
            _onyx_sync_holding(ticker)
            _onyx_log_ticker_evidence(ticker)
            log_watchlist_event(ticker, "added", "Added/updated from watchlist form.")
            self._send_html(render(f"Updated watchlist entry for {ticker}."))
            return

        if parsed.path == "/portfolio/update":
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or ""))
            action = (form.get("action", ["save"])[0] or "save").strip().lower()
            shares = (form.get("shares", [""])[0] or "").strip()
            cost = (form.get("cost_basis", [""])[0] or "").strip()
            notes = (form.get("notes", [""])[0] or "").strip()
            source = (form.get("source", [""])[0] or "").strip().lower()
            tab = (form.get("tab", ["portfolio"])[0] or "portfolio").strip().lower()
            focus_ticker = resolve_ticker_input((form.get("t", [""])[0] or ""))
            render = (lambda msg: dashboard_html(msg, focus_ticker=focus_ticker)) if source == "dashboard" else (lambda msg: universe_html(tab=tab, message=msg))
            path = DATA / "portfolio.csv"
            rows = read_portfolio_rows(path)
            if not ticker:
                self._send_html(render("Ticker is required."))
                return
            if action == "remove":
                rows = [r for r in rows if (r[0].upper() != ticker)]
                write_portfolio_rows(path, rows)
                invalidate_l2_cache(start_async=True)
                if _onyx_set_holding_status is not None:
                    try:
                        _onyx_set_holding_status(ticker, status="Inactive")
                    except Exception:
                        pass
                log_portfolio_event(ticker, "removed", "Removed from portfolio edit page.")
                self._send_html(render(f"Removed {ticker} from portfolio."))
                return

            sh = to_float(shares)
            if sh is None or sh <= 0:
                self._send_html(portfolio_edit_html(ticker, "Shares must be a positive number."))
                return

            updated = False
            out_rows: list[tuple[str, str, str, str]] = []
            for r in rows:
                if r[0].upper() == ticker:
                    out_rows.append((ticker, shares, cost, notes))
                    updated = True
                else:
                    out_rows.append(r)
            if not updated:
                out_rows.append((ticker, shares, cost, notes))
            write_portfolio_rows(path, out_rows)
            queue_company_sync(ticker)
            invalidate_l2_cache(start_async=True)
            _onyx_sync_holding(ticker, thesis_hint=notes)
            _onyx_log_ticker_evidence(ticker)
            log_portfolio_event(ticker, "updated", f"Shares={shares} | Cost={cost or '-'}")
            if _onyx_run_thesis_check_if_due is not None:
                try:
                    _onyx_run_thesis_check_if_due(ticker, min_interval_seconds=0)
                except Exception:
                    pass
            self._send_html(render(f"Updated portfolio entry for {ticker}."))
            return

        if parsed.path == "/note/add":
            scope = (form.get("scope", ["portfolio"])[0] or "portfolio").strip().lower()
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or ""))
            sentiment = (form.get("sentiment", ["watch"])[0] or "watch").strip().lower()
            tags = (form.get("tags", [""])[0] or "").strip()
            note = (form.get("note", [""])[0] or "").strip()
            t = (form.get("t", [""])[0] or "").strip().upper()
            source = (form.get("source", [""])[0] or "").strip().lower()
            render = (lambda msg: dashboard_html(msg, focus_ticker=t))
            if scope not in {"portfolio", "watchlist"}:
                scope = "portfolio"
            if sentiment not in {"like", "dislike", "watch", "change", "neutral"}:
                sentiment = "watch"
            if not note:
                self._send_html(render("Note text is required."))
                return
            nid = add_note_row(scope=scope, ticker=ticker, sentiment=sentiment, note=note, tags=tags)
            invalidate_l2_cache(start_async=True)
            self._send_html(render(f"Note saved (ID #{nid})."))
            return

        if parsed.path == "/note/import":
            scope = (form.get("scope", ["portfolio"])[0] or "portfolio").strip().lower()
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or ""))
            sentiment = (form.get("sentiment", ["watch"])[0] or "watch").strip().lower()
            tags = (form.get("tags", [""])[0] or "").strip()
            bulk_text = (form.get("bulk_notes", [""])[0] or "").strip()
            if scope not in {"portfolio", "watchlist"}:
                scope = "portfolio"
            if sentiment not in {"like", "dislike", "watch", "change", "neutral"}:
                sentiment = "watch"
            notes = [ln.strip() for ln in bulk_text.splitlines() if ln.strip()]
            if not notes:
                self._send_html(dashboard_html("No notes found to import. Paste one note per line."))
                return

            ok = 0
            for note in notes:
                add_note_row(scope=scope, ticker=ticker, sentiment=sentiment, note=note, tags=tags)
                ok += 1
            invalidate_l2_cache(start_async=True)
            self._send_html(dashboard_html(f"Imported notes: {ok} success, 0 failed."))
            return

        if parsed.path == "/note/update":
            try:
                note_id = int((form.get("id", ["0"])[0] or "0").strip())
            except Exception:
                note_id = 0
            scope = (form.get("scope", ["portfolio"])[0] or "portfolio").strip().lower()
            ticker = resolve_ticker_input((form.get("ticker", [""])[0] or ""))
            sentiment = (form.get("sentiment", ["watch"])[0] or "watch").strip().lower()
            tags = (form.get("tags", [""])[0] or "").strip()
            note = (form.get("note", [""])[0] or "").strip()
            action = (form.get("action", ["save"])[0] or "save").strip().lower()
            if scope not in {"portfolio", "watchlist"}:
                scope = "portfolio"
            if sentiment not in {"like", "dislike", "watch", "change", "neutral"}:
                sentiment = "watch"
            if note_id <= 0:
                self._send_html(dashboard_html("Invalid note ID."))
                return
            if not note:
                self._send_html(note_edit_html(note_id, "Note text cannot be empty."))
                return

            if action == "followup":
                new_id = add_note_row(scope=scope, ticker=ticker, sentiment=sentiment, note=note, tags=tags)
                invalidate_l2_cache(start_async=True)
                self._send_html(dashboard_html(f"Follow-up note added (ID #{new_id})."))
                return

            ok = update_note_row(note_id=note_id, scope=scope, ticker=ticker, sentiment=sentiment, note=note, tags=tags)
            if ok:
                invalidate_l2_cache(start_async=True)
                self._send_html(dashboard_html(f"Note #{note_id} updated."))
            else:
                self._send_html(dashboard_html(f"Note #{note_id} not found."))
            return

        self.send_response(404)
        self.end_headers()
        return

    def _send_html(self, content: str) -> None:
        themed = content
        data = themed.encode("utf-8", errors="ignore")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args) -> None:
        return


def main() -> None:
    p = argparse.ArgumentParser(description="Run local Investor Terminal web app.")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    args = p.parse_args()

    def _startup_background() -> None:
        if _onyx_init_db is not None:
            try:
                _onyx_init_db()
                _onyx_backfill_portfolio_holdings()
            except Exception:
                pass
        unified_on = os.getenv("ONYX_UNIFIED_SCHED_ON", "1").strip().lower() not in {"0", "false", "no", "off"}
        if unified_on:
            start_unified_scheduler()
        else:
            _start_onyx_scheduler(interval_seconds=21600)
            start_l2_scheduler(interval_seconds=21600)
            start_sec_risk_scheduler(interval_seconds=21600)
            start_fast_intel_feed_scheduler(interval_seconds=max(600, int(float(os.getenv("ONYX_FAST_FEED_INTERVAL", "1800").strip() or "1800"))))
            start_intel_feed_scheduler(interval_seconds=max(1800, int(float(os.getenv("ONYX_INTEL_FEED_INTERVAL", "14400").strip() or "14400"))))
            start_intel24_snapshot_scheduler(interval_seconds=max(300, int(float(os.getenv("ONYX_INTEL24_INTERVAL", "900").strip() or "900"))))
            start_workspace_snapshot_scheduler(interval_seconds=max(3600, int(float(os.getenv("ONYX_WORKSPACE_SNAPSHOT_INTERVAL", "21600").strip() or "21600"))))
            start_company_profile_enrich_scheduler(interval_seconds=max(600, int(float(os.getenv("ONYX_COMPANY_PROFILE_ENRICH_INTERVAL", "1800").strip() or "1800"))))
        start_startup_intel_prewarm()

    threading.Thread(target=_startup_background, daemon=True).start()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"http://{args.host}:{args.port}")
    httpd.serve_forever()


def ticker_html(ticker: str) -> str:
    if not ticker:
        return dashboard_html("Ticker is required for insight.")

    red_flag = latest("reports/.terminal_inputs/red_flag_alert_*.txt")
    earnings = _latest_earnings_file()

    alerts = run_cmd(["python3", "tools/red_flag_rank.py", "--file", red_flag, "--limit", "200"]) if red_flag else []
    alert_hits = [a for a in alerts if f"| {ticker} |" in a][:12]

    upcoming = run_cmd(["python3", "tools/earnings_watch_rank.py", "--file", earnings, "--limit", "200", "--scope", "upcoming"]) if earnings else []
    week_events = run_cmd(["python3", "tools/earnings_watch_rank.py", "--file", earnings, "--limit", "200", "--scope", "week"]) if earnings else []
    earn_hits = [e for e in (upcoming + week_events) if f"| {ticker} |" in e][:12]

    notes_rows = list_investor_notes(limit=8, ticker=ticker)
    notes = [
        f"#{int(r['id'])} [{r['scope']}] {(r['created_at'] or '')[:16].replace('T',' ')} - {r['note']}"
        for r in notes_rows
    ]
    decisions = run_cmd(["python3", "research_agent.py", "decision", "list", ticker, "--limit", "8"])

    return f"""<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>{html.escape(ticker)} Insight</title>
<style>
  body {{ margin:0; font-family:"Avenir Next","Helvetica Neue",sans-serif; background:#0b1014; color:#e7eef6; }}
  .wrap {{ max-width:1050px; margin:0 auto; padding:20px; }}
  .card {{ background:#131d25; border:1px solid #294051; border-radius:12px; padding:14px; margin-bottom:12px; }}
  h1,h2 {{ margin:0 0 8px 0; }} ul {{ margin:0; padding-left:18px; }} li {{ margin:4px 0; }} a {{ color:#9ad0ff; }}
</style></head><body><div class='wrap'>
  <div class='card'><h1>{html.escape(ticker)} - Quick Insight</h1><a href='/'>Back to Dashboard</a></div>
  <div class='card'><h2>Earnings Signals</h2><ul>{render_list(earn_hits, "No current earnings lines found for this ticker.")}</ul></div>
  <div class='card'><h2>Red-Flag Signals</h2><ul>{render_list(alert_hits, "No current red-flag lines found for this ticker.")}</ul></div>
  <div class='card'><h2>Recent Notes</h2><ul>{render_list(notes, "No notes found for this ticker.")}</ul></div>
  <div class='card'><h2>Decision Memos</h2><ul>{render_list(decisions, "No decision memos found for this ticker.")}</ul></div>
</div></body></html>"""


def insider_summary_html(ticker: str, meaningful_only: bool = False) -> str:
    t = (ticker or "").strip().upper()
    if not t:
        return dashboard_html("Ticker is required.")

    d = _sec_insider_details(t, months=18, meaningful_usd=100000.0)
    raw_events = [r for r in d.get("events", []) if isinstance(r, dict)]
    events = [e for e in raw_events if bool(e.get("meaningful"))] if meaningful_only else raw_events

    def aggregate(code: str) -> list[dict[str, object]]:
        by_person: dict[str, dict[str, object]] = {}
        for e in events:
            if str(e.get("code")) != code:
                continue
            key = f"{e.get('name','')}|{e.get('role','')}"
            row = by_person.get(key)
            if row is None:
                row = {
                    "name": str(e.get("name", "-")),
                    "role": str(e.get("role", "-")),
                    "trades": 0,
                    "shares": 0.0,
                    "value": 0.0,
                    "last_date": str(e.get("date", "-")),
                }
                by_person[key] = row
            row["trades"] = int(row["trades"]) + 1
            row["shares"] = float(row["shares"]) + float(_to_num(e.get("shares")) or 0.0)
            row["value"] = float(row["value"]) + float(_to_num(e.get("value")) or 0.0)
            if str(e.get("date", "")) > str(row["last_date"]):
                row["last_date"] = str(e.get("date", "-"))
        out_rows = []
        for row in by_person.values():
            sh = float(row["shares"]) or 0.0
            val = float(row["value"]) or 0.0
            row["avg_price"] = (val / sh) if sh > 0 else 0.0
            out_rows.append(row)
        out_rows.sort(key=lambda x: float(x["value"]), reverse=True)
        return out_rows

    buyers = aggregate("P")
    sellers = aggregate("S")

    def rows_for(items: list[dict[str, object]]) -> str:
        if not items:
            return "<tr><td colspan='7' class='muted'>No records</td></tr>"
        out = []
        for r in items[:20]:
            out.append(
                "<tr>"
                f"<td>{html.escape(str(r.get('name','-')))}</td>"
                f"<td>{html.escape(str(r.get('role','-')))}</td>"
                f"<td>{html.escape(str(r.get('trades','-')))}</td>"
                f"<td>{float(_to_num(r.get('shares')) or 0.0):,.0f}</td>"
                f"<td>{fmt_money(_to_num(r.get('avg_price')))}</td>"
                f"<td>{fmt_money(_to_num(r.get('value')))}</td>"
                f"<td>{html.escape(str(r.get('last_date','-')))}</td>"
                "</tr>"
            )
        return "".join(out)

    recent_rows = []
    for e in events[:30]:
        code = str(e.get("code", "-"))
        side = "BUY" if code == "P" else ("SELL" if code == "S" else code)
        recent_rows.append(
            "<tr>"
            f"<td>{html.escape(str(e.get('date','-')))}</td>"
            f"<td>{html.escape(side)}</td>"
            f"<td>{html.escape(str(e.get('name','-')))}</td>"
            f"<td>{html.escape(str(e.get('role','-')))}</td>"
            f"<td>{float(_to_num(e.get('shares')) or 0.0):,.0f}</td>"
            f"<td>{fmt_money(_to_num(e.get('price')))}</td>"
            f"<td>{fmt_money(_to_num(e.get('value')))}</td>"
            "</tr>"
        )
    recent_html = "".join(recent_rows) if recent_rows else "<tr><td colspan='7' class='muted'>No recent open-market Form 4 rows.</td></tr>"

    buy_total = sum(float(_to_num(x.get("value")) or 0.0) for x in buyers)
    sell_total = sum(float(_to_num(x.get("value")) or 0.0) for x in sellers)
    buy_sh = sum(float(_to_num(x.get("shares")) or 0.0) for x in buyers)
    sell_sh = sum(float(_to_num(x.get("shares")) or 0.0) for x in sellers)
    buy_avg = (buy_total / buy_sh) if buy_sh > 0 else None
    sell_avg = (sell_total / sell_sh) if sell_sh > 0 else None

    mode_label = "Meaningful only (>= $100k)" if meaningful_only else "All open-market trades"
    toggle_href = f"/insider?t={html.escape(t)}" if meaningful_only else f"/insider?t={html.escape(t)}&meaningful=1"
    toggle_text = "Show All Trades" if meaningful_only else "Show Meaningful Only (>= $100k)"

    return f"""<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>{html.escape(t)} Insider Summary</title>
<style>
  body {{ margin:0; font-family:"Avenir Next","Helvetica Neue",sans-serif; background:#0b1014; color:#e7eef6; }}
  .wrap {{ max-width:1200px; margin:0 auto; padding:20px; }}
  .card {{ background:#131d25; border:1px solid #294051; border-radius:12px; padding:14px; margin-bottom:12px; }}
  .btn {{ background:#1a3d56; color:#e7eef6; border:1px solid #2e5c7b; border-radius:8px; padding:6px 10px; text-decoration:none; }}
  table {{ width:100%; border-collapse:collapse; font-size:14px; }}
  th,td {{ border-bottom:1px solid #294051; padding:7px; text-align:left; }}
  .muted {{ color:#9ab0c0; }}
</style></head><body><div class='wrap'>
  <div class='card'><h1>{html.escape(t)} Insider Summary (18 Months)</h1><a class='btn' href='/?t={html.escape(t)}'>Back to Smart Feed</a> <a class='btn' href='{toggle_href}'>{toggle_text}</a><div class='muted' style='margin-top:8px;'>View: {mode_label}</div></div>
  <div class='card'>
    <h2>Quick Totals</h2>
    <ul>
      <li>Total Buy Value: {fmt_money(buy_total)} | Avg Buy Price: {fmt_money(buy_avg)}</li>
      <li>Total Sell Value: {fmt_money(sell_total)} | Avg Sell Price: {fmt_money(sell_avg)}</li>
      <li>Net Flow: {fmt_money(buy_total - sell_total)}</li>
    </ul>
    <div class='muted'>Source: SEC Form 4 open-market rows parsed from local filings cache.</div>
  </div>
  <div class='card'><h2>Top Buyers</h2>
    <table><thead><tr><th>Name</th><th>Role</th><th>Trades</th><th>Shares</th><th>Avg Price</th><th>Total Value</th><th>Last Date</th></tr></thead>
    <tbody>{rows_for(buyers)}</tbody></table>
  </div>
  <div class='card'><h2>Top Sellers</h2>
    <table><thead><tr><th>Name</th><th>Role</th><th>Trades</th><th>Shares</th><th>Avg Price</th><th>Total Value</th><th>Last Date</th></tr></thead>
    <tbody>{rows_for(sellers)}</tbody></table>
  </div>
  <div class='card'><h2>Recent Transactions</h2>
    <table><thead><tr><th>Date</th><th>Side</th><th>Name</th><th>Role</th><th>Shares</th><th>Price</th><th>Value</th></tr></thead>
    <tbody>{recent_html}</tbody></table>
  </div>
</div></body></html>"""


def safe_resolve_file(path_s: str) -> Path | None:
    if not path_s:
        return None
    p = Path(path_s).expanduser()
    if not p.is_absolute():
        p = (ROOT / p).resolve()
    else:
        p = p.resolve()
    allowed = [ROOT / "filings", ROOT / "filing_docs", ROOT / "reports"]
    if not any(str(p).startswith(str(a.resolve())) for a in allowed):
        return None
    if not p.exists() or not p.is_file():
        return None
    return p


def safe_read_file(path_s: str) -> str:
    p = safe_resolve_file(path_s)
    if p is None:
        return ""
    return p.read_text(encoding="utf-8", errors="ignore")


def _competitor_landscape(ticker: str) -> dict[str, object]:
    t = (ticker or "").strip().upper()
    if not t:
        return {"source": "N/A", "source_detail": "Ticker missing.", "competitors": []}
    ck = f"comphunt:{t}"
    cached = _cache_get(ck, ttl_seconds=6 * 3600)
    if isinstance(cached, dict):
        return cached
    try:
        p = subprocess.run(
            ["python3", "tools/competitor_hunter.py", "--ticker", t],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=45,
        )
        rc, out, err = p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        rc, out, err = 1, "", "timeout"
    data: dict[str, object] = {
        "source": "N/A",
        "source_detail": "Competitor engine unavailable.",
        "competitors": [],
    }
    if rc == 0 and out.strip():
        try:
            parsed = json.loads(out.strip())
            if isinstance(parsed, dict):
                data = parsed
        except Exception:
            pass
    elif err.strip():
        data["source_detail"] = f"Engine error: {err.strip()[:180]}"
    _cache_put(ck, data)
    return data


def _fmt_cell_num(v: object, suffix: str = "") -> str:
    n = _to_num(v)
    if n is None:
        return "-"
    return f"{n:.2f}{suffix}"


def render_research_tab(
    ticker: str,
    earn_hits: list[object],
    alert_hits: list[object],
    filing_rows: str,
    change_rows: str,
    transcript_rows: str,
    note_rows: str,
) -> str:
    t = (ticker or "").strip().upper()
    comp = _competitor_landscape(t)
    source = html.escape(str(comp.get("source", "N/A")))
    source_detail = html.escape(str(comp.get("source_detail", "")))
    comp_rows_data = [r for r in (comp.get("competitors") or []) if isinstance(r, dict)]
    comp_rows_html: list[str] = []
    for r in comp_rows_data:
        winner = bool(r.get("winner"))
        cls = " class='winner'" if winner else ""
        comp_rows_html.append(
            "<tr>"
            f"<td{cls}>{html.escape(str(r.get('symbol', '-')))}</td>"
            f"<td{cls}>{html.escape(str(r.get('name', '-')))}</td>"
            f"<td{cls}>{html.escape(_fmt_cell_num(r.get('pe'), 'x'))}</td>"
            f"<td{cls}>{html.escape(_fmt_cell_num(r.get('profit_margin'), '%'))}</td>"
            f"<td{cls}>{html.escape(_fmt_cell_num(r.get('ytd_return'), '%'))}</td>"
            "</tr>"
        )
    comp_table = (
        "<table><thead><tr><th>Ticker</th><th>Company</th><th>P/E</th><th>Profit Margin</th><th>YTD Return</th></tr></thead>"
        f"<tbody>{''.join(comp_rows_html)}</tbody></table>"
        if comp_rows_html
        else "<div class='muted'>No competitors found from 10-K parsing. Fallback peers unavailable.</div>"
    )

    filing_block = (
        "<table><thead><tr><th>Form</th><th>Date</th><th>Accession</th><th>Open</th></tr></thead><tbody>"
        + filing_rows
        + "</tbody></table>"
    ) if filing_rows else "<div class='muted'>No filings in local DB yet. Click Resync Company.</div>"
    change_block = (
        "<table><thead><tr><th>Date</th><th>Form</th><th>Section</th><th>Type</th><th>Summary</th></tr></thead><tbody>"
        + change_rows
        + "</tbody></table>"
    ) if change_rows else "<div class='muted'>No detected changes yet.</div>"
    transcript_block = (
        "<table><thead><tr><th>Form</th><th>Date</th><th>Excerpt</th></tr></thead><tbody>"
        + transcript_rows
        + "</tbody></table>"
    ) if transcript_rows else "<div class='muted'>No transcript markers found yet in local filing extracts.</div>"
    notes_block = (
        "<table><thead><tr><th>ID</th><th>Created</th><th>Sentiment</th><th>Note</th><th>Action</th></tr></thead><tbody>"
        + note_rows
        + "</tbody></table>"
    ) if note_rows else "<div class='muted'>No notes yet for this ticker.</div>"

    return (
        "<div class='grid'>"
        f"<section class='card c6'><h2>Earnings Signals</h2><ul>{render_signal_list(earn_hits, 'No current earnings signals.')}</ul></section>"
        f"<section class='card c6'><h2>COMPETITIVE LANDSCAPE (Source: 10-K)</h2><div class='muted' style='margin-bottom:8px;'>Route: {source} | {source_detail}</div>{comp_table}<div class='muted' style='margin-top:6px;'>Green highlight = Winner (highest margin / best return).</div></section>"
        f"<section class='card c12'><h2>Red-Flag Signals</h2><ul>{render_signal_list(alert_hits, 'No current red-flag signals.')}</ul></section>"
        f"<section class='card c12'><h2>Latest Filings</h2>{filing_block}</section>"
        f"<section class='card c12'><h2>Detected Filing Changes</h2>{change_block}</section>"
        f"<section class='card c12'><h2>Earnings Transcript Hints (if detected in filings)</h2>{transcript_block}</section>"
        f"<section class='card c12'><h2>Your Notes For {html.escape(t)}</h2>{notes_block}</section>"
        "</div>"
    )


def company_html(ticker: str, tab: str = "overview", run_lens: bool = False) -> str:
    t = ticker.upper().strip()
    if not t:
        return dashboard_html("Ticker is required.")

    snap = get_company_snapshot(t)
    company = snap.get("company")
    filings = list(snap.get("filings") or [])
    changes = list(snap.get("changes") or [])
    sync = snap.get("sync", {"running": False, "last": "never", "result": "-", "message": ""})

    def _filing_rows(rows: list[object]) -> str:
        return "".join(
            f"<tr><td>{html.escape(str(r['form']))}</td><td>{html.escape(str(r['date']))}</td>"
            f"<td>{html.escape(str(r['accession']))}</td>"
            f"<td><a class='btn' href='/filing?path={urllib.parse.quote(str(r['path'] or ''), safe='')}&doc={urllib.parse.quote(str(r['doc_url'] or ''), safe='') if str(r['doc_url'] or '').strip() else ''}'>Open</a></td></tr>"
            for r in rows
        )
    def _form_of(r: object) -> str:
        try:
            return str(r["form"] or "")  # type: ignore[index]
        except Exception:
            return ""

    change_rows = "".join(
        f"<tr><td>{html.escape(str(r['date']))}</td><td>{html.escape(str(r['form']))}</td><td>{html.escape(str(r['section_name']))}</td>"
        f"<td>{html.escape(str(r['change_type']))}</td><td>{html.escape(str((r['summary'] or '')[:170]))}</td></tr>"
        for r in changes
    )

    cname = html.escape(str(company["name"])) if company else t
    cik = html.escape(str(company["cik"])) if company else "-"
    sync_state = "running" if sync.get("running") else sync.get("result", "-")
    sync_line = f"{sync_state} | {html.escape(str(sync.get('last','never')))} | {html.escape(str(sync.get('message','')))}"

    def _is_sec3(form: str) -> bool:
        f = (form or "").upper().replace(" ", "")
        return f in {"10-K", "10-Q", "8-K"}

    sec3 = [r for r in filings if _is_sec3(_form_of(r))]
    latest_rows = _filing_rows(sec3[:50])
    all_rows = _filing_rows(sec3)
    tenk_rows = _filing_rows([r for r in sec3 if _form_of(r).upper().replace(" ", "") == "10-K"])
    tenq_rows = _filing_rows([r for r in sec3 if _form_of(r).upper().replace(" ", "") == "10-Q"])
    eightk_rows = _filing_rows([r for r in sec3 if _form_of(r).upper().replace(" ", "") == "8-K"])

    overview_filing_block = (
        "<div class='table-wrap'><table class='dense'><thead><tr><th>Form</th><th>Date</th><th>Accession</th><th>Open</th></tr></thead><tbody>"
        + latest_rows
        + "</tbody></table></div>"
    ) if latest_rows else "<div class='muted'>No SEC 10-K / 10-Q / 8-K filings in local DB yet. Click Resync Company.</div>"
    overview_change_block = (
        "<div class='table-wrap'><table class='dense'><thead><tr><th>Date</th><th>Form</th><th>Section</th><th>Type</th><th>Summary</th></tr></thead><tbody>"
        + change_rows
        + "</tbody></table></div>"
    ) if change_rows else "<div class='muted'>No detected changes yet.</div>"
    all_filing_block = (
        "<div class='table-wrap'><table class='dense'><thead><tr><th>Form</th><th>Date</th><th>Accession</th><th>Open</th></tr></thead><tbody>"
        + all_rows
        + "</tbody></table></div>"
    ) if all_rows else "<div class='muted'>No SEC 3-category filings found yet.</div>"
    tenk_block = (
        "<div class='table-wrap'><table class='dense'><thead><tr><th>Form</th><th>Date</th><th>Accession</th><th>Open</th></tr></thead><tbody>"
        + tenk_rows
        + "</tbody></table></div>"
    ) if tenk_rows else "<div class='muted'>No 10-K filings found.</div>"
    tenq_block = (
        "<div class='table-wrap'><table class='dense'><thead><tr><th>Form</th><th>Date</th><th>Accession</th><th>Open</th></tr></thead><tbody>"
        + tenq_rows
        + "</tbody></table></div>"
    ) if tenq_rows else "<div class='muted'>No 10-Q filings found.</div>"
    eightk_block = (
        "<div class='table-wrap'><table class='dense'><thead><tr><th>Form</th><th>Date</th><th>Accession</th><th>Open</th></tr></thead><tbody>"
        + eightk_rows
        + "</tbody></table></div>"
    ) if eightk_rows else "<div class='muted'>No 8-K filings found.</div>"

    body_block = (
        "<div class='grid'>"
        f"<section class='card c12'><details open><summary>Latest Filings</summary>{overview_filing_block}</details></section>"
        f"<section class='card c12'><details open><summary>Detected Filing Changes</summary>{overview_change_block}</details></section>"
        f"<section class='card c12'><details><summary>All SEC Filings (3 Categories)</summary>{all_filing_block}</details></section>"
        f"<section class='card c4'><details><summary>10-K</summary>{tenk_block}</details></section>"
        f"<section class='card c4'><details><summary>10-Q</summary>{tenq_block}</details></section>"
        f"<section class='card c4'><details><summary>8-K</summary>{eightk_block}</details></section>"
        "</div>"
    )

    return f"""<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>{t} SEC Filings</title>
<style>
  body {{ margin:0; font-family:"Avenir Next","Helvetica Neue",sans-serif; background:#0b1014; color:#e7eef6; }}
  .wrap {{ max-width:1280px; margin:0 auto; padding:20px; }}
  .grid {{ display:grid; grid-template-columns:repeat(12, 1fr); gap:12px; }}
  .card {{ background:#131d25; border:1px solid #294051; border-radius:12px; padding:14px; }}
  .c4 {{ grid-column:span 4; }} .c6 {{ grid-column:span 6; }} .c12 {{ grid-column:span 12; }}
  .table-wrap {{ overflow:auto; border:1px solid #253a4a; border-radius:10px; margin-top:8px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  table.dense {{ min-width:980px; }}
  th,td {{ border-bottom:1px solid #294051; padding:7px 8px; text-align:left; vertical-align:top; line-height:1.35; }}
  th {{ position:sticky; top:0; background:#10202b; color:#c8deef; z-index:1; }}
  tr:nth-child(odd) td {{ background:#101920; }}
  tr:nth-child(even) td {{ background:#0e171e; }}
  tr:hover td {{ background:#172633 !important; }}
  .btn {{ background:#1a3d56; color:#e7eef6; border:1px solid #2e5c7b; border-radius:8px; padding:6px 10px; text-decoration:none; }}
  details > summary {{ list-style:none; cursor:pointer; font-weight:700; color:#d9e9f7; }}
  details > summary::-webkit-details-marker {{ display:none; }}
  ul {{ margin:0; padding-left:18px; }} li {{ margin:4px 0; }} .muted {{ color:#9ab0c0; }}
  @media (max-width:900px) {{ .c4,.c6,.c12 {{ grid-column:span 12; }} }}
</style></head><body><div class='wrap'>
  <div class='card c12'>
    <h1>{html.escape(t)} SEC Filings</h1>
    <div>{cname} | CIK: {cik}</div>
    <div class='muted'>Sync: {sync_line}</div>
    <div class='muted'>Only filings are shown: Latest Filings, Detected Filing Changes, and SEC 3-category views.</div>
    <div style='margin-top:8px;'><a class='btn' href='/'>Home</a> <a class='btn' href='/universe'>My Companies</a> <a class='btn' href='/sync?t={html.escape(t)}&tab=overview'>Resync Company</a></div>
  </div>
  {body_block}
</div></body></html>"""


def _kickoff_company_memo_prep(ticker: str, force: bool = False) -> None:
    t = resolve_ticker_input(ticker)
    if not t:
        return

    def _worker() -> None:
        try:
            queue_company_sync(t)
        except Exception:
            pass
        try:
            generate_deep_dive(t, force=force, max_age_minutes=720)
        except Exception:
            pass
        try:
            perform_mda_diff(t)
        except Exception:
            pass
        if _onyx_run_thesis_check_if_due is not None:
            try:
                _onyx_run_thesis_check_if_due(t, min_interval_seconds=0)
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True).start()


def one_page_memo_html(ticker: str, refresh: bool = False, note_message: str = "") -> str:
    t = resolve_ticker_input(ticker)
    if not t:
        return dashboard_html("Ticker is required for SEC Filings.")

    if refresh:
        _kickoff_company_memo_prep(t, force=True)

    snap = get_company_snapshot(t)
    company = snap.get("company")
    filings = list(snap.get("filings") or [])
    changes = list(snap.get("changes") or [])
    earn_hits = list(snap.get("earn_hits") or [])
    alert_hits = list(snap.get("alert_hits") or [])

    mda: dict[str, object] = {}
    with LOCK:
        mda_keys = [
            k for k in MDA_DIFF_CACHE.keys()
            if str(k).startswith(f"{t}:") and str(k).endswith(":mda_v2")
        ]
        if mda_keys:
            mk = sorted(mda_keys, key=lambda x: float(MDA_DIFF_CACHE.get(x, {}).get("ts", 0.0)), reverse=True)[0]
            mc = MDA_DIFF_CACHE.get(mk, {})
            if isinstance(mc.get("result"), dict):
                mda = dict(mc.get("result") or {})
    dd_file = latest_deep_dive_file(t)
    dd_raw = _safe_read(dd_file)
    def _rv(row: object, key: str, default: str = "") -> str:
        if isinstance(row, dict):
            return str(row.get(key) or default)
        try:
            return str(row[key] or default)  # type: ignore[index]
        except Exception:
            return default

    def _pick(d: dict[str, object], key: str) -> str:
        v = str(d.get(key) or "").strip()
        return v if v else "Not clearly disclosed in current extracted text."

    def _memo_line(*vals: str) -> str:
        for v in vals:
            s = str(v or "").strip()
            if s:
                return s
        return "Not clearly disclosed in current extracted text."

    filing = _latest_filing_for_financial_intel(t)
    filing_path = str(filing.get("path") or "")
    filing_text = _read_filing_text(filing_path, max_chars=260000) if filing_path else ""
    focus_txt = _extract_mda_financial_focus(filing_text, max_chars=14000)

    mda_sections = _mda_sections_from_result(mda) if bool(mda.get("ok")) else {}

    dd_lines = [ln.strip() for ln in dd_raw.splitlines() if ln.strip() and not ln.strip().startswith("#") and not ln.strip().startswith("_Generated")]
    dd_excerpt = " ".join(dd_lines[:4])
    if len(dd_excerpt) > 520:
        dd_excerpt = dd_excerpt[:519].rstrip() + "..."
    if not dd_excerpt:
        dd_excerpt = "Deep dive narrative is being prepared in the background."

    filing_rows = "".join(
        f"<tr><td>{html.escape(_rv(r, 'form', '-'))}</td><td>{html.escape(_rv(r, 'date', '-'))}</td>"
        f"<td>{html.escape(_rv(r, 'accession', '-'))}</td>"
        f"<td><a class='btn' href='/filing?path={urllib.parse.quote(_rv(r, 'path', ''), safe='')}&doc={urllib.parse.quote(_rv(r, 'doc_url', ''), safe='') if _rv(r, 'doc_url', '').strip() else ''}'>Open</a></td></tr>"
        for r in filings[:8]
    ) or "<tr><td colspan='4' class='muted'>No local filings loaded yet. Use Refresh SEC Filings after sync.</td></tr>"

    latest_form = _rv(filings[0], "form", "-") if filings else "-"
    latest_date = _rv(filings[0], "date", "-") if filings else "-"
    cname = html.escape(_rv(company, "name", t))

    rev_line = _memo_line(_best_sentence_by_keywords(focus_txt, ("revenue", "volume", "pricing", "demand")))
    margin_line = _memo_line(_best_sentence_by_keywords(focus_txt, ("margin", "gross margin", "operating margin", "cost", "expense")))
    guidance_line = _memo_line(_best_sentence_by_keywords(focus_txt, ("guidance", "outlook", "expect", "forecast")))
    capex_line = _memo_line(_best_sentence_by_keywords(focus_txt, ("cash flow", "free cash flow", "capex", "liquidity")))
    risk_line = _memo_line(_best_sentence_by_keywords(focus_txt, ("risk", "uncertainty", "headwind", "pressure", "debt")))
    governance_line = "Owner Lens disabled by configuration."
    mda_change_line = _memo_line(
        "; ".join(mda_sections.get("what_changed", [])[:2]) if mda_sections else "",
        "; ".join(mda_sections.get("guidance", [])[:2]) if mda_sections else "",
    )

    earn_points = "".join(f"<li>{html.escape(str(x))}</li>" for x in earn_hits[:4]) or "<li class='muted'>No active earnings signals.</li>"
    alert_points = "".join(f"<li>{html.escape(str(x))}</li>" for x in alert_hits[:4]) or "<li class='muted'>No active red-flag signals.</li>"
    mda_status = "Ready" if bool(mda.get("ok")) else f"Pending ({html.escape(str(mda.get('detail') or mda.get('error') or 'not ready'))})"
    dd_name = html.escape(Path(dd_file).name) if dd_file else "-"
    ws_rows = list_workspace_journal(t, limit=10)
    ws_notes_html = "".join(
        (
            "<div class='note-item'>"
            f"<div class='note-meta'>{html.escape(str((r['created_at'] or '')[:16]))} | {html.escape(str(r['action'] or '-'))} | {html.escape(str(r['emotion'] or '-'))}</div>"
            f"<div>{html.escape(str(r['note'] or ''))}</div>"
            "</div>"
        )
        for r in ws_rows
    ) or "<div class='muted'>No company notes yet for this ticker.</div>"
    ws_link = f"/company_file?t={html.escape(t)}"

    msg_html = f"<div class='memo-msg'>{html.escape(note_message)}</div>" if note_message else ""

    return f"""<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>{html.escape(t)} SEC Filings</title>
<style>
  body {{ margin:0; font-family:"Avenir Next","Helvetica Neue",sans-serif; background:#0b1014; color:#e7eef6; }}
  .wrap {{ max-width:1120px; margin:0 auto; padding:18px; }}
  .card {{ background:#131d25; border:1px solid #294051; border-radius:12px; padding:14px; margin-bottom:12px; }}
  .grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
  .memo-line {{ border:1px solid #2a4659; border-radius:10px; background:#10202b; padding:10px; margin-top:8px; }}
  .memo-line h3 {{ margin:0 0 6px 0; font-size:12px; text-transform:uppercase; color:#c8deef; letter-spacing:.4px; }}
  .note-item {{ border:1px solid #2a4659; border-radius:8px; background:#0f1a23; padding:8px; margin-top:8px; }}
  .note-meta {{ color:#9ab0c0; font-size:11px; margin-bottom:4px; }}
  .memo-msg {{ margin-top:8px; border:1px solid #2e5c7b; border-radius:8px; background:#132838; padding:8px; color:#cfe7f7; }}
  .note-form {{ margin-top:8px; border:1px solid #2a4659; border-radius:8px; background:#0f1a23; padding:8px; }}
  .note-form input,.note-form textarea,.note-form select {{ width:100%; box-sizing:border-box; margin-top:6px; border:1px solid #2e5c7b; border-radius:8px; background:#0b141b; color:#e7eef6; padding:7px; }}
  .note-form .row {{ display:grid; grid-template-columns:1fr 1fr; gap:8px; }}
  .btn {{ background:#1a3d56; color:#e7eef6; border:1px solid #2e5c7b; border-radius:8px; padding:6px 10px; text-decoration:none; display:inline-block; margin-right:6px; }}
  .muted {{ color:#9ab0c0; }}
  .table-wrap {{ overflow:auto; border:1px solid #294051; border-radius:10px; margin-top:8px; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th,td {{ border-bottom:1px solid #294051; padding:7px 8px; text-align:left; vertical-align:top; line-height:1.35; }}
  th {{ background:#10202b; color:#c8deef; }}
  ul {{ margin:0; padding-left:18px; }}
  @media (max-width:900px) {{ .grid {{ grid-template-columns:1fr; }} }}
</style></head><body><div class='wrap'>
  <div class='card'>
    <h1>{html.escape(t)} - SEC Filings</h1>
    <div>{cname}</div>
    <div class='muted'>Latest filing: {html.escape(latest_form)} ({html.escape(latest_date)}) | Total filings loaded: {len(filings)} | Filing changes: {len(changes)}</div>
    <div style='margin-top:8px;'><a class='btn' href='/'>Home</a><a class='btn' href='/universe'>My Companies</a><a class='btn' href='/company?t={html.escape(t)}'>Refresh SEC Filings</a><a class='btn' href='/company?t={html.escape(t)}&tab=overview'>Company Detail</a></div>
    <div class='muted' style='margin-top:8px;'>Mode: Item 7 / MD&A + Financials only | MD&A diff: {mda_status} | Deep Dive file: {dd_name}</div>
    {msg_html}
  </div>

  <div class='grid'>
    <div class='card'>
      <h2>Investment Narrative</h2>
      <div class='memo-line'><h3>Quality Check</h3><div><b>Revenue driver:</b> {html.escape(rev_line)}</div><div style='margin-top:4px;'><b>Margin story:</b> {html.escape(margin_line)}</div></div>
      <div class='memo-line'><h3>Future Signal</h3><div>{html.escape(guidance_line)}</div></div>
      <div class='memo-line'><h3>Capex / Cash Reality</h3><div>{html.escape(capex_line)}</div></div>
      <div class='memo-line'><h3>Risk + Governance</h3><div><b>Risk:</b> {html.escape(risk_line)}</div><div style='margin-top:4px;'><b>Governance:</b> {html.escape(governance_line)}</div></div>
    </div>
    <div class='card'>
      <h2>Company Notes + Signals</h2>
      <div class='memo-line'><h3>Your Company Notes</h3><div><a class='btn' href='{ws_link}'>Open Company File</a></div>{ws_notes_html}
        <form class='note-form' method='post' action='/memo/note'>
          <input type='hidden' name='ticker' value='{html.escape(t)}'>
          <div class='row'>
            <div><label>Action</label><select name='action'><option>Note</option><option>Buy</option><option>Sell</option><option>Mistake</option><option>Lesson</option></select></div>
            <div><label>Emotion</label><select name='emotion'><option>Calm</option><option>Excited</option><option>Anxious</option></select></div>
          </div>
          <div><label>Quick note</label><textarea name='note' rows='3' placeholder='Write your company note...'></textarea></div>
          <div style='margin-top:8px;'><button type='submit' class='btn'>Save Note</button></div>
        </form>
      </div>
      <div class='memo-line'><h3>Deep Dive Summary</h3><div>{html.escape(dd_excerpt)}</div></div>
      <div class='memo-line'><h3>MD&A Change Signal</h3><div>{html.escape(mda_change_line)}</div></div>
      <div class='memo-line'><h3>Earnings Cues</h3><ul>{earn_points}</ul></div>
      <div class='memo-line'><h3>Red-Flag Cues</h3><ul>{alert_points}</ul></div>
    </div>
  </div>

  <div class='card'>
    <h2>Source Pack (SEC + Local)</h2>
    <div class='table-wrap'><table><thead><tr><th>Form</th><th>Date</th><th>Accession</th><th>Open</th></tr></thead><tbody>{filing_rows}</tbody></table></div>
  </div>
</div></body></html>"""


def note_edit_html(note_id: int, message: str = "") -> str:
    row = get_note(note_id)
    if not row:
        return dashboard_html("Note not found.")
    msg_html = f"<div class='msg'>{html.escape(message)}</div>" if message else ""
    scope = row["scope"] or "portfolio"
    ticker = row["ticker"] or ""
    sentiment = row["sentiment"] or "watch"
    tags = row["tags"] or ""
    note = row["note"] or ""
    created = (row["created_at"] or "")[:19].replace("T", " ")
    return f"""<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Edit Note #{int(row['id'])}</title>
<style>
  body {{ margin:0; font-family:"Avenir Next","Helvetica Neue",sans-serif; background:#0b1014; color:#e7eef6; }}
  .wrap {{ max-width:900px; margin:0 auto; padding:20px; }}
  .card {{ background:#131d25; border:1px solid #294051; border-radius:12px; padding:14px; margin-bottom:12px; }}
  .msg {{ margin-top:10px; background:#1f3445; border:1px solid #31536c; border-radius:8px; padding:8px; }}
  input,select,textarea {{ width:100%; padding:8px; border-radius:8px; border:1px solid #2e5c7b; background:#0f1921; color:#e7eef6; margin:6px 0; }}
  button,a.btn {{ background:#1a3d56; color:#e7eef6; border:1px solid #2e5c7b; border-radius:8px; padding:8px 12px; text-decoration:none; cursor:pointer; margin-right:8px; }}
</style></head><body><div class='wrap'>
  <div class='card'><h1>Edit Note #{int(row['id'])}</h1><div>Created: {html.escape(created)} | <a class='btn' href='/'>Back to Dashboard</a></div></div>
  {msg_html}
  <div class='card'>
    <form method='post' action='/note/update'>
      <input type='hidden' name='id' value='{int(row['id'])}'>
      <label>Scope</label>
      <select name='scope'>
        <option value='portfolio' {'selected' if scope=='portfolio' else ''}>portfolio</option>
        <option value='watchlist' {'selected' if scope=='watchlist' else ''}>watchlist</option>
      </select>
      <label>Ticker (optional)</label>
      <input name='ticker' value='{html.escape(ticker)}'>
      <label>Sentiment</label>
      <input name='sentiment' value='{html.escape(sentiment)}'>
      <label>Tags</label>
      <input name='tags' value='{html.escape(tags)}'>
      <label>Note text</label>
      <textarea name='note' rows='8'>{html.escape(note)}</textarea>
      <button name='action' value='save'>Save Changes</button>
      <button name='action' value='followup'>Add As Follow-up Note</button>
    </form>
  </div>
</div></body></html>"""


def _one_line(s: str, max_chars: int = 420) -> str:
    txt = re.sub(r"\s+", " ", (s or "").strip())
    # Strip common 10-K boilerplate that pollutes "About" snippets.
    txt = re.sub(r"documents incorporated by reference.*?(?=item\s+1\.|business)", " ", txt, flags=re.IGNORECASE)
    txt = re.sub(r"table of contents.*?(?=item\s+1\.|business)", " ", txt, flags=re.IGNORECASE)
    txt = re.sub(r"part i item 1[a-z]?\..*?(?=business)", " ", txt, flags=re.IGNORECASE)
    txt = re.sub(r"\s+", " ", txt).strip()
    if len(txt) <= max_chars:
        return txt
    return txt[: max_chars - 1].rstrip() + "..."


def _extract_key_points(text: str, keywords: tuple[str, ...], max_points: int = 3, max_len: int = 280) -> list[str]:
    merged = text or ""
    if not merged.strip():
        return []
    merged = re.sub(r"[ \t]+", " ", merged)
    chunks = re.split(r"(?<=[\.\;\:])\s+|\n+", merged)
    out: list[str] = []
    seen: set[str] = set()
    for c in chunks:
        s = (c or "").strip()
        if len(s) < 30:
            continue
        low = s.lower()
        if "table of contents" in low or "documents incorporated by reference" in low:
            continue
        if any(x in low for x in ("indemnif", "director", "officer", "proceeding", "litigation", "proxy statement")):
            continue
        if not any(k in low for k in keywords):
            continue
        if "revenue" in keywords and "revenue" not in low and "segment" not in low:
            continue
        line = _one_line(s, max_len)
        if line.lower() in seen:
            continue
        seen.add(line.lower())
        out.append(line)
        if len(out) >= max_points:
            break
    return out


def _extract_revenue_points(mda_text: str, seg_text: str, rev_text: str, business_text: str, max_points: int = 4) -> list[str]:
    # 1) MD&A (Results of Operations style narrative)
    mda_kw = (
        "results of operations",
        "revenue",
        "segment",
        "geograph",
        "product",
        "service",
        "growth",
        "decrease",
        "increase",
    )
    mda_points = _extract_key_points(mda_text, mda_kw, max_points=2)
    # 2) Financial statement footnotes (segment/revenue recognition)
    notes_kw = (
        "segment",
        "operating segment",
        "geograph",
        "revenue recognition",
        "subscription",
        "license",
        "services",
        "product",
        "line of business",
    )
    notes_points = _extract_key_points("\n".join([seg_text or "", rev_text or ""]), notes_kw, max_points=2)
    # 3) Item 1 Business (fallback percentages / key concentration statements)
    biz_kw = (
        "revenue",
        "customers",
        "percentage",
        "product",
        "service",
        "segment",
    )
    biz_points = _extract_key_points(business_text, biz_kw, max_points=2)

    out: list[str] = []
    for p in mda_points + notes_points + biz_points:
        if p not in out:
            out.append(p)
        if len(out) >= max_points:
            break
    return out


def _extract_about_business(business_text: str, mda_text: str) -> str:
    # Primary source: Item 1 Business; fallback: high-level MD&A sentence.
    if business_text.strip():
        btxt = re.sub(r"^item\s+1\.?\s+business\s*", "", business_text, flags=re.IGNORECASE).strip()
        about_sents = re.split(r"(?<=[\.\;])\s+", btxt)
        for s in about_sents:
            low = s.lower()
            if len(s.strip()) < 40:
                continue
            if any(v in low for v in ("provides", "offers", "develops", "sells", "serves", "platform", "solutions")):
                return _one_line(s, 380)
        keys = ("company", "provides", "platform", "products", "services", "customers", "industry")
        pts = _extract_key_points(btxt, keys, max_points=1, max_len=380)
        if pts:
            return pts[0]
        return _one_line(btxt, 380)
    if mda_text.strip():
        pts = _extract_key_points(mda_text, ("company", "business", "operations", "customers"), max_points=1, max_len=380)
        if pts:
            return pts[0]
        return _one_line(mda_text, 380)
    return "Business section not extracted for this 10-K."


def _resolve_filing_path(path_s: str) -> Path | None:
    ptxt = (path_s or "").strip()
    if not ptxt:
        return None
    p = Path(ptxt)
    if not p.is_absolute():
        p = (ROOT / p).resolve()
    # Path migration fallback.
    if not p.exists():
        alt = ROOT / "filings" / p.name
        if alt.exists():
            p = alt
    return p if p.exists() and p.is_file() else None


def _read_latest_10k_text(path_s: str, max_chars: int = 900000) -> str:
    p = _resolve_filing_path(path_s)
    if not p:
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="ignore")[:max_chars]
    except Exception:
        return ""


def _extract_item_block(text: str, start_re: str, end_res: tuple[str, ...], max_chars: int = 240000) -> str:
    if not text:
        return ""
    ms = list(re.finditer(start_re, text, flags=re.IGNORECASE))
    if not ms:
        return ""
    # First hit is often Table of Contents; prefer second hit when available.
    m = ms[1] if len(ms) > 1 else ms[0]
    s = m.start()
    end_idx = len(text)
    for er in end_res:
        em = re.search(er, text[s + 1 :], flags=re.IGNORECASE)
        if em:
            end_idx = min(end_idx, s + 1 + em.start())
    blk = text[s:end_idx]
    blk = re.sub(r"\s+", " ", blk).strip()
    return blk[:max_chars]


def _extract_notes_windows(text: str, window: int = 900) -> str:
    if not text:
        return ""
    pats = [
        r"segment information",
        r"segment reporting",
        r"revenue recognition",
        r"operating segment",
        r"geographic information",
    ]
    chunks: list[str] = []
    for pat in pats:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            a = max(0, m.start() - window)
            b = min(len(text), m.end() + window)
            chunks.append(text[a:b])
            if len(chunks) >= 8:
                break
        if len(chunks) >= 8:
            break
    if not chunks:
        return ""
    merged = " ".join(chunks)
    return re.sub(r"\s+", " ", merged)[:90000]


def latest_10k_profiles(tickers: list[str], limit: int = 60) -> list[dict[str, str]]:
    conn = research_db()
    out: list[dict[str, str]] = []
    try:
        for t in tickers[:limit]:
            row = conn.execute(
                """
                SELECT id, form, date, path
                FROM filings
                WHERE ticker = ? AND form IN ('10-K', '20-F')
                ORDER BY date DESC
                LIMIT 1
                """,
                (t,),
            ).fetchone()
            if not row:
                out.append(
                    {
                        "ticker": t,
                        "form": "-",
                        "date": "-",
                        "about": "No 10-K/20-F found in local database.",
                        "revenue": "No revenue breakdown available.",
                    }
                )
                continue
            fid = int(row["id"])
            filing_text = _read_latest_10k_text(str(row["path"] or ""))
            if filing_text.strip():
                business_blk = _extract_item_block(
                    filing_text,
                    r"\bitem\s+1\.?\s+business\b",
                    (
                        r"\bitem\s+1a\.?\s+risk\s+factors\b",
                        r"\bitem\s+1b\.?\b",
                        r"\bitem\s+2\.?\b",
                    ),
                )
                mda_blk = _extract_item_block(
                    filing_text,
                    r"\bitem\s+7\.?\s+(management['’]?\s+s\s+discussion|management|md&a|management’s)\b|\bitem\s+7\.?\b",
                    (
                        r"\bitem\s+7a\.?\b",
                        r"\bitem\s+8\.?\b",
                    ),
                )
                notes_blk = _extract_notes_windows(filing_text)
                about = _extract_about_business(business_blk, mda_blk)
                rev_points = _extract_revenue_points(mda_blk, notes_blk, notes_blk, business_blk, max_points=4)
            else:
                # Fallback to pre-extracted sections if raw filing text is unavailable.
                sec_rows = conn.execute(
                    "SELECT section_name, content FROM sections WHERE filing_id = ?",
                    (fid,),
                ).fetchall()
                sec_map = {str(r["section_name"]): str(r["content"] or "") for r in sec_rows}
                about_src = sec_map.get("business", "")
                mda_src = sec_map.get("mda", "")
                seg_src = sec_map.get("segments", "")
                rev_src = sec_map.get("revenue_recognition", "")
                about = _extract_about_business(about_src, mda_src)
                rev_points = _extract_revenue_points(mda_src, seg_src, rev_src, about_src, max_points=4)
            revenue = " | ".join(rev_points) if rev_points else "Revenue breakdown text not clearly extracted from MD&A, Segment Information, or Item 1 Business."
            out.append(
                {
                    "ticker": t,
                    "form": str(row["form"] or "-"),
                    "date": str(row["date"] or "-"),
                    "about": about,
                    "revenue": revenue,
                }
            )
    finally:
        conn.close()
    return out


def _parse_iso_datetime(s: str) -> dt.datetime | None:
    txt = (s or "").strip()
    if not txt:
        return None
    try:
        return dt.datetime.fromisoformat(txt.replace("Z", "+00:00"))
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(txt[: len(fmt)], fmt)
        except Exception:
            continue
    return None


def _recent_intel_feed_signals_map(tickers: list[str], hours: int = 24) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {str(t).upper(): [] for t in tickers if str(t).strip()}
    if not out:
        return out
    now = dt.datetime.now()
    cutoff = now - dt.timedelta(hours=max(1, int(hours)))
    try:
        rows = list_intel_feed(limit=300)
    except Exception:
        return out
    for r in rows:
        t = str(r["ticker"] or "").strip().upper()
        if t not in out:
            continue
        created = _parse_iso_datetime(str(r["created_at"] or ""))
        if created is None or created < cutoff:
            continue
        cat = str(r["category"] or "").strip().upper()
        if cat:
            out[t].append(cat)
    for t in list(out.keys()):
        seen: set[str] = set()
        dedup: list[str] = []
        for c in out[t]:
            if c in seen:
                continue
            seen.add(c)
            dedup.append(c)
        out[t] = dedup
    return out


def _recent_material_news_signal(ticker: str, hours: int = 24, max_items: int = 14) -> dict[str, object]:
    t = resolve_ticker_input(ticker)
    if not t or yf is None:
        return {}
    ck = f"news_sig:{t}:{hours}:{max_items}"
    cached = _cache_get(ck, ttl_seconds=900)
    if isinstance(cached, dict):
        return dict(cached)
    out: dict[str, object] = {}
    try:
        tk = yf.Ticker(t)
        rows = getattr(tk, "news", None) or []
        cutoff = dt.datetime.now() - dt.timedelta(hours=max(1, int(hours)))
        material_score = 0
        recent_count = 0
        mat_count = 0
        tags: set[str] = set()
        top_headline = ""
        tag_rules: list[tuple[str, str, int]] = [
            ("earnings", "earnings", 2),
            ("guidance", "guidance", 2),
            ("outlook", "guidance", 1),
            ("acquisition", "m&a", 2),
            ("merger", "m&a", 2),
            ("buyout", "m&a", 2),
            ("sec", "regulatory", 2),
            ("investigation", "regulatory", 2),
            ("subpoena", "regulatory", 2),
            ("lawsuit", "litigation", 2),
            ("litigation", "litigation", 2),
            ("bankruptcy", "distress", 3),
            ("restructuring", "distress", 2),
            ("restatement", "accounting", 3),
            ("impairment", "accounting", 2),
            ("material weakness", "accounting", 3),
            ("ceo", "management", 1),
            ("cfo", "management", 1),
            ("resigns", "management", 1),
            ("resignation", "management", 1),
        ]
        for row in rows[:max_items]:
            ts = row.get("providerPublishTime")
            if not ts:
                continue
            try:
                pd = dt.datetime.fromtimestamp(int(ts))
            except Exception:
                continue
            if pd < cutoff:
                continue
            recent_count += 1
            title = str(row.get("title") or "").strip()
            summary = str(row.get("summary") or row.get("content") or "").strip()
            low = (title + " " + summary).lower()
            row_score = 0
            row_tags: set[str] = set()
            for kw, tg, pts in tag_rules:
                if kw in low:
                    row_score += pts
                    row_tags.add(tg)
            if row_score >= 2:
                mat_count += 1
                material_score += row_score
                tags.update(row_tags)
                if not top_headline and title:
                    top_headline = title
        out = {
            "material": bool(mat_count > 0),
            "score": int(material_score),
            "recent_count": int(recent_count),
            "material_count": int(mat_count),
            "headline": top_headline,
            "tags": sorted(tags),
        }
    except Exception:
        out = {}
    _cache_put(ck, out)
    return out


def _last24_suggestion_payload(
    day_pct: float | None,
    forms_unique: list[str],
    buy_v: float,
    sell_v: float,
    recent_earn: dict[str, object],
    intel_cats: list[str],
    news_sig: dict[str, object],
) -> tuple[str, list[str], int]:
    d = float(day_pct) if isinstance(day_pct, float) else None
    score = 0
    reasons: list[str] = []
    forms_set = {str(x).upper().strip() for x in (forms_unique or []) if str(x).strip()}
    cats_set = {str(x).upper().strip() for x in (intel_cats or []) if str(x).strip()}
    news_material = bool(news_sig.get("material")) if isinstance(news_sig, dict) else False
    news_head = str(news_sig.get("headline") or "").strip() if isinstance(news_sig, dict) else ""
    news_tags = [str(x) for x in (news_sig.get("tags") or [])] if isinstance(news_sig, dict) else []

    if isinstance(d, float):
        ad = abs(d)
        if ad >= 7.0:
            score += 4
            reasons.append(f"very large move {d:+.2f}%")
        elif ad >= 5.0:
            score += 3
            reasons.append(f"large move {d:+.2f}%")
        elif ad >= 3.0:
            score += 1
            reasons.append(f"notable move {d:+.2f}%")

    if "8-K" in forms_set:
        score += 2
        reasons.append("new 8-K")
    if ("10-K" in forms_set) or ("10-Q" in forms_set):
        score += 2
        reasons.append("new periodic filing")

    if buy_v > sell_v and (buy_v - sell_v) >= 250000:
        score += 1
        reasons.append(f"insider net buy {fmt_money(buy_v - sell_v)}")
    if sell_v > buy_v and (sell_v - buy_v) >= 250000:
        score += 2
        reasons.append(f"insider net sell {fmt_money(sell_v - buy_v)}")

    if recent_earn:
        days_ago = _to_int(recent_earn.get("days_ago"), 99)
        verdict = str(recent_earn.get("verdict") or "REPORTED").upper()
        surprise = recent_earn.get("surprise_pct")
        if days_ago <= 3:
            score += 3
        elif days_ago <= 10:
            score += 1
        if isinstance(surprise, (int, float)):
            reasons.append(f"earnings {verdict} {days_ago}d ago ({float(surprise):+.1f}%)")
        else:
            reasons.append(f"earnings {verdict} {days_ago}d ago")

    if cats_set:
        score += 2
        reasons.append("fresh watcher intel: " + ", ".join(sorted(cats_set)))
    if news_material:
        score += 2
        nt = ", ".join(news_tags[:3]) if news_tags else "material headline"
        reasons.append(f"material news: {nt}")

    suggestion = "Monitor only."
    if recent_earn and isinstance(d, float) and abs(d) >= 5.0:
        suggestion = "Post-earnings volatility: read earnings release + guidance + transcript now."
    elif news_material and isinstance(d, float) and abs(d) >= 3.0:
        suggestion = "Material news + move: read primary source headline and re-check thesis."
    elif news_material:
        suggestion = "Material news detected: open latest company headlines and validate impact."
    elif "8-K" in forms_set and isinstance(d, float) and abs(d) >= 3.0:
        suggestion = "Read latest 8-K now and verify whether move is fundamental."
    elif ("10-K" in forms_set) or ("10-Q" in forms_set):
        suggestion = "Update key metrics from the new filing and refresh checklist."
    elif isinstance(d, float) and abs(d) >= 5.0:
        suggestion = "Large move >5%: run Deep Dive and check catalyst before next action."
    elif recent_earn:
        suggestion = "Recent earnings print: review guidance, segment trends, and update thesis."
    elif sell_v > buy_v and isinstance(d, float) and d < -3.0:
        suggestion = "Downside pressure is elevated; review thesis and risk limits."
    elif buy_v > sell_v and isinstance(d, float) and d > 0.0:
        suggestion = "Constructive signal; wait for follow-through before adding."

    if score >= 6 and isinstance(d, float) and d < 0:
        suggestion = "High-signal downside: run Deep Dive + SEC Risk Diff, then reassess sizing."
    elif score >= 6 and isinstance(d, float) and d > 0 and suggestion == "Monitor only.":
        suggestion = "High-signal upside: verify durability (earnings/filings) before adding."
    elif score >= 5 and news_material and suggestion == "Monitor only.":
        suggestion = "High-signal news cluster: review headline evidence before making changes."
    if news_head and news_material and len(reasons) < 4:
        reasons.append("headline: " + _compact_signal_text(news_head, max_chars=90))
    return suggestion, reasons[:4], max(0, min(100, int(score * 10)))


def _build_last24_rows_for_tickers(tickers: list[str], quotes: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    shown = [str(t or "").strip().upper() for t in tickers if str(t or "").strip()]
    if not shown:
        return []
    cutoff = (dt.date.today() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    today_d = dt.date.today()
    sec_map: dict[str, list[str]] = {t: [] for t in shown}
    w_events = _watchlist_earnings_event_map(limit=500)
    feed_sig_map = _recent_intel_feed_signals_map(shown, hours=24)
    try:
        conn = research_db()
        try:
            ph = ",".join("?" for _ in shown)
            q_sql = (
                f"SELECT ticker, form, date FROM filings "
                f"WHERE ticker IN ({ph}) AND date >= ? ORDER BY date DESC LIMIT 400"
            )
            rows24 = conn.execute(q_sql, tuple(shown) + (cutoff,)).fetchall()
            for rr in rows24:
                tt = str(rr["ticker"] or "").strip().upper()
                fm = str(rr["form"] or "").strip().upper()
                if tt and fm and tt in sec_map:
                    sec_map[tt].append(fm)
        finally:
            conn.close()
    except Exception:
        pass

    out: list[dict[str, object]] = []
    for t in shown:
        q = quotes.get(t, {}) if isinstance(quotes, dict) else {}
        day = q.get("day_pct")
        day_pct = float(day) if isinstance(day, float) else None
        forms = sec_map.get(t, [])
        forms_unique: list[str] = []
        seenf: set[str] = set()
        for f in forms:
            if f in seenf:
                continue
            seenf.add(f)
            forms_unique.append(f)
        forms_txt = ", ".join(forms_unique[:3]) if forms_unique else "-"

        buy_v = 0.0
        sell_v = 0.0
        buy_n = 0
        sell_n = 0
        if "4" in forms_unique:
            ins = _sec_insider_details(t, months=1, meaningful_usd=100000.0)
            events = [e for e in (ins.get("events") or []) if isinstance(e, dict) and str(e.get("date") or "") >= cutoff]
            buy_v = sum(float(_to_num(e.get("value")) or 0.0) for e in events if str(e.get("code")) == "P")
            sell_v = sum(float(_to_num(e.get("value")) or 0.0) for e in events if str(e.get("code")) == "S")
            buy_n = sum(1 for e in events if str(e.get("code")) == "P")
            sell_n = sum(1 for e in events if str(e.get("code")) == "S")
        if buy_n == 0 and sell_n == 0:
            insider_txt = "No notable insider trades"
        elif buy_v >= sell_v:
            insider_txt = f"Net buy {fmt_money(buy_v - sell_v)} ({buy_n} buy / {sell_n} sell)"
        else:
            insider_txt = f"Net sell {fmt_money(sell_v - buy_v)} ({buy_n} buy / {sell_n} sell)"

        recent_earn: dict[str, object] = {}
        ev = w_events.get(t, {}) if isinstance(w_events, dict) else {}
        if isinstance(ev, dict):
            rd = _parse_feed_date_text(str(ev.get("last_report_date") or ""))
            if rd is not None:
                days_ago = (today_d - rd).days
                if 0 <= days_ago <= 10:
                    recent_earn = {
                        "date": rd.isoformat(),
                        "days_ago": days_ago,
                        "surprise_pct": ev.get("last_surprise_pct"),
                        "verdict": str(ev.get("last_verdict") or "REPORTED").upper(),
                    }
        if (not recent_earn) and isinstance(day_pct, float) and abs(day_pct) >= 4.0:
            recent_earn = _recent_reported_earnings_snapshot(t, max_days=10)
        feed_cats = list(feed_sig_map.get(t) or [])
        news_sig = _recent_material_news_signal(t, hours=24, max_items=14)

        happened_bits: list[str] = []
        if isinstance(day_pct, float):
            happened_bits.append(f"Day move {day_pct:+.2f}%")
        if recent_earn:
            ev_days = _to_int(recent_earn.get("days_ago"), 0)
            ev_verdict = str(recent_earn.get("verdict") or "REPORTED").upper()
            ev_sur = recent_earn.get("surprise_pct")
            if isinstance(ev_sur, (int, float)):
                happened_bits.append(f"Earnings {ev_verdict} {ev_days}d ago ({float(ev_sur):+.1f}%)")
            else:
                happened_bits.append(f"Earnings {ev_verdict} {ev_days}d ago")
        if forms_unique:
            happened_bits.append(f"New filing {forms_unique[0]}")
        if buy_n or sell_n:
            happened_bits.append("Insider activity changed")
        if feed_cats:
            happened_bits.append("Watcher intel: " + ", ".join(feed_cats[:2]))
        if bool(news_sig.get("material")):
            nhead = _compact_signal_text(str(news_sig.get("headline") or ""), max_chars=92)
            if nhead:
                happened_bits.append("Material news: " + nhead)
            else:
                ntags = ", ".join(str(x) for x in (news_sig.get("tags") or [])[:2])
                happened_bits.append("Material news: " + (ntags or "headline signal"))
        happened = "; ".join(happened_bits) if happened_bits else "No material change detected."

        suggestion, why_bits, event_score = _last24_suggestion_payload(
            day_pct=day_pct,
            forms_unique=forms_unique,
            buy_v=buy_v,
            sell_v=sell_v,
            recent_earn=recent_earn,
            intel_cats=feed_cats,
            news_sig=news_sig,
        )
        if why_bits:
            happened = happened + " | Logic: " + "; ".join(why_bits)

        out.append(
            {
                "ticker": t,
                "day_pct": day_pct,
                "insider_txt": insider_txt,
                "sec_txt": forms_txt,
                "happened": happened,
                "suggestion": suggestion,
                "event_score": event_score,
            }
        )
    return out


def universe_html(tab: str = "portfolio", message: str = "", deep_mode: bool = False) -> str:
    fast_mode = False if deep_mode else True
    tab = (tab or "portfolio").strip().lower()
    if tab not in {"portfolio", "watchlist", "all"}:
        tab = "portfolio"
    watch_entries = read_watchlist_entries(DATA / "my_watchlist.txt")
    portfolio_rows = read_portfolio_rows(DATA / "portfolio.csv")
    watch = {e["ticker"]: e for e in watch_entries}
    port = {r[0]: r for r in portfolio_rows if r and r[0]}
    tickers = sorted(set(watch.keys()) | set(port.keys()))
    quotes = get_live_quotes(tickers)
    shown_portfolio_tickers = [t for t in tickers if (tab != "watchlist" and port.get(t))]
    shown_watchlist_tickers = [t for t in tickers if (tab != "portfolio" and watch.get(t))]
    all_intel = {} if fast_mode else get_portfolio_intel(tickers)
    intel = {t: dict(all_intel.get(t, {})) for t in shown_portfolio_tickers}
    watch_intel = {t: dict(all_intel.get(t, {})) for t in shown_watchlist_tickers}
    try:
        emap_now = _earnings_days_map_from_feed()
        for t in shown_portfolio_tickers:
            row = intel.get(t, {})
            if row.get("earn_days") is None and t in emap_now:
                row["earn_days"] = int(emap_now[t])
                intel[t] = row
        for t in shown_watchlist_tickers:
            row = watch_intel.get(t, {})
            if row.get("earn_days") is None and t in emap_now:
                row["earn_days"] = int(emap_now[t])
                watch_intel[t] = row
    except Exception:
        pass
    profiles = {} if fast_mode else get_portfolio_profiles(tickers)
    base_ccy = _preferred_base_currency()
    cash_balance_rows = read_cash_balances(CASH_BALANCES_FILE)

    position_values: dict[str, float] = {}
    cash_value = 0.0
    cash_lines: list[str] = []
    for t in shown_portfolio_tickers:
        p = port.get(t)
        if not p:
            continue
        shares = to_float(p[1]) or 0.0
        cost = to_float(p[2])
        notes = (p[3] or "").strip().lower()
        is_cash = t in {"CASH", "USD", "USDCASH", "MONEYMARKET", "MMF"} or "cash" in notes
        if shares <= 0:
            continue
        if is_cash:
            px_cash = cost if cost not in (None, 0.0) else 1.0
            usd_cash = shares * float(px_cash)
            conv_cash = fx_convert(usd_cash, "USD", base_ccy)
            if conv_cash is not None:
                cash_value += float(conv_cash)
                cash_lines.append(f"USD {usd_cash:,.2f} ({base_ccy} {float(conv_cash):,.2f})")
            else:
                cash_value += usd_cash
                cash_lines.append(f"USD {usd_cash:,.2f}")
            continue
        q = quotes.get(t, {})
        now = q.get("price")
        px = float(now) if isinstance(now, float) else (float(cost) if cost not in (None, 0.0) else None)
        if px is None:
            continue
        px_base = fx_convert(px, "USD", base_ccy)
        px_used = float(px_base) if isinstance(px_base, float) else float(px)
        position_values[t] = shares * px_used
    for row in cash_balance_rows:
        ccy = str(row.get("currency") or "").upper().strip()
        amt = to_float(str(row.get("amount") or ""))
        note = str(row.get("note") or "").strip()
        if amt is None:
            continue
        conv = fx_convert(float(amt), ccy, base_ccy)
        if conv is None:
            continue
        cash_value += float(conv)
        txt = f"{ccy} {float(amt):,.2f} ({base_ccy} {float(conv):,.2f})"
        if note:
            txt += f" - {note}"
        cash_lines.append(txt)
    equity_value = sum(v for v in position_values.values() if v > 0)
    total_aum = equity_value + (cash_value if cash_value > 0 else 0.0)
    weight_map: dict[str, float] = {
        t: (v / total_aum * 100.0) for t, v in position_values.items() if total_aum > 0
    }

    top3 = sorted(weight_map.items(), key=lambda kv: kv[1], reverse=True)[:3]
    top3_txt = ", ".join(f"{t} {w:.1f}%" for t, w in top3) if top3 else "-"
    cash_drag = (cash_value / total_aum * 100.0) if total_aum > 0 and cash_value > 0 else 0.0
    stock_weight = (equity_value / total_aum * 100.0) if total_aum > 0 else 0.0
    vital_html = ""
    if tab == "portfolio":
        top_chips = "".join(
            f"<span class='pm-chip'><b>{html.escape(t)}</b> {w:.1f}%</span>" for t, w in top3
        ) or "<span class='muted'>No holdings yet.</span>"
        top_bars = "".join(
            f"<div class='pm-hold'>"
            f"<div class='pm-hold-head'><span>{html.escape(t)}</span><span>{w:.1f}%</span></div>"
            f"<div class='pm-track'><span style='width:{min(100.0, max(0.0, w)):.1f}%;'></span></div>"
            f"</div>"
            for t, w in top3
        ) or "<div class='muted'>-</div>"
        vital_html = (
            "<div class='card pm-bar'>"
            "<div class='pm-head'><h2 class='section-title'>Portfolio Vital Signs</h2>"
            f"<div class='muted pm-asof'>Concentration: {html.escape(top3_txt)}</div></div>"
            "<div class='pm-grid'>"
            f"<div class='pm-kpi'><span class='muted'>Total AUM ({html.escape(base_ccy)})</span><div class='pmv'>{fmt_money_ccy(total_aum if total_aum > 0 else None, base_ccy)}</div></div>"
            f"<div class='pm-kpi'><span class='muted'>Stock Value</span><div class='pmv'>{fmt_money_ccy(equity_value if equity_value > 0 else 0.0, base_ccy)}</div><div class='muted'>{stock_weight:.1f}% of AUM</div></div>"
            f"<div class='pm-kpi'><span class='muted'>Cash Value</span><div class='pmv'>{fmt_money_ccy(cash_value if cash_value > 0 else 0.0, base_ccy)}</div><div class='muted'>{cash_drag:.1f}% of AUM</div></div>"
            f"<div class='pm-kpi'><span class='muted'>Cash Drag</span><div class='pmv'>{cash_drag:.2f}%</div></div>"
            f"<div class='pm-kpi pm-top'><span class='muted'>Top 3 Holdings</span><div class='pm-chip-row'>{top_chips}</div><div class='pm-bars'>{top_bars}</div></div>"
            "</div>"
            + (
                f"<div class='muted' style='margin-top:8px;'>Cash balances: {' | '.join(html.escape(x) for x in cash_lines[:6])}</div>"
                if cash_lines
                else ""
            )
            +
            "</div>"
        )

    # Portfolio Intelligence: sector/industry exposure + quick diagnostics.
    intelligence_html = ""
    if tab == "portfolio" and shown_portfolio_tickers and not fast_mode:
        sector_vals: dict[str, float] = {}
        industry_vals: dict[str, float] = {}
        geo_vals: dict[str, float] = {}
        for t, v in position_values.items():
            pr = profiles.get(t, {})
            sec = str(pr.get("sector") or "Unknown").strip() or "Unknown"
            ind = str(pr.get("industry") or "Unknown").strip() or "Unknown"
            ctry = str(pr.get("country") or "").strip()
            geo = ctry if ctry else "Unknown"
            sector_vals[sec] = sector_vals.get(sec, 0.0) + v
            industry_vals[ind] = industry_vals.get(ind, 0.0) + v
            geo_vals[geo] = geo_vals.get(geo, 0.0) + v
        sector_rows = sorted(sector_vals.items(), key=lambda kv: kv[1], reverse=True)
        industry_rows = sorted(industry_vals.items(), key=lambda kv: kv[1], reverse=True)
        geo_rows = sorted(geo_vals.items(), key=lambda kv: kv[1], reverse=True)
        sec_html = "".join(
            f"<div class='exp-row'><span>{html.escape(k)}</span><b>{(v/total_aum*100.0):.1f}%</b></div>"
            for k, v in sector_rows[:6]
        ) or "<div class='muted'>No sector mapping yet.</div>"
        ind_html = "".join(
            f"<div class='exp-row'><span>{html.escape(k)}</span><b>{(v/total_aum*100.0):.1f}%</b></div>"
            for k, v in industry_rows[:6]
        ) or "<div class='muted'>No industry mapping yet.</div>"
        geo_html = "".join(
            f"<div class='exp-row'><span>{html.escape(k)}</span><b>{(v/total_aum*100.0):.1f}%</b></div>"
            for k, v in geo_rows[:6]
        ) or "<div class='muted'>No country mapping yet.</div>"

        quick_points: list[str] = []
        if top3:
            t1, w1 = top3[0]
            if w1 >= 35.0:
                quick_points.append(f"Concentration: {t1} is {w1:.1f}% of AUM (high single-name exposure).")
        if len(sector_rows) <= 2 and sector_rows:
            quick_points.append(f"Diversification: only {len(sector_rows)} sector(s) represented.")
        if sector_rows:
            s0, sv0 = sector_rows[0]
            sw = sv0 / total_aum * 100.0 if total_aum > 0 else 0.0
            quick_points.append(f"Sector tilt: {s0} is {sw:.1f}% of portfolio.")
        day_moves: list[tuple[str, float]] = []
        for t in shown_portfolio_tickers:
            d = quotes.get(t, {}).get("day_pct")
            if isinstance(d, float):
                day_moves.append((t, d))
        if day_moves:
            best_t, best_d = sorted(day_moves, key=lambda kv: kv[1], reverse=True)[0]
            worst_t, worst_d = sorted(day_moves, key=lambda kv: kv[1])[0]
            quick_points.append(f"Daily dispersion: best {best_t} {best_d:+.2f}%, worst {worst_t} {worst_d:+.2f}%.")
        weighted_upside_num = 0.0
        weighted_upside_den = 0.0
        for t in shown_portfolio_tickers:
            row = port.get(t)
            if not row:
                continue
            sh = to_float(row[1]) or 0.0
            nowp = quotes.get(t, {}).get("price")
            tgt = intel.get(t, {}).get("target_mean")
            if sh <= 0 or not isinstance(nowp, float) or not isinstance(tgt, float) or nowp == 0:
                continue
            nowp_base = fx_convert(nowp, "USD", base_ccy)
            nowp_used = float(nowp_base) if isinstance(nowp_base, float) else float(nowp)
            posv = sh * nowp_used
            up = (tgt - nowp) / nowp * 100.0
            weighted_upside_num += up * posv
            weighted_upside_den += posv
        if weighted_upside_den > 0:
            quick_points.append(f"Street skew: weighted analyst upside is {weighted_upside_num/weighted_upside_den:+.1f}%.")
        quick_html = "".join(f"<li>{html.escape(p)}</li>" for p in quick_points[:5]) or "<li class='muted'>Not enough live data for quick diagnostics yet.</li>"
        geo_ai_lines = [
            f"Stock value ({base_ccy}): {equity_value:,.2f}",
            f"Cash value ({base_ccy}): {cash_value:,.2f}",
            "Country exposure: " + ", ".join(
                f"{k} {(v/total_aum*100.0):.1f}%" for k, v in geo_rows[:5]
            ),
        ]
        geo_ai = _ai_cached_reliable_summary(
            cache_key=f"geo_intel:{base_ccy}:{'|'.join(shown_portfolio_tickers)}:{int(total_aum)}",
            source_text="\n".join(geo_ai_lines),
            system=(
                "You are a portfolio construction analyst. Use ONLY provided numbers. "
                "Return exactly 2 bullets: (1) stock-vs-cash stance, (2) geographic concentration risk/cue."
            ),
            ttl_seconds=900,
        )
        geo_ai_html = (
            "<div class='focus-box' style='margin-bottom:8px;'>"
            "<div class='focus-title'>AI Allocation Take</div>"
            f"<pre class='chart' style='white-space:pre-wrap;margin:0;'>{html.escape(geo_ai)}</pre>"
            "</div>"
            if geo_ai
            else ""
        )

        w_events = _watchlist_earnings_event_map(limit=500)
        earn_now: list[tuple[str, int]] = []
        earn_next: list[tuple[str, int]] = []
        earn_missing: list[str] = []
        for t in shown_portfolio_tickers:
            d = intel.get(t, {}).get("earn_days")
            if isinstance(d, int):
                if d < 7:
                    earn_now.append((t, d))
                elif d <= 21:
                    earn_next.append((t, d))
            else:
                earn_missing.append(t)
        earn_now.sort(key=lambda kv: kv[1])
        earn_next.sort(key=lambda kv: kv[1])
        signal_lines: list[str] = []
        if earn_now:
            signal_lines.append("Binary earnings within 7 days: " + ", ".join(f"{t} ({d}d)" for t, d in earn_now[:6]))
        if earn_next:
            signal_lines.append("Next 8-21 days: " + ", ".join(f"{t} ({d}d)" for t, d in earn_next[:6]))
        if not earn_now and not earn_next:
            signal_lines.append("No near-term earnings catalysts in the next 21 days.")

        last_rows: list[tuple[str, str, float | None]] = []
        for t in shown_portfolio_tickers:
            ev = w_events.get(t, {})
            verdict = str(ev.get("last_verdict") or "").upper().strip()
            if verdict in {"BEAT", "MISS", "REPORTED"}:
                sp = ev.get("last_surprise_pct")
                spf = float(sp) if isinstance(sp, (int, float)) else None
                last_rows.append((t, verdict, spf))
        beats = sum(1 for _, v, _ in last_rows if v == "BEAT")
        misses = sum(1 for _, v, _ in last_rows if v == "MISS")
        if last_rows:
            signal_lines.append(f"Last quarter scorecard: {beats} beat, {misses} miss across tracked holdings.")
            ranked = sorted(last_rows, key=lambda x: abs(x[2]) if isinstance(x[2], float) else -1.0, reverse=True)
            top = ranked[0]
            if isinstance(top[2], float):
                signal_lines.append(f"Largest earnings surprise: {top[0]} {top[1]} ({top[2]:+.1f}%).")
            else:
                signal_lines.append(f"Largest earnings surprise: {top[0]} {top[1]} (estimate gap unavailable).")
        else:
            signal_lines.append("Last-quarter earnings scorecard not available for current holdings.")

        if earn_missing:
            signal_lines.append("Missing next earnings date for: " + ", ".join(earn_missing[:6]) + ".")
        signal_html = "".join(f"<li>{html.escape(x)}</li>" for x in signal_lines)

        # Dividend outlook: expected cash from owned shares + key dates + confidence.
        div_lines: list[str] = []
        annual_div_cash = 0.0
        div_count = 0
        next_ex: list[tuple[str, int, str]] = []
        for t in shown_portfolio_tickers:
            p = port.get(t)
            if not p:
                continue
            sh = to_float(p[1]) or 0.0
            row = intel.get(t, {})
            rate = row.get("dividend_rate")
            yld = row.get("dividend_yield_pct")
            exd = str(row.get("ex_div_date") or "").strip()
            if isinstance(rate, float) and sh > 0:
                annual_div_cash += sh * rate
                div_count += 1
            if exd:
                try:
                    dd = (dt.datetime.strptime(exd[:10], "%Y-%m-%d").date() - dt.date.today()).days
                    if dd >= 0:
                        next_ex.append((t, dd, exd[:10]))
                except Exception:
                    pass
            if isinstance(rate, float) or isinstance(yld, float):
                ytxt = f"{yld:.2f}%" if isinstance(yld, float) else "-"
                rtxt = f"${rate:.2f}/yr" if isinstance(rate, float) else "-"
                div_lines.append(f"{t}: {rtxt}, yield {ytxt}, ex-div {exd or '-'}.")
        if div_count > 0:
            div_lines.insert(0, f"Expected annual dividend cash (current shares): {fmt_money(annual_div_cash)}.")
        else:
            div_lines.append("No reliable dividend-per-share values detected for current holdings.")
        if next_ex:
            next_ex.sort(key=lambda x: x[1])
            div_lines.append("Next ex-dividend windows: " + ", ".join(f"{t} ({d}d)" for t, d, _ in next_ex[:5]) + ".")
        div_html = "".join(f"<li>{html.escape(x)}</li>" for x in div_lines[:8])

        # Similar names near 52-week lows (watchlist + holdings sectors).
        low_lines: list[str] = []
        focus_secs = {s for s, _ in sector_rows[:2]}
        candidates: list[tuple[str, float, str]] = []
        for t in sorted(set(shown_watchlist_tickers) | set(watch.keys())):
            pr = profiles.get(t, {})
            sec = str(pr.get("sector") or "Unknown").strip() or "Unknown"
            if focus_secs and sec not in focus_secs:
                continue
            nlp = all_intel.get(t, {}).get("near_low_pct")
            if isinstance(nlp, float) and 0.0 <= nlp <= 15.0:
                candidates.append((t, nlp, sec))
        if candidates:
            candidates.sort(key=lambda x: x[1])
            low_lines.append("Similar names close to 52W low (same sector as your holdings):")
            for t, pct, sec in candidates[:6]:
                low_lines.append(f"{t}: {pct:.1f}% above 52W low ({sec}).")
        else:
            low_lines.append("No same-sector watchlist names currently within 15% of 52W lows.")
        low_html = "".join(f"<li>{html.escape(x)}</li>" for x in low_lines[:8])

        intelligence_html = (
            "<div class='card intel-card'>"
            "<div class='pm-head'><h2 class='section-title'>Portfolio Intelligence</h2>"
            "<div class='muted pm-asof'>Exposure + diagnostics + earnings + dividend + 52W low radar</div></div>"
            f"{geo_ai_html}"
            "<div class='intel-grid'>"
            f"<div class='intel-box'><h3>Sector Exposure (%)</h3>{sec_html}</div>"
            f"<div class='intel-box'><h3>Industry Exposure (%)</h3>{ind_html}</div>"
            f"<div class='intel-box'><h3>Geographic Exposure (%)</h3>{geo_html}</div>"
            f"<div class='intel-box'><h3>Quick Analysis</h3><ul>{quick_html}</ul></div>"
            f"<div class='intel-box'><h3>Portfolio Signal Brief</h3><ul>{signal_html}</ul></div>"
            f"<div class='intel-box'><h3>Dividend Outlook</h3><ul>{div_html}</ul></div>"
            f"<div class='intel-box'><h3>52W Low Radar</h3><ul>{low_html}</ul></div>"
            "</div>"
            "<div class='muted' style='margin-top:8px;'>Dividend dates/cash are from Yahoo fields with SEC filing mention cross-check labels where available.</div>"
            "</div>"
        )
    if tab == "watchlist" and shown_watchlist_tickers and not fast_mode:
        w_events = _watchlist_earnings_event_map(limit=500)
        upcoming: list[str] = []
        lastq: list[str] = []
        expected: list[str] = []
        risk: list[str] = []
        missing_watch_earn: list[str] = []
        for t in shown_watchlist_tickers:
            ev = w_events.get(t, {})
            d = watch_intel.get(t, {}).get("earn_days")
            ds = str(watch_intel.get(t, {}).get("earn_source") or "-")
            if isinstance(d, int):
                if d < 7:
                    upcoming.append(f"{t}: earnings in {d} day(s) ⚠️ [{ds}]")
                elif d <= 21:
                    upcoming.append(f"{t}: earnings in {d} day(s) [{ds}]")
            else:
                missing_watch_earn.append(t)
            if isinstance(ev.get("last_surprise_pct"), float):
                s = float(ev.get("last_surprise_pct"))
                verdict = str(ev.get("last_verdict") or "REPORTED")
                lastq.append(f"{t}: last EPS {verdict} ({s:+.1f}%).")
                if s < 0:
                    risk.append(f"{t}: last quarter miss ({s:+.1f}%), watch estimate risk.")
            elif ev.get("last_verdict"):
                lastq.append(f"{t}: last EPS {ev.get('last_verdict')}.")
            est = ev.get("est_eps")
            if isinstance(est, float):
                expected.append(f"{t}: expected EPS {est:.2f}.")
            q = quotes.get(t, {})
            day = q.get("day_pct")
            if isinstance(day, float) and day <= -4.0:
                risk.append(f"{t}: large down day {day:+.2f}% (news/earnings check).")
        if not upcoming:
            upcoming = ["No near-term earnings dates detected from current feed."]
        if missing_watch_earn:
            upcoming.append(f"Missing earnings date feed: {', '.join(missing_watch_earn[:8])}")
            upcoming.append("Fallback tip: add manual dates in data/earnings_overrides.csv as TICKER,YYYY-MM-DD.")
        if not lastq:
            lastq = ["No recent reported EPS snapshot parsed yet."]
        if not expected:
            expected = ["No expected EPS values parsed from current feed."]
        if not risk:
            risk = ["No immediate watchlist risk signals from earnings/day-move screen."]

        ai_lines = []
        ai_lines.extend(f"UPCOMING: {x}" for x in upcoming[:8])
        ai_lines.extend(f"LAST: {x}" for x in lastq[:8])
        ai_lines.extend(f"EXPECTED: {x}" for x in expected[:8])
        ai_lines.extend(f"RISK: {x}" for x in risk[:8])
        ai_summary = _ai_cached_reliable_summary(
            cache_key=f"watch_intel:{'|'.join(shown_watchlist_tickers)}:{len(ai_lines)}",
            source_text="\n".join(ai_lines),
            system=(
                "You are a buy-side watchlist analyst. Use ONLY provided lines. "
                "Return exactly 3 bullets: (1) near-term catalyst map, (2) estimate-risk signal, (3) action cue. "
                "No external facts."
            ),
            ttl_seconds=900,
        )
        ai_html = (
            "<div class='focus-box' style='margin-bottom:8px;'>"
            "<div class='focus-title'>AI Watchlist Intelligence</div>"
            f"<pre class='chart' style='white-space:pre-wrap;margin:0;'>{html.escape(ai_summary)}</pre>"
            "<div class='muted' style='margin-top:6px;'>Source scope: earnings feed rows + quote day moves + analyst target/earnings calendar fields.</div>"
            "</div>"
        ) if ai_summary else ""

        intelligence_html = (
            "<div class='card intel-card'>"
            "<div class='pm-head'><h2 class='section-title'>Watchlist Intelligence</h2>"
            "<div class='muted pm-asof'>Catalysts, expected EPS, and quick risk cues</div></div>"
            f"{ai_html}"
            "<div class='intel-grid'>"
            f"<div class='intel-box'><h3>Upcoming Earnings</h3><ul>{''.join(f'<li>{html.escape(x)}</li>' for x in upcoming[:8])}</ul></div>"
            f"<div class='intel-box'><h3>Last Earnings Snapshot</h3><ul>{''.join(f'<li>{html.escape(x)}</li>' for x in lastq[:8])}</ul></div>"
            f"<div class='intel-box'><h3>Expected EPS</h3><ul>{''.join(f'<li>{html.escape(x)}</li>' for x in expected[:8])}</ul></div>"
            f"<div class='intel-box'><h3>Risk / Priority Cues</h3><ul>{''.join(f'<li>{html.escape(x)}</li>' for x in risk[:8])}</ul></div>"
            "</div>"
            "</div>"
        )

    def _pct_chip(v: float | None) -> str:
        if not isinstance(v, float):
            return "<span class='pct-chip flat'>-</span>"
        cls = "up" if v >= 0 else "down"
        return f"<span class='pct-chip {cls}'>{v:+.2f}%</span>"

    def _pct_text(v: float | None) -> str:
        if not isinstance(v, float):
            return "-"
        cls = "up" if v >= 0 else "down"
        return f"<span class='pct-text {cls}'>{v:+.2f}%</span>"

    rows = []
    intel24_html = ""
    display_tickers = [t for t in tickers if not ((tab == "watchlist" and not watch.get(t)) or (tab == "portfolio" and not port.get(t)))]
    onyx_signals = _onyx_get_signals_for_tickers(display_tickers) if _onyx_get_signals_for_tickers is not None else {}
    shown_tickers: list[str] = []
    for t in tickers:
        w = watch.get(t)
        p = port.get(t)
        if tab == "watchlist" and not w:
            continue
        if tab == "portfolio" and not p:
            continue
        shown_tickers.append(t)
        q = quotes.get(t, {})
        now = q.get("price")
        day = q.get("day_pct")
        tag = []
        if p:
            tag.append("portfolio")
        if w:
            tag.append("watchlist")
        shares = to_float(p[1]) if p else None
        port_cost = to_float(p[2]) if p else None
        watch_add = to_float(w.get("added_price", "")) if w else None
        basis_px = port_cost if port_cost not in (None, 0.0) else watch_add
        basis_src = "portfolio cost" if port_cost not in (None, 0.0) else ("watchlist add" if watch_add not in (None, 0.0) else "-")
        since_basis = ((float(now) - basis_px) / basis_px * 100.0) if (now is not None and basis_px not in (None, 0.0)) else None
        pos_pnl = (float(now) - port_cost) * shares if (now is not None and shares not in (None, 0.0) and port_cost not in (None, 0.0)) else None
        now_disp = fx_convert(float(now), "USD", base_ccy) if isinstance(now, float) else None
        cost_disp = fx_convert(float(port_cost), "USD", base_ccy) if isinstance(port_cost, float) else None
        pnl_disp = fx_convert(float(pos_pnl), "USD", base_ccy) if isinstance(pos_pnl, float) else None
        wt = weight_map.get(t)
        if wt is None or not p:
            weight_html = "-"
        elif wt > 20.0:
            weight_html = f"<span style='font-weight:800;color:#ff6d6d;'>{wt:.1f}%</span>"
        else:
            weight_html = f"{wt:.1f}%"
        intel_row = intel.get(t, {}) if p else (watch_intel.get(t, {}) if w else {})
        earn_days = intel_row.get("earn_days")
        if isinstance(earn_days, int):
            if earn_days < 7:
                earn_html = f"<span class='warn-badge'>⚠️ {earn_days} Days</span>"
            else:
                earn_html = f"In {earn_days} Days"
        else:
            earn_html = "-"
        tgt = intel_row.get("target_mean")
        upside = ((float(tgt) - float(now)) / float(now) * 100.0) if (
            isinstance(tgt, float) and isinstance(now, float) and float(now) != 0.0
        ) else None
        upside_html = _pct_text(upside)
        sig_html = _onyx_signal_badge_html(onyx_signals.get(t))
        rows.append(
            f"<tr><td class='tk-cell'><a href='/company_file?t={html.escape(t)}'>{html.escape(t)}</a></td>"
            f"<td style='width:86px;white-space:nowrap;'>{sig_html}</td>"
            f"<td>{html.escape((str(int(shares)) if shares and float(shares).is_integer() else (str(shares) if shares is not None else '-')))}</td>"
            f"<td>{fmt_money_ccy(cost_disp if isinstance(cost_disp, float) else port_cost, base_ccy)}</td>"
            f"<td>{fmt_money_ccy(now_disp if isinstance(now_disp, float) else (now if isinstance(now, float) else None), base_ccy)}</td><td>{_pct_chip(day if isinstance(day, float) else None)}</td><td>{_pct_text(since_basis)}</td><td>{fmt_money_ccy(pnl_disp if isinstance(pnl_disp, float) else pos_pnl, base_ccy)}</td>"
            f"<td>{weight_html}</td><td>{earn_html}</td><td>{upside_html}</td>"
            f"<td><div class='actions'><a class='btn' href='/company_file?t={html.escape(t)}'>Company File</a></div></td></tr>"
        )

    # Last 24h Intelligence for displayed companies (portfolio/watchlist/all).
    if shown_tickers and not fast_mode:
        snap_map = load_intel24_snapshot_map(max_age_seconds=max(300, int(float(os.getenv("ONYX_INTEL24_MAX_AGE", "2400").strip() or "2400"))))
        missing = [t for t in shown_tickers if t not in snap_map]
        if missing:
            fresh_rows = _build_last24_rows_for_tickers(missing, quotes)
            if fresh_rows:
                save_intel24_snapshot(fresh_rows, source="inline_fallback")
                for r in fresh_rows:
                    tt = str(r.get("ticker") or "").strip().upper()
                    if not tt:
                        continue
                    snap_map[tt] = dict(r)

        intel24_rows: list[str] = []
        for t in shown_tickers:
            row = snap_map.get(t, {})
            day_pct = row.get("day_pct")
            day_html = _pct_chip(float(day_pct) if isinstance(day_pct, (int, float)) else None)
            insider_txt = str(row.get("insider_txt") or "No notable insider trades")
            sec_txt = str(row.get("sec_txt") or "-")
            happened = str(row.get("happened") or "No material change detected.")
            suggestion = str(row.get("suggestion") or "Monitor only.")
            intel24_rows.append(
                f"<tr><td class='tk-cell'><a href='/company_file?t={html.escape(t)}'>{html.escape(t)}</a></td>"
                f"<td>{day_html}</td>"
                f"<td>{html.escape(insider_txt)}</td>"
                f"<td>{html.escape(sec_txt)}</td>"
                f"<td>{html.escape(happened)}</td>"
                f"<td>{html.escape(suggestion)}</td></tr>"
            )

        intel24_html = (
            "<div class='card'>"
            "<h2 class='section-title'>Last 24h Intelligence</h2>"
            "<div class='muted'>What changed in the last 24 hours and what to do next.</div>"
            "<div class='table-wrap'><table class='tbl-intel'><thead><tr><th>Ticker</th><th>Day %</th><th>Insider</th><th>SEC (24h)</th><th>What Happened</th><th>Suggestion</th></tr></thead><tbody>"
            + "".join(intel24_rows)
            + "</tbody></table></div></div>"
        )
    if fast_mode and tab in {"portfolio", "watchlist"}:
        intelligence_html = ""

    table = (
        "<div class='table-wrap'><table class='tbl-holdings'><thead><tr><th>Ticker</th><th style='width:86px;'>Thesis Signal</th><th>Shares</th><th>Cost/Share</th><th>Now</th><th>Day %</th><th>Since Basis %</th><th>P&L $</th><th>Weight</th><th>Earnings</th><th>Analyst Upside</th><th>Action</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    ) if rows else "<div class='muted'>No portfolio/watchlist companies yet.</div>"
    msg_html = f"<div class='msg'>{html.escape(message)}</div>" if message else ""
    tab_port = " style='font-weight:700;border-color:#ff9f87;background:#ff7a59;color:#14263b;'" if tab == "portfolio" else ""
    tab_watch = " style='font-weight:700;border-color:#ff9f87;background:#ff7a59;color:#14263b;'" if tab == "watchlist" else ""
    tab_all = " style='font-weight:700;border-color:#ff9f87;background:#ff7a59;color:#14263b;'" if tab == "all" else ""
    deep_q = "&deep=1" if deep_mode else ""
    tab_port_href = f"/universe?tab=portfolio{deep_q}"
    tab_watch_href = f"/universe?tab=watchlist{deep_q}"
    tab_all_href = f"/universe?tab=all{deep_q}"
    intel_expand_href = f"/universe?tab={urllib.parse.quote(tab)}&deep=1#intelPanel"
    intel_collapse_href = f"/universe?tab={urllib.parse.quote(tab)}#intelPanel"
    all_manage_tickers = sorted(set(watch.keys()) | set(port.keys()))
    base_options = "".join(
        f"<option value='{c}'{' selected' if c == base_ccy else ''}>{c}</option>"
        for c in ["USD", "EUR", "GBP", "TRY", "JPY", "CAD", "CHF", "AUD"]
    )
    cash_opts = ""
    for idx, r in enumerate(cash_balance_rows):
        ccy = str(r.get("currency") or "USD")
        amt = to_float(str(r.get("amount") or "")) or 0.0
        note = str(r.get("note") or "")
        label = f"{ccy} {amt:,.2f}" + (f" - {note[:32]}" if note else "")
        cash_opts += f"<option value='{idx}'>{html.escape(label)}</option>"
    if not cash_opts:
        cash_opts = "<option value=''>No cash entries</option>"
    all_opts = "".join(
        f"<option value='{html.escape(t)}'>{html.escape(t)}</option>" for t in all_manage_tickers
    ) or "<option value=''>No tickers</option>"
    add_html = f"""
<div class='card add-compact'>
  <form method='post' action='/asset/add' class='add-row'>
    <input type='hidden' name='source' value='universe'>
    <input type='hidden' name='tab' value='{html.escape(tab)}'>
    <select name='list_type'>
      <option value='portfolio' selected>Portfolio</option>
      <option value='watchlist'>Watchlist</option>
    </select>
    <select name='asset_type'>
      <option value='stock' selected>Stock</option>
      <option value='bond'>Bond</option>
      <option value='cash'>Cash</option>
    </select>
    <input name='ticker' placeholder='Ticker / CCY'>
    <input name='shares' placeholder='Shares / Amount'>
    <input name='cost_basis' placeholder='Cost'>
    <input name='notes' placeholder='Note'>
    <button>Add</button>
  </form>
</div>
"""

    manage_html = f"""
<div class='card manage-mini'>
  <span class='muted'>Manage</span>
  <form method='post' action='/list/remove' class='mini-inline' onsubmit="return confirm('Remove selected ticker from selected list?');">
    <input type='hidden' name='source' value='universe'>
    <input type='hidden' name='tab' value='{html.escape(tab)}'>
    <select name='list_type'>
      <option value='watchlist'>Watchlist</option>
      <option value='portfolio'>Portfolio</option>
    </select>
    <select name='ticker'>{all_opts}</select>
    <input name='reason' placeholder='Reason (required for watchlist remove)'>
    <button type='submit'>Remove</button>
  </form>
  <form method='post' action='/cash/update' class='mini-inline'>
    <input type='hidden' name='source' value='universe'>
    <input type='hidden' name='tab' value='{html.escape(tab)}'>
    <select name='cash_id'>{cash_opts}</select>
    <input name='amount' placeholder='Amount (0 = remove)'>
    <input name='note' placeholder='Note'>
    <button type='submit'>Edit</button>
  </form>
</div>
"""
    return f"""<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>My Companies</title>
<style>body{{margin:0;background:radial-gradient(980px 460px at 0% 0%, #23b2ae22 0%, transparent 62%),radial-gradient(900px 420px at 100% 0%, #ff955422 0%, transparent 64%),#f3f9ff;color:#17324a;font-family:'Avenir Next','Helvetica Neue',sans-serif;}}
a{{color:#1e5f8a;}}
.wrap{{max-width:1280px;margin:0 auto;padding:16px;}}.card{{background:linear-gradient(180deg,#ffffff,#f7fbff);border:1px solid #c8dced;border-radius:12px;padding:12px;margin-bottom:10px;box-shadow:0 8px 22px rgba(39,86,126,.08);}}
.btn{{background:#ff7a59;color:#ffffff;border:1px solid #ff7a59;border-radius:8px;padding:6px 10px;text-decoration:none;display:inline-block;white-space:nowrap;font-size:13px;font-weight:700;}}
.btn:hover{{background:#e66040;}}
.table-wrap{{overflow:auto;border:1px solid #c8dced;border-radius:10px;margin-top:8px;}}
table{{width:100%;border-collapse:collapse;}}
th,td{{border-bottom:1px solid #d5e6f3;padding:9px 10px;text-align:left;vertical-align:top;font-size:12px;line-height:1.35;}}
th{{color:#355f7f;font-weight:700;background:#edf5fc;position:sticky;top:0;z-index:1;white-space:nowrap;}}
tbody tr:nth-child(odd) td{{background:#ffffff;}}
tbody tr:nth-child(even) td{{background:#f8fcff;}}
tr:hover td{{background:#ecf6ff !important;}}
.tbl-holdings{{min-width:1140px;}}
.tbl-intel{{min-width:980px;}}
.tk-cell a{{font-weight:800;letter-spacing:.2px;color:#33475b;}}
.pct-chip{{display:inline-block;min-width:70px;text-align:center;border-radius:999px;padding:2px 8px;font-weight:700;font-size:11px;border:1px solid #cbd8e3;background:#f5f8fa;color:#516f90;}}
.pct-chip.up{{border-color:#00bda5;background:#e5f8f6;color:#00bda5;}}
.pct-chip.down{{border-color:#d93b59;background:#fbeaef;color:#d93b59;}}
.pct-chip.flat{{border-color:#cbd8e3;background:#f5f8fa;color:#516f90;}}
.pct-text.up{{color:#00bda5;font-weight:700;}}
.pct-text.down{{color:#d93b59;font-weight:700;}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;}}
.form-grid{{display:grid;grid-template-columns:1fr 1fr auto;gap:8px;align-items:end;}}
.form-grid.port{{grid-template-columns:1fr 1fr 1fr 1fr auto;}}
.top-toolbar{{border-color:#233c4e;padding:8px 10px;}}
.top-toolbar summary{{cursor:pointer;font-weight:700;list-style:none;}}
.top-toolbar summary::-webkit-details-marker{{display:none;}}
.tb-row{{display:grid;grid-template-columns:1.2fr .9fr;gap:6px;margin-top:8px;}}
.tb-inline{{display:grid;grid-template-columns:1fr;gap:5px;background:#ffffff;border:1px solid #e1e6eb;border-radius:8px;padding:6px;}}
.tb-compact{{grid-template-columns:1fr;}}
.manage-mini{{display:flex;align-items:center;gap:8px;padding:7px 8px;}}
.mini-inline{{display:flex;align-items:center;gap:6px;margin:0;}}
.mini-inline select{{width:auto;min-width:110px;max-width:220px;padding:5px 6px;font-size:12px;}}
.mini-inline button{{padding:5px 8px;font-size:12px;}}
.top-tabs{{display:flex;justify-content:space-between;gap:8px;align-items:center;}}
.base-top{{display:flex;align-items:center;gap:6px;}}
.base-top select{{min-width:74px;max-width:90px;padding:5px 6px;font-size:12px;}}
.base-top button{{padding:5px 8px;min-width:30px;}}
.add-compact{{border-color:#233c4e;padding:8px;}}
.add-row{{display:grid;grid-template-columns:1fr 1fr 1.2fr 1fr .9fr 1fr auto;gap:6px;align-items:end;}}
.base-mini{{display:flex;align-items:center;gap:6px;}}
.base-mini select{{min-width:74px;max-width:90px;padding:5px 6px;font-size:12px;}}
.base-mini button{{padding:5px 8px;min-width:30px;}}
.base-ico{{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;border:1px solid #d0dae3;border-radius:999px;color:#516f90;font-size:11px;line-height:1;}}
.field label{{display:block;color:#516f90;font-size:12px;margin-bottom:4px;}}
.actions{{display:flex;flex-wrap:wrap;gap:6px;align-items:flex-start;min-width:190px;}}
.actions .btn{{font-size:12px;padding:5px 8px;}}
input{{width:100%;padding:7px;border-radius:7px;border:1px solid #9fbad0;background:#ffffff;color:#102a43;box-sizing:border-box;}}
button{{background:#ff7a59;color:#ffffff;border:1px solid #ff7a59;border-radius:7px;padding:7px 10px;cursor:pointer;font-weight:700;}}
button:hover{{background:#e66040;}}
select{{width:100%;padding:7px;border-radius:7px;border:1px solid #9fbad0;background:#ffffff;color:#102a43;box-sizing:border-box;}}
.warn-badge{{display:inline-block;background:#fbeaeF;border:1px solid #d93b59;color:#d93b59;border-radius:999px;padding:2px 8px;font-size:12px;font-weight:700;}}
.pm-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;}}
.pm-asof{{font-size:12px;}}
.pm-grid{{display:grid;grid-template-columns:1fr 1fr 1fr 1fr 2fr;gap:10px;align-items:stretch;}}
.pm-kpi{{border:1px solid #c8dced;background:#ffffff;border-radius:10px;padding:10px;}}
.pmv{{font-size:20px;font-weight:800;margin-top:2px;letter-spacing:.2px;}}
.pm-top .pmv{{font-size:15px;}}
.pm-chip-row{{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;}}
.pm-chip{{display:inline-block;background:#edf5fc;border:1px solid #b6d1e6;color:#244866;border-radius:999px;padding:3px 8px;font-size:12px;}}
.pm-bars{{margin-top:8px;display:grid;gap:6px;}}
.pm-hold-head{{display:flex;justify-content:space-between;gap:8px;color:#486581;font-size:12px;}}
.pm-track{{margin-top:3px;height:7px;background:#edf5fc;border:1px solid #c8dced;border-radius:999px;overflow:hidden;}}
.pm-track span{{display:block;height:100%;background:linear-gradient(90deg,#3ea2d8,#79d5ff);}}
.intel-card{{border-color:#e1e6eb;}}
.intel-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;}}
.intel-box{{border:1px solid #c8dced;background:#ffffff;border-radius:10px;padding:10px;}}
.intel-box h3{{margin:0 0 8px 0;font-size:13px;color:#355f7f;}}
.intel-box ul{{margin:0 0 0 16px;padding:0;}}
.intel-box li{{margin:5px 0;font-size:12px;line-height:1.35;}}
.focus-box{{border:1px solid #c8dced;background:#ffffff;border-radius:10px;padding:10px;}}
.focus-title{{font-size:12px;font-weight:700;color:#355f7f;margin-bottom:6px;}}
.exp-row{{display:flex;justify-content:space-between;gap:8px;padding:5px 0;border-bottom:1px solid #d7e4ee;font-size:12px;}}
.exp-row:last-child{{border-bottom:0;}}
.muted{{color:#486581;font-size:12px;}}
.section-title{{margin:0 0 8px 0;font-size:16px;color:#33475b;}}
.profile-table .pitem{{border:1px solid #d7e4ee;border-radius:10px;background:#ffffff;margin:8px 0;overflow:hidden;}}
.profile-table .pitem summary{{list-style:none;cursor:pointer;display:flex;justify-content:space-between;gap:10px;align-items:center;padding:10px 12px;background:#f5f8fa;border-bottom:1px solid #d7e4ee;}}
.profile-table .pitem summary::-webkit-details-marker{{display:none;}}
.profile-table .ptk{{font-weight:800;letter-spacing:.2px;}}
.profile-table .pgrid{{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:10px 12px;}}
.profile-table h4{{margin:0 0 6px 0;font-size:13px;color:#355f7f;}}
.profile-table p{{margin:0;font-size:12px;line-height:1.45;color:#33475b;}}
.deep-modal{{position:fixed;inset:0;display:none;align-items:center;justify-content:center;background:rgba(30,47,66,.26);z-index:10030;}}
.deep-modal.open{{display:flex;}}
.deep-modal-card{{width:min(900px,calc(100vw - 28px));max-height:calc(100vh - 28px);overflow:auto;background:#ffffff;border:1px solid #d7e4ee;border-radius:12px;box-shadow:0 12px 28px rgba(31,58,86,.14);}}
.deep-modal-head{{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid #d7e4ee;background:#f5f8fa;}}
.deep-modal-head h3{{margin:0;font-size:14px;}}
.deep-modal-head button{{border:1px solid #9fbad0;padding:5px 9px;border-radius:7px;background:#ffffff;color:#355f7f;cursor:pointer;}}
.deep-modal-body{{padding:12px;}}
.deep-status{{color:#486581;font-size:12px;margin-bottom:8px;}}
.deep-output{{white-space:pre-wrap;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;line-height:1.4;border:1px solid #d7e4ee;border-radius:8px;padding:10px;background:#f8fbfe;color:#33475b;}}
.deep-actions{{margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;}}
.deep-actions a{{border:1px solid #ff7a59;padding:6px 10px;border-radius:8px;background:#ff7a59;color:#ffffff;text-decoration:none;}}
.thesis-signal-btn{{border:1px solid #9fbad0;border-radius:999px;padding:2px 8px;font-size:11px;line-height:1.2;cursor:pointer;background:#ffffff;color:#355f7f;white-space:nowrap;}}
.thesis-signal-btn.sig-green{{border-color:#00bda5;background:#e5f8f6;color:#008f7e;}}
.thesis-signal-btn.sig-red{{border-color:#d93b59;background:#fbeaeF;color:#b62e4a;}}
.thesis-signal-btn.sig-na{{border-color:#b8c7d5;background:#f5f8fa;color:#486581;}}
.sig-tip{{position:fixed;right:16px;bottom:16px;width:min(460px,calc(100vw - 24px));background:#ffffff;border:1px solid #d7e4ee;border-radius:10px;box-shadow:0 10px 24px rgba(31,58,86,.12);z-index:10040;}}
.sig-tip-head{{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 10px;background:#f5f8fa;border-bottom:1px solid #d7e4ee;font-size:12px;}}
.sig-tip-head button{{padding:3px 8px;font-size:11px;}}
.sig-tip-body{{padding:10px;font-size:12px;line-height:1.4;color:#33475b;white-space:pre-wrap;max-height:220px;overflow:auto;}}
@media (max-width:1400px){{.tb-row{{grid-template-columns:1fr 1fr 1fr;}}}}
@media (max-width:1300px){{.add-row{{grid-template-columns:1fr 1fr 1fr 1fr;}}}}
@media (max-width:1100px){{.grid{{grid-template-columns:1fr;}}.form-grid,.form-grid.port{{grid-template-columns:1fr;}}.actions{{min-width:0;}}.pm-grid{{grid-template-columns:1fr;}}.intel-grid{{grid-template-columns:1fr;}}.tb-row{{grid-template-columns:1fr;}}.top-tabs{{flex-direction:column;align-items:flex-start;}}.add-row{{grid-template-columns:1fr;}}.manage-mini{{flex-direction:column;align-items:flex-start;}}.mini-inline{{flex-wrap:wrap;}}}}
</style></head>
<body><div class='wrap'><div class='card'><h1>My Companies</h1><a class='btn' href='/'>Home</a></div>
<div class='card top-tabs'>
  <div>
    <a class='btn' href='{tab_port_href}'{tab_port}>Portfolio</a>
    <a class='btn' href='{tab_watch_href}'{tab_watch}>Watchlist</a>
    <a class='btn' href='{tab_all_href}'{tab_all}>All</a>
  </div>
  <form method='post' action='/settings/base_currency' class='base-top'>
    <input type='hidden' name='source' value='universe'>
    <input type='hidden' name='tab' value='{html.escape(tab)}'>
    <span class='base-ico' title='Base currency'>⚙︎</span>
    <select name='base_currency'>{base_options}</select>
    <button type='submit'>✓</button>
  </form>
</div>
{msg_html}
{add_html}
{manage_html}
{vital_html}
<div id='intelPanel' class='card'>
  <details {'open' if deep_mode else ''}>
    <summary style='cursor:pointer;font-weight:700;color:#355f7f;'>Get Intelligence</summary>
    <div style='margin-top:8px;'>
      {(
        intelligence_html + intel24_html + f"<div style='margin-top:8px;'><a class='btn' href='{intel_collapse_href}'>Collapse Intelligence</a></div>"
      ) if deep_mode else (
        "<div class='muted'>Click below to load full portfolio/watchlist intelligence on this page.</div>"
        + f"<div style='margin-top:8px;'><a class='btn' href='{intel_expand_href}'>Expand Intelligence</a></div>"
      )}
    </div>
  </details>
</div>
<div class='card'>{table}</div></div>
<div id='sigTip' class='sig-tip' style='display:none;' aria-hidden='true'>
  <div class='sig-tip-head'>
    <strong id='sigTipTitle'>Thesis Signal</strong>
    <button type='button' id='sigTipClose'>Close</button>
  </div>
  <div id='sigTipBody' class='sig-tip-body'></div>
</div>
<div id='deepDiveModal' class='deep-modal' aria-hidden='true'>
  <div class='deep-modal-card'>
    <div class='deep-modal-head'>
      <h3 id='deepDiveTitle'>Deep Dive</h3>
      <button id='deepDiveClose' type='button'>Close</button>
    </div>
    <div class='deep-modal-body'>
      <div id='deepDiveStatus' class='deep-status'></div>
      <pre id='deepDiveOutput' class='deep-output'></pre>
      <div class='deep-actions'>
        <a id='deepDiveOpenFull' href='#'>Open Full Report</a>
      </div>
    </div>
  </div>
</div>
<script>
  (function() {{
    var tip = document.getElementById('sigTip');
    var tipTitle = document.getElementById('sigTipTitle');
    var tipBody = document.getElementById('sigTipBody');
    var tipClose = document.getElementById('sigTipClose');
    function closeTip() {{
      if (!tip) return;
      tip.style.display = 'none';
      tip.setAttribute('aria-hidden', 'true');
    }}
    if (tipClose) tipClose.addEventListener('click', closeTip);

    var modal = document.getElementById('deepDiveModal');
    var closeBtn = document.getElementById('deepDiveClose');
    var title = document.getElementById('deepDiveTitle');
    var status = document.getElementById('deepDiveStatus');
    var output = document.getElementById('deepDiveOutput');
    var openFull = document.getElementById('deepDiveOpenFull');
    function esc(v) {{
      return String(v == null ? '' : v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }}
    function setOpen(isOpen) {{
      if (!modal) return;
      if (isOpen) {{
        modal.classList.add('open');
        modal.setAttribute('aria-hidden', 'false');
      }} else {{
        modal.classList.remove('open');
        modal.setAttribute('aria-hidden', 'true');
      }}
    }}
    if (closeBtn) closeBtn.addEventListener('click', function() {{ setOpen(false); }});
    if (modal) {{
      modal.addEventListener('click', function(e) {{
        if (e.target === modal) setOpen(false);
      }});
    }}
    function runDeepDive(ticker) {{
      if (!ticker) return;
      if (title) title.textContent = 'Deep Dive: ' + ticker;
      if (status) status.textContent = 'Analyzing ' + ticker + '...';
      if (output) output.textContent = 'Collecting recent news and running local AI analysis...';
      if (openFull) openFull.setAttribute('href', '/company?t=' + encodeURIComponent(ticker));
      setOpen(true);
      var body = new URLSearchParams({{ ticker: ticker }});
      fetch('/api/deep_dive', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
        body: body.toString()
      }})
      .then(function(r) {{ return r.json(); }})
      .then(function(res) {{
        var m = (res && res.model) ? (' | model: ' + res.model) : '';
        if (status) status.textContent = ((res && res.ok) ? 'Completed' : 'Completed with warning') + m;
        var top = (res && Array.isArray(res.headlines)) ? res.headlines.slice(0, 5) : [];
        var raw = top.length ? ("\\n\\nTop 5 Raw Headlines:\\n- " + top.map(function(h) {{ return String(h || '').trim(); }}).join('\\n- ')) : "";
        if (output) output.innerHTML = esc(((res && res.analysis) || 'No response.') + raw);
        if (openFull) openFull.setAttribute('href', '/company?t=' + encodeURIComponent((res && res.ticker) || ticker));
      }})
      .catch(function(err) {{
        if (status) status.textContent = 'Deep dive request failed.';
        if (output) output.innerHTML = esc(String(err || 'request_failed'));
      }});
    }}
    document.addEventListener('click', function(e) {{
      var b = e.target && e.target.closest ? e.target.closest('.thesis-signal-btn') : null;
      if (b) {{
        var st = b.getAttribute('data-status') || 'Signal';
        var rs = b.getAttribute('data-reasoning') || 'No signal reasoning available yet.';
        if (tipTitle) tipTitle.textContent = 'Thesis Signal: ' + st;
        if (tipBody) tipBody.textContent = rs;
        if (tip) {{
          tip.style.display = 'block';
          tip.setAttribute('aria-hidden', 'false');
        }}
        return;
      }}
      var a = e.target && e.target.closest ? e.target.closest('a.deep-dive-trigger') : null;
      if (!a) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      var ticker = (a.getAttribute('data-ticker') || '').toUpperCase();
      if (!ticker) return;
      e.preventDefault();
      runDeepDive(ticker);
    }});
  }})();
</script>
</body></html>"""


def watchlist_edit_html(ticker: str, message: str = "") -> str:
    t = (ticker or "").strip().upper()
    entry = None
    for e in read_watchlist_entries(DATA / "my_watchlist.txt"):
        if e.get("ticker", "").upper() == t:
            entry = e
            break
    if not t or not entry:
        return universe_html(tab="watchlist", message=f"Watchlist entry not found for {t or '-'}")
    added_at = entry.get("added_at", "")
    added_price = entry.get("added_price", "")
    msg_html = f"<div class='msg'>{html.escape(message)}</div>" if message else ""
    return f"""<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Edit Watchlist {html.escape(t)}</title>
<style>body{{margin:0;background:#0b1014;color:#e7eef6;font-family:'Avenir Next','Helvetica Neue',sans-serif;}}
.wrap{{max-width:760px;margin:0 auto;padding:20px;}}.card{{background:#131d25;border:1px solid #294051;border-radius:12px;padding:14px;margin-bottom:12px;}}
.btn,button{{background:#1a3d56;color:#e7eef6;border:1px solid #2e5c7b;border-radius:8px;padding:8px 12px;text-decoration:none;cursor:pointer;}}
input{{width:100%;padding:8px;border-radius:8px;border:1px solid #2e5c7b;background:#0f1921;color:#e7eef6;margin-top:6px;}}</style></head>
<body><div class='wrap'><div class='card'><h1>Edit Watchlist: {html.escape(t)}</h1><a class='btn' href='/universe?tab=watchlist'>Back</a></div>{msg_html}
<div class='card'><form method='post' action='/watchlist/update'>
<input type='hidden' name='ticker' value='{html.escape(t)}'>
<label>Added At</label><input name='added_at' value='{html.escape(added_at)}' placeholder='YYYY-MM-DD HH:MM'>
<label>Added Price</label><input name='added_price' value='{html.escape(added_price)}' placeholder='e.g. 245.10'>
<label>Removal Reason (required when removing)</label><input name='reason' value='' placeholder='Why removed?'>
<button name='action' value='save'>Save</button> <button name='action' value='remove'>Remove</button>
</form></div></div></body></html>"""


def portfolio_edit_html(ticker: str, message: str = "") -> str:
    t = (ticker or "").strip().upper()
    row = None
    for r in read_portfolio_rows(DATA / "portfolio.csv"):
        if r[0].upper() == t:
            row = r
            break
    if not t or not row:
        return universe_html(tab="portfolio", message=f"Portfolio entry not found for {t or '-'}")
    shares, cost, notes = row[1], row[2], row[3]
    msg_html = f"<div class='msg'>{html.escape(message)}</div>" if message else ""
    return f"""<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Edit Portfolio {html.escape(t)}</title>
<style>body{{margin:0;background:#0b1014;color:#e7eef6;font-family:'Avenir Next','Helvetica Neue',sans-serif;}}
.wrap{{max-width:760px;margin:0 auto;padding:20px;}}.card{{background:#131d25;border:1px solid #294051;border-radius:12px;padding:14px;margin-bottom:12px;}}
.btn,button{{background:#1a3d56;color:#e7eef6;border:1px solid #2e5c7b;border-radius:8px;padding:8px 12px;text-decoration:none;cursor:pointer;}}
input{{width:100%;padding:8px;border-radius:8px;border:1px solid #2e5c7b;background:#0f1921;color:#e7eef6;margin-top:6px;}}</style></head>
<body><div class='wrap'><div class='card'><h1>Edit Portfolio: {html.escape(t)}</h1><a class='btn' href='/universe?tab=portfolio'>Back</a></div>{msg_html}
<div class='card'><form method='post' action='/portfolio/update'>
<input type='hidden' name='ticker' value='{html.escape(t)}'>
<label>Shares</label><input name='shares' value='{html.escape(shares)}'>
<label>Cost Basis (per share)</label><input name='cost_basis' value='{html.escape(cost)}'>
<label>Notes</label><input name='notes' value='{html.escape(notes)}'>
<button name='action' value='save'>Save</button> <button name='action' value='remove'>Remove</button>
</form></div></div></body></html>"""


def earnings_reported_html() -> str:
    snap = get_reported_earnings_snapshot(limit=200)
    parsed = list(snap.get("parsed") or [])
    holdings_rows = list(snap.get("holdings_rows") or [])
    this_week_rows = list(snap.get("this_week_rows") or [])
    portfolio = set(snap.get("portfolio") or set())
    watchlist = set(snap.get("watchlist") or set())
    fr = dict(snap.get("freshness") or {})
    label = str(fr.get("label") or "-")
    stamp = str(fr.get("stamp") or "-")
    note = str(fr.get("note") or "Data freshness unknown.")
    color = "#8ebad6"
    if label == "FRESH":
        color = "#8fe2b4"
    elif label == "AGING":
        color = "#ffd98b"
    elif label == "STALE":
        color = "#ff9ea8"
    freshness_html = (
        "<div class='card'>"
        "<div class='pm-head'>"
        "<h2 style='margin:0;'>Freshness</h2>"
        f"<div class='muted'>As of {html.escape(stamp)}</div>"
        "</div>"
        f"<div style='color:{color};font-weight:800;margin-top:6px;'>{html.escape(label)}</div>"
        f"<div class='muted'>{html.escape(note)} Auto-refreshed from latest earnings feed daily.</div>"
        "</div>"
    )
    # Sector read-through relevance: reported names in same sector as holdings.
    universe = sorted(portfolio | watchlist)
    hold_prof = get_portfolio_profiles(universe)
    hold_sectors = {str((hold_prof.get(t, {}) or {}).get("sector") or "").strip() for t in universe}
    hold_sectors = {s for s in hold_sectors if s}
    rep_tickers = [str(r.get("ticker") or "") for r in parsed]
    rep_prof = get_portfolio_profiles(rep_tickers)
    readthrough_rows = []
    for r in parsed:
        t = str(r.get("ticker") or "")
        sec = str((rep_prof.get(t, {}) or {}).get("sector") or "").strip()
        if sec and sec in hold_sectors and str(r.get("in_scope")) == "-":
            r2 = dict(r)
            r2["readthrough_sector"] = sec
            readthrough_rows.append(r2)
    readthrough_rows.sort(key=lambda r: int(r.get("score") or 0), reverse=True)

    p_hits = sum(1 for r in parsed if str(r.get("in_scope")) == "portfolio")
    w_hits = sum(1 for r in parsed if str(r.get("in_scope")) == "watchlist")
    miss_count = sum(1 for r in parsed if str(r.get("verdict")) == "MISS")
    beat_count = sum(1 for r in parsed if str(r.get("verdict")) == "BEAT")

    ai_lines = []
    holdings_universe = sorted(portfolio | watchlist)
    ai_lines.append(f"Holdings universe: {', '.join(holdings_universe) if holdings_universe else '(none)'}")
    ai_lines.append(f"Portfolio hits: {p_hits} | Watchlist hits: {w_hits} | Total reported parsed: {len(parsed)}")
    ai_lines.append(f"Beat count: {beat_count} | Miss count: {miss_count}")
    for r in parsed[:24]:
        ai_lines.append(
            f"{r.get('ticker')} | in={r.get('in_scope')} | verdict={r.get('verdict')} | surprise={r.get('surprise')} | mcap={r.get('mcap_txt')} | score={r.get('score')}"
        )
    if holdings_rows:
        ai_summary = _ai_cached_reliable_summary(
            cache_key=f"reported_earn_ai_holdings:{len(parsed)}:{p_hits}:{w_hits}:{beat_count}:{miss_count}",
            source_text="\n".join(ai_lines),
            system=(
                "You are an earnings triage analyst. Use ONLY provided rows. "
                "Return exactly 4 bullets: (1) holdings impact, (2) biggest positive surprise in holdings, "
                "(3) biggest negative surprise/risk in holdings, (4) tomorrow action plan for holdings/watchlist only. "
                "If there are no holdings hits, say that explicitly and do not suggest position changes in non-holdings. "
                "No external facts."
            ),
            ttl_seconds=900,
        )
    else:
        pos = next((r for r in parsed if isinstance(r.get("surprise"), float) and float(r.get("surprise")) > 0), None)
        neg = next((r for r in parsed if isinstance(r.get("surprise"), float) and float(r.get("surprise")) < 0), None)
        ai_summary = (
            "- No direct portfolio/watchlist earnings prints in this reported set.\n"
            f"- Biggest positive surprise (market context): {pos.get('ticker')} ({fmt_pct(float(pos.get('surprise')))})."
            if pos else "- Biggest positive surprise: insufficient estimate data."
        )
        ai_summary += (
            f"\n- Biggest negative surprise/risk (market context): {neg.get('ticker')} ({fmt_pct(float(neg.get('surprise')))})."
            if neg else "\n- Biggest negative surprise/risk: insufficient estimate data."
        )
        ai_summary += (
            "\n- Action plan: keep holdings/watchlist discipline; use these as read-through signals only, "
            "and add any high-importance names to watchlist if strategically relevant."
        )

    def _row_html(r: dict[str, object]) -> str:
        t = html.escape(str(r.get("ticker") or "-"))
        in_scope_raw = str(r.get("in_scope") or "-")
        in_scope = html.escape(in_scope_raw)
        date_txt = html.escape(str(r.get("date_txt") or "-"))
        mcap_txt = html.escape(str(r.get("mcap_txt") or "-"))
        actual = r.get("actual")
        est = r.get("est")
        surprise = r.get("surprise")
        verdict = str(r.get("verdict") or "-")
        imp = str(r.get("importance") or "LOW")
        imp_color = {"CRITICAL": "#ff7f7f", "HIGH": "#ffb266", "MEDIUM": "#ffd98b", "LOW": "#8ebad6"}.get(imp, "#8ebad6")
        verdict_color = "#66d38f" if verdict == "BEAT" else ("#ff7f7f" if verdict == "MISS" else "#9ab0c0")
        return (
            f"<tr class='er-row' data-scope='{html.escape(in_scope_raw)}' data-verdict='{html.escape(verdict)}' data-importance='{html.escape(imp)}'>"
            f"<td><a href='/company_file?t={t}'>{t}</a></td>"
            f"<td>{in_scope}</td>"
            f"<td>{date_txt}</td>"
            f"<td>{f'{float(actual):.2f}' if isinstance(actual, float) else '-'}</td>"
            f"<td>{f'{float(est):.2f}' if isinstance(est, float) else '-'}</td>"
            f"<td>{fmt_pct(float(surprise)) if isinstance(surprise, float) else '-'}</td>"
            f"<td><span style='color:{verdict_color};font-weight:700;'>{html.escape(verdict)}</span></td>"
            f"<td>{mcap_txt}</td>"
            f"<td><span style='color:{imp_color};font-weight:800;'>{html.escape(imp)}</span></td>"
            f"<td><a class='btn' href='/company_file?t={t}'>Company File</a></td>"
            "</tr>"
        )

    holdings_table = (
        "<table><thead><tr><th>Ticker</th><th>In</th><th>Report Date</th><th>EPS</th><th>Est</th><th>Surprise</th><th>Verdict</th><th>MCap</th><th>Importance</th><th>Action</th></tr></thead><tbody>"
        + "".join(_row_html(r) for r in holdings_rows[:40])
        + "</tbody></table>"
    ) if holdings_rows else "<div class='muted'>No reported-earnings rows tied to your portfolio/watchlist in current feed.</div>"

    this_week_table = (
        "<table><thead><tr><th>Ticker</th><th>In</th><th>Report Date</th><th>EPS</th><th>Est</th><th>Surprise</th><th>Verdict</th><th>MCap</th><th>Importance</th><th>Action</th></tr></thead><tbody>"
        + "".join(_row_html(r) for r in this_week_rows[:120])
        + "</tbody></table>"
    ) if this_week_rows else "<div class='muted'>No reported rows fall inside the current calendar week.</div>"

    all_table = (
        "<table><thead><tr><th>Ticker</th><th>In</th><th>Report Date</th><th>EPS</th><th>Est</th><th>Surprise</th><th>Verdict</th><th>MCap</th><th>Importance</th><th>Action</th></tr></thead><tbody>"
        + "".join(_row_html(r) for r in parsed[:80])
        + "</tbody></table>"
    ) if parsed else "<div class='muted'>No reported earnings lines available.</div>"
    readthrough_table = (
        "<table><thead><tr><th>Ticker</th><th>Sector Match</th><th>Report Date</th><th>EPS</th><th>Est</th><th>Surprise</th><th>Verdict</th><th>MCap</th><th>Importance</th><th>Action</th></tr></thead><tbody>"
        + "".join(
            (
                "<tr>"
                f"<td><a href='/company_file?t={html.escape(str(r.get('ticker') or '-'))}'>{html.escape(str(r.get('ticker') or '-'))}</a></td>"
                f"<td>{html.escape(str(r.get('readthrough_sector') or '-'))}</td>"
                f"<td>{html.escape(str(r.get('date_txt') or '-'))}</td>"
                f"<td>{f'{float(r.get('actual')):.2f}' if isinstance(r.get('actual'), float) else '-'}</td>"
                f"<td>{f'{float(r.get('est')):.2f}' if isinstance(r.get('est'), float) else '-'}</td>"
                f"<td>{fmt_pct(float(r.get('surprise'))) if isinstance(r.get('surprise'), float) else '-'}</td>"
                f"<td>{html.escape(str(r.get('verdict') or '-'))}</td>"
                f"<td>{html.escape(str(r.get('mcap_txt') or '-'))}</td>"
                f"<td>{html.escape(str(r.get('importance') or '-'))}</td>"
                f"<td><a class='btn' href='/company_file?t={html.escape(str(r.get('ticker') or '-'))}'>Company File</a></td>"
                "</tr>"
            )
            for r in readthrough_rows[:30]
        )
        + "</tbody></table>"
    ) if readthrough_rows else "<div class='muted'>No sector read-through matches to your holdings in current reported set.</div>"

    ai_html = (
        "<div class='focus-box' style='margin-top:8px;'><div class='focus-title'>AI Intelligence</div>"
        f"<pre class='chart' style='white-space:pre-wrap;margin:0;'>{html.escape(ai_summary)}</pre>"
        "<div class='muted' style='margin-top:6px;'>Source scope: reported earnings feed rows + holdings map + parsed EPS surprise and market cap.</div></div>"
    ) if ai_summary else ""

    return f"""<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Reported Earnings Intelligence</title>
<style>body{{margin:0;background:#0b1014;color:#e7eef6;font-family:'Avenir Next','Helvetica Neue',sans-serif;}}
.wrap{{max-width:1360px;margin:0 auto;padding:18px;}}.card{{background:#131d25;border:1px solid #294051;border-radius:12px;padding:14px;margin-bottom:12px;}}
.btn{{background:#1a3d56;color:#e7eef6;border:1px solid #2e5c7b;border-radius:8px;padding:6px 10px;text-decoration:none;display:inline-block;}}
.muted{{color:#9ab0c0;}}table{{width:100%;border-collapse:collapse;font-size:13px;}}
th,td{{border-bottom:1px solid #243849;padding:8px;text-align:left;vertical-align:top;}}th{{color:#b8cde0;background:#0f1a23;position:sticky;top:0;z-index:1;}}
.grid{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;}}.kpi{{border:1px solid #264052;background:#0f1921;border-radius:10px;padding:10px;}}
.kpi .v{{font-size:22px;font-weight:800;margin-top:4px;}}.focus-box{{margin-top:8px;border:1px solid #2a4e67;border-radius:8px;padding:8px;background:#0f1f2b;}}
.focus-title{{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:#8fc5e9;margin-bottom:4px;}}.chart{{background:#0b151d;border:1px solid #2b4558;padding:8px;border-radius:8px;color:#95d6ff;overflow:auto;}}
@media (max-width:1100px){{.grid{{grid-template-columns:1fr;}}}}</style></head>
<body><div class='wrap'>
  <div class='card'><h1>Reported Earnings Intelligence</h1><a class='btn' href='/'>Back</a></div>
  {freshness_html}
  <div class='card'>
    <h2 style='margin-top:0;'>Filters</h2>
    <div style='display:flex;flex-wrap:wrap;gap:8px;align-items:center;'>
      <label style='display:flex;gap:6px;align-items:center;'><input id='fHoldings' type='checkbox'> Only My Holdings</label>
      <label style='display:flex;gap:6px;align-items:center;'><input id='fMiss' type='checkbox'> Only Misses</label>
      <label style='display:flex;gap:6px;align-items:center;'>Importance >=
        <select id='fImp'>
          <option value='ALL'>All</option>
          <option value='HIGH'>High</option>
          <option value='CRITICAL'>Critical</option>
        </select>
      </label>
      <span id='fCount' class='muted'></span>
    </div>
  </div>
  <div class='card'>
    <div class='grid'>
      <div class='kpi'><div class='muted'>Total Parsed Reports</div><div class='v'>{len(parsed)}</div></div>
      <div class='kpi'><div class='muted'>Holdings Coverage</div><div class='v'>{p_hits} portfolio / {w_hits} watchlist</div></div>
      <div class='kpi'><div class='muted'>Beat vs Miss</div><div class='v'>{beat_count} / {miss_count}</div></div>
    </div>
    {ai_html}
  </div>
  <div class='card'><h2>This Week Reported Earnings (Expanded)</h2><div class='muted'>Calendar week view for all reported rows in current feed.</div>{this_week_table}</div>
  <div class='card'><h2>Holdings-First (Most Relevant)</h2><div class='muted'>Sorted by scope and importance score.</div>{holdings_table}</div>
  <div class='card'><h2>Sector Read-Through (Relevant To Holdings)</h2><div class='muted'>Reported names outside your holdings that share sectors with your holdings/watchlist.</div>{readthrough_table}</div>
  <div class='card'><h2>All Reported Earnings (Ranked by Importance)</h2><div class='muted'>Importance considers holdings relevance, market cap, and EPS surprise magnitude.</div>{all_table}</div>
</div>
<script>
  (function() {{
    var cbHold = document.getElementById('fHoldings');
    var cbMiss = document.getElementById('fMiss');
    var selImp = document.getElementById('fImp');
    var cnt = document.getElementById('fCount');
    var rows = Array.prototype.slice.call(document.querySelectorAll('tr.er-row'));
    var rank = {{'LOW':1,'MEDIUM':2,'HIGH':3,'CRITICAL':4}};
    function apply() {{
      var onlyH = !!(cbHold && cbHold.checked);
      var onlyM = !!(cbMiss && cbMiss.checked);
      var imp = (selImp && selImp.value) || 'ALL';
      var minRank = imp === 'ALL' ? 0 : (rank[imp] || 0);
      var shown = 0;
      rows.forEach(function(r) {{
        var scope = (r.getAttribute('data-scope') || '-').toLowerCase();
        var verdict = (r.getAttribute('data-verdict') || '-').toUpperCase();
        var importance = (r.getAttribute('data-importance') || 'LOW').toUpperCase();
        var ok = true;
        if (onlyH && !(scope === 'portfolio' || scope === 'watchlist')) ok = false;
        if (onlyM && verdict !== 'MISS') ok = false;
        if ((rank[importance] || 0) < minRank) ok = false;
        r.style.display = ok ? '' : 'none';
        if (ok) shown += 1;
      }});
      if (cnt) cnt.textContent = shown + ' rows shown';
    }}
    if (cbHold) cbHold.addEventListener('change', apply);
    if (cbMiss) cbMiss.addEventListener('change', apply);
    if (selImp) selImp.addEventListener('change', apply);
    apply();
  }})();
</script>
</body></html>"""


if __name__ == "__main__":
    main()
