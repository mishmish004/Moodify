"""
Phase 27: Google sign-in for YouTube Music: authorization code with PKCE and the client secret, refresh, revoke.

Google's web clients need the client secret even with PKCE. The ID token says who signed in: its `sub` is the
account id. It comes straight from Google's token endpoint over HTTPS in exchange for the client secret, which
Google's OpenID Connect guide says is enough to trust it without checking its signature.
"""
import base64
import json
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from src.core.config import get_settings
from src.core.exceptions import YouTubeError, YouTubeReauthRequired
from src.spotify.auth import code_challenge, new_verifier  # noqa: F401 (PKCE is the same everywhere; main uses new_verifier)

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube"
# openid + profile say who it is; the youtube scope reads likes, playlists and subscriptions, makes playlists, likes
SCOPES = f"openid profile {YOUTUBE_SCOPE}"
TIMEOUT = 10.0


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: str = ""                       # what the user granted, space-separated (Google may let them untick some)
    claims: dict = field(default_factory=dict)  # from the ID token: sub, name, ...


def missing_scopes(granted: str | None) -> set[str]:
    """The one scope Moodify can't work without, if the login didn't grant it. Google returns `profile` as
    .../auth/userinfo.profile, so only the YouTube scope is compared."""
    return {YOUTUBE_SCOPE} - set((granted or "").split())


def authorize_url(state: str, verifier: str) -> str:
    settings = get_settings()
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "S256",
        # A refresh token, every time: Google only sends one with consent, and Moodify acts while the user is away
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(code: str, verifier: str) -> Tokens:
    settings = get_settings()
    data = _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "code_verifier": verifier,
    })
    return Tokens(data["access_token"], data.get("refresh_token", ""), int(data["expires_in"]), data.get("scope", ""),
                  claims(data.get("id_token", "")))


def refresh(refresh_token: str) -> Tokens:
    """New access token. Raises YouTubeReauthRequired when Google rejects the refresh token."""
    data = _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
    return Tokens(data["access_token"], data.get("refresh_token") or refresh_token, int(data["expires_in"]))


def revoke(token: str) -> bool:
    """Take back Moodify's access (YouTube API policy III.D.2.3.a). Best effort: False if Google didn't confirm."""
    try:
        resp = httpx.post(REVOKE_URL, data={"token": token}, timeout=TIMEOUT)
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


def claims(id_token: str) -> dict:
    """The payload of an ID token received from TOKEN_URL (see the module docstring)."""
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}


def _token_request(form: dict) -> dict:
    settings = get_settings()
    form = {**form, "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET.get_secret_value() if settings.GOOGLE_CLIENT_SECRET else ""}
    try:
        resp = httpx.post(TOKEN_URL, data=form, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise YouTubeError(0, repr(exc)) from exc
    if resp.status_code != 200:
        if resp.status_code == 400 and _oauth_error(resp.text) == "invalid_grant":
            raise YouTubeReauthRequired(resp.status_code, resp.text)
        raise YouTubeError(resp.status_code, resp.text)
    return resp.json()


def _oauth_error(body: str) -> str:
    try:
        parsed = json.loads(body)
    except ValueError:
        return ""
    return parsed.get("error", "") if isinstance(parsed, dict) else ""
