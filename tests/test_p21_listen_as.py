"""Phase 21: hear it like a friend: a playlist from a friend's stored taste, saved in your account only."""
import inspect
import json
from urllib.parse import parse_qs

import pytest

from src import library, listen_as, playlists, prompts, sharing, taste, users
from src.db import get_conn
from src.spotify import auth
from src.spotify.client import Track

from .conftest import CHAT_URL, default_brief, finish_build
from .test_p6_playlist import SONG_MS, FakeSpotify, _completion, logged_in, user  # noqa: F401
from .test_p4_taste import ONBOARDING

SAM_TASTE = "Favourite genres: jazz, classical. Artists they love: Bill Evans, Debussy. Avoid: drill, explicit lyrics."
SAMS = [Track(f"spotify:track:s{i}", f"Jazz {i}", ("Bill Evans",), SONG_MS) for i in range(10)]
MINE = [Track(f"spotify:track:m{i}", f"Mine {i}", ("Gunna",), SONG_MS) for i in range(10)]


def _sam(listen=True, **onboarding):
    users.upsert("friend2", "Sam Rivera", "refresh-friend2", auth.SCOPES)
    users.set_onboarding("friend2", {"genres": "jazz, classical", "artists": "Bill Evans", "energy": "low",
                                     "avoid": "drill, explicit lyrics", "familiar": "known", **onboarding})
    users.set_taste_summary("friend2", SAM_TASTE, "2026-10-24 08:00:00")
    library.replace("friend2", {"top": SAMS})
    if listen:
        sharing.save("friend2", False, False, True)


def _listen(client, mood="gym"):
    return client.post("/friends/friend2/listen", data={"mood": mood})


def _songs(tracks):
    return [{"title": t.name, "artist": t.artists[0]} for t in tracks]


def _build(client, respx_mock, mood="gym"):
    chat = respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(SAMS)))
    sp = FakeSpotify(respx_mock, {t.name: t.uri.rsplit(":", 1)[1] for t in SAMS})
    page = finish_build(client, _listen(client, mood))
    return page, chat, sp


# --- consent ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("switches", [(False, False, False), (True, True, False)])
def test_needs_their_listen_as_switch(logged_in, respx_mock, switches):
    _sam(listen=False)
    sharing.save("friend2", *switches)
    assert _listen(logged_in).status_code == 403
    assert "Make a playlist the way Sam would hear it" not in logged_in.get("/friends/friend2").text
    with get_conn() as conn:
        assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_switched_off_before_the_build_starts(user):
    _sam()
    from src import jobs
    job = jobs.create("friend1", "gym")
    sharing.save("friend2", False, False, False)
    listen_as.run(job, "friend1", "gym", "friend2")
    assert jobs.get(job, "friend1")["error"] == "not_shared"
    assert playlists.recent("friend1") == []


def test_you_cant_listen_as_yourself(logged_in):
    sharing.save("friend1", True, True, True)
    assert logged_in.post("/friends/friend1/listen", data={"mood": "x"}).status_code == 403


# --- whose taste ----------------------------------------------------------------------------------------------

def test_the_prompts_carry_their_taste_not_yours(logged_in, respx_mock, briefs):
    _sam()
    library.replace("friend1", {"top": MINE})
    page, chat, _ = _build(logged_in, respx_mock)
    assert page.status_code == 200
    brief_prompt = prompts.sent_text(json.loads(briefs.calls[-1].content)["messages"])
    suggest_prompt = prompts.sent_text(json.loads(chat.calls[0].request.content)["messages"])
    for prompt in (brief_prompt, suggest_prompt):
        assert SAM_TASTE in prompt
        assert ONBOARDING["genres"] not in prompt and "Gunna" not in prompt  # nothing of yours
    assert "1. Jazz 0 by Bill Evans" in suggest_prompt and "Mine 0" not in suggest_prompt  # their known songs
    assert "about 60%" in suggest_prompt  # their familiarity answer ("known"), not yours


def test_their_avoid_rules_apply_and_yours_dont(logged_in, respx_mock, briefs):
    _sam()
    page, chat, _ = _build(logged_in, respx_mock)
    suggest_prompt = prompts.sent_text(json.loads(chat.calls[0].request.content)["messages"])
    assert "Hard rules, never break them: no explicit lyrics; no drill." in suggest_prompt
    assert "no country" not in suggest_prompt  # your onboarding says "no country"


def test_your_memory_isnt_used(logged_in, respx_mock, briefs):
    from src.memory import Draft
    from .test_p15_reviews import _lesson, _store
    _store("friend1", [Draft(**_lesson(title="Skip long intros", kind="preference", tags=("workout",)))])
    _sam()
    _, chat, _ = _build(logged_in, respx_mock)
    assert "Skip long intros" not in prompts.sent_text(json.loads(chat.calls[0].request.content)["messages"])
    assert "Skip long intros" not in prompts.sent_text(json.loads(briefs.calls[-1].content)["messages"])


