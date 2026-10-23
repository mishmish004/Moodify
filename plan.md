# Moodify build plan

Friends-only Spotify mood-playlist app. One FastAPI process, one SQLite file, two external APIs (Spotify, OpenAI). Nothing else until something breaks.

**How to use this file:** work phases in order. Tick `[ ]` → `[x]` as you go. A phase is done when every box in it is ticked and its gate command is green. Don't start the next phase before that.

**Test layout:** `tests/test_pN_<name>.py`, one file per phase. Offline tests mock Spotify and OpenAI over HTTP (`respx`) so the whole suite runs in seconds with no keys. Tests that hit real services are marked `live` and run on demand only.

```bash
.venv/bin/python -m pytest -m "not live and not docker"   # fast, every commit
.venv/bin/python -m pytest -m live                         # needs .env with real keys
```

**Not doing (and when that changes):** Redis / job queue (only if a request takes >30 s), vector DB or CLAP (the LLM is the catalog), parallel search (only if the playlist step feels slow), separate frontend, auth framework, Supabase. The earlier `docs/PLAN.md` (Supabase + LangGraph) is superseded by this file.

---

## Phase 0: Reset

Goal: an empty-but-running app with the old pipeline gone and nothing lost.

Build
- [x] `git add -A && git commit` the current state so the removal is reversible
- [x] Delete `src/services/{ai_engine,ingestion,query_engine,searcher}.py`, `src/schemas/{ingestion,songs}.py`, `src/api/v1/endpoints/ingest.py`, `scripts/`, `notebooks/`, `app/`
- [x] Delete `tests/checkup/` except the hygiene checks, which move to `tests/test_p0_reset.py`
- [x] `requirements.txt`: keep `fastapi`, `uvicorn`, `httpx`, `pydantic`, `pydantic-settings`, `openai`, `python-multipart`; add `jinja2`, `itsdangerous`; dev: `pytest`, `pytest-asyncio`, `respx`. Drop torch, laion-clap, qdrant-client, redis, tinytag, requests
- [x] `docker-compose.yml`: single `app` service, one volume for the SQLite file; remove redis and qdrant
- [x] `src/main.py`: FastAPI app with `GET /health` → `{"status": "ok"}`
- [x] `.env.example` listing every setting from Phase 1 with placeholder values

Tests (`tests/test_p0_reset.py`)
- [x] no secrets in tracked files (`sk-`, `client_secret`, `.env` contents)
- [x] `.gitignore` covers `.env`, `*.db`, `.venv`
- [x] every module under `src/` imports without side effects
- [x] no banned import anywhere in `src/` (`torch`, `laion_clap`, `qdrant_client`, `redis`)
- [x] `GET /health` returns 200

Gate: `pytest tests/test_p0_reset.py` green and `pip install -r requirements.txt` finishes in under a minute on a clean venv.

---

## Phase 1: Config and storage

Goal: settings fail loudly, the database exists, nothing else.

Build
- [x] `src/core/config.py` trimmed to: `ENV`, `SECRET_KEY`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REDIRECT_URI`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `DB_PATH`, `BASE_URL`. Secrets as `SecretStr`
- [x] Decide `OPENAI_MODEL` (config currently says `gpt-6.1-sol`, notes say `gpt-5.4-mini`) and set the default once
- [x] `src/db.py`: stdlib `sqlite3`, `init_db()` runs `schema.sql`, WAL mode, `get_conn()` context manager
- [x] `schema.sql`: `users(spotify_id PK, display_name, refresh_token, onboarding_json, taste_summary, taste_updated_at, created_at)` and `playlists(id PK, spotify_id FK, prompt, spotify_url, track_uris_json, created_at)`
- [x] `init_db()` called on app startup

Tests (`tests/test_p1_config.py`)
- [x] missing required setting raises `ValidationError`, never `sys.exit`
- [x] secrets don't appear in `repr(settings)`
- [x] `init_db()` twice on the same file is a no-op (idempotent)
- [x] insert → select round-trip for both tables using a `tmp_path` database
- [x] deleting a user cascades to their playlists

Gate: `pytest tests/test_p1_config.py` green.

---

## Phase 2: Spotify login

Goal: a friend clicks Login, lands back in the app, and we hold a refresh token for them.

Build
- [x] `src/spotify/auth.py`: Authorization Code + PKCE. `authorize_url(state, verifier)`, `exchange_code(code, verifier)`, `refresh(refresh_token)`
- [x] Scopes: `user-top-read user-read-private playlist-modify-public playlist-modify-private user-modify-playback-state`
- [x] `GET /login` → redirect to Spotify; `GET /callback` → exchange, upsert user, set signed session cookie (Starlette `SessionMiddleware`)
- [x] `current_user` dependency: reads cookie, loads user, 401 → redirect to `/login`
- [x] `POST /disconnect`: delete the user's row and clear the cookie (Spotify requires deletion within 5 days of disconnect; we do it instantly)
- [x] Redirect URI uses `http://127.0.0.1:9091/callback` in dev (Spotify rejects `localhost`; host port 8000 is taken by another project), HTTPS in prod. Both 9091 and 8888 are registered in the dashboard

