#!/usr/bin/env python3
"""
Market Scanner — Earnings Calendar, Insider Trading, 52-Week Lows, Cross-Signals.

Data sources (all official/reliable):
  - Earnings Calendar: Nasdaq.com API
  - Insider Trading: SEC EDGAR Form 4 filings
  - 52-Week Lows: Yahoo Finance price data

Usage:
    python3 market_scanner.py              # full report with AI analysis
    python3 market_scanner.py --earnings   # earnings only
    python3 market_scanner.py --signals    # insider + lows + cross-signals only
    python3 market_scanner.py --data       # raw data, no AI analysis

Flags can be combined:
    python3 market_scanner.py --earnings --data
    python3 market_scanner.py --signals --data
"""

import sys, os, argparse, datetime, json, time, re, sqlite3, threading
import xml.etree.ElementTree as ET
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from html.parser import HTMLParser
from tools.llm_engine import ask_ai_complete

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, "tools"))
from source_policy import load_source_mode

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SEC_AGENT = "Solmaz ahmetardasolmaz@gmail.com"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "research.db")
INSIDER_LOOKBACK_DAYS = 7
LOW_THRESHOLD_PCT = 5.0  # within 5% of 52-week low
YF_BATCH_SIZE = 60
EARNINGS_PRIORITY_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "earnings_priority_tickers.txt"
)
WIKI_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
WIKI_NASDAQ100_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"
WIKI_DOW30_URL = "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average"
ADR_TOP100_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "adr_top100_tickers.txt"
)

# ---------------------------------------------------------------------------
# SEC Rate Limiter (thread-safe, 9 req/sec)
# ---------------------------------------------------------------------------

class _RateLimiter:
    def __init__(self, rps=9):
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

_sec_rl = _RateLimiter(9)


def sec_get(url):
    """Rate-limited GET to SEC EDGAR."""
    _sec_rl.wait()
    req = Request(url, headers={"User-Agent": SEC_AGENT})
    with urlopen(req, timeout=20) as resp:
        return resp.read()

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_watchlist():
    """Return [(ticker, cik, name), ...] from Postgres (cloud) or SQLite (local)."""
    # Try Postgres first so cloud jobs don't crash on missing SQLite tables
    try:
        sys.path.insert(0, SCRIPT_DIR)
        from app.services.postgres_core_service import pg_connect
        con_pg = pg_connect()
        if con_pg is not None:
            try:
                cur = con_pg.cursor()
                cur.execute("SELECT ticker, COALESCE(cik,''), COALESCE(name,'') FROM companies_core ORDER BY ticker")
                rows = cur.fetchall()
                if rows:
                    return rows
            finally:
                con_pg.close()
    except Exception:
        pass
    # Fallback: local SQLite
    if not os.path.exists(DB_PATH):
        return []
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT ticker, cik, name FROM companies").fetchall()
    conn.close()
    return rows

# ---------------------------------------------------------------------------
# EARNINGS CALENDAR  (Nasdaq API)
# Best-of from earnings_radar.py: sorts by market cap, last-year EPS,
# large/mega-cap counts, better error handling, (DONE) marker for past days.
# ---------------------------------------------------------------------------

def _parse_cap(cap_str):
    """Parse market cap string like '$77,213,614,886' to int."""
    try:
        return int(cap_str.replace("$", "").replace(",", ""))
    except (ValueError, TypeError, AttributeError):
        return 0


def canonical_ticker(sym):
    """Normalize ticker symbols across data sources (., -, / variants)."""
    return str(sym or "").strip().upper().replace("/", ".").replace("-", ".")


def _with_retries(fn, attempts=3, base_sleep=0.7):
    """Retry helper for transient data-source/network failures."""
    last_err = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:
            last_err = exc
            if i < attempts - 1:
                time.sleep(base_sleep * (i + 1))
    raise last_err


