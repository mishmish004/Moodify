import hashlib
import json
import secrets
import sqlite3
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Form, Query, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from src import (activity, blend, clarify, jobs, library, likes, links, listen_as, memory, music, pages, playlist,
                 playlists, prompts, reflect, search_cache, sharing, taste, tastemap, users)
from src.brief import Brief, rule_list
from src.core.config import PROJECT_ROOT, get_settings
from src.core.exceptions import QuotaExceeded, ReauthRequired, ServiceError, SpotifyError
from src.core.logger import MoodifyLogger
from src.db import init_db
from src.spotify import auth
from src.youtube import auth as youtube_auth
from src.youtube import quota as youtube_quota

SESSION_MAX_AGE = 30 * 24 * 60 * 60  # 30 days
templates = Jinja2Templates(directory=PROJECT_ROOT / "templates")
templates.env.filters["day"] = pages.day
templates.env.filters["duration"] = pages.duration


@lru_cache(maxsize=64)
def _digest(path: Path, mtime_ns: int) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8]


def _fingerprint(url: str) -> str:
    """/static/style.css -> /static/style.css?v=<8 hex of its contents>. Cloudflare and browsers keep static files
    for hours, so without this a new page could arrive with the old stylesheet or script."""
    path = PROJECT_ROOT / url.lstrip("/")
    return f"{url}?v={_digest(path, path.stat().st_mtime_ns)}"


templates.env.filters["fingerprint"] = _fingerprint


def _user_agent(context) -> str:
    request = context.get("request")
    return request.headers.get("user-agent", "") if request is not None else ""


@pass_context
def _app_link(context, url: str) -> str:
    """Phase 17: a link for the device asking, so a tap on a phone opens the music app."""
    return links.for_url(url, _user_agent(context))


@pass_context
def _get_app(context, service: str = "spotify") -> str:
    return links.GET_APP[service][links.device(_user_agent(context))]


templates.env.filters["app_link"] = _app_link
templates.env.globals["get_app_url"] = _get_app
# Phase 26: pages name the user's service, and mark each link with the service it opens
templates.env.globals["service_info"] = music.info
templates.env.filters["service_of"] = links.service_of


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Settings are validated here, so a bad .env stops startup with the field name
    init_db(get_settings().DB_PATH)
    # Builds run in threads of this process; any still marked running were cut off by a restart
    if interrupted := jobs.fail_interrupted():
        MoodifyLogger.get_logger("jobs").warning("%d build(s) interrupted by a restart", interrupted)
    search_cache.prune()
    memory.expire_unused()
    library.prune()
    youtube_quota.prune()
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


@app.exception_handler(QuotaExceeded)
def _quota(request: Request, exc: QuotaExceeded):
    MoodifyLogger.get_logger("music").warning("quota/rate limit hit: %s", exc)
    title, text = music.quota_words(exc.service_id or "")
    return _message(request, title, text, tone="low")


def _message(request: Request, title: str, text: str, status_code: int = 200, *, tone: Optional[str] = None,
             back: Optional[tuple[str, str]] = None, code: str = "", **context):
    """A plain page with a heading and a sentence, for every outcome that isn't the normal page.
    tone "up" (good news) or "low" sets the picture; back is (label, href), by default the mood box
    or, for someone logged out, the start."""
    if back is None:
        back = ("Back to the mood box", "/") if context.get("user") else ("Back to the start", "/login")
    return templates.TemplateResponse(request, "message.html", {
        "title": title, "text": text, "tone": tone or ("up" if status_code < 400 else "low"),
        "back": back, "code": code, **context,
    }, status_code=status_code)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    """Unknown addresses get a page, not JSON. Other HTTP errors keep FastAPI's answer."""
    if exc.status_code != 404:
        return await http_exception_handler(request, exc)
    user_id = _session_user_id(request) if "session" in request.scope else None
    user = users.get(user_id) if user_id else None
    return _message(request, "Nothing here",
                    "There's no page at this address. It may have moved, or the link has a typo.",
                    status_code=404, code="404 · Not found", user=user)


def _session_user_id(request: Request) -> Optional[str]:
    """The user id the session cookie holds. Cookies from before Phase 25 call it spotify_id: same value."""
    if "spotify_id" in request.session:
        request.session["user_id"] = request.session.pop("spotify_id")
    return request.session.get("user_id")


