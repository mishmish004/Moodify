"""More than one Spotify developer app (SPOTIFY_EXTRA_CLIENT_IDS), for testing with more than 5 people.

One login link for everyone. Moodify tries the apps in turn: Spotify lets a user log in to a Development
Mode app they aren't allowlisted on but answers 403 to the API after, so a 403 from /me means "next app".
The app that worked is kept with the user (a refresh token only works with its own app) and in a cookie.
Everyone lands in the same database, so friends on different apps share taste, Blends and the map as before.
"""
import html
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from src import users
from src.core.config import get_settings
from src.db import get_conn, init_db
from src.main import SPOTIFY_APP_COOKIE, app
from src.spotify import auth
from src.spotify.client import SpotifyClient

from .conftest import finish_build
from .test_p20_blend import MINE, SAMS, SHARED, Chat, _blend, _friend, _mine_in_library, _song, _spotify
from .test_p6_playlist import logged_in, user  # noqa: F401

APPS = ["test-client-id", "app-two", "app-three"]


@pytest.fixture(autouse=True)
def three_apps(monkeypatch):
    monkeypatch.setenv("SPOTIFY_EXTRA_CLIENT_IDS", "app-two, app-three,,app-two")
    get_settings.cache_clear()
    init_db()


@pytest.fixture
def client():
    with TestClient(app, follow_redirects=False) as c:
        yield c


