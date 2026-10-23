"""Phase 5: mood + taste -> songs from the LLM, mocked over HTTP except the live test."""
import json
import logging

import httpx
import pytest

from src import suggest
from src.suggest import Song, suggest_songs

from .conftest import TEST_ENV

CHAT_URL = "https://api.openai.com/v1/chat/completions"
TASTE = "Favourite genres: indie rock. Artists they love: Radiohead, The National."


def _completion(content: str) -> dict:
    return {
        "id": "chatcmpl-1", "object": "chat.completion", "created": 0, "model": "test-model",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
    }


def _songs_json(n: int) -> str:
    return json.dumps({"songs": [{"title": f"Song {i}", "artist": f"Artist {i % 15}"} for i in range(n)]})


@pytest.fixture
def chat(respx_mock):
    return respx_mock.post(CHAT_URL)


@pytest.fixture
def suggest_log(caplog):
    # MoodifyLogger doesn't propagate to the root logger, so attach caplog's handler directly
    logger = logging.getLogger("moodify.suggest")
    logger.addHandler(caplog.handler)
    yield caplog
    logger.removeHandler(caplog.handler)


def test_thirty_valid_songs(chat):
    chat.respond(200, json=_completion(_songs_json(30)))
    songs = suggest_songs("rainy sunday", TASTE)
    assert len(songs) == 30
    assert songs[0] == Song(title="Song 0", artist="Artist 0")
    assert all(isinstance(s, Song) for s in songs)


def test_request_carries_mood_taste_model_and_schema(chat, test_settings):
    chat.respond(200, json=_completion(_songs_json(3)))
    suggest_songs("rainy sunday, nostalgic", TASTE, n=25)

    request = chat.calls.last.request
    assert request.headers["authorization"] == f"Bearer {TEST_ENV['OPENAI_API_KEY']}"
    body = json.loads(request.content)
    assert body["model"] == test_settings.OPENAI_MODEL == "gpt-5.4-mini"
    user_message = body["messages"][-1]["content"]
    assert "Mood: rainy sunday, nostalgic" in user_message
    assert TASTE in user_message
    assert "25 songs" in user_message
    assert body["messages"][0]["role"] == "system"
    assert "real, released songs" in body["messages"][0]["content"]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True


def test_long_mood_is_truncated_to_300(chat):
    chat.respond(200, json=_completion(_songs_json(1)))
    suggest_songs("m" * 2000, TASTE)
    user_message = json.loads(chat.calls.last.request.content)["messages"][-1]["content"]
    assert "m" * 300 in user_message
    assert "m" * 301 not in user_message


def test_clean_mood_strips_control_characters_and_whitespace():
    assert suggest.clean_mood("rainy\x00 sunday\x1b[31m,\n\n  nostalgic​") == "rainy sunday[31m, nostalgic"
    assert suggest.clean_mood("  ") == ""
    assert len(suggest.clean_mood("x" * 1000)) == 300


def test_malformed_json_gives_empty_list_and_logs(chat, suggest_log):
    chat.respond(200, json=_completion("Here are some songs: 1. Creep by Radiohead"))
    assert suggest_songs("rainy", TASTE) == []
    assert any(r.levelno == logging.ERROR and "suggestion call failed" in r.message for r in suggest_log.records)


def test_wrong_shape_gives_empty_list(chat, suggest_log):
    chat.respond(200, json=_completion(json.dumps({"songs": [{"name": "Creep"}]})))
    assert suggest_songs("rainy", TASTE) == []
    assert suggest_log.records


def test_refusal_gives_empty_list(chat, suggest_log):
    body = _completion("")
    body["choices"][0]["message"] = {"role": "assistant", "content": None, "refusal": "I can't help with that."}
    chat.respond(200, json=body)
    assert suggest_songs("rainy", TASTE) == []
    assert suggest_log.records


@pytest.mark.parametrize("failure", [
    httpx.Response(500, json={"error": {"message": "server error"}}),
    httpx.Response(401, json={"error": {"message": "bad key"}}),
    httpx.Response(429, json={"error": {"message": "rate limited"}}),
    httpx.ReadTimeout("timed out"),
    httpx.ConnectError("refused"),
])
def test_api_failures_give_empty_list(chat, suggest_log, failure):
    chat.mock(side_effect=[failure])
    assert suggest_songs("rainy", TASTE) == []
    assert chat.call_count == 1  # no SDK retries: the route decides what to do
    assert suggest_log.records


def test_blank_and_duplicate_songs_are_dropped(chat):
    chat.respond(200, json=_completion(json.dumps({"songs": [
        {"title": " Creep ", "artist": "Radiohead"},
        {"title": "creep", "artist": "RADIOHEAD"},
        {"title": "", "artist": "Nobody"},
        {"title": "Lucky", "artist": "   "},
        {"title": "Lucky", "artist": "Radiohead"},
    ]})))
    assert suggest_songs("rainy", TASTE) == [Song(title="Creep", artist="Radiohead"), Song(title="Lucky", artist="Radiohead")]


@pytest.mark.live
def test_live_rainy_sunday(test_settings):
    songs = suggest_songs("rainy sunday, nostalgic", TASTE)
    assert len(songs) >= 20, f"only {len(songs)} songs from {test_settings.OPENAI_MODEL}"
    assert all(s.title and s.artist for s in songs)
    assert len({s.artist.lower() for s in songs}) >= 10
