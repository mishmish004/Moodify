"""Phase 16: recall in every build, the cached memory block, no-repeat, lesson upkeep, "More like this",
and the "What Moodify remembers" page."""
import json

import httpx
import pytest

from src import memory, playlists, prompts, users
from src.brief import Brief
from src.db import get_conn
from src.memory import Draft
from src.spotify import auth
from src.spotify.client import Track

from .conftest import CHAT_URL, default_brief, finish_build
from .test_p6_playlist import FakeSpotify, _completion, _songs, logged_in, user  # noqa: F401
from .test_p14_feedback import Library
from .test_p15_reviews import ChatMock, _lesson, _store


def _brief(*tags, **changes) -> Brief:
    return Brief.model_validate(default_brief("Test", tags=list(tags), **changes))


def _lessons_fixture(user_id="friend1"):
    drafts = [
        Draft(**_lesson(title="Workout energy 8+", tags=("workout", "energy"), content="Keep workouts at 8 or more.")),
        Draft(**_lesson(title="No warm-up songs at the gym", tags=("workout", "gym"), content="Start at full energy.")),
        Draft(**_lesson(title="Hard cut into focus works", tags=("focus",), kind="strategy", content="Cut straight in.")),
        Draft(**_lesson(title="Sleep music too busy", tags=("sleep",), content="Sparser songs for sleep.")),
        Draft(**_lesson(title="Skip long intros", tags=("intros",), kind="preference", content="No long intros.")),
        Draft(**_lesson(title="Less Drake", tags=("artists",), kind="preference", content="At most one Drake song.")),
    ]
    return _store(user_id, drafts)


def _playlist(owner="friend1", uris=None, prompt="rainy day", rating=None) -> int:
    uris = uris or [f"spotify:track:{owner}-x"]
    tracks = [Track(u, f"Song {u.rsplit(':', 1)[-1]}", (f"Artist {u.rsplit(':', 1)[-1]}",), 210_000) for u in uris]
    pid = playlists.insert(owner, prompt, f"https://open.spotify.com/playlist/{owner}", tracks, json.dumps(default_brief(prompt)))
    if rating:
        playlists.set_rating(pid, owner, rating)
    return pid


def _prompt_of(call) -> str:
    """The whole prompt as the model reads it: system message (static rules, then per user), then the request."""
    return prompts.sent_text(json.loads(call.content if hasattr(call, "content") else call.request.content)["messages"])


# --- recall -----------------------------------------------------------------------------------------------

def test_recall_gets_matching_lessons_and_preferences_not_others(user):
    _lessons_fixture()
    for i in range(200):  # a long history must not grow the memory past its limits
        _playlist(uris=[f"spotify:track:h{i}"], prompt=f"playlist number {i} " + "words " * 30, rating=8 + i % 3)
    with get_conn() as conn:
        conn.executemany("INSERT INTO track_likes (user_id, uri) VALUES ('friend1', ?)", [(f"spotify:track:h{i}",) for i in range(200)])
    text = memory.memory_for("friend1", _brief("workout"))
    assert "Workout energy 8+" in text and "No warm-up songs at the gym" in text
    assert "Skip long intros" in text and "Less Drake" in text
    assert "Sleep music too busy" not in text and "Hard cut into focus" not in text
    assert len(text) <= 1500
    assert sum(line.startswith("- ") for line in text.splitlines()) <= 8


def test_recall_never_gives_more_than_8_lessons(user):
    _store("friend1", [Draft(**_lesson(title=f"Workout {i}", tags=("workout",), kind="pitfall" if i % 2 else "preference"))
                       for i in range(20)])
    parts = memory.prompt_parts("friend1", {"workout"})
    lines = [l for l in (parts.block + "\n" + parts.recalled).splitlines() if l.startswith("- ")]
    assert len(lines) == 8 and len(parts.ids) == 8 - 4  # 4 preferences in the block, 4 recalled


def test_recall_ranks_by_overlap_then_recency(user):
    ids = _store("friend1", [Draft(**_lesson(title="old two tags", tags=("cooking", "reading"))),
                             Draft(**_lesson(title="new one tag", tags=("cooking",))),
                             Draft(**_lesson(title="newest one tag", tags=("reading",)))])
    rows = memory.recall("friend1", {"cooking", "reading"})
    assert [r["id"] for r in rows] == [ids[0], ids[2], ids[1]]


