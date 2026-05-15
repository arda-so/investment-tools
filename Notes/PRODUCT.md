# New Intelligence — Product & Business Overview

> **Your portfolio never sleeps. Neither does your AI analyst.**

---

## Company Identity

**Name candidates**: New Intelligence (`newintelligence.ai`)
**Tagline**: AI investment analyst that never sleeps. Monitors filings, debates trades, and learns from outcomes — so you don't miss what matters.

---

## The Problem

Self-directed investors, solo RIAs, and small fund managers are outnumbered. Institutions have teams of 20 analysts, Bloomberg terminals, and custom data pipelines. Independent investors have a browser and a spreadsheet.

- The SEC publishes thousands of filings per day. You can't read them all. You miss the 8-K that warned about your biggest holding — and find out when the stock is already down 12%.
- Earnings season hits and you have 15 calls in two weeks. You skim the headlines, miss the CFO's tone shift from "confident" to "cautious," and hold too long.
- An insider sells $4M in stock on a Tuesday. You don't see the Form 4 until the following week. By then, everyone knows.
- Oil spikes, yields move, the yen collapses — and you're trying to figure out which of your 25 holdings gets hit by the second-order effect.
- You make a trade based on a thesis. Three months later you don't remember why you bought it, whether the thesis still holds, or what changed.

---

## The Solution

An AI-powered investment intelligence platform that monitors, reads, analyzes, debates, and learns — 24 hours a day, 7 days a week — across your entire portfolio and watchlist.

---

## Target Customer

| Segment | Profile | Portfolio Size |
|---------|---------|---------------|
| **Self-directed investors** | Serious retail, manage their own money, 5-10 hrs/week on research | $100K – $2M |
| **Solo RIAs / Independent advisors** | Need institutional-grade research without institutional headcount | $2M – $20M AUM |
| **Family offices** | 1-3 people managing a diversified portfolio across sectors and geographies | $5M – $50M |
| **Small fund managers** | Running a fund, need an analyst that doesn't take vacation | $5M – $50M AUM |

**Not for**: Day traders, stock tip seekers, crypto gamblers. This is for people who build investment theses and need an AI system that continuously validates or challenges those theses with real data.

---

## Pricing

| Tier | Price | Annual | What's Included |
|------|-------|--------|-----------------|
| **Starter** | $149/mo | $1,490/yr | SEC filing monitoring, insider trade detection, AI earnings analysis, live macro dashboard, news search, Workspace OS, 1 portfolio |
| **Pro** | $349/mo | $3,490/yr | Everything in Starter + multi-agent debate, cascade analysis, reflexion learning, earnings trend tracking, up to 3 portfolios |
| **Institutional** | $799/mo | $7,990/yr | Everything in Pro + unlimited portfolios, API access, custom signal universe, priority support |
| **Enterprise** | Custom | $15K+/yr | Self-hosted, white-label, dedicated support, custom integrations |

### Pricing Evolution — Move to Usage-Based Model

> **TODO**: Migrate from flat monthly subscriptions to a modern usage/consumption-based SaaS model.

The industry is shifting. Flat monthly pricing is becoming outdated. Companies like OpenAI, Snowflake, Twilio, and AWS proved that **pay-for-what-you-use** aligns cost with value and scales better. Our platform is naturally suited for this because every action is measurable.

**Proposed hybrid model: Base + Usage**

| Component | How to Meter |
|-----------|-------------|
| **Base platform fee** | Low monthly base ($29-49/mo) for Workspace OS, calendar, hub, dashboard access |
| **AI analysis credits** | Per filing analyzed, per earnings transcript processed, per debate run |
| **Monitoring seats** | Per ticker monitored (held + watchlist + universe) |
| **API calls** | Per request for Institutional/Enterprise |
| **Storage** | Per GB of historical proposals, debate artifacts, reflexion rules |

**Example consumption pricing:**

| Action | Cost |
|--------|------|
| SEC filing analyzed by AI | $0.50 per filing |
| Earnings transcript analysis | $1.00 per transcript |
| Multi-agent debate (bull/bear/risk/judge) | $0.75 per debate |
| Cascade analysis | $0.25 per cascade |
| Ticker monitoring slot | $5/mo per ticker |
| AI chat query | $0.10 per query |

