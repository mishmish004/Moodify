"""
mood -> a real playlist in the user's Spotify account.

suggest (LLM) -> search each song on Spotify -> pick the matching track
-> dedupe, stop at MAX_TRACKS -> create playlist -> add items -> save the row and its tracks.
Songs the LLM takes from the user's known-songs pool need no search (Phase 10).
If fewer than MIN_TRACKS matched, the LLM is asked once more for extra songs.
run() is the background entry point: it records how the build ended on its job.
"""
import json

from src import jobs, playlists, taste, users
from src.core.exceptions import ReauthRequired, SpotifyQuotaExceeded
from src.core.logger import MoodifyLogger
from src.jobs import NO_TRACE, Trace
from src.match import pick
from src.spotify.client import SpotifyClient, Track
from src.suggest import Song, clean_mood, suggest_songs

MAX_TRACKS = 25
MIN_TRACKS = 8
EXTRA_SONGS = 15
NAME_CHARS = 60  # how much of the mood goes into the playlist name


class BuildFailed(Exception):
    """Nothing to make. `reason` is one of the jobs.NO_* codes. No playlist was created."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def run(job_id: int, spotify_id: str, mood: str) -> None:
    """Build in the background and record the outcome on the job. Never raises: a thread has nobody to raise to."""
    log = MoodifyLogger.get_logger("playlist")
    try:
        user = users.get(spotify_id)
        if user is None:  # disconnected while queued; the job row went with the user
            return
        playlist_id = build(user, mood, Trace(job_id))
    except BuildFailed as exc:
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


def build(user, mood: str, trace: Trace = NO_TRACE) -> int:
    """Returns the new playlists row id. Raises BuildFailed when there is nothing to make;
    SpotifyQuotaExceeded and ReauthRequired propagate. In every failure no playlist is created."""
    mood = clean_mood(mood)
    trace.step("Reading your taste")
    profile = taste.profile_for(user)
    ask = {"known": profile.pool, "familiar_percent": taste.familiar_percent(json.loads(user["onboarding_json"] or "{}"))}
    trace.step("Asking for song ideas")
    songs = suggest_songs(mood, profile.summary, trace=trace, **ask)
    if not songs:
        raise BuildFailed(jobs.NO_SUGGESTIONS)
    trace.step("Finding the songs on Spotify")
    with SpotifyClient.for_user(user) as sp:
        tracks, searched = _resolve(sp, songs, trace, profile.pool)
        suggested = len(songs)
        if len(tracks) < MIN_TRACKS:
            trace.step("Asking for a few more songs")
            extra = suggest_songs(mood, profile.summary, n=EXTRA_SONGS, exclude=songs, trace=trace, **ask)
            more, more_searched = _resolve(
                sp, extra, trace, profile.pool, seen={t.uri for t in tracks}, room=MAX_TRACKS - len(tracks)
            )
            tracks, searched, suggested = tracks + more, searched + more_searched, suggested + len(extra)
        if not tracks:
            raise BuildFailed(jobs.NO_MATCHES)
        trace.step("Saving the playlist to your Spotify")
        playlist = sp.create_playlist(f"Moodify: {mood[:NAME_CHARS]}", f"Made by Moodify for: {mood}")
        sp.add_items(playlist.id, [t.uri for t in tracks])
    row_id = playlists.insert(user["spotify_id"], mood, playlist.url, tracks)
    pool_uris = {t.uri for t in profile.pool}
    known = sum(t.uri in pool_uris for t in tracks)
    MoodifyLogger.get_logger("playlist").info(
        "%s: %d tracks (%d known) from %d searches (%d suggestions) for %r",
        user["spotify_id"], len(tracks), known, searched, suggested, mood,
    )
    trace.event("built", playlist_id=row_id, tracks=len(tracks), known=known, searched=searched, suggested=suggested)
    return row_id


def _resolve(
    sp: SpotifyClient, songs: list[Song], trace: Trace, pool: list[Track] = (),
    seen: set[str] | None = None, room: int = MAX_TRACKS,
) -> tuple[list[Track], int]:
    """Find each suggestion's track: straight from the known-songs pool when the LLM picked one from it
    (and it really is that song), else by search. Stops once `room` tracks are found, so a good first
    batch costs at most MAX_TRACKS searches. Returns (tracks, searches made)."""
    seen = set() if seen is None else seen
    tracks: list[Track] = []
    searched = 0
    for song in songs:
        if len(tracks) >= room:
            break
        from_pool = pool[song.known - 1] if song.known and 1 <= song.known <= len(pool) else None
        # A wrong number from the LLM must not slip in a different song: the pool entry has to match too
        if from_pool and pick(song, [from_pool]):
            candidates, uri = [from_pool], from_pool.uri
            trace.event("known", title=song.title, artist=song.artist, number=song.known)
        else:
            candidates = sp.search_track(song.title, song.artist)
            searched += 1
            uri = pick(song, candidates)
            trace.event(
                "search", step=f"Finding the songs on Spotify ({searched}/{len(songs)})",
                title=song.title, artist=song.artist, candidates=len(candidates), matched=uri is not None,
            )
        if uri and uri not in seen:
            seen.add(uri)
            tracks.append(next(t for t in candidates if t.uri == uri))
    return tracks, searched
