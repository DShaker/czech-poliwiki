-- Czech PoliWiki core schema.
-- One row per real person in `person`; every source-specific identifier
-- lives in `person_source` so a person who sat in several chambers is one row.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS person (
    id           INTEGER PRIMARY KEY,
    given_name   TEXT,
    family_name  TEXT,
    title_before TEXT,
    title_after  TEXT,
    birth_date   TEXT,          -- ISO date, NULL when unknown
    gender       TEXT,          -- 'M' / 'F' / NULL
    country      TEXT           -- ISO 3166 alpha-3, 'CZE' for Czech politicians
);

CREATE TABLE IF NOT EXISTS person_source (
    source     TEXT NOT NULL,   -- 'psp' | 'senat' | 'ep'
    source_id  TEXT NOT NULL,
    person_id  INTEGER NOT NULL REFERENCES person(id),
    url        TEXT,
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS ix_person_source_person ON person_source(person_id);

-- A seat in a chamber for a period.
CREATE TABLE IF NOT EXISTS mandate (
    id            INTEGER PRIMARY KEY,
    person_id     INTEGER NOT NULL REFERENCES person(id),
    chamber       TEXT NOT NULL,  -- 'psp' | 'senat' | 'ep'
    term          TEXT NOT NULL,  -- e.g. '2025' for PSP 2025-, '10' for EP10
    start_date    TEXT,
    end_date      TEXT,
    constituency  TEXT,
    source_ref    TEXT,
    UNIQUE (person_id, chamber, term, start_date)
);

-- Club / party / EP group memberships with time ranges.
CREATE TABLE IF NOT EXISTS affiliation (
    id          INTEGER PRIMARY KEY,
    person_id   INTEGER NOT NULL REFERENCES person(id),
    chamber     TEXT NOT NULL,
    kind        TEXT NOT NULL,    -- 'club' | 'party' | 'ep_group' | 'national_party'
    org_id      TEXT,
    org_name    TEXT NOT NULL,
    org_abbr    TEXT,
    start_date  TEXT,
    end_date    TEXT,
    UNIQUE (person_id, chamber, kind, org_id, start_date)
);

CREATE TABLE IF NOT EXISTS bill (
    id              TEXT PRIMARY KEY,   -- e.g. 'psp:2025:123'
    chamber         TEXT NOT NULL,
    term            TEXT NOT NULL,
    number          TEXT,
    title           TEXT,
    bill_type       TEXT,
    submitted_date  TEXT,
    proposer_kind   TEXT,               -- 'mps' | 'government' | 'senate' | 'region' | 'other'
    url             TEXT
);

CREATE TABLE IF NOT EXISTS bill_sponsor (
    bill_id     TEXT NOT NULL REFERENCES bill(id),
    person_id   INTEGER REFERENCES person(id),
    sponsor_text TEXT,                  -- raw sponsor label when not a person
    PRIMARY KEY (bill_id, person_id, sponsor_text)
);

CREATE TABLE IF NOT EXISTS vote_event (
    id            TEXT PRIMARY KEY,     -- e.g. 'psp:2025:84512', 'ep:171425'
    chamber       TEXT NOT NULL,
    term          TEXT NOT NULL,
    date          TEXT NOT NULL,
    time          TEXT,
    session       TEXT,
    number        TEXT,
    title         TEXT,
    title_detail  TEXT,
    outcome       TEXT,                 -- 'passed' | 'rejected' | 'other'
    yes_count     INTEGER,
    no_count      INTEGER,
    abstain_count INTEGER,
    present_count INTEGER,
    quorum        INTEGER,
    bill_id       TEXT REFERENCES bill(id),
    doc_ref       TEXT,                 -- source document reference, e.g. 'A10-0027/2024'
    url           TEXT,
    -- What was voted on, from the source's own data (no generated text):
    agenda_id     TEXT,                 -- agenda item / debate the vote belongs to, e.g. 'psp:bod:59238'
    agenda        TEXT,                 -- full title of that agenda item
    stage         TEXT,                 -- legislative stage of the item, e.g. '3. čtení'
    motion        TEXT,                 -- what exactly was put to the vote, when the source says
    key_vote      TEXT                  -- set when the source records this vote as the decisive one
);
CREATE INDEX IF NOT EXISTS ix_vote_event_chamber_date ON vote_event(chamber, date);

-- One row per person per vote event.
-- option: 'yes' | 'no' | 'abstain' | 'not_voting' (present, did not press)
--         | 'absent' | 'excused'
CREATE TABLE IF NOT EXISTS vote (
    vote_event_id TEXT NOT NULL REFERENCES vote_event(id),
    person_id     INTEGER NOT NULL REFERENCES person(id),
    option        TEXT NOT NULL,
    PRIMARY KEY (vote_event_id, person_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_vote_person ON vote(person_id);

CREATE TABLE IF NOT EXISTS ingest_run (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    detail      TEXT
);