def _query(url: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


def _form(request) -> dict:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


class Spotify:
    """Spotify's login for a user allowlisted on `home` only: /me answers 403 through any other app."""

    def __init__(self, respx_mock, home="test-client-id"):
        self.home = home
        respx_mock.post(auth.TOKEN_URL).mock(side_effect=self._token)
        respx_mock.get(auth.ME_URL).mock(side_effect=self._me)

    def _token(self, request):
        client_id = _form(request)["client_id"]
        return httpx.Response(200, json={"access_token": f"access-{client_id}", "refresh_token": "refresh-1",
                                         "expires_in": 3600, "token_type": "Bearer", "scope": auth.SCOPES})

    def _me(self, request):
        if request.headers["authorization"] != f"Bearer access-{self.home}":
            return httpx.Response(403, json={"error": {"status": 403, "message": "User not registered in the Developer Dashboard"}})
        return httpx.Response(200, json={"id": "friend1", "display_name": "Friend One"})


def _log_in(client, spotify, number=None):
    """Press the button (or follow /login?app=N), then follow every redirect back to Spotify until Moodify
    stops at a page or logs the user in. Returns the Client IDs tried, in order, and the last response."""
    resp = client.post("/login", data={"app": number} if number else {})
    tried = []
    while resp.status_code == 303 and resp.headers["location"].startswith(auth.AUTHORIZE_URL):
        params = _query(resp.headers["location"])
        tried.append(params["client_id"])
        resp = client.get("/callback", params={"code": "auth-code", "state": params["state"]})
    return tried, resp


# --- settings ---------------------------------------------------------------------------------------------

def test_client_ids_list_the_main_app_first_without_blanks_or_repeats():
    assert get_settings().spotify_client_ids == APPS


def test_no_extra_apps_means_one(monkeypatch):
    monkeypatch.delenv("SPOTIFY_EXTRA_CLIENT_IDS")
    get_settings.cache_clear()
    assert get_settings().spotify_client_ids == ["test-client-id"]


# --- one link for everyone ----------------------------------------------------------------------------------

def test_a_friend_on_app_3_gets_there_from_the_plain_link(client, respx_mock):
    tried, done = _log_in(client, Spotify(respx_mock, home="app-three"))
    assert tried == APPS  # every app was tried once, in order, with no page in between
    assert done.status_code == 303 and done.headers["location"] == "/"
    assert users.get("friend1")["oauth_client_id"] == "app-three"
    assert client.cookies.get(SPOTIFY_APP_COOKIE) == "app-three"


def test_the_remembered_app_is_tried_first(client, respx_mock):
    client.cookies.set(SPOTIFY_APP_COOKIE, "app-three")
    tried, done = _log_in(client, Spotify(respx_mock, home="app-three"))
    assert tried == ["app-three"] and done.status_code == 303


def test_a_cookie_for_an_app_that_is_gone_is_ignored(client, respx_mock):
    client.cookies.set(SPOTIFY_APP_COOKIE, "app-that-was-removed")
    tried, _ = _log_in(client, Spotify(respx_mock))
    assert tried == ["test-client-id"]


def test_apps_with_free_slots_come_before_full_ones(client, respx_mock):
    # 5 people already use the main app and 2 use app 2: a new friend is most likely on app 3
    for i in range(5):
        users.upsert(f"old{i}", None, "r", auth.SCOPES, oauth_client_id=None if i % 2 else "test-client-id")
    for i in range(2):
        users.upsert(f"two{i}", None, "r", auth.SCOPES, oauth_client_id="app-two")
    users.upsert("youtube:g", None, "r", auth.SCOPES, service="youtube")
    tried, _ = _log_in(client, Spotify(respx_mock, home="test-client-id"))
    assert tried == ["app-three", "app-two", "test-client-id"]


def test_not_on_any_app_says_so_once(client, respx_mock):
    tried, page = _log_in(client, Spotify(respx_mock, home="an-app-moodify-doesnt-have"))
    assert tried == APPS
    assert page.status_code == 403
    assert "any of Moodify's 3 Spotify apps" in html.unescape(page.text)
    assert users.get("friend1") is None


def test_each_attempt_has_its_own_state_and_the_old_one_is_spent(client, respx_mock):
    Spotify(respx_mock, home="app-two")
    params = _query(client.post("/login").headers["location"])
    second = client.get("/callback", params={"code": "auth-code", "state": params["state"]})
    assert second.status_code == 303 and _query(second.headers["location"])["client_id"] == "app-two"
    assert _query(second.headers["location"])["state"] != params["state"]
    # replaying the first callback can't log anyone in: that attempt is over
    assert client.get("/callback", params={"code": "auth-code", "state": params["state"]}).status_code == 400


# --- forcing one app, for testing it ------------------------------------------------------------------------

def test_the_app_link_forces_that_app_only(client, respx_mock):
    page = client.get("/login", params={"app": 2})
    assert page.status_code == 200 and 'name="app" value="2"' in page.text
    assert 'name="app"' not in client.get("/login").text
    tried, page = _log_in(client, Spotify(respx_mock, home="app-three"), number=2)
    assert tried == ["app-two"] and page.status_code == 403


@pytest.mark.parametrize("number", [4, -1])
def test_an_app_that_isnt_set_up_is_refused(client, number):
    page = client.get("/login", params={"app": number})
    assert page.status_code == 404 and "Unknown login link" in page.text
    assert client.post("/login", data={"app": number}).status_code == 404


# --- after login --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("stored, sent", [("app-three", "app-three"), (None, "test-client-id")])
def test_refresh_goes_through_the_users_own_app(respx_mock, stored, sent):
    # NULL: a user from before the extra apps, whose token came from SPOTIFY_CLIENT_ID
    users.upsert("friend1", "Friend One", "refresh-1", auth.SCOPES, oauth_client_id=stored)
    token = respx_mock.post(auth.TOKEN_URL).respond(200, json={"access_token": "access-2", "expires_in": 3600})
    with SpotifyClient.for_user(users.get("friend1")) as sp:
        sp._refresh_access_token()
    assert _form(token.calls.last.request)["client_id"] == sent


def test_friends_on_different_apps_share_one_moodify(logged_in, respx_mock):
    """A Blend between someone on app 2 and someone on app 3: one database, each token through its own app."""
    me = users.get("friend1")
    users.upsert("friend1", me["display_name"], me["refresh_token"], auth.SCOPES, oauth_client_id="app-two")
    _friend("friend2", tracks=SAMS)
    users.upsert("friend2", "Sam Rivera", "refresh-friend2", auth.SCOPES, oauth_client_id="app-three")
    _mine_in_library()
    Chat(respx_mock, [_song(t, "Sam") for t in SAMS[:3]] + [_song(t, "Friend") for t in MINE[:3]] + [_song(SHARED, "everyone")])
    _spotify(respx_mock, SAMS + MINE + [SHARED])
    page = finish_build(logged_in, _blend(logged_in))
    assert page.status_code == 200 and "Sam" in page.text
    refreshes = [_form(c.request) for c in respx_mock.calls if c.request.url == auth.TOKEN_URL]
    assert refreshes and {r["client_id"] for r in refreshes} == {"app-two"}  # the requester's own app


# --- the owner's list: which friend is on which app -----------------------------------------------------------

OWNER = "o-owner"


def _owner(monkeypatch):
    monkeypatch.setenv("OWNER_USER_ID", OWNER)
    get_settings.cache_clear()
    users.upsert(OWNER, "Owner", "refresh-o", auth.SCOPES)
    users.set_onboarding(OWNER, {"genres": "jazz", "artists": "Bill Evans", "energy": "low"})


def _as(client, user_id):
    import base64
    import itsdangerous
    import json
    signer = itsdangerous.TimestampSigner(get_settings().SECRET_KEY.get_secret_value())
    client.cookies.clear()  # a cookie the server set carries a domain; a second copy without one would shadow it
    client.cookies.set("moodify_session", signer.sign(base64.b64encode(json.dumps({"user_id": user_id}).encode())).decode())


def test_pasted_lists_are_read_in_any_layout():
    from src import spotify_apps
    text = "Sam <SAM@Example.com>, priya@example.com;\nsam@example.com\nnot an email\n  veer@example.in "
    assert spotify_apps.parse(text) == ["sam@example.com", "priya@example.com", "veer@example.in"]


def test_only_the_owner_sees_the_apps_page(client, monkeypatch):
    users.upsert("friend1", "Friend One", "refresh-1", auth.SCOPES)
    users.set_onboarding("friend1", {"genres": "jazz", "artists": "Bill Evans", "energy": "low"})
    _as(client, "friend1")
    assert client.get("/apps").status_code == 404
    _owner(monkeypatch)
    assert client.get("/apps").status_code == 404  # still friend1's cookie
    _as(client, OWNER)
    page = client.get("/apps")
    assert page.status_code == 200 and "App 3" in page.text and "test-cli…" in page.text


def test_the_owner_saves_each_apps_list_and_an_email_moves_with_it(client, monkeypatch):
    from src import spotify_apps
    _owner(monkeypatch)
    _as(client, OWNER)
    resp = client.post("/apps", data={"app1": "a@x.com", "app2": "b@x.com, c@x.com", "app3": ""})
    assert resp.status_code == 303 and resp.headers["location"] == "/apps?saved=1"
    assert spotify_apps.members() == {"test-client-id": ["a@x.com"], "app-two": ["b@x.com", "c@x.com"], "app-three": []}
    client.post("/apps", data={"app1": "a@x.com", "app2": "b@x.com", "app3": "c@x.com"})
    assert spotify_apps.app_for("C@x.com") == "app-three"
    assert "c@x.com" in client.get("/apps").text


def test_a_listed_email_goes_to_its_app_first(client, respx_mock):
    from src import spotify_apps
    spotify_apps.set_members("app-three", "sam@example.com")
    spotify = Spotify(respx_mock, home="app-three")
    resp = client.post("/login", data={"email": "Sam@Example.com"})
    tried, done = [], resp
    while done.status_code == 303 and done.headers["location"].startswith(auth.AUTHORIZE_URL):
        params = _query(done.headers["location"])
        tried.append(params["client_id"])
        done = client.get("/callback", params={"code": "auth-code", "state": params["state"]})
    assert tried == ["app-three"] and done.status_code == 303


def test_an_unlisted_email_still_gets_in_and_is_remembered(client, respx_mock):
    from src import spotify_apps
    Spotify(respx_mock, home="app-two")
    resp = client.post("/login", data={"email": "new@example.com"})
    tried = []
    while resp.status_code == 303 and resp.headers["location"].startswith(auth.AUTHORIZE_URL):
        params = _query(resp.headers["location"])
        tried.append(params["client_id"])
        resp = client.get("/callback", params={"code": "auth-code", "state": params["state"]})
    assert tried == ["test-client-id", "app-two"] and resp.status_code == 303
    assert spotify_apps.app_for("new@example.com") == "app-two"


def test_a_wrong_listing_is_corrected_by_the_login_that_worked(client, respx_mock):
    from src import spotify_apps
    spotify_apps.set_members("app-two", "sam@example.com")  # the owner moved Sam to app 3 on the dashboard
    Spotify(respx_mock, home="app-three")
    resp = client.post("/login", data={"email": "sam@example.com"})
    tried = []
    while resp.status_code == 303 and resp.headers["location"].startswith(auth.AUTHORIZE_URL):
        params = _query(resp.headers["location"])
        tried.append(params["client_id"])
        resp = client.get("/callback", params={"code": "auth-code", "state": params["state"]})
    assert tried[0] == "app-two" and tried[-1] == "app-three" and resp.status_code == 303
    assert spotify_apps.app_for("sam@example.com") == "app-three"


def test_the_login_page_asks_for_the_email_only_when_it_helps(client, monkeypatch):
    assert 'name="email"' in client.get("/login").text
    client.cookies.set(SPOTIFY_APP_COOKIE, "app-two")
    assert 'name="email"' not in client.get("/login").text  # this browser knows its app
    client.cookies.clear()
    monkeypatch.delenv("SPOTIFY_EXTRA_CLIENT_IDS")
    get_settings.cache_clear()
    assert 'name="email"' not in client.get("/login").text  # one app: nothing to look up


def test_the_email_is_never_stored_on_the_user(client, respx_mock):
    Spotify(respx_mock)
    resp = client.post("/login", data={"email": "me@example.com"})
    params = _query(resp.headers["location"])
    client.get("/callback", params={"code": "auth-code", "state": params["state"]})
    with get_conn() as conn:
        assert "me@example.com" not in " ".join(str(v) for v in dict(users.get("friend1")).values())
        assert conn.execute("SELECT count(*) FROM spotify_app_members").fetchone()[0] == 1
