"""Google sign-in: every page waits behind it, the Spotify login included.

These tests meet the real check (marked google_gate); the rest of the suite starts signed in.
Google's token endpoint is mocked with respx.
"""
import base64
import json
import re
import time

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src import google_auth, users
from src.core.config import Settings, get_settings
from src.main import app
from src.spotify import auth

from .conftest import TEST_ENV
from .test_p2_auth import _mock_spotify, _query

pytestmark = pytest.mark.google_gate

OPEN = {"/health", "/signin", "/signin/callback"}
GATED = sorted((method, route.path) for route in app.routes if isinstance(route, APIRoute) and route.path not in OPEN
               for method in route.methods)


@pytest.fixture
def client():
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _id_token(**claims) -> str:
    return f"{_b64({'alg': 'RS256', 'kid': 'k'})}.{_b64(claims)}.signature"


def _claims(nonce: str) -> dict:
    return {"iss": "https://accounts.google.com", "aud": TEST_ENV["GOOGLE_CLIENT_ID"], "sub": "1234567890",
            "email": "ada@gmail.com", "email_verified": True, "iat": int(time.time()), "exp": int(time.time()) + 3600,
            "nonce": nonce}


def _sign_in(client, respx_mock, **changes):
    """POST /signin, then Google sends the user back. Returns (callback response, token route, authorize query)."""
    query = _query(client.post("/signin").headers["location"])
    token = respx_mock.post(google_auth.TOKEN_URL).respond(200, json={
        "access_token": "google-access", "expires_in": 3599, "token_type": "Bearer", "scope": "openid email",
        "id_token": _id_token(**{**_claims(query["nonce"]), **changes})})
    return client.get("/signin/callback", params={"code": "google-code", "state": query["state"]}), token, query


# --- the gate -------------------------------------------------------------------------------------

def test_the_spotify_login_is_behind_the_gate_too():
    assert ("GET", "/login") in GATED and ("POST", "/login") in GATED and ("GET", "/callback") in GATED


@pytest.mark.parametrize("method, path", GATED)
def test_every_page_waits_for_google_sign_in(client, method, path):
    resp = client.request(method, re.sub(r"\{[^}]+\}", "1", path), data={"mood": "x"} if method == "POST" else None)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/signin"