def load_earnings_priority_tickers():
    """Load priority earnings tickers from env + local file."""
    out = set()

    env_raw = os.getenv("EARNINGS_PRIORITY_TICKERS", "")
    if env_raw.strip():
        for tok in re.split(r"[,\s]+", env_raw.strip()):
            if tok:
                out.add(tok.upper())

    if os.path.exists(EARNINGS_PRIORITY_FILE):
        try:
            with open(EARNINGS_PRIORITY_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    out.add(line.upper())
        except Exception:
            pass

    return sorted(out)


class _WikiConstituentsParser(HTMLParser):
    """Extract symbols from a Wikipedia table with id='constituents'."""

    def __init__(self):
        super().__init__()
        self.in_constituents_table = False
        self.table_depth = 0
        self.in_tr = False
        self.in_td = False
        self.in_th = False
        self.in_symbol_cell = False
        self.current_cell = []
        self.cell_index = -1
        self.header_cells = []
        self.symbol_col_idx = 0
        self.seen_header = False
        self.symbols = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "table" and attrs_dict.get("id") == "constituents":
            self.in_constituents_table = True
            self.table_depth = 1
            return

        if not self.in_constituents_table:
            return

        if tag == "table":
            self.table_depth += 1
        elif tag == "tr":
            self.in_tr = True
            self.cell_index = -1
            self.header_cells = []
        elif tag == "td" and self.in_tr:
            self.in_td = True
            self.current_cell = []
            self.cell_index += 1
            self.in_symbol_cell = self.cell_index == self.symbol_col_idx
        elif tag == "th" and self.in_tr:
            self.in_th = True
            self.current_cell = []
            self.cell_index += 1

    def handle_data(self, data):
        if not self.in_constituents_table:
            return
        if self.in_th:
            self.current_cell.append(data)
        elif self.in_td and self.in_symbol_cell:
            self.current_cell.append(data)

    def handle_endtag(self, tag):
        if not self.in_constituents_table:
            return

        if tag == "th" and self.in_th:
            txt = "".join(self.current_cell).strip()
            self.header_cells.append(txt)
            self.in_th = False
            self.current_cell = []
        if tag == "td" and self.in_td:
            if self.in_symbol_cell:
                sym = "".join(self.current_cell).strip()
                if sym and sym.upper() != "SYMBOL":
                    self.symbols.append(sym.upper())
            self.in_td = False
            self.in_symbol_cell = False
            self.current_cell = []
        elif tag == "tr":
            if self.header_cells and not self.seen_header:
                low = [h.lower() for h in self.header_cells]
                for idx, h in enumerate(low):
                    if "symbol" in h or "ticker" in h:
                        self.symbol_col_idx = idx
                        break
                self.seen_header = True
            self.in_tr = False
        elif tag == "table":
            self.table_depth -= 1
            if self.table_depth <= 0:
                self.in_constituents_table = False


def _fetch_wiki_constituents(url, label):
    """Fetch ticker list from a Wikipedia page constituents table."""
    def _fetch():
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    html = _with_retries(_fetch, attempts=3, base_sleep=1.0)
    parser = _WikiConstituentsParser()
    parser.feed(html)
    symbols = sorted(set(s.strip().upper() for s in parser.symbols if s.strip()))
    if not symbols:
        raise RuntimeError(f"Could not parse {label} symbols from Wikipedia constituents table.")
    return symbols


def fetch_sp500_tickers():
    return _fetch_wiki_constituents(WIKI_SP500_URL, "S&P 500")


def fetch_nasdaq100_tickers():
    return _fetch_wiki_constituents(WIKI_NASDAQ100_URL, "Nasdaq-100")


def fetch_dow30_tickers():
    return _fetch_wiki_constituents(WIKI_DOW30_URL, "Dow 30")


def load_top_adr_tickers():
    """Load Top ADR universe from local file (configurable, one ticker per line)."""
    out = []
    if not os.path.exists(ADR_TOP100_FILE):
        return out
    with open(ADR_TOP100_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.append(line.upper())
    return sorted(set(out))


def build_golden_earnings_universe():
    """Build institutional earnings universe from core benchmark sets."""
    sp500 = fetch_sp500_tickers()
    ndx = fetch_nasdaq100_tickers()
    dow30 = fetch_dow30_tickers()
    adr100 = load_top_adr_tickers()

    combined = {}
    for sym in sp500:
        combined[canonical_ticker(sym)] = sym
    for sym in ndx:
        combined.setdefault(canonical_ticker(sym), sym)
    for sym in dow30:
        combined.setdefault(canonical_ticker(sym), sym)
    for sym in adr100:
        combined.setdefault(canonical_ticker(sym), sym)

    meta = {
        "sp500": len(sp500),
        "ndx": len(ndx),
        "dow30": len(dow30),
        "adr100": len(adr100),
        "combined": len(combined),
    }
    return sorted(combined.values()), meta


def fetch_earnings_calendar():
    """Fetch this week's earnings from Nasdaq API."""
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    all_earnings = []
    errors = []

    print(f"Fetching earnings calendar for week of {monday.isoformat()}...", file=sys.stderr)

    for i in range(5):  # Mon-Fri
        day = monday + datetime.timedelta(days=i)
        try:
            def _fetch_day():
                url = f"https://api.nasdaq.com/api/calendar/earnings?date={day.isoformat()}"
                req = Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                    "Accept": "application/json, text/plain, */*",
                })
                with urlopen(req, timeout=15) as resp:
                    return json.loads(resp.read())

            data = _with_retries(_fetch_day, attempts=3, base_sleep=0.7)

            rows = (data.get("data") or {}).get("rows") or []
            for r in rows:
                all_earnings.append({
                    "date": day.isoformat(),
                    "day_name": day.strftime("%A"),
                    "symbol": r.get("symbol", ""),
                    "name": r.get("name", ""),
                    "time": r.get("time", "").replace("time-", "").replace("-", " "),
                    "eps_est": r.get("epsForecast", ""),
                    "eps_last": r.get("lastYearEPS", ""),
                    "market_cap": r.get("marketCap", ""),
                })
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            msg = f"{day.isoformat()}: {type(exc).__name__}: {exc}"
            errors.append(msg)
            print(f"  Warning: earnings fetch failed for {day.isoformat()} ({type(exc).__name__})", file=sys.stderr)
        except Exception as exc:
            msg = f"{day.isoformat()}: {type(exc).__name__}: {exc}"
            errors.append(msg)
            print(f"  Warning: unexpected error for {day.isoformat()} ({type(exc).__name__})", file=sys.stderr)
        time.sleep(0.3)

    if errors and not all_earnings:
        raise RuntimeError(
            "Earnings calendar fetch failed for all weekdays. "
            f"First error: {errors[0]}"
        )

    if errors:
        print(f"  Warning: {len(errors)} of 5 weekdays failed to fetch", file=sys.stderr)

    print(f"  Found {len(all_earnings)} companies reporting this week", file=sys.stderr)
    return all_earnings

