"""
Phase 20: Moodify Blend. Pick friends, type a prompt, get one playlist that fits the prompt and everyone's taste.

Only stored data is used for the friends: their taste summary, onboarding answers and music library
(Phase 19). Their service logins are never touched; the playlist is searched and saved with the
requester's alone. Each friend's consent (Phase 18, "Let friends include me in Blends") is checked once,
when the build starts.

Fairness: every placed track is credited to the members whose library holds it, else to the member the
model said it's for ("everyone": all of them). A song credited to several counts fully for each. Each
member's credited minutes should reach their share less FAIR_SLACK (the share is equal, or doubled for
the one the blend leans toward). A member below that gets one top-up call, and their songs replace
tracks of members who stay above their own floor without them.
"""
import json
import math
import re
from dataclasses import dataclass, field
from typing import Optional

from src import jobs, library, playlists, sharing, taste, users
from src.brief import Brief
from src.core.exceptions import QuotaExceeded, ReauthRequired
from src.core.logger import MoodifyLogger
from src.db import get_conn
from src.fill import SectionFill, minutes, songs_to_ask_for, window
from src.jobs import NO_TRACE, Trace
from src.music import Track
from src.suggest import Song

MAX_FRIENDS = 4         # plus the requester: Development Mode allows 5 users
SUMMARY_CHARS = 600     # each member's taste, condensed
POOL_SIZE = 150
FAIR_SLACK = 0.10
LEAN_WEIGHT = 2
NOT_SHARED = "not_shared"  # jobs.error when a friend switched Blends off before the build started
EXPLICIT_WORDS = re.compile(r"explicit|swear|curs|profan|clean lyrics|clean songs|bad words", re.I)

log = MoodifyLogger.get_logger("blend")


@dataclass(frozen=True)
class Member:
    user_id: str
    name: str                     # first name, unique within the blend
    summary: str                  # their stored taste, condensed to SUMMARY_CHARS
    avoid: tuple[str, ...]        # from their onboarding answers
    no_explicit: bool
    weights: dict = field(default_factory=dict, compare=False)  # library weight by track URI


def avoid_phrases(onboarding: dict) -> tuple[str, ...]:
    """The onboarding "avoid" answer as phrases, without a leading "no"/"avoid" ("no country" -> "country"),
    since the rules list already says "no ..." in front of each."""
    raw = str(onboarding.get("avoid") or "")
    phrases = (re.sub(r"^(no|not|avoid|without|nothing)\s+", "", p.strip(" ."), flags=re.I)
               for p in re.split(r"[,;/\n]| and ", raw))
    return tuple(dict.fromkeys(p for p in phrases if p))


def member(row) -> Member:
    """A person as a Blend sees them, from stored data only."""
    onboarding = json.loads(row["onboarding_json"] or "{}")
    avoid = avoid_phrases(onboarding)
    return Member(
        user_id=row["user_id"], name=sharing.first_name(row),
        summary=taste.condense(taste.stored_summary(row), SUMMARY_CHARS), avoid=avoid,
        no_explicit=any(EXPLICIT_WORDS.search(a) for a in avoid),
        weights={t.uri: w for t, w in library.for_user(row["user_id"])},
    )


def members_for(requester, friend_ids: list[str]) -> list[Member]:
    """The requester first, then the friends in the order picked. Repeated first names get a number."""
    found = [member(requester)] + [member(users.get(f)) for f in friend_ids]
    seen: dict[str, int] = {}
    out = []
    for m in found:
        seen[m.name.casefold()] = seen.get(m.name.casefold(), 0) + 1
        out.append(m if seen[m.name.casefold()] == 1 else Member(m.user_id, f"{m.name} {seen[m.name.casefold()]}",
                                                                   m.summary, m.avoid, m.no_explicit, m.weights))
    return out


