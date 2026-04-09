CREATE TABLE orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  order_id TEXT,
  order_date DATETIME NOT NULL,
  items TEXT NOT NULL,
  total REAL,
  source_email_id TEXT,
  source_account TEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
, tracking_number TEXT, carrier TEXT, tracking_status TEXT, tracking_updated_at TEXT, store TEXT);
CREATE TABLE sqlite_sequence(name,seq);
CREATE INDEX idx_orders_date ON orders(order_date);
CREATE INDEX idx_orders_source ON orders(source);
CREATE INDEX idx_orders_tracking ON orders(tracking_number) WHERE tracking_number IS NOT NULL;
CREATE TABLE item_aliases (raw_name TEXT PRIMARY KEY, canonical_name TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now')));
