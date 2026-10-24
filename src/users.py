"""The users table: who has logged in, and the refresh token Moodify acts with."""
import json
import sqlite3
from typing import Optional

from src.db import get_conn


def upsert(spotify_id: str, display_name: Optional[str], refresh_token: str, granted_scopes: Optional[str] = None) -> None:
    """Create the user, or refresh name, token and granted scopes on a repeat login without touching onboarding or taste."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (spotify_id, display_name, refresh_token, granted_scopes) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (spotify_id) DO UPDATE SET display_name = excluded.display_name,"
            " refresh_token = excluded.refresh_token, granted_scopes = excluded.granted_scopes",
            (spotify_id, display_name, refresh_token, granted_scopes),
        )


def set_onboarding(spotify_id: str, onboarding: dict) -> None:
    """Save the questionnaire answers. New answers invalidate the cached taste summary."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET onboarding_json = ?, taste_summary = NULL, taste_updated_at = NULL WHERE spotify_id = ?",
            (json.dumps(onboarding), spotify_id),
        )


def set_taste_summary(spotify_id: str, summary: str, updated_at: str, pool_json: Optional[str] = None) -> None:
    """The summary and the known-songs pool are cached together (src/taste.py)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET taste_summary = ?, taste_pool_json = ?, taste_updated_at = ? WHERE spotify_id = ?",
            (summary, pool_json, updated_at, spotify_id),
        )


def set_likes_to_spotify(spotify_id: str, on: bool) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET likes_to_spotify = ? WHERE spotify_id = ?", (int(on), spotify_id))


def set_refresh_token(spotify_id: str, refresh_token: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET refresh_token = ? WHERE spotify_id = ?", (refresh_token, spotify_id))


def get(spotify_id: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE spotify_id = ?", (spotify_id,)).fetchone()


def delete(spotify_id: str) -> None:
    """Remove the user and, through ON DELETE CASCADE, everything stored about them."""
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE spotify_id = ?", (spotify_id,))
