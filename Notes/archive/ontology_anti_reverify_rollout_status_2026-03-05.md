# Ontology Phase: Decay-Aware Anti-Relationship Re-Verification

Date: 2026-03-05
Environment: Local + Cloud Run (`onyx-terminal-487613`, `europe-west1`)

## Implemented
- Added stale anti-relationship re-verification queue schema:
  - table: `anti_relationship_reverify_queue_core`
  - indexes on status/priority and relationship id
- Added queue enqueuer logic for protective edges (`HEDGES_AGAINST`, `IMMUNE_TO`):
  - `enqueue_stale_anti_relationships()` in `app/services/ontology_quality_service.py`
  - enqueue conditions:
    - stale by age (`last_verified_at` older than threshold)
    - or low effective confidence (below threshold)
- Added feature controls via env:
  - `ONTOLOGY_ANTI_REVERIFY_ENABLED` (default `1`)
  - `ONTOLOGY_ANTI_REVERIFY_STALE_DAYS` (default `180`)
  - `ONTOLOGY_ANTI_REVERIFY_MIN_CONFIDENCE` (default `0.65`)
  - `ONTOLOGY_ANTI_REVERIFY_RETIRE_CONFIDENCE` (default `0.35`)
  - `ONTOLOGY_ANTI_REVERIFY_MAX_ATTEMPTS` (default `3`)
  - `ONTOLOGY_ANTI_REVERIFY_RECENT_DAYS` (default `120`)
- Integrated enqueue into decay refresh pipeline:
  - `refresh_relationship_decay_scores()` now returns `anti_reverify` section
  - when `--apply` is used, stale anti edges are queued automatically
- Added queue processor logic:
  - `process_anti_reverify_queue()` in `app/services/ontology_quality_service.py`
  - decision outcomes: `refresh`, `retire`, `needs_review`
  - `refresh` updates `last_verified_at` and `effective_confidence`
  - `retire` sets `status='inactive'` and stamps `valid_to`

## New scripts/jobs
- New tool:
  - `tools/enqueue_stale_anti_relationships.py`
- New tool:
  - `tools/process_anti_reverify_queue.py`
- New Cloud Run job entrypoint:
  - `bin/run_anti_reverify_enqueue_job`
- New Cloud Run job entrypoint:
  - `bin/run_anti_reverify_worker_job`
- New scheduled cloud job:
  - job: `investor-anti-reverify-job`
  - schedule: `25 2 * * *` UTC
  - command: `/app/bin/run_anti_reverify_enqueue_job`
- New scheduled cloud job:
  - job: `investor-anti-reverify-worker-job`
  - schedule: `40 2 * * *` UTC
  - command: `/app/bin/run_anti_reverify_worker_job`

## Deploy updates
- Added anti-reverify env vars to cloud app/worker deploy env strings:
  - `bin/deploy_cloud_run.sh`
  - `bin/deploy_worker_job.sh`
- Added executable permission hardening for new runner scripts in image build:
  - `Dockerfile`

## Verification
- Local dry-run enqueue: success.
- Local decay refresh dry-run includes `anti_reverify` section: success.
- Cloud app deployed: revision `investor-tools-app-00212-l99` serving 100%.
- Cloud health (`/health/ready`, authenticated): `db_ok=true`.
- Cloud env confirms anti-reverify vars are present.
- Cloud anti-reverify job manual execution succeeded:
  - execution: `investor-anti-reverify-job-brd5d`
  - status: `Completed=True`
- Cloud anti-reverify worker job manual execution succeeded:
  - execution: `investor-anti-reverify-worker-job-csxpl`
  - status: `Completed=True`

## Files changed
- `app/services/ontology_quality_service.py`
- `tools/enqueue_stale_anti_relationships.py` (new)
- `tools/process_anti_reverify_queue.py` (new)
- `bin/run_anti_reverify_enqueue_job` (new)
- `bin/run_anti_reverify_worker_job` (new)
- `tools/refresh_relationship_decay_scores.py` (output includes anti queue details via service)
- `bin/deploy_cloud_run.sh`
- `bin/deploy_worker_job.sh`
- `Dockerfile`