# ---------------------------------------------------------------------------
# INSIDER TRADING  (SEC EDGAR Form 4)
# ---------------------------------------------------------------------------

def fetch_insider_trades():
    """Scan watchlist companies for recent Form 4 filings."""
    watchlist = get_watchlist()
    cutoff = (datetime.date.today() - datetime.timedelta(days=INSIDER_LOOKBACK_DAYS)).isoformat()
    all_trades = []
    total = len(watchlist)

    print(f"Scanning {total} companies for insider trades (SEC Form 4)...", file=sys.stderr)

    def _scan_company(args):
        ticker, cik, name = args
        trades = []
        try:
            url = (
                f"https://www.sec.gov/cgi-bin/browse-edgar?"
                f"action=getcompany&CIK={cik}&type=4&dateb=&"
                f"owner=include&count=20&output=atom"
            )
            raw = sec_get(url)
            root = ET.fromstring(raw)
            ns = {"a": "http://www.w3.org/2005/Atom"}

            for entry in root.findall("a:entry", ns):
                updated = entry.findtext("a:updated", "", ns)[:10]
                if updated < cutoff:
                    continue

                link_el = entry.find("a:link", ns)
                if link_el is None:
                    continue
                href = link_el.get("href", "")
                if not href:
                    continue

                base = href.rsplit("/", 1)[0] + "/"
                trades.extend(_parse_form4_filing(base, ticker, name))
        except Exception:
            pass
        return trades

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_scan_company, w) for w in watchlist]
        done = 0
        for f in as_completed(futures):
            done += 1
            if done % 100 == 0:
                print(f"  [{done}/{total}]", file=sys.stderr)
            result = f.result()
            if result:
                all_trades.extend(result)

    all_trades.sort(key=lambda t: (t["type"] != "BUY", -t["value"]))
    print(f"  Insider trades found: {len(all_trades)}", file=sys.stderr)
    return all_trades


def _parse_form4_filing(base_url, ticker, company_name):
    """Download and parse a single Form 4 filing XML."""
    xml_data = None

    # Try doc4.xml first (most common filename)
    try:
        xml_data = sec_get(base_url + "doc4.xml")
        if b"ownershipDocument" not in xml_data:
            xml_data = None
    except Exception:
        xml_data = None

    # Fallback: fetch directory, find XML
    if xml_data is None:
        try:
            html = sec_get(base_url).decode("utf-8", errors="ignore")
            for fname in re.findall(r'href="([^"]+\.xml)"', html):
                if "index" in fname.lower() or fname.startswith("R"):
                    continue
                try:
                    xml_data = sec_get(base_url + fname)
                    if b"ownershipDocument" in xml_data:
                        break
                    xml_data = None
                except Exception:
                    continue
        except Exception:
            return []

    if xml_data is None:
        return []

    trades = []
    try:
        root = ET.fromstring(xml_data)
        owner_name = root.findtext(".//rptOwnerName", "").strip()
        officer_title = root.findtext(".//officerTitle", "").strip()
        is_director = root.findtext(".//isDirector", "0") == "1"
        role = officer_title or ("Director" if is_director else "Other")

        for txn in root.findall(".//nonDerivativeTransaction"):
            code = txn.findtext(".//transactionCoding/transactionCode", "")
            if code not in ("P", "S"):
                continue

            try:
                shares = float(txn.findtext(".//transactionShares/value", "0"))
                price = float(txn.findtext(".//transactionPricePerShare/value", "0"))
                value = shares * price
            except (ValueError, TypeError):
                continue

            if value < 10_000:
                continue

            trades.append({
                "ticker": ticker,
                "company": company_name,
                "insider": owner_name,
                "title": role,
                "type": "BUY" if code == "P" else "SELL",
                "shares": int(shares),
                "price": f"{price:.2f}",
                "value": value,
                "date": txn.findtext(".//transactionDate/value", ""),
            })
    except Exception:
        pass

    return trades

# ---------------------------------------------------------------------------
# 52-WEEK & 5-YEAR LOWS  (Yahoo Finance)
# ---------------------------------------------------------------------------

