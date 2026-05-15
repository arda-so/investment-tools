# Spaghetti Refactor Tickets (Safety-First)

Date: 2026-03-09  
Objective: Minimum risk, maximum safety, measurable structural improvement.

Execution rules:
- No behavior-changing refactors in batch 1.
- One hotspot per PR.
- Must pass compile + smoke checks before merge.
- Keep rollback path straightforward.

## Phase 0: Guardrails (No Runtime Logic Change)

### T0.1 Import cycle regression test
- Add a CI/static check that fails if SCC cycle count increases.
- Baseline cycles allowed initially: 3.
- Success metric: no new cycles introduced.

### T0.2 Function-size lint baseline
- Add static report in CI that flags:
  - new functions > 120 lines
  - new modules > 1200 LOC
- Start non-blocking (warning), then enforce for new/changed files.

### T0.3 Silent-exception visibility
- Create mechanical rule for core modules:
  - replace `except Exception: pass` with minimal logging in high-traffic paths.
- Start with router + orchestration layers only.

## Phase 1: Cycle Breaks (High Value, Low Blast Radius)

### T1.1 Break orchestrator SCC
Modules:
- `app/services/ai_orchestrator.py`
- `app/services/orchestrator/command_parser.py`
- `app/services/orchestrator/payload_executor.py`
- `app/services/orchestrator/routing_rules.py`

Approach:
- Extract pure helper utilities to `app/services/orchestrator/common.py`
- Remove cross-calls from parser -> orchestrator implementation internals.

Success:
- SCC removed for this cluster.

### T1.2 Break organizer/agent SCC
Modules:
- `app/services/agent_service.py`
- `app/services/organizer_service.py`

Approach:
- Extract shared memory-write adapter into `app/services/organizer_agent_bridge.py`

Success:
- Direct mutual imports removed.

### T1.3 Break daily_operator/portfolio_memory SCC
Modules:
- `app/services/daily_operator.py`
- `app/services/portfolio_memory_service.py`

Approach:
- Move shared payload formatting to `app/services/memory_context_service.py`

Success:
- Direct mutual imports removed.

## Phase 2: God-Module Decomposition (Controlled)

### T2.1 Split `app/routers/dashboard.py`
Target split:
- `dashboard_workspace_routes.py`
- `dashboard_classic_routes.py`
- `dashboard_api_routes.py`
- `dashboard_view_models.py`

Safety:
- Keep route URLs and response contracts unchanged.
- Use import-only move first, no logic rewrites.

### T2.2 Split `app/services/proactive_ai_service.py`
Target split:
- `proactive_proposals.py`
- `proactive_cascade.py`
- `proactive_monitoring.py`
- `proactive_reflexion.py`

Safety:
- Keep public function names in facade module for compatibility.

### T2.3 Split `app/services/ai_orchestrator.py`
Target split:
- `orchestrator_intent.py`
- `orchestrator_routing.py`
- `orchestrator_execution.py`
- `orchestrator_quality.py`

Safety:
- Preserve `run_ai_command()` signature.

### T2.4 Split `app/services/postgres_core_service.py`
Target split:
- `postgres_schema.py`
- `postgres_company_ops.py`
- `postgres_portfolio_ops.py`
- `postgres_queue_ops.py`

Safety:
- Keep `postgres_core_service.py` as compatibility facade during migration.

## Phase 3: Long Function Refactors (Pure Extraction)

### T3.1 Extract helpers from `app/main.py::create_app`
- Move startup schema registration and thread bootstrapping to helper modules.

### T3.2 Extract `api_global_search` in dashboard router
- Separate parsing/ranking/building into isolated pure functions.

### T3.3 Extract monolithic schema builders
- `ensure_postgres_core_schema` split by domain table groups.

## Phase 4: Silent Exception Reduction

### T4.1 Core paths only
Scope:
- `app/routers/dashboard.py`
- `app/services/proactive_ai_service.py`
- `app/services/ai_orchestrator.py`
- `app/services/portfolio_memory_service.py`

Rule:
- Replace silent pass with log-once utilities for expected degradations.

Success:
- Reduce silent `except ...: pass` in these files by >= 40%.

## Validation Gate for Every Ticket

1. `py_compile` on changed modules.
2. App boot smoke:
- `/health/live`
- `/health/ready`
3. Critical route smoke:
- `/dashboard`
- `/dashboard/classic`
- `/today`
- `/company_file?t=NVDA`
4. No diff in API response shape for touched endpoints (unless explicitly intended).

## Priority Order (Best Risk/Return)
1. T0.1, T0.2, T0.3
2. T1.1, T1.2, T1.3
3. T2.1
4. T2.2
5. T2.3
6. T2.4
7. T3.x + T4.1

