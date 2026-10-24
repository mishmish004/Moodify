"""Phase 24: the activity feed and mood timeline: consenting people only, titles never prompts."""
import json
from datetime import date, timedelta

import pytest

from src import activity, blend, playlists, sharing, users
from src.core.config import PROJECT_ROOT
from src.db import get_conn
from src.spotify import auth
from src.spotify.client import Track

from .conftest import default_brief
from .test_p6_playlist import logged_in, user  # noqa: F401

PROMPT = "my private words about tonight"


def _friend(spotify_id="friend2", name="Sam Rivera", show=True):
    users.upsert(spotify_id, name, f"refresh-{spotify_id}", auth.SCOPES)
    if show:
        sharing.save(spotify_id, True, False, False)
    return spotify_id


def _playlist(owner, title, days_ago=0, energy=6, minutes=42, sections=1, support=False, hidden=False, heard_as=None):
    brief = default_brief(title, support_note=support, sections=[
        {"label": f"Part {i}", "feeling": "x", "energy": energy, "minutes": None, "activity": None, "transition_in": "blend"}
        for i in range(sections)])
    n = max(1, round(minutes / 3.5))
    tracks = [Track(f"spotify:track:{owner}{title}{i}".replace(" ", ""), f"{title} song {i}", ("A",), 210_000) for i in range(n)]
    pid = playlists.insert(owner, PROMPT, "u", tracks, json.dumps(brief), [0] * n, [energy] * n)
    with get_conn() as conn:
        conn.execute("UPDATE playlists SET created_at = datetime('now', ?), hidden = ?, heard_as = ? WHERE id = ?",
                     (f"-{days_ago} days", int(hidden), heard_as, pid))
    return pid


def _lines(viewer="friend1"):
    return [f"{i.who} {i.verb} {i.title or ''}{i.after}" for i in activity.feed(viewer)]


# --- the feed -----------------------------------------------------------------------------------------------

def test_feed_holds_consenting_peoples_visible_playlists_newest_first(user):
    _friend("friend2", "Sam Rivera")
    _friend("friend3", "Priya Shah", show=False)
    _playlist("friend1", "Mine old", days_ago=3)
    _playlist("friend2", "Summer morning → workout → lock in", days_ago=1, minutes=56, sections=3)
    _playlist("friend3", "Priya's secret", days_ago=0)
    _playlist("friend2", "Too old", days_ago=40)
    assert _lines() == ["Sam made Summer morning → workout → lock in · 56 min · 3 parts", "You made Mine old · 42 min"]


def test_support_note_and_hidden_playlists_never_appear(user):
    _friend()
    _playlist("friend2", "Quiet company", support=True)
    _playlist("friend2", "Hidden one", hidden=True)
    _playlist("friend1", "My hard day", support=True)
    _playlist("friend1", "My hidden one", hidden=True)
    _playlist("friend2", "Visible")
    assert [i.title for i in activity.feed("friend1")] == ["My hidden one", "Visible"]  # your own hidden: yours to see
    assert {i.title for i in activity.feed("friend2")} == {"Visible", "Hidden one"}  # Sam's own, hidden one included
    timeline = activity.timeline("friend1")
    titles = [m["title"] for p in timeline["people"] for m in p["marks"]]
    assert "Quiet company" not in titles and "Hidden one" not in titles and "My hard day" not in titles


def test_prompts_never_appear_in_the_page_or_the_json(logged_in):
    _friend()
    sharing.save("friend1", True, False, False)
    _playlist("friend2", "Visible title")
    _playlist("friend1", "My title")
    page, data = logged_in.get("/activity"), logged_in.get("/activity.json")
    assert page.status_code == 200 and "Visible title" in page.text and "My title" in page.text
    assert PROMPT not in page.text and PROMPT not in data.text
    assert "prompt" not in data.text and "review" not in data.text and "rating" not in data.text


def test_the_way_youd_hear_it_shows_to_that_friend_only(user):
    _friend("friend2", "Sam Rivera")
    _friend("friend3", "Priya Shah")
    sharing.save("friend1", True, False, False)
    _playlist("friend1", "Gym like Sam", heard_as="friend2")
    assert _lines("friend2") == ["Friend made Gym like Sam the way you'd hear it · 42 min"]
    assert _lines("friend3") == []
    assert _lines("friend1") == ["You made Gym like Sam the way Sam would hear it · 42 min"]


