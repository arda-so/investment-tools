#!/usr/bin/env python3
"""
research_agent.py — SEC filing research assistant with watchlist,
change detection, notes, and Claude integration.

Usage:
    python3 research_agent.py init
    python3 research_agent.py watch KO AAPL
    python3 research_agent.py unwatch GOOG
    python3 research_agent.py list
    python3 research_agent.py update [--days 30] [--full]
    python3 research_agent.py status
    python3 research_agent.py changes [TICKER]
    python3 research_agent.py section KO risk_factors
    python3 research_agent.py ask "question about my portfolio"
    python3 research_agent.py note KO "interesting finding" [--category thesis]
    python3 research_agent.py notes KO
    python3 research_agent.py export KO
    python3 research_agent.py schedule

Data directory: ./data/, ./filings/, ./logs/, ./outputs/exports/
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Import shared utilities from existing modules
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

from sec_client import (
    sec_get,
    load_ticker_cik_map,
    get_all_filings,
    download_filing_text,
    sanitize_form_name,
    DEFAULT_FORMS,
)
from securities_monitor import get_recent_filings
from extract_deep_excerpts import SECTION_PATTERNS, extract_section
from tools.llm_engine import ask_ai, ask_ai_complete
from source_policy import get_mode_config, set_source_mode, SOURCE_MODES

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = REPO_ROOT / "data" / "research.db"
FILINGS_DIR = REPO_ROOT / "filings"
EXPORTS_DIR = REPO_ROOT / "outputs" / "exports"
LOG_DIR = REPO_ROOT / "logs"

SEC_EMAIL = "ahmetardasolmaz@gmail.com"

# Section name mapping: CLI name -> (label from SECTION_PATTERNS, description)
SECTION_MAP = {
    "risk_factors":        "Risk Factors (Item 1A)",
    "mda":                 "MD&A (Item 7)",
    "business":            "Business Overview (Item 1)",
    "segments":            "Segments",
    "buybacks":            "Share Count / Equity / Buybacks",
    "sbc":                 "Stock-Based Compensation",
    "debt_liquidity":      "Debt / Liquidity / Covenants",
    "revenue_recognition": "Revenue Recognition",
    "compensation":        "Proxy — Compensation",
    "governance":          "Proxy — Governance",
    "material_events":     "8-K Material Events",
}

# Reverse map: pattern label -> CLI name
LABEL_TO_CLI = {v: k for k, v in SECTION_MAP.items()}

# Keywords that signal notable changes
NOTABLE_KEYWORDS = [
    "litigation", "impairment", "restructuring", "goodwill",
    "restatement", "material weakness", "going concern",
    "default", "covenant", "investigation", "subpoena",
    "cybersecurity", "breach", "layoff", "workforce reduction",
]

# Expanded industry leader set for coverage checks in quarterly review.
INDUSTRY_LEADERS = {
    "Technology": ["AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "ADBE", "CRM", "INTC", "AMD", "QCOM"],
    "Communication Services": ["GOOGL", "META", "NFLX", "DIS", "CMCSA", "TMUS", "VZ", "T", "CHTR"],
    "Financials": ["JPM", "BAC", "WFC", "C", "GS", "MS", "BLK", "SCHW", "SPGI"],
    "Healthcare": ["LLY", "UNH", "JNJ", "MRK", "ABBV", "ABT", "TMO", "PFE", "BMY"],
    "Consumer Discretionary": ["AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "BKNG", "LOW", "TJX"],
    "Consumer Staples": ["PG", "KO", "PEP", "COST", "WMT", "PM", "MO", "MDLZ", "CL"],
    "Industrials": ["GE", "CAT", "BA", "RTX", "HON", "UPS", "UNP", "DE", "LMT", "ETN"],
    "Energy & Materials": ["XOM", "CVX", "COP", "SLB", "EOG", "FCX", "NEM", "APD", "DOW"],
    "Utilities & Real Assets": ["NEE", "SO", "DUK", "EXC", "AEP", "AMT", "PLD", "PSA", "WELL", "SPG"],
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    ticker TEXT PRIMARY KEY,
    name TEXT,
    cik TEXT,
    added_date TEXT
);

CREATE TABLE IF NOT EXISTS filings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    form TEXT NOT NULL,
    date TEXT NOT NULL,
    accession TEXT UNIQUE NOT NULL,
    doc_url TEXT,
    path TEXT,
    downloaded_at TEXT,
    FOREIGN KEY (ticker) REFERENCES companies(ticker)
);

CREATE TABLE IF NOT EXISTS sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filing_id INTEGER NOT NULL,
    section_name TEXT NOT NULL,
    content TEXT,
    content_hash TEXT,
    FOREIGN KEY (filing_id) REFERENCES filings(id)
);

CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    filing_id INTEGER NOT NULL,
    section_name TEXT NOT NULL,
    change_type TEXT NOT NULL,
    summary TEXT,
    detected_at TEXT,
    FOREIGN KEY (ticker) REFERENCES companies(ticker),
    FOREIGN KEY (filing_id) REFERENCES filings(id)
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    category TEXT DEFAULT 'general',
    content TEXT NOT NULL,
    created_at TEXT,
    FOREIGN KEY (ticker) REFERENCES companies(ticker)
);

CREATE INDEX IF NOT EXISTS idx_filings_ticker ON filings(ticker);
CREATE INDEX IF NOT EXISTS idx_filings_accession ON filings(accession);
CREATE INDEX IF NOT EXISTS idx_sections_filing ON sections(filing_id);
CREATE INDEX IF NOT EXISTS idx_changes_ticker ON changes(ticker);
CREATE INDEX IF NOT EXISTS idx_notes_ticker ON notes(ticker);

CREATE TABLE IF NOT EXISTS theses (
    ticker TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    horizon TEXT,
    key_kpis TEXT,
    buy_below REAL,
    trim_above REAL,
    fair_value_low REAL,
    fair_value_high REAL,
    invalidate_on TEXT,
    created_at TEXT,
    updated_at TEXT,
    FOREIGN KEY (ticker) REFERENCES companies(ticker)
);

CREATE TABLE IF NOT EXISTS valuation_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    as_of TEXT NOT NULL,
    price REAL NOT NULL,
    fair_value_low REAL,
    fair_value_high REAL,
    status TEXT,
    upside_to_mid_pct REAL,
    FOREIGN KEY (ticker) REFERENCES companies(ticker)
);

CREATE INDEX IF NOT EXISTS idx_theses_ticker ON theses(ticker);
CREATE INDEX IF NOT EXISTS idx_valuation_snapshots_ticker ON valuation_snapshots(ticker);
CREATE INDEX IF NOT EXISTS idx_valuation_snapshots_asof ON valuation_snapshots(as_of);

CREATE TABLE IF NOT EXISTS investor_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,              -- portfolio | watchlist
    ticker TEXT,                      -- nullable for portfolio-wide notes
    sentiment TEXT DEFAULT 'neutral', -- like | dislike | watch | change | neutral
    note TEXT NOT NULL,
    tags TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_investor_notes_scope ON investor_notes(scope);
CREATE INDEX IF NOT EXISTS idx_investor_notes_ticker ON investor_notes(ticker);
CREATE INDEX IF NOT EXISTS idx_investor_notes_created ON investor_notes(created_at);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    action TEXT NOT NULL,            -- buy | sell | hold
    status TEXT NOT NULL,            -- open | closed
    thesis TEXT NOT NULL,
    valuation_view TEXT,
    key_risks TEXT,
    trigger_condition TEXT,
    expected_return_3y REAL,
    kill_criteria TEXT,
    outcome_note TEXT,
    created_at TEXT,
    reviewed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_decisions_ticker ON decisions(ticker);
CREATE INDEX IF NOT EXISTS idx_decisions_status ON decisions(status);
CREATE INDEX IF NOT EXISTS idx_decisions_created ON decisions(created_at);
"""


def get_db():
    """Get a database connection."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    return conn


def ensure_schema(conn):
    """Apply schema/migrations for existing databases."""
    conn.executescript(SCHEMA)
    conn.commit()


# ---------------------------------------------------------------------------
# Section extraction helpers
# ---------------------------------------------------------------------------

def normalize_form_for_patterns(form):
    """Normalize form name for matching against SECTION_PATTERNS.

    SECTION_PATTERNS uses DEF_14A (underscore) but SEC EDGAR returns
    'DEF 14A' (space). We need to handle both directions.
    """
    return form.replace(" ", "_")


def extract_sections_from_text(text, form):
    """Extract all matching sections from filing text.

    Returns list of (section_cli_name, content) tuples.
    """
    form_normalized = normalize_form_for_patterns(form)
    results = []

    for section_def in SECTION_PATTERNS:
        # Check if this section applies to this form type
        # Match against both original form and normalized form
        if form not in section_def["forms"] and form_normalized not in section_def["forms"]:
            continue

        chars_after = section_def.get("chars_after", 8000)
        label = section_def["label"]
        cli_name = LABEL_TO_CLI.get(label)
        if not cli_name:
            continue

        for pattern in section_def["patterns"]:
            excerpt = extract_section(text, pattern, chars_before=300, chars_after=chars_after)
            if excerpt and len(excerpt) > 100:
                results.append((cli_name, excerpt))
                break  # Only first match per section definition

    return results


def compute_hash(content):
    """SHA256 hash of content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def generate_change_summary(old_content, new_content, section_name):
    """Generate a summary of what changed between two section versions."""
    old_len = len(old_content) if old_content else 0
    new_len = len(new_content) if new_content else 0

    parts = []

    # Size change
    if old_len > 0:
        pct = ((new_len - old_len) / old_len) * 100
        if abs(pct) >= 1:
            direction = "+" if pct > 0 else ""
            parts.append(f"Size: {direction}{pct:.0f}% ({old_len:,} -> {new_len:,} chars)")
    else:
        parts.append(f"Size: {new_len:,} chars (new)")

    # Word-level diff summary
    if old_content and new_content:
        old_words = set(old_content.lower().split())
        new_words = set(new_content.lower().split())
        added_words = new_words - old_words
        removed_words = old_words - new_words

        # Check for notable keywords in newly added words
        notable_found = []
        for kw in NOTABLE_KEYWORDS:
            kw_lower = kw.lower()
            if any(kw_lower in w for w in added_words):
                notable_found.append(kw)

        if notable_found:
            parts.append(f"New keywords: {', '.join(notable_found)}")

        parts.append(f"Words added: ~{len(added_words):,}, removed: ~{len(removed_words):,}")

    return "; ".join(parts) if parts else "Content changed"


