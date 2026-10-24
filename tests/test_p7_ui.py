"""Phase 7: the Jinja pages. OpenAI and Spotify mocked over HTTP, with the Phase 6 helpers."""
import hashlib
import json
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from src import jobs, pages, playlists, taste, users
from src.db import get_conn, init_db
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
    assert '<meta name="viewport" content="width=device-width, initial-scale=1' in resp.text
    assert '<link rel="stylesheet" href="/static/style.css?v=' in resp.text


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


def test_stylesheet_and_scripts_are_fingerprinted(anon):
    """A new version of a static file gets a new URL, so a cached old copy is never paired with a new page."""
    digest = hashlib.sha256((STATIC / "style.css").read_bytes()).hexdigest()[:8]
    assert f'<link rel="stylesheet" href="/static/style.css?v={digest}">' in anon.get("/login").text
    assert anon.get(f"/static/style.css?v={digest}").status_code == 200


def test_static_files_are_served_with_their_types(anon):
    assert anon.get("/static/style.css").headers["content-type"].startswith("text/css")
    assert anon.get("/static/spotify-logo.svg").headers["content-type"].startswith("image/svg+xml")


# --- the canvas redesign: shell, onboarding, progress, playlist, messages -------------------------

def test_every_page_uses_the_design_shell(logged_in, respx_mock):
    FakeSpotify(respx_mock, {})
    html = logged_in.get("/history").text
    assert '<body class="mf">' in html and 'class="mf-wordmark"' in html
    # The same two links in the pill nav (laptops) and the tab bar (phones), the current one marked
    assert html.count('href="/history" aria-current="page"') == 2
    assert 'class="mf-tabs"' in html and "Logged in as Friend One" in html


def test_logged_out_pages_show_only_the_wordmark(anon):
    html = anon.get("/login").text
    assert 'class="mf-wordmark"' in html and 'class="mf-nav"' not in html and 'class="mf-tabs"' not in html


def test_onboarding_takes_genre_chips_other_genres_and_three_artists(logged_in):
    resp = logged_in.post("/onboarding", data={
        "genres": ["House", "R&B"], "genres_other": "indie rock, house",
        "artists": ["Frank Ocean", "", "SZA, KAYTRANADA"],
        "energy": "mixed", "instrumental": "sometimes", "familiar": "new",
    })
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    saved = json.loads(users.get("friend1")["onboarding_json"])
    assert saved["genres"] == "House, R&B, indie rock"  # repeats dropped, chips first
    assert saved["artists"] == "Frank Ocean, SZA, KAYTRANADA"
    assert saved["instrumental"] == "sometimes" and saved["familiar"] == "new"
    page = logged_in.get("/onboarding").text
    assert 'value="House" checked' in page and 'value="R&amp;B" checked' in page and 'value="Pop" checked' not in page
    assert 'name="instrumental" value="sometimes" checked' in page


def test_onboarding_with_a_missing_answer_shows_the_form_again(logged_in):
    resp = logged_in.post("/onboarding", data={"genres": ["Pop"], "artists": ["", "", ""], "energy": "low"})
    assert resp.status_code == 422 and resp.headers["content-type"].startswith("text/html")
    assert "One answer is missing." in resp.text and 'href="#q2"' in resp.text
    assert 'value="Pop" checked' in resp.text  # what was picked is kept
    assert 'id="artist-1" class="mf-input" type="text" name="artists"' in resp.text and 'aria-invalid="true"' in resp.text


def test_instrumental_sometimes_reads_as_a_soft_preference():
    text = taste.build_summary({"genres": "Pop", "instrumental": "sometimes"}, taste.Listening())
    assert "Instrumental tracks are fine now and then." in text and "No instrumental" not in text


def test_running_build_shows_its_steps(logged_in):
    job_id = jobs.create("friend1", "rainy sunday")
    with get_conn() as conn:
        conn.execute("UPDATE jobs SET step = ? WHERE id = ?", ("Finding the songs on Spotify (12/30)", job_id))
    html = logged_in.get(f"/jobs/{job_id}").text
    assert html.count('class="mf-step is-done"') == 3 and html.count('class="mf-step is-now"') == 1
    assert "Finding the songs on Spotify (12/30)" in html and 'style="width: 40%"' in html
    assert '<noscript><meta http-equiv="refresh" content="3"></noscript>' in html


