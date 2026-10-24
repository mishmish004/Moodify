"""
Phase 24: what everyone has been up to, from the tables that already exist (playlists, playlist_tracks,
blends, taste_access, track_likes); nothing new is stored.

Who appears: you, plus friends who show their taste (Phase 18). What never appears: anyone's prompt (titles
only), support-note playlists (not even your own), other people's hidden playlists, reviews, and other
people's likes and ratings. A Blend shows only to the people in it, and a playlist made "the way Sam would
hear it" only to its maker and Sam.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from src import sharing, users
from src.db import get_conn

DAYS = 30
FEED_LIMIT = 100
NOT_SUPPORT = "coalesce(json_extract(p.brief_json, '$.support_note'), 0) = 0"


@dataclass
class Item:
    """One line: "{who} {verb} {title}{after}", e.g. "Sam made *Sunday cooking* · 58 min · 3 parts"."""
    at: str                 # UTC timestamp, for sorting
    kind: str               # playlist | blend | blend_with_you | heard_as_you | like | rating
    who: str                # first name, or "You"
    verb: str
    title: Optional[str] = None
    after: str = ""
    playlist_id: Optional[int] = None  # only for your own playlists (a link to the page)

    @property
    def day(self) -> str:
        return self.at[:10]


def _since() -> str:
    return (datetime.utcnow() - timedelta(days=DAYS)).strftime("%Y-%m-%d %H:%M:%S")


def people(viewer: str) -> list:
    me = users.get(viewer)
    return ([me] if me else []) + [users.get(f["spotify_id"]) for f in sharing.friends(viewer, "taste")]


def _playlists(conn, owner: str, viewer: str) -> list:
    """`owner`'s playlists from the last DAYS days that `viewer` may see, newest first. Never the prompt."""
    mine = owner == viewer
    rows = conn.execute(
        "SELECT p.id, p.created_at, json_extract(p.brief_json, '$.title') AS title, p.heard_as, p.hidden,"
        " json_array_length(json_extract(p.brief_json, '$.sections')) AS parts,"
        " (SELECT sum(duration_ms) FROM playlist_tracks t WHERE t.playlist_id = p.id) AS ms,"
        " (SELECT avg(energy) FROM playlist_tracks t WHERE t.playlist_id = p.id) AS energy,"
        " (SELECT avg(value ->> '$.energy') FROM json_each(p.brief_json, '$.sections')) AS brief_energy,"
        " b.id AS blend_id"
        f" FROM playlists p LEFT JOIN blends b ON b.playlist_id = p.id WHERE p.spotify_id = ? AND {NOT_SUPPORT}"
        " AND p.created_at >= ?" + ("" if mine else " AND p.hidden = 0") + " ORDER BY p.id DESC",
        (owner, _since()),
    ).fetchall()
    out = []
    for r in rows:
        if r["blend_id"] is not None and not mine:
            member = conn.execute("SELECT 1 FROM blend_members WHERE blend_id = ? AND spotify_id = ?",
                                  (r["blend_id"], viewer)).fetchone()
            if not member:
                continue  # a Blend shows only to the people in it
        if r["heard_as"] and not mine and r["heard_as"] != viewer:
            continue      # "the way Sam would hear it" shows only to its maker and Sam
        out.append(r)
    return out


def _minutes(row) -> int:
    return round((row["ms"] or 0) / 60_000)


def _parts(row) -> str:
    n = row["parts"] or 1
    return f" · {n} parts" if n > 1 else ""