def any_user(request: Request) -> sqlite3.Row:
    """The logged-in user, whatever access they granted their service. Only /disconnect settles for this."""
    user_id = _session_user_id(request)
    user = users.get(user_id) if user_id else None
    if user is None:
        # No cookie, or the row is gone (disconnected): drop whatever the cookie held
        request.session.clear()
        raise LoginRequired()
    return user


def current_user(request: Request, user: sqlite3.Row = Depends(any_user)) -> sqlite3.Row:
    if music.missing_scopes(user):
        # Logged in before Moodify asked for more access (Phase 10). One new login grants it; data stays
        request.session.clear()
        raise LoginRequired("scopes")
    return user


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/login")
def login_page(request: Request, reason: str = ""):
    return templates.TemplateResponse(request, "login.html", {
        "reason": reason, "youtube": get_settings().youtube_enabled,
    })


@app.post("/login")
def login(request: Request) -> RedirectResponse:
    state, verifier = secrets.token_urlsafe(16), auth.new_verifier()
    request.session["oauth"] = {"state": state, "verifier": verifier, "service": "spotify"}
    return RedirectResponse(auth.authorize_url(state, verifier), status_code=303)


def _oauth_ok(oauth: Optional[dict], service: str, code: str, state: str) -> bool:
    """The callback finishes a login that started here, for this service, and hasn't been used."""
    return bool(oauth and code and oauth.get("service", "spotify") == service
                and secrets.compare_digest(state.encode(), oauth["state"].encode()))


@app.get("/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    oauth = request.session.pop("oauth", None)  # one use only, even when it fails
    if error:
        return _message(request, "Login cancelled",
                        "You didn't finish logging in with Spotify, so nothing changed. You can try again whenever you like.",
                        status_code=400, back=("Try again", "/login"), tone="up")
    if not _oauth_ok(oauth, "spotify", code, state):
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
                            status_code=403, code="Can't log in")
        return _message(request, "Login failed", "Spotify login failed. Please try again.", status_code=502)
    users.upsert(profile["id"], profile.get("display_name"), tokens.refresh_token, tokens.scope)
    request.session["user_id"] = profile["id"]
    return RedirectResponse("/", status_code=303)


# --- YouTube Music (Phase 27): Google sign-in --------------------------------------------------------------

@app.post("/login/youtube")
def login_youtube(request: Request) -> RedirectResponse:
    if not get_settings().youtube_enabled:
        raise StarletteHTTPException(404)
    state, verifier = secrets.token_urlsafe(16), youtube_auth.new_verifier()
    request.session["oauth"] = {"state": state, "verifier": verifier, "service": "youtube"}
    return RedirectResponse(youtube_auth.authorize_url(state, verifier), status_code=303)


@app.get("/callback/youtube")
def callback_youtube(request: Request, code: str = "", state: str = "", error: str = ""):
    oauth = request.session.pop("oauth", None)
    if error:
        return _message(request, "Sign-in cancelled",
                        "You didn't finish signing in with Google, so nothing changed. You can try again whenever you like.",
                        status_code=400, back=("Try again", "/login"), tone="up")
    if not _oauth_ok(oauth, "youtube", code, state):
        return _message(request, "Login expired", "This login link is stale or didn't start here. Please log in again.",
                        status_code=400)
    try:
        tokens = youtube_auth.exchange_code(code, oauth["verifier"])
    except ServiceError as exc:
        MoodifyLogger.get_logger("auth").warning("YouTube sign-in failed: %s", exc)
        return _message(request, "Sign-in failed", "Google sign-in failed. Please try again.", status_code=502)
    if youtube_auth.missing_scopes(tokens.scope):
        # Google lets people untick a permission; without this one Moodify can't read or make anything
        return _message(request, "Moodify needs YouTube access",
                        "To read your likes and save playlists to YouTube Music, Moodify needs the permission to manage"
                        " your YouTube account. Sign in again and leave it ticked.",
                        status_code=403, back=("Try again", "/login"), tone="low")
    user_id = f"youtube:{tokens.claims.get('sub', '')}"
    existing = users.get(user_id)
    refresh_token = tokens.refresh_token or (existing["refresh_token"] if existing else "")
    if not tokens.claims.get("sub") or not refresh_token:
        return _message(request, "Sign-in failed", "Google didn't say who signed in. Please try again.", status_code=502)
    users.upsert(user_id, tokens.claims.get("name"), refresh_token, tokens.scope, service="youtube")
    request.session["user_id"] = user_id
    return RedirectResponse("/", status_code=303)


