CREATE TABLE cards (
    id          INTEGER PRIMARY KEY,
    holder      TEXT NOT NULL,
    card_name   TEXT NOT NULL,
    issuer      TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')), network TEXT, annual_fee REAL DEFAULT 0, reward_type TEXT DEFAULT 'cashback', point_valuation_cpp REAL,
    UNIQUE(holder, card_name)
);
CREATE TABLE benefits (
    id              INTEGER PRIMARY KEY,
    card_id         INTEGER NOT NULL REFERENCES cards(id),
    name            TEXT NOT NULL,
    merchant        TEXT,
    amount          REAL NOT NULL,
    cycle           TEXT NOT NULL,
    period_rule     TEXT NOT NULL,
    requires_activation INTEGER NOT NULL DEFAULT 0,
    auto_applied    INTEGER NOT NULL DEFAULT 0,
    notes           TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE usage (
    id              INTEGER PRIMARY KEY,
    benefit_id      INTEGER NOT NULL REFERENCES benefits(id),
    period_key      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    notified_at     TEXT,
    acknowledged_at TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX idx_usage_benefit_period ON usage(benefit_id, period_key);
CREATE TABLE benefit_changes (
    id          INTEGER PRIMARY KEY,
    card_id     INTEGER NOT NULL REFERENCES cards(id),
    change_type TEXT NOT NULL,
    description TEXT NOT NULL,
    detected_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE reward_rates (id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL REFERENCES cards(id), category TEXT NOT NULL, rate REAL NOT NULL, cap_amount REAL, cap_period TEXT, rate_after_cap REAL DEFAULT 1.0, is_quarterly INTEGER DEFAULT 0, notes TEXT, UNIQUE(card_id, category));
CREATE TABLE quarterly_categories (id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL REFERENCES cards(id), year INTEGER NOT NULL, quarter INTEGER NOT NULL, categories TEXT NOT NULL, activated INTEGER DEFAULT 0, activated_at TEXT, UNIQUE(card_id, year, quarter));
CREATE TABLE spending_estimates (id INTEGER PRIMARY KEY, category TEXT NOT NULL UNIQUE, monthly_amount REAL NOT NULL, updated_at TEXT NOT NULL DEFAULT (datetime('now')));
