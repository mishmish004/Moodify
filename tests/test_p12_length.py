"""Phase 12: playlists as long as asked, filled per section with Spotify's real track lengths."""
import json

import httpx
import pytest

from src import jobs, playlists, search_cache, users
from src.brief import Brief, NO_RULES
from src.db import get_conn
from src.fill import fill, section_targets, songs_to_ask_for, window
from src.spotify.client import Track

from .conftest import CHAT_URL, default_brief, finish_build, track_json
from .test_p6_playlist import FakeSpotify, _build, _completion, _playlist_rows, logged_in, user  # noqa: F401


def _t(minutes, name="x"):
    return Track(f"spotify:track:{name}{minutes}", name, ("A",), int(minutes * 60_000))


def _brief(*minutes, total=None, **rules):
    sections = [{"label": f"Part {i}", "feeling": "x", "energy": 5, "minutes": m, "activity": None,
                 "transition_in": "blend"} for i, m in enumerate(minutes, start=1)]
    return default_brief("Test", sections=sections, total_minutes=total,
                         hard_rules={**default_brief("")["hard_rules"], **rules})


def _job_events(post):
    return jobs.events(int(post.headers["location"].rsplit("/", 1)[1]))


# --- pure parts ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("target, lengths, kept", [
    (30, [10, 10, 10], [10, 10, 10]),           # exact fit
    (30, [10, 10, 12], [10, 10, 12]),           # 32 is inside 27-33
    (30, [10, 10, 15, 8], [10, 10, 8]),         # 35 would overshoot: skip the 15, take the shorter 8
    (30, [4] * 20, [4] * 7),                    # stops once inside the window (28)
    (30, [], []),                               # no candidates
    (5, [7, 4], [7]),                           # short section: window 2-8, and 7 alone is inside it
    (5, [9, 4], [4]),                           # 9 passes the window: skipped
])
def test_fill(target, lengths, kept):
    tracks = [_t(m, f"s{i}-") for i, m in enumerate(lengths)]
    assert [round(t.duration_ms / 60_000) for t in fill(target, tracks, NO_RULES)] == kept


def test_fill_drops_songs_breaking_a_length_rule():
    short_only = NO_RULES.model_copy(update={"max_track_minutes": 3.0})
    assert fill(10, [_t(4, "a"), _t(5, "b")], short_only) == []
    assert [t.name for t in fill(6, [_t(4, "long"), _t(2.5, "ok1"), _t(2.9, "ok2")], short_only)] == ["ok1", "ok2"]
    long_only = NO_RULES.model_copy(update={"min_track_minutes": 6.0})
    assert [t.name for t in fill(14, [_t(3, "short"), _t(7, "a"), _t(7, "b")], long_only)] == ["a", "b"]


@pytest.mark.parametrize("target, low, high", [(60, 54, 66), (30, 27, 33), (10, 7, 13), (120, 108, 132)])
def test_window_is_ten_percent_or_three_minutes(target, low, high):
    assert window(target) == pytest.approx((low, high))


@pytest.mark.parametrize("minutes, total, targets", [
    ((None,), None, [60]),                       # nothing given: an hour
    ((None,), 45, [45]),                         # "45 minute commute"
    ((None, 30, None), None, [15, 30, 15]),      # the 2026-10-23 prompt: the rest of an hour is shared
    ((20, 40), None, [20, 40]),                  # every section stated
    ((None, None), 120, [60, 60]),
    ((50, None), None, [50, 10]),                # at least 10 minutes for a section without a length
    ((None,), 600, [180]),                       # capped at 3 hours
    ((120, 120), None, [90, 90]),
])
def test_section_targets(minutes, total, targets):
    assert section_targets(Brief.model_validate(_brief(*minutes, total=total))) == pytest.approx(targets)


def test_songs_to_ask_for_over_asks():
    assert [songs_to_ask_for(m) for m in (60, 30, 15, 1)] == [26, 13, 7, 3]


# --- in a build -------------------------------------------------------------------------------------------

def test_half_an_hour_workout_comes_out_at_27_to_33_minutes(logged_in, respx_mock, briefs):
    briefs.answer = _brief(30)
    respx_mock.post(CHAT_URL).respond(200, json=_completion([{"title": f"Song {i}", "artist": f"Artist {i}"} for i in range(13)]))
    sp = FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(13)})
    _build(logged_in, "workout for half an hour")
    minutes = len(sp.added_uris()) * 3.5
    assert 27 <= minutes <= 33
    assert sp.search.call_count == len(sp.added_uris())  # no searching once the section is full


