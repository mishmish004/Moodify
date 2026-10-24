"""Phase 17: on a phone, Spotify links open the Spotify app, and a finished playlist can open it by itself."""
import json
import sqlite3
from urllib.parse import unquote

import pytest

from src import links, playlists, users
from src.db import init_db
from src.spotify.client import Track

from .conftest import CHAT_URL, finish_build
from .test_p6_playlist import FakeSpotify, _completion, _songs, logged_in, user  # noqa: F401

IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
          "Version/18.0 Mobile/15E148 Safari/604.1")
IPAD = "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
ANDROID_CHROME = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0.0.0 Mobile Safari/537.36")
SAMSUNG = ("Mozilla/5.0 (Linux; Android 14; SAMSUNG SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) "
           "SamsungBrowser/26.0 Chrome/122.0.0.0 Mobile Safari/537.36")
DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"

PLAYLIST_ID = "37i9dQZF1DXcBWIGoYBM5M"
WEB = f"https://open.spotify.com/playlist/{PLAYLIST_ID}"
INTENT = (f"intent://playlist/{PLAYLIST_ID}#Intent;scheme=spotify;package=com.spotify.music;"
          f"S.browser_fallback_url=https%3A%2F%2Fopen.spotify.com%2Fplaylist%2F{PLAYLIST_ID};end")


# --- links ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("ua, expected_device, tap, auto", [
    (IPHONE, "ios", WEB, f"spotify:playlist:{PLAYLIST_ID}"),
    (IPAD, "ios", WEB, f"spotify:playlist:{PLAYLIST_ID}"),
    (ANDROID_CHROME, "android", INTENT, INTENT),
    (SAMSUNG, "android", INTENT, INTENT),
    (DESKTOP, "desktop", WEB, None),
    (MAC, "desktop", WEB, None),
    ("", "desktop", WEB, None),
    (None, "desktop", WEB, None),
])
def test_open_link_per_device(ua, expected_device, tap, auto):
    assert links.device(ua) == expected_device
    assert links.open_link("playlist", PLAYLIST_ID, ua) == tap
    assert links.auto_open_link("playlist", PLAYLIST_ID, ua) == auto


def test_android_fallback_is_the_web_page():
    link = links.open_link("track", "4uLU6hMCjMI75M1A2tKUQC", ANDROID_CHROME)
    fallback = link.split("S.browser_fallback_url=", 1)[1].split(";end")[0]
    assert unquote(fallback) == "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
    assert link.startswith("intent://track/4uLU6hMCjMI75M1A2tKUQC#Intent;scheme=spotify;package=com.spotify.music;")


@pytest.mark.parametrize("kind, spotify_id", [("playlist", "abc/../x"), ("playlist", "a b"), ("playlist", "x;end"),
                                               ("show", "abc"), ("playlist", "")])
def test_ids_that_arent_plain_spotify_ids_only_get_a_quoted_web_link(kind, spotify_id):
    for ua in (IPHONE, ANDROID_CHROME, DESKTOP):
        link = links.open_link(kind, spotify_id, ua)
        assert link.startswith(f"https://open.spotify.com/{kind}/") and "intent:" not in link
        assert "/" not in link[len(f"https://open.spotify.com/{kind}/"):] and " " not in link and ";" not in link
        assert links.auto_open_link(kind, spotify_id, ua) is None


@pytest.mark.parametrize("url, expected", [
    (WEB, ("playlist", PLAYLIST_ID)),
    (f"{WEB}?si=abc", ("playlist", PLAYLIST_ID)),
    ("https://open.spotify.com/intl-de/track/4uLU6hMCjMI75M1A2tKUQC", ("track", "4uLU6hMCjMI75M1A2tKUQC")),
    ("spotify:track:4uLU6hMCjMI75M1A2tKUQC", ("track", "4uLU6hMCjMI75M1A2tKUQC")),
    ("https://example.com/playlist/x", None),
    ("https://open.spotify.com/", None),
])
def test_parse(url, expected):
    assert links.parse(url) == expected


def test_store_links_per_device():
    assert links.GET_SPOTIFY["android"].startswith("https://play.google.com/store/apps/details?id=com.spotify.music")
    assert links.GET_SPOTIFY["ios"].startswith("https://apps.apple.com/")


# --- pages ----------------------------------------------------------------------------------------------

def _build(client, respx_mock, ua):
    client.headers["user-agent"] = ua
    respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(20)))
    FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(20)})
    return finish_build(client, client.post("/playlist", data={"mood": "rain"}))


