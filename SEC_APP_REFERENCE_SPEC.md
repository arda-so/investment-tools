# SEC Intelligence App — Complete Reference Specification

> **Purpose**: This document provides everything needed to build a standalone SEC intelligence app
> from scratch, replicating the exact design system, architecture, and UI patterns from the
> original Investment_Tools workspace. A new Claude agent should be able to build the entire
> app using only this spec.

---

## 1. Architecture Overview

### Stack
- **Backend**: FastAPI (Python 3.11+)
- **Database**: PostgreSQL (strict — no SQLite fallback)
- **Frontend**: Vanilla HTML/CSS/JS (no React/Vue — inline in Jinja2 templates)
- **SEC Data**: `edgartools` library (free, no API key needed)
- **LLM**: Claude API for filing analysis, earnings analysis, cascade reasoning
- **Deployment**: Google Cloud Run (containerized)
- **File Storage**: GCS or local filesystem

### Key Principles
- No paid financial APIs (SEC EDGAR only for filings)
- Background daemon threads for polling (not cron)
- Event-driven architecture with idempotent event bus
- All decisions auditable (store rejections, not just approvals)
- Metadata-first fetching (check before expensive downloads)

---

## 2. CSS Design System

### 2.1 Custom Properties — Light Mode (`:root`)

```css
/* Sidebar */
--ws-sidebar-bg: #1e1f2e;
--ws-sidebar-border: rgba(255,255,255,.06);
--ws-sidebar-text: #c3cbe4;
--ws-sidebar-muted: #7b85a4;
--ws-sidebar-hover: rgba(255,255,255,.06);
--ws-sidebar-active: rgba(99,102,241,.3);

/* Accent — unified indigo */
--ws-accent: #6366f1;
--ws-accent-soft: rgba(99,102,241,.08);

/* Semantic Colors */
--ws-agent: #f97316;
--ws-agent-soft: #fff7ed;
--ws-success: #10b981;
--ws-success-soft: rgba(16,185,129,.08);
--ws-danger: #ef4444;
--ws-danger-soft: rgba(239,68,68,.08);
--ws-warning: #f59e0b;
--ws-warning-soft: rgba(245,158,11,.08);
--ws-info: #3b82f6;
--ws-info-soft: rgba(59,130,246,.08);
--ws-purple: #8b5cf6;
--ws-purple-soft: rgba(139,92,246,.08);

/* Layout */
--ws-board-bg: #f8fafc;
--ws-col-bg: #f8fafc;
--ws-card-hover: rgba(99,102,241,.03);
--ws-radius: 10px;

/* Typography */
--ws-font: 'Inter', system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;

/* Spacing Scale */
--sp-1: 4px;
--sp-2: 8px;
--sp-3: 12px;
--sp-4: 16px;
--sp-6: 24px;
--sp-8: 32px;
--sp-12: 48px;
```

### 2.2 Dark Mode (`body.theme-dark`)

```css
--ws-sidebar-bg: #0b0d14;
--ws-sidebar-border: rgba(255,255,255,.05);
--ws-sidebar-text: #c3cbe4;
--ws-sidebar-muted: #6b7394;
--ws-sidebar-hover: rgba(255,255,255,.06);
--ws-sidebar-active: rgba(129,140,248,.25);
--ws-accent: #818cf8;
--ws-accent-soft: rgba(129,140,248,.1);
--ws-board-bg: var(--bg);
--ws-col-bg: var(--panel);
--ws-agent-soft: rgba(251,191,36,.08);
--ws-success-soft: rgba(74,222,128,.1);
--ws-danger-soft: rgba(248,113,113,.08);
--ws-warning-soft: rgba(251,191,36,.08);
--ws-info-soft: rgba(96,165,250,.08);
--ws-purple-soft: rgba(167,139,250,.08);
--ws-card-hover: rgba(129,140,248,.05);
```

### 2.3 Animations

```css
@keyframes ws-shimmer {
  0% { background-position: -200% center; }
  100% { background-position: 200% center; }
}

@keyframes ws-pulse-badge {
  0%, 100% { transform: scale(1); }
  50% { transform: scale(1.1); }
}

@keyframes ws-glow-breathe {
  0%, 100% { opacity: .4; }
  50% { opacity: .7; }
}

@keyframes ws-pulse {
  0%, 100% { opacity: .3; transform: scale(.8); }
  50% { opacity: 1; transform: scale(1.1); }
}

@keyframes ws-space-fade-in {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: translateY(0); }
}
```

### 2.4 Global Transitions & Polish

```css
button, a, input, select, textarea, [onclick] {
  transition: color .15s ease, background .15s ease,
              border-color .15s ease, box-shadow .15s ease,
              transform .15s ease, opacity .15s ease;
}

input:focus, select:focus, textarea:focus {
  outline: none;
  border-color: var(--ws-accent) !important;
  box-shadow: 0 0 0 3px rgba(99,102,241,.1) !important;
}

::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(0,0,0,.1); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: rgba(0,0,0,.18); }
body.theme-dark ::-webkit-scrollbar-thumb { background: rgba(255,255,255,.08); }
body.theme-dark ::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,.15); }
```

### 2.5 Key Design Tokens Summary

| Token | Value |
|-------|-------|
| Accent | Indigo `#6366f1` (light) / `#818cf8` (dark) |
| Primary Radius | 10px (cards), 8px (buttons), 12px (columns), 14px (hero cards) |
| Font | Inter + system fallbacks, `-0.01em` tracking |
| Semantic Colors | Success `#10b981`, Danger `#ef4444`, Warning `#f59e0b`, Info `#3b82f6`, Purple `#8b5cf6`, Agent `#f97316` |
| Spacing | 8px base unit, scales: 4/8/12/16/24/32/48px |
| Transitions | `.15s ease` standard, `.2s` for major state changes |
| Shadows | Minimal `0 1px 3px rgba(0,0,0,.04)`, elevated on hover `0 4px 16px` |

