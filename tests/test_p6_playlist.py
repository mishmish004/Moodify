"""Phase 6: suggestions -> Spotify search -> match -> a real playlist. OpenAI and Spotify mocked over HTTP."""
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from src import match, playlists, users
from src.db import get_conn, init_db
from src.main import app
from src.spotify import auth
from src.spotify.client import API_URL, SpotifyClient, Track
from src.suggest import Song

from .conftest import finish_build, tracks
from .test_p4_taste import ONBOARDING

CHAT_URL = "https://api.openai.com/v1/chat/completions"


# --- pick --------------------------------------------------------------------------

def _track(name, *artists, uri="spotify:track:x"):
    return Track(uri, name, tuple(artists or ("Queen",)), 1)


@pytest.mark.parametrize("song, candidates, expected", [
    (Song(title="Song", artist="Queen"), [_track("Song", "Queen")], "spotify:track:x"),
    (Song(title="song", artist="queen"), [_track("SONG", "Queen")], "spotify:track:x"),
    (Song(title="Song", artist="Queen"), [_track("Song - Remastered 2011", "Queen")], "spotify:track:x"),
    (Song(title="Song - Remastered 2011", artist="Queen"), [_track("Song", "Queen")], "spotify:track:x"),
    (Song(title="Song", artist="Queen"), [_track("Song (feat. X)", "Queen", "X")], "spotify:track:x"),
    (Song(title="Song (Live)", artist="Queen"), [_track("Song", "Queen")], "spotify:track:x"),
    (Song(title="Song", artist="Queen feat. Bowie"), [_track("Song", "Queen", "David Bowie")], "spotify:track:x"),
    (Song(title="Don't Stop Me Now!", artist="Queen"), [_track("Dont Stop Me Now", "Queen")], "spotify:track:x"),
    (Song(title="Jóga", artist="Bjork"), [_track("Joga", "Björk")], "spotify:track:x"),
    (Song(title="Song", artist="Simon & Garfunkel"), [_track("Song", "Simon and Garfunkel")], "spotify:track:x"),
    (Song(title="(I Can't Get No) Satisfaction", artist="The Rolling Stones"),
     [_track("(I Can't Get No) Satisfaction - Mono Version", "The Rolling Stones")], "spotify:track:x"),
    (Song(title="Song", artist="Queen"), [_track("Song", "ABBA")], None),
    (Song(title="Song", artist="Queen"), [_track("Other Song", "Queen")], None),
    (Song(title="Song", artist="Queen"), [_track("Song II", "Queen")], None),
    (Song(title="Song", artist="Queen"), [], None),
    (Song(title="", artist="Queen"), [_track("", "Queen")], None),
    (Song(title="Song", artist="Queen"),
     [_track("Song", "ABBA", uri="spotify:track:wrong"), _track("Song", "Queen", uri="spotify:track:right")], "spotify:track:right"),
])
def test_pick(song, candidates, expected):
    assert match.pick(song, candidates) == expected


# --- build, end to end ------------------------------------------------------------------

def _songs(n, start=0):
    return [{"title": f"Song {i}", "artist": f"Artist {i}"} for i in range(start, start + n)]


