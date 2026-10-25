"""Phase 9: background builds with a progress page and a log, retries for slow models, schema migrations."""
import logging
import sqlite3
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from src import db, jobs, playlists, users
from src.core.logger import MoodifyLogger
from src.db import get_conn, init_db
from src.main import app
from src.spotify import auth

from .conftest import ROOT, default_brief, finish_build, tracks
from .test_p6_playlist import CHAT_URL, FakeSpotify, _build, _completion, _playlist_rows, _songs, logged_in, user  # noqa: F401

V0_SCHEMA = (ROOT / "tests" / "fixtures" / "schema_v0.sql").read_text()  # schema.sql as of Phase 8


def _slow(seconds, response):
    def answer(request):
        time.sleep(seconds)
        return response
    return answer


def _job_id(resp) -> int:
    return int(resp.headers["location"].rsplit("/", 1)[1])


# --- the 2026-10-23 failure: a slow model ---------------------------------------------------------

def test_first_call_times_out_and_the_retry_gives_a_full_playlist(logged_in, respx_mock):
    chat = respx_mock.post(CHAT_URL).mock(side_effect=[
        httpx.ReadTimeout("timed out"), httpx.Response(200, json=_completion(_songs(30)))])
    sp = FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(30)})
    resp = _build(logged_in, "summer morning, then workout")
    assert resp.status_code == 200
    assert chat.call_count == 2
    assert len(sp.added_uris()) == 16  # a full 60 minutes (Phase 12), not the 15-song fallback


def test_both_calls_failing_explains_and_saves_nothing(logged_in, respx_mock):
    respx_mock.post(CHAT_URL).mock(side_effect=[httpx.ReadTimeout("t"), httpx.ReadTimeout("t")])
    sp = FakeSpotify(respx_mock, {})
    post = logged_in.post("/playlist", data={"mood": "anything"})
    resp = finish_build(logged_in, post)
    assert "No song ideas" in resp.text
    assert sp.search.call_count == 0 and sp.create.call_count == 0
    assert _playlist_rows() == []
    job = jobs.get(_job_id(post), "friend1")
    assert job["status"] == "failed" and job["error"] == jobs.NO_SUGGESTIONS


# --- background jobs and the progress page -----------------------------------------------------------

def test_post_answers_at_once_and_the_job_page_shows_progress(logged_in, respx_mock):
    respx_mock.post(CHAT_URL).mock(side_effect=_slow(1.0, httpx.Response(200, json=_completion(_songs(10)))))
    FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(10)})

    started = time.monotonic()
    post = logged_in.post("/playlist", data={"mood": "rainy sunday"})
    assert time.monotonic() - started < 0.5
    assert post.status_code == 303 and post.headers["location"].startswith("/jobs/")

    # The model takes 1 s to answer, so the page soon shows that step
    deadline = time.monotonic() + 0.9
    while "Asking for song ideas" not in (page := logged_in.get(post.headers["location"])).text:
        assert time.monotonic() < deadline, page.text
        time.sleep(0.02)
    assert page.status_code == 200
    assert '<meta http-equiv="refresh" content="3">' in page.text
    assert "rainy sunday" in page.text

    done = finish_build(logged_in, post)
    assert ">Song 3<" in done.text


def test_second_submit_while_running_joins_the_same_build(logged_in, respx_mock, briefs):
    briefs.answer = default_brief("focus", total_minutes=35)  # 10 songs of 3.5 min: no top-up call
    chat = respx_mock.post(CHAT_URL).mock(side_effect=_slow(0.5, httpx.Response(200, json=_completion(_songs(10)))))
    FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(10)})
    first = logged_in.post("/playlist", data={"mood": "focus"})
    second = logged_in.post("/playlist", data={"mood": "focus"})
    assert second.headers["location"] == first.headers["location"]
    finish_build(logged_in, first)
    assert chat.call_count == 1 and len(_playlist_rows()) == 1


