"""Phase 23: the taste map: who's on it, what its data may hold, and how it's drawn."""
import json
import re
import shutil
import subprocess

import pytest

from src import library, playlists, sharing, users
from src.core.config import PROJECT_ROOT
from src.db import get_conn
from src.spotify import auth
from src.spotify.client import Track

from .conftest import default_brief
from .test_p6_playlist import logged_in, user  # noqa: F401

SECRETS = ("my secret prompt", "my secret review", "SUPPORT ONLY SONG", "lesson about me", "refresh-")


def _person(spotify_id, name, genre, energy, show=True, n=6):
    users.upsert(spotify_id, name, f"refresh-{spotify_id}", auth.SCOPES)
    tracks = [Track(f"spotify:track:{spotify_id}{i}", f"{genre} {i}", (f"{name} Artist",), 200_000) for i in range(n)]
    shared = Track("spotify:track:shared", "Shared Song", ("Frank Ocean",), 200_000)
    library.replace(spotify_id, {"top": tracks[:2] + [shared], "liked": tracks[2:]})
    with get_conn() as conn:
        for i, t in enumerate(tracks + [shared]):
            conn.execute("INSERT OR IGNORE INTO track_features (uri, energy, mood, texture, groove, vocals, era, genres_json,"
                         " language, model) VALUES (?, ?, 5, 5, 5, 5, ?, ?, 'English', 'test')",
                         (t.uri, energy, 1990 + 10 * (i % 3), json.dumps([genre])))
    if show:
        sharing.save(spotify_id, True, False, False)
    return spotify_id


def _secrets_for(owner):
    pid = playlists.insert(owner, "my secret prompt", "u", [Track("spotify:track:x", "Song", ("A",), 1)],
                           json.dumps(default_brief("Fine title")))
    playlists.set_rating(pid, owner, 2)
    playlists.set_review(pid, owner, "my secret review")
    playlists.insert(owner, "hard day", "u", [Track("spotify:track:support", "SUPPORT ONLY SONG", ("A",), 1)],
                     json.dumps(default_brief("x", support_note=True)))
    with get_conn() as conn:
        conn.execute("INSERT INTO lessons (spotify_id, title, description, content, kind, evidence) VALUES (?, 'lesson about me',"
                     " 'd', 'c', 'preference', 'e')", (owner,))


