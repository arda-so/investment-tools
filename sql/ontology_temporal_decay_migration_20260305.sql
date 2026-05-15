-- Ontology temporal + decay additive migration
-- Date: 2026-03-05
-- Safe properties:
-- 1) Additive columns only
-- 2) No drops/renames
-- 3) Backward compatible with existing read paths

-- =========================
-- Postgres (relationships_core)
-- =========================
ALTER TABLE IF EXISTS relationships_core
  ADD COLUMN IF NOT EXISTS valid_from TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS valid_to TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS last_verified_at TEXT NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS decay_half_life_days INTEGER NOT NULL DEFAULT 365,
  ADD COLUMN IF NOT EXISTS effective_confidence DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active',
  ADD COLUMN IF NOT EXISTS signed_weight DOUBLE PRECISION NOT NULL DEFAULT 1.0;

CREATE INDEX IF NOT EXISTS idx_rel_core_valid_from ON relationships_core(valid_from);
CREATE INDEX IF NOT EXISTS idx_rel_core_valid_to ON relationships_core(valid_to);
CREATE INDEX IF NOT EXISTS idx_rel_core_status ON relationships_core(status);
CREATE INDEX IF NOT EXISTS idx_rel_core_effective_conf ON relationships_core(effective_confidence DESC);

-- =========================
-- SQLite fallback (relationships)
-- =========================
-- SQLite does not support ADD COLUMN IF NOT EXISTS in many versions.
-- Apply via application migration helper that checks column existence first.

