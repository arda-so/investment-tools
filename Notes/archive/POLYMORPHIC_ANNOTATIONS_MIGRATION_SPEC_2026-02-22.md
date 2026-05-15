# Polymorphic Annotations Migration Spec

Date: 2026-02-22  
Owner: Engineering  
Scope: Replace fragmented user-authored tables with unified polymorphic annotations model, while preserving behavior.

## 0) Goal and Non-Goals

## Goal
Unify user-authored artifacts into one canonical table so notes/tasks/reminders/thesis can be rendered consistently across Organizer, Company File, Dashboard, and future multi-tenant SaaS APIs.

## Non-Goals (this phase)
- No change to market/facts/event/system tables (`report_facts_core`, `action_proposals_core`, queue/log tables).
- No breaking API behavior changes in existing endpoints during migration window.
- No immediate deletion of legacy tables; retirement is post-parity and post-burn-in.

---

## 1) Current Fragmentation (Source Tables)

Current user-authored data is split across:
- `todos_core`
- `investor_notes_core`
- `workspace_journal_core`
- `company_reminders_core`
- `daily_notes_core` + `daily_note_tags_core`
- (optional) `watchlist_thesis_core` for thesis text (currently separate domain table)

Primary write/read services currently:
- `app/services/postgres_core_service.py`:
  - `add_todo_pg`, `list_todos_pg`, `toggle_todo_pg`, `update_todo_pg`, `delete_todo_pg`
  - `add_investor_note_pg`, `list_recent_notes_pg`, `delete_notes_pg`
  - `add_workspace_journal_note_pg`, `update_workspace_journal_note_pg`, `delete_workspace_journal_note_pg`
  - `add_company_reminder_pg`, `list_company_reminders_pg`, `toggle_company_reminder_pg`, `update_company_reminder_pg`, `delete_company_reminder_pg`
- `app/services/organizer_service.py`:
  - `get_daily_note`, `save_daily_note`, `close_day`

---

## 2) Target Canonical Schema

Create one unified table: `investor_annotations_core`

```sql
CREATE TABLE IF NOT EXISTS investor_annotations_core (
  id BIGSERIAL PRIMARY KEY,

  -- tenancy / ownership
  tenant_id TEXT NOT NULL DEFAULT 'default',
  user_id TEXT NOT NULL DEFAULT 'default',

  -- polymorphic target
  entity_type TEXT NOT NULL,          -- company | portfolio | global
  entity_id TEXT NOT NULL DEFAULT '', -- ticker (for company), portfolio id, or '' for global

  -- annotation semantics
  annotation_type TEXT NOT NULL,      -- note | task | reminder | thesis
  title TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT '',
  content_json JSONB NOT NULL DEFAULT '{}'::jsonb,

  status TEXT NOT NULL DEFAULT 'open',  -- open | done | resolved | archived | pending | approved
  priority TEXT NOT NULL DEFAULT 'P2',
  due_at TEXT NOT NULL DEFAULT '',
  remind_at TEXT NOT NULL DEFAULT '',

  -- provenance
  source_table TEXT NOT NULL DEFAULT '',
  source_id TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '',
  sentiment TEXT NOT NULL DEFAULT 'neutral',
  created_by TEXT NOT NULL DEFAULT 'human',

  -- AI metadata compatibility
  ai_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  ai_reasoning TEXT NOT NULL DEFAULT '',
  trace_id TEXT NOT NULL DEFAULT '',

  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ann_scope ON investor_annotations_core(tenant_id, user_id, entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_ann_type_status ON investor_annotations_core(annotation_type, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ann_due ON investor_annotations_core(due_at, remind_at);
CREATE INDEX IF NOT EXISTS idx_ann_tags ON investor_annotations_core(tags);
CREATE INDEX IF NOT EXISTS idx_ann_trace ON investor_annotations_core(trace_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ann_source ON investor_annotations_core(source_table, source_id)
WHERE source_table <> '' AND source_id <> '';
```

## Enum constraints (enforce via CHECK)

```sql
ALTER TABLE investor_annotations_core
ADD CONSTRAINT ck_ann_entity_type CHECK (entity_type IN ('company','portfolio','global'));

ALTER TABLE investor_annotations_core
ADD CONSTRAINT ck_ann_type CHECK (annotation_type IN ('note','task','reminder','thesis'));

ALTER TABLE investor_annotations_core
ADD CONSTRAINT ck_ann_status CHECK (status IN ('open','done','resolved','archived','pending','approved'));
```

---

## 3) Mapping Rules (Legacy -> Canonical)

