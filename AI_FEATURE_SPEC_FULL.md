# Investor OS AI - Full Feature Specification (Current State)

Last updated: 2026-02-19
Codebase root: `/Users/solmaz/Investment_Tools`

## 1) What This Document Covers
This is the detailed product + technical specification of the current in-app AI system, including:
- Orchestrator architecture (Manager + Worker + ReAct info path)
- Tooling and intent routing behavior
- Long-term memory schema and how it is used
- Proactive behavior (Morning Brief + gap prompts)
- Chat UI behavior (cards, markdown, silent navigation, interview flow)
- Voice and vision capabilities
- APIs and runtime context injection
- Test coverage and current verification status
- Known gaps and practical next improvements

## 2) System Architecture (Current)

### 2.1 Core Layers
- Entry API router: `app/routers/ai.py`
- Main orchestrator: `app/services/ai_orchestrator.py`
- Information-first ReAct service: `app/services/ai_react_service.py`
- Memory + watcher + interview logic: `app/services/portfolio_memory_service.py`
- AI engine/providers: `tools/llm_engine.py`
- Chat UI shell + interaction logic: `app/templates/base.html`

### 2.2 Routing Pattern (What happens per message)
1. Frontend sends `POST /ai/command` with:
- `query`
- `context` (history, path, last intent, etc.)
- optional `image_data_url`

2. Router (`ai.py`):
- learns user preferences from text
- learns investor style memory from structured answers
- injects user profile + preference summary + investor style summary
- merges DB chat memory + client history
- injects runtime context (`inject_runtime_context`)
- if image present -> vision path (Gemini multimodal)
- else -> `run_ai_command(...)`

3. Orchestrator (`run_ai_command`):
- checks manager interrupt (topic switch)
- runs ReAct information path first (`run_react_information`)
- if no ReAct match, runs worker handlers (interview/follow-up) unless interrupted
- then deterministic intent handlers
- finally LLM fallback with guarded prompt

4. Router returns structured response:
- `status`, `intent`, `message`, `confidence`
- optional `redirect_url`
- `traces`
- optional structured `ui`
- `action` object (`NAVIGATE` or `NONE`)
- `interview_progress`

## 3) Manager + Worker Interrupt Model
Implemented in `app/services/ai_orchestrator.py`.

### 3.1 Key constants
- `ORCHESTRATOR_VERSION = "v1.2.0"`
- `MANAGER_LLM_TIMEOUT_SEC = 1.2`
- `MANAGER_INTERRUPT_THRESHOLD = 0.82`
- Worker intents include: `portfolio_interview`, `list_watchlist`, `list_portfolio`, `list_notes`, `list_reports`, `list_tasks`

### 3.2 Behavior
- If currently in a worker flow, manager checks if new message is a high-confidence topic switch.
- Detection methods:
  - Rule-based interrupt classifier
  - LLM JSON classifier fallback
- If interrupt detected:
  - worker follow-up handling is skipped for that turn
  - query may be rewritten into canonical command (example: portfolio/watchlist list request)
  - avoids yes/no trap loops

### 3.3 Confirmation loop safety
- Navigation follow-up has clarification handling
- Provides explicit recovery choices when unclear input persists (`open now`, `cancel`, `back to chat`)

## 4) ReAct Information Path (Information before Navigation)
Implemented in `app/services/ai_react_service.py` and called early from orchestrator.

### 4.1 Purpose
When user asks a question (What/Why/summary), AI fetches data and answers in chat instead of immediately navigating.

### 4.2 Tool registry
Current ReAct tools:
- `read_morning_briefing`
- `read_watchlist`
- `get_holdings`
- `list_notes`
- `list_tasks`
- `list_reports`
- `read_latest_report`
- `read_current_report`
- `report_facts`

### 4.3 ReAct planning behavior
- `_plan(query, context)` classifies informational intent
- For informational query:
  - selects needed tool(s)
  - executes bounded steps (max 3)
  - renders natural answer + optional structured `ui`
- `open/go/navigate` phrasing bypasses this and stays navigation-oriented

### 4.4 ReAct intents currently supported
- `morning_updates`
- `report_facts_summary`
- `list_watchlist`
- `list_portfolio`
- `list_tasks`
- `list_notes`
- `list_reports`
- `summarize_latest_report`
- `summarize_current_report`
- `notes_summary`