def _completion(songs):
    return {"id": "c", "object": "chat.completion", "created": 0, "model": "m", "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps({"songs": songs})}}]}


class FakeSpotify:
    """respx routes for one scenario. `found` says which suggested titles exist on Spotify."""

    def __init__(self, respx_mock, found):
        self.found = found
        respx_mock.post(auth.TOKEN_URL).respond(200, json={"access_token": "a", "expires_in": 3600})
        respx_mock.get(f"{API_URL}/me/top/artists").respond(200, json={"items": [{"name": "Radiohead"}]})
        respx_mock.get(f"{API_URL}/me/top/tracks").respond(200, json={"items": []})
        self.search = respx_mock.get(f"{API_URL}/search").mock(side_effect=self._search)
        self.create = respx_mock.post(f"{API_URL}/me/playlists").respond(201, json={
            "id": "pl1", "external_urls": {"spotify": "https://open.spotify.com/playlist/pl1"}})
        self.add = respx_mock.post(f"{API_URL}/playlists/pl1/items").respond(201, json={"snapshot_id": "s"})

    def _search(self, request):
        q = request.url.params["q"]  # "track:<title> artist:<artist>"
        title, artist = q[len("track:"):].split(" artist:")
        if title not in self.found:
            return httpx.Response(200, json={"tracks": {"items": []}})
        # A decoy with the wrong artist comes first; pick() must skip it
        items = [
            {"uri": f"spotify:track:decoy-{title}", "name": title, "artists": [{"name": "Someone Else"}], "duration_ms": 1},
            {"uri": f"spotify:track:{self.found[title]}", "name": title, "artists": [{"name": artist}], "duration_ms": 1},
        ]
        return httpx.Response(200, json={"tracks": {"items": items}})

    def added_uris(self):
        return [uri for call in self.add.calls for uri in json.loads(call.request.content)["uris"]]


@pytest.fixture
def user(test_settings):
    init_db()
    users.upsert("friend1", "Friend One", "refresh-1")
    users.set_onboarding("friend1", ONBOARDING)
    return users.get("friend1")


@pytest.fixture
def logged_in(user, respx_mock):
    client = TestClient(app, follow_redirects=False)
    with client:
        state = parse_qs(urlsplit(client.post("/login").headers["location"]).query)["state"][0]
        respx_mock.post(auth.TOKEN_URL).respond(200, json={"access_token": "a", "refresh_token": "refresh-1", "expires_in": 3600})
        respx_mock.get(auth.ME_URL).respond(200, json={"id": "friend1", "display_name": "Friend One"})
        client.get("/callback", params={"code": "c", "state": state})
        respx_mock.routes.clear()
        yield client


def _build(client, mood):
    return finish_build(client, client.post("/playlist", data={"mood": mood}))


def _playlist_rows():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM playlists").fetchall()


def test_end_to_end_24_of_30_match(logged_in, respx_mock):
    chat = respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(30)))
    found = {f"Song {i}": f"t{i}" for i in range(30) if i % 5 != 0}  # 6 of 30 don't exist
    sp = FakeSpotify(respx_mock, found)

    resp = _build(logged_in, "rainy sunday")

    assert resp.status_code == 200
    assert "https://open.spotify.com/playlist/pl1" in resp.text
    assert "Song 1" in resp.text and "Song 0" not in resp.text
    assert chat.call_count == 1
    assert sp.create.call_count == 1
    assert json.loads(sp.create.calls.last.request.content)["name"] == "Moodify: rainy sunday"
    uris = sp.added_uris()
    assert len(uris) == 24 and len(set(uris)) == 24
    assert not any("decoy" in u for u in uris)
    rows = _playlist_rows()
    assert len(rows) == 1
    assert rows[0]["prompt"] == "rainy sunday"
    assert rows[0]["spotify_url"] == "https://open.spotify.com/playlist/pl1"
    assert json.loads(rows[0]["track_uris_json"]) == uris


def test_stops_searching_at_25(logged_in, respx_mock):
    respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(30)))
    sp = FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(30)})
    _build(logged_in, "upbeat")
    assert len(sp.added_uris()) == 25
    assert sp.search.call_count == 25


def test_duplicate_suggestions_resolve_to_one_uri(logged_in, respx_mock):
    # Two titles that both resolve to the same Spotify track
    songs = [{"title": "Song 1", "artist": "Artist 1"}, {"title": "Song 1 - Remastered", "artist": "Artist 1"}] + _songs(10, start=2)
    respx_mock.post(CHAT_URL).respond(200, json=_completion(songs))
    found = {f"Song {i}": f"t{i}" for i in range(2, 12)}
    found["Song 1"] = found["Song 1 - Remastered"] = "same"
    sp = FakeSpotify(respx_mock, found)
    _build(logged_in, "upbeat")
    uris = sp.added_uris()
    assert uris.count("spotify:track:same") == 1
    assert len(uris) == 11


def test_fewer_than_8_matches_asks_once_more(logged_in, respx_mock):
    chat = respx_mock.post(CHAT_URL).mock(side_effect=[
        httpx.Response(200, json=_completion(_songs(30))),
        httpx.Response(200, json=_completion(_songs(15, start=100))),
    ])
    found = {f"Song {i}": f"t{i}" for i in [1, 2, 3, 4, 5]} | {f"Song {i}": f"t{i}" for i in range(100, 110)}
    sp = FakeSpotify(respx_mock, found)

    resp = _build(logged_in, "obscure")

    assert resp.status_code == 200
    assert chat.call_count == 2
    second = json.loads(chat.calls[1].request.content)["messages"][-1]["content"]
    assert "15 songs" in second and "Song 1 by Artist 1" in second
    assert len(sp.added_uris()) == 15


def test_still_few_after_retry_builds_anyway(logged_in, respx_mock):
    chat = respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(30)))
    sp = FakeSpotify(respx_mock, {"Song 3": "t3"})
    resp = _build(logged_in, "obscure")
    assert resp.status_code == 200 and chat.call_count == 2
    assert sp.added_uris() == ["spotify:track:t3"]


def test_zero_matches_creates_nothing(logged_in, respx_mock):
    respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(30)))
    sp = FakeSpotify(respx_mock, {})
    resp = _build(logged_in, "nothing exists")
    assert resp.status_code == 200
    assert "Nothing matched" in resp.text
    assert sp.create.call_count == 0
    assert _playlist_rows() == []