## `todos_core` -> `investor_annotations_core`
- `annotation_type = 'task'`
- `entity_type = CASE WHEN ticker<>'' THEN 'company' ELSE 'global' END`
- `entity_id = UPPER(ticker)` or `''`
- `content = task`
- `priority = priority`
- `due_at = due_date`
- `status = status`
- `source_table = 'todos_core'`
- `source_id = CAST(id AS TEXT)`

## `investor_notes_core` -> canonical
- `annotation_type = 'note'` (or `'thesis'` if tags/scope indicate thesis)
- `entity_type = CASE WHEN ticker<>'' THEN 'company' ELSE 'global' END`
- `entity_id = UPPER(ticker)`
- `content = note`
- `tags = tags`
- `sentiment = sentiment`
- `status = status`
- `created_by`, `ai_confidence`, `ai_reasoning`, `trace_id` carry forward
- `source_table = 'investor_notes_core'`, `source_id = id`

## `workspace_journal_core` -> canonical
- `annotation_type = 'note'`
- `entity_type = 'company'`
- `entity_id = UPPER(ticker)`
- `title = action`
- `content = note`
- `content_json = {'emotion': emotion}`
- `status = status`
- `created_by = created_by`
- `source_table = 'workspace_journal_core'`, `source_id = id`

## `company_reminders_core` -> canonical
- `annotation_type = 'reminder'`
- `entity_type = 'company'`
- `entity_id = UPPER(ticker)`
- `content = note`
- `remind_at = remind_at`
- `status = status`
- `source_table = 'company_reminders_core'`, `source_id = id`

## `daily_notes_core` (+ tags) -> canonical
- `annotation_type = 'note'`
- `entity_type = 'global'`
- `entity_id = ''`
- `title = day`
- `content = content`
- `status = CASE WHEN locked=1 THEN 'archived' ELSE 'open' END`
- `content_json = {'day': day, 'locked': locked, 'archived_at': archived_at, 'tickers': [...from daily_note_tags_core...]}`
- `source_table = 'daily_notes_core'`, `source_id = day`

## `watchlist_thesis_core` -> canonical (optional this phase)
- Recommended **Phase 2.2** only, to avoid thesis regression.
- If included:
  - `annotation_type = 'thesis'`
  - `entity_type = 'company'`
  - `entity_id = ticker`
  - `content`/`content_json` from thesis fields.

---

## 4) Migration Plan (Safe Cutover)

## Phase 1: Schema + Adapter Introduction
1. Add table DDL in `app/services/postgres_core_service.py` schema bootstrap.
2. Add new adapter functions in `postgres_core_service.py`:
   - `create_annotation_pg(...)`
   - `list_annotations_pg(...)`
   - `update_annotation_pg(...)`
   - `set_annotation_status_pg(...)`
   - `delete_annotation_pg(...)`
   - `upsert_annotation_from_legacy_pg(source_table, source_id, payload)`

## Phase 2: Backfill (idempotent)
Create `migrate_annotations_backfill_pg()`:
- Inserts from each legacy table using mapping rules.
- Uses `ON CONFLICT (source_table, source_id)` to upsert and remain idempotent.

Recommended SQL shape (example for todos):
```sql
INSERT INTO investor_annotations_core (
  tenant_id,user_id,entity_type,entity_id,annotation_type,title,content,content_json,
  status,priority,due_at,remind_at,source_table,source_id,tags,sentiment,created_by,
  ai_confidence,ai_reasoning,trace_id,created_at,updated_at
)
SELECT
  'default','default',
  CASE WHEN COALESCE(ticker,'')<>'' THEN 'company' ELSE 'global' END,
  UPPER(COALESCE(ticker,'')),'task','',COALESCE(task,''),'{}'::jsonb,
  COALESCE(status,'open'),COALESCE(priority,'P2'),COALESCE(due_date,''),'',
  'todos_core',CAST(id AS TEXT),'','','human',0.0,'','',
  COALESCE(created_at,now()::text),COALESCE(created_at,now()::text)
FROM todos_core
ON CONFLICT (source_table, source_id) DO UPDATE SET
  content=EXCLUDED.content,
  status=EXCLUDED.status,
  priority=EXCLUDED.priority,
  due_at=EXCLUDED.due_at,
  updated_at=now()::text;
```

## Phase 3: Dual-Write (no read cutover yet)
Patch legacy write paths to write both:
- Existing table (current behavior)
- `investor_annotations_core` using `upsert_annotation_from_legacy_pg`

