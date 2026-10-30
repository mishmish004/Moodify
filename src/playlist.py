"""
mood -> a real playlist in the user's account on their music service.

read the prompt into a brief (LLM) -> work out minutes per section -> suggest songs per section (LLM)
-> find each on the service (known-songs pool, search cache, or search) -> fill each section to its length
-> create the playlist -> add items -> save the row and its tracks.
Sections still short after the first round get one extra suggestion call, for those sections only.
run() is the background entry point: it records how the build ended on its job.
Memory reaches both LLM calls: the user-wide memory block, plus the lessons recalled for the prompt (brief
call) and for the brief (suggestion call). Songs from the last 3 playlists aren't used again unless liked,
and then at most MAX_REPEATS of them (Phase 16). "Fix it with this review" is a build with `fix_of`: the
earlier brief and its review go in as a correction, and the new playlist points back at the old one.
"More like this" is a build with `like_of`: that playlist's brief, liked songs and review seed it.
When the prompt pulls against a lesson, the build asks the listener first and waits up to 20 s for the answer
(Phase 30, src/clarify.py).
"""
import json
from dataclasses import dataclass
from typing import Callable, Optional

from src import clarify, jobs, likes, memory, music, playlists, search_cache, taste, users
from src.arc import arrange, timeline
from src.brief import Brief, familiar_percent, read_prompt
from src.core.exceptions import QuotaExceeded, ReauthRequired, ServiceError
from src.core.logger import MoodifyLogger
from src.fill import SectionFill, section_targets, songs_to_ask_for
from src.jobs import NO_TRACE, Trace
from src.match import normalize_artist, normalize_title, pick, primary_artist
from src.music import MusicService, Track
from src.suggest import Song, clean_mood, suggest_songs

RECENT_PLAYLISTS = 3     # their songs aren't used again unless liked
MAX_REPEATS = 2          # liked songs from those playlists, per new playlist
RECENT_LISTED = 40       # of those songs named in the suggestion prompt
# Searches per build and playlist name and description lengths are the service's (src/music)


class BuildFailed(Exception):
    """Nothing to make. `reason` is one of the jobs.NO_* codes. No playlist was created."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def run(job_id: int, user_id: str, mood: str, fix_of: Optional[int] = None, like_of: Optional[int] = None) -> None:
    """Build in the background and record the outcome on the job. Never raises: a thread has nobody to raise to."""
    log = MoodifyLogger.get_logger("playlist")
    try:
        user = users.get(user_id)
        if user is None:  # disconnected while queued; the job row went with the user
            return
        playlist_id = build(user, mood, Trace(job_id), fix_of=fix_of, like_of=like_of)
    except BuildFailed as exc:
        jobs.fail(job_id, exc.reason)
    except QuotaExceeded as exc:
        log.warning("quota/rate limit hit during job %d: %s", job_id, exc)
        jobs.fail(job_id, jobs.QUOTA)
    except ReauthRequired:
        jobs.fail(job_id, jobs.REAUTH)
    except ServiceError as exc:
        log.warning("job %d: %s didn't answer: %s", job_id, exc.service, exc)
        jobs.fail(job_id, jobs.service_failure(exc))
    except Exception:
        log.exception("job %d failed", job_id)
        jobs.fail(job_id, jobs.ERROR)
    else:
        jobs.finish(job_id, playlist_id)


@dataclass
class Recipe:
    """What a build follows besides the prompt: whose taste, which memory, how the result is named.
    build() makes one from the user's own profile; Blends (Phase 20) and "hear it like a friend" (Phase 21)
    make their own from stored profiles, so their builds never use anyone's service but the requester's."""
    profile: taste.Profile
    familiar_percent: int                                  # share of known songs unless the prompt says otherwise
    # The requester's memory, for their own playlists: recall(words, skip=lesson ids) -> memory.Parts
    recall: Optional[Callable[..., memory.Parts]] = None
    clarify: bool = False                                  # may ask before building when the prompt and memory clash
    correction: str = ""
    feedback: str = ""
    repeats: Optional["_Repeats"] = None
    name: Callable[[Brief], str] = lambda brief: f"Moodify: {brief.title}"
    note: str = ""                                         # opens the playlist description
    public: bool = False
    parent_id: Optional[int] = None
    tidy: Callable[[Brief], Brief] = lambda brief: brief   # e.g. a Blend's union of everyone's avoid rules
    balance: Optional[object] = None                       # a Blend's fair share (src/blend.py)


@dataclass
class Built:
    playlist_id: int
    tracks: list[Track]
    finder: "_Finder"


def build(user, mood: str, trace: Trace = NO_TRACE, fix_of: Optional[int] = None, like_of: Optional[int] = None) -> int:
    """Returns the new playlists row id. Raises BuildFailed when there is nothing to make;
    QuotaExceeded and ReauthRequired propagate. In every failure no playlist is created.
    `fix_of`: the id of the user's playlist whose review this build corrects. `like_of`: the id of the
    user's playlist to make more like."""
    user_id = user["user_id"]
    trace.step("Reading your taste")
    profile = taste.profile_for(user)
    parent = playlists.get(fix_of, user_id) if fix_of else None
    seed = playlists.get(like_of, user_id) if like_of else None
    correction, feedback = fix_notes(parent, user_id) if parent else more_notes(seed, user_id) if seed else ("", "")
    version = f" (v{playlists.version(parent['id']) + 1})" if parent else ""
    recipe = Recipe(
        profile=profile, familiar_percent=taste.familiar_percent(json.loads(user["onboarding_json"] or "{}")),
        recall=lambda wanted, skip=(): memory.prompt_parts(user_id, wanted, skip),
        clarify=not correction, correction=correction, feedback=feedback,
        repeats=_Repeats(user_id, skip=parent["id"] if parent else None, also=seed["id"] if seed else None),
        name=lambda brief: f"Moodify: {brief.title}{version}", parent_id=parent["id"] if parent else None,
    )
    return make(user, mood, recipe, trace).playlist_id


