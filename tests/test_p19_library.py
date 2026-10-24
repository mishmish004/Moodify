"""Phase 19: the shared music library: one tracks table, weighted rows per person, retention."""
import json
import sqlite3

import pytest

from src import library, playlists, taste, users
from src.db import get_conn, init_db
from src.spotify import auth
from src.spotify.client import API_URL, Track

from .conftest import mock_listening, track_json
from .test_p6_playlist import logged_in, user  # noqa: F401
from .test_p16_memory import assert_no_rows_for


def _t(name, artist="Artist", uri=None, explicit=False) -> Track:
    return Track(uri or f"spotify:track:{name.replace(' ', '')}", name, (artist,), 200_000, explicit)


def _rows(spotify_id="friend1"):
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT uri, source, weight FROM library WHERE spotify_id = ? ORDER BY source, position", (spotify_id,))]


def _track_uris():
    with get_conn() as conn:
        return {r[0] for r in conn.execute("SELECT uri FROM tracks")}


def _second_user(spotify_id="friend2"):
    users.upsert(spotify_id, "Sam", f"r-{spotify_id}", auth.SCOPES)


# --- rebuilding ---------------------------------------------------------------------------------------------

def test_every_source_is_written_with_its_weight_and_weights_add_up(user):
    both = _t("Both")
    pid = playlists.insert("friend1", "p", "u", [_t("Made by Moodify"), both])
    kept = library.replace("friend1", {"top": [both, _t("Top")], "recent": [_t("Recent"), both], "liked": [_t("Liked")],
                                       "own_playlist": [_t("Mine")]})
    assert kept == {"top": 2, "recent": 2, "liked": 1, "own_playlist": 1, "moodify": 2}
    weights = {(r["uri"], r["source"]): r["weight"] for r in _rows()}
    assert weights[(both.uri, "top")] == 3 and weights[(both.uri, "recent")] == 2 and weights[(both.uri, "moodify")] == 1
    assert weights[("spotify:track:Liked", "liked")] == 1 and weights[("spotify:track:Mine", "own_playlist")] == 1
    by_track = {t.name: w for t, w in library.for_user("friend1")}
    assert by_track["Both"] == 6 and by_track["Top"] == 3 and by_track["Made by Moodify"] == 1
    assert library.for_user("friend1")[0][0] == both  # heaviest first
    assert pid


def test_track_details_are_stored_once_for_everyone(user):
    _second_user()
    shared = _t("Shared", explicit=True)
    library.replace("friend1", {"liked": [shared]})
    library.replace("friend2", {"top": [shared]})
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM tracks").fetchall()
    assert len(rows) == 1 and rows[0]["explicit"] == 1 and json.loads(rows[0]["artists_json"]) == ["Artist"]
    assert library.for_user("friend2")[0][0] == shared


def test_a_refresh_replaces_everything(user):
    gone, stays = _t("Unliked"), _t("Still liked")
    library.replace("friend1", {"liked": [gone, stays]})
    library.replace("friend1", {"liked": [stays]})
    assert [r["uri"] for r in _rows()] == [stays.uri]
    assert _track_uris() == {stays.uri}  # nobody else had it: the track went too


def test_the_600_row_cap_keeps_the_heaviest(user):
    top = [_t(f"Top {i}") for i in range(50)]
    liked = [_t(f"Liked {i}") for i in range(700)]
    kept = library.replace("friend1", {"top": top, "liked": liked})
    assert kept["top"] == 50 and kept["liked"] == 550 and len(_rows()) == 600
    assert "spotify:track:Liked549" in {r["uri"] for r in _rows()} and "spotify:track:Liked550" not in _track_uris()


def test_moodify_playlists_feed_the_library_from_the_database(user):
    playlists.insert("friend1", "old", "u", [_t("Old one")])
    playlists.insert("friend1", "new", "u", [_t("New one")])
    library.replace("friend1", {})
    assert [r["uri"] for r in _rows()] == ["spotify:track:Newone", "spotify:track:Oldone"]


# --- reading ------------------------------------------------------------------------------------------------

