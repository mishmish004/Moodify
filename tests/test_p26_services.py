"""Phase 26: one interface for every music service. A fake second service, defined here, does what a Spotify user's
builds, likes and Blends need without one Spotify call, and every page names the user's own service."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import library, music, pages, playlists, sharing, users
from src.core.exceptions import ServiceError
from src.db import get_conn, init_db
from src.main import app
from src.music import Info, Listening, Playlist, Track
from src.spotify import auth
from src.youtube import auth as youtube_auth

from .conftest import default_brief, finish_build
from .test_p4_taste import ONBOARDING
from .test_p6_playlist import CHAT_URL, SONG_MS, _completion, _songs
from .test_p25_accounts import _cookie

ROOT = Path(__file__).resolve().parents[1]
FAKE = Info("youtube", "Tunebox", "Favourites", can_queue=False, pool_explicit=False, max_searches=40, name_chars=30,
            description_chars=500, access_url="https://tunebox.example/apps")
KNOWN = Track("youtube:track:known1", "Known Song", ("Known Artist",), SONG_MS)


class FakeService:
    """A whole service in memory: every searched song exists there; likes are a set."""

    def __init__(self):
        self.searches, self.created, self.added, self.liked = [], [], [], set()
        self.fail_likes = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def listening(self) -> Listening:
        return Listening(liked=[KNOWN], followed=["Known Artist"], playlist_names=["Road trip"])

    def search_track(self, title, artist):
        self.searches.append((title, artist))
        return [Track(f"youtube:track:{title.replace(' ', '-')}", title, (artist,), SONG_MS)]

    def create_playlist(self, name, description="", public=False):
        self.created.append((name, description, public))
        return Playlist("PL1", "https://music.youtube.com/playlist?list=PL1")

    def add_items(self, playlist_id, uris):
        self.added += uris

    def check_room(self, songs):
        pass

    def is_liked(self, uri):
        return uri in self.liked

    def like(self, uri):
        if self.fail_likes:
            raise ServiceError(503, "down")
        self.liked.add(uri)

    def unlike(self, uri):
        self.liked.discard(uri)


@pytest.fixture
def fake(monkeypatch):
    service = FakeService()
    monkeypatch.setitem(music.INFO, "youtube", FAKE)
    spotify = music.for_user
    monkeypatch.setattr(music, "for_user", lambda user: service if user["service"] == "youtube" else spotify(user))
    return service


@pytest.fixture
def client(test_settings, fake):
    init_db()
    with get_conn() as conn:
        conn.execute("INSERT INTO users (user_id, display_name, refresh_token, service, onboarding_json, granted_scopes)"
                     " VALUES ('youtube:t1', 'Tess Fake', 'r', 'youtube', ?, ?)", (json.dumps(ONBOARDING), youtube_auth.SCOPES))
    with TestClient(app, follow_redirects=False) as c:
        c.cookies.set("moodify_session", _cookie(test_settings, {"user_id": "youtube:t1"}))
        yield c


def _build(client, respx_mock, briefs, mood="rainy sunday", **brief):
    briefs.answer = default_brief(mood, total_minutes=30, **brief)
    respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(15)))
    return finish_build(client, client.post("/playlist", data={"mood": mood}))


def _spotify_calls(respx_mock):
    return [c for c in respx_mock.calls if "spotify.com" in str(c.request.url)]


def test_a_full_build_on_another_service_never_calls_spotify(client, fake, respx_mock, briefs):
    page = _build(client, respx_mock, briefs)
    assert page.status_code == 200
    assert fake.created[0][0] == "Moodify: rainy sunday" and fake.created[0][2] is False
    assert fake.added and all(uri.startswith("youtube:track:") for uri in fake.added)
    assert not _spotify_calls(respx_mock)
    row = playlists.recent("youtube:t1")[0]
    assert row["url"] == "https://music.youtube.com/playlist?list=PL1"
    assert "Open in Tunebox" in page.text and "https://music.youtube.com/watch?v=Song-1" in page.text
    # The taste came from the fake service and went into the library under its URIs
    assert [t.uri for t, _ in library.for_user("youtube:t1")] == [KNOWN.uri]


def test_the_services_limits_shape_the_playlist(client, fake, respx_mock, briefs):
    long = "x" * 80
    _build(client, respx_mock, briefs, mood=long)
    name, description, _ = fake.created[0]
    assert len(name) == FAKE.name_chars and len(description) <= FAKE.description_chars


def test_under_no_explicit_a_known_song_without_the_flag_is_searched(client, fake, respx_mock, briefs):
    """The fake service's taste reads don't say what's explicit (pool_explicit False), so the known song is
    looked up, where the search result carries the flag."""
    briefs.answer = default_brief("clean", total_minutes=10, hard_rules={**default_brief("x")["hard_rules"], "no_explicit": True})
    respx_mock.post(CHAT_URL).respond(200, json=_completion([{"title": "Known Song", "artist": "Known Artist", "known": 1}]))
    finish_build(client, client.post("/playlist", data={"mood": "clean"}))
    assert ("Known Song", "Known Artist") in fake.searches


def test_likes_go_to_the_users_own_service(client, fake, respx_mock, briefs):
    page = _build(client, respx_mock, briefs)
    playlist_id = playlists.recent("youtube:t1")[0]["id"]
    first = playlists.track_at(playlist_id, 0)["uri"]
    resp = client.post(f"/playlists/{playlist_id}/tracks/0/like", headers={"accept": "application/json"})
    assert resp.json() == {"liked": True, "notice": None} and fake.liked == {first}
    client.post(f"/playlists/{playlist_id}/tracks/0/unlike", headers={"accept": "application/json"})
    assert fake.liked == set()
    fake.fail_likes = True
    resp = client.post(f"/playlists/{playlist_id}/tracks/1/like", headers={"accept": "application/json"})
    assert resp.json()["notice"] == "Liked in Moodify, but your Tunebox Favourites couldn't be updated."
    assert "saved to your Tunebox Favourites too" in page.text
    assert not _spotify_calls(respx_mock)


def test_a_blend_with_a_spotify_friend_is_made_on_the_requesters_service(client, fake, respx_mock, briefs):
    """The friend's known songs are Spotify tracks; the requester's playlist can only hold their own service's,
    so a picked one is searched there instead of copied in."""
    friend_song = Track("spotify:track:sam1", "Sam's Song", ("Sam Artist",), SONG_MS)
    users.upsert("friend2", "Sam Rivera", "refresh-friend2", auth.SCOPES)
    users.set_onboarding("friend2", {"genres": "jazz", "artists": "Bill Evans", "energy": "low"})
    sharing.save("friend2", False, True, False)
    library.replace("friend2", {"liked": [friend_song]})
    briefs.answer = default_brief("together", total_minutes=10)
    respx_mock.post(CHAT_URL).respond(200, json=_completion(
        [{"title": "Sam's Song", "artist": "Sam Artist", "known": n} for n in range(1, 3)] + _songs(6)))
    resp = client.post("/blend", data={"mood": "together", "friends": ["friend2"]})
    page = finish_build(client, resp)
    assert page.status_code == 200 and fake.created
    assert "spotify:track:sam1" not in fake.added and "youtube:track:Sam's-Song" in fake.added
    assert ("Sam's Song", "Sam Artist") in fake.searches
    assert not _spotify_calls(respx_mock)


PAGES = ["/", "/history", "/memory", "/sharing", "/friends", "/blend", "/map", "/activity", "/settings", "/onboarding"]


def test_every_page_names_the_users_service_and_never_spotify(client, fake, respx_mock, briefs):
    page = _build(client, respx_mock, briefs)
    playlist_id = playlists.recent("youtube:t1")[0]["id"]
    with get_conn() as conn:
        job_id = conn.execute("SELECT id FROM jobs").fetchone()[0]
    for path in PAGES + [f"/playlists/{playlist_id}", f"/jobs/{job_id}"]:
        resp = client.get(path)
        assert resp.status_code in (200, 303), path
        text = client.get(resp.headers["location"]).text if resp.status_code == 303 else resp.text
        assert "spotify" not in text.lower(), path
    assert "Tunebox" in page.text


def test_no_queue_where_the_service_has_none(client, fake, respx_mock, briefs):
    page = _build(client, respx_mock, briefs)
    playlist_id = playlists.recent("youtube:t1")[0]["id"]
    assert "/queue/" not in page.text and "Play now" not in page.text
    assert client.post(f"/queue/{playlist_id}").status_code == 404


def test_disconnect_names_where_to_take_back_access(client, fake, respx_mock):
    respx_mock.post(youtube_auth.REVOKE_URL).respond(200)  # a youtube user's token is taken back too (Phase 27)
    page = client.post("/disconnect")
    assert "https://tunebox.example/apps" in page.text and "Spotify" not in page.text
    assert users.get("youtube:t1") is None


def test_build_steps_carry_the_services_name():
    steps = pages.build_steps("Finding the songs on Tunebox (3/12)", service="Tunebox")
    assert [s["state"] for s in steps] == ["done", "done", "done", "now", "wait"]
    assert steps[3]["label"] == "Finding the songs on Tunebox (3/12)" and steps[3]["percent"] == 25
    assert steps[4]["label"] == "Saving the playlist to your Tunebox"


def test_only_the_spotify_package_and_the_service_switch_name_the_spotify_client():
    naming = {str(p.relative_to(ROOT)) for p in (ROOT / "src").rglob("*.py") if "SpotifyClient" in p.read_text()}
    assert naming == {"src/spotify/client.py", "src/music/__init__.py"}