def profile(members: list[Member]) -> taste.Profile:
    """The Blend's taste for the prompts: each member's condensed summary under their name, and a known-songs
    list with common ground first (tracks in two or more libraries, by combined weight), then each member's
    heaviest tracks in turn, every song labelled with whose it is."""
    summary = (f"A Blend for {len(members)} people: {', '.join(m.name for m in members)}. The playlist must suit "
               "all of them, with a fair share of songs for each. Each person's taste:\n"
               + "\n".join(f"{m.name}: {m.summary}" for m in members))
    tracks: dict[str, Track] = {}
    for m in members:
        for track, _ in library.for_user(m.user_id):
            tracks.setdefault(track.uri, track)
    owners = {uri: [m for m in members if uri in m.weights] for uri in tracks}
    common = sorted((uri for uri, who in owners.items() if len(who) > 1),
                    key=lambda uri: (-sum(m.weights[uri] for m in owners[uri]), uri))
    pool, labels = [tracks[uri] for uri in common], ["common ground"] * len(common)
    queues = [[uri for uri in sorted(m.weights, key=lambda u: (-m.weights[u], u)) if len(owners[uri]) == 1] for m in members]
    while len(pool) < POOL_SIZE and any(queues):
        for m, queue in zip(members, queues):
            if queue and len(pool) < POOL_SIZE:
                uri = queue.pop(0)
                pool.append(tracks[uri])
                labels.append(m.name)
    return taste.Profile(summary, pool[:POOL_SIZE], tuple(labels[:POOL_SIZE]))


def union_rules(members: list[Member]):
    """Everyone's "avoid" applies to the whole Blend, and anyone's "no explicit" applies to everyone."""
    def tidy(brief: Brief) -> Brief:
        have = {a.casefold() for a in brief.hard_rules.avoid}
        avoid = list(brief.hard_rules.avoid)
        for m in members:
            for phrase in m.avoid:
                if EXPLICIT_WORDS.search(phrase):
                    continue  # covered by no_explicit below
                if phrase.casefold() not in have:
                    have.add(phrase.casefold())
                    avoid.append(phrase)
        rules = brief.hard_rules.model_copy(update={
            "avoid": avoid, "no_explicit": brief.hard_rules.no_explicit or any(m.no_explicit for m in members)})
        return brief.model_copy(update={"hard_rules": rules})
    return tidy


