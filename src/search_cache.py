"""
Spotify search results, kept 7 days per user (phase-plan.md, caching layer 1).

The same song often comes up again in a user's next playlists, and every search costs quota.
Kept per user, not shared: results follow each user's market, so a track one friend can play
may be unavailable to another. Disconnect deletes a user's entries with them.
"""
import json
from typing import Optional

from src.db import get_conn
from src.spotify.client import Track

TTL_DAYS = 7


def key(title: str, artist: str) -> str:
    return " ".join(f"{title}|{artist}".lower().split())


def get(spotify_id: str, title: str, artist: str) -> Optional[list[Track]]:
    """The cached candidates, or None when there's no fresh entry."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT tracks_json FROM search_cache WHERE spotify_id = ? AND query = ?"
            f" AND fetched_at >= datetime('now', '-{TTL_DAYS} days')",
            (spotify_id, key(title, artist)),
        ).fetchone()
    if row is None:
        return None
    return [Track(uri, name, tuple(artists), ms, explicit) for uri, name, artists, ms, explicit in json.loads(row[0])]


def put(spotify_id: str, title: str, artist: str, tracks: list[Track]) -> None:
    payload = json.dumps([[t.uri, t.name, list(t.artists), t.duration_ms, t.explicit] for t in tracks])
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO search_cache (spotify_id, query, tracks_json) VALUES (?, ?, ?)"
            " ON CONFLICT (spotify_id, query) DO UPDATE SET tracks_json = excluded.tracks_json,"
            " fetched_at = CURRENT_TIMESTAMP",
            (spotify_id, key(title, artist), payload),
        )


def prune() -> int:
    """Drop expired entries. Run at startup."""
    with get_conn() as conn:
        return conn.execute(f"DELETE FROM search_cache WHERE fetched_at < datetime('now', '-{TTL_DAYS} days')").rowcount
