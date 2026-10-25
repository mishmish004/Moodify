"""Phase 25: a user is a Moodify account on some music service. spotify_id became user_id everywhere, the
Spotify-named columns got service-neutral names, and users.service says which service. Ids keep their values."""
import base64
import json
import sqlite3

import itsdangerous
import pytest
from fastapi.testclient import TestClient

from src import db, users
from src.db import get_conn, init_db
from src.main import app
from src.spotify import auth

from .test_p16_memory import assert_no_rows_for
from .test_p9_jobs import V0_SCHEMA, _schema

# One row (or two) in every table, written with the column names a version-14 database has
V14_ROWS = [
    "INSERT INTO users (spotify_id, display_name, refresh_token, onboarding_json, taste_summary, granted_scopes,"
    " likes_to_spotify, open_spotify) VALUES ('u1', 'One', 'r1', '{}', 'likes rain', 's', 0, 0),"
    " ('u2', 'Two', 'r2', '{}', NULL, 's', 1, 1)",
    "INSERT INTO playlists (id, spotify_id, prompt, spotify_url, track_uris_json, rating, review, heard_as)"
    " VALUES (1, 'u1', 'rain', 'https://open.spotify.com/playlist/p1', '[\"spotify:track:a\"]', 7, 'good', 'u2'),"
    " (2, 'u2', 'sun', 'https://open.spotify.com/playlist/p2', '[]', NULL, NULL, NULL)",
    "INSERT INTO playlist_tracks (playlist_id, position, uri, name, artists_json, duration_ms)"
    " VALUES (1, 0, 'spotify:track:a', 'A', '[\"X\"]', 1000)",
    "INSERT INTO jobs (id, spotify_id, prompt, status, playlist_id) VALUES (1, 'u1', 'rain', 'done', 1)",
    "INSERT INTO job_events (job_id, kind) VALUES (1, 'step')",
    "INSERT INTO search_cache (spotify_id, query, tracks_json) VALUES ('u1', 'a|x', '[]')",
    "INSERT INTO track_likes (spotify_id, uri, playlist_id, added_to_spotify) VALUES ('u1', 'spotify:track:a', 1, 1)",
    "INSERT INTO review_history (playlist_id, text, created_at) VALUES (1, 'ok', '2026-10-24 10:00:00')",
    "INSERT INTO lessons (spotify_id, source_playlist_id, title, description, content, kind, evidence)"
    " VALUES ('u1', 1, 't', 'd', 'c', 'preference', 'e')",
    "INSERT INTO sharing (spotify_id, show_taste, allow_blends, allow_listen_as) VALUES ('u1', 1, 1, 1), ('u2', 1, 1, 1)",
    "INSERT INTO taste_access (owner, viewer, kind, playlist_id) VALUES ('u2', 'u1', 'listen_as', 1)",
    "INSERT INTO tracks (uri, name, artists_json, duration_ms) VALUES ('spotify:track:a', 'A', '[\"X\"]', 1000)",
    "INSERT INTO library (spotify_id, uri, source, weight, position) VALUES ('u1', 'spotify:track:a', 'liked', 1, 0),"
    " ('u2', 'spotify:track:a', 'top', 3, 0)",
    "INSERT INTO blends (id, playlist_id, owner, lean) VALUES (1, 1, 'u1', 'u2')",
    "INSERT INTO blend_members (blend_id, spotify_id, position) VALUES (1, 'u1', 0), (1, 'u2', 1)",
    "INSERT INTO blend_credits (blend_id, position, spotify_id) VALUES (1, 0, 'u2')",
    "INSERT INTO track_features (uri, energy, mood, texture, groove, vocals, era, language, model)"
    " VALUES ('spotify:track:a', 5, 5, 5, 5, 5, 2010, 'en', 'm')",
]


