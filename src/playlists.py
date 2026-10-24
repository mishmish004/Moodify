"""The playlists table: every playlist Moodify made, with its tracks, so the app can show, list and re-queue them."""
import json
import sqlite3
from typing import Optional

from src.db import get_conn
from src.memory import invalidate
from src.spotify.client import Track


def insert(
    spotify_id: str, prompt: str, spotify_url: str, tracks: list[Track], brief_json: Optional[str] = None,
    sections: Optional[list[int]] = None, energies: Optional[list[Optional[int]]] = None,
    roles: Optional[list[Optional[str]]] = None, parent_id: Optional[int] = None,
) -> int:
    """The playlist row and its tracks, in order, in one transaction. `sections`, `energies`, `roles`: per track.
    `parent_id`: the playlist this one fixes ("Fix it with this review", Phase 15)."""
    sections = sections or [None] * len(tracks)
    energies = energies or [None] * len(tracks)
    roles = roles or [None] * len(tracks)
    with get_conn() as conn:
        playlist_id = conn.execute(
            "INSERT INTO playlists (spotify_id, prompt, spotify_url, track_uris_json, brief_json, parent_id)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (spotify_id, prompt, spotify_url, json.dumps([t.uri for t in tracks]), brief_json, parent_id),
        ).lastrowid
        conn.executemany(
            "INSERT INTO playlist_tracks (playlist_id, position, uri, name, artists_json, duration_ms, section, energy, role)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(playlist_id, i, t.uri, t.name, json.dumps(list(t.artists)), t.duration_ms, section, energy, role)
             for i, (t, section, energy, role) in enumerate(zip(tracks, sections, energies, roles))],
        )
        return playlist_id


def get(playlist_id: int, spotify_id: str) -> Optional[sqlite3.Row]:
    """Only the owner's playlists: the id alone is not enough."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM playlists WHERE id = ? AND spotify_id = ?", (playlist_id, spotify_id)
        ).fetchone()


def tracks(playlist_id: int) -> list[Track]:
    """In playlist order. Empty for playlists made before track details were saved (Phase 9)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT uri, name, artists_json, duration_ms FROM playlist_tracks WHERE playlist_id = ? ORDER BY position",
            (playlist_id,),
        ).fetchall()
    return [Track(r["uri"], r["name"], tuple(json.loads(r["artists_json"])), r["duration_ms"]) for r in rows]


def track_sections(playlist_id: int) -> list[Optional[int]]:
    """Each track's section index, in playlist order (None for playlists made before Phase 12)."""
    with get_conn() as conn:
        rows = conn.execute("SELECT section FROM playlist_tracks WHERE playlist_id = ? ORDER BY position", (playlist_id,))
        return [None if r[0] is None else int(r[0]) for r in rows]


def section_minutes(playlist_id: int) -> dict[int, float]:
    """How long each section came out, by section index."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT section, sum(duration_ms) / 60000.0 FROM playlist_tracks WHERE playlist_id = ? AND section IS NOT NULL"
            " GROUP BY section",
            (playlist_id,),
        ).fetchall()
    return {int(section): minutes for section, minutes in rows}


def track_rows(playlist_id: int) -> list[sqlite3.Row]:
    """Every playlist_tracks row in order, with section, energy and role. Check the owner with get() first."""
    with get_conn() as conn:
        return conn.execute("SELECT * FROM playlist_tracks WHERE playlist_id = ? ORDER BY position", (playlist_id,)).fetchall()


def track_at(playlist_id: int, position: int) -> Optional[sqlite3.Row]:
    """One row of playlist_tracks. Check the playlist's owner with get() first."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM playlist_tracks WHERE playlist_id = ? AND position = ?", (playlist_id, position)
        ).fetchone()


def set_rating(playlist_id: int, spotify_id: str, rating: int) -> bool:
    """1-10, replacing any earlier rating. False if the playlist isn't this user's."""
    with get_conn() as conn:
        changed = conn.execute(
            "UPDATE playlists SET rating = ?, rated_at = CURRENT_TIMESTAMP WHERE id = ? AND spotify_id = ?",
            (rating, playlist_id, spotify_id),
        ).rowcount == 1
        if changed:
            invalidate(conn, spotify_id)
        return changed


# What set_review did
SAVED, CLEARED, UNCHANGED = "saved", "cleared", "unchanged"