def test_finished_job_logs_every_step_with_token_counts(logged_in, respx_mock, briefs):
    briefs.answer = default_brief("gym", total_minutes=45)  # window 40.5-49.5: all 12 songs below (42 min)
    completion = _completion(_songs(12))
    completion["usage"] = {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500,
                           "prompt_tokens_details": {"cached_tokens": 1024}}
    respx_mock.post(CHAT_URL).respond(200, json=completion)
    FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(12)})
    log = MoodifyLogger.get_logger("playlist")  # set up before our handler goes on
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    log.addHandler(handler)
    try:
        post = logged_in.post("/playlist", data={"mood": "gym"})
        finish_build(logged_in, post)
    finally:
        log.removeHandler(handler)

    events = jobs.events(_job_id(post))
    assert [e["text"] for e in events if e["kind"] == "step"] == [
        "Reading your taste", "Reading your prompt", "Asking for song ideas", "Finding the songs on Spotify",
        "Saving the playlist to your Spotify"]
    llm = [e for e in events if e["kind"] == "llm" and e["call"] == "suggest"]
    assert len(llm) == 1
    assert llm[0] | {"seconds": 0} == {"kind": "llm", "call": "suggest", "model": "gpt-5.4-mini", "attempt": 1, "ok": True,
                                       "songs": 12, "seconds": 0, "input_tokens": 1200, "cached_tokens": 1024, "output_tokens": 300}
    searches = [e for e in events if e["kind"] == "search"]
    assert len(searches) == 12 and all(e["matched"] for e in searches)
    assert events[-1]["kind"] == "built" and events[-1]["tracks"] == 12 and events[-1]["searched"] == 12
    assert any("12 tracks, 42 min (0 known) from 12 searches (12 suggestions)" in r.getMessage() for r in records)


def test_failed_attempts_are_logged_too(logged_in, respx_mock, briefs):
    briefs.answer = default_brief("x", total_minutes=35)
    respx_mock.post(CHAT_URL).mock(side_effect=[httpx.ReadTimeout("t"), httpx.Response(200, json=_completion(_songs(10)))])
    FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(10)})
    post = logged_in.post("/playlist", data={"mood": "x"})
    finish_build(logged_in, post)
    llm = [e for e in jobs.events(_job_id(post)) if e["kind"] == "llm" and e["call"] == "suggest"]
    assert [(e["attempt"], e["ok"], e.get("error")) for e in llm] == [(1, False, "APITimeoutError"), (2, True, None)]


def test_expired_spotify_login_during_a_build_goes_to_login(logged_in, respx_mock):
    respx_mock.post(auth.TOKEN_URL).respond(400, json={"error": "invalid_grant"})
    respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(10)))
    post = logged_in.post("/playlist", data={"mood": "x"})
    deadline = time.monotonic() + 10
    while jobs.get(_job_id(post), "friend1")["status"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    page = logged_in.get(post.headers["location"])
    assert page.status_code == 303 and page.headers["location"] == "/login"


# --- result page -----------------------------------------------------------------------------------

def test_playlist_page_is_rebuilt_from_the_database(logged_in, respx_mock):
    respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(10)))
    FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(10)})
    _build(logged_in, "late night")
    row_id = _playlist_rows()[0]["id"]
    respx_mock.routes.clear()  # the page must not need Spotify or OpenAI

    page = logged_in.get(f"/playlists/{row_id}")
    again = logged_in.get(f"/playlists/{row_id}")
    assert page.status_code == 200 and again.status_code == 200
    positions = [page.text.index(f">Song {i}<") for i in range(10)]
    assert positions == sorted(positions)
    assert 'href="https://open.spotify.com/track/t4"' in page.text
    assert len(_playlist_rows()) == 1  # reloading never makes another playlist
    assert [t.uri for t in playlists.tracks(row_id)] == [f"spotify:track:t{i}" for i in range(10)]


def test_old_playlist_without_saved_tracks_still_opens(logged_in):
    with get_conn() as conn:
        row_id = conn.execute(
            "INSERT INTO playlists (user_id, prompt, url, track_uris_json) VALUES (?, ?, ?, ?)",
            ("friend1", "from phase 6", "https://open.spotify.com/playlist/old", '["spotify:track:a"]'),
        ).lastrowid
    page = logged_in.get(f"/playlists/{row_id}")
    assert page.status_code == 200
    assert "before Moodify kept track lists" in page.text
    assert 'href="https://open.spotify.com/playlist/old"' in page.text


def test_other_users_jobs_and_playlists_are_404(logged_in):
    users.upsert("friend2", "Two", "r2")
    job_id = jobs.create("friend2", "their mood")
    row_id = playlists.insert("friend2", "their mood", "https://open.spotify.com/playlist/x", tracks("spotify:track:a"))
    assert logged_in.get(f"/jobs/{job_id}").status_code == 404
    assert logged_in.get(f"/playlists/{row_id}").status_code == 404
    assert "their mood" not in logged_in.get(f"/playlists/{row_id}").text


def test_job_and_playlist_pages_need_login(test_settings):
    init_db()
    client = TestClient(app, follow_redirects=False)
    assert client.get("/jobs/1").headers["location"] == "/login"
    assert client.get("/playlists/1").headers["location"] == "/login"


