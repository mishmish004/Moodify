"""
Google sign-in: OpenID Connect's Authorization Code flow with PKCE. Every visitor signs in with
Google before the Spotify login; any Google account gets in.

/signin keeps a random state, nonce and PKCE verifier in the session cookie and sends the user to
Google. /signin/callback trades the code for an ID token with the client secret and the verifier,
and keeps the account's ID and email in the session. Moodify doesn't store them anywhere else.
"""
import base64
import json
import time
from urllib.parse import urlencode, urlsplit

import httpx

from src.core.config import get_settings
from src.core.exceptions import GoogleSignInError
from src.spotify.auth import code_challenge

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
TIMEOUT = 10.0


def redirect_uri() -> str:
    """GOOGLE_REDIRECT_URI, or /signin/callback on the Spotify redirect's host."""
    settings = get_settings()
    if settings.GOOGLE_REDIRECT_URI:
        return settings.GOOGLE_REDIRECT_URI
    spotify = urlsplit(settings.SPOTIFY_REDIRECT_URI)
    return f"{spotify.scheme}://{spotify.netloc}/signin/callback"


def authorize_url(state: str, nonce: str, verifier: str) -> str:
    params = {
        "client_id": get_settings().GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": "openid email",
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge(verifier),
        # Always show the account chooser, so a shared computer can switch accounts
        "prompt": "select_account",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def sign_in(code: str, verifier: str, nonce: str) -> dict:
    """The signed-in account, {"sub", "email"}. Raises GoogleSignInError when Google or the token says no."""
    settings = get_settings()
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(),
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET.get_secret_value(),
        "code_verifier": verifier,
    }
    try:
        resp = httpx.post(TOKEN_URL, data=form, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise GoogleSignInError(f"token request failed: {exc!r}") from exc
    if resp.status_code != 200:
        raise GoogleSignInError(f"Google returned {resp.status_code}: {resp.text[:200]}")

    # The ID token came straight from Google over TLS, in answer to a request made with the client
    # secret, so its signature needs no separate check (OpenID Connect Core 3.1.3.7, item 6). Its claims do.
    try:
        body = resp.json()
    except ValueError:
        body = {}
    claims = _claims(body.get("id_token") if isinstance(body, dict) else None)
    if not (claims.get("iss") in ISSUERS and claims.get("aud") == settings.GOOGLE_CLIENT_ID
            and isinstance(claims.get("exp"), (int, float)) and claims["exp"] > time.time()
            and claims.get("nonce") == nonce and isinstance(claims.get("sub"), str) and claims["sub"]):
        raise GoogleSignInError("ID token rejected (issuer, audience, expiry, nonce or subject)")
    return {"sub": claims["sub"], "email": claims.get("email")}


def _claims(id_token) -> dict:
    """The payload of a JWT, unverified; {} when it isn't one."""
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (AttributeError, IndexError, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}