def set_review(playlist_id: int, spotify_id: str, text: str) -> Optional[str]:
    """Save the listener's review (already cleaned and capped), keeping the one it replaces in review_history.
    Empty text clears the review and withdraws the lessons learned from it. None if the playlist isn't theirs."""
    with get_conn() as conn:
        row = conn.execute("SELECT review, reviewed_at FROM playlists WHERE id = ? AND spotify_id = ?",
                           (playlist_id, spotify_id)).fetchone()
        if row is None:
            return None
        if (row["review"] or "") == text:
            return UNCHANGED
        if row["review"]:
            conn.execute("INSERT INTO review_history (playlist_id, text, created_at) VALUES (?, ?, ?)",
                         (playlist_id, row["review"], row["reviewed_at"]))
        if text:
            conn.execute("UPDATE playlists SET review = ?, reviewed_at = CURRENT_TIMESTAMP, review_verdict = NULL WHERE id = ?",
                         (text, playlist_id))
            return SAVED
        conn.execute("UPDATE playlists SET review = NULL, reviewed_at = NULL, review_verdict = NULL WHERE id = ?", (playlist_id,))
        conn.execute("DELETE FROM lessons WHERE spotify_id = ? AND source_playlist_id = ?", (spotify_id, playlist_id))
        invalidate(conn, spotify_id)
        return CLEARED


def review_history(playlist_id: int) -> list[sqlite3.Row]:
    """Earlier versions of a playlist's review, oldest first. Check the owner with get() first."""
    with get_conn() as conn:
        return conn.execute("SELECT text, created_at FROM review_history WHERE playlist_id = ? ORDER BY id",
                            (playlist_id,)).fetchall()


def version(playlist_id: int) -> int:
    """1 for a playlist made from a prompt, 2 for its first fix, and so on along parent_id."""
    with get_conn() as conn:
        return conn.execute(
            "WITH RECURSIVE chain(id, parent_id) AS (SELECT id, parent_id FROM playlists WHERE id = ?"
            " UNION ALL SELECT p.id, p.parent_id FROM playlists p JOIN chain ON p.id = chain.parent_id)"
            " SELECT count(*) FROM chain",
            (playlist_id,),
        ).fetchone()[0] or 1


def fixes(playlist_id: int, spotify_id: str) -> list[sqlite3.Row]:
    """Playlists made by fixing this one, oldest first."""
    with get_conn() as conn:
        return conn.execute("SELECT id, created_at FROM playlists WHERE parent_id = ? AND spotify_id = ? ORDER BY id",
                            (playlist_id, spotify_id)).fetchall()


def forget(playlist_id: int, spotify_id: str) -> bool:
    """Delete a playlist from Moodify's memory: its row, tracks, rating, review and, by cascade, the likes made
    there and the lessons learned from it. The playlist stays in Spotify. False if it isn't this user's."""
    with get_conn() as conn:
        gone = conn.execute("DELETE FROM playlists WHERE id = ? AND spotify_id = ?", (playlist_id, spotify_id)).rowcount == 1
        if gone:
            invalidate(conn, spotify_id)
        return gone


def recent_tracks(spotify_id: str, last: int = 3, skip: Optional[int] = None) -> list[sqlite3.Row]:
    """Every playlist_tracks row of the user's `last` newest playlists, leaving out playlist `skip`; newest first."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT t.* FROM playlist_tracks t WHERE t.playlist_id IN (SELECT id FROM playlists WHERE spotify_id = ?"
            " AND id IS NOT ? ORDER BY id DESC LIMIT ?) ORDER BY t.playlist_id DESC, t.position",
            (spotify_id, skip, last),
        ).fetchall()


ORDERS = {
    "new": "id DESC",
    "rating": "rating IS NULL, rating DESC, id DESC",  # unrated last
}


def recent(spotify_id: str, limit: int = 5, order: str = "new") -> list[sqlite3.Row]:
    """With track_count, like_count (its songs the user liked, wherever they liked them) and minutes
    (NULL for playlists from before track lists were kept). Newest first, or by `order` (a key of ORDERS).
    limit=-1 means all of them (SQLite's "no limit")."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT *, json_array_length(track_uris_json) AS track_count, json_extract(brief_json, '$.title') AS title,"
            " (SELECT count(*) FROM playlist_tracks t JOIN track_likes l ON l.spotify_id = playlists.spotify_id"
            "  AND l.uri = t.uri WHERE t.playlist_id = playlists.id) AS like_count,"
            " (SELECT sum(duration_ms) / 60000.0 FROM playlist_tracks WHERE playlist_id = playlists.id) AS minutes"
            f" FROM playlists WHERE spotify_id = ? ORDER BY {ORDERS[order]} LIMIT ?",
            (spotify_id, limit),
        ).fetchall()
