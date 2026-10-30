"""
Spotify login: Authorization Code with PKCE, plus token refresh.

PKCE replaces the client secret: /login makes a random verifier, sends Spotify
its SHA-256 (the challenge), and /callback proves it started the login by
sending the verifier itself.
"""
import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import httpx

from src.core.config import get_settings
from src.core.exceptions import SpotifyError, SpotifyReauthRequired

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
ME_URL = "https://api.spotify.com/v1/me"
SCOPES = (
    "user-top-read user-read-private playlist-modify-public playlist-modify-private user-modify-playback-state"
    # Phase 10: listening history, Liked Songs, follows, own playlists; liking (Phase 14) and devices (Phase 17).
    # Asked for together so friends re-consent once
    " user-read-recently-played user-library-read user-library-modify user-follow-read playlist-read-private"
    " user-read-playback-state"
)
TIMEOUT = 10.0


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str = ""  # what the user granted, space-separated


def missing_scopes(granted: str | None) -> set[str]:
    """Scopes Moodify asks for that this user's login didn't grant (all of them if unknown)."""
    return set(SCOPES.split()) - set((granted or "").split())


def new_verifier() -> str:
    """PKCE code verifier: 86 characters from the URL-safe alphabet (the spec allows 43-128)."""
    return secrets.token_urlsafe(64)


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(state: str, verifier: str, client_id: Optional[str] = None) -> str:
    """`client_id`: the Spotify app to log in through (SPOTIFY_EXTRA_CLIENT_IDS). None: SPOTIFY_CLIENT_ID."""
    settings = get_settings()
    params = {
        "client_id": client_id or settings.SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": settings.SPOTIFY_REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge(verifier),
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(code: str, verifier: str, client_id: Optional[str] = None) -> Tokens:
    settings = get_settings()
    data = _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.SPOTIFY_REDIRECT_URI,
        "client_id": client_id or settings.SPOTIFY_CLIENT_ID,
        "code_verifier": verifier,
    })
    return Tokens(data["access_token"], data["refresh_token"], int(data["expires_in"]), data.get("scope", ""))


def refresh(refresh_token: str, client_id: Optional[str] = None) -> Tokens:
    """New access token. Raises SpotifyReauthRequired when Spotify rejects the refresh token.
    `client_id` must be the app the user logged in through: a refresh token only works with its own app."""
    data = _token_request({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id or get_settings().SPOTIFY_CLIENT_ID,
    })
    # Spotify sometimes rotates the refresh token; keep the old one when it doesn't
    return Tokens(data["access_token"], data.get("refresh_token") or refresh_token, int(data["expires_in"]))


def fetch_profile(access_token: str) -> dict:
    """GET /me: {"id", "display_name", ...}. Dev Mode answers 403 for accounts not on the app's allowlist."""
    return _send("GET", ME_URL, headers={"Authorization": f"Bearer {access_token}"})


def _token_request(form: dict) -> dict:
    try:
        return _send("POST", TOKEN_URL, data=form)
    except SpotifyError as exc:
        if exc.status == 400 and _oauth_error(exc.body) == "invalid_grant":
            raise SpotifyReauthRequired(exc.status, exc.body) from exc
        raise


def _oauth_error(body: str) -> str:
    """The "error" field of an OAuth error response, or "" when the body isn't one."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return ""
    return parsed.get("error", "") if isinstance(parsed, dict) else ""


def _send(method: str, url: str, **kwargs) -> dict:
    try:
        resp = httpx.request(method, url, timeout=TIMEOUT, **kwargs)
    except httpx.HTTPError as exc:
        raise SpotifyError(0, repr(exc)) from exc
    if resp.status_code != 200:
        raise SpotifyError(resp.status_code, resp.text)
    return resp.json()
