"""
Phase 21: hear it like a friend. "What would Sam put on for this?": a playlist built from a friend's taste,
saved in your account, without touching theirs.

Needs the friend's "Let friends hear music like me" (Phase 18), checked when the build starts. The build
gets the friend as a taste.Profile and nothing else: their stored summary, their known songs from the
library, their onboarding familiarity and avoid rules. Their prompts, reviews, ratings, lessons and
support-note playlists can't reach it, and neither can your own taste or memory. Only your Spotify login is
used: to search, and to save the playlist in your account.
"""
import json
from typing import Optional

from src import blend, jobs, library, playlists, sharing, taste, users
from src.brief import Brief
from src.core.exceptions import ReauthRequired, SpotifyQuotaExceeded
from src.core.logger import MoodifyLogger
from src.db import get_conn
from src.jobs import NO_TRACE, Trace

log = MoodifyLogger.get_logger("listen_as")


def friend_profile(friend) -> taste.Profile:
    """A friend's taste from stored data only. Call it only after sharing.allowed(..., "listen_as")."""
    onboarding = json.loads(friend["onboarding_json"] or "{}")
    avoid = blend.avoid_phrases(onboarding)
    return taste.Profile(
        summary=taste.stored_summary(friend), pool=library.pool(friend["user_id"]), name=sharing.first_name(friend),
        familiar_percent=taste.familiar_percent(onboarding),
        avoid=tuple(a for a in avoid if not blend.EXPLICIT_WORDS.search(a)),
        no_explicit=any(blend.EXPLICIT_WORDS.search(a) for a in avoid),
    )


def _their_rules(profile: taste.Profile):
    def tidy(brief: Brief) -> Brief:
        have = {a.casefold() for a in brief.hard_rules.avoid}
        avoid = list(brief.hard_rules.avoid) + [a for a in profile.avoid if a.casefold() not in have]
        rules = brief.hard_rules.model_copy(update={"avoid": avoid,
                                                    "no_explicit": brief.hard_rules.no_explicit or profile.no_explicit})
        return brief.model_copy(update={"hard_rules": rules})
    return tidy


def build_as(user, mood: str, profile: taste.Profile, trace: Trace = NO_TRACE) -> int:
    """A playlist in `user`'s account for `mood`, the way the person behind `profile` would hear it."""
    if not isinstance(profile, taste.Profile):
        raise TypeError("build_as takes a friend's taste.Profile, nothing else")
    from src import playlist
    name = profile.name or "a friend"
    recipe = playlist.Recipe(
        profile=profile, familiar_percent=profile.familiar_percent or taste.FAMILIAR_PERCENT[taste.DEFAULT_FAMILIAR],
        name=lambda brief: f"Moodify: {brief.title} (as {name} hears it)", note=f"The way {name} would hear it.",
        tidy=_their_rules(profile),
    )
    return playlist.make(user, mood, recipe, trace).playlist_id


def run(job_id: int, user_id: str, mood: str, friend_id: str) -> None:
    """Background entry, like playlist.run. Never raises. Consent is checked here, once, before anything of the
    friend's is read."""
    from src import playlist
    try:
        user = users.get(user_id)
        if user is None:
            return
        friend = users.get(friend_id)
        if friend is None or not sharing.allowed(friend_id, user_id, "listen_as"):
            raise playlist.BuildFailed(blend.NOT_SHARED)
        trace = Trace(job_id)
        trace.step(f"Reading {sharing.first_name(friend)}'s taste")
        playlist_id = build_as(user, mood, friend_profile(friend), trace)
        with get_conn() as conn:
            conn.execute("UPDATE playlists SET heard_as = ? WHERE id = ?", (friend_id, playlist_id))
        sharing.log(friend_id, user_id, "listen_as", playlist_id)
    except playlist.BuildFailed as exc:
        jobs.fail(job_id, exc.reason)
    except SpotifyQuotaExceeded as exc:
        log.warning("quota/rate limit hit during job %d: %s", job_id, exc)
        jobs.fail(job_id, jobs.QUOTA)
    except ReauthRequired:
        jobs.fail(job_id, jobs.REAUTH)
    except Exception:
        log.exception("job %d failed", job_id)
        jobs.fail(job_id, jobs.ERROR)
    else:
        jobs.finish(job_id, playlist_id)


def heard_as(playlist_row) -> Optional[str]:
    """Whose taste a playlist was made with, as a first name, while that person is still on Moodify."""
    if not playlist_row["heard_as"]:
        return None
    friend = users.get(playlist_row["heard_as"])
    return sharing.first_name(friend) if friend else None