def test_prompt_words_match_tags():
    assert {"workout", "gym", "run", "runs"} <= memory.words("Gym + runs, then a WORKOUT!")
    assert "lock-in" in memory.words("time to lock-in")


def test_the_brief_call_gets_lessons_recalled_from_the_prompt(logged_in, respx_mock, briefs):
    _lessons_fixture()
    respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(20)))
    FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(20)})
    finish_build(logged_in, logged_in.post("/playlist", data={"mood": "gym, 30 minutes"}))
    prompt = _prompt_of(briefs.calls[-1])
    per_user, per_request = prompt.split("Request: gym", 1)
    assert "- Skip long intros: No long intros." in per_user                # preferences: the cached block
    assert "No warm-up songs at the gym" in per_request and "Workout energy 8+" in per_request  # "gym" is a workout
    assert "Sleep music" not in prompt


def test_the_suggestion_call_gets_lessons_recalled_from_the_brief(logged_in, respx_mock, briefs):
    _lessons_fixture()
    briefs.answer = default_brief("Run", tags=["workout"])
    chat = respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(20)))
    FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(20)})
    finish_build(logged_in, logged_in.post("/playlist", data={"mood": "something to move to"}))
    prompt = _prompt_of(chat.calls[0])
    per_user, per_request = prompt.split("Mood: something to move to", 1)
    assert "Skip long intros" in per_user and "Workout energy 8+" in per_request and "Sleep music" not in prompt
    used = {r["title"]: (r["use_count"], r["last_used_at"]) for r in memory.active("friend1")}
    assert used["Workout energy 8+"][0] == 1 and used["Workout energy 8+"][1]
    assert used["Sleep music too busy"] == (0, None)


# --- the memory block ---------------------------------------------------------------------------------------

def test_memory_block_lists_preferences_likes_and_good_ratings(user):
    _lessons_fixture()
    good = _playlist(uris=["spotify:track:a", "spotify:track:b"], prompt="sunday cooking with friends", rating=9)
    _playlist(uris=["spotify:track:c"], prompt="bad one", rating=3)
    with get_conn() as conn:
        conn.executemany("INSERT INTO track_likes (user_id, uri, playlist_id) VALUES ('friend1', ?, ?)",
                         [("spotify:track:a", good), ("spotify:track:b", good)])
    block = memory.memory_block("friend1")
    assert "Preferences from their reviews:\n- Less Drake: At most one Drake song.\n- Skip long intros" in block
    assert "Lean toward the artists they liked most: Artist a (1), Artist b (1)." in block
    assert "Songs they liked: Song a by Artist a; Song b by Artist b." in block or "Song b by Artist b; Song a" in block
    assert '"sunday cooking with friends" (9/10)' in block and "bad one" not in block


def test_memory_block_is_cached_until_feedback_changes(logged_in, respx_mock, briefs):
    _lessons_fixture()
    chat = respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(40)))
    FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(40)})
    finish_build(logged_in, logged_in.post("/playlist", data={"mood": "first"}))
    version = users.get("friend1")["memory_version"]
    finish_build(logged_in, logged_in.post("/playlist", data={"mood": "second"}))
    assert users.get("friend1")["memory_version"] == version  # not rendered again

    def per_user(call, marker):
        return _prompt_of(call).split(marker)[0]
    assert per_user(briefs.calls[-2], "Request:") == per_user(briefs.calls[-1], "Request:")
    # The suggestion prompt's per-user part also lists known songs, the same both times
    assert per_user(chat.calls[0], "Mood:") == per_user(chat.calls[-1], "Mood:")

    newest = max(r["id"] for r in playlists.recent("friend1", limit=-1))
    Library(respx_mock)
    logged_in.post(f"/playlists/{newest}/tracks/0/like")
    assert users.get("friend1")["memory_block"] is None
    assert "Songs they liked: " in memory.memory_block("friend1")
    assert users.get("friend1")["memory_version"] == version + 1


@pytest.mark.parametrize("change", ["rating", "review cleared", "lesson deleted", "forgotten"])
def test_each_kind_of_feedback_invalidates_the_block(user, change):
    lesson_id = _lessons_fixture()[0]
    pid = _playlist()
    playlists.set_review(pid, "friend1", "too slow")
    memory.memory_block("friend1")
    assert users.get("friend1")["memory_block"] is not None
    {"rating": lambda: playlists.set_rating(pid, "friend1", 4),
     "review cleared": lambda: playlists.set_review(pid, "friend1", ""),
     "lesson deleted": lambda: memory.delete_lesson("friend1", lesson_id),
     "forgotten": lambda: playlists.forget(pid, "friend1")}[change]()
    assert users.get("friend1")["memory_block"] is None