@app.get("/privacy")
def privacy(request: Request):
    user_id = _session_user_id(request)
    return templates.TemplateResponse(request, "privacy.html", {
        "user": users.get(user_id) if user_id else None, "contact": get_settings().CONTACT_EMAIL,
    })


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
def home(request: Request, user: sqlite3.Row = Depends(onboarded_user), redo: Optional[int] = None,
         again: Optional[int] = None):
    # "Adjust and redo" on a playlist page comes back here with that playlist's prompt in the box,
    # and "Back" after a failed build with what was written for it
    remade = playlists.get(redo, user["user_id"]) if redo else None
    failed = jobs.get(again, user["user_id"]) if again and not remade else None
    prompt = remade["prompt"] if remade else failed["prompt"] if failed else ""
    return templates.TemplateResponse(request, "index.html", {
        "user": user, "summary": taste.summary_for(user), "recent": playlists.recent(user["user_id"]),
        "prompt": prompt, "remaking": _title(remade) if remade else "",
    })


def _title(row: sqlite3.Row) -> str:
    """A playlist as its lists show it: the title Moodify gave it, or the start of the prompt."""
    title = json.loads(row["brief_json"]).get("title") if row["brief_json"] else None
    return f"Moodify: {title}" if title else row["prompt"][:80]


# How a failed build is explained, by jobs.error; {service} is the name of the user's service
BUILD_FAILURES = {
    jobs.NO_SUGGESTIONS: ("No song ideas this time",
                          "The AI model didn't answer, even on a second try. Nothing was created. Try again in a minute."),
    jobs.NO_MATCHES: ("Nothing matched",
                      "None of the suggested songs could be found on {service}. Try describing the mood differently."),
    jobs.UNREACHABLE: ("{service} isn't answering",
                       "Moodify couldn't get through to {service}, so nothing was made. Try again in a few minutes."),
    jobs.NO_CHANNEL: ("Your Google account needs a YouTube channel",
                      "YouTube only lets accounts with a channel own playlists, so nothing was made. Create one at"
                      " youtube.com/create_channel (it can stay empty), then make the playlist again."),
    jobs.INTERRUPTED: ("Interrupted", "Moodify restarted while making this playlist. Nothing was saved. Try again."),
    jobs.ERROR: ("Something went wrong", "The playlist couldn't be made, and nothing was saved. Try again."),
    blend.NOT_SHARED: ("Not shared any more", "Someone you picked switched off sharing before the playlist started."
                                              " Nothing was made."),
}


@app.post("/playlist")
def make_playlist(user: sqlite3.Row = Depends(onboarded_user), mood: str = Form(..., min_length=1, max_length=2000)):
    # One build at a time per user: a second submit lands on the build already running
    job = jobs.running_for(user["user_id"])
    job_id = job["id"] if job else jobs.create(user["user_id"], mood)
    if job is None:
        jobs.start(playlist.run, job_id, user["user_id"], mood)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.get("/jobs/{job_id}")
def job_page(request: Request, job_id: int, user: sqlite3.Row = Depends(current_user)):
    job = jobs.get(job_id, user["user_id"])
    if job is None:
        return _message(request, "Not found", "That build isn't yours or doesn't exist.", status_code=404, user=user, code="404 · Not found")
    if job["status"] == "done":
        # ?ready: the first view after a build, where a phone may open the music app by itself (Phase 17)
        return RedirectResponse(f"/playlists/{job['playlist_id']}?ready=1", status_code=303)
    if job["status"] == "failed":
        if job["error"] == jobs.REAUTH:
            request.session.clear()
            return RedirectResponse("/login", status_code=303)
        service = music.info(user["service"]).name
        if job["error"] == jobs.QUOTA:
            title, text = music.quota_words(user["service"])
        else:
            title, text = (words.format(service=service) for words in BUILD_FAILURES.get(job["error"], BUILD_FAILURES[jobs.ERROR]))
        return templates.TemplateResponse(request, "job.html", {
            "user": user, "job": job, "failed": True, "title": title, "text": text,
            "steps": pages.build_steps(job["step"], failed=True, service=service),
        })
    question = clarify.for_page(job)
    return templates.TemplateResponse(request, "job.html", {
        "user": user, "job": job, "failed": False, "question": question,
        # Without JavaScript the page reloads itself: not while someone is choosing an answer
        "refresh": question["seconds"] + 1 if question and question["open"] else 3,
        "steps": pages.build_steps(job["step"], service=music.info(user["service"]).name,
                                   asking=bool(question and question["open"])),
    })