Dual-write targets:
- `add_todo_pg`, `update_todo_pg`, `toggle_todo_pg`, `delete_todo_pg`
- `add_investor_note_pg`, `add_workspace_journal_note_pg`, note updates/deletes
- reminder add/update/toggle/delete
- `save_daily_note`, `close_day`

## Phase 4: Read Cutover via Feature Flag
Add env flag:
- `ANNOTATIONS_READ_SOURCE=legacy|poly` (default `legacy`)

Switch readers under flag:
- Organizer timeline (`list_recent_notes_pg` equivalent from annotations)
- Company detail notes/tasks/reminders panel
- Right drawer quick-capture feed

## Phase 5: Parity Verification
Create `/ai/migration/verify-annotations` or script to compare:
- counts by type/status/entity
- random sample row content hashes
- mutation parity during dual-write window

Parity SQL examples:
```sql
-- tasks parity
SELECT (SELECT COUNT(*) FROM todos_core) AS legacy,
       (SELECT COUNT(*) FROM investor_annotations_core WHERE annotation_type='task' AND source_table='todos_core') AS poly;

-- reminders parity
SELECT (SELECT COUNT(*) FROM company_reminders_core) AS legacy,
       (SELECT COUNT(*) FROM investor_annotations_core WHERE annotation_type='reminder' AND source_table='company_reminders_core') AS poly;

-- note parity (combined)
SELECT
 (SELECT COUNT(*) FROM investor_notes_core) + (SELECT COUNT(*) FROM workspace_journal_core) AS legacy,
 (SELECT COUNT(*) FROM investor_annotations_core
   WHERE annotation_type='note' AND source_table IN ('investor_notes_core','workspace_journal_core')) AS poly;
```

## Phase 6: Legacy Retirement
After parity + burn-in (7-14 days):
- switch `ANNOTATIONS_READ_SOURCE=poly`
- disable legacy writes
- archive legacy tables (rename) before final drop

---

## 5) Router/UI Integration Requirements

## A) Portfolio Context Injection in Company File
Update `app/routers/company_file.py`:
- On `/company_file?t=...`, load `portfolio_context` from `app/services/portfolio_memory_service.py`.
- Include in template payload:
  - shares
  - cost basis
  - market value
  - unrealized PnL / pct
  - last transaction date

Template updates:
- `app/templates/company_detail.html`
- `app/templates/components/company_detail_body.html`

Render a top banner for held names.

## B) 3-Pane HTMX behavior
- Left pane: nav/universe/portfolio summary (stable shell)
- Center pane: ticker workspace (`company_detail_body.html`) swapped by HTMX
- Right drawer: quick capture + AI chat actions

Mutation rule:
- POST mutations return `204` + OOB fragments when possible.
- Never full rerender center pane on right-drawer actions.

---

## 6) Backward Compatibility + Risk Controls

## Feature flags
- `ANNOTATIONS_DUAL_WRITE=1`
- `ANNOTATIONS_READ_SOURCE=legacy|poly`

## Idempotency
- `UNIQUE (source_table, source_id)` in polymorphic table

## Rollback strategy
If issue after read cutover:
1. Flip `ANNOTATIONS_READ_SOURCE=legacy`
2. Keep dual-write enabled
3. Re-run parity check
4. Fix mapper/query and retry

## Data safety
- No destructive drops until:
  - parity green
  - two successful deploy cycles
  - manual sign-off

---

## 7) Engineering Work Breakdown

1. DDL + adapter API in `postgres_core_service.py`
2. Backfill migration function + verify endpoint/script
3. Dual-write patch in existing CRUD functions
4. Read adapter for annotations + feature-flag switch
5. Router/template changes for portfolio context banner
6. HTMX 3-pane mutation response hardening (`204`/OOB)
7. Burn-in, parity, retirement

---

## 8) Acceptance Criteria

- All legacy-created notes/tasks/reminders appear in polymorphic table.
- Organizer + Company File show identical data before/after cutover.
- Company File header shows live portfolio context for held tickers.
- Right-drawer mutations do not trigger full center-pane rerender.
- Parity checks pass for counts + sampled content hashes.
- Rollback via flags works within one deploy.


## 2026-02-24 Refresh
- Runtime update: active company market-cap lookup and events runtime paths are Postgres-only.
- AI command reliability: `/ai/command` timeout path now returns queued fallback quickly (no blocking hang).
- Health posture unchanged and valid in current run:
  - `/health/live` ok
  - `/health/ready` => `core_db_backend=postgres`, `queue_backend=postgres`, `db_ok=true`
- Continuation policy: keep async-first AI flow and avoid reintroducing SQLite fallback in active runtime request paths.