**Why this is better:**
- Small investors pay less → lower barrier to entry → more users
- Heavy users pay more → revenue scales with value delivered
- Aligns our costs (Vertex AI compute) directly with revenue
- Enterprise customers prefer predictable per-unit costs over opaque tiers
- Easier to upsell: "add 10 more tickers" vs "upgrade to next tier"

**What we need to build:**
- Usage metering middleware (track every AI call, filing processed, debate run)
- Usage dashboard for the user ("you've used 47 analysis credits this month")
- Billing integration (Stripe usage-based billing)
- Spending alerts / caps so users don't get surprised

### Billing Implementation — Three Options

We already track every AI call, every filing, every debate, every proposal in Postgres with timestamps. The usage data exists — we just need to meter and charge for it.

**The billing flow:**

```
User action
  → backend processes it
    → writes to usage_metrics_core table
      → real-time credit deduction (Option A)
      → or end-of-month aggregation (Option B)
      → or monthly ticker count (Option C)
        → Stripe charges card
```

---

#### Option A — Credit Packs (Simple, Prepaid)

User buys credits upfront. Each AI action costs credits. When credits run low, buy more or auto-refill.

| Pack | Price | Credits |
|------|-------|---------|
| Starter | $49 | 100 credits |
| Pro | $199 | 500 credits |
| Fund | $499 | 1,500 credits |

| Action | Credit Cost |
|--------|------------|
| SEC filing analysis | 2 credits |
| Multi-agent debate (bull/bear/risk/judge) | 3 credits |
| Earnings transcript analysis | 4 credits |
| Cascade analysis | 1 credit |
| AI chat query | 1 credit |
| Insider trade evaluation | 1 credit |

**Pros:**
- Users control spend — buy what they need
- Revenue upfront (prepaid)
- Easy to understand
- Can offer bonus credits for larger packs

**Cons:**
- Users might hesitate to use features ("saving credits")
- Requires real-time credit balance tracking
- Credits expiring or not expiring — policy headache

**What to build:**
- `user_credits_core` table (balance, transactions)
- Credit deduction middleware in every AI call path
- Low-balance notification system
- Stripe checkout for credit pack purchase
- Credits balance widget on the dashboard

---

#### Option B — Metered Billing (Pay-as-you-go, Postpaid)

Track everything. Bill at end of month for actual usage. Like a phone bill or AWS.

| Action | Unit Price |
|--------|-----------|
| SEC filing analyzed | $0.50 |
| Multi-agent debate | $0.75 |
| Earnings transcript analysis | $1.00 |
| Cascade analysis | $0.25 |
| AI chat query | $0.10 |
| Insider trade evaluation | $0.25 |
| Ticker monitoring (per ticker/month) | $5.00 |

**Example monthly bill for a typical user (20 tickers):**
```
Ticker monitoring:  20 × $5.00  = $100.00
Filings analyzed:   45 × $0.50  =  $22.50
Debates run:        12 × $0.75  =   $9.00
Earnings analyzed:   8 × $1.00  =   $8.00
Cascades:           15 × $0.25  =   $3.75
Chat queries:       60 × $0.10  =   $6.00
                                 --------
Total:                            $149.25
```

**Pros:**
- True pay-for-what-you-use — fairest model
- Aligns our AI compute costs directly with revenue
- No credits to manage — just use it
- Scales naturally — heavy users pay more
- Stripe has native usage-based billing support

**Cons:**
- Users don't know their bill until end of month (anxiety)
- Need spending caps / alerts to prevent surprise bills
- Slightly harder to market ("starting at..." is vague)
- Need invoice generation

**What to build:**
- `usage_metrics_core` table (action, ticker, timestamp, cost)
- Stripe metered subscription with usage records
- Monthly usage aggregation job
- User-facing usage dashboard ("$87.50 so far this month")
- Spending cap setting (user sets max $200/mo, system pauses monitoring at limit)
- Email alerts at 50%, 80%, 100% of cap

---

#### Option C — Per-Ticker Subscription (Simplest)

