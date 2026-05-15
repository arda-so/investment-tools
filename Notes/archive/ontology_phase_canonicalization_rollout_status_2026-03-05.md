# Ontology Phase: Canonicalization Rollout Status

Date: 2026-03-05
Environment: Local + Cloud Run (`onyx-terminal-487613`, `europe-west1`)

## Implemented
- Added deterministic-first canonicalization service:
  - `app/services/entity_resolution_service.py`
  - Creates/maintains:
    - `entity_resolution_queue_core`
    - `entity_canonical_map_core`
  - Deterministic rules:
    - same ticker / same CIK
    - same normalized name
    - legal-suffix-stripped company key match
  - Safe merge behavior:
    - dedupe conflicting edges first
    - remap `relationships_core` source/target from duplicate -> canonical
    - final duplicate cleanup
    - delete duplicate entity
    - record canonical mapping

- Added LLM-judge queue processor (async style):
  - queue rows in `pending`
  - conservative JSON verdict (`same_company`, `canonical`, `confidence`, `rationale`)
  - guarded by `ONTOLOGY_CANONICAL_LLM_ENABLED`
  - low-confidence or uncertain pairs are rejected, not merged

- Added canonicalization runner script:
  - `tools/run_entity_canonicalization.py`
  - supports dry-run and apply modes

- Added cloud/local job entrypoint:
  - `bin/run_entity_canonicalization_job`

- Wired schema bootstrap into ingest schema ensure:
  - `app/services/sec_ingest_pipeline_service.py`

- Added scheduled cloud job deployment:
  - job: `investor-entity-canonicalization-job`
  - cron: `20 2 * * *` UTC
  - command: `/app/bin/run_entity_canonicalization_job`
  - deployed via `bin/deploy_worker_job.sh`

## Safety Hardening
- Cloud worker service remains disabled by default (`DEPLOY_WORKER=0` already in place).
- Jobs are digest-pinned via tag->digest resolution in deploy script.
- LLM judge is opt-in via env flag; deterministic pass runs regardless.

## Verification
- Local dry-run (Postgres env): success.
- Local apply run (Postgres env): success.
- Cloud app deploy: revision `investor-tools-app-00209-s6q` live.
- Cloud app health (`/health/ready`, authenticated): `db_ok=true`.
- Cloud job deployed and manual execution successful:
  - execution: `investor-entity-canonicalization-job-547qm`
  - status: `Completed=True`

## Files Changed
- `app/services/entity_resolution_service.py` (new)
- `tools/run_entity_canonicalization.py` (new)
- `bin/run_entity_canonicalization_job` (new)
- `app/services/sec_ingest_pipeline_service.py`
- `bin/deploy_worker_job.sh`
- `bin/deploy_cloud_run.sh` (already hardened in prior phase)