class Balance:
    """A Blend's fair share, used by playlist.make(): the order songs are tried in, then a top-up."""

    def __init__(self, members: list[Member], lean: Optional[str] = None):
        self.members = members
        weights = {m.user_id: LEAN_WEIGHT if m.user_id == lean else 1 for m in members}
        self.share = {sid: w / sum(weights.values()) for sid, w in weights.items()}
        self.by_name = {m.name.casefold(): m.user_id for m in members}
        self.top_ups: dict[str, int] = {}  # member -> how many songs their top-up asked for

    def credit(self, uri: str, song: Optional[Song]) -> set[str]:
        owners = {m.user_id for m in self.members if uri in m.weights}
        if owners:
            return owners
        named = self.by_name.get((song.for_whom or "").strip().casefold()) if song else None
        return {named} if named else {m.user_id for m in self.members}

    def order(self, songs: list[Song]) -> list[Song]:
        """Songs for everyone first, then one for each person in turn, so placing them in order can't use up
        a section on one person's songs."""
        everyone, per = [], {m.user_id: [] for m in self.members}
        for song in songs:
            named = self.by_name.get((song.for_whom or "").strip().casefold())
            (per[named] if named else everyone).append(song)
        out = list(everyone)
        while any(per.values()):
            for queue in per.values():
                if queue:
                    out.append(queue.pop(0))
        return out

    def minutes(self, fills: list[SectionFill], finder) -> dict[str, float]:
        out = {m.user_id: 0.0 for m in self.members}
        for fill in fills:
            for track in fill.tracks:
                for sid in self.credit(track.uri, finder.songs.get(track.uri)):
                    out[sid] += minutes(track)
        return out

    def floors(self, total: float) -> dict[str, float]:
        return {sid: max(0.0, share - FAIR_SLACK) * total for sid, share in self.share.items()}

    def settle(self, ask_more, sp, finder, fills: list[SectionFill], trace: Trace = NO_TRACE) -> list[Song]:
        """One top-up call per member below their floor, and swaps toward them. Returns the songs asked for."""
        total = sum(f.total for f in fills)
        if not total:
            return []
        floors = self.floors(total)
        have = self.minutes(fills, finder)
        trace.event("share", minutes={sid: round(v, 1) for sid, v in have.items()},
                    floors={sid: round(v, 1) for sid, v in floors.items()})
        asked: list[Song] = []
        for m in self.members:
            have = self.minutes(fills, finder)
            if have[m.user_id] >= floors[m.user_id]:
                continue
            missing = floors[m.user_id] - have[m.user_id]
            wanted = songs_to_ask_for(missing)
            targets = [f.target for f in fills]
            counts = [max(1, math.ceil(wanted * t / sum(targets))) for t in targets]
            self.top_ups[m.user_id] = sum(counts)
            trace.step(f"Finding more for {m.name}")
            note = (f"This call is a top-up for {m.name}, who has too few songs in the Blend so far. Suggest songs "
                    f"mainly for {m.name}, from their taste and the songs labelled ({m.name}), with \"for_whom\" set "
                    f"to \"{m.name}\".")
            more = ask_more(counts, note)
            asked += more
            for song in more:
                if self.minutes(fills, finder)[m.user_id] >= floors[m.user_id]:
                    break
                self._swap_in(sp, finder, fills, song, m.user_id, floors)
        after = self.minutes(fills, finder)
        trace.event("share", minutes={sid: round(v, 1) for sid, v in after.items()}, top_ups=self.top_ups)
        return asked

    def _swap_in(self, sp, finder, fills, song: Song, sid: str, floors: dict[str, float]) -> None:
        fill = finder.section_of(song, fills)
        track = finder.lookup(sp, song, len(fill.tracks) + 1)
        if track is None or sid not in self.credit(track.uri, song):
            return
        if fill.offer(track):
            finder.keep(track, song)
            return
        have = self.minutes(fills, finder)
        low, high = window(fill.target)
        best = None
        for index, victim in enumerate(fill.tracks):
            credited = self.credit(victim.uri, finder.songs.get(victim.uri))
            if sid in credited or any(have[o] - minutes(victim) < floors[o] for o in credited):
                continue
            after = fill.total - minutes(victim) + minutes(track)
            if not low <= after <= high:
                continue
            gap = abs(minutes(victim) - minutes(track))
            if best is None or gap < best[0]:
                best = (gap, index)
        if best is not None:
            fill.tracks[best[1]] = track
            finder.keep(track, song)

    def credits(self, tracks: list[Track], finder) -> list[set[str]]:
        return [self.credit(t.uri, finder.songs.get(t.uri)) for t in tracks]


# --- building ------------------------------------------------------------------------------------------

class NotShared(Exception):
    pass


def check(requester_id: str, friend_ids: list[str]) -> None:
    """Raises NotShared unless there are 1-MAX_FRIENDS distinct friends who all allow Blends right now."""
    if not 1 <= len(friend_ids) <= MAX_FRIENDS or len(set(friend_ids)) != len(friend_ids) or requester_id in friend_ids:
        raise NotShared()
    if not all(sharing.allowed(f, requester_id, "blend") for f in friend_ids):
        raise NotShared()


def title(members: list[Member], brief_title: str) -> str:
    return f"Moodify Blend: {' × '.join(m.name for m in members)}: {brief_title}"


def build_blend(user, mood: str, friend_ids: list[str], trace: Trace = NO_TRACE, lean: Optional[str] = None,
                public: bool = False) -> int:
    """The Blend playlist in `user`'s account. Raises playlist.BuildFailed(NOT_SHARED) if a friend no longer
    allows it; consent is checked here, once, before anything of theirs is read."""
    from src import playlist  # playlist imports nothing from here; importing at call time keeps it that way
    try:
        check(user["user_id"], friend_ids)
    except NotShared:
        raise playlist.BuildFailed(NOT_SHARED)
    trace.step("Reading everyone's taste")
    members = members_for(user, friend_ids)
    balance = Balance(members, lean if lean in {m.user_id for m in members} else None)
    names = [m.name for m in members]
    recipe = playlist.Recipe(
        profile=profile(members), familiar_percent=taste.familiar_percent(json.loads(user["onboarding_json"] or "{}")),
        repeats=playlist._Repeats(user["user_id"]), name=lambda brief: title(members, brief.title),
        note=f"A Moodify Blend for {', '.join(names[:-1])} and {names[-1]}.", public=public,
        tidy=union_rules(members), balance=balance,
    )
    built = playlist.make(user, mood, recipe, trace)
    save(built.playlist_id, user["user_id"], members, balance.credits(built.tracks, built.finder), lean)
    return built.playlist_id


