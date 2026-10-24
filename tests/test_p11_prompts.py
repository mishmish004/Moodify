"""Phase 11: any prompt becomes a brief the listener can see and the song picker follows."""
import json
import re
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from src import brief as briefs_module, prompts, users
from src.brief import Brief, SYSTEM_PROMPT, fallback, read_prompt, tidy
from src.db import get_conn
from src.spotify.client import API_URL, Track

from .conftest import CHAT_URL, default_brief, finish_build, track_json
from .test_p6_playlist import FakeSpotify, _build, _completion, _playlist_rows, _songs, logged_in, user  # noqa: F401

TASTE = ("Favourite genres: rap, lo-fi, rnb, house. Artists they love: Gunna, Travis Scott, Drake. Preferred energy: high. "
         "Avoid: No sad songs. No instrumental tracks. Most played artists lately: Drake, Travis Scott, Don Toliver.")

LONG_PROMPT = (
    "Okay so here is exactly what I need, please read all of it. I'm writing my thesis tonight and I'll be at my desk for "
    "about 90 minutes. I get distracted really easily by some kinds of music, so these are the rules. Rule one: no explicit "
    "lyrics at all, my flatmate's kid is around. Rule two: nothing with sudden loud drops or bass hits, those make me jump "
    "and then I lose my train of thought for ten minutes. Rule three: English or Spanish lyrics only, I tune out other "
    "languages in a weird way that actually distracts me more. Rule four: no Drake, I've heard him too much this month. "
    "Rule five: nothing over 6 minutes, long songs make me lose track of time and I like using song changes as little "
    "markers to check my progress. Otherwise I'm pretty open. I'd like it to start calm, like the first few pages where I'm "
    "just warming up and rereading what I wrote yesterday, and then build a little bit of momentum towards the end when I'm "
    "hopefully in the zone and typing fast. Not hype though, just a bit more drive than the start. Think warm, a bit "
    "jazzy or soulful, the kind of thing that sounds like a coffee shop at night. I don't mind if some songs are ones I "
    "already know, that's actually comforting. Thanks!"
)


def _energies(b):
    return [s.energy for s in b.sections]


def _minutes(b):
    return b.total_minutes or sum(s.minutes or 0 for s in b.sections)


def _has(items, *words):
    return any(w in item.lower() for item in items for w in words)


