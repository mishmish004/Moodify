"""Phase 22: taste dimensions judged per track, and per-person numbers and taste match computed from them."""
import json
import re

import httpx
import pytest

from src import features, library, prompts, taste, users
from src.db import get_conn
from src.features import Feature, Summary
from src.spotify import auth
from src.spotify.client import Track

from .conftest import CHAT_URL
from .test_p6_playlist import user  # noqa: F401


def _tracks(n, prefix="t"):
    return [Track(f"spotify:track:{prefix}{i}", f"Song {prefix}{i}", (f"Artist {i}",), 200_000) for i in range(n)]


class Judge:
    """Answers judging calls: every listed song gets `values` (with "number" filled in), or `status` fails."""

    def __init__(self, respx_mock, status=200, **values):
        self.status, self.calls = status, []
        self.values = {"energy": 5, "mood": 5, "texture": 5, "groove": 5, "vocals": 5, "era": 1990,
                       "genres": ["pop"], "language": "English", **values}
        respx_mock.post(CHAT_URL, json__response_format__json_schema__name="Judgements").mock(side_effect=self)

    def __call__(self, request):
        text = prompts.sent_text(json.loads(request.content)["messages"])
        numbers = [int(n) for n in re.findall(r"^(\d+)\. ", text.split("Songs:\n")[-1], re.M)]
        self.calls.append(numbers)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "nope"}})
        content = json.dumps({"tracks": [{"number": n, **self.values} for n in numbers]})
        return httpx.Response(200, json={"id": "j", "object": "chat.completion", "created": 0, "model": "m", "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}]})


def _feature_rows():
    with get_conn() as conn:
        return {r["uri"]: dict(r) for r in conn.execute("SELECT * FROM track_features")}


# --- judging ------------------------------------------------------------------------------------------------

def test_120_unjudged_tracks_take_3_calls_and_judged_ones_none(user, respx_mock):
    library.replace("friend1", {"liked": _tracks(120)})
    judge = Judge(respx_mock)
    assert features.judge_new() == 120
    assert [len(c) for c in judge.calls] == [50, 50, 20]
    assert features.judge_new() == 0 and len(judge.calls) == 3


def test_only_new_tracks_are_judged(user, respx_mock):
    library.replace("friend1", {"liked": _tracks(10)})
    judge = Judge(respx_mock)
    features.judge_new()
    library.replace("friend1", {"liked": _tracks(10) + _tracks(5, "new")})
    features.judge_new()
    assert [len(c) for c in judge.calls] == [10, 5]


def test_out_of_range_values_are_clamped_and_unknown_fields_ignored(user, respx_mock):
    library.replace("friend1", {"liked": _tracks(2)})
    Judge(respx_mock, energy=14, mood=0, texture=-3, groove=11, vocals=7, era=1987, tempo_bpm=120,
          genres=["Hip Hop", "hip hop", "https://spam.example", "a very long sentence that is not a genre at all"],
          language="  English ")
    features.judge_new()
    row = _feature_rows()["spotify:track:t0"]
    assert (row["energy"], row["mood"], row["texture"], row["groove"], row["vocals"], row["era"]) == (10, 1, 1, 10, 7, 1980)
    assert json.loads(row["genres_json"]) == ["hip hop"] and row["language"] == "English" and "tempo_bpm" not in row


def test_a_failed_batch_leaves_its_tracks_unjudged(user, respx_mock):
    library.replace("friend1", {"liked": _tracks(3)})
    Judge(respx_mock, status=500)
    assert features.judge_new() == 0
    assert _feature_rows() == {} and len(features.unjudged()) == 3


def test_numbers_outside_the_batch_are_ignored(user, respx_mock):
    library.replace("friend1", {"liked": _tracks(2)})
    content = json.dumps({"tracks": [{"number": n, "energy": 5, "mood": 5, "texture": 5, "groove": 5, "vocals": 5, "era": 2000,
                                      "genres": [], "language": "English"} for n in (1, 7, 1)]})
    respx_mock.post(CHAT_URL, json__response_format__json_schema__name="Judgements").respond(200, json={
        "id": "j", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}]})
    assert features.judge_new() == 1 and list(_feature_rows()) == ["spotify:track:t0"]


