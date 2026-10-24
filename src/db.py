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
