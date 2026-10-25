"""Phase 27: YouTube Music. Google sign-in, the YouTube Data API (mocked over HTTP) for reads and writes, and
YouTube Music's search (stubbed: it's ytmusicapi, not httpx). Quota is counted per Pacific-time day."""
import base64
import html
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src import library, playlists, users
from src.core.config import Settings, get_settings
from src.core.exceptions import YouTubeError, YouTubeReauthRequired
from src.db import get_conn, init_db
from src.main import app
from src.music import Track
from src.youtube import auth, quota
from src.youtube import search as search_module
from src.youtube.client import API_URL, YouTubeClient, _duration_ms, _video_track

from .conftest import TEST_ENV, default_brief, finish_build
from .test_p4_taste import ONBOARDING
from .test_p6_playlist import CHAT_URL, SONG_MS, _completion, _songs
from .test_p16_memory import assert_no_rows_for

ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36"
USER = "youtube:g-123"


def _id_token(**claims) -> str:
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature"


def _video(video_id, title, channel, category="10", duration="PT3M30S"):
    return {"id": video_id, "snippet": {"title": title, "channelTitle": channel, "categoryId": category},
            "contentDetails": {"duration": duration}}


@pytest.fixture
def google(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "google-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "google-secret")
    get_settings.cache_clear()
    init_db()
    return get_settings()


class FakeYouTube:
    """respx routes for the Data API, and a stand-in for YouTube Music's search. Every searched song exists."""

    def __init__(self, respx_mock, monkeypatch, liked=(), subscriptions=(), own=(), items=None, search_errors=0):
        respx_mock.post(auth.TOKEN_URL).respond(200, json={"access_token": "ya29", "expires_in": 3600})
        self.liked = list(liked)
        respx_mock.get(f"{API_URL}/videos").mock(side_effect=self._videos)
        respx_mock.get(f"{API_URL}/subscriptions").respond(200, json={"items": [{"snippet": {"title": t}} for t in subscriptions]})
        respx_mock.get(f"{API_URL}/playlists").respond(200, json={"items": [{"id": i, "snippet": {"title": n}} for i, n in own]})
        self.items = items or {}
        respx_mock.get(f"{API_URL}/playlistItems").mock(side_effect=self._items)
        self.create = respx_mock.post(f"{API_URL}/playlists").respond(200, json={"id": "PLmood1"})
        self.insert = respx_mock.post(f"{API_URL}/playlistItems").respond(200, json={"id": "item"})
        self.rate = respx_mock.post(f"{API_URL}/videos/rate").respond(204)
        self.rating = respx_mock.get(f"{API_URL}/videos/getRating").respond(200, json={"items": [{"rating": "none"}]})
        self.searches, self.search_errors = [], search_errors
        monkeypatch.setattr(search_module, "search", self._search)

    def _videos(self, request):
        params = request.url.params
        if params.get("myRating") == "like":
            return httpx.Response(200, json={"items": self.liked})
        ids = params["id"].split(",")
        return httpx.Response(200, json={"items": [v for vids in self.items.values() for v in vids if v["id"] in ids]})

    def _items(self, request):
        videos = self.items.get(request.url.params["playlistId"], [])
        return httpx.Response(200, json={"items": [{"contentDetails": {"videoId": v["id"]}} for v in videos]})

    def _search(self, title, artist):
        self.searches.append((title, artist))
        if self.search_errors:
            self.search_errors -= 1
            raise YouTubeError(0, "search failed")
        return [Track(f"youtube:track:{title.replace(' ', '')}", title, (artist,), SONG_MS)]

    def inserted(self):
        return [json.loads(c.request.content)["snippet"]["resourceId"]["videoId"] for c in self.insert.calls]


def _log_in(client, respx_mock, scope=f"openid {auth.YOUTUBE_SCOPE}", refresh="1//refresh", sub="g-123"):
    location = client.post("/login/youtube").headers["location"]
    state = parse_qs(urlsplit(location).query)["state"][0]
    respx_mock.post(auth.TOKEN_URL).respond(200, json={
        "access_token": "ya29", "refresh_token": refresh, "expires_in": 3599, "scope": scope,
        "id_token": _id_token(sub=sub, name="Gita Rao")})
    return client.get("/callback/youtube", params={"code": "4/code", "state": state})


@pytest.fixture
def client(google):
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def signed_in(client, respx_mock):
    assert _log_in(client, respx_mock).status_code == 303
    users.set_onboarding(USER, ONBOARDING)
    respx_mock.routes.clear()
    return client


def _build(client, respx_mock, briefs, n=8, minutes=25, ua=None):
    briefs.install(respx_mock)
    briefs.answer = default_brief("evening walk", total_minutes=minutes)
    respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(n)))
    headers = {"user-agent": ua} if ua else {}
    return finish_build(client, client.post("/playlist", data={"mood": "evening walk"}, headers=headers))