def make(user, mood: str, recipe: Recipe, trace: Trace = NO_TRACE) -> Built:
    """The build itself, for whichever recipe. Only `user`'s service is used: to search, and to save the
    playlist in their account."""
    user_id = user["user_id"]
    service = music.info(user["service"])
    mood = clean_mood(mood)
    profile = recipe.profile
    no_memory = memory.Parts("", "", ())
    for_prompt = recipe.recall(memory.words(mood)) if recipe.recall else no_memory
    trace.step("Reading your prompt")
    brief = recipe.tidy(read_prompt(mood, profile.summary, trace, user_id=user_id, memory=for_prompt.block,
                                    correction=recipe.correction, recalled=for_prompt.recalled))
    feedback, skip = recipe.feedback, ()
    asks = recipe.clarify and recipe.recall is not None and trace.job_id is not None  # someone may be watching
    question = clarify.from_brief(brief, user_id) if asks else None
    if question:
        answer = clarify.ask(trace, question)
        if answer in (clarify.THIS_TIME, clarify.FROM_NOW_ON):
            if answer == clarify.FROM_NOW_ON:
                memory.retire(user_id, question.lesson_id)
            note, skip = clarify.note(question, answer), (question.lesson_id,)
            feedback = "\n\n".join(x for x in (feedback, note) if x)
            for_prompt = recipe.recall(memory.words(mood), skip=skip)
            trace.step("Reading your prompt again")
            brief = recipe.tidy(read_prompt(mood, profile.summary, trace, user_id=user_id, memory=for_prompt.block,
                                            correction=note, recalled=for_prompt.recalled))
    targets = section_targets(brief)
    trace.event("brief", title=brief.title, sections=len(brief.sections), support_note=brief.support_note,
                minutes=[round(t, 1) for t in targets])
    remembered = recipe.recall(memory.brief_words(brief) | memory.words(mood), skip=skip) if recipe.recall else no_memory
    if recipe.recall:
        memory.mark_used(user_id, for_prompt.ids + remembered.ids)
    repeats = recipe.repeats
    trace.event("memory", block_chars=len(remembered.block), recalled=list(dict.fromkeys(for_prompt.ids + remembered.ids)),
                recent_blocked=len(repeats.blocked) if repeats else 0, recent_liked=len(repeats.liked) if repeats else 0)
    ask = {"known": profile.pool, "known_labels": profile.labels,
           "familiar_percent": familiar_percent(brief, recipe.familiar_percent),
           "brief": brief, "user_id": user_id, "memory": remembered.block, "recalled": remembered.recalled,
           "feedback": feedback, "recent": repeats.listed if repeats else []}

    trace.step("Asking for song ideas")
    counts = [songs_to_ask_for(t) for t in targets]
    songs = suggest_songs(mood, profile.summary, n=sum(counts), counts=counts, trace=trace, **ask)
    if not songs:
        raise BuildFailed(jobs.NO_SUGGESTIONS)
    if recipe.balance:
        songs = recipe.balance.order(songs)
    fills = [SectionFill(t, brief.hard_rules) for t in targets]
    finder = _Finder(user_id, brief, profile.pool, trace, repeats, service)
    trace.step(f"Finding the songs on {service.name}")
    with music.for_user(user) as sp:
        finder.place(sp, songs, fills)
        short = [i for i, f in enumerate(fills) if not f.done]
        if short:
            trace.step("Asking for a few more songs")
            extra_counts = [songs_to_ask_for(f.missing) if i in short else 0 for i, f in enumerate(fills)]
            extra = suggest_songs(mood, profile.summary, n=sum(extra_counts), counts=extra_counts, exclude=songs,
                                  trace=trace, **ask)
            if recipe.balance:
                extra = recipe.balance.order(extra)
            finder.place(sp, extra, fills)
            songs = songs + extra
        if recipe.balance:
            songs = songs + recipe.balance.settle(
                lambda counts, note: suggest_songs(mood, profile.summary, n=sum(counts), counts=counts, exclude=songs,
                                                   trace=trace, **{**ask, "feedback": "\n\n".join(
                                                       x for x in (feedback, note) if x)}),
                sp, finder, fills, trace)
        if not any(f.tracks for f in fills):
            raise BuildFailed(jobs.NO_MATCHES)
        ordered = arrange([f.tracks for f in fills], finder.energy, finder.roles, brief)
        tracks = [t for section in ordered for t in section]
        minutes = round(sum(f.total for f in fills))
        trace.step(f"Saving the playlist to your {service.name}")
        sp.check_room(len(tracks))  # a used-up quota stops the build here, before any write
        description = _description(brief, [f.total for f in fills], minutes, recipe.note, service.description_chars)
        playlist = sp.create_playlist(recipe.name(brief)[:service.name_chars], description, public=recipe.public)
        sp.add_items(playlist.id, [t.uri for t in tracks])
    sections = [i for i, section in enumerate(ordered) for _ in section]
    row_id = playlists.insert(
        user_id, mood, playlist.url, tracks, brief.model_dump_json(), sections,
        [finder.energy.get(t.uri) for t in tracks], [finder.roles.get(t.uri) for t in tracks],
        parent_id=recipe.parent_id,
    )
    pool_uris = {t.uri for t in profile.pool}
    known = sum(t.uri in pool_uris for t in tracks)
    MoodifyLogger.get_logger("playlist").info(
        "%s: %d tracks, %d min (%d known) from %d searches (%d suggestions) for %r",
        user_id, len(tracks), minutes, known, finder.searched, len(songs), brief.title,
    )
    trace.event("built", playlist_id=row_id, tracks=len(tracks), minutes=minutes, known=known,
                searched=finder.searched, cache_hits=finder.cache_hits, suggested=len(songs),
                section_minutes=[round(f.total, 1) for f in fills], targets=[round(t, 1) for t in targets])
    return Built(row_id, tracks, finder)