def _walk(value, path=""):
    """Every key and value in a JSON document, as (path, text)."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield f"{path}.{k}", str(k)
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, str(value)


# --- who's on it --------------------------------------------------------------------------------------------

def test_only_you_and_people_who_show_their_taste(logged_in):
    _person("friend1", "Friend One", "rap", 8, show=False)
    _person("friend2", "Sam Rivera", "jazz", 3)
    _person("friend3", "Priya Shah", "pop", 6, show=False)
    sharing.save("friend3", False, True, True)  # shares other things, not taste
    data = logged_in.get("/map.json").json()
    assert [p["id"] for p in data["people"]] == ["friend1", "friend2"] and data["people"][0]["me"]
    sharing.save("friend2", False, False, False)
    assert [p["id"] for p in logged_in.get("/map.json").json()["people"]] == ["friend1"]


def test_opening_the_map_logs_one_view_per_friend_shown(logged_in):
    _person("friend1", "Friend One", "rap", 8)
    _person("friend2", "Sam", "jazz", 3)
    _person("friend3", "Priya", "pop", 6)
    logged_in.get("/map.json")
    with get_conn() as conn:
        rows = [tuple(r) for r in conn.execute("SELECT owner, viewer, kind FROM taste_access ORDER BY owner")]
    assert rows == [("friend2", "friend1", "view"), ("friend3", "friend1", "view")]


# --- what it holds ------------------------------------------------------------------------------------------

def test_no_prompts_reviews_ratings_lessons_or_support_note_data(logged_in):
    _person("friend1", "Friend One", "rap", 8)
    _person("friend2", "Sam", "jazz", 3)
    for owner in ("friend1", "friend2"):
        _secrets_for(owner)
        library.replace(owner, {"liked": [Track(f"spotify:track:{owner}0", "rap 0", ("A",), 200_000)]})
    raw = logged_in.get("/map.json").text
    for path, text in _walk(json.loads(raw)):
        for secret in SECRETS:
            assert secret not in text, path
        assert path.rsplit(".", 1)[-1] not in {"prompt", "review", "rating", "support_note", "lessons", "refresh_token"}, path
    assert "my secret" not in raw and "SUPPORT ONLY" not in raw


def test_every_track_links_to_spotify_and_values_are_in_range(logged_in):
    _person("friend1", "Friend One", "rap", 8)
    _person("friend2", "Sam", "jazz", 3)
    data = logged_in.get("/map.json").json()
    ranges = {d["key"]: (d["min"], d["max"]) for d in data["dimensions"]}
    assert set(ranges) == {"energy", "mood", "texture", "groove", "vocals", "era"}
    tracks = [t for p in data["people"] for t in p["tracks"]]
    assert tracks
    for t in tracks:
        assert re.fullmatch(r"https://open\.spotify\.com/track/[A-Za-z0-9]+", t["url"])
        for key in ("energy", "mood", "texture", "groove", "vocals"):
            assert 1 <= t[key] <= 10
        assert 1900 <= t["era"] <= 2020 and t["era"] % 10 == 0
    for p in data["people"]:
        for key, (low, high) in ranges.items():
            mean = p["profile"][key]["mean"]
            assert mean is None or (key == "era" or low <= mean <= high)
        assert abs(sum(share for _, share in p["genres"]) - 1) < 0.01


def test_common_ground_match_and_genre_colours(logged_in):
    _person("friend1", "Friend One", "rap", 8)
    _person("friend2", "Sam", "jazz", 3)
    data = logged_in.get("/map.json").json()
    assert data["genres"][:2] in (["jazz", "rap"], ["rap", "jazz"])  # every person's share, coloured the same for all
    [pair] = data["match"]
    assert {pair["a"], pair["b"]} == {"friend1", "friend2"} and 0 <= pair["match"] <= 100
    assert data["common"][0]["tracks"][0]["name"] == "Shared Song"
    assert [t["common"] for t in data["people"][0]["tracks"] if t["name"] == "Shared Song"] == [True]


def test_someone_with_nothing_judged_yet(logged_in):
    users.upsert("friend1", "Friend One", "refresh-1", auth.SCOPES)
    data = logged_in.get("/map.json").json()
    me = data["people"][0]
    assert me["tracks"] == [] and me["profile"]["energy"]["mean"] is None and data["match"] == []


# --- the page -----------------------------------------------------------------------------------------------

def test_page_and_static_files(logged_in):
    page = logged_in.get("/map")
    assert page.status_code == 200
    assert '<script src="/static/map.js"></script>' in page.text and 'href="/static/map.css"' in page.text
    assert 'data-src="/map.json"' in page.text and "As a table" not in page.text  # tables are built by map.js
    for path in ("/static/map.js", "/static/map.css"):
        assert logged_in.get(path).status_code == 200
    assert 'href="/map"' in logged_in.get("/friends").text


def test_logged_out_goes_to_login(test_settings):
    from fastapi.testclient import TestClient
    from src.db import init_db
    from src.main import app
    init_db()
    with TestClient(app, follow_redirects=False) as client:
        assert client.get("/map").headers["location"] == "/login"
        assert client.get("/map.json").headers["location"] == "/login"


def test_reduced_motion_turns_off_transitions():
    css = (PROJECT_ROOT / "static" / "map.css").read_text()
    block = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    assert "transition: none !important" in block.split("}")[0] + block.split("}")[1]
    js = (PROJECT_ROOT / "static" / "map.js").read_text()
    assert 'matchMedia("(prefers-reduced-motion: reduce)")' in js


def test_map_js_sets_names_as_text_never_html():
    js = (PROJECT_ROOT / "static" / "map.js").read_text()
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "textContent" in js


def _palette(mode):
    css = (PROJECT_ROOT / "static" / "map.css").read_text()
    block = css.split(f"/* palette:{mode} */", 1)[1].split("/* /palette */", 1)[0]
    return re.findall(r"--genre-\d+:\s*(#[0-9a-fA-F]{6})", block)


@pytest.mark.parametrize("mode, surface", [("light", "#ffffff"), ("dark", "#121212")])
def test_palette_passes_the_dataviz_validator(mode, surface):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node isn't installed: run scripts/validate_palette.js by hand")
    colours = _palette(mode)
    assert len(colours) == 6
    result = subprocess.run([node, str(PROJECT_ROOT / "scripts" / "validate_palette.js"), ",".join(colours),
                             "--mode", mode, "--surface", surface], capture_output=True, text=True, timeout=60)
    print(result.stdout)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout.replace("ALL CHECKS PASS", "")
