"""
Phase 14: likes on tracks, kept in Moodify and, if the user wants, in their Spotify Liked Songs.

A like is per song, not per playlist row: a song liked in one playlist shows liked in all of them.
The like is stored first, so a Spotify failure never loses it. Unlike takes a song out of Liked Songs
only if Moodify put it there, so it never undoes a like the user made in Spotify themselves. That
holds even with the setting off: unliking undoes what Moodify did, and nothing more.
"""
import sqlite3
from typing import Optional

from src.core.exceptions import ReauthRequired, SpotifyError
from src.core.logger import MoodifyLogger
from src.db import get_conn
from src.memory import invalidate
from src.spotify.client import SpotifyClient

log = MoodifyLogger.get_logger("likes")

# What happened on Spotify's side
SAVED = "saved"          # added to Liked Songs
REMOVED = "removed"      # taken out of Liked Songs
ALREADY = "already"      # was in Liked Songs before Moodify: left alone, now and on unlike
KEPT = "kept"            # unliked, but Liked Songs had it before Moodify, so it stays there
OFF = "off"              # the user's setting: likes stay in Moodify
UNCHANGED = "unchanged"  # liked twice, or unliked something that wasn't liked: nothing to do
FAILED = "failed"        # Spotify refused or didn't answer; the like itself is stored

NOTICES = {
    ("like", FAILED): "Liked in Moodify, but your Spotify Liked Songs couldn't be updated.",
    ("unlike", FAILED): "Unliked in Moodify, but the song is still in your Spotify Liked Songs.",
}


def like(user: sqlite3.Row, uri: str, playlist_id: int) -> str:
    """Store the like, then save the song to Liked Songs unless it's there already or the setting is off."""
    with get_conn() as conn:
        new = conn.execute(
            "INSERT OR IGNORE INTO track_likes (user_id, uri, playlist_id) VALUES (?, ?, ?)",
            (user["user_id"], uri, playlist_id),
        ).rowcount
        if new:
            invalidate(conn, user["user_id"])
    if not new:
        return UNCHANGED
    if not user["likes_to_service"]:
        return OFF
    try:
        with SpotifyClient.for_user(user) as sp:
            if sp.library_contains([uri])[0]:
                return ALREADY
            sp.save_to_library([uri])
    except ReauthRequired:
        raise
    except SpotifyError as exc:
        log.warning("couldn't save %s to Liked Songs: %s", uri, exc)
        return FAILED
    with get_conn() as conn:
        conn.execute("UPDATE track_likes SET added_to_service = 1 WHERE user_id = ? AND uri = ?",
                     (user["user_id"], uri))
    return SAVED


def unlike(user: sqlite3.Row, uri: str) -> str:
    """Remove the like, and the song from Liked Songs if Moodify added it there."""
    with get_conn() as conn:
        row = conn.execute("DELETE FROM track_likes WHERE user_id = ? AND uri = ? RETURNING added_to_service",
                           (user["user_id"], uri)).fetchone()
        if row is not None:
            invalidate(conn, user["user_id"])
    if row is None:
        return UNCHANGED
    if not row["added_to_service"]:
        return KEPT
    try:
        with SpotifyClient.for_user(user) as sp:
            sp.remove_from_library([uri])
    except ReauthRequired:
        raise
    except SpotifyError as exc:
        log.warning("couldn't remove %s from Liked Songs: %s", uri, exc)
        return FAILED
    return REMOVED


def notice(action: str, outcome: str) -> Optional[str]:
    """The sentence to show for this outcome, if any. Only failures are worth interrupting for."""
    return NOTICES.get((action, outcome))


def liked_uris(user_id: str, uris: list[str]) -> set[str]:
    """Which of these songs the user has liked, wherever they liked them."""
    if not uris:
        return set()
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT uri FROM track_likes WHERE user_id = ? AND uri IN ({','.join('?' * len(uris))})",
            (user_id, *uris),
        ).fetchall()
    return {r["uri"] for r in rows}
