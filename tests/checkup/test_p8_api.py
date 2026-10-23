"""Phase 8 — HTTP API: the service the Docker image is supposed to run."""
import pytest


@pytest.fixture(scope="module")
def client(settings):
    try:
        from src.main import app
    except Exception as e:
        pytest.fail(f"src.main does not expose `app`: {type(e).__name__}: {e}")
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


def test_app_is_fastapi(settings):
    from fastapi import FastAPI

    import src.main

    assert isinstance(getattr(src.main, "app", None), FastAPI), "src/main.py is empty (app/main.py was deleted)"


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200


def test_search_rejects_blank_query(client):
    r = client.post("/api/v1/search", json={"query": "  "})
    assert r.status_code == 422


@pytest.mark.live
@pytest.mark.docker
def test_search_returns_songs(client):
    r = client.post("/api/v1/search", json={"query": "chill lofi beats to study to"})
    assert r.status_code == 200, r.text
    songs = r.json()
    assert songs and {"title", "artist"} <= set(songs[0])
