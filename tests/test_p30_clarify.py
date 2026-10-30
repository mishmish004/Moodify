"""Phase 30: one question before building when the prompt pulls against a lesson (Meta's PAHF, step 1).
The answer keeps to the lesson, goes with the prompt this time, or retires the lesson; no answer keeps to it."""
import json
import re
import time

import pytest

from src import clarify, jobs, memory, pages, prompts, reflect, users
from src.brief import SYSTEM_PROMPT, Brief
from src.db import get_conn
from src.memory import Draft

from .conftest import CHAT_URL, default_brief, finish_build
from .test_p6_playlist import FakeSpotify, _completion, _songs, logged_in, user  # noqa: F401
from .test_p15_reviews import _lesson, _lessons, _store
from .test_p16_memory import _playlist

ASK = {"lesson": "Workout energy 8+", "question": "Last time you said workouts need energy 8 or more. Keep it that high today?",
       "keep": "Yes, keep it high", "this_time": "Go easier this time", "from_now_on": "Easier workouts from now on"}
MOOD = "workout but kinda tired today"


@pytest.fixture
def clash(logged_in, respx_mock, briefs, monkeypatch):
    """friend1 remembers that workouts need energy 8+ (a preference, so it's in the memory block) and that sleep
    music was too busy; the brief call asks about the first. Song search finds everything."""
    monkeypatch.setattr(clarify, "WAIT_SECONDS", 10)
    ids = _store("friend1", [
        Draft(**_lesson(title="Workout energy 8+", kind="preference", content="Keep workouts at energy 8 or more.")),
        Draft(**_lesson(title="Sleep music too busy", tags=("sleep",), content="Sparser songs for sleep.")),
    ])
    briefs.answer = default_brief("Tired but training", tags=["workout"], clarify=ASK)
    chat = respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(20)))
    FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(20)})
    return {"client": logged_in, "briefs": briefs, "chat": chat, "lesson": ids[0], "other": ids[1]}


def _start(client, mood=MOOD) -> str:
    resp = client.post("/playlist", data={"mood": mood})
    assert resp.status_code == 303
    return resp.headers["location"]


def _asked(client, url, timeout=10.0):
    """The job page once its question shows."""
    deadline = time.monotonic() + timeout
    while True:
        page = client.get(url)
        assert page.status_code == 200, f"the build ended before asking: {page.status_code}"
        if 'data-state="open"' in page.text:
            return page
        assert time.monotonic() < deadline, page.text
        time.sleep(0.05)


def _done(client, url):
    """The page a running build ends on: the playlist, or why it failed."""
    class Started:  # what POST /playlist answered
        status_code, headers = 303, {"location": url}
    return finish_build(client, Started())


def _job(url):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (int(url.rsplit("/", 1)[1]),)).fetchone()


def _last_job_id() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT max(id) FROM jobs").fetchone()[0]


def _stored_brief() -> dict:
    with get_conn() as conn:
        return json.loads(conn.execute("SELECT brief_json FROM playlists ORDER BY id DESC LIMIT 1").fetchone()[0])


def _request_part(call) -> str:
    return json.loads(call.content)["messages"][-1]["content"]


def _system_part(call) -> str:
    return json.loads(call.content)["messages"][0]["content"]


# --- asking ---------------------------------------------------------------------------------------------

def test_a_clash_shows_one_question_with_three_answers(clash):
    client = clash["client"]
    url = _start(client)
    page = _asked(client, url).text
    assert ASK["question"] in page and "Workout energy 8+" in page
    assert page.count('name="choice"') == 3
    for value, words in (("keep", ASK["keep"]), ("this_time", ASK["this_time"]), ("from_now_on", ASK["from_now_on"])):
        assert re.search(rf'value="{value}">{re.escape(words)}</button>', page)
    assert f'action="{url}/answer"' in page and "data-countdown" in page
    assert "One question for you, below" in page
    # Without JavaScript the page doesn't reload under someone choosing an answer
    refresh = int(re.search(r'http-equiv="refresh" content="(\d+)"', page).group(1))
    assert 3 < refresh <= clarify.WAIT_SECONDS + 1
    client.post(f"{url}/answer", data={"choice": "keep"})
    _done(client, url)


def test_keep_builds_as_read_with_the_lesson(clash):
    client, briefs = clash["client"], clash["briefs"]
    url = _start(client)
    _asked(client, url)
    resp = client.post(f"{url}/answer", data={"choice": "keep"})
    assert resp.status_code == 303 and resp.headers["location"] == url
    page = _done(client, url)
    assert page.status_code == 200 and "Tired but training" in page.text
    assert len(briefs.calls) == 1                                # nothing read again
    assert _job(url)["answer"] == "keep"
    assert _lessons(id=clash["lesson"])[0]["status"] == "active"
    events = jobs.events(_job(url)["id"])
    assert any(e["kind"] == "question" and e["lesson"] == clash["lesson"] for e in events)
    assert any(e["kind"] == "answer" and e["answer"] == "keep" and e["said"] == ASK["keep"] for e in events)


