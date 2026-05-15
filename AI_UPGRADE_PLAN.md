# AI Upgrade Plan - Investor OS v2

## Goal
Move Investor OS v2 from AI-augmented to AI-first (with manual fallback), safely and incrementally.

## Principles
- **Invisible Overlay:** AI features float above the existing dashboard; no separate "Chat Page."
- **Manual Fallback:** Keep manual workflows available at all times.
- **Evidence-Backed:** AI actions must include proof links (citations).
- **Safety First:** No destructive or high-risk operation without explicit confirmation.

---

## Phase 1 - Control Plane & UI Foundation
### Core Deliverables
- **`app/services/ai_orchestrator.py`**: The "Brain" that receives intent and routes to tools.
- **Tool Registry**: Mapping existing services (Dashboard, Reports, Database) as callable tools.
- **Unified Command Endpoint**: `POST /ai/command` to handle natural language requests.
- **Action Log**: Database table to track every AI decision for auditing.

### UI Deliverables (The "Glass & Ghost" System)
- **Global Command Bar (`Cmd+K`):**
  - *Design:* Glassmorphism backdrop (blur), large transparent input, centered white card.
  - *Location:* `app/templates/base.html` (replaces current placeholder).
  - *Behavior:* Accepts natural language ("Research Adobe"), returns HTML partials.
- **"Ghost" Status Indicator:**
  - *Design:* Subtle pulsing dot (Emerald/Orange) in the top navbar.
  - *Behavior:* Only visible when the Orchestrator is processing; replaces full-screen spinners.

### Design Constraints (Mandatory)
- Keep glass effects subtle and readable (no heavy blur/glow).
- Standardize tokens in one place for consistency:
  - `--glass-bg`, `--glass-border`, `--glass-blur`, `--glass-shadow`, `--glass-opacity`
- Preserve current SaaS palette direction (Slate/White/Emerald accents).

### Accessibility Requirements (Mandatory)
- Full keyboard support: open, navigate, submit, close.
- Focus trap inside command modal.
- `Esc` closes modal safely.
- Respect `prefers-reduced-motion` for pulsing/animations.
- Maintain WCAG AA contrast for text and statuses.

### Mobile Behavior (Mandatory)
- Add visible mobile trigger button (since `Cmd+K` is desktop-centric).
- Use bottom-sheet style command UI on small screens.

### Latency UX States (Mandatory)
- Status machine: `idle` -> `thinking` -> `tool-running` -> `done` / `failed`.
- Show timeout/retry affordance on failures.

### What changes for users
- One universal command bar can trigger multi-step workflows.
- No visual clutter; the interface remains clean until requested.

---

## Phase 2 - Automation Levels & Feedback Loops
### Levels
- **L0 (Suggest):** AI creates a "Toast" notification with an idea.
- **L1 (Draft):** AI creates a "Draft" record (Log/Note/Task).
- **L2 (Execute):** Auto-execute low-risk actions (tagging news, updating prices).
- **L3 (Restrict):** Approval required for high-risk actions (deleting notes, changing thesis).

### UI Deliverables
- **Draft Badge System:**
  - *Design:* Distinct visual state for AI-generated items (e.g., Orange left-border + "Review" button).
  - *Location:* `app/templates/components/organizer_body.html`.
- **Approval Queue:** A simple list of L3 actions waiting for user confirmation.

### What changes for users
- Users wake up to "Drafts" to review instead of blank pages.
- Sensitive actions always trigger a confirmation dialog.

---

## Phase 3 - Memory Backbone with Source Vectors
### Core Model
Store both claims and source evidence vectors.

#### Claim Record
`{entity, claim, evidence_ids, timestamp, confidence, status}`

#### Evidence Record
`{source_id, source_type, path_or_url, span_text, embedding, created_at}`

### Rules
- No claim can be marked `trusted` without at least one valid evidence link.
- Every recommendation must render **[Citation]** links to exact source text.
- No AI output should be presented as fact without citation or explicit `uncited` label.

### What changes for users
- Hovering over an AI insight shows the exact paragraph in the source PDF/Article.
- Trust and explainability improve significantly.

---

## Phase 4 - High-ROI Automations
1. **Report Auto-Triage:** "Read Now" vs "Read Later" with summary reasons.
2. **Organizer Auto-Update:** Signals/News automatically append to the Daily Log.
3. **Thesis Delta:** System compares new earnings data against stored "Conviction" notes.

### What changes for users
- Daily operations move from manual triage to supervised AI workflow.
- "Thesis Breach" alerts appear on the Dashboard if data contradicts your view.

---

## Phase 5 - Safety + Governance
### Safety Rules
- Low confidence (<70%) must trigger a clarification question back to the user.
- Log every action with `trace_id`, input, output, and tool calls.

### Deliverables
- Safety Policy Middleware.
- Audit View (Timeline of AI actions).

---

## Phase 6 - Evaluation Framework
Track from day one:
- **Precision:** % of Alerts that were useful.
- **Acceptance:** % of L1 Drafts saved vs. discarded.
- **Efficiency:** Time saved per day (estimated).

### Deliverables
- KPI Dashboard cards (Hidden by default, visible in "Admin" view).

---

## UX End-State (User Experience)
- **Primary Interaction:** AI Command Bar (`Cmd+K`).
- **Visual Style:** Preserves the "SaaS Hybrid" look (Slate/White/Emerald).
- **Integration:** AI outputs inject directly into existing slots (Organizer cards, Report rows).
- **Grounding:** All AI text is hyperlinked to source proofs.

## Suggested Initial Implementation Sequence
1.  **UI:** Inject `command_modal.html` and `status_indicator` into `base.html`.
2.  **Backend:** Build `ai_orchestrator.py` skeleton.
3.  **Connection:** Wire `POST /ai/command` to the Orchestrator.
4.  **First Tool:** Connect `search_reports` to the Command Bar.

## Rollback Strategy
- Feature flags for Orchestrator and Automations.
- Ability to force `manual-only` mode instantly via config.
- Existing routes (`/dashboard`, `/organizer`) remain untouched and independent.

---
Last updated: 2026-02-17