# --- sign-in -------------------------------------------------------------------------------------------------

def test_the_youtube_button_shows_only_when_google_is_set_up(test_settings):
    init_db()
    with TestClient(app, follow_redirects=False) as c:
        assert "Log in with YouTube Music" not in c.get("/login").text
        assert c.post("/login/youtube").status_code == 404


def test_login_page_offers_youtube_and_its_terms(client):
    page = client.get("/login").text
    assert "Log in with YouTube Music" in page and "Log in with Spotify" in page
    assert 'href="https://www.youtube.com/t/terms"' in page and 'href="/privacy"' in page


def test_authorize_url_asks_for_offline_access_with_pkce(client):
    location = client.post("/login/youtube").headers["location"]
    params = parse_qs(urlsplit(location).query)
    assert location.startswith(auth.AUTHORIZE_URL)
    assert params["scope"] == [auth.SCOPES] and params["access_type"] == ["offline"] and params["prompt"] == ["consent"]
    assert params["code_challenge_method"] == ["S256"] and params["redirect_uri"] == ["http://127.0.0.1:9091/callback/youtube"]


def test_a_new_youtube_user_from_the_id_token(client, respx_mock):
    resp = _log_in(client, respx_mock)
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    user = users.get(USER)
    assert (user["service"], user["display_name"], user["refresh_token"]) == ("youtube", "Gita Rao", "1//refresh")
    assert user["likes_to_service"] == 0  # off until they say so (YouTube's policy)
    form = parse_qs(respx_mock.calls.last.request.content.decode())
    assert form["client_secret"] == ["google-secret"] and form["code_verifier"][0]


def test_a_second_login_updates_the_token_and_keeps_everything_else(client, respx_mock):
    _log_in(client, respx_mock)
    users.set_onboarding(USER, ONBOARDING)
    users.set_likes_to_service(USER, True)
    _log_in(client, respx_mock, refresh="1//newer")
    user = users.get(USER)
    assert user["refresh_token"] == "1//newer" and json.loads(user["onboarding_json"]) == ONBOARDING
    assert user["likes_to_service"] == 1


def test_a_login_without_the_youtube_permission_is_refused(client, respx_mock):
    resp = _log_in(client, respx_mock, scope="openid https://www.googleapis.com/auth/userinfo.profile")
    assert resp.status_code == 403 and "Moodify needs YouTube access" in resp.text
    assert users.get(USER) is None


def test_a_state_from_the_spotify_login_doesnt_finish_a_google_one(client, respx_mock):
    state = parse_qs(urlsplit(client.post("/login").headers["location"]).query)["state"][0]
    resp = client.get("/callback/youtube", params={"code": "c", "state": state})
    assert resp.status_code == 400 and "Login expired" in resp.text


