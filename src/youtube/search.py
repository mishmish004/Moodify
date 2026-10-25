"""
Phase 27: finding songs on YouTube Music, with ytmusicapi's search and no login.

Unofficial: ytmusicapi calls YouTube Music's own web API, which costs no YouTube Data API quota (whose search is
capped at 100 calls a day for the whole app). It can break when YouTube changes that API, so every failure
becomes a YouTubeError, and src/youtube/client.py stops a build's searching after a few in a row.
Results are songs only (filter "songs"), with their video id, title, artists, length and explicit flag.
"""
import threading

import requests
from ytmusicapi import YTMusic

from src.core.exceptions import YouTubeError
from src.music import Track

LIMIT = 10      # candidates per search, as on Spotify
TIMEOUT = 10.0  # seconds per request; ytmusicapi sets none of its own

_lock = threading.Lock()  # one YTMusic and one HTTP session for the process; builds search one song at a time
_client: YTMusic | None = None


class _Session(requests.Session):
    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", TIMEOUT)
        return super().request(*args, **kwargs)


def search(title: str, artist: str) -> list[Track]:
    global _client
    try:
        with _lock:
            if _client is None:
                _client = YTMusic(requests_session=_Session())
            results = _client.search(f"{title} {artist}", filter="songs", limit=LIMIT)
    except Exception as exc:  # network errors, YouTube's errors, and parsing errors after a change on their side
        raise YouTubeError(0, f"search failed: {exc!r}") from exc
    return [track for track in map(_track, results[:LIMIT]) if track]


def _track(result: dict) -> Track | None:
    video_id = result.get("videoId")
    if not video_id or result.get("resultType", "song") != "song":
        return None
    artists = tuple(a["name"] for a in result.get("artists") or [] if a.get("name"))
    return Track(f"youtube:track:{video_id}", result.get("title") or "", artists,
                 int(result.get("duration_seconds") or 0) * 1000, bool(result.get("isExplicit")))
