"""
Phase 19: the shared music library, one place holding each person's music in a shape that compares across people.

tracks holds each track's details once, for everyone who has it. library holds who has which track, from
which source, with a weight. A user's rows are rebuilt from the same Spotify reads as the taste profile
(src/taste.py, Phase 10), with no extra calls, and replaced in one transaction, so a song taken out of
Liked Songs leaves the library. Spotify Developer Terms IV.3.1 forbid keeping Spotify Content
indefinitely: rows not refreshed for RETENTION_DAYS are deleted at startup, disconnect deletes a user's
rows, and a track nobody has any more goes with them.
"""
import json
import sqlite3
from dataclasses import dataclass
from typing import Iterable

from src.db import get_conn
from src.spotify.client import Track

# Source -> weight, in order of how much a source says about someone's taste. Spotify's top tracks are read
# for the 6-month range only (Phase 10), and Phase 19 adds no calls, so there's one "top" source, not a
# short- and a long-term one. "moodify": tracks in playlists Moodify made for them, from the database.
WEIGHTS = {"top": 3, "recent": 2, "liked": 1, "own_playlist": 1, "moodify": 1}
SOURCES = tuple(WEIGHTS)
POOL_SOURCES = ("top", "recent", "liked", "own_playlist")  # the known-songs pool: what Phase 10 offered
MAX_ROWS = 600
POOL_SIZE = 150
RETENTION_DAYS = 30


@dataclass(frozen=True)
class Common:
    """What two people share. Weights are the sum of both people's, so common(a, b) == common(b, a)."""
    tracks: list[tuple[Track, int]]
    artists: list[tuple[str, int]]


def _track(row) -> Track:
    return Track(row["uri"], row["name"], tuple(json.loads(row["artists_json"])), row["duration_ms"], bool(row["explicit"]))


def replace(user_id: str, sources: dict[str, Iterable[Track]]) -> dict[str, int]:
    """Replace the user's library with these tracks by source (keys of WEIGHTS, each in its own order), plus
    their Moodify playlists. Keeps the MAX_ROWS heaviest rows. Returns how many rows each source kept."""
    rows, seen = [], set()
    with get_conn() as conn:
        # Friends see the library (Phases 20-23): hidden and support-note playlists stay out of it
        moodify = [_track(r) for r in conn.execute(
            "SELECT t.uri, t.name, t.artists_json, t.duration_ms, 0 AS explicit FROM playlist_tracks t"
            " JOIN playlists p ON p.id = t.playlist_id WHERE p.user_id = ? AND p.hidden = 0"
            " AND coalesce(json_extract(p.brief_json, '$.support_note'), 0) = 0 ORDER BY p.id DESC, t.position",
            (user_id,))]
        everything = {**{s: list(tracks) for s, tracks in sources.items()}, "moodify": moodify}
        for source in SOURCES:
            for position, track in enumerate(everything.get(source, [])):
                if (track.uri, source) not in seen:
                    seen.add((track.uri, source))
                    rows.append((source, position, track))
        # Heaviest first; among equals, the more telling source, then its own order
        rows.sort(key=lambda r: (-WEIGHTS[r[0]], SOURCES.index(r[0]), r[1]))
        rows = rows[:MAX_ROWS]
        for source, _, track in rows:
            if source == "moodify":  # playlist_tracks doesn't keep the explicit flag: don't overwrite a known one
                conn.execute("INSERT INTO tracks (uri, name, artists_json, duration_ms, explicit) VALUES (?, ?, ?, ?, 0)"
                             " ON CONFLICT (uri) DO UPDATE SET last_seen = CURRENT_TIMESTAMP",
                             (track.uri, track.name, json.dumps(list(track.artists)), track.duration_ms))
            else:
                conn.execute("INSERT INTO tracks (uri, name, artists_json, duration_ms, explicit) VALUES (?, ?, ?, ?, ?)"
                             " ON CONFLICT (uri) DO UPDATE SET name = excluded.name, artists_json = excluded.artists_json,"
                             " duration_ms = excluded.duration_ms, explicit = excluded.explicit, last_seen = CURRENT_TIMESTAMP",
                             (track.uri, track.name, json.dumps(list(track.artists)), track.duration_ms, int(track.explicit)))
        conn.execute("DELETE FROM library WHERE user_id = ?", (user_id,))
        conn.executemany(
            "INSERT INTO library (user_id, uri, source, weight, position) VALUES (?, ?, ?, ?, ?)",
            [(user_id, track.uri, source, WEIGHTS[source], position) for source, position, track in rows],
        )
        _delete_orphans(conn)
    counts = {source: 0 for source in SOURCES}
    for source, _, _ in rows:
        counts[source] += 1
    return counts


