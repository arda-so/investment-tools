# Design Map - Investor OS v2

This document maps current UI design to exact template code locations.

## How to use this file
- Open route in browser.
- Capture screenshot (desktop + mobile).
- Compare against referenced template lines.
- Edit only the section that controls that visual area.

## 1. Global Shell
Route scope: all pages extending base template.

Screenshot slots:
- [ ] Desktop full app frame
- [ ] Command bar overlay (`Cmd/Ctrl+K`)

Code anchors:
- Theme variables: `app/templates/base.html:9`
- Global wide wrapper (`max-width: 1920px`): `app/templates/base.html:21`
- Global primitives (`.card`, `.btn`, inputs): `app/templates/base.html:31`
- Command bar markup: `app/templates/base.html:60`
- Command bar keyboard logic: `app/templates/base.html:82`

## 2. Dashboard
Route: `/dashboard`

Screenshot slots:
- [ ] Top nav + page title
- [ ] Live News Wire
- [ ] Index scoreboard cards
- [ ] Morning/Low/Earnings tri-grid
- [ ] Rates & Commodities

Code anchors:
- Dashboard page-level CSS and nav: `app/templates/dashboard.html:4`
- Dashboard body root: `app/templates/components/dashboard_body.html:26`
- Live News Wire: `app/templates/components/dashboard_body.html:36`
- Index cards: `app/templates/components/dashboard_body.html:75`
- Morning / 52-week lows / Earnings: `app/templates/components/dashboard_body.html:90`
- Macro section: `app/templates/components/dashboard_body.html:171`
- Morning bullet sentiment coloring JS: `app/templates/components/dashboard_body.html:223`

## 3. Portfolio (Command Center)
Route: `/my_universe`

Screenshot slots:
- [ ] Sidebar + header
- [ ] AUM scoreboard
- [ ] Spotlight command bar
- [ ] Positions + radar split view
- [ ] Mobile stacked layout

Code anchors:
- Full-screen overrides: `app/templates/my_universe.html:5`
- Sidebar styles: `app/templates/my_universe.html:33`
- Main content area: `app/templates/my_universe.html:58`
- Header/actions: `app/templates/my_universe.html:264`
- Scoreboard: `app/templates/my_universe.html:283`
- Spotlight command bar: `app/templates/my_universe.html:305`
- Main 2-column grid: `app/templates/my_universe.html:130`
- Mobile behavior: `app/templates/my_universe.html:224`

## 4. Reports (Newsstand + Dense Inbox)
Route: `/reports`

Screenshot slots:
- [ ] Priority Reads cards
- [ ] Archive row density
- [ ] Hover arrow + unread dot
- [ ] Keyboard selection state

Code anchors:
- Reports CSS root: `app/templates/reports.html:5`
- Priority cards: `app/templates/reports.html:175`
- Dense archive row styles: `app/templates/reports.html:76`
- Unread/read typography: `app/templates/reports.html:105`
- Right metadata (tags, score, time): `app/templates/reports.html:117`
- Archive loop rendering: `app/templates/reports.html:221`
- Keyboard nav (`j/k/Enter`): `app/templates/reports.html:265`

## 5. Organizer Workspace
Route: `/organizer`

Screenshot slots:
- [ ] Sidebar + workspace split
- [ ] Daily Log panel
- [ ] Day Priority Board
- [ ] Collapsible hubs (Tasks/To-Do/Notes)
- [ ] Mobile layout

Code anchors:
- Full-screen wrapper: `app/templates/organizer.html:5`
- Organizer layout system: `app/templates/components/organizer_body.html:6`
- Sidebar shell: `app/templates/components/organizer_body.html:24`
- Main content container: `app/templates/components/organizer_body.html:63`
- Daily Log card: `app/templates/components/organizer_body.html:297`
- Priority board card: `app/templates/components/organizer_body.html:329`
- Quick Capture row alignment: `app/templates/components/organizer_body.html:168`
- Collapsible hubs: `app/templates/components/organizer_body.html:348`
- First-3 display behavior (example): `app/templates/components/organizer_body.html:354`
- Mobile behavior: `app/templates/components/organizer_body.html:257`

## 6. Company Detail (Deal Room)
Route: `/company_file?t=ADBE` (all tickers use same template)

Screenshot slots:
- [ ] Hero identity card
- [ ] Moat + Conviction + Valuation stack
- [ ] Research notebook
- [ ] Action sidebar
- [ ] IR modal

Code anchors:
- Deal-room page CSS: `app/templates/company_detail.html:4`
- Deal grid structure: `app/templates/company_detail.html:26`
- Body root and hero: `app/templates/components/company_detail_body.html:1`
- Moat section: `app/templates/components/company_detail_body.html:19`
- Conviction box: `app/templates/components/company_detail_body.html:37`
- Valuation & quality: `app/templates/components/company_detail_body.html:51`
- Research notebook: `app/templates/components/company_detail_body.html:85`
- Quick actions/SEC: `app/templates/components/company_detail_body.html:114`
- Competitors: `app/templates/components/company_detail_body.html:120`
- Tasks & reminders: `app/templates/components/company_detail_body.html:140`
- IR modal: `app/templates/components/company_detail_body.html:211`
- Deal localStorage + gauge JS: `app/templates/components/company_detail_body.html:271`

## 7. Visual Tokens (Current)
Primary palette direction:
- Background: slate/light gray surfaces
- Cards: white with soft border and subtle shadow
- Positive: emerald
- Negative: rose/red
- Accent: orange/salmon primary action
- Sidebar surfaces: dark navy (portfolio/organizer)

Typography direction:
- Sans serif for app body and data
- Serif hero title for company detail page
- Tabular numerics where finance values are shown

## 8. Editing Rules for Designers/Engineers
- Start from route template first, then component partial.
- Keep responsive breakpoints consistent with current page behavior.
- Preserve keyboard interactions on `/reports`.
- Preserve manual fallback controls while adding AI-first interactions.
- Prefer changing one page at a time and validating the five core routes:
  - `/dashboard`
  - `/my_universe`
  - `/reports`
  - `/organizer`
  - `/company_file?t=ADBE`

---
Last updated: 2026-02-17
