# AI Training Materials (Internal)
As of: 2026-02-21

Purpose: align team on how AI is trained/steered in this system without hardcoding investment conclusions.

## 1) Training/Steering Layers

### Layer A: Prompt + Context Engineering

- Inputs:
  - live portfolio context
  - watchlist thesis
  - investor style memory
  - recent artifacts (`analysis_id`, latest reports, recent chat context)
- Goal: dynamic reasoning from current data, not static templates.

### Layer B: Deterministic Routing + Gating

- Fast vs deep path routing.
- Noise/relevance gates.
- Timeout + retry + fallback behavior.
- These are system controls, not investment logic.

### Layer C: Memory Learning

- Persistent memory tables capture:
  - investor style/rules
  - thesis updates
  - decision logs
- Learning should update stored memory, then future prompts consume it.

### Layer D: Regression Feedback

- Real transcript failures become test/regression cases.
- Fix routing/reliability; avoid patching with hardcoded answer text.

## 2) What We Explicitly Avoid

- Hardcoded thesis verdicts.
- Hardcoded risk narratives per ticker.
- Fake constraints not in user memory.
- Static “one-size-fits-all” analysis categories for final content.

## 3) Recommended Prompt Pattern

Use structure like:

1. Relevance/Noise check.
2. Comparative analysis vs user thesis and peer read-through.
3. Adaptive learning statement (how thesis should evolve).
4. Structured JSON output for UI rendering.

This keeps outputs machine-safe while preserving dynamic reasoning.

## 4) Evaluation Checklist (per model change)

1. Relevance:
- Does model reject unrelated signals?

2. Hallucination:
- Does it avoid inventing user rules/limits?

3. Continuity:
- Does follow-up question reference correct recent analysis artifact?

4. Latency:
- Does async path finish within timeout budget?

5. UX contract:
- Does response preserve `intent/status/message/traces` shape?

## 5) Data for Ongoing Model Tuning

- AI action logs (`ai_action_log`)
- queue outcome logs (`ai_command_jobs` status/error)
- chat memory transcripts for successful vs failed follow-ups
- phase2 reducer artifacts and user follow-up outcomes

## 6) Team Operating Cadence

Weekly:
- review top failure intents
- review queue timeout/error rates
- review 5 follow-up continuity examples

Monthly:
- prompt/routing policy revision
- regression suite expansion from real conversations

## 7) Golden Rule

No hardcoded investment conclusions.  
Invest in better context, better routing, better memory, and better evaluation.


## 2026-02-22 Refresh
- New training signal class: transport-recovery and latency-path correctness (fast-path vs deep-path route quality).\n- Add transcript-based examples for short conversational turns that should avoid deep queueing.

## 2026-02-24 Refresh
- Runtime update: active company market-cap lookup and events runtime paths are Postgres-only.
- AI command reliability: `/ai/command` timeout path now returns queued fallback quickly (no blocking hang).
- Health posture unchanged and valid in current run:
  - `/health/live` ok
  - `/health/ready` => `core_db_backend=postgres`, `queue_backend=postgres`, `db_ok=true`
- Continuation policy: keep async-first AI flow and avoid reintroducing SQLite fallback in active runtime request paths.
