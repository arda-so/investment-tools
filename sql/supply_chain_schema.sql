CREATE TABLE IF NOT EXISTS companies (
  id BIGSERIAL PRIMARY KEY,
  ticker VARCHAR(16) NOT NULL UNIQUE,
  name TEXT NOT NULL,
  market_cap NUMERIC(20,2),
  sector TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS relationships (
  id BIGSERIAL PRIMARY KEY,
  source_company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  target_company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  relationship_type TEXT NOT NULL CHECK (relationship_type IN ('supplies', 'partners_with')),
  evidence_text TEXT NOT NULL,
  source_url TEXT,
  confidence_score INTEGER NOT NULL CHECK (confidence_score BETWEEN 1 AND 100),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (source_company_id, target_company_id, relationship_type, evidence_text)
);

CREATE INDEX IF NOT EXISTS idx_companies_ticker ON companies(ticker);
CREATE INDEX IF NOT EXISTS idx_relationships_source ON relationships(source_company_id);
CREATE INDEX IF NOT EXISTS idx_relationships_target ON relationships(target_company_id);
CREATE INDEX IF NOT EXISTS idx_relationships_confidence ON relationships(confidence_score);
