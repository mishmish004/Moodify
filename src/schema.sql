-- Applied by src/db.py:init_db() on every startup, so every statement must be idempotent.
-- Timestamps are UTC text from CURRENT_TIMESTAMP ('YYYY-MM-DD HH:MM:SS').

CREATE TABLE IF NOT EXISTS users (
    spotify_id        TEXT PRIMARY KEY,
    display_name      TEXT,
    refresh_token     TEXT NOT NULL,
    onboarding_json   TEXT,
    taste_summary     TEXT,
    taste_updated_at  TEXT,
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS playlists (
    id               INTEGER PRIMARY KEY,
    -- Disconnect deletes the user row; their playlists go with it (Spotify's 5-day deletion rule)
    spotify_id       TEXT NOT NULL REFERENCES users(spotify_id) ON DELETE CASCADE,
    prompt           TEXT NOT NULL,
    spotify_url      TEXT NOT NULL,
    track_uris_json  TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS playlists_by_user ON playlists (spotify_id, created_at);