def test_a_dead_refresh_token_means_signing_in_again(google, respx_mock):
    respx_mock.post(auth.TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    with pytest.raises(YouTubeReauthRequired), YouTubeClient(USER, "dead") as yt:
        yt.my_playlists()


def test_settings_need_the_secret_and_https_in_prod():
    with pytest.raises(ValidationError, match="GOOGLE_CLIENT_SECRET"):
        Settings(**TEST_ENV, GOOGLE_CLIENT_ID="x", _env_file=None)
    with pytest.raises(ValidationError, match="GOOGLE_REDIRECT_URI"):
        Settings(**{**TEST_ENV, "ENV": "prod", "SPOTIFY_REDIRECT_URI": "https://m.example/callback"},
                 GOOGLE_CLIENT_ID="x", GOOGLE_CLIENT_SECRET="y", _env_file=None)


# --- reading the taste -------------------------------------------------------------------------------------

@pytest.mark.parametrize("video, expected", [
    (_video("a", "Midnight City", "M83 - Topic"), ("Midnight City", ("M83",))),
    (_video("b", "M83 - Midnight City (Official Video)", "M83VEVO"), ("Midnight City", ("M83",))),
    (_video("c", "Kun Faya Kun [Official Music Video]", "T-Series"), ("Kun Faya Kun", ("T-Series",))),
    (_video("d", "Unboxing my new phone", "Tech Bro", category="28"), None),
    (_video("e", "Lofi radio 24/7", "Lofi Girl", duration="P0D"), None),
])
def test_a_liked_video_as_a_track(video, expected):
    track = _video_track(video)
    if expected is None:
        assert track is None
        return
    assert (track.name, track.artists) == expected
    assert track.uri == f"youtube:track:{video['id']}" and track.duration_ms == 210_000


@pytest.mark.parametrize("iso, ms", [("PT4M4S", 244_000), ("PT1H2M3S", 3_723_000), ("PT45S", 45_000), ("P0D", 0), ("", 0)])
def test_durations(iso, ms):
    assert _duration_ms(iso) == ms


def test_listening_reads_likes_subscriptions_and_own_playlists(google, respx_mock, monkeypatch):
    road = [_video("r1", "Ilahi", "Arijit Singh - Topic"), _video("r2", "Not music", "Vlogs", category="22")]
    FakeYouTube(respx_mock, monkeypatch,
                liked=[_video("l1", "Midnight City", "M83 - Topic"), _video("l2", "A talk", "TED", category="27")],
                subscriptions=["M83 - Topic", "Prateek KuhadVEVO", "Veritasium"],
                own=[("PLroad", "Road trip"), ("PLmoodify", "Moodify: rainy sunday")], items={"PLroad": road})
    with YouTubeClient(USER, "1//refresh") as yt:
        listening = yt.listening()
    assert [t.name for t in listening.liked] == ["Midnight City"]
    assert listening.followed == ["M83", "Prateek Kuhad", "Veritasium"]
    assert listening.playlist_names == ["Road trip"]  # Moodify's own playlists would only echo it
    assert [t.uri for t in listening.playlist_tracks] == ["youtube:track:r1"]
    assert listening.complete and not listening.top_tracks and not listening.recent_tracks
    # liked (1 page) + subscriptions + playlists + one playlist's items and videos: 5 units
    assert quota.spent() == 5


# --- building ------------------------------------------------------------------------------------------------

def test_a_build_makes_one_private_playlist_with_one_insert_per_song(signed_in, respx_mock, monkeypatch, briefs):
    yt = FakeYouTube(respx_mock, monkeypatch, liked=[_video("l1", "Midnight City", "M83 - Topic")])
    page = _build(signed_in, respx_mock, briefs)
    assert page.status_code == 200
    body = json.loads(yt.create.calls.last.request.content)
    assert body["status"] == {"privacyStatus": "private"} and body["snippet"]["title"] == "Moodify: evening walk"
    row = playlists.recent(USER)[0]
    uris = json.loads(row["track_uris_json"])
    assert yt.create.call_count == 1 and yt.insert.call_count == len(uris) and uris
    assert yt.inserted() == [u.rsplit(":", 1)[1] for u in uris] and all(u.startswith("youtube:track:") for u in uris)
    assert row["url"] == "https://music.youtube.com/playlist?list=PLmood1"
    reads = 3  # liked, subscriptions, playlists
    assert quota.spent() == reads + 50 * (1 + len(uris))
    assert [t.uri for t, _ in library.for_user(USER)] == ["youtube:track:l1"]


def test_a_build_that_cant_afford_its_writes_stops_before_any(signed_in, respx_mock, monkeypatch, briefs):
    monkeypatch.setenv("YOUTUBE_DAILY_QUOTA", "200")
    get_settings.cache_clear()
    yt = FakeYouTube(respx_mock, monkeypatch)
    page = _build(signed_in, respx_mock, briefs)
    assert "YouTube's daily limit is used up" in html.unescape(page.text) and "India time" in page.text
    assert yt.create.call_count == 0 and yt.insert.call_count == 0 and playlists.recent(USER) == []


def test_googles_own_quota_answer_ends_the_build_the_same_way(signed_in, respx_mock, monkeypatch, briefs):
    yt = FakeYouTube(respx_mock, monkeypatch)
    yt.insert.respond(403, json={"error": {"code": 403, "errors": [{"reason": "quotaExceeded"}]}})
    page = _build(signed_in, respx_mock, briefs)
    assert "YouTube's daily limit is used up" in html.unescape(page.text)


def test_an_account_without_a_channel_is_told_how_to_make_one(signed_in, respx_mock, monkeypatch, briefs):
    yt = FakeYouTube(respx_mock, monkeypatch)
    yt.create.respond(401, json={"error": {"code": 401, "errors": [{"reason": "youtubeSignupRequired"}]}})
    page = _build(signed_in, respx_mock, briefs)
    assert "needs a YouTube channel" in page.text and "youtube.com/create_channel" in page.text
    assert yt.create.call_count == 1  # a 401 with this reason isn't an expired token: no refresh and retry


def test_three_search_failures_in_a_row_stop_the_build(signed_in, respx_mock, monkeypatch, briefs):
    yt = FakeYouTube(respx_mock, monkeypatch, search_errors=3)
    page = _build(signed_in, respx_mock, briefs)
    assert "YouTube Music isn't answering" in html.unescape(page.text) and len(yt.searches) == 3
    assert yt.create.call_count == 0


def test_a_search_failure_or_two_only_cost_those_songs(signed_in, respx_mock, monkeypatch, briefs):
    yt = FakeYouTube(respx_mock, monkeypatch, search_errors=2)
    page = _build(signed_in, respx_mock, briefs, n=10, minutes=20)
    assert page.status_code == 200 and yt.create.call_count == 1
    assert ("Song 0", "Artist 0") in yt.searches and "youtube:track:Song0" not in json.loads(playlists.recent(USER)[0]["track_uris_json"])


def test_angle_brackets_never_reach_youtube(google, respx_mock, monkeypatch):
    yt = FakeYouTube(respx_mock, monkeypatch)
    with YouTubeClient(USER, "1//refresh") as client:
        client.create_playlist("Moodify: <3 songs", "a > b")
    snippet = json.loads(yt.create.calls.last.request.content)["snippet"]
    assert "<" not in snippet["title"] and ">" not in snippet["description"]


# --- likes ------------------------------------------------------------------------------------------------------

def _like(client, playlist_id, position, action="like"):
    return client.post(f"/playlists/{playlist_id}/tracks/{position}/{action}", headers={"accept": "application/json"})


def test_likes_stay_in_moodify_until_switched_on(signed_in, respx_mock, monkeypatch, briefs):
    yt = FakeYouTube(respx_mock, monkeypatch)
    _build(signed_in, respx_mock, briefs)
    playlist_id = playlists.recent(USER)[0]["id"]
    assert _like(signed_in, playlist_id, 0).json() == {"liked": True, "notice": None}
    assert yt.rate.call_count == 0 and yt.rating.call_count == 0
    signed_in.post("/settings", data={"likes_to_service": "on"})
    _like(signed_in, playlist_id, 1)
    assert yt.rate.calls.last.request.url.params["rating"] == "like"
    _like(signed_in, playlist_id, 1, "unlike")
    assert yt.rate.calls.last.request.url.params["rating"] == "none"
    # The first like was made with the setting off, so Moodify never liked it on YouTube: unliking leaves YouTube alone
    rates = yt.rate.call_count
    _like(signed_in, playlist_id, 0, "unlike")
    assert yt.rate.call_count == rates


def test_onboarding_asks_youtube_users_about_likes(signed_in):
    page = signed_in.get("/onboarding").text
    assert "Also like songs on YouTube when I ♥ them in Moodify" in page
    form = {"genres": "indie", "artists": "M83", "energy": "medium", "instrumental": "yes", "familiar": "mix",
            "likes_to_service": "on"}
    assert signed_in.post("/onboarding", data=form).status_code == 303
    assert users.get(USER)["likes_to_service"] == 1


# --- pages, links and leaving -----------------------------------------------------------------------------------

def test_pages_link_to_youtube_music_and_have_no_queue(signed_in, respx_mock, monkeypatch, briefs):
    FakeYouTube(respx_mock, monkeypatch)
    page = _build(signed_in, respx_mock, briefs, ua=ANDROID)
    playlist_id = playlists.recent(USER)[0]["id"]
    assert "Open in YouTube Music" in page.text and "Play now" not in page.text and "spotify" not in page.text.lower()
    phone = signed_in.get(f"/playlists/{playlist_id}?ready=1", headers={"user-agent": ANDROID}).text
    assert "intent://music.youtube.com/playlist?list=PLmood1#Intent;scheme=https;package=com.google.android.apps.youtube.music" in phone
    assert "No YouTube Music app?" in phone
    desktop = signed_in.get(f"/playlists/{playlist_id}").text
    assert 'href="https://music.youtube.com/playlist?list=PLmood1"' in desktop
    assert "https://music.youtube.com/watch?v=Song1" in desktop


def test_the_privacy_page_says_what_youtubes_policy_asks(client):
    page = client.get("/privacy").text
    for needed in ("https://www.youtube.com/t/terms", "https://policies.google.com/privacy",
                   "https://myaccount.google.com/connections", "YouTube API Services", "Ask whoever sent you the link"):
        assert needed in page, needed


def test_disconnect_takes_back_googles_token_and_every_row(signed_in, respx_mock, monkeypatch, briefs):
    FakeYouTube(respx_mock, monkeypatch, liked=[_video("l1", "Midnight City", "M83 - Topic")])
    _build(signed_in, respx_mock, briefs)
    revoke = respx_mock.post(auth.REVOKE_URL).respond(200)
    page = signed_in.post("/disconnect")
    assert revoke.call_count == 1 and parse_qs(revoke.calls.last.request.content.decode())["token"] == ["1//refresh"]
    assert "myaccount.google.com/connections" in page.text
    assert_no_rows_for(USER)


# --- quota ---------------------------------------------------------------------------------------------------

def test_the_quota_day_is_pacific_and_resets_at_its_midnight():
    autumn = datetime(2026, 10, 25, 6, 30, tzinfo=timezone.utc)  # 23:30 on the 24th in Los Angeles (PDT)
    assert quota.day(autumn) == "2026-10-24"
    assert quota.resets_at(autumn) == datetime(2026, 10, 25, 7, 0, tzinfo=timezone.utc)
    assert "12:30 PM India time" in quota.words(autumn)[1]
    winter = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)  # PST: an hour later in India
    assert "1:30 PM India time" in quota.words(winter)[1]


def test_spending_adds_up_per_day(google):
    quota.spend(50)
    quota.spend(1)
    assert quota.spent() == 51 and quota.left() == 10_000 - 51


# --- live ----------------------------------------------------------------------------------------------------

@pytest.mark.live
def test_live_youtube_reads_and_search():
    """Needs a YouTube user in data/moodify.db (sign in on the site first). Reads only: no playlist is made."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE service = 'youtube' LIMIT 1").fetchone()
    if row is None:
        pytest.skip("no YouTube user signed in yet")
    with YouTubeClient.for_user(row) as yt:
        listening = yt.listening()
        found = yt.search_track("Midnight City", "M83")
    print(f"\n{len(listening.liked)} liked, {len(listening.followed)} subscriptions, {len(listening.playlist_tracks)}"
          f" playlist songs; search: {found[0] if found else None}; quota today: {quota.spent()}")
    assert found and found[0].artists[0] == "M83"
