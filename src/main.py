import secrets
import sqlite3
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from src import users
from src.core.config import get_settings
from src.core.exceptions import SpotifyError
from src.core.logger import MoodifyLogger
from src.db import init_db
from src.spotify import auth

SESSION_MAX_AGE = 30 * 24 * 60 * 60  # 30 days


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Settings are validated here, so a bad .env stops startup with the field name
    init_db(get_settings().DB_PATH)
    yield


class _Sessions:
    """Signed-cookie sessions, built on first use so importing this module never reads settings."""

    def __init__(self, app):
        self.app = app
        self.inner = None

    async def __call__(self, scope, receive, send):
        if self.inner is None:
            settings = get_settings()
            self.inner = SessionMiddleware(
                self.app,
                secret_key=settings.SECRET_KEY.get_secret_value(),
                session_cookie="moodify_session",
                max_age=SESSION_MAX_AGE,
                # lax still sends the cookie on the top-level redirect back from Spotify
                same_site="lax",
                https_only=settings.ENV == "prod",
            )
        await self.inner(scope, receive, send)


app = FastAPI(title="Moodify", lifespan=lifespan)
app.add_middleware(_Sessions)


class LoginRequired(Exception):
    """Raised by current_user; answered with a redirect to /login."""


@app.exception_handler(LoginRequired)
def _redirect_to_login(request: Request, exc: LoginRequired) -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


def current_user(request: Request) -> sqlite3.Row:
    spotify_id = request.session.get("spotify_id")
    user = users.get(spotify_id) if spotify_id else None
    if user is None:
        # No cookie, or the row is gone (disconnected): drop whatever the cookie held
        request.session.clear()
        raise LoginRequired()
    return user


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/login")
def login(request: Request) -> RedirectResponse:
    state, verifier = secrets.token_urlsafe(16), auth.new_verifier()
    request.session["oauth"] = {"state": state, "verifier": verifier}
    return RedirectResponse(auth.authorize_url(state, verifier), status_code=303)


@app.get("/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    oauth = request.session.pop("oauth", None)  # one use only, even when it fails
    if error:
        return PlainTextResponse("Spotify login was cancelled or refused. Go to /login to try again.", status_code=400)
    if not oauth or not code or not secrets.compare_digest(state.encode(), oauth["state"].encode()):
        return PlainTextResponse("This login link is stale or didn't start here. Go to /login to try again.", status_code=400)
    try:
        tokens = auth.exchange_code(code, oauth["verifier"])
        profile = auth.fetch_profile(tokens.access_token)
    except SpotifyError as exc:
        MoodifyLogger.get_logger("auth").warning("login failed: %s", exc)
        if exc.status == 403:
            return PlainTextResponse(
                "Your Spotify account isn't on this app's allowlist yet. Ask the owner to add you, then try /login again.",
                status_code=403,
            )
        return PlainTextResponse("Spotify login failed. Go to /login to try again.", status_code=502)
    users.upsert(profile["id"], profile.get("display_name"), tokens.refresh_token)
    request.session["spotify_id"] = profile["id"]
    return RedirectResponse("/", status_code=303)


@app.get("/")
def home(user: sqlite3.Row = Depends(current_user)) -> PlainTextResponse:
    # Placeholder: Phase 4 sends new users to onboarding, Phase 7 renders the real page
    return PlainTextResponse(f"Logged in as {user['display_name'] or user['spotify_id']}.")


@app.post("/disconnect")
def disconnect(request: Request, user: sqlite3.Row = Depends(current_user)) -> PlainTextResponse:
    # Spotify requires deleting a user's data within 5 days of disconnect; we do it now
    users.delete(user["spotify_id"])
    request.session.clear()
    return PlainTextResponse(
        "Disconnected. Your Moodify data is deleted. To revoke Spotify access as well,"
        " remove Moodify at https://www.spotify.com/account/apps/"
    )