def fix_notes(parent, user_id: str) -> tuple[str, str]:
    """(correction for the brief call, feedback for the suggestion call) from a playlist's review, the
    lessons learned from it, and the songs liked in it."""
    lines = []
    if parent["brief_json"]:
        lines.append(f"Earlier brief: {parent['brief_json']}")
    lines.append(f"What the listener said after hearing it: {parent['review']}")
    learned = memory.from_playlist(user_id, parent["id"])
    if learned:
        lines.append("What Moodify took from that:\n" + memory.render(learned, limit=800))
    rows = playlists.track_rows(parent["id"])
    liked = likes.liked_uris(user_id, [r["uri"] for r in rows])
    kept = [f"{r['name']} by {json.loads(r['artists_json'])[0]}" for r in rows if r["uri"] in liked][:10]
    feedback = "Feedback on an earlier version of this playlist:\n" + "\n".join(lines[1:])
    if kept:
        feedback += "\nSongs they liked in it (keep them if they still fit): " + "; ".join(kept)
    return "\n".join(lines), feedback


def more_notes(seed, user_id: str) -> tuple[str, str]:
    """(correction for the brief call, note for the suggestion call) for "More like this": the earlier
    playlist's brief, its liked songs and its review, asking for new songs in the same spirit."""
    rows = playlists.track_rows(seed["id"])
    liked = likes.liked_uris(user_id, [r["uri"] for r in rows])
    kept = [f"{r['name']} by {json.loads(r['artists_json'])[0]}" for r in rows if r["uri"] in liked][:10]
    lines = ["The listener wants more like an earlier playlist: same feel and shape, new songs."]
    if seed["brief_json"]:
        lines.append(f"Earlier brief: {seed['brief_json']}")
    if kept:
        lines.append("Songs they liked in it (more like these): " + "; ".join(kept))
    if seed["review"]:
        lines.append(f"What they said about it: {seed['review']}")
    return "\n".join(lines), "More like an earlier playlist:\n" + "\n".join(lines[2:] if seed["brief_json"] else lines[1:])