def test_no_answer_in_time_goes_on_with_the_lesson(clash, monkeypatch):
    monkeypatch.setattr(clarify, "WAIT_SECONDS", 0.3)
    client, briefs = clash["client"], clash["briefs"]
    started = time.monotonic()
    page = finish_build(client, client.post("/playlist", data={"mood": MOOD}))
    assert page.status_code == 200 and time.monotonic() - started >= 0.3
    job = _job(f"/jobs/{_last_job_id()}")
    assert job["status"] == "done" and job["answer"] == clarify.NO_ANSWER
    assert len(briefs.calls) == 1
    assert _lessons(id=clash["lesson"])[0]["status"] == "active"
    # The lesson reached the song picker: it's a preference, in the per-user part
    assert "Workout energy 8+" in _system_part(clash["chat"].calls[0].request)


def test_this_time_reads_the_prompt_again_without_the_lesson(clash):
    client, briefs, chat = clash["client"], clash["briefs"], clash["chat"]
    url = _start(client)
    _asked(client, url)
    briefs.answer = default_brief("Easy session", tags=["workout"], sections=[
        {"label": "Easy", "feeling": "gentle", "energy": 4, "minutes": None, "activity": "workout", "transition_in": "blend"}])
    client.post(f"{url}/answer", data={"choice": "this_time"})
    page = _done(client, url)
    assert page.status_code == 200 and "Easy session" in page.text
    assert len(briefs.calls) == 2
    again = _request_part(briefs.calls[1])
    assert 'They answered: "Go easier this time"' in again and "not with their earlier lesson \"Workout energy 8+\"" in again
    assert "from now on" not in again
    picker = prompts.sent_text(json.loads(chat.calls[0].request.content)["messages"])
    assert 'They answered: "Go easier this time"' in picker  # the song picker hears it too
    stored = _stored_brief()
    assert stored["sections"][0]["energy"] == 4
    assert _lessons(id=clash["lesson"])[0]["status"] == "active"  # only this once


def test_from_now_on_retires_the_lesson(clash):
    client, briefs, chat = clash["client"], clash["briefs"], clash["chat"]
    url = _start(client)
    _asked(client, url)
    client.post(f"{url}/answer", data={"choice": "from_now_on"})
    _done(client, url)
    lesson = _lessons(id=clash["lesson"])[0]
    assert lesson["status"] == "superseded" and lesson["superseded_by"] is None  # kept as the audit trail
    assert _lessons(id=clash["other"])[0]["status"] == "active"
    assert "They want it that way from now on." in _request_part(briefs.calls[1])
    # Gone from the memory block of the second read and of the song picker's prompt
    assert "- Workout energy 8+: Keep workouts" not in _system_part(briefs.calls[1])
    assert "- Workout energy 8+: Keep workouts" not in _system_part(chat.calls[0].request)
    assert "Workout energy 8+" not in client.get("/memory").text


def test_a_question_about_a_lesson_not_in_memory_isnt_asked(clash):
    clash["briefs"].answer = default_brief("Tired but training", clarify={**ASK, "lesson": "Something never said"})
    url = _start(clash["client"])
    page = _done(clash["client"], url)
    assert page.status_code == 200
    assert _job(url)["question_json"] is None and not any(e["kind"] == "question" for e in jobs.events(_job(url)["id"]))


def test_the_lesson_title_matches_loosely():
    assert memory._plain("  workout ENERGY 8+. ") == memory._plain("Workout energy 8+")


def test_more_like_this_never_asks(clash):
    seed = _playlist(prompt="gym", rating=8)
    page = finish_build(clash["client"], clash["client"].post(f"/playlists/{seed}/more"))
    assert page.status_code == 200
    assert _job(f"/jobs/{_last_job_id()}")["question_json"] is None


def test_a_build_without_a_job_never_waits(clash):
    # Scripts and live tests build with no job page to ask on
    from src import playlist
    started = time.monotonic()
    playlist.build(users.get("friend1"), MOOD)
    assert time.monotonic() - started < clarify.WAIT_SECONDS


# --- the answer route -------------------------------------------------------------------------------------

