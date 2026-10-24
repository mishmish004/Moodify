"""The playlists table: every playlist Moodify made, with its tracks, so the app can show, list and re-queue them."""
import json
import sqlite3
from typing import Optional

from src.db import get_conn
from src.spotify.client import Track


def insert(
    spotify_id: str, prompt: str, spotify_url: str, tracks: list[Track], brief_json: Optional[str] = None,
    sections: Optional[list[int]] = None, energies: Optional[list[Optional[int]]] = None,
    roles: Optional[list[Optional[str]]] = None,
) -> int:
    """The playlist row and its tracks, in order, in one transaction. `sections`, `energies`, `roles`: per track."""
    sections = sections or [None] * len(tracks)
    energies = energies or [None] * len(tracks)
    roles = roles or [None] * len(tracks)
    with get_conn() as conn:
        playlist_id = conn.execute(
            "INSERT INTO playlists (spotify_id, prompt, spotify_url, track_uris_json, brief_json) VALUES (?, ?, ?, ?, ?)",
            (spotify_id, prompt, spotify_url, json.dumps([t.uri for t in tracks]), brief_json),
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


def track_at(playlist_id: int, position: int) -> Optional[sqlite3.Row]:
    """One row of playlist_tracks. Check the playlist's owner with get() first."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM playlist_tracks WHERE playlist_id = ? AND position = ?", (playlist_id, position)
        ).fetchone()


def set_rating(playlist_id: int, spotify_id: str, rating: int) -> bool:
    """1-10, replacing any earlier rating. False if the playlist isn't this user's."""
    with get_conn() as conn:
        return conn.execute(
            "UPDATE playlists SET rating = ?, rated_at = CURRENT_TIMESTAMP WHERE id = ? AND spotify_id = ?",
            (rating, playlist_id, spotify_id),
        ).rowcount == 1


ORDERS = {
    "new": "id DESC",
    "rating": "rating IS NULL, rating DESC, id DESC",  # unrated last
}


def recent(spotify_id: str, limit: int = 5, order: str = "new") -> list[sqlite3.Row]:
    """With track_count and like_count (its songs the user liked, wherever they liked them).
    Newest first, or by `order` (a key of ORDERS). limit=-1 means all of them (SQLite's "no limit")."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT *, json_array_length(track_uris_json) AS track_count, json_extract(brief_json, '$.title') AS title,"
            " (SELECT count(*) FROM playlist_tracks t JOIN track_likes l ON l.spotify_id = playlists.spotify_id"
            "  AND l.uri = t.uri WHERE t.playlist_id = playlists.id) AS like_count"
            f" FROM playlists WHERE spotify_id = ? ORDER BY {ORDERS[order]} LIMIT ?",
            (spotify_id, limit),
        ).fetchall()
