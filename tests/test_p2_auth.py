"""Phase 2: Spotify login with PKCE, the session cookie, and disconnect.

Spotify is mocked over HTTP with respx. A request no route matches fails the test (respx_mock,
or the no_network guard in conftest.py), so "nothing was called" needs no extra assertion.
"""
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src import users
from src.core.config import Settings
from src.core.exceptions import ReauthRequired, SpotifyError
from src.db import get_conn
from src.main import app
from src.spotify import auth

from .conftest import TEST_ENV


@pytest.fixture
def client():
    # follow_redirects=False: the redirect to accounts.spotify.com is what we assert on
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def _start_login(client) -> dict:
    resp = client.post("/login")
    assert resp.status_code == 303
    return _query(resp.headers["location"])


def _mock_spotify(respx_mock, user_id="friend1", display_name="Friend One", me_status=200):
    token = respx_mock.post(auth.TOKEN_URL).respond(
        200, json={"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600, "token_type": "Bearer",
                   "scope": auth.SCOPES}
    )
    me = respx_mock.get(auth.ME_URL).respond(me_status, json={"id": user_id, "display_name": display_name})
    return token, me


def _log_in(client, respx_mock, **profile):
    params = _start_login(client)
    _mock_spotify(respx_mock, **profile)
    return client.get("/callback", params={"code": "auth-code", "state": params["state"]})


def _user_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT count(*) FROM users").fetchone()[0]


# --- PKCE and the authorize URL -----------------------------------------------

def test_code_challenge_matches_rfc7636_example():
    # RFC 7636 Appendix B
    assert auth.code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_verifier_is_valid_and_random():
    verifier = auth.new_verifier()
    assert 43 <= len(verifier) <= 128
    assert set(verifier) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    assert auth.new_verifier() != verifier


def test_authorize_url_has_pkce_scopes_and_state():
    url = auth.authorize_url("state-123", "v" * 64)
    assert url.startswith(auth.AUTHORIZE_URL + "?")
    q = _query(url)
    assert q["client_id"] == TEST_ENV["SPOTIFY_CLIENT_ID"]
    assert q["response_type"] == "code"
    assert q["redirect_uri"] == "http://127.0.0.1:9091/callback"
    assert q["state"] == "state-123"
    assert q["code_challenge_method"] == "S256"
    assert q["code_challenge"] == auth.code_challenge("v" * 64)
    assert set(q["scope"].split()) == {
        "user-top-read", "user-read-private", "playlist-modify-public",
        "playlist-modify-private", "user-modify-playback-state",
        # Phase 10
        "user-read-recently-played", "user-library-read", "user-library-modify", "user-follow-read",
        "playlist-read-private", "user-read-playback-state",
    }


def test_login_redirects_to_spotify_with_fresh_state(client):
    first, second = _start_login(client), _start_login(client)
    assert first["state"] != second["state"]
    assert first["code_challenge"] != second["code_challenge"]
    assert "moodify_session" in client.cookies


# --- /callback ----------------------------------------------------------------

def test_callback_with_mismatched_state_is_rejected(client):
    _start_login(client)
    resp = client.get("/callback", params={"code": "auth-code", "state": "forged"})
    assert resp.status_code == 400
    assert _user_count() == 0


def test_callback_with_non_ascii_state_is_rejected_not_crashed(client):
    _start_login(client)
    resp = client.get("/callback", params={"code": "auth-code", "state": "état"})
    assert resp.status_code == 400


def test_callback_without_a_login_in_progress_is_rejected(client):
    resp = client.get("/callback", params={"code": "auth-code", "state": "anything"})
    assert resp.status_code == 400
    assert _user_count() == 0


def test_callback_state_works_only_once(client, respx_mock):
    params = _start_login(client)
    _mock_spotify(respx_mock)
    assert client.get("/callback", params={"code": "auth-code", "state": params["state"]}).status_code == 303
    replay = client.get("/callback", params={"code": "auth-code", "state": params["state"]})
    assert replay.status_code == 400


def test_callback_when_user_denies_access(client):
    params = _start_login(client)
    resp = client.get("/callback", params={"error": "access_denied", "state": params["state"]})
    assert resp.status_code == 400
    assert _user_count() == 0


def test_callback_logs_the_user_in(client, respx_mock):
    params = _start_login(client)
    token_route, _ = _mock_spotify(respx_mock)

    resp = client.get("/callback", params={"code": "auth-code", "state": params["state"]})

    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    row = users.get("friend1")
    assert row["display_name"] == "Friend One"
    assert row["refresh_token"] == "refresh-1"
    # PKCE end to end: the verifier sent to Spotify hashes to the challenge from /login
    form = {k: v[0] for k, v in parse_qs(token_route.calls.last.request.content.decode()).items()}
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "auth-code"
    assert form["client_id"] == TEST_ENV["SPOTIFY_CLIENT_ID"]
    assert "client_secret" not in form
    assert auth.code_challenge(form["code_verifier"]) == params["code_challenge"]

    # Logged in, but no onboarding yet: home sends them there (Phase 4)
    home = client.get("/")
    assert home.status_code == 303
    assert home.headers["location"] == "/onboarding"


