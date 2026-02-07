# WEEKLY SCANNER — Claude Agent Instructions

You have TWO jobs every week. Run them in order.

---

## JOB 1: Weekly Macro & Market Snapshot

Produce: `outputs/weekly/Market_Snapshot.md`

### What to track

#### Rates and Inflation ("the gravity")

| Metric | Why |
|--------|-----|
| 10Y yield (level + weekly change) | Valuation pressure / discount rate |
| 2Y yield | Market view of policy over 0-24 months |
| Yield curve (2s10s or 3m10y) | Recession/slowdown signal + bank profitability |
| 10Y real yield (TIPS) | Separates "rates up because growth" vs "rates up because inflation" |
| Breakeven inflation (5Y or 10Y) | Inflation expectations shift pricing power narratives |

#### Credit ("the stress meter")

| Metric | Why |
|--------|-----|
| High yield spread (HY OAS) | When this blows out, equities follow |
| Investment grade spread (IG OAS) | Confirms whether stress is broad or just lower quality |

#### Equity Market Internals (signal vs noise)

| Metric | Why |
|--------|-----|
| S&P 500 return + Equal-weight S&P return | Tells you if "the market" is really just a few mega-caps |
| Breadth: % of stocks above 200-day MA | Weak breadth = fragile rally |
| New highs vs new lows | Risk appetite expanding or shrinking |

#### Volatility and Risk Appetite

| Metric | Why |
|--------|-----|
| VIX (equity volatility) | Risk-off / forced de-risking shows up here first |
| MOVE (bond volatility) | Bond volatility often drives equity multiple compression |

#### FX

| Metric | Why |
|--------|-----|
| DXY (US dollar index) | Strong USD hits multinationals' reported revenue/margins |

#### Commodities (real-economy pressure)

| Metric | Why |
|--------|-----|
| Oil (Brent/WTI) | Consumer squeeze + inflation impulse |
| Copper | Industrial demand proxy; "growth pulse" |

#### Company Fundamentals (weekly-relevant only)

- Earnings revisions direction (up/down) for holdings + sector
- Guidance changes (raised/cut/maintained)
- Share count trend (buyback yield vs SBC) — track announcements weekly
- Credit event language (covenants, liquidity, "material weakness", investigations) from filings

### Market Snapshot Format

```
# Market Snapshot — Week of [DATE_RANGE]

## Rates & Inflation
| Metric | Level | Weekly Change | Signal |
...

## Credit
...

## Equity Internals
...

## Volatility
...

## FX & Commodities
...

## Fundamentals Pulse
...

## Bottom Line (3 sentences max)
```

Use web search to pull current data. If a data point is unavailable, write "N/A — source unavailable" instead of guessing.

---

## JOB 2: SEC Filing Scanner (S&P 100)

### Working Directory Expectations
Repo root: /Users/solmaz/Investment_Tools
Expected to exist:
- `filing_docs/` (downloaded filings as .txt)
- `securities_monitor.py` and/or `dashboard.py` (may exist)

If `filing_docs/` is empty or stale, ask the user to run their downloader script first.
Do NOT browse the internet for filings.

### CORE RULES (prevent tool spam)
1. DO NOT read all filings. Never open more than 12 filing files in one run.
2. Always do a 2-pass workflow:
   - PASS 1 (cheap): index + score using filenames + small excerpts.
   - PASS 2 (deep): open ONLY the top 8-12 filings.
3. If there are >50 filings for the week, create a quick Python batching file first to extract excerpts into ONE jsonl file.
4. Stop once outputs are written. Do not keep exploring.

### UNIVERSE
Default universe is S&P 100.

### INPUT SCOPE
"Weekly" = last 7 days based on date embedded in filenames: `TICKER_FORM_DATE.txt`
If dates are missing, fall back to file modified time.

### PASS 1 — Index & Score (NO file deep reads)
Create/overwrite: `outputs/weekly/filings_index.csv`
Columns: ticker, form, date, path

Create/overwrite: `outputs/weekly/top_filings.txt` (ranked list)

Scoring heuristic:
- Form weight: 8-K=5, 10-Q=4, 10-K=4, 20-F=4, 6-K=3, DEF14A=3, Form4=2, others=1
- Keyword boosts (filename OR quick excerpt): +3 guidance, +3 acquisition/merger, +3 impairment, +3 restructuring, +3 material weakness, +3 subpoena/DOJ/SEC, +2 liquidity/covenant, +2 investigation, +2 going concern
- Recency boost: newer date higher

Select top 8-12 filings for PASS 2.

### PASS 1 Batching Script (only if needed, >50 filings)
Write: `tools/build_weekly_excerpts.py`
Output: `outputs/weekly/excerpts.jsonl`
One record per filing: `{ticker, form, date, path, excerpt}`

Excerpt rules:
- 10-K/10-Q: find Item 1A Risk Factors / Item 7 MD&A anchors; take 4,000-8,000 chars around best match.
- 8-K: extract around Item 2.02, 1.01, 7.01, 8.01 and "forward-looking statements".
- Keep excerpt <= 8,000 chars per filing.

### PASS 2 — Deep Read (8-12 filings max)
For each selected filing, extract:
- **What happened** (1-2 sentences)
- **Why it matters** (cause -> effect, 1-2 sentences)
- **What to watch next** (1 sentence)
- **Signal tags** (e.g., GUIDANCE_CUT, BUYBACK_UP, REGULATORY, LIQUIDITY, IMPAIRMENT)

Do not summarize the entire filing.

### OUTPUTS (required)

1. `outputs/weekly/Weekly_Digest.md`
```
# Weekly Filing Digest — [WEEK_RANGE]

## Top 10 Items (ranked by signal)

### 1. TICKER — FORM — DATE
**What happened:**
**Why it matters:**
**What to watch next:**
Source: filename
```

2. `outputs/weekly/Signal_Alerts.md`
Only include alerts matching these triggers:
- Guidance raised/cut
- Margin pressure language
- Buyback expanded/suspended
- Restructuring/layoffs
- Impairment/material weakness
- Subpoena/regulator/investigation
- Liquidity/covenant language

Each alert: ticker, trigger, 1 quote (<=20 words), source file.

3. `outputs/weekly/top_filings.txt` (the ranked list used)

### STYLE
- Plain English
- Cause -> effect
- No fluff, no hype
- If the text doesn't support it, say "Not supported by filing text."

### STOP RULE
Stop immediately after writing the 3 filing output files + Market_Snapshot.md. Do not continue browsing or reading extra filings.
