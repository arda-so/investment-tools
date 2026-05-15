# Production Gap Analysis — Investment Tools

Date: 2026-03-05
Status: Audit of REAL gaps found in codebase (not theoretical)

---

## Executive Summary

The app is a **fully functional single-user tool** with sophisticated AI, SEC integration, and
dual deployment (local + cloud). However, it has **real production gaps** in 3 categories:

| Category | Severity | Summary |
|----------|----------|---------|
| **Security** | CRITICAL | Zero authentication, no CORS/CSRF, secrets in git, no rate limiting |
| **Testing** | HIGH | 2% test coverage, 88% of services untested, CI doesn't run tests |
| **Infrastructure** | MEDIUM | No structured logging, backup script broken (SQLite not Postgres), no migration framework |

---

## PART 1: SECURITY GAPS (12 Findings)

### GAP S1: Zero Authentication — CRITICAL

**Status:** No login, no JWT, no OAuth, no API keys on ANY endpoint.

**Evidence:**
- `app/main.py` — no auth middleware registered
- All 8 routers completely open
- Admin endpoints unprotected: `POST /api/admin/ir-registry` (`company_file.py:1470`) — anyone can modify IR registry

**Impact:** If exposed to the internet, anyone can view/modify your entire portfolio, trigger AI commands, and modify database records.

---

### GAP S2: No Authorization / Multi-Tenancy — CRITICAL

**Status:** Single-user by design. No `user_id` filtering on any query.

**Evidence:**
- `postgres_core_service.py:216-217` — `tenant_id` and `user_id` columns exist in schema but are **never used** in WHERE clauses (placeholder columns)
- `portfolio_memory_service.py` — stores/retrieves portfolio data without any user filtering
- All dashboard routes public (`dashboard.py`) — no `@requires_auth` decorator exists

**Impact:** Cannot safely add multiple users without a full data isolation rewrite.

---

### GAP S3: SQL Injection Risk — HIGH

**Status:** Most queries use parameterized `%s` placeholders (good), but several use f-string table names.

**Concrete instances:**
- `postgres_core_service.py:2483` — `f"SELECT COUNT(*) AS c FROM {sq}"` (table name injected via f-string)
- `postgres_core_service.py:2486` — `f"SELECT COUNT(*) FROM {pg}"` (same pattern)
- `organizer_service.py:924` — `f"SELECT {col} AS txt FROM {tbl} WHERE {where}"` (column + table + where all f-string)
- `proactive_ai_service.py:115` — `ALTER TABLE` with `{col_def}` string formatting

**Current risk:** LOW (table names are hardcoded in loops, not user-supplied), but fragile — any refactor could expose these.

---

### GAP S4: XSS via innerHTML — HIGH

**Status:** Widespread `innerHTML` usage in frontend templates without sanitization.

**Evidence:**
- `base.html:1056-1066` — `result.innerHTML = html;`
- `financial_lab.html` — 7 instances of `innerHTML` assignment (lines 578, 591, 630, 705, 753, 856, 881)
- `workspace_os_day.html:1460-2346` — extensive `innerHTML` usage

**Mitigation in place:** Jinja2 auto-escapes server-rendered content (good). But client-side JS renders API responses directly into DOM without sanitization.

**Attack vector:** If any API response contains user-controlled text (e.g., ticker notes, AI responses), XSS payload could execute.

---

### GAP S5: Secrets Committed to Git — CRITICAL

**Status:** `.env` file is tracked in git with real API keys and credentials.

**Exposed secrets:**
```
FINNHUB_API_KEY=d67lidpr01...
FMP_API_KEY=HDQTJq4KdK...
POSTGRES_DSN=postgresql://investor:investor_dev@127.0.0.1:5432/investor_os
AI_REDIS_URL=redis://127.0.0.1:6379/0
SEC_USER_AGENT="AhmetArdaSolmaz ahmetardasolmaz@gmail.com"  (PII)
```

**Impact:** If repo ever becomes public or shared, all credentials exposed. Git history preserves them forever even after deletion.

---

### GAP S6: No CORS / CSRF Protection — HIGH

**Status:** No CORSMiddleware configured. No CSRF tokens on any form.

**Evidence:**
- `main.py:62-69` — only middleware is Cache-Control headers
- No `CORSMiddleware` import or configuration anywhere
- POST endpoints like `/portfolio/record-trade` (`dashboard.py:1000+`) accept requests from any origin without CSRF token

