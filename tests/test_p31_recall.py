"""Phase 31: lessons recalled by meaning as well as by tags (Google's ReasoningBank), with one embeddings call
per recall, vectors kept with the lessons, and the tags to fall back on."""
import json

import httpx
import pytest

from src import embeddings, jobs, memory, reflect
from src.core.config import get_settings
from src.db import get_conn, init_db
from src.memory import Draft

from .conftest import CHAT_URL, default_brief, finish_build
from .test_p6_playlist import FakeSpotify, _completion, _songs, logged_in, user  # noqa: F401
from .test_p15_reviews import ChatMock, _lesson, _lessons, _playlist, _store
from .test_p16_memory import _brief

EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"

# What the fake model "understands": a text is close to another when they share a concept
CONCEPTS = {
    "workout": ("workout", "gym", "leg day", "deadlift", "lifting", "5k"),
    "sleep": ("sleep", "bedtime", "drift off"),
    "rain": ("rain", "rainy"),
    "intros": ("intro", "opening", "slow start"),
}


def _vector(text: str) -> list[float]:
    text = text.casefold()
    vector = [0.0] * embeddings.DIMENSIONS
    for i, words in enumerate(CONCEPTS.values()):
        vector[i] = float(any(w in text for w in words))
    vector[100] = 0.3  # what every text has in common
    return vector


class FakeEmbeddings:
    """The embeddings endpoint: one vector per input, from CONCEPTS. `status` other than 200 fails every call."""

    def __init__(self, respx_mock):
        self.status, self.calls = 200, []
        self.route = respx_mock.post(EMBEDDINGS_URL).mock(side_effect=self)

    def __call__(self, request):
        body = json.loads(request.content)
        self.calls.append(body)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "mocked failure"}})
        return httpx.Response(200, json={"object": "list", "model": body["model"], "usage": {
            "prompt_tokens": 7, "total_tokens": 7}, "data": [
                {"object": "embedding", "index": i, "embedding": _vector(text)} for i, text in enumerate(body["input"])]})

    def inputs(self, n: int = -1) -> list[str]:
        return self.calls[n]["input"]


@pytest.fixture
def meaning(user, respx_mock, monkeypatch):
    """Embeddings on (conftest turns them off for every other test), answered by FakeEmbeddings. Comes after
    logged_in in a test's arguments: logging in clears the mocked routes."""
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
    get_settings.cache_clear()
    yield FakeEmbeddings(respx_mock)
    get_settings.cache_clear()


def _draft(title, tags, description, kind="pitfall") -> Draft:
    return Draft(**{**_lesson(title=title, tags=tags, kind=kind), "description": description})


def _fixture(user_id="friend1") -> dict[str, int]:
    drafts = [
        _draft("Workout energy 8+", ("workout",), "For workout and gym sections."),
        _draft("Sleep music too busy", ("sleep",), "For bedtime playlists."),
        _draft("Rainy days too sad", ("cosy",), "For rainy-day playlists."),
    ]
    return dict(zip([d.title for d in drafts], _store(user_id, drafts)))


def _titles(rows) -> set[str]:
    return {r["title"] for r in rows}


# --- recall ------------------------------------------------------------------------------------------------

def test_a_request_without_the_tag_words_recalls_by_meaning(meaning):
    _fixture()
    wanted = memory.words("leg day, 40 minutes")
    assert memory.recall("friend1", wanted) == []  # no tag in common: Phase 16 found nothing
    assert _titles(memory.recall("friend1", wanted, about=["leg day, 40 minutes"])) == {"Workout energy 8+"}
    # "rainy" isn't one of that lesson's tags either
    assert _titles(memory.recall("friend1", memory.words("pouring rain, coffee"), about=["pouring rain, coffee"])) == {
        "Rainy days too sad"}


def test_an_unrelated_request_recalls_nothing(meaning):
    _fixture()
    assert memory.recall("friend1", memory.words("sunday cooking"), about=["sunday cooking"]) == []