---

## 3. UI Components

### 3.1 Main Shell Layout

```css
.ws-shell {
  display: flex;
  height: 100%;
  overflow: hidden;
  background: var(--bg);
  font-family: var(--ws-font);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  font-feature-settings: 'cv02','cv03','cv04','cv11';
  letter-spacing: -0.01em;
}
```

### 3.2 Sidebar

```html
<aside class="ws-sidebar" id="wsSidebar">
  <div class="ws-sidebar-header">
    <span class="ws-brand" onclick="toggleSidebar()">
      <span class="ws-brand-icon">💎</span>
      <span class="ws-brand-label">WORKSPACE</span>
    </span>
    <button class="ws-sidebar-collapse" onclick="toggleSidebar()">‹</button>
  </div>

  <button class="ws-qc-btn" onclick="openQC()">
    <span class="ws-qc-icon"><svg viewBox="0 0 24 24">...</svg></span>
    <span class="ws-qc-label">Quick Capture</span>
    <span class="ws-qc-kbd">C</span>
  </button>

  <nav class="ws-nav">
    <button class="ws-nav-item active" data-space="today" onclick="switchSpace('today',this)">
      <span class="ws-nav-icon"><svg viewBox="0 0 24 24">...</svg></span>
      <span class="ws-nav-text">Today</span>
      <span class="ws-nav-badge" id="badge-today">5</span>
    </button>
  </nav>

  <div class="ws-sidebar-footer">
    <button class="ws-footer-btn" onclick="toggleDark()">
      <span class="ws-footer-btn-icon"><svg>...</svg></span>
      <span class="ws-footer-btn-label">Dark Mode</span>
    </button>
  </div>
</aside>
```

```css
.ws-sidebar {
  width: 244px; flex-shrink: 0; display: flex; flex-direction: column;
  background: var(--ws-sidebar-bg); border-right: none; overflow: hidden;
  transition: width .22s cubic-bezier(.4, 0, .2, 1);
}
.ws-sidebar.collapsed { width: 56px; }
.ws-sidebar-header { display: flex; align-items: center; justify-content: space-between; padding: 14px 14px 6px; min-height: 44px; }

.ws-brand { display: flex; align-items: center; gap: 9px; padding: 5px 10px; border-radius: 10px; cursor: default; transition: background .15s; }
.ws-brand:hover { background: var(--ws-sidebar-hover); }
.ws-brand-label {
  font-size: 14px; font-weight: 800; letter-spacing: -.02em; white-space: nowrap;
  background: linear-gradient(135deg, #a78bfa, #818cf8, #60a5fa);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
}

.ws-nav-item {
  display: flex; align-items: center; gap: 11px; width: calc(100% - 24px); text-align: left;
  padding: 7px 10px; border: none; background: none; cursor: pointer;
  color: var(--ws-sidebar-text); font-size: 13px; font-weight: 500; border-radius: 8px;
  margin: 1px 12px; transition: all .15s ease; white-space: nowrap; overflow: hidden;
}
.ws-nav-item:hover { background: var(--ws-sidebar-hover); color: #fff; transform: translateX(2px); }
.ws-nav-item.active {
  color: #fff; font-weight: 600;
  background: linear-gradient(135deg, rgba(99,102,241,.25), rgba(139,92,246,.15));
  box-shadow: inset 0 0 0 1px rgba(99,102,241,.2);
}

.ws-nav-icon {
  flex-shrink: 0; width: 26px; height: 26px; display: flex; align-items: center; justify-content: center;
  border-radius: 7px; transition: all .15s; --icon-color: #818cf8; --icon-bg: rgba(129,140,248,.12);
  background: var(--icon-bg); color: var(--icon-color);
}

.ws-nav-badge {
  background: rgba(99,102,241,.2); color: #a5b4fc; border-radius: 6px; padding: 2px 7px;
  font-size: 10px; font-weight: 700; flex-shrink: 0; min-width: 18px; text-align: center;
  animation: ws-pulse-badge 2s ease-in-out infinite;
}
```

**Per-section icon colors:**
```css
.ws-nav-item[data-space="dashboard"] .ws-nav-icon { --icon-color: #60a5fa; --icon-bg: rgba(96,165,250,.15); }
.ws-nav-item[data-space="today"] .ws-nav-icon { --icon-color: #34d399; --icon-bg: rgba(52,211,153,.15); }
.ws-nav-item[data-space="portfolio"] .ws-nav-icon { --icon-color: #a78bfa; --icon-bg: rgba(167,139,250,.15); }
.ws-nav-item[data-space="search"] .ws-nav-icon { --icon-color: #f472b6; --icon-bg: rgba(244,114,182,.15); }
.ws-nav-item[data-space="sec"] .ws-nav-icon { --icon-color: #fbbf24; --icon-bg: rgba(251,191,36,.12); }
```

### 3.3 Topbar

```html
<div class="ws-topbar" id="wsTopbar">
  <div class="ws-topbar-title" id="wsSpaceTitle">📋 Today</div>
  <div class="ws-views-bar">
    <button class="ws-view-btn active" data-view="list" onclick="switchView('list',this)">📋 List</button>
    <button class="ws-view-btn" data-view="board" onclick="switchView('board',this)">▦ Board</button>
  </div>
  <div class="ws-topbar-actions">
    <button class="ws-topbar-btn ai-btn" onclick="toggleAI();" id="aiToggleBtn">🤖 AI</button>
  </div>
</div>
```

