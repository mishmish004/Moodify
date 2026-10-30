"""What the templates need beyond the database rows: dates and lengths in words, the build's steps
for the progress page, the energy chart on the playlist page, and the onboarding choices.

Plain functions over plain values, so they're easy to test and the templates stay simple."""
import re
from datetime import date, datetime
from typing import Optional

# --- dates and lengths ---------------------------------------------------------------------------


def day(created_at: Optional[str], today: Optional[date] = None) -> str:
    """'2026-10-18 09:12:00' → 'Sun 18 Oct'; a year other than this one is added: 'Sun 2 Aug 2025'."""
    if not created_at:
        return ""
    when = datetime.fromisoformat(created_at[:19]).date()
    text = f"{when:%a} {when.day} {when:%b}"
    return text if when.year == (today or date.today()).year else f"{text} {when.year}"


def duration(minutes: Optional[float]) -> str:
    """46 → '46 min', 80 → '1 h 20 min', 120 → '2 h'."""
    total = round(minutes or 0)
    hours, mins = divmod(total, 60)
    if not hours:
        return f"{mins} min"
    return f"{hours} h {mins} min" if mins else f"{hours} h"


# --- the build's steps (progress page) -------------------------------------------------------------

# The steps src/playlist.py reports, in order, as they start; the last two end with the service's name
# ("Finding the songs on Spotify"). "Asking for a few more songs" happens inside step 4, and the question
# asked before building (Phase 30) inside step 2.
BUILD_STEPS = [
    "Reading your taste",
    "Reading your prompt",
    "Asking for song ideas",
    "Finding the songs on {service}",
    "Saving the playlist to your {service}",
]
READ, FIND = 1, 3
ASKING = "Checking one thing with you"  # src/clarify.py STEP
COUNT = re.compile(r"\((\d+)/(\d+)\)")


def build_steps(step: Optional[str], failed: bool = False, service: str = "Spotify", asking: bool = True) -> list[dict]:
    """Every step with its state (done, now, wait or failed) and the words that say so.
    `step` is jobs.step: the text of the step running, or the one a failed build stopped in.
    `service` is the name of the user's service. `asking`: the question on the page is still open (Phase 30)."""
    current = 0
    for i, name in enumerate(BUILD_STEPS):
        if step and step.startswith(name.split(" {", 1)[0]):  # any service's name
            current = i
    if step and step.startswith("Asking for a few more songs"):
        current = FIND
    if step and step.startswith(ASKING):
        current = READ
    count = COUNT.search(step or "") if current == FIND else None

    steps = []
    for i, name in enumerate(step.format(service=service) for step in BUILD_STEPS):
        if i < current:
            state, status = "done", "Done"
        elif i > current:
            state, status = "wait", "Waiting"
        elif failed:
            state, status = "failed", "Stopped here"
        else:
            state, status = "now", "In progress"
        label, percent = name, None
        if i == current and count:
            found, of = int(count.group(1)), int(count.group(2))
            label, percent = f"{name} ({found}/{of})", round(100 * min(found, of) / of) if of else 0
            if failed:
                status = f"Stopped here: {found} of {of} looked up"
        if i == current and step and step.startswith("Asking for a few more songs") and not failed:
            status = "Asking for a few more songs"
        if i == current and step and step.startswith(ASKING) and not failed and asking:
            status = "One question for you, below"
        steps.append({"label": label, "state": state, "status": status, "percent": percent})
    return steps


# --- energy chart (playlist page) ------------------------------------------------------------------

# The chart's frame, in SVG units: the line runs from X0 to X1; energy 10 sits at y=10, 0 at the axis
X0, X1, AXIS, WIDTH, HEIGHT = 8.0, 350.0, 100.0, 358, 118


def _y(energy: int) -> float:
    return AXIS - 9 * max(0, min(10, energy))


