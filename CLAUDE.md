# Investment_Tools — Root Project

This is the root of the Investment_Tools workspace.
Two agent workflows live here, each with its own CLAUDE.md:

| Agent | Path | Purpose |
|-------|------|---------|
| Weekly Scanner | `agents/weekly/` | S&P 100 SEC filing digest + market macro snapshot |
| Deep Comparison | `agents/deep/` | 10-year single-ticker change report |

## Repo Layout

```
Investment_Tools/
├── agents/
│   ├── weekly/          # Weekly Scanner agent (run claude from here)
│   └── deep/            # Deep Comparison agent (run claude from here)
├── filing_docs/         # Downloaded SEC filings (.txt)
├── tools/               # Shared Python scripts
├── outputs/
│   ├── weekly/          # Weekly Scanner outputs
│   └── deep/            # Deep Comparison outputs (per ticker)
└── data/                # Reference data (focus lists, universes, etc.)
```

## How to Run

Weekly Scanner:
```bash
cd /Users/solmaz/Investment_Tools/agents/weekly && claude
```

Deep Comparison:
```bash
cd /Users/solmaz/Investment_Tools/agents/deep && claude
```