def find_52week_lows():
    """Scan watchlist for stocks near 52-week and 5-year lows."""
    import yfinance as yf

    watchlist = get_watchlist()
    tickers = [t[0] for t in watchlist]
    if not tickers:
        return []

    # Keep yfinance cache inside project path to avoid sqlite cache path issues.
    try:
        cache_dir = os.path.join(os.path.dirname(DB_PATH), "yf_cache")
        os.makedirs(cache_dir, exist_ok=True)
        yf.set_tz_cache_location(cache_dir)
    except Exception:
        pass

    print(f"Scanning {len(tickers)} stocks for 52-week lows...", file=sys.stderr)
    lows = []
    failed = 0
    for i in range(0, len(tickers), YF_BATCH_SIZE):
        batch = tickers[i:i + YF_BATCH_SIZE]
        data_1y = None
        data_5y = None
        try:
            # threads=False + smaller batches reduce random failures significantly.
            data_1y = _with_retries(
                lambda: yf.download(batch, period="1y", progress=False, threads=False, auto_adjust=False),
                attempts=3,
                base_sleep=0.8,
            )
            data_5y = _with_retries(
                lambda: yf.download(batch, period="5y", progress=False, threads=False, auto_adjust=False),
                attempts=3,
                base_sleep=0.8,
            )
        except Exception:
            # Fallback: one-by-one retries so one bad ticker does not sink a full batch.
            data_1y = {}
            data_5y = {}
            for sym in batch:
                try:
                    data_1y[sym] = _with_retries(
                        lambda s=sym: yf.download([s], period="1y", progress=False, threads=False, auto_adjust=False),
                        attempts=2,
                        base_sleep=0.6,
                    )
                    data_5y[sym] = _with_retries(
                        lambda s=sym: yf.download([s], period="5y", progress=False, threads=False, auto_adjust=False),
                        attempts=2,
                        base_sleep=0.6,
                    )
                except Exception:
                    failed += 1
                    continue

        for sym in batch:
            try:
                if isinstance(data_1y, dict):
                    d1 = data_1y.get(sym)
                    d5 = data_5y.get(sym)
                    if d1 is None or d5 is None:
                        continue
                    c1 = d1["Close"].dropna()
                    c5 = d5["Close"].dropna()
                elif len(batch) == 1:
                    c1 = data_1y["Close"].dropna()
                    c5 = data_5y["Close"].dropna()
                else:
                    c1 = data_1y["Close"][sym].dropna() if sym in data_1y["Close"] else []
                    c5 = data_5y["Close"][sym].dropna() if sym in data_5y["Close"] else []

                if len(c1) < 20:
                    continue

                cur = c1.iloc[-1]
                lo52 = c1.min()
                hi52 = c1.max()
                pct = ((cur - lo52) / lo52) * 100

                if pct > LOW_THRESHOLD_PCT:
                    continue

                lo5y = c5.min() if len(c5) > 50 else lo52

                lows.append({
                    "ticker": sym,
                    "current": f"{cur:.2f}",
                    "low_52w": f"{lo52:.2f}",
                    "high_52w": f"{hi52:.2f}",
                    "pct_above_low": f"{pct:.1f}",
                    "pct_from_high": f"{((cur - hi52) / hi52 * 100):.1f}",
                    "at_52w_low": cur <= lo52 * 1.002,
                    "at_5y_low": cur <= lo5y * 1.05,
                })
            except Exception:
                failed += 1
                continue

    lows.sort(key=lambda x: float(x["pct_above_low"]))
    if failed:
        print(f"  Note: {failed} ticker fetch/parse failures were skipped", file=sys.stderr)
    print(f"  Near 52-week lows: {len(lows)} stocks", file=sys.stderr)
    return lows


def filter_earnings_to_universe(earnings, tickers):
    """Return earnings entries whose symbol maps to the provided ticker universe."""
    universe_canon = {canonical_ticker(s) for s in tickers}
    out = []
    for e in earnings:
        if canonical_ticker(e.get("symbol", "")) in universe_canon:
            out.append(e)
    return out

# ---------------------------------------------------------------------------
# Report builder
# Best-of formatting from each script:
#   - Earnings: earnings_radar.py (market cap sort, eps_last, summary stats)
#   - Insider:  signal_tracker.py (buy/sell summary, company name column)
#   - Lows:     signal_tracker.py (split 52w+5y vs 52w-only)
#   - Cross:    signal_tracker.py (earnings cross-signals added)
# ---------------------------------------------------------------------------

