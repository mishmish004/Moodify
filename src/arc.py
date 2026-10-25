"""
Phase 13: put each section's songs in an order that moves the way the listener asked.

Sections play in the brief's order. Inside a section:
- a section that eases in ("blend") opens with up to 2 bridge songs, closest to the previous
  section's energy first, so the change is gradual;
- a section that starts with a hard cut ("cut") gets no bridges and opens with the song closest
  to its own energy, so it starts at full level;
- the other songs climb or fall toward the next section's energy (the last section keeps the
  arc's direction; a single-section playlist keeps the model's order).
Energy is the LLM's judgement per song: Spotify's audio features are deprecated (2026).
"""
from typing import Optional

from src.brief import Brief
from src.music import Track

MAX_BRIDGES = 2


def arrange(sections: list[list[Track]], energy: dict[str, int], roles: dict[str, Optional[str]], brief: Brief) -> list[list[Track]]:
    """The same tracks per section, reordered. `energy` and `roles` are by track URI."""
    levels = [s.energy for s in brief.sections]
    ordered = []
    for i, tracks in enumerate(sections):
        own = levels[i]
        before = levels[i - 1] if i > 0 else None
        after = levels[i + 1] if i + 1 < len(levels) else None
        cut = i > 0 and brief.sections[i].transition_in == "cut"

        def level(track: Track) -> int:
            return energy.get(track.uri) or own

        bridges = []
        if before is not None and not cut:
            bridges = sorted((t for t in tracks if roles.get(t.uri) == "bridge"), key=lambda t: abs(level(t) - before))[:MAX_BRIDGES]
        rest = [t for t in tracks if t not in bridges]
        direction = _direction(own, before, after)
        if direction:
            rest = sorted(rest, key=level, reverse=direction < 0)  # stable: ties keep the model's order
        if cut and rest:
            opener = min(rest, key=lambda t: abs(level(t) - own))
            rest = [opener] + [t for t in rest if t is not opener]
        ordered.append(bridges + rest)
    return ordered


def _direction(own: int, before: Optional[int], after: Optional[int]) -> int:
    """+1 to climb, -1 to fall, 0 to keep the order."""
    if after is not None and after != own:
        return 1 if after > own else -1
    if before is not None and before != own:
        return 1 if own > before else -1
    return 0


def timeline(brief: Brief, section_minutes: list[float]) -> str:
    """ "0–14 Summer morning → 14–44 Workout → 44–58 Lock in" """
    parts, start = [], 0.0
    for section, length in zip(brief.sections, section_minutes):
        if length <= 0:
            continue
        end = start + length
        parts.append(f"{round(start)}–{round(end)} {section.label}")
        start = end
    return " → ".join(parts)
