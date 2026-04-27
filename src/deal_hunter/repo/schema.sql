PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS listings (
    source          TEXT    NOT NULL,
    source_id       TEXT    NOT NULL,
    url             TEXT    NOT NULL,

    city            TEXT    DEFAULT '',
    neighborhood    TEXT    DEFAULT '',
    street          TEXT    DEFAULT '',
    house_number    TEXT    DEFAULT '',
    address         TEXT    DEFAULT '',

    rooms           REAL,
    sqm             INTEGER,
    sqm_build       INTEGER,
    floor           INTEGER,

    price           INTEGER NOT NULL,
    price_before    INTEGER,
    price_per_sqm   INTEGER,

    listing_type    TEXT    DEFAULT '',
    is_agent        INTEGER DEFAULT 0,

    parking         INTEGER DEFAULT 0,
    elevator        INTEGER DEFAULT 0,
    balcony         INTEGER DEFAULT 0,
    ac              INTEGER DEFAULT 0,
    mamad           INTEGER DEFAULT 0,
    renovated       INTEGER DEFAULT 0,

    description     TEXT    DEFAULT '',
    images_json     TEXT    DEFAULT '[]',
    tags_json       TEXT    DEFAULT '[]',
    lat             REAL,
    lon             REAL,

    publish_date    TEXT    DEFAULT '',
    first_listed_date TEXT  DEFAULT '',
    first_seen_at   TEXT    NOT NULL,
    last_seen_at    TEXT    NOT NULL,

    canonical_id    TEXT,

    fair_price_estimate INTEGER,
    fair_price_low      INTEGER,
    fair_price_high     INTEGER,

    score           REAL,
    score_reasons   TEXT    DEFAULT '{}',

    source_payload  TEXT    DEFAULT '{}',

    is_favorite     INTEGER NOT NULL DEFAULT 0,
    user_notes      TEXT    NOT NULL DEFAULT '',

    units_count     INTEGER,
    garden_sqm      INTEGER,

    sqm_user        INTEGER,
    sqm_build_user  INTEGER,
    units_count_user INTEGER,
    garden_sqm_user INTEGER,

    PRIMARY KEY (source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_listings_canonical   ON listings(canonical_id);
CREATE INDEX IF NOT EXISTS idx_listings_city        ON listings(city);
CREATE INDEX IF NOT EXISTS idx_listings_score       ON listings(score DESC);
CREATE INDEX IF NOT EXISTS idx_listings_first_seen  ON listings(first_seen_at DESC);

CREATE TABLE IF NOT EXISTS price_history (
    source    TEXT    NOT NULL,
    source_id TEXT    NOT NULL,
    ts        TEXT    NOT NULL,
    price     INTEGER NOT NULL,
    PRIMARY KEY (source, source_id, ts)
);

CREATE INDEX IF NOT EXISTS idx_price_history_ts ON price_history(source, source_id, ts DESC);

CREATE TABLE IF NOT EXISTS comps (
    provider       TEXT    NOT NULL,
    address_hash   TEXT    NOT NULL,
    deal_date      TEXT    NOT NULL,
    price          INTEGER NOT NULL,
    sqm            INTEGER,
    rooms          REAL,
    city           TEXT    DEFAULT '',
    neighborhood   TEXT    DEFAULT '',
    street         TEXT    DEFAULT '',
    house_number   TEXT    DEFAULT '',
    year_built     INTEGER,
    raw            TEXT    DEFAULT '{}',
    fetched_at     TEXT    NOT NULL,
    PRIMARY KEY (provider, address_hash, deal_date, price)
);

CREATE INDEX IF NOT EXISTS idx_comps_lookup ON comps(city, neighborhood, rooms);

CREATE TABLE IF NOT EXISTS scan_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    fetched     INTEGER NOT NULL,
    new         INTEGER NOT NULL,
    updated     INTEGER NOT NULL,
    price_drops INTEGER NOT NULL,
    alerted     INTEGER NOT NULL,
    duration_sec REAL   NOT NULL,
    errors      TEXT    DEFAULT '[]'
);
