"""
Phase 26: what Moodify needs from a music service, and the client that acts for a user.

Build code asks for_user(user) for the user's client and never imports one; pages ask info(service) for the
service's name and words. A track's URI starts with its service ("spotify:track:…"), so a track knows where it
links to, whoever's library it came from.
"""
from dataclasses import dataclass, field
from typing import Callable, Protocol, TypeVar

from src import links
from src.core.exceptions import ReauthRequired, ServiceError
from src.core.logger import MoodifyLogger


@dataclass(frozen=True)
class Track:
    uri: str
    name: str
    artists: tuple[str, ...]
    duration_ms: int
    explicit: bool = False  # the service's flag; "no explicit lyrics" is enforced with it (Phase 11)

    @property
    def service(self) -> str:
        return self.uri.split(":", 1)[0]

    @property
    def url(self) -> str:
        """The track's web page on its service: every track shown links back to where it came from."""
        return links.track_url(self.uri)

    @property
    def key(self) -> str:
        """The same song on two services has the same key: title and first artist, normalized as matching does."""
        from src.match import normalize_artist, normalize_title  # match imports Track from here
        return f"{normalize_title(self.name)}|{normalize_artist(self.artists[0] if self.artists else '')}"


@dataclass(frozen=True)
class Playlist:
    id: str
    url: str


@dataclass(frozen=True)
class UserPlaylist:
    """One of the user's own playlists. Only `readable` ones have items Moodify can fetch."""
    id: str
    name: str
    readable: bool


@dataclass
class Listening:
    """Everything read from a user's service for their taste. Empty lists are normal: new accounts, missing
    access, or a service that has no such list."""
    top_artists: list[str] = field(default_factory=list)       # about the last 4 weeks
    all_time_artists: list[str] = field(default_factory=list)  # years
    top_tracks: list[Track] = field(default_factory=list)
    recent_tracks: list[Track] = field(default_factory=list)
    liked: list[Track] = field(default_factory=list)
    followed: list[str] = field(default_factory=list)
    playlist_names: list[str] = field(default_factory=list)
    playlist_tracks: list[Track] = field(default_factory=list)
    complete: bool = True  # False when a source failed: such a profile isn't cached


@dataclass(frozen=True)
class Info:
    """What pages and builds need to know about a service."""
    service: str
    name: str               # as the service writes it
    likes: str              # where a like goes on the service
    can_queue: bool         # Moodify can put songs in the user's queue there
    pool_explicit: bool     # songs read for the taste carry the service's explicit flag
    max_searches: int       # per build, cache hits not counted
    name_chars: int         # longest playlist name Moodify gives it
    description_chars: int  # the service's limit for a playlist description
    access_url: str         # where the user can take back Moodify's access


INFO = {
    # Spotify's quota is per developer account, so searches per build are capped
    "spotify": Info("spotify", "Spotify", "Liked Songs", can_queue=True, pool_explicit=True, max_searches=150,
                    name_chars=100, description_chars=300, access_url="https://www.spotify.com/account/apps/"),
}


def info(service: str) -> Info:
    return INFO[service]


class MusicService(Protocol):
    """One user's calls to their service. Used as a context manager, so connections get closed.
    Calls raise ServiceError; ReauthRequired when the stored login is dead, QuotaExceeded when the service
    wants Moodify to stop for now."""

    def __enter__(self) -> "MusicService": ...

    def __exit__(self, *exc) -> None: ...

    def listening(self) -> Listening:
        """The reads the taste profile and library are built from, each best effort (see best_effort)."""

    def search_track(self, title: str, artist: str) -> list[Track]: ...

    def create_playlist(self, name: str, description: str = "", public: bool = False) -> Playlist: ...

    def add_items(self, playlist_id: str, uris: list[str]) -> None: ...

    def is_liked(self, uri: str) -> bool: ...

    def like(self, uri: str) -> None: ...

    def unlike(self, uri: str) -> None: ...

    def queue(self, uri: str) -> None:
        """Only where info(service).can_queue."""


def for_user(user) -> MusicService:
    """The client that acts for this user on their service."""
    if user["service"] == "spotify":
        from src.spotify.client import SpotifyClient  # it imports Track from here
        return SpotifyClient.for_user(user)
    raise ValueError(f"no client for {user['service']}")


def missing_scopes(user) -> set[str]:
    """Access Moodify asks the user's service for that their last login didn't grant (Phase 10)."""
    if user["service"] == "spotify":
        from src.spotify import auth
        return auth.missing_scopes(user["granted_scopes"])
    return set()


T = TypeVar("T")
log = MoodifyLogger.get_logger("taste")


def best_effort(listening: Listening, user_id: str, name: str, read: Callable[[], T], empty: T) -> T:
    """One read for the taste. A failure leaves it out and marks the listening incomplete, so the profile isn't
    cached and the next build tries again. A dead login (ReauthRequired) goes up so the user can log in again."""
    try:
        return read()
    except ReauthRequired:
        raise
    except ServiceError as exc:
        log.warning("%s unavailable for %s: %s", name, user_id, exc)
        listening.complete = False
        return empty

