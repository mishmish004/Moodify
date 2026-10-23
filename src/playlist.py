"""
mood -> a real playlist in the user's Spotify account.

suggest (LLM) -> search each song on Spotify -> pick the matching track
-> dedupe, stop at MAX_TRACKS -> create playlist -> add items -> save a row.
If fewer than MIN_TRACKS matched, the LLM is asked once more for extra songs.
"""
from dataclasses import dataclass

from src import playlists, taste
from src.core.logger import MoodifyLogger
from src.match import pick
from src.spotify.client import SpotifyClient, Track
from src.suggest import Song, clean_mood, suggest_songs

MAX_TRACKS = 25
MIN_TRACKS = 8
EXTRA_SONGS = 15
NAME_CHARS = 60  # how much of the mood goes into the playlist name


@dataclass(frozen=True)
class Result:
    id: int        # playlists row
    url: str       # open.spotify.com link
    mood: str
    tracks: list[Track]


def build(user, mood: str) -> Result | None:
    """None when nothing could be matched: no playlist is created and nothing is stored.
    SpotifyQuotaExceeded and ReauthRequired propagate to the route."""
    mood = clean_mood(mood)
    summary = taste.summary_for(user)
    songs = suggest_songs(mood, summary)
    with SpotifyClient.for_user(user) as sp:
        tracks = _resolve(sp, songs)
        if len(tracks) < MIN_TRACKS:
            extra = suggest_songs(mood, summary, n=EXTRA_SONGS, exclude=songs)
            tracks += _resolve(sp, extra, seen={t.uri for t in tracks}, room=MAX_TRACKS - len(tracks))
        if not tracks:
            return None
        playlist = sp.create_playlist(f"Moodify: {mood[:NAME_CHARS]}", f"Made by Moodify for: {mood}")
        sp.add_items(playlist.id, [t.uri for t in tracks])
    row_id = playlists.insert(user["spotify_id"], mood, playlist.url, [t.uri for t in tracks])
    MoodifyLogger.get_logger("playlist").info(
        "%s: %d/%d suggestions matched for %r", user["spotify_id"], len(tracks), len(songs), mood
    )
    return Result(row_id, playlist.url, mood, tracks)


def _resolve(sp: SpotifyClient, songs: list[Song], seen: set[str] | None = None, room: int = MAX_TRACKS) -> list[Track]:
    """Search each suggestion and keep the matching track. Stops once `room` tracks are found,
    so a good first batch costs at most MAX_TRACKS searches."""
    seen = set() if seen is None else seen
    tracks: list[Track] = []
    for song in songs:
        if len(tracks) >= room:
            break
        candidates = sp.search_track(song.title, song.artist)
        uri = pick(song, candidates)
        if uri and uri not in seen:
            seen.add(uri)
            tracks.append(next(t for t in candidates if t.uri == uri))
    return tracks
