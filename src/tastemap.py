"""
Phase 23: the taste map's data, for you plus every friend who shows their taste (Phase 18).

Only taste data goes out: names, per-dimension means and spreads, genre shares, decades, library tracks
with their judged dimensions and Spotify links, taste matches and common ground. Never prompts, reviews,
ratings, lessons, or anything from hidden or support-note playlists (the library leaves those out).
Each request logs one "view" per friend shown.
"""
from typing import Optional

from src import features, library, sharing, users

TRACKS_PER_PERSON = 200   # the heaviest, enough to see a shape without drawing 600 dots per panel
COMMON_SHOWN = 5
GENRE_SLOTS = 6           # genres coloured across the page; the rest of everyone's is "Other"


def people(viewer: str) -> list:
    """You first, then friends showing their taste, at most 5 in all (Development Mode's user cap)."""
    me = users.get(viewer)
    return ([me] if me else []) + [users.get(f["spotify_id"]) for f in sharing.friends(viewer, "taste")][:4]


def data(viewer: str) -> dict:
    shown = people(viewer)
    summaries, tracks_by, weights_by = {}, {}, {}
    for person in shown:
        sid = person["spotify_id"]
        rows = features.for_person(sid)
        summaries[sid] = features.summarize(rows)
        weights_by[sid] = {t.uri: (t, w) for t, w in library.for_user(sid)}
        tracks_by[sid] = sorted(rows, key=lambda f: (-f.weight, f.uri))[:TRACKS_PER_PERSON]
    holders: dict[str, int] = {}
    for sid, weights in weights_by.items():
        for uri in weights:
            holders[uri] = holders.get(uri, 0) + 1

    # Genre colours follow the genre, not anyone's rank: the page-wide top genres by summed share
    totals: dict[str, float] = {}
    for summary in summaries.values():
        for genre, share in summary.genres.items():
            totals[genre] = totals.get(genre, 0.0) + share
    coloured = [g for g, _ in sorted(totals.items(), key=lambda item: (-item[1], item[0]))[:GENRE_SLOTS]]

    out_people = []
    for slot, person in enumerate(shown, start=1):
        sid = person["spotify_id"]
        summary = summaries[sid]
        genres = [(g, round(summary.genres.get(g, 0.0), 4)) for g in coloured if summary.genres.get(g)]
        other = round(max(0.0, 1 - sum(v for _, v in genres)), 4) if summary.genres else 0.0
        out_people.append({
            "id": sid, "name": sharing.first_name(person), "me": sid == viewer, "slot": slot,
            "tracks_judged": summary.tracks, "tracks_total": len(weights_by[sid]),
            "profile": {d: {"mean": _round(summary.means[d]), "spread": _round(summary.spreads[d])} for d in features.DIMENSIONS},
            "genres": genres + ([("Other", other)] if other > 0.0005 else []),
            "decades": [[d, round(v, 4)] for d, v in summary.decades.items()],
            "tracks": [_track(f, weights_by[sid][f.uri][0], holders.get(f.uri, 0) > 1) for f in tracks_by[sid]],
        })
    pairs, common = [], []
    for i, a in enumerate(shown):
        for b in shown[i + 1:]:
            pairs.append({"a": a["spotify_id"], "b": b["spotify_id"],
                          "match": features.match(summaries[a["spotify_id"]], summaries[b["spotify_id"]])})
            both = library.common(a["spotify_id"], b["spotify_id"], COMMON_SHOWN)
            common.append({"a": a["spotify_id"], "b": b["spotify_id"], "artists": [name for name, _ in both.artists],
                           "tracks": [{"name": t.name, "artists": list(t.artists), "url": t.url} for t, _ in both.tracks]})
    for person in shown[1:]:
        sharing.log(person["spotify_id"], viewer, "view")
    return {
        "dimensions": [{"key": d, "low": features.LABELS[d][0], "high": features.LABELS[d][1],
                        "min": 1940 if d == "era" else 1, "max": 2020 if d == "era" else 10} for d in features.DIMENSIONS],
        "genres": coloured, "people": out_people, "match": pairs, "common": common,
    }


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 2)


def _track(f: features.Feature, track, common: bool) -> dict:
    return {"name": track.name, "artists": list(track.artists), "url": track.url, "weight": f.weight, "common": common,
            **{d: getattr(f, d) for d in features.DIMENSIONS}, "genres": list(f.genres)}
