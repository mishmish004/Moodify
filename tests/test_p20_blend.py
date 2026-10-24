"""Phase 20: Moodify Blend: a prompt plus several friends' stored taste, fair to each, saved for the requester only."""
import json
from urllib.parse import parse_qs

import httpx
import pytest

from src import blend, jobs, library, playlists, prompts, sharing, taste, users
from src.db import get_conn
from src.spotify import auth
from src.spotify.client import Track

from .conftest import CHAT_URL, default_brief, finish_build
from .test_p6_playlist import SONG_MS, FakeSpotify, logged_in, user  # noqa: F401

MINE = [Track(f"spotify:track:m{i}", f"Mine {i}", ("Gunna",), SONG_MS) for i in range(10)]
SAMS = [Track(f"spotify:track:s{i}", f"Jazz {i}", ("Bill Evans",), SONG_MS) for i in range(10)]
SHARED = Track("spotify:track:shared", "Shared Song", ("Frank Ocean",), SONG_MS)


def _friend(spotify_id="friend2", name="Sam Rivera", blends=True, avoid="", tracks=(), summary=None):
    users.upsert(spotify_id, name, f"refresh-{spotify_id}", auth.SCOPES)
    users.set_onboarding(spotify_id, {"genres": "jazz, classical", "artists": "Bill Evans", "energy": "low", "avoid": avoid})
    if summary:
        users.set_taste_summary(spotify_id, summary, "2026-10-24 08:00:00")
    if blends:
        sharing.save(spotify_id, False, True, False)
    library.replace(spotify_id, {"liked": list(tracks)})
    return spotify_id


def _song(track: Track, for_whom=None, section=1) -> dict:
    return {"title": track.name, "artist": track.artists[0], "known": None, "section": section, "energy": 5,
            "role": "build", "for_whom": for_whom}