@pytest.mark.parametrize("raw, clean", [
    (["Hip Hop", "R&B", "k-pop"], ["hip hop", "r&b", "k-pop"]),
    (["", "unknown", "N/A", "123", "lo-fi"], ["lo-fi"]),
    (["jazz", "Jazz", "classical", "house", "techno"], ["jazz", "classical", "house"]),
    (["synth_pop", "drum and bass"], ["synth pop", "drum and bass"]),
    (["this is a whole sentence about the song"], []),
])
def test_clean_genres(raw, clean):
    assert features.clean_genres(raw) == clean


def test_features_go_with_their_track(user, respx_mock):
    library.replace("friend1", {"liked": _tracks(2)})
    Judge(respx_mock)
    features.judge_new()
    library.replace("friend1", {"liked": _tracks(1)})
    assert list(_feature_rows()) == ["spotify:track:t0"]
    users.delete("friend1")
    assert _feature_rows() == {}


def test_a_taste_refresh_queues_judging(user, respx_mock, monkeypatch):
    queued = []
    monkeypatch.setattr(features, "judge_soon", lambda: queued.append(True))
    from .test_p4_taste import _mock_top
    from .conftest import mock_listening
    _mock_top(respx_mock)
    mock_listening(respx_mock)
    taste.profile_for(users.get("friend1"))
    assert queued == [True]


def test_judging_runs_once_at_a_time(user):
    assert features._judging.acquire(blocking=False)
    try:
        assert features.judge_new() == 0  # another run holds the lock
    finally:
        features._judging.release()


# --- per person ---------------------------------------------------------------------------------------------

def _f(weight, energy=5, mood=5, texture=5, groove=5, vocals=5, era=2000, genres=("pop",), uri=None):
    return Feature(uri or f"u{weight}{energy}{era}", weight, energy, mood, texture, groove, vocals, era, tuple(genres), "English")


@pytest.mark.parametrize("rows, mean, spread", [
    ([_f(1, energy=2), _f(1, energy=8)], 5.0, 3.0),
    ([_f(3, energy=2), _f(1, energy=10)], 4.0, 3.4641),
    ([_f(2, energy=7)], 7.0, 0.0),
    ([], None, None),
])
def test_weighted_mean_and_spread(rows, mean, spread):
    got_mean, got_spread = features.mean_and_spread(rows, "energy")
    assert got_mean == (pytest.approx(mean) if mean is not None else None)
    assert got_spread == (pytest.approx(spread, abs=1e-4) if spread is not None else None)


def test_genre_shares_split_each_tracks_weight():
    shares = features.genre_shares([_f(2, genres=("jazz", "soul")), _f(2, genres=("jazz",))])
    assert shares == {"jazz": pytest.approx(0.75), "soul": pytest.approx(0.25)}
    assert features.genre_shares([]) == {}


def test_top_genres_fold_the_rest_into_other():
    shares = {f"g{i}": 1 / 10 for i in range(10)}
    top = features.top_genres(shares)
    assert [g for g, _ in top] == ["g0", "g1", "g2", "g3", "g4", "g5", "Other"]
    assert top[-1][1] == pytest.approx(0.4)
    assert features.top_genres({"jazz": 1.0}) == [("jazz", 1.0)]


def test_decade_histogram():
    assert features.decades([_f(1, era=1990), _f(3, era=2010), _f(1, era=1990)]) == {1990: 0.4, 2010: 0.6}


def test_for_person_uses_library_weights(user, respx_mock):
    a, b = _tracks(2)
    library.replace("friend1", {"top": [a], "liked": [a, b]})
    Judge(respx_mock, energy=9)
    features.judge_new()
    rows = {f.uri: f.weight for f in features.for_person("friend1")}
    assert rows == {a.uri: 4, b.uri: 1}


# --- taste match --------------------------------------------------------------------------------------------

def _summary(rows):
    return features.summarize(rows)


