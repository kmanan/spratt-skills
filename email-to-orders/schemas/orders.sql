-- Orders schema — ingested from email scanning.

CREATE TABLE orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    order_id        TEXT,
    order_date      DATETIME NOT NULL,
    items           TEXT NOT NULL,
    total           REAL,
    source_email_id TEXT,
    source_account  TEXT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_orders_date ON orders(order_date);
CREATE INDEX idx_orders_source ON orders(source);
