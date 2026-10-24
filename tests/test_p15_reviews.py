"""Phase 15: free-text reviews, the lessons reflection draws from them, consolidation, and "Fix it"."""
import json
import time

import httpx
import pytest

from src import memory, playlists, prompts, reflect, users
from src.db import get_conn
from src.memory import Consolidation, Draft, Merge, Operation
from src.spotify import auth
from src.spotify.client import Track

from .conftest import CHAT_URL, default_brief
from .test_p6_playlist import FakeSpotify, _completion, _songs, logged_in, user  # noqa: F401

WORKOUT_REVIEW = "the workout part was way too slow, rest was great"

THREE_PARTS = default_brief("Summer morning → workout → lock in", sections=[
    {"label": "Summer morning", "feeling": "warm", "energy": 4, "minutes": None, "activity": None, "transition_in": "blend"},
    {"label": "Workout", "feeling": "driving", "energy": 8, "minutes": 30, "activity": "workout", "transition_in": "blend"},
    {"label": "Lock in", "feeling": "focused", "energy": 5, "minutes": None, "activity": "coding", "transition_in": "cut"},
], tags=["workout", "focus"])


def _playlist(owner="friend1", brief=THREE_PARTS, n=6) -> int:
    tracks = [Track(f"spotify:track:{owner}-{i}", f"Song {i}", (f"Artist {i}",), 210_000) for i in range(n)]
    sections = [min(i // 2, 2) for i in range(n)]
    return playlists.insert(owner, "summer morning, then a 30 min workout, then lock in",
                            f"https://open.spotify.com/playlist/{owner}", tracks, json.dumps(brief), sections,
                            [4, 4, 7, 8, 5, 5][:n], ["opener", "closer", "bridge", "peak", "opener", "closer"][:n])


def _reply(payload: dict) -> httpx.Response:
    return httpx.Response(200, json={"id": "r", "object": "chat.completion", "created": 0, "model": "m", "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(payload)}}]})


class ChatMock:
    """Answers one kind of structured call (by its schema name) with `answer`, after `delay` seconds."""

    def __init__(self, respx_mock, schema: str, answer: dict, delay: float = 0.0):
        self.answer, self.delay, self.calls = answer, delay, []
        respx_mock.post(CHAT_URL, json__response_format__json_schema__name=schema).mock(side_effect=self)

    def __call__(self, request):
        self.calls.append(json.loads(request.content))
        time.sleep(self.delay)
        return _reply(self.answer)

    def prompt(self, n: int = -1) -> str:
        return prompts.sent_text(self.calls[n]["messages"])


def _lesson(title="Workout sections need more energy", tags=("workout", "energy"), kind="pitfall", scope="lasting",
            evidence="the workout part was way too slow", content="Keep workout sections at energy 8 or more.") -> dict:
    return {"title": title, "description": "In workout sections.", "content": content, "kind": kind, "scope": scope,
            "tags": list(tags), "about": "Workout section", "evidence": evidence}


def _wait_for(check, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def _row(playlist_id):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()


def _lessons(spotify_id="friend1", **where):
    clause = "".join(f" AND {k} = ?" for k in where)
    with get_conn() as conn:
        return conn.execute(f"SELECT * FROM lessons WHERE spotify_id = ?{clause} ORDER BY id",
                            (spotify_id, *where.values())).fetchall()


def _store(spotify_id, drafts, plan=None, source=None):
    plan = plan or Consolidation(operations=[memory._as_op(i, d, "ADD") for i, d in enumerate(
        [d for d in drafts if d.scope == "lasting"], start=1)], merges=[])
    with get_conn() as conn:
        return memory.store(conn, spotify_id, source, drafts, plan)


# --- saving a review --------------------------------------------------------------------------------------

def test_saving_answers_at_once_while_reflection_runs_in_the_background(logged_in, respx_mock):
    pid = _playlist()
    slow = ChatMock(respx_mock, "Reflection", {"verdict": "partly", "lessons": [_lesson()]}, delay=5)
    started = time.monotonic()
    resp = logged_in.post(f"/playlists/{pid}/review", data={"review": WORKOUT_REVIEW})
    assert time.monotonic() - started < 1
    assert resp.status_code == 303 and resp.headers["location"] == f"/playlists/{pid}#review"
    row = _row(pid)
    assert row["review"] == WORKOUT_REVIEW and row["reviewed_at"] and row["review_verdict"] is None
    page = logged_in.get(f"/playlists/{pid}").text
    assert "Review saved. Moodify is reading it now." in page and "Moodify is reading your review" in page
    _wait_for(lambda: _row(pid)["review_verdict"] == "partly")
    assert len(slow.calls) == 1


def test_editing_keeps_the_old_review_in_history(logged_in, respx_mock):
    pid = _playlist()
    ChatMock(respx_mock, "Reflection", {"verdict": "worked", "lessons": []})
    logged_in.post(f"/playlists/{pid}/review", data={"review": "loved it"})
    _wait_for(lambda: _row(pid)["review_verdict"] == "worked")
    logged_in.post(f"/playlists/{pid}/review", data={"review": "actually the ending dragged"})
    assert _row(pid)["review"] == "actually the ending dragged"
    assert [r["text"] for r in playlists.review_history(pid)] == ["loved it"]
    logged_in.post(f"/playlists/{pid}/review", data={"review": "actually the ending dragged"})  # unchanged
    assert len(playlists.review_history(pid)) == 1


def test_review_over_2000_chars_is_refused(logged_in, respx_mock):
    pid = _playlist()
    ChatMock(respx_mock, "Reflection", {"verdict": "worked", "lessons": []})
    assert logged_in.post(f"/playlists/{pid}/review", data={"review": "x" * 2001}).status_code == 422
    assert _row(pid)["review"] is None
    resp = logged_in.post(f"/playlists/{pid}/review", data={"review": "y" * 2000})
    assert resp.status_code == 303
    assert len(_row(pid)["review"]) == 2000


def test_empty_review_clears_it_and_its_lessons(logged_in, respx_mock):
    pid = _playlist()
    ChatMock(respx_mock, "Reflection", {"verdict": "partly", "lessons": [_lesson(), _lesson(
        title="Morning too slow", tags=("morning",), scope="this_playlist", evidence="rest was great")]})
    logged_in.post(f"/playlists/{pid}/review", data={"review": WORKOUT_REVIEW})
    _wait_for(lambda: len(_lessons()) == 2)
    resp = logged_in.post(f"/playlists/{pid}/review", data={"review": "   "})
    assert resp.status_code == 303
    row = _row(pid)
    assert row["review"] is None and row["reviewed_at"] is None and row["review_verdict"] is None
    assert _lessons() == []
    assert [r["text"] for r in playlists.review_history(pid)] == [WORKOUT_REVIEW]
    assert "Review deleted" in logged_in.get(f"/playlists/{pid}").text


def test_another_users_playlist_is_404(logged_in, respx_mock):
    users.upsert("friend2", "Friend Two", "refresh-2", auth.SCOPES)
    theirs = _playlist(owner="friend2")
    assert logged_in.post(f"/playlists/{theirs}/review", data={"review": "mine now"}).status_code == 404
    assert logged_in.post(f"/playlists/{theirs}/fix").status_code == 404
    assert _row(theirs)["review"] is None
    assert logged_in.post("/playlists/999/review", data={"review": "hi"}).status_code == 404


def test_reviews_get_the_same_tolerance_as_prompts(logged_in, respx_mock):
    pid = _playlist()
    chat = ChatMock(respx_mock, "Reflection", {"verdict": "worked", "lessons": []})
    review = "- intro 🔥🔥\n- workout lowkey mid ngl\n- lock in = perfect 👩‍💻"
    logged_in.post(f"/playlists/{pid}/review", data={"review": review})
    _wait_for(lambda: chat.calls)
    assert _row(pid)["review"] == review  # line breaks, slang and emoji (with their joiners) kept
    assert chat.prompt().endswith(f"Review: {review}")


# --- reflecting -------------------------------------------------------------------------------------------

def test_reflection_gets_the_whole_situation_and_stores_a_grounded_lesson(logged_in, respx_mock):
    pid = _playlist()
    with get_conn() as conn:
        conn.execute("INSERT INTO track_likes (spotify_id, uri, playlist_id) VALUES ('friend1', 'spotify:track:friend1-4', ?)", (pid,))
        conn.execute("UPDATE playlists SET rating = 6 WHERE id = ?", (pid,))
        job = conn.execute("INSERT INTO jobs (spotify_id, prompt, status, playlist_id) VALUES ('friend1', 'p', 'done', ?)",
                           (pid,)).lastrowid
        conn.execute("INSERT INTO job_events (job_id, kind, detail_json) VALUES (?, 'built', ?)", (job, json.dumps(
            {"suggested": 20, "tracks": 6, "known": 2, "targets": [15.0, 30.0, 15.0], "section_minutes": [7.0, 7.0, 7.0]})))
    chat = ChatMock(respx_mock, "Reflection", {"verdict": "partly", "lessons": [_lesson()]})
    logged_in.post(f"/playlists/{pid}/review", data={"review": WORKOUT_REVIEW})
    _wait_for(lambda: _row(pid)["review_verdict"] == "partly")

    prompt = chat.prompt()
    for part in ("Request: summer morning", "Sections, in order:", "Workout (starts at 7:00):",
                 "4. Song 3 by Artist 3 (3:30, energy 8, peak)", "5. Song 4 by Artist 4 (3:30, energy 5, opener) ♥ liked",
                 "Rating: 6/10.", "20 songs suggested, 6 used", "15 vs 7, 30 vs 7, 15 vs 7", f"Review: {WORKOUT_REVIEW}"):
        assert part in prompt, part
    [lesson] = _lessons()
    assert lesson["status"] == "active" and lesson["scope"] == "lasting" and lesson["source_playlist_id"] == pid
    assert "workout" in json.loads(lesson["tags_json"])
    assert lesson["evidence"] in WORKOUT_REVIEW and lesson["evidence"] == "the workout part was way too slow"
    page = logged_in.get(f"/playlists/{pid}").text
    assert "it partly worked" in page and "Workout sections need more energy" in page
    assert "from “the workout part was way too slow”" in page


@pytest.mark.parametrize("evidence, kept", [
    ("the workout part was way too slow", True),
    ("The Workout Part   was way too slow!", True),       # case, spacing, edge punctuation
    ("“rest was great”", True),                           # curly quotes
    ("the workout part ... rest was great", True),        # an elision, each part quoted
    ("the workout was too slow", False),                  # a paraphrase
    ("workout dragged", False),
    ("", False),
    ("...", False),
])
def test_grounding(evidence, kept):
    assert reflect.grounded(evidence, WORKOUT_REVIEW) is kept


def test_a_lesson_without_evidence_in_the_review_is_dropped(user, respx_mock):
    ChatMock(respx_mock, "Reflection", {"verdict": "partly", "lessons": [
        _lesson(), _lesson(title="Hates the morning part", evidence="the morning part was boring"),
        _lesson(title="Four", evidence="rest was great"), _lesson(title="Five", evidence="way too slow"),
    ]})
    result = reflect.reflect(WORKOUT_REVIEW, "Request: x")
    assert [l.title for l in result.lessons] == ["Workout sections need more energy", "Four", "Five"]  # ≤3, grounded


def test_a_failed_reflection_leaves_the_review_alone(logged_in, respx_mock):
    pid = _playlist()
    respx_mock.post(CHAT_URL, json__response_format__json_schema__name="Reflection").respond(500)
    logged_in.post(f"/playlists/{pid}/review", data={"review": WORKOUT_REVIEW})
    from src import jobs
    assert jobs.wait_idle(10)
    assert _row(pid)["review"] == WORKOUT_REVIEW and _row(pid)["review_verdict"] is None and _lessons() == []


def test_an_edit_during_reflection_wins(logged_in, respx_mock):
    pid = _playlist()
    ChatMock(respx_mock, "Reflection", {"verdict": "missed", "lessons": [_lesson()]}, delay=1)
    logged_in.post(f"/playlists/{pid}/review", data={"review": WORKOUT_REVIEW})
    playlists.set_review(pid, "friend1", "never mind, it was good")  # no reflection queued for this one
    from src import jobs
    assert jobs.wait_idle(10)
    assert _row(pid)["review_verdict"] is None and _lessons() == []


# --- consolidating ----------------------------------------------------------------------------------------

def test_newer_feedback_supersedes_older(user, respx_mock):
    old = Draft(**_lesson(title="Loves long intros", tags=("intros",), kind="preference", evidence="loved the long intros",
                          content="Pick songs with long intros."))
    [old_id] = _store("friend1", [old])
    chat = ChatMock(respx_mock, "Consolidation", {"operations": [{
        "new": 1, "op": "SUPERSEDE", "target": old_id, "title": "Skip long intros", "description": "Always.",
        "content": "Avoid songs with long intros.", "tags": ["intros"]}], "merges": []})
    new = Draft(**_lesson(title="Skip long intros", tags=("intros", "workout"), kind="preference",
                          evidence="skip long intros, they bore me", content="Avoid long intros."))
    plan = memory.consolidate("friend1", [new], source_playlist_id=None)
    assert f"id {old_id} [preference] Loves long intros" in chat.prompt() and "skip long intros, they bore me" in chat.prompt()
    _store("friend1", [new], plan)

    old_row, new_row = _lessons()
    assert old_row["status"] == "superseded" and old_row["superseded_by"] == new_row["id"]
    assert new_row["status"] == "active" and new_row["title"] == "Skip long intros"
    assert [r["id"] for r in memory.active("friend1")] == [new_row["id"]]


def test_update_and_noop(user, respx_mock):
    first = Draft(**_lesson())
    [lesson_id] = _store("friend1", [first])
    ChatMock(respx_mock, "Consolidation", {"operations": [
        {"new": 1, "op": "UPDATE", "target": lesson_id, "title": "Workout: energy 8+ and no slow intros",
         "description": "In workout sections.", "content": "Energy 8 or more, no slow intros.", "tags": ["workout"]},
        {"new": 2, "op": "NOOP", "target": lesson_id, "title": "x", "description": "x", "content": "x", "tags": ["workout"]},
    ], "merges": []})
    drafts = [Draft(**_lesson(title="No slow intros in workouts", evidence="slow intros")),
              Draft(**_lesson(title="Same again", evidence="too slow"))]
    _store("friend1", drafts, memory.consolidate("friend1", drafts, None))
    [row] = _lessons()
    assert row["title"] == "Workout: energy 8+ and no slow intros" and row["evidence"].startswith("slow intros / ")


def test_no_related_lessons_means_no_consolidation_call(user, respx_mock):
    _store("friend1", [Draft(**_lesson(tags=("sleep",)))])
    route = respx_mock.post(CHAT_URL, json__response_format__json_schema__name="Consolidation")
    plan = memory.consolidate("friend1", [Draft(**_lesson(tags=("workout",)))], None)
    assert route.call_count == 0 and [op.op for op in plan.operations] == ["ADD"]


def test_a_target_that_isnt_the_users_is_treated_as_add(user, respx_mock):
    users.upsert("friend2", "Friend Two", "refresh-2")
    [theirs] = _store("friend2", [Draft(**_lesson())])
    _store("friend1", [Draft(**_lesson())])
    bad = Consolidation(operations=[Operation(new=1, op="SUPERSEDE", target=theirs, title="t", description="d",
                                              content="c", tags=["workout"])], merges=[])
    _store("friend1", [Draft(**_lesson())], bad)
    assert _lessons("friend2")[0]["status"] == "active"
    assert len(_lessons("friend1", status="active")) == 2


def test_never_more_than_40_active(user, respx_mock):
    _store("friend1", [Draft(**_lesson(title=f"Lesson {i}", tags=(f"tag{i}",))) for i in range(40)])
    chat = ChatMock(respx_mock, "Consolidation", {"operations": [
        {"new": 1, "op": "ADD", "target": None, "title": "One more", "description": "d", "content": "c", "tags": ["new"]}],
        "merges": []})
    new = Draft(**_lesson(title="One more", tags=("new",)))
    plan = memory.consolidate("friend1", [new], None)
    assert "at most 40 may stay active" in chat.prompt() and "id 1 [pitfall] Lesson 0" in chat.prompt()  # sees all 40
    added = _store("friend1", [new], plan)
    active = memory.active("friend1")
    assert len(active) == 40 and added[0] in [r["id"] for r in active]  # the oldest expired, never the new one
    assert len(_lessons(status="expired")) == 1


def test_merging_keeps_40_active(user):
    ids = _store("friend1", [Draft(**_lesson(title=f"Lesson {i}", tags=(f"tag{i}",))) for i in range(40)])
    plan = Consolidation(operations=[memory._as_op(1, Draft(**_lesson(title="New")), "ADD")],
                         merges=[Merge(ids=ids[:2], kind="pitfall", title="Merged", description="d", content="c",
                                       tags=["tag0", "tag1"])])
    _store("friend1", [Draft(**_lesson(title="New"))], plan)
    assert len(memory.active("friend1")) == 40
    merged = _lessons(title="Merged")[0]
    assert all(r["superseded_by"] == merged["id"] for r in _lessons() if r["id"] in ids[:2])
    assert _lessons(status="expired") == []


# --- using lessons ----------------------------------------------------------------------------------------

def test_the_next_build_gets_active_lessons_not_superseded_ones(logged_in, respx_mock, briefs):
    [old] = _store("friend1", [Draft(**_lesson(title="Loves long intros", kind="preference", content="Long intros."))])
    with get_conn() as conn:
        conn.execute("UPDATE lessons SET status = 'superseded' WHERE id = ?", (old,))
    _store("friend1", [Draft(**_lesson(title="Skip long intros", kind="preference", content="No long intros."))])
    chat = respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(20)))
    FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(20)})
    from .conftest import finish_build
    finish_build(logged_in, logged_in.post("/playlist", data={"mood": "gym"}))

    brief_prompt = prompts.sent_text(json.loads(briefs.calls[-1].content)["messages"])
    suggest_prompt = prompts.sent_text(json.loads(chat.calls.last.request.content)["messages"])
    for prompt, request_marker in ((brief_prompt, "Request: gym"), (suggest_prompt, "Mood: gym")):
        per_user = prompt.split(request_marker)[0]
        assert "- Skip long intros: No long intros." in per_user
        assert "Loves long intros" not in prompt


def test_lessons_reach_builds_within_1500_chars(user):
    # Phase 16 replaced "every active lesson" with recall; the limit stayed
    _store("friend1", [Draft(**_lesson(title=f"Lesson {i}", content="x" * 300)) for i in range(30)])
    parts = memory.prompt_parts("friend1", {"workout"})
    assert len(parts.block) + len(parts.recalled) <= 1500
    assert parts.recalled.splitlines()[1].startswith("- Lesson 29")  # newest first


def test_fix_it_builds_a_linked_new_version_with_the_review_as_a_correction(logged_in, respx_mock, briefs):
    pid = _playlist()
    ChatMock(respx_mock, "Reflection", {"verdict": "partly", "lessons": [_lesson(), _lesson(
        title="Morning part too short", scope="this_playlist", tags=("morning",), evidence="rest was great",
        content="Give the morning part its full length.")]})
    with get_conn() as conn:
        conn.execute("INSERT INTO track_likes (spotify_id, uri, playlist_id) VALUES ('friend1', 'spotify:track:friend1-5', ?)", (pid,))
    logged_in.post(f"/playlists/{pid}/review", data={"review": WORKOUT_REVIEW})
    _wait_for(lambda: len(_lessons()) == 2)
    chat = respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(30)))
    sp = FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(30)})

    from .conftest import finish_build
    page = finish_build(logged_in, logged_in.post(f"/playlists/{pid}/fix"))
    new = max(r["id"] for r in playlists.recent("friend1", limit=-1))
    assert _row(new)["parent_id"] == pid and _row(pid)["parent_id"] is None
    assert "Version 2, fixed with your review of" in page.text and f'href="/playlists/{pid}"' in page.text
    assert json.loads(sp.create.calls.last.request.content)["name"].endswith("(v2)")
    assert "A fixed version" in logged_in.get(f"/playlists/{pid}").text

    brief_prompt = prompts.sent_text(json.loads(briefs.calls[-1].content)["messages"])
    correction = brief_prompt.split("Correction:\n", 1)[1]
    assert f"What the listener said after hearing it: {WORKOUT_REVIEW}" in correction
    assert "Earlier brief: " in correction and "Lock in" in correction
    assert "- Morning part too short: Give the morning part its full length." in correction
    suggest_prompt = prompts.sent_text(json.loads(chat.calls[0].request.content)["messages"])
    assert f"Feedback on an earlier version of this playlist:\nWhat the listener said after hearing it: {WORKOUT_REVIEW}" in suggest_prompt
    assert "Songs they liked in it (keep them if they still fit): Song 5 by Artist 5" in suggest_prompt