@pytest.mark.parametrize("ua, auto", [(IPHONE, '"spotify:playlist:pl1"'), (ANDROID_CHROME, json.dumps(
    "intent://playlist/pl1#Intent;scheme=spotify;package=com.spotify.music;"
    "S.browser_fallback_url=https%3A%2F%2Fopen.spotify.com%2Fplaylist%2Fpl1;end"))])
def test_a_finished_build_on_a_phone_opens_spotify(logged_in, respx_mock, ua, auto):
    page = _build(logged_in, respx_mock, ua).text
    assert f"location.href = {auto};" in page and "sessionStorage" in page
    assert 'class="mf-btn mf-btn--spotify mf-btn--xl mf-btn--block"' in page and "GET SPOTIFY FREE" in page


def test_a_finished_build_on_desktop_only_has_the_button(logged_in, respx_mock):
    page = _build(logged_in, respx_mock, DESKTOP).text
    assert "location.href" not in page and "GET SPOTIFY FREE" not in page
    assert '<a class="mf-btn mf-btn--spotify mf-btn--xl mf-btn--block" href="https://open.spotify.com/playlist/pl1">' in page


def test_setting_off_means_no_auto_open(logged_in, respx_mock):
    logged_in.post("/settings", data={"likes_to_spotify": "on"})
    assert users.get("friend1")["open_spotify"] == 0 and users.get("friend1")["likes_to_spotify"] == 1
    page = _build(logged_in, respx_mock, IPHONE).text
    assert "location.href" not in page and 'class="mf-btn mf-btn--spotify mf-btn--xl mf-btn--block"' in page


def test_only_the_first_view_after_a_build_opens_spotify(logged_in, respx_mock):
    page = _build(logged_in, respx_mock, IPHONE)
    assert "location.href" in page.text
    pid = max(r["id"] for r in playlists.recent("friend1", limit=-1))
    assert "location.href" not in logged_in.get(f"/playlists/{pid}").text  # a later visit, or from History


def test_track_and_playlist_links_open_the_app_on_android(logged_in):
    pid = playlists.insert("friend1", "rain", WEB, [Track("spotify:track:4uLU6hMCjMI75M1A2tKUQC", "Song", ("A",), 200_000)])
    logged_in.headers["user-agent"] = ANDROID_CHROME
    page = logged_in.get(f"/playlists/{pid}").text
    assert 'href="intent://track/4uLU6hMCjMI75M1A2tKUQC#Intent;scheme=spotify;' in page
    assert f'<a class="mf-btn mf-btn--spotify mf-btn--xl mf-btn--block" href="intent://playlist/{PLAYLIST_ID}#Intent;' in page
    assert 'href="https://play.google.com/store/apps/details?id=com.spotify.music">GET SPOTIFY FREE</a>' in page
    assert f'href="intent://playlist/{PLAYLIST_ID}#Intent;' in logged_in.get("/history").text
    logged_in.headers["user-agent"] = DESKTOP
    page = logged_in.get(f"/playlists/{pid}").text
    assert 'href="https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"' in page and "intent:" not in page


def test_iphone_links_stay_web_links_for_universal_links(logged_in):
    pid = playlists.insert("friend1", "rain", WEB, [Track("spotify:track:4uLU6hMCjMI75M1A2tKUQC", "Song", ("A",), 200_000)])
    logged_in.headers["user-agent"] = IPHONE
    page = logged_in.get(f"/playlists/{pid}").text
    assert 'href="https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"' in page and "intent:" not in page
    assert 'href="https://apps.apple.com/app/spotify-music-and-podcasts/id324684580">GET SPOTIFY FREE</a>' in page


def test_settings_page_has_the_open_spotify_switch(logged_in):
    page = logged_in.get("/settings").text
    assert 'name="open_spotify" value="on" checked' in page
    logged_in.post("/settings", data={"open_spotify": "on"})
    assert users.get("friend1")["open_spotify"] == 1 and users.get("friend1")["likes_to_spotify"] == 0


def test_existing_users_get_open_spotify_on_after_migrating(test_settings):
    from .test_p9_jobs import V0_SCHEMA
    conn = sqlite3.connect(test_settings.DB_PATH)
    conn.executescript(V0_SCHEMA)
    conn.execute("INSERT INTO users (spotify_id, refresh_token) VALUES ('old', 'r')")
    conn.commit()
    conn.close()
    init_db()
    assert users.get("old")["open_spotify"] == 1
