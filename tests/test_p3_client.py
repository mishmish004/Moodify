"""Phase 3: the Spotify client. Every call is respx-mocked except the one marked live."""
import json

import httpx
import pytest

from src import users
from src.core.exceptions import ReauthRequired, SpotifyError, SpotifyQuotaExceeded
from src.db import get_conn, init_db
from src.spotify import auth
from src.spotify.client import API_URL, Playlist, SpotifyClient, Track


@pytest.fixture
def user(test_settings):
    init_db()
    users.upsert("friend1", "Friend One", "refresh-1")
    return users.get("friend1")


@pytest.fixture
def sp(user):
    # Start with a valid-looking access token so tests only see a refresh when they cause one
    with SpotifyClient(user["user_id"], user["refresh_token"], access_token="access-0") as client:
        yield client


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr("src.spotify.client.time.sleep", calls.append)
    return calls


def _track_json(n: int, *artists: str) -> dict:
    # Feb 2026 track shape: no popularity, available_markets or linked_from
    return {
        "type": "track", "id": f"t{n}", "uri": f"spotify:track:t{n}", "name": f"Song {n}",
        "artists": [{"id": f"a{i}", "name": name, "uri": f"spotify:artist:a{i}"} for i, name in enumerate(artists or ("Queen",))],
        "duration_ms": 200_000 + n, "explicit": False, "is_local": False,
    }


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content)


# --- endpoints ------------------------------------------------------------------

def test_search_track_query_and_parsing(sp, respx_mock):
    route = respx_mock.get(f"{API_URL}/search").respond(200, json={
        "tracks": {"href": "...", "limit": 10, "offset": 0, "total": 2, "next": None, "previous": None,
                   "items": [_track_json(1, "Queen"), _track_json(2, "Queen", "David Bowie")]},
    })

    results = sp.search_track("Bohemian Rhapsody", "Queen")

    request = route.calls.last.request
    assert request.url.params["q"] == "track:Bohemian Rhapsody artist:Queen"
    assert request.url.params["type"] == "track"
    assert request.url.params["limit"] == "10"
    assert request.headers["authorization"] == "Bearer access-0"
    assert results == [
        Track("spotify:track:t1", "Song 1", ("Queen",), 200_001),
        Track("spotify:track:t2", "Song 2", ("Queen", "David Bowie"), 200_002),
    ]


def test_search_with_no_results(sp, respx_mock):
    respx_mock.get(f"{API_URL}/search").respond(200, json={"tracks": {"items": [], "total": 0}})
    assert sp.search_track("No Such Song", "Nobody") == []


def test_create_playlist_is_private(sp, respx_mock):
    route = respx_mock.post(f"{API_URL}/me/playlists").respond(201, json={
        "id": "p1", "name": "Moodify: rainy", "public": False, "snapshot_id": "s1",
        "external_urls": {"spotify": "https://open.spotify.com/playlist/p1"},
        "items": {"href": "...", "total": 0},  # Feb 2026: was "tracks"
    })

    playlist = sp.create_playlist("Moodify: rainy", "made by Moodify")

    assert _body(route.calls.last.request) == {"name": "Moodify: rainy", "description": "made by Moodify", "public": False}
    assert playlist == Playlist("p1", "https://open.spotify.com/playlist/p1")


def test_add_items_batches_by_100(sp, respx_mock):
    route = respx_mock.post(f"{API_URL}/playlists/p1/items").respond(201, json={"snapshot_id": "s2"})
    uris = [f"spotify:track:t{n}" for n in range(150)]

    sp.add_items("p1", uris)

    batches = [_body(call.request)["uris"] for call in route.calls]
    assert [len(b) for b in batches] == [100, 50]
    assert batches[0] + batches[1] == uris


def test_add_items_with_nothing_to_add_makes_no_request(sp, respx_mock):
    sp.add_items("p1", [])  # respx_mock fails any request it didn't expect


def test_queue(sp, respx_mock):
    route = respx_mock.post(f"{API_URL}/me/player/queue").respond(204)
    assert sp.queue("spotify:track:t1") is None
    assert route.calls.last.request.url.params["uri"] == "spotify:track:t1"


def test_queue_without_premium_or_device_raises(sp, respx_mock):
    respx_mock.post(f"{API_URL}/me/player/queue").respond(403, json={"error": {"status": 403, "message": "Premium required"}})
    with pytest.raises(SpotifyError) as exc:
        sp.queue("spotify:track:t1")
    assert exc.value.status == 403


def test_top_artists_and_tracks(sp, respx_mock):
    artists = respx_mock.get(f"{API_URL}/me/top/artists").respond(200, json={"items": [{"name": "Radiohead"}, {"name": "Björk"}]})
    tracks = respx_mock.get(f"{API_URL}/me/top/tracks").respond(200, json={"items": [_track_json(1, "Radiohead")]})

    assert sp.top_artists(limit=5) == ["Radiohead", "Björk"]
    assert sp.top_tracks(limit=3) == [Track("spotify:track:t1", "Song 1", ("Radiohead",), 200_001)]
    assert artists.calls.last.request.url.params["limit"] == "5"
    assert artists.calls.last.request.url.params["time_range"] == "medium_term"
    assert tracks.calls.last.request.url.params["limit"] == "3"


def test_me(sp, respx_mock):
    respx_mock.get(f"{API_URL}/me").respond(200, json={"id": "friend1", "display_name": "Friend One"})
    assert sp.me()["id"] == "friend1"