Tests (`tests/test_p2_auth.py`)
- [x] `authorize_url` contains `code_challenge`, `code_challenge_method=S256`, the configured scopes and `state`
- [x] `/callback` with a mismatched `state` → 400, nothing stored
- [x] `/callback` with mocked token + `/me` responses → user row exists, cookie set, redirect to `/`
- [x] `refresh()` with mocked 200 returns new access token; mocked `invalid_grant` raises `ReauthRequired` (refresh tokens expire after 6 months)
- [x] `/disconnect` deletes the row and a following request to a protected route redirects to `/login`
- [x] protected route without cookie → redirect to `/login`
- [x] `live` (manual, once): log in with your own account in a browser, confirm the row in SQLite

Gate: `pytest tests/test_p2_auth.py` green and one real login works end to end.

---

## Phase 3: Spotify client

Goal: every Spotify call the app needs, in one small file, with retries.

Build
- [x] `src/spotify/client.py` on `httpx`, about 100 lines: `me()`, `top_artists(limit)`, `top_tracks(limit)`, `search_track(title, artist)` (returns ≤10 candidates), `create_playlist(name, description)` (`POST /me/playlists`), `add_items(playlist_id, uris)` (`POST /playlists/{id}/items`, ≤100 per call), `queue(uri)`
- [x] Transparent access-token refresh on 401, one retry
- [x] On 429: sleep `Retry-After` and retry once; if the body says `QUOTA_EXCEEDED`, raise `SpotifyQuotaExceeded` so the caller can show a message instead of hammering. A `Retry-After` over 10 s also raises instead of blocking the request
- [x] Response parsing uses the Feb-2026 shapes: playlist objects say `items`/`item` instead of `tracks`/`track`; search results are still under `tracks.items` (checked against Spotify's docs on 2026-10-23). Create playlist and add items answer 201, queue answers 204

Tests (`tests/test_p3_client.py`, all `respx`-mocked unless marked)
- [x] `search_track` builds `q=track:... artist:...`, `type=track`, `limit=10` and returns a list of `{uri, name, artists, duration_ms}`
- [x] `add_items` with 150 URIs makes two requests of 100 and 50
- [x] 401 → refresh → retry → success, and the new token is persisted
- [x] 429 with `Retry-After: 1` → one retry; `QUOTA_EXCEEDED` → `SpotifyQuotaExceeded`
- [x] non-2xx other than 401/429 raises `SpotifyError` with status and body
- [x] `live`: `search_track("Bohemian Rhapsody", "Queen")` returns at least one candidate whose artist is Queen

Gate: `pytest tests/test_p3_client.py` green.

---

## Phase 4: Onboarding and taste summary

Goal: a short form plus top artists becomes one paragraph of text we can hand to the LLM.

Build
- [x] `GET/POST /onboarding`: 5–6 questions (favourite genres, 3 artists you love, energy preference, instrumental ok?, languages, anything to avoid). Store as `onboarding_json`
- [x] `src/taste.py`: `build_summary(onboarding, top_artists, top_tracks) -> str`. Plain string formatting, no LLM, under 1,500 characters
- [x] Cache in `users.taste_summary`; rebuild when `taste_updated_at` is older than 7 days or onboarding changes
- [x] After onboarding, redirect to `/` (the mood box)

Tests (`tests/test_p4_taste.py`)
- [x] `build_summary` on fixture data is deterministic and ≤1,500 chars
- [x] summary mentions every onboarding answer and the top 5 artists
- [x] empty top artists (new Spotify account) still produces a valid summary from onboarding alone
- [x] stale `taste_updated_at` (8 days) triggers a rebuild; fresh (1 day) does not (mock `top_artists`, assert call count)
- [x] `POST /onboarding` with a missing required field → 422 and nothing stored
- [x] logged-in user without onboarding hitting `/` → redirected to `/onboarding`

Gate: `pytest tests/test_p4_taste.py` green.

---

## Phase 5: Song suggestions from the LLM

Goal: mood + taste summary → about 30 `{title, artist}` pairs, every time, without crashing.

Build
- [x] `src/suggest.py`: `suggest_songs(mood, taste_summary, n=30) -> list[Song]` with one OpenAI call using structured JSON output (`Song` is a Pydantic model: `title`, `artist`)
- [x] System prompt: suggest real, released songs that fit the mood and the taste; vary artists (max 2 songs per artist); no commentary
- [x] Cap `mood` input at 300 characters, strip control characters
- [x] Any parse or API error → log and return `[]`; never raise to the route

Tests (`tests/test_p5_suggest.py`)
- [x] mocked response with 30 valid songs → 30 `Song` objects
- [x] mocked malformed JSON → `[]`, no exception, error logged
- [x] mocked API error (timeout, 500) → `[]`
- [x] the request body sent to OpenAI contains the mood, the taste summary and the configured model
- [x] a 2,000-character mood is truncated to 300 before sending
- [x] `live`: real call with "rainy sunday, nostalgic" returns ≥20 songs, each with non-empty title and artist, ≥10 distinct artists

Gate: `pytest tests/test_p5_suggest.py` green.

---

## Phase 6: Match and create the playlist

Goal: `POST /playlist {mood}` ends with a real playlist in the user's Spotify account.

Build
- [ ] `src/match.py`: `pick(song, candidates) -> uri | None`. Lowercase, strip punctuation and anything after ` - ` / `(feat.` in titles; accept the first candidate whose artist list contains the suggested artist and whose title matches. No fuzzy-matching library
- [ ] `src/playlist.py`: `build(user, mood)`: suggest → search each → pick → dedupe URIs → stop at 25 → `create_playlist("Moodify: <mood>")` → `add_items` → insert `playlists` row → return URL. If fewer than 8 tracks matched, ask the LLM once more for 15 extra songs
- [ ] `POST /playlist`: calls `build`, renders the result page with the Spotify link; on `SpotifyQuotaExceeded` or 0 matches, show a plain message instead of a stack trace
- [ ] Optional `POST /queue/{playlist_id}`: adds the tracks to the active device; on 403/404 show "needs Premium and an active device"

Tests (`tests/test_p6_playlist.py`)
- [ ] `pick` table test: exact match; case difference; "Song - Remastered 2011" vs "Song"; "Song (feat. X)" vs "Song"; same title different artist → `None`; empty candidates → `None`
- [ ] end to end with mocked OpenAI and Spotify: 30 suggestions, 24 match, 6 don't → playlist created once, `add_items` called with 24 unique URIs, one `playlists` row, response contains the URL
- [ ] duplicate suggestions resolve to one URI
- [ ] fewer than 8 matches triggers exactly one extra suggestion call
- [ ] 0 matches → no playlist created, friendly message, no DB row
- [ ] `SpotifyQuotaExceeded` from search → 200 page with message, no partial playlist
- [ ] `live`: full run on your own account creates a playlist with ≥15 tracks (delete it by hand afterwards)

Gate: `pytest tests/test_p6_playlist.py` green and one real playlist shows up in your Spotify.

---

## Phase 7: UI

Goal: usable on a phone. Four Jinja pages, no JavaScript framework.

Build
- [ ] `templates/`: `base.html` (viewport meta, one small CSS file), `login.html`, `onboarding.html`, `index.html` (mood box + last 5 playlists), `playlist.html` (result with Spotify link and track list)
- [ ] `GET /history`: list of the user's playlists with links
- [ ] Spotify logo/link on every track and playlist (policy requirement)
- [ ] Disconnect button on `index.html`

Tests (`tests/test_p7_ui.py`)
- [ ] every page returns 200 for a logged-in user with onboarding done
- [ ] `/`, `/history`, `/playlist` redirect to `/login` when logged out
- [ ] `index.html` contains the mood form posting to `/playlist`
- [ ] `playlist.html` renders every track and the `open.spotify.com` link
- [ ] no template references a static file that doesn't exist
- [ ] manual: open on a phone, complete login → onboarding → playlist without zooming

Gate: `pytest tests/test_p7_ui.py` green and the manual phone run works.

---

## Phase 8: Deploy and onboard friends

Goal: a URL your friends can open.

Build
- [ ] `Dockerfile`: slim Python image, `uvicorn src.main:app`, SQLite on a mounted volume
- [ ] Host behind HTTPS (Spotify requires HTTPS redirect URIs outside loopback); set `SPOTIFY_REDIRECT_URI` and `BASE_URL` accordingly
- [ ] Add the redirect URI in the Spotify dashboard; add each friend's Spotify email to the app's user allowlist (max 5)
- [ ] `.env` on the host only, never in the image
- [ ] Tell friends their listening data is sent to OpenAI to generate suggestions

Tests (`tests/test_p8_deploy.py`, marked `docker`)
- [ ] image builds and is under 400 MB
- [ ] container starts, `GET /health` returns 200 within 10 s
- [ ] data survives a container restart (write a row, restart, read it)
- [ ] `live`: deployed `/health` returns 200 over HTTPS
- [ ] manual: one friend completes login → onboarding → playlist on the deployed URL

Gate: a friend has a playlist in their account that they didn't make themselves.

---

## Later, only if needed

- [ ] Re-consent flow when `ReauthRequired` fires (refresh tokens die after 6 months): redirect to `/login` with a message
- [ ] Export playlist as CSV/JSON (allowed under Policy III.9)
- [ ] Background task if `build()` regularly exceeds 30 s
- [ ] Thread pool for the search step if it regularly exceeds 5 s
- [ ] Retention job deleting cached `taste_summary` older than 30 days for inactive users
