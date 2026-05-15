# Session Handoff - 2026-02-20 (Latest)

## Scope Completed
- Dashboard redesign implemented in:
  - `app/templates/dashboard.html`
  - `app/templates/components/dashboard_body.html`
- Added company names to `52-Week & All-Time Lows` via backend enrichment in:
  - `app/routers/dashboard.py`
- Action Proposals UI improved:
  - Progressive reject input reveal
  - Cleaner meta chips
  - Raw `kind` chip removed from card UI
- Morning report converted from text wall to insight-chip grid
- Live news marquee removed; calmer rotating headline behavior added
- Earnings rows refactored to strict grid and mobile-specific compact layout

## AI Insight Direction (No Hardcoding)
- Removed forced `Risk sizing: ...` bullet injection in proposals.
- Added model-driven insight extraction for proposal cards in:
  - `app/services/proactive_ai_service.py`
- Proposal cards now render insight blocks (`label + text`) before evidence bullets.

## Live Pipeline Tests Run
### MSFT Patient Zero
- Real filing copied into `filing_docs/` and inserted into DB.
- Pipeline run completed and produced dynamic proposals.
- Verified proposals for MSFT with Blue Chip badge classification in formatter output.

### AAPL Patient Zero
- Same end-to-end flow executed.
- New AAPL proposals inserted dynamically.
- Verified side-by-side dynamic card generation behavior in DB/formatter output.

## Bug Found + Fixed
- Ingestion failure: `UNIQUE constraint failed: entities.id_text`
- Fix applied in:
  - `app/services/sec_ingest_pipeline_service.py`
- `_entity_id()` now writes deterministic `id_text`, with backfill for legacy empty values.

## Last In-Progress Change (Important)
- Began patch to parse SEC `full-submission.txt` properly by extracting primary `<DOCUMENT>` body before section parsing.
- File touched:
  - `app/services/sec_ingest_pipeline_service.py`
- Work was interrupted mid-session by usage limit.
- Resume from these functions:
  - `_extract_primary_doc_from_submission(...)`
  - `_read_filing_text(path_s, form=...)`
  - caller update using `form=form` when reading filing text

## Verification Status
- Multiple app restarts attempted during session; some background launch attempts were flaky.
- Foreground runs showed server startup logs and functional endpoints at points during session.
- Final recommendation: run one clean restart and re-verify:
  - `/dashboard`
  - `/api/sec/ingest-new-filings`

## Next Step Checklist
1. Re-open `app/services/sec_ingest_pipeline_service.py` and confirm the primary-document extraction patch is complete and syntactically valid.
2. Run compile/smoke checks for touched Python files.
3. Execute one rich filing test (CRM or similar full submission) through API endpoint.
4. Confirm proposal insight quality improves (product/margin/risk content) and appears in dashboard cards.
5. Optional cleanup: remove remaining inline style fragments in `dashboard_body.html` for full token consistency.
