"""Phase 10: more of the user's Spotify (history, Liked Songs, follows, playlists), re-consent, known songs."""
import json

import pytest
from fastapi.testclient import TestClient

from src import jobs, playlists, taste, users
from src.db import get_conn, init_db
from src.main import app
from src.spotify import auth
from src.spotify.client import API_URL, SpotifyClient, Track
from src.suggest import user_prompt

from .conftest import finish_build, mock_listening, track_json
from .test_p6_playlist import CHAT_URL, FakeSpotify, _build, _completion, logged_in, user  # noqa: F401
from .test_p4_taste import ONBOARDING


def _t(name, artist="Someone", uri=None):
    return Track(uri or f"spotify:track:{name.replace(' ', '')}", name, (artist,), 200_000)


LIKED = [_t(f"Liked {i}", f"Liker {i % 7}") for i in range(12)]
RECENT = [_t(f"Recent {i}", f"Player {i}") for i in range(5)]


@pytest.fixture
def sp(test_settings, respx_mock):
    init_db()
    users.upsert("friend1", "Friend One", "refresh-1")
    respx_mock.post(auth.TOKEN_URL).respond(200, json={"access_token": "a", "expires_in": 3600})
    with SpotifyClient("friend1", "refresh-1") as client:
        yield client


# --- client reads, Feb-2026 shapes ---------------------------------------------------------------

def test_recently_played_skips_episodes_and_removed_tracks(sp, respx_mock):
    respx_mock.get(f"{API_URL}/me/player/recently-played").respond(200, json={"items": [
        {"track": track_json(RECENT[0]), "played_at": "x"},
        {"track": {**track_json(RECENT[1]), "type": "episode"}, "played_at": "x"},
        {"track": None, "played_at": "x"},
    ]})
    assert sp.recently_played() == [RECENT[0]]


@pytest.mark.parametrize("liked, requests, returned", [(230, 4, 200), (120, 3, 120), (0, 1, 0)])
def test_saved_tracks_pages_until_max_or_the_end(sp, respx_mock, liked, requests, returned):
    routes = mock_listening(respx_mock, liked=[_t(f"L{i}") for i in range(liked)])
    found = sp.saved_tracks(max_tracks=200)
    assert len(found) == returned and routes["liked"].call_count == requests
    assert [int(c.request.url.params["offset"]) for c in routes["liked"].calls] == [50 * i for i in range(requests)]


def test_followed_artists_and_playlists(sp, respx_mock):
    mock_listening(respx_mock, followed=["Bon Iver", "SZA"],
                   own_playlists=[("p1", "gym", "friend1"), ("p2", "Today's Top Hits", "spotify")])
    assert sp.followed_artists() == ["Bon Iver", "SZA"]
    assert [(p.id, p.name, p.readable) for p in sp.my_playlists()] == [("p1", "gym", True), ("p2", "Today's Top Hits", False)]


def test_playlist_items_reads_the_item_wrapper_and_skips_local_files(sp, respx_mock):
    respx_mock.get(f"{API_URL}/playlists/p1/items").respond(200, json={"items": [
        {"is_local": False, "item": track_json(LIKED[0])},
        {"is_local": True, "item": track_json(LIKED[1])},
        {"is_local": False, "item": None},
    ], "next": None})
    assert sp.playlist_items("p1") == [LIKED[0]]


def test_playlist_items_of_a_followed_only_playlist_is_empty_not_an_error(sp, respx_mock):
    respx_mock.get(f"{API_URL}/playlists/p2/items").respond(403, json={"error": {"status": 403, "message": "Forbidden"}})
    assert sp.playlist_items("p2") == []


# --- re-consent -------------------------------------------------------------------------------------

def test_login_stores_granted_scopes(logged_in):
    assert auth.missing_scopes(users.get("friend1")["granted_scopes"]) == set()


def test_user_without_the_new_scopes_is_asked_to_log_in_again(logged_in):
    with get_conn() as conn:  # as if they logged in before Phase 10
        conn.execute("UPDATE users SET granted_scopes = ? WHERE user_id = 'friend1'",
                     ("user-top-read user-read-private playlist-modify-public playlist-modify-private user-modify-playback-state",))
    resp = logged_in.get("/")
    assert resp.status_code == 303 and resp.headers["location"] == "/login?reason=scopes"
    page = logged_in.get("/login?reason=scopes")
    assert "Please log in again" in page.text and "Nothing you saved is lost" in page.text
    assert users.get("friend1")["onboarding_json"]  # data kept
    assert logged_in.get("/").headers["location"] == "/login"  # session was cleared