def energy_chart(groups: list[dict], total: str, total_ms: int) -> Optional[dict]:
    """The energy line across a playlist with several sections, from _section_groups' output
    (each group has label, start, start_ms, energy and cut). None when there's nothing to draw."""
    if len(groups) < 2 or total_ms <= 0:
        return None
    span = X1 - X0
    xs = [X0 + span * g["start_ms"] / total_ms for g in groups] + [X1]
    ys = [_y(g["energy"]) for g in groups]

    line = [f"M{X0:.1f} {ys[0]:.1f}"]
    for i in range(1, len(groups)):
        x, prev, here = xs[i], ys[i - 1], ys[i]
        if groups[i]["cut"]:
            line.append(f"H{x:.1f}V{here:.1f}")
        else:
            # A short S-curve over at most 16 units, never wider than either section allows
            half = max(0.0, min(8.0, (x - xs[i - 1]) / 2, (xs[i + 1] - x) / 2))
            line.append(f"H{x - half:.1f}C{x:.1f} {prev:.1f} {x:.1f} {here:.1f} {x + half:.1f} {here:.1f}")
    line.append(f"H{X1:.1f}")
    path = "".join(line)

    ticks = [{"x": round(x, 1), "text": g["start"]} for x, g in zip(xs[1:], groups[1:])]
    levels = [{"x": round((xs[i] + xs[i + 1]) / 2, 1), "y": round(max(ys[i] - 8, 9), 1), "text": g["energy"]}
              for i, g in enumerate(groups)]

    parts = [f"{groups[0]['label']} {groups[0]['energy']} of 10"]
    for g in groups[1:]:
        how = "a straight cut to" if g["cut"] else "then"
        parts.append(f"{how} {g['label']} {g['energy']} of 10 from {g['start']}")
    cuts = [g["start"] for g in groups[1:] if g["cut"]]
    minutes = round(total_ms / 60_000)
    caption = f"Energy, 1 to 10, across the {minutes} minutes."
    if cuts:
        caption += f" The change at {' and '.join(cuts)} is a straight cut, as you asked."
    return {
        "width": WIDTH, "height": HEIGHT, "x0": X0, "x1": X1, "axis": AXIS, "y5": _y(5), "y10": _y(10),
        "line": path, "area": f"{path}V{AXIS:.0f}H{X0:.0f}Z",
        "ticks": ticks, "levels": levels, "total": total,
        "label": "Energy across the playlist: " + ", ".join(parts) + ".",
        "caption": caption,
    }


def energy_bars(energy: int) -> list[dict]:
    """The ten rising ticks of a section's energy meter; the first `energy` are filled."""
    return [{"height": round(4 + i * 1.2, 1), "on": i <= energy} for i in range(1, 11)]


# --- onboarding ------------------------------------------------------------------------------------

GENRES = ["Hip-hop", "R&B", "House", "Soul", "Pop", "Indie", "Electronic", "Jazz", "Lo-fi", "Bollywood",
          "Punjabi", "Afrobeats", "Rock", "Classical"]


def split_list(*values: str) -> list[str]:
    """Comma-separated answers (from chips and free text alike) → one list, blanks and repeats dropped."""
    seen, items = set(), []
    for value in values:
        for item in (value or "").split(","):
            item = " ".join(item.split())
            if item and item.lower() not in seen:
                seen.add(item.lower())
                items.append(item)
    return items


def genre_choices(saved: str) -> tuple[list[dict], str]:
    """The genre chips with the saved ones ticked, and whatever didn't match a chip for the
    "Other genres" box: 'indie rock, bollywood' → Bollywood ticked, 'indie rock' left over."""
    picked = {g.lower(): g for g in split_list(saved)}
    chips = [{"name": g, "on": g.lower() in picked} for g in GENRES]
    known = {g.lower() for g in GENRES}
    other = ", ".join(g for key, g in picked.items() if key not in known)
    return chips, other


def artist_fields(saved: str) -> list[str]:
    """Three boxes: the first two saved artists, then the rest together in the third."""
    names = split_list(saved)
    return (names[:2] + [", ".join(names[2:])] + ["", "", ""])[:3]
