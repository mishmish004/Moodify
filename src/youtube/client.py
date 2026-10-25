"""
Phase 27: every YouTube Data API v3 call Moodify makes, for one user, plus the YouTube Music search.

Playlists Moodify creates on YouTube show up in YouTube Music's library. Songs are YouTube videos (a YouTube
Music song is an "Artist - Topic" upload), so a track URI is youtube:track:<video id>. What the API can't give:
listening history and top tracks. The taste is built from liked music videos, the user's playlists and their
subscriptions instead.

Every call spends quota from the project's daily allowance (src/youtube/quota.py): reads 1 unit, a playlist,
an added song or a rating 50. Songs go into a playlist one call each.
"""
import re
from typing import Optional

import httpx

from src import users
from src.core.exceptions import YouTubeError, YouTubeQuotaExceeded
from src.music import Listening, Playlist, Track, UserPlaylist, best_effort
from src.youtube import auth, quota, search

API_URL = "https://www.googleapis.com/youtube/v3"
PAGE = 50              # the most items one list call returns
LIKED_PAGES = 4        # liked videos read per taste rebuild, 50 a page
OWN_PLAYLISTS_TO_READ = 5
MUSIC = "10"           # YouTube's video category for music
READ, WRITE = 1, 50    # quota units
SEARCH_FAILURES = 3    # in a row, and the build stops searching
TITLE_CHARS = 150      # YouTube's limits for a playlist's title and description
DESCRIPTION_CHARS = 5000
RETRIED = {409, 500, 503}  # Google sometimes answers these to quick playlist writes; once more usually works

# "(Official Video)", "[Official Music Video]", "(Lyrics)", "(HD)": tails a video title has and a song's doesn't
_TAIL = re.compile(r"\s*[\(\[][^\)\]]*\b(official|video|audio|lyrics?|visuali[sz]er|hd|4k|mv)\b[^\)\]]*[\)\]]\s*$", re.I)
_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


class YouTubeClient:
    """One user's YouTube calls. Use as a context manager so the connection pool gets closed.

    The first call trades the stored refresh token for an access token; a 401 refreshes once and retries.
    YouTubeReauthRequired from the refresh means the user has to sign in again.
    """

    def __init__(self, user_id: str, refresh_token: str, access_token: Optional[str] = None):
        self.user_id = user_id
        self.refresh_token = refresh_token
        self.access_token = access_token
        self.http = httpx.Client(base_url=API_URL, timeout=auth.TIMEOUT)
        self.search_failures = 0

    @classmethod
    def for_user(cls, user) -> "YouTubeClient":
        return cls(user["user_id"], user["refresh_token"])

    def __enter__(self) -> "YouTubeClient":
        return self

    def __exit__(self, *exc) -> None:
        self.http.close()

    # --- taste -------------------------------------------------------------------------------------------

    def listening(self) -> Listening:
        """About 15 units: liked videos (4 pages), playlists (1 + 2 per playlist read), subscriptions (1)."""
        listening = Listening()

        def read(name, call, empty):
            return best_effort(listening, self.user_id, name, call, empty)

        listening.liked = read("liked videos", self.liked_videos, [])
        listening.followed = read("subscriptions", self.subscriptions, [])
        own = [p for p in read("playlists", self.my_playlists, []) if not p.name.startswith("Moodify:")]
        listening.playlist_names = [p.name for p in own]
        for playlist in own[:OWN_PLAYLISTS_TO_READ]:
            listening.playlist_tracks += read(f"playlist {playlist.id}", lambda: self.playlist_items(playlist.id), [])
        return listening

    def liked_videos(self) -> list[Track]:
        """Liked music videos, newest first. A like in YouTube Music is a like of the song's video."""
        found: list[Track] = []
        token = None
        for _ in range(LIKED_PAGES):
            params = {"part": "snippet,contentDetails", "myRating": "like", "maxResults": PAGE}
            data = self._request("GET", "/videos", READ, params={**params, **({"pageToken": token} if token else {})})
            found += [t for t in map(_video_track, data.get("items", [])) if t]
            token = data.get("nextPageToken")
            if not token:
                break
        return found

    def subscriptions(self) -> list[str]:
        """Channels the user subscribes to, "Artist - Topic" channels by their artist's name."""
        data = self._request("GET", "/subscriptions", READ, params={"part": "snippet", "mine": "true", "maxResults": PAGE})
        return [_artist_name(item["snippet"]["title"]) for item in data.get("items", []) if item.get("snippet")]

    def my_playlists(self) -> list[UserPlaylist]:
        data = self._request("GET", "/playlists", READ, params={"part": "snippet", "mine": "true", "maxResults": PAGE})
        return [UserPlaylist(p["id"], p["snippet"]["title"], True) for p in data.get("items", [])]

    def playlist_items(self, playlist_id: str) -> list[Track]:
        """The first PAGE songs of a playlist. Items don't carry lengths or categories, so one videos call adds them."""
        data = self._request("GET", "/playlistItems", READ,
                             params={"part": "contentDetails", "playlistId": playlist_id, "maxResults": PAGE})
        ids = [item["contentDetails"]["videoId"] for item in data.get("items", []) if item.get("contentDetails")]
        if not ids:
            return []
        videos = self._request("GET", "/videos", READ, params={"part": "snippet,contentDetails", "id": ",".join(ids)})
        return [t for t in map(_video_track, videos.get("items", [])) if t]

    # --- building ----------------------------------------------------------------------------------------

    def search_track(self, title: str, artist: str) -> list[Track]:
        """YouTube Music songs for a suggestion (src/youtube/search.py), no quota. A failure gives no candidates;
        SEARCH_FAILURES in a row raise, so a build doesn't try a hundred more searches against a broken search."""
        try:
            tracks = search.search(title, artist)
        except YouTubeError:
            self.search_failures += 1
            if self.search_failures >= SEARCH_FAILURES:
                raise
            return []
        self.search_failures = 0
        return tracks

    def check_room(self, songs: int) -> None:
        """Raise YouTubeQuotaExceeded now if today's quota can't pay for a playlist of `songs` songs."""
        if quota.left() < WRITE * (1 + songs):
            raise YouTubeQuotaExceeded(0, f"{quota.left()} units left, {WRITE * (1 + songs)} needed", "quotaExceeded")

    def create_playlist(self, name: str, description: str = "", public: bool = False) -> Playlist:
        body = {"snippet": {"title": _plain(name)[:TITLE_CHARS], "description": _plain(description)[:DESCRIPTION_CHARS]},
                "status": {"privacyStatus": "public" if public else "private"}}
        data = self._request("POST", "/playlists", WRITE, params={"part": "snippet,status"}, json=body)
        return Playlist(data["id"], f"https://music.youtube.com/playlist?list={data['id']}")

    def add_items(self, playlist_id: str, uris: list[str]) -> None:
        for uri in uris:
            body = {"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": _video_id(uri)}}}
            self._request("POST", "/playlistItems", WRITE, params={"part": "snippet"}, json=body)

    # --- likes (MusicService) ------------------------------------------------------------------------------

    def is_liked(self, uri: str) -> bool:
        data = self._request("GET", "/videos/getRating", READ, params={"id": _video_id(uri)})
        return any(item.get("rating") == "like" for item in data.get("items", []))

    def like(self, uri: str) -> None:
        self._request("POST", "/videos/rate", WRITE, params={"id": _video_id(uri), "rating": "like"})

    def unlike(self, uri: str) -> None:
        self._request("POST", "/videos/rate", WRITE, params={"id": _video_id(uri), "rating": "none"})

    def queue(self, uri: str) -> None:
        raise YouTubeError(404, "YouTube has no queue Moodify can fill")

    # --- plumbing ------------------------------------------------------------------------------------------

    def _request(self, method: str, path: str, cost: int, **kwargs) -> dict:
        if self.access_token is None:
            self._refresh_access_token()
        resp = self._send(method, path, cost, **kwargs)
        if resp.status_code == 401 and _reason(resp) != "youtubeSignupRequired":
            # Access tokens last an hour: refresh once, and if that doesn't help, give up
            self._refresh_access_token()
            resp = self._send(method, path, cost, **kwargs)
        if resp.status_code in RETRIED and method == "POST":
            resp = self._send(method, path, cost, **kwargs)
        if not resp.is_success:
            reason = _reason(resp)
            if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
                raise YouTubeQuotaExceeded(resp.status_code, resp.text, reason)
            raise YouTubeError(resp.status_code, resp.text, reason)
        return resp.json() if resp.content else {}

    def _send(self, method: str, path: str, cost: int, **kwargs) -> httpx.Response:
        quota.spend(cost)  # Google charges failed calls too
        try:
            return self.http.request(method, path, headers={"Authorization": f"Bearer {self.access_token}"}, **kwargs)
        except httpx.HTTPError as exc:
            raise YouTubeError(0, repr(exc)) from exc

    def _refresh_access_token(self) -> None:
        tokens = auth.refresh(self.refresh_token)
        self.access_token = tokens.access_token
        if tokens.refresh_token != self.refresh_token:
            self.refresh_token = tokens.refresh_token
            users.set_refresh_token(self.user_id, tokens.refresh_token)