def test_only_your_token_is_used_and_the_playlist_is_yours(logged_in, respx_mock):
    _sam()
    page, _, sp = _build(logged_in, respx_mock)
    sent = [parse_qs(c.request.content.decode()) for c in respx_mock.calls if c.request.url == auth.TOKEN_URL]
    assert {s["refresh_token"][0] for s in sent if s["grant_type"] == ["refresh_token"]} == {"refresh-1"}
    assert json.loads(sp.create.calls.last.request.content)["name"].endswith("(as Sam hears it)")
    assert json.loads(sp.create.calls.last.request.content)["description"].startswith("The way Sam would hear it.")
    with get_conn() as conn:
        log = [tuple(r) for r in conn.execute("SELECT owner, viewer, kind, playlist_id IS NOT NULL FROM taste_access")]
        owner = conn.execute("SELECT user_id, heard_as FROM playlists").fetchone()
    assert log == [("friend2", "friend1", "listen_as", 1)]
    assert tuple(owner) == ("friend1", "friend2")
    assert "Made the way Sam would hear it" in page.text
    assert playlists.recent("friend2") == []  # nothing in Sam's Moodify either


def test_the_build_takes_only_a_profile():
    params = inspect.signature(listen_as.build_as).parameters
    assert list(params) == ["user", "mood", "profile", "trace"]
    assert params["profile"].annotation is taste.Profile
    with pytest.raises(TypeError):
        listen_as.build_as({"user_id": "x"}, "gym", {"user_id": "friend2", "taste_summary": "x"})


def test_a_friend_profile_holds_only_taste(user):
    _sam()
    playlists.set_review(playlists.insert("friend2", "my secret prompt", "u", SAMS[:1], json.dumps(default_brief("x"))),
                         "friend2", "my secret review")
    profile = listen_as.friend_profile(users.get("friend2"))
    assert profile.summary == SAM_TASTE and profile.pool == SAMS and profile.name == "Sam"
    assert profile.avoid == ("drill",) and profile.no_explicit and profile.familiar_percent == 60
    assert "secret" not in repr(profile)


def test_after_they_disconnect_the_playlist_stays_yours(logged_in, respx_mock):
    _sam()
    _build(logged_in, respx_mock)
    users.delete("friend2")
    from .test_p16_memory import assert_no_rows_for
    assert_no_rows_for("friend2")
    pid = playlists.recent("friend1")[0]["id"]
    page = logged_in.get(f"/playlists/{pid}").text
    assert "Made the way" not in page


# --- live ---------------------------------------------------------------------------------------------------

@pytest.mark.live
def test_live_gym_the_way_a_jazz_friend_hears_it(monkeypatch):
    """Gate. A synthetic jazz-and-classical friend; prompt "gym". Prints the tracks for a look and checks the
    suggestion prompt carried the friend's taste. Creates one private playlist in your Spotify."""
    from src.db import init_db
    from src.jobs import Trace
    from src.spotify.client import SpotifyClient
    from .test_p20_blend import JAZZ
    init_db()
    with get_conn() as conn:
        me = conn.execute("SELECT * FROM users WHERE onboarding_json IS NOT NULL ORDER BY created_at LIMIT 1").fetchone()
    friend = "synthetic-jazz-friend"
    users.upsert(friend, "Jazz Friend", "no-token-synthetic")
    sent = {}
    real_ask = prompts.ask

    def recording_ask(call, model, messages, *args, **kwargs):
        sent.setdefault(call, []).append(prompts.sent_text(messages))
        return real_ask(call, model, messages, *args, **kwargs)
    monkeypatch.setattr(prompts, "ask", recording_ask)

    try:
        users.set_onboarding(friend, {"genres": "jazz, classical", "artists": "Bill Evans, Miles Davis, Debussy",
                                      "energy": "low", "avoid": "rap"})
        summary = ("Favourite genres: jazz, classical. Artists they love: Bill Evans, Miles Davis, Claude Debussy, Erik Satie. "
                   "Preferred energy: low. Avoid: rap.")
        users.set_taste_summary(friend, summary, "2026-10-24 08:00:00")
        with SpotifyClient.for_user(me) as sp:
            found = [next(iter(sp.search_track(t, a)), None) for t, a in JAZZ]
        library.replace(friend, {"top": [t for t in found if t]})
        profile = listen_as.friend_profile(users.get(friend))
        assert profile.summary == summary
        pid = listen_as.build_as(users.get(me["user_id"]), "gym", profile, Trace())
        for t in playlists.tracks(pid):
            print(f"{t.name} by {', '.join(t.artists)}")
        print(playlists.get(pid, me["user_id"])["url"])
        mine = taste.stored_summary(users.get(me["user_id"]))
        for call in ("brief", "suggest"):
            assert all(summary in text and mine not in text for text in sent[call]), call
    finally:
        users.delete(friend)