def _delete_orphans(conn: sqlite3.Connection) -> int:
    return conn.execute("DELETE FROM tracks WHERE uri NOT IN (SELECT uri FROM library)").rowcount


def delete_orphans() -> int:
    with get_conn() as conn:
        return _delete_orphans(conn)


def prune() -> int:
    """At startup: rows not refreshed for RETENTION_DAYS go, and tracks nobody has any more go with them."""
    with get_conn() as conn:
        gone = conn.execute("DELETE FROM library WHERE refreshed_at < datetime('now', ?)",
                            (f"-{RETENTION_DAYS} days",)).rowcount
        _delete_orphans(conn)
    return gone


def counts(user_id: str) -> dict[str, int]:
    with get_conn() as conn:
        rows = conn.execute("SELECT source, count(*) FROM library WHERE user_id = ? GROUP BY source", (user_id,))
        found = dict(rows.fetchall())
    return {source: found.get(source, 0) for source in SOURCES}


def for_user(user_id: str) -> list[tuple[Track, int]]:
    """Every track the user has, with the weights of all its sources added up, heaviest first."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT t.*, sum(l.weight) AS weight FROM library l JOIN tracks t ON t.uri = l.uri WHERE l.user_id = ?"
            " GROUP BY l.uri ORDER BY weight DESC, min(l.position), l.uri",
            (user_id,),
        ).fetchall()
    return [(_track(r), r["weight"]) for r in rows]


def _artist_weights(user_id: str) -> dict[str, int]:
    weights: dict[str, int] = {}
    for track, weight in for_user(user_id):
        for artist in track.artists[:1]:  # the main artist; features would count everyone twice
            weights[artist] = weights.get(artist, 0) + weight
    return weights


def top_artists(user_id: str, n: int = 10) -> list[tuple[str, int]]:
    """Main artists by summed track weight."""
    return sorted(_artist_weights(user_id).items(), key=lambda item: (-item[1], item[0].casefold()))[:n]


def common(a: str, b: str, n: int = 20) -> Common:
    """Tracks and main artists both people have, by their combined weight. Symmetric."""
    mine, theirs = dict((t.uri, (t, w)) for t, w in for_user(a)), dict((t.uri, (t, w)) for t, w in for_user(b))
    tracks = [(mine[uri][0], mine[uri][1] + theirs[uri][1]) for uri in mine.keys() & theirs.keys()]
    tracks.sort(key=lambda item: (-item[1], item[0].uri))
    artists_a, artists_b = _artist_weights(a), _artist_weights(b)
    artists = [(name, artists_a[name] + artists_b[name]) for name in artists_a.keys() & artists_b.keys()]
    artists.sort(key=lambda item: (-item[1], item[0].casefold()))
    return Common(tracks[:n], artists[:n])


def pool(user_id: str, size: int = POOL_SIZE) -> list[Track]:
    """The known-songs pool (Phase 10) from the library: top, recent, liked, then own-playlist tracks, each in
    Spotify's order, every song once at its first place. The same list Phase 10 built from the same reads."""
    ranks = " ".join(f"WHEN '{s}' THEN {i}" for i, s in enumerate(POOL_SOURCES))
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT t.*, min((CASE l.source {ranks} END) * 100000 + l.position) AS place FROM library l"
            f" JOIN tracks t ON t.uri = l.uri WHERE l.user_id = ? AND l.source IN ({','.join('?' * len(POOL_SOURCES))})"
            " GROUP BY l.uri ORDER BY place LIMIT ?",
            (user_id, *POOL_SOURCES, size),
        ).fetchall()
    return [_track(r) for r in rows]