# --- tokens ----------------------------------------------------------------------

def test_first_call_gets_an_access_token(user, respx_mock):
    token = respx_mock.post(auth.TOKEN_URL).respond(200, json={"access_token": "access-1", "expires_in": 3600})
    me = respx_mock.get(f"{API_URL}/me").respond(200, json={"id": "friend1"})
    with SpotifyClient.for_user(user) as sp:
        sp.me()
    assert token.call_count == 1
    assert me.calls.last.request.headers["authorization"] == "Bearer access-1"


def test_401_refreshes_retries_and_persists_the_new_token(sp, respx_mock):
    token = respx_mock.post(auth.TOKEN_URL).respond(
        200, json={"access_token": "access-1", "refresh_token": "refresh-2", "expires_in": 3600}
    )
    me = respx_mock.get(f"{API_URL}/me").mock(side_effect=[
        httpx.Response(401, json={"error": {"status": 401, "message": "The access token expired"}}),
        httpx.Response(200, json={"id": "friend1"}),
        httpx.Response(200, json={"id": "friend1"}),
    ])

    assert sp.me()["id"] == "friend1"
    sp.me()  # later calls reuse the new token without refreshing again

    assert [c.request.headers["authorization"] for c in me.calls] == ["Bearer access-0", "Bearer access-1", "Bearer access-1"]
    assert token.call_count == 1
    assert users.get("friend1")["refresh_token"] == "refresh-2"


def test_401_after_refresh_gives_up(sp, respx_mock):
    token = respx_mock.post(auth.TOKEN_URL).respond(200, json={"access_token": "access-1", "expires_in": 3600})
    respx_mock.get(f"{API_URL}/me").respond(401, json={"error": {"status": 401, "message": "Invalid access token"}})
    with pytest.raises(SpotifyError) as exc:
        sp.me()
    assert exc.value.status == 401
    assert token.call_count == 1


def test_dead_refresh_token_raises_reauth_required(sp, respx_mock):
    respx_mock.get(f"{API_URL}/me").respond(401)
    respx_mock.post(auth.TOKEN_URL).respond(400, json={"error": "invalid_grant", "error_description": "Refresh token revoked"})
    with pytest.raises(ReauthRequired):
        sp.me()


# --- 429 and errors ----------------------------------------------------------------

def test_429_waits_retry_after_and_retries_once(sp, respx_mock, sleeps):
    route = respx_mock.get(f"{API_URL}/me").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "1"}),
        httpx.Response(200, json={"id": "friend1"}),
    ])
    assert sp.me()["id"] == "friend1"
    assert sleeps == [1.0]
    assert route.call_count == 2


def test_429_twice_raises_quota_exceeded(sp, respx_mock, sleeps):
    route = respx_mock.get(f"{API_URL}/me").respond(429, headers={"Retry-After": "2"})
    with pytest.raises(SpotifyQuotaExceeded):
        sp.me()
    assert sleeps == [2.0]
    assert route.call_count == 2


def test_quota_exceeded_raises_without_waiting(sp, respx_mock, sleeps):
    route = respx_mock.get(f"{API_URL}/search").respond(
        429, headers={"Retry-After": "1"},
        json={"error": {"status": 429, "message": "Quota exceeded", "reason": "QUOTA_EXCEEDED"}},
    )
    with pytest.raises(SpotifyQuotaExceeded):
        sp.search_track("Song", "Artist")
    assert sleeps == []
    assert route.call_count == 1


def test_long_retry_after_raises_without_waiting(sp, respx_mock, sleeps):
    respx_mock.get(f"{API_URL}/me").respond(429, headers={"Retry-After": "3600"})
    with pytest.raises(SpotifyQuotaExceeded):
        sp.me()
    assert sleeps == []


def test_missing_or_bad_retry_after_waits_one_second(sp, respx_mock, sleeps):
    respx_mock.get(f"{API_URL}/me").mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "soon"}),
        httpx.Response(200, json={"id": "friend1"}),
    ])
    sp.me()
    assert sleeps == [1.0]


@pytest.mark.parametrize("status", [400, 403, 404, 500, 503])
def test_other_errors_raise_spotify_error_with_status_and_body(sp, respx_mock, status):
    respx_mock.get(f"{API_URL}/me").respond(status, text=f"error body {status}")
    with pytest.raises(SpotifyError) as exc:
        sp.me()
    assert exc.value.status == status
    assert exc.value.body == f"error body {status}"
    assert not isinstance(exc.value, SpotifyQuotaExceeded)


def test_network_error_raises_spotify_error(sp, respx_mock):
    respx_mock.get(f"{API_URL}/me").mock(side_effect=httpx.ConnectError("connection refused"))
    with pytest.raises(SpotifyError) as exc:
        sp.me()
    assert exc.value.status == 0


# --- live ---------------------------------------------------------------------------

@pytest.mark.live
def test_live_search_finds_queen():
    with get_conn() as conn:
        user = conn.execute("SELECT * FROM users ORDER BY created_at LIMIT 1").fetchone()
    if user is None:
        pytest.skip("no user in data/moodify.db: log in once at /login first")
    with SpotifyClient.for_user(user) as sp:
        results = sp.search_track("Bohemian Rhapsody", "Queen")
    assert results, "search returned nothing"
    assert any("Queen" in track.artists for track in results), [(t.name, t.artists) for t in results]