# (id, prompt, checks): each check is (description, predicate on the Brief)
FIXTURES = [
    ("first_real_prompt", "I want to remember the chill summer morning and a hella stuff to do, workout for 1/2 an hour and "
     "then i want to instantly lock in for a programming task.", [
        ("3 sections", lambda b: len(b.sections) == 3),
        ("workout is 30 min", lambda b: b.sections[1].minutes == 30),
        ("focus starts with a cut", lambda b: b.sections[2].transition_in == "cut"),
        ("workout has the most energy", lambda b: b.sections[1].energy == max(_energies(b))),
    ]),
    ("calm_down", "so anxious about tomorrow, i need to calm down", [
        ("2+ sections", lambda b: len(b.sections) >= 2),
        ("energy falls", lambda b: _energies(b) == sorted(_energies(b), reverse=True) and _energies(b)[-1] < _energies(b)[0]),
    ]),
    ("get_hyped", "feeling super sluggish, need to get hyped for the gym", [
        ("2+ sections", lambda b: len(b.sections) >= 2),
        ("energy rises", lambda b: _energies(b)[-1] > _energies(b)[0]),
    ]),
    ("bullet_list", "- studying\n- 2 hours\n- lofi or jazz\n- NO vocals\n- nothing sad", [
        ("no vocals", lambda b: b.hard_rules.no_vocals),
        ("120 minutes", lambda b: _minutes(b) == 120),
        ("avoids sad", lambda b: _has(b.hard_rules.avoid, "sad")),
    ]),
    ("sensory", "no screaming, no harsh synths, nothing too busy, predictable songs only please", [
        ("no sudden loud changes", lambda b: b.hard_rules.no_sudden_loud_changes),
        ("avoids screaming", lambda b: _has(b.hard_rules.avoid, "scream")),
    ]),
    ("literal_rules", "Same tempo the whole time. No lyrics. 40 minutes. Nothing with sudden drops.", [
        ("steady tempo", lambda b: b.hard_rules.steady_tempo),
        ("no vocals", lambda b: b.hard_rules.no_vocals),
        ("no sudden changes", lambda b: b.hard_rules.no_sudden_loud_changes),
        ("40 minutes", lambda b: _minutes(b) == 40),
    ]),
    ("one_word", "meh", [
        ("1 section", lambda b: len(b.sections) == 1),
        ("no support note", lambda b: not b.support_note),
    ]),
    ("emoji_only", "🌧️☕", [
        ("1 section", lambda b: len(b.sections) == 1),
        ("calm", lambda b: max(_energies(b)) <= 5),
    ]),
    ("feeling_numb", "i feel numb today", [
        ("not pushed to high energy", lambda b: max(_energies(b)) <= 5),
        ("no support note", lambda b: not b.support_note),
    ]),
    ("distress", "I don't see the point of anything anymore, I just want to disappear", [
        ("support note", lambda b: b.support_note),
        ("still a playlist", lambda b: len(b.sections) >= 1),
    ]),
    ("hinglish", "yaar mood off hai, kuch chill sa chahiye, no sad songs", [
        ("chill", lambda b: max(_energies(b)) <= 6),
        ("avoids sad", lambda b: _has(b.hard_rules.avoid, "sad")),
    ]),
    ("slang_focus", "lock in mode fr, need to grind for 2 hrs no distractions", [
        ("120 minutes", lambda b: _minutes(b) == 120),
        ("focus tag", lambda b: _has(b.tags, "focus", "study", "work", "grind", "coding", "productiv")),
    ]),
    ("only_known", "only songs I know, road trip with the boys", [
        ("only known", lambda b: b.familiarity == "only_known"),
    ]),
    ("discover", "show me stuff I've never heard before, chill indie", [
        ("new", lambda b: b.familiarity in ("only_new", "mostly_new")),
    ]),
    ("keep_it_clean", "family dinner, my little cousins are here so keep it clean", [
        ("no explicit", lambda b: b.hard_rules.no_explicit),
    ]),
    ("short_run", "20 minute run, short songs only, under 3 minutes each", [
        ("max 3 minutes", lambda b: b.hard_rules.max_track_minutes is not None and b.hard_rules.max_track_minutes <= 3),
        ("20 minutes", lambda b: _minutes(b) == 20),
        ("high energy", lambda b: max(_energies(b)) >= 7),
    ]),
    ("long_detailed", LONG_PROMPT, [
        ("no explicit", lambda b: b.hard_rules.no_explicit),
        ("no sudden loud changes", lambda b: b.hard_rules.no_sudden_loud_changes),
        ("English and Spanish", lambda b: _has(b.hard_rules.languages, "english") and _has(b.hard_rules.languages, "spanish")),
        ("no Drake", lambda b: _has(b.hard_rules.avoid, "drake")),
        ("max 6 minutes", lambda b: b.hard_rules.max_track_minutes == 6),
        ("90 minutes", lambda b: _minutes(b) == 90),
        ("builds up", lambda b: _energies(b)[-1] >= _energies(b)[0]),
    ]),
    ("implied_not_stated", "lock in for coding, 1 hour", [
        # The taste says "No instrumental tracks"; "lock in" only implies instrumental, so it doesn't override
        ("vocals still allowed", lambda b: not b.hard_rules.no_vocals),
        ("60 minutes", lambda b: _minutes(b) == 60),
    ]),
]
assert len(FIXTURES) >= 15


def _failures(brief: Brief, checks) -> list[str]:
    failed = []
    for name, check in checks:
        try:
            ok = check(brief)
        except (IndexError, ValueError, TypeError):
            ok = False
        if not ok:
            failed.append(name)
    return failed


