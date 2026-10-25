"""
The taste profile: one paragraph about a user for the LLM prompt, plus a pool of songs they know.

Built by plain string formatting from the onboarding answers plus what the user's service knows
(on Spotify: top artists lately and all-time, top tracks, recently played, Liked Songs, followed
artists and the user's own playlists). The summary is cached for a day; the same reads
rebuild the user's rows of the shared music library (Phase 19), which the pool is read from.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from src import features, library, music, users
from src.music import Listening, Track

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

# The pool the LLM may pick known songs from
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


@dataclass(frozen=True)
class Profile:
    """A taste as builds see it. For a friend (Phase 21) it's all a build gets of them: no prompts, reviews,
    ratings, lessons or login, only what "show/use my taste" covers."""
    summary: str
    pool: list[Track]  # songs the user knows, most telling first
    labels: tuple[str, ...] = ()  # per pool song, shown after it (a Blend says whose it is); may be empty
    name: str = ""                       # whose taste, for a friend's profile
    familiar_percent: Optional[int] = None  # their onboarding answer, as a share of known songs
    avoid: tuple[str, ...] = ()          # their onboarding "avoid", as phrases
    no_explicit: bool = False


def build_summary(onboarding: dict, listening: Listening) -> str:
    """Deterministic, at most MAX_CHARS. The least telling lists lose items first until it fits."""
    lines = []
    for key, label in QUESTIONS:
        answer = " ".join(str(onboarding.get(key) or "").split())
        if answer:
            lines.append(f"{label}: {answer}.")
    if onboarding.get("instrumental") == "no":
        lines.append("No instrumental tracks.")
    elif onboarding.get("instrumental") == "sometimes":
        lines.append("Instrumental tracks are fine now and then.")
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


def stored_summary(user) -> str:
    """A user's taste from what's stored, never from their service: the cached summary, else one built from their
    onboarding answers alone. Used for friends (Phases 20-21), whose logins Moodify never uses."""
    if user["taste_summary"]:
        return user["taste_summary"]
    return build_summary(json.loads(user["onboarding_json"] or "{}"), Listening())


def condense(summary: str, limit: int) -> str:
    """The summary cut to `limit` characters at the end of a sentence; the most telling parts come first."""
    if len(summary) <= limit:
        return summary
    cut = summary[:limit]
    end = cut.rfind(". ")
    return cut[: end + 1] if end > limit // 3 else cut[: limit - 1] + "…"


def summary_for(user) -> str:
    return profile_for(user).summary


def profile_for(user) -> Profile:
    """The cached profile, rebuilt when it's missing or older than STALE_AFTER.

    Each source is best effort: one that fails is left out, and the profile is then not
    cached (nor the library replaced), so the next build tries again. A dead refresh token
    (ReauthRequired) is passed up so the caller can send the user back to /login.
    """
    if user["taste_summary"] and not _stale(user["taste_updated_at"]):
        return Profile(user["taste_summary"], library.pool(user["user_id"], POOL_SIZE))
    onboarding = json.loads(user["onboarding_json"] or "{}")
    listening = read_listening(user)
    profile = Profile(build_summary(onboarding, listening), known_pool(listening))
    if listening.complete:
        library.replace(user["user_id"], library_sources(listening))
        features.judge_soon()  # new library tracks get their taste dimensions in the background (Phase 22)
        users.set_taste_summary(user["user_id"], profile.summary, datetime.now(timezone.utc).strftime(TIMESTAMP))
    return profile


def library_sources(listening: Listening) -> dict[str, list[Track]]:
    """The reads, by library source (src/library.py)."""
    return {"top": listening.top_tracks, "recent": listening.recent_tracks, "liked": listening.liked,
            "own_playlist": listening.playlist_tracks}


def read_listening(user) -> Listening:
    """What the user's service gives for their taste; each read is best effort (src/music)."""
    with music.for_user(user) as service:
        return service.listening()


def _stale(updated_at) -> bool:
    if not updated_at:
        return True
    then = datetime.strptime(updated_at, TIMESTAMP).replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - then > STALE_AFTER