**Attack:** Malicious website could submit cross-site form that modifies portfolio data.

---

### GAP S7: No Rate Limiting — HIGH

**Status:** Zero rate limiting on any endpoint.

**Evidence:**
- No `slowapi` or rate-limiting library in `requirements.txt`
- No rate-limit decorators on any endpoint
- Expensive endpoints like `/ai/command` (triggers LLM calls) can be hammered indefinitely

---

### GAP S8: Error Information Disclosure — MEDIUM

**Status:** Exception details returned to client in several places.

**Evidence:**
- `company_file.py:1497-1498` — `return JSONResponse({"error": str(exc)})` — raw exception string to client
- `ai.py:117` — `traceback.format_exc(limit=6)` stored in state, exposed via `/api/universe/status`

**Risk:** Database schema, file paths, connection strings could leak via error messages.

---

### GAP S9: Missing Security Headers — MEDIUM

**Status:** Only `Cache-Control` set. Missing all standard security headers.

**Missing:**
- `X-Frame-Options: DENY` (clickjacking)
- `X-Content-Type-Options: nosniff` (MIME sniffing)
- `Strict-Transport-Security` (HTTPS enforcement)
- `Content-Security-Policy` (resource loading)
- `Referrer-Policy` (referrer leakage)

---

### GAP S10: SSL Verification Bypass — MEDIUM

**File:** `ir_audio_ingest_service.py:22-28`

**Status:** If `certifi` import fails, SSL verification is silently disabled:
```python
except Exception:
    _SSL_CTX.check_hostname = False
    _SSL_CTX.verify_mode = _ssl.CERT_NONE  # Accepts any cert
```

Should fail-secure (raise exception) rather than silently disable.

---

### GAP S11: No Input Validation Framework — MEDIUM

**Status:** No Pydantic models for request bodies. Endpoints accept raw `dict` payloads.

**Evidence:**
- `ai.py:916-920` — `payload: dict = Body(default={})` — no schema validation
- Most POST endpoints parse `await request.json()` manually

---

### GAP S12: Docker Compose Hardcodes Credentials — LOW

**File:** `docker-compose.yml:8-9`
```yaml
POSTGRES_PASSWORD: investor_dev
```

Should use environment variable substitution or Docker secrets.

---

## PART 2: TESTING GAPS

### Test Coverage Summary

```
Services tested:     5 / 42   = 12%
Routers tested:      4 / 8    = 50%
Tools tested:        0 / 47   = 0%
Test methods:        36 total
Test lines:          633 lines
Project size:        ~94,000 lines Python
Coverage ratio:      0.7% (tests:code)
```

### What IS Tested (Strengths)

| Area | Tests | Quality |
|------|-------|---------|
| AI chat flow & interruption recovery | 15 tests | Excellent |
| Intent routing & entity extraction | 15 tests | Excellent |
| UI action contracts & frontend hooks | 6 tests | Good |
| Conversation regressions | 5 tests | Good |
| Memory/learning lifecycle | 3 tests | Basic |
| Quality/policy snapshots | 2 tests | Basic |

### What Is NOT Tested (Critical Gaps)

| Service | Lines | Risk | What's untested |
|---------|-------|------|-----------------|
| `proactive_ai_service.py` | ~3,000+ | **CRITICAL** | Proposal engine, signal evaluation, outcome measurement, cascade analysis |
| `sec_edgar_poller_service.py` | ~1,500+ | **CRITICAL** | SEC filing polling, Form 4 extraction, dedup state machine |
| `events_service.py` | ~500+ | **HIGH** | Event bus routing, signal publishing |
| `debate_service.py` | ~800+ | **HIGH** | Multi-agent debate (bull/bear/risk/judge), rejection logic |
| `earnings_transcript_service.py` | ~600+ | **HIGH** | Earnings text extraction, LLM analysis |
| `postgres_core_service.py` | ~3,500+ | **HIGH** | All database operations, schema management |
| `portfolio_memory_service.py` | ~1,000+ | **MEDIUM** | Context injection, memory engine |
| `web_search_service.py` | ~500+ | **MEDIUM** | Macro snapshot, RSS news, realtime detection |
| 32 other services | ~30,000+ | **MEDIUM** | Company intel, supply chain, workspace, reports, etc. |

### All 47 CLI Tools — 0% Tested