def _description(brief: Brief, section_minutes: list[float], minutes: int, note: str, limit: int) -> str:
    """The timeline for a playlist with several sections ("0–14 Summer morning → 14–44 Workout → ..."),
    else the summary, after `note` if any; always within the service's `limit`."""
    lead = f"{note} " if note else ""
    if len(brief.sections) > 1:
        text = f"{lead}{timeline(brief, section_minutes)}. Made by Moodify."
        if len(text) <= limit:
            return text
    text = f"{lead}Made by Moodify: {brief.summary} ({minutes} min)"
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _Repeats:
    """Songs of the last RECENT_PLAYLISTS playlists (leaving out the one being fixed, adding the one being made
    more like): `blocked` unless liked; `liked` ones may come back, MAX_REPEATS per playlist."""

    def __init__(self, user_id: str, skip: Optional[int] = None, also: Optional[int] = None):
        rows = playlists.recent_tracks(user_id, RECENT_PLAYLISTS, skip=skip)
        if also is not None:
            rows += playlists.track_rows(also)
        by_uri = {r["uri"]: r for r in rows}
        self.liked = likes.liked_uris(user_id, list(by_uri))
        self.blocked = set(by_uri) - self.liked
        self.left = MAX_REPEATS
        self.listed = [f"{r['name']} by {json.loads(r['artists_json'])[0]}" for uri, r in by_uri.items()
                       if uri in self.blocked][:RECENT_LISTED]


