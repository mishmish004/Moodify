"""
mood -> a real playlist in the user's Spotify account.

read the prompt into a brief (LLM) -> work out minutes per section -> suggest songs per section (LLM)
-> find each on Spotify (known-songs pool, search cache, or search) -> fill each section to its length
-> create the playlist -> add items -> save the row and its tracks.
Sections still short after the first round get one extra suggestion call, for those sections only.
run() is the background entry point: it records how the build ended on its job.
Lessons from reviews reach both LLM calls (Phase 15). "Fix it with this review" is a build with `fix_of`:
the earlier brief and its review go in as a correction, and the new playlist points back at the old one.
"""
import json
from typing import Optional

from src import jobs, likes, memory, playlists, search_cache, taste, users
from src.arc import arrange, timeline
from src.brief import Brief, familiar_percent, read_prompt
from src.core.exceptions import ReauthRequired, SpotifyQuotaExceeded
from src.core.logger import MoodifyLogger
from src.fill import SectionFill, section_targets, songs_to_ask_for
from src.jobs import NO_TRACE, Trace
from src.match import normalize_artist, normalize_title, pick, primary_artist
from src.spotify.client import SpotifyClient, Track
from src.suggest import Song, clean_mood, suggest_songs

MAX_SEARCHES = 150       # per build, cache hits not counted: Spotify's quota is per developer account
DESCRIPTION_CHARS = 300  # Spotify's limit for a playlist description


class BuildFailed(Exception):
    """Nothing to make. `reason` is one of the jobs.NO_* codes. No playlist was created."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def run(job_id: int, spotify_id: str, mood: str, fix_of: Optional[int] = None) -> None:
    """Build in the background and record the outcome on the job. Never raises: a thread has nobody to raise to."""
    log = MoodifyLogger.get_logger("playlist")
    try:
        user = users.get(spotify_id)
        if user is None:  # disconnected while queued; the job row went with the user
            return
        playlist_id = build(user, mood, Trace(job_id), fix_of=fix_of)
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


def build(user, mood: str, trace: Trace = NO_TRACE, fix_of: Optional[int] = None) -> int:
    """Returns the new playlists row id. Raises BuildFailed when there is nothing to make;
    SpotifyQuotaExceeded and ReauthRequired propagate. In every failure no playlist is created.
    `fix_of`: the id of the user's playlist whose review this build corrects."""
    mood = clean_mood(mood)
    trace.step("Reading your taste")
    profile = taste.profile_for(user)
    lessons = memory.lessons_block(user["spotify_id"])
    parent = playlists.get(fix_of, user["spotify_id"]) if fix_of else None
    correction, feedback = fix_notes(parent, user["spotify_id"]) if parent else ("", "")
    trace.step("Reading your prompt")
    brief = read_prompt(mood, profile.summary, trace, spotify_id=user["spotify_id"], memory=lessons, correction=correction)
    targets = section_targets(brief)
    trace.event("brief", title=brief.title, sections=len(brief.sections), support_note=brief.support_note,
                minutes=[round(t, 1) for t in targets])
    onboarding_percent = taste.familiar_percent(json.loads(user["onboarding_json"] or "{}"))
    ask = {"known": profile.pool, "familiar_percent": familiar_percent(brief, onboarding_percent),
           "brief": brief, "spotify_id": user["spotify_id"], "memory": lessons, "feedback": feedback}

    trace.step("Asking for song ideas")
    counts = [songs_to_ask_for(t) for t in targets]
    songs = suggest_songs(mood, profile.summary, n=sum(counts), counts=counts, trace=trace, **ask)
    if not songs:
        raise BuildFailed(jobs.NO_SUGGESTIONS)
    fills = [SectionFill(t, brief.hard_rules) for t in targets]
    finder = _Finder(user["spotify_id"], brief, profile.pool, trace)
    trace.step("Finding the songs on Spotify")
    with SpotifyClient.for_user(user) as sp:
        finder.place(sp, songs, fills)
        short = [i for i, f in enumerate(fills) if not f.done]
        if short:
            trace.step("Asking for a few more songs")
            extra_counts = [songs_to_ask_for(f.missing) if i in short else 0 for i, f in enumerate(fills)]
            extra = suggest_songs(mood, profile.summary, n=sum(extra_counts), counts=extra_counts, exclude=songs,
                                  trace=trace, **ask)
            finder.place(sp, extra, fills)
            songs = songs + extra
        if not any(f.tracks for f in fills):
            raise BuildFailed(jobs.NO_MATCHES)
        ordered = arrange([f.tracks for f in fills], finder.energy, finder.roles, brief)
        tracks = [t for section in ordered for t in section]
        minutes = round(sum(f.total for f in fills))
        trace.step("Saving the playlist to your Spotify")
        description = _description(brief, [f.total for f in fills], minutes)
        name = f"Moodify: {brief.title}" + (f" (v{playlists.version(parent['id']) + 1})" if parent else "")
        playlist = sp.create_playlist(name, description)
        sp.add_items(playlist.id, [t.uri for t in tracks])
    sections = [i for i, section in enumerate(ordered) for _ in section]
    row_id = playlists.insert(
        user["spotify_id"], mood, playlist.url, tracks, brief.model_dump_json(), sections,
        [finder.energy.get(t.uri) for t in tracks], [finder.roles.get(t.uri) for t in tracks],
        parent_id=parent["id"] if parent else None,
    )
    pool_uris = {t.uri for t in profile.pool}
    known = sum(t.uri in pool_uris for t in tracks)
    MoodifyLogger.get_logger("playlist").info(
        "%s: %d tracks, %d min (%d known) from %d searches (%d suggestions) for %r",
        user["spotify_id"], len(tracks), minutes, known, finder.searched, len(songs), brief.title,
    )
    trace.event("built", playlist_id=row_id, tracks=len(tracks), minutes=minutes, known=known,
                searched=finder.searched, cache_hits=finder.cache_hits, suggested=len(songs),
                section_minutes=[round(f.total, 1) for f in fills], targets=[round(t, 1) for t in targets])
    return row_id


