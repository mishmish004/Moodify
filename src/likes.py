"""
Phase 14: likes on tracks, kept in Moodify and, if the user wants, on their service too (Spotify's Liked Songs).

A like is per song, not per playlist row: a song liked in one playlist shows liked in all of them.
The like is stored first, so a failure on the service never loses it. Unlike takes a song out of the
service's likes only if Moodify put it there, so it never undoes a like the user made there themselves. That
holds even with the setting off: unliking undoes what Moodify did, and nothing more.
"""
import sqlite3
from typing import Optional

from src import music
from src.core.exceptions import ReauthRequired, ServiceError
from src.core.logger import MoodifyLogger
from src.db import get_conn
from src.memory import invalidate

log = MoodifyLogger.get_logger("likes")

# What happened on the service's side
SAVED = "saved"          # liked there (Spotify: added to Liked Songs)
REMOVED = "removed"      # the like taken back there
ALREADY = "already"      # liked there before Moodify: left alone, now and on unlike
KEPT = "kept"            # unliked, but it was liked there before Moodify, so it stays
OFF = "off"              # the user's setting: likes stay in Moodify
UNCHANGED = "unchanged"  # liked twice, or unliked something that wasn't liked: nothing to do
FAILED = "failed"        # the service refused or didn't answer; the like itself is stored

# Filled in with the service's name and where its likes go (src/music)
NOTICES = {
    ("like", FAILED): "Liked in Moodify, but your {name} {likes} couldn't be updated.",
    ("unlike", FAILED): "Unliked in Moodify, but the song is still in your {name} {likes}.",
}


def like(user: sqlite3.Row, uri: str, playlist_id: int) -> str:
    """Store the like, then like the song on the service unless it's liked there already or the setting is off."""
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
        with music.for_user(user) as service:
            if service.is_liked(uri):
                return ALREADY
            service.like(uri)
    except ReauthRequired:
        raise
    except ServiceError as exc:
        log.warning("couldn't like %s on %s: %s", uri, user["service"], exc)
        return FAILED
    with get_conn() as conn:
        conn.execute("UPDATE track_likes SET added_to_service = 1 WHERE user_id = ? AND uri = ?",
                     (user["user_id"], uri))
    return SAVED


def unlike(user: sqlite3.Row, uri: str) -> str:
    """Remove the like, and take back the like on the service if Moodify made it there."""
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
        with music.for_user(user) as service:
            service.unlike(uri)
    except ReauthRequired:
        raise
    except ServiceError as exc:
        log.warning("couldn't take back the like of %s on %s: %s", uri, user["service"], exc)
        return FAILED
    return REMOVED


def notice(action: str, outcome: str, service: str = "spotify") -> Optional[str]:
    """The sentence to show for this outcome, if any. Only failures are worth interrupting for."""
    text = NOTICES.get((action, outcome))
    found = music.info(service)
    return text.format(name=found.name, likes=found.likes) if text else None


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
