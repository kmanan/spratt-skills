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
  accepted_at TEXT,
  stable_key TEXT,
  decision_id TEXT,
  classification TEXT,
  context_refs_json TEXT NOT NULL DEFAULT '[]',
  capabilities_json TEXT NOT NULL DEFAULT '{}',
  actions_json TEXT NOT NULL DEFAULT '[]',
  surface_channel TEXT,
  last_surfaced_at TEXT,
  cooldown_until TEXT,
  outcome TEXT
);

CREATE INDEX IF NOT EXISTS idx_insights_surface
ON insights(status, owner, confidence, expires_at, created_at);

CREATE INDEX IF NOT EXISTS idx_insights_source_ref
ON insights(source, source_ref);

CREATE UNIQUE INDEX IF NOT EXISTS idx_insights_stable_key
ON insights(stable_key)
WHERE stable_key IS NOT NULL AND stable_key != '';

CREATE INDEX IF NOT EXISTS idx_insights_decision
ON insights(decision_id, classification, status);