# --- the static part of the prompt ---------------------------------------------------------------------

def test_static_prompt_is_long_enough_to_cache_and_its_examples_are_valid_briefs():
    assert len(SYSTEM_PROMPT) >= 4 * 1024  # ~4 characters a token; OpenAI caches prompts of 1,024+ tokens
    examples = re.findall(r"^Brief: (\{.*\})$", SYSTEM_PROMPT, re.M)
    assert len(examples) >= 5
    for example in examples:
        Brief.model_validate_json(example)


def test_every_call_puts_static_then_per_user_then_per_request(test_settings, respx_mock, briefs):
    read_prompt("first prompt", "taste A", spotify_id="a")
    read_prompt("second, different prompt", "taste B", spotify_id="b")
    first, second = (json.loads(c.content)["messages"] for c in briefs.calls)
    # The static rules come first and are byte-identical; the per-user part follows them in the system message
    # (where gpt-6.1-sol's cache reaches, see src/prompts.py); the request alone is the user message
    assert first[0]["role"] == second[0]["role"] == "system"
    assert first[0]["content"] == SYSTEM_PROMPT + prompts.PER_USER_HEADER + "Listener's taste: taste A"
    assert second[0]["content"] == SYSTEM_PROMPT + prompts.PER_USER_HEADER + "Listener's taste: taste B"
    assert first[1] == {"role": "user", "content": "Request: first prompt"}
    assert [json.loads(c.content)["prompt_cache_key"] for c in briefs.calls] == ["moodify:brief:a", "moodify:brief:b"]


# --- reading prompts (mocked model) -------------------------------------------------------------------

@pytest.mark.parametrize("fixture_id, prompt", [(f[0], f[1]) for f in FIXTURES])
def test_every_fixture_prompt_reaches_the_model_whole(test_settings, respx_mock, briefs, fixture_id, prompt):
    brief = read_prompt(prompt, TASTE)
    sent = json.loads(briefs.calls[-1].content)["messages"][-1]["content"]
    assert sent.endswith("Request: " + prompts.clean_text(prompt, 2000))
    assert len(sent.split("Request: ", 1)[1]) == len(prompts.clean_text(prompt, 10_000))  # nothing under 2,000 cut
    assert brief.sections


def test_line_breaks_in_a_list_prompt_survive(test_settings, respx_mock, briefs):
    read_prompt("- studying\n- 2 hours", TASTE)
    assert json.loads(briefs.calls[-1].content)["messages"][-1]["content"].endswith("Request: - studying\n- 2 hours")


@pytest.mark.parametrize("answer", ["not json", json.dumps({"title": "x"}), json.dumps(default_brief("x", sections=[]))])
def test_unusable_brief_falls_back_to_the_words_themselves(test_settings, respx_mock, briefs, answer):
    briefs.raw = answer
    brief = read_prompt("rainy sunday\nand more", TASTE)
    assert brief == fallback("rainy sunday\nand more")
    assert brief.title == "rainy sunday" and len(brief.sections) == 1


def test_failed_brief_call_falls_back(test_settings, respx_mock, briefs):
    briefs.status = 500
    assert read_prompt("x", TASTE) == fallback("x")
    assert len(briefs.calls) == 2  # retried once


def test_tidy_keeps_the_brief_in_bounds():
    raw = Brief.model_validate(default_brief("  A very long title that goes on and on and on forever  ", sections=[
        {"label": "a", "feeling": "x", "energy": 14, "minutes": -5, "activity": None, "transition_in": "cut"},
        {"label": " ", "feeling": "y", "energy": 0, "minutes": 30, "activity": None, "transition_in": "cut"}],
        tags=["Focus", "focus ", "", "Calm"], total_minutes=0))
    b = tidy(raw, "prompt")
    assert [s.energy for s in b.sections] == [10, 1]
    assert b.sections[0].minutes is None and b.sections[1].minutes == 30
    assert b.sections[0].transition_in == "blend" and b.sections[1].transition_in == "cut"
    assert b.sections[1].label == "Part 2"
    assert len(b.title) <= 40 and b.tags == ["focus", "calm"] and b.total_minutes is None


