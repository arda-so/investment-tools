# Session Notes

Purpose: quick checkpoint log so we can resume exactly where we left off after interruptions.

How to add a note:

```bash
cd /Users/solmaz/Investment_Tools
./bin/session_note "what we completed" "next step"
```

## 2026-02-12 16:00 local
- Completed:
  - Consolidated workspace under `/Users/solmaz/Investment_Tools`.
  - Added terminal-style report pipeline and helper commands (`run_terminal_daily`, `run_terminal_weekly`, `run_terminal_monthly`, `status`).
  - Generated latest terminal reports for 2026-02-12.
  - Ran manual deep-dive syncs today (latest logs include HUBS/CRM).
- Next step:
  - Continue with ticker-specific deep dives and automation hardening.

## 2026-02-12 19:37 GMT
- Completed: Recovered project state after interrupted chat and reviewed latest reports/logs
- Next step: Continue from latest deep-dive tasks (HUBS/CRM) and produce next report

## 2026-02-12 20:38 GMT
- Completed: Pivoted terminal_app to Onyx 3-column Smart Feed; added Red Team bear-case card; switched insider logic to SEC Form 4 first with director/10% owner coverage; added Insider Summary drill-down with meaningful-only toggle; added SEC evidence cross-checks; implemented auto insider refresh on ticker add + daily pipeline + intraday LaunchAgent; set intraday schedule to every 6 hours
- Next step: Validate 2-3 tickers in Smart Feed and Insider Summary, then tune meaningful threshold/role filters if needed

## Session Save — 2026-02-13 (Latest)
- Fixed crash in `tools/terminal_app.py` on `/company` page (`sqlite3.Row` had `.get` call).
- Patch applied: `form4_count` now uses row key access (`r["form"]`).
- App recompiled and restarted on `http://127.0.0.1:8765`.
- Current state: app running, but environment shows high exec-session pressure warnings.

### User priorities to continue
1. Appendix intelligence must be generated from Appendix report content itself (not portfolio-based).
2. Executive Synthesis must stay report-event based (Daily/Appendix/Weekly/Monthly/Quarterly).
3. Improve readability and one-page report experience; full text only on click.
4. Keep design clean; remove unnecessary source labels in user-facing UI.

### Next actions on resume
- Kill/reduce stale exec sessions and do clean app restart.
- Force clear report-intelligence cache.
- Verify Appendix card outputs true report-derived insights.
- Validate Report Studio cards: readable top line, key points, and useful action cues.

---

## 2026-02-16 Handoff Update
- Saved full migration snapshot to `SESSION_SNAPSHOT.md`.
- v2 base URL: `http://127.0.0.1:8766`.
- Reports page is now migrated to v2 at `/reports`.
- My Companies is now a dedicated page at `/my_companies` (portfolio/watchlist separated).
- Next session priority: final pixel-level dashboard parity and final cleanup of remaining legacy gaps.

## 2026-02-16 Late Session Update
- Completed:
  - Restored per-company SEC experience in v2 with dedicated route: `/company_file/sec?t=...`.
  - Added filing reader routes in v2: `/filing` and `/filing_raw`.
  - Fixed missing filings by loading full per-company dataset (no low cap; includes local + SEC-link rows).
  - Organized filings into categories (Annual, Quarterly, Proxy, Current, Ownership, Other) for easier scan/read.
  - Removed legacy-style `/company?t=...` usage from v2 flow (new-system route only).
  - Disabled old launchd jobs and moved automation entrypoints to v2-safe scripts.
  - Added optimized v2 automation stack + scheduler installer:
    - `automations/v2_daily.sh`, `automations/v2_morning.sh`, `automations/v2_signals.sh`, `automations/v2_earnings.sh`, `automations/v2_insider_deep.sh`
    - `bin/install_v2_launchd`
  - Loaded only v2 launch agents:
    - `com.solmaz.v2.morning`
    - `com.solmaz.v2.earnings`
    - `com.solmaz.v2.signals`
    - `com.solmaz.v2.daily`
    - `com.solmaz.v2.insider.deep`
  - Optimized cadence and load:
    - Morning 07:50 (Mon-Fri)
    - Earnings 08:20 (Mon-Fri)
    - Signals 12:20 (Mon-Fri)
    - Daily 17:50 (Mon-Fri)
    - Insider Deep 08:30 Saturday
    - Removed duplicate midday insider deep refresh and reduced daily insider lookback to 3 months (weekly deep backfill keeps full 18-month coverage).