Charge per ticker monitored per month. All features included. No credit counting, no metering complexity.

| Tickers | Price/mo | Annual |
|---------|----------|--------|
| 5 tickers | $49/mo | $490/yr |
| 15 tickers | $129/mo | $1,290/yr |
| 30 tickers | $249/mo | $2,490/yr |
| 50 tickers | $399/mo | $3,990/yr |
| Unlimited | $699/mo | $6,990/yr |

Everything included at every level: filing monitoring, debates, cascades, earnings, chat, workspace OS.

**Pros:**
- Dead simple — user picks how many tickers, done
- Easy to explain and market
- Predictable revenue per user
- Directly tied to value: more tickers = more monitoring = more AI work
- User controls cost by controlling ticker count
- Upsell is natural: "add 10 more tickers" not "upgrade to next tier"
- No credit anxiety, no surprise bills

**Cons:**
- Heavy AI chat users subsidized by light users
- Doesn't capture value from debate/earnings analysis specifically
- Users might game it by rotating tickers in and out

**What to build:**
- `user_subscriptions_core` table (plan, ticker_limit, stripe_id)
- Ticker count enforcement in monitoring threads
- Stripe subscription with plan tiers
- Upgrade/downgrade flow in UI
- "You're using 14 of 15 ticker slots" indicator

---

#### Recommendation

**Start with Option C (per-ticker), evolve to Option B (metered).**

Option C is the fastest to ship and easiest for early customers to understand. No one wants to think about credits when they're evaluating a new product. "Pick your tickers, everything works" is a clean pitch.

Once we have paying users and usage data, we can analyze actual consumption patterns and migrate to Option B (metered) for power users and institutional customers who want granular billing.

The hybrid future state:
```
Base platform fee ($29/mo)
  + per-ticker monitoring ($5/ticker/mo)
  + AI analysis overage ($0.50/filing beyond included quota)
```

### Competitive Positioning

| Competitor | Price | What They Offer | What We Do Better |
|-----------|-------|-----------------|-------------------|
| Bloomberg Terminal | $25,000/yr | Raw data, you do the analysis | We read, analyze, debate, and recommend |
| AlphaSense | $10,000+/yr | Document search | We monitor proactively + evaluate impact |
| Koyfin Pro | $600/yr | Charts and screening | We cover filings, earnings, insider trades, cascades |
| Seeking Alpha Premium | $240/yr | Crowdsourced opinions | We use AI debate with bull/bear/risk/judge |

---

## Platform Capabilities — Technical Detail

### 1. SEC Filing Monitor

- Background thread polls SEC EDGAR every 6 hours (configurable `SEC_POLL_INTERVAL_SEC`)
- Covers all held tickers + signal universe tickers
- Forms monitored: `8-K`, `10-Q`, `10-K`, `6-K`, `20-F`
- **Metadata-first approach**: fetches accession number + date first, only downloads full filing markdown for new/unseen filings
- Deduplication via `sec_edgar_poll_state_core` table (form type + accession key)
- New filing → fires `sec_filing` event into event bus → triggers AI proposal generation
- Parallel fetch with `ThreadPoolExecutor(max_workers=4)`

### 2. Form 4 Insider Trade Detection

- Separate daily cycle (every 24 hours, configurable `FORM4_POLL_INTERVAL_SEC`)
- Fetches Form 4 filings from EDGAR for all held + watched tickers
- **Smart filter criteria**:
  - Transaction code `S` (sale) or `P` (open-market purchase) only
  - Filer must be CEO, CFO, President, Director, or Officer
  - Estimated value must exceed $500K (shares × price)
  - Excludes 10b5-1 pre-scheduled trades
  - Excludes grants, options, and other non-market transactions
- Qualifying trades fire `insider_trade_signal` event → AI evaluation

### 3. Signal Universe Expansion

- `signal_universe_core` table holds 12 default macro/supply-chain tickers: TSM, NVDA, ASML, INTC, MSFT, AMZN, GOOGL, META, JPM, COST, WMT, CAT
- These are tickers the user doesn't hold but that affect held positions
- Held tickers → filing events → full proposal generation
- Non-held universe tickers → `universe_signal` events → cascade analysis only (noise-filtered, no direct proposals)