# --- the brief in a build ---------------------------------------------------------------------------------

def _suggest_request(chat):
    return json.loads(chat.calls.last.request.content)


def test_brief_and_both_kinds_of_avoid_reach_the_song_picker(logged_in, respx_mock, briefs):
    logged_in.post("/onboarding", data={"genres": "rap", "artists": "Gunna", "energy": "high", "avoid": "no sad songs"})
    briefs.answer = default_brief("Gym", summary="High energy for the gym.", hard_rules={
        **default_brief("")["hard_rules"], "avoid": ["country"], "no_explicit": True})
    chat = respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(10)))
    FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(10)})
    _build(logged_in, "gym")
    content = prompts.sent_text(_suggest_request(chat)["messages"])
    assert "Avoid: no sad songs" in content  # onboarding, via the taste summary
    assert "Hard rules, never break them: no explicit lyrics; no country." in content  # the brief
    assert _suggest_request(chat)["prompt_cache_key"] == "moodify:suggest:friend1"


def test_prompt_familiarity_overrides_onboarding(logged_in, respx_mock, briefs):
    briefs.answer = default_brief("Road trip", familiarity="only_known")
    chat = respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(10)))
    FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(10)}, liked=[Track("spotify:track:k", "K", ("A",), 1)])
    _build(logged_in, "only songs I know")
    assert "about 100%" in _suggest_request(chat)["messages"][-1]["content"]


def test_no_explicit_picks_the_clean_version_and_skips_explicit_only_songs(logged_in, respx_mock, briefs):
    briefs.answer = default_brief("Dinner", hard_rules={**default_brief("")["hard_rules"], "no_explicit": True})
    respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(10)))
    sp = FakeSpotify(respx_mock, {})

    def search(request):
        title = request.url.params["q"][len("track:"):].split(" artist:")[0]
        i = int(title.split()[-1])
        explicit = Track(f"spotify:track:e{i}", title, (f"Artist {i}",), 1, explicit=True)
        clean = Track(f"spotify:track:c{i}", title, (f"Artist {i}",), 1)
        items = [explicit, clean] if i % 2 == 0 else [explicit]  # odd songs only exist explicit
        return httpx.Response(200, json={"tracks": {"items": [{**track_json(t), "explicit": t.explicit} for t in items]}})

    sp.search.mock(side_effect=search)
    _build(logged_in, "family dinner, keep it clean")
    assert sp.added_uris() == [f"spotify:track:c{i}" for i in range(0, 10, 2)]


def test_playlist_is_named_after_the_brief_and_shows_how_it_was_read(logged_in, respx_mock, briefs):
    briefs.answer = default_brief("Summer morning → workout → lock in", summary="Warm start, 30-minute workout, then focus.",
                                  sections=[
        {"label": "Summer morning", "feeling": "warm", "energy": 4, "minutes": None, "activity": None, "transition_in": "blend"},
        {"label": "Workout", "feeling": "driving", "energy": 8, "minutes": 30, "activity": "workout", "transition_in": "blend"},
        {"label": "Lock in", "feeling": "focused", "energy": 5, "minutes": None, "activity": "coding", "transition_in": "cut"}],
        hard_rules={**default_brief("")["hard_rules"], "avoid": ["sad songs"]})
    respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(10)))
    sp = FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(10)})
    long_prompt = "I want to remember the chill summer morning " + "and more " * 50
    page = _build(logged_in, long_prompt)

    created = json.loads(sp.create.calls.last.request.content)
    assert created["name"] == "Moodify: Summer morning → workout → lock in"
    assert len(created["description"]) <= 300
    assert "<h1>Moodify: Summer morning → workout → lock in</h1>" in page.text
    assert "How Moodify read this" in page.text and "Warm start, 30-minute workout, then focus." in page.text
    assert "<strong>Workout</strong>: driving, 30 min" in page.text and "(straight in)" in page.text
    assert "Rules kept: no sad songs." in page.text
    row_id = _playlist_rows()[0]["id"]
    assert f'href="/?redo={row_id}"' in page.text
    # Lists show the short title, not the long prompt
    assert "Summer morning → workout → lock in" in logged_in.get("/history").text
    assert "and more and more and more and more" not in logged_in.get("/history").text