def test_disconnect_works_without_the_new_scopes(logged_in):
    with get_conn() as conn:
        conn.execute("UPDATE users SET granted_scopes = NULL WHERE user_id = 'friend1'")
    assert logged_in.post("/disconnect").status_code == 200
    assert users.get("friend1") is None


def test_login_page_without_reason_has_no_notice(test_settings):
    init_db()
    page = TestClient(app).get("/login")
    assert "Please log in again" not in page.text
    assert "liked songs" in page.text and "OpenAI" in page.text  # the data list names the new data


# --- summary and pool ------------------------------------------------------------------------------

def _full_listening():
    return taste.Listening(
        top_artists=["Drake", "Travis Scott"], all_time_artists=["Drake", "Kanye West"],
        top_tracks=[_t("E85", "Don Toliver")], recent_tracks=RECENT, liked=LIKED,
        followed=["Bon Iver"], playlist_names=["gym", "late night"], playlist_tracks=[_t("Playlist Song", "SZA")],
    )


def test_summary_mentions_every_source():
    text = taste.build_summary(ONBOARDING, _full_listening())
    for expected in ("Most played artists lately: Drake, Travis Scott", "All-time favourite artists: Kanye West",
                     "E85 by Don Toliver", "Recent 0 by Player 0", "Liked 0 by Liker 0", "Follows: Bon Iver",
                     "Their own playlists: gym, late night"):
        assert expected in text, expected
    assert text.count("Drake") == 1  # all-time list leaves out artists already named as recent favourites
    assert len(text) <= taste.MAX_CHARS


def test_summary_trims_the_least_telling_lists_first():
    listening = _full_listening()
    listening.playlist_names = [f"A very long playlist name number {i} " * 3 for i in range(10)]
    listening.followed = [f"An artist with a long long name {i} " * 3 for i in range(15)]
    listening.liked = [_t(f"A liked song with a very long title {i} " * 3, "X") for i in range(15)]
    text = taste.build_summary({k: "y" * 200 for k in ("genres", "artists", "avoid", "languages")}, listening)
    assert len(text) <= taste.MAX_CHARS
    assert "Their own playlists" not in text  # trimmed first
    assert "Most played artists lately: Drake" in text and "E85 by Don Toliver" in text  # most telling survive
    assert "y" * 200 in text


def test_pool_is_deduped_most_telling_first_and_capped():
    listening = taste.Listening(top_tracks=[LIKED[3]], recent_tracks=[RECENT[0], LIKED[3]],
                                liked=[_t(f"P{i}") for i in range(300)])
    pool = taste.known_pool(listening)
    assert pool[:2] == [LIKED[3], RECENT[0]] and len(pool) == taste.POOL_SIZE
    assert len({t.uri for t in pool}) == len(pool)


def test_profile_reads_every_source_caches_it_and_skips_moodify_playlists(user, respx_mock):
    respx_mock.post(auth.TOKEN_URL).respond(200, json={"access_token": "a", "expires_in": 3600})
    top = respx_mock.get(f"{API_URL}/me/top/artists").respond(200, json={"items": [{"name": "Drake"}]})
    respx_mock.get(f"{API_URL}/me/top/tracks").respond(200, json={"items": [track_json(_t("E85", "Don Toliver"))]})
    routes = mock_listening(respx_mock, recent=RECENT, liked=LIKED, followed=["Bon Iver"],
                            own_playlists=[("p1", "gym", "friend1"), ("p2", "Moodify: rainy", "friend1"),
                                           ("p3", "Top Hits", "spotify")],
                            playlist_items={"p1": [_t("Gym Song", "Gunna")]})

    profile = taste.profile_for(users.get("friend1"))

    assert "Bon Iver" in profile.summary and "gym" in profile.summary and "Moodify:" not in profile.summary
    assert [c.request.url.path for c in routes["items"].calls] == ["/v1/playlists/p1/items"]  # not followed-only, not Moodify's
    assert {"E85", "Recent 0", "Liked 0", "Gym Song"} <= {t.name for t in profile.pool}
    row = users.get("friend1")
    assert row["taste_summary"] == profile.summary
    from src import library
    assert library.pool("friend1") == profile.pool  # Phase 19: the pool is kept in the library
    # Cached: a second read makes no Spotify calls and gives the same pool
    calls = top.call_count
    again = taste.profile_for(users.get("friend1"))
    assert again == profile and top.call_count == calls