def test_identical_people_match_100():
    rows = [_f(2, energy=8, mood=3, genres=("drill",)), _f(1, energy=4, mood=7, era=1990, genres=("soul", "r&b"))]
    assert features.match(_summary(rows), _summary(rows)) == 100


def test_opposite_people_match_near_0():
    a = _summary([_f(1, energy=10, mood=10, texture=10, groove=10, vocals=10, era=2020, genres=("edm",))])
    b = _summary([_f(1, energy=1, mood=1, texture=1, groove=1, vocals=1, era=1960, genres=("baroque",))])
    assert features.match(a, b) <= 5


def test_match_is_symmetric_and_in_range():
    a = _summary([_f(2, energy=8, genres=("rap",)), _f(1, energy=3, genres=("jazz",))])
    b = _summary([_f(1, energy=6, mood=8, genres=("jazz", "pop"))])
    assert features.match(a, b) == features.match(b, a) and 0 <= features.match(a, b) <= 100


def test_no_judged_tracks_means_no_match():
    assert features.match(_summary([]), _summary([_f(1)])) is None


# --- live ---------------------------------------------------------------------------------------------------

LIVE_TRACKS = {
    "clair": ("Clair de Lune", "Claude Debussy"), "collapse": ("Till I Collapse", "Eminem"),
    "blinding": ("Blinding Lights", "The Weeknd"), "fastcar": ("Fast Car", "Tracy Chapman"),
    "heyya": ("Hey Ya!", "Outkast"), "hurt": ("Hurt", "Johnny Cash"),
    "happy": ("Happy", "Pharrell Williams"), "someone": ("Someone Like You", "Adele"),
    "sandstorm": ("Sandstorm", "Darude"), "blackbird": ("Blackbird", "The Beatles"),
    "gymno": ("Gymnopédie No. 1", "Erik Satie"), "levels": ("Levels", "Avicii"),
}
# (higher, lower, dimension): the first should score higher than the second
LIVE_ORDERINGS = [
    ("collapse", "clair", "energy"), ("sandstorm", "gymno", "energy"), ("heyya", "hurt", "energy"),
    ("blinding", "fastcar", "texture"), ("levels", "blackbird", "texture"), ("sandstorm", "clair", "texture"),
    ("heyya", "hurt", "groove"), ("levels", "gymno", "groove"), ("happy", "someone", "groove"),
    ("happy", "hurt", "mood"), ("heyya", "someone", "mood"), ("happy", "collapse", "mood"),
    ("someone", "clair", "vocals"), ("fastcar", "gymno", "vocals"), ("hurt", "sandstorm", "vocals"),
    ("blinding", "blackbird", "era"), ("levels", "fastcar", "era"), ("happy", "hurt", "era"),
]


@pytest.mark.live
def test_live_obvious_orderings(tmp_path, monkeypatch):
    """Gate. The real model judges 12 well-known tracks; at least 90% of 18 obvious pairwise orderings must hold."""
    from src.core.config import get_settings
    from src.db import init_db
    monkeypatch.setenv("DB_PATH", str(tmp_path / "live.db"))
    get_settings.cache_clear()
    init_db()
    users.upsert("synthetic", "S", "no-token")
    tracks = {key: Track(f"spotify:track:{key}", name, (artist,), 200_000) for key, (name, artist) in LIVE_TRACKS.items()}
    library.replace("synthetic", {"liked": list(tracks.values())})
    assert features.judge_new() == 12
    rows = _feature_rows()
    right = 0
    for high, low, dim in LIVE_ORDERINGS:
        a, b = rows[tracks[high].uri][dim], rows[tracks[low].uri][dim]
        ok = a > b
        right += ok
        print(f"{'ok  ' if ok else 'FAIL'} {dim}: {LIVE_TRACKS[high][0]} {a} > {LIVE_TRACKS[low][0]} {b}")
    for key, track in tracks.items():
        r = rows[track.uri]
        print(f"{track.name}: " + ", ".join(f"{d} {r[d]}" for d in features.DIMENSIONS) + f", {r['genres_json']}, {r['language']}")
    print(f"{right}/{len(LIVE_ORDERINGS)} orderings right")
    assert right >= 0.9 * len(LIVE_ORDERINGS)