```css
.ws-topbar {
  display: flex; align-items: center; gap: 12px; padding: 0 40px; height: 56px;
  flex-shrink: 0; background: var(--panel); border-bottom: none; z-index: 10;
  box-shadow: 0 1px 0 var(--border);
}
.ws-topbar-title { font-size: 20px; font-weight: 800; color: var(--text); letter-spacing: -.02em; }
.ws-view-btn { padding: 6px 14px; border: none; background: transparent; color: var(--muted); font-size: 13px; font-weight: 600; border-radius: 8px; cursor: pointer; }
.ws-view-btn:hover { background: var(--panel2); color: var(--text); }
.ws-view-btn.active { background: var(--ws-accent-soft); color: var(--ws-accent); font-weight: 700; }
.ws-topbar-btn.ai-btn { background: var(--ws-accent); color: #fff; }
.ws-topbar-btn.ai-btn:hover { filter: brightness(1.1); }
```

### 3.4 Cards

```html
<div class="today-card" data-type="filing" data-id="123" onclick="openPanel('filing',123)">
  <div class="today-card-icon">📄</div>
  <div class="today-card-body">
    <div class="today-card-title">AAPL 8-K Filing</div>
    <div class="today-card-sub">8-K · 2024-03-15 · Revenue guidance update</div>
    <div class="today-card-meta">
      <span class="ws-badge ticker">$AAPL</span>
      <span class="ws-badge high">🔴 material</span>
    </div>
  </div>
  <div class="today-card-btn">
    <button class="ws-act-btn done" onclick="event.stopPropagation();">Read</button>
    <button class="ws-act-btn del" onclick="event.stopPropagation();">✕</button>
  </div>
</div>
```

```css
.today-card {
  display: flex; gap: 14px; padding: 14px 16px;
  background: var(--panel); border-radius: 12px; margin-bottom: 8px;
  box-shadow: 0 1px 3px rgba(0,0,0,.04); cursor: pointer; transition: all .15s;
}
.today-card:hover { transform: translateY(-1px); box-shadow: 0 4px 16px rgba(99,102,241,.1); }
.today-card.urgent { border-left: 3px solid var(--danger); }
.today-card.active { border-left: 3px solid var(--ws-accent); }
.today-card-icon {
  font-size: 20px; width: 36px; height: 36px; display: flex; align-items: center;
  justify-content: center; background: var(--ws-accent-soft); border-radius: 10px;
}
.today-card-title { font-size: 14px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.today-card-sub { font-size: 12px; color: var(--muted); margin-top: 3px; }
.today-card-btn { display: flex; gap: 4px; flex-shrink: 0; }
```

### 3.5 Lanes (Section Groups)

```css
.today-lane { margin-bottom: 24px; }
.today-lane-header { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.today-lane-title { font-size: 14px; font-weight: 800; color: var(--text); }
.today-lane-badge { background: var(--ws-accent); color: #fff; font-size: 10px; padding: 2px 8px; border-radius: 10px; }
.today-lane-action { margin-left: auto; font-size: 11px; color: var(--ws-accent); cursor: pointer; background: none; border: none; }
```

### 3.6 Slide-Out Detail Panel

```html
<div class="today-panel" id="todayPanel">
  <div class="today-panel-inner">
    <div class="today-panel-header">
      <button class="today-panel-close" onclick="closePanel()">✕</button>
      <div class="today-panel-title" id="panelTitle"></div>
      <span class="today-panel-badge" id="panelBadge"></span>
    </div>
    <div class="today-panel-body" id="panelBody"></div>
    <div class="today-panel-actions" id="panelActions"></div>
  </div>
</div>
```

```css
.today-panel { width: 0; overflow: hidden; flex-shrink: 0; transition: width .25s ease; background: var(--panel); position: sticky; top: 0; max-height: 100vh; }
.today-panel.open { width: 420px; border-left: 1px solid var(--border); }
.today-panel-header { display: flex; gap: 8px; padding: 14px 16px 10px; border-bottom: 1px solid var(--border); flex-shrink: 0; position: sticky; top: 0; background: var(--panel); }
.today-panel-title { font-size: 14px; font-weight: 800; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.today-panel-body { flex: 1; padding: 16px; overflow-y: auto; }
.tp-section { margin-bottom: 16px; }
.tp-section-title { font-size: 11px; font-weight: 800; color: var(--muted); text-transform: uppercase; margin-bottom: 6px; }
.tp-field { font-size: 13px; color: var(--text); line-height: 1.5; }
```

### 3.7 Global Slide Panel (Full-width Reports)

```css
.slide-panel { position: fixed; inset: 0; z-index: 11000; display: flex; justify-content: flex-end; }
.slide-panel-backdrop { position: absolute; inset: 0; background: rgba(15,23,42,.35); }
.slide-panel-drawer {
  width: min(680px,90vw); height: 100%; background: var(--panel);
  border-left: 1px solid var(--border); display: flex; flex-direction: column;
  box-shadow: -8px 0 30px rgba(15,23,42,.15);
}
.slide-panel-head { padding: 16px 20px; border-bottom: 1px solid var(--border); }
.slide-panel-body { flex: 1; overflow: auto; padding: 20px; font-size: 13px; }
```

### 3.8 Badges

```css
.ws-badge { display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 11px; font-weight: 600; border: none; background: var(--panel2); color: var(--muted); }
.ws-badge.ticker { background: var(--ws-accent-soft); color: var(--ws-accent); }
.ws-badge.high { background: var(--ws-danger-soft); color: var(--ws-danger); }
.ws-badge.agent { background: var(--ws-agent-soft); color: var(--ws-agent); }
```

