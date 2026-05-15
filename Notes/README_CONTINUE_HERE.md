# Continue Here

Canonical navigation: `NOTES_INDEX.md`

If a new AI agent or developer is continuing this project, start in this order:

1. `NOTES_INDEX.md`  
   Single map of active vs archived notes.

2. `APP_STATUS_2026-03-07.md`
   Latest current-state snapshot (local/cloud status, `/today` code structure, backups).

3. `AGENT_CONTINUATION_HANDOFF.md`  
   Primary current-state handoff with exact runbook and verified outputs.

4. `TECHNICAL_CHANGELOG.md`  
   File-level technical changes and behavior updates.

5. `RECENT_ARCHITECTURE_2026-02-21.md`  
   Current runtime architecture and AI flow.

6. `FUNDAMENTAL_ANALYSIS_OPERATOR_MAP.md`
   End-to-end operator runbook for dashboard fundamental insights.

7. `CLOUD_READINESS_CHECKLIST.md`  
   Deployment and validation checklist for cloud path.

8. `../docs/CLOUD_GO_LIVE_CHECKLIST.md`
   Strict cloud go-live pass/fail checklist and runbook.

## Quick Validation Commands
```bash
curl -sS http://127.0.0.1:8766/health/live
curl -sS http://127.0.0.1:8766/health/ready
curl -sS http://127.0.0.1:8766/ai/worker/health
curl -sS -X POST http://127.0.0.1:8766/ai/migration/bootstrap
./bin/cloud_preflight
```

Expected in non-dev with admin disabled:
- live: `ok:true`
- ready: `core_db_backend:"postgres"` and `db_ok:true`
- worker health: `ok:true`
- migration bootstrap: `{"detail":"admin_api_disabled"}`
- cloud preflight: all `PASS` (fails if required env like `AI_REDIS_URL` is missing)


## 2026-02-22 Refresh
- Include `./bin/cloud_preflight` in standard continuation validation sequence.

## 2026-02-23 Refresh
- Added root navigation index: `START_HERE.md`.
- New cloud execution docs:
  - `docs/GCP_CUTOVER_RUNBOOK.md`
  - `docs/GCP_CUTOVER_RUNBOOK_FILLED.md` (copy/paste ready)
- New AI data-first enforcement:
  - runtime guard in `app/core/ai_data_policy.py` + `tools/llm_engine.py`
  - CI gate `bin/check_ai_data_policy.py` integrated into `./bin/quality_gate.sh`
- Company intelligence now auto-refreshes on filing ingestion (guarded by staleness/form filters).
- Financial integrity automation added:
  - `automations/v2_financial_integrity.sh`
  - `bin/run_financial_integrity`

## 2026-02-24 Refresh
- Runtime hardening completed for this pass:
  - Company lookup market-cap path is Postgres-only.
  - Events runtime service is Postgres-only.
  - `/ai/command` sync timeout no longer blocks/hangs; clean queued fallback now.
- Continuation priority now:
  1. Remove remaining non-critical SQLite references in low-priority/legacy paths.
  2. Keep async-first AI flow as default UX contract.
  3. Maintain strict validation (`py_compile`, health, dashboard, ai command smoke) after each change set.

## 2026-02-24 Notes Cleanup
- Notes were consolidated.
- Overlapping legacy docs moved to `Notes/archive/` (not deleted).
- Use `NOTES_INDEX.md` as the canonical notes map.

## 2026-03-07 Refresh
- Added `APP_STATUS_2026-03-07.md` as the fastest current-state entry point.
- Updated continuation priority to include the latest `/today` route-service-template mapping and backup traceability.
- Archived stale planning docs (`NEXT_7_DAYS_PLAN.md`, `PHASE3_PHASE4_EXECUTION_PLAN_LOCAL_FIRST.md`) to `Notes/archive/`.