def feed(viewer: str) -> list[Item]:
    """Newest first, at most FEED_LIMIT items from the last DAYS days."""
    items: list[Item] = []
    shown = people(viewer)
    names = {p["spotify_id"]: sharing.first_name(p) for p in shown}
    with get_conn() as conn:
        for person in shown:
            sid = person["spotify_id"]
            mine = sid == viewer
            who = "You" if mine else names[sid]
            for r in _playlists(conn, sid, viewer):
                title = r["title"] or "a playlist"
                details = f" · {_minutes(r)} min{_parts(r)}"
                link = r["id"] if mine else None
                if r["blend_id"] is not None:
                    others = [sharing.first_name(m) if m["spotify_id"] != viewer else "you" for m in conn.execute(
                        "SELECT u.spotify_id, u.display_name FROM blend_members m JOIN users u USING (spotify_id)"
                        " WHERE m.blend_id = ? AND m.spotify_id != ? ORDER BY m.position", (r["blend_id"], sid))]
                    kind = "blend" if mine else "blend_with_you"
                    items.append(Item(r["created_at"], kind, who, f"made a Blend with {_and(others)}:", title, details, link))
                elif r["heard_as"] and not mine:  # only that friend gets here (see _playlists)
                    items.append(Item(r["created_at"], "heard_as_you", who, "made", title,
                                      f" the way you'd hear it{details}"))
                elif r["heard_as"]:
                    friend = users.get(r["heard_as"])
                    whose = sharing.first_name(friend) if friend else "a friend"
                    items.append(Item(r["created_at"], "playlist", who, "made", title,
                                      f" the way {whose} would hear it{details}", link))
                else:
                    items.append(Item(r["created_at"], "playlist", who, "made", title, details, link))
        # Blends you're in that someone who doesn't show their taste made
        for r in conn.execute(
            "SELECT b.owner, u.display_name, u.spotify_id, p.created_at, json_extract(p.brief_json, '$.title') AS title"
            " FROM blend_members m JOIN blends b ON b.id = m.blend_id JOIN playlists p ON p.id = b.playlist_id"
            f" JOIN users u ON u.spotify_id = b.owner WHERE m.spotify_id = ? AND b.owner != ? AND p.created_at >= ? AND {NOT_SUPPORT}",
            (viewer, viewer, _since()),
        ):
            if r["owner"] not in names:
                items.append(Item(r["created_at"], "blend_with_you", sharing.first_name(r), "made a Blend with you:", r["title"]))
        # Your own likes and ratings: yours only
        for r in conn.execute(
            "SELECT l.liked_at, min(t.name) AS name FROM track_likes l JOIN playlist_tracks t ON t.uri = l.uri"
            " JOIN playlists p ON p.id = t.playlist_id AND p.spotify_id = l.spotify_id"
            " WHERE l.spotify_id = ? AND l.liked_at >= ? GROUP BY l.uri", (viewer, _since())):
            items.append(Item(r["liked_at"], "like", "You", "liked", r["name"]))
        for r in conn.execute(
            "SELECT p.id, p.rated_at, p.rating, json_extract(p.brief_json, '$.title') AS title FROM playlists p"
            f" WHERE p.spotify_id = ? AND p.rated_at >= ? AND {NOT_SUPPORT}", (viewer, _since())):
            items.append(Item(r["rated_at"], "rating", "You", "rated", r["title"], f" {r['rating']}/10", r["id"]))
    items.sort(key=lambda i: i.at, reverse=True)
    return items[:FEED_LIMIT]


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def timeline(viewer: str) -> dict:
    """One strip per person over the last DAYS days: a mark per playlist they made, by date, with its length
    and average energy (the songs' energy, else the brief's). Titles only."""
    today = date.today()
    days = [(today - timedelta(days=DAYS - 1 - i)).isoformat() for i in range(DAYS)]
    out = []
    with get_conn() as conn:
        for person in people(viewer):
            sid = person["spotify_id"]
            marks = []
            for r in _playlists(conn, sid, viewer):
                energy = r["energy"] if r["energy"] is not None else r["brief_energy"]
                marks.append({"day": r["created_at"][:10], "title": r["title"] or "a playlist", "minutes": _minutes(r),
                              "energy": None if energy is None else round(min(10.0, max(1.0, float(energy))), 1),
                              "parts": r["parts"] or 1})
            out.append({"id": sid, "name": sharing.first_name(person), "me": sid == viewer,
                        "marks": sorted(marks, key=lambda m: m["day"])})
    return {"days": days, "people": out}
