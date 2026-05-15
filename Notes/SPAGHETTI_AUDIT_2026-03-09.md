# Spaghetti Code Audit (2026-03-09)

Scope audited: `app/` + `tools/` Python code.

Method:
- File size and function length scan (AST-based)
- Local-import density scan (cycle workaround signal)
- Import graph SCC cycle detection
- Broad exception / silent pass scan
- Coupling hotspot scan

## Executive Summary
- The app is functional, but core architecture still has **high maintainability risk**.
- Highest risk is concentrated in a small set of very large modules and long functions.
- Circular dependency pressure is still present and confirmed by SCC cycles.
- Error handling is over-defensive (`except ...: pass`), reducing observability and increasing hidden-failure risk.

Risk grade:
- Runtime stability today: `Medium`
- Change/regression risk: `High`
- Long-term velocity risk: `High`

## Key Metrics
- Python files scanned: `162`
- Total LOC scanned (`app` + `tools`): `95,829`
- Files >= 1,000 LOC: `19`
- Functions >= 100 LOC: `129`
- Functions >= 200 LOC: `33`
- Broad catch blocks (`except Exception`/`except`): `1,474`
- Silent pass blocks (`except ...: pass`): `1,226`
- Files with >= 10 local imports: `10`

## P0 Hotspots (Immediate Structural Risk)

1. God modules
- `tools/terminal_app.py` (21,333 LOC)
- `app/services/ai_orchestrator.py` (6,550 LOC)
- `app/services/postgres_core_service.py` (6,183 LOC)
- `app/services/proactive_ai_service.py` (4,734 LOC)
- `app/routers/dashboard.py` (4,374 LOC)

2. Longest high-risk functions
- `tools/terminal_app.py::do_POST` (~1668 lines)
- `tools/terminal_app.py::dashboard_html` (~1245 lines)
- `app/services/postgres_core_service.py::sync_core_from_sqlite` (~1205 lines)
- `app/services/postgres_core_service.py::ensure_postgres_core_schema` (~1050 lines)
- `app/main.py::create_app` (~487 lines)
- `app/routers/dashboard.py::api_global_search` (~435 lines)

3. Local-import heavy modules (cycle workaround smell)
- `app/routers/workspace_os.py` (`63` local imports)
- `app/main.py` (`21` local imports)
- `app/routers/dashboard.py` (`20` local imports)
- `app/services/financial_lab_service.py` (`17` local imports)
- `app/services/portfolio_memory_service.py` (`12` local imports)
- `app/services/proactive_ai_service.py` (`11` local imports)

4. Confirmed import cycles (SCC)
- `app.services.ai_orchestrator`
  - `app.services.orchestrator.command_parser`
  - `app.services.orchestrator.payload_executor`
  - `app.services.orchestrator.routing_rules`
- `app.services.agent_service` <-> `app.services.organizer_service`
- `app.services.daily_operator` <-> `app.services.portfolio_memory_service`

## P1 Hotspots (Medium Risk)

1. Coupling concentration
- `app/services/postgres_core_service.py` has highest inbound coupling in services.
- `app/services/proactive_ai_service.py` has high in/out coupling.
- `app/services/ai_orchestrator.py` has high outbound coupling.

2. Duplicate pattern clusters (signal)
- `playbook_service.py` <-> `research_thread_service.py`
- `proactive_ai_service.py` <-> `sec_ingest_pipeline_service.py`
- `autonomic_service.py` <-> `thinking_service.py` / `work_graph_service.py`

Note: duplicate-window scans overestimate generic boilerplate; treat as triage signals, not proof.

## P2 Hotspots (Lower Risk, Worth Cleanup)
- Legacy terminal surface (`tools/terminal_app.py`) is still monolithic.
- Router layer does business/data orchestration that should sit in service/use-case layers.
- Sparse structural tests for import boundaries and module size drift.

## Why This Matters
- The current codebase can continue shipping features, but each high-impact change has elevated blast radius.
- Regression probability increases around dashboard/orchestration/database touchpoints.
- Debug time remains high due to broad silent exception handling.

## Recommendation
- Apply refactor in **phased, no-behavior-change slices** with strict validation gates.
- Do **not** run broad rewrites.
- Execute the ticket plan in `SPAGHETTI_REFACTOR_TICKETS_2026-03-09.md`.