def test_llm_failure_is_a_friendly_page(logged_in, respx_mock):
    chat = respx_mock.post(CHAT_URL).respond(500)
    sp = FakeSpotify(respx_mock, {})
    resp = _build(logged_in, "anything")
    assert resp.status_code == 200 and "No song ideas" in resp.text
    assert chat.call_count == 2  # Phase 9: one retry, then a clear message instead of a 15-song fallback
    assert sp.search.call_count == 0 and sp.create.call_count == 0


def test_quota_exceeded_mid_search_is_a_message_and_no_partial_playlist(logged_in, respx_mock):
    respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(30)))
    sp = FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(30)})
    calls = {"n": 0}
    good = sp._search

    def flaky(request):
        calls["n"] += 1
        if calls["n"] > 5:
            return httpx.Response(429, json={"error": {"status": 429, "message": "quota", "reason": "QUOTA_EXCEEDED"}})
        return good(request)

    sp.search.mock(side_effect=flaky)
    resp = _build(logged_in, "busy day")
    assert resp.status_code == 200
    assert "slow down" in resp.text
    assert sp.create.call_count == 0
    assert _playlist_rows() == []


def test_mood_is_cleaned_before_use(logged_in, respx_mock):
    chat = respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(10)))
    sp = FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(10)})
    _build(logged_in, "  rainy\x00   sunday  ")
    assert "Mood: rainy sunday\n" in json.loads(chat.calls.last.request.content)["messages"][-1]["content"]
    assert json.loads(sp.create.calls.last.request.content)["name"] == "Moodify: rainy sunday"
    assert _playlist_rows()[0]["prompt"] == "rainy sunday"


def test_playlist_requires_login_and_onboarding(respx_mock, test_settings):
    init_db()
    client = TestClient(app, follow_redirects=False)
    assert client.post("/playlist", data={"mood": "x"}).headers["location"] == "/login"


def test_empty_mood_is_422(logged_in):
    assert logged_in.post("/playlist", data={"mood": ""}).status_code == 422


def test_home_lists_recent_playlists(logged_in, respx_mock):
    FakeSpotify(respx_mock, {})
    for i in range(7):
        playlists.insert("friend1", f"mood {i}", f"https://open.spotify.com/playlist/p{i}", [])
    resp = logged_in.get("/")
    assert resp.status_code == 200
    assert "mood 6" in resp.text and "mood 2" in resp.text and "mood 1" not in resp.text
    assert 'action="/playlist"' in resp.text


# --- queue --------------------------------------------------------------------------------

def test_queue_adds_every_track(logged_in, respx_mock):
    FakeSpotify(respx_mock, {})
    row_id = playlists.insert("friend1", "m", "https://open.spotify.com/playlist/p", tracks("spotify:track:a", "spotify:track:b"))
    queue = respx_mock.post(f"{API_URL}/me/player/queue").respond(204)
    resp = logged_in.post(f"/queue/{row_id}")
    assert resp.status_code == 200 and "Queued" in resp.text
    assert [c.request.url.params["uri"] for c in queue.calls] == ["spotify:track:a", "spotify:track:b"]


@pytest.mark.parametrize("status", [403, 404])
def test_queue_without_premium_or_device(logged_in, respx_mock, status):
    FakeSpotify(respx_mock, {})
    row_id = playlists.insert("friend1", "m", "u", tracks("spotify:track:a"))
    respx_mock.post(f"{API_URL}/me/player/queue").respond(status, json={"error": {"status": status, "message": "x"}})
    resp = logged_in.post(f"/queue/{row_id}")
    assert resp.status_code == 200 and "Premium" in resp.text


def test_queue_someone_elses_playlist_is_404(logged_in, respx_mock):
    users.upsert("friend2", "Two", "r2")
    row_id = playlists.insert("friend2", "m", "u", tracks("spotify:track:a"))
    assert logged_in.post(f"/queue/{row_id}").status_code == 404
    assert logged_in.post("/queue/999").status_code == 404


# --- live ---------------------------------------------------------------------------------------

@pytest.mark.live
def test_live_full_run_creates_a_real_playlist():
    from src import playlist as pl
    with get_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE onboarding_json IS NOT NULL ORDER BY created_at LIMIT 1").fetchone()
    if user is None:
        pytest.skip("no onboarded user in data/moodify.db: log in and complete /onboarding first")
    row_id = pl.build(user, "rainy sunday, nostalgic")
    made = playlists.tracks(row_id)
    print(f"\ncreated {playlists.get(row_id, user['spotify_id'])['spotify_url']} with {len(made)} tracks (delete it by hand afterwards)")
    assert len(made) >= 15, [(t.name, t.artists) for t in made]