def test_health_styles_and_the_sign_in_page_are_open(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/static/style.css").status_code == 200
    page = client.get("/signin")
    assert page.status_code == 200
    assert '<form method="post" action="/signin">' in page.text and "Sign in with Google" in page.text
    # Viewing the page starts nothing: no state, no cookie
    assert "set-cookie" not in page.headers


# --- signing in -----------------------------------------------------------------------------------

def test_signin_sends_the_user_to_google_with_pkce_state_and_nonce(client):
    resp = client.post("/signin")
    assert resp.status_code == 303
    url = resp.headers["location"]
    assert url.startswith(google_auth.AUTHORIZE_URL + "?")
    q = _query(url)
    assert q["client_id"] == TEST_ENV["GOOGLE_CLIENT_ID"]
    assert q["redirect_uri"] == "http://127.0.0.1:9091/signin/callback"
    assert q["response_type"] == "code" and q["scope"] == "openid email"
    assert q["code_challenge_method"] == "S256" and len(q["code_challenge"]) == 43
    assert q["state"] and q["nonce"] and q["prompt"] == "select_account"


def test_coming_back_from_google_signs_in_and_leads_to_the_spotify_login(client, respx_mock):
    resp, token, query = _sign_in(client, respx_mock)
    assert resp.status_code == 303 and resp.headers["location"] == "/"

    # The code went back with the client secret and the verifier behind the challenge
    sent = dict(httpx.QueryParams(token.calls.last.request.content.decode()))
    assert sent["code"] == "google-code" and sent["grant_type"] == "authorization_code"
    assert sent["client_secret"] == TEST_ENV["GOOGLE_CLIENT_SECRET"]
    assert sent["redirect_uri"] == "http://127.0.0.1:9091/signin/callback"
    assert auth.code_challenge(sent["code_verifier"]) == query["code_challenge"]

    assert client.get("/").headers["location"] == "/login"
    login = client.get("/login")
    assert login.status_code == 200 and "Signed in with Google as ada@gmail.com" in login.text
    assert client.get("/signin").headers["location"] == "/"


def test_after_google_the_spotify_login_works_as_before(client, respx_mock):
    _sign_in(client, respx_mock)
    query = _query(client.post("/login").headers["location"])
    _mock_spotify(respx_mock)
    assert client.get("/callback", params={"code": "auth-code", "state": query["state"]}).headers["location"] == "/"
    assert users.get("friend1") is not None
    assert client.get("/").headers["location"] == "/onboarding"


def test_a_spotify_relogin_keeps_the_google_sign_in(client, respx_mock):
    _sign_in(client, respx_mock)
    query = _query(client.post("/login").headers["location"])
    _mock_spotify(respx_mock)
    client.get("/callback", params={"code": "auth-code", "state": query["state"]})
    users.delete("friend1")  # the row is gone, as after a disconnect elsewhere
    assert client.get("/").headers["location"] == "/login"
    assert client.get("/login").status_code == 200


def test_disconnect_signs_out_of_google_too(client, respx_mock):
    _sign_in(client, respx_mock)
    query = _query(client.post("/login").headers["location"])
    _mock_spotify(respx_mock)
    client.get("/callback", params={"code": "auth-code", "state": query["state"]})
    assert client.post("/disconnect").status_code == 200
    assert client.get("/").headers["location"] == "/signin"


# --- failures -------------------------------------------------------------------------------------

def test_cancelling_on_googles_screen_is_explained(client):
    query = _query(client.post("/signin").headers["location"])
    resp = client.get("/signin/callback", params={"error": "access_denied", "state": query["state"]})
    assert resp.status_code == 400 and "Sign-in cancelled" in resp.text
    assert client.get("/").headers["location"] == "/signin"


@pytest.mark.parametrize("params", [{"code": "c", "state": "forged"}, {"code": "c", "state": "état"}, {"state": "x"}])
def test_a_forged_or_incomplete_callback_is_refused_without_calling_google(client, params):
    client.post("/signin")
    resp = client.get("/signin/callback", params=params)
    assert resp.status_code == 400 and "Sign-in expired" in resp.text
    assert client.get("/").headers["location"] == "/signin"


def test_a_callback_without_a_started_sign_in_is_refused(client):
    resp = client.get("/signin/callback", params={"code": "c", "state": "anything"})
    assert resp.status_code == 400 and "Sign-in expired" in resp.text


def test_a_callback_works_once(client, respx_mock):
    _, token, query = _sign_in(client, respx_mock)
    replay = client.get("/signin/callback", params={"code": "google-code", "state": query["state"]})
    assert replay.status_code == 400 and token.call_count == 1


@pytest.mark.parametrize("changes", [
    {"aud": "someone-elses-client"},
    {"iss": "https://evil.example"},
    {"nonce": "another-sign-in"},
    {"exp": int(time.time()) - 1},
    {"sub": ""},
])
def test_an_id_token_that_doesnt_check_out_is_refused(client, respx_mock, changes):
    resp, _, _ = _sign_in(client, respx_mock, **changes)
    assert resp.status_code == 502 and "Sign-in failed" in resp.text
    assert client.get("/").headers["location"] == "/signin"


@pytest.mark.parametrize("answer", [
    httpx.Response(400, json={"error": "invalid_grant"}),
    httpx.Response(200, json={"access_token": "a"}),
    httpx.Response(200, json={"id_token": "not-a-jwt"}),
    httpx.Response(200, text="<html>"),
    httpx.ConnectError("down"),
])
def test_google_failing_is_a_failed_sign_in(client, respx_mock, answer):
    query = _query(client.post("/signin").headers["location"])
    respx_mock.post(google_auth.TOKEN_URL).mock(side_effect=[answer])
    resp = client.get("/signin/callback", params={"code": "google-code", "state": query["state"]})
    assert resp.status_code == 502 and "Sign-in failed" in resp.text
    assert client.get("/").headers["location"] == "/signin"


# --- settings -------------------------------------------------------------------------------------

def test_redirect_uri_follows_the_spotify_host_unless_set(monkeypatch):
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "https://moodify.example.com/callback")
    get_settings.cache_clear()
    assert google_auth.redirect_uri() == "https://moodify.example.com/signin/callback"
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "https://elsewhere.example.com/signin/callback")
    get_settings.cache_clear()
    assert google_auth.redirect_uri() == "https://elsewhere.example.com/signin/callback"


def test_prod_needs_an_https_google_redirect(monkeypatch):
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "https://moodify.example.com/callback")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://moodify.example.com/signin/callback")
    with pytest.raises(ValidationError, match="GOOGLE_REDIRECT_URI"):
        Settings()