def build_report(
    earnings,
    trades,
    lows,
    sections,
    mode,
    priority_tickers=None,
    sp500_tickers=None,
    earnings_universe_meta=None,
):
    """Build markdown report.

    sections: dict with keys 'earnings' and 'signals' (both True for full).
    """
    today = datetime.date.today()
    mon = today - datetime.timedelta(days=today.weekday())
    fri = mon + datetime.timedelta(days=4)
    week = f"{mon.strftime('%b %d')} – {fri.strftime('%b %d, %Y')}"

    L = [f"# Market Scanner — {today.strftime('%A, %B %d, %Y')}\n"]
    if mode == "strict":
        L.append("*Source mode: strict (primary sources only).*\n")
        L.append("*Sources in this run: SEC EDGAR Form 4 only.*\n")
    else:
        L.append("*Source mode: bloomberg_like (broader source set).*\n")
        L.append("*Sources: Nasdaq.com (earnings), SEC EDGAR Form 4 (insider trading), Yahoo Finance (price data)*\n")

    # --- Earnings section (from earnings_radar.py) ---
    if sections["earnings"]:
        L.append(f"## EARNINGS CALENDAR — Week of {week}\n")
        if earnings:
            by_day = defaultdict(list)
            for e in earnings:
                by_day[e["date"]].append(e)
            by_symbol = defaultdict(list)
            for e in earnings:
                by_symbol[e["symbol"].upper()].append(e)

            total = len(earnings)
            large_cap = [e for e in earnings if _parse_cap(e["market_cap"]) >= 10_000_000_000]
            mega_cap = [e for e in earnings if _parse_cap(e["market_cap"]) >= 100_000_000_000]
            L.append(f"**Total: {total} companies** | Large-cap (>$10B): {len(large_cap)} | Mega-cap (>$100B): {len(mega_cap)}\n")

            if sp500_tickers or earnings_universe_meta:
                ref_tickers = sp500_tickers or []
                ref_set = {canonical_ticker(s) for s in ref_tickers}
                ref_week = [e for e in earnings if canonical_ticker(e["symbol"]) in ref_set] if ref_set else earnings
                tomorrow = today + datetime.timedelta(days=1)
                ref_next2 = [
                    e for e in ref_week
                    if e["date"] in {today.isoformat(), tomorrow.isoformat()}
                ]
                ref_next2.sort(key=lambda x: (x["date"], -_parse_cap(x["market_cap"]), x["symbol"]))

                if earnings_universe_meta:
                    L.append(
                        "\n### GOLDEN LIST COVERAGE CHECK\n"
                        f"Universe sizes: S&P 500={earnings_universe_meta.get('sp500', 0)}, "
                        f"Nasdaq-100={earnings_universe_meta.get('ndx', 0)}, "
                        f"Dow 30={earnings_universe_meta.get('dow30', 0)}, "
                        f"Top ADR list={earnings_universe_meta.get('adr100', 0)}\n"
                    )
                    L.append(
                        f"Combined deduped universe: {earnings_universe_meta.get('combined', 0)} tickers | "
                        f"Universe earnings this week: {len(ref_week)}\n"
                    )
                else:
                    L.append(
                        f"\n### S&P 500 COVERAGE CHECK\n"
                        f"Current S&P 500 universe: {len(ref_tickers)} tickers | "
                        f"S&P 500 earnings this week: {len(ref_week)}\n"
                    )

                L.append(f"#### Universe Reporting Today/Tomorrow ({len(ref_next2)})\n")
                if ref_next2:
                    L.append("| Date | Symbol | Company | Time | EPS Est | Last Year EPS | Market Cap |")
                    L.append("|------|--------|---------|------|---------|---------------|------------|")
                    for e in ref_next2:
                        L.append(
                            f"| {e['date']} | {e['symbol']} | {e['name'][:40]} | {e['time']} "
                            f"| {e['eps_est']} | {e['eps_last']} | {e['market_cap']} |"
                        )
                else:
                    L.append("*No universe companies in today/tomorrow window from Nasdaq calendar.*")

            if priority_tickers:
                L.append("\n### PRIORITY TICKERS COVERAGE\n")
                L.append("| Symbol | Status | Date | Time | EPS Est | Last Year EPS | Market Cap |")
                L.append("|--------|--------|------|------|---------|---------------|------------|")
                for sym in priority_tickers:
                    matches = by_symbol.get(sym, [])
                    if not matches:
                        L.append(f"| {sym} | NOT IN THIS WEEK CALENDAR | - | - | - | - | - |")
                        continue

                    e = sorted(matches, key=lambda x: x["date"])[0]
                    d = datetime.date.fromisoformat(e["date"])
                    if d < today:
                        status = "REPORTED"
                    elif d == today:
                        status = "REPORTS TODAY"
                    else:
                        status = "UPCOMING"
                    L.append(
                        f"| {sym} | {status} | {e['date']} | {e['time']} "
                        f"| {e['eps_est']} | {e['eps_last']} | {e['market_cap']} |"
                    )

            for d in sorted(by_day):
                day_label = datetime.date.fromisoformat(d).strftime("%A, %B %d")
                day_earnings = by_day[d]
                day_earnings.sort(key=lambda x: _parse_cap(x["market_cap"]), reverse=True)

                past = datetime.date.fromisoformat(d) < today
                marker = " (DONE)" if past else ""

                L.append(f"\n### {day_label}{marker} — {len(day_earnings)} companies\n")
                L.append("| Symbol | Company | Time | EPS Est | Last Year EPS | Market Cap |")
                L.append("|--------|---------|------|---------|---------------|------------|")
                for e in day_earnings:
                    L.append(
                        f"| {e['symbol']} | {e['name'][:40]} | {e['time']} "
                        f"| {e['eps_est']} | {e['eps_last']} | {e['market_cap']} |"
                    )
        else:
            L.append("*No earnings data available.*\n")

    # --- Insider Trading section (from signal_tracker.py) ---
    if sections["signals"]:
        buys = [t for t in trades if t["type"] == "BUY"]
        sells = [t for t in trades if t["type"] == "SELL"]

        L.append(f"\n## INSIDER TRADING — Last {INSIDER_LOOKBACK_DAYS} Days (SEC Form 4)\n")

        total_buy_value = sum(t["value"] for t in buys)
        total_sell_value = sum(t["value"] for t in sells)
        L.append(f"**Summary:** {len(buys)} open-market buys (${total_buy_value:,.0f}) | "
                 f"{len(sells)} open-market sells (${total_sell_value:,.0f})\n")

        if buys:
            L.append("### INSIDER BUYS (Open-Market Purchases)\n")
            L.append("| Date | Ticker | Company | Insider | Title | Shares | Price | Value |")
            L.append("|------|--------|---------|---------|-------|--------|-------|-------|")
            for t in sorted(buys, key=lambda x: -x["value"]):
                L.append(
                    f"| {t['date']} | {t['ticker']} | {t['company'][:25]} "
                    f"| {t['insider'][:25]} | {t['title'][:20]} "
                    f"| {t['shares']:,} | ${t['price']} | ${t['value']:,.0f} |"
                )
        else:
            L.append("### INSIDER BUYS\n*No open-market insider buys detected this week.*\n")

        if sells:
            top_sells = sorted(sells, key=lambda x: -x["value"])[:60]
            L.append(f"\n### INSIDER SELLS (Top {len(top_sells)} by Value)\n")
            L.append("| Date | Ticker | Company | Insider | Title | Shares | Price | Value |")
            L.append("|------|--------|---------|---------|-------|--------|-------|-------|")
            for t in top_sells:
                L.append(
                    f"| {t['date']} | {t['ticker']} | {t['company'][:25]} "
                    f"| {t['insider'][:25]} | {t['title'][:20]} "
                    f"| {t['shares']:,} | ${t['price']} | ${t['value']:,.0f} |"
                )

        # --- 52-Week Lows (from signal_tracker.py — split 52w+5y vs 52w-only) ---
        L.append(f"\n## STOCKS NEAR 52-WEEK LOWS (within {LOW_THRESHOLD_PCT:.0f}%)\n")

        if mode == "strict":
            L.append("*Skipped in strict mode (Yahoo Finance is disabled in strict primary-source mode).*")
            lows = []

        at_both = [s for s in lows if s["at_5y_low"]]
        at_52_only = [s for s in lows if not s["at_5y_low"]]

        if at_both:
            L.append(f"### AT BOTH 52-WEEK AND 5-YEAR LOWS ({len(at_both)} stocks)\n")
            L.append("| Ticker | Current | 52W Low | 52W High | % Above Low | % From High |")
            L.append("|--------|---------|---------|----------|-------------|-------------|")
            for s in at_both:
                L.append(
                    f"| **{s['ticker']}** | ${s['current']} | ${s['low_52w']} "
                    f"| ${s['high_52w']} | {s['pct_above_low']}% | {s['pct_from_high']}% |"
                )

        if at_52_only:
            L.append(f"\n### NEAR 52-WEEK LOWS ONLY ({len(at_52_only)} stocks)\n")
            L.append("| Ticker | Current | 52W Low | 52W High | % Above Low | % From High |")
            L.append("|--------|---------|---------|----------|-------------|-------------|")
            for s in at_52_only:
                at_tag = " **AT LOW**" if s["at_52w_low"] else ""
                L.append(
                    f"| {s['ticker']}{at_tag} | ${s['current']} | ${s['low_52w']} "
                    f"| ${s['high_52w']} | {s['pct_above_low']}% | {s['pct_from_high']}% |"
                )

        if not lows:
            L.append("*No stocks within 5% of 52-week low.*\n")

        # --- Cross-Signals (from signal_tracker.py + earnings cross-signals) ---
        low_tickers = {s["ticker"] for s in lows}
        insider_buy_tickers = {t["ticker"] for t in buys}
        insider_sell_tickers = {t["ticker"] for t in sells}

        buy_at_low = insider_buy_tickers & low_tickers
        sell_at_low = insider_sell_tickers & low_tickers

        # Add earnings cross-signals if earnings data is available
        earning_tickers = {e["symbol"] for e in earnings} if earnings else set()
        earnings_at_low = earning_tickers & low_tickers
        earnings_with_buy = earning_tickers & insider_buy_tickers

        L.append("\n## CROSS-SIGNALS\n")

        if buy_at_low:
            L.append("### INSIDER BUYING + NEAR 52-WEEK LOW (Bullish Confluence)\n")
            for sym in sorted(buy_at_low):
                buy_val = sum(t["value"] for t in buys if t["ticker"] == sym)
                low_info = next(s for s in lows if s["ticker"] == sym)
                L.append(f"- **{sym}**: Insider buys totaling ${buy_val:,.0f} while stock is "
                         f"{low_info['pct_above_low']}% above 52-week low")
        else:
            L.append("*No stocks with insider buying at 52-week lows.*\n")

        if sell_at_low:
            L.append("\n### INSIDER SELLING + NEAR 52-WEEK LOW (Bearish Confluence)\n")
            for sym in sorted(sell_at_low):
                sell_val = sum(t["value"] for t in sells if t["ticker"] == sym)
                low_info = next(s for s in lows if s["ticker"] == sym)
                L.append(f"- **{sym}**: Insider sells totaling ${sell_val:,.0f} while stock is "
                         f"{low_info['pct_above_low']}% above 52-week low")

        if earnings_at_low:
            L.append("\n### EARNINGS THIS WEEK + NEAR 52-WEEK LOW (Catalyst Potential)\n")
            for sym in sorted(earnings_at_low):
                low_info = next(s for s in lows if s["ticker"] == sym)
                earn_info = next(e for e in earnings if e["symbol"] == sym)
                L.append(f"- **{sym}**: Reports {earn_info['date']} ({earn_info['time']}), "
                         f"stock is {low_info['pct_above_low']}% above 52-week low")

        if earnings_with_buy:
            L.append("\n### EARNINGS THIS WEEK + INSIDER BUYING\n")
            for sym in sorted(earnings_with_buy):
                buy_val = sum(t["value"] for t in buys if t["ticker"] == sym)
                earn_info = next(e for e in earnings if e["symbol"] == sym)
                L.append(f"- **{sym}**: Reports {earn_info['date']}, insider buys totaling ${buy_val:,.0f}")

    return "\n".join(L)

