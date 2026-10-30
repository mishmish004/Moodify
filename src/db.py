"""SQLite storage: one file, stdlib sqlite3, tables defined in schema.sql."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from src.core.config import get_settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Upgrades for databases made by an older schema.sql, one step per version: MIGRATIONS[0] takes
# PRAGMA user_version 0 to 1, and so on. A new database gets schema.sql, which always holds the
# current schema, and starts at the latest version. test_p9_jobs checks both paths end up identical.
MIGRATIONS = [
    # 1 (Phase 9): playlist_tracks, jobs, job_events
    """
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
    CREATE TABLE IF NOT EXISTS job_events (
        id           INTEGER PRIMARY KEY,
        job_id       INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
        kind         TEXT NOT NULL,
        detail_json  TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS job_events_by_job ON job_events (job_id, id);
    """,
    # 2 (Phase 10): granted scopes, known-songs pool
    """
    ALTER TABLE users ADD COLUMN granted_scopes TEXT;
    ALTER TABLE users ADD COLUMN taste_pool_json TEXT;
    """,
    # 3 (Phase 11): the brief each playlist was built from
    """
    ALTER TABLE playlists ADD COLUMN brief_json TEXT;
    """,
    # 4 (Phase 12): search results per user
    """
    CREATE TABLE IF NOT EXISTS search_cache (
        spotify_id   TEXT NOT NULL REFERENCES users(spotify_id) ON DELETE CASCADE,
        query        TEXT NOT NULL,
        tracks_json  TEXT NOT NULL,
        fetched_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (spotify_id, query)
    );
    """,
    # 5 (Phase 13): each track's energy and role, for the order and for reflecting on reviews (Phase 15)
    """
    ALTER TABLE playlist_tracks ADD COLUMN energy INTEGER;
    ALTER TABLE playlist_tracks ADD COLUMN role TEXT;
    """,
    # 6 (Phase 14): likes, ratings, and whether likes also go to Spotify's Liked Songs
    """
    ALTER TABLE users ADD COLUMN likes_to_spotify INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE playlists ADD COLUMN rating INTEGER CHECK (rating BETWEEN 1 AND 10);
    ALTER TABLE playlists ADD COLUMN rated_at TEXT;
    CREATE TABLE IF NOT EXISTS track_likes (
        spotify_id        TEXT NOT NULL REFERENCES users(spotify_id) ON DELETE CASCADE,
        uri               TEXT NOT NULL,
        playlist_id       INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
        added_to_spotify  INTEGER NOT NULL DEFAULT 0,
        liked_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (spotify_id, uri)
    );
    """,
    # 7 (Phase 15): reviews, versions, and the lessons Moodify learns from reviews
    """
    ALTER TABLE playlists ADD COLUMN review TEXT;
    ALTER TABLE playlists ADD COLUMN reviewed_at TEXT;
    ALTER TABLE playlists ADD COLUMN review_verdict TEXT CHECK (review_verdict IN ('worked', 'partly', 'missed'));
    ALTER TABLE playlists ADD COLUMN parent_id INTEGER REFERENCES playlists(id) ON DELETE SET NULL;
    CREATE TABLE IF NOT EXISTS review_history (
        id           INTEGER PRIMARY KEY,
        playlist_id  INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
        text         TEXT NOT NULL,
        created_at   TEXT NOT NULL
    );
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
        about               TEXT NOT NULL DEFAULT '',
        evidence            TEXT NOT NULL,
        status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'expired')),
        superseded_by       INTEGER REFERENCES lessons(id) ON DELETE SET NULL,
        created_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_used_at        TEXT,
        use_count           INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS lessons_by_user ON lessons (spotify_id, status);
    """,
    # 8 (Phase 16): the rendered memory block and how often it was rebuilt
    """
    ALTER TABLE users ADD COLUMN memory_block TEXT;
    ALTER TABLE users ADD COLUMN memory_version INTEGER NOT NULL DEFAULT 0;
    """,
    # 9 (Phase 17): open the Spotify app on a phone when a playlist is ready
    """
    ALTER TABLE users ADD COLUMN open_spotify INTEGER NOT NULL DEFAULT 1;
    """,
    # 10 (Phase 18): what each person shares with friends (no row: nothing), who used it, and hidden playlists
    """
    ALTER TABLE playlists ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE IF NOT EXISTS sharing (
        spotify_id       TEXT PRIMARY KEY REFERENCES users(spotify_id) ON DELETE CASCADE,
        show_taste       INTEGER NOT NULL DEFAULT 0 CHECK (show_taste IN (0, 1)),
        allow_blends     INTEGER NOT NULL DEFAULT 0 CHECK (allow_blends IN (0, 1)),
        allow_listen_as  INTEGER NOT NULL DEFAULT 0 CHECK (allow_listen_as IN (0, 1)),
        updated_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS taste_access (
        id           INTEGER PRIMARY KEY,
        owner        TEXT NOT NULL REFERENCES users(spotify_id) ON DELETE CASCADE,
        viewer       TEXT NOT NULL REFERENCES users(spotify_id) ON DELETE CASCADE,
        kind         TEXT NOT NULL CHECK (kind IN ('view', 'blend', 'listen_as')),
        playlist_id  INTEGER REFERENCES playlists(id) ON DELETE SET NULL,
        at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE INDEX IF NOT EXISTS taste_access_by_owner ON taste_access (owner, at);
    CREATE INDEX IF NOT EXISTS taste_access_by_viewer ON taste_access (viewer);
    """,
    # 11 (Phase 19): the shared music library. The known-songs pool moves into it, so every cached profile is
    # rebuilt once (which fills the library) and the old JSON copy goes
    """
    CREATE TABLE IF NOT EXISTS tracks (
        uri           TEXT PRIMARY KEY,
        name          TEXT NOT NULL,
        artists_json  TEXT NOT NULL,
        duration_ms   INTEGER NOT NULL,
        explicit      INTEGER NOT NULL DEFAULT 0,
        first_seen    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_seen     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS library (
        spotify_id    TEXT NOT NULL REFERENCES users(spotify_id) ON DELETE CASCADE,
        uri           TEXT NOT NULL REFERENCES tracks(uri) ON DELETE CASCADE,
        source        TEXT NOT NULL CHECK (source IN ('top', 'recent', 'liked', 'own_playlist', 'moodify')),
        weight        INTEGER NOT NULL,
        position      INTEGER NOT NULL,
        refreshed_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (spotify_id, uri, source)
    );
    CREATE INDEX IF NOT EXISTS library_by_uri ON library (uri);
    UPDATE users SET taste_updated_at = NULL;
    ALTER TABLE users DROP COLUMN taste_pool_json;
    """,
    # 12 (Phase 20): Blends, who is in each, and whose taste each track came from
    """
    CREATE TABLE IF NOT EXISTS blends (
        id           INTEGER PRIMARY KEY,
        playlist_id  INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
        owner        TEXT NOT NULL REFERENCES users(spotify_id) ON DELETE CASCADE,
        lean         TEXT REFERENCES users(spotify_id) ON DELETE SET NULL,
        created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS blend_members (
        blend_id     INTEGER NOT NULL REFERENCES blends(id) ON DELETE CASCADE,
        spotify_id   TEXT NOT NULL REFERENCES users(spotify_id) ON DELETE CASCADE,
        position     INTEGER NOT NULL,
        PRIMARY KEY (blend_id, spotify_id)
    );
    CREATE TABLE IF NOT EXISTS blend_credits (
        blend_id     INTEGER NOT NULL REFERENCES blends(id) ON DELETE CASCADE,
        position     INTEGER NOT NULL,
        spotify_id   TEXT NOT NULL REFERENCES users(spotify_id) ON DELETE CASCADE,
        PRIMARY KEY (blend_id, position, spotify_id)
    );
    CREATE INDEX IF NOT EXISTS blend_members_by_user ON blend_members (spotify_id);
    """,
    # 13 (Phase 21): whose taste a "hear it like a friend" playlist was made with
    """
    ALTER TABLE playlists ADD COLUMN heard_as TEXT REFERENCES users(spotify_id) ON DELETE SET NULL;
    """,
    # 14 (Phase 22): how each library track sounds, judged by the LLM; goes when its track goes
    """
    CREATE TABLE IF NOT EXISTS track_features (
        uri          TEXT PRIMARY KEY REFERENCES tracks(uri) ON DELETE CASCADE,
        energy       INTEGER NOT NULL CHECK (energy BETWEEN 1 AND 10),
        mood         INTEGER NOT NULL CHECK (mood BETWEEN 1 AND 10),
        texture      INTEGER NOT NULL CHECK (texture BETWEEN 1 AND 10),
        groove       INTEGER NOT NULL CHECK (groove BETWEEN 1 AND 10),
        vocals       INTEGER NOT NULL CHECK (vocals BETWEEN 1 AND 10),
        era          INTEGER NOT NULL,
        genres_json  TEXT NOT NULL DEFAULT '[]',
        language     TEXT NOT NULL,
        model        TEXT NOT NULL,
        judged_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # 15 (Phase 25): a user is a Moodify account on some music service, not a Spotify account. RENAME COLUMN
    # also rewrites the foreign keys and indexes that name the column. Ids keep their values
    """
    ALTER TABLE users RENAME COLUMN spotify_id TO user_id;
    ALTER TABLE playlists RENAME COLUMN spotify_id TO user_id;
    ALTER TABLE jobs RENAME COLUMN spotify_id TO user_id;
    ALTER TABLE search_cache RENAME COLUMN spotify_id TO user_id;
    ALTER TABLE track_likes RENAME COLUMN spotify_id TO user_id;
    ALTER TABLE lessons RENAME COLUMN spotify_id TO user_id;
    ALTER TABLE sharing RENAME COLUMN spotify_id TO user_id;
    ALTER TABLE library RENAME COLUMN spotify_id TO user_id;
    ALTER TABLE blend_members RENAME COLUMN spotify_id TO user_id;
    ALTER TABLE blend_credits RENAME COLUMN spotify_id TO user_id;
    ALTER TABLE users RENAME COLUMN likes_to_spotify TO likes_to_service;
    ALTER TABLE users RENAME COLUMN open_spotify TO open_app;
    ALTER TABLE playlists RENAME COLUMN spotify_url TO url;
    ALTER TABLE track_likes RENAME COLUMN added_to_spotify TO added_to_service;
    ALTER TABLE users ADD COLUMN service TEXT NOT NULL DEFAULT 'spotify' CHECK (service IN ('spotify', 'youtube', 'apple'));
    """,
    # 16 (Phase 27): quota units spent per service and day
    """
    CREATE TABLE IF NOT EXISTS service_quota (
        service  TEXT NOT NULL,
        day      TEXT NOT NULL,
        units    INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (service, day)
    );
    """,
    # 17 (Phase 30): the question a build asks before it goes on, and the answer (src/clarify.py)
    """
    ALTER TABLE jobs ADD COLUMN question_json TEXT;
    ALTER TABLE jobs ADD COLUMN answer TEXT;
    """,
]
SCHEMA_VERSION = len(MIGRATIONS)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Off by default and per connection; without it ON DELETE CASCADE does nothing
    conn.execute("PRAGMA foreign_keys = ON")
    # SQLite's recommended setting under WAL: no corruption risk, far fewer disk syncs per commit
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(db_path: Optional[Path] = None) -> None:
    """Create the database, or bring an older one up to the current schema. Safe to run on every startup."""
    db_path = Path(db_path or get_settings().DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    try:
        # WAL is stored in the file itself, so setting it once here covers every later connection
        conn.execute("PRAGMA journal_mode = WAL")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'users'").fetchone() is None:
            _apply(conn, SCHEMA_PATH.read_text(), SCHEMA_VERSION)
            return
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for number, step in enumerate(MIGRATIONS[version:], start=version + 1):
            _apply(conn, step, number)
    finally:
        conn.close()


def _apply(conn: sqlite3.Connection, script: str, version: int) -> None:
    # One transaction per step, version included: a failed step leaves the database as it was
    conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")


@contextmanager
def get_conn(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Yield a connection that commits on success, rolls back on error, and always closes."""
    conn = _connect(Path(db_path or get_settings().DB_PATH))
    try:
        with conn:
            yield conn
    finally:
        conn.close()