def test_cached_tokens_are_recorded_in_job_events(logged_in, respx_mock):
    reply = _completion(_songs(20))
    reply["usage"] = {"prompt_tokens": 2400, "completion_tokens": 300, "total_tokens": 2700,
                      "prompt_tokens_details": {"cached_tokens": 1792}}
    respx_mock.post(CHAT_URL).respond(200, json=reply)
    FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(20)})
    finish_build(logged_in, logged_in.post("/playlist", data={"mood": "rain"}))
    from src import jobs
    with get_conn() as conn:
        job_id = conn.execute("SELECT max(id) FROM jobs").fetchone()[0]
    suggest = [e for e in jobs.events(job_id) if e["kind"] == "llm" and e["call"] == "suggest"]
    assert suggest[0]["cached_tokens"] == 1792 and suggest[0]["input_tokens"] == 2400


# --- no-repeat ----------------------------------------------------------------------------------------------

def test_songs_from_the_last_3_playlists_come_back_only_if_liked_and_at_most_2(logged_in, respx_mock, briefs):
    briefs.answer = default_brief("Run", total_minutes=21)  # six 3.5-minute songs
    earlier = _playlist(uris=[f"spotify:track:t{i}" for i in range(6)])
    with get_conn() as conn:
        conn.executemany("INSERT INTO track_likes (user_id, uri, playlist_id) VALUES ('friend1', ?, ?)",
                         [(f"spotify:track:t{i}", earlier) for i in (0, 1, 2)])
    chat = respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(14)))  # the model ignores the rule
    sp = FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(14)})
    finish_build(logged_in, logged_in.post("/playlist", data={"mood": "run"}))
    added = sp.added_uris()
    assert not {f"spotify:track:t{i}" for i in (3, 4, 5)} & set(added)       # not liked: never again
    assert len({f"spotify:track:t{i}" for i in (0, 1, 2)} & set(added)) == 2  # liked: at most 2
    prompt = _prompt_of(chat.calls[0])
    assert "already, so don't suggest them: Song t3 by Artist t3; Song t4 by Artist t4; Song t5 by Artist t5" in prompt


def test_only_the_last_3_playlists_count(logged_in, respx_mock, briefs):
    briefs.answer = default_brief("Run", total_minutes=7)
    _playlist(uris=["spotify:track:t0"])  # 4th newest by the time we build
    for i in range(3):
        _playlist(uris=[f"spotify:track:other{i}"])
    respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(2)))
    sp = FakeSpotify(respx_mock, {"Song 0": "t0", "Song 1": "t1"})
    finish_build(logged_in, logged_in.post("/playlist", data={"mood": "run"}))
    assert "spotify:track:t0" in sp.added_uris()


def test_fixing_a_playlist_may_reuse_its_songs(logged_in, respx_mock, briefs):
    briefs.answer = default_brief("Run", total_minutes=7)
    pid = _playlist(uris=["spotify:track:t0", "spotify:track:t1"])
    playlists.set_review(pid, "friend1", "keep these but make it louder")
    respx_mock.post(CHAT_URL, json__response_format__json_schema__name="Reflection").respond(500)
    respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(2)))
    sp = FakeSpotify(respx_mock, {"Song 0": "t0", "Song 1": "t1"})
    finish_build(logged_in, logged_in.post(f"/playlists/{pid}/fix"))
    assert set(sp.added_uris()) == {"spotify:track:t0", "spotify:track:t1"}


# --- lesson upkeep ------------------------------------------------------------------------------------------