# ---------------------------------------------------------------------------
# AI Prompts
# ---------------------------------------------------------------------------

PROMPT_FULL = """You are a senior equity analyst preparing a daily market scanner report for a sophisticated long-term investor based in Ireland. Today is {today}.

Below is REAL data sourced from:
- **Earnings Calendar**: Nasdaq.com (official exchange data)
- **Insider Trading**: SEC EDGAR Form 4 filings (official regulatory filings)
- **52-Week Lows**: Yahoo Finance price data (all companies > $500M market cap)

{data_report}

---

Write a structured analysis:

## 1. EARNINGS CALENDAR HIGHLIGHTS
Which companies reporting this week are most significant? Why do their earnings matter? Group by day. Flag any that could move the broader market or sectors.

## 2. INSIDER TRADING ANALYSIS
Summarize the week's insider activity: total buys vs sells, dollar volume, notable patterns.

Focus primarily on BUYS — insider buying is a much stronger signal than selling.
For notable buys: who bought, their role, transaction size, and what it might signal.
For large sells: distinguish between routine (10b5-1 plans, diversification) vs concerning (clustered selling, unusual timing, C-suite dumping ahead of bad news).

## 3. 52-WEEK & MULTI-YEAR LOWS
For each stock near its lows: why is it there? Value opportunity or value trap? What would need to change for a reversal? Stocks at BOTH 52-week AND 5-year lows deserve extra scrutiny.

Identify sector patterns — are entire sectors getting repriced?

## 4. CROSS-SIGNALS
The most powerful signals come from combinations:
- **Insider buying + near lows** = potentially strong contrarian buy signal
- **Insider selling + near lows** = red flag, insiders not defending the stock
- **Earnings this week + near lows** = catalyst potential (binary outcome)
- **No insider buying + many stocks at multi-year lows** = lack of smart money conviction

## 5. WATCHLIST
Based on all data, recommend 3-5 names that deserve closer research. For each:
- The thesis (why it's interesting)
- The catalyst (what could change)
- The risk (what could go wrong)

RULES:
- Use ONLY the data provided
- If you don't know why something is happening, say so
- Be specific with numbers, names, dates
- No disclaimers, no filler
- Insider buying is a MUCH stronger signal than selling — weight your analysis accordingly
"""

