# Investor OS v2 - System Overview

Current canonical architecture reference:
- `RECENT_ARCHITECTURE_2026-02-21.md`
- `ARCHITECTURE_EXEC_SUMMARY_2026-02-21.md` (non-technical)
- `ARCHITECTURE_DIAGRAM_2026-02-21.md` (diagram)
- `CODING_STYLE_GUIDE_2026-02-21.md`
- `IMPORTANT_SYSTEM_RULES_2026-02-21.md`
- `AI_TRAINING_MATERIALS_2026-02-21.md`

## 1. Product Scope
Investor OS v2 is a local-first investment operations platform with four primary workflows:
- Daily market briefing (`/dashboard`)
- Portfolio command center (`/my_universe`)
- Report triage library (`/reports`)
- Company research workspace (`/company_file?t=...`)
- Organizer for tasks/notes/daily log (`/organizer`)

Primary goal: zero-friction investor workflow from signal -> analysis -> execution notes.

## 2. Architecture
High-level layers:
- Web app entry: `app/main.py`
- Routing/controllers: `app/routers/*.py`
- Domain services/business logic: `app/services/*.py`
- Server-rendered UI: `app/templates/*.html`
- Data stores: SQLite DBs + generated report files
- Automation runners: `bin/*` and `automations/*`

Request path:
1. Route receives request (FastAPI)
2. Route delegates data prep/mutations to service functions
3. Jinja template renders page/partial
4. HTMX handles partial refreshes for interactive sections

## 3. Tech Stack
- Backend: FastAPI, Starlette
- Templating: Jinja2
- DB/ORM: SQLite + SQLModel/SQLAlchemy + sqlite3
- Frontend interaction: HTMX + vanilla JS + page-level CSS
- Market/news data: yfinance, RSS/Google news style feeds, report parsers
- AI providers: OpenAI, Anthropic, Ollama via hybrid engine
- Optional memory subsystem: ChromaDB + sentence-transformers

## 4. Key Modules
### 4.1 App bootstrap
- `app/main.py`
- Registers routers, creates tables, ensures schema, sets no-cache middleware.

### 4.2 Routers
- `app/routers/dashboard.py`: dashboard + my_universe endpoints, quick capture, command actions
- `app/routers/reports.py`: reports index, filtering, read-state actions, report viewer
- `app/routers/organizer.py`: daily log, tasks/todos, notes hub, memory screen, Google actions
- `app/routers/company_file.py`: company list/detail, SEC views, moat/notes/tasks/reminders, IR email

### 4.3 Services
- `app/services/dashboard_service.py`: market/home snapshot, news, metrics composition
- `app/services/reports_service.py`: report indexing/scoring/grouping/read state/intelligence
- `app/services/organizer_service.py`: organizer schema + task/note/daily CRUD + recall helpers
- `app/services/company_file_service.py`: company detail aggregation, filings context, live price fallback
- `app/services/agent_service.py`: agent query context assembly + AI answer orchestration
- `app/services/google_workspace_service.py`: Google OAuth/calendar/email read-send integration

## 5. Data Model & Storage
Primary directories/files:
- `data/` persistent app data
- `reports/` generated intelligence/report files
- `filings/`, `filing_docs/` SEC artifacts
- `logs/` automation/application logs
- `backups/` archive outputs

Primary DB files currently used:
- `data/core.db`
- `data/cache.db`
- `data/filings.db`
- `onyx_brain.db` (additional local intelligence state)

## 6. UI System
Base shell:
- `app/templates/base.html`
- Global wide wrapper uses hybrid SaaS width (`max-width: 1920px`).

Key pages:
- `app/templates/dashboard.html`
- `app/templates/my_universe.html`
- `app/templates/reports.html`
- `app/templates/company_detail.html`
- `app/templates/organizer.html`

Component partials:
- `app/templates/components/dashboard_body.html`
- `app/templates/components/organizer_body.html`
- `app/templates/components/company_detail_body.html`

Current UI direction:
- high-density operational dashboards
- minimal modal depth
- keyboard-forward interactions where practical
- cards + feed patterns instead of heavy tables where possible

## 7. AI Strategy
AI is integrated as an augmentation layer, not as the sole execution layer.

Engine:
- `tools/llm_engine.py`
- Provider routing with fallback (`openai`/`anthropic`/`ollama`)

Usage patterns:
- summarize or triage reports
- assist command parsing and context-rich responses
- blend deterministic signals + AI narrative

Classification:
- The system is AI-augmented with AI-native features, but core workflows remain deterministic and auditable.

## 8. Automation & Scheduling
Runner scripts:
- `bin/run_v2_app` for web app on `127.0.0.1:8766`
- multiple `bin/run_*` wrappers for daily/morning/signals/earnings/terminal jobs

Automation scripts:
- `automations/*.sh`
- `automations/launchd/*.plist` for scheduled execution on macOS

Typical cadence:
- daily/morning intelligence refresh
- earnings/signal scans
- nightly sync tasks

## 9. Operational Commands
Start app:
```bash
source .venv-memory/bin/activate
./bin/run_v2_app
```

Primary routes:
- `/dashboard`
- `/my_universe`
- `/reports`
- `/company_file`
- `/organizer`

## 10. Backup & Recovery
Current full backup artifact:
- `backups/investment_tools_backup_latest.tar`

Recommended backup policy:
- weekly full + daily incremental snapshots
- retention policy (e.g., 4 weekly fulls + 30 daily incrementals)
- periodic restore test (non-production folder)

If using Google Drive manually:
1. upload archive
2. confirm uploaded size matches local
3. keep at least one previous known-good restore point

## 11. Coding Style & Conventions
Observed conventions in this repo:
- backend-first architecture with thin templates
- explicit helper functions and defensive parsing
- minimal client JS for focused interactions
- pragmatic over framework-heavy
- product iteration prioritized with incremental refactors

## 12. Known Next Improvements
- Standardize config management and secrets loading by environment profile
- Add structured tests for route render + service transforms
- Introduce typed response DTOs for high-change views
- Add backup automation with integrity checks and retention pruning
- Consolidate duplicated logic between terminal and v2 web surfaces

---
Last updated: 2026-02-17
