"""
The taste profile: one paragraph about a user for the LLM prompt, plus a pool of songs they know.

Built by plain string formatting from the onboarding answers plus what Spotify knows:
top artists (lately and all-time), top tracks, recently played, Liked Songs, followed
artists and the user's own playlists. Summary and pool are cached together for a day.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, TypeVar

from src import users
from src.core.exceptions import ReauthRequired, SpotifyError
from src.core.logger import MoodifyLogger
from src.spotify.client import SpotifyClient, Track

MAX_CHARS = 3000
STALE_AFTER = timedelta(hours=24)  # recent plays go stale fast
TIMESTAMP = "%Y-%m-%d %H:%M:%S"  # same format as SQLite's CURRENT_TIMESTAMP, always UTC

# How many of each go into the summary
TOP_ARTISTS = 10
TOP_TRACKS = 10
RECENT_TRACKS = 10
LIKED_TRACKS = 15
FOLLOWED = 15
PLAYLIST_NAMES = 10

# Reads from Spotify per rebuild, and the pool the LLM may pick known songs from
LIKED_TO_READ = 200
OWN_PLAYLISTS_TO_READ = 5
POOL_SIZE = 150

# Onboarding answer -> share of each playlist taken from songs the user knows
FAMILIAR_PERCENT = {"new": 10, "mix": 30, "known": 60}
DEFAULT_FAMILIAR = "mix"

# (onboarding key, label in the summary); free-text answers are rendered as "label: answer."
QUESTIONS = (
    ("genres", "Favourite genres"),
    ("artists", "Artists they love"),
    ("energy", "Preferred energy"),
    ("languages", "Languages they listen in"),
    ("avoid", "Avoid"),
)


@dataclass
class Listening:
    """Everything read from Spotify for one user. Empty lists are normal (new accounts, missing scopes)."""
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
class Profile:
    summary: str
    pool: list[Track]  # songs the user knows, most telling first


def build_summary(onboarding: dict, listening: Listening) -> str:
    """Deterministic, at most MAX_CHARS. The least telling lists lose items first until it fits."""
    lines = []
    for key, label in QUESTIONS:
        answer = " ".join(str(onboarding.get(key) or "").split())
        if answer:
            lines.append(f"{label}: {answer}.")
    if onboarding.get("instrumental") == "no":
        lines.append("No instrumental tracks.")
    elif onboarding.get("instrumental"):
        lines.append("Instrumental tracks are fine.")

    lately = _names(listening.top_artists, TOP_ARTISTS)
    # Most telling first; trimming starts from the last list
    sections = [
        ("Most played artists lately", lately, ", "),
        ("All-time favourite artists", [a for a in _names(listening.all_time_artists, TOP_ARTISTS) if a not in lately], ", "),
        ("On repeat lately", _songs(listening.top_tracks, TOP_TRACKS), "; "),
        ("Recently played", _songs(listening.recent_tracks, RECENT_TRACKS), "; "),
        ("Liked songs include", _songs(listening.liked, LIKED_TRACKS), "; "),
        ("Follows", _names(listening.followed, FOLLOWED), ", "),
        ("Their own playlists", _names(listening.playlist_names, PLAYLIST_NAMES), ", "),
    ]
    while True:
        text = _render(lines, sections)
        if len(text) <= MAX_CHARS:
            return text
        last_with_items = next((items for _, items, _ in reversed(sections) if items), None)
        if last_with_items is None:
            return text[: MAX_CHARS - 1] + "…"
        last_with_items.pop()


def _names(names: list[str], limit: int) -> list[str]:
    unique = list(dict.fromkeys(n.strip() for n in names if n and n.strip()))
    return unique[:limit]


def _songs(tracks: list[Track], limit: int) -> list[str]:
    return _names([f"{t.name} by {', '.join(t.artists)}" for t in tracks if t.name], limit)


def _render(lines: list[str], sections: list[tuple[str, list[str], str]]) -> str:
    parts = list(lines) + [f"{label}: {sep.join(items)}." for label, items, sep in sections if items]
    return " ".join(parts) if parts else "No taste information yet."


def known_pool(listening: Listening) -> list[Track]:
    """Songs the user already knows, deduped, most telling first, at most POOL_SIZE."""
    pool: dict[str, Track] = {}
    for track in (*listening.top_tracks, *listening.recent_tracks, *listening.liked, *listening.playlist_tracks):
        pool.setdefault(track.uri, track)
    return list(pool.values())[:POOL_SIZE]


def familiar_percent(onboarding: dict) -> int:
    return FAMILIAR_PERCENT.get(onboarding.get("familiar"), FAMILIAR_PERCENT[DEFAULT_FAMILIAR])


def summary_for(user) -> str:
    return profile_for(user).summary


def profile_for(user) -> Profile:
    """The cached profile, rebuilt when it's missing or older than STALE_AFTER.

    Each Spotify source is best effort: one that fails is left out, and the profile is then not
    cached, so the next build tries again. A dead refresh token (ReauthRequired) is passed up so
    the caller can send the user back to /login.
    """
    if user["taste_summary"] and not _stale(user["taste_updated_at"]):
        return Profile(user["taste_summary"], _load_pool(user["taste_pool_json"]))
    onboarding = json.loads(user["onboarding_json"] or "{}")
    listening = read_listening(user)
    profile = Profile(build_summary(onboarding, listening), known_pool(listening))
    if listening.complete:
        users.set_taste_summary(
            user["spotify_id"], profile.summary, datetime.now(timezone.utc).strftime(TIMESTAMP), _dump_pool(profile.pool)
        )
    return profile


T = TypeVar("T")


def read_listening(user) -> Listening:
    """About 15 Spotify reads: top items (3), history, Liked Songs (4 pages), follows, playlists (1 + 5)."""
    listening = Listening()
    log = MoodifyLogger.get_logger("taste")

    def best_effort(name: str, read: Callable[[], T], empty: T) -> T:
        try:
            return read()
        except ReauthRequired:
            raise
        except SpotifyError as exc:
            log.warning("%s unavailable for %s: %s", name, user["spotify_id"], exc)
            listening.complete = False
            return empty

    with SpotifyClient.for_user(user) as sp:
        listening.top_artists = best_effort("top artists", lambda: sp.top_artists(TOP_ARTISTS, "short_term"), [])
        listening.all_time_artists = best_effort("all-time artists", lambda: sp.top_artists(TOP_ARTISTS, "long_term"), [])
        listening.top_tracks = best_effort("top tracks", lambda: sp.top_tracks(TOP_TRACKS), [])
        listening.recent_tracks = best_effort("recently played", sp.recently_played, [])
        listening.liked = best_effort("liked songs", lambda: sp.saved_tracks(LIKED_TO_READ), [])
        listening.followed = best_effort("followed artists", sp.followed_artists, [])
        # Moodify's own playlists would only feed its suggestions back to itself
        own = [p for p in best_effort("playlists", sp.my_playlists, []) if not p.name.startswith("Moodify:")]
        listening.playlist_names = [p.name for p in own]
        for playlist in [p for p in own if p.readable][:OWN_PLAYLISTS_TO_READ]:
            listening.playlist_tracks += best_effort(f"playlist {playlist.id}", lambda: sp.playlist_items(playlist.id), [])
    return listening


def _dump_pool(pool: list[Track]) -> str:
    return json.dumps([[t.uri, t.name, list(t.artists), t.duration_ms] for t in pool])


def _load_pool(raw) -> list[Track]:
    return [Track(uri, name, tuple(artists), ms) for uri, name, artists, ms in json.loads(raw or "[]")]


def _stale(updated_at) -> bool:
    if not updated_at:
        return True
    then = datetime.strptime(updated_at, TIMESTAMP).replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - then > STALE_AFTER
