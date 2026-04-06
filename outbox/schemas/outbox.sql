-- Outbox schema — single source of truth for all outbound messages.

CREATE TABLE messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient   TEXT NOT NULL,
    channel     TEXT NOT NULL DEFAULT 'imessage',
    body        TEXT NOT NULL,
    send_at     TEXT NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    source      TEXT,
    created_by  TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    delivered_at TEXT,
    failed_at   TEXT,
    error       TEXT,
    trip_id     TEXT
);

CREATE INDEX idx_pending ON messages(status, send_at) WHERE status = 'pending';
CREATE INDEX idx_source ON messages(source);
