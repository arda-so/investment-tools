# Ontology Phase Cloud Rollout Status

Date: 2026-03-05
Environment: Cloud Run (project `onyx-terminal-487613`, region `europe-west1`)

## Completed
- Deployed app revision `investor-tools-app-00208-9b9` with Postgres-only core/queue and ontology flags enabled.
- Deployed Cloud Run jobs with immutable image digest pinning.
- Deployed new scheduled job:
  - `investor-decay-refresh-job`
  - schedule: `15 2 * * *` (UTC)
  - command: `/app/bin/run_decay_refresh_job`

## Safety fixes applied
- `bin/deploy_cloud_run.sh`
  - `DEPLOY_WORKER` default changed from `1` to `0`.
  - Reason: `investor-tools-worker` service command (`/app/bin/run_ai_worker`) is non-HTTP and fails Cloud Run service port checks.
- `bin/deploy_worker_job.sh`
  - Added automatic tag->digest resolution so jobs run immutable images.
- `bin/run_decay_refresh_job`
  - Disabled automatic `.env` sourcing in cloud context (opt-in only via `SOURCE_DOTENV=1`).
  - Reason: `.env` could override Cloud Run secrets/env and break Postgres connectivity.

## Verification
- App health (authenticated): `/health/ready` returned:
  - `{"ok":true,"service":"investor_app","core_db_backend":"postgres","queue_backend":"postgres","db_ok":true}`
- Decay refresh manual execution succeeded after fix:
  - execution: `investor-decay-refresh-job-twjjx`
  - status: `Completed=True`
- Decay job now pinned to digest:
  - `europe-west1-docker.pkg.dev/onyx-terminal-487613/investor-tools/app@sha256:d64f6b4e40a40effe33fb4ce9837628f606b56fe91eef69e654d7c172c5faeb6`

## Remaining recommendation
- Keep `investor-tools-worker` Cloud Run *service* disabled/deprecated and rely on Cloud Run *jobs* for worker execution.
