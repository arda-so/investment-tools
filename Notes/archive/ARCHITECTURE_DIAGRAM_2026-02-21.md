# Architecture Diagram (Current)
As of: 2026-02-21

```text
                              +-----------------------------+
                              |        Browser UI           |
                              | (dashboard + AI command)    |
                              +--------------+--------------+
                                             |
                         HTTP + SSE          |
                                             v
                              +-----------------------------+
                              |        FastAPI App          |
                              |        (run_v2_app)         |
                              +------+----------------------+
                                     | routes/services
                     +---------------+-------------------------------+
                     |                                               |
                     v                                               v
      +-------------------------------+                +------------------------------+
      | AI sync path (/ai/command)    |                | AI async path                |
      | fast deterministic + orchestr |                | (/ai/command/async + SSE)    |
      +---------------+---------------+                +---------------+--------------+
                      |                                                |
                      |                                                v
                      |                              +-----------------+------------------+
                      |                              | Queue: ai_command_jobs (Postgres) |
                      |                              +-----------------+------------------+
                      |                                                |
                      |                                                v
                      |                              +-----------------+------------------+
                      |                              | AI Worker (run_ai_worker)         |
                      |                              | map_task / reduce_task / chat     |
                      |                              +-----------------+------------------+
                      |                                                |
                      +----------------------+-------------------------+
                                             |
                                             v
                              +------------------------------+
                              | Domain Services / Orchestr. |
                              | ai_orchestrator, react,     |
                              | dashboard_service, etc.     |
                              +--------------+---------------+
                                             |
                     +-----------------------+-------------------------+
                     |                                                 |
                     v                                                 v
      +-------------------------------+                +------------------------------+
      | SQLite core domain data       |                | Postgres phase2 + queue      |
      | (portfolio, notes, thesis,    |                | (jobs, worker HB, reports,   |
      | report facts, chat memory)    |                | vector-ready tables)         |
      +-------------------------------+                +------------------------------+

```

## Legend

- Fast path: immediate DB/tool answers.
- Deep path: queued AI reasoning via worker.
- SSE push with polling fallback endpoint (`/ai/command/result/{job_id}`).
- Hybrid storage: SQLite + Postgres during migration.


## 2026-02-22 Refresh
- Diagram behavior change: deep jobs now emit early partial progress text, with reconnect/recovery before user-visible failure.\n- Proposal action lane is now localized card mutation rather than full dashboard redraw.

## 2026-02-24 Refresh
- Runtime update: active company market-cap lookup and events runtime paths are Postgres-only.
- AI command reliability: `/ai/command` timeout path now returns queued fallback quickly (no blocking hang).
- Health posture unchanged and valid in current run:
  - `/health/live` ok
  - `/health/ready` => `core_db_backend=postgres`, `queue_backend=postgres`, `db_ok=true`
- Continuation policy: keep async-first AI flow and avoid reintroducing SQLite fallback in active runtime request paths.
