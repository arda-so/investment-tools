# App Status — 2026-03-07

## Scope
Current-state snapshot for local + cloud after recovery/stabilization work.

## Runtime Status
- Local app path:
  - Main app runs on `127.0.0.1:8766`
  - `/today` and `/day` are served by the same route handler.
- Cloud app path:
  - Cloud Run service remains active (`investor-tools-app`, `europe-west1`).
  - Auth-protected routes return unauthorized without valid app auth.

## `/today` Code Structure (Current)
- Route:
  - `app/routers/workspace_os.py`
  - `@router.get("/today")` + `@router.get("/day")` -> `day_view()`
- Service:
  - `app/services/workspace_os_service.py`
  - `get_day_view()` builds:
    - `focus`
    - `agent_flagged`
    - `backlog`
  - Data inputs:
    - `investor_annotations_core` (tasks)
    - `action_proposals_core` (open proposals)
    - `investment_records_core` (open records)
- Template:
  - `app/templates/workspace_os_day.html`
  - Restored full workspace template (not fallback)
  - Theme key: `themeDarkV1`

## Recovery/Hardening Notes
- Workspace day template was restored to the full version from a known good cloud image.
- Local safe-restart path used after restore.
- Cloud redeploy path re-run after restore to keep app parity.

## Backup Status
- Local recovery bundle created under:
  - `backups/recovery_20260307_124041/`
- Includes:
  - repo snapshot archive(s)
  - git bundle + diffs
  - Cloud Run and Cloud SQL config exports
  - restore README + checksums
- Cloud SQL on-demand backup executed successfully (latest recorded run during recovery session).
- Recovery artifacts were also exported to GCS under:
  - `gs://onyx-terminal-487613-runtime-files/backups/recovery_20260307_124041/`

## Outstanding Follow-ups
- Continue template-level visual QA for dark mode consistency across all pages.
- Keep authentication secrets rotation + Secret Manager rollout as a separate controlled change window.