### 3.9 Action Buttons

```css
.ws-act-btn { border: none; background: var(--panel2); color: var(--text); border-radius: 8px; padding: 7px 14px; font-size: 12px; font-weight: 600; cursor: pointer; white-space: nowrap; transition: all .15s; }
.ws-act-btn:hover { background: var(--border); transform: translateY(-1px); }
.ws-act-btn.done { background: var(--ws-success-soft); color: var(--ws-success); }
.ws-act-btn.done:hover { background: var(--ws-success); color: #fff; }
.ws-act-btn.dismiss { background: var(--ws-danger-soft); color: var(--ws-danger); }
.ws-act-btn.dismiss:hover { background: var(--ws-danger); color: #fff; }
```

### 3.10 Quick Capture Modal

```css
.qc-overlay { position: fixed; inset: 0; background: rgba(15,23,42,.35); z-index: 9000; display: none; align-items: flex-start; justify-content: center; padding-top: min(20vh, 140px); backdrop-filter: blur(4px); }
.qc-overlay.open { display: flex; }
.qc-card { background: var(--panel); border: none; border-radius: 16px; padding: 0; width: min(540px, calc(100vw - 32px)); box-shadow: 0 24px 64px rgba(15,23,42,.2), 0 8px 16px rgba(15,23,42,.08); }
.qc-title-input { flex: 1; height: 48px; border: none; border-radius: 14px; padding: 0 16px; font-size: 15px; font-weight: 500; background: transparent; color: var(--text); }
.qc-submit-btn { padding: 8px 18px; background: var(--ws-accent); color: #fff; border: none; border-radius: 10px; font-size: 13px; font-weight: 700; }
```

### 3.11 Tab Bar

```css
.ws-tabbar { display: flex; align-items: flex-end; gap: 0; padding: 0 8px; height: 42px; flex-shrink: 0; background: linear-gradient(180deg, var(--panel) 0%, var(--panel) 100%); border-bottom: 1px solid var(--border); overflow-x: auto; scrollbar-width: none; }
.ws-tab { display: flex; align-items: center; gap: 7px; padding: 7px 16px 8px; border: none; background: transparent; color: var(--muted); font-size: 12.5px; font-weight: 600; cursor: pointer; white-space: nowrap; border-radius: 8px 8px 0 0; }
.ws-tab:hover { color: var(--text); background: rgba(0,0,0,.04); }
.ws-tab.active { color: var(--text); font-weight: 700; background: var(--bg, #f6f7fb); box-shadow: 0 -1px 0 var(--border), -1px 0 0 var(--border), 1px 0 0 var(--border); }
```

### 3.12 Tables

```css
.sp-review-table { width: 100%; border-collapse: separate; border-spacing: 0; margin-bottom: 16px; }
.sp-review-table th { font-size: 10px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); padding: 10px 12px; text-align: left; border-bottom: 2px solid var(--border); font-weight: 700; }
.sp-review-table td { font-size: 13px; color: var(--text); padding: 12px 12px; border-bottom: 1px solid rgba(0,0,0,.04); vertical-align: middle; }
.sp-review-table tbody tr { transition: all .12s; border-radius: 8px; }
.sp-review-table tbody tr:hover { background: var(--ws-accent-soft); }
```

### 3.13 Summary Stat Cards

```css
.sp-summary-card { flex: 1; min-width: 140px; border-radius: 14px; padding: 18px 22px; position: relative; overflow: hidden; }
.sp-summary-card:nth-child(1) { background: linear-gradient(135deg, #6366f1 0%, #818cf8 100%); color: #fff; }
.sp-summary-card:nth-child(2) { background: linear-gradient(135deg, #8b5cf6 0%, #a78bfa 100%); color: #fff; }
.sp-summary-card:nth-child(3) { background: linear-gradient(135deg, #3b82f6 0%, #60a5fa 100%); color: #fff; }
.sp-summary-card:nth-child(4) { background: linear-gradient(135deg, #10b981 0%, #34d399 100%); color: #fff; }
.sp-summary-card::after { content: ''; position: absolute; top: -20px; right: -20px; width: 80px; height: 80px; border-radius: 50%; background: rgba(255,255,255,.1); }
.sp-summary-val { font-size: 22px; font-weight: 900; margin-top: 4px; letter-spacing: -.02em; }
```

### 3.14 Form Inputs

```css
.ws-inline-input { flex: 1; height: 40px; border: 1px solid var(--border); border-radius: 10px; padding: 0 14px; font-size: 13px; background: var(--panel); color: var(--text); }
.ws-inline-input:focus { outline: none; border-color: var(--ws-accent); box-shadow: 0 0 0 3px rgba(99,102,241,.1); }
.ws-inline-select { height: 40px; border: 1px solid var(--border); border-radius: 10px; padding: 0 10px; font-size: 12px; background: var(--panel); color: var(--text); }
```

### 3.15 Hero / Next-Up Card

```css
.today-nextup {
  display: flex; gap: 16px; padding: 20px 22px;
  background: linear-gradient(135deg,#6366f1 0%,#818cf8 50%,#a78bfa 100%);
  border-radius: 14px; margin-bottom: 22px; cursor: pointer;
  box-shadow: 0 4px 16px rgba(99,102,241,.25); position: relative; overflow: hidden;
}
.today-nextup:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(99,102,241,.35); }
.today-nextup-label { font-size: 10px; color: rgba(255,255,255,.8); text-transform: uppercase; }
.today-nextup-name { font-size: 16px; font-weight: 800; color: #fff; }
.today-nextup-btn button { background: #fff; color: var(--ws-accent); padding: 8px 20px; border: none; border-radius: 8px; cursor: pointer; }
```