def test_tags_still_count_alongside_meaning(meaning):
    _fixture()
    # "homework" means focus through memory.TAG_GROUPS; the fake model sees nothing in it
    ids = _store("friend1", [_draft("Hard cut into focus", ("focus",), "When switching into studying.")])
    rows = memory.recall("friend1", memory.words("homework"), about=["homework"])
    assert [r["id"] for r in rows] == ids


def test_lessons_that_fit_both_ways_come_first(meaning):
    ids = _store("friend1", [
        _draft("Gym needs no warm-up", ("warmup",), "For the gym."),           # by meaning only
        _draft("Workout energy 8+", ("workout",), "For workout sections."),      # by both
        _draft("Short songs when running", ("workout",), "For runs and jogs."),  # by tag only
    ])
    rows = memory.recall("friend1", {"workout"}, about=["workout at the gym"])
    assert [r["id"] for r in rows] == [ids[1], ids[0], ids[2]]


def test_skipped_lessons_stay_out(meaning):
    ids = _fixture()
    rows = memory.recall("friend1", {"workout"}, about=["leg day"], skip=[ids["Workout energy 8+"]])
    assert rows == []


def test_a_brief_is_asked_about_as_a_whole_and_per_section():
    brief = _brief("workout", "focus", sections=[
        {"label": "Leg day", "feeling": "heavy", "energy": 8, "minutes": 30, "activity": "workout", "transition_in": "blend"},
        {"label": "Lock in", "feeling": "steady", "energy": 5, "minutes": None, "activity": "coding", "transition_in": "cut"}])
    assert memory.brief_queries(brief) == ["Test. workout, focus", "Leg day: heavy (workout)", "Lock in: steady (coding)"]
    one = _brief("rainy", sections=[{"label": "Rain", "feeling": "cosy", "energy": 3, "minutes": None, "activity": None,
                                      "transition_in": "blend"}])
    assert memory.brief_queries(one) == ["Test. rainy. Rain: cosy"]


# --- the embeddings calls ---------------------------------------------------------------------------------------

def test_one_call_per_recall_and_vectors_are_kept(meaning):
    ids = _fixture()
    memory.recall("friend1", set(), about=["leg day"])
    assert len(meaning.calls) == 1
    assert meaning.inputs() == ["Workout energy 8+. For workout and gym sections.", "Sleep music too busy. For bedtime playlists.",
                                "Rainy days too sad. For rainy-day playlists.", "leg day"]
    assert meaning.calls[0]["dimensions"] == 512 and meaning.calls[0]["model"] == "text-embedding-3-large"
    stored = {r["id"]: r for r in _lessons()}
    assert all(stored[i]["embedded_with"] == "text-embedding-3-large/512" for i in ids.values())
    assert len(embeddings.unpack(stored[ids["Workout energy 8+"]]["embedding"])) == 512
    memory.recall("friend1", set(), about=["deadlift day"])
    assert len(meaning.calls) == 2 and meaning.inputs() == ["deadlift day"]  # only the query this time


def test_no_lessons_no_call(meaning):
    assert memory.recall("friend1", {"workout"}, about=["leg day"]) == []
    assert meaning.calls == []


def test_a_rewritten_lesson_or_another_model_is_embedded_again(meaning, monkeypatch):
    ids = _fixture()
    embeddings.fill("friend1")
    assert len(meaning.inputs()) == 3
    plan = memory.Consolidation(operations=[memory.Operation(
        new=1, op="UPDATE", target=ids["Sleep music too busy"], title="Sleep music too busy",
        description="For bedtime and naps.", content="Sparser.", tags=["sleep"])], merges=[])
    _store("friend1", [_draft("x", ("sleep",), "y")], plan)
    embeddings.fill("friend1")
    assert meaning.inputs() == ["Sleep music too busy. For bedtime and naps."]
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
    get_settings.cache_clear()
    embeddings.fill("friend1")
    assert len(meaning.inputs()) == 3 and meaning.calls[-1]["model"] == "text-embedding-3-small"


