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
    taste_pool_json   TEXT,
    -- Phase 14: a like in Moodify also saves the song to Spotify's Liked Songs (the user's setting)
    likes_to_spotify  INTEGER NOT NULL DEFAULT 1,
    -- Phase 16: the memory text every build's prompt gets (src/memory.py), NULL until rendered again
    -- after feedback changes; memory_version counts the renders
    memory_block      TEXT,
    memory_version    INTEGER NOT NULL DEFAULT 0,
    -- Phase 17: on a phone, open the Spotify app when a playlist is ready (the user's setting)
    open_spotify      INTEGER NOT NULL DEFAULT 1
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
    brief_json       TEXT,
    -- Phase 14: the user's 1-10 rating, changeable any time
    rating           INTEGER CHECK (rating BETWEEN 1 AND 10),
    rated_at         TEXT,
    -- Phase 15: the listener's own words on how it went (<= 2,000 chars), when, and how reflection read it
    review           TEXT,
    reviewed_at      TEXT,
    review_verdict   TEXT CHECK (review_verdict IN ('worked', 'partly', 'missed')),
    -- Phase 15: "Fix it with this review" makes a new version; this points at the one it fixed
    parent_id        INTEGER REFERENCES playlists(id) ON DELETE SET NULL
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
    -- Phase 13: the model's energy (1-10) and role for the track; see src/arc.py
    energy        INTEGER,
    role          TEXT,
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

-- Spotify search results per user, 7 days (src/search_cache.py)
CREATE TABLE IF NOT EXISTS search_cache (
    spotify_id   TEXT NOT NULL REFERENCES users(spotify_id) ON DELETE CASCADE,
    query        TEXT NOT NULL,
    tracks_json  TEXT NOT NULL,
    fetched_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (spotify_id, query)
);

-- Phase 14: songs the user liked. Per song, not per playlist row: a song liked in one playlist shows
-- liked in every playlist. added_to_spotify is 1 when Moodify put it in the user's Liked Songs, so an
-- unlike only takes out of Spotify what Moodify put there (src/likes.py)
CREATE TABLE IF NOT EXISTS track_likes (
    spotify_id        TEXT NOT NULL REFERENCES users(spotify_id) ON DELETE CASCADE,
    uri               TEXT NOT NULL,
    -- where it was liked; deleting that playlist deletes the like (Phase 16's "delete from memory")
    playlist_id       INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
    added_to_spotify  INTEGER NOT NULL DEFAULT 0,
    liked_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (spotify_id, uri)
);

-- Phase 15: earlier versions of a review, kept when it's edited
CREATE TABLE IF NOT EXISTS review_history (
    id           INTEGER PRIMARY KEY,
    playlist_id  INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    text         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- Phase 15: what Moodify learned from reviews (src/reflect.py, src/memory.py). The shape follows
-- ReasoningBank's memory items: title, description, content. scope 'this_playlist' lessons are only
-- about their playlist (used by "Fix it"); 'lasting' ones reach later builds. A superseded lesson
-- stays, pointing at the one that replaced it, as an audit trail
CREATE TABLE IF NOT EXISTS lessons (
    id                  INTEGER PRIMARY KEY,
    spotify_id          TEXT NOT NULL REFERENCES users(spotify_id) ON DELETE CASCADE,
    source_playlist_id  INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
    title               TEXT NOT NULL,
    description         TEXT NOT NULL,
    content             TEXT NOT NULL,
    kind                TEXT NOT NULL CHECK (kind IN ('preference', 'pitfall', 'strategy')),
    scope               TEXT NOT NULL DEFAULT 'lasting' CHECK (scope IN ('lasting', 'this_playlist')),
    tags_json           TEXT NOT NULL DEFAULT '[]',
    -- the section or tracks it's about, and the words of the review it rests on
    about               TEXT NOT NULL DEFAULT '',
    evidence            TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'expired')),
    superseded_by       INTEGER REFERENCES lessons(id) ON DELETE SET NULL,
    created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at        TEXT,
    use_count           INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS lessons_by_user ON lessons (spotify_id, status);