PROMPT_EARNINGS = """You are a senior equity analyst preparing a weekly earnings preview for a sophisticated long-term investor based in Ireland. Today is {today}.

Below is the COMPLETE earnings calendar for this week, sourced from Nasdaq.com (official exchange data).

{data_report}

---

Write a comprehensive earnings preview:

## 1. THIS WEEK AT A GLANCE
Summarize the week: how many companies are reporting, which days are heaviest, and the overall theme (is this peak earnings season? Winding down?).

## 2. MUST-WATCH EARNINGS (by day)
For each day, identify the 3-5 most important reports and explain:
- **What to watch**: the key metrics/questions for each company
- **Why it matters**: how this earnings report affects the broader market, sector, or investor sentiment
- **Consensus expectations**: EPS estimate and whether the bar is high or low
- **Sector implications**: what this report tells us about the sector as a whole

Focus on mega/large-cap names that move markets. Include notable mid-caps if they're sector bellwethers.

## 3. SECTOR THEMES
Which sectors have the most companies reporting? What are the key questions for each sector this earnings season?

## 4. EARNINGS SURPRISES TO WATCH
Flag any companies where:
- The consensus estimate seems too low or too high based on recent sector trends
- There's a significant gap between this year's estimate and last year's actual
- The stock has moved dramatically ahead of earnings (pricing in a beat or miss?)

## 5. CALENDAR STRATEGY
- Which after-hours/pre-market reports could set the tone for the next trading day?
- Any earnings "clusters" where multiple related companies report the same day?
- What days have the highest volatility potential?

RULES:
- Use ONLY the data provided
- Be specific with numbers, dates, and company names
- If you don't know something about a company, say so — don't fabricate
- No disclaimers, no filler
- Write for a sophisticated investor who values signal over noise
"""

PROMPT_SIGNALS = """You are a senior equity analyst preparing an insider trading and value signal report for a sophisticated long-term investor based in Ireland. Today is {today}.

Below is REAL data from official sources:
- **Insider Trading**: SEC EDGAR Form 4 filings (regulatory filings, not estimates)
- **52-Week / 5-Year Lows**: Yahoo Finance price data (S&P 500 universe, all >$500M market cap)

{data_report}

---

Write a structured analysis:

## 1. INSIDER TRADING OVERVIEW
Summarize the week's insider activity: total buys vs sells, dollar volume, notable patterns. Is insider buying elevated or suppressed? What does the buy/sell ratio tell us about corporate insider sentiment?

## 2. NOTABLE INSIDER BUYS
For each meaningful buy:
- Who bought, their exact role, and transaction size
- Why this buy matters (C-suite buys are stronger signals than director buys)
- What it might signal about the company's outlook
- Is this a cluster buy (multiple insiders buying) or a lone buyer?

If no buys: discuss what the ABSENCE of insider buying means when many quality names are at multi-year lows.

## 3. CONCERNING INSIDER SELLS
For the largest sells:
- Distinguish between likely routine (10b5-1 plan, diversification, post-vesting) vs potentially concerning (discretionary, unusual timing, clustered)
- Flag any C-suite members selling aggressively
- Note any companies where multiple insiders are selling simultaneously

## 4. 52-WEEK & 5-YEAR LOWS ANALYSIS
For each stock at or near lows:
- Why is it there? (sector rotation, earnings miss, company-specific issue?)
- Is this a value opportunity or a value trap?
- What would need to change for a reversal?

Separate analysis for stocks at BOTH 52-week AND 5-year lows (these deserve extra scrutiny).

Identify sector patterns — are entire sectors getting repriced?

## 5. CROSS-SIGNAL ANALYSIS
The most powerful signals come from combinations:
- **Insider buying + near lows** = potentially strong contrarian buy signal
- **Insider selling + near lows** = red flag, insiders not defending the stock
- **No insider buying + many stocks at multi-year lows** = lack of smart money conviction

## 6. WATCHLIST
Based on all the data, recommend 3-5 names that deserve closer research. For each:
- The thesis (why it's interesting)
- The catalyst (what could change)
- The risk (what could go wrong)

RULES:
- Use ONLY the data provided — do not fabricate transactions or prices
- If you don't know why something is happening, say so
- Be specific with numbers, names, dates
- No disclaimers, no filler
- Insider buying is a MUCH stronger signal than selling — weight your analysis accordingly
"""

