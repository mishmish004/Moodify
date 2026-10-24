"""Phase 14: likes per track (mirrored to Spotify's Liked Songs) and a 1-10 rating per playlist."""
import sqlite3

import httpx
import pytest

from src import likes, playlists, users
from src.db import get_conn, init_db
from src.spotify import auth
from src.spotify.client import API_URL, SpotifyClient, Track

from .test_p6_playlist import FakeSpotify, logged_in, user  # noqa: F401
from .test_p9_jobs import V0_SCHEMA


def _playlist(owner="friend1", n=3, uris=None) -> int:
    uris = uris or [f"spotify:track:{owner}-{i}" for i in range(n)]
    tracks = [Track(uri, f"Song {i}", ("Artist",), 210_000) for i, uri in enumerate(uris)]
    return playlists.insert(owner, "rainy day", f"https://open.spotify.com/playlist/{owner}", tracks)


class Library:
    """respx routes for /me/library. `saved` holds what's in the user's Liked Songs."""

    def __init__(self, respx_mock, saved=(), status=200):
        respx_mock.post(auth.TOKEN_URL).respond(200, json={"access_token": "a", "expires_in": 3600})
        self.saved, self.status = set(saved), status
        self.contains = respx_mock.get(f"{API_URL}/me/library/contains").mock(side_effect=self._contains)
        self.put = respx_mock.put(f"{API_URL}/me/library").mock(side_effect=self._write(self.saved.add))
        self.delete = respx_mock.delete(f"{API_URL}/me/library").mock(side_effect=self._write(self.saved.discard))

    def _contains(self, request):
        return httpx.Response(200, json=[u in self.saved for u in request.url.params["uris"].split(",")])

    def _write(self, change):
        def answer(request):
            if self.status != 200:
                return httpx.Response(self.status, headers={"Retry-After": "0"},
                                      json={"error": {"status": self.status, "message": "nope"}})
            for uri in request.url.params["uris"].split(","):
                change(uri)
            return httpx.Response(200)
        return answer

    def calls(self) -> int:
        return self.contains.call_count + self.put.call_count + self.delete.call_count


def _like_rows():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT spotify_id, uri, playlist_id, added_to_spotify FROM track_likes")]


def _rating(playlist_id):
    with get_conn() as conn:
        return conn.execute("SELECT rating FROM playlists WHERE id = ?", (playlist_id,)).fetchone()[0]


JSON = {"Accept": "application/json"}


# --- likes ------------------------------------------------------------------------------------------------

def test_like_saves_to_liked_songs(logged_in, respx_mock):
    pid = _playlist()
    library = Library(respx_mock)
    resp = logged_in.post(f"/playlists/{pid}/tracks/1/like")
    assert resp.status_code == 303 and resp.headers["location"] == f"/playlists/{pid}#t1"
    assert _like_rows() == [{"spotify_id": "friend1", "uri": "spotify:track:friend1-1", "playlist_id": pid, "added_to_spotify": 1}]
    assert library.put.calls.last.request.url.params["uris"] == "spotify:track:friend1-1"
    assert library.saved == {"spotify:track:friend1-1"}

    page = logged_in.get(f"/playlists/{pid}").text
    assert f'action="/playlists/{pid}/tracks/1/unlike"' in page        # the row now offers unlike
    assert f'action="/playlists/{pid}/tracks/0/like"' in page
    assert page.count('aria-pressed="true" aria-label="Like') == 1


def test_liking_twice_stores_one_like_and_calls_spotify_once(logged_in, respx_mock):
    pid = _playlist()
    library = Library(respx_mock)
    logged_in.post(f"/playlists/{pid}/tracks/0/like")
    logged_in.post(f"/playlists/{pid}/tracks/0/like")
    assert len(_like_rows()) == 1 and library.put.call_count == 1