def test_failed_build_stays_on_its_page_and_keeps_the_words(logged_in, respx_mock):
    job_id = jobs.create("friend1", "late train home, 30 min")
    with get_conn() as conn:
        conn.execute("UPDATE jobs SET step = ? WHERE id = ?", ("Finding the songs on Spotify (0/30)", job_id))
    jobs.fail(job_id, jobs.NO_MATCHES)
    html = logged_in.get(f"/jobs/{job_id}").text
    assert '<h1 class="mf-h1">Nothing matched</h1>' in html and 'http-equiv="refresh"' not in html
    assert 'class="mf-step is-failed"' in html and "Stopped here: 0 of 30 looked up" in html
    assert f'href="/?again={job_id}"' in html
    FakeSpotify(respx_mock, {})
    assert ">late train home, 30 min</textarea>" in logged_in.get(f"/?again={job_id}").text


def test_unknown_address_is_a_page(logged_in, respx_mock):
    resp = logged_in.get("/no/such/page")
    assert resp.status_code == 404 and resp.headers["content-type"].startswith("text/html")
    assert "Nothing here" in resp.text and "404 · Not found" in resp.text and 'href="/history"' in resp.text


def test_recent_playlists_show_their_length(logged_in, respx_mock):
    FakeSpotify(respx_mock, {})
    row = playlists.insert("friend1", "long one", "https://open.spotify.com/playlist/p1", tracks(*(f"spotify:track:{n}" for n in range(20))))
    html = logged_in.get("/").text
    assert "1 h · 20 tracks<" in html  # 20 × 3 min
    assert f'href="/playlists/{row}"' in html


# --- src/pages.py --------------------------------------------------------------------------------

def test_dates_and_lengths_in_words():
    assert pages.day("2026-10-18 09:12:00", today=date(2026, 10, 24)) == "Sun 18 Oct"
    assert pages.day("2025-08-02 09:12:00", today=date(2026, 10, 24)) == "Sat 2 Aug 2025"
    assert [pages.duration(m) for m in (0.4, 46, 60, 80)] == ["0 min", "46 min", "1 h", "1 h 20 min"]


@pytest.mark.parametrize("step, states", [
    (None, ["now", "wait", "wait", "wait", "wait"]),
    ("Reading your prompt", ["done", "now", "wait", "wait", "wait"]),
    ("Asking for a few more songs", ["done", "done", "done", "now", "wait"]),
    ("Saving the playlist to your Spotify", ["done", "done", "done", "done", "now"]),
])
def test_build_steps_follow_the_job(step, states):
    assert [s["state"] for s in pages.build_steps(step)] == states


def test_build_steps_mark_where_a_build_stopped():
    steps = pages.build_steps("Asking for song ideas", failed=True)
    assert [s["state"] for s in steps] == ["done", "done", "failed", "wait", "wait"]
    assert steps[2]["status"] == "Stopped here"


def test_energy_chart_draws_cuts_and_blends():
    groups = [
        {"label": "Summer morning", "start": "0:00", "start_ms": 0, "energy": 4, "cut": False},
        {"label": "Workout", "start": "14:00", "start_ms": 840_000, "energy": 8, "cut": False},
        {"label": "Lock in", "start": "42:00", "start_ms": 2_520_000, "energy": 6, "cut": True},
    ]
    chart = pages.energy_chart(groups, "58:00", 3_480_000)
    assert chart["line"].startswith("M8.0 64.0") and "C" in chart["line"] and "V46.0" in chart["line"]
    assert chart["area"].endswith("V100H8Z")
    assert [t["text"] for t in chart["ticks"]] == ["14:00", "42:00"]
    assert "a straight cut to Lock in 6 of 10 from 42:00" in chart["label"]
    assert chart["caption"] == "Energy, 1 to 10, across the 58 minutes. The change at 42:00 is a straight cut, as you asked."
    assert pages.energy_chart(groups[:1], "14:00", 840_000) is None


def test_onboarding_choices_round_trip():
    chips, other = pages.genre_choices("indie rock, bollywood, Lo-Fi")
    assert {c["name"] for c in chips if c["on"]} == {"Bollywood", "Lo-fi"} and other == "indie rock"
    assert pages.artist_fields("A, B, C, D") == ["A", "B", "C, D"]
    assert pages.artist_fields("") == ["", "", ""]
    assert pages.split_list("a, b", " A ,c", "") == ["a", "b", "c"]
