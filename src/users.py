"""The users table: who has logged in, and the refresh token Moodify acts with."""
import sqlite3
from typing import Optional

from src.db import get_conn


def upsert(spotify_id: str, display_name: Optional[str], refresh_token: str) -> None:
    """Create the user, or refresh name and token on a repeat login without touching onboarding or taste."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (spotify_id, display_name, refresh_token) VALUES (?, ?, ?)"
            " ON CONFLICT (spotify_id) DO UPDATE SET"
            " display_name = excluded.display_name, refresh_token = excluded.refresh_token",
            (spotify_id, display_name, refresh_token),
        )


def get(spotify_id: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE spotify_id = ?", (spotify_id,)).fetchone()


def delete(spotify_id: str) -> None:
    """Remove the user and, through ON DELETE CASCADE, their playlists."""
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE spotify_id = ?", (spotify_id,))