@app.post("/jobs/{job_id}/answer")
def answer_question(request: Request, job_id: int, user: sqlite3.Row = Depends(current_user),
                    choice: Literal["keep", "this_time", "from_now_on"] = Form(...)):
    """The answer to the question a build asked before going on (Phase 30). Too late, or already answered:
    back to the build, which has gone on without it."""
    if jobs.get(job_id, user["user_id"]) is None:
        return _message(request, "Not found", "That build isn't yours or doesn't exist.", status_code=404, user=user,
                        code="404 · Not found")
    clarify.answer(job_id, user["user_id"], choice)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.get("/playlists/{playlist_id}")
def playlist_page(request: Request, playlist_id: int, user: sqlite3.Row = Depends(current_user), ready: int = 0):
    row = playlists.get(playlist_id, user["user_id"])
    if row is None:
        return _message(request, "Not found", "That playlist isn't yours or doesn't exist.", status_code=404, user=user,
                        code="404 · Not found")
    auto_open = (links.auto_open_for_url(row["url"], request.headers.get("user-agent"))
                 if ready and user["open_app"] else None)
    brief = Brief.model_validate_json(row["brief_json"]) if row["brief_json"] else None
    tracks = playlists.tracks(playlist_id)
    total_ms = sum(t.duration_ms for t in tracks)
    groups = _section_groups(brief, tracks, playlists.track_sections(playlist_id))
    return templates.TemplateResponse(request, "playlist.html", {
        "user": user, "playlist": row, "tracks": tracks, "brief": brief,
        "liked": likes.liked_uris(user["user_id"], [t.uri for t in tracks]),
        "notice": request.session.pop("notice", None),
        "rules": rule_list(brief.hard_rules) if brief else [],
        "total_minutes": round(total_ms / 60_000),
        "section_minutes": playlists.section_minutes(playlist_id),
        "groups": groups,
        "chart": pages.energy_chart(groups, _clock(total_ms), total_ms),
        "lessons": memory.from_playlist(user["user_id"], playlist_id) if row["review"] else [],
        "verdict": VERDICTS.get(row["review_verdict"]),
        "parent": playlists.get(row["parent_id"], user["user_id"]) if row["parent_id"] else None,
        "version": playlists.version(playlist_id) if row["parent_id"] else 1,
        "fixes": playlists.fixes(playlist_id, user["user_id"]),
        "auto_open": auto_open, "phone": links.is_phone(request.headers.get("user-agent")),
        "shows_taste": sharing.get(user["user_id"])["taste"],
        "blend": blend.for_playlist(playlist_id, user["user_id"], tracks),
        "heard_as": listen_as.heard_as(row),
    })


VERDICTS = {"worked": "it worked", "partly": "it partly worked", "missed": "it missed"}


def _section_groups(brief: Optional[Brief], tracks: list, sections: list[Optional[int]]) -> list[dict]:
    """Tracks under their section's heading with its start time, for playlists with several sections."""
    if brief is None or len(brief.sections) < 2 or None in sections:
        return []
    groups, elapsed = [], 0
    for position, (track, section) in enumerate(zip(tracks, sections), start=1):
        if not groups or groups[-1]["section"] != section:
            part = brief.sections[section]
            groups.append({"section": section, "label": part.label, "start": _clock(elapsed), "start_ms": elapsed,
                           "energy": part.energy, "bars": pages.energy_bars(part.energy),
                           "cut": len(groups) > 0 and part.transition_in == "cut",
                           "first": position, "tracks": [], "ms": 0})
        groups[-1]["tracks"].append(track)
        groups[-1]["ms"] += track.duration_ms
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
    track = playlists.track_at(playlist_id, position) if playlists.get(playlist_id, user["user_id"]) else None
    if track is None:
        return _feedback_not_found(request, user)
    if action == "like":
        outcome = likes.like(user, track["uri"], playlist_id)
    else:
        outcome = likes.unlike(user, track["uri"])
    return _feedback_answer(request, f"/playlists/{playlist_id}#t{position}", likes.notice(action, outcome, user["service"]),
                            liked=action == "like")