def _reason(resp: httpx.Response) -> str:
    """Google's error reason ("quotaExceeded", "youtubeSignupRequired", ...), or ""."""
    try:
        errors = resp.json().get("error", {}).get("errors") or [{}]
    except ValueError:
        return ""
    return errors[0].get("reason", "")


def _video_id(uri: str) -> str:
    return uri.rsplit(":", 1)[-1]


def _plain(text: str) -> str:
    """YouTube refuses playlist titles and descriptions with angle brackets."""
    return text.replace("<", "‹").replace(">", "›")


def _artist_name(channel: str) -> str:
    """ "M83 - Topic" and "M83VEVO" are M83's channels."""
    name = channel.removesuffix(" - Topic")
    return name[:-4] if name.endswith("VEVO") and len(name) > 4 else name


def _video_track(video: dict) -> Optional[Track]:
    """A music video as a track: title and artist from a Topic channel, else from "Artist - Title", else the
    channel. Non-music videos (another category) and live streams give None."""
    snippet, details = video.get("snippet") or {}, video.get("contentDetails") or {}
    if snippet.get("categoryId") != MUSIC or not video.get("id"):
        return None
    title, channel = snippet.get("title", ""), snippet.get("channelTitle", "")
    if channel.endswith(" - Topic"):
        artist, name = _artist_name(channel), title
    elif " - " in title:
        artist, name = (part.strip() for part in title.split(" - ", 1))
    else:
        artist, name = _artist_name(channel), title
    name = _TAIL.sub("", name).strip() or name
    length = _duration_ms(details.get("duration", ""))
    # A live stream has no length, and a song without one would throw off how long a section is (Phase 12)
    return Track(f"youtube:track:{video['id']}", name, (artist,), length) if length else None


def _duration_ms(iso: str) -> int:
    """ "PT4M4S" -> 244000. Anything else (a live stream's "P0D", nothing) -> 0."""
    found = _DURATION.match(iso or "")
    if not found:
        return 0
    days, hours, minutes, seconds = (int(part or 0) for part in found.groups())
    return (((days * 24 + hours) * 60 + minutes) * 60 + seconds) * 1000