### 4. AI Proposal Engine

- When an event fires (filing, insider trade, universe signal), the proposal engine evaluates it
- LLM receives: filing/signal content + portfolio context + financial data + active reflexion rules
- Outputs structured proposal: action (buy/sell/hold/watch), confidence score (0-1), reasoning
- Peer comparison injected — compares against sector peers using `_SECTOR_PEERS` mapping
- Every proposal stored in `action_proposals_core` with full audit trail

### 5. Multi-Agent Debate

- Only fires for proposals with confidence ≥ 0.5 (filters noise)
- **3 parallel LLM calls**:
  - 🟢 Bull analyst — argues for the opportunity
  - 🔴 Bear analyst — argues against
  - ⚠️ Risk analyst — flags tail risks
- **1 sequential judge call** — weighs all three, decides: approve or reject
- Approved proposals shown to user
- Rejected proposals stored as `status='debate_rejected'` — fully auditable
- Judge errors default to `approved=True` — never silently blocks
- All artifacts stored in `proposal_debate_artifacts` table

### 6. Cascade Analysis

- Auto-fires after every new proposal
- Checks second-order impact on other portfolio positions
- Uses `relationships_core` table + `_SECTOR_PEERS` mapping
- Example: TSMC weak guidance → flags NVDA, INTC exposure in user's portfolio
- Cascade proposals linked back to original trigger

### 7. Earnings Transcript Analysis

- Extracts earnings call transcript text
- LLM analyzes: revenue surprise, EPS beat/miss, guidance direction (raised/maintained/lowered), management tone (confident/neutral/defensive), key risks
- Stored in `earnings_analysis_core` table
- Designed for trend tracking — compare consecutive quarters
- Detects deterioration: guidance raised → maintained → lowered across 2+ quarters

### 8. Reflexion Learning (Outcome Measurement)

- Background thread every 6 hours (60s startup stagger)
- Measures actual outcomes of past proposals
- When outcomes miss → generates reflexion rule
- Rules stored in `reflexion_notes_core` table
- Active rules injected into every future AI evaluation
- System tells itself "last time I overreacted to this signal type, require stronger confirmation"
- Poor-performing rules can auto-expire

### 9. Live Macro Dashboard

- Background thread refreshes every 2 minutes
- Pulls real-time prices via `yfinance` for 18 key indicators:
  - Commodities: oil (CL=F), gold (GC=F)
  - Volatility: VIX
  - Bonds: 10Y yield, 2Y yield, TLT
  - Indices: S&P 500, Nasdaq, Dow, Russell 2000
  - Crypto: BTC, ETH
  - FX: DXY, EUR/USD, USD/JPY
- Snapshot injected into every AI query — LLM always knows current market conditions

### 10. News Headline Search

- Background thread refreshes every 5 minutes
- Scrapes RSS feeds: Yahoo Finance, CNBC, MarketWatch, Reuters
- Scores headlines against user query
- Real-time query detection via pattern matching ("why is oil up today?")
- Headlines injected into AI context as `live_news_text`

### 11. Workspace OS

- **Calendar**: DB records with due dates + earnings dates + Google Calendar events (blue dots)
- Click any day → Quick Capture opens with date pre-filled
- **Tasks**: Postgres-backed todos with check/uncheck, priority levels
- **Projects**: grouped records with emoji + color coding
- **Hub**: 160+ curated research links in 18 collapsible categories with search and favicons
- **Activity feed**: unified view of all platform actions
- **Quick Capture**: modal for any record type with `$TICKER` auto-detection

### 12. Google Calendar Integration

- OAuth 2.0 connect/disconnect flow
- Fetches monthly events via Google Calendar API
- Events shown as blue dots on calendar
- Connection status visible on `/day` page
- Token auto-refreshes

### 13. AI Chat Interface

- Conversational AI with full portfolio context
- Every query receives: holdings, watchlist, macro snapshot, news, recent proposals
- 38-second sync budget for deep analysis
- Response caching (5 min TTL) to reduce API calls
- Orchestrator ensures LLM uses live data

---

## Background Threads

