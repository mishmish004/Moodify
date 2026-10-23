"""Phase 7: the Jinja pages. OpenAI and Spotify mocked over HTTP, with the Phase 6 helpers."""
import re

import pytest
from fastapi.testclient import TestClient

from src import playlists, users
from src.db import init_db
from src.main import app
from src.spotify.client import API_URL

from .conftest import ROOT, finish_build, tracks
from .test_p6_playlist import CHAT_URL, FakeSpotify, _completion, _songs, logged_in, user  # noqa: F401 (fixtures)

STATIC = ROOT / "static"
STATIC_REF = re.compile(r"""/static/([^"'\s)?#]+)""")


@pytest.fixture
def anon(test_settings):
    init_db()
    with TestClient(app, follow_redirects=False) as client:
        yield client


def _make_playlist(client, respx_mock, n=12):
    respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(n)))
    FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(n)})
    return finish_build(client, client.post("/playlist", data={"mood": "rainy sunday"}))


# --- every page ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/", "/history", "/onboarding", "/login"])
def test_pages_are_200_for_an_onboarded_user(logged_in, respx_mock, path):
    FakeSpotify(respx_mock, {})
    resp = logged_in.get(path)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in resp.text
    assert '<link rel="stylesheet" href="/static/style.css">' in resp.text


def test_playlist_and_message_pages_use_the_layout(logged_in, respx_mock):
    made = _make_playlist(logged_in, respx_mock)
    row_id = playlists.recent("friend1")[0]["id"]
    respx_mock.post(f"{API_URL}/me/player/queue").respond(204)
    queued = logged_in.post(f"/queue/{row_id}")
    for resp in (made, queued):
        assert resp.status_code == 200
        assert '<meta name="viewport"' in resp.text and 'href="/history"' in resp.text


@pytest.mark.parametrize("method, path", [
    ("get", "/"), ("get", "/history"), ("get", "/onboarding"), ("post", "/playlist"), ("post", "/queue/1"),
])
def test_logged_out_redirects_to_login(anon, method, path):
    resp = anon.post(path, data={"mood": "x"}) if method == "post" else anon.get(path)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# --- login ----------------------------------------------------------------------------------

def test_login_page_explains_and_posts_to_login(anon):
    resp = anon.get("/login")
    assert resp.status_code == 200
    assert '<form method="post" action="/login">' in resp.text
    # Friends are told their taste goes to OpenAI before they log in (plan Phase 8)
    assert "OpenAI" in resp.text and "Disconnect" in resp.text
    assert 'href="/history"' not in resp.text  # no nav for logged-out visitors
    # Viewing the page starts nothing: no OAuth state, no cookie
    assert "set-cookie" not in resp.headers


def test_login_errors_are_html_pages(anon):
    resp = anon.get("/callback", params={"error": "access_denied"})
    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("text/html")
    assert "Login cancelled" in resp.text


# --- home -------------------------------------------------------------------------------------

def test_home_has_the_mood_form_history_link_and_disconnect(logged_in, respx_mock):
    FakeSpotify(respx_mock, {})
    html = logged_in.get("/").text
    form = re.search(r'<form method="post" action="/playlist"[^>]*>(.*?)</form>', html, re.S)
    assert form, "mood form posting to /playlist"
    assert "data-busy" in form.group(0)  # one submit only
    assert re.search(r'<textarea[^>]*name="mood"[^>]*required', form.group(1))
    assert 'href="/history"' in html
    assert '<form method="post" action="/disconnect"' in html


def test_home_attributes_spotify_next_to_the_taste_summary(logged_in, respx_mock):
    FakeSpotify(respx_mock, {})  # top artist: Radiohead
    html = logged_in.get("/").text
    assert "Radiohead" in html
    assert 'src="/static/spotify-logo.svg"' in html


def test_home_recent_playlists_link_to_spotify(logged_in, respx_mock):
    FakeSpotify(respx_mock, {})
    playlists.insert("friend1", "late night drive", "https://open.spotify.com/playlist/p1", tracks("spotify:track:a"))
    html = logged_in.get("/").text
    assert 'href="https://open.spotify.com/playlist/p1"' in html
    assert "1 track<" in html
    assert html.count('src="/static/spotify-icon.svg"') == 1


# --- playlist result --------------------------------------------------------------------------

def test_playlist_page_renders_every_track_with_spotify_links(logged_in, respx_mock):
    resp = _make_playlist(logged_in, respx_mock, n=12)
    html = resp.text
    assert resp.status_code == 200
    assert 'href="https://open.spotify.com/playlist/pl1"' in html
    assert 'src="/static/spotify-logo.svg"' in html and "OPEN SPOTIFY" in html
    for i in range(12):
        assert f">Song {i}<" in html and f">Artist {i}<" in html
        assert f'href="https://open.spotify.com/track/t{i}"' in html
    assert html.count('src="/static/spotify-icon.svg"') == 12
    assert re.search(r'<form method="post" action="/queue/\d+"', html)


def test_queued_page_links_to_the_playlist(logged_in, respx_mock):
    FakeSpotify(respx_mock, {})
    row_id = playlists.insert("friend1", "m", "https://open.spotify.com/playlist/p9", tracks("spotify:track:a"))
    respx_mock.post(f"{API_URL}/me/player/queue").respond(204)
    html = logged_in.post(f"/queue/{row_id}").text
    assert 'href="https://open.spotify.com/playlist/p9"' in html and "OPEN SPOTIFY" in html


# --- history ----------------------------------------------------------------------------------

def test_history_lists_every_playlist_newest_first(logged_in):
    for i in range(7):
        playlists.insert("friend1", f"mood {i}", f"https://open.spotify.com/playlist/p{i}", tracks(*(f"spotify:track:{n}" for n in range(i))))
    html = logged_in.get("/history").text
    positions = [html.index(f">mood {i}<") for i in reversed(range(7))]
    assert positions == sorted(positions)
    for i in range(7):
        assert f'href="https://open.spotify.com/playlist/p{i}"' in html
    assert "6 tracks<" in html and "1 track<" in html
    assert html.count('src="/static/spotify-icon.svg"') == 7


def test_history_shows_only_your_playlists(logged_in):
    users.upsert("friend2", "Two", "r2")
    playlists.insert("friend2", "someone else's mood", "https://open.spotify.com/playlist/x", [])
    html = logged_in.get("/history").text
    assert "someone else" not in html
    assert "Nothing yet" in html


def test_user_text_is_escaped(logged_in):
    playlists.insert("friend1", "<script>alert(1)</script>", "https://open.spotify.com/playlist/x", [])
    html = logged_in.get("/history").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# --- static files -------------------------------------------------------------------------------

def test_templates_only_reference_static_files_that_exist(anon):
    refs = {ref for path in (ROOT / "templates").glob("*.html") for ref in STATIC_REF.findall(path.read_text())}
    css_urls = re.findall(r"""url\(["']?([^"')]+)""", (STATIC / "style.css").read_text())
    refs |= {u for u in css_urls if not u.startswith("data:")}
    assert {"style.css", "spotify-logo.svg", "spotify-icon.svg"} <= refs
    missing = sorted(ref for ref in refs if not (STATIC / ref).is_file())
    assert not missing, f"referenced but not in static/: {missing}"
    for ref in refs:
        assert anon.get(f"/static/{ref}").status_code == 200


def test_static_files_are_served_with_their_types(anon):
    assert anon.get("/static/style.css").headers["content-type"].startswith("text/css")
    assert anon.get("/static/spotify-logo.svg").headers["content-type"].startswith("image/svg+xml")