# ---------------------------------------------------------------------------
# CLI Commands
# ---------------------------------------------------------------------------

def cmd_init(args):
    """Create data directories and initialize the SQLite database."""
    for d in [REPO_ROOT / "data", FILINGS_DIR, EXPORTS_DIR, LOG_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    conn = get_db()
    ensure_schema(conn)
    conn.close()

    print(f"Initialized research database")
    print(f"  Database: {DB_PATH}")
    print(f"  Filings:  {FILINGS_DIR}")
    print(f"  Exports:  {EXPORTS_DIR}")
    print(f"  Logs:     {LOG_DIR}")


def cmd_watch(args):
    """Add tickers to the watchlist."""
    if not args.tickers:
        print("Usage: research_agent.py watch TICKER [TICKER ...]")
        return

    conn = get_db()
    ticker_cik = load_ticker_cik_map()

    for ticker in args.tickers:
        ticker = ticker.upper()

        # Check if already watched
        existing = conn.execute(
            "SELECT ticker FROM companies WHERE ticker = ?", (ticker,)
        ).fetchone()
        if existing:
            print(f"  {ticker}: already on watchlist")
            continue

        # Look up CIK
        cik = ticker_cik.get(ticker) or ticker_cik.get(ticker.replace(".", ""))
        if not cik:
            print(f"  {ticker}: CIK not found, skipping")
            continue

        # Fetch company name from SEC
        try:
            url = f"https://data.sec.gov/submissions/CIK{cik}.json"
            resp = sec_get(url)
            data = resp.json()
            name = data.get("name", ticker)
        except Exception:
            name = ticker

        conn.execute(
            "INSERT INTO companies (ticker, name, cik, added_date) VALUES (?, ?, ?, ?)",
            (ticker, name, cik, datetime.now().isoformat()),
        )
        conn.commit()
        print(f"  {ticker}: added ({name}, CIK {cik})")

    conn.close()


def cmd_unwatch(args):
    """Remove tickers from the watchlist and cascade delete all data."""
    if not args.tickers:
        print("Usage: research_agent.py unwatch TICKER [TICKER ...]")
        return

    conn = get_db()

    for ticker in args.tickers:
        ticker = ticker.upper()

        existing = conn.execute(
            "SELECT ticker FROM companies WHERE ticker = ?", (ticker,)
        ).fetchone()
        if not existing:
            print(f"  {ticker}: not on watchlist")
            continue

        # Get filing IDs for cascade delete
        filing_ids = [
            row["id"] for row in
            conn.execute("SELECT id FROM filings WHERE ticker = ?", (ticker,)).fetchall()
        ]

        # Delete sections and changes for these filings
        for fid in filing_ids:
            conn.execute("DELETE FROM sections WHERE filing_id = ?", (fid,))
            conn.execute("DELETE FROM changes WHERE filing_id = ?", (fid,))

        conn.execute("DELETE FROM changes WHERE ticker = ?", (ticker,))
        conn.execute("DELETE FROM filings WHERE ticker = ?", (ticker,))
        conn.execute("DELETE FROM notes WHERE ticker = ?", (ticker,))
        conn.execute("DELETE FROM companies WHERE ticker = ?", (ticker,))
        conn.commit()

        # Delete filing files
        for f in FILINGS_DIR.glob(f"{ticker}_*"):
            f.unlink()

        print(f"  {ticker}: removed (all data deleted)")

    conn.close()


def cmd_list(args):
    """Show the watchlist with filing counts."""
    conn = get_db()
    companies = conn.execute(
        "SELECT ticker, name, cik, added_date FROM companies ORDER BY ticker"
    ).fetchall()

    if not companies:
        print("Watchlist is empty. Use 'watch TICKER' to add companies.")
        return

    print(f"{'Ticker':<8} {'Name':<35} {'CIK':<12} {'Filings':>8}  {'Added'}")
    print("-" * 85)

    for c in companies:
        count = conn.execute(
            "SELECT COUNT(*) as cnt FROM filings WHERE ticker = ?", (c["ticker"],)
        ).fetchone()["cnt"]
        added = c["added_date"][:10] if c["added_date"] else "?"
        print(f"{c['ticker']:<8} {c['name'][:34]:<35} {c['cik']:<12} {count:>8}  {added}")

    print(f"\n{len(companies)} companies watched")
    conn.close()


def cmd_update(args):
    """Download new filings, extract sections, detect changes."""
    conn = get_db()

    if args.ticker:
        tickers = [t.strip().upper() for t in args.ticker.split(",")]
        placeholders = ",".join("?" * len(tickers))
        companies = conn.execute(
            f"SELECT ticker, cik FROM companies WHERE ticker IN ({placeholders}) ORDER BY ticker",
            tickers,
        ).fetchall()
        missing = set(tickers) - {c["ticker"] for c in companies}
        if missing:
            print(f"Not on watchlist: {', '.join(sorted(missing))}")
    else:
        companies = conn.execute("SELECT ticker, cik FROM companies ORDER BY ticker").fetchall()

    if not companies:
        print("Watchlist is empty. Use 'watch TICKER' to add companies.")
        conn.close()
        return

    days = args.days
    full_mode = args.full

    if args.forms:
        forms_filter = set(f.strip() for f in args.forms.split(","))
    else:
        forms_filter = DEFAULT_FORMS

    print(f"=== Research Agent Update ===")
    print(f"  Companies: {len(companies)}")
    print(f"  Mode: {'full history' if full_mode else f'last {days} days'}")
    print(f"  Forms: {', '.join(sorted(forms_filter))}")
    print()

    total_filings = 0
    total_sections = 0
    total_changes = 0

    for idx, company in enumerate(companies, 1):
        ticker = company["ticker"]
        cik = company["cik"]
        print(f"[{idx}/{len(companies)}] {ticker} (CIK {cik})")

        try:
            if full_mode:
                # Use get_all_filings for full history
                got = get_all_filings(cik)
                if isinstance(got, tuple) and len(got) >= 5:
                    all_filings_meta = got[4]
                elif isinstance(got, tuple) and len(got) == 2:
                    all_filings_meta = got[1]
                else:
                    raise RuntimeError("Unexpected get_all_filings return shape")
                filings = []
                cik_trimmed = cik.lstrip("0")
                for f in all_filings_meta:
                    if f["form"] not in forms_filter:
                        continue
                    accession_flat = f["accession"].replace("-", "")
                    primary_doc = f.get("primaryDocument", "")
                    if not primary_doc:
                        continue
                    doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik_trimmed}/{accession_flat}/{primary_doc}"
                    filings.append({
                        "accession": f["accession"],
                        "form": f["form"],
                        "date": f["date"],
                        "doc_url": doc_url,
                    })
            else:
                cutoff = (datetime.now() - timedelta(days=days)).date()
                filings = get_recent_filings(cik, forms_filter, cutoff)

        except Exception as e:
            print(f"  ERROR fetching filings: {e}")
            continue

        if not filings:
            print(f"  No new filings")
            continue

        new_count = 0
        for filing in filings:
            accession = filing["accession"]

            # Skip if already in DB (dedup on accession)
            existing = conn.execute(
                "SELECT id FROM filings WHERE accession = ?", (accession,)
            ).fetchone()
            if existing:
                continue

            # Download filing
            form_safe = sanitize_form_name(filing["form"])
            acc_suffix = accession[-8:].replace("-", "")
            filename = f"{ticker}_{form_safe}_{filing['date']}_{acc_suffix}.txt"
            filepath = FILINGS_DIR / filename

            try:
                text = download_filing_text(filing["doc_url"])
                if len(text.strip()) < 100:
                    continue

                filepath.write_text(text, encoding="utf-8")
            except Exception as e:
                print(f"    ERROR downloading {filing['form']} {filing['date']}: {e}")
                continue

            # Insert filing record
            conn.execute(
                """INSERT INTO filings (ticker, form, date, accession, doc_url, path, downloaded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (ticker, filing["form"], filing["date"], accession,
                 filing["doc_url"], str(filepath), datetime.now().isoformat()),
            )
            conn.commit()
            filing_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            new_count += 1

            # Extract sections
            sections = extract_sections_from_text(text, filing["form"])
            section_count = 0

            for cli_name, content in sections:
                content_hash = compute_hash(content)
                conn.execute(
                    """INSERT INTO sections (filing_id, section_name, content, content_hash)
                       VALUES (?, ?, ?, ?)""",
                    (filing_id, cli_name, content, content_hash),
                )
                section_count += 1

                # Change detection: compare to previous filing of same form type
                prev = conn.execute(
                    """SELECT s.content, s.content_hash
                       FROM sections s
                       JOIN filings f ON s.filing_id = f.id
                       WHERE f.ticker = ? AND f.form = ? AND s.section_name = ?
                         AND f.id != ? AND f.date < ?
                       ORDER BY f.date DESC LIMIT 1""",
                    (ticker, filing["form"], cli_name, filing_id, filing["date"]),
                ).fetchone()

                if prev is None:
                    # New section (no previous filing of this form had it)
                    change_type = "added"
                    summary = f"First occurrence of {SECTION_MAP.get(cli_name, cli_name)} in {filing['form']}"
                elif prev["content_hash"] != content_hash:
                    change_type = "modified"
                    summary = generate_change_summary(prev["content"], content, cli_name)
                else:
                    change_type = None  # No change

                if change_type:
                    conn.execute(
                        """INSERT INTO changes (ticker, filing_id, section_name, change_type, summary, detected_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (ticker, filing_id, cli_name, change_type, summary,
                         datetime.now().isoformat()),
                    )
                    total_changes += 1

            conn.commit()
            total_sections += section_count
            print(f"    {filing['form']} {filing['date']}: {section_count} sections extracted")

            # Check for removed sections
            if not full_mode:
                _detect_removed_sections(conn, ticker, filing, filing_id)

        total_filings += new_count
        if new_count:
            print(f"  {new_count} new filing(s) processed")
        else:
            print(f"  All filings already in database")

    print()
    print(f"=== Update Complete ===")
    print(f"  New filings: {total_filings}")
    print(f"  Sections extracted: {total_sections}")
    print(f"  Changes detected: {total_changes}")
    conn.close()


def _detect_removed_sections(conn, ticker, filing, filing_id):
    """Detect sections that were in the previous filing but not in this one."""
    # Get sections from the previous filing of the same form
    prev_sections = conn.execute(
        """SELECT DISTINCT s.section_name
           FROM sections s
           JOIN filings f ON s.filing_id = f.id
           WHERE f.ticker = ? AND f.form = ? AND f.id != ? AND f.date < ?
           ORDER BY f.date DESC""",
        (ticker, filing["form"], filing_id, filing["date"]),
    ).fetchall()

    if not prev_sections:
        return

    # Get the most recent previous filing ID
    prev_filing = conn.execute(
        """SELECT id FROM filings
           WHERE ticker = ? AND form = ? AND id != ? AND date < ?
           ORDER BY date DESC LIMIT 1""",
        (ticker, filing["form"], filing_id, filing["date"]),
    ).fetchone()

    if not prev_filing:
        return

    prev_section_names = set()
    for row in conn.execute(
        "SELECT DISTINCT section_name FROM sections WHERE filing_id = ?",
        (prev_filing["id"],),
    ).fetchall():
        prev_section_names.add(row["section_name"])

    current_section_names = set()
    for row in conn.execute(
        "SELECT DISTINCT section_name FROM sections WHERE filing_id = ?",
        (filing_id,),
    ).fetchall():
        current_section_names.add(row["section_name"])

    removed = prev_section_names - current_section_names
    for section_name in removed:
        label = SECTION_MAP.get(section_name, section_name)
        conn.execute(
            """INSERT INTO changes (ticker, filing_id, section_name, change_type, summary, detected_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ticker, filing_id, section_name, "removed",
             f"{label} present in previous {filing['form']} but not found in this filing",
             datetime.now().isoformat()),
        )
    conn.commit()


def cmd_status(args):
    """Show database statistics."""
    conn = get_db()
    ensure_schema(conn)

    companies = conn.execute("SELECT COUNT(*) as cnt FROM companies").fetchone()["cnt"]
    filings = conn.execute("SELECT COUNT(*) as cnt FROM filings").fetchone()["cnt"]
    sections = conn.execute("SELECT COUNT(*) as cnt FROM sections").fetchone()["cnt"]
    changes_count = conn.execute("SELECT COUNT(*) as cnt FROM changes").fetchone()["cnt"]
    notes_count = conn.execute("SELECT COUNT(*) as cnt FROM notes").fetchone()["cnt"]
    thesis_count = conn.execute("SELECT COUNT(*) as cnt FROM theses").fetchone()["cnt"]
    valuation_count = conn.execute("SELECT COUNT(*) as cnt FROM valuation_snapshots").fetchone()["cnt"]
    mode_cfg = get_mode_config()

    db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    filings_size = sum(f.stat().st_size for f in FILINGS_DIR.glob("*.txt")) if FILINGS_DIR.exists() else 0

    print(f"=== Research Database Status ===")
    print(f"  Companies watched:  {companies}")
    print(f"  Filings stored:     {filings}")
    print(f"  Sections extracted:  {sections}")
    print(f"  Changes detected:   {changes_count}")
    print(f"  Notes:              {notes_count}")
    print(f"  Theses:             {thesis_count}")
    print(f"  Valuation snaps:    {valuation_count}")
    print(f"  Source mode:        {mode_cfg['mode']}")
    print(f"  Mode policy:        {mode_cfg['description']}")
    print()
    print(f"  Database size:      {db_size / 1024:.1f} KB")
    print(f"  Filings on disk:    {filings_size / (1024*1024):.1f} MB")
    print(f"  Database path:      {DB_PATH}")

    # Form breakdown
    form_counts = conn.execute(
        "SELECT form, COUNT(*) as cnt FROM filings GROUP BY form ORDER BY cnt DESC"
    ).fetchall()
    if form_counts:
        print(f"\n  Filings by form:")
        for row in form_counts:
            print(f"    {row['form']:<12} {row['cnt']:>5}")

    conn.close()


def cmd_mode(args):
    """Show or set active source policy mode."""
    if args.set_mode:
        payload = set_source_mode(args.set_mode)
        print(f"Source mode set to: {payload['mode']}")
        print(f"Policy: {payload['description']}")
        return

    cfg = get_mode_config()
    print(f"Source mode: {cfg['mode']}")
    print(f"Policy: {cfg['description']}")
    print(f"Available modes: {', '.join(sorted(SOURCE_MODES.keys()))}")


def _split_terms(raw: Optional[str]) -> List[str]:
    """Split comma/semicolon-separated terms into clean list."""
    if not raw:
        return []
    parts = re.split(r"[;,]", raw)
    return [p.strip() for p in parts if p.strip()]


def _is_watched(conn, ticker: str) -> bool:
    row = conn.execute("SELECT 1 FROM companies WHERE ticker = ?", (ticker,)).fetchone()
    return bool(row)


def _write_report(body: str, default_name: str) -> Path:
    """Write report to REPORT_PATH or reports/<default_name>."""
    report_path = os.getenv("REPORT_PATH")
    if report_path:
        out_path = Path(report_path)
    else:
        out_path = REPO_ROOT / "reports" / default_name
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text(body, encoding="utf-8")
    os.replace(tmp_path, out_path)
    return out_path


def cmd_thesis(args):
    """Thesis tracker for long-term investing discipline."""
    conn = get_db()
    action = args.action
    ticker = args.ticker.upper() if args.ticker else None

    if action == "list":
        rows = conn.execute(
            """SELECT t.ticker, c.name, t.horizon, t.fair_value_low, t.fair_value_high,
                      t.buy_below, t.trim_above, t.updated_at
               FROM theses t
               LEFT JOIN companies c ON c.ticker = t.ticker
               ORDER BY t.ticker"""
        ).fetchall()
        if not rows:
            print("No theses saved yet. Use: thesis set TICKER --summary \"...\"")
            conn.close()
            return
        print("Ticker  Name                              Horizon     FV Range        Buy<     Trim>    Updated")
        print("-" * 98)
        for r in rows:
            fv_low = "-" if r["fair_value_low"] is None else f"{r['fair_value_low']:.2f}"
            fv_high = "-" if r["fair_value_high"] is None else f"{r['fair_value_high']:.2f}"
            buy_below = "-" if r["buy_below"] is None else f"{r['buy_below']:.2f}"
            trim_above = "-" if r["trim_above"] is None else f"{r['trim_above']:.2f}"
            updated = (r["updated_at"] or "")[:10]
            name = (r["name"] or r["ticker"])[:32]
            horizon = (r["horizon"] or "-")[:10]
            print(f"{r['ticker']:<7} {name:<33} {horizon:<10} {fv_low}-{fv_high:<12} {buy_below:<8} {trim_above:<8} {updated}")
        conn.close()
        return

    if action == "check" and args.all:
        ticker = None
    elif not ticker:
        print(f"Action '{action}' requires TICKER.")
        conn.close()
        return

    if action == "set":
        if not args.summary:
            print("Missing --summary for thesis set.")
            conn.close()
            return
        if not _is_watched(conn, ticker):
            print(f"{ticker} is not on watchlist. Add first with: watch {ticker}")
            conn.close()
            return

        now = datetime.now().isoformat()
        existing = conn.execute("SELECT * FROM theses WHERE ticker = ?", (ticker,)).fetchone()
        kpis_json = json.dumps(args.kpi or [])
        if existing:
            conn.execute(
                """UPDATE theses SET
                       summary = ?,
                       horizon = COALESCE(?, horizon),
                       key_kpis = COALESCE(?, key_kpis),
                       buy_below = COALESCE(?, buy_below),
                       trim_above = COALESCE(?, trim_above),
                       fair_value_low = COALESCE(?, fair_value_low),
                       fair_value_high = COALESCE(?, fair_value_high),
                       invalidate_on = COALESCE(?, invalidate_on),
                       updated_at = ?
                   WHERE ticker = ?""",
                (
                    args.summary,
                    args.horizon,
                    kpis_json if args.kpi else None,
                    args.buy_below,
                    args.trim_above,
                    args.fv_low,
                    args.fv_high,
                    args.invalidate_on,
                    now,
                    ticker,
                ),
            )
            verb = "updated"
        else:
            conn.execute(
                """INSERT INTO theses (
                       ticker, summary, horizon, key_kpis, buy_below, trim_above,
                       fair_value_low, fair_value_high, invalidate_on, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ticker,
                    args.summary,
                    args.horizon,
                    kpis_json,
                    args.buy_below,
                    args.trim_above,
                    args.fv_low,
                    args.fv_high,
                    args.invalidate_on,
                    now,
                    now,
                ),
            )
            verb = "created"
        conn.commit()
        print(f"Thesis {verb} for {ticker}")
        conn.close()
        return

    if action == "check":
        targets = [ticker]
        if args.all:
            targets = [r["ticker"] for r in conn.execute("SELECT ticker FROM theses ORDER BY ticker").fetchall()]
        if not targets:
            print("No theses to check.")
            conn.close()
            return

        lines = [f"# Thesis Health Check — {datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
        for t in targets:
            thesis = conn.execute("SELECT * FROM theses WHERE ticker = ?", (t,)).fetchone()
            if not thesis:
                continue
            changes = conn.execute(
                """SELECT c.change_type, c.section_name, c.summary, c.detected_at, f.form, f.date
                   FROM changes c
                   JOIN filings f ON f.id = c.filing_id
                   WHERE c.ticker = ?
                   ORDER BY c.detected_at DESC
                   LIMIT ?""",
                (t, args.limit),
            ).fetchall()

            triggers = _split_terms(thesis["invalidate_on"])
            matched = []
            for ch in changes:
                hay = f"{ch['summary']} {ch['section_name']}".lower()
                for trig in triggers:
                    if trig.lower() in hay:
                        matched.append((trig, ch))
            status = "ON TRACK"
            if matched:
                status = "AT RISK"
            elif not changes:
                status = "NO RECENT CHANGES"

            lines.append(f"## {t} — {status}")
            lines.append(f"- Thesis: {thesis['summary']}")
            lines.append(f"- Invalidation rules: {thesis['invalidate_on'] or '-'}")
            if matched:
                lines.append("- Trigger matches:")
                for trig, ch in matched[:8]:
                    dt = (ch["detected_at"] or "")[:10]
                    lines.append(
                        f"  - `{trig}` matched `{ch['section_name']}` in {ch['form']} {ch['date']} ({dt})"
                    )
            else:
                lines.append("- Trigger matches: none")
            recent = changes[:3]
            if recent:
                lines.append("- Most recent filing changes:")
                for ch in recent:
                    lines.append(
                        f"  - {ch['form']} {ch['date']} | {ch['section_name']} | {ch['change_type']}"
                    )
            lines.append("")

        body = "\n".join(lines)
        print(body)
        out = _write_report(body, f"thesis_check_{datetime.now().strftime('%Y%m%d')}.md")
        print(f"\nSaved report to {out}")
        conn.close()
        return

    row = conn.execute(
        """SELECT t.*, c.name
           FROM theses t
           LEFT JOIN companies c ON c.ticker = t.ticker
           WHERE t.ticker = ?""",
        (ticker,),
    ).fetchone()
    if not row:
        print(f"No thesis found for {ticker}. Use: thesis set {ticker} --summary \"...\"")
        conn.close()
        return

    if action == "show":
        print(f"=== Thesis: {ticker} ({row['name'] or ticker}) ===")
        print(f"Summary: {row['summary']}")
        print(f"Horizon: {row['horizon'] or '-'}")
        try:
            kpis = json.loads(row["key_kpis"] or "[]")
        except Exception:
            kpis = []
        print(f"KPIs: {', '.join(kpis) if kpis else '-'}")
        print(f"Fair value band: {row['fair_value_low'] or '-'} to {row['fair_value_high'] or '-'}")
        print(f"Accumulate below: {row['buy_below'] or '-'}")
        print(f"Trim above: {row['trim_above'] or '-'}")
        print(f"Invalidation triggers: {row['invalidate_on'] or '-'}")
        print(f"Updated: {(row['updated_at'] or '')[:19].replace('T', ' ')}")
        conn.close()
        return

    print(f"Unknown thesis action: {action}")
    conn.close()


def cmd_quarterly(args):
    """Quarterly earnings analyzer based on filing deltas."""
    conn = get_db()
    if args.all:
        tickers = [r["ticker"] for r in conn.execute("SELECT ticker FROM companies ORDER BY ticker").fetchall()]
    elif args.ticker:
        tickers = [args.ticker.upper()]
    else:
        tickers = [r["ticker"] for r in conn.execute("SELECT ticker FROM companies ORDER BY ticker").fetchall()]

    if not tickers:
        print("No watchlist companies found.")
        conn.close()
        return

    now = datetime.now()
    cutoff = (now - timedelta(days=args.lookback_days)).date().isoformat()
    lines = [f"# Quarterly Analyst Review — {now.strftime('%Y-%m-%d')}", ""]
    section_focus = ("risk_factors", "mda", "debt_liquidity", "business", "buybacks")
    analyses = []

    for ticker in tickers:
        filings = conn.execute(
            """SELECT id, form, date
               FROM filings
               WHERE ticker = ? AND form IN ('10-Q', '10-K')
               ORDER BY date DESC
               LIMIT ?""",
            (ticker, args.lookback),
        ).fetchall()

        if not filings:
            analyses.append(
                {
                    "ticker": ticker,
                    "status": "missing",
                    "reason": "No 10-Q/10-K found in local database.",
                    "latest": None,
                    "prev": None,
                    "changes": [],
                    "score": 0,
                    "keywords": [],
                }
            )
            continue
        if len(filings) < 2:
            latest = filings[0]
            analyses.append(
                {
                    "ticker": ticker,
                    "status": "partial",
                    "reason": "Only one 10-Q/10-K available; need at least two for quarter-over-quarter comparison.",
                    "latest": latest,
                    "prev": None,
                    "changes": [],
                    "score": 0,
                    "keywords": [],
                }
            )
            continue

        latest = filings[0]
        prev = filings[1]
        change_rows = conn.execute(
            """SELECT section_name, change_type, summary
               FROM changes
               WHERE ticker = ? AND filing_id = ? AND section_name IN (?, ?, ?, ?, ?)
               ORDER BY id DESC""",
            (ticker, latest["id"], *section_focus),
        ).fetchall()

        score = 0
        key_hits = set()
        for ch in change_rows:
            if ch["change_type"] == "modified":
                score += 3
            else:
                score += 2
            s = (ch["summary"] or "").lower()
            for kw in NOTABLE_KEYWORDS:
                if kw in s:
                    key_hits.add(kw)
                    score += 1

        analyses.append(
            {
                "ticker": ticker,
                "status": "ok",
                "reason": "",
                "latest": latest,
                "prev": prev,
                "changes": change_rows,
                "score": score,
                "keywords": sorted(key_hits),
            }
        )

    ok_rows = [a for a in analyses if a["status"] == "ok"]
    partial_rows = [a for a in analyses if a["status"] == "partial"]
    missing_rows = [a for a in analyses if a["status"] == "missing"]

    lines.append("## Executive Summary")
    lines.append(f"- Universe analyzed: {len(analyses)} companies")
    lines.append(f"- Full quarter-over-quarter coverage: {len(ok_rows)}")
    lines.append(f"- Partial coverage (only one filing in DB): {len(partial_rows)}")
    lines.append(f"- Missing 10-Q/10-K coverage: {len(missing_rows)}")
    lines.append(f"- Fresh filing window for 'current cycle' checks: since {cutoff}")
    lines.append("")

    top_material = sorted(ok_rows, key=lambda x: x["score"], reverse=True)
    lines.append(f"## Top Material Filing Shifts (Top {min(args.top, len(top_material))})")
    if top_material:
        for row in top_material[: args.top]:
            latest = row["latest"]
            kw = ", ".join(row["keywords"][:5]) if row["keywords"] else "none"
            lines.append(
                f"- **{row['ticker']}** | {latest['form']} {latest['date']} | "
                f"score {row['score']} | changes {len(row['changes'])} | notable keywords: {kw}"
            )
    else:
        lines.append("- No material shifts found in tracked sections.")
    lines.append("")

    ticker_to_sector = {}
    for sector, leaders in INDUSTRY_LEADERS.items():
        for leader in leaders:
            ticker_to_sector[leader] = sector

    leader_records = []

    lines.append("## Industry Leader Coverage Matrix")
    lines.append("| Sector | Ticker | Status | Note |")
    lines.append("|---|---|---|---|")

    gaps = []
    for sector, leaders in INDUSTRY_LEADERS.items():
        for t in leaders:
            if not _is_watched(conn, t):
                status = "NOT ON WATCHLIST"
                note = "Add ticker to watchlist to include it in automated quarterly coverage."
                gaps.append((t, sector, note))
            else:
                latest_q = conn.execute(
                    """SELECT form, date
                       FROM filings
                       WHERE ticker = ? AND form IN ('10-Q', '10-K')
                       ORDER BY date DESC
                       LIMIT 1""",
                    (t,),
                ).fetchone()
                if not latest_q:
                    status = "MISSING IN DB"
                    note = "No local 10-Q/10-K found yet; run deeper history download or wait for filings."
                    gaps.append((t, sector, note))
                elif latest_q["date"] < cutoff:
                    status = "STALE"
                    note = (
                        f"Latest {latest_q['form']} is {latest_q['date']}; "
                        "no fresh quarterly filing in current window (may not have reported yet)."
                    )
                    gaps.append((t, sector, note))
                else:
                    status = "COVERED"
                    note = f"Latest {latest_q['form']} {latest_q['date']} is within current filing window."
                leader_records.append(
                    {
                        "sector": sector,
                        "ticker": t,
                        "status": status,
                        "note": note,
                        "latest_form": latest_q["form"] if latest_q else None,
                        "latest_date": latest_q["date"] if latest_q else None,
                    }
                )
            lines.append(f"| {sector} | {t} | {status} | {note} |")
    lines.append("")

    # Professional analyst-style summary sections.
    lines.append("## Analyst Executive View")
    keyword_counts: Dict[str, int] = {}
    for row in ok_rows:
        for kw in row["keywords"]:
            keyword_counts[kw] = keyword_counts.get(kw, 0) + 1
    top_keywords = sorted(keyword_counts.items(), key=lambda x: x[1], reverse=True)
    top_kw_text = ", ".join(f"{k} ({v})" for k, v in top_keywords[:6]) if top_keywords else "No elevated risk keywords in top material set."
    lines.append(f"- **Materiality concentration:** Top {min(args.top, len(top_material))} names are dominated by annual-report transitions and debt/liquidity section rewrites.")
    lines.append(f"- **Risk-language pulse:** {top_kw_text}")
    lines.append(f"- **Coverage quality:** {len(ok_rows)}/{len(analyses)} names have full quarter-over-quarter comparability.")
    lines.append(f"- **Known blind spots:** {len(gaps)} leader gaps across not-on-watchlist names and missing/stale filings.")
    lines.append("- **Portfolio action posture:** Prioritize names with score >= 15 and non-zero risk keywords for immediate deep review.")
    lines.append("")

    lines.append("## Sector Scorecard")
    lines.append("| Sector | Covered | Stale | Missing in DB | Not on Watchlist |")
    lines.append("|---|---:|---:|---:|---:|")
    for sector in INDUSTRY_LEADERS:
        rows = [r for r in leader_records if r["sector"] == sector]
        covered = sum(1 for r in rows if r["status"] == "COVERED")
        stale = sum(1 for r in rows if r["status"] == "STALE")
        missing_db = sum(1 for r in rows if r["status"] == "MISSING IN DB")
        not_watch = sum(1 for r in rows if r["status"] == "NOT ON WATCHLIST")
        lines.append(f"| {sector} | {covered} | {stale} | {missing_db} | {not_watch} |")
    lines.append("")

    lines.append("## Sector Leadership Snapshots")
    for sector in INDUSTRY_LEADERS:
        lines.append(f"### {sector}")
        sector_leaders = INDUSTRY_LEADERS[sector]
        candidates = [r for r in ok_rows if r["ticker"] in sector_leaders]
        candidates = sorted(candidates, key=lambda x: x["score"], reverse=True)
        if candidates:
            for r in candidates[:3]:
                latest = r["latest"]
                preview = "No tracked delta."
                if r["changes"]:
                    c = r["changes"][0]
                    s = (c["summary"] or "").strip().replace("\n", " ")
                    preview = f"{c['section_name']} {c['change_type']}: {s[:130]}{'...' if len(s) > 130 else ''}"
                kw = ", ".join(r["keywords"][:3]) if r["keywords"] else "none"
                lines.append(
                    f"- **{r['ticker']}** | {latest['form']} {latest['date']} | score {r['score']} | keywords: {kw} | {preview}"
                )
        else:
            # Explain why there is no leader snapshot.
            reasons = [x for x in gaps if x[1] == sector]
            if reasons:
                for t, _, note in reasons[:3]:
                    lines.append(f"- **{t}**: {note}")
            else:
                lines.append("- No leader qualified for this cycle (no material deltas in tracked sections).")
        lines.append("")

    lines.append("## Actionable Watchlist (Priority)")
    if top_material:
        for row in top_material[: args.top]:
            latest = row["latest"]
            preview = "no section deltas"
            if row["changes"]:
                ch = row["changes"][0]
                summary = (ch["summary"] or "").strip().replace("\n", " ")
                if len(summary) > 140:
                    summary = summary[:140] + "..."
                preview = f"{ch['section_name']} {ch['change_type']}: {summary}"
            lines.append(
                f"- **{row['ticker']}** ({latest['form']} {latest['date']}) score {row['score']}: {preview}"
            )
    else:
        lines.append("- No high-priority deltas detected.")
    lines.append("")

    if gaps:
        lines.append("## Coverage Gaps & Why We Don't Have Them Yet")
        for t, sector, note in gaps:
            lines.append(f"- **{t}** ({sector}): {note}")
        lines.append("")

    lines.append("## Detailed Company Deltas")
    for row in sorted(ok_rows, key=lambda x: x["ticker"]):
        latest = row["latest"]
        prev = row["prev"]
        lines.append(f"### {row['ticker']}")
        lines.append(f"- Latest filing: {latest['form']} on {latest['date']}")
        lines.append(f"- Previous filing: {prev['form']} on {prev['date']}")
        lines.append(f"- Materiality score: {row['score']}")
        lines.append(f"- Focus changes detected: {len(row['changes'])}")
        if row["changes"]:
            lines.append("- Key deltas:")
            for ch in row["changes"][:8]:
                summary = (ch["summary"] or "").strip().replace("\n", " ")
                if len(summary) > 180:
                    summary = summary[:180] + "..."
                lines.append(f"  - {ch['section_name']} ({ch['change_type']}): {summary}")
        else:
            lines.append("- Key deltas: none detected in tracked sections.")
        lines.append("")

    if partial_rows:
        lines.append("## Partial Coverage (Need More Filing History)")
        for row in sorted(partial_rows, key=lambda x: x["ticker"]):
            latest = row["latest"]
            lines.append(
                f"- {row['ticker']}: {row['reason']} Latest available filing: {latest['form']} {latest['date']}."
            )
        lines.append("")

    if missing_rows:
        lines.append("## Missing Coverage (No 10-Q/10-K in DB)")
        for row in sorted(missing_rows, key=lambda x: x["ticker"]):
            lines.append(f"- {row['ticker']}: {row['reason']}")
        lines.append("")

    body = "\n".join(lines)
    print(body)
    out = _write_report(body, f"quarterly_longterm_{datetime.now().strftime('%Y%m%d')}.md")
    print(f"\nSaved report to {out}")

    # Build a concise brief for quick reading.
    brief_lines = [
        f"# Quarterly Brief — {now.strftime('%Y-%m-%d')}",
        "",
        "## Quick Coverage Snapshot",
        f"- Universe analyzed: {len(analyses)}",
        f"- Full coverage: {len(ok_rows)}",
        f"- Partial: {len(partial_rows)}",
        f"- Missing: {len(missing_rows)}",
        "",
        "## Top 15 Must-Read This Quarter",
    ]
    if top_material:
        for row in top_material[:15]:
            latest = row["latest"]
            ch_preview = "no tracked deltas"
            if row["changes"]:
                ch = row["changes"][0]
                ch_preview = f"{ch['section_name']} {ch['change_type']}"
            kws = ", ".join(row["keywords"][:4]) if row["keywords"] else "none"
            brief_lines.append(
                f"- **{row['ticker']}** | {latest['form']} {latest['date']} | score {row['score']} | {ch_preview} | keywords: {kws}"
            )
    else:
        brief_lines.append("- No high-materiality names detected.")

    if gaps:
        brief_lines.extend(
            [
                "",
                "## Important Coverage Gaps (Why Missing)",
            ]
        )
        for t, sector, note in gaps[:20]:
            brief_lines.append(f"- **{t}** ({sector}): {note}")

    brief_body = "\n".join(brief_lines)
    if "REPORT_PATH" in os.environ and os.environ["REPORT_PATH"]:
        rp = Path(os.environ["REPORT_PATH"])
        brief_out = rp.with_name(rp.stem + "_brief.md")
    else:
        brief_out = REPO_ROOT / "reports" / f"quarterly_report_brief_{now.strftime('%Y%m%d')}.md"
    brief_out.parent.mkdir(parents=True, exist_ok=True)
    brief_out.write_text(brief_body, encoding="utf-8")
    print(f"Saved brief to {brief_out}")
    conn.close()


def _fetch_latest_prices(tickers: List[str]) -> Dict[str, float]:
    """Fetch latest prices with yfinance."""
    import yfinance as yf

    if not tickers:
        return {}
    data = yf.download(tickers, period="5d", progress=False, threads=True)
    prices: Dict[str, float] = {}
    for t in tickers:
        try:
            if len(tickers) == 1:
                close_col = data["Close"].dropna()
            else:
                close_col = data["Close"][t].dropna()
            if close_col.empty:
                continue
            prices[t] = float(close_col.iloc[-1])
        except Exception:
            continue
    return prices


def cmd_valuation(args):
    """Valuation monitor versus thesis-defined bands."""
    conn = get_db()
    mode = get_mode_config()["mode"]
    as_of = args.as_of or datetime.now().strftime("%Y-%m-%d")

    if args.all:
        thesis_rows = conn.execute("SELECT * FROM theses ORDER BY ticker").fetchall()
    elif args.ticker:
        thesis_rows = conn.execute("SELECT * FROM theses WHERE ticker = ?", (args.ticker.upper(),)).fetchall()
    else:
        thesis_rows = conn.execute("SELECT * FROM theses ORDER BY ticker").fetchall()

    if not thesis_rows:
        print("No thesis rows found. Create with: thesis set TICKER --summary ...")
        conn.close()
        return

    tickers = [r["ticker"] for r in thesis_rows]
    prices: Dict[str, float] = {}
    using_cache = False

    if args.price is not None:
        if len(tickers) != 1:
            print("--price can only be used with a single ticker.")
            conn.close()
            return
        prices[tickers[0]] = float(args.price)
    elif mode == "strict" and not args.allow_broader_sources:
        using_cache = True
        for t in tickers:
            row = conn.execute(
                """SELECT price FROM valuation_snapshots
                   WHERE ticker = ?
                   ORDER BY as_of DESC, id DESC LIMIT 1""",
                (t,),
            ).fetchone()
            if row:
                prices[t] = float(row["price"])
    else:
        prices = _fetch_latest_prices(tickers)

    lines = [
        f"# Valuation Monitor — {as_of}",
        f"Mode: {mode}",
        "",
        "| Ticker | Price | Fair Value Band | Status | Upside to Mid |",
        "|---|---:|---:|---|---:|",
    ]

    for r in thesis_rows:
        t = r["ticker"]
        if t not in prices:
            lines.append(f"| {t} | N/A | N/A | NO PRICE | N/A |")
            continue

        price = float(prices[t])
        fv_low = r["fair_value_low"]
        fv_high = r["fair_value_high"]
        buy_below = r["buy_below"]
        trim_above = r["trim_above"]

        status = "HOLD"
        if buy_below is not None and price <= float(buy_below):
            status = "ACCUMULATE"
        elif fv_low is not None and price <= float(fv_low):
            status = "ACCUMULATE"
        elif trim_above is not None and price >= float(trim_above):
            status = "TRIM"
        elif fv_high is not None and price >= float(fv_high):
            status = "TRIM"

        upside = None
        if fv_low is not None and fv_high is not None and price > 0:
            mid = (float(fv_low) + float(fv_high)) / 2.0
            upside = ((mid - price) / price) * 100.0

        fv_band = "N/A"
        if fv_low is not None or fv_high is not None:
            fv_band = f"{'-' if fv_low is None else f'{float(fv_low):.2f}'} - {'-' if fv_high is None else f'{float(fv_high):.2f}'}"

        lines.append(
            f"| {t} | {price:.2f} | {fv_band} | {status} | {'N/A' if upside is None else f'{upside:+.1f}%'} |"
        )

        conn.execute(
            """INSERT INTO valuation_snapshots (
                   ticker, as_of, price, fair_value_low, fair_value_high, status, upside_to_mid_pct
               ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (t, as_of, price, fv_low, fv_high, status, upside),
        )

    conn.commit()
    if using_cache:
        lines.append("")
        lines.append("_Strict mode note: used last stored prices from valuation snapshots._")

    body = "\n".join(lines)
    print(body)
    out = _write_report(body, f"valuation_monitor_{datetime.now().strftime('%Y%m%d')}.md")
    print(f"\nSaved report to {out}")
    conn.close()


def cmd_journal(args):
    """Structured notes for portfolio/watchlist."""
    conn = get_db()
    action = args.action

    if action == "add":
        scope = args.scope or "watchlist"
        ticker = args.ticker.upper() if args.ticker else None
        sentiment = (args.sentiment or "neutral").lower()
        if not args.note:
            print("Missing --note for journal add.")
            conn.close()
            return
        if sentiment not in {"like", "dislike", "watch", "change", "neutral"}:
            print("Invalid sentiment. Use: like, dislike, watch, change, neutral")
            conn.close()
            return
        if scope == "watchlist" and not ticker:
            print("For watchlist scope, provide --ticker TICKER")
            conn.close()
            return
        if ticker and not _is_watched(conn, ticker):
            print(f"{ticker} is not on watchlist. Add it first with: watch {ticker}")
            conn.close()
            return
        tags = ",".join(args.tag or [])
        conn.execute(
            """INSERT INTO investor_notes (scope, ticker, sentiment, note, tags, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (scope, ticker, sentiment, args.note, tags, datetime.now().isoformat()),
        )
        conn.commit()
        print(f"Note added [{scope}] {ticker or 'PORTFOLIO'} ({sentiment})")
        conn.close()
        return

    if action == "list":
        q = """SELECT id, scope, ticker, sentiment, note, tags, created_at
               FROM investor_notes
               WHERE 1=1"""
        params: List[object] = []
        if args.scope:
            q += " AND scope = ?"
            params.append(args.scope)
        if args.ticker:
            q += " AND ticker = ?"
            params.append(args.ticker.upper())
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(args.limit)
        rows = conn.execute(q, params).fetchall()
        if not rows:
            print("No notes found.")
            conn.close()
            return
        print(f"=== Investor Notes (last {len(rows)}) ===\n")
        for r in rows:
            dt = (r["created_at"] or "")[:16].replace("T", " ")
            target = r["ticker"] or "PORTFOLIO"
            tags = f" | tags: {r['tags']}" if r["tags"] else ""
            print(f"#{r['id']} [{r['scope']}] {target} [{r['sentiment']}] {dt}{tags}")
            print(f"  {r['note']}")
            print("")
        conn.close()
        return

    print(f"Unknown journal action: {action}")
    conn.close()


def cmd_decision(args):
    """Decision layer: store and review buy/sell/hold decision memos."""
    conn = get_db()
    action = args.action

    if action == "add":
        if not args.thesis:
            print("Missing --thesis for decision add.")
            conn.close()
            return
        if not args.ticker:
            print("Decision add requires ticker.")
            conn.close()
            return
        ticker = args.ticker.upper()
        if not _is_watched(conn, ticker):
            print(f"{ticker} is not on watchlist. Add first with: watch {ticker}")
            conn.close()
            return
        conn.execute(
            """INSERT INTO decisions (
                   ticker, action, status, thesis, valuation_view, key_risks,
                   trigger_condition, expected_return_3y, kill_criteria, created_at
               ) VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)""",
            (
                ticker,
                args.trade_action,
                args.thesis,
                args.valuation_view,
                args.key_risks,
                args.trigger_condition,
                args.expected_return_3y,
                args.kill_criteria,
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
        did = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        print(f"Decision #{did} added for {ticker} ({args.trade_action})")
        conn.close()
        return

    if action == "list":
        q = """SELECT id, ticker, action, status, thesis, expected_return_3y, created_at, reviewed_at
               FROM decisions WHERE 1=1"""
        params: List[object] = []
        if args.ticker:
            q += " AND ticker = ?"
            params.append(args.ticker.upper())
        if args.status:
            q += " AND status = ?"
            params.append(args.status)
        q += " ORDER BY created_at DESC LIMIT ?"
        params.append(args.limit)
        rows = conn.execute(q, params).fetchall()
        if not rows:
            print("No decisions found.")
            conn.close()
            return
        print("ID   Ticker Action Status  ExpRet3Y  Created")
        print("-" * 64)
        for r in rows:
            er = "-" if r["expected_return_3y"] is None else f"{float(r['expected_return_3y']):.1f}%"
            created = (r["created_at"] or "")[:10]
            print(f"{r['id']:<4} {r['ticker']:<6} {r['action']:<6} {r['status']:<7} {er:<8} {created}")
            t = (r["thesis"] or "").strip().replace("\n", " ")
            if len(t) > 110:
                t = t[:110] + "..."
            print(f"     {t}")
        conn.close()
        return

    if action == "show":
        if not args.id:
            print("Decision show requires --id N")
            conn.close()
            return
        row = conn.execute("SELECT * FROM decisions WHERE id = ?", (args.id,)).fetchone()
        if not row:
            print(f"Decision #{args.id} not found.")
            conn.close()
            return
        print(f"=== Decision #{row['id']} ===")
        print(f"Ticker: {row['ticker']}")
        print(f"Action: {row['action']}")
        print(f"Status: {row['status']}")
        print(f"Created: {(row['created_at'] or '')[:19].replace('T', ' ')}")
        print(f"Reviewed: {(row['reviewed_at'] or '')[:19].replace('T', ' ') if row['reviewed_at'] else '-'}")
        print(f"Expected 3Y return: {row['expected_return_3y'] if row['expected_return_3y'] is not None else '-'}")
        print(f"\nThesis:\n{row['thesis']}")
        print(f"\nValuation view:\n{row['valuation_view'] or '-'}")
        print(f"\nKey risks:\n{row['key_risks'] or '-'}")
        print(f"\nTrigger condition:\n{row['trigger_condition'] or '-'}")
        print(f"\nKill criteria:\n{row['kill_criteria'] or '-'}")
        print(f"\nOutcome note:\n{row['outcome_note'] or '-'}")
        conn.close()
        return

    if action == "close":
        if not args.id:
            print("Decision close requires --id N")
            conn.close()
            return
        row = conn.execute("SELECT id FROM decisions WHERE id = ?", (args.id,)).fetchone()
        if not row:
            print(f"Decision #{args.id} not found.")
            conn.close()
            return
        conn.execute(
            """UPDATE decisions
               SET status = 'closed',
                   reviewed_at = ?,
                   outcome_note = COALESCE(?, outcome_note)
               WHERE id = ?""",
            (datetime.now().isoformat(), args.outcome_note, args.id),
        )
        conn.commit()
        print(f"Decision #{args.id} closed.")
        conn.close()
        return

    print(f"Unknown decision action: {action}")
    conn.close()


def cmd_changes(args):
    """Show flagged section changes."""
    conn = get_db()
    ticker = args.ticker.upper() if args.ticker else None

    if ticker:
        rows = conn.execute(
            """SELECT c.*, f.form, f.date as filing_date
               FROM changes c
               JOIN filings f ON c.filing_id = f.id
               WHERE c.ticker = ?
               ORDER BY c.detected_at DESC""",
            (ticker,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT c.*, f.form, f.date as filing_date
               FROM changes c
               JOIN filings f ON c.filing_id = f.id
               ORDER BY c.detected_at DESC
               LIMIT 50""",
        ).fetchall()

    if not rows:
        scope = f"for {ticker}" if ticker else ""
        print(f"No changes detected {scope}. Run 'update' to scan for changes.")
        return

    header = f"Changes for {ticker}" if ticker else "Recent Changes (last 50)"
    print(f"=== {header} ===\n")

    for row in rows:
        label = SECTION_MAP.get(row["section_name"], row["section_name"])
        type_icon = {"added": "+", "modified": "~", "removed": "-"}.get(row["change_type"], "?")
        print(f"  [{type_icon}] {row['ticker']} | {row['form']} {row['filing_date']} | {label}")
        print(f"      {row['change_type'].upper()}: {row['summary']}")
        print()

    print(f"{len(rows)} change(s)")
    conn.close()


def cmd_section(args):
    """Print a specific section from the latest filing."""
    conn = get_db()
    ticker = args.ticker.upper()
    section_name = args.section.lower()

    if section_name not in SECTION_MAP:
        print(f"Unknown section: {section_name}")
        print(f"Available sections: {', '.join(sorted(SECTION_MAP.keys()))}")
        return

    row = conn.execute(
        """SELECT s.content, f.form, f.date, f.ticker
           FROM sections s
           JOIN filings f ON s.filing_id = f.id
           WHERE f.ticker = ? AND s.section_name = ?
           ORDER BY f.date DESC LIMIT 1""",
        (ticker, section_name),
    ).fetchone()

    if not row:
        print(f"No {section_name} section found for {ticker}")
        print(f"Run 'update' to download and extract filings.")
        return

    label = SECTION_MAP[section_name]
    print(f"=== {ticker} — {label} ===")
    print(f"  Form: {row['form']}  |  Date: {row['date']}")
    print(f"  Length: {len(row['content']):,} chars")
    print(f"{'=' * 60}\n")
    print(row["content"])
    conn.close()


def cmd_ask(args):
    """Send context and question to AI with Claude->OpenAI fallback."""
    conn = get_db()
    question = args.question

    # Gather context
    context_parts = []

    # Watchlist
    companies = conn.execute("SELECT ticker, name FROM companies ORDER BY ticker").fetchall()
    if companies:
        watchlist = ", ".join(f"{c['ticker']} ({c['name']})" for c in companies)
        context_parts.append(f"WATCHLIST: {watchlist}")

    # Recent changes
    changes_rows = conn.execute(
        """SELECT c.ticker, c.section_name, c.change_type, c.summary,
                  f.form, f.date as filing_date
           FROM changes c
           JOIN filings f ON c.filing_id = f.id
           ORDER BY c.detected_at DESC LIMIT 20"""
    ).fetchall()
    if changes_rows:
        context_parts.append("\nRECENT CHANGES:")
        for row in changes_rows:
            label = SECTION_MAP.get(row["section_name"], row["section_name"])
            context_parts.append(
                f"  {row['ticker']} {row['form']} {row['filing_date']}: "
                f"{row['change_type']} in {label} — {row['summary']}"
            )

    # Latest sections (truncated) for each company
    for company in companies:
        ticker = company["ticker"]
        sections = conn.execute(
            """SELECT s.section_name, s.content, f.form, f.date
               FROM sections s
               JOIN filings f ON s.filing_id = f.id
               WHERE f.ticker = ?
               ORDER BY f.date DESC""",
            (ticker,),
        ).fetchall()

        if sections:
            context_parts.append(f"\n--- {ticker} LATEST SECTIONS ---")
            seen = set()
            for s in sections:
                if s["section_name"] in seen:
                    continue
                seen.add(s["section_name"])
                label = SECTION_MAP.get(s["section_name"], s["section_name"])
                # Truncate each section to keep total context manageable
                content = s["content"][:4000]
                context_parts.append(f"\n[{label} — {s['form']} {s['date']}]")
                context_parts.append(content)

    # Notes
    notes_rows = conn.execute(
        "SELECT ticker, category, content, created_at FROM notes ORDER BY created_at DESC LIMIT 20"
    ).fetchall()
    if notes_rows:
        context_parts.append("\nRESEARCH NOTES:")
        for n in notes_rows:
            context_parts.append(f"  [{n['ticker']}] ({n['category']}) {n['content']}")

    conn.close()

    # Build prompt, cap at ~80K chars
    context = "\n".join(context_parts)
    if len(context) > 80000:
        context = context[:80000] + "\n\n[... context truncated at 80K chars ...]"

    prompt = (
        f"You are a research assistant analyzing SEC filings for my investment watchlist.\n\n"
        f"CONTEXT FROM MY RESEARCH DATABASE:\n{context}\n\n"
        f"QUESTION: {question}\n\n"
        f"Answer based on the filing data above. Be specific, cite form types and dates. "
        f"If the data doesn't contain enough information to answer, say so."
    )

    print(f"Sending to AI ({len(prompt):,} chars of context)...", file=sys.stderr)
    try:
        answer = ask_ai_complete(
            prompt,
            "You are a research assistant analyzing SEC filings for an investment watchlist.",
            max_continuations=2,
        )
        answer = (answer or "").strip()
        if not answer:
            raise RuntimeError("AI returned empty output.")

        print(answer)
        report_path = os.getenv("REPORT_PATH")
        if report_path:
            os.makedirs(os.path.dirname(report_path), exist_ok=True)
            # Write atomically so failed runs never leave partial/empty files.
            tmp_path = report_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(answer)
            os.replace(tmp_path, report_path)
    except Exception as e:
        report_path = os.getenv("REPORT_PATH")
        if report_path:
            error_path = report_path + ".error.log"
            try:
                with open(error_path, "a", encoding="utf-8") as ef:
                    ef.write(f"{datetime.now().isoformat()} ask failure: {e}\n")
            except Exception:
                pass
        print(f"Error from AI connector: {e}", file=sys.stderr)
        raise SystemExit(1)


def cmd_note(args):
    """Add a research note."""
    conn = get_db()
    ticker = args.ticker.upper()
    content = args.content
    category = args.category or "general"

    # Verify ticker is on watchlist
    existing = conn.execute(
        "SELECT ticker FROM companies WHERE ticker = ?", (ticker,)
    ).fetchone()
    if not existing:
        print(f"{ticker} is not on your watchlist. Add it with 'watch {ticker}' first.")
        conn.close()
        return

    conn.execute(
        "INSERT INTO notes (ticker, category, content, created_at) VALUES (?, ?, ?, ?)",
        (ticker, category, content, datetime.now().isoformat()),
    )
    conn.commit()
    print(f"Note added for {ticker} [{category}]")
    conn.close()


def cmd_notes(args):
    """View notes for a ticker."""
    conn = get_db()
    ticker = args.ticker.upper()

    rows = conn.execute(
        "SELECT id, category, content, created_at FROM notes WHERE ticker = ? ORDER BY created_at DESC",
        (ticker,),
    ).fetchall()

    if not rows:
        print(f"No notes for {ticker}")
        return

    print(f"=== Notes for {ticker} ===\n")
    for row in rows:
        dt = row["created_at"][:16].replace("T", " ") if row["created_at"] else "?"
        print(f"  #{row['id']}  [{row['category']}]  {dt}")
        print(f"    {row['content']}")
        print()

    print(f"{len(rows)} note(s)")
    conn.close()


def cmd_export(args):
    """Export all data for a ticker as JSON."""
    conn = get_db()
    ticker = args.ticker.upper()

    company = conn.execute(
        "SELECT * FROM companies WHERE ticker = ?", (ticker,)
    ).fetchone()
    if not company:
        print(f"{ticker} is not on your watchlist.")
        conn.close()
        return

    export = {
        "ticker": ticker,
        "company": dict(company),
        "exported_at": datetime.now().isoformat(),
        "filings": [],
        "changes": [],
        "notes": [],
    }

    # Filings with sections
    filings = conn.execute(
        "SELECT * FROM filings WHERE ticker = ? ORDER BY date DESC", (ticker,)
    ).fetchall()
    for f in filings:
        filing_dict = dict(f)
        sections = conn.execute(
            "SELECT section_name, content_hash FROM sections WHERE filing_id = ?",
            (f["id"],),
        ).fetchall()
        filing_dict["sections"] = [dict(s) for s in sections]
        export["filings"].append(filing_dict)

    # Changes
    changes_rows = conn.execute(
        """SELECT c.*, f.form, f.date as filing_date
           FROM changes c
           JOIN filings f ON c.filing_id = f.id
           WHERE c.ticker = ?
           ORDER BY c.detected_at DESC""",
        (ticker,),
    ).fetchall()
    export["changes"] = [dict(r) for r in changes_rows]

    # Notes
    notes_rows = conn.execute(
        "SELECT * FROM notes WHERE ticker = ? ORDER BY created_at DESC", (ticker,)
    ).fetchall()
    export["notes"] = [dict(r) for r in notes_rows]

    # Write export file
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    export_path = EXPORTS_DIR / f"{ticker}_{timestamp}.json"
    with open(export_path, "w", encoding="utf-8") as f:
        json.dump(export, f, indent=2, default=str)

    filing_count = len(export["filings"])
    change_count = len(export["changes"])
    note_count = len(export["notes"])
    print(f"Exported {ticker}: {filing_count} filings, {change_count} changes, {note_count} notes")
    print(f"  -> {export_path}")

    conn.close()


def cmd_schedule(args):
    """Print or install a daily cron job for auto-updating."""
    script_path = Path(__file__).resolve()
    python_path = sys.executable
    log_path = LOG_DIR / "cron.log"

    cron_line = f"0 7 * * 1-5 {python_path} {script_path} update --days 3 >> {log_path} 2>&1"

    print(f"=== Daily Update Schedule ===\n")
    print(f"Add this line to your crontab (crontab -e):\n")
    print(f"  {cron_line}")
    print(f"\nThis runs Monday-Friday at 7:00 AM, checking the last 3 days of filings.")
    print(f"Logs go to: {log_path}")

    if args.install:
        try:
            # Get current crontab
            result = subprocess.run(
                ["crontab", "-l"], capture_output=True, text=True
            )
            current = result.stdout if result.returncode == 0 else ""

            # Check if already installed
            if str(script_path) in current:
                print(f"\nCron job already installed.")
                return

            # Add new line
            new_crontab = current.rstrip() + "\n" + cron_line + "\n"
            install = subprocess.run(
                ["crontab", "-"], input=new_crontab, capture_output=True, text=True
            )
            if install.returncode == 0:
                print(f"\nCron job installed successfully.")
            else:
                print(f"\nError installing cron: {install.stderr}")
        except Exception as e:
            print(f"\nError: {e}")


# ---------------------------------------------------------------------------
# CLI Parser
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="research_agent.py",
        description="SEC filing research assistant with watchlist, change detection, and Claude integration",
    )
    sub = parser.add_subparsers(dest="command")

    # init
    sub.add_parser("init", help="Create directories and initialize database")

    # watch
    p = sub.add_parser("watch", help="Add tickers to watchlist")
    p.add_argument("tickers", nargs="+", help="Ticker symbols to watch")

    # unwatch
    p = sub.add_parser("unwatch", help="Remove tickers from watchlist")
    p.add_argument("tickers", nargs="+", help="Ticker symbols to remove")

    # list
    sub.add_parser("list", help="Show watchlist with filing counts")

    # update
    p = sub.add_parser("update", help="Download new filings and detect changes")
    p.add_argument("--ticker", "-t", help="Comma-separated tickers to update (default: all)")
    p.add_argument("--days", type=int, default=30, help="Look back N days (default: 30)")
    p.add_argument("--forms", help="Comma-separated form types (default: all tracked forms)")
    p.add_argument("--full", action="store_true", help="Download full filing history")

    # status
    sub.add_parser("status", help="Show database statistics")

    # mode
    p = sub.add_parser("mode", help="Show or set source reliability mode")
    p.add_argument("set_mode", nargs="?", choices=sorted(SOURCE_MODES.keys()))

    # changes
    p = sub.add_parser("changes", help="Show flagged section changes")
    p.add_argument("ticker", nargs="?", help="Filter by ticker (optional)")

    # section
    p = sub.add_parser("section", help="Print specific section from latest filing")
    p.add_argument("ticker", help="Ticker symbol")
    p.add_argument("section", help=f"Section name: {', '.join(sorted(SECTION_MAP.keys()))}")

    # ask
    p = sub.add_parser("ask", help="Ask Claude a question with filing context")
    p.add_argument("question", help="Your research question")

    # note
    p = sub.add_parser("note", help="Add a research note")
    p.add_argument("ticker", help="Ticker symbol")
    p.add_argument("content", help="Note text")
    p.add_argument("--category", "-c", default="general", help="Category (default: general)")

    # notes
    p = sub.add_parser("notes", help="View notes for a ticker")
    p.add_argument("ticker", help="Ticker symbol")

    # export
    p = sub.add_parser("export", help="Export all data for a ticker as JSON")
    p.add_argument("ticker", help="Ticker symbol")

    # schedule
    p = sub.add_parser("schedule", help="Print/install daily cron job")
    p.add_argument("--install", action="store_true", help="Install the cron job")

    # thesis
    p = sub.add_parser("thesis", help="Manage long-term investment theses")
    p.add_argument("action", choices=["set", "show", "check", "list"], help="Thesis action")
    p.add_argument("ticker", nargs="?", help="Ticker symbol")
    p.add_argument("--all", action="store_true", help="Apply action to all tickers with theses")
    p.add_argument("--summary", help="Thesis summary (required for set)")
    p.add_argument("--horizon", help="Investment horizon (e.g., 3-5y)")
    p.add_argument("--kpi", action="append", help="Key KPI to track; repeat for multiple")
    p.add_argument("--buy-below", dest="buy_below", type=float, help="Accumulation threshold")
    p.add_argument("--trim-above", dest="trim_above", type=float, help="Trim threshold")
    p.add_argument("--fv-low", dest="fv_low", type=float, help="Fair value band low")
    p.add_argument("--fv-high", dest="fv_high", type=float, help="Fair value band high")
    p.add_argument("--invalidate-on", help="Comma/semicolon-separated invalidation triggers")
    p.add_argument("--limit", type=int, default=30, help="Recent change rows to scan for check")

    # quarterly
    p = sub.add_parser("quarterly", help="Analyze latest quarterly/annual filing deltas")
    p.add_argument("ticker", nargs="?", help="Ticker symbol (optional)")
    p.add_argument("--all", action="store_true", help="Analyze all watchlist tickers")
    p.add_argument("--lookback", type=int, default=4, help="How many recent 10-Q/10-K filings to inspect")
    p.add_argument("--lookback-days", type=int, default=140, help="Fresh filing window in days for coverage checks")
    p.add_argument("--top", type=int, default=25, help="Top material names to surface in summary/action list")

    # valuation
    p = sub.add_parser("valuation", help="Check prices versus thesis valuation bands")
    p.add_argument("ticker", nargs="?", help="Ticker symbol (optional)")
    p.add_argument("--all", action="store_true", help="Run for all thesis tickers")
    p.add_argument("--price", type=float, help="Manual price (single ticker only)")
    p.add_argument("--as-of", dest="as_of", help="Valuation date label (YYYY-MM-DD)")
    p.add_argument(
        "--allow-broader-sources",
        action="store_true",
        help="Allow market vendor pricing in strict mode",
    )

    # journal
    p = sub.add_parser("journal", help="Portfolio/watchlist structured notes")
    p.add_argument("action", choices=["add", "list"], help="Journal action")
    p.add_argument("--scope", choices=["portfolio", "watchlist"])
    p.add_argument("--ticker", help="Ticker symbol (required for watchlist scope)")
    p.add_argument("--sentiment", default="neutral", help="like|dislike|watch|change|neutral")
    p.add_argument("--note", help="Note text")
    p.add_argument("--tag", action="append", help="Tag (repeatable)")
    p.add_argument("--limit", type=int, default=30, help="Rows to list")

    # decision
    p = sub.add_parser("decision", help="Decision memo layer (buy/sell/hold)")
    p.add_argument("action", choices=["add", "list", "show", "close"], help="Decision action")
    p.add_argument("ticker", nargs="?", help="Ticker symbol for add/list filtering")
    p.add_argument("--trade-action", choices=["buy", "sell", "hold"], default="hold")
    p.add_argument("--thesis", help="Decision thesis (required for add)")
    p.add_argument("--valuation-view", dest="valuation_view", help="Valuation stance/assumptions")
    p.add_argument("--key-risks", dest="key_risks", help="Key downside risks")
    p.add_argument("--trigger-condition", dest="trigger_condition", help="What would trigger action")
    p.add_argument("--expected-return-3y", dest="expected_return_3y", type=float, help="Expected 3Y return %%")
    p.add_argument("--kill-criteria", dest="kill_criteria", help="Conditions to exit thesis")
    p.add_argument("--status", choices=["open", "closed"], help="List filter")
    p.add_argument("--id", type=int, help="Decision ID for show/close")
    p.add_argument("--outcome-note", dest="outcome_note", help="Outcome note when closing")
    p.add_argument("--limit", type=int, default=30, help="Rows to list")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Ensure init has been run for commands that need the DB
    if args.command not in ("init", "mode") and not DB_PATH.exists():
        print(f"Database not found. Run 'python3 research_agent.py init' first.")
        return

    commands = {
        "init": cmd_init,
        "watch": cmd_watch,
        "unwatch": cmd_unwatch,
        "list": cmd_list,
        "update": cmd_update,
        "status": cmd_status,
        "mode": cmd_mode,
        "changes": cmd_changes,
        "section": cmd_section,
        "ask": cmd_ask,
        "note": cmd_note,
        "notes": cmd_notes,
        "export": cmd_export,
        "schedule": cmd_schedule,
        "thesis": cmd_thesis,
        "quarterly": cmd_quarterly,
        "valuation": cmd_valuation,
        "journal": cmd_journal,
        "decision": cmd_decision,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