def test_a_vector_for_text_that_changed_meanwhile_isnt_kept(meaning):
    ids = _fixture()
    rows = embeddings._active("friend1")
    with get_conn() as conn:  # consolidation rewrites a lesson while it's being embedded
        conn.execute("UPDATE lessons SET description = 'Changed.' WHERE id = ?", (ids["Rainy days too sad"],))
    embeddings._save("friend1", rows, [[1.0] + [0.0] * 511] * len(rows))
    kept = {r["id"]: r["embedding"] for r in _lessons()}
    assert kept[ids["Rainy days too sad"]] is None and kept[ids["Workout energy 8+"]] is not None


def test_a_failed_call_falls_back_to_tags(meaning):
    _fixture()
    meaning.status = 500
    assert memory.recall("friend1", memory.words("leg day"), about=["leg day"]) == []
    assert _titles(memory.recall("friend1", memory.words("gym"), about=["gym"])) == {"Workout energy 8+"}
    assert len(meaning.calls) == 4  # each recall tried twice: a 5xx gets one more try
    assert all(r["embedding"] is None for r in _lessons())


def test_no_model_no_call(meaning, monkeypatch):
    monkeypatch.setenv("OPENAI_EMBEDDING_MODEL", "")
    get_settings.cache_clear()
    _fixture()
    assert _titles(memory.recall("friend1", memory.words("gym"), about=["gym"])) == {"Workout energy 8+"}
    assert meaning.calls == []


def test_the_median_rule_needs_enough_lessons():
    assert embeddings.MEDIAN_OF >= 5 and 0 < embeddings.ABOVE_MEDIAN < embeddings.MIN_SIMILARITY


# --- consolidation and reflection ------------------------------------------------------------------------------

def test_consolidation_sees_a_lesson_close_in_meaning_without_a_shared_tag(meaning):
    old = _store("friend1", [_draft("Loves long intros", ("focus",), "Whenever a song has a long intro.", "preference")])
    new = _draft("Skip slow openings", ("openings",), "When songs have a slow start.")
    assert [r["id"] for r in memory.candidates("friend1", [new], None)] == old


def test_without_embeddings_that_lesson_isnt_seen(user):
    _store("friend1", [_draft("Loves long intros", ("focus",), "Whenever a song has a long intro.", "preference")])
    assert memory.candidates("friend1", [_draft("Skip slow openings", ("openings",), "When songs start slowly.")], None) == []


def test_reflection_embeds_the_new_lessons(meaning, respx_mock):
    pid = _playlist()
    ChatMock(respx_mock, "Reflection", {"verdict": "partly", "lessons": [_lesson()]})
    with get_conn() as conn:
        conn.execute("UPDATE playlists SET review = ? WHERE id = ?", ("the workout part was way too slow", pid))
    reflect.run(pid, "friend1", "the workout part was way too slow")
    stored = _lessons()
    assert len(stored) == 1 and stored[0]["embedded_with"] == "text-embedding-3-large/512"


# --- in a build ---------------------------------------------------------------------------------------------------

def test_a_build_recalls_by_meaning_and_logs_the_calls(logged_in, meaning, respx_mock, briefs):
    _fixture()
    briefs.answer = default_brief("Leg day", tags=["strength"])
    chat = respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(20)))
    FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(20)})
    page = finish_build(logged_in, logged_in.post("/playlist", data={"mood": "leg day, 40 minutes"}))
    assert page.status_code == 200
    assert "- Workout energy 8+:" in json.loads(briefs.calls[0].content)["messages"][-1]["content"]
    assert "- Workout energy 8+:" in json.loads(chat.calls[0].request.content)["messages"][-1]["content"]
    assert "Sleep music" not in json.loads(chat.calls[0].request.content)["messages"][-1]["content"]
    with get_conn() as conn:
        job_id = conn.execute("SELECT max(id) FROM jobs").fetchone()[0]
    calls = [e for e in jobs.events(job_id) if e["kind"] == "embed"]
    assert len(calls) == 2 and all(e["ok"] for e in calls)  # one for the prompt, one for the brief