### 3.16 Board View (Kanban)

```css
.ws-board { display: flex; gap: 12px; height: 100%; align-items: flex-start; overflow-x: auto; padding-bottom: 20px; }
.ws-board-col { width: 260px; flex-shrink: 0; background: var(--ws-board-bg); border: none; border-radius: 12px; display: flex; flex-direction: column; max-height: calc(100vh - 160px); box-shadow: 0 1px 3px rgba(0,0,0,.04); }
.ws-col-header { padding: 12px 14px 10px; display: flex; align-items: center; gap: 7px; flex-shrink: 0; }
.ws-col-title { font-size: 11px; font-weight: 800; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); flex: 1; }
.ws-col-count { font-size: 10px; font-weight: 700; color: var(--muted); background: var(--panel2); border: 1px solid var(--border); border-radius: 999px; padding: 1px 6px; }
```

---

## 4. SEC Service Logic

### 4.1 SEC EDGAR Poller Service

**Purpose**: Auto-ingests SEC filings for monitored tickers. Routes events through the pipeline.

**External Library**: `edgartools`
```python
import edgar
edgar.set_identity("YourApp research@yourapp.local")
company = edgar.Company(ticker)
filings = list(company.get_filings(form=["8-K", "10-Q", "10-K", ...]))
for filing in filings[:limit]:
    accession = filing.accession_number
    form = filing.form
    filing_date = filing.filing_date
    markdown = filing.markdown()  # Full text download (expensive)
```

**Forms Polled**: `["8-K", "10-Q", "10-K", "6-K", "20-F", "DEF 14A", "DEFA14A", "PRE 14A", "DEF 14C"]`

**Key Functions**:

1. `ensure_poller_schema()` — Creates poll state + signal universe tables at startup

2. `_fetch_filing_metadata(ticker, forms, limit=8)` — Returns metadata only (accession, form, date, url) — NO markdown yet. Metadata-first = fast check before expensive download.

3. `_download_markdown(filing_obj, form)` — Downloads full text. Limits: 300KB for most forms, 1.5MB for annual forms (10-K, 10-Q, 20-F).

4. `_save_filing_text(ticker, form, accession, text)` — Saves to filesystem/GCS.

5. `_upsert_filing_core(ticker, form, filing_date, accession, doc_url, path, content)` — Inserts into `filings_core` if accession not seen. Content stored in DB so Cloud Run can read without GCS.

6. `_mark_seen(ticker, accession, form, filing_date, filing_id)` — Inserts into `sec_edgar_poll_state_core` for dedup. Key: `(ticker, accession)`.

7. `poll_ticker(ticker, forms, is_held, per_ticker_limit=8)` — Main entry point per ticker:
   - Phase 1: Fetch metadata only (fast)
   - Phase 2: Identify new filings via dedupe check
   - Phase 3: Download markdown + save + upsert + create events
   - Phase 4: Process events in parallel (`ThreadPoolExecutor(max_workers=4)`)

8. `poll_and_ingest_tickers(tickers=None, forms=None, held_only=False)` — Background loop entry point. Polls all monitored tickers + signal universe.

**Form 4 (Insider Trade) Filtering**:

9. `_is_significant_insider_trade(markdown_text, threshold_usd=500_000)`:
   - Transaction codes S (sale) or P (purchase) only — excludes grants/options
   - Filer must be CEO, CFO, President, Director, or Officer
   - Estimated value > $500K
   - NOT a 10b5-1 plan (auto-scheduled trades)

10. `poll_form4_for_tickers(tickers=None)` — Daily separate cycle. Creates `insider_trade_signal` events for significant trades only.

**Default Signal Universe** (12 macro/supply-chain tickers):
```
TSM, NVDA, ASML, INTC, MSFT, AMZN, GOOGL, META, JPM, COST, WMT, CAT
```

### 4.2 Events Service (Event Bus)

**Purpose**: Event bus + router. Stores filings as events, routes by type through processing pipelines.

**Key Functions**:

1. `create_event(source, ticker, event_type, occurred_at, payload, dedupe_key)` — Idempotent event creation. Payload stored as JSONB.

2. `process_event(event_ref)` — Main event router. Routes by `event_type`:
   - `sec_filing` → filing pipeline + monitor + earnings analysis
   - `universe_signal` → cascade analysis (noise-filtered)
   - `insider_trade_signal` → cascade analysis
   - `news_signal`/`monitor_trigger` → proposal monitor

3. `_process_sec_filing_event(eid, ticker, payload)`:
   - Calls `process_new_filings_pipeline([filing_id])` — chunk + entities + relationships
   - Calls `run_event_driven_monitor(force=True)` — generate proposals
   - If form in {8-K, 10-Q, 6-K, 20-F}: calls `analyze_latest_earnings_for_ticker()`

**Event Flow**:
```
SEC filing detected → create_event(type='sec_filing', payload={filing_id, form, accession, ...})
  ↓
process_event() routes to _process_sec_filing_event()
  ↓
filing pipeline (chunks + entities) + monitor (proposals) + earnings analysis
  ↓
event marked done, result cached
```

### 4.3 Earnings Transcript Service

**Purpose**: Extracts earnings transcripts from SEC filings (8-K, 6-K), analyzes with LLM.

**Key Functions**:

1. `refresh_earnings_transcripts_from_sec(ticker, max_filings=400, lookback_years=10)`:
   - Queries `filings_core` for 8-K/6-K in lookback window
   - Detects earnings content via markers: "operator:", "prepared remarks", "earnings call", "financial results", "revenue", "guidance"
   - Quality scoring: transcripts 0.35-0.95, press releases 0.45-0.95

2. `analyze_earnings_transcript(ticker, text, filing_date, ...)`:

**LLM Prompt** (JSON, temp=0.1):
```
You are analyzing an earnings document for {ticker}.
Extract structured intelligence:
- guidance_direction: "raised|maintained|lowered|withdrawn|none"
- tone: "confident|neutral|cautious|defensive"
- eps_vs_prior: "e.g. EPS $2.40 vs $2.18 prior (+10%) or N/A"
- revenue_actual: "e.g. Revenue $94.9B vs $89.5B (+6.0%) or N/A"
- margin_commentary: "specific gross/operating margin numbers and trend"
- key_signals: ["signal 1 with number", "signal 2 with number", "signal 3"]
- deflected_topics: "topics management avoided or gave vague answers on, or N/A"
- vs_prior_quarter: "how narrative/tone/guidance changed vs prior quarter, or N/A"
- summary: "2-sentence summary of most important takeaway with specific numbers"
```

3. `analyze_latest_earnings_for_ticker(ticker)` — Auto-triggered after 8-K/10-Q ingestion. Refreshes transcripts, finds unanalyzed ones, runs LLM analysis on top 3 by date.

### 4.4 Cascade Analysis (Proactive AI Service)

**Purpose**: When a signal hits one holding, analyze cascading effects on OTHER holdings.

1. `analyze_portfolio_cascades(trigger_ticker, trigger_signal, trigger_proposal_id=0)`:

**LLM Prompt** (JSON, temp=0.1):
```
You are a portfolio risk cascade engine.
TRIGGER: {trigger_ticker}
SIGNAL: {trigger_signal}
GRAPH-DERIVED IMPACT CHAINS: {supply/customer/competitor/sector links}
OTHER PORTFOLIO HOLDINGS: {financial context for each}

Analyze:
STEP 1: direct impacts (trigger → held names)
STEP 2: downstream impacts (first-order → secondary)
STEP 3: tertiary (only if specific + material)
STEP 4: magnitude filter (drop noise)

Return JSON:
{
  "cascades": [
    {
      "affected_ticker": "TK",
      "effect_type": "shared_supplier|same_sector|customer_dependency|regulatory_contagion|competitive_impact|macro_correlation",
      "effect_summary": "Specific explanation with reasoning chain",
      "relationship": "How connected",
      "confidence": 0.0-1.0,
      "severity": "low|medium|high|critical"
    }
  ]
}
```

2. `analyze_universe_signal_for_portfolio(universe_ticker, filing_text, filing_form, held_tickers)` — For non-held tickers: determines if filing is relevant to portfolio. Only includes cascades with confidence >= 0.5.

3. `_evaluate_signal_reasoning(ticker, signal_text, ...)`:

**LLM Prompt** (JSON, temp=0.0):
```
You are an Elite Fundamental Equity Analyst.

TARGET ASSET: {ticker}
STRUCTURED FINANCIAL DATA: {financial_context}
USER'S INVESTMENT THESIS: {thesis_text}
NEW SIGNAL: {signal_text}
LESSONS FROM PAST PREDICTION MISSES: {reflexion_rules}

STEP 1: RELEVANCE CHECK — if generic macro noise, return empty {}
STEP 2: DEEP NUMERICAL ANALYSIS — margins, growth, cash flow, segments, insider signals
STEP 3: CAUSE-EFFECT REASONING — X → Y margin impact → Z FCF impact → valuation
STEP 4: GENERATE INSIGHT CARDS

Return JSON:
{
  "insight_cards": [{"label": "SPECIFIC_LABEL", "text": "Analysis WITH numbers. Max 2 sentences."}],
  "confidence": "high|medium|low",
  "margin_impact": "specific margin analysis with numbers",
  "thesis_validation": "does data confirm or contradict thesis?",
  "risk_assessment": "key risks with quantification",
  "recommended_stance": "HOLD|ADD|TRIM|WATCH|EXIT"
}
```

### 4.5 SEC Ingest Pipeline

**Purpose**: Chunks filing text, extracts entities + relationships, builds ontology graph.

- Chunking: 1200 chars default, 200 char overlap, semantic boundary-aware
- Relationship types: `EXPOSED_TO, COMPETES_WITH, SUPPLIER_TO, CUSTOMER_OF, SIGNALS_MACRO, HEDGES_AGAINST, IMMUNE_TO`
- Criticality scoring: boosted by "sole-source", "primary supplier", "largest customer" (+0.2), penalized by "immaterial", "minor" (-0.2)

---

## 5. Database Schema

### 5.1 Core Filing Tables

```sql
-- Filings storage
CREATE TABLE IF NOT EXISTS filings_core (
    id BIGSERIAL PRIMARY KEY,
    ticker TEXT NOT NULL,
    form TEXT NOT NULL,
    date DATE,
    accession TEXT UNIQUE NOT NULL,
    doc_url TEXT,
    path TEXT,
    downloaded_at TIMESTAMPTZ DEFAULT now(),
    content TEXT
);

-- Deduplication state for polling
CREATE TABLE IF NOT EXISTS sec_edgar_poll_state_core (
    ticker TEXT NOT NULL,
    accession TEXT NOT NULL,
    form TEXT,
    filing_date DATE,
    filing_id BIGINT,
    processed_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (ticker, accession)
);

-- Signal universe (non-held monitored tickers)
CREATE TABLE IF NOT EXISTS signal_universe_core (
    ticker TEXT PRIMARY KEY,
    reason TEXT,
    category TEXT DEFAULT 'macro',
    added_at TIMESTAMPTZ DEFAULT now()
);
```

