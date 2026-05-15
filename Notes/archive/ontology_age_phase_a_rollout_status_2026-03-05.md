# Ontology Phase: Apache AGE Pilot Scaffolding (Phase A)

Date: 2026-03-05
Environment: Local + Cloud Run (`onyx-terminal-487613`, `europe-west1`)

## Implemented
- Expanded AGE service with safe diagnostics and graph-sync primitives:
  - `age_status()`
  - `ensure_age_graph_schema(apply=...)`
  - `sync_age_projection(apply=..., limit=...)`
- Added AGE preflight CLI:
  - `tools/age_preflight.py`
- Added AGE projection sync CLI:
  - `tools/age_sync_projection.py`
- Added cloud/local runner:
  - `bin/run_age_sync_job`
  - hard-gated by `ONTOLOGY_AGE_SYNC_ENABLED=1` (default off)
- Health readiness now includes AGE state fields:
  - `age_ok`
  - `age_available`
  - `age_graph`

## Cloud/Deploy Wiring
- Added env flags to cloud app + worker deploy env:
  - `AGE_GRAPH_NAME`
  - `ONTOLOGY_AGE_SYNC_ENABLED`
  - `ONTOLOGY_AGE_SYNC_LIMIT`
- Added `run_age_sync_job` executable into image build.
- Added conditional scheduler block in worker deployment:
  - `investor-age-sync-job` only deploys when `ONTOLOGY_AGE_SYNC_ENABLED=1`.

## Safety Posture
- Default behavior remains unchanged:
  - `ONTOLOGY_TRAVERSAL_ENGINE=sql`
  - `ONTOLOGY_AGE_ENABLED=0`
  - `ONTOLOGY_AGE_SYNC_ENABLED=0`
- If AGE extension/graph is unavailable, sync path returns no-op status without affecting SQL traversal.

## Verification
- Python compile checks passed for AGE modules/tools.
- Shell syntax checks passed for new runner + deploy scripts.
- Local AGE preflight executed:
  - result indicates `age_extension_missing` (expected in current environment).
- Cloud app deployed:
  - revision `investor-tools-app-00213-5kf` serving 100%.
- Cloud workers redeployed successfully.
- AGE sync job intentionally skipped in cloud deploy because:
  - `ONTOLOGY_AGE_SYNC_ENABLED!=1`

## Phase B Gate Check (Cloud)
- Ran cloud readiness check through authenticated app endpoint:
  - `/health/ready` reports `age_available=false`.
- Executed one-off cloud job to attempt extension install + graph bootstrap:
  - job: `investor-age-enable-job`
  - execution: `investor-age-enable-job-rmqbz`
- Result:
  - AGE install is blocked on current Cloud SQL runtime.
  - Error:
    - `extension "age" is not available`
    - `Could not open extension control file "/share/extension/age.control": No such file or directory.`

## Decision
- Keep AGE flags disabled in production cloud.
- Continue with SQL traversal as canonical runtime path.
- If graph acceleration is required in cloud, evaluate one of:
  - Cloud SQL PostgreSQL + Apache AGE-compatible runtime path (if/when supported),
  - dedicated graph backend (Neo4j/Memgraph/ArangoDB),
  - Postgres recursive traversal optimization + caching/shadow benchmarks.

## Files changed
- `app/services/age_graph_service.py`
- `app/main.py`
- `tools/age_preflight.py` (new)
- `tools/age_sync_projection.py` (new)
- `bin/run_age_sync_job` (new)
- `bin/deploy_cloud_run.sh`
- `bin/deploy_worker_job.sh`
- `Dockerfile`
- `.env.cloud.example`