def test_a_build_goes_on_when_embeddings_fail(logged_in, meaning, respx_mock, briefs):
    _fixture()
    meaning.status = 401
    respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(20)))
    FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(20)})
    page = finish_build(logged_in, logged_in.post("/playlist", data={"mood": "gym, 40 minutes"}))
    assert page.status_code == 200
    assert "- Workout energy 8+:" in json.loads(briefs.calls[0].content)["messages"][-1]["content"]  # by tag


def test_disconnect_takes_the_vectors_with_the_lessons(logged_in, meaning):
    _fixture()
    embeddings.fill("friend1")
    logged_in.post("/disconnect")
    with get_conn() as conn:
        assert conn.execute("SELECT count(*) FROM lessons WHERE embedding IS NOT NULL").fetchone()[0] == 0


# --- live ----------------------------------------------------------------------------------------------------

LIVE_LESSONS = {  # title: when it applies
    "Workout energy 8+": "For workout and gym sections.",
    "No warm-up songs at the gym": "When the playlist is for lifting or the gym.",
    "Hard cut into focus works": "When switching into studying or coding.",
    "Sleep music too busy": "For bedtime and sleep playlists.",
    "Skip long intros": "Whenever choosing songs.",
    "Less Drake": "In any playlist.",
    "Rainy-day playlists too sad": "For rainy-day or cosy playlists.",
    "Party needs singalongs": "For parties and pregames.",
    "Commute mixes too long": "For drives and commutes.",
    "Running tempo steady": "For runs and jogging.",
    "More Bollywood in cooking": "When cooking or in the kitchen.",
    "Morning start gentle": "For morning and wake-up playlists.",
}
LIVE_REQUESTS = {  # request: the lessons that fit it
    "leg day, 40 minutes": {"Workout energy 8+", "No warm-up songs at the gym"},
    "deadlift PRs today": {"Workout energy 8+", "No warm-up songs at the gym"},
    "hype me up for a 5k": {"Workout energy 8+", "Running tempo steady"},
    "help me drift off": {"Sleep music too busy"},
    "pouring rain outside, coffee": {"Rainy-day playlists too sad"},
    "grinding through a problem set": {"Hard cut into focus works"},
    "pregame with the boys": {"Party needs singalongs"},
    "making dal for dinner": {"More Bollywood in cooking"},
    "stuck in traffic on the way to work": {"Commute mixes too long"},
    "just woke up, slow start": {"Morning start gentle"},
    "road trip with my sister": {"Commute mixes too long"},
    "chill sunday afternoon": set(),
    "meh": set(),
    "heartbreak hours": set(),
}


@pytest.mark.live
def test_live_recall_by_meaning_finds_what_tags_miss(tmp_path, monkeypatch):
    """The 2026-10-27 calibration, against the real model, in a scratch database (not data/moodify.db)."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "live.db"))
    get_settings.cache_clear()
    init_db()
    with get_conn() as conn:
        conn.execute("INSERT INTO users (user_id, display_name, refresh_token) VALUES ('synthetic', 'S', 'none')")
    _store("synthetic", [_draft(t, ("x",), d) for t, d in LIVE_LESSONS.items()])
    found = wrong = 0
    for request, fits in LIVE_REQUESTS.items():
        close = embeddings.relevant("synthetic", [request])
        got = {r["title"] for r in _lessons("synthetic") if r["id"] in close}
        print(f"{request!r}: {sorted(got)}")
        found += len(got & fits)
        wrong += len(got - fits)
    total = sum(len(f) for f in LIVE_REQUESTS.values())
    print(f"found {found} of {total}, {wrong} that don't fit")
    assert found >= 0.7 * total and wrong <= 3
