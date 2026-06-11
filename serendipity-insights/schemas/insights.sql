CREATE TABLE IF NOT EXISTS insights (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  owner TEXT NOT NULL,
  title TEXT NOT NULL,
  summary TEXT NOT NULL,
  suggested_action TEXT,
  source TEXT NOT NULL,
  source_ref TEXT,
  evidence_json TEXT NOT NULL,
  confidence REAL NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  expires_at TEXT,
  status TEXT NOT NULL,
  reconciliation_state TEXT NOT NULL,
  surface_policy TEXT NOT NULL,
  surfaced_at TEXT,
  dismissed_at TEXT,
  accepted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_insights_surface
ON insights(status, owner, confidence, expires_at, created_at);

CREATE INDEX IF NOT EXISTS idx_insights_source_ref
ON insights(source, source_ref);
