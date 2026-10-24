"""
Phase 18: nothing about a person reaches anyone else until they switch it on.

Three switches per person, all off until they turn them on (Spotify Developer Terms V.4: explicit consent
before using a user's data for anything but showing it back to them):
  - taste:     show my taste to friends (taste profile, top artists, taste map, activity: playlist titles only)
  - blend:     let friends include me in Blends
  - listen_as: let friends hear music like me
Every Part 2 route asks allowed() first, and every use of someone's data is logged in taste_access,
which only its owner can read. Prompts, reviews, ratings, lessons, support-note playlists and tokens are
never shared, whatever the switches say: friend-facing queries don't select them.
"""
import sqlite3
from typing import Optional

from src.db import get_conn

SWITCHES = {"taste": "show_taste", "blend": "allow_blends", "listen_as": "allow_listen_as"}
LOG_KINDS = ("view", "blend", "listen_as")

# A playlist friends may see: not hidden, and not made from a prompt that got a support note (Phase 11)
FRIEND_VISIBLE = "hidden = 0 AND coalesce(json_extract(brief_json, '$.support_note'), 0) = 0"


def get(spotify_id: str) -> dict[str, bool]:
    """The user's switches by kind. A user without a row has all of them off."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sharing WHERE spotify_id = ?", (spotify_id,)).fetchone()
    return {kind: bool(row and row[column]) for kind, column in SWITCHES.items()}


def save(spotify_id: str, taste: bool, blend: bool, listen_as: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sharing (spotify_id, show_taste, allow_blends, allow_listen_as) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (spotify_id) DO UPDATE SET show_taste = excluded.show_taste, allow_blends = excluded.allow_blends,"
            " allow_listen_as = excluded.allow_listen_as, updated_at = CURRENT_TIMESTAMP",
            (spotify_id, int(taste), int(blend), int(listen_as)),
        )


def allowed(owner: str, viewer: str, kind: str) -> bool:
    """May `viewer` use `owner`'s data for `kind` (a key of SWITCHES)? Your own data, always; anyone else's,
    only while their switch is on. Read fresh on every call, so switching off works on the next request."""
    if kind not in SWITCHES:
        raise ValueError(kind)
    if owner == viewer:
        return True
    with get_conn() as conn:
        row = conn.execute(
            f"SELECT s.{SWITCHES[kind]} FROM sharing s JOIN users u ON u.spotify_id = s.spotify_id"
            " WHERE s.spotify_id = ? AND EXISTS (SELECT 1 FROM users WHERE spotify_id = ?)",
            (owner, viewer),
        ).fetchone()
    return bool(row and row[0])


def friends(viewer: str, kind: Optional[str] = None) -> list[sqlite3.Row]:
    """The other people on this Moodify who share something with friends (or `kind` in particular), with their
    switches and display name. Never anyone with every switch off."""
    if kind is not None and kind not in SWITCHES:
        raise ValueError(kind)
    wanted = f"s.{SWITCHES[kind]} = 1" if kind else " OR ".join(f"s.{c} = 1" for c in SWITCHES.values())
    with get_conn() as conn:
        return conn.execute(
            "SELECT u.spotify_id, u.display_name, s.show_taste, s.allow_blends, s.allow_listen_as"
            f" FROM users u JOIN sharing s ON s.spotify_id = u.spotify_id WHERE u.spotify_id != ? AND ({wanted})"
            " ORDER BY lower(coalesce(u.display_name, u.spotify_id))",
            (viewer,),
        ).fetchall()


def first_name(row) -> str:
    """How a person is labelled to friends: the first word of their Spotify display name."""
    name = (row["display_name"] or "").strip()
    return name.split()[0] if name else row["spotify_id"]


def log(owner: str, viewer: str, kind: str, playlist_id: Optional[int] = None) -> None:
    """One use of `owner`'s data by `viewer`. Uses of your own data aren't logged."""
    if kind not in LOG_KINDS:
        raise ValueError(kind)
    if owner == viewer:
        return
    with get_conn() as conn:
        conn.execute("INSERT INTO taste_access (owner, viewer, kind, playlist_id) VALUES (?, ?, ?, ?)",
                     (owner, viewer, kind, playlist_id))


def access_log(owner: str) -> list[sqlite3.Row]:
    """Who used `owner`'s data, grouped by person and kind, most recent first. Only the owner sees this."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT a.viewer, u.display_name, a.kind, count(*) AS uses, max(a.at) AS last"
            " FROM taste_access a JOIN users u ON u.spotify_id = a.viewer WHERE a.owner = ?"
            " GROUP BY a.viewer, a.kind ORDER BY last DESC",
            (owner,),
        ).fetchall()


def visible_playlists(owner: str, limit: int = 10) -> list[sqlite3.Row]:
    """`owner`'s playlists as friends may see them: title, date, length in tracks. Never the prompt, the review
    or the rating, and never hidden or support-note playlists (enforced here, not in the template)."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, json_extract(brief_json, '$.title') AS title, created_at, json_array_length(track_uris_json) AS"
            f" track_count FROM playlists WHERE spotify_id = ? AND {FRIEND_VISIBLE} ORDER BY id DESC LIMIT ?",
            (owner, limit),
        ).fetchall()


def set_hidden(playlist_id: int, spotify_id: str, hidden: bool) -> bool:
    """Hide one of your playlists from friends, or show it again. False if it isn't yours."""
    with get_conn() as conn:
        return conn.execute("UPDATE playlists SET hidden = ? WHERE id = ? AND spotify_id = ?",
                            (int(hidden), playlist_id, spotify_id)).rowcount == 1