### 5.2 Events Table

```sql
CREATE TABLE IF NOT EXISTS events_core (
    id BIGSERIAL PRIMARY KEY,
    event_uid TEXT UNIQUE NOT NULL,
    source TEXT NOT NULL,
    ticker TEXT NOT NULL,
    event_type TEXT NOT NULL,
    occurred_at TIMESTAMPTZ,
    payload_json JSONB DEFAULT '{}',
    dedupe_key TEXT UNIQUE,
    status TEXT DEFAULT 'pending',
    attempts INT DEFAULT 0,
    analysis_id BIGINT,
    result_json JSONB,
    last_error TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_events_status ON events_core(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_ticker ON events_core(ticker, created_at DESC);
```

### 5.3 Earnings Tables

```sql
CREATE TABLE IF NOT EXISTS earnings_transcripts_core (
    id BIGSERIAL PRIMARY KEY,
    ticker TEXT NOT NULL,
    call_date DATE,
    fiscal_year INT,
    fiscal_quarter TEXT,
    title TEXT,
    source_type TEXT,
    source_url TEXT,
    filing_id BIGINT,
    accession TEXT,
    excerpt TEXT,
    transcript_text TEXT,
    char_count INT,
    quality_score FLOAT DEFAULT 0.5,
    is_partial BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS earnings_analysis_core (
    id BIGSERIAL PRIMARY KEY,
    ticker TEXT NOT NULL,
    filing_date DATE,
    form TEXT,
    fiscal_quarter TEXT,
    transcript_id BIGINT,
    guidance_direction TEXT,
    tone TEXT,
    eps_vs_prior TEXT,
    revenue_actual TEXT,
    margin_commentary TEXT,
    key_signals JSONB,
    deflected_topics TEXT,
    vs_prior_quarter TEXT,
    summary TEXT,
    analyzed_at TIMESTAMPTZ DEFAULT now(),
    quality_score FLOAT
);
```

### 5.4 Cascade Alerts Table

```sql
CREATE TABLE IF NOT EXISTS portfolio_cascade_alerts_core (
    id BIGSERIAL PRIMARY KEY,
    trigger_ticker TEXT NOT NULL,
    trigger_signal TEXT,
    trigger_proposal_id BIGINT,
    affected_ticker TEXT NOT NULL,
    effect_type TEXT,
    effect_summary TEXT,
    relationship TEXT,
    confidence FLOAT,
    severity TEXT,
    detected_at TIMESTAMPTZ DEFAULT now(),
    status TEXT DEFAULT 'open'
);
```

---

## 6. Background Workers

### Thread Pattern
```python
import threading, os

refresh_stop = threading.Event()

# SEC EDGAR poll (every 6h)
_SEC_POLL_INTERVAL_SEC = int(os.getenv("SEC_POLL_INTERVAL_SEC", "21600"))

def _sec_edgar_poll_loop():
    refresh_stop.wait(90)  # startup stagger — 90s delay
    while not refresh_stop.is_set():
        try:
            from app.services.sec_edgar_poller_service import poll_and_ingest_tickers
            poll_and_ingest_tickers()
        except Exception as exc:
            LOGGER.warning("[sec-edgar-poll] cycle failed: %s", exc)
        refresh_stop.wait(_SEC_POLL_INTERVAL_SEC)

# Form 4 insider poll (every 24h)
_FORM4_POLL_INTERVAL_SEC = int(os.getenv("FORM4_POLL_INTERVAL_SEC", "86400"))

def _form4_poll_loop():
    refresh_stop.wait(120)  # startup stagger — 120s delay
    while not refresh_stop.is_set():
        try:
            from app.services.sec_edgar_poller_service import poll_form4_for_tickers
            poll_form4_for_tickers()
        except Exception as exc:
            LOGGER.warning("[form4-insider-poll] cycle failed: %s", exc)
        refresh_stop.wait(_FORM4_POLL_INTERVAL_SEC)

# Start threads (all daemon)
if str(os.getenv("SEC_POLL_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}:
    th = threading.Thread(target=_sec_edgar_poll_loop, name="sec-edgar-poll", daemon=True)
    th.start()

if str(os.getenv("FORM4_POLL_ENABLED", "1")).strip().lower() in {"1", "true", "yes", "on"}:
    th = threading.Thread(target=_form4_poll_loop, name="form4-insider-poll", daemon=True)
    th.start()
```

**Startup Schema Calls** (in `main.py` app factory):
```python
ensure_events_schema()
ensure_poller_schema()
ensure_earnings_analysis_schema()
ensure_sec_ingest_schema()
```

### Worker Summary

| Thread | Interval | Stagger | Purpose |
|--------|----------|---------|---------|
| `sec-edgar-poll` | 6h | 90s | Poll SEC filings for all monitored tickers |
| `form4-insider-poll` | 24h | 120s | Poll Form 4 insider trades (significant only) |

---

## 7. API Endpoints (Key Routes)

### Filing Management
```
GET  /api/filings?ticker=AAPL&limit=20        — List filings for ticker
GET  /api/filings/{id}                         — Get single filing with content
POST /api/filings/poll                         — Trigger manual poll for ticker
```

### Events
```
GET  /api/events?status=pending&limit=50       — List events by status
GET  /api/events/{id}                          — Get single event with result
POST /api/events/{id}/process                  — Manually process event
```

### Earnings
```
GET  /api/earnings/{ticker}?limit=10           — List earnings analyses
POST /api/earnings/{ticker}/refresh            — Trigger earnings refresh
```