def test_setting_off_keeps_likes_in_moodify(logged_in, respx_mock):
    pid = _playlist()
    assert logged_in.post("/settings", data={}).status_code == 303  # an unticked checkbox sends nothing
    assert users.get("friend1")["likes_to_spotify"] == 0
    library = Library(respx_mock, saved={"spotify:track:friend1-0"})
    logged_in.post(f"/playlists/{pid}/tracks/0/like")
    logged_in.post(f"/playlists/{pid}/tracks/1/like")
    assert len(_like_rows()) == 2 and library.calls() == 0
    logged_in.post(f"/playlists/{pid}/tracks/0/unlike")
    assert library.calls() == 0 and library.saved == {"spotify:track:friend1-0"}
    page = logged_in.get(f"/playlists/{pid}").text
    assert "They stay in Moodify" in page and "Spotify Liked Songs too" not in page


def test_unlike_removes_from_liked_songs_only_what_moodify_added(logged_in, respx_mock):
    pid = _playlist()
    library = Library(respx_mock, saved={"spotify:track:friend1-0"})  # liked in Spotify before Moodify
    logged_in.post(f"/playlists/{pid}/tracks/0/like")
    logged_in.post(f"/playlists/{pid}/tracks/1/like")
    assert library.put.call_count == 1  # only song 1 needed saving
    assert {r["uri"]: r["added_to_spotify"] for r in _like_rows()} == {"spotify:track:friend1-0": 0, "spotify:track:friend1-1": 1}

    logged_in.post(f"/playlists/{pid}/tracks/0/unlike")
    assert library.delete.call_count == 0                     # not Moodify's to remove
    logged_in.post(f"/playlists/{pid}/tracks/1/unlike")
    assert library.delete.calls.last.request.url.params["uris"] == "spotify:track:friend1-1"
    assert library.saved == {"spotify:track:friend1-0"} and _like_rows() == []


def test_unlike_after_turning_the_setting_off_still_undoes_moodifys_save(logged_in, respx_mock):
    pid = _playlist()
    library = Library(respx_mock)
    logged_in.post(f"/playlists/{pid}/tracks/0/like")
    logged_in.post("/settings", data={})
    logged_in.post(f"/playlists/{pid}/tracks/0/unlike")
    assert library.delete.call_count == 1 and library.saved == set()


def test_unliking_something_not_liked_does_nothing(logged_in, respx_mock):
    pid = _playlist()
    library = Library(respx_mock)
    assert logged_in.post(f"/playlists/{pid}/tracks/0/unlike").status_code == 303
    assert library.calls() == 0


@pytest.mark.parametrize("status", [403, 429, 500])
def test_spotify_refusing_keeps_the_like_and_says_so(logged_in, respx_mock, status):
    pid = _playlist()
    Library(respx_mock, status=status)
    resp = logged_in.post(f"/playlists/{pid}/tracks/0/like")
    assert resp.status_code == 303
    assert _like_rows()[0]["added_to_spotify"] == 0
    page = logged_in.get(resp.headers["location"].split("#")[0]).text
    assert "Liked in Moodify, but your Spotify Liked Songs couldn&#39;t be updated." in page
    assert "couldn&#39;t be updated" not in logged_in.get(f"/playlists/{pid}").text  # shown once


def test_spotify_unreachable_keeps_the_like(logged_in, respx_mock):
    pid = _playlist()
    respx_mock.post(auth.TOKEN_URL).respond(200, json={"access_token": "a", "expires_in": 3600})
    respx_mock.get(f"{API_URL}/me/library/contains").mock(side_effect=httpx.ConnectTimeout("slow"))
    resp = logged_in.post(f"/playlists/{pid}/tracks/0/like", headers=JSON)
    assert resp.json() == {"liked": True, "notice": likes.NOTICES[("like", likes.FAILED)]}
    assert len(_like_rows()) == 1


def test_failed_unlike_says_the_song_is_still_in_liked_songs(logged_in, respx_mock):
    pid = _playlist()
    library = Library(respx_mock)
    logged_in.post(f"/playlists/{pid}/tracks/0/like")
    library.status = 403
    resp = logged_in.post(f"/playlists/{pid}/tracks/0/unlike", headers=JSON)
    assert resp.json() == {"liked": False, "notice": "Unliked in Moodify, but the song is still in your Spotify Liked Songs."}
    assert _like_rows() == []


