"""
Every Spotify Web API call Moodify makes, for one user.

Shapes as of the Feb 2026 changes: playlists are created with POST /me/playlists
(201) and filled with POST /playlists/{id}/items (201, at most 100 URIs per call);
playlist objects say `items`/`item` where they used to say `tracks`/`track`.
Search still answers under `tracks` -> `items`, at most 10 results. Track
popularity and /me's country, email and product are gone, so nothing reads them.
Playlist items are readable only for playlists the user owns or collaborates on (403 otherwise).
"""
import time
from typing import Optional

import httpx

from src import users
from src.core.exceptions import SpotifyError, SpotifyQuotaExceeded
from src.music import Listening, Playlist, Track, UserPlaylist, best_effort
from src.spotify import auth

API_URL = "https://api.spotify.com/v1"
SEARCH_LIMIT = 10      # Spotify's maximum since Feb 2026
PAGE = 50              # the most items one page of library, history or playlist reads returns
ADD_ITEMS_BATCH = 100  # most URIs one POST /playlists/{id}/items accepts
MAX_WAIT = 10.0        # seconds; a longer Retry-After is a real back-off, not something to sleep through

# Reads per taste rebuild (Phase 10)
TOP_READ = 10
LIKED_TO_READ = 200
OWN_PLAYLISTS_TO_READ = 5


