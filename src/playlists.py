"""The playlists table: every playlist Moodify made, so the app can list and re-queue them."""
import json
import sqlite3
from typing import Optional

from src.db import get_conn


def insert(spotify_id: str, prompt: str, spotify_url: str, track_uris: list[str]) -> int:
    with get_conn() as conn:
        return conn.execute(
            "INSERT INTO playlists (spotify_id, prompt, spotify_url, track_uris_json) VALUES (?, ?, ?, ?)",
            (spotify_id, prompt, spotify_url, json.dumps(track_uris)),
        ).lastrowid


def get(playlist_id: int, spotify_id: str) -> Optional[sqlite3.Row]:
    """Only the owner's playlists: the id alone is not enough."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM playlists WHERE id = ? AND spotify_id = ?", (playlist_id, spotify_id)
        ).fetchone()


def recent(spotify_id: str, limit: int = 5) -> list[sqlite3.Row]:
    """Newest first, with track_count. limit=-1 means all of them (SQLite's "no limit")."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT *, json_array_length(track_uris_json) AS track_count FROM playlists"
            " WHERE spotify_id = ? ORDER BY id DESC LIMIT ?",
            (spotify_id, limit),
        ).fetchall()