- Next step:
  - Continue v2 UI polish and data QA (home/dashboard parity and company workflows), then optional Gemma analysis as a controlled background module.

## 2026-02-20 — Handoff Saved
- Full handoff saved to `SESSION_HANDOFF_2026-02-20.md`.
- This session included: Risk Veto quantitative config + UI, proactive run/reflexion tracking, SEC ingestion upgrades, and meta-suggestions engine.

## 2026-02-24 — Runtime Hardening Update
- Completed:
  - Removed active SQLite fallback from `company_lookup_service.market_cap_map()` (Postgres-only).
  - Converted `events_service` runtime operations to Postgres-only (no SQLite branch in runtime flow).
  - Fixed `/ai/command` sync timeout behavior so it returns queued response quickly instead of hanging.
  - Cleaned dead code in `dashboard_service` (`_safe_count` removed).
- Validation:
  - `py_compile` on changed modules passed.
  - `/health/live` and `/health/ready` passed.
  - `/dashboard` passed.
  - `/ai/command` returns queued fallback with `job_id` under timeout budget.
  - `/ai/command/async` returns queued as expected.
- Next step:
  - Continue strict Postgres-only cleanup on remaining low-priority/legacy SQLite references without changing business behavior.

## 2026-02-25 — Phase 3.5 Intelligence Upgrade COMPLETE

### All 5 intelligence features shipped this session:

| Feature | Status | Key Files |
|---------|--------|-----------|
| F1: Earnings Analysis LLM | ✅ | `earnings_transcript_service.py`, table: `earnings_analysis_core` |
| F2: Multi-agent Debate | ✅ | `debate_service.py` (NEW), table: `proposal_debate_artifacts` |
| F3: Peer Comparison Context | ✅ | `proactive_ai_service._build_financial_context()`, `_SECTOR_PEERS` map |
| F4: Outcome→Learning Loop | ✅ | `proactive_ai_service._read_active_reflexion_rules()` injected into proposal eval |
| F5: Smarter Form 4 Filter | ✅ | `sec_edgar_poller_service.poll_form4_for_tickers()` + daily thread |

### Other fixes this session:
- SEC poll interval changed from 30 min → 6 hours (was too aggressive)
- Form 4 removed from main POLL_FORMS (now its own daily 24h cycle)
- Fixed `list()` conversion for edgartools slicing (was returning 0 filings bug)
- Metadata-first fetch pattern (skip markdown download for already-seen filings)
- `signal_universe_core` table: 12 macro/supply-chain tickers for cross-portfolio signals
  (TSM, NVDA, ASML, INTC, MSFT, AMZN, GOOGL, META, JPM, COST, WMT, CAT)
- Background threads: market-refresh, outcome-measurement, sec-edgar-poll, form4-insider-poll
- Shutdown handler now cleans up all 4 threads including form4_poll_thread

### How the debate gate works (F2):
- Only fires for proposals with confidence >= 0.5
- 3 parallel LLM calls: bull analyst + bear analyst + risk manager
- 1 sequential judge reads all three, returns approved/rejected + rationale
- Rejected proposals stored as status='debate_rejected' (auditable, hidden from UI)
- Judge errors default to approved (never silently drops proposals)

### Suggested Phase 4.x next steps:
1. **Phase 4.1 — Debate Audit UI**: Show bull/bear/risk/judge text on proposal click; "Debate Rejected" tab
   - `list_debate_artifacts(proposal_id)` already exists in `debate_service.py`
2. **Phase 4.2 — Earnings Trend**: Track guidance_direction trajectory across quarters; alert on tone deterioration
3. **Phase 4.3 — Portfolio Stress Testing**: Macro scenario simulation across held tickers
4. **Phase 4.4 — Thesis Health Dashboard**: Per-ticker red/amber/green with debate pass rate + earnings tone
5. **Phase 4.5 — Reflexion Rule Management**: UI to review/approve/delete reflexion rules; auto-expire low-accuracy rules

## 2026-02-26 — Cloud Cutover + Report Audit Handoff
- Completed:
  - Reviewed notes/docs/runbooks and report corpus for continuity handoff.
  - Added fresh handoff note: `SESSION_HANDOFF_2026-02-26_CLOUD.md`.
  - Captured cloud runtime state, completed code fixes, and open issues.
  - Identified report quality issue: several latest reports are truncated mid-sentence.
  - Added async-safe universe migration endpoint flow (`/ai/migration/sync-universe` + status endpoint).
- Next step:
  - Deploy latest code, run cloud universe sync/status, repair report generation completeness, and re-sync runtime files to cloud bucket.
