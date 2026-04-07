-- Places schema — restaurants, activities, attractions saved from URLs or manual entry.

CREATE TABLE places (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    category        TEXT,              -- restaurant, cafe, bar, activity, park, attraction, shop
    source_url      TEXT,
    source_platform TEXT,              -- instagram, google, yelp, tiktok, website, manual
    location        TEXT,              -- neighborhood/city or full address
    cuisine_or_type TEXT,              -- cuisine for restaurants, type for activities
    tags            TEXT,              -- JSON array: ["date-night", "kid-friendly", "outdoor"]
    price_range     TEXT,              -- $, $$, $$$, $$$$
    notes           TEXT,
    saved_by        TEXT,              -- who shared it: manan, wife, etc.
    visited_at      DATETIME,
    rating          INTEGER,           -- 1-5, after visiting
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_places_name ON places(name);
CREATE INDEX idx_places_category ON places(category);
CREATE INDEX idx_places_tags ON places(tags);
CREATE INDEX idx_places_location ON places(location);
CREATE INDEX idx_places_created ON places(created_at);