class Chat:
    """Suggestion calls: `first` answers the first call; a top-up for someone gets `top_up[name]`."""

    def __init__(self, respx_mock, first, top_up=None):
        self.first, self.top_up, self.calls = first, top_up or {}, []
        respx_mock.post(CHAT_URL).mock(side_effect=self)

    def __call__(self, request):
        body = json.loads(request.content)
        text = prompts.sent_text(body["messages"])
        self.calls.append(text)
        songs = self.first
        for name, answer in self.top_up.items():
            if f"This call is a top-up for {name}" in text:
                songs = answer
        content = json.dumps({"songs": songs})
        return httpx.Response(200, json={"id": "c", "object": "chat.completion", "created": 0, "model": "m", "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}]})

    def top_ups(self, name):
        return [c for c in self.calls if f"This call is a top-up for {name}" in c]


def _spotify(respx_mock, tracks, explicit=()):
    sp = FakeSpotify(respx_mock, {t.name: t.uri.rsplit(":", 1)[1] for t in tracks})
    if explicit:
        def search(request):
            title = request.url.params["q"][len("track:"):].split(" artist:")[0]
            match = next(t for t in tracks if t.name == title)
            return httpx.Response(200, json={"tracks": {"items": [{
                "uri": match.uri, "name": match.name, "artists": [{"name": match.artists[0]}], "duration_ms": SONG_MS,
                "explicit": match.name in explicit}]}})
        sp.search.mock(side_effect=search)
    return sp


def _blend(client, friends=("friend2",), mood="sunday cooking", **form):
    return client.post("/blend", data={"mood": mood, "friends": list(friends), **form})


def _mine_in_library():
    library.replace("friend1", {"top": MINE[:5], "liked": [SHARED, *MINE[5:]]})


# --- who can be picked --------------------------------------------------------------------------------------

def test_only_friends_who_allow_blends_can_be_picked(logged_in, respx_mock):
    _friend("friend2", blends=True)
    _friend("friend3", "Priya", blends=False)
    sharing.save("friend3", True, False, True)  # shares everything but Blends
    page = logged_in.get("/blend").text
    assert 'value="friend2"' in page and 'value="friend3"' not in page
    for friends in (["friend3"], ["friend2", "friend3"], ["nobody"]):
        resp = _blend(logged_in, friends)
        assert resp.status_code == 403, friends
    with get_conn() as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM taste_access").fetchone()[0] == 0


@pytest.mark.parametrize("friends", [[], ["friend2", "friend2"], ["friend1"], [f"f{i}" for i in range(5)]])
def test_one_to_four_distinct_friends(logged_in, friends):
    _friend("friend2")
    assert _blend(logged_in, friends).status_code in (400, 403)


def test_a_friend_who_switched_off_before_the_build_stops_it(user):
    _friend("friend2")
    sharing.save("friend2", False, False, False)
    from src.playlist import BuildFailed
    with pytest.raises(BuildFailed) as failed:
        blend.build_blend(users.get("friend1"), "cooking", ["friend2"])
    assert failed.value.reason == blend.NOT_SHARED


def test_consent_is_checked_once_at_the_start(logged_in, respx_mock, monkeypatch):
    _friend("friend2", tracks=SAMS)
    _mine_in_library()
    asked = []
    real = sharing.allowed
    monkeypatch.setattr(sharing, "allowed", lambda *args: asked.append(args) or real(*args))
    Chat(respx_mock, [_song(t, "Sam") for t in SAMS[:4]] + [_song(t, "Friend") for t in MINE[:4]])
    _spotify(respx_mock, SAMS + MINE)
    finish_build(logged_in, _blend(logged_in))
    assert asked.count(("friend2", "friend1", "blend")) == 2  # the form's check, then the build's, once


# --- building -----------------------------------------------------------------------------------------------

def test_no_members_token_is_ever_used(logged_in, respx_mock):
    _friend("friend2", tracks=SAMS)
    _friend("friend3", "Priya Shah", tracks=[SHARED])
    _mine_in_library()
    Chat(respx_mock, [_song(t, "Sam") for t in SAMS[:3]] + [_song(t, "Friend") for t in MINE[:3]] + [_song(SHARED, "everyone")])
    sp = _spotify(respx_mock, SAMS + MINE + [SHARED])
    page = finish_build(logged_in, _blend(logged_in, ("friend2", "friend3")))
    assert page.status_code == 200
    sent = [parse_qs(c.request.content.decode()) for c in respx_mock.calls if c.request.url == auth.TOKEN_URL]
    refreshes = [s for s in sent if s["grant_type"] == ["refresh_token"]]
    assert refreshes and {s["refresh_token"][0] for s in refreshes} == {"refresh-1"}
    assert sp.create.call_count == 1  # one playlist, in the requester's account


def test_everyones_avoid_rules_apply_and_one_no_explicit_filters_for_all(logged_in, respx_mock, briefs):
    _friend("friend2", avoid="country, explicit lyrics", tracks=SAMS)
    _mine_in_library()
    briefs.answer = default_brief("Cooking", total_minutes=14)
    chat = Chat(respx_mock, [_song(t, "Sam") for t in SAMS[:4]] + [_song(t, "Friend") for t in MINE[:4]])
    sp = _spotify(respx_mock, SAMS + MINE, explicit={"Jazz 0", "Mine 0"})
    finish_build(logged_in, _blend(logged_in))
    brief_prompt = prompts.sent_text(json.loads(briefs.calls[-1].content)["messages"])
    assert "Sam: " in brief_prompt and "Avoid: country, explicit lyrics" in brief_prompt
    assert any("Hard rules, never break them: no explicit lyrics; no country." in c for c in chat.calls)
    added = sp.added_uris()
    assert "spotify:track:s0" not in added and "spotify:track:m0" not in added and added


def test_common_ground_comes_first_and_every_known_song_is_labelled(user):
    _friend("friend2", tracks=[SHARED, *SAMS[:3]])
    library.replace("friend1", {"top": [*MINE[:3], SHARED]})
    members = blend.members_for(users.get("friend1"), ["friend2"])
    profile = blend.profile(members)
    assert profile.pool[0] == SHARED and profile.labels[0] == "common ground"
    assert list(profile.labels[1:]) == ["Friend", "Sam", "Friend", "Sam", "Friend", "Sam"]
    assert [t.name for t in profile.pool[1:3]] == ["Mine 0", "Jazz 0"]  # heaviest first, in turn
    assert profile.summary.startswith("A Blend for 2 people: Friend, Sam.")


def test_known_songs_reach_the_prompt_with_their_labels(logged_in, respx_mock):
    _friend("friend2", tracks=[SHARED, *SAMS[:3]])
    library.replace("friend1", {"top": [*MINE[:3], SHARED]})
    chat = Chat(respx_mock, [_song(t, "Sam") for t in SAMS[:3]] + [_song(t, "Friend") for t in MINE[:3]])
    _spotify(respx_mock, SAMS + MINE + [SHARED])
    finish_build(logged_in, _blend(logged_in))
    assert "1. Shared Song by Frank Ocean (common ground)\n2. Mine 0 by Gunna (Friend)\n3. Jazz 0 by Bill Evans (Sam)" in chat.calls[0]


def test_each_summary_is_condensed_to_600_characters(user):
    long = "Favourite genres: jazz. " + "Recently played: " + "; ".join(f"Song {i} by Someone" for i in range(80)) + "."
    _friend("friend2", summary=long)
    [_, sam] = blend.members_for(users.get("friend1"), ["friend2"])
    assert len(sam.summary) <= 600 and sam.summary.startswith("Favourite genres: jazz.")


def test_an_under_served_member_gets_exactly_one_top_up(logged_in, respx_mock, briefs):
    _friend("friend2", tracks=SAMS)
    _mine_in_library()
    briefs.answer = default_brief("Cooking", total_minutes=28)  # 8 songs
    # The model gives nearly everything to the requester; Sam's top-up brings Sam up to the floor (40%)
    chat = Chat(respx_mock, [_song(t, "Friend") for t in MINE] + [_song(SAMS[0], "Sam")],
                top_up={"Sam": [_song(t, "Sam") for t in SAMS[1:8]]})
    sp = _spotify(respx_mock, SAMS + MINE)
    finish_build(logged_in, _blend(logged_in))
    assert len(chat.top_ups("Sam")) == 1 and chat.top_ups("Friend") == []
    added = sp.added_uris()
    sams = sum(u.startswith("spotify:track:s") for u in added)
    assert sams / len(added) >= 0.5 - blend.FAIR_SLACK
    total = 3.5 * len(added)
    assert 25.2 - 3 <= total <= 30.8 + 3  # still about as long as asked


def test_a_fair_first_answer_needs_no_top_up(logged_in, respx_mock, briefs):
    _friend("friend2", tracks=SAMS)
    _mine_in_library()
    briefs.answer = default_brief("Cooking", total_minutes=28)
    chat = Chat(respx_mock, [_song(t, w) for pair in zip(MINE, SAMS) for t, w in ((pair[0], "Friend"), (pair[1], "Sam"))])
    _spotify(respx_mock, SAMS + MINE)
    finish_build(logged_in, _blend(logged_in))
    assert chat.top_ups("Sam") == [] and chat.top_ups("Friend") == []


def test_leaning_toward_someone_raises_their_floor():
    members = [blend.Member("a", "A", "", (), False), blend.Member("b", "B", "", (), False), blend.Member("c", "C", "", (), False)]
    floors = blend.Balance(members, lean="b").floors(100)
    assert round(floors["b"]) == 40 and round(floors["a"]) == 15 and round(floors["c"]) == 15
    assert {k: round(v, 1) for k, v in blend.Balance(members).floors(90).items()} == {"a": 21.0, "b": 21.0, "c": 21.0}


def test_credit_goes_by_library_then_by_who_it_was_for(user):
    members = [blend.Member("friend1", "Friend", "", (), False, {"spotify:track:m": 1, "spotify:track:both": 1}),
               blend.Member("friend2", "Sam", "", (), False, {"spotify:track:s": 1, "spotify:track:both": 1})]
    balance = blend.Balance(members)
    from src.suggest import Song
    assert balance.credit("spotify:track:both", None) == {"friend1", "friend2"}
    assert balance.credit("spotify:track:s", Song(title="x", artist="y", for_whom="Friend")) == {"friend2"}
    assert balance.credit("spotify:track:new", Song(title="x", artist="y", for_whom="sam")) == {"friend2"}
    assert balance.credit("spotify:track:new", Song(title="x", artist="y", for_whom="everyone")) == {"friend1", "friend2"}


def test_name_description_access_log_and_page(logged_in, respx_mock):
    _friend("friend2", "Sam Rivera", tracks=SAMS)
    _friend("friend3", "Priya Shah", tracks=[SHARED])
    _mine_in_library()
    Chat(respx_mock, [_song(t, "Sam") for t in SAMS[:4]] + [_song(t, "Friend") for t in MINE[:4]]
         + [_song(SHARED, "everyone"), _song(SAMS[5], "Priya")])
    sp = _spotify(respx_mock, SAMS + MINE + [SHARED])
    page = finish_build(logged_in, _blend(logged_in, ("friend2", "friend3"), public="on")).text
    created = json.loads(sp.create.calls.last.request.content)
    assert created["name"].startswith("Moodify Blend: Friend × Sam × Priya: ")
    assert created["description"].startswith("A Moodify Blend for Friend, Sam and Priya.") and created["public"] is True
    with get_conn() as conn:
        rows = [tuple(r) for r in conn.execute("SELECT owner, viewer, kind FROM taste_access ORDER BY owner")]
    assert rows == [("friend2", "friend1", "blend"), ("friend3", "friend1", "blend")]
    assert "Whose taste it came from" in page and 'class="share-bar member-2"' in page
    assert '<i class="swatch member-2" aria-hidden="true"></i>SR</span>' in page and "common ground" in page
    assert "<th scope=\"row\">Priya</th>" in page


def test_private_by_default(logged_in, respx_mock):
    _friend("friend2", tracks=SAMS)
    _mine_in_library()
    Chat(respx_mock, [_song(t, "Sam") for t in SAMS[:4]] + [_song(t, "Friend") for t in MINE[:4]])
    sp = _spotify(respx_mock, SAMS + MINE)
    finish_build(logged_in, _blend(logged_in))
    assert json.loads(sp.create.calls.last.request.content)["public"] is False


def test_a_friend_who_disconnects_leaves_the_owners_blend(logged_in, respx_mock):
    _friend("friend2", tracks=SAMS)
    _mine_in_library()
    Chat(respx_mock, [_song(t, "Sam") for t in SAMS[:4]] + [_song(t, "Friend") for t in MINE[:4]])
    _spotify(respx_mock, SAMS + MINE)
    finish_build(logged_in, _blend(logged_in))
    users.delete("friend2")
    from .test_p16_memory import assert_no_rows_for
    assert_no_rows_for("friend2")
    pid = max(r["id"] for r in playlists.recent("friend1", limit=-1))
    assert logged_in.get(f"/playlists/{pid}").status_code == 200


def test_friend_page_links_to_a_blend(logged_in):
    _friend("friend2")
    page = logged_in.get("/friends/friend2").text
    assert 'href="/blend?with=friend2"' in page
    assert 'value="friend2" checked' in logged_in.get("/blend?with=friend2").text


# --- live ---------------------------------------------------------------------------------------------------

JAZZ = [("So What", "Miles Davis"), ("Take Five", "The Dave Brubeck Quartet"), ("Blue in Green", "Miles Davis"),
        ("Waltz for Debby", "Bill Evans Trio"), ("My Favorite Things", "John Coltrane"), ("Clair de Lune", "Claude Debussy"),
        ("Gymnopédie No. 1", "Erik Satie"), ("Cello Suite No. 1 in G Major, BWV 1007: I. Prélude", "Johann Sebastian Bach")]


@pytest.mark.live
def test_live_blend_with_a_synthetic_jazz_friend():
    """Gate. You plus a synthetic friend (jazz and classical; no Spotify account, a stored profile and library) make
    "sunday cooking". Each of you should get at least 40% of the minutes, by whose list each track came from.
    Creates one private playlist in your Spotify; the synthetic friend is deleted afterwards."""
    from src.db import init_db
    from src.spotify.client import SpotifyClient
    init_db()
    with get_conn() as conn:
        me = conn.execute("SELECT * FROM users WHERE onboarding_json IS NOT NULL ORDER BY created_at LIMIT 1").fetchone()
    friend = "synthetic-jazz-friend"
    users.upsert(friend, "Jazz Friend", "no-token-synthetic")
    try:
        users.set_onboarding(friend, {"genres": "jazz, classical", "artists": "Bill Evans, Miles Davis, Debussy",
                                      "energy": "low", "avoid": "rap"})
        users.set_taste_summary(friend, "Favourite genres: jazz, classical. Artists they love: Bill Evans, Miles Davis, "
                                "Claude Debussy, Erik Satie. Preferred energy: low. Avoid: rap. Most played lately: "
                                "Miles Davis, Bill Evans, Chet Baker, John Coltrane, Debussy.", "2026-10-24 08:00:00")
        sharing.save(friend, False, True, False)
        with SpotifyClient.for_user(me) as sp:  # real URIs for the friend's library, found with your login
            found = [next(iter(sp.search_track(t, a)), None) for t, a in JAZZ]
        library.replace(friend, {"top": [t for t in found if t]})
        taste.profile_for(users.get(me["spotify_id"]))  # your own library, fresh
        playlist_id = blend.build_blend(users.get(me["spotify_id"]), "sunday cooking", [friend])
        tracks = playlists.tracks(playlist_id)
        shown = blend.for_playlist(playlist_id, me["spotify_id"], tracks)
        for t, who in zip(tracks, shown.credits):
            print(f"{t.name} by {', '.join(t.artists)}: {[m['name'] for m in shown.members if m['id'] in who]}")
        for s in shown.shares:
            print(f"{s['name']}: {s['percent']}% ({s['minutes']} min), floor {s['floor']}%")
        assert all(s["percent"] >= 40 for s in shown.shares)
    finally:
        users.delete(friend)


@pytest.mark.parametrize("answer, phrases", [
    ("no country, explicit lyrics", ("country", "explicit lyrics")),
    ("Avoid screaming; sad songs and drill", ("screaming", "sad songs", "drill")),
    ("", ()), ("nothing", ("nothing",)),
])
def test_avoid_phrases(answer, phrases):
    assert blend.avoid_phrases({"avoid": answer}) == phrases