class _Finder:
    """Turns suggested songs into tracks on the user's service and offers each to its section. Searches only
    while the song's section still needs tracks, never twice for the same song, and at most the service's
    max_searches. Enforces the no-repeat rule in code, whatever the model suggested."""

    def __init__(self, user_id: str, brief: Brief, pool: list[Track], trace: Trace, repeats: Optional[_Repeats],
                 service: music.Info):
        self.user_id, self.brief, self.pool, self.trace = user_id, brief, pool, trace
        self.repeats = repeats
        self.service = service
        self.searched = 0
        self.cache_hits = 0
        self.tried: set[tuple[str, str]] = set()  # songs already looked at in this build
        self.used: set[str] = set()                # URIs already in the playlist
        self.energy: dict[str, int] = {}           # by URI, from the song that placed it
        self.roles: dict[str, Optional[str]] = {}
        self.songs: dict[str, Song] = {}

    def section_of(self, song: Song, fills: list[SectionFill]) -> SectionFill:
        return fills[min(max((song.section or 1) - 1, 0), len(fills) - 1)]

    def lookup(self, sp: MusicService, song: Song, of: int) -> Optional[Track]:
        """The track for a suggested song, if it may go in the playlist: found, not looked at before in this
        build, not already in it, not a repeat from a recent playlist. None also past the search limit."""
        key = (normalize_title(song.title), normalize_artist(song.artist))
        if key in self.tried:
            return None
        if self.searched >= self.service.max_searches:
            self.trace.event("limit", searches=self.searched)
            return None
        self.tried.add(key)
        track = self._find(sp, song, of)
        if track is None or track.uri in self.used:
            return None
        repeat = self.repeats is not None and track.uri in self.repeats.liked
        if self.repeats is not None and (track.uri in self.repeats.blocked or repeat and self.repeats.left <= 0):
            self.trace.event("skipped", title=track.name, reason="in a recent playlist")
            return None
        return track

    def keep(self, track: Track, song: Song) -> None:
        """Record a track that went into the playlist, with what the song that placed it said."""
        self.used.add(track.uri)
        if self.repeats is not None and track.uri in self.repeats.liked:
            self.repeats.left -= 1
        if song.energy:
            self.energy[track.uri] = min(10, max(1, song.energy))
        self.roles[track.uri] = song.role
        self.songs[track.uri] = song

    def place(self, sp: MusicService, songs: list[Song], fills: list[SectionFill]) -> None:
        for song in songs:
            fill = self.section_of(song, fills)
            if fill.done:
                continue
            if self.searched >= self.service.max_searches:
                self.trace.event("limit", searches=self.searched)
                return
            track = self.lookup(sp, song, len(songs))
            if track is None:
                continue
            if fill.offer(track):
                self.keep(track, song)
            else:
                self.trace.event("skipped", title=track.name, minutes=round(track.duration_ms / 60_000, 1),
                                 reason="length rule" if not fill.done else "section full")

    def _allowed(self, track: Track) -> bool:
        return not (self.brief.hard_rules.no_explicit and track.explicit)

    def _find(self, sp: MusicService, song: Song, of: int) -> Track | None:
        """From the known-songs pool when the LLM picked one from it (and it really is that song), else by
        search. An explicit pool track under "no explicit" is searched instead: that can find the clean version.
        So is a pool track from a service whose taste reads don't say what's explicit, and one from another
        service than the user's (a friend's, in a Blend): only the user's service can go in their playlist."""
        pool = self.pool
        from_pool = pool[song.known - 1] if song.known and 1 <= song.known <= len(pool) else None
        usable = from_pool is not None and from_pool.service == self.service.service and (
            self.service.pool_explicit or not self.brief.hard_rules.no_explicit)
        if usable and self._allowed(from_pool) and pick(song, [from_pool]):
            self.trace.event("known", title=song.title, artist=song.artist, number=song.known)
            return from_pool
        artist = primary_artist(song.artist)
        candidates = search_cache.get(self.user_id, song.title, artist)
        cached = candidates is not None
        if cached:
            self.cache_hits += 1
        else:
            candidates = sp.search_track(song.title, artist)
            search_cache.put(self.user_id, song.title, artist, candidates)
            self.searched += 1
        uri = pick(song, [c for c in candidates if self._allowed(c)])
        track = next((c for c in candidates if c.uri == uri), None)
        self.trace.event(
            "search", step=f"Finding the songs on {self.service.name} ({self.searched + self.cache_hits}/{of})",
            title=song.title, artist=song.artist, candidates=len(candidates), matched=track is not None, cached=cached,
            **({"explicit_skipped": sum(not self._allowed(c) for c in candidates)} if self.brief.hard_rules.no_explicit else {}),
        )
        return track