def test_fix_it_needs_a_review(logged_in):
    pid = _playlist()
    resp = logged_in.post(f"/playlists/{pid}/fix")
    assert resp.status_code == 400 and "Write a review first" in resp.text


def test_page_shows_the_review_box_and_fix_button_only_with_a_review(logged_in, respx_mock):
    pid = _playlist()
    page = logged_in.get(f"/playlists/{pid}").text
    assert f'<form id="review" class="mf-field" method="post" action="/playlists/{pid}/review">' in page
    assert 'maxlength="2000"' in page and "too much Drake" in page
    assert f'action="/playlists/{pid}/fix"' not in page
    ChatMock(respx_mock, "Reflection", {"verdict": "worked", "lessons": []})
    logged_in.post(f"/playlists/{pid}/review", data={"review": "<b>loved</b> it"})
    page = logged_in.get(f"/playlists/{pid}").text
    assert f'action="/playlists/{pid}/fix"' in page and "&lt;b&gt;loved&lt;/b&gt; it</textarea>" in page


def test_login_page_says_what_happens_to_reviews(test_settings):
    from fastapi.testclient import TestClient
    from src.db import init_db
    from src.main import app
    init_db()
    with TestClient(app) as client:
        page = client.get("/login").text
    assert "When you write a review of a playlist, it's sent to OpenAI" in page
    assert "kept until you delete them or disconnect" in page


