"""
Phase 17: links that open the music app on phones. Phase 26: a link or URI says which service it belongs to.

Spotify:
- Android: an intent:// link opens the app directly, and falls back to the web page when it isn't installed.
- iPhone/iPad: open.spotify.com links open the app on a tap (universal links). Opening it without a tap
  (when a playlist is ready) needs the spotify: URI, which raises the system "Open in Spotify?" prompt.
- Desktop: the web link, which hands over to the desktop app when it's installed.
Chrome on Android blocks an app switch that no tap started, so the big OPEN SPOTIFY button stays the sure path.
"""
import re
from typing import Literal, Optional
from urllib.parse import quote, urlsplit

Device = Literal["android", "ios", "desktop"]
KINDS = {"track", "playlist", "album", "artist"}
_ID = re.compile(r"^[A-Za-z0-9]+$")  # Spotify ids are base62

# Spotify's guidelines: when the app may be missing, offer "GET SPOTIFY FREE" with a link to get it
GET_SPOTIFY = {
    "android": "https://play.google.com/store/apps/details?id=com.spotify.music",
    "ios": "https://apps.apple.com/app/spotify-music-and-podcasts/id324684580",
    "desktop": "https://www.spotify.com/download/",
}
GET_APP = {"spotify": GET_SPOTIFY}

# Where each service's web pages live
HOSTS = {"open.spotify.com": "spotify", "music.youtube.com": "youtube", "music.apple.com": "apple"}


def device(user_agent: Optional[str]) -> Device:
    ua = user_agent or ""
    if "Android" in ua:
        return "android"
    if re.search(r"iPhone|iPad|iPod", ua):
        return "ios"
    return "desktop"


def is_phone(user_agent: Optional[str]) -> bool:
    return device(user_agent) != "desktop"


def service_of(link: str) -> Optional[str]:
    """"spotify" for spotify:track:… or an open.spotify.com link, and so on; None for anything else."""
    if link.startswith(("http://", "https://")):
        return HOSTS.get(urlsplit(link).hostname or "")
    service = link.split(":", 1)[0]
    return service if service in set(HOSTS.values()) else None


def track_url(uri: str) -> str:
    """A track's web page from its URI ("<service>:track:<id>")."""
    service, _, rest = uri.partition(":")
    track_id = rest.rsplit(":", 1)[-1]
    if service == "youtube":
        return f"https://music.youtube.com/watch?v={quote(track_id, safe='')}"
    return web_url("track", track_id)


def web_url(kind: str, spotify_id: str) -> str:
    return f"https://open.spotify.com/{kind}/{quote(spotify_id, safe='')}"


def open_link(kind: str, spotify_id: str, user_agent: Optional[str]) -> str:
    """The link to put behind a tap: it opens the app where it can, else Spotify's web player."""
    web = web_url(kind, spotify_id)
    if kind not in KINDS or not _ID.match(spotify_id):
        return web
    if device(user_agent) == "android":
        return (f"intent://{kind}/{spotify_id}#Intent;scheme=spotify;package=com.spotify.music;"
                f"S.browser_fallback_url={quote(web, safe='')};end")
    return web


def auto_open_link(kind: str, spotify_id: str, user_agent: Optional[str]) -> Optional[str]:
    """What a page may open by itself when a playlist is ready, or None (desktop, or an id it can't trust)."""
    if kind not in KINDS or not _ID.match(spotify_id):
        return None
    found = device(user_agent)
    if found == "ios":
        return f"spotify:{kind}:{spotify_id}"
    if found == "android":
        return open_link(kind, spotify_id, user_agent)
    return None


def parse(url: str) -> Optional[tuple[str, str]]:
    """("playlist", "37i9...") from an open.spotify.com URL or a spotify: URI; None for anything else."""
    if url.startswith("spotify:"):
        parts = url.split(":")
        return (parts[1], parts[2]) if len(parts) == 3 else None
    split = urlsplit(url)
    parts = [p for p in split.path.split("/") if p]
    if split.hostname == "open.spotify.com" and len(parts) >= 2 and parts[-2] in KINDS:
        return parts[-2], parts[-1]
    return None


def for_url(url: str, user_agent: Optional[str]) -> str:
    """An open.spotify.com URL as the link for this device. URLs it doesn't recognise are returned as they are."""
    found = parse(url)
    return open_link(*found, user_agent) if found else url
