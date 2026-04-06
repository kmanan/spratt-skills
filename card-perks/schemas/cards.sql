-- Card Perks Tracker — SQLite schema
-- Tracks credit card use-it-or-lose-it benefits, usage status, and change history.

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS cards (
    id          INTEGER PRIMARY KEY,
    holder      TEXT NOT NULL,           -- e.g. 'manan' or 'harshita'
    card_name   TEXT NOT NULL,           -- e.g. 'AMEX Platinum'
    issuer      TEXT,                    -- e.g. 'amex', 'chase', 'apple'
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(holder, card_name)
);

CREATE TABLE IF NOT EXISTS benefits (
    id              INTEGER PRIMARY KEY,
    card_id         INTEGER NOT NULL REFERENCES cards(id),
    name            TEXT NOT NULL,           -- e.g. 'Uber Cash'
    merchant        TEXT,                    -- e.g. 'Uber / Uber Eats'
    amount          REAL NOT NULL,           -- e.g. 15.00
    cycle           TEXT NOT NULL,           -- 'monthly', 'quarterly', 'semi-annual', 'annual'
    period_rule     TEXT NOT NULL,           -- 'calendar', 'december-only', 'chase-freedom'
    requires_activation INTEGER NOT NULL DEFAULT 0,
    auto_applied    INTEGER NOT NULL DEFAULT 0,  -- 1 = benefit applies automatically (e.g. Walmart+, CLEAR)
    notes           TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS usage (
    id              INTEGER PRIMARY KEY,
    benefit_id      INTEGER NOT NULL REFERENCES benefits(id),
    period_key      TEXT NOT NULL,           -- '2026-04' (monthly), '2026-Q2' (quarterly), '2026-H1' (semi-annual), '2026' (annual)
    status          TEXT NOT NULL DEFAULT 'pending',  -- 'pending', 'used', 'skipped'
    notified_at     TEXT,                    -- set on first notification; prevents duplicate reminders
    acknowledged_at TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_benefit_period ON usage(benefit_id, period_key);

CREATE TABLE IF NOT EXISTS benefit_changes (
    id          INTEGER PRIMARY KEY,
    card_id     INTEGER NOT NULL REFERENCES cards(id),
    change_type TEXT NOT NULL,           -- 'added', 'removed', 'modified'
    description TEXT NOT NULL,
    detected_at TEXT NOT NULL DEFAULT (datetime('now'))
);