def test_job_left_running_by_a_restart_fails_at_startup(user):
    job_id = jobs.create("friend1", "cut off")
    with TestClient(app, follow_redirects=False) as client:
        job = jobs.get(job_id, "friend1")
        assert job["status"] == "failed" and job["error"] == jobs.INTERRUPTED
        # The page explains it once the user is logged in
        with get_conn() as conn:
            assert conn.execute("SELECT count(*) FROM job_events WHERE job_id = ?", (job_id,)).fetchone()[0] == 0
    assert job["finished_at"]


def test_disconnect_deletes_jobs_and_their_events(logged_in, respx_mock):
    respx_mock.post(CHAT_URL).respond(200, json=_completion(_songs(10)))
    FakeSpotify(respx_mock, {f"Song {i}": f"t{i}" for i in range(10)})
    _build(logged_in, "x")
    logged_in.post("/disconnect")
    with get_conn() as conn:
        for table in ("jobs", "job_events", "playlists", "playlist_tracks"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table


# --- migrations ----------------------------------------------------------------------------------------

def _schema(path) -> dict:
    """Every table's columns and every index, normalized, so two databases can be compared."""
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        return {
            "tables": {t: [tuple(c) for c in conn.execute(f"PRAGMA table_info({t})")] for t in tables},
            "foreign_keys": {t: [tuple(f) for f in conn.execute(f"PRAGMA foreign_key_list({t})")] for t in tables},
            "indexes": sorted(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")),
            "version": conn.execute("PRAGMA user_version").fetchone()[0],
        }
    finally:
        conn.close()


def _v0_database(path):
    conn = sqlite3.connect(path)
    conn.executescript(V0_SCHEMA)
    conn.execute("INSERT INTO users (spotify_id, display_name, refresh_token, onboarding_json) VALUES ('u1', 'U', 'r', '{}')")
    conn.execute("INSERT INTO playlists (spotify_id, prompt, spotify_url, track_uris_json) VALUES ('u1', 'old', 'url', '[]')")
    conn.commit()
    conn.close()


def test_new_database_starts_at_the_latest_version(tmp_path):
    init_db(tmp_path / "new.db")
    assert _schema(tmp_path / "new.db")["version"] == db.SCHEMA_VERSION == len(db.MIGRATIONS)


def test_init_db_twice_changes_nothing(tmp_path):
    init_db(tmp_path / "a.db")
    before = _schema(tmp_path / "a.db")
    init_db(tmp_path / "a.db")
    assert _schema(tmp_path / "a.db") == before


def test_phase_8_database_upgrades_without_losing_data(tmp_path):
    _v0_database(tmp_path / "old.db")
    init_db(tmp_path / "old.db")
    init_db(tmp_path / "new.db")
    assert _schema(tmp_path / "old.db") == _schema(tmp_path / "new.db")  # migrated == fresh
    conn = sqlite3.connect(tmp_path / "old.db")
    assert conn.execute("SELECT user_id, onboarding_json FROM users").fetchall() == [("u1", "{}")]
    assert conn.execute("SELECT prompt FROM playlists").fetchall() == [("old",)]
    conn.close()


def test_a_failing_migration_step_leaves_the_database_as_it_was(tmp_path, monkeypatch):
    init_db(tmp_path / "a.db")
    before = _schema(tmp_path / "a.db")
    broken = "CREATE TABLE half_done (x); SELECT no_such_function();"
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + [broken])
    with pytest.raises(sqlite3.OperationalError):
        init_db(tmp_path / "a.db")
    assert _schema(tmp_path / "a.db") == before  # no half_done table, version unchanged


# --- live ------------------------------------------------------------------------------------------

FIRST_REAL_PROMPT = ("I want to remember the chill summer morning and a hella stuff to do, workout for 1/2 an hour"
                     " and then i want to instantly lock in for a programming task.")


@pytest.mark.live
def test_live_first_real_prompt_now_gives_a_full_playlist():
    """The prompt that gave 15 tracks on 2026-10-23 after a 45 s timeout. Creates a real playlist."""
    with get_conn() as conn:
        user = conn.execute("SELECT * FROM users WHERE onboarding_json IS NOT NULL ORDER BY created_at LIMIT 1").fetchone()
    if user is None:
        pytest.skip("no onboarded user in data/moodify.db: log in and complete /onboarding first")
    job_id = jobs.create(user["user_id"], FIRST_REAL_PROMPT)
    from src.playlist import run
    run(job_id, user["user_id"], FIRST_REAL_PROMPT)
    job = jobs.get(job_id, user["user_id"])
    for event in jobs.events(job_id):
        if event["kind"] in ("llm", "built", "failed"):
            print(event)
    assert job["status"] == "done", job["error"]
    made = playlists.tracks(job["playlist_id"])
    print(f"\n{len(made)} tracks: {playlists.get(job['playlist_id'], user['user_id'])['url']}")
    assert len(made) >= 20
