# Session Handoff — 2026-03-02 (UI: Organizer Spaces Tab + Ticker Board View)

**Branch:** `cloud-cutover-2026-02-25`
**Last deployed revision:** `investor-tools-app-00160-6mc` (europe-west1, 100% traffic)
**Cloud proxy access:** `http://127.0.0.1:8088` via `gcloud run services proxy`

---

## What Was Done This Session

### Context
The goal was a ClickUp-inspired extension to the Organizer. An initial attempt changed the global nav to a vertical sidebar — that was reverted. The final implementation keeps the global nav untouched and adds the new UI as a tab inside the existing Organizer page (`/day`).

---

## 1. New Backend: `get_sidebar_data()` + `/api/sidebar-data`

### File: `app/services/workspace_os_service.py`
**Added function:** `get_sidebar_data()` — appended at the end of the file, just before `get_ticker_ai_context()`.

```python
def get_sidebar_data() -> dict:
```

- Queries `portfolio_positions_core` → list of `{ticker, name}` for portfolio holdings
- Queries `watchlist_core` → list of `{ticker, name}` for watchlist
- Enriches both with company names from `universe_registry_core` (single IN query)
- Counts open records from `investment_records_core WHERE status='open'`
- Returns: `{"portfolio": [...], "watchlist": [...], "open_tasks": N}`
- Graceful fallback: returns `{"portfolio": [], "watchlist": [], "open_tasks": 0}` if Postgres unavailable or any exception

### File: `app/routers/workspace_os.py`
**Added import:** `get_sidebar_data` added to the existing import block from workspace_os_service.
**Added endpoint** at the end of the file:

```python
@router.get("/api/sidebar-data")
def api_sidebar_data(request: Request):
    return JSONResponse(get_sidebar_data())
```

**Verified response (live):**
```json
{
  "portfolio": [
    {"ticker": "CRM", "name": "Salesforce, Inc."},
    {"ticker": "IT",  "name": "GARTNER INC"},
    {"ticker": "UNH", "name": "UNITEDHEALTH GROUP INC"}
  ],
  "watchlist": [
    {"ticker": "AAPL", "name": "Apple Inc."},
    ...
  ],
  "open_tasks": N
}
```
Note: HUBS was in portfolio but did not appear in this test — check `portfolio_positions_core` if needed.

---

## 2. Organizer Day Page (`/day`) — Greeting + Tabs

### File: `app/templates/workspace_os_day.html`

**What changed (everything is self-contained in this file, base.html is untouched):**

#### A. Greeting Banner
Replaces the old `.day-header` div. Shown above the tab bar.
```html
<div class="home-greeting">
  <div class="home-greeting-text">Good morning 👋</div>  <!-- "weekend" on Sat/Sun -->
  <div class="home-date">Monday, March 2, 2026</div>
  <div class="home-summary">
    <span class="home-summary-pill focus">N in focus</span>
    <span class="home-summary-pill agent">N agent signals</span>
    <span class="home-summary-pill backlog">N in backlog</span>
  </div>
</div>
```
The greeting text uses `{% if weekday in ('Saturday','Sunday') %}weekend{% else %}morning{% endif %}`.
The counts are Jinja template values passed from the router.

#### B. Tab Bar
Two tabs: **Today** and **Spaces**. Rendered as `<button class="day-tab">` inside `<div class="day-tabs">`.
- Active tab has `class="day-tab active"` and a purple bottom border (`border-bottom-color:#6366f1`).
- `onclick="switchDayTab('today',this)"` / `onclick="switchDayTab('spaces',this)"`
- Last selected tab is persisted in `localStorage` key `'dayTab'` and restored on page load.

#### C. Today Tab Panel (`#tabToday`)
All the existing content (quick-add input, Focus section, Agent Flagged section, Backlog section) is now wrapped inside:
```html
<div id="tabToday"> ... </div>
```
Nothing else changed in this panel — it's identical to the previous version of the page.

