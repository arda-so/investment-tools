# Long-Term Automation (Thesis / Quarterly / Valuation)

## What Was Added

- `research_agent.py thesis ...` for thesis tracking and health checks.
- `research_agent.py quarterly ...` for latest 10-Q/10-K delta analysis.
- `research_agent.py valuation ...` for valuation band monitoring.
- `automations/long_term_weekly.sh` to run all three automatically.
- `bin/run_longterm` shortcut command.

## Quick Commands

```bash
cd /Users/solmaz/Investment_Tools
./bin/mode strict
./bin/run_longterm
```

## Thesis Tracker Examples

Set a thesis:

```bash
python3 research_agent.py thesis set AAPL \
  --summary "Services mix expansion + buybacks can sustain high FCF compounding." \
  --horizon "3-5y" \
  --kpi "Services gross margin" \
  --kpi "Net cash trend" \
  --fv-low 185 --fv-high 245 \
  --buy-below 190 --trim-above 250 \
  --invalidate-on "sustained services deceleration, negative FCF trend, balance-sheet stress"
```

Check health:

```bash
python3 research_agent.py thesis check AAPL
python3 research_agent.py thesis check --all
```

Expected output shape:

```text
## AAPL — ON TRACK
- Thesis: Services mix expansion + buybacks can sustain high FCF compounding.
- Invalidation rules: sustained services deceleration, negative FCF trend, balance-sheet stress
- Trigger matches: none
- Most recent filing changes:
  - 10-Q 2026-01-29 | mda | modified
```

## Quarterly Analyzer Examples

Run all:

```bash
python3 research_agent.py quarterly --all --lookback 4
```

Run one:

```bash
python3 research_agent.py quarterly AAPL --lookback 4
```

Expected output shape:

```text
## AAPL
- Latest filing: 10-Q on 2026-01-29
- Previous filing: 10-K on 2025-11-01
- Focus changes detected: 3
- Key deltas:
  - mda (modified): Size: +6% (...); New keywords: restructuring
  - debt_liquidity (modified): Words added: ...
```

## Valuation Monitor Examples

Use market prices:

```bash
python3 research_agent.py valuation --all --allow-broader-sources
```

Use manual price (strict, single ticker):

```bash
python3 research_agent.py valuation AAPL --price 201.35 --as-of 2026-02-11
```

Expected output shape:

```text
| Ticker | Price | Fair Value Band | Status | Upside to Mid |
| AAPL   | 201.35 | 185.00 - 245.00 | HOLD | +6.8% |
| KO     | 58.40  | 55.00 - 68.00   | HOLD | +5.3% |
```

## Automated Weekly Package Outputs

Running `./bin/run_longterm` generates:

- `reports/thesis_check_YYYYMMDD.md`
- `reports/quarterly_longterm_YYYYMMDD.md`
- `reports/valuation_monitor_YYYYMMDD.md`
- log: `logs/long_term_weekly.log`