def _v14_database(path):
    """A database as Phase 24 left it, with rows in every table."""
    conn = sqlite3.connect(path)
    conn.executescript(V0_SCHEMA)
    conn.close()
    conn = db._connect(path)
    for number, step in enumerate(db.MIGRATIONS[:14], start=1):
        db._apply(conn, step, number)
    with conn:
        for statement in V14_ROWS:
            conn.execute(statement)
    conn.close()


def _counts(path) -> dict[str, int]:
    with get_conn(path) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}


@pytest.fixture
def upgraded(test_settings):
    _v14_database(test_settings.DB_PATH)
    before = _counts(test_settings.DB_PATH)
    init_db()
    return before


def test_a_version_14_database_upgrades_with_every_row_and_foreign_key(upgraded, test_settings, tmp_path):
    after = _counts(test_settings.DB_PATH)  # later migrations may add tables of their own
    assert {t: after[t] for t in upgraded} == upgraded and all(upgraded.values())
    with get_conn() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    init_db(tmp_path / "fresh.db")
    assert _schema(test_settings.DB_PATH) == _schema(tmp_path / "fresh.db")


def test_ids_and_settings_keep_their_values_under_the_new_names(upgraded):
    one, two = users.get("u1"), users.get("u2")
    assert (one["service"], one["likes_to_service"], one["open_app"]) == ("spotify", 0, 0)
    assert (two["service"], two["likes_to_service"], two["open_app"]) == ("spotify", 1, 1)
    with get_conn() as conn:
        assert conn.execute("SELECT user_id, url FROM playlists WHERE id = 1").fetchone()[:] == (
            "u1", "https://open.spotify.com/playlist/p1")
        assert conn.execute("SELECT added_to_service FROM track_likes").fetchone()[0] == 1
        assert sorted(r[0] for r in conn.execute("SELECT user_id FROM blend_members")) == ["u1", "u2"]


@pytest.mark.parametrize("fresh", [False, True])
def test_no_table_has_a_spotify_named_column(test_settings, fresh):
    if not fresh:
        _v14_database(test_settings.DB_PATH)
    init_db()
    with get_conn() as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        columns = {(t, r[1]) for t in tables for r in conn.execute(f"PRAGMA table_info({t})")}
        sql = " ".join(r[0] or "" for r in conn.execute("SELECT sql FROM sqlite_master"))
    assert not [c for c in columns if "spotify" in c[1]]
    assert "spotify_id" not in sql


def test_service_only_takes_known_services(upgraded):
    with pytest.raises(sqlite3.IntegrityError):
        with get_conn() as conn:
            conn.execute("UPDATE users SET service = 'tidal' WHERE user_id = 'u1'")


def test_disconnect_after_the_upgrade_deletes_every_row_about_the_user(upgraded):
    """The rewritten foreign keys still cascade."""
    users.delete("u1")
    assert_no_rows_for("u1")
    assert users.get("u2") is not None


def _cookie(test_settings, session: dict) -> str:
    """A session cookie the way Starlette's SessionMiddleware signs it."""
    signer = itsdangerous.TimestampSigner(test_settings.SECRET_KEY.get_secret_value())
    return signer.sign(base64.b64encode(json.dumps(session).encode())).decode()


def test_a_session_cookie_from_before_the_upgrade_still_logs_in(upgraded, test_settings):
    with get_conn() as conn:
        # every scope granted, and a fresh cached taste, so the mood box opens without calling Spotify
        conn.execute("UPDATE users SET granted_scopes = ?, taste_updated_at = CURRENT_TIMESTAMP WHERE user_id = 'u1'",
                     (auth.SCOPES,))
    with TestClient(app, follow_redirects=False) as client:
        resp = client.get("/", headers={"cookie": f"moodify_session={_cookie(test_settings, {'spotify_id': 'u1'})}"})
        # u1 has onboarding answers, so the mood box opens instead of the login page
        assert resp.status_code == 200
        renewed = resp.cookies.get("moodify_session")
    assert renewed, "the cookie is rewritten under the new key"
    payload = json.loads(base64.b64decode(renewed.split(".")[0]))
    assert payload == {"user_id": "u1"}