def save(playlist_id: int, owner: str, members: list[Member], credits: list[set[str]], lean: Optional[str]) -> int:
    with get_conn() as conn:
        blend_id = conn.execute("INSERT INTO blends (playlist_id, owner, lean) VALUES (?, ?, ?)",
                                (playlist_id, owner, lean)).lastrowid
        conn.executemany("INSERT INTO blend_members (blend_id, user_id, position) VALUES (?, ?, ?)",
                         [(blend_id, m.user_id, i) for i, m in enumerate(members)])
        conn.executemany("INSERT INTO blend_credits (blend_id, position, user_id) VALUES (?, ?, ?)",
                         [(blend_id, position, sid) for position, who in enumerate(credits) for sid in sorted(who)])
    for m in members[1:]:
        sharing.log(m.user_id, owner, "blend", playlist_id)
    return blend_id


def run(job_id: int, user_id: str, mood: str, friend_ids: list[str], lean: Optional[str] = None,
        public: bool = False) -> None:
    """Background entry, like playlist.run. Never raises."""
    from src import playlist
    try:
        user = users.get(user_id)
        if user is None:
            return
        playlist_id = build_blend(user, mood, friend_ids, Trace(job_id), lean, public)
    except playlist.BuildFailed as exc:
        jobs.fail(job_id, exc.reason)
    except QuotaExceeded as exc:
        log.warning("quota/rate limit hit during blend job %d: %s", job_id, exc)
        jobs.fail(job_id, jobs.QUOTA)
    except ReauthRequired:
        jobs.fail(job_id, jobs.REAUTH)
    except Exception:
        log.exception("blend job %d failed", job_id)
        jobs.fail(job_id, jobs.ERROR)
    else:
        jobs.finish(job_id, playlist_id)


# --- reading -------------------------------------------------------------------------------------------

@dataclass
class Shown:
    """A Blend as its page shows it: members in order with their colour slot, each track's credit, and shares."""
    members: list[dict]
    credits: list[list[str]]   # per track position: member ids
    shares: list[dict]         # per member: minutes, percent of the playlist, floor percent


def for_playlist(playlist_id: int, owner: str, tracks: list[Track]) -> Optional[Shown]:
    with get_conn() as conn:
        blend = conn.execute("SELECT * FROM blends WHERE playlist_id = ? AND owner = ?", (playlist_id, owner)).fetchone()
        if blend is None:
            return None
        rows = conn.execute(
            "SELECT m.user_id, m.position, u.display_name FROM blend_members m JOIN users u USING (user_id)"
            " WHERE m.blend_id = ? ORDER BY m.position", (blend["id"],)).fetchall()
        credit_rows = conn.execute("SELECT position, user_id FROM blend_credits WHERE blend_id = ?", (blend["id"],)).fetchall()
    members = [{"id": r["user_id"], "name": sharing.first_name(r), "slot": i + 1,
                "initials": _initials(r["display_name"] or r["user_id"])} for i, r in enumerate(rows)]
    present = {m["id"] for m in members}
    credits = [[] for _ in tracks]
    for r in credit_rows:
        if r["position"] < len(credits) and r["user_id"] in present:
            credits[r["position"]].append(r["user_id"])
    total = sum(minutes(t) for t in tracks) or 1.0
    weights = {m["id"]: LEAN_WEIGHT if m["id"] == blend["lean"] else 1 for m in members}
    shares = []
    for m in members:
        got = sum(minutes(t) for t, who in zip(tracks, credits) if m["id"] in who)
        share = weights[m["id"]] / sum(weights.values())
        shares.append({**m, "minutes": round(got), "percent": round(100 * got / total),
                       "floor": round(100 * max(0.0, share - FAIR_SLACK))})
    return Shown(members, credits, shares)


def _initials(name: str) -> str:
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"