| Thread | Interval | Purpose |
|--------|----------|---------|
| `market-snapshot-refresh` | 3 min | Market data for dashboard |
| `macro-snapshot-refresh` | 2 min | 18 macro tickers via yfinance |
| `news-headlines-refresh` | 5 min | RSS from 4 news sources |
| `sec-edgar-poll` | 6 hours | EDGAR filings for held + universe tickers |
| `form4-insider-poll` | 24 hours | Insider trade Form 4 filings |
| `outcome-measurement` | 6 hours | Proposal outcome tracking + reflexion rules |

---

## Infrastructure

| Component | Choice | Why |
|-----------|--------|-----|
| Runtime | Google Cloud Run | Serverless, scales to zero, no ops |
| Database | Cloud SQL PostgreSQL | Strict Postgres only, no SQLite fallback |
| AI (Cloud) | Gemini via Vertex AI | Service account auth, no API key to leak |
| AI (Local) | Ollama + Llama | Free, runs locally, no API key needed |
| Secrets | Google Secret Manager | POSTGRES_DSN, OPENAI_API_KEY |
| Auth | Service account IAM | `investor-tools-runtime` with minimal roles |
| Container | Single Docker image | One image, multiple entrypoints (app/worker) |
| CI | GitHub Actions | Lint + smoke test on push to main |
| Deploy | `bin/deploy_cloud_run.sh` | Builds, pushes, deploys app + worker |
| Rollback | `bin/rollback_cloud_run.sh` | Redeploy any previous image tag |

---

## Security

- **No API keys in production** — Gemini accessed via Vertex AI service account (IAM-based)
- **Secrets in Secret Manager** — never in code or environment files
- **Private Cloud Run service** — `--no-allow-unauthenticated`
- **Minimal IAM roles** — service account only has: secretmanager.secretAccessor, cloudsql.client, artifactregistry.reader, logging.logWriter, monitoring.metricWriter, aiplatform.user
- **Billing budget alerts** — set to notify at threshold
- **SEC EDGAR only** — no paid data APIs (no Bloomberg, FMP, Finnhub)

---

## Data Sources (All Free / No-Cost)

| Source | What | How |
|--------|------|-----|
| SEC EDGAR | Filings (8-K, 10-Q, 10-K, 6-K, 20-F, Form 4) | Direct HTTP polling |
| Yahoo Finance (yfinance) | Real-time prices, macro indicators | Python library |
| Yahoo Finance RSS | News headlines | RSS feed scraping |
| CNBC RSS | News headlines | RSS feed scraping |
| MarketWatch RSS | News headlines | RSS feed scraping |
| Reuters RSS | News headlines | RSS feed scraping |
| Google Calendar API | User's calendar events | OAuth 2.0 |
| Google Gmail API | Email integration | OAuth 2.0 |

---

## Key Files

| File | Purpose |
|------|---------|
| `app/main.py` | App factory, startup schema, background threads |
| `app/services/proactive_ai_service.py` | Proposal engine, signal evaluation, cascades, reflexion |
| `app/services/sec_edgar_poller_service.py` | SEC EDGAR polling, Form 4, signal universe |
| `app/services/events_service.py` | Event bus (sec_filing / universe_signal / insider_trade) |
| `app/services/debate_service.py` | Multi-agent debate (bull/bear/risk/judge) |
| `app/services/earnings_transcript_service.py` | Earnings analysis + LLM layer |
| `app/services/web_search_service.py` | Live macro snapshot + RSS news search |
| `app/services/workspace_os_service.py` | Workspace OS backend (calendar, records, projects) |
| `app/services/google_workspace_service.py` | Google Calendar/Gmail OAuth + API |
| `app/templates/workspace_os_day.html` | Day view UI (calendar, tasks, hub, AI panel) |
| `app/routers/workspace_os.py` | Workspace API endpoints |
| `tools/llm_engine.py` | AI engine (Gemini, OpenAI, Anthropic, Groq, Ollama) |
| `bin/deploy_cloud_run.sh` | Cloud Run deployment script |
| `bin/deploy_worker_job.sh` | Cloud Run scheduled jobs deployment |

---

*Last updated: March 2026*