# --- storage ----------------------------------------------------------------------------------------------

def test_disconnect_deletes_reviews_history_and_lessons(logged_in, respx_mock):
    pid = _playlist()
    ChatMock(respx_mock, "Reflection", {"verdict": "partly", "lessons": [_lesson()]})
    logged_in.post(f"/playlists/{pid}/review", data={"review": "first take"})
    logged_in.post(f"/playlists/{pid}/review", data={"review": WORKOUT_REVIEW})
    _wait_for(lambda: len(_lessons()) == 1)
    logged_in.post("/disconnect")
    with get_conn() as conn:
        for table in ("playlists", "review_history", "lessons"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table


def test_deleting_a_playlist_deletes_its_lessons_and_unlinks_its_fixes(user):
    pid = _playlist()
    child = playlists.insert("friend1", "p", "u", [], parent_id=pid)
    _store("friend1", [Draft(**_lesson())], source=pid)
    with get_conn() as conn:
        conn.execute("DELETE FROM playlists WHERE id = ?", (pid,))
    assert _lessons() == [] and _row(child)["parent_id"] is None


def test_the_database_refuses_unknown_verdicts_kinds_and_statuses(user):
    import sqlite3
    pid = _playlist()
    with pytest.raises(sqlite3.IntegrityError), get_conn() as conn:
        conn.execute("UPDATE playlists SET review_verdict = 'meh' WHERE id = ?", (pid,))
    [lesson] = _store("friend1", [Draft(**_lesson())])
    for column, value in (("kind", "vibe"), ("status", "gone"), ("scope", "forever")):
        with pytest.raises(sqlite3.IntegrityError), get_conn() as conn:
            conn.execute(f"UPDATE lessons SET {column} = ? WHERE id = ?", (value, lesson))


# --- live -------------------------------------------------------------------------------------------------

# One listener with steady dislikes, each said in at least two reviews: Drake, slow starts in high-energy
# playlists, long intros, sad acoustic ballads. For each fixture, the lessons come from the other four reviews.
LIVE_TASTE = ("Favourite genres: hip hop, r&b, pop, reggaeton. Artists they love: Drake, The Weeknd, Travis Scott, SZA, "
              "Bad Bunny. Preferred energy: high. Most played artists lately: Drake, Travis Scott, Future, The Weeknd, "
              "21 Savage. On repeat lately: Rich Flex by Drake; God's Plan by Drake; SICKO MODE by Travis Scott.")
LIVE_FIXTURES = [
    ("gym session, 30 min", "first three songs were way too slow for the gym and there was too much Drake"),
    ("running in the morning", "the long intros killed my pace, and why so much Drake again"),
    ("sunday cleaning the flat", "too many sad acoustic ballads, and the intros take forever"),
    ("pregame with friends", "slow start, needs to go hard from track one. less sad acoustic stuff"),
    ("late night drive", "please stop with the Drake, and I skip anything with a long intro"),
]


@pytest.mark.live
def test_live_lessons_from_other_reviews_make_better_playlists():
    """Gate (on demand). Reflects on each fixture review, then builds every fixture's prompt (brief + songs, no
    Spotify) with and without the lasting lessons from the other four reviews. A separate judge call says which
    song list better respects the fixture's own review. With-lessons should win at least 4 of 5."""
    import random
    from concurrent.futures import ThreadPoolExecutor

    from openai import BadRequestError, OpenAI
    from pydantic import BaseModel

    from src.brief import read_prompt
    from src.core.config import get_settings
    from src.suggest import suggest_songs

    class Verdict(BaseModel):
        better: str  # "A", "B" or "tie"
        reason: str

    def lessons_of(i):
        result = reflect.reflect(LIVE_FIXTURES[i][1], f"Request: {LIVE_FIXTURES[i][0]}")
        return [d for d in (result.lessons if result else []) if d.scope == "lasting"]

    with ThreadPoolExecutor(max_workers=5) as pool:
        drafts = list(pool.map(lessons_of, range(5)))
    for (prompt, review), found in zip(LIVE_FIXTURES, drafts):
        print(f"{review!r} -> {[(d.title, d.kind, d.tags) for d in found]}")

    def build(prompt, lessons):
        block = ""
        if lessons:
            block = "What Moodify learned from the listener's reviews:\n" + memory.render(
                [{"title": d.title, "content": d.content} for d in lessons])
        brief = read_prompt(prompt, LIVE_TASTE, memory=block)
        return suggest_songs(prompt, LIVE_TASTE, n=12, brief=brief, memory=block)

    settings = get_settings()
    client = OpenAI(api_key=settings.OPENAI_API_KEY.get_secret_value(), timeout=120)

    def judge(prompt, review, with_lessons, without):
        lists = [("with", with_lessons), ("without", without)]
        random.Random(prompt).shuffle(lists)
        listing = "\n\n".join(f"Playlist {label}:\n" + "\n".join(f"- {s.title} by {s.artist}" for s in songs)
                              for label, (_, songs) in zip("AB", lists))
        messages = [
            {"role": "system", "content": "You compare two playlists for one listener. Judge only by how well each "
             "respects what the listener said in their review of an earlier playlist. Answer A, B or tie."},
            {"role": "user", "content": f"Request: {prompt}\n\nTheir review of an earlier playlist: {review}\n\n{listing}"},
        ]
        try:
            answer = client.chat.completions.parse(model=settings.OPENAI_MODEL, messages=messages,
                                                   response_format=Verdict, temperature=0)
        except BadRequestError:  # some models only take their default temperature
            answer = client.chat.completions.parse(model=settings.OPENAI_MODEL, messages=messages, response_format=Verdict)
        verdict = answer.choices[0].message.parsed
        winner = dict(zip("AB", (name for name, _ in lists))).get(verdict.better.strip().upper()[:1], "tie")
        return winner, verdict.reason

    def run(i):
        prompt, review = LIVE_FIXTURES[i]
        others = [d for j, found in enumerate(drafts) if j != i for d in found]
        with ThreadPoolExecutor(max_workers=2) as inner:
            with_lessons, without = inner.map(lambda lessons: build(prompt, lessons), (others, []))
        return (prompt, with_lessons, without, *judge(prompt, review, with_lessons, without))

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(run, range(5)))
    wins = 0
    for prompt, with_lessons, without, winner, reason in results:
        wins += winner == "with"
        print(f"\n{prompt}: winner={winner} ({reason})")
        print("  with:   " + "; ".join(f"{s.title} by {s.artist}" for s in with_lessons))
        print("  without:" + "; ".join(f"{s.title} by {s.artist}" for s in without))
    print(f"\nwith lessons won {wins}/5")
    assert wins >= 4