### Cascade Alerts
```
GET  /api/cascades?status=open&limit=20        — List open cascade alerts
POST /api/cascades/{id}/dismiss                — Dismiss cascade alert
```

### Signal Universe
```
GET  /api/signal-universe                      — List monitored tickers
POST /api/signal-universe/add                  — Add ticker to universe
POST /api/signal-universe/remove               — Remove ticker from universe
```

---

## 8. JavaScript Patterns

### Panel Open/Close
```javascript
function openPanel(type, id) {
  fetch('/api/'+type+'/'+id)
    .then(r => r.json())
    .then(data => {
      document.getElementById('panelTitle').textContent = data.title;
      document.getElementById('panelBody').innerHTML = buildPanelHtml(data);
      document.getElementById('todayPanel').classList.add('open');
      // Mark card as selected
      document.querySelectorAll('.today-card.selected').forEach(c => c.classList.remove('selected'));
      var card = document.querySelector('[data-type="'+type+'"][data-id="'+id+'"]');
      if (card) card.classList.add('selected');
    });
}

function closePanel() {
  document.getElementById('todayPanel').classList.remove('open');
  document.querySelectorAll('.today-card.selected').forEach(c => c.classList.remove('selected'));
}
```

### Content Rendering
```javascript
function renderContent(data) {
  var el = document.getElementById('mainContent');
  var html = '';

  // Lane header
  html += '<div class="today-lane">';
  html += '<div class="today-lane-header">';
  html += '<div class="today-lane-title">Recent Filings</div>';
  html += '<div class="today-lane-badge">' + data.filings.length + '</div>';
  html += '</div>';

  // Cards
  data.filings.forEach(function(f) {
    html += '<div class="today-card" data-type="filing" data-id="' + f.id + '" onclick="openPanel(\'filing\',' + f.id + ')">';
    html += '<div class="today-card-icon">📄</div>';
    html += '<div class="today-card-body">';
    html += '<div class="today-card-title">' + esc(f.ticker + ' ' + f.form) + '</div>';
    html += '<div class="today-card-sub">' + esc(f.date) + '</div>';
    html += '<div class="today-card-meta"><span class="ws-badge ticker">$' + f.ticker + '</span></div>';
    html += '</div></div>';
  });

  html += '</div>';
  el.innerHTML = html;
}
```

### Data Fetching Pattern
```javascript
function loadData() {
  fetch('/api/today-briefing')
    .then(r => r.json())
    .then(d => {
      _data = d;
      renderContent(d);
    })
    .catch(err => {
      document.getElementById('mainContent').innerHTML = '<div class="ws-empty">Could not load data.</div>';
    });
}
```

### Dark Mode Toggle
```javascript
function toggleDark() {
  document.body.classList.toggle('theme-dark');
  localStorage.setItem('theme', document.body.classList.contains('theme-dark') ? 'dark' : 'light');
}
// On load
if (localStorage.getItem('theme') === 'dark') document.body.classList.add('theme-dark');
```

### Sidebar Toggle
```javascript
function toggleSidebar() {
  document.getElementById('wsSidebar').classList.toggle('collapsed');
  localStorage.setItem('sidebar', document.getElementById('wsSidebar').classList.contains('collapsed') ? 'collapsed' : 'expanded');
}
```

### HTML Escaping
```javascript
function esc(s) {
  if (!s) return '';
  var d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
```

---

## 9. Deployment

### Google Cloud Run
```bash
gcloud run deploy sec-intelligence-app \
  --source . \
  --region europe-west1 \
  --project YOUR_PROJECT_ID \
  --allow-unauthenticated \
  --quiet
```

### Environment Variables
```
DATABASE_URL=postgresql://user:pass@host:5432/dbname
SEC_POLL_ENABLED=1
SEC_POLL_INTERVAL_SEC=21600
FORM4_POLL_ENABLED=1
FORM4_POLL_INTERVAL_SEC=86400
ANTHROPIC_API_KEY=sk-ant-...
```

### Dockerfile (FastAPI)
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
```

### Key Dependencies
```
fastapi
uvicorn
psycopg2-binary
edgartools
anthropic
jinja2
python-multipart
```

---

## 10. Complete Example Flow

```
1. SEC EDGAR Poll (every 6h):
   poll_and_ingest_tickers()
   → poll_ticker("AAPL", is_held=True)
     → _fetch_filing_metadata()     # fast, metadata only
     → _download_markdown()         # full text (expensive)
     → _save_filing_text()          # to filesystem/GCS
     → _upsert_filing_core()        # to DB (content stored inline)
     → create_event(type='sec_filing', payload={filing_id, form, ...})
     → process_event(event_id)      # in ThreadPoolExecutor(max_workers=4)

2. Event Processing:
   _process_sec_filing_event()
   → process_new_filings_pipeline([filing_id])  # chunk + entities + relationships
   → run_event_driven_monitor(force=True)       # generate proposals
   → analyze_latest_earnings_for_ticker("AAPL") # if 8-K/10-Q/6-K/20-F
     → refresh_earnings_transcripts_from_sec()
     → analyze_earnings_transcript()            # LLM analysis
       → stores in earnings_analysis_core

3. Cascade Analysis:
   analyze_portfolio_cascades("AAPL", signal_text)
   → checks graph relationships to other holdings
   → LLM reasons about second/third-order effects
   → stores cascade alerts with confidence >= 0.4

4. UI displays filing cards, earnings analysis, cascade alerts
   → User clicks card → slide panel shows details
   → User can dismiss, act on, or investigate further
```