No unit tests for any tool. Critical untested tools:
- `insider_refresh.py` — Form 4 insider trades
- `red_flag_filter.py` — Risk detection
- `sec_watchdog.py` — SEC filing monitoring
- `fetch_macro.py` — Macro data
- `news_wire.py` — News aggregation
- `run_morning_brief.py` — Daily briefing
- `earnings_watch_rank.py` — Earnings timeline
- `run_daily_catchup.py` — Daily updates

### Test Infrastructure Gaps

| Item | Status |
|------|--------|
| pytest configuration (pytest.ini / pyproject.toml) | Missing |
| conftest.py (shared fixtures) | Missing |
| Test database setup/teardown | Missing |
| Coverage.py reporting | Missing |
| CI runs tests | **NO** — CI only does syntax check + linting + smoke endpoints |
| Minimum coverage threshold | Not set |
| Integration tests | None |
| Performance tests | None |

### CI Pipeline (`.github/workflows/ci.yml`)

CI runs on push/PR but does NOT execute pytest:
- Syntax validation (py_compile)
- Ruff linting (E9, F63, F7, F82)
- DRY audit (custom)
- AI data-policy eval
- Replay smoke tests
- Runtime smoke tests (health + dashboard endpoints)

**Tests will NOT block deployment.**

---

## PART 3: INFRASTRUCTURE GAPS

### GAP I1: No Structured Logging — HIGH

**Status:** Almost no logging in the entire app. Only 1 `logging.info()` call found.

**Evidence:**
- `app/main.py:305` — single `logging.getLogger(__name__).info(...)` for market cap seeding
- All 42 services use `print()` or no output at all
- No JSON structured logging (`structlog`, `python-json-logger`)
- No log rotation configured
- No log level environment variable

**Impact:** Cannot diagnose production issues, audit actions, or set up log-based alerts.

---

### GAP I2: Backup Strategy is Broken — HIGH

**Status:** `bin/run_backup_local` backs up **SQLite** files, but the app is **Postgres-only**.

**Evidence:**
- Script creates tar.gz of: `core.db`, `ai_queue.db`, `cache.db`, `onyx_brain.db` — all deprecated
- No `pg_dump` anywhere in the codebase
- No cloud backup replication
- 7-day local retention only

**Impact:** No working backup of the actual Postgres database.

---

### GAP I3: Schema Error Suppression — HIGH

**Status:** All 19 `ensure_*_schema()` calls wrapped in `try/except: pass` at startup.

**Evidence:** `app/main.py:121-141`:
```python
for _fn in (...):
    try:
        _fn()
    except Exception:
        pass  # Silent failure — schema might not exist!
```

**Impact:** App starts even if critical tables don't exist. Errors happen later at runtime with confusing messages.

---

### GAP I4: No Database Migration Framework — MEDIUM

**Status:** Schema managed via `CREATE TABLE IF NOT EXISTS` statements. No Alembic, no version tracking.

**Evidence:**
- `postgres_core_service.py:186-289+` — inline SQL DDL statements
- 19 separate `ensure_*_schema()` functions across services
- No migration history table
- No rollback mechanism

**Impact:** Schema changes are invisible. Two servers running different code versions = silent schema divergence.

---

### GAP I5: Graceful Shutdown Too Aggressive — MEDIUM

**Status:** 2-second thread join timeout is too short for LLM-heavy background tasks.

**Evidence:** `app/main.py:334-346`:
- `th.join(timeout=2.0)` for all 4 background threads
- SEC EDGAR polling can take 10+ seconds mid-request
- Outcome measurement runs LLM calls that take 30+ seconds
- No explicit SIGTERM handler (relies on Uvicorn implicit handling)

**Impact:** In-flight work (proposals, evaluations) silently lost on deploy/restart.

---

### GAP I6: Health Check Missing Worker Status — LOW

**Status:** `/health/ready` checks Postgres connectivity but not background threads or Redis.

**Evidence:** `app/main.py:71-105`:
- Checks: DB connection with `SELECT 1`
- Missing: market-snapshot thread alive?, SEC poller alive?, Redis connected?, schema tables exist?

---

### GAP I7: No .env Validation — LOW

**Status:** No schema validation for environment variables. Typos like `POSTGRE_DSN` silently ignored.

**Recommendation:** Use Pydantic `BaseSettings` for env var validation with defaults and type checking.

---

## PART 4: PRIORITY FIX ORDER

### P0 — Fix Before Any External Exposure