#### D. Spaces Tab Panel (`#tabSpaces`)
```html
<div id="tabSpaces" style="display:none;">
  <div class="spaces-grid" id="spacesGrid">
    <div class="space-loading">Loading spaces…</div>
  </div>
</div>
```
- Hidden by default (`display:none`), shown when Spaces tab is active.
- Contains a CSS grid (`spaces-grid`) that gets populated by JS.
- **Lazy load**: data is only fetched from `/api/sidebar-data` on the first time the Spaces tab is opened (`_spacesLoaded` flag prevents repeat fetches).

#### E. JavaScript (all inline in workspace_os_day.html `<script>` block)

**`switchDayTab(tab, btn)`**
- Toggles `.active` class on tab buttons
- Shows/hides `#tabToday` and `#tabSpaces`
- Saves `'dayTab'` to localStorage
- Calls `loadSpaces()` when switching to the spaces tab

**`loadSpaces()`**
- Checks `_spacesLoaded` flag (only runs once per page load)
- Fetches `/api/sidebar-data`
- Calls `renderSpaces(data)` on success
- On error: shows "Could not load spaces." message

**`renderSpaces(data)`**
- Takes the API response `{portfolio:[...], watchlist:[...]}`
- Builds two space cards: Portfolio 📈 and Watchlist 👁
- Each card has a header (icon + title + count + collapse arrow) and a body with company links
- Each company link: `<a href="/workspace/ticker/{TICKER}">` showing ticker (bold) + name (muted)
- Empty state: "None" shown if a space has no entries
- Cards start expanded (▼ arrow)

**`toggleSpaceCard(id, headerEl)`**
- Toggles `.collapsed` class on the items div (CSS hides it when collapsed)
- Flips the arrow text between ▼ and ▶

**On-load restore:**
```js
var last = localStorage.getItem('dayTab') || 'today';
if(last === 'spaces'){ switchDayTab('spaces', btn); }
```

---

## 3. Ticker Page (`/workspace/ticker/{ticker}`) — List/Board Views

### File: `app/templates/workspace_os_ticker.html`

**Two additions (no other changes):**

#### A. Timeline data passed to JS
Right at the start of `{% block body %}`, before the macro definition:
```html
<script>var _TIMELINE_DATA = {{ timeline | tojson }};</script>
```
This makes the server-rendered timeline data available to JS for the board view without an extra API call.

#### B. Views bar
Added immediately before the AI insight box:
```html
<div class="views-bar">
  <button class="view-btn active" data-view="list" onclick="switchView('list',this)">▣ List</button>
  <button class="view-btn" data-view="board" onclick="switchView('board',this)">▦ Board</button>
</div>
```

#### C. List/Board containers
The existing `<div class="tl-list" id="tlList">` is now wrapped:
```html
<div id="viewList">
  <div class="tl-list" id="tlList"> ... </div>
</div>
<div id="viewBoard" style="display:none;"></div>
```

#### D. Board JavaScript
**`BOARD_COLS`** — array of 4 column definitions: thought/action/decision/event

**`buildBoard(timeline)`**
- Groups timeline items by `r.kind` into 4 buckets
- Renders 4 `.board-col` divs side by side (flex layout)
- Each card shows: title, date, open/done badge, Done + Delete buttons
- Unknown kinds fall into the `action` bucket
- Done/Delete buttons call `tlAction()` (the existing function — no duplication)

**`switchView(view, btn)`**
- Toggles `.active` on view buttons
- Shows `#viewList` or `#viewBoard`
- When switching to board: calls `buildBoard(_TIMELINE_DATA)` and injects HTML into `#viewBoard`
- Persists last view in localStorage: key `'tickerView_' + _TICKER` (e.g. `tickerView_CRM`)

**On-load restore:**
```js
var last = localStorage.getItem('tickerView_' + _TICKER) || 'list';
if(last !== 'list'){ switchView(last, btn); }
```

**`tlAddRecord()` was updated:** new records are `unshift`ed into `_TIMELINE_DATA` so the board stays in sync if the user switches to board view after adding a record.