class SpotifyClient:
    """One user's Spotify calls. Use as a context manager so the connection pool gets closed.

    The first call trades the stored refresh token for an access token. A 401 refreshes once and
    retries; a rotated refresh token is saved to the users table. ReauthRequired from the refresh
    means the user has to log in again.
    """

    def __init__(self, spotify_id: str, refresh_token: str, access_token: Optional[str] = None):
        self.spotify_id = spotify_id
        self.refresh_token = refresh_token
        self.access_token = access_token
        self.http = httpx.Client(base_url=API_URL, timeout=auth.TIMEOUT)

    @classmethod
    def for_user(cls, user) -> "SpotifyClient":
        # A Spotify user's Moodify id is their Spotify id (src/schema.sql)
        return cls(user["user_id"], user["refresh_token"])

    def __enter__(self) -> "SpotifyClient":
        return self

    def __exit__(self, *exc) -> None:
        self.http.close()

    def me(self) -> dict:
        return self._request("GET", "/me")

    def listening(self) -> Listening:
        """About 15 reads: top items (3), history, Liked Songs (4 pages), follows, playlists (1 + 5)."""
        listening = Listening()

        def read(name, call, empty):
            return best_effort(listening, self.spotify_id, name, call, empty)

        listening.top_artists = read("top artists", lambda: self.top_artists(TOP_READ, "short_term"), [])
        listening.all_time_artists = read("all-time artists", lambda: self.top_artists(TOP_READ, "long_term"), [])
        listening.top_tracks = read("top tracks", lambda: self.top_tracks(TOP_READ), [])
        listening.recent_tracks = read("recently played", self.recently_played, [])
        listening.liked = read("liked songs", lambda: self.saved_tracks(LIKED_TO_READ), [])
        listening.followed = read("followed artists", self.followed_artists, [])
        # Moodify's own playlists would only feed its suggestions back to itself
        own = [p for p in read("playlists", self.my_playlists, []) if not p.name.startswith("Moodify:")]
        listening.playlist_names = [p.name for p in own]
        for playlist in [p for p in own if p.readable][:OWN_PLAYLISTS_TO_READ]:
            listening.playlist_tracks += read(f"playlist {playlist.id}", lambda: self.playlist_items(playlist.id), [])
        return listening

    def top_artists(self, limit: int = 20, time_range: str = "medium_term") -> list[str]:
        """time_range: short_term (about 4 weeks), medium_term (6 months) or long_term (years)."""
        data = self._request("GET", "/me/top/artists", params={"limit": limit, "time_range": time_range})
        return [artist["name"] for artist in data["items"]]

    def top_tracks(self, limit: int = 20) -> list[Track]:
        data = self._request("GET", "/me/top/tracks", params={"limit": limit, "time_range": "medium_term"})
        return [_track(item) for item in data["items"]]

    def recently_played(self, limit: int = PAGE) -> list[Track]:
        data = self._request("GET", "/me/player/recently-played", params={"limit": limit})
        return _unwrap(data["items"])

    def saved_tracks(self, max_tracks: int = 200) -> list[Track]:
        """Liked Songs, newest first, one page of 50 at a time until max_tracks or the end."""
        found: list[Track] = []
        offset = 0
        while len(found) < max_tracks:
            data = self._request("GET", "/me/tracks", params={"limit": PAGE, "offset": offset})
            found += _unwrap(data["items"])
            if not data.get("next") or not data["items"]:
                break
            offset += PAGE
        return found[:max_tracks]

    def followed_artists(self, limit: int = PAGE) -> list[str]:
        data = self._request("GET", "/me/following", params={"type": "artist", "limit": limit})
        return [artist["name"] for artist in data["artists"]["items"] if artist]

    def my_playlists(self, limit: int = PAGE) -> list[UserPlaylist]:
        data = self._request("GET", "/me/playlists", params={"limit": limit})
        return [
            UserPlaylist(p["id"], p["name"], p["owner"]["id"] == self.spotify_id or bool(p.get("collaborative")))
            for p in data["items"] if p
        ]

    def playlist_items(self, playlist_id: str, limit: int = PAGE) -> list[Track]:
        """The first `limit` tracks. A playlist the user only follows answers 403 since Feb 2026: that gives []."""
        try:
            data = self._request("GET", f"/playlists/{playlist_id}/items", params={"limit": limit})
        except SpotifyError as exc:
            if exc.status == 403:
                return []
            raise
        return _unwrap(data["items"])

    def search_track(self, title: str, artist: str) -> list[Track]:
        params = {"q": f"track:{title} artist:{artist}", "type": "track", "limit": SEARCH_LIMIT}
        data = self._request("GET", "/search", params=params)
        return [_track(item) for item in data["tracks"]["items"] if item]

    def create_playlist(self, name: str, description: str = "", public: bool = False) -> Playlist:
        # public defaults to true on Spotify's side; mood playlists stay private unless the user shares them
        data = self._request("POST", "/me/playlists", json={"name": name, "description": description, "public": public})
        return Playlist(data["id"], data["external_urls"]["spotify"])

    def add_items(self, playlist_id: str, uris: list[str]) -> None:
        for start in range(0, len(uris), ADD_ITEMS_BATCH):
            self._request("POST", f"/playlists/{playlist_id}/items", json={"uris": uris[start:start + ADD_ITEMS_BATCH]})

    # Liked Songs since Feb 2026: one /me/library for every kind of item, URIs comma-separated in the
    # query string, at most 40 per call. Moodify sends one track at a time
    def library_contains(self, uris: list[str]) -> list[bool]:
        return self._request("GET", "/me/library/contains", params={"uris": ",".join(uris)})

    def save_to_library(self, uris: list[str]) -> None:
        self._request("PUT", "/me/library", params={"uris": ",".join(uris)})

    def remove_from_library(self, uris: list[str]) -> None:
        self._request("DELETE", "/me/library", params={"uris": ",".join(uris)})

    # A like is a song in Liked Songs (MusicService)
    def is_liked(self, uri: str) -> bool:
        return self.library_contains([uri])[0]

    def like(self, uri: str) -> None:
        self.save_to_library([uri])

    def unlike(self, uri: str) -> None:
        self.remove_from_library([uri])

    def queue(self, uri: str) -> None:
        """Premium only, on the user's active device: expect a SpotifyError (403/404) otherwise."""
        self._request("POST", "/me/player/queue", params={"uri": uri})

    def _request(self, method: str, path: str, **kwargs) -> Optional[dict]:
        if self.access_token is None:
            self._refresh_access_token()
        resp = self._send(method, path, **kwargs)
        if resp.status_code == 401:
            # Access tokens last an hour: refresh once, and if that doesn't help, give up
            self._refresh_access_token()
            resp = self._send(method, path, **kwargs)
        if resp.status_code == 429:
            self._wait_or_give_up(resp)
            resp = self._send(method, path, **kwargs)
            if resp.status_code == 429:
                raise SpotifyQuotaExceeded(429, resp.text)
        if not resp.is_success:
            raise SpotifyError(resp.status_code, resp.text)
        return resp.json() if resp.content else None

    def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            return self.http.request(method, path, headers={"Authorization": f"Bearer {self.access_token}"}, **kwargs)
        except httpx.HTTPError as exc:
            raise SpotifyError(0, repr(exc)) from exc

    def _refresh_access_token(self) -> None:
        tokens = auth.refresh(self.refresh_token)
        self.access_token = tokens.access_token
        if tokens.refresh_token != self.refresh_token:
            self.refresh_token = tokens.refresh_token
            users.set_refresh_token(self.spotify_id, tokens.refresh_token)

    @staticmethod
    def _wait_or_give_up(resp: httpx.Response) -> None:
        # The quota 429 carries "reason": "QUOTA_EXCEEDED" in a JSON body whose full shape isn't documented
        if "QUOTA_EXCEEDED" in resp.text:
            raise SpotifyQuotaExceeded(429, resp.text)
        wait = _retry_after(resp)
        if wait > MAX_WAIT:
            raise SpotifyQuotaExceeded(429, resp.text)
        time.sleep(wait)


def _retry_after(resp: httpx.Response) -> float:
    try:
        return max(0.0, float(resp.headers.get("Retry-After", 1)))
    except ValueError:
        return 1.0


def _track(item: dict) -> Track:
    return Track(item["uri"], item["name"], tuple(a["name"] for a in item["artists"]), item["duration_ms"],
                 bool(item.get("explicit")))


def _unwrap(entries: list[dict]) -> list[Track]:
    """Tracks from list entries that wrap one: {"track": ...} in history and Liked Songs, {"item": ...} in
    playlists since Feb 2026. Skips podcast episodes, local files and removed tracks (null)."""
    tracks = []
    for entry in entries:
        item = (entry or {}).get("item") or (entry or {}).get("track")
        if not item or item.get("type", "track") != "track" or entry.get("is_local") or item.get("is_local"):
            continue
        tracks.append(_track(item))
    return tracks
