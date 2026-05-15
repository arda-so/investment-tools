# Onboarding Guide - Investor OS v2

## 1. What This System Is
Investor OS v2 is a local FastAPI application for investor workflows:
- Dashboard (`/dashboard`)
- Portfolio (`/my_universe`)
- Reports library (`/reports`)
- Company research (`/company_file`)
- Organizer (`/organizer`)

## 2. Prerequisites
- macOS shell environment
- Python 3.11+
- Existing repo at `/Users/solmaz/Investment_Tools`
- `.venv-memory` preferred for runtime

## 3. First Run
```bash
cd /Users/solmaz/Investment_Tools
source .venv-memory/bin/activate
./bin/run_v2_app
```

Open:
- `http://127.0.0.1:8766/dashboard`

## 4. Project Structure
- `app/main.py` app bootstrap
- `app/routers/` HTTP routes
- `app/services/` business logic
- `app/templates/` Jinja views
- `automations/` scheduled jobs
- `bin/` run wrappers
- `data/` and `reports/` runtime data

## 5. Core Development Workflow
1. Change service or route logic.
2. Update template if UI changes.
3. Validate with route smoke checks.
4. Keep changes focused per page/feature.

## 6. Quick Smoke Test
```bash
source .venv-memory/bin/activate
python - <<'PY'
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)
for p in ['/dashboard','/my_universe','/reports','/organizer','/company_file?t=ADBE']:
    r = c.get(p)
    print(p, r.status_code)
PY
```

## 7. Key Config
Loaded from `.env` via `app/core/config.py`.
Common keys:
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `GEMINI_API_KEY` (or `GOOGLE_API_KEY`)
- `AI_PROVIDER`
- `GEMINI_MODEL` (default: `gemini-2.5-flash`)
- `GEMINI_FALLBACK_MODELS` (default: `gemini-2.0-flash,gemini-1.5-flash`)
- Google OAuth keys/paths for Organizer integration

## 8. UI Notes
- Base shell is in `app/templates/base.html`
- Global command palette behavior also lives there
- Organizer and My Universe pages use full-screen overrides

## 9. Common Pitfalls
- Running without `.venv-memory` can miss dependencies.
- Stale server process on port `8766` can hide new changes.
- Google features require valid OAuth setup and token files.

## 10. Useful Commands
Check server port:
```bash
lsof -nP -iTCP:8766 -sTCP:LISTEN
```

Restart app quickly:
```bash
./bin/run_v2_app
```

---
Last updated: 2026-02-17