| # | Gap | Effort | What to do |
|---|-----|--------|------------|
| 1 | S1: Authentication | 8–16 hrs | Add JWT or API key middleware to all routes |
| 2 | S5: Secrets in git | 1 hr | Add `.env` to `.gitignore`, rotate exposed API keys, use GCP Secret Manager for cloud |
| 3 | I2: Broken backup | 2 hrs | Replace SQLite tar with `pg_dump --format=custom`, add to cron/launchd |
| 4 | I3: Schema error suppression | 1 hr | Replace `pass` with `logging.error()` + raise on critical tables |

### P1 — Fix Before Production Users

| # | Gap | Effort | What to do |
|---|-----|--------|------------|
| 5 | S6: CORS/CSRF | 2–4 hrs | Add `CORSMiddleware`, CSRF tokens on POST forms |
| 6 | S9: Security headers | 1 hr | Add middleware for X-Frame-Options, HSTS, CSP, etc. |
| 7 | S7: Rate limiting | 2–4 hrs | Add `slowapi` to `/ai/command` and other expensive endpoints |
| 8 | I1: Structured logging | 4–8 hrs | Add `structlog` or `python-json-logger`, configure in main.py |
| 9 | S8: Error disclosure | 2 hrs | Wrap all `str(exc)` returns in generic error messages |
| 10 | S4: XSS / innerHTML | 4–8 hrs | Add DOMPurify or escape user content before innerHTML |

### P2 — Fix Before Scaling

| # | Gap | Effort | What to do |
|---|-----|--------|------------|
| 11 | Testing: Core services | 40–80 hrs | Add pytest + tests for proactive_ai, events, SEC poller, debate |
| 12 | Testing: CI integration | 2 hrs | Add `pytest` step to `.github/workflows/ci.yml` |
| 13 | I4: Migration framework | 8–16 hrs | Add Alembic, convert `ensure_*_schema()` to versioned migrations |
| 14 | S2: Multi-tenancy | 30–60 hrs | Add user_id filtering, row-level security (only if SaaS) |
| 15 | I5: Graceful shutdown | 2 hrs | Increase timeout to 10s, add SIGTERM handler, log in-flight work |
| 16 | S3: SQL injection cleanup | 4 hrs | Replace f-string table names with allowlist lookups |

### P3 — Nice to Have

| # | Gap | Effort | What to do |
|---|-----|--------|------------|
| 17 | S10: SSL fallback | 30 min | Change to fail-secure (raise exception) |
| 18 | S11: Input validation | 8–16 hrs | Add Pydantic request models |
| 19 | I6: Health check workers | 2 hrs | Add thread alive checks + Redis check to `/health/ready` |
| 20 | I7: Env validation | 2 hrs | Add Pydantic BaseSettings |
| 21 | S12: Docker secrets | 1 hr | Use `${POSTGRES_PASSWORD}` env substitution |
| 22 | Testing: Tools | 20–40 hrs | Add tests for critical CLI tools |

---

## Total Remediation Effort Estimate

| Priority | Hours | For |
|----------|-------|-----|
| P0 (before exposure) | 12–20 hrs | Auth, secrets, backup, schema errors |
| P1 (before production) | 15–27 hrs | CORS, headers, rate limiting, logging, errors, XSS |
| P2 (before scaling) | 86–162 hrs | Testing, CI, migrations, multi-tenancy, shutdown |
| P3 (nice to have) | 33–62 hrs | Input validation, health checks, env validation |
| **Total** | **146–271 hrs** | |

At AI-assisted speed (~3x faster): **~50–90 hours of your time** to close all gaps.

---

## Context: What's Actually GOOD

Not everything is a gap. The app does many things right:

- Postgres-only enforcement with strict mode guards
- Connection pooling configured (`POSTGRES_POOL_SIZE`, `MAX_OVERFLOW`, `RECYCLE`)
- Health endpoints exist (`/health/live`, `/health/ready`) with DB checks
- Environment separation (dev/test/prod configs)
- Dependencies fully pinned in `requirements.txt`
- Docker builds reproducible
- Cloud Run deployment with rollback scripts
- GCP monitoring policies (latency P95, job failures, stuck runs)
- subprocess calls use list args (no `shell=True`) — safe
- Jinja2 auto-escaping on server-rendered templates
- Parameterized SQL for most user-facing queries
- Daemon threads with stop events for graceful shutdown (even if timeout is short)