## 5) Deterministic Intent Handlers (Orchestrator)
In addition to ReAct, orchestrator includes deterministic intents such as:
- open/navigation: `_intent_open_page`, `_intent_app_map_route`, `_intent_open_company`, `_intent_open_notes`, `_intent_open_sec_filings`
- list/read: `_intent_list_watchlist`, `_intent_list_portfolio`, `_intent_list_tasks`, `_intent_list_notes`, `_intent_list_reports`, `_intent_get_holdings`
- analysis/status: `_intent_portfolio_today_status`, `_intent_portfolio_change_log`, `_intent_position_why`, `_intent_watchlist_why`, `_intent_company_compare`, `_intent_notes_summary`, `_intent_morning_updates`
- write actions: `_intent_add_task`, `_intent_add_note_draft`, `_intent_add_daily_log`, `_intent_save_thesis`, `_intent_backfill_trade_history`, `_intent_delete_notes`
- interview: `_intent_portfolio_interview`

## 6) Long-Term Memory and Data Model
Main DB path includes `onyx_brain.db` with schema in `portfolio_memory_service.py`.

### 6.1 Key memory tables
- `watchlist_thesis`
  - stores thesis summary, conviction, horizon, invalidation, strategy tags
- `decision_log`
  - timestamped actions (`BUY/SELL/TRIM` etc.), quantity, price, reasoning
- `portfolio_interview_queue`
  - tracks per-ticker interview progress, step, pending question, status
- `investor_style_memory`
  - persistent key/value profile memory (style/rules/mistakes)
- `investor_question_overrides`
  - user-defined better wording for future interview questions
- `morning_briefs`
  - daily morning snapshot by day
- `report_facts`
  - extracted important facts from reports with importance score and ticker
- `report_fact_ingest_state`
  - watermark for periodic ingest schedule

### 6.2 Learning behavior now implemented
- On each user message, AI attempts lightweight learning from structured signals:
  - examples recognized: `priority=...`, `horizon=...`, `risk=...`, and mistake statements
- Saves into `investor_style_memory`
- User preference text also learned separately via `user_preferences_service`
- User profile for prompt is built from:
  - base profile
  - user preferences summary
  - investor style memory summary

### 6.3 Legacy awareness
Interview logic tags strategy as `LONG_TERM`, `TRADE`, or `LEGACY`; `LEGACY` can be marked non-learnable for pattern learning paths.

## 7) Interview Mode (One-Question Flow)
Implemented via `start_portfolio_interview` and `submit_portfolio_interview_answer`.

### 7.1 Flow
- Starts from holdings and missing thesis/profile gaps
- Queues pending interview items
- Asks one question at a time
- Waits for user input before next question
- Supports storing custom question overrides from user corrections

### 7.2 Question categories
- Ticker-level: why own, horizon (`LONG_TERM/TRADE/LEGACY`), invalidation/exit
- Investor profile questions (missing playbook items)
- Peer gap rationale (own X but not Y)

### 7.3 UI support
- Interview progress bar: `Reviewed X/Y holdings`
- Interview assistant chips: `Skip`, `Answer Later`, `Why this matters`

## 8) Proactive Intelligence (Watcher + Morning Brief)

### 8.1 Runtime watcher context
`inject_runtime_context(...)` adds:
- `portfolio_live` summary
- watcher events
- recent transactions
- report facts (top relevant)
- current page hint

### 8.2 Morning brief
- endpoint: `GET /ai/morning_brief`
- uses cached daily snapshot if exists, else computes and saves
- saved in `morning_briefs`
- includes top bullets from:
  - portfolio day move
  - big movers
  - top holdings + recent intel feed

### 8.3 Proactive gap prompt
endpoint: `GET /ai/proactive`
priority order:
1. Earnings alerts for portfolio/watchlist names
2. High-impact `report_facts` alerts
3. Missing profile question
4. Missing thesis for held ticker
5. Peer decision gap prompt

## 9) Report Facts Pipeline

### 9.1 Ingestion
- `ingest_recent_report_facts(limit_reports, facts_per_report)`
- scans recent reports, extracts meaningful lines, scores importance
- stores deduplicated facts by `fact_hash`
- updates ingest watermark

### 9.2 Query
- `query_report_facts(query, tickers, limit)`
- ranked by `importance DESC`, `fact_date DESC`

### 9.3 Runtime use
- included in runtime context to support current answer quality
- used for proactive alerts
- available as ReAct tool `report_facts`

## 10) Voice and Vision Capabilities

### 10.1 Vision
- Frontend supports image upload/drag-drop in chat
- Router parses base64 data URL and sends to `ask_ai_vision`
- `llm_engine.py` multimodal path uses Gemini generateContent with inline image
- MIME support: png/jpeg/jpg/webp
- Max image size in UI: 5MB

### 10.2 Voice (Phase 2 behavior in UI)
- Mic input via Web Speech API (`SpeechRecognition` / `webkitSpeechRecognition`)
- Read-aloud via `speechSynthesis`
- Voice loop mode:
  - listen -> auto submit -> await response -> optional speak -> resume listening