def test_blends_show_to_the_people_in_them(user):
    _friend("friend2", "Sam Rivera", show=False)  # in the Blend, but doesn't show their taste
    _friend("friend3", "Priya Shah")
    sharing.save("friend1", True, False, False)
    pid = _playlist("friend1", "Sunday cooking")
    blend.save(pid, "friend1", blend.members_for(users.get("friend1"), ["friend2"]), [set()], None)
    assert _lines("friend1") == ["You made a Blend with Sam: Sunday cooking · 42 min"]
    assert _lines("friend2") == ["Friend made a Blend with you: Sunday cooking · 42 min"]
    assert _lines("friend3") == []  # not in it


def test_a_blend_by_someone_who_doesnt_show_their_taste_still_reaches_its_members(user):
    _friend("friend2", "Sam Rivera", show=False)
    pid = _playlist("friend2", "Sam's blend")
    blend.save(pid, "friend2", blend.members_for(users.get("friend2"), ["friend1"]), [set()], None)
    assert _lines("friend1") == ["Sam made a Blend with you: Sam's blend"]


def test_likes_and_ratings_are_yours_only(user):
    _friend()
    mine, theirs = _playlist("friend1", "Mine"), _playlist("friend2", "Theirs")
    playlists.set_rating(mine, "friend1", 8)
    playlists.set_rating(theirs, "friend2", 3)
    with get_conn() as conn:
        conn.execute("INSERT INTO track_likes (spotify_id, uri, playlist_id) VALUES ('friend1', 'spotify:track:friend1Mine0', ?)", (mine,))
        conn.execute("INSERT INTO track_likes (spotify_id, uri, playlist_id) VALUES ('friend2', 'spotify:track:friend2Theirs0', ?)", (theirs,))
    mine_lines = _lines("friend1")
    assert "You rated Mine 8/10" in mine_lines and "You liked Mine song 0" in mine_lines
    assert not any("3/10" in line or "Theirs song" in line for line in mine_lines)


# --- the timeline -------------------------------------------------------------------------------------------

def test_timeline_dates_and_energy(user):
    _friend()
    _playlist("friend1", "A", days_ago=2, energy=8, minutes=60)
    _playlist("friend2", "B", days_ago=10, energy=2)
    _playlist("friend2", "Old", days_ago=31)
    data = activity.timeline("friend1")
    assert len(data["days"]) == 30 and data["days"][-1] == date.today().isoformat()
    first = date.fromisoformat(data["days"][0])
    marks = [m for p in data["people"] for m in p["marks"]]
    assert [m["title"] for m in marks] == ["A", "B"]
    for m in marks:
        assert first <= date.fromisoformat(m["day"]) <= date.today() and 1 <= m["energy"] <= 10
    assert marks[0]["minutes"] == 60 and marks[0]["energy"] == 8.0
    assert [p["name"] for p in data["people"]] == ["Friend", "Sam"] and data["people"][0]["me"]


def test_energy_falls_back_to_the_brief(user):
    pid = _playlist("friend1", "No song energy", energy=4)
    with get_conn() as conn:
        conn.execute("UPDATE playlist_tracks SET energy = NULL WHERE playlist_id = ?", (pid,))
    [mark] = activity.timeline("friend1")["people"][0]["marks"]
    assert mark["energy"] == 4.0


# --- the page -----------------------------------------------------------------------------------------------

def test_page_and_static_files(logged_in):
    _playlist("friend1", "Mine")
    page = logged_in.get("/activity").text
    assert '<script src="/static/activity.js"></script>' in page and 'data-day="' in page
    assert "Replay the week" in page and 'aria-label="Previous day"' in page and 'aria-label="Next day"' in page
    assert logged_in.get("/static/activity.js").status_code == 200
    assert 'href="/activity"' in logged_in.get("/friends").text


def test_logged_out_goes_to_login(test_settings):
    from fastapi.testclient import TestClient
    from src.db import init_db
    from src.main import app
    init_db()
    with TestClient(app, follow_redirects=False) as client:
        assert client.get("/activity").headers["location"] == "/login"
        assert client.get("/activity.json").headers["location"] == "/login"


def test_reduced_motion_is_respected():
    css = (PROJECT_ROOT / "static" / "map.css").read_text()
    assert "@media (prefers-reduced-motion: reduce)" in css and "transition: none !important" in css
    js = (PROJECT_ROOT / "static" / "activity.js").read_text()
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in js and "if (still.matches) { move(1); return; }" in js
    assert "innerHTML" not in js
