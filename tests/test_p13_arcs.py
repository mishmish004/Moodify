"""Phase 13: sections play in the order asked, at the times asked, switching the way asked."""
import json

import pytest

from src import jobs, playlists
from src.arc import arrange, timeline
from src.brief import Brief
from src.db import get_conn
from src.spotify.client import API_URL, SpotifyClient, Track
from src.spotify import auth

from .conftest import CHAT_URL, default_brief, finish_build
from .test_p6_playlist import FakeSpotify, _completion, _playlist_rows, logged_in, user  # noqa: F401


def _sections(*spec):
    """spec: (energy, minutes, transition_in) per section."""
    return [{"label": f"S{i}", "feeling": "x", "energy": e, "minutes": m, "activity": None, "transition_in": t}
            for i, (e, m, t) in enumerate(spec, start=1)]


def _brief(*spec, **changes) -> Brief:
    return Brief.model_validate(default_brief("Arc", sections=_sections(*spec), **changes))


def _t(name):
    return Track(f"spotify:track:{name}", name, ("A",), 210_000)


# --- ordering (pure) ---------------------------------------------------------------------------------

def test_three_sections_climb_bridge_and_cut():
    # The 2026-10-23 shape: morning (4) eases into workout (8), then a hard cut to focus (5)
    brief = _brief((4, None, "blend"), (8, 30, "blend"), (5, None, "cut"))
    morning = [_t("m3"), _t("m5"), _t("m4")]
    workout = [_t("w9"), _t("bridge5"), _t("w8"), _t("bridge6"), _t("bridge7")]
    focus = [_t("f6"), _t("bridgeF"), _t("f4"), _t("f5")]
    energy = {"m3": 3, "m5": 5, "m4": 4, "w9": 9, "bridge5": 5, "w8": 8, "bridge6": 6, "bridge7": 7,
              "f6": 6, "bridgeF": 7, "f4": 4, "f5": 5}
    energy = {f"spotify:track:{k}": v for k, v in energy.items()}
    roles = {f"spotify:track:{k}": "bridge" for k in ("bridge5", "bridge6", "bridge7", "bridgeF")}

    out = arrange([morning, workout, focus], energy, roles, brief)
    names = [[t.name for t in section] for section in out]
    assert names[0] == ["m3", "m4", "m5"]                     # climbs toward the workout
    assert names[1][:2] == ["bridge5", "bridge6"]             # at most 2 bridges, closest to the morning first
    assert names[1][2:] == ["w9", "w8", "bridge7"]            # then falls toward focus (5)
    assert names[2][0] == "f5"                                 # hard cut: opens at the section's own energy
    assert "bridgeF" in names[2] and names[2][0] != "bridgeF"  # no bridge opening after a cut


def test_calm_down_arc_falls_all_the_way():
    brief = _brief((6, None, "blend"), (3, None, "blend"), (1, None, "blend"))
    sections = [[_t("a5"), _t("a7"), _t("a6")], [_t("b2"), _t("b4"), _t("b3")], [_t("c2"), _t("c1")]]
    energy = {f"spotify:track:{n}": int(n[1]) for s in sections for n in (t.name for t in s)}
    out = arrange(sections, energy, {}, brief)
    flat = [energy[t.uri] for s in out for t in s]
    assert flat == sorted(flat, reverse=True)


def test_single_section_keeps_the_models_order():
    brief = _brief((5, None, "blend"))
    tracks = [_t("x"), _t("y"), _t("z")]
    energy = {"spotify:track:x": 9, "spotify:track:y": 1, "spotify:track:z": 5}
    assert arrange([tracks], energy, {}, brief) == [tracks]


def test_missing_energy_counts_as_the_sections_own():
    brief = _brief((5, None, "blend"), (9, None, "blend"))
    tracks = [_t("hi"), _t("unknown"), _t("lo")]
    out = arrange([tracks, []], {"spotify:track:hi": 8, "spotify:track:lo": 2}, {}, brief)
    assert [t.name for t in out[0]] == ["lo", "unknown", "hi"]


def test_timeline():
    brief = _brief((4, None, "blend"), (8, 30, "blend"), (5, None, "cut"))
    assert timeline(brief, [14.2, 29.6, 14.0]) == "0–14 S1 → 14–44 S2 → 44–58 S3"
    assert timeline(brief, [14.2, 0, 14.0]) == "0–14 S1 → 14–28 S3"  # a section that came out empty is left out


# --- Spotify keeps the order ----------------------------------------------------------------------------

def test_add_items_keeps_order_across_batches(user, respx_mock):
    respx_mock.post(auth.TOKEN_URL).respond(200, json={"access_token": "a", "expires_in": 3600})
    route = respx_mock.post(f"{API_URL}/playlists/p/items").respond(201, json={"snapshot_id": "s"})
    uris = [f"spotify:track:{i:03d}" for i in range(150)]
    with SpotifyClient("friend1", "refresh-1") as sp:
        sp.add_items("p", uris)
    sent = [u for call in route.calls for u in json.loads(call.request.content)["uris"]]
    assert sent == uris and route.call_count == 2


# --- a whole build ----------------------------------------------------------------------------------------------