def test_a_failing_source_is_left_out_and_nothing_is_cached(user, respx_mock):
    respx_mock.post(auth.TOKEN_URL).respond(200, json={"access_token": "a", "expires_in": 3600})
    respx_mock.get(f"{API_URL}/me/top/artists").respond(200, json={"items": [{"name": "Drake"}]})
    respx_mock.get(f"{API_URL}/me/top/tracks").respond(200, json={"items": []})
    mock_listening(respx_mock, liked=LIKED, fail=("liked",))
    profile = taste.profile_for(users.get("friend1"))
    assert "Drake" in profile.summary and "Liked 0" not in profile.summary
    assert users.get("friend1")["taste_summary"] is None  # tried again next time


def test_summary_cached_before_phase_19_is_rebuilt(user, respx_mock):
    # Fresh, but cached before the library existed: migration 11 clears taste_updated_at, so it's rebuilt
    users.set_taste_summary("friend1", "phase 4 summary", taste.datetime.now(taste.timezone.utc).strftime(taste.TIMESTAMP))
    with get_conn() as conn:
        conn.execute("UPDATE users SET taste_updated_at = NULL")
    respx_mock.post(auth.TOKEN_URL).respond(200, json={"access_token": "a", "expires_in": 3600})
    respx_mock.get(f"{API_URL}/me/top/artists").respond(200, json={"items": [{"name": "Drake"}]})
    respx_mock.get(f"{API_URL}/me/top/tracks").respond(200, json={"items": []})
    mock_listening(respx_mock, liked=LIKED)
    profile = taste.profile_for(users.get("friend1"))
    assert profile.summary != "phase 4 summary" and LIKED[0] in profile.pool


def test_new_account_with_no_listening_still_gets_a_summary(user, respx_mock):
    respx_mock.post(auth.TOKEN_URL).respond(200, json={"access_token": "a", "expires_in": 3600})
    respx_mock.get(f"{API_URL}/me/top/artists").respond(200, json={"items": []})
    respx_mock.get(f"{API_URL}/me/top/tracks").respond(200, json={"items": []})
    mock_listening(respx_mock)
    profile = taste.profile_for(users.get("friend1"))
    assert ONBOARDING["genres"] in profile.summary and profile.pool == []


# --- known songs in the prompt and the build ---------------------------------------------------------

def test_prompt_lists_known_songs_per_user_part_first():
    per_user, per_request = user_prompt("rainy sunday", "Likes rap.", 30, [], LIKED[:3], 30)
    assert "1. Liked 0 by Liker 0\n2. Liked 1 by Liker 1\n3. Liked 2 by Liker 2" in per_user
    assert "about 30%" in per_request  # the share can change per prompt (Phase 11), so it's per request
    assert per_user.index("Listener's taste") < per_user.index("already knows")
    assert per_request.startswith("Mood: rainy sunday")


@pytest.mark.parametrize("answer, percent", [("new", 10), ("mix", 30), ("known", 60), (None, 30)])
def test_familiarity_answer_sets_the_share(answer, percent):
    assert taste.familiar_percent({"familiar": answer} if answer else {}) == percent


def test_known_picks_skip_search_and_count_as_known(logged_in, respx_mock):
    # 4 picked from the pool by number (pool order: liked songs), 8 found by search
    songs = [{"title": f"Liked {i}", "artist": f"Liker {i % 7}", "known": i + 1} for i in range(4)]
    songs += [{"title": f"Song {i}", "artist": f"Artist {i}", "known": None} for i in range(8)]
    chat = respx_mock.post(CHAT_URL).respond(200, json=_completion(songs))
    sp = FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(8)}, liked=LIKED)

    post = logged_in.post("/playlist", data={"mood": "familiar vibes"})
    finish_build(logged_in, post)

    assert sp.search.call_count == 8  # none for the known picks
    assert sp.added_uris()[:4] == [t.uri for t in LIKED[:4]]
    built = [e for e in jobs.events(int(post.headers["location"].rsplit("/", 1)[1])) if e["kind"] == "built"][0]
    assert built["known"] == 4 and built["tracks"] == 12
    assert "Liked 0 by Liker 0" in json.loads(chat.calls.last.request.content)["messages"][0]["content"]


def test_a_wrong_known_number_falls_back_to_search(logged_in, respx_mock):
    # The model says "known 1" (Liked 0) but names a different song: it must be searched, not swapped in
    songs = [{"title": "Song 0", "artist": "Artist 0", "known": 1}] + [
        {"title": f"Song {i}", "artist": f"Artist {i}", "known": None} for i in range(1, 9)]
    respx_mock.post(CHAT_URL).respond(200, json=_completion(songs))
    sp = FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(9)}, liked=LIKED)
    _build(logged_in, "x")
    assert sp.search.call_count == 9
    assert LIKED[0].uri not in sp.added_uris() and "spotify:track:t0" in sp.added_uris()


