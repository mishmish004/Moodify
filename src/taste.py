"""
The taste summary: one paragraph about a user that goes into the LLM prompt.

Built by plain string formatting from the onboarding answers plus the user's
top artists and tracks on Spotify. Cached in the users table for 7 days.
"""
import json
from datetime import datetime, timedelta, timezone

from src import users
from src.core.exceptions import ReauthRequired, SpotifyError
from src.core.logger import MoodifyLogger
from src.spotify.client import SpotifyClient, Track

MAX_CHARS = 1500
STALE_AFTER = timedelta(days=7)
TOP_ARTISTS = 10
TOP_TRACKS = 10
TIMESTAMP = "%Y-%m-%d %H:%M:%S"  # same format as SQLite's CURRENT_TIMESTAMP, always UTC

# (onboarding key, label in the summary); free-text answers are rendered as "label: answer."
QUESTIONS = (
    ("genres", "Favourite genres"),
    ("artists", "Artists they love"),
    ("energy", "Preferred energy"),
    ("languages", "Languages they listen in"),
    ("avoid", "Avoid"),
)


def build_summary(onboarding: dict, top_artists: list[str], top_tracks: list[Track]) -> str:
    """Deterministic, at most MAX_CHARS. Listening data is dropped from the end until it fits."""
    lines = []
    for key, label in QUESTIONS:
        answer = " ".join(str(onboarding.get(key) or "").split())
        if answer:
            lines.append(f"{label}: {answer}.")
    if onboarding.get("instrumental") == "no":
        lines.append("No instrumental tracks.")
    elif onboarding.get("instrumental"):
        lines.append("Instrumental tracks are fine.")

    artists = [a for a in top_artists if a][:TOP_ARTISTS]
    tracks = [f"{t.name} by {', '.join(t.artists)}" for t in top_tracks if t.name][:TOP_TRACKS]
    while True:
        text = _render(lines, artists, tracks)
        if len(text) <= MAX_CHARS:
            return text
        if tracks:
            tracks.pop()
        elif artists:
            artists.pop()
        else:
            return text[: MAX_CHARS - 1] + "…"


def _render(lines: list[str], artists: list[str], tracks: list[str]) -> str:
    parts = list(lines)
    if artists:
        parts.append(f"Most played artists lately: {', '.join(artists)}.")
    if tracks:
        parts.append(f"On repeat lately: {'; '.join(tracks)}.")
    return " ".join(parts) if parts else "No taste information yet."


def summary_for(user) -> str:
    """The cached summary, rebuilt when it's missing or older than STALE_AFTER.

    Spotify data is best effort: if the call fails, the summary comes from the onboarding
    answers alone and is not cached, so the next visit tries Spotify again. A dead refresh
    token (ReauthRequired) is passed up so the route can send the user back to /login.
    """
    if user["taste_summary"] and not _stale(user["taste_updated_at"]):
        return user["taste_summary"]
    onboarding = json.loads(user["onboarding_json"] or "{}")
    try:
        with SpotifyClient.for_user(user) as sp:
            artists, tracks = sp.top_artists(TOP_ARTISTS), sp.top_tracks(TOP_TRACKS)
    except ReauthRequired:
        raise
    except SpotifyError as exc:
        MoodifyLogger.get_logger("taste").warning("top items unavailable for %s: %s", user["spotify_id"], exc)
        return build_summary(onboarding, [], [])
    summary = build_summary(onboarding, artists, tracks)
    users.set_taste_summary(user["spotify_id"], summary, datetime.now(timezone.utc).strftime(TIMESTAMP))
    return summary


def _stale(updated_at) -> bool:
    if not updated_at:
        return True
    then = datetime.strptime(updated_at, TIMESTAMP).replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - then > STALE_AFTER
