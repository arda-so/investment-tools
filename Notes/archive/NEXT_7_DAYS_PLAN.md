# Next 7 Days Plan

Goal: Finish cloud-production readiness without changing user-facing behavior.

## Day 1: Stabilize Strict Startup
- Add startup validator for required env in non-dev:
  - `POSTGRES_DSN`
  - `CORE_DB_BACKEND=postgres`
  - queue backend consistency
- Emit one clear startup diagnostic payload on failure.
- Deliverable:
  - deterministic startup pass/fail with explicit missing keys.

## Day 2: Secret Hygiene
- Remove non-dev reliance on local secret files.
- Ensure cloud path uses env/secret manager only.
- Verify no secret-bearing files are required at runtime in non-dev.
- Deliverable:
  - secret loading matrix (dev vs non-dev) documented.

## Day 3: Deployment Manifests
- Finalize deploy shape:
  - app service
  - worker service/job
  - postgres + redis wiring
- Add runtime probes and restart policy in manifests.
- Deliverable:
  - repeatable deploy manifest set.

## Day 4: CI/CD + Gates
- Add pipeline checks:
  - lint/compile
  - startup smoke
  - `/health/live` + `/health/ready`
  - admin endpoint policy check
- Deliverable:
  - push-to-deploy gate that blocks unsafe config.

## Day 5: Observability
- Add dashboards + alerts:
  - readiness failures
  - queue depth growth
  - worker heartbeat gaps
  - error-rate spikes on `/ai/command`
- Deliverable:
  - actionable alerts with owner + runbook links.

## Day 6: Data Safety
- Validate backup schedule and restore procedure in target environment.
- Run one restore drill and verify app startup on restored dataset.
- Deliverable:
  - backup/restore signed off with recovery time.

## Day 7: Hardening + Handoff
- Remove dead compatibility code after final confidence pass.
- Freeze contracts for:
  - readiness payload
  - async job lifecycle
  - admin endpoint gating
- Final handoff package:
  - runbook
  - rollback steps
  - known risks

## Parallel Safety Checks (daily)
- Run:
  - `GET /health/live`
  - `GET /health/ready`
  - async queue smoke (`/ai/command/async` + SSE)
- Confirm:
  - `core_db_backend=postgres`
  - `queue_backend` expected value
  - no unexpected fallback behavior



## 2026-02-22 Refresh
- Prioritize cloud preflight completion (`AI_REDIS_URL`, managed DSN, secret manager wiring).\n- Run full smoke pack after env completion: live/ready/worker health + async command + proposal feedback cycle.\n- Add regression test for proposal list empty-row crash and proposal-card HTMX delete flow.

## 2026-02-24 Refresh
- Runtime update: active company market-cap lookup and events runtime paths are Postgres-only.
- AI command reliability: `/ai/command` timeout path now returns queued fallback quickly (no blocking hang).
- Health posture unchanged and valid in current run:
  - `/health/live` ok
  - `/health/ready` => `core_db_backend=postgres`, `queue_backend=postgres`, `db_ok=true`
- Continuation policy: keep async-first AI flow and avoid reintroducing SQLite fallback in active runtime request paths.
