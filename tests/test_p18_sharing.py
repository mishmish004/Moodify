"""Phase 18: sharing with friends only after they switch it on, with every use logged for the owner."""
import json
import sqlite3

import pytest

from src import playlists, sharing, users
from src.db import get_conn, init_db
from src.spotify import auth
from src.spotify.client import Track

from .conftest import default_brief
from .test_p6_playlist import logged_in, user  # noqa: F401
from .test_p16_memory import assert_no_rows_for

KINDS = list(sharing.SWITCHES)


def _friend(spotify_id="friend2", name="Sam Rivera", taste=False, blend=False, listen_as=False, summary="Loves jazz and Bach."):
    users.upsert(spotify_id, name, f"refresh-{spotify_id}", auth.SCOPES)
    users.set_onboarding(spotify_id, {"genres": "jazz", "artists": "Bill Evans", "energy": "low"})
    users.set_taste_summary(spotify_id, summary, "2026-10-24 08:00:00", "[]")
    if taste or blend or listen_as:
        sharing.save(spotify_id, taste, blend, listen_as)
    return spotify_id


def _playlist(owner, title="Sunday cooking", prompt="secret prompt words", support=False, hidden=False) -> int:
    brief = default_brief(title, support_note=support)
    pid = playlists.insert(owner, prompt, "https://open.spotify.com/playlist/x",
                           [Track("spotify:track:a", "A", ("B",), 200_000)], json.dumps(brief))
    if hidden:
        sharing.set_hidden(pid, owner, True)
    return pid


def _access_rows():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT owner, viewer, kind, playlist_id FROM taste_access ORDER BY id")]


def _everywhere(viewer="friend1") -> dict:
    """Every list a person could appear in, by feature."""
    return {"friends": [r["spotify_id"] for r in sharing.friends(viewer)],
            **{kind: [r["spotify_id"] for r in sharing.friends(viewer, kind)] for kind in KINDS}}


# --- off by default ---------------------------------------------------------------------------------------

def test_a_new_user_shares_nothing_and_appears_nowhere(logged_in):
    _friend()
    assert sharing.get("friend2") == {"taste": False, "blend": False, "listen_as": False}
    assert _everywhere() == {"friends": [], "taste": [], "blend": [], "listen_as": []}
    assert all(not sharing.allowed("friend2", "friend1", kind) for kind in KINDS)
    assert "Sam Rivera" not in logged_in.get("/friends").text
    assert logged_in.get("/friends/friend2").status_code == 404
    assert _access_rows() == []


def test_users_from_before_the_migration_share_nothing(test_settings):
    from .test_p9_jobs import V0_SCHEMA
    conn = sqlite3.connect(test_settings.DB_PATH)
    conn.executescript(V0_SCHEMA)
    conn.executemany("INSERT INTO users (spotify_id, refresh_token) VALUES (?, 'r')", [("old1",), ("old2",)])
    conn.commit()
    conn.close()
    init_db()
    assert sharing.get("old1") == {"taste": False, "blend": False, "listen_as": False}
    assert _everywhere("old2") == {"friends": [], "taste": [], "blend": [], "listen_as": []}


# --- each switch grants exactly its own feature ----------------------------------------------------------

@pytest.mark.parametrize("on", KINDS)
def test_each_switch_grants_only_its_own_feature(logged_in, on):
    _friend(**{on: True})
    for kind in KINDS:
        assert sharing.allowed("friend2", "friend1", kind) is (kind == on), kind
    lists = _everywhere()
    assert lists["friends"] == ["friend2"]
    assert {kind for kind in KINDS if lists[kind]} == {on}
    page = logged_in.get("/friends/friend2")
    assert page.status_code == 200
    assert ("Loves jazz and Bach." in page.text) is (on == "taste")


def test_you_may_always_use_your_own_data(user):
    assert all(sharing.allowed("friend1", "friend1", kind) for kind in KINDS)


def test_unknown_people_and_kinds(user):
    _friend(taste=True)
    assert not sharing.allowed("friend2", "nobody", "taste")  # a viewer who isn't on Moodify
    assert not sharing.allowed("nobody", "friend1", "taste")
    with pytest.raises(ValueError):
        sharing.allowed("friend2", "friend1", "everything")


def test_switching_off_works_on_the_next_request(logged_in):
    _friend(taste=True, blend=True, listen_as=True)
    assert logged_in.get("/friends/friend2").status_code == 200
    sharing.save("friend2", False, False, False)
    assert logged_in.get("/friends/friend2").status_code == 404
    assert _everywhere() == {"friends": [], "taste": [], "blend": [], "listen_as": []}
    assert not any(sharing.allowed("friend2", "friend1", kind) for kind in KINDS)


def test_the_sharing_form(logged_in):
    page = logged_in.get("/sharing").text
    for words in ("Show my taste to friends", "Let friends include me in Blends", "Let friends hear music like me",
                  "Never what you typed", "saves the playlist in their Spotify, not yours", "Nothing is ever added to yours",
                  "Playlists friends already made stay in their Spotify"):
        assert words in page, words
    assert "checked" not in page
    resp = logged_in.post("/sharing", data={"show_taste": "on", "allow_listen_as": "on"})
    assert resp.status_code == 303
    assert sharing.get("friend1") == {"taste": True, "blend": False, "listen_as": True}
    assert logged_in.get("/sharing").text.count("checked") == 2
    logged_in.post("/sharing", data={})
    assert sharing.get("friend1") == {"taste": False, "blend": False, "listen_as": False}


