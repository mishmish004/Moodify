import json
import secrets
import sqlite3
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from src import jobs, likes, playlist, playlists, search_cache, taste, users
from src.brief import Brief, rule_list
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
    # Builds run in threads of this process; any still marked running were cut off by a restart
    if interrupted := jobs.fail_interrupted():
        MoodifyLogger.get_logger("jobs").warning("%d build(s) interrupted by a restart", interrupted)
    search_cache.prune()
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
    """Raised by current_user; answered with a redirect to /login. `reason` is shown there."""

    def __init__(self, reason: str = ""):
        super().__init__(reason)
        self.reason = reason


@app.exception_handler(LoginRequired)
def _redirect_to_login(request: Request, exc: LoginRequired) -> RedirectResponse:
    return RedirectResponse(f"/login?reason={exc.reason}" if exc.reason else "/login", status_code=303)


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


def any_user(request: Request) -> sqlite3.Row:
    """The logged-in user, whatever Spotify access they granted. Only /disconnect settles for this."""
    spotify_id = request.session.get("spotify_id")
    user = users.get(spotify_id) if spotify_id else None
    if user is None:
        # No cookie, or the row is gone (disconnected): drop whatever the cookie held
        request.session.clear()
        raise LoginRequired()
    return user


def current_user(request: Request, user: sqlite3.Row = Depends(any_user)) -> sqlite3.Row:
    if auth.missing_scopes(user["granted_scopes"]):
        # Logged in before Moodify asked for more access (Phase 10). One new login grants it; data stays
        request.session.clear()
        raise LoginRequired("scopes")
    return user


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/login")
def login_page(request: Request, reason: str = ""):
    return templates.TemplateResponse(request, "login.html", {"reason": reason})


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
    users.upsert(profile["id"], profile.get("display_name"), tokens.refresh_token, tokens.scope)
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
def home(request: Request, user: sqlite3.Row = Depends(onboarded_user), redo: Optional[int] = None):
    # "Adjust and redo" on a playlist page comes back here with that playlist's prompt in the box
    again = playlists.get(redo, user["spotify_id"]) if redo else None
    return templates.TemplateResponse(request, "index.html", {
        "user": user, "summary": taste.summary_for(user), "recent": playlists.recent(user["spotify_id"]),
        "prompt": again["prompt"] if again else "",
    })


# How a failed build is explained, by jobs.error
BUILD_FAILURES = {
    jobs.NO_SUGGESTIONS: ("No song ideas this time",
                          "The AI model didn't answer, even on a second try. Nothing was created. Try again in a minute."),
    jobs.NO_MATCHES: ("Nothing matched",
                      "None of the suggested songs could be found on Spotify. Try describing the mood differently."),
    jobs.QUOTA: ("Spotify is asking us to slow down",
                 "The app's Spotify quota is used up for now. Try again in an hour or so."),
    jobs.INTERRUPTED: ("Interrupted", "Moodify restarted while making this playlist. Nothing was saved. Try again."),
    jobs.ERROR: ("Something went wrong", "The playlist couldn't be made, and nothing was saved. Try again."),
}


@app.post("/playlist")
def make_playlist(user: sqlite3.Row = Depends(onboarded_user), mood: str = Form(..., min_length=1, max_length=2000)):
    # One build at a time per user: a second submit lands on the build already running
    job = jobs.running_for(user["spotify_id"])
    job_id = job["id"] if job else jobs.create(user["spotify_id"], mood)
    if job is None:
        jobs.start(playlist.run, job_id, user["spotify_id"], mood)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.get("/jobs/{job_id}")
def job_page(request: Request, job_id: int, user: sqlite3.Row = Depends(current_user)):
    job = jobs.get(job_id, user["spotify_id"])
    if job is None:
        return _message(request, "Not found", "That build isn't yours or doesn't exist.", status_code=404, user=user)
    if job["status"] == "done":
        return RedirectResponse(f"/playlists/{job['playlist_id']}", status_code=303)
    if job["status"] == "failed":
        if job["error"] == jobs.REAUTH:
            request.session.clear()
            return RedirectResponse("/login", status_code=303)
        title, text = BUILD_FAILURES.get(job["error"], BUILD_FAILURES[jobs.ERROR])
        return _message(request, title, text, user=user)
    return templates.TemplateResponse(request, "job.html", {"user": user, "job": job})


@app.get("/playlists/{playlist_id}")
def playlist_page(request: Request, playlist_id: int, user: sqlite3.Row = Depends(current_user)):
    row = playlists.get(playlist_id, user["spotify_id"])
    if row is None:
        return _message(request, "Not found", "That playlist isn't yours or doesn't exist.", status_code=404, user=user)
    brief = Brief.model_validate_json(row["brief_json"]) if row["brief_json"] else None
    tracks = playlists.tracks(playlist_id)
    return templates.TemplateResponse(request, "playlist.html", {
        "user": user, "playlist": row, "tracks": tracks, "brief": brief,
        "liked": likes.liked_uris(user["spotify_id"], [t.uri for t in tracks]),
        "notice": request.session.pop("notice", None),
        "rules": rule_list(brief.hard_rules) if brief else [],
        "total_minutes": round(sum(t.duration_ms for t in tracks) / 60_000),
        "section_minutes": playlists.section_minutes(playlist_id),
        "groups": _section_groups(brief, tracks, playlists.track_sections(playlist_id)),
    })


