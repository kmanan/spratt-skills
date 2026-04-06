-- Trips schema — full trip management with flights, hotels, reservations, travelers.

CREATE TABLE trips (
    id              TEXT PRIMARY KEY,
    name            TEXT,
    travelers       TEXT,
    destination     TEXT,
    timezone        TEXT,
    tz_utc_offset   TEXT,
    start_date      TEXT,
    end_date        TEXT,
    status          TEXT NOT NULL DEFAULT 'upcoming',
    manifest_path   TEXT,
    group_chat_guid TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX idx_trips_status ON trips(status);

CREATE TABLE flights (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id         TEXT NOT NULL,
    traveler        TEXT,
    flight_number   TEXT NOT NULL,
    route           TEXT,
    departs_utc     TEXT,
    arrives_utc     TEXT,
    status          TEXT NOT NULL DEFAULT 'scheduled',
    gate            TEXT,
    delay_minutes   INTEGER DEFAULT 0,
    notified_landed INTEGER DEFAULT 0,
    notified_delay  INTEGER DEFAULT 0,
    notified_gate   INTEGER DEFAULT 0,
    last_checked    TEXT,
    outbox_msg_id   INTEGER,
    outbox_generated_at TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX idx_flights_trip ON flights(trip_id);
CREATE INDEX idx_flights_status ON flights(status);

CREATE TABLE hotels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id     TEXT NOT NULL,
    name        TEXT,
    address     TEXT,
    check_in    TEXT,
    check_out   TEXT,
    outbox_msg_id INTEGER,
    outbox_generated_at TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX idx_hotels_trip ON hotels(trip_id);

CREATE TABLE reservations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id         TEXT NOT NULL,
    type            TEXT NOT NULL,
    name            TEXT,
    date            TEXT,
    time            TEXT,
    address         TEXT,
    party_size      INTEGER,
    confirmation    TEXT,
    notes           TEXT,
    outbox_msg_id   INTEGER,
    outbox_generated_at TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX idx_reservations_trip ON reservations(trip_id);

CREATE TABLE travelers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id     TEXT NOT NULL,
    name        TEXT NOT NULL,
    phone       TEXT,
    role        TEXT,
    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX idx_travelers_trip ON travelers(trip_id);
