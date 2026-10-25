# Moodify phase plan: Phases 9–29

The next build plan, written 2026-10-23 after the first real run. Part 2 (Phases 18–24, friends) starts after Phase 17. Part 3 (Phases 25–29, YouTube Music and Apple Music) was added on 2026-10-25. It continues `plan.md`: same rules, same test layout (`tests/test_pN_<name>.py`, `respx`-mocked, `live` on demand), and the phase numbers carry on from Phase 8. Work phases in order. A phase is done when every box is ticked and its gate is green.

```bash
.venv/bin/python -m pytest -m "not live and not docker"   # fast, every commit
.venv/bin/python -m pytest -m live                         # needs .env with real keys
```

## What was asked, and how this plan reads it

| Request | Read as | Phase |
|---|---|---|
| "my Spotify data needs to be accessed" | Use far more than top artists/tracks: Liked Songs, recently played, followed artists, your own playlists. Mix songs you already know into playlists | 10 |
| Tolerance for autistic prompts, prompts about changing mood, about feeling something | First turn every prompt into a written-down brief. Literal, detailed, list-like and sensory-specific prompts are honoured exactly; feelings are met where they are; mood-change requests get a gradual path. No prompt is rejected or "corrected" | 11 |
| "prepare appropriate songs of length" | The playlist lasts as long as asked ("workout for half an hour" = ~30 min), and song lengths fit the request ("short songs", "nothing over 6 minutes") | 12 |
| Mood switches "exactly orchestrated" | Sections play in the order asked, each about as long as asked, and switch the way asked: a hard cut for "instantly", a short bridge for "slowly" | 13 |
| Like a recommendation, rate it out of 10 | Like/unlike each track (also saved to Spotify Liked Songs); rate each playlist 1–10 | 14 |
| Comment on the playlist in your own words; keep it in a SQL column; use it to get better | A free-text review per playlist (`playlists.review`), written like a prompt. Moodify turns it into lessons, fixes the playlist on request, and uses the lessons in later playlists | 15 |
| Persistent memory of their curated playlists | Moodify remembers every playlist, its tracks, likes, ratings and reviews, and feeds the relevant part into each new build. Your own Spotify playlists feed taste (Phase 10) | 16 |
| On a phone, open the Spotify app straight away | When the playlist is ready on a phone, the Spotify app opens on it (one system prompt at most), with a web fallback | 17 |
| Caching and structure modelled on Google's and Meta's agent work | See "How the agent is structured" and "Caching" below. Each phase points to the part it builds | 9–16 |

Open questions (the defaults above stand until answered):
1. Should a like also add the song to Spotify's Liked Songs, or stay inside Moodify? Default: both, with a setting to turn the Spotify part off.
2. Are ratings per playlist only, or per track as well? Default: 1–10 per playlist, like/unlike per track.
3. Default length when the prompt names none? Default: 60 minutes.
4. Should "Fix it with this review" make a new playlist or replace the old one's songs? Default: a new version, linked to the old one.

## What the first real run showed (2026-10-23)

Prompt: *"I want to remember the chill summer morning and a hella stuff to do, workout for 1/2 an hour and then i want to instantly lock in for a programming task."*
- Your Spotify data was used: the taste summary held your top artists and tracks.
- The first suggestion call timed out at 45 s, so the playlist came from the 15-song fallback only: 15 tracks, no sections, no length target. The log line said "15/0 matched".
- The three parts (summer morning → 30-min workout → instant switch to focus) were not understood as a sequence.
- Onboarding says "no instrumental tracks"; "lock in for programming" usually suggests instrumental. The prompt only implies it, so onboarding wins (see Phase 11 rules).

## How the agent is structured

Moodify is a small agent: it reads a request, plans, acts through tools (OpenAI, Spotify), gets feedback, and should improve. The structure follows published agent designs from Google and Meta, scaled down to one process and one SQLite file. Sources are listed at the end.

**The loop for one playlist.** It follows Meta's PAHF loop: "(1) seeking pre-action clarification to resolve ambiguity, (2) grounding actions in preferences retrieved from memory, and (3) integrating post-action feedback to update memory when preferences drift".
1. **Read** the prompt into a brief, grounded in the user's taste and memory (Phase 11, 16)
2. **Act**: suggest songs, search Spotify, fill sections to length, order the arc, create the playlist (Phases 12–13)
3. **Observe**: likes, the 1–10 rating and the written review (Phases 14–15)
4. **Reflect**: turn the review into a verdict and a few grounded lessons (Phase 15)
5. **Consolidate**: merge the lessons into the user's memory. Newer feedback wins when preferences change (Phase 15)
6. **Recall**: the next build retrieves only the lessons relevant to its prompt (Phase 16)

**Storage is kept apart from what the model sees**, after Google's ADK: "Separate storage from presentation: We distinguish between durable state (Sessions) and per-call views (working context)." In Moodify:

| ADK concept | Moodify | Built in |
|---|---|---|
| Session: the durable log of one interaction | `jobs` + `job_events`: every step, LLM call, search, match, failure, timing and token count of one build | 9 |
| Memory: long-lived knowledge across sessions | `lessons` (from reviews), `track_likes`, ratings, `playlists.review`, the taste profile | 10, 14–16 |
| Artifacts: big data kept out of prompts, by reference | `playlist_tracks`, the known-songs pool. The prompt gets numbered references, not full dumps | 9, 10 |
| Working context: an ephemeral per-call view | Built fresh for each LLM call from the tables above; never stored | 11, 16 |

**Scope by default** (ADK: "Every model call and sub-agent sees the minimum context required"). Each LLM call gets only its part:
- reading the prompt: rules, taste summary, interpretation lessons, the prompt
- suggesting songs: the brief, song-level lessons, the known-songs pool, the no-repeat list
- reflecting on a review: the review, rating and likes, the brief, the track list by section
- consolidating: the new lessons plus only the existing lessons that share tags with them

**Lessons are grounded in what happened.** Google's ReasoningBank "distills and organizes memory items from both successful and failed experiences". It caps each run at three items, each with "(i) a title ... (ii) a description ... and (iii) the content". Moodify uses the same shape. One difference: ReasoningBank judges success itself ("LLM-as-a-judge ... without any ground-truth reference"), while Moodify has the user's own review, a real outcome signal. Meta's Early Experience work makes the same point: reflection should be grounded in "the resulting future states", not in the model's opinion. So a lesson must point at evidence in the review ("tracks 6–9 in the workout section"); a lesson without evidence is dropped. ReasoningBank consolidates "with a simple addition operation", which can't handle a user changing their mind. Moodify consolidates the way Memory Bank describes, "allowing memories to evolve as new information is ingested", and isolates memory per user.

## Caching

Four layers. Each says what is cached, how it's invalidated, and where it's built.

1. **Spotify reads** (saves quota and time)
   - Taste profile and known-songs pool: per user, 24 h, rebuilt on new onboarding answers (Phase 10)
   - Search results: `search_cache(spotify_id, query, tracks_json, fetched_at)`, keyed on title + artist, 7 days, per user because Spotify's results follow each user's market (Phase 12). A cache hit doesn't count toward the per-build search limit
2. **Derived memory** (saves LLM calls and keeps prompts stable)
   - Lessons are extracted and consolidated once, when a review is saved, in the background; never per build (Phase 15). ADK compacts history asynchronously the same way
   - `users.memory_block` and `users.memory_version`: the rendered memory text, rebuilt only when lessons, likes or ratings change (Phase 16)
3. **OpenAI prompt cache** (cheaper and faster calls). The rules come from OpenAI's guide, since OpenAI is the model Moodify calls; ADK gives the same advice ("Stable prefixes: System instructions, agent identity, and long-lived summaries")
   - Each prompt is laid out in three blocks: **static** (system rules, JSON schema, worked examples; identical for everyone) → **per user, slow-changing** (taste summary + `memory_block`, which change at most daily) → **per request** (the prompt, the brief). Only prompts of 1,024 tokens or more are cached, so the static block is written to exceed that
   - Never edit the prefix per request. OpenAI's guide: "Summarization, compaction, or context truncation can change the prefix and reset cache reuse." Compaction happens in consolidation, not in the prompt
   - Every call sends `prompt_cache_key = "moodify:{step}:{spotify_id}"` (harmless on GPT-5.6+, which route automatically, and it gives per-user cache accounting). No retention options: they differ by model generation and an unsupported one fails the request. Decided in Phase 11; revisit if a user's builds are often more than 30 minutes apart
   - Log cached tokens per call into `job_events` (`usage.prompt_tokens_details.cached_tokens` in Chat Completions). Target: ≥50% of input tokens cached on a user's second build within the TTL (Phase 16 test)
4. **Not cached on purpose**: whole LLM responses (the same prompt should be able to give a fresh playlist), and playlist pages (rebuilt from `playlist_tracks` each time)

## Spotify limits these phases are built around

Checked against developer.spotify.com on 2026-10-23 (February, March, May and July 2026 changelogs plus the reference pages):
- Still available to a Development Mode app: recently played (`GET /me/player/recently-played`, max 50), Liked Songs (`GET /me/tracks`, max 50 per page), followed artists (`GET /me/following?type=artist`), your playlists (`GET /me/playlists`), playlist items (`GET /playlists/{id}/items`) **only for playlists you own or collaborate on** (403 otherwise), start playback (`PUT /me/player/play`, Premium only), devices (`GET /me/player/devices`), single track (`GET /tracks/{id}`).
- Changed: saving to Liked Songs is now `PUT /me/library` (max 40 URIs). Removing is `DELETE /me/library`, checking is `GET /me/library/contains`; check their scopes when building Phase 14. Get Several Tracks is removed, so tracks are fetched one at a time. Search returns at most 10 results.
- Deprecated, so treated as unavailable: audio features, audio analysis, recommendations. **Energy and tempo of a song come from the LLM's judgement, not from Spotify.**
- `duration_ms` is still on every track object, so the length targets in Phase 12 rely on Spotify's numbers, not the LLM's.
- Quota is per developer account; going over answers 429 with `QUOTA_EXCEEDED`. Longer playlists mean more searches, so every phase counts its Spotify calls.
- These phases send more Spotify data, plus reviews, to OpenAI and keep more track metadata. That falls under the 2026-10-22 friends-only decision. The login page's data section must stay accurate (Phase 10, 15 and 16 items).

---

## Phase 9: A build pipeline that survives slow models

Goal: no more 15-track accidents. A playlist build can take a few minutes without the browser timing out, every step is logged, and the result page can be reopened.

Build
- [x] `src/suggest.py`: timeout 45 s → 120 s, plus one retry on timeout or 5xx. When a call returns nothing, retry the same full request once instead of dropping to the 15-song fallback
- [x] Schema migrations: `PRAGMA user_version` plus numbered steps in `src/db.py`, so new columns and tables reach existing databases. `schema.sql` stays the fresh-install path
- [x] `playlist_tracks(playlist_id FK, position, uri, name, artists_json, duration_ms, section)`: the result page and later phases need names and lengths, not only URIs
- [x] `jobs(id, spotify_id, prompt, status, step, error, playlist_id, created_at)`. `POST /playlist` creates a job, starts `build` in a background thread and answers `303 /jobs/{id}`
- [x] `job_events(job_id, at, kind, detail_json)`: the session log (see "How the agent is structured"). One row per step, LLM call (model, latency, input/cached/output tokens), search (query, hit/miss, cache hit), match and failure. Phase 15 reflects on it
- [x] `GET /jobs/{id}`: owner only. While running, show the current step ("Reading your prompt…", "Finding songs on Spotify 12/30…"), with `<meta http-equiv="refresh" content="3">` so no JS is needed. When done, `303` to `GET /playlists/{id}`. On failure, show the friendly message
- [x] `GET /playlists/{id}`: the result page rebuilt from `playlist_tracks`. Refreshing it no longer re-submits the form
- [x] Log line: `matched/searched` from all batches (not "15/0")
- [x] On startup, a job left `running` by a restart is marked `failed`

