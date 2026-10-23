"""
Every Spotify Web API call Moodify makes, for one user.

Shapes as of the Feb 2026 changes: playlists are created with POST /me/playlists
(201) and filled with POST /playlists/{id}/items (201, at most 100 URIs per call);
playlist objects say `items`/`item` where they used to say `tracks`/`track`.
Search still answers under `tracks` -> `items`, at most 10 results. Track
popularity and /me's country, email and product are gone, so nothing reads them.
"""
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from src import users
from src.core.exceptions import SpotifyError, SpotifyQuotaExceeded
from src.spotify import auth

API_URL = "https://api.spotify.com/v1"
SEARCH_LIMIT = 10      # Spotify's maximum since Feb 2026
ADD_ITEMS_BATCH = 100  # most URIs one POST /playlists/{id}/items accepts
MAX_WAIT = 10.0        # seconds; a longer Retry-After is a real back-off, not something to sleep through


@dataclass(frozen=True)
class Track:
    uri: str
    name: str
    artists: tuple[str, ...]
    duration_ms: int


@dataclass(frozen=True)
class Playlist:
    id: str
    url: str


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
        return cls(user["spotify_id"], user["refresh_token"])

    def __enter__(self) -> "SpotifyClient":
        return self

    def __exit__(self, *exc) -> None:
        self.http.close()

    def me(self) -> dict:
        return self._request("GET", "/me")

    def top_artists(self, limit: int = 20) -> list[str]:
        data = self._request("GET", "/me/top/artists", params={"limit": limit, "time_range": "medium_term"})
        return [artist["name"] for artist in data["items"]]

    def top_tracks(self, limit: int = 20) -> list[Track]:
        data = self._request("GET", "/me/top/tracks", params={"limit": limit, "time_range": "medium_term"})
        return [_track(item) for item in data["items"]]

    def search_track(self, title: str, artist: str) -> list[Track]:
        params = {"q": f"track:{title} artist:{artist}", "type": "track", "limit": SEARCH_LIMIT}
        data = self._request("GET", "/search", params=params)
        return [_track(item) for item in data["tracks"]["items"] if item]

    def create_playlist(self, name: str, description: str = "") -> Playlist:
        # public defaults to true on Spotify's side; mood playlists stay private unless the user shares them
        data = self._request("POST", "/me/playlists", json={"name": name, "description": description, "public": False})
        return Playlist(data["id"], data["external_urls"]["spotify"])

    def add_items(self, playlist_id: str, uris: list[str]) -> None:
        for start in range(0, len(uris), ADD_ITEMS_BATCH):
            self._request("POST", f"/playlists/{playlist_id}/items", json={"uris": uris[start:start + ADD_ITEMS_BATCH]})

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
    return Track(item["uri"], item["name"], tuple(a["name"] for a in item["artists"]), item["duration_ms"])
