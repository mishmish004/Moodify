"""The users table: who has logged in, and the refresh token Moodify acts with."""
import json
import sqlite3
from typing import Optional

from src.db import get_conn


def upsert(user_id: str, display_name: Optional[str], refresh_token: str, granted_scopes: Optional[str] = None) -> None:
    """Create the user, or refresh name, token and granted scopes on a repeat login without touching onboarding or taste."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO users (user_id, display_name, refresh_token, granted_scopes) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (user_id) DO UPDATE SET display_name = excluded.display_name,"
            " refresh_token = excluded.refresh_token, granted_scopes = excluded.granted_scopes",
            (user_id, display_name, refresh_token, granted_scopes),
        )


def set_onboarding(user_id: str, onboarding: dict) -> None:
    """Save the questionnaire answers. New answers invalidate the cached taste summary."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET onboarding_json = ?, taste_summary = NULL, taste_updated_at = NULL WHERE user_id = ?",
            (json.dumps(onboarding), user_id),
        )


def set_taste_summary(user_id: str, summary: str, updated_at: str) -> None:
    """The cached taste summary (src/taste.py). The known-songs pool lives in the library (Phase 19)."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET taste_summary = ?, taste_updated_at = ? WHERE user_id = ?",
            (summary, updated_at, user_id),
        )


def set_likes_to_service(user_id: str, on: bool) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET likes_to_service = ? WHERE user_id = ?", (int(on), user_id))


def set_open_app(user_id: str, on: bool) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET open_app = ? WHERE user_id = ?", (int(on), user_id))


def set_refresh_token(user_id: str, refresh_token: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE users SET refresh_token = ? WHERE user_id = ?", (refresh_token, user_id))


def get(user_id: str) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def delete(user_id: str) -> None:
    """Remove the user and, through ON DELETE CASCADE, everything stored about them. Tracks in the shared
    library that nobody else has go too (Phase 19)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM tracks WHERE uri NOT IN (SELECT uri FROM library)")
