# Master System + AI Document (2026-02-21)

This is the single entry-point document for your architecture, coding standards, and AI training materials.

## Start Here
- Technical architecture: `RECENT_ARCHITECTURE_2026-02-21.md`
- Executive summary: `ARCHITECTURE_EXEC_SUMMARY_2026-02-21.md`
- Architecture diagram: `ARCHITECTURE_DIAGRAM_2026-02-21.md`

## Engineering Standards
- Coding style guide: `CODING_STYLE_GUIDE_2026-02-21.md`
- Important system rules: `IMPORTANT_SYSTEM_RULES_2026-02-21.md`

## AI Materials
- AI training materials: `AI_TRAINING_MATERIALS_2026-02-21.md`

## Existing System Overview
- System overview (cross-linked): `SYSTEM_OVERVIEW.md`

## Scope Covered
- Current app architecture (HTTP path + async worker path)
- Queue/worker model, SSE behavior, and Map/Reduce flow
- Postgres/SQLite split and migration notes
- AI prompting/governance principles (no hardcoded analysis logic)
- Reliability/observability and operational checklists

## Suggested Reading Order
1. `ARCHITECTURE_EXEC_SUMMARY_2026-02-21.md`
2. `ARCHITECTURE_DIAGRAM_2026-02-21.md`
3. `RECENT_ARCHITECTURE_2026-02-21.md`
4. `IMPORTANT_SYSTEM_RULES_2026-02-21.md`
5. `CODING_STYLE_GUIDE_2026-02-21.md`
6. `AI_TRAINING_MATERIALS_2026-02-21.md`


## 2026-02-22 Refresh
- Master index now effectively superseded by latest go-live controls in `docs/CLOUD_GO_LIVE_CHECKLIST.md` and `Notes/AGENT_CONTINUATION_HANDOFF.md` for active operations.

## 2026-02-24 Refresh
- Runtime architecture tightened further:
  - `company_lookup_service.market_cap_map()` now Postgres-only.
  - `events_service` runtime flow now Postgres-only.
  - `/ai/command` timeout path is non-blocking and queues immediately.
- This keeps the system aligned with:
  - Async-first AI UX
  - Strict Postgres runtime policy
  - No silent fallback behavior in active request paths
