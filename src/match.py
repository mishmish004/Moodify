"""
Pick the Spotify track that is the song the LLM suggested, out of ≤10 search candidates.

Plain normalization, no fuzzy-matching library: the search already did the
fuzzy part, this step only rejects candidates that are a different song.
"""
import re
import unicodedata
from typing import Optional

from src.spotify.client import Track
from src.suggest import Song

_FEAT = re.compile(r"\s+(feat\.?|ft\.?|featuring)\s.*$")
_PUNCT = re.compile(r"[^\w\s]")


def normalize_title(title: str) -> str:
    """'Song - Remastered 2011', 'Song (feat. X)', 'SONG!' all become 'song'."""
    title = title.split(" - ", 1)[0]
    # Trailing bracketed qualifiers only: a title can start with one, e.g. "(I Can't Get No) Satisfaction"
    title = re.split(r"\s+[\(\[]", title, maxsplit=1)[0]
    return _clean(_FEAT.sub("", title))


def normalize_artist(artist: str) -> str:
    return _clean(_FEAT.sub("", artist))


def listed_artists(artist: str) -> list[str]:
    """The LLM sometimes names every artist, "Masego, FKJ". Keep the whole string too: some single
    names contain a comma ("Tyler, The Creator")."""
    parts = [p.strip() for p in _FEAT.sub("", artist).split(",")]
    return list(dict.fromkeys([artist.strip(), *[p for p in parts if p]]))


def primary_artist(artist: str) -> str:
    """What to search for: the first listed artist. Spotify's artist: filter finds nothing for a list."""
    names = listed_artists(artist)
    return names[1] if len(names) > 1 else names[0]


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))  # "Björk" == "Bjork"
    text = _PUNCT.sub("", text.lower().replace("&", " and "))
    return " ".join(text.split())


def pick(song: Song, candidates: list[Track]) -> Optional[str]:
    """URI of the first candidate with the same title and one of the suggested artists among its artists."""
    want_title = normalize_title(song.title)
    want_artists = {normalize_artist(a) for a in listed_artists(song.artist)} - {""}
    if not want_title or not want_artists:
        return None
    for track in candidates:
        if normalize_title(track.name) == want_title and any(normalize_artist(a) in want_artists for a in track.artists):
            return track.uri
    return None