---

## 4. What Was NOT Changed

- **`app/templates/base.html`** — completely untouched. Global horizontal top nav is exactly as it was.
- **`app/routers/dashboard.py`**, **`app/services/postgres_core_service.py`**, and all other backend files — untouched.
- **`app/templates/workspace_os_ticker.html` thesis card, AI insight box, quick-add, action functions** — all untouched.

---

## 5. File Change Summary

| File | Change |
|------|--------|
| `app/services/workspace_os_service.py` | Added `get_sidebar_data()` function (appended before `get_ticker_ai_context`) |
| `app/routers/workspace_os.py` | Added `get_sidebar_data` import + `GET /api/sidebar-data` endpoint |
| `app/templates/workspace_os_day.html` | Greeting banner + Today/Spaces tab bar + Spaces tab panel + all JS |
| `app/templates/workspace_os_ticker.html` | `_TIMELINE_DATA` injection + Views bar + Board view HTML + Board JS |
| `app/templates/base.html` | **No changes** (global nav is original horizontal top bar) |

---

## 6. Key Architectural Notes for Next Agent

- **`/api/sidebar-data`** is a standalone GET endpoint with no auth — same as all other internal API routes. Safe to call from any page.
- **Spaces data is loaded lazily** on first tab switch. If you want it pre-loaded, remove the `_spacesLoaded` flag check in `loadSpaces()`.
- **Board view is entirely client-side** — no server round-trip. It uses `_TIMELINE_DATA` which is the Jinja `{{ timeline | tojson }}` injected on page load.
- **localStorage keys in use (new):**
  - `'dayTab'` → `'today'` or `'spaces'` (which tab was last active on `/day`)
  - `'tickerView_{TICKER}'` → `'list'` or `'board'` (per ticker, e.g. `tickerView_CRM`)
- **The `/workspace/ticker/{ticker}` page** is its own template (`workspace_os_ticker.html`) completely separate from the day view. Both pages extend `base.html`.

---

## 7. Possible Next Steps (Not Done Yet)

- Add a **Research Docs** space card to the Spaces tab (links to `/organizer/memory`)
- Add a **Theses & Goals** space card (links to `/my_universe?tab=watchlist`)
- Add **open task count badges** per ticker on the Spaces company links (requires an extra query or API change)
- Make **space card collapse state** persistent (currently resets on page reload)
- Add a **"+ Add to watchlist"** shortcut directly from the Spaces tab
- Board view: make cards **draggable** between columns (pure front-end, no backend needed for visual-only reorder)

---

## 8. How to Verify

```bash
# API check
curl http://127.0.0.1:8088/api/sidebar-data
# → {"portfolio":[{"ticker":"CRM","name":"Salesforce, Inc."},...], "watchlist":[...], "open_tasks":N}

# Page checks
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8088/day
# → 200

curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8088/workspace/ticker/CRM
# → 200

# Syntax check
.venv-memory/bin/python -m py_compile app/services/workspace_os_service.py app/routers/workspace_os.py
# → (no output = OK)

# Smoke tests
.venv-memory/bin/python tools/run_replay_tests.py --smoke
# → === PASS ===
```

**Visual checks in browser (http://127.0.0.1:8088/day):**
1. Greeting banner visible at top with "Good morning 👋", date, 3 summary pills
2. "Today" and "Spaces" tab bar visible below greeting
3. Today tab shows existing Focus/Agent Flagged/Backlog sections (no regression)
4. Click "Spaces" tab → Portfolio 📈 and Watchlist 👁 cards appear with company links
5. Click a company link → navigates to `/workspace/ticker/{TICKER}`
6. On ticker page: "▣ List" and "▦ Board" buttons visible below thesis card
7. Click "▦ Board" → 4 Kanban columns appear (Thoughts/Actions/Decisions/Events)
8. Refresh ticker page → last view (list/board) is restored from localStorage
9. Refresh `/day` → last tab (today/spaces) is restored from localStorage