Tests (`tests/test_p9_jobs.py`)
- [x] first call times out, retry succeeds → full-size playlist, not 15 tracks
- [x] both calls fail → job `failed`, friendly page, no playlist, no DB row in `playlists`
- [x] `POST /playlist` answers 303 within 1 s even when the mocked LLM takes 5 s
- [x] `/jobs/{id}` and `/playlists/{id}` of another user → 404
- [x] a finished job has events for every step, in order, with token counts on LLM calls
- [x] a migration run twice is a no-op; a database at `user_version` 0 with data upgrades without loss
- [x] job left `running` at startup becomes `failed`

Gate: `pytest tests/test_p9_jobs.py` green, and the 2026-10-23 prompt run live gives ≥20 tracks.

Done 2026-10-23: the live run gave 25 tracks (25/25 searches matched, one 31 s call to `gpt-6.1-sol`, 455 input tokens, 0 cached; below the 1,024-token minimum until Phase 11). Also fixed on the way: `MoodifyLogger` skipped its setup whenever the root logger had handlers, dropping INFO lines; SQLite now runs `synchronous = NORMAL` under WAL; tests wait for leftover build threads.

---

## Phase 10: Read more of your Spotify

Goal: suggestions draw on what you actually play and save, and playlists can include songs you already know.

Build
- [x] Scopes: add `user-read-recently-played user-library-read user-library-modify user-follow-read playlist-read-private user-read-playback-state`. Phases 14 and 17 need the last ones, and adding them all now means friends re-consent only once
- [x] Store `granted_scopes` from the token response. When a needed scope is missing, redirect to `/login?reason=scopes`, and the login page explains why. This is the "re-consent flow" from plan.md's Later list
- [x] `src/spotify/client.py`: `recently_played(50)`, `saved_tracks(max=200)` (paged, 50 per page), `followed_artists()`, `my_playlists()`, `playlist_items(id)` for owned/collaborative playlists only (skip on 403). Each returns `Track` with `duration_ms`
- [x] `src/taste.py` v2: top artists for short and long term, recent plays, a sample of Liked Songs, followed artists, names of your own playlists. Summary cap 1,500 → 3,000 chars. Cached 24 h (caching layer 1)
- [x] Known-songs pool: Liked + recent + top tracks with URIs and lengths, cached with the summary. The LLM may pick from it by number, which needs no search, so the match is exact and costs no quota
- [x] Familiarity: default 30% known songs, changed by the prompt ("only songs I know", "discover new stuff") and an optional onboarding question
- [x] Login page: the data list names the new data that goes to OpenAI

Tests (`tests/test_p10_spotify_data.py`)
- [x] every new client method parses the Feb-2026 shapes (`items`/`item`, paging with `next`), with fixtures
- [x] `saved_tracks(max=200)` makes 4 requests and stops at `next: null`
- [x] `playlist_items` on a followed-only playlist (403) is skipped, not raised
- [x] a user whose `granted_scopes` lacks a new scope is sent to `/login?reason=scopes`
- [x] summary includes each data source and stays ≤3,000 chars; empty sources (new account) still give a valid summary
- [x] a pool song picked by number skips search entirely (assert no `/search` call)
- [x] `live`: your account returns recent plays and Liked Songs, and the summary mentions artists from both

Gate: `pytest tests/test_p10_spotify_data.py` green, and after re-login a live playlist has ~30% songs from your library.

Done 2026-10-23 after a re-login. Live: 50 recent plays, 200 liked, 9 follows, 47 playlists (56 tracks from owned ones). The first gate run gave 19 tracks, 26% known, but missed 11 of 25 searches: the known list showed "Title by Artist1, Artist2" and the model copied that format into `artist`. Fixed by listing only the main artist, searching the first listed artist and accepting any listed artist when matching. Rerun: 25 tracks, 8 known (32%), 17/17 searches matched. Also: summaries cached before Phase 10 (no pool) are now rebuilt instead of served for up to a day. Picks from the pool are checked against title and artist before use; Moodify's own playlists are left out of the profile.

---

## Phase 11: Understand any prompt

Goal: every prompt, however it's written, becomes a clear brief that the user can see. Nothing is refused or brushed aside.