# --- what friends never see ---------------------------------------------------------------------------------

def test_support_note_playlists_never_reach_friends(logged_in):
    _friend(taste=True)
    _playlist("friend2", title="Quiet company", support=True)
    _playlist("friend2", title="Sunday cooking")
    assert [r["title"] for r in sharing.visible_playlists("friend2")] == ["Sunday cooking"]
    page = logged_in.get("/friends/friend2").text
    assert "Sunday cooking" in page and "Quiet company" not in page


def test_hidden_playlists_reach_only_their_owner(logged_in):
    _friend(taste=True)
    users.upsert("friend3", "Priya", "r3", auth.SCOPES)
    sharing.save("friend1", True, False, False)
    mine = _playlist("friend1", title="Late night drive", hidden=True)
    _playlist("friend1", title="Morning run")
    assert [r["title"] for r in sharing.visible_playlists("friend1")] == ["Morning run"]
    assert "Late night drive" in logged_in.get("/history").text
    assert "Hidden from friends." in logged_in.get(f"/playlists/{mine}").text


def test_hide_and_show_again(logged_in):
    sharing.save("friend1", True, False, False)
    pid = _playlist("friend1")
    resp = logged_in.post(f"/playlists/{pid}/hide", data={"hidden": "1"})
    assert resp.status_code == 303 and resp.headers["location"] == f"/playlists/{pid}#hide"
    assert sharing.visible_playlists("friend1") == []
    logged_in.post(f"/playlists/{pid}/hide", data={"hidden": "0"})
    assert len(sharing.visible_playlists("friend1")) == 1
    assert logged_in.post(f"/playlists/{pid}/hide", data={"hidden": "2"}).status_code == 422


def test_hiding_someone_elses_playlist_is_404(logged_in):
    _friend()
    theirs = _playlist("friend2")
    assert logged_in.post(f"/playlists/{theirs}/hide", data={"hidden": "1"}).status_code == 404
    with get_conn() as conn:
        assert conn.execute("SELECT hidden FROM playlists WHERE id = ?", (theirs,)).fetchone()[0] == 0


def test_prompts_reviews_and_ratings_never_reach_friends(logged_in):
    _friend(taste=True, blend=True, listen_as=True)
    pid = _playlist("friend2", prompt="my secret prompt about my ex")
    playlists.set_rating(pid, "friend2", 3)
    playlists.set_review(pid, "friend2", "my secret review")
    page = logged_in.get("/friends/friend2").text
    assert "Sunday cooking" in page
    for secret in ("my secret prompt", "my secret review", "3/10", "rated"):
        assert secret not in page
    row = sharing.visible_playlists("friend2")[0]
    assert set(row.keys()) == {"id", "title", "created_at", "track_count"}


# --- the access log -----------------------------------------------------------------------------------------

def test_every_use_is_logged_once_and_only_the_owner_reads_it(logged_in):
    _friend(taste=True)
    logged_in.get("/friends/friend2")
    logged_in.get("/friends/friend2")
    assert _access_rows() == [{"owner": "friend2", "viewer": "friend1", "kind": "view", "playlist_id": None}] * 2
    sharing.log("friend1", "friend2", "blend")
    sharing.log("friend1", "friend1", "view")  # your own data isn't logged
    mine = logged_in.get("/sharing").text
    assert "made a Blend with you · 1 time" in mine and "looked at your taste" not in mine  # friend2's log is theirs
    assert [r["viewer"] for r in sharing.access_log("friend2")] == ["friend1"]
    assert sharing.access_log("friend2")[0]["uses"] == 2


def test_looking_at_someone_who_only_shares_blends_isnt_a_view(logged_in):
    _friend(blend=True)
    assert logged_in.get("/friends/friend2").status_code == 200
    assert _access_rows() == []


def test_disconnect_deletes_sharing_and_the_log_both_ways(logged_in):
    _friend(taste=True)
    sharing.save("friend1", True, True, True)
    logged_in.get("/friends/friend2")          # friend1 viewed friend2
    sharing.log("friend1", "friend2", "blend")  # friend2 used friend1's taste
    logged_in.post("/disconnect")
    assert_no_rows_for("friend1")
    with get_conn() as conn:
        assert conn.execute("SELECT count(*) FROM taste_access").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM sharing").fetchone()[0] == 1  # friend2's own row stays


def test_pages_need_a_login(test_settings):
    from fastapi.testclient import TestClient
    from src.main import app
    init_db()
    with TestClient(app, follow_redirects=False) as client:
        for path in ("/sharing", "/friends", "/friends/friend2"):
            assert client.get(path).headers["location"] == "/login", path


def test_friends_page_lists_who_shares_what(logged_in):
    _friend(taste=True, listen_as=True)
    _friend("friend3", "Priya Shah", blend=True)
    _friend("friend4", "Nobody Shares")
    page = logged_in.get("/friends").text
    assert "Shares: their taste, hear it like Sam" in page and "Shares: Blends" in page
    assert "Nobody Shares" not in page
    assert 'href="/friends"' in page  # in the header
