"""The playlists table: every playlist Moodify made, with its tracks, so the app can show, list and re-queue them."""
import json
import sqlite3
from typing import Optional

from src.db import get_conn
from src.spotify.client import Track


def insert(spotify_id: str, prompt: str, spotify_url: str, tracks: list[Track]) -> int:
    """The playlist row and its tracks, in order, in one transaction."""
    with get_conn() as conn:
        playlist_id = conn.execute(
            "INSERT INTO playlists (spotify_id, prompt, spotify_url, track_uris_json) VALUES (?, ?, ?, ?)",
            (spotify_id, prompt, spotify_url, json.dumps([t.uri for t in tracks])),
        ).lastrowid
        conn.executemany(
            "INSERT INTO playlist_tracks (playlist_id, position, uri, name, artists_json, duration_ms)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(playlist_id, i, t.uri, t.name, json.dumps(list(t.artists)), t.duration_ms) for i, t in enumerate(tracks)],
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


def recent(spotify_id: str, limit: int = 5) -> list[sqlite3.Row]:
    """Newest first, with track_count. limit=-1 means all of them (SQLite's "no limit")."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT *, json_array_length(track_uris_json) AS track_count FROM playlists"
            " WHERE spotify_id = ? ORDER BY id DESC LIMIT ?",
            (spotify_id, limit),
        ).fetchall()