def fix_notes(parent, spotify_id: str) -> tuple[str, str]:
    """(correction for the brief call, feedback for the suggestion call) from a playlist's review, the
    lessons learned from it, and the songs liked in it."""
    lines = []
    if parent["brief_json"]:
        lines.append(f"Earlier brief: {parent['brief_json']}")
    lines.append(f"What the listener said after hearing it: {parent['review']}")
    learned = memory.from_playlist(spotify_id, parent["id"])
    if learned:
        lines.append("What Moodify took from that:\n" + memory.render(learned, limit=800))
    rows = playlists.track_rows(parent["id"])
    liked = likes.liked_uris(spotify_id, [r["uri"] for r in rows])
    kept = [f"{r['name']} by {json.loads(r['artists_json'])[0]}" for r in rows if r["uri"] in liked][:10]
    feedback = "\n".join(lines[1:])
    if kept:
        feedback += "\nSongs they liked in it (keep them if they still fit): " + "; ".join(kept)
    return "\n".join(lines), feedback


def _description(brief: Brief, section_minutes: list[float], minutes: int) -> str:
    """The timeline for a playlist with several sections ("0–14 Summer morning → 14–44 Workout → ..."),
    else the summary; always within Spotify's limit."""
    if len(brief.sections) > 1:
        text = f"{timeline(brief, section_minutes)}. Made by Moodify."
        if len(text) <= DESCRIPTION_CHARS:
            return text
    text = f"Made by Moodify: {brief.summary} ({minutes} min)"
    return text if len(text) <= DESCRIPTION_CHARS else text[: DESCRIPTION_CHARS - 1] + "…"


class _Finder:
    """Turns suggested songs into Spotify tracks and offers each to its section. Searches only while
    the song's section still needs tracks, never twice for the same song, and at most MAX_SEARCHES."""

    def __init__(self, spotify_id: str, brief: Brief, pool: list[Track], trace: Trace):
        self.spotify_id, self.brief, self.pool, self.trace = spotify_id, brief, pool, trace
        self.searched = 0
        self.cache_hits = 0
        self.tried: set[tuple[str, str]] = set()  # songs already looked at in this build
        self.used: set[str] = set()                # URIs already in the playlist
        self.energy: dict[str, int] = {}           # by URI, from the song that placed it
        self.roles: dict[str, Optional[str]] = {}

    def place(self, sp: SpotifyClient, songs: list[Song], fills: list[SectionFill]) -> None:
        for song in songs:
            fill = fills[min(max((song.section or 1) - 1, 0), len(fills) - 1)]
            key = (normalize_title(song.title), normalize_artist(song.artist))
            if fill.done or key in self.tried:
                continue
            if self.searched >= MAX_SEARCHES:
                self.trace.event("limit", searches=self.searched)
                return
            self.tried.add(key)
            track = self._find(sp, song, len(songs))
            if track is None or track.uri in self.used:
                continue
            if fill.offer(track):
                self.used.add(track.uri)
                if song.energy:
                    self.energy[track.uri] = min(10, max(1, song.energy))
                self.roles[track.uri] = song.role
            else:
                self.trace.event("skipped", title=track.name, minutes=round(track.duration_ms / 60_000, 1),
                                 reason="length rule" if not fill.done else "section full")

    def _allowed(self, track: Track) -> bool:
        return not (self.brief.hard_rules.no_explicit and track.explicit)

    def _find(self, sp: SpotifyClient, song: Song, of: int) -> Track | None:
        """From the known-songs pool when the LLM picked one from it (and it really is that song), else by
        search. An explicit pool track under "no explicit" is searched instead: that can find the clean version."""
        pool = self.pool
        from_pool = pool[song.known - 1] if song.known and 1 <= song.known <= len(pool) else None
        if from_pool and self._allowed(from_pool) and pick(song, [from_pool]):
            self.trace.event("known", title=song.title, artist=song.artist, number=song.known)
            return from_pool
        artist = primary_artist(song.artist)
        candidates = search_cache.get(self.spotify_id, song.title, artist)
        cached = candidates is not None
        if cached:
            self.cache_hits += 1
        else:
            candidates = sp.search_track(song.title, artist)
            search_cache.put(self.spotify_id, song.title, artist, candidates)
            self.searched += 1
        uri = pick(song, [c for c in candidates if self._allowed(c)])
        track = next((c for c in candidates if c.uri == uri), None)
        self.trace.event(
            "search", step=f"Finding the songs on Spotify ({self.searched + self.cache_hits}/{of})",
            title=song.title, artist=song.artist, candidates=len(candidates), matched=track is not None, cached=cached,
            **({"explicit_skipped": sum(not self._allowed(c) for c in candidates)} if self.brief.hard_rules.no_explicit else {}),
        )
        return track
