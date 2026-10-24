"""Phase 1: settings fail loudly, the database exists, nothing else."""
import json
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.core.config import PROJECT_ROOT, Settings
from src.db import get_conn, init_db
from src.main import app

from .conftest import ROOT, TEST_ENV

REQUIRED = ["SECRET_KEY", "SPOTIFY_CLIENT_ID", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "OPENAI_API_KEY"]


# --- settings ----------------------------------------------------------------

@pytest.mark.parametrize("name", REQUIRED)
def test_missing_required_setting_raises_validation_error(monkeypatch, name):
    # pytest.raises only catches ValidationError; a sys.exit() would fail this test
    monkeypatch.delenv(name)
    with pytest.raises(ValidationError, match=name):
        Settings()


def test_empty_spotify_client_id_is_rejected(monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "")
    with pytest.raises(ValidationError, match="SPOTIFY_CLIENT_ID"):
        Settings()


def test_secrets_hidden_from_repr(monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test-spotify-secret")
    settings = Settings()
    secrets = [TEST_ENV["SECRET_KEY"], TEST_ENV["OPENAI_API_KEY"], TEST_ENV["GOOGLE_CLIENT_SECRET"], "test-spotify-secret"]
    for secret in secrets:
        assert secret not in repr(settings)
        assert secret not in str(settings)
    assert settings.OPENAI_API_KEY.get_secret_value() == TEST_ENV["OPENAI_API_KEY"]


def test_validation_error_does_not_echo_secrets(monkeypatch):
    monkeypatch.delenv("SECRET_KEY")
    with pytest.raises(ValidationError) as exc:
        Settings()
    assert TEST_ENV["OPENAI_API_KEY"] not in str(exc.value)


def test_defaults(monkeypatch):
    monkeypatch.delenv("DB_PATH")
    settings = Settings()
    assert settings.OPENAI_MODEL == "gpt-5.4-mini"
    assert settings.SPOTIFY_REDIRECT_URI == "http://127.0.0.1:9091/callback"
    assert settings.SPOTIFY_CLIENT_SECRET is None
    assert settings.DB_PATH == PROJECT_ROOT / "data" / "moodify.db"


def test_env_example_lists_every_setting():
    text = (ROOT / ".env.example").read_text()
    missing = [name for name in Settings.model_fields if not re.search(rf"^#?\s*{name}=", text, re.M)]
    assert not missing, f".env.example is missing {missing}"


# --- storage -----------------------------------------------------------------

@pytest.fixture
def db(test_settings):
    init_db()
    return test_settings.DB_PATH


def _schema(db_path):
    with get_conn(db_path) as conn:
        return [tuple(r) for r in conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name")]


def _add_user(conn, spotify_id="u1"):
    conn.execute(
        "INSERT INTO users (spotify_id, display_name, refresh_token) VALUES (?, ?, ?)",
        (spotify_id, "Test User", "refresh-1"),
    )


def _add_playlist(conn, spotify_id="u1"):
    return conn.execute(
        "INSERT INTO playlists (spotify_id, prompt, spotify_url, track_uris_json) VALUES (?, ?, ?, ?)",
        (spotify_id, "rainy sunday", "https://open.spotify.com/playlist/p1", json.dumps(["spotify:track:a"])),
    ).lastrowid


def test_init_db_is_idempotent(tmp_path):
    db_path = tmp_path / "nested" / "moodify.db"
    init_db(db_path)
    with get_conn(db_path) as conn:
        _add_user(conn)
    before = _schema(db_path)
    init_db(db_path)
    assert _schema(db_path) == before
    with get_conn(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1


def test_wal_mode(db):
    with get_conn() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_users_round_trip(db):
    onboarding = {"genres": ["shoegaze"], "energy": "low"}
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (spotify_id, display_name, refresh_token, onboarding_json, taste_summary,"
            " taste_updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("u1", "Test User", "refresh-1", json.dumps(onboarding), "likes slow guitars", "2026-10-23 10:00:00"),
        )
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE spotify_id = ?", ("u1",)).fetchone()
    assert row["display_name"] == "Test User"
    assert row["refresh_token"] == "refresh-1"
    assert json.loads(row["onboarding_json"]) == onboarding
    assert row["taste_summary"] == "likes slow guitars"
    assert row["taste_updated_at"] == "2026-10-23 10:00:00"
    assert row["created_at"]


def test_playlists_round_trip(db):
    with get_conn() as conn:
        _add_user(conn)
        playlist_id = _add_playlist(conn)
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
    assert row["spotify_id"] == "u1"
    assert row["prompt"] == "rainy sunday"
    assert row["spotify_url"] == "https://open.spotify.com/playlist/p1"
    assert json.loads(row["track_uris_json"]) == ["spotify:track:a"]
    assert row["created_at"]


def test_playlist_needs_an_existing_user(db):
    with pytest.raises(sqlite3.IntegrityError):
        with get_conn() as conn:
            _add_playlist(conn, spotify_id="nobody")


def test_deleting_user_cascades_to_playlists(db):
    with get_conn() as conn:
        _add_user(conn, "u1")
        _add_user(conn, "u2")
        _add_playlist(conn, "u1")
        _add_playlist(conn, "u1")
        _add_playlist(conn, "u2")
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE spotify_id = ?", ("u1",))
    with get_conn() as conn:
        owners = [r["spotify_id"] for r in conn.execute("SELECT spotify_id FROM playlists")]
    assert owners == ["u2"]


def test_get_conn_rolls_back_on_error(db):
    with pytest.raises(RuntimeError):
        with get_conn() as conn:
            _add_user(conn)
            raise RuntimeError("boom")
    with get_conn() as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0


def test_app_startup_creates_the_database(test_settings):
    assert not test_settings.DB_PATH.exists()
    with TestClient(app):
        pass
    tables = {name for _, name, _ in _schema(test_settings.DB_PATH)}
    assert {"users", "playlists"} <= tables