def test_unused_pitfalls_expire_after_180_days_preferences_never(user):
    pitfall, strategy, preference, fresh = _store("friend1", [
        Draft(**_lesson(title="old pitfall")), Draft(**_lesson(title="old strategy", kind="strategy")),
        Draft(**_lesson(title="old preference", kind="preference")), Draft(**_lesson(title="used lately"))])
    with get_conn() as conn:
        conn.execute("UPDATE lessons SET created_at = datetime('now', '-400 days')")
        conn.execute("UPDATE lessons SET last_used_at = datetime('now', '-181 days') WHERE id IN (?, ?)", (pitfall, strategy))
        conn.execute("UPDATE lessons SET last_used_at = datetime('now', '-400 days') WHERE id = ?", (preference,))
        conn.execute("UPDATE lessons SET last_used_at = datetime('now', '-179 days') WHERE id = ?", (fresh,))
    assert memory.expire_unused() == 2
    status = {r["title"]: r["status"] for r in _all_lessons()}
    assert status == {"old pitfall": "expired", "old strategy": "expired", "old preference": "active", "used lately": "active"}


def _all_lessons():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM lessons ORDER BY id").fetchall()


# --- "More like this" ---------------------------------------------------------------------------------------

def test_more_like_this_seeds_a_new_playlist(logged_in, respx_mock, briefs):
    briefs.answer = default_brief("Cooking", total_minutes=7)
    seed = _playlist(uris=["spotify:track:t0", "spotify:track:t1", "spotify:track:t2"], prompt="sunday cooking")
    with get_conn() as conn:
        conn.execute("INSERT INTO track_likes (user_id, uri, playlist_id) VALUES ('friend1', 'spotify:track:t1', ?)", (seed,))
    playlists.set_review(seed, "friend1", "loved the jazzy bits")
    for _ in range(3):  # the seed is no longer among the last 3, but its songs still don't repeat unless liked
        _playlist(uris=[f"spotify:track:x{_}"])
    chat = respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(6)))
    sp = FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(6)})
    page = finish_build(logged_in, logged_in.post(f"/playlists/{seed}/more"))
    assert page.status_code == 200
    correction = _prompt_of(briefs.calls[-1]).split("Correction:\n", 1)[1]
    assert correction.startswith("The listener wants more like an earlier playlist")
    assert "Songs they liked in it (more like these): Song t1 by Artist t1" in correction
    assert "What they said about it: loved the jazzy bits" in correction and "Earlier brief: " in correction
    assert "More like an earlier playlist:" in _prompt_of(chat.calls[0])
    added = set(sp.added_uris())
    assert not {"spotify:track:t0", "spotify:track:t2"} & added
    new = max(r["id"] for r in playlists.recent("friend1", limit=-1))
    assert playlists.get(new, "friend1")["parent_id"] is None


def test_more_like_this_on_another_users_playlist_is_404(logged_in):
    users.upsert("friend2", "Friend Two", "refresh-2", auth.SCOPES)
    theirs = _playlist(owner="friend2")
    assert logged_in.post(f"/playlists/{theirs}/more").status_code == 404


# --- the memory page ----------------------------------------------------------------------------------------

def test_memory_page_shows_the_block_and_lessons_with_their_reviews(logged_in):
    pid = _playlist(prompt="gym time")
    _store("friend1", [Draft(**_lesson(title="Workout energy 8+", evidence="way too slow"))], source=pid)
    _store("friend1", [Draft(**_lesson(title="Skip long intros", kind="preference", evidence="intros bore me"))])
    page = logged_in.get("/memory").text
    assert '<pre class="memory-block">What Moodify knows from the listener&#39;s feedback:' in page
    assert "Workout energy 8+" in page and "From your review: “way too slow” on" in page
    assert f'href="/playlists/{pid}"' in page and 'action="/memory/lessons/' in page
    assert f'action="/playlists/{pid}/forget"' in page
    assert 'href="/memory"' in page  # in the header


def test_deleting_a_lesson(logged_in):
    [lesson] = _store("friend1", [Draft(**_lesson())])
    resp = logged_in.post(f"/memory/lessons/{lesson}/delete")
    assert resp.status_code == 303 and resp.headers["location"] == "/memory#lessons"
    assert _all_lessons() == [] and "Lesson deleted." in logged_in.get("/memory").text


def test_another_users_lesson_or_playlist_cant_be_deleted(logged_in):
    users.upsert("friend2", "Friend Two", "refresh-2", auth.SCOPES)
    [theirs] = _store("friend2", [Draft(**_lesson())])
    their_playlist = _playlist(owner="friend2")
    assert logged_in.post(f"/memory/lessons/{theirs}/delete").status_code == 404
    assert logged_in.post(f"/playlists/{their_playlist}/forget").status_code == 404
    assert len(_all_lessons()) == 1 and playlists.get(their_playlist, "friend2") is not None


