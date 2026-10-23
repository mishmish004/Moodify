-- The full current schema, applied by src/db.py:init_db() to a new database only.
-- An existing database is upgraded by db.MIGRATIONS instead; a change here needs a matching step there.
-- Timestamps are UTC text from CURRENT_TIMESTAMP ('YYYY-MM-DD HH:MM:SS').

CREATE TABLE IF NOT EXISTS users (
    spotify_id        TEXT PRIMARY KEY,
    display_name      TEXT,
    refresh_token     TEXT NOT NULL,
    onboarding_json   TEXT,
    taste_summary     TEXT,
    taste_updated_at  TEXT,
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- Phase 10: space-separated scopes from the last login, and the cached known-songs pool (src/taste.py)
    granted_scopes    TEXT,
    taste_pool_json   TEXT
);

CREATE TABLE IF NOT EXISTS playlists (
    id               INTEGER PRIMARY KEY,
    -- Disconnect deletes the user row; their playlists go with it (Spotify's 5-day deletion rule)
    spotify_id       TEXT NOT NULL REFERENCES users(spotify_id) ON DELETE CASCADE,
    prompt           TEXT NOT NULL,
    spotify_url      TEXT NOT NULL,
    track_uris_json  TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- Phase 11: how Moodify read the prompt (src/brief.py Brief as JSON)
    brief_json       TEXT
);

CREATE INDEX IF NOT EXISTS playlists_by_user ON playlists (spotify_id, created_at);

-- Every track of a playlist, in order, so pages and later phases don't need Spotify to show it
CREATE TABLE IF NOT EXISTS playlist_tracks (
    playlist_id   INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    position      INTEGER NOT NULL,
    uri           TEXT NOT NULL,
    name          TEXT NOT NULL,
    artists_json  TEXT NOT NULL,
    duration_ms   INTEGER NOT NULL,
    section       TEXT,
    PRIMARY KEY (playlist_id, position)
);

-- One row per playlist build. Builds run in a background thread; the progress page reads step and status
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY,
    spotify_id   TEXT NOT NULL REFERENCES users(spotify_id) ON DELETE CASCADE,
    prompt       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'failed')),
    step         TEXT,
    error        TEXT,
    playlist_id  INTEGER REFERENCES playlists(id) ON DELETE SET NULL,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at  TEXT
);

CREATE INDEX IF NOT EXISTS jobs_by_user ON jobs (spotify_id, status);

-- A build's log: steps, LLM calls with token counts, searches, failures
CREATE TABLE IF NOT EXISTS job_events (
    id           INTEGER PRIMARY KEY,
    job_id       INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
    kind         TEXT NOT NULL,
    detail_json  TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS job_events_by_job ON job_events (job_id, id);