def test_adjust_and_redo_prefills_the_prompt(logged_in, respx_mock):
    respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(10)))
    FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(10)})
    _build(logged_in, "rainy sunday, nostalgic")
    row_id = _playlist_rows()[0]["id"]
    assert ">rainy sunday, nostalgic</textarea>" in logged_in.get(f"/?redo={row_id}").text
    users.upsert("friend2", "Two", "r")
    with get_conn() as conn:
        other = conn.execute("INSERT INTO playlists (spotify_id, prompt, spotify_url, track_uris_json) VALUES "
                             "('friend2', 'secret', 'u', '[]')").lastrowid
    assert "secret" not in logged_in.get(f"/?redo={other}").text


def test_support_note_shows_a_calm_line_and_the_playlist_is_still_made(logged_in, respx_mock, briefs):
    briefs.answer = default_brief("Heavy day", support_note=True)
    respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(10)))
    sp = FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(10)})
    page = _build(logged_in, "I don't see the point of anything")
    assert sp.create.call_count == 1 and len(sp.added_uris()) == 10
    assert "findahelpline.com" in page.text and "you don't have to carry it alone" in page.text


def test_no_support_line_normally(logged_in, respx_mock):
    respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(10)))
    FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(10)})
    assert "findahelpline" not in _build(logged_in, "gym").text


def test_failed_brief_still_builds_and_says_so(logged_in, respx_mock, briefs):
    briefs.status = 500
    respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(10)))
    sp = FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(10)})
    page = _build(logged_in, "rainy sunday")
    assert sp.create.call_count == 1
    assert "read this request in detail, so it used your words as they are" in page.text


@pytest.mark.parametrize("length, status", [(2000, 303), (2001, 422)])
def test_prompts_up_to_2000_characters_are_accepted(logged_in, respx_mock, length, status):
    respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(10)))
    FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(10)})
    resp = logged_in.post("/playlist", data={"mood": "x" * length})
    assert resp.status_code == status
    if status == 303:
        finish_build(logged_in, resp)


def test_old_playlists_without_a_brief_still_render(logged_in):
    with get_conn() as conn:
        row_id = conn.execute("INSERT INTO playlists (spotify_id, prompt, spotify_url, track_uris_json) VALUES "
                              "('friend1', 'phase 9 prompt', 'u', '[]')").lastrowid
    page = logged_in.get(f"/playlists/{row_id}")
    assert page.status_code == 200 and "Moodify: phase 9 prompt" in page.text and "How Moodify read this" not in page.text


# --- live eval ----------------------------------------------------------------------------------------------

@pytest.mark.live
def test_live_eval_reads_at_least_90_percent_of_fixtures_right():
    """Gate. Runs every fixture through the real model (brief only, no playlists made)."""
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda f: (f, read_prompt(f[1], TASTE)), FIXTURES))
    passed = 0
    for (fixture_id, _, checks), brief in results:
        failed = _failures(brief, checks)
        passed += not failed
        print(f"{'ok  ' if not failed else 'FAIL'} {fixture_id}: {brief.title!r} sections="
              f"{[(s.label, s.energy, s.minutes, s.transition_in) for s in brief.sections]} total={brief.total_minutes}"
              f" rules={briefs_module.rule_list(brief.hard_rules)} familiarity={brief.familiarity} support={brief.support_note}"
              + (f"\n     failed: {failed}" if failed else ""))
    print(f"\n{passed}/{len(FIXTURES)} fixtures read right")
    assert passed >= 0.9 * len(FIXTURES)
