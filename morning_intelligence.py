#!/usr/bin/env python3
"""
Global Morning Intelligence — Daily market & geopolitical briefing.
Fetches real market data from Yahoo Finance + headlines from RSS feeds,
then sends to Claude CLI for analysis and commentary.

Usage:
    python3 morning_intelligence.py          # generate full briefing
    python3 morning_intelligence.py --data   # print raw data only (no Claude)
"""

import sys, os, datetime, xml.etree.ElementTree as ET
from urllib.request import urlopen, Request
from urllib.error import URLError
from concurrent.futures import ThreadPoolExecutor, as_completed

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, "tools"))
from llm_engine import ask_ai_complete
from source_policy import load_source_mode

# ---------------------------------------------------------------------------
# Market tickers — Yahoo Finance symbols
# ---------------------------------------------------------------------------

INDICES = {
    "S&P 500": "^GSPC", "Nasdaq": "^IXIC", "Dow Jones": "^DJI",
    "Russell 2000": "^RUT",
    "FTSE 100": "^FTSE", "DAX": "^GDAXI", "CAC 40": "^FCHI",
    "STOXX 600": "^STOXX", "BIST 100": "XU100.IS",
    "Nikkei 225": "^N225", "Shanghai Comp": "000001.SS",
    "Hang Seng": "^HSI", "KOSPI": "^KS11", "ASX 200": "^AXJO",
    "Nifty 50": "^NSEI",
}

COMMODITIES = {
    "WTI Crude Oil": "CL=F", "Brent Crude Oil": "BZ=F",
    "Gold": "GC=F", "Silver": "SI=F", "Natural Gas": "NG=F",
    "Copper": "HG=F", "Wheat": "ZW=F",
}

CURRENCIES = {
    "EUR/USD": "EURUSD=X", "GBP/USD": "GBPUSD=X", "USD/JPY": "USDJPY=X",
    "USD/TRY": "USDTRY=X", "USD/CNY": "USDCNY=X", "USD/KZT": "USDKZT=X",
    "USD/RUB": "USDRUB=X", "USD/INR": "USDINR=X",
    "DXY (USD Index)": "DX-Y.NYB",
}

BONDS = {"US 10Y Yield": "^TNX", "US 2Y Yield": "^IRX", "US 30Y Yield": "^TYX"}
VOLATILITY = {"VIX": "^VIX"}
CRYPTO = {"Bitcoin": "BTC-USD", "Ethereum": "ETH-USD"}

# ---------------------------------------------------------------------------
# RSS Feeds — categorized intelligence sources
# ---------------------------------------------------------------------------

RSS_FEEDS = {
    # Central Banks & Liquidity
    "Federal Reserve": "https://www.federalreserve.gov/feeds/press_all.xml",
    "ECB": "https://www.ecb.europa.eu/rss/press.html",
    "BIS": "https://www.bis.org/doclist/rss_all_categories.rss",
    # Geopolitical Intelligence
    "CSIS": "https://www.csis.org/rss/news",
    "ISW": "https://www.understandingwar.org/feeds.xml",
    "Chatham House": "https://www.chathamhouse.org/rss/research",
    "BBC World": "https://feeds.bbci.co.uk/news/world/rss.xml",
    # Energy & Commodities
    "OilPrice.com": "https://oilprice.com/rss/main",
    "EIA Petroleum": "https://www.eia.gov/rss/petroleum.xml",
    "IEA": "https://www.iea.org/rss/news",
    "USDA": "https://www.usda.gov/rss/latest-releases.xml",
    # Corporate & Smart Money
    "McKinsey": "https://www.mckinsey.com/insights/rss",
    "SEC Press": "https://www.sec.gov/news/pressreleases.rss",
    # Market News
    "CNBC Finance": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664",
    "Reuters Business": "http://feeds.reuters.com/reuters/businessNews",
}

STRICT_RSS_FEEDS = {
    k: v for k, v in RSS_FEEDS.items()
    if k in {"Federal Reserve", "ECB", "BIS", "EIA Petroleum", "IEA", "USDA", "SEC Press"}
}

# Max headlines per feed
MAX_HEADLINES = 8
# Max total headlines to include in prompt
MAX_TOTAL_HEADLINES = 120


def fetch_single_feed(name, url):
    """Fetch and parse a single RSS feed. Returns list of (title, date) tuples."""
    headlines = []
    try:
        req = Request(url, headers={"User-Agent": "MorningIntelligence/1.0"})
        with urlopen(req, timeout=10) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)

        # Handle both RSS and Atom feeds
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)

        for item in items[:MAX_HEADLINES]:
            title = (
                item.findtext("title")
                or item.findtext("atom:title", namespaces=ns)
                or ""
            ).strip()
            pub = (
                item.findtext("pubDate")
                or item.findtext("atom:updated", namespaces=ns)
                or item.findtext("dc:date", namespaces={"dc": "http://purl.org/dc/elements/1.1/"})
                or ""
            ).strip()
            if title:
                headlines.append((title, pub))
    except Exception:
        pass  # silently skip failed feeds
    return name, headlines