def test_repeat_login_updates_token_and_keeps_onboarding(client, respx_mock):
    respx_mock.post(auth.TOKEN_URL).mock(side_effect=[
        httpx.Response(200, json={"access_token": "a1", "refresh_token": "refresh-1", "expires_in": 3600}),
        httpx.Response(200, json={"access_token": "a2", "refresh_token": "refresh-2", "expires_in": 3600}),
    ])
    respx_mock.get(auth.ME_URL).mock(side_effect=[
        httpx.Response(200, json={"id": "friend1", "display_name": "Friend One"}),
        httpx.Response(200, json={"id": "friend1", "display_name": "Renamed"}),
    ])
    client.get("/callback", params={"code": "code-1", "state": _start_login(client)["state"]})
    with get_conn() as conn:
        conn.execute("UPDATE users SET onboarding_json = '{\"energy\": \"low\"}' WHERE user_id = 'friend1'")

    client.get("/callback", params={"code": "code-2", "state": _start_login(client)["state"]})

    row = users.get("friend1")
    assert (row["refresh_token"], row["display_name"]) == ("refresh-2", "Renamed")
    assert json.loads(row["onboarding_json"]) == {"energy": "low"}
    assert _user_count() == 1


def test_callback_for_account_not_on_allowlist(client, respx_mock):
    resp = _log_in(client, respx_mock, me_status=403)
    assert resp.status_code == 403
    assert "allowlist" in resp.text
    assert _user_count() == 0


def test_callback_when_spotify_is_down(client, respx_mock):
    params = _start_login(client)
    respx_mock.post(auth.TOKEN_URL).respond(503, text="unavailable")
    resp = client.get("/callback", params={"code": "auth-code", "state": params["state"]})
    assert resp.status_code == 502
    assert _user_count() == 0


# --- refresh ------------------------------------------------------------------

def test_refresh_returns_new_access_token(respx_mock):
    route = respx_mock.post(auth.TOKEN_URL).respond(200, json={"access_token": "fresh", "expires_in": 3600})
    tokens = auth.refresh("refresh-1")
    assert tokens.access_token == "fresh"
    assert tokens.refresh_token == "refresh-1"  # Spotify didn't rotate it, so we keep the old one
    form = {k: v[0] for k, v in parse_qs(route.calls.last.request.content.decode()).items()}
    assert form == {"grant_type": "refresh_token", "refresh_token": "refresh-1", "client_id": TEST_ENV["SPOTIFY_CLIENT_ID"]}


def test_refresh_keeps_a_rotated_refresh_token(respx_mock):
    respx_mock.post(auth.TOKEN_URL).respond(200, json={"access_token": "fresh", "refresh_token": "refresh-2", "expires_in": 3600})
    assert auth.refresh("refresh-1").refresh_token == "refresh-2"


def test_refresh_invalid_grant_raises_reauth_required(respx_mock):
    respx_mock.post(auth.TOKEN_URL).respond(400, json={"error": "invalid_grant", "error_description": "Refresh token revoked"})
    with pytest.raises(ReauthRequired):
        auth.refresh("dead-token")


def test_refresh_other_failures_are_not_reauth(respx_mock):
    respx_mock.post(auth.TOKEN_URL).respond(500, text="oops")
    with pytest.raises(SpotifyError) as exc:
        auth.refresh("refresh-1")
    assert exc.value.status == 500
    assert not isinstance(exc.value, ReauthRequired)


# --- protected routes and disconnect -------------------------------------------

def test_protected_route_without_cookie_redirects_to_login(client):
    resp = client.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_disconnect_without_login_redirects_to_login(client):
    resp = client.post("/disconnect")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_disconnect_deletes_user_and_playlists_and_logs_out(client, respx_mock):
    _log_in(client, respx_mock)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO playlists (user_id, prompt, url, track_uris_json) VALUES (?, ?, ?, ?)",
            ("friend1", "rainy sunday", "https://open.spotify.com/playlist/p1", "[]"),
        )

    resp = client.post("/disconnect")

    assert resp.status_code == 200
    assert users.get("friend1") is None
    with get_conn() as conn:
        assert conn.execute("SELECT count(*) FROM playlists").fetchone()[0] == 0
    after = client.get("/")
    assert after.status_code == 303
    assert after.headers["location"] == "/login"


def test_cookie_for_a_deleted_user_redirects_to_login(client, respx_mock):
    _log_in(client, respx_mock)
    users.delete("friend1")
    resp = client.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_session_cookie_is_signed(client, respx_mock):
    _log_in(client, respx_mock)
    cookie = client.cookies["moodify_session"]
    tampered = cookie[:-4] + ("AAAA" if not cookie.endswith("AAAA") else "BBBB")
    # A fresh client with an empty cookie jar, so only the header below is sent
    other = TestClient(app, follow_redirects=False)
    assert other.get("/", headers={"cookie": f"moodify_session={cookie}"}).headers["location"] == "/onboarding"
    assert other.get("/", headers={"cookie": f"moodify_session={tampered}"}).headers["location"] == "/login"


# --- redirect URI rules -------------------------------------------------------

def test_localhost_redirect_uri_is_rejected(monkeypatch):
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "http://localhost:9091/callback")
    with pytest.raises(ValidationError, match="127.0.0.1"):
        Settings()


def test_prod_requires_https_redirect_uri(monkeypatch):
    monkeypatch.setenv("ENV", "prod")
    with pytest.raises(ValidationError, match="https"):
        Settings()
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "https://moodify.example.com/callback")
    assert Settings().SPOTIFY_REDIRECT_URI == "https://moodify.example.com/callback"
