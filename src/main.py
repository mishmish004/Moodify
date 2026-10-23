import json
import secrets
import sqlite3
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from src import playlist, playlists, taste, users
from src.core.config import PROJECT_ROOT, get_settings
from src.core.exceptions import ReauthRequired, SpotifyError, SpotifyQuotaExceeded
from src.core.logger import MoodifyLogger
from src.db import init_db
from src.spotify import auth
from src.spotify.client import SpotifyClient

SESSION_MAX_AGE = 30 * 24 * 60 * 60  # 30 days
templates = Jinja2Templates(directory=PROJECT_ROOT / "templates")


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
app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "static"), name="static")


class LoginRequired(Exception):
    """Raised by current_user; answered with a redirect to /login."""


@app.exception_handler(LoginRequired)
def _redirect_to_login(request: Request, exc: LoginRequired) -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(ReauthRequired)
def _reauth(request: Request, exc: ReauthRequired) -> RedirectResponse:
    # The stored refresh token is dead (revoked, or past 6 months): a fresh login replaces it
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.exception_handler(SpotifyQuotaExceeded)
def _quota(request: Request, exc: SpotifyQuotaExceeded):
    MoodifyLogger.get_logger("spotify").warning("quota/rate limit hit: %s", exc)
    return _message(request, "Spotify is asking us to slow down",
                    "The app's Spotify quota is used up for now. Try again in an hour or so.")


def _message(request: Request, title: str, text: str, status_code: int = 200, **context):
    """A plain page with a heading and a sentence, for every outcome that isn't the normal page."""
    return templates.TemplateResponse(
        request, "message.html", {"title": title, "text": text, **context}, status_code=status_code
    )


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
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html")


@app.post("/login")
def login(request: Request) -> RedirectResponse:
    state, verifier = secrets.token_urlsafe(16), auth.new_verifier()
    request.session["oauth"] = {"state": state, "verifier": verifier}
    return RedirectResponse(auth.authorize_url(state, verifier), status_code=303)


@app.get("/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    oauth = request.session.pop("oauth", None)  # one use only, even when it fails
    if error:
        return _message(request, "Login cancelled", "Spotify login was cancelled or refused.", status_code=400)
    if not oauth or not code or not secrets.compare_digest(state.encode(), oauth["state"].encode()):
        return _message(request, "Login expired", "This login link is stale or didn't start here. Please log in again.",
                        status_code=400)
    try:
        tokens = auth.exchange_code(code, oauth["verifier"])
        profile = auth.fetch_profile(tokens.access_token)
    except SpotifyError as exc:
        MoodifyLogger.get_logger("auth").warning("login failed: %s", exc)
        if exc.status == 403:
            return _message(request, "Not on the list yet",
                            "Your Spotify account isn't on this app's allowlist yet. Ask the owner to add you, then log in again.",
                            status_code=403)
        return _message(request, "Login failed", "Spotify login failed. Please try again.", status_code=502)
    users.upsert(profile["id"], profile.get("display_name"), tokens.refresh_token)
    request.session["spotify_id"] = profile["id"]
    return RedirectResponse("/", status_code=303)


def onboarded_user(user: sqlite3.Row = Depends(current_user)) -> sqlite3.Row:
    if not user["onboarding_json"]:
        raise OnboardingRequired()
    return user


class OnboardingRequired(Exception):
    pass


@app.exception_handler(OnboardingRequired)
def _redirect_to_onboarding(request: Request, exc: OnboardingRequired) -> RedirectResponse:
    return RedirectResponse("/onboarding", status_code=303)


@app.get("/")
def home(request: Request, user: sqlite3.Row = Depends(onboarded_user)):
    return templates.TemplateResponse(request, "index.html", {
        "user": user, "summary": taste.summary_for(user), "recent": playlists.recent(user["spotify_id"]),
    })


@app.post("/playlist")
def make_playlist(request: Request, user: sqlite3.Row = Depends(onboarded_user), mood: str = Form(..., min_length=1, max_length=300)):
    result = playlist.build(user, mood)
    if result is None:
        return _message(request, "Nothing matched",
                        "None of the suggested songs could be found on Spotify. Try describing the mood differently.")
    return templates.TemplateResponse(request, "playlist.html", {"user": user, "result": result})


@app.get("/history")
def history(request: Request, user: sqlite3.Row = Depends(current_user)):
    return templates.TemplateResponse(request, "history.html", {
        "user": user, "rows": playlists.recent(user["spotify_id"], limit=-1),
    })


@app.post("/queue/{playlist_id}")
def queue_playlist(request: Request, playlist_id: int, user: sqlite3.Row = Depends(onboarded_user)):
    row = playlists.get(playlist_id, user["spotify_id"])
    if row is None:
        return _message(request, "Not found", "That playlist isn't yours or doesn't exist.", status_code=404, user=user)
    try:
        with SpotifyClient.for_user(user) as sp:
            for uri in json.loads(row["track_uris_json"]):
                sp.queue(uri)
    except SpotifyError as exc:
        if exc.status in (403, 404):
            return _message(request, "Can't queue", "Queuing needs Spotify Premium and Spotify open on one of your devices.",
                            user=user)
        raise
    return _message(request, "Queued", "The tracks are in your queue. Open Spotify to listen.",
                    user=user, spotify_url=row["spotify_url"])


@app.get("/onboarding")
def onboarding_form(request: Request, user: sqlite3.Row = Depends(current_user)):
    answers = json.loads(user["onboarding_json"] or "{}")
    return templates.TemplateResponse(request, "onboarding.html", {"user": user, "answers": answers})


@app.post("/onboarding")
def onboarding_submit(
    user: sqlite3.Row = Depends(current_user),
    genres: str = Form(..., min_length=1, max_length=200),
    artists: str = Form(..., min_length=1, max_length=200),
    energy: Literal["low", "medium", "high", "mixed"] = Form(...),
    instrumental: Literal["yes", "no"] = Form("yes"),
    languages: str = Form("", max_length=200),
    avoid: str = Form("", max_length=200),
) -> RedirectResponse:
    answers = {
        "genres": genres.strip(), "artists": artists.strip(), "energy": energy,
        "instrumental": instrumental, "languages": languages.strip(), "avoid": avoid.strip(),
    }
    users.set_onboarding(user["spotify_id"], answers)
    return RedirectResponse("/", status_code=303)


@app.post("/disconnect")
def disconnect(request: Request, user: sqlite3.Row = Depends(current_user)):
    # Spotify requires deleting a user's data within 5 days of disconnect; we do it now
    users.delete(user["spotify_id"])
    request.session.clear()
    return _message(request, "Disconnected",
                    "Your Moodify data is deleted. To revoke Spotify access as well,"
                    " remove Moodify at https://www.spotify.com/account/apps/")