def fetch_all_rss(feeds=None):
    """Fetch all RSS feeds in parallel. Returns dict of {source: [(title, date)]}."""
    if feeds is None:
        feeds = RSS_FEEDS
    results = {}
    print(f"Fetching {len(feeds)} RSS feeds...", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {
            pool.submit(fetch_single_feed, name, url): name
            for name, url in feeds.items()
        }
        for future in as_completed(futures):
            name, headlines = future.result()
            if headlines:
                results[name] = headlines

    total = sum(len(v) for v in results.values())
    print(f"  Got {total} headlines from {len(results)} feeds", file=sys.stderr)
    return results


def format_rss_report(rss_data):
    """Format RSS headlines into a text report."""
    lines = ["\n# LATEST HEADLINES FROM INTELLIGENCE SOURCES\n"]
    count = 0
    for source, headlines in sorted(rss_data.items()):
        if count >= MAX_TOTAL_HEADLINES:
            break
        lines.append(f"\n**{source}:**")
        for title, pub in headlines:
            if count >= MAX_TOTAL_HEADLINES:
                break
            date_str = f" ({pub[:16]})" if pub else ""
            lines.append(f"- {title}{date_str}")
            count += 1
    return "\n".join(lines)


def fetch_market_data():
    """Fetch latest quotes for all tracked instruments using yfinance."""
    import yfinance as yf

    all_tickers = {}
    all_tickers.update(INDICES)
    all_tickers.update(COMMODITIES)
    all_tickers.update(CURRENCIES)
    all_tickers.update(BONDS)
    all_tickers.update(VOLATILITY)
    all_tickers.update(CRYPTO)

    symbols = list(all_tickers.values())
    names = list(all_tickers.keys())

    print(f"Fetching {len(symbols)} instruments from Yahoo Finance...", file=sys.stderr)

    data = yf.download(symbols, period="5d", progress=False, threads=True)

    results = {}
    for name, sym in zip(names, symbols):
        try:
            if len(symbols) == 1:
                close_col = data["Close"]
            else:
                close_col = data["Close"][sym] if sym in data["Close"].columns else None

            if close_col is None or close_col.dropna().empty:
                results[name] = {"price": "N/A", "change": "N/A", "change_pct": "N/A"}
                continue

            closes = close_col.dropna()
            if len(closes) < 2:
                results[name] = {"price": f"{closes.iloc[-1]:.2f}", "change": "N/A", "change_pct": "N/A"}
                continue

            latest = closes.iloc[-1]
            prev = closes.iloc[-2]
            change = latest - prev
            change_pct = (change / prev) * 100

            results[name] = {
                "price": f"{latest:.2f}",
                "change": f"{change:+.2f}",
                "change_pct": f"{change_pct:+.2f}%",
                "prev_close": f"{prev:.2f}",
            }
        except Exception as e:
            results[name] = {"price": "N/A", "change": "N/A", "change_pct": "N/A", "error": str(e)}

    return results


def format_section(title, tickers, data):
    """Format a section of market data as a text table."""
    lines = [f"\n### {title}", "| Instrument | Price | Change | % Change |", "|---|---|---|---|"]
    for name in tickers:
        d = data.get(name, {})
        lines.append(f"| {name} | {d.get('price','N/A')} | {d.get('change','N/A')} | {d.get('change_pct','N/A')} |")
    return "\n".join(lines)


def build_data_report(market_data, mode):
    """Build the full market data report as markdown text."""
    today = datetime.date.today().strftime("%A, %B %d, %Y")
    if mode == "strict":
        return (
            f"# GLOBAL MARKET DATA — {today}\n\n"
            "Source mode is `strict`.\n"
            "Real-time market prices from Yahoo Finance are disabled in strict mode.\n"
            "Use `tools/fetch_macro.py` + SEC filings for primary-source analysis.\n"
        )

    sections = [
        f"# GLOBAL MARKET DATA — {today}\n",
        format_section("Stock Indices", INDICES.keys(), market_data),
        format_section("Commodities", COMMODITIES.keys(), market_data),
        format_section("Currencies", CURRENCIES.keys(), market_data),
        format_section("Bond Yields", BONDS.keys(), market_data),
        format_section("Volatility", VOLATILITY.keys(), market_data),
        format_section("Crypto", CRYPTO.keys(), market_data),
    ]
    return "\n".join(sections)


PROMPT = """You are a senior global macro analyst preparing a morning intelligence briefing for a long-term investor based in Ireland. Today's date is {today}.

Below is REAL market data fetched just now from Yahoo Finance, followed by the LATEST HEADLINES from official government sources, central banks, geopolitical think tanks, energy agencies, and major financial news wires. Use ALL of this as your foundation.

{data_report}

{rss_report}

---

Write the morning intelligence briefing in this EXACT structure:

# Global Morning Intelligence — {today}

## 1. OVERNIGHT & EARLY MORNING SUMMARY
One paragraph: what happened globally since yesterday's close. Start from Asia-Pacific (Japan, China, Korea, India, Australia), then Europe & Turkey, then US futures/yesterday's US close. Include Kazakhstan, Russia, Middle East if anything notable. Use the REAL data above.

## 2. KEY MARKET MOVES & WHY
For each significant move (indices, commodities, currencies), explain:
- **WHAT**: the move with exact numbers from the data above
- **WHY**: the reason (central bank decision, earnings, geopolitics, data release, etc.)
Use the RSS headlines to identify catalysts. Only cover moves that matter. Skip flat/unremarkable instruments.

## 3. COMMODITIES & ENERGY
Oil, gold, natural gas, copper, wheat — what moved and why. Reference OilPrice, EIA, IEA headlines if relevant. Include OPEC news, supply disruptions, sanctions, inventory data.

## 4. CURRENCIES & RATES
Major currency moves and why. Any central bank decisions or rate changes ANYWHERE in the world (check Fed, ECB, BOJ, BOE, CBRT, PBOC headlines). Turkish lira, Kazakh tenge, Chinese yuan. US Treasury yield movements and what's driving them. Reference the yield curve (10Y vs 2Y).

## 5. GEOPOLITICAL & MACRO INTELLIGENCE
Key geopolitical developments from the headlines (CSIS, ISW, Chatham House, BBC). Wars, conflicts, elections, sanctions, trade disputes — but ONLY what affects markets or investors. Include SEC enforcement actions if any.

## 6. WHAT TO WATCH TODAY
- Economic data releases scheduled today (GDP, CPI, PMI, employment, trade balance — from any country)
- Central bank meetings, decisions, or speeches today
- Corporate earnings of note reporting today
- Political events, summits, votes, hearings today
- Any market-moving deadlines today

## 7. UPCOMING EVENTS — NEXT 6 MONTHS
List the most important scheduled events in the next 6 months:
- Central bank meeting dates (Fed, ECB, BOJ, BOE, CBRT, PBOC, RBI, etc.)
- Major summits (G7, G20, Davos/WEF, IMF/World Bank Spring & Annual meetings, OPEC+, etc.)
- Major elections or referendums globally
- Key earnings season windows
- Geopolitical deadlines (trade deals, sanctions reviews, treaty expirations, debt ceilings)
Only confirmed/highly likely events. Include approximate dates.

## 8. ANALYST COMMENT
2-3 paragraphs of analysis based on the facts above. What does the data tell you? What should the investor focus on this week? Emerging risks or opportunities? Cross-reference the market data with the headlines. Be direct, factual, no noise. End with 2-3 specific action items.

---

RULES:
- Use ONLY the real Yahoo Finance data for prices and percentage changes
- Use the RSS headlines for context, catalysts, and geopolitical intelligence
- For additional WHY context, rely on: government data, central banks, Reuters, Bloomberg, Yahoo Finance, BBC, official statistics agencies
- If you don't know why something moved, say "no clear catalyst" — do NOT speculate or fabricate
- No filler, no fluff, no disclaimers
- Be specific with numbers, dates, names, sources
- Write for a sophisticated global investor who values signal over noise
"""

PROMPT_STRICT = """You are preparing a strict-source morning intelligence note.

Rules:
- Use only official institutional headlines provided below.
- Do not use or infer market price moves that are not in the data.
- If a catalyst lacks enough details, state "insufficient strict-source confirmation."

{rss_report}

Write:
# Global Morning Intelligence — {today}
## 1. Official Policy And Macro Headlines
## 2. Cross-Market Implications For Long-Term Investors
## 3. What To Watch Next (next 1-5 days)
## 4. Data Gaps To Fill (SEC filings, FRED series, official releases)
"""


def main():
    data_only = "--data" in sys.argv
    mode = load_source_mode()
    strict_mode = mode == "strict"

    # Fetch market data and RSS in parallel
    from concurrent.futures import ThreadPoolExecutor

    if strict_mode:
        market_data = {}
        rss_data = fetch_all_rss(STRICT_RSS_FEEDS)
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            market_future = pool.submit(fetch_market_data)
            rss_future = pool.submit(fetch_all_rss)

            market_data = market_future.result()
            rss_data = rss_future.result()

    data_report = build_data_report(market_data, mode)
    rss_report = format_rss_report(rss_data)

    if data_only:
        print(data_report)
        print(rss_report)
        return

    today = datetime.date.today().strftime("%A, %B %d, %Y")
    if strict_mode:
        prompt = PROMPT_STRICT.format(today=today, rss_report=rss_report)
    else:
        prompt = PROMPT.format(today=today, data_report=data_report, rss_report=rss_report)

    print(f"Sending to AI ({len(prompt):,} chars of context)...", file=sys.stderr)
    output = ask_ai_complete(prompt, "You are a senior global macro analyst.", max_continuations=2)

    report_path = os.getenv("REPORT_PATH", "").strip()
    if report_path:
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
    else:
        report_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
        os.makedirs(report_dir, exist_ok=True)
        date_tag = datetime.date.today().strftime("%Y%m%d")
        report_path = os.path.join(report_dir, f"morning_intelligence_{date_tag}.md")

    tmp_path = report_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(output)
    os.replace(tmp_path, report_path)

    print(output)
    print(f"Saved report to {report_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