def test_top_artists_by_weight(user):
    library.replace("friend1", {"top": [_t("A1", "Gunna"), _t("B1", "Drake")], "liked": [_t("A2", "Gunna"), _t("C1", "SZA")]})
    assert library.top_artists("friend1", 2) == [("Gunna", 4), ("Drake", 3)]


def test_common_is_symmetric(user):
    _second_user()
    library.replace("friend1", {"top": [_t("Shared", "Gunna"), _t("Mine", "Drake")]})
    library.replace("friend2", {"liked": [_t("Shared", "Gunna"), _t("Theirs", "Drake"), _t("Other", "Bach")]})
    ab, ba = library.common("friend1", "friend2"), library.common("friend2", "friend1")
    assert ab == ba
    assert [(t.name, w) for t, w in ab.tracks] == [("Shared", 4)]
    assert ab.artists == [("Drake", 4), ("Gunna", 4)]


def test_no_overlap_is_empty(user):
    _second_user()
    library.replace("friend1", {"top": [_t("Mine", "Drake")]})
    library.replace("friend2", {"top": [_t("Theirs", "Bach")]})
    assert library.common("friend1", "friend2") == library.Common([], [])


def test_the_pool_from_the_library_is_the_phase_10_pool(user):
    a, b, c = _t("A"), _t("B", explicit=True), _t("C")
    listening = taste.Listening(top_tracks=[a, b], recent_tracks=[c, a, c], liked=[b, _t("D"), a],
                                playlist_tracks=[_t("E"), c, _t("F")])
    library.replace("friend1", taste.library_sources(listening))
    assert library.pool("friend1") == taste.known_pool(listening)
    assert [t.name for t in library.pool("friend1")] == ["A", "B", "C", "D", "E", "F"]
    assert library.pool("friend1")[1].explicit is True


def test_a_rebuild_makes_the_same_spotify_calls_as_phase_10(user, respx_mock):
    respx_mock.post(auth.TOKEN_URL).respond(200, json={"access_token": "a", "expires_in": 3600})
    artists = respx_mock.get(f"{API_URL}/me/top/artists").respond(200, json={"items": [{"name": "Drake"}]})
    top = respx_mock.get(f"{API_URL}/me/top/tracks").respond(200, json={"items": [track_json(_t("Top"))]})
    routes = mock_listening(respx_mock, recent=[_t("R")], liked=[_t(f"L{i}") for i in range(120)], followed=["Bon Iver"],
                            own_playlists=[("p1", "gym", "friend1")], playlist_items={"p1": [_t("Gym")]})
    taste.profile_for(users.get("friend1"))
    calls = {"top artists": artists.call_count, "top tracks": top.call_count,
             **{name: route.call_count for name, route in routes.items()}}
    assert calls == {"top artists": 2, "top tracks": 1, "recent": 1, "liked": 3, "followed": 1, "playlists": 1, "items": 1}
    assert library.counts("friend1") == {"top": 1, "recent": 1, "liked": 120, "own_playlist": 1, "moodify": 0}


def test_a_failed_read_leaves_the_library_as_it_was(user, respx_mock):
    library.replace("friend1", {"liked": [_t("Kept")]})
    respx_mock.post(auth.TOKEN_URL).respond(200, json={"access_token": "a", "expires_in": 3600})
    respx_mock.get(f"{API_URL}/me/top/artists").respond(200, json={"items": []})
    respx_mock.get(f"{API_URL}/me/top/tracks").respond(200, json={"items": []})
    mock_listening(respx_mock, fail=("liked",))
    taste.profile_for(users.get("friend1"))
    assert [r["uri"] for r in _rows()] == ["spotify:track:Kept"]


def test_a_cached_profile_reads_its_pool_from_the_library(user, respx_mock):
    library.replace("friend1", {"liked": [_t("Known")]})
    users.set_taste_summary("friend1", "cached", taste.datetime.now(taste.timezone.utc).strftime(taste.TIMESTAMP))
    assert taste.profile_for(users.get("friend1")).pool == [_t("Known")]  # and no Spotify call


# --- retention ----------------------------------------------------------------------------------------------