- Barge-in behavior:
  - user speech start interrupts TTS immediately
- User-configurable speech rate
- Voice preferences persisted in localStorage

## 11) Chat UI Contract and Behavior

### 11.1 Message rendering
- Markdown rendering enabled (`marked` fallback parser)
- Rich cards for selected intents
- Task list rendering as interactive checklist cards
- Structured UI payload rendering (`ui.type` such as `task_list`, `checklist`, `brief_card`)

### 11.2 Task interactivity
- Checkbox click posts to `POST /ai/task/complete`
- on success: card fades/strikes/removes

### 11.3 Silent navigation
- If AI response action is `NAVIGATE` or known nav intent with URL:
  - show small toast (`Redirecting...`)
  - navigate directly
  - suppress chat clutter bubble for navigation text

### 11.4 Status surface
- per-message status line (e.g., `✓ Used N tool steps`)
- details/traces hidden behind small `i` toggle

## 12) API Surface (AI)
Defined in `app/routers/ai.py`.

- `POST /ai/command`
  - primary chat endpoint (text + optional image)
- `GET /ai/proactive`
  - proactive prompt endpoint
- `GET /ai/morning_brief`
  - morning brief retrieval
- `POST /ai/task/complete`
  - complete task by id or text
- `POST /api/portfolio/import-history`
  - CSV import path (feature-gated by env)

## 13) AI Provider Engine
`tools/llm_engine.py`

### 13.1 Provider strategy
- primary provider configurable (`AI_PROVIDER`)
- fallback provider configurable (`AI_FALLBACK_PROVIDER`)
- supported providers: OpenAI, Groq, Anthropic, Gemini, Ollama

### 13.2 Gemini model chain
- primary: `GEMINI_MODEL` (default `gemini-2.5-flash`)
- fallbacks from config + defaults (`gemini-2.0-flash`, `gemini-1.5-flash`)
- retry per model before falling through

### 13.3 Vision path
- Gemini-only in this stack for multimodal image+text

## 14) SQLite Reliability Hardening
`app/core/sqlite_hardening.py`

- `PRAGMA journal_mode=WAL`
- `PRAGMA synchronous=NORMAL`
- `PRAGMA foreign_keys=ON`
- `PRAGMA busy_timeout` from env (`SQLITE_BUSY_TIMEOUT_MS`, default 8000)
- retry wrapper `sqlite_retry(...)` with backoff (`SQLITE_RETRY_ATTEMPTS`, `SQLITE_RETRY_BASE_MS`)

## 15) What AI Knows Today (Practically)
The AI can currently reason with and/or retrieve:
- active holdings and watchlist
- live-ish portfolio snapshot from dashboard service
- notes/tasks/reports
- morning brief and proactive gaps
- thesis and decision history memory
- extracted report facts from recent reports
- user style and preference profile summaries
- current page/path context and short conversation history

## 16) Known Failure Modes / Limitations
1. Entity/ticker ambiguity still possible on very short prompts
- Example class: a free-form phrase can be mistaken as ticker intent in some paths.

2. ReAct coverage is broad but not universal
- Some complex requests still route to deterministic handlers or fallback.

3. Voice reliability depends on browser speech APIs
- Not all browsers provide stable `SpeechRecognition`.

4. Vision quality depends on image clarity and model output quality.

5. UI contract tests need full FastAPI test dependency set in environment.

## 17) Verification Status (Current Session)
Executed tests:
- Passed:
  - `app.tests.test_ai_react_service`
  - `app.tests.test_ai_longterm_memory`
- Could not run completely in this environment:
  - `app.tests.test_ai_ui_contracts` failed import (`ModuleNotFoundError: fastapi`)

Interpretation:
- Core ReAct + memory logic validated.
- UI API contract test requires installing test/runtime dependency stack (FastAPI) in current Python env.

## 18) Operational Notes
- Chat keeps in-memory pending clarification state only for immediate turn; avoids stale carry-over across sessions.
- Session ID is persisted in localStorage and used by server chat memory store.
- Proactive badge polls once per minute.
- Morning brief card is injected once per day in local UI state.

## 19) Suggested Next Enhancements (Priority)
1. Expand ReAct planner coverage for more comparative/strategy questions before fallback.
2. Add confidence guardrails for ticker extraction to reduce accidental symbol jumps.
3. Add explicit "I’m unsure" clarification prompts before any company navigation when ticker confidence is low.
4. Add report-fact source linking in chat cards for traceability.
5. Stabilize test env with pinned FastAPI + TestClient dependencies and CI run.

---
If you want, I can also generate a second document as a product-facing version (non-technical) for daily use, and keep this one as the engineering spec.