def test_a_like_is_per_song_across_playlists(logged_in, respx_mock):
    Library(respx_mock)
    shared = "spotify:track:shared"
    first, second = _playlist(uris=[shared, "spotify:track:a"]), _playlist(uris=["spotify:track:b", shared])
    logged_in.post(f"/playlists/{first}/tracks/0/like")
    assert f'action="/playlists/{second}/tracks/1/unlike"' in logged_in.get(f"/playlists/{second}").text
    counts = {r["id"]: r["like_count"] for r in playlists.recent("friend1", limit=-1)}
    assert counts == {first: 1, second: 1}


def test_json_answers_for_the_script(logged_in, respx_mock):
    pid = _playlist()
    Library(respx_mock)
    assert logged_in.post(f"/playlists/{pid}/tracks/2/like", headers=JSON).json() == {"liked": True, "notice": None}
    assert logged_in.post(f"/playlists/{pid}/tracks/2/unlike", headers=JSON).json() == {"liked": False, "notice": None}
    assert logged_in.post(f"/playlists/{pid}/rating", data={"rating": "7"}, headers=JSON).json() == {"rating": 7, "notice": None}
    missing = logged_in.post(f"/playlists/{pid}/tracks/9/like", headers=JSON)
    assert missing.status_code == 404 and missing.json() == {"error": "not found"}


# --- ratings ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("rating", ["0", "11", "abc", "", "5.5"])
def test_bad_ratings_are_refused(logged_in, rating):
    pid = _playlist()
    assert logged_in.post(f"/playlists/{pid}/rating", data={"rating": rating}).status_code == 422
    assert _rating(pid) is None


def test_ratings_1_and_10_and_rerating(logged_in):
    pid = _playlist()
    resp = logged_in.post(f"/playlists/{pid}/rating", data={"rating": "1"})
    assert resp.status_code == 303 and resp.headers["location"] == f"/playlists/{pid}#rating"
    assert _rating(pid) == 1
    logged_in.post(f"/playlists/{pid}/rating", data={"rating": "10"})
    assert _rating(pid) == 10
    page = logged_in.get(f"/playlists/{pid}").text
    assert page.count('aria-pressed="true">10</button>') == 1 and 'aria-pressed="true">1</button>' not in page


def test_the_database_refuses_out_of_range_ratings(user):
    pid = _playlist()
    with pytest.raises(sqlite3.IntegrityError), get_conn() as conn:
        conn.execute("UPDATE playlists SET rating = 11 WHERE id = ?", (pid,))


# --- owner only -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path, data", [
    ("/tracks/0/like", {}), ("/tracks/0/unlike", {}), ("/rating", {"rating": "8"}),
])
def test_another_users_playlist_is_404_and_stores_nothing(logged_in, respx_mock, path, data):
    users.upsert("friend2", "Friend Two", "refresh-2", auth.SCOPES)
    theirs = _playlist(owner="friend2")
    with get_conn() as conn:
        conn.execute("INSERT INTO track_likes (spotify_id, uri, playlist_id) VALUES ('friend2', 'spotify:track:friend2-0', ?)",
                     (theirs,))
    library = Library(respx_mock)
    resp = logged_in.post(f"/playlists/{theirs}{path}", data=data)
    assert resp.status_code == 404
    assert _rating(theirs) is None and library.calls() == 0
    assert _like_rows() == [{"spotify_id": "friend2", "uri": "spotify:track:friend2-0", "playlist_id": theirs, "added_to_spotify": 0}]


@pytest.mark.parametrize("path", ["/playlists/999/tracks/0/like", "/playlists/{pid}/tracks/3/like", "/playlists/{pid}/tracks/-1/like",
                                  "/playlists/999/rating"])
