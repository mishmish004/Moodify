"""
Phase 12: make each section of a playlist as long as asked, with Spotify's real track lengths.

section_targets() turns a brief into minutes per section. A SectionFill takes tracks in the
order offered and keeps one only if it moves the section toward its target without passing
the upper bound; songs are never cut. A section is done once it's inside the window:
target ± 10%, or ± 3 minutes for short sections.
"""
import math
from dataclasses import dataclass, field

from src.brief import Brief, HardRules
from src.music import Track

DEFAULT_TOTAL = 60   # minutes, when the prompt names no length
MAX_TOTAL = 180      # 3 hours: Spotify search quota
MIN_SECTION = 10     # a section without a length gets at least this
TOLERANCE = 0.10
MIN_TOLERANCE = 3.0  # minutes
AVERAGE_SONG = 3.5   # minutes, for how many songs to ask for
OVER_ASK = 1.5       # ask for more than needed: some won't match or won't fit


def section_targets(brief: Brief) -> list[float]:
    """Minutes per section. Stated lengths are kept; sections without one share the rest of
    total_minutes, or of DEFAULT_TOTAL when the prompt names no length at all. Capped at MAX_TOTAL."""
    given = [s.minutes for s in brief.sections]
    stated = sum(m for m in given if m)
    open_sections = sum(1 for m in given if not m)
    total = brief.total_minutes or DEFAULT_TOTAL
    each = max(total - stated, MIN_SECTION * open_sections) / open_sections if open_sections else 0.0
    targets = [float(m) if m else each for m in given]
    if sum(targets) > MAX_TOTAL:
        targets = [t * MAX_TOTAL / sum(targets) for t in targets]
    return targets


def window(target: float) -> tuple[float, float]:
    slack = max(target * TOLERANCE, MIN_TOLERANCE)
    return max(0.0, target - slack), target + slack


def songs_to_ask_for(minutes: float) -> int:
    return max(3, math.ceil(OVER_ASK * minutes / AVERAGE_SONG))


def minutes(track: Track) -> float:
    return track.duration_ms / 60_000


def allowed_length(track: Track, rules: HardRules) -> bool:
    """The brief's max/min song length ("short songs only", "nothing over 6 minutes")."""
    length = minutes(track)
    if rules.max_track_minutes and length > rules.max_track_minutes:
        return False
    return not (rules.min_track_minutes and length < rules.min_track_minutes)


@dataclass
class SectionFill:
    target: float  # minutes
    rules: HardRules
    tracks: list[Track] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(minutes(t) for t in self.tracks)

    @property
    def done(self) -> bool:
        return self.total >= window(self.target)[0]

    @property
    def missing(self) -> float:
        """Minutes still needed to reach the target."""
        return max(0.0, self.target - self.total)

    def offer(self, track: Track) -> bool:
        """Keep the track if the section isn't done, its length is allowed and it doesn't pass the window."""
        if self.done or not allowed_length(track, self.rules):
            return False
        if self.total + minutes(track) > window(self.target)[1]:
            return False  # would overshoot: a shorter song later may fit
        self.tracks.append(track)
        return True


def fill(target: float, candidates: list[Track], rules: HardRules) -> list[Track]:
    """The tracks a section keeps from these candidates, in order."""
    section = SectionFill(target, rules)
    for track in candidates:
        section.offer(track)
    return section.tracks