def test_forgetting_a_playlist_removes_its_likes_rating_review_and_lessons(logged_in):
    pid = _playlist(uris=["spotify:track:a", "spotify:track:b"], rating=7)
    keep = _playlist(uris=["spotify:track:c"])
    playlists.set_review(pid, "friend1", "first")
    playlists.set_review(pid, "friend1", "second")
    with get_conn() as conn:
        conn.execute("INSERT INTO track_likes (user_id, uri, playlist_id) VALUES ('friend1', 'spotify:track:a', ?)", (pid,))
        conn.execute("INSERT INTO track_likes (user_id, uri, playlist_id) VALUES ('friend1', 'spotify:track:c', ?)", (keep,))
    _store("friend1", [Draft(**_lesson())], source=pid)
    resp = logged_in.post(f"/playlists/{pid}/forget")
    assert resp.status_code == 303
    with get_conn() as conn:
        assert conn.execute("SELECT count(*) FROM playlist_tracks WHERE playlist_id = ?", (pid,)).fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM review_history").fetchone()[0] == 0
        assert [r[0] for r in conn.execute("SELECT uri FROM track_likes")] == ["spotify:track:c"]
    assert _all_lessons() == [] and playlists.get(keep, "friend1") is not None
    assert "It&#39;s still in your Spotify." in logged_in.get("/memory").text


def test_disconnect_leaves_no_row_about_the_user_in_any_table(logged_in, respx_mock):
    users.upsert("friend2", "Friend Two", "refresh-2", auth.SCOPES)
    _lessons_fixture("friend2")
    pid = _playlist(rating=9)
    playlists.set_review(pid, "friend1", "first")
    playlists.set_review(pid, "friend1", "second")
    _lessons_fixture()
    with get_conn() as conn:
        conn.execute("INSERT INTO track_likes (user_id, uri, playlist_id) VALUES ('friend1', 'spotify:track:friend1-x', ?)", (pid,))
        job = conn.execute("INSERT INTO jobs (user_id, prompt) VALUES ('friend1', 'p')").lastrowid
        conn.execute("INSERT INTO job_events (job_id, kind) VALUES (?, 'step')", (job,))
        conn.execute("INSERT INTO search_cache (user_id, query, tracks_json) VALUES ('friend1', 'q', '[]')")
    memory.memory_block("friend1")
    logged_in.post("/disconnect")
    assert_no_rows_for("friend1")
    assert len(memory.active("friend2")) == 6


def assert_no_rows_for(user_id: str):
    """Every table: no row holds the user's id in any column, and no row hangs off one of their rows."""
    with get_conn() as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            for column in columns:
                count = conn.execute(f"SELECT count(*) FROM {table} WHERE CAST({column} AS TEXT) = ?", (user_id,)).fetchone()[0]
                assert count == 0, f"{table}.{column} still mentions {user_id}"
        orphans = {
            "playlist_tracks": "playlist_id NOT IN (SELECT id FROM playlists)",
            "review_history": "playlist_id NOT IN (SELECT id FROM playlists)",
            "job_events": "job_id NOT IN (SELECT id FROM jobs)",
        }
        for table, where in orphans.items():
            assert conn.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()[0] == 0, table


def test_migration_adds_the_memory_columns(test_settings):
    import sqlite3
    from src.db import init_db
    from .test_p9_jobs import V0_SCHEMA
    conn = sqlite3.connect(test_settings.DB_PATH)
    conn.executescript(V0_SCHEMA)
    conn.execute("INSERT INTO users (spotify_id, refresh_token) VALUES ('old', 'r')")
    conn.commit()
    conn.close()
    init_db()
    row = users.get("old")
    assert row["memory_block"] is None and row["memory_version"] == 0
    assert memory.memory_block("old") == "" and users.get("old")["memory_version"] == 1


# --- live ---------------------------------------------------------------------------------------------------

