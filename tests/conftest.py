"""Shared fixtures. Every test gets settings built from TEST_ENV alone, its own SQLite file,
and no real network unless it's marked live."""
import socket
import time
from pathlib import Path

import pytest

from src import jobs
from src.core.config import Settings, get_settings
from src.spotify.client import Track

ROOT = Path(__file__).resolve().parents[1]

TEST_ENV = {
    "ENV": "test",
    "SECRET_KEY": "test-secret-key",
    "SPOTIFY_CLIENT_ID": "test-client-id",
    "OPENAI_API_KEY": "test-openai-key",
}


@pytest.fixture(autouse=True)
def test_settings(request, monkeypatch, tmp_path) -> Settings:
    """Ignore the real .env and any exported variables, so results don't depend on the machine.
    Live tests are the exception: they use the real .env and data/moodify.db."""
    get_settings.cache_clear()
    if request.node.get_closest_marker("live"):
        yield get_settings()
        get_settings.cache_clear()
        return
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
    for name, value in {**TEST_ENV, "DB_PATH": str(tmp_path / "moodify.db")}.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def no_leftover_builds():
    """A build thread that outlived its test would write into the next test's database."""
    yield
    assert jobs.wait_idle(timeout=15), "a background build was still running 15 s after its test ended"


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    """Fail any real connection. respx and TestClient answer before a socket is opened, so mocked
    calls still work; a call that slipped past the mocks fails here instead of reaching Spotify.
    Docker tests are exempt: they talk to the container on 127.0.0.1."""
    if request.node.get_closest_marker("live") or request.node.get_closest_marker("docker"):
        return

    def blocked(*args, **kwargs):
        raise RuntimeError(f"real network access in an offline test: {args[1:] or args}")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


def finish_build(client, resp, timeout: float = 10.0):
    """Follow a POST /playlist through its background build and return the final page:
    the playlist page when it worked, the explanation when it didn't."""
    assert resp.status_code == 303 and resp.headers["location"].startswith("/jobs/"), resp.text
    url, deadline = resp.headers["location"], time.monotonic() + timeout
    while True:
        page = client.get(url)
        if page.status_code == 303:
            return client.get(page.headers["location"])
        if 'http-equiv="refresh"' not in page.text:
            return page
        assert time.monotonic() < deadline, f"build still running after {timeout} s:\n{page.text}"
        time.sleep(0.05)


def tracks(*uris: str) -> list[Track]:
    """Placeholder Track objects for rows that only need URIs."""
    return [Track(uri, f"Track {uri.rsplit(':', 1)[-1]}", ("Someone",), 180_000) for uri in uris]
