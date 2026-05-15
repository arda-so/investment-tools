# Spaghetti Cleanup Execution Plan (Safety-First)

Date: 2026-03-09  
Owner: Engineering

## Objective
Clean structural spaghetti without breaking behavior in local or cloud environments.

## Current Baseline
1. Import cycles: `0` (clean).
2. Python files scanned: `164`.
3. Files over 1200 LOC: `16`.
4. Functions over 200 LOC: `34`.
5. `fetchall()` occurrences across app/services/routers/tools: ~`290`.

## Risk Summary
1. Behavior drift from large-file extraction.
2. Hidden coupling in large modules.
3. Endpoint response regressions.
4. Performance regressions from query/loop changes.
5. SQLite/Postgres parity regressions.

## Guardrails (Non-Negotiable)
1. No schema migrations in cleanup slices.
2. No route/path changes.
3. No template/UI redesign.
4. No business-rule changes.
5. Every slice must keep wrapper compatibility.
6. Stop and rollback the slice on first regression.

## Execution Strategy
Use micro-slices. Each slice is independent and reversible.

For each slice:
1. Extract one helper/module only.
2. Keep original entrypoint function in place as wrapper.
3. Run compile + smoke + architecture checks.
4. Commit/record before next slice.

## Tomorrow Micro-Slice Plan

### Group A (Lowest Risk): `dashboard.py` helpers
Scope:
1. Extract parser/filter/format helpers into `app/routers/dashboard_helpers.py`.
2. Keep existing router handlers unchanged except internal calls.

Validation:
1. `/dashboard`
2. `/dashboard/classic`
3. key dashboard API search endpoints

### Group B (Low Risk): `portfolio_memory_service.py` query helpers
Scope:
1. Extract repeated cursor iteration and simple query transform helpers into `app/services/portfolio_query_utils.py`.
2. Replace only repetitive internal loops.

Validation:
1. proactive prompt path
2. recent memory summaries
3. portfolio context read path

### Group C (Medium Risk): `ai_orchestrator.py` constants/types split only
Scope:
1. Move constants/types only (no logic rewrite).
2. Keep orchestration function bodies unchanged.

Validation:
1. ticker parsing
2. intent routing smoke path
3. fallback reply path

### Group D (Medium Risk): `postgres_core_service.py` schema helper split
Scope:
1. Extract schema DDL chunks into dedicated helper module.
2. Keep `ensure_postgres_core_schema()` public behavior unchanged.

Validation:
1. schema ensure call returns ok
2. key core table presence checks

### Group E (High Risk, Last): `tools/terminal_app.py`
Scope:
1. Only after A-D are stable.
2. Extract utility/render helpers in tiny sections.
3. No endpoint behavior changes.

Validation:
1. terminal app page load
2. top 3 routes used daily

## Daily Timeline Template (Tomorrow)
1. 09:00-09:30: baseline run + record.
2. 09:30-11:00: Group A.
3. 11:00-11:30: validation A.
4. 13:00-14:30: Group B.
5. 14:30-15:00: validation B.
6. 15:00-16:00: report + next-slice prep.

## Mandatory Validation Commands
1. `.venv-memory/bin/python -m py_compile <changed_files>`
2. `.venv-memory/bin/python bin/check_import_cycles.py`
3. `.venv-memory/bin/python bin/check_code_structure.py --baseline config/architecture_baseline.json`

## Rollback Protocol
1. One slice per commit/checkpoint.
2. Revert only failing slice.
3. Keep successful slices untouched.
4. Re-run full validation after rollback.

## Completion Criteria
1. No regressions in core pages.
2. Import cycles stay `0`.
3. At least two hotspot files simplified safely.
4. Updated changelog + notes index.