@app.post("/playlists/{playlist_id}/rating")
def rate_playlist(request: Request, playlist_id: int, rating: int = Form(..., ge=1, le=10),
                  user: sqlite3.Row = Depends(current_user)):
    if not playlists.set_rating(playlist_id, user["user_id"], rating):
        return _feedback_not_found(request, user)
    return _feedback_answer(request, f"/playlists/{playlist_id}#rating", rating=rating)


# --- reviews (Phase 15) -------------------------------------------------------------------------------------

REVIEW_NOTICES = {
    playlists.SAVED: "Review saved. Moodify is reading it now.",
    playlists.CLEARED: "Review deleted, with what Moodify learned from it.",
    playlists.UNCHANGED: None,
}


@app.post("/playlists/{playlist_id}/review")
def review_playlist(request: Request, playlist_id: int, user: sqlite3.Row = Depends(current_user),
                    review: str = Form("", max_length=reflect.MAX_REVIEW_CHARS)):
    # Saved at once; reflection (an LLM call or two) runs in the background, so this answers immediately
    text = prompts.clean_text(review, reflect.MAX_REVIEW_CHARS)
    outcome = playlists.set_review(playlist_id, user["user_id"], text)
    if outcome is None:
        return _feedback_not_found(request, user)
    if outcome == playlists.SAVED:
        jobs.start(reflect.run, playlist_id, user["user_id"], text)
    return _feedback_answer(request, f"/playlists/{playlist_id}#review", REVIEW_NOTICES[outcome], review=outcome)


