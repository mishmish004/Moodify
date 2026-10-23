"""Shared fixtures. Every test gets settings built from TEST_ENV alone, its own SQLite file,
and no real network unless it's marked live."""
import socket
from pathlib import Path

import pytest

from src.core.config import Settings, get_settings

ROOT = Path(__file__).resolve().parents[1]

TEST_ENV = {
    "ENV": "test",
    "SECRET_KEY": "test-secret-key",
    "SPOTIFY_CLIENT_ID": "test-client-id",
    "OPENAI_API_KEY": "test-openai-key",
}


@pytest.fixture(autouse=True)
def test_settings(monkeypatch, tmp_path) -> Settings:
    """Ignore the real .env and any exported variables, so results don't depend on the machine."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
    for name, value in {**TEST_ENV, "DB_PATH": str(tmp_path / "moodify.db")}.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    """Fail any real connection. respx and TestClient answer before a socket is opened, so mocked
    calls still work; a call that slipped past the mocks fails here instead of reaching Spotify."""
    if request.node.get_closest_marker("live"):
        return

    def blocked(*args, **kwargs):
        raise RuntimeError(f"real network access in an offline test: {args[1:] or args}")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
