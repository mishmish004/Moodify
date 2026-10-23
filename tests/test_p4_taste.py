"""Phase 4: onboarding answers + Spotify top items -> one cached taste paragraph."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src import taste, users
from src.db import init_db
from src.main import app
from src.spotify import auth
from src.spotify.client import API_URL, Track

from .conftest import mock_listening

ONBOARDING = {
    "genres": "indie rock, bollywood, lo-fi",
    "artists": "Prateek Kuhad, Radiohead, Arijit Singh",
    "energy": "medium",
    "instrumental": "no",
    "languages": "English, Hindi",
    "avoid": "no country, nothing explicit",
}
TOP_ARTISTS = ["Radiohead", "Björk", "Prateek Kuhad", "The National", "Arijit Singh", "Phoebe Bridgers", "Bon Iver"]
TOP_TRACKS = [Track(f"spotify:track:t{i}", f"Song {i}", (TOP_ARTISTS[i % len(TOP_ARTISTS)],), 200_000) for i in range(5)]


def _stamp(hours_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(taste.TIMESTAMP)


@pytest.fixture
def user(test_settings):
    init_db()
    users.upsert("friend1", "Friend One", "refresh-1")
    return users.get("friend1")


@pytest.fixture
def client():
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def logged_in(client, user, respx_mock):
    params = dict(p.split("=", 1) for p in client.post("/login").headers["location"].split("?", 1)[1].split("&"))
    respx_mock.post(auth.TOKEN_URL).respond(200, json={"access_token": "a", "refresh_token": "refresh-1", "expires_in": 3600,
                                                       "scope": auth.SCOPES})
    respx_mock.get(auth.ME_URL).respond(200, json={"id": "friend1", "display_name": "Friend One"})
    assert client.get("/callback", params={"code": "c", "state": params["state"]}).status_code == 303
    return client


def _mock_top(respx_mock, artists=TOP_ARTISTS, tracks=TOP_TRACKS):
    # A fresh client trades the refresh token for an access token before its first call
    respx_mock.post(auth.TOKEN_URL).respond(200, json={"access_token": "a", "expires_in": 3600})
    a = respx_mock.get(f"{API_URL}/me/top/artists").respond(200, json={"items": [{"name": n} for n in artists]})
    t = respx_mock.get(f"{API_URL}/me/top/tracks").respond(200, json={"items": [
        {"uri": x.uri, "name": x.name, "artists": [{"name": n} for n in x.artists], "duration_ms": x.duration_ms} for x in tracks
    ]})
    mock_listening(respx_mock)  # Phase 10's other reads, empty
    return a, t


# --- build_summary -------------------------------------------------------------------

def _listening(artists=TOP_ARTISTS, tracks=TOP_TRACKS):
    return taste.Listening(top_artists=list(artists), top_tracks=list(tracks))


def test_build_summary_is_deterministic_and_short():
    first = taste.build_summary(ONBOARDING, _listening())
    assert first == taste.build_summary(ONBOARDING, _listening())
    assert len(first) <= taste.MAX_CHARS


def test_build_summary_mentions_every_answer_and_top_artists():
    text = taste.build_summary(ONBOARDING, _listening())
    for key in ("genres", "artists", "energy", "languages", "avoid"):
        assert ONBOARDING[key] in text, key
    assert "No instrumental" in text
    for artist in TOP_ARTISTS[:5]:
        assert artist in text
    assert "Song 0 by Radiohead" in text


def test_build_summary_without_spotify_data():
    text = taste.build_summary(ONBOARDING, taste.Listening())
    assert ONBOARDING["genres"] in text
    assert "Most played" not in text and "On repeat" not in text
    assert taste.build_summary({}, taste.Listening()) == "No taste information yet."


def test_build_summary_drops_listening_data_to_fit():
    long_answers = {k: ("x" * 200 if k not in ("energy", "instrumental") else v) for k, v in ONBOARDING.items()}
    many_artists = [f"Artist number {i} with a long name" for i in range(40)]
    many_tracks = [Track(f"u{i}", f"A very long song title number {i}", (f"Artist {i}",), 1) for i in range(40)]
    text = taste.build_summary(long_answers, _listening(many_artists, many_tracks))
    assert len(text) <= taste.MAX_CHARS
    assert "x" * 200 in text  # the answers always survive; listening data gives way first
    assert "Artist number 0" in text


# --- summary_for: cache and staleness ------------------------------------------------

def test_fresh_summary_is_served_from_cache(user, respx_mock):
    users.set_onboarding("friend1", ONBOARDING)
    users.set_taste_summary("friend1", "cached summary", _stamp(hours_ago=1))
    assert taste.summary_for(users.get("friend1")) == "cached summary"  # any Spotify call would fail the test


def test_stale_summary_is_rebuilt_and_recached(user, respx_mock):
    users.set_onboarding("friend1", ONBOARDING)
    users.set_taste_summary("friend1", "old summary", _stamp(hours_ago=25))  # Phase 10: stale after a day
    artists, tracks = _mock_top(respx_mock)

    text = taste.summary_for(users.get("friend1"))

    assert "Radiohead" in text and text != "old summary"
    assert artists.call_count == 2 and tracks.call_count == 1  # lately and all-time
    row = users.get("friend1")
    assert row["taste_summary"] == text
    assert datetime.strptime(row["taste_updated_at"], taste.TIMESTAMP) > datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)


def test_new_onboarding_answers_invalidate_the_cache(user, respx_mock):
    users.set_taste_summary("friend1", "cached summary", _stamp(hours_ago=1))
    users.set_onboarding("friend1", ONBOARDING)
    _mock_top(respx_mock)
    assert taste.summary_for(users.get("friend1")) != "cached summary"


def test_spotify_failure_falls_back_to_onboarding_only_and_does_not_cache(user, respx_mock):
    users.set_onboarding("friend1", ONBOARDING)
    respx_mock.post(auth.TOKEN_URL).respond(200, json={"access_token": "a", "expires_in": 3600})
    respx_mock.get(f"{API_URL}/me/top/artists").respond(503)
    respx_mock.get(f"{API_URL}/me/top/tracks").respond(503)
    mock_listening(respx_mock, fail=("recent", "liked", "followed", "playlists"))

    text = taste.summary_for(users.get("friend1"))

    assert ONBOARDING["genres"] in text and "Most played" not in text
    assert users.get("friend1")["taste_summary"] is None


# --- routes -------------------------------------------------------------------------

def test_home_without_onboarding_redirects_to_onboarding(logged_in):
    resp = logged_in.get("/")
    assert resp.status_code == 303 and resp.headers["location"] == "/onboarding"


def test_onboarding_form_renders(logged_in):
    resp = logged_in.get("/onboarding")
    assert resp.status_code == 200
    for field in ("genres", "artists", "energy", "instrumental", "languages", "avoid"):
        assert f'name="{field}"' in resp.text


def test_onboarding_submit_stores_answers_and_redirects_home(logged_in, respx_mock):
    resp = logged_in.post("/onboarding", data=ONBOARDING)
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    assert json.loads(users.get("friend1")["onboarding_json"]) == {**ONBOARDING, "familiar": "mix"}  # Phase 10 default

    _mock_top(respx_mock)
    home = logged_in.get("/")
    assert home.status_code == 200
    assert ONBOARDING["genres"] in home.text and "Radiohead" in home.text


def test_onboarding_form_is_prefilled_on_return(logged_in):
    logged_in.post("/onboarding", data=ONBOARDING)
    resp = logged_in.get("/onboarding")
    assert 'value="indie rock, bollywood, lo-fi"' in resp.text
    assert '<option value="medium" selected>' in resp.text


@pytest.mark.parametrize("missing", ["genres", "artists", "energy"])
def test_onboarding_missing_required_field_is_422_and_stores_nothing(logged_in, missing):
    data = {k: v for k, v in ONBOARDING.items() if k != missing}
    assert logged_in.post("/onboarding", data=data).status_code == 422
    data[missing] = ""
    assert logged_in.post("/onboarding", data=data).status_code == 422
    assert users.get("friend1")["onboarding_json"] is None


def test_onboarding_rejects_unknown_energy(logged_in):
    assert logged_in.post("/onboarding", data={**ONBOARDING, "energy": "ultra"}).status_code == 422


def test_onboarding_requires_login(client):
    assert client.get("/onboarding").headers["location"] == "/login"
    assert client.post("/onboarding", data=ONBOARDING).headers["location"] == "/login"


def test_dead_refresh_token_sends_user_back_to_login(logged_in, respx_mock):
    logged_in.post("/onboarding", data=ONBOARDING)
    respx_mock.get(f"{API_URL}/me/top/artists").respond(401)
    respx_mock.post(auth.TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    resp = logged_in.get("/")
    assert resp.status_code == 303 and resp.headers["location"] == "/login"
