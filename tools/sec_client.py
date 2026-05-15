#!/usr/bin/env python3
"""
sec_client.py — Shared SEC EDGAR utilities.

Provides rate-limited HTTP access, HTML-to-text conversion,
ticker-CIK mapping, and filing metadata retrieval.

Used by: securities_monitor.py, download_history.py, coverage_check.py,
         research_agent.py
"""

import json
import os
import re
import threading
import time
from html.parser import HTMLParser
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / ".cache"

SEC_USER_AGENT = os.environ.get(
    "SEC_USER_AGENT",
    "Investment_Tools_Bot contact@example.com"
)

HEADERS = {
    "User-Agent": SEC_USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
}

DEFAULT_FORMS = {"10-K", "10-Q", "8-K", "20-F", "6-K", "DEF 14A", "4", "SC 13D", "SC 13G"}

# ---------------------------------------------------------------------------
# Rate limiter (thread-safe, ~8 req/sec to stay under SEC's 10/sec limit)
# ---------------------------------------------------------------------------

class _RateLimiter:
    def __init__(self, rps=8):
        self._lock = threading.Lock()
        self._last = 0.0
        self._interval = 1.0 / rps

    def wait(self):
        with self._lock:
            now = time.time()
            gap = self._interval - (now - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.time()

_sec_rl = _RateLimiter(8)

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def sec_get(url, timeout=30, stream=False):
    """Rate-limited GET to SEC EDGAR. Returns requests.Response."""
    _sec_rl.wait()
    resp = requests.get(url, headers=HEADERS, timeout=timeout, stream=stream)
    resp.raise_for_status()
    return resp

# ---------------------------------------------------------------------------
# HTML -> plain text
# ---------------------------------------------------------------------------

class HTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.result = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True
        if tag in ("p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"):
            self.result.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False
        if tag in ("p", "div", "tr", "li", "td", "th"):
            self.result.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.result.append(data)

    def get_text(self):
        raw = "".join(self.result)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def html_to_text(html_content):
    """Convert HTML to readable plain text."""
    stripper = HTMLStripper()
    try:
        stripper.feed(html_content)
        return stripper.get_text()
    except Exception:
        # Fallback: brute-force tag removal
        text = re.sub(r"<[^>]+>", " ", html_content)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"&lt;", "<", text)
        text = re.sub(r"&gt;", ">", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

# ---------------------------------------------------------------------------
# Ticker-CIK mapping (cached 24 hours)
# ---------------------------------------------------------------------------

def load_ticker_cik_map():
    """Load SEC ticker->CIK mapping. Caches locally for 24 hours.

    Returns {TICKER: CIK_str_padded_10}.
    """
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / "company_tickers.json"

    if cache_file.exists():
        age_hours = (time.time() - cache_file.stat().st_mtime) / 3600
        if age_hours < 24:
            with open(cache_file) as f:
                data = json.load(f)
            return _parse_ticker_map(data)

    url = "https://www.sec.gov/files/company_tickers.json"
    print("  Downloading ticker-CIK map from SEC...")
    resp = sec_get(url)
    data = resp.json()

    with open(cache_file, "w") as f:
        json.dump(data, f)

    return _parse_ticker_map(data)


def _parse_ticker_map(data):
    """Returns {TICKER: CIK_str_padded_10}"""
    mapping = {}
    for entry in data.values():
        ticker = entry["ticker"].upper()
        cik = str(entry["cik_str"]).zfill(10)
        mapping[ticker] = cik
    return mapping

# ---------------------------------------------------------------------------
# Filing metadata
# ---------------------------------------------------------------------------

def get_all_filings(cik):
    """Fetch ALL filings metadata for a CIK, including older pages.

    Returns (company_name, entity_type, sic, tickers, filings_list).
    Each filing in the list is a dict with keys:
        form, date, accession, primaryDocument
    """
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    resp = sec_get(url)
    data = resp.json()

    company_name = data.get("name", "Unknown")
    entity_type = data.get("entityType", "")
    sic = data.get("sic", "")
    tickers = data.get("tickers", [])

    recent = data.get("filings", {}).get("recent", data.get("recent", {}))
    filings_list = []

    def _collect(src):
        forms = src.get("form", [])
        dates = src.get("filingDate", [])
        acc_nums = src.get("accessionNumber", [])
        primary_docs = src.get("primaryDocument", [])
        for i in range(len(forms)):
            filings_list.append({
                "form": forms[i] if i < len(forms) else "",
                "date": dates[i] if i < len(dates) else "",
                "accession": acc_nums[i] if i < len(acc_nums) else "",
                "primaryDocument": primary_docs[i] if i < len(primary_docs) else "",
            })

    if recent:
        _collect(recent)

    for file_ref in data.get("filings", {}).get("files", []):
        fname = file_ref.get("name", "")
        if fname:
            try:
                older_resp = sec_get(f"https://data.sec.gov/submissions/{fname}")
                _collect(older_resp.json())
            except Exception:
                pass

    return company_name, entity_type, sic, tickers, filings_list

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def sanitize_form_name(form):
    """Make form name safe for filenames."""
    return form.replace("/", "-").replace(" ", "_")


def download_filing_text(doc_url):
    """Download a filing document and return plain text."""
    resp = sec_get(doc_url)
    content_type = resp.headers.get("Content-Type", "")
    text = resp.text

    if "html" in content_type.lower() or text.strip().startswith(("<", "<!DOCTYPE")):
        return html_to_text(text)
    return text