def test_no_length_means_about_an_hour(logged_in, respx_mock):
    respx_mock.post(CHAT_URL).respond(200, json=_completion([{"title": f"Song {i}", "artist": f"Artist {i}"} for i in range(26)]))
    sp = FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(26)})
    page = _build(logged_in, "chill")
    assert 54 <= len(sp.added_uris()) * 3.5 <= 66
    assert "16 tracks, 56 min" in page.text


def test_sections_fill_to_their_own_lengths_in_order(logged_in, respx_mock, briefs):
    briefs.answer = _brief(None, 30, None)  # 15 + 30 + 15
    songs = [{"title": f"Song {s}{i}", "artist": f"Artist {s}{i}", "section": s} for s in (1, 2, 3) for i in range(14)]
    chat = respx_mock.post(CHAT_URL).respond(200, json=_completion(songs))
    sp = FakeSpotify(respx_mock, {song["title"]: song["title"].replace(" ", "") for song in songs})
    post = logged_in.post("/playlist", data={"mood": "summer morning, workout 30 min, then lock in"})
    page = finish_build(logged_in, post)

    asked = json.loads(chat.calls[0].request.content)["messages"][-1]["content"]
    assert "1. Part 1: 7 songs\n2. Part 2: 13 songs\n3. Part 3: 7 songs" in asked
    uris = sp.added_uris()
    by_section = [[u for u in uris if u.startswith(f"spotify:track:Song{s}")] for s in (1, 2, 3)]
    assert uris == by_section[0] + by_section[1] + by_section[2]  # section order kept
    for got, target in zip(by_section, (15, 30, 15)):
        low, high = window(target)
        assert low <= len(got) * 3.5 <= high
    row_id = _playlist_rows()[0]["id"]
    assert playlists.section_minutes(row_id) == pytest.approx({0: 14.0, 1: 28.0, 2: 14.0})
    built = [e for e in _job_events(post) if e["kind"] == "built"][0]
    assert built["targets"] == [15.0, 30.0, 15.0] and built["section_minutes"] == [14.0, 28.0, 14.0]
    created = json.loads(sp.create.calls.last.request.content)
    assert created["description"].endswith("(56 min)")


def test_only_short_sections_get_one_more_call(logged_in, respx_mock, briefs):
    briefs.answer = _brief(14, 14)
    first = [{"title": f"Song A{i}", "artist": "Artist A", "section": 1} for i in range(4)] + \
            [{"title": f"Song B{i}", "artist": "Artist B", "section": 2} for i in range(4)]
    extra = [{"title": f"Song C{i}", "artist": "Artist C", "section": 2} for i in range(6)]
    chat = respx_mock.post(CHAT_URL).mock(side_effect=[
        httpx.Response(200, json=_completion(first)), httpx.Response(200, json=_completion(extra))])
    found = {f"Song A{i}": f"a{i}" for i in range(4)} | {"Song B0": "b0"} | {f"Song C{i}": f"c{i}" for i in range(6)}
    sp = FakeSpotify(respx_mock, found)
    _build(logged_in, "two parts")

    assert chat.call_count == 2
    second = json.loads(chat.calls[1].request.content)["messages"][-1]["content"]
    # Part 2 had one 3.5-minute song, so 10.5 minutes were missing: ceil(1.5 x 10.5 / 3.5) = 5 songs
    assert "2. Part 2: 5 songs" in second and "1. Part 1" not in second.split("Suggest songs for these sections")[1]
    uris = sp.added_uris()
    assert uris[:4] == [f"spotify:track:a{i}" for i in range(4)]  # part 1 was already full
    assert 11 <= (len(uris) - 4) * 3.5 <= 17  # part 2 filled by the extra call


def test_a_song_suggested_twice_is_looked_up_once(logged_in, respx_mock):
    songs = [{"title": "Song 0", "artist": "Artist 0"}] * 3 + [{"title": f"Song {i}", "artist": f"Artist {i}"} for i in range(1, 20)]
    respx_mock.post(CHAT_URL).respond(200, json=_completion(songs))
    sp = FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(20)})
    _build(logged_in, "x")
    queries = [c.request.url.params["q"] for c in sp.search.calls]
    assert queries.count("track:Song 0 artist:Artist 0") == 1