def test_out_of_range_known_number_is_searched(logged_in, respx_mock):
    songs = [{"title": f"Song {i}", "artist": f"Artist {i}", "known": 999 if i == 0 else None} for i in range(9)]
    respx_mock.post(CHAT_URL).respond(200, json=_completion(songs))
    sp = FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(9)}, liked=LIKED)
    _build(logged_in, "x")
    assert sp.search.call_count == 9 and len(sp.added_uris()) == 9


# --- several artists in one field (the live run of 2026-10-23 missed 11 of 25 this way) ----------------

@pytest.mark.parametrize("artist, track_artists, matches", [
    ("Masego, FKJ", ("Masego", "FKJ"), True),
    ("Masego, FKJ", ("FKJ",), True),
    ("Calvin Harris, Frank Ocean, Migos", ("Calvin Harris", "Frank Ocean", "Migos"), True),
    ("Drake, Tems", ("Drake", "Tems"), True),
    ("Tyler, The Creator", ("Tyler, The Creator",), True),  # one name with a comma
    ("Earth, Wind & Fire", ("Earth, Wind & Fire",), True),
    ("Masego, FKJ", ("ABBA",), False),
])
def test_pick_accepts_any_listed_artist(artist, track_artists, matches):
    from src.match import pick
    from src.suggest import Song
    found = pick(Song(title="Tadow", artist=artist), [Track("spotify:track:x", "Tadow", track_artists, 1)])
    assert (found == "spotify:track:x") is matches


@pytest.mark.parametrize("artist, query", [
    ("Masego, FKJ", "Masego"), ("Drake feat. Rihanna", "Drake"), ("Queen", "Queen"), ("Simon & Garfunkel", "Simon & Garfunkel"),
])
def test_search_uses_the_first_listed_artist(artist, query):
    from src.match import primary_artist
    assert primary_artist(artist) == query


def test_build_finds_songs_named_with_several_artists(logged_in, respx_mock):
    songs = [{"title": f"Song {i}", "artist": f"Artist {i}, Guest {i}", "known": None} for i in range(10)]
    respx_mock.post(CHAT_URL).respond(200, json=_completion(songs))
    sp = FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(10)})
    _build(logged_in, "x")
    assert all(c.request.url.params["q"].endswith(f"artist:Artist {i}") for i, c in enumerate(sp.search.calls))
    assert len(sp.added_uris()) == 10


def test_known_list_shows_only_the_main_artist():
    per_user, _ = user_prompt("x", "y", 30, [], [Track("spotify:track:a", "Slide", ("Calvin Harris", "Frank Ocean", "Migos"), 1)], 30)
    assert per_user.endswith("1. Slide by Calvin Harris") and "Frank Ocean" not in per_user


# --- live ------------------------------------------------------------------------------------------

def _live_user():
    with get_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE onboarding_json IS NOT NULL ORDER BY created_at LIMIT 1").fetchone()
    if user is None:
        pytest.skip("no onboarded user in data/moodify.db: log in and complete /onboarding first")
    if auth.missing_scopes(user["granted_scopes"]):
        pytest.skip("log in again first: this account hasn't granted the Phase 10 scopes")
    return user


@pytest.mark.live
def test_live_listening_reads_history_and_liked_songs():
    user = _live_user()
    listening = taste.read_listening(user)
    print(f"\nrecent {len(listening.recent_tracks)}, liked {len(listening.liked)}, followed {len(listening.followed)},"
          f" playlists {len(listening.playlist_names)}, playlist tracks {len(listening.playlist_tracks)}")
    assert listening.complete
    assert listening.recent_tracks and listening.liked
    summary = taste.build_summary(json.loads(user["onboarding_json"]), listening)
    assert listening.recent_tracks[0].artists[0] in summary or listening.liked[0].artists[0] in summary


@pytest.mark.live
def test_live_playlist_has_about_a_third_known_songs():
    """Phase 10 gate. Creates a real playlist."""
    user = _live_user()
    from src.playlist import run
    job_id = jobs.create(user["user_id"], "chill evening, winding down after a long day")
    run(job_id, user["user_id"], "chill evening, winding down after a long day")
    job = jobs.get(job_id, user["user_id"])
    assert job["status"] == "done", job["error"]
    built = [e for e in jobs.events(job_id) if e["kind"] == "built"][0]
    print(f"\n{built}\n{playlists.get(job['playlist_id'], user['user_id'])['url']}")
    assert 0.15 <= built["known"] / built["tracks"] <= 0.5
