# Ontology Phase: Anti-Relationships + Signed Propagation Rollout

Date: 2026-03-05
Environment: Local + Cloud Run (`onyx-terminal-487613`, `europe-west1`)

## Implemented
- Added anti-relationship signed semantics (`HEDGES_AGAINST`, `IMMUNE_TO`) for ontology edges.
- Added signed propagation toggle in simulation runtime:
  - `ONTOLOGY_SIGNED_PROPAGATION_ENABLED`
- Updated macro shock traversal to support signed edge propagation:
  - uses `relationships_core.signed_weight` (fallback to relationship-type default)
  - keeps strongest absolute path per node
  - supports mitigating paths (negative signed impact)
- Updated simulation output fields:
  - `impact_score` (absolute magnitude)
  - `signed_score` (net signed value)
  - `propagation_effect` (`contagion` or `mitigating`)
- Updated answer synthesis to include mitigated holdings when present.

## Ingestion updates
- SEC relationship reflection now allows anti-relationship types:
  - `HEDGES_AGAINST`, `IMMUNE_TO`
- SEC heuristic relationship extraction now detects anti keywords:
  - `hedge against`, `hedges against`, `hedging` => `HEDGES_AGAINST`
  - `immune to`, `insulated from`, `resilient to` => `IMMUNE_TO`
- Signed weight assignment during writes:
  - anti-relationships => `signed_weight=-1.0`
  - others => `signed_weight=1.0`
- Applied same signed write behavior to proactive report-facts ontology ingest path.

## Cloud defaults
- Enabled signed propagation by default in deploy scripts:
  - `bin/deploy_cloud_run.sh`
  - `bin/deploy_worker_job.sh`
  - default now: `ONTOLOGY_SIGNED_PROPAGATION_ENABLED=1`

## Verification
- Local compile checks: pass.
- Local simulation smoke call with signed mode enabled: pass (no runtime errors).
- Cloud app deployed: revision `investor-tools-app-00210-tf9` serving 100%.
- Cloud app health (`/health/ready`, authenticated): `db_ok=true`.
- Cloud app env confirms `ONTOLOGY_SIGNED_PROPAGATION_ENABLED=1`.
- Cloud worker/job rollout completed with new image digest.

## Files changed
- `app/services/proactive_ai_service.py`
- `app/services/sec_ingest_pipeline_service.py`
- `bin/deploy_cloud_run.sh`
- `bin/deploy_worker_job.sh`