def _arc_build(logged_in, respx_mock, briefs, transitions=("blend", "blend", "cut")):
    briefs.answer = default_brief("Summer morning → workout → lock in",
                                  sections=[{**s, "label": label} for s, label in zip(
                                      _sections((4, 14, transitions[0]), (8, 28, transitions[1]), (5, 14, transitions[2])),
                                      ("Summer morning", "Workout", "Lock in"))])
    songs = (
        [{"title": f"Morning {i}", "artist": f"M{i}", "section": 1, "energy": e, "role": "build"} for i, e in enumerate([5, 3, 4, 4])]
        + [{"title": "Bridge", "artist": "B", "section": 2, "energy": 6, "role": "bridge"}]
        + [{"title": f"Workout {i}", "artist": f"W{i}", "section": 2, "energy": e, "role": "peak"} for i, e in enumerate([9, 8, 7, 9, 8, 7, 8])]
        + [{"title": "Focus bridge", "artist": "FB", "section": 3, "energy": 7, "role": "bridge"}]
        + [{"title": f"Focus {i}", "artist": f"F{i}", "section": 3, "energy": e, "role": "build"} for i, e in enumerate([4, 5, 6])]
    )
    respx_mock.post(CHAT_URL).respond(200, json=_completion(songs))
    sp = FakeSpotify(respx_mock, {s["title"]: s["title"].replace(" ", "") for s in songs})
    post = logged_in.post("/playlist", data={"mood": "summer morning, workout, then instantly lock in"})
    return sp, post, finish_build(logged_in, post)


def test_build_orders_sections_and_shows_them_with_start_times(logged_in, respx_mock, briefs):
    sp, post, page = _arc_build(logged_in, respx_mock, briefs)
    names = [u.rsplit(":", 1)[1] for u in sp.added_uris()]
    assert names[:4] == ["Morning1", "Morning2", "Morning3", "Morning0"]  # 3, 4, 4, 5: climbing (ties keep order)
    assert names[4] == "Bridge"                                            # eases into the workout
    assert names[5:12] == ["Workout0", "Workout3", "Workout1", "Workout4", "Workout6", "Workout2", "Workout5"]  # falling to 5
    assert names[12] == "Focus1"                                            # hard cut: opens at energy 5
    assert "Focusbridge" in names[12:]

    # Sections under headings with start times; numbering runs on
    assert "Summer morning <small>· starts at 0:00</small>" in page.text
    assert "Workout <small>· starts at 14:00</small>" in page.text
    assert "Lock in <small>· starts at 42:00</small>" in page.text
    assert '<span class="n">13</span><span class="what">Focus 1<small>' in page.text

    created = json.loads(sp.create.calls.last.request.content)
    assert created["description"] == "0–14 Summer morning → 14–42 Workout → 42–56 Lock in. Made by Moodify."
    with get_conn() as conn:
        stored = conn.execute("SELECT name, energy, role FROM playlist_tracks ORDER BY position").fetchall()
    assert [tuple(r) for r in stored][4] == ("Bridge", 6, "bridge")


def test_cut_boundary_has_no_bridge_and_blend_has_one(logged_in, respx_mock, briefs):
    sp, _, _ = _arc_build(logged_in, respx_mock, briefs, transitions=("blend", "cut", "blend"))
    names = [u.rsplit(":", 1)[1] for u in sp.added_uris()]
    assert names[4] != "Bridge" and names[4] in ("Workout1", "Workout4", "Workout6")  # cut: opens at 8
    assert names[12] == "Focusbridge"                                                   # blend: bridge first


def test_long_labels_fall_back_to_the_summary_in_the_description(logged_in, respx_mock, briefs):
    sections = [{**s, "label": "A very long section label " * 2} for s in _sections((4, 14, "blend"), (8, 14, "blend"))]
    briefs.answer = default_brief("Long", summary="Short summary.", sections=sections)
    respx_mock.post(CHAT_URL).respond(200, json=_completion(
        [{"title": f"Song {i}", "artist": f"Artist {i}", "section": 1 + i // 4} for i in range(8)]))
    sp = FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(8)})
    finish_build(logged_in, logged_in.post("/playlist", data={"mood": "x"}))
    description = json.loads(sp.create.calls.last.request.content)["description"]
    assert len(description) <= 300


# --- live -----------------------------------------------------------------------------------------------------

@pytest.mark.live
def test_live_first_real_prompt_plays_in_three_parts():
    """Gate. Creates a real playlist: listen to it and note in phase-plan.md whether the switches felt right."""
    from src.playlist import run
    with get_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE onboarding_json IS NOT NULL ORDER BY created_at LIMIT 1").fetchone()
    if user is None or auth.missing_scopes(user["granted_scopes"]):
        pytest.skip("needs an onboarded user who logged in after Phase 10")
    prompt = ("I want to remember the chill summer morning and a hella stuff to do, workout for 1/2 an hour and then i want"
              " to instantly lock in for a programming task.")
    job_id = jobs.create(user["spotify_id"], prompt)
    run(job_id, user["spotify_id"], prompt)
    job = jobs.get(job_id, user["spotify_id"])
    assert job["status"] == "done", job["error"]
    with get_conn() as conn:
        rows = conn.execute("SELECT section, name, artists_json, duration_ms, energy, role FROM playlist_tracks"
                            " WHERE playlist_id = ? ORDER BY position", (job["playlist_id"],)).fetchall()
    brief = Brief.model_validate_json(playlists.get(job["playlist_id"], user["spotify_id"])["brief_json"])
    for r in rows:
        print(f"  [{brief.sections[int(r['section'])].label}] e={r['energy']} {r['role']}: {r['name']} - "
              f"{', '.join(json.loads(r['artists_json']))} ({r['duration_ms'] / 60000:.1f} min)")
    print(playlists.get(job["playlist_id"], user["spotify_id"])["spotify_url"])
    sections = [int(r["section"]) for r in rows]
    assert sections == sorted(sections) and set(sections) == {0, 1, 2}  # three parts, in order
    workout = sum(r["duration_ms"] for r in rows if int(r["section"]) == 1) / 60_000
    assert 27 <= workout <= 33
    first_focus = next(r for r in rows if int(r["section"]) == 2)
    assert brief.sections[2].transition_in == "cut" and first_focus["role"] != "bridge"