def test_answer_route_guards(clash):
    client = clash["client"]
    url = _start(client)
    _asked(client, url)
    assert client.post(f"{url}/answer", data={"choice": "maybe"}).status_code == 422
    assert client.post("/jobs/999999/answer", data={"choice": "keep"}).status_code == 404
    with get_conn() as conn:  # someone else's build
        conn.execute("INSERT INTO users (user_id, display_name, refresh_token) VALUES ('other', 'O', 'r')")
        theirs = conn.execute("INSERT INTO jobs (user_id, prompt) VALUES ('other', 'x')").lastrowid
    assert client.post(f"/jobs/{theirs}/answer", data={"choice": "keep"}).status_code == 404
    assert client.post(f"{url}/answer", data={"choice": "this_time"}).status_code == 303
    client.post(f"{url}/answer", data={"choice": "from_now_on"})  # a second answer changes nothing
    _done(client, url)
    assert _job(url)["answer"] == "this_time"
    assert _lessons(id=clash["lesson"])[0]["status"] == "active"


def test_an_answer_after_the_build_went_on_changes_nothing(clash, monkeypatch):
    monkeypatch.setattr(clarify, "WAIT_SECONDS", 0.2)
    client = clash["client"]
    finish_build(client, client.post("/playlist", data={"mood": MOOD}))
    job_id = _last_job_id()
    assert client.post(f"/jobs/{job_id}/answer", data={"choice": "from_now_on"}).status_code == 303
    assert _job(f"/jobs/{job_id}")["answer"] == clarify.NO_ANSWER
    assert _lessons(id=clash["lesson"])[0]["status"] == "active"


def test_the_page_says_what_was_answered(clash):
    client = clash["client"]
    url = _start(client)
    _asked(client, url)
    client.post(f"{url}/answer", data={"choice": "this_time"})
    job = _job(url)
    shown = clarify.for_page(job)
    assert shown["open"] is False and shown["said"] == ASK["this_time"]
    _done(client, url)


# --- the brief call and what's kept ------------------------------------------------------------------------

def test_the_static_prompt_teaches_when_to_ask():
    assert "- clarify: null, unless" in SYSTEM_PROMPT
    examples = [Brief.model_validate_json(e) for e in re.findall(r"^Brief: (\{.*\})$", SYSTEM_PROMPT, re.M)]
    asking = [b for b in examples if b.clarify]
    assert len(asking) == 1 and asking[0].clarify.lesson == "Workout energy 8+"
    assert all(b.clarify is None for b in examples if b is not asking[0])


def test_the_question_is_never_stored_with_the_playlist(clash):
    client = clash["client"]
    url = _start(client)
    _asked(client, url)
    client.post(f"{url}/answer", data={"choice": "keep"})
    _done(client, url)
    assert "clarify" not in _stored_brief()
    assert "clarify" not in Brief.model_validate(default_brief("x", clarify=ASK)).model_dump_json()


def test_the_step_list_shows_the_question_under_reading_the_prompt():
    steps = pages.build_steps(clarify.STEP)
    assert [s["state"] for s in steps] == ["done", "now", "wait", "wait", "wait"]
    assert steps[1]["status"] == "One question for you, below"
    assert pages.build_steps("Reading your prompt again")[1]["state"] == "now"
    assert pages.build_steps(clarify.STEP, asking=False)[1]["status"] == "In progress"  # answered, going on


def test_reflection_hears_what_was_asked_and_answered(clash):
    client = clash["client"]
    url = _start(client)
    _asked(client, url)
    client.post(f"{url}/answer", data={"choice": "this_time"})
    _done(client, url)
    log = reflect.build_log(_job(url)["playlist_id"], "friend1")
    assert f'Moodify asked "{ASK["question"]}" and the listener answered "{ASK["this_time"]}"' in log


# --- live --------------------------------------------------------------------------------------------------

LIVE_LESSONS = ("What Moodify knows from the listener's feedback:\nPreferences from their reviews:\n"
                "- Workout energy 8+: Keep workout sections at energy 8 or more from the first song.\n"
                "- Less Drake: At most one Drake song per playlist.")
LIVE_CASES = [  # request, the lesson it should ask about (None: no question)
    ("gym session, running on 4 hours of sleep", "Workout energy 8+"),
    ("drake vibes tonight", "Less Drake"),
    ("gym, let's go", None),
    ("I know I said workouts need to be intense, but today I just want an easy stretch", None),
    ("rainy day reading", None),
    ("cooking dinner, something warm", None),
]


@pytest.mark.live
def test_live_the_model_asks_only_on_a_real_clash():
    from src.brief import read_prompt
    right = 0
    for request, lesson in LIVE_CASES:
        brief = read_prompt(request, "Listens to hip hop and R&B: Drake, Don Toliver, Gunna.", memory=LIVE_LESSONS)
        asked = brief.clarify.lesson if brief.clarify else None
        print(f"{request!r}: asked about {asked!r}" + (f" — {brief.clarify.question}" if brief.clarify else ""))
        right += (memory._plain(asked) if asked else None) == (memory._plain(lesson) if lesson else None)
    assert right >= len(LIVE_CASES) - 1
