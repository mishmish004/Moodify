"""Shared fixtures. Every test gets settings built from TEST_ENV alone, its own SQLite file,
and no real network unless it's marked live."""
import json
import socket
import time
from pathlib import Path

import pytest

import httpx

from src import jobs
from src.core.config import Settings, get_settings
from src.spotify.client import API_URL, Track

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


CHAT_URL = "https://api.openai.com/v1/chat/completions"


class BriefMock:
    """Answers the brief call (src/brief.py) in offline tests, which builds make before suggesting songs.
    By default: one section, titled with the request's first line. Set `answer` (a Brief dict), `raw`
    (the reply text as is) or `status` (an HTTP error) to change it; `calls` holds the requests."""

    def __init__(self):
        self.answer: dict | None = None
        self.raw: str | None = None
        self.status = 200
        self.calls: list = []

    def install(self, respx_mock) -> None:
        """Route brief calls here. Again after a test clears its routes."""
        respx_mock.post(CHAT_URL, json__response_format__json_schema__name="Brief").mock(side_effect=self)

    def __call__(self, request):
        self.calls.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "mocked failure"}})
        request_text = json.loads(request.content)["messages"][-1]["content"].split("Request: ", 1)[-1]
        content = self.raw if self.raw is not None else json.dumps(
            self.answer or default_brief(request_text.split("\n", 1)[0][:40]))
        return httpx.Response(200, json={"id": "b", "object": "chat.completion", "created": 0, "model": "m", "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}]})


def default_brief(title: str, **changes) -> dict:
    brief = {
        "title": title or "Your mood", "summary": "Read as one mood.",
        "sections": [{"label": "Your mood", "feeling": "as described", "energy": 5, "minutes": None, "activity": None,
                      "transition_in": "blend"}],
        "total_minutes": None,
        "hard_rules": {"no_vocals": False, "no_explicit": False, "no_sudden_loud_changes": False, "steady_tempo": False,
                       "languages": [], "avoid": [], "max_track_minutes": None, "min_track_minutes": None},
        "familiarity": "default", "tags": [], "support_note": False,
    }
    return {**brief, **changes}


@pytest.fixture(autouse=True)
def briefs(request):
    """Every offline test gets the brief route, ahead of any catch-all chat route a test adds later."""
    if request.node.get_closest_marker("live") or request.node.get_closest_marker("docker"):
        yield None
        return
    mock = BriefMock()
    mock.install(request.getfixturevalue("respx_mock"))
    yield mock


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


def track_json(track: Track) -> dict:
    return {"type": "track", "uri": track.uri, "name": track.name, "duration_ms": track.duration_ms,
            "artists": [{"name": a} for a in track.artists]}


def mock_listening(respx_mock, recent=(), liked=(), followed=(), own_playlists=(), playlist_items=None, fail=()):
    """Spotify's Phase 10 reads (history, Liked Songs, follows, playlists), in their Feb-2026 shapes.
    Empty by default. own_playlists: (id, name, owner_id) tuples. fail: names to answer 503 instead."""
    def status_or(name, payload):
        return httpx.Response(503) if name in fail else httpx.Response(200, json=payload)

    def liked_page(request):
        offset, limit = int(request.url.params.get("offset", 0)), int(request.url.params["limit"])
        page = list(liked)[offset:offset + limit]
        more = offset + limit < len(liked)
        return status_or("liked", {"items": [{"added_at": "2026-10-01T00:00:00Z", "track": track_json(t)} for t in page],
                                   "next": f"{API_URL}/me/tracks?offset={offset + limit}" if more else None})

    def items(request):
        playlist_id = request.url.path.split("/")[-2]
        found = (playlist_items or {}).get(playlist_id)
        if found is None:
            return httpx.Response(403, json={"error": {"status": 403, "message": "Forbidden"}})
        return status_or("items", {"items": [{"is_local": False, "item": track_json(t)} for t in found], "next": None})

    return {
        "recent": respx_mock.get(f"{API_URL}/me/player/recently-played").mock(side_effect=lambda r: status_or(
            "recent", {"items": [{"track": track_json(t), "played_at": "2026-10-23T08:00:00Z"} for t in recent]})),
        "liked": respx_mock.get(f"{API_URL}/me/tracks").mock(side_effect=liked_page),
        "followed": respx_mock.get(f"{API_URL}/me/following").mock(side_effect=lambda r: status_or(
            "followed", {"artists": {"items": [{"name": n} for n in followed], "next": None}})),
        "playlists": respx_mock.get(f"{API_URL}/me/playlists").mock(side_effect=lambda r: status_or(
            "playlists", {"items": [{"id": i, "name": n, "owner": {"id": o}, "collaborative": False,
                                     "items": {"total": 1}} for i, n, o in own_playlists]})),
        "items": respx_mock.get(url__regex=rf"{API_URL}/playlists/[^/]+/items").mock(side_effect=items),
    }


def tracks(*uris: str) -> list[Track]:
    """Placeholder Track objects for rows that only need URIs."""
    return [Track(uri, f"Track {uri.rsplit(':', 1)[-1]}", ("Someone",), 180_000) for uri in uris]