def test_missing_playlist_or_track_is_404(logged_in, path):
    pid = _playlist()
    assert logged_in.post(path.format(pid=pid), data={"rating": "5"}).status_code == 404
    assert _like_rows() == []


def test_unknown_action_is_refused(logged_in):
    pid = _playlist()
    assert logged_in.post(f"/playlists/{pid}/tracks/0/love").status_code == 422


def test_logged_out_goes_to_login(test_settings):
    from fastapi.testclient import TestClient
    from src.main import app
    init_db()
    with TestClient(app, follow_redirects=False) as client:
        assert client.post("/playlists/1/tracks/0/like").headers["location"] == "/login"
        assert client.post("/playlists/1/rating", data={"rating": "5"}).headers["location"] == "/login"


# --- pages ---------------------------------------------------------------------------------------------------

def test_playlist_page_works_without_javascript(logged_in):
    pid = _playlist()
    page = logged_in.get(f"/playlists/{pid}").text
    # Plain forms that post and redirect: the script only enhances them
    assert f'<form class="mf-like-form" method="post" action="/playlists/{pid}/tracks/0/like" data-feedback="like">' in page
    assert f'<form id="rating" class="mf-rate" method="post" action="/playlists/{pid}/rating" data-feedback="rating">' in page
    for n in range(1, 11):
        assert f'name="rating" value="{n}"' in page
    assert '<script src="/static/feedback.js"></script>' in page
    assert '<p id="toast" class="mf-toast" role="status"></p>' in page  # empty: hidden by CSS
    assert logged_in.get("/static/feedback.js").status_code == 200


def test_history_shows_ratings_and_likes_and_sorts_by_rating(logged_in, respx_mock):
    Library(respx_mock)
    low, unrated, high = (_playlist(uris=[f"spotify:track:{name}{i}" for i in range(3)]) for name in ("low", "unrated", "high"))
    logged_in.post(f"/playlists/{low}/rating", data={"rating": "3"})
    logged_in.post(f"/playlists/{high}/rating", data={"rating": "9"})
    logged_in.post(f"/playlists/{high}/tracks/0/like")
    logged_in.post(f"/playlists/{high}/tracks/1/like")

    page = logged_in.get("/history").text
    assert "3 tracks · 2 liked · rated 9/10</span>" in page
    assert "3 tracks · rated 3/10</span>" in page and "3 tracks</span>" in page
    by_rating = [r["id"] for r in playlists.recent("friend1", limit=-1, order="rating")]
    assert by_rating == [high, low, unrated]
    assert [r["id"] for r in playlists.recent("friend1", limit=-1)] == [high, unrated, low]
    sorted_page = logged_in.get("/history?sort=rating").text
    assert sorted_page.index("rated 9/10") < sorted_page.index("rated 3/10")
    assert 'href="/history?sort=rating" aria-current="page"' in sorted_page
    assert logged_in.get("/history?sort=plays").status_code == 422


def test_settings_page(logged_in, respx_mock):
    page = logged_in.get("/settings").text
    assert 'name="likes_to_spotify" value="on" checked' in page
    resp = logged_in.post("/settings", data={})
    assert resp.headers["location"] == "/settings"
    page = logged_in.get("/settings").text
    assert 'value="on" >' in page and "checked" not in page and '<p id="toast" class="mf-toast" role="status">Saved.</p>' in page
    logged_in.post("/settings", data={"likes_to_spotify": "on"})
    assert users.get("friend1")["likes_to_spotify"] == 1
    FakeSpotify(respx_mock, {})  # the home page shows the taste summary, built from Spotify
    assert 'href="/settings"' in logged_in.get("/").text


def test_login_page_lists_likes_and_ratings(test_settings):
    from fastapi.testclient import TestClient
    from src.main import app
    init_db()
    with TestClient(app) as client:
        page = client.get("/login").text
    assert "the songs you like and the 1–10 ratings you give" in page
    assert "you can turn this off in Settings" in page


# --- storage ---------------------------------------------------------------------------------------------------