@app.post("/playlists/{playlist_id}/fix")
def fix_playlist(request: Request, playlist_id: int, user: sqlite3.Row = Depends(onboarded_user)):
    """A new version built from the same prompt with the review as a correction. The old one stays on the service."""
    row = playlists.get(playlist_id, user["user_id"])
    if row is None:
        return _message(request, "Not found", "That playlist isn't yours or doesn't exist.", status_code=404, user=user)
    if not row["review"]:
        return _message(request, "Write a review first", "Say what to change in the review box, save it, then fix it.",
                        status_code=400, user=user)
    job = jobs.running_for(user["user_id"])
    job_id = job["id"] if job else jobs.create(user["user_id"], row["prompt"])
    if job is None:
        jobs.start(playlist.run, job_id, user["user_id"], row["prompt"], playlist_id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


# --- memory (Phase 16) --------------------------------------------------------------------------------------

@app.post("/playlists/{playlist_id}/more")
def more_like_this(request: Request, playlist_id: int, user: sqlite3.Row = Depends(onboarded_user)):
    """A new playlist seeded with this one's brief, liked songs and review, with new songs."""
    row = playlists.get(playlist_id, user["user_id"])
    if row is None:
        return _message(request, "Not found", "That playlist isn't yours or doesn't exist.", status_code=404, user=user)
    job = jobs.running_for(user["user_id"])
    job_id = job["id"] if job else jobs.create(user["user_id"], row["prompt"])
    if job is None:
        jobs.start(playlist.run, job_id, user["user_id"], row["prompt"], None, playlist_id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


@app.get("/memory")
def memory_page(request: Request, user: sqlite3.Row = Depends(current_user)):
    lessons = memory.active(user["user_id"])
    sources = {r["id"]: r for r in playlists.recent(user["user_id"], limit=-1)}
    return templates.TemplateResponse(request, "memory.html", {
        "user": user, "block": memory.memory_block(user["user_id"]), "lessons": lessons, "sources": sources,
        "tags": {r["id"]: memory.tags(r) for r in lessons}, "notice": request.session.pop("notice", None),
        "rows": list(sources.values()),
    })


@app.post("/memory/lessons/{lesson_id}/delete")
def delete_lesson(request: Request, lesson_id: int, user: sqlite3.Row = Depends(current_user)):
    if not memory.delete_lesson(user["user_id"], lesson_id):
        return _message(request, "Not found", "That lesson isn't yours or is already gone.", status_code=404, user=user)
    request.session["notice"] = "Lesson deleted."
    return RedirectResponse("/memory#lessons", status_code=303)


@app.post("/playlists/{playlist_id}/forget")
def forget_playlist(request: Request, playlist_id: int, user: sqlite3.Row = Depends(current_user)):
    if not playlists.forget(playlist_id, user["user_id"]):
        return _message(request, "Not found", "That playlist isn't yours or doesn't exist.", status_code=404, user=user)
    request.session["notice"] = f"Moodify forgot that playlist. It's still in your {music.info(user['service']).name}."
    return RedirectResponse("/memory#playlists", status_code=303)


# --- sharing with friends (Phase 18) ------------------------------------------------------------------------

def _not_shared(request: Request, user: sqlite3.Row):
    return _message(request, "Not shared with you", "That person hasn't switched this on, or isn't on Moodify.",
                    status_code=403, user=user)


@app.get("/sharing")
def sharing_page(request: Request, user: sqlite3.Row = Depends(current_user)):
    return templates.TemplateResponse(request, "sharing.html", {
        "user": user, "switches": sharing.get(user["user_id"]), "log": sharing.access_log(user["user_id"]),
        "notice": request.session.pop("notice", None),
    })


@app.post("/sharing")
def sharing_submit(request: Request, user: sqlite3.Row = Depends(current_user), show_taste: Optional[str] = Form(None),
                   allow_blends: Optional[str] = Form(None), allow_listen_as: Optional[str] = Form(None)):
    sharing.save(user["user_id"], show_taste is not None, allow_blends is not None, allow_listen_as is not None)
    request.session["notice"] = "Saved. It applies from now on."
    return RedirectResponse("/sharing", status_code=303)


@app.get("/friends")
def friends_page(request: Request, user: sqlite3.Row = Depends(current_user)):
    return templates.TemplateResponse(request, "friends.html", {
        "user": user, "friends": sharing.friends(user["user_id"]), "first_name": sharing.first_name,
        "mine": sharing.get(user["user_id"]),
    })


@app.get("/friends/{friend_id}")
def friend_page(request: Request, friend_id: str, user: sqlite3.Row = Depends(current_user)):
    viewer = user["user_id"]
    if friend_id == viewer:
        return RedirectResponse("/sharing", status_code=303)
    shared = {kind: sharing.allowed(friend_id, viewer, kind) for kind in sharing.SWITCHES}
    friend = users.get(friend_id)
    if friend is None or not any(shared.values()):
        return _message(request, "Not found", "No friend by that name shares anything here.", status_code=404, user=user)
    if shared["taste"]:
        sharing.log(friend_id, viewer, "view")
    return templates.TemplateResponse(request, "friend.html", {
        "user": user, "friend": friend, "name": sharing.first_name(friend), "shared": shared,
        # Only what "show my taste" covers: the stored summary (never refreshed with their token) and playlist titles
        "summary": friend["taste_summary"] if shared["taste"] else None,
        "playlists": sharing.visible_playlists(friend_id) if shared["taste"] else [],
    })


# --- Moodify Blend (Phase 20) -------------------------------------------------------------------------------

@app.get("/blend")
def blend_page(request: Request, user: sqlite3.Row = Depends(onboarded_user), with_: list[str] = Query([], alias="with")):
    return templates.TemplateResponse(request, "blend.html", {
        "user": user, "friends": sharing.friends(user["user_id"], "blend"), "picked": set(with_),
        "max_friends": blend.MAX_FRIENDS,
    })


@app.post("/blend")
def make_blend(request: Request, user: sqlite3.Row = Depends(onboarded_user),
               mood: str = Form(..., min_length=1, max_length=2000), friends: list[str] = Form([]),
               lean: str = Form(""), public: Optional[str] = Form(None)):
    try:
        blend.check(user["user_id"], friends)
    except blend.NotShared:
        if not 1 <= len(friends) <= blend.MAX_FRIENDS:
            return _message(request, "Pick 1 to 4 friends", "A Blend is you plus one to four friends who allow it.",
                            status_code=400, user=user)
        return _not_shared(request, user)
    job = jobs.running_for(user["user_id"])
    job_id = job["id"] if job else jobs.create(user["user_id"], mood)
    if job is None:
        jobs.start(blend.run, job_id, user["user_id"], mood, friends, lean or None, public is not None)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


# --- hear it like a friend (Phase 21) ------------------------------------------------------------------------

@app.post("/friends/{friend_id}/listen")
def listen_like(request: Request, friend_id: str, user: sqlite3.Row = Depends(onboarded_user),
                mood: str = Form(..., min_length=1, max_length=2000)):
    if friend_id == user["user_id"] or not sharing.allowed(friend_id, user["user_id"], "listen_as"):
        return _not_shared(request, user)
    job = jobs.running_for(user["user_id"])
    job_id = job["id"] if job else jobs.create(user["user_id"], mood)
    if job is None:
        jobs.start(listen_as.run, job_id, user["user_id"], mood, friend_id)
    return RedirectResponse(f"/jobs/{job_id}", status_code=303)


# --- the taste map (Phase 23) --------------------------------------------------------------------------------

@app.get("/map")
def map_page(request: Request, user: sqlite3.Row = Depends(current_user)):
    return templates.TemplateResponse(request, "map.html", {
        "user": user, "friend_count": len(sharing.friends(user["user_id"], "taste")),
    })


@app.get("/map.json")
def map_data(user: sqlite3.Row = Depends(current_user)):
    return JSONResponse(tastemap.data(user["user_id"]), headers={"Cache-Control": "no-store"})


# --- activity (Phase 24) -------------------------------------------------------------------------------------

@app.get("/activity")
def activity_page(request: Request, user: sqlite3.Row = Depends(current_user)):
    return templates.TemplateResponse(request, "activity.html", {
        "user": user, "items": activity.feed(user["user_id"]),
        "friend_count": len(sharing.friends(user["user_id"], "taste")),
    })


@app.get("/activity.json")
def activity_data(user: sqlite3.Row = Depends(current_user)):
    return JSONResponse(activity.timeline(user["user_id"]), headers={"Cache-Control": "no-store"})


@app.post("/playlists/{playlist_id}/hide")
def hide_playlist(request: Request, playlist_id: int, user: sqlite3.Row = Depends(current_user),
                  hidden: int = Form(..., ge=0, le=1)):
    if not sharing.set_hidden(playlist_id, user["user_id"], bool(hidden)):
        return _feedback_not_found(request, user)
    notice = "Hidden from friends." if hidden else "Friends who see your taste can see this playlist's title again."
    return _feedback_answer(request, f"/playlists/{playlist_id}#hide", notice, hidden=bool(hidden))


@app.get("/history")
def history(request: Request, user: sqlite3.Row = Depends(current_user), sort: Literal["new", "rating"] = "new"):
    return templates.TemplateResponse(request, "history.html", {
        "user": user, "rows": playlists.recent(user["user_id"], limit=-1, order=sort), "sort": sort,
    })


@app.get("/settings")
def settings_page(request: Request, user: sqlite3.Row = Depends(current_user)):
    return templates.TemplateResponse(request, "settings.html", {
        "user": user, "notice": request.session.pop("notice", None),
    })


@app.post("/settings")
def settings_submit(request: Request, user: sqlite3.Row = Depends(current_user),
                    likes_to_service: Optional[str] = Form(None), open_app: Optional[str] = Form(None)) -> RedirectResponse:
    # An unticked checkbox sends nothing at all
    users.set_likes_to_service(user["user_id"], likes_to_service is not None)
    users.set_open_app(user["user_id"], open_app is not None)
    request.session["notice"] = "Saved."
    return RedirectResponse("/settings", status_code=303)


@app.post("/queue/{playlist_id}")
def queue_playlist(request: Request, playlist_id: int, user: sqlite3.Row = Depends(onboarded_user)):
    row = playlists.get(playlist_id, user["user_id"])
    # Only Spotify has a queue Moodify can fill, so the button is only shown there (Phase 26)
    if row is None or not music.info(user["service"]).can_queue:
        return _message(request, "Not found", "That playlist isn't yours or doesn't exist.", status_code=404, user=user,
                        code="404 · Not found")
    try:
        with music.for_user(user) as service:
            for uri in json.loads(row["track_uris_json"]):
                service.queue(uri)
    except ServiceError as exc:
        if exc.status in (403, 404):
            return _message(request, "Can't play it from here",
                            "Playing from Moodify needs Spotify Premium and Spotify open on one of your devices."
                            " You can still open the playlist in Spotify.",
                            user=user, tone="low", playlist_url=row["url"],
                            back=("Back to the playlist", f"/playlists/{playlist_id}"))
        raise
    return _message(request, "Queued", "The tracks are in your queue. Open Spotify to listen.",
                    user=user, playlist_url=row["url"], back=("Back to the playlist", f"/playlists/{playlist_id}"))


ENERGY = ("low", "medium", "high", "mixed")
INSTRUMENTAL = ("yes", "sometimes", "no")
FAMILIAR = ("new", "mix", "known")


def _onboarding_page(request: Request, user: sqlite3.Row, answers: dict, errors: dict, status_code: int = 200):
    chips, other = pages.genre_choices(answers.get("genres") or "")
    return templates.TemplateResponse(request, "onboarding.html", {
        "user": user, "answers": answers, "errors": errors, "first": not user["onboarding_json"],
        "genres": chips, "genres_other": other, "artists": pages.artist_fields(answers.get("artists") or ""),
    }, status_code=status_code)


@app.get("/onboarding")
def onboarding_form(request: Request, user: sqlite3.Row = Depends(current_user)):
    return _onboarding_page(request, user, json.loads(user["onboarding_json"] or "{}"), {})


@app.post("/onboarding")
async def onboarding_submit(request: Request, user: sqlite3.Row = Depends(current_user)):
    form = await request.form()

    def one(key: str, default: str = "") -> str:
        return " ".join(str(form.get(key) or default).split())

    answers = {
        # Ticked chips, the "Other genres" box and older single-box answers all arrive as "genres"
        "genres": ", ".join(pages.split_list(*form.getlist("genres"), one("genres_other"))),
        "artists": ", ".join(pages.split_list(*form.getlist("artists"))),
        "energy": one("energy"), "instrumental": one("instrumental", "yes"),
        "languages": one("languages"), "avoid": one("avoid"), "familiar": one("familiar", taste.DEFAULT_FAMILIAR),
    }
    errors = {}
    if not answers["genres"]:
        errors["genres"] = "Pick at least one genre, or type your own."
    if not answers["artists"]:
        errors["artists"] = "Add at least one artist."
    if answers["energy"] not in ENERGY:
        errors["energy"] = "Pick the energy you usually want."
    if answers["instrumental"] not in INSTRUMENTAL or answers["familiar"] not in FAMILIAR:
        errors["other"] = "One of the answers isn't one of the choices. Pick again."
    for key in ("genres", "artists", "languages", "avoid"):
        if len(answers[key]) > 200:
            errors[key] = "Keep it under 200 characters."
    if errors:
        return _onboarding_page(request, user, answers, errors, status_code=422)
    users.set_onboarding(user["user_id"], answers)
    if user["service"] == "youtube":  # asked on this page for YouTube users (Phase 27)
        users.set_likes_to_service(user["user_id"], form.get("likes_to_service") is not None)
    return RedirectResponse("/", status_code=303)


@app.post("/disconnect")
def disconnect(request: Request, user: sqlite3.Row = Depends(any_user)):
    # Spotify requires deleting a user's data within 5 days of disconnect; we do it now
    service = music.info(user["service"])
    music.revoke(user)  # Google's policy: take back the token too, not only the data (Phase 27)
    users.delete(user["user_id"])
    request.session.clear()
    return _message(request, "Disconnected",
                    "Moodify deleted everything it stored about you and logged you out. Playlists already in your"
                    f" {service.name} are still there. To remove Moodify's access to {service.name} as well, remove it at"
                    f" {service.access_url}", tone="low")