@pytest.mark.live
def test_live_second_build_within_the_hour_hits_the_prompt_cache():
    """Gate. Two builds' LLM calls (brief and songs) for your account, a minute apart, with no playlist made.
    The second should report cached input tokens."""
    from src import taste
    from src.brief import read_prompt
    from src.db import init_db
    from src.jobs import Trace
    from src.suggest import suggest_songs

    init_db()
    with get_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE onboarding_json IS NOT NULL ORDER BY created_at LIMIT 1").fetchone()
    if user is None:
        pytest.skip("needs an onboarded user")
    profile = taste.profile_for(user)
    events = []

    class Recorder(Trace):
        def event(self, kind, step=None, **detail):
            events.append({"kind": kind, **detail})

    def build(mood):
        parts = memory.prompt_parts(user["user_id"], memory.words(mood))
        brief = read_prompt(mood, profile.summary, Recorder(), user_id=user["user_id"], memory=parts.block,
                            recalled=parts.recalled)
        suggest_songs(mood, profile.summary, n=12, trace=Recorder(), known=profile.pool, brief=brief,
                      user_id=user["user_id"], memory=parts.block)

    build("rainy afternoon, reading")
    first = len(events)
    build("late night, winding down")
    second = [e for e in events[first:] if e["kind"] == "llm" and e.get("ok")]
    for e in second:
        print(f"{e['call']}: {e.get('cached_tokens')} of {e.get('input_tokens')} input tokens cached")
    cached = sum(e.get("cached_tokens") or 0 for e in second)
    total = sum(e.get("input_tokens") or 0 for e in second)
    print(f"second build: {cached}/{total} input tokens cached ({cached / total:.0%})")
    assert cached > 0


@pytest.mark.live
def test_live_a_too_slow_review_raises_the_energy_next_time(tmp_path, monkeypatch):
    """Gate. A synthetic listener (no Spotify) rates a workout playlist 2/10 with "too slow". After reflection,
    the brief for a similar prompt should have higher energy than the same brief made without memory."""
    from src import reflect
    from src.brief import read_prompt
    from src.core.config import get_settings
    from src.db import init_db

    monkeypatch.setenv("DB_PATH", str(tmp_path / "live.db"))
    get_settings.cache_clear()
    init_db()
    users.upsert("synthetic", "Synthetic", "no-token")
    taste_text = "Favourite genres: hip hop, pop. Artists they love: Kendrick Lamar, Dua Lipa. Preferred energy: mixed."
    workout = default_brief("Workout", sections=[{"label": "Workout", "feeling": "steady, upbeat", "energy": 6, "minutes": 30,
                                                  "activity": "workout", "transition_in": "blend"}], tags=["workout"])
    tracks = [Track(f"spotify:track:w{i}", name, (artist,), 220_000) for i, (name, artist) in enumerate([
        ("Levitating", "Dua Lipa"), ("Sunflower", "Post Malone"), ("Location", "Khalid"), ("LOVE.", "Kendrick Lamar")])]
    pid = playlists.insert("synthetic", "workout, 30 minutes", "https://open.spotify.com/playlist/x", tracks,
                           json.dumps(workout), [0] * 4, [6, 5, 4, 4], ["opener", "build", "build", "closer"])
    playlists.set_rating(pid, "synthetic", 2)
    playlists.set_review(pid, "synthetic", "too slow")
    reflect.run(pid, "synthetic", "too slow")
    print("lessons:", [(r["title"], r["kind"], r["scope"], r["tags_json"]) for r in memory.active("synthetic")])

    prompt = "gym session, 40 minutes"
    parts = memory.prompt_parts("synthetic", memory.words(prompt))
    with_memory = read_prompt(prompt, taste_text, memory=parts.block, recalled=parts.recalled)
    without = read_prompt(prompt, taste_text)

    def energy(brief):
        return sum(s.energy for s in brief.sections) / len(brief.sections)
    print(f"memory sent: {parts.block!r} / {parts.recalled!r}")
    print(f"energy with memory {energy(with_memory):.1f} {[s.energy for s in with_memory.sections]}, "
          f"without {energy(without):.1f} {[s.energy for s in without.sections]}")
    assert energy(with_memory) > energy(without)


def test_everyday_words_recall_lessons_with_shared_tags(user):
    _lessons_fixture()
    assert {r["title"] for r in memory.recall("friend1", memory.words("gym session, 40 minutes"))} == {
        "Workout energy 8+", "No warm-up songs at the gym"}
    assert {r["title"] for r in memory.recall("friend1", memory.words("studying for exams"))} == {"Hard cut into focus works"}
    assert {r["title"] for r in memory.recall("friend1", memory.words("going to bed"))} == {"Sleep music too busy"}
    assert memory.recall("friend1", memory.words("sunday cooking")) == []