def test_length_rules_are_enforced_on_spotify_lengths(logged_in, respx_mock, briefs):
    briefs.answer = _brief(None, total=20, max_track_minutes=3.0)
    respx_mock.post(CHAT_URL).respond(200, json=_completion([{"title": f"Song {i}", "artist": f"Artist {i}"} for i in range(16)]))
    sp = FakeSpotify(respx_mock, {})

    def search(request):
        title = request.url.params["q"][len("track:"):].split(" artist:")[0]
        i = int(title.split()[-1])
        length = 4.0 if i % 2 == 0 else 2.5  # even songs are too long
        return httpx.Response(200, json={"tracks": {"items": [track_json(Track(f"spotify:track:t{i}", title, (f"Artist {i}",),
                                                                                int(length * 60_000)))]}})

    sp.search.mock(side_effect=search)
    _build(logged_in, "20 minute run, songs under 3 minutes")
    assert sp.added_uris() and all(int(u.rsplit("t", 1)[1]) % 2 == 1 for u in sp.added_uris())
    assert 17 <= len(sp.added_uris()) * 2.5 <= 23


# --- search cache ------------------------------------------------------------------------------------------

def test_a_song_searched_again_within_7_days_comes_from_the_cache(logged_in, respx_mock, briefs):
    briefs.answer = _brief(None, total=7)  # two songs
    respx_mock.post(CHAT_URL).respond(200, json=_completion([{"title": f"Song {i}", "artist": f"Artist {i}"} for i in range(2)]))
    sp = FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(2)})
    _build(logged_in, "first")
    _build(logged_in, "second")
    assert sp.search.call_count == 2  # the second build hit the cache
    assert len(_playlist_rows()) == 2

    with get_conn() as conn:  # 8 days later
        conn.execute("UPDATE search_cache SET fetched_at = datetime('now', '-8 days')")
    _build(logged_in, "third")
    assert sp.search.call_count == 4


def test_search_cache_is_per_user_and_keeps_the_explicit_flag(user):
    users.upsert("friend2", "Two", "r")
    track = Track("spotify:track:x", "Song", ("A",), 200_000, explicit=True)
    search_cache.put("friend1", "Song", "A", [track])
    assert search_cache.get("friend1", "song", "a") == [track]  # case doesn't matter
    assert search_cache.get("friend2", "Song", "A") is None
    users.delete("friend1")
    with get_conn() as conn:
        assert conn.execute("SELECT count(*) FROM search_cache").fetchone()[0] == 0  # gone with the user


def test_expired_entries_are_pruned_at_startup(user):
    from fastapi.testclient import TestClient
    from src.main import app
    search_cache.put("friend1", "Old", "A", [])
    search_cache.put("friend1", "New", "A", [])
    with get_conn() as conn:
        conn.execute("UPDATE search_cache SET fetched_at = datetime('now', '-8 days') WHERE query = 'old|a'")
    with TestClient(app):
        pass
    with get_conn() as conn:
        assert [r[0] for r in conn.execute("SELECT query FROM search_cache")] == ["new|a"]


def test_a_build_stops_at_150_searches(logged_in, respx_mock, briefs):
    briefs.answer = _brief(None, total=180)
    respx_mock.post(CHAT_URL).respond(200, json=_completion([{"title": f"Song {i}", "artist": f"Artist {i}"} for i in range(200)]))
    sp = FakeSpotify(respx_mock, {})  # nothing matches
    post = logged_in.post("/playlist", data={"mood": "three hours"})
    page = finish_build(logged_in, post)
    assert sp.search.call_count == 150
    assert "Nothing matched" in page.text
    assert any(e["kind"] == "limit" for e in _job_events(post))


# --- live --------------------------------------------------------------------------------------------------

@pytest.mark.live
def test_live_45_minute_commute_is_40_to_50_minutes():
    """Gate. Creates a real playlist."""
    from src.playlist import run
    from src.spotify import auth
    with get_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE onboarding_json IS NOT NULL ORDER BY created_at LIMIT 1").fetchone()
    if user is None or auth.missing_scopes(user["granted_scopes"]):
        pytest.skip("needs an onboarded user who logged in after Phase 10")
    job_id = jobs.create(user["spotify_id"], "45 minute commute, chill")
    run(job_id, user["spotify_id"], "45 minute commute, chill")
    job = jobs.get(job_id, user["spotify_id"])
    assert job["status"] == "done", job["error"]
    built = [e for e in jobs.events(job_id) if e["kind"] == "built"][0]
    print(f"\n{built}\n{playlists.get(job['playlist_id'], user['spotify_id'])['spotify_url']}")
    total = sum(t.duration_ms for t in playlists.tracks(job["playlist_id"])) / 60_000
    assert 40.5 <= total <= 49.5