PROMPT_STRICT_SIGNALS = """You are a senior equity analyst preparing a strict-source insider report for a long-term investor.

Constraints:
- Use ONLY SEC EDGAR Form 4 data provided below.
- Do NOT infer price-based setups or earnings calendar conclusions.
- If information is missing, say "insufficient data from strict-source set".

{data_report}

Write:
## 1. Insider Activity Summary
## 2. Most Meaningful Insider Buys
## 3. Most Meaningful Insider Sells
## 4. What This Suggests For Long-Term Monitoring
## 5. Next Data To Pull (10-K/10-Q/8-K sections to verify thesis)
"""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Market Scanner — Earnings, Insider Trading, 52-Week Lows")
    parser.add_argument("--earnings", action="store_true", help="Earnings calendar only")
    parser.add_argument("--signals", action="store_true", help="Insider trading + 52-week lows + cross-signals only")
    parser.add_argument("--data", action="store_true", help="Raw data only (no AI analysis)")
    args = parser.parse_args()

    # Default: if neither --earnings nor --signals, run all sections
    if not args.earnings and not args.signals:
        args.earnings = True
        args.signals = True

    mode = load_source_mode()
    strict_mode = mode == "strict"

    if strict_mode and args.earnings:
        print("Strict mode: disabling earnings calendar (Nasdaq source).", file=sys.stderr)
        args.earnings = False

    sections = {"earnings": args.earnings, "signals": args.signals}

    print(f"=== Market Scanner ({mode}) ===", file=sys.stderr)

    earnings, trades, lows = [], [], []
    sp500_tickers = []
    earnings_universe_meta = None

    # Fetch data in parallel based on which sections are needed
    futures = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        if sections["earnings"]:
            futures["earnings"] = pool.submit(fetch_earnings_calendar)
        if sections["signals"]:
            futures["trades"] = pool.submit(fetch_insider_trades)
            if not strict_mode:
                futures["lows"] = pool.submit(find_52week_lows)

        if "earnings" in futures:
            earnings = futures["earnings"].result()
            # --earnings mode anchors to the combined "golden list" universe.
            try:
                golden_tickers, earnings_universe_meta = build_golden_earnings_universe()
                sp500_tickers = golden_tickers
                if args.earnings and not args.signals:
                    earnings = filter_earnings_to_universe(earnings, golden_tickers)
                    print(
                        f"Golden-list earnings filter applied: {len(earnings)} companies "
                        f"(universe size: {len(golden_tickers)}).",
                        file=sys.stderr,
                    )
            except Exception as exc:
                print(f"Warning: could not build golden earnings universe ({type(exc).__name__}: {exc})", file=sys.stderr)
        if "trades" in futures:
            trades = futures["trades"].result()
        if "lows" in futures:
            lows = futures["lows"].result()

    if not sections["earnings"] and not sections["signals"]:
        print("No enabled sections for current source mode.", file=sys.stderr)
        print("Tip: use `python3 research_agent.py mode bloomberg_like` for broader datasets.")
        return

    priority_tickers = load_earnings_priority_tickers()
    report = build_report(
        earnings,
        trades,
        lows,
        sections,
        mode,
        priority_tickers=priority_tickers,
        sp500_tickers=sp500_tickers,
        earnings_universe_meta=earnings_universe_meta,
    )

    if args.data:
        print(report)
        return

    # Select prompt based on mode
    if strict_mode:
        prompt_template = PROMPT_STRICT_SIGNALS
        report_name = "signal_tracker"
    else:
        if sections["earnings"] and sections["signals"]:
            prompt_template = PROMPT_FULL
            report_name = "market_scanner"
        elif sections["earnings"]:
            prompt_template = PROMPT_EARNINGS
            report_name = "earnings_radar"
        else:
            prompt_template = PROMPT_SIGNALS
            report_name = "signal_tracker"

    today = datetime.date.today().strftime("%A, %B %d, %Y")
    prompt = prompt_template.format(today=today, data_report=report)
    print(f"Sending to AI ({len(prompt):,} chars)...", file=sys.stderr)
    output = ask_ai_complete(prompt, "You are a senior equity analyst.", max_continuations=2)

    full_output = f"{report}\n\n---\n\n{output}"

    report_path = os.getenv("REPORT_PATH", "").strip()
    if report_path:
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
    else:
        report_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
        os.makedirs(report_dir, exist_ok=True)
        date_tag = datetime.date.today().strftime("%Y%m%d")
        report_path = os.path.join(report_dir, f"{report_name}_{date_tag}.md")

    tmp_path = report_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(full_output)
    os.replace(tmp_path, report_path)

    print(full_output)
    print(f"Saved report to {report_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