def _section_groups(brief: Optional[Brief], tracks: list, sections: list[Optional[int]]) -> list[dict]:
    """Tracks under their section's heading with its start time, for playlists with several sections."""
    if brief is None or len(brief.sections) < 2 or None in sections:
        return []
    groups, elapsed = [], 0
    for position, (track, section) in enumerate(zip(tracks, sections), start=1):
        if not groups or groups[-1]["section"] != section:
            groups.append({"section": section, "label": brief.sections[section].label, "start": _clock(elapsed),
                           "first": position, "tracks": []})
        groups[-1]["tracks"].append(track)
        elapsed += track.duration_ms
    return groups


def _clock(ms: int) -> str:
    """14:20, or 1:04:20 past an hour."""
    seconds = ms // 1000
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours}:{minutes:02d}:{seconds % 60:02d}" if hours else f"{minutes}:{seconds % 60:02d}"


# --- likes and ratings (Phase 14) ------------------------------------------------------------------
# Plain forms that post and redirect back, so they work without JavaScript. static/feedback.js sends the
# same posts with "Accept: application/json" and gets JSON instead, so the page doesn't reload.

def _wants_json(request: Request) -> bool:
    return request.headers.get("accept", "").startswith("application/json")


def _feedback_answer(request: Request, back: str, notice: Optional[str] = None, **state):
    if _wants_json(request):
        return JSONResponse({**state, "notice": notice})
    if notice:
        request.session["notice"] = notice  # shown once, on the page we go back to
    return RedirectResponse(back, status_code=303)


def _feedback_not_found(request: Request, user: sqlite3.Row):
    if _wants_json(request):
        return JSONResponse({"error": "not found"}, status_code=404)
    return _message(request, "Not found", "That playlist isn't yours or doesn't exist.", status_code=404, user=user)


@app.post("/playlists/{playlist_id}/tracks/{position}/{action}")
def like_track(request: Request, playlist_id: int, position: int, action: Literal["like", "unlike"],
               user: sqlite3.Row = Depends(current_user)):
    # position is the track's place in playlist_tracks (0-based); the owner check comes first
    track = playlists.track_at(playlist_id, position) if playlists.get(playlist_id, user["spotify_id"]) else None
    if track is None:
        return _feedback_not_found(request, user)
    if action == "like":
        outcome = likes.like(user, track["uri"], playlist_id)
    else:
        outcome = likes.unlike(user, track["uri"])
    return _feedback_answer(request, f"/playlists/{playlist_id}#t{position}", likes.notice(action, outcome),
                            liked=action == "like")


@app.post("/playlists/{playlist_id}/rating")
def rate_playlist(request: Request, playlist_id: int, rating: int = Form(..., ge=1, le=10),
                  user: sqlite3.Row = Depends(current_user)):
    if not playlists.set_rating(playlist_id, user["spotify_id"], rating):
        return _feedback_not_found(request, user)
    return _feedback_answer(request, f"/playlists/{playlist_id}#rating", rating=rating)


@app.get("/history")
def history(request: Request, user: sqlite3.Row = Depends(current_user), sort: Literal["new", "rating"] = "new"):
    return templates.TemplateResponse(request, "history.html", {
        "user": user, "rows": playlists.recent(user["spotify_id"], limit=-1, order=sort), "sort": sort,
    })


@app.get("/settings")
def settings_page(request: Request, user: sqlite3.Row = Depends(current_user)):
    return templates.TemplateResponse(request, "settings.html", {
        "user": user, "notice": request.session.pop("notice", None),
    })


@app.post("/settings")
def settings_submit(request: Request, user: sqlite3.Row = Depends(current_user),
                    likes_to_spotify: Optional[str] = Form(None)) -> RedirectResponse:
    # An unticked checkbox sends nothing at all
    users.set_likes_to_spotify(user["spotify_id"], likes_to_spotify is not None)
    request.session["notice"] = "Saved."
    return RedirectResponse("/settings", status_code=303)


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
    familiar: Literal["new", "mix", "known"] = Form(taste.DEFAULT_FAMILIAR),
) -> RedirectResponse:
    answers = {
        "genres": genres.strip(), "artists": artists.strip(), "energy": energy,
        "instrumental": instrumental, "languages": languages.strip(), "avoid": avoid.strip(), "familiar": familiar,
    }
    users.set_onboarding(user["spotify_id"], answers)
    return RedirectResponse("/", status_code=303)


@app.post("/disconnect")
def disconnect(request: Request, user: sqlite3.Row = Depends(any_user)):
    # Spotify requires deleting a user's data within 5 days of disconnect; we do it now
    users.delete(user["spotify_id"])
    request.session.clear()
    return _message(request, "Disconnected",
                    "Your Moodify data is deleted. To revoke Spotify access as well,"
                    " remove Moodify at https://www.spotify.com/account/apps/")