def test_disconnect_deletes_likes_and_ratings(logged_in, respx_mock):
    pid = _playlist()
    Library(respx_mock)
    logged_in.post(f"/playlists/{pid}/tracks/0/like")
    logged_in.post(f"/playlists/{pid}/rating", data={"rating": "8"})
    logged_in.post("/disconnect")
    with get_conn() as conn:
        assert conn.execute("SELECT count(*) FROM track_likes").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM playlists").fetchone()[0] == 0


def test_deleting_a_playlist_deletes_the_likes_made_there(user):
    pid = _playlist()
    with get_conn() as conn:
        conn.execute("INSERT INTO track_likes (spotify_id, uri, playlist_id) VALUES ('friend1', 'spotify:track:friend1-0', ?)", (pid,))
        conn.execute("DELETE FROM playlists WHERE id = ?", (pid,))
        assert conn.execute("SELECT count(*) FROM track_likes").fetchone()[0] == 0


def test_existing_users_get_likes_to_spotify_on_after_migrating(test_settings):
    conn = sqlite3.connect(test_settings.DB_PATH)
    conn.executescript(V0_SCHEMA)
    conn.execute("INSERT INTO users (spotify_id, refresh_token) VALUES ('old', 'r')")
    conn.commit()
    conn.close()
    init_db()
    assert users.get("old")["likes_to_spotify"] == 1


def test_library_calls_send_comma_separated_uris(user, respx_mock):
    respx_mock.post(auth.TOKEN_URL).respond(200, json={"access_token": "a", "expires_in": 3600})
    route = respx_mock.get(f"{API_URL}/me/library/contains").respond(200, json=[True, False])
    with SpotifyClient("friend1", "refresh-1") as sp:
        assert sp.library_contains(["spotify:track:a", "spotify:track:b"]) == [True, False]
    assert route.calls.last.request.url.params["uris"] == "spotify:track:a,spotify:track:b"


# --- live ------------------------------------------------------------------------------------------------------

@pytest.mark.live
def test_live_like_reaches_liked_songs_and_unlike_takes_it_back():
    """Gate. Likes a song from your newest playlist that isn't in your Liked Songs yet, checks Spotify has it,
    then unlikes it and checks it's gone again. Your Liked Songs end up as they were."""
    init_db()
    with get_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE onboarding_json IS NOT NULL ORDER BY created_at LIMIT 1").fetchone()
        if user is None or auth.missing_scopes(user["granted_scopes"]):
            pytest.skip("needs an onboarded user who logged in after Phase 10")
        rows = conn.execute(
            "SELECT t.playlist_id, t.uri, t.name FROM playlist_tracks t JOIN playlists p ON p.id = t.playlist_id"
            " WHERE p.spotify_id = ? AND t.uri NOT IN (SELECT uri FROM track_likes WHERE spotify_id = ?)"
            " ORDER BY p.id DESC, t.position LIMIT 40",
            (user["spotify_id"], user["spotify_id"]),
        ).fetchall()
    if not rows:
        pytest.skip("needs a playlist made after Phase 9")
    with SpotifyClient.for_user(user) as sp:
        saved = sp.library_contains([r["uri"] for r in rows])
    pick = next((r for r, s in zip(rows, saved) if not s), None)
    if pick is None:
        pytest.skip("every song of the newest playlist is already in Liked Songs")
    user = users.get(user["spotify_id"])  # the refresh above may have rotated the token
    print(f"liking {pick['name']} ({pick['uri']})")

    assert likes.like(user, pick["uri"], pick["playlist_id"]) == likes.SAVED
    with SpotifyClient.for_user(users.get(user["spotify_id"])) as sp:
        assert sp.library_contains([pick["uri"]]) == [True]
    assert likes.unlike(users.get(user["spotify_id"]), pick["uri"]) == likes.REMOVED
    with SpotifyClient.for_user(users.get(user["spotify_id"])) as sp:
        assert sp.library_contains([pick["uri"]]) == [False]