def test_rows_not_refreshed_for_30_days_are_pruned_with_their_tracks(user):
    _second_user()
    library.replace("friend1", {"liked": [_t("Old"), _t("Shared")]})
    library.replace("friend2", {"liked": [_t("Fresh"), _t("Shared")]})
    with get_conn() as conn:
        conn.execute("UPDATE library SET refreshed_at = datetime('now', '-31 days') WHERE spotify_id = 'friend1'")
        conn.execute("UPDATE library SET refreshed_at = datetime('now', '-29 days') WHERE spotify_id = 'friend2'")
    assert library.prune() == 2
    assert _rows("friend1") == [] and len(_rows("friend2")) == 2
    assert _track_uris() == {"spotify:track:Fresh", "spotify:track:Shared"}


def test_startup_prunes(test_settings):
    from fastapi.testclient import TestClient
    from src.main import app
    init_db()
    users.upsert("friend1", "F", "r")
    library.replace("friend1", {"liked": [_t("Old")]})
    with get_conn() as conn:
        conn.execute("UPDATE library SET refreshed_at = datetime('now', '-31 days')")
    with TestClient(app):
        pass
    assert _rows() == [] and _track_uris() == set()


def test_disconnect_leaves_no_rows_and_no_orphan_tracks(logged_in):
    _second_user()
    library.replace("friend1", {"liked": [_t("Mine only"), _t("Shared")]})
    library.replace("friend2", {"liked": [_t("Shared")]})
    logged_in.post("/disconnect")
    assert_no_rows_for("friend1")
    assert _track_uris() == {"spotify:track:Shared"}


def test_migration_moves_the_pool_out_of_users(test_settings):
    from .test_p9_jobs import V0_SCHEMA
    conn = sqlite3.connect(test_settings.DB_PATH)
    conn.executescript(V0_SCHEMA)
    conn.execute("INSERT INTO users (spotify_id, refresh_token, taste_summary, taste_updated_at)"
                 " VALUES ('old', 'r', 'cached', datetime('now'))")
    conn.commit()
    conn.close()
    init_db()
    row = users.get("old")
    assert "taste_pool_json" not in row.keys() and row["taste_updated_at"] is None  # rebuilt, and filling the library


def test_unknown_sources_are_refused(user):
    with pytest.raises(sqlite3.IntegrityError), get_conn() as conn:
        conn.execute("INSERT INTO tracks (uri, name, artists_json, duration_ms) VALUES ('u', 'n', '[]', 1)")
        conn.execute("INSERT INTO library (spotify_id, uri, source, weight, position) VALUES ('friend1', 'u', 'radio', 1, 0)")


# --- live ---------------------------------------------------------------------------------------------------

@pytest.mark.live
def test_live_your_library():
    """Gate. Rebuilds your profile (Spotify reads only) and prints your library's rows per source."""
    init_db()
    with get_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE onboarding_json IS NOT NULL ORDER BY created_at LIMIT 1").fetchone()
        conn.execute("UPDATE users SET taste_updated_at = NULL WHERE spotify_id = ?", (user["spotify_id"],))
    profile = taste.profile_for(users.get(user["spotify_id"]))
    counts = library.counts(user["spotify_id"])
    print(f"rows per source: {counts} (total {sum(counts.values())})")
    print("heaviest:", [(t.name, w) for t, w in library.for_user(user["spotify_id"])[:8]])
    print("top artists:", library.top_artists(user["spotify_id"], 8))
    print(f"pool: {len(profile.pool)} songs, same as the library's: {library.pool(user['spotify_id']) == profile.pool}")
    assert 0 < sum(counts.values()) <= library.MAX_ROWS


def test_hidden_and_support_note_playlists_stay_out_of_the_library(user):
    from .conftest import default_brief
    from src import sharing
    playlists.insert("friend1", "ok", "u", [_t("Fine")], json.dumps(default_brief("ok")))
    playlists.insert("friend1", "hard day", "u", [_t("Private")], json.dumps(default_brief("x", support_note=True)))
    hidden = playlists.insert("friend1", "secret", "u", [_t("Hidden")], json.dumps(default_brief("y")))
    sharing.set_hidden(hidden, "friend1", True)
    library.replace("friend1", {})
    assert [r["uri"] for r in _rows()] == ["spotify:track:Fine"]