Build
- [x] `src/brief.py`: first LLM call, `read_prompt(prompt, taste, memory) -> Brief` (structured output). `Brief`: `sections[]` (`label`, `feeling`, `energy` 1–10, `minutes | None`, `activity`, `transition_in: cut | blend`), `total_minutes | None`, `hard_rules` (`no_vocals`, `no_explicit`, `no_sudden_loud_changes`, `steady_tempo`, `languages`, `avoid[]`, `max_track_minutes`, `min_track_minutes`), `familiarity`, `tags[]` (activities and feelings, used for memory recall in Phase 16), `support_note: bool`. `OPENAI_BRIEF_MODEL` setting, defaulting to `OPENAI_MODEL`
- [x] `src/prompts.py`: every LLM call is assembled in the three-block layout from "Caching" (static → per user → per request). The static block, with rules, schema and worked examples, is over 1,024 tokens. Cache settings are chosen for the configured model
- [x] Mood cap 300 → 2,000 chars. Detailed prompts are often long, and cutting them drops rules
- [x] Rules in the system prompt, each covered by a fixture prompt in the tests:
  - **Literal and precise prompts** (common among autistic users, useful for everyone): every stated rule is applied exactly as written. "No songs with screaming", "same tempo the whole time", "only songs I know", "nothing with sudden drops" become `hard_rules`, never softened or reinterpreted
  - **Sensory needs**: volume jumps, harsh sounds, busy production and unpredictability can be named as things to avoid, and they are hard rules
  - **Lists, bullet points, info-dumps, mixed languages, slang, typos, emoji** ("lock in", "hella", "🌧️☕") are all read without asking the user to rephrase
  - **Very short or vague** ("meh", "idk", one emoji): one section that fits taste and time of day; never an error
  - **Saying how you feel** ("I feel numb", "kinda empty today"): the playlist meets the feeling where it is. It never pushes darker and doesn't force cheerfulness unless asked. Onboarding "avoid" still applies (e.g. "no sad songs") unless the prompt explicitly overrides it
  - **Wanting to change mood** ("anxious, want to calm down", "need to get hyped"): at least 2 sections following the iso principle. Start close to the current feeling, then move step by step to the target
  - **Conflicts**: something the prompt states outright beats onboarding and memory; something it only implies does not (the 2026-10-23 case: "lock in" doesn't override "no instrumental")
  - **Distress**: if the prompt suggests crisis or self-harm, the playlist is still made, and the result page adds one calm line with a support link. Never a refusal, never a lecture
- [x] Result page: a "How Moodify read this" box showing sections, length and rules, plus an "Adjust and redo" link that pre-fills the prompt
- [x] Suggestion call takes the `Brief` instead of the raw mood

Tests (`tests/test_p11_prompts.py`)
- [x] fixture set of ≥15 prompts covering every rule above (including the 2026-10-23 prompt and a 1,500-char detailed one). With a mocked LLM: each parses, nothing is cut below 2,000 chars, the brief reaches the suggestion call
- [x] prompt layout: for two different users and prompts, the static block is byte-identical and comes first; per-request text only appears after the per-user block
- [x] malformed brief from the LLM → fallback single-section brief from the raw prompt, never an error page
- [x] `support_note` → the support line shows, and the playlist is still created
- [x] `hard_rules.avoid` and onboarding "avoid" both reach the suggestion prompt
- [x] `live` eval over the fixture set with the real model: the 2026-10-23 prompt gives 3 sections in order with the workout at 30 min and `transition_in: cut` on the focus section; "anxious, want to calm down" gives ≥2 sections with falling energy; every explicit rule in the literal prompts shows up in `hard_rules`

Gate: `pytest tests/test_p11_prompts.py` green and the live eval passes on ≥90% of fixtures. Failures get read and the prompt fixed.

Done 2026-10-23. Live eval: 18/18 fixtures read right on `gpt-6.1-sol` (the conflict rule held both ways: "lock in for coding" kept onboarding's "No instrumental tracks", "- NO vocals" overrode it). Prompt caching measured: a second brief call had 2,627 of 2,661 input tokens cached. Built differently from the list above: `src/prompts.py` also holds the one LLM call path (`ask`: retries, tracing, cache key) that both calls now use; prompts keep line breaks (they were being deleted, gluing list items together); "no explicit" is enforced in code with Spotify's `explicit` flag, preferring the clean version among search results; the playlist is named after the brief's title.

---

## Phase 12: The right length

Goal: a playlist lasts as long as asked, and its songs fit the request, using Spotify's real track lengths.

Build
- [x] Target length: sum of section minutes, else `total_minutes`, else 60 min. Hard cap 3 h / 100 tracks (quota)
- [x] Over-ask: request songs per section for about 1.5× its minutes at ~3.5 min/song, so there is choice after matching
- [x] `src/fill.py`: pure function `fill(section_minutes, candidates) -> tracks`. Add matched tracks in suggested order until the section is within ±10% or ±3 min, whichever is larger; never cut a song mid-way. Drop tracks breaking `max_track_minutes` / `min_track_minutes`
- [x] Short section after matching → one extra LLM call for that section only (replaces the global "under 8 → 15 more")
- [x] `search_cache` (caching layer 1): normalized title + artist → candidates, 7 days
- [x] Result page and playlist description show total length and each section's length
- [x] Count searches per build (cache hits excluded) into `job_events`; refuse to start a build that would exceed 150 searches

Tests (`tests/test_p12_length.py`)
- [x] `fill` table tests: exact fit; overshoot by one long song stays within tolerance or drops it; no candidates; all too long for `max_track_minutes`
- [x] "workout for half an hour" section → 27–33 min
- [x] no length in prompt → 54–66 min
- [x] a short section triggers exactly one extra call, for that section only
- [x] the same song searched twice within 7 days → one Spotify call; after 7 days → two
- [x] `live`: "45 minute commute, chill" → 40.5–49.5 min

Gate: `pytest tests/test_p12_length.py` green, and the live run is in range.

Done 2026-10-23. Live: "45 minute commute, chill" gave 43.1 min (15 tracks, 6 known, 10 searches, 20 suggestions). Sections without a stated length share the rest of the total, or of 60 minutes (the 2026-10-23 prompt becomes 15 + 30 + 15). Different from the list above: the search cache is per user, not shared, because Spotify's results follow each user's market; short sections get one extra call together, not one call each; a song suggested twice is looked up once; the 100-track cap was dropped, since the 3-hour cap and the 150-search limit already bound a build.

---

## Phase 13: Mood switches, exactly orchestrated

Goal: "first X, then Y for 30 minutes, then instantly Z" plays as X → Y → Z, at those times, switching the way the user said.

Build
- [x] Suggestions come back per section, each song with `energy` 1–10 and a `role` (`opener`, `build`, `peak`, `bridge`, `closer`)
- [x] Order: sections exactly as in the brief. Inside a section, songs climb or fall toward the next section's energy
- [x] `transition_in: cut` (prompt says "instantly", "then straight into", "switch"): the new section starts at full target energy with no bridge. `blend` ("slowly", "ease into", default for mood-change arcs): 1–2 bridge songs between the two energies
- [x] Spotify playlist order = Moodify order (`add_items` keeps batch order; test it)
- [x] Playlist description (≤300 chars): the timeline, e.g. "0–14 summer morning → 14–44 workout → 44–90 lock in"
- [x] Result page: tracks under section headings with start times ("Workout · starts at 14:20")

Tests (`tests/test_p13_arcs.py`)
- [x] 3-section brief → tracks grouped in that exact order; section start times equal cumulative lengths
- [x] `cut` → no bridge songs at that boundary; `blend` → 1–2 bridge songs
- [x] energies inside a section move monotonically toward the next section (given mocked energies)
- [x] `add_items` with 150 ordered URIs → Spotify receives them in the same order across batches
- [x] description fits 300 chars and contains every section label
- [x] `live`: the 2026-10-23 prompt gives 3 sections in order, workout 27–33 min, and starts the focus section with no bridge. Listen to it once and note in this file whether the switches felt right

Gate: `pytest tests/test_p13_arcs.py` green, and the live playlist sounds like the prompt.

Done 2026-10-23 apart from the listening check. Live (https://open.spotify.com/playlist/6syI6aMNiem5YKKJxItpJu): Summer morning, 3 tracks (12.7 min) → Workout, 12 tracks (28.7 min), opening with two bridges at energy 6 and 7, peaking at 9, easing to 8 → Lock in, 4 tracks (16.3 min), opening straight at energy 5 with no bridge. Each track's energy and role are stored (migration 5) for Phase 15's reflection. Energy is the model's judgement; see "Later" if it proves unreliable.
Listening check (yours): did the switches feel right?

---

## Phase 14: Likes and ratings

Goal: quick signals on what landed, per track and per playlist.

Build
- [x] `track_likes(spotify_id, uri, playlist_id, liked_at)`. A ♥ button per track row on the playlist page: a plain POST form that redirects back, with a small JS enhancement so the page doesn't reload
- [x] Like also saves to Spotify Liked Songs via `PUT /me/library` (`user-library-modify`). Unlike removes from Moodify, and from Liked Songs only if Moodify added it. Setting: "Also add likes to my Spotify Liked Songs" (default on)
- [x] `playlists.rating` (1–10): a row of 1–10 buttons on the playlist page, changeable any time. The written review is Phase 15
- [x] History shows rating and number of likes, and can sort by rating
- [x] All endpoints owner-only: `POST /playlists/{id}/rating`, `POST /playlists/{id}/tracks/{position}/like` and `/unlike`

Tests (`tests/test_p14_feedback.py`)
- [x] like → row stored, `PUT /me/library` called with that URI; with the setting off → no Spotify call
- [x] unlike of a song Moodify added → `DELETE /me/library`; of a song already in Liked Songs before → no Spotify call
- [x] rating 0, 11, "abc" → 422; 1 and 10 accepted; re-rating overwrites
- [x] another user's playlist or track → 404, nothing stored
- [x] Spotify 403 on save (scope missing) → the like is still stored, and the page says Liked Songs wasn't updated
- [x] pages work with JS disabled (form POST + redirect)

Gate: `pytest tests/test_p14_feedback.py` green, and a live like shows up in your Spotify Liked Songs.

Done 2026-10-24. Migration 6 adds `track_likes`, `playlists.rating`/`rated_at` and `users.likes_to_spotify`. A like is per song, so a song liked in one playlist shows liked in every playlist. Before saving, Moodify asks `GET /me/library/contains`, so a song already in Liked Songs is marked as not Moodify's and is never removed on unlike. A like made through Moodify is undone on unlike even after the setting is turned off. The setting is on a new Settings page (`/settings`), linked from the home page and from the note under each track list; Phase 17's "open Spotify" setting can go there too. Spotify failures (403, 429, 5xx, no answer) keep the like and show a message once. The ♥ and the 1–10 row are plain forms; `static/feedback.js` sends the same posts asking for JSON, so the page doesn't reload. Both paths were checked in headless Chrome: a phone-width screenshot in light and dark, and with scripts turned off. Live: liking "Drip Too Hard" put it in Liked Songs (`user-library-modify` is enough for track URIs), and unliking took it out again. 37 tests.

---

## Phase 15: Reviews: tell Moodify how it went, in your own words

Goal: after listening, the user writes what they thought, as freely as they wrote the prompt. The review is kept in SQL, fixes this playlist on request, and makes later playlists better. This phase covers steps 3–5 of the loop in "How the agent is structured".

Build
- [x] Schema: `playlists.review TEXT` (the current review, ≤2,000 chars), `playlists.reviewed_at`, `playlists.review_verdict` (`worked | partly | missed`, filled by reflection). `review_history(playlist_id, text, created_at)` keeps earlier versions when a review is edited
- [x] `lessons(id, spotify_id, source_playlist_id, title, description, content, kind, tags_json, evidence, status, superseded_by, created_at, last_used_at, use_count)`. `kind` is `preference | pitfall | strategy`; `status` is `active | superseded | expired`. The shape follows ReasoningBank's memory items (title, description, content)
- [x] Playlist page: a "How was it?" box under the rating: a textarea with examples ("the workout part dragged", "loved the first 10 minutes", "too much Drake", "the switch to focus was perfect"). Reviews get the same tolerance as prompts (Phase 11 rules): slang, lists, emotion, one word or a long paragraph, all fine
- [x] Saving a review answers immediately and queues reflection in the background. The ADK and Memory Bank designs generate memory asynchronously too, so the user never waits
- [x] **Reflect** (`src/reflect.py`, one LLM call): inputs are the review, rating, likes, the brief, the track list by section with start times, and the job's `job_events` (what was asked vs what matched). Output: `review_verdict` plus 0–3 lessons. Each lesson must quote its `evidence` from the review and name the section or tracks it's about; lessons without evidence are dropped (grounded reflection, as in Meta's Early Experience). The output separates lessons about this playlist only ("summer-morning section too slow") from lasting preferences ("doesn't like long intros in workout music")
- [x] **Consolidate** (`src/memory.py`, one LLM call, only when new lessons exist): the new lessons plus existing active lessons with overlapping tags. The answer is one operation per lesson: `ADD`, `UPDATE(id)`, `SUPERSEDE(id)` or `NOOP`. Newer feedback wins when it contradicts older feedback, since preferences drift (PAHF); superseded lessons stay in the table with `superseded_by` as an audit trail. At most 40 active lessons per user: past that, consolidation must merge. Every query filters by `spotify_id`
- [x] **Fix it with this review**: a button next to a saved review. Builds a new version of the playlist with the review as a correction on top of the original brief. `playlists.parent_id` links v2 to v1; v1 stays in Spotify
- [x] Until Phase 16 adds selective recall, the next build gets every active lesson (≤1,500 chars) in the per-user prompt block
- [x] Login page and "What Moodify remembers" (Phase 16): reviews and lessons are sent to OpenAI and kept until you delete them or disconnect

Tests (`tests/test_p15_reviews.py`)
- [x] review saved → `playlists.review` and `reviewed_at` set, response under 1 s with the mocked LLM delayed 5 s; editing keeps the old text in `review_history`
- [x] review over 2,000 chars → 422; an empty review clears it; another user's playlist → 404
- [x] reflection with fixture review "the workout part was way too slow, rest was great" → verdict `partly`, a lesson tagged `workout` whose evidence is quoted from the review
- [x] a lesson whose `evidence` isn't in the review text is dropped
- [x] consolidation: "loves long intros" then later "skip long intros, they bore me" → the old lesson `superseded`, the new one `active`, `superseded_by` set
- [x] consolidation with 41 candidate lessons → at most 40 active afterwards
- [x] "Fix it with this review" → new playlist with `parent_id`; the review text reaches the brief call as a correction
- [x] the next build's per-user prompt block contains the active lessons, not the superseded ones
- [x] Disconnect deletes reviews, history and lessons (cascade)
- [x] `live` eval (on demand, results written into this file): 5 fixture playlists with reviews. For each, build with and without the lessons from the others, and have an LLM judge (temperature 0, separate call) say which result better respects the review. With-lessons should win at least 4 of 5

Gate: `pytest tests/test_p15_reviews.py` green, the live eval passes, and one real review → "Fix it" gives a v2 that you rate higher than v1.

Done 2026-10-24 apart from your real-review check. Live eval: with-lessons won 5/5 on `gpt-6.1-sol` (judge: same model; it takes temperature 0 or falls back to its default). The first run also won 5/5, but two of the five reviews ("too many sad acoustic ballads", "less sad acoustic stuff") gave no lasting lessons, because reflection was told to pick "this playlist only" when unsure; the rule now names artists, kinds of song, intros, vocals, energy and switches as lasting, and every review gave 2 lessons. Lessons are also told they beat the taste summary when the two disagree (a listener whose top artist is Drake but who said "too much Drake"). Built differently from the list above: `lessons` also has `scope` (`lasting | this_playlist`) and `about`; this-playlist lessons skip consolidation and are used only by "Fix it"; consolidation makes no LLM call when no existing lesson shares a tag (all ADD) and treats a failed call or an id that isn't the user's as ADD, so a grounded lesson is never lost; past 40 active it sees every lesson and is asked to merge, and if it doesn't merge enough, the least recently used expire (never the new ones). An edit while reflection runs wins: the older result is thrown away. Clearing a review deletes the lessons learned from it. "Fix it" sends the earlier brief, the review, the lessons from it and the songs liked in it, and names the new playlist "… (v2)". 34 tests.
Your check: write a real review on a playlist, press "Fix it with this review", and rate both.

---

## Phase 16: Memory in every build

Goal: every build recalls just the relevant parts of everything Moodify knows about the user, cheaply. Liked and high-rated things come back; reviewed problems don't repeat.

Build
- [x] `memory_for(user, brief) -> str` (≤1,500 chars), recall step 6 of the loop. Proactive recall as in ADK ("a pre-processor to run a similarity search based on the latest user input"), scaled down: lessons whose tags overlap the brief's `tags`, plus every `preference` lesson, ranked by overlap then recency, top 8. Tag overlap instead of embeddings, because a user has at most 40 active lessons (plan.md: no vector DB; see Later)
- [x] Also in memory, from Phase 14: liked tracks and the artists liked most ("lean toward"); playlists rated ≥8 with their prompts ("this worked for these moods")
- [x] No-repeat: tracks from the last 3 playlists are not used again unless liked, at most 2 per playlist. Enforced in code, not only in the prompt
- [x] `users.memory_block` + `users.memory_version` (caching layer 2): the user-wide part (preferences, likes, ratings) is rendered once and rebuilt only when it changes, so the per-user prompt block stays byte-identical between builds and hits OpenAI's prompt cache (layer 3). The brief-specific recalled lessons go in the per-request block
- [x] Lesson upkeep: `last_used_at` and `use_count` updated on recall. `pitfall` and `strategy` lessons unused for 180 days become `expired` (Memory Bank-style TTL); `preference` lessons never expire but can be superseded
- [ ] (Not built: optional) After PAHF's pre-action clarification: when the brief's sections conflict with an active lesson, the job page asks one question with 2–3 choices ("Last time you said workout music was too slow; keep it high-energy?"). It auto-continues with the lesson after 20 s, so a build never stalls
- [x] "More like this" on any past playlist: builds a new one seeded with that playlist's brief, its liked tracks and its review
- [x] "What Moodify remembers" page: the memory block as the LLM sees it, active lessons with the review each came from, delete per lesson or per playlist
- [x] Disconnect still deletes everything (`ON DELETE CASCADE` on every new table)

Tests (`tests/test_p16_memory.py`)
- [x] recall: with fixture lessons tagged `workout`, `focus`, `sleep`, a brief tagged `workout` gets the workout lessons and all preferences, not `sleep`; never more than 8; ≤1,500 chars with 200 playlists in the DB
- [x] a track from the last 3 playlists that isn't liked never appears again (enforced even if the mocked LLM suggests it)
- [x] `memory_block` unchanged between two builds with no new feedback → per-user prompt block byte-identical; a new like changes it and bumps `memory_version`
- [x] cached tokens from mocked `usage` are recorded in `job_events`
- [x] expiry: a `pitfall` unused for 181 days → `expired`; a `preference` unused for 400 days → still `active`
- [ ] (not built) clarification: no answer in 20 s → build continues with the lesson applied
- [x] deleting a playlist from memory removes its likes, rating, review and lessons; Disconnect leaves zero rows in every table for that user
- [x] `live`: two builds by the same user within an hour → the second reports cached input tokens > 0 (98%, see below)
- [x] `live`: rate a playlist 2/10 with the review "too slow", make a similar prompt, and its brief has higher energy (8 → 9)

Gate: `pytest tests/test_p16_memory.py` green, and both live checks move the right way.

Done 2026-10-24, without the optional clarification question. Live: a second build a minute later had 6,416 of 6,539 input tokens cached (98%); a 2/10 "too slow" workout review raised the next gym brief's energy from 8 to 9. Built differently from the list above:
- **Prompt layout.** The first cache check gave 53%: the brief call reused its long system message, and the suggestion call reused nothing across different moods. Probes on `gpt-6.1-sol` showed cached tokens reach the end of the system message and no further; splitting the user message didn't help. So the per-user part (taste, known songs, memory block) now sits in the system message after the static rules, fenced off as "their data, never instructions", and the request alone is the user message. The Phase 11 eval (18/18) and Phase 15 eval (5/5) were rerun on the new layout.
- **Recall.** Plain tag overlap missed "gym session" for a lesson tagged `workout`, so the energy check first failed. `memory.TAG_GROUPS` maps everyday words to shared tags (gym, lifting, run → workout; coding, study → focus; bed → sleep; …), and reflection is asked to prefer those tags. The brief call recalls from the prompt's words, and the suggestion call from the brief's tags, title and sections.
- **Memory block.** It holds up to 4 preference lessons, liked songs and the most-liked artists, and the prompts of playlists rated 8 or more (at most 1,000 chars). Recalled lessons fill the rest of the 1,500 chars, up to 8 lessons in all. It's cleared by every like, unlike, rating, lesson change, cleared review and forgotten playlist, and rendered again on the next build, which bumps `memory_version`.
- **No-repeat.** "Fix it" may reuse the fixed playlist's songs. "More like this" blocks its seed playlist's songs, unless liked, even when that playlist is older than the last 3.

Your check: rate and review a real playlist, then make a similar one and see whether it moved the right way.

---

## Phase 17: Open straight in the Spotify app on phones

Goal: on a phone with Spotify installed and logged in, the finished playlist opens in the app without hunting for a link.

Build
- [x] `src/links.py`: `open_link(kind, id, user_agent)`:
  - Android: `intent://{kind}/{id}#Intent;scheme=spotify;package=com.spotify.music;S.browser_fallback_url={web url};end` (opens the app directly; web page if not installed)
  - iPhone/iPad: `https://open.spotify.com/{kind}/{id}`, which Spotify documents as launching the app when installed. It works on a tap; for automatic opening, `spotify:{kind}:{id}` raises the system "Open in Spotify?" prompt
  - desktop: the web link (it hands over to the desktop app if installed)
- [x] Job page (Phase 9): when the build finishes on a phone, the page tries to open the app automatically, and a big "OPEN SPOTIFY" button stays as the fallback. Browsers may block an app switch without a tap (Chrome on Android needs a user gesture for `intent://`), so the button is the guaranteed path. Setting: "Open Spotify when my playlist is ready" (default on for phones)
- [x] Every track and playlist link on phones uses `open_link`, so a tap goes into the app
- [x] App not installed: "GET SPOTIFY FREE" link to the store, per Spotify's guidelines
- [ ] (Not built: optional) "Play on this phone": `GET /me/player/devices`, then `PUT /me/player/play` with `context_uri` on the phone (Premium only; explain otherwise)
- [ ] (yours) Needs the HTTPS deploy from plan.md Phase 8. A phone can't reach 127.0.0.1, and Spotify allows `http` redirect URIs only on loopback

Tests (`tests/test_p17_mobile.py`)
- [x] `open_link` for iPhone Safari, Android Chrome, Samsung Internet and desktop user agents → expected form, ids URL-safe, fallback URL present on Android
- [x] job-done page on a phone UA contains the auto-open script and the button; on desktop, only the button
- [x] setting off → no auto-open script
- [ ] (yours) manual, on the deployed URL: iPhone with Spotify → app opens on the playlist (one system prompt at most); Android with Spotify → app opens with no prompt; a phone without Spotify → store link

Gate: `pytest tests/test_p17_mobile.py` green, and both manual phone checks pass.

Done 2026-10-24 apart from the phone checks, which need the HTTPS deploy running (the moodify.mish4mish.com tunnel was down on 2026-10-24). How it works: when a build finishes, the job page sends the browser to `/playlists/{id}?ready=1`. Only that first view, on a phone with the setting on, carries the script. The script opens the app once per tab (sessionStorage), using `spotify:playlist:…` on iPhone and the intent link on Android. Reloads and visits from History never auto-open. Every Spotify link (OPEN SPOTIFY, track rows, playlist lists) goes through `links.for_url`, so on Android a tap uses the intent link. On iPhone it stays the open.spotify.com link, which universal links hand to the app on a tap. "GET SPOTIFY FREE" shows on phones only and links to the Play Store or App Store. Ids that aren't plain base62 only get a quoted web link. The setting is on `/settings`, default on (migration 9, `users.open_spotify`). 30 tests. Checked in headless Chrome with an iPhone user agent, light and dark.
Your checks: on the deployed URL, make a playlist on an iPhone with Spotify (the app should open after at most one prompt), on an Android phone with Spotify (it opens, or the button does it in one tap), and on a phone without Spotify (the store link).

---

# Part 2: Friends (Phases 18–24)

Asked on 2026-10-23: everyone on Moodify knows each other, so put everyone's music and taste in one central database; show each person's activity and a multi-dimensional graph of what they listen to, with animations; let anyone make a **Moodify Blend** from a prompt and several friends' tastes; and let someone use a friend's taste to hear "what they would listen to".

These phases need only Phases 9–13, which are done, so they can be built before 14–17. Phase 24 also shows likes and ratings once Phase 14 exists. Real multi-person checks need friends on the app, so they need the HTTPS deploy (plan.md Phase 8) and each friend on the Spotify allowlist. Until then, live tests pair your account with a **synthetic friend**: a stored taste profile with no Spotify account behind it. Blends and "hear it like a friend" read only stored profiles, never a friend's Spotify token, so a synthetic friend exercises the same code.

| Request | Read as | Phase |
|---|---|---|
| (needed by everything below) | Nothing about a person reaches anyone else until they switch it on. They can see who used their taste, and undo it | 18 |
| All music options and taste in a central database | One shared library: each person's tracks from every source (top, recent, liked, own playlists, Moodify's), weighted, refreshed daily, never kept indefinitely | 19 |
| Moodify Blend: pick people, type a prompt | One playlist from several friends' tastes plus a prompt, with a fair share for each person, saved in the requester's account | 20 |
| Use someone else's account to understand what they'd listen to | "Hear it like a friend": a playlist built from a friend's taste, saved in your own account. Never their login, never their account | 21 |
| A multi-dimensional graph of what each person listens to | Every track scored on a few sound dimensions (energy, mood, texture, groove, vocals, era, genres), per-person profiles and taste-match scores | 22 |
| (the graph itself) | The taste map: per-dimension dot rows, per-person track maps, a match grid and genre bars, animated | 23 |
| Animations showing each user's activity | An activity feed and mood timeline per person, with a "replay the week" animation | 24 |

Open questions (defaults stand until answered):
5. Should a Blend be created **public**, so its members can open the link? Spotify has no "shared with these people" setting, and a private playlist only opens for its owner. Default: private, with a checkbox to make it public.
6. Keep the name **Moodify Blend**? Spotify has a feature called Blend. No rule forbids the word, but Policy III.11 bans replicating a Spotify experience without "independent value". The prompt, sections and lengths are that value (Phase 20). Default: keep it.

## Rules Part 2 is built around

Read on 2026-10-23 from Spotify's Developer Policy (effective 15 May 2025), Developer Terms v10 and the Web API reference:
- **Consent to share between users** (Terms V.4): "you must obtain explicit consent from the user who provided the data to us before using it for any purpose other than displaying it back to the user on your SDA." Showing Sam's taste to Amish, or using it in Amish's blend, is such a purpose. Phase 18 makes that consent explicit, per use, and revocable.
- **No indefinite storage** (Terms IV.3.1.1): "Do not store Spotify Content indefinitely", and no "databases of Spotify Content, other than as strictly necessary to operate your SDA". The central library expires after 30 days without a refresh and is deleted on disconnect (Phase 19).
- **Independent value** (Policy III.11): "Do not build products or services that mimic, or replicate or attempt to replace a core user experience of Spotify... must add independent value." The Blend is prompt-driven, sectioned and length-aware; Spotify's isn't (Phase 20).
- **Attribution** (Policy II.4): every track shown, including points on charts and tooltips, links back to Spotify with its icon.
- **Profiling and AI** (Policy III.13 and III.14) apply even more here: taste profiles are shown to other people, and track data goes to the LLM for scoring. This is covered by the 2026-10-22 decision for the friends-only build; revisit it if Moodify grows beyond friends.
- **Genres:** the artist object's `genres` is marked Deprecated, and Get Several Artists is removed (Feb 2026), so genres would cost one call per artist and may disappear. The LLM tags genres instead (Phase 22).
- **At most 5 users** in Development Mode, so a blend has 2 to 5 people and the charts never show more than 5.

## Charts: how they're drawn

Phases 23–24 follow the `dataviz` skill:
- The form is picked by the data's job before any colour.
- Categorical hues come in a fixed order, and the palette is checked with the skill's validator (`scripts/validate_palette.js`) for both light and dark surfaces.
- Scatter-type charts cap at 3 colours. With up to 5 people, each person gets a small-multiple panel, or one person is in the accent colour and the rest are grey.
- Radar charts are not used: their areas distort comparisons and their shape depends on axis order. Dot rows on one shared axis do the job instead.
- Every chart has hover tooltips with hit areas of at least 24 px, a legend plus direct labels, and a table view.
- Animation explains change (points move to their new place); it never carries data on its own. It is off under `prefers-reduced-motion`.
- Everything is drawn in SVG by one small vanilla JS file per page, keeping plan.md's no-framework rule, with no CDN.

---

## Phase 18: Sharing, only with consent

Goal: nothing about a person reaches another person unless they switched it on, and they can see every use and take it back.

Build
- [x] `sharing(spotify_id PK, show_taste, allow_blends, allow_listen_as, updated_at)`: three switches, all **off** for every user, existing and new (migration)
- [x] `/sharing`: the three switches in plain words, each saying exactly what it shares:
  - **Show my taste to friends:** taste profile, top artists, the taste map and activity feed (playlist titles, never prompts)
  - **Let friends include me in Blends:** stored taste and library used in their blend playlists
  - **Let friends hear music like me:** stored taste used for playlists saved in their account
- [x] Friends are the other people on this Moodify (at most 5) with at least one switch on. `/friends` lists them and what each shares
- [x] `taste_access(id, owner, viewer, kind, playlist_id, at)` logs every use of someone's data: a map view, a blend, a "hear it like". The owner sees "Who used my taste" on `/sharing`; nobody else sees it
- [x] "Hide from friends" on any playlist. Playlists whose brief has `support_note` are **never** shown to anyone else; that is enforced in the query, not the template
- [x] Never shared, whatever the switches say: prompts, reviews and ratings (Phases 14–15), memory lessons (Phase 16), support-note playlists, Spotify tokens
- [x] Switching off works on the next request. Playlists already in someone else's Spotify stay there (they're theirs); the switch text says so
- [x] Disconnect deletes the user's sharing row and the access log in both directions
- [x] One permission check, `sharing.allowed(owner, viewer, kind)`, used by every Part 2 route

Tests (`tests/test_p18_sharing.py`)
- [x] a new user, and every user who existed before the migration, has all three switches off and appears nowhere: friends list, map data, blend picker, "hear it like"
- [x] table test, switch × feature: each switch grants exactly its own feature, and the others still answer 403
- [x] switching off → the next request is refused and the person is gone from every list
- [x] a support-note playlist never appears in any friend-facing query, with `show_taste` on and the playlist not hidden
- [x] a hidden playlist never appears to friends, but still appears to its owner
- [x] every use writes one `taste_access` row; only the owner can read their log
- [x] disconnect leaves no sharing or access rows mentioning the user, in either column

Gate: `pytest tests/test_p18_sharing.py` green, and you read `/sharing` and agree it says plainly what is shared.

Done 2026-10-24 apart from your read of `/sharing`. A person without a `sharing` row has every switch off, so users from before the migration and new users need no row (migration 10 adds the tables and `playlists.hidden`). `sharing.allowed(owner, viewer, kind)` reads the switch on every call. `sharing.visible_playlists` is the only friend-facing playlist query: it selects id, title, date and track count, and filters hidden and support-note playlists in SQL. A friend's page shows their stored taste summary, never refreshed with their token. Every look at a friend's taste writes a `view` row, and the owner's "Who used my taste" groups the rows by person and kind. "Friends" joined the header, which still fits at 390 px. 24 tests.

---

## Phase 19: The shared music library (central database)

Goal: one place holding each person's music (what they play, save and keep), in a shape that compares across people, without keeping Spotify data longer than the app needs it.

Build
- [x] `tracks(uri PK, name, artists_json, duration_ms, explicit, first_seen, last_seen)`: track details stored once, shared by everyone who has the track
- [x] `library(spotify_id, uri, source, weight, refreshed_at)`. Sources: `top_short`, `top_long`, `recent`, `liked`, `own_playlist`, `moodify` (tracks in playlists Moodify made for them). Default weights 3, 2, 2, 1, 1, 1, kept in one table in `src/library.py`
- [x] Filled from Phase 10's reads at each taste rebuild, with **no extra Spotify calls**. A user's rows are replaced in one transaction, so a song removed from Liked Songs leaves the library
- [x] At most 600 rows per user, the heaviest kept
- [x] Retention: rows not refreshed for 30 days are deleted at startup; tracks no one references are deleted with them; disconnect deletes the user's rows, and orphan tracks follow
- [x] `library.for_user(id)`, `library.top_artists(id, n)`, `library.common(a, b)`: shared tracks and artists, weighted
- [x] The known-songs pool (Phase 10) reads from the library instead of its own JSON copy, one source of truth (migration drops `users.taste_pool_json` after the switch)

Tests (`tests/test_p19_library.py`)
- [x] a rebuild writes every source with its weight; a track in several sources keeps one row per source and its weights add up in `for_user`
- [x] a refresh replaces: a song gone from Liked Songs is gone from the library
- [x] the 600-row cap keeps the heaviest
- [x] `common(a, b) == common(b, a)`; no overlap gives an empty list
- [x] rows 31 days old are pruned at startup, rows 29 days old stay; orphan tracks go too
- [x] disconnect leaves no library rows for the user and no orphan tracks
- [x] the Spotify call count of a rebuild is the same as in Phase 10 (assert per route)
- [x] the pool built from the library equals the Phase 10 pool for the same reads
- [x] `live`: your rebuild prints rows per source and stays at or under 600

Gate: `pytest tests/test_p19_library.py` green, and your library's counts per source look right to you.

Done 2026-10-24 apart from your look at the counts. Live: your library holds 391 rows: 10 top, 34 recent (50 plays, some repeated), 200 liked, 53 from your own playlists and 94 from Moodify's. Top artists by weight: Drake 24, Don Toliver 21, Gunna 16, Travis Scott 15. The 150-song pool read back from the library equals the one built from the reads. Built differently from the list above: Spotify's top tracks are read for the 6-month range only, and this phase adds no calls, so there's one `top` source (weight 3) instead of `top_short`/`top_long`. Weights: top 3, recent 2, liked 1, own playlist 1, Moodify 1. `library.position` keeps each source's order, so the pool can be rebuilt exactly. A failed read leaves the library as it was. Migration 11 clears every cached profile, so each one is rebuilt once and fills the library, then drops `users.taste_pool_json`. The real database was backed up before it ran. Orphan tracks are deleted in code (after a replace, at startup, on disconnect), not by a trigger: a trigger would also drop a track another refresh was about to re-add, together with its Phase 22 features. 18 tests.

---

## Phase 20: Moodify Blend

Goal: pick friends, type a prompt, get one playlist that fits the prompt and everyone's taste fairly.

Build
- [x] `/blend`: choose 1–4 friends who allow blends (you are always in it), and a prompt box with the same tolerance as Phase 11. Optionally lean toward one person: equal by default
- [x] Blend taste: each member's summary condensed to at most 600 characters, labelled with their first name. The blended known-songs list puts **common ground** first (tracks in two or more members' libraries), then each member's heaviest tracks in turn, each labelled with whose they are
- [x] Hard rules are the **union** of all members': if anyone avoids country, the blend avoids it; anyone's "no explicit" applies to everyone. Familiarity refers to the blended list
- [x] Brief and sections work as in Phases 11–13. Each suggested song says who it's mainly for (`for`: a member, or `everyone`)
- [x] Fair share: each member's songs, counting their own and shared ones, fill at least (1/n − 10%) of the minutes. A member below that gets one top-up call, as short sections do
- [x] Saved **only in the requester's account**, as "Moodify Blend: Amish × Sam × Priya: <title>", with members named in the description. Members' Spotify tokens are never used, only their stored library and profile
- [x] `blends(id, playlist_id, owner)` and `blend_members(blend_id, spotify_id)`; each member's access log gets a row; members see "Amish made a blend with you" in their activity (Phase 24)
- [x] Blend page: each track marked with whose taste it came from (initials) or "common ground"; a share bar per member (the dataviz rules apply: one colour per member in a fixed order)
- [x] Public/private as in open question 5

Tests (`tests/test_p20_blend.py`)
- [x] only friends who allow blends can be picked; anyone else → 403, nothing built
- [x] **no member's refresh token is ever used**: the token route sees only the requester's
- [x] the union of avoid rules reaches the brief and suggestion prompts; one member's "no explicit" filters explicit tracks
- [x] common-ground songs come first in the known list, each member's songs are labelled
- [x] fair share holds within tolerance; an under-served member triggers exactly one top-up call, for that member
- [x] playlist name and description list every member; one access-log row per member
- [x] a member who switched off before the build can no longer be picked; the build checks consent once, at the start
- [x] `live`: you plus a synthetic friend whose taste is very different (jazz and classical) with "sunday cooking" → each of you at least 40% of the minutes, judged by whose list each track came from

Gate: `pytest tests/test_p20_blend.py` green, the live blend passes, and once a real friend is on the deployed app, one real blend sounds fair to both of you.

Done 2026-10-24 apart from the real-friend blend. Live: you plus a synthetic jazz-and-classical friend, "sunday cooking", gave 15 tracks: you 55% (31 min), the friend 45% (26 min), each above the 40% floor, and no top-up was needed. The playlist is in your Spotify, private. The synthetic friend was deleted afterwards.

How it was built:
- **One build core.** `playlist.make(user, mood, Recipe)` serves your own playlists (`build`), Blends and Phase 21. A Recipe carries the taste profile, memory, name, description note, rule tidy-up and an optional fair-share balance.
- **Friends' data.** It comes only from what's stored (`taste.stored_summary`, onboarding, library), never their login. A test checks that every token refresh carries only the requester's refresh token.
- **Credit.** A track counts for whoever has it in their library, else for whoever the model's `for_whom` names ("everyone" or no name: all of them). Shared songs count fully for each person.
- **Placing.** Songs are tried in turns (songs for everyone first, then one per person), so placement can't fill a section with one person's songs.
- **Top-up.** It asks for songs for the under-served person and swaps them in for tracks of people who stay above their floor without them. A section stays inside its length window.
- **Avoid rules.** Phrases lose a leading "no"/"avoid" ("no country" → "country"). A phrase that means "no explicit" sets that rule instead of being listed twice.
- **Storage.** Migration 12 adds `blends`, `blend_members` and `blend_credits`. A friend who disconnects takes their rows and credits with them; the owner's playlist stays.
- **The page.** Each track shows the initials of whose taste it came from, or "common ground". A share bar per person shows minutes against their fair minimum, with a table view. Colours are the dataviz reference palette's first five slots, in fixed order, checked with its validator on Moodify's own surfaces (#ffffff, #121212). Light mode warns on contrast for slots 3–5, so every bar carries its name and value as text.

25 tests.

---

## Phase 21: Hear it like a friend

Goal: "what would Sam put on for this?": a playlist built from a friend's taste, saved in your account, without touching theirs.

Build
- [x] On `/friends/{id}`, if they allow it: "Make a playlist the way Sam would hear it" with a prompt box
- [x] The build swaps in the friend's profile: their summary, their known songs from the library, their avoid rules. Your onboarding answers don't apply; familiarity means songs *they* know
- [x] Only their taste profile and library are used. Their prompts, reviews, ratings, lessons and support-note playlists are never read (the build takes a `Profile`, nothing else)
- [x] Saved in **your** account as "Moodify: <title> (as Sam hears it)"; the page says whose taste was used
- [x] Logged for the friend: "Amish made 'Sunday cooking' the way you'd hear it"
- [x] The friend's Spotify token is never used, and nothing is written to their account

Tests (`tests/test_p21_listen_as.py`)
- [x] needs `allow_listen_as`; otherwise 403 and nothing built
- [x] the brief and suggestion prompts contain the friend's summary and **not** yours (assert both); the known list is the friend's
- [x] the friend's avoid rules apply; yours don't
- [x] only your token is used; the playlist name ends "(as Sam hears it)"; one access-log row
- [x] the build function cannot receive anything but a `Profile` (signature test), so private data can't leak in later phases
- [x] `live`: a synthetic jazz-and-classical friend, prompt "gym": print the tracks for a manual look, and check the suggestion prompt carried the friend's taste

Gate: `pytest tests/test_p21_listen_as.py` green, and the live result sounds like the synthetic friend, not like you.

Done 2026-10-24 apart from your listen. Live: "gym" the way a synthetic jazz-and-classical friend hears it gave upbeat jazz (Take the "A" Train, Blue Rondo à la Turk, Night Train, So What, Take Five, …) with none of your rap. The brief and suggestion prompts held the friend's summary and not yours (https://open.spotify.com/playlist/5Riic2Pkgg23OI5eDV8nXr, private, in your account). How it works: `listen_as.build_as(user, mood, profile, trace)` runs the Phase 20 build core with the friend's `taste.Profile`, which now also carries their name, familiarity answer and avoid rules. A non-Profile raises TypeError. Your memory, your onboarding and your no-repeat list don't apply. Consent is checked when the build starts. Migration 13 adds `playlists.heard_as` (SET NULL when the friend leaves), and the playlist page says whose taste was used. 12 tests.

---

## Phase 22: Taste dimensions

Goal: every track in the library gets a few numbers that describe how it sounds, so people can be compared, matched and drawn.

Build
- [x] Dimensions, judged by the LLM because Spotify's audio features are deprecated:
  - **energy** 1–10
  - **mood**: 1 dark to 10 bright
  - **texture**: 1 acoustic to 10 electronic
  - **groove**: 1 still to 10 danceable
  - **vocals**: 1 instrumental to 10 vocal-led
  - **era**: the decade of release
  - up to 3 **genre tags**, and the main **language**
- [x] `track_features(uri PK, energy, mood, texture, groove, vocals, era, genres_json, language, model, judged_at)`, judged in batches of 50 new tracks per structured call through `prompts.ask`. A track is judged once; features go when their track goes (Phase 19 retention)
- [x] A batch that fails isn't cached; its tracks are retried at the next refresh. Values outside their range are clamped, and nonsense genres are dropped
- [x] Per person: the weighted mean and spread of each dimension over their library, genre shares (top 6 plus Other), and a decade histogram. Pure functions over at most 600 rows, computed on demand
- [x] Taste match between two people, 0–100: cosine similarity of the standardized dimension means, combined with the weighted overlap of genre shares. Pure, symmetric
- [ ] (Out of scope, as planned) Phase 13's ordering may use judged energy for known tracks later

Tests (`tests/test_p22_dimensions.py`)
- [x] 120 unjudged tracks → 3 calls of at most 50; already-judged tracks → 0 calls
- [x] out-of-range values clamped, unknown fields ignored, a failed batch leaves its tracks unjudged
- [x] table tests for weighted mean, spread, genre shares and the decade histogram
- [x] match: identical people 100, opposite profiles near 0, `match(a, b) == match(b, a)`
- [x] features are deleted with their track
- [x] `live`: 12 well-known tracks with obvious orderings ("Clair de Lune" calmer than "Till I Collapse"; "Blinding Lights" more electronic than "Fast Car"; "Hey Ya!" more danceable than "Hurt") → at least 90% of the pairwise orderings right

Gate: `pytest tests/test_p22_dimensions.py` green and the live ordering check passes.

Done 2026-10-24. Live: 18 of 18 obvious orderings right on `gpt-6.1-sol` (Till I Collapse energy 9 > Clair de Lune 2; Blinding Lights texture 9 > Fast Car 3; Hey Ya! groove 10 > Hurt 2; Levels era 2010 > Fast Car 1980), from one call of 12 tracks. How it works: judging runs in the background after each library refresh (`features.judge_soon`), never inside a playlist build. One run at a time; the most widely held tracks go first. `OPENAI_FEATURES_MODEL` can name a cheaper model. Genres are cleaned to at most 3 plausible names (lowercase, short, no sentences or URLs). Era is clamped to 1900 through the current decade. The taste match is 60% how close the standardized dimension means are (cosine, mapped to 0–1) and 40% the overlap of genre shares; identical people score 100, opposite ones about 0. Migration 14 adds `track_features` (cascades with `tracks`). Tests don't judge in the background unless they ask to (a conftest fixture). 26 tests.

---

## Phase 23: The taste map

Goal: one page that shows at a glance what each friend listens to and where tastes overlap.

Build (rules in "Charts: how they're drawn")
- [x] `/map` (you plus friends who show their taste), data from `GET /map.json`, drawn by `static/map.js` in SVG
- [x] **Profiles**: one row per dimension (energy, mood, texture, groove, vocals, era) on a shared axis, one dot per person with a thin spread bar; the selected person in the accent colour, the others grey; tap a name to switch
- [x] **Track map**: tracks on two chosen dimensions (default energy × mood), one small panel per person; point size by weight, common-ground tracks ringed. Nearest-point hover; the tooltip shows the track and artist with the Spotify icon and link
- [x] **Match**: the taste-match grid (at most 5 × 5) as a heatmap in one sequential hue, with values written in the cells, plus a "common ground" list of shared artists and tracks per pair
- [x] **Genres**: one horizontal stacked bar per person, top 6 genres plus Other
- [x] A table view for every chart; legend plus direct labels; dark mode with its own validated colours
- [x] Animation: on load, dots glide from the centre to their places (about 400 ms, eased); changing axes or the selected person morphs points to their new positions. Under `prefers-reduced-motion`, everything appears in place at once
- [x] Opening the map logs one `view` row per person shown

Tests (`tests/test_p23_map.py`)
- [x] `/map.json` holds only you and people with `show_taste` on; switching it off removes them on the next request
- [x] no prompts, reviews, ratings or support-note data anywhere in the JSON (key and value scan)
- [x] every track in the JSON carries an `open.spotify.com` link; values are inside their ranges
- [x] the page is 200 when logged in and redirects to `/login` when logged out; its static files exist
- [x] `map.css` has a `prefers-reduced-motion` rule that turns off transitions
- [x] the palette in `map.css` passes the dataviz validator for light and dark (the test runs `node scripts/validate_palette.js` when Node is installed, and is skipped with a message otherwise)
- [x] screenshots at phone and desktop width, light and dark, checked by eye for collisions and overflow (headless Chrome, as in Phase 7)

Gate: `pytest tests/test_p23_map.py` green, the validator passes in both modes, and the screenshots look right.

Done 2026-10-24. The validator passes for the six genre colours on this site's surfaces (#ffffff, #121212), both modes. Light mode warns on contrast for slots 3–5, so every chart has a legend, labels where they fit and a table view. Screenshots in headless Chrome, at phone width (light and dark) and desktop: no overflow and no label collisions. Your real library (334 songs, judged in 7 calls on 2026-10-24) shows its 200 heaviest songs spread across energy and mood, with genres led by trap 18% and hip hop 14%.

Fixed on the way:
- Spread bars ran past the axis ends.
- A weak jitter hash lined similar track ids up diagonally.
- "↑" rendered as a colour emoji in dark mode.
- The stylesheet's text colour overrode the labels inside genre segments.

Built differently from the list above:
- **Colour.** Track-map panels use one hue for everyone (identity is the panel title), which keeps every plot within the all-pairs limit. Genre colours follow the genre across people: the page-wide top 6 genres, the rest Other.
- **Track map.** It draws each person's 200 heaviest songs. Scores are whole numbers, so dots are spread a little, the same way every time. A tap pins the tooltip with an "Open on Spotify" link.
- **Heatmap.** Quantised into 5 steps of the sequential blue, reversed in dark mode, with near-black or white numbers by step.
- **Library.** It no longer takes songs from hidden or support-note Moodify playlists, since friends now see library data (map, Blends, hear it like a friend).

`scripts/validate_palette.js` is the dataviz skill's validator, copied in for the test. 12 tests (+1 in Phase 19).

---

## Phase 24: Activity, animated

Goal: see what everyone has been up to (playlists, moods over time, blends), with a replay that makes it feel alive.

Build
- [x] `/activity`, newest first, for you plus friends who show their taste:
  - "Sam made *Summer morning → workout → lock in* · 58 min · 3 parts" (the title, never the prompt)
  - blends you're in
  - "Amish made a playlist the way you'd hear it" (shown to that friend only)
  - your own likes and ratings once Phase 14 exists (yours only)
- [x] **Mood timeline**: one strip per person over the last 30 days; each playlist is a mark placed by date, sized by length and shaded by average energy (sequential, one hue); hover shows the title
- [x] **Replay the week**: plays the feed and timelines day by day over about 3 seconds, with pause and step buttons that work from the keyboard; respects `prefers-reduced-motion` (step only)
- [x] Built from existing tables (`playlists`, `playlist_tracks`, `blends`, `taste_access`); no new storage beyond Phase 18's hide flag
- [x] Never shown: prompts, support-note playlists, hidden playlists, reviews, ratings of others

Tests (`tests/test_p24_activity.py`)
- [x] the feed holds only consenting people's visible playlists, newest first
- [x] support-note and hidden playlists never appear; prompt text never appears in the HTML or JSON (only titles)
- [x] "the way you'd hear it" items appear to that friend only
- [x] timeline data: dates within 30 days, energy within 1–10
- [x] reduced-motion rule present; the page is 200 when logged in and redirects when logged out
- [x] screenshots as in Phase 23

Gate: `pytest tests/test_p24_activity.py` green, and the replay looks good on a phone.

Done 2026-10-24 apart from your look on a real phone. In headless Chrome at phone width (light and dark), "Replay the week" hid the feed back to six days ago and brought it back a day at a time over about 3 seconds, then reset its button. Under reduced motion it steps one day per press. Built as planned from existing tables; nothing new is stored. Who sees what:
- **Support-note playlists** never appear, not even to their maker.
- **Hidden playlists** appear only to their maker.
- **Blends** appear only to the people in them, even when the maker doesn't show their taste.
- **"The way you'd hear it" playlists** appear only to their maker and that friend.
- **Likes and ratings** appear only to their owner.

The feed is server-rendered HTML, so it works without JavaScript; `static/activity.js` draws the timeline from `/activity.json` and runs the replay. A timeline mark's energy is the average of its songs' energy, else the brief's. 12 tests.

---

# Part 3: Every music service (Phases 25–29)

Asked on 2026-10-25: make Moodify work for friends on YouTube Music and Apple Music, not only Spotify. Decided the same day:
- **Any service can sign in.** A friend with only YouTube Music (or, later, Apple Music) gets what a Spotify friend gets: a taste read from their own service, playlists saved there, likes, memory, and the friends features with people on other services. One service per Moodify account.
- **YouTube Music first.** Apple Music needs the Apple Developer Program ($99 a year) for its signing key. Phase 29 is written now and built once you've enrolled.
- **YouTube Music goes through the official YouTube Data API** for sign-in, reads and writes. Songs are found with `ytmusicapi`'s no-login search.

| Request | Read as | Phase |
|---|---|---|
| (needed by everything below) | A Moodify account is no longer a Spotify account: `user_id` everywhere, plus which service it uses. Nothing changes for current users | 25 |
| (needed by everything below) | Every Spotify call goes through one music-service interface. Pages name the user's own service; Spotify-only features (the queue) only show for Spotify | 26 |
| YouTube Music | Sign in with Google. Taste comes from your YouTube likes, playlists and subscriptions. Playlists are saved to YouTube, where YouTube Music shows them. Likes there are optional | 27 |
| Friends on different services | The library, Blends, "hear it like a friend", taste dimensions and the map compare songs, not service IDs | 28 |
| Apple Music | Sign in with Apple plus MusicKit. Taste comes from recently played, heavy rotation and your library. Playlists are saved to your Apple Music library | 29, after you enroll |

Open questions (defaults stand until answered):
7. Can one person connect two services (say Spotify and YouTube Music) to one Moodify account? Default: no. One service per account; a second service means a second account. Listed under Later.
8. Should a ♥ in Moodify also like the song on YouTube? YouTube's policy wants express consent before liking on someone's behalf. Default: off for YouTube users, with the choice on the onboarding page and in settings.
9. Cross-service Blends touch two Spotify rules (below). Default: build them, and `/sharing` says your songs can end up in a friend's playlist on another service. The alternative: Blends and "hear it like a friend" only between people on the same service.

## What each service allows

Checked on 2026-10-25.

**YouTube Music** has no public API. What Moodify can use:
- **YouTube Data API v3**, with Google sign-in. The scopes are `https://www.googleapis.com/auth/youtube`, plus `openid profile` to know who you are.
  - It can create playlists, which YouTube Music lists in your library.
  - It can read your liked videos (`videos.list?myRating=like`), your playlists and their items, and your subscriptions.
  - It can like and unlike (`videos.rate`).
  - It **can't read listening history**, and has nothing like "top tracks".
- **Quota** is per Google Cloud project, so every YouTube user of Moodify shares it.
  - The allowance is 10,000 units a day, reset at midnight Pacific time (12:30 or 13:30 IST).
  - Reads cost 1 unit. `playlists.insert`, `playlistItems.insert` and `videos.rate` cost 50 each, and songs are added **one call per song**.
  - A 30-song playlist costs 1,550 units, so about 6 playlists a day across all YouTube friends.
  - `search.list` has its own bucket of 100 calls a day, which is why Moodify doesn't search through the API.
  - Google takes requests for more quota through an audit form. That's a task for you, listed under Later.
- **`ytmusicapi` search without a login** finds YouTube Music songs at no quota cost. Each result has the title, artists, length, explicit flag and video id. On 2026-10-25, `YTMusic().search("Midnight City M83", filter="songs")` returned M83's track first.
  - It's unofficial: it calls YouTube Music's internal web API, can break when YouTube changes that, and isn't covered by YouTube's API terms.
  - Logged-in `ytmusicapi` (history, library) has been broken since September 2026: issue #813 is open, and the maintainer sees no way forward. So Moodify uses it for search only.
- **Google sign-in limits.**
  - While the OAuth consent screen is in **Testing**, refresh tokens for the `youtube` scope expire after 7 days.
  - Publishing it without Google's verification avoids that, but friends see an "unverified app" warning, and at most 100 users can sign in.
  - Verification (privacy policy, home page on the domain, a demo video) removes the warning.
- **YouTube API Services Developer Policies** add rules that Spotify's don't have:
  - III.A.1, III.A.2: link to YouTube's Terms of Service. Users agree to a privacy policy before using Moodify, and that policy links Google's Privacy Policy and says access can be revoked in Google's security settings.
  - III.D.2.3.a: on disconnect, revoke the token through Google and delete the data within 7 days. Moodify deletes at once.
  - III.E.4.c: API data is kept for at most 30 days unless refreshed. The library's 30-day expiry already does this.
  - III.E.3.d and I.2: writes on someone's behalf (playlists, likes) must be clearly started by the user and expressly consented to. That's why likes are off by default (question 8).
  - III.F.2.a: pages showing YouTube content make it clear that YouTube is the source, with YouTube's branding.
  - III.E.4.h: no "new or derived data or metrics" from API data. Taste summaries and Phase 22's dimensions are derived data for songs read through the API. That's the same kind of call as the 2026-10-22 decision on Spotify's III.13 and III.14. It holds for the friends-only build and is revisited if Moodify grows.

**Apple Music** (Phase 29):
- It has an official API. On the web, **MusicKit on the Web** shows Apple's sign-in and hands the page a Music User Token. Personal endpoints need an Apple Music subscriber.
- An Apple Developer Program membership ($99 a year) is needed for the MusicKit key that signs developer tokens. Those are ES256 JWTs that last at most 6 months.
- Apple never says who the user is, so Apple users sign in with **Sign in with Apple** (same membership) to get a stable account id.
- Reads: recently played tracks, heavy rotation, library songs and playlists, recommendations, ratings. Writes: create a library playlist, add tracks, love a song.
- Music User Tokens can't be refreshed. Developer forum reports (not Apple's docs) say they last about 6 months, after which the user reconnects.
- No user cap like Spotify's 5.
- Apple's licence limits MusicKit to giving users access to their Apple Music subscription. Read the MusicKit sections of the Developer Program License Agreement in full, including 3.3.11 on AI, before building Phase 29.

**Spotify**, again, for features that cross services (Developer Policy, re-read 2026-10-25):
- III.9: "Do not build an SDA that enables the transfer of data to another service, except for the purpose of enabling a user to transfer their personal data, or the metadata of the user's playlists to another service."
- III.5: "Do not create any product or service which is integrated with streams or content from another service."
- Example: a YouTube user makes a Blend with a Spotify friend. The friend's songs end up in a YouTube playlist, so the friend's data goes to another service at someone else's request. It's the same kind of call as 2026-10-22, so Phase 28 builds it behind the friend's Blend switch, whose text says so (question 9).

## How Moodify changes

- **Accounts.** `users.user_id` (was `spotify_id`) is Moodify's own id, and `users.service` says which client acts for the user. Current users keep their value, so sessions, friend links and OpenAI cache keys keep working. New users get `youtube:<Google sub>` and `apple:<Apple sub>`.
- **Songs.** Every track still has a `uri`, prefixed by its service: `spotify:track:…` as today, `youtube:track:<videoId>`, `apple:track:<catalog id>`. Comparisons across services use a **song key**: `normalize_title(name)|normalize_artist(first artist)`, the normalization `src/match.py` already uses to accept search results.
- **One interface, several clients.** `src/music/` holds the `MusicService` protocol, with these parts:
  - `listening()` reads the user's taste.
  - Playlists and songs: `search_track`, `create_playlist`, `add_items`.
  - Likes: `liked`, `like`, `unlike`.
  - The optional `queue`.
  - The service's links, limits and name.

  `src/spotify/` becomes the first implementation, with no change in behaviour. Build code calls `music.for_user(user)` and never imports a client.
- **Quota is counted per service** in `service_quota(service, day, units)`. Before a YouTube build writes anything, it checks that it can afford its writes. If it can't, it fails with a clear message instead of leaving a half-made playlist.

---

## Phase 25: Accounts that aren't Spotify accounts

Goal: the database and the code stop assuming a user is a Spotify account. Nothing visible changes.

Build
- [x] Migration 15:
  - Rename `spotify_id` to `user_id` in every table. `ALTER TABLE … RENAME COLUMN` also rewrites the foreign keys and indexes that point at it.
  - Add `users.service TEXT NOT NULL DEFAULT 'spotify'`, checked to be `spotify`, `youtube` or `apple`.
  - Rename the Spotify-named columns: `users.likes_to_spotify` → `likes_to_service`, `users.open_spotify` → `open_app`, `playlists.spotify_url` → `url`, `track_likes.added_to_spotify` → `added_to_service`.
  - `schema.sql` gets the same names. The live database is backed up before the migration runs.
- [x] Existing users keep their id values, so their sessions, friend links and history keep working
- [x] In the code, `spotify_id` becomes `user_id` wherever it means the Moodify user. It stays `spotify_id` only where it really is a Spotify id (links, the Spotify client). The session key becomes `user_id`; a cookie that still holds `spotify_id` is read once and rewritten
- [x] `users.refresh_token` keeps its name and holds whatever credential the service's client acts with (for Apple, the Music User Token)

Tests (`tests/test_p25_accounts.py`)
- [x] a version-14 database with users, playlists, likes, lessons, sharing, library, Blends and features upgrades with every row kept and `PRAGMA foreign_key_check` empty. A fresh database from `schema.sql` has the same tables and columns
- [x] after the upgrade, no table has a `spotify_id` column
- [x] a session cookie from before the upgrade still logs the user in
- [x] disconnect still deletes every row about the user, in every table
- [x] the fast suite passes, with assertions changed only for the renamed columns

Gate: fast suite green, and an upgraded copy of the live database shows your history, memory and map as before.

Done 2026-10-25. Migration 15 is fourteen `RENAME COLUMN` steps plus `users.service`. SQLite (3.53 here, 3.46.1 in the image) rewrote every foreign key and index that named the old columns, so nothing was rebuilt by hand. Checked on a read-only copy of the live database (4 users, 16 playlists, 449 library rows): every row kept, `foreign_key_check` empty, and history, memory, a playlist, a Blend, the map, activity, friends, sharing and settings all open from it with the network blocked. That check found an older bug, fixed separately in 5ee761e: `/activity` answered 500 for a Blend whose only friend had disconnected (the Phase 20 live test's synthetic friend). Old migrations and `tests/fixtures/schema_v0.sql` keep `spotify_id`, since they describe older databases. `SpotifyClient.for_user` passes `user_id` as the Spotify id: for a Spotify user they're the same value. The live database still has to be backed up when this is deployed. 7 tests.

---

## Phase 26: One interface for every music service

Goal: Spotify works exactly as before, but through an interface that a second service can implement.

Build
- [x] `src/music/__init__.py` holds:
  - `Track`, moved from `src/spotify/client.py`, with a new `key` (the song key).
  - `Playlist` and `UserPlaylist`.
  - `Listening`, moved from `src/taste.py`.
  - The `MusicService` protocol.
  - `for_user(user)`, and `info(service)` with the name, logo, app links, "get the app" links, limits and capabilities.
- [x] Errors: `ServiceError(service, status, body)`, with `ReauthRequired` and `QuotaExceeded` under it. `SpotifyError` and `SpotifyQuotaExceeded` stay as Spotify's subclasses, and the handlers catch the general ones
- [x] Each service reads its own taste: `taste.read_listening` moves into the Spotify client as `listening()`, and `taste.py` only formats. A service that can't fill a summary section leaves it out, as an empty section is left out today
- [x] `src/playlist.py`, `src/likes.py`, `src/blend.py`, `src/listen_as.py` and `/queue` go through `music.for_user(user)`. Per-service limits move to the service: searches per build, playlist name and description lengths
- [x] Capabilities:
  - `queue`: Spotify only.
  - `likes`: every service.
  - `pool_explicit`: whether the service's known songs carry an explicit flag. Under "no explicit", a known song without the flag is searched instead, as an explicit known song already is.
- [x] `src/links.py` works per service: the web link, the phone link, the auto-open link and "get the app" come from the service of the track or playlist
- [x] Pages and build steps name the user's service:
  - Build steps: "Finding the songs on Spotify" becomes "…on {service}".
  - `_spotify.html` becomes `_service.html`, which picks the service's logo and link for attribution.
  - The queue button only shows where the service can queue.
- [x] Build failures and quota messages name the service

Tests (`tests/test_p26_services.py`)
- [x] the Spotify tests from Phases 2–24 pass, with three wording assertions changed (below)
- [x] a fake second service, defined in the test, runs a full build, a like, and a Blend with a Spotify member, with no Spotify call for the fake user. Only `src/spotify/` and the service switch in `src/music` name `SpotifyClient`
- [x] every page renders for a fake-service user without the word "Spotify", except attribution on Spotify tracks
- [x] for a service without queue, the queue button is absent and `POST /queue/{id}` answers 404

Gate: fast suite green, and a live Spotify build is unchanged: same steps and the same number of Spotify calls.

Done 2026-10-25 apart from the live Spotify build, which I didn't run because it makes a real playlist in your account. Offline, every Spotify test from Phases 2–24 still passes against the same mocked endpoints and call counts. Built as planned, plus:
- **Errors**: `SpotifyReauthRequired` and `SpotifyQuotaExceeded` are both Spotify errors and the general kind, so old `except` clauses still catch them.
- **Build steps** are stored as "Finding the songs on {service}" and matched on the words before the name, so jobs already in the database still show their progress.
- **The known-songs shortcut takes only tracks from the user's own service.** Without that, a Spotify friend's track could have gone into a Blend saved on another service. Phase 28 builds the rest of the cross-service Blend.
- **Copy**: pages say "their account" where a friend's service is unknown ("Playlists friends already made stay in their accounts"). That's the wording change three Spotify tests now assert.
- **The map** (`map.js`) labels each link with its track's service and shows the Spotify icon only on Spotify links.
- **`Track.key`** (the song key for Phase 28) exists already.

10 tests.

---

## Phase 27: YouTube Music

Goal: a friend with only YouTube Music signs in with Google and gets playlists in their YouTube Music library, made from their taste.

Before building (you)
- [ ] A Google Cloud project with **YouTube Data API v3** enabled
- [ ] OAuth consent screen:
  - External, with the scopes `openid`, `profile` and `.../auth/youtube`.
  - App name Moodify, with the home page and privacy policy on `moodify.mish4mish.com`.
  - Publishing status **In production** (unverified), so tokens don't expire after 7 days. Submit for verification later if the warning bothers friends.
- [ ] OAuth client of type "Web application", with redirect URIs `https://moodify.mish4mish.com/callback/youtube` and `http://127.0.0.1:9091/callback/youtube`. Put `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env`

Build
- [ ] `src/youtube/auth.py`: authorization code flow with PKCE plus the client secret (Google's web clients need both), `access_type=offline`, and `prompt=consent` on the first login. The account id is the ID token's `sub` and the name is its `name`. `invalid_grant` raises `ReauthRequired`
- [ ] Login page: "Continue with YouTube Music" next to "Continue with Spotify", with `POST /login/youtube` and `GET /callback/youtube`. Spotify's routes stay where they are, since Spotify's dashboard has `/callback` registered. The data section says what's read from each service
- [ ] `src/youtube/client.py` implements `MusicService`:
  - `listening()` reads, for about 15 units:
    - liked videos (`videos.list?myRating=like`), Music category only, up to 4 pages of 50
    - your playlists (`playlists.list?mine=true`), and the items of the first 5, with lengths from `videos.list` (50 ids per call)
    - subscriptions (`subscriptions.list?mine=true`), as followed artists

    There's no top or recent, and the summary says only what it has.
  - Title and artist of a video: an "Artist - Topic" channel gives the artist and a clean title. Otherwise "Artist - Title (Official Video)" is split. Otherwise the channel name is the artist. `src/match.py` already drops "(Official Video)"-style tails.
  - `search_track`: a `ytmusicapi` search with `filter="songs"` and 10 results, run in a thread with a 10-second timeout. Errors become `ServiceError`. Three failures in a row stop the search step, instead of trying 150 times.
  - `create_playlist` calls `playlists.insert` with the playlist private, a title of at most 150 characters and a description of at most 5,000. `add_items` makes one `playlistItems.insert` call per song.
  - Likes: `videos.getRating` (1 unit), and `videos.rate` with `like` or `none` (50 units).
- [ ] Quota:
  - `service_quota(service, day, units)`, where the day is a Pacific-time date. Every call adds its cost.
  - Before a build starts, its writes for the target length (50 + 50 per song) must fit in what's left. If they don't, the build fails with "YouTube's daily limit for Moodify is used up. It resets at {time in IST}".
  - A 403 `quotaExceeded` from Google gives the same message.
- [ ] YouTube likes default to **off** for YouTube users. The onboarding page asks once ("Also like songs on YouTube when I ♥ them in Moodify"), and settings can change it
- [ ] Links:
  - Playlists open at `https://music.youtube.com/playlist?list=…`, songs at `https://music.youtube.com/watch?v=…`.
  - On Android, an `intent://` link opens the YouTube Music app (`com.google.android.apps.youtube.music`), falling back to the web page.
  - On iPhone, a tap on the music.youtube.com link opens the app.
  - Opening the playlist by itself when it's ready works like Phase 17 where the phone allows it; otherwise there's the big button.
- [ ] Branding: "Open in YouTube Music" with the YouTube Music icon from YouTube's brand resources. Every YouTube track links back to YouTube Music
- [ ] `/privacy` covers:
  - what Moodify stores, what goes to OpenAI, and how to delete it
  - links to YouTube's Terms of Service and Google's Privacy Policy
  - how to revoke access at https://myaccount.google.com/connections

  It's linked from the login page and the footer, and the Google button says that signing in means agreeing to it.
- [ ] Disconnect revokes the Google token at `https://oauth2.googleapis.com/revoke` before deleting the data
- [ ] An account without a YouTube channel may not be able to own playlists. If `playlists.insert` answers `youtubeSignupRequired`, show how to create a channel. Check in the live test whether this happens
- [ ] `ytmusicapi`, pinned, in `requirements.txt`

Tests (`tests/test_p27_youtube.py`: Google APIs mocked with `respx`, `ytmusicapi` stubbed)
- [ ] login: the state check, the code exchange, and a new `youtube:<sub>` user. A second login updates the token without touching onboarding
- [ ] `listening()` handles Topic channels, "Artist - Title" uploads and plain uploads, and leaves out liked videos that aren't music
- [ ] a full build makes one private playlist with one insert per song, counts the units, and saves `youtube:track:` URIs
- [ ] a build that can't afford its writes fails before any write and gives the reset time. A 403 `quotaExceeded` during a build gives the same message
- [ ] likes: with the setting off, there's no `videos.rate` call. With it on, a like rates `like`, and an unlike rates `none` only if Moodify did the like
- [ ] three search errors in a row stop the searching. With nothing found, the build fails with "Nothing matched", never an unhandled error
- [ ] disconnect calls the revoke endpoint and leaves no rows
- [ ] every page for a YouTube user shows YouTube Music links and branding, and no queue button
- [ ] `live`: your Google account signs in and builds a 20-minute playlist that shows in YouTube Music, and the quota counter matches the Google Cloud console

Gate: `pytest tests/test_p27_youtube.py` green, and a playlist made on `moodify.mish4mish.com` with a YouTube account plays in the YouTube Music app on your phone.

---

## Phase 28: Friends on different services

Goal: Blends, "hear it like a friend", the taste map and activity work between a Spotify friend and a YouTube Music friend.

Build
- [ ] `tracks.song_key`, indexed and filled for existing rows. `library.common`, a Blend's common ground, the map's "shared" marks and taste-match scores compare song keys, not URIs
- [ ] `track_features` is keyed by song key, so a song is judged once whichever service it came from. The migration moves existing rows
- [ ] A Blend or "hear it like a friend" playlist goes to the requester's service:
  - Friends' known songs from another service reach the suggestion prompt by name and artist only.
  - A picked song from another service is searched on the requester's service like any suggestion. The known-songs shortcut only applies to same-service tracks.
  - Credits follow the song key.
- [ ] The `/sharing` text for Blends and "hear it like a friend" says the songs can end up in a friend's playlist on Spotify, YouTube Music or Apple Music
- [ ] Moodify can now have more than 5 people (only Spotify's allowlist stops at 5):
  - Blends stay at 2–5 people.
  - The map and activity show you and up to 4 friends, by default the ones you share the most songs with, with a picker for the others.
- [ ] Track links on friends' pages and the map go to that track's own service

Tests (`tests/test_p28_cross.py`)
- [ ] the same song on Spotify and on YouTube has one song key, and `common` finds it between a Spotify user and a YouTube user
- [ ] a YouTube user's Blend with a Spotify friend searches YouTube for the friend's picked songs and makes no Spotify call
- [ ] the features migration keeps every judged song, and a song judged from Spotify isn't judged again when a YouTube user has it
- [ ] with 6 people sharing, the map shows 5
- [ ] every Phase 18 consent test still passes with people on mixed services

Gate: fast suite green, and a live Blend between your Spotify account and a YouTube test account lands in each requester's own service.

---

## Phase 29: Apple Music (after you enroll)

Goal: a friend with only Apple Music signs in with Apple and gets playlists in their Apple Music library.

Before building (you)
- [ ] Join the Apple Developer Program ($99 a year)
- [ ] In Certificates, Identifiers & Profiles:
  - Create a Media ID with MusicKit, and a MusicKit private key (.p8).
  - The team ID and key ID go in `.env`. The key file is mounted read-only from outside the image.
- [ ] Sign in with Apple: a Services ID for moodify.mish4mish.com with the return URL `https://moodify.mish4mish.com/callback/apple`, and its key
- [ ] (me) Read the MusicKit sections of the Developer Program License Agreement in full, including 3.3.11 on AI, and add here what applies

Build
- [ ] Developer token: an ES256 JWT (`iss` the team, `kid` the key, at most 6 months, `origin` the site), cached and renewed a day before it expires. New dependency: PyJWT with `cryptography`
- [ ] Sign in with Apple, as a redirect with `response_mode=form_post`. The ID token is checked against Apple's keys, its `sub` is the account id, and the user id is `apple:<sub>`
- [ ] `/connect/apple`: MusicKit on the Web, loaded from Apple, asks for access and posts the Music User Token to Moodify, which stores it. Someone without a subscription gets a plain "Apple Music needs a subscription" page
- [ ] `src/apple/client.py` implements `MusicService`:
  - The storefront comes from `/v1/me/storefront`.
  - `listening()` reads recently played tracks, heavy rotation, library songs (4 pages) and library playlists (the first 5).
  - Search: `/v1/catalog/{storefront}/search?types=songs`.
  - `create_playlist` calls `POST /v1/me/library/playlists`, and `add_items` calls `POST /v1/me/library/playlists/{id}/tracks` in as few calls as Apple allows.
  - Likes: `PUT` and `DELETE /v1/me/ratings/songs/{id}`.
  - The explicit flag comes from `contentRating` and lengths from `durationInMillis`.
  - A 401 or 403 from a personal endpoint raises `ReauthRequired`, which sends the user to `/connect/apple`, not to a new sign-in.
- [ ] Links: song links on `music.apple.com`, and library playlist links. "Open in Apple Music" uses Apple's badge
- [ ] Disconnect deletes everything, and the page says how to remove Moodify at appleid.apple.com, since Apple has no API for that

Tests (`tests/test_p29_apple.py`)
- [ ] the developer token's claims and its renewal. Sign in with Apple tokens that are expired, for the wrong audience or badly signed are refused
- [ ] `listening()` parsing. Search picks the right song, and a build creates one playlist and adds every song
- [ ] a dead Music User Token sends the user to reconnect, and their data stays
- [ ] `live`: your Apple Music account builds a playlist that shows in the Music app

Gate: `pytest tests/test_p29_apple.py` green, and the playlist plays in Apple Music on a phone.

---

## Later, only if needed

- [ ] Embedding search for lesson recall (ReasoningBank retrieves "using embedding-based similarity search"), if a user ever has more than ~100 active lessons and tag overlap misses relevant ones
- [ ] Streaming progress (server-sent events) instead of the 3-second refresh, if the refresh feels clunky
- [ ] Per-section LLM calls run in parallel, if builds regularly take over 2 minutes
- [ ] Per-track 1–10 ratings, if playlist ratings turn out too coarse
- [ ] Energy check from a second source, if the LLM's energy judgements turn out unreliable in Phase 13 listening tests
- [ ] "Compare": the same prompt as you'd hear it and as a friend would, side by side (Phase 21 twice), if friends ask for it
- [ ] Judged energy from Phase 22 feeding Phase 13's ordering, if the per-build energy guesses disagree with it
- [ ] The pre-action clarification question from Phase 16 (optional there, not built): ask one question when a brief conflicts with an active lesson, auto-continuing after 20 s
- [ ] "Play on this phone" from Phase 17 (optional there, not built): `GET /me/player/devices`, then `PUT /me/player/play` on the phone (Premium only)
- [ ] Replace a playlist's songs in place for "Fix it" (check that Spotify's replace-items endpoint still works for Dev Mode first), if v1/v2 copies pile up
- [ ] Two services on one Moodify account (Part 3, question 7)
- [ ] Move one of your playlists to another service: your own playlist metadata, which Spotify's Policy III.9 allows
- [ ] More YouTube quota through Google's audit form, if friends hit the daily limit (Phase 27)
- [ ] Google verification of the consent screen, if the "unverified app" warning puts friends off (Phase 27)

## Sources

Read on 2026-10-23. Quotes in this file are verbatim from these pages.

Google
- [Architecting efficient context-aware multi-agent framework for production](https://developers.googleblog.com/en/architecting-efficient-context-aware-multi-agent-framework-for-production/), Google Developers Blog (ADK): storage vs working context, stable prefixes, async compaction, reactive vs proactive recall, scope by default
- [ReasoningBank: Scaling Agent Self-Evolving with Reasoning Memory](https://arxiv.org/abs/2509.25140), Google (ICLR 2026): memory items from successes and failures, title/description/content, ≤3 per run, LLM-as-judge, append-only consolidation
- [Vertex AI Agent Engine Memory Bank overview](https://docs.cloud.google.com/agent-builder/agent-engine/memory-bank/overview), Google Cloud docs: extraction, consolidation that lets memories evolve, per-identity isolation, TTL, revisions. The page itself served a generic landing page when fetched, so these points rely on the docs text quoted in search results; re-check before relying on details

Meta
- [Learning Personalized Agents from Human Feedback (PAHF)](https://ai.meta.com/research/publications/learning-personalized-agents-from-human-feedback/) ([arXiv 2602.16173](https://arxiv.org/abs/2602.16173)), Meta Superintelligence Labs, 2026: pre-action clarification, memory-grounded action, post-action feedback updating per-user memory when preferences drift
- [Agent Learning via Early Experience](https://arxiv.org/abs/2510.08558), Meta, 2025: self-reflection grounded in observed outcomes

Spotify (Part 2)
- [Developer Policy](https://developer.spotify.com/policy) (effective 15 May 2025): II.4 attribution, III.11 independent value, III.13 analysis and profiles, III.14 AI
- [Developer Terms v10](https://developer.spotify.com/terms): IV.3.1 storage, V.4 consent to share, V.7 processing only as long as needed, Appendix A 5(c) deletion within 5 days
- [Get Artist](https://developer.spotify.com/documentation/web-api/reference/get-an-artist) (`genres` deprecated) and the [February 2026 changes](https://developer.spotify.com/documentation/web-api/references/changes/february-2026) (Get Several Artists removed)

Model provider (the cache mechanics Moodify actually runs on)
- [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching), OpenAI: 1,024-token minimum, stable prefix first, compaction resets the cache, `prompt_cache_key` and retention options by model generation, cached-token usage fields

Part 3, read on 2026-10-25
- [Quota costs](https://developers.google.com/youtube/v3/determine_quota_cost), YouTube Data API: 10,000 units a day, `search.list` in its own 100-a-day bucket, 50 per playlist, item insert and rating
- [YouTube API Services Developer Policies](https://developers.google.com/youtube/terms/developer-policies): III.A terms and privacy policy, III.C and III.E.3 writes only with consent, III.D.2.3 revoke and delete, III.E.4 30-day storage and no derived metrics, III.F branding
- [Using OAuth 2.0 to access Google APIs](https://developers.google.com/identity/protocols/oauth2): refresh tokens expire in 7 days while the consent screen is in Testing
- [ytmusicapi issue #813](https://github.com/sigma67/ytmusicapi/issues/813): logged-in OAuth broken since September 2026, still open, maintainer's comment of 2026-09-01
- [Apple Music API](https://developer.apple.com/documentation/applemusicapi) and [User Authentication for MusicKit](https://developer.apple.com/documentation/applemusicapi/user-authentication-for-musickit): personal endpoints, the Music User Token, MusicKit on the Web; [MusicKit](https://developer.apple.com/musickit/): keys from Certificates, Identifiers & Profiles
- [Apple Developer Forums, Apple Music API](https://developer.apple.com/forums/tags/apple-music-api): reports that Music User Tokens last about 6 months with no refresh (not Apple's docs)
- [Spotify Developer Policy](https://developer.spotify.com/policy): III.5 integration with other services, III.9 transfers to another service
