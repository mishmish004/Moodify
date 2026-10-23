# Moodify phase plan: Phases 9–17

The next build plan, written 2026-10-23 after the first real run. It continues `plan.md`: same rules, same test layout (`tests/test_pN_<name>.py`, `respx`-mocked, `live` on demand), and the phase numbers carry on from Phase 8. Work phases in order. A phase is done when every box is ticked and its gate is green.

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
   - Search results: `search_cache(key, candidates_json, fetched_at)`, keyed on normalized title + artist, 7 days; shared across users because results don't depend on the user (Phase 12). A cache hit doesn't count toward the per-build search limit
2. **Derived memory** (saves LLM calls and keeps prompts stable)
   - Lessons are extracted and consolidated once, when a review is saved, in the background; never per build (Phase 15). ADK compacts history asynchronously the same way
   - `users.memory_block` and `users.memory_version`: the rendered memory text, rebuilt only when lessons, likes or ratings change (Phase 16)
3. **OpenAI prompt cache** (cheaper and faster calls). The rules come from OpenAI's guide, since OpenAI is the model Moodify calls; ADK gives the same advice ("Stable prefixes: System instructions, agent identity, and long-lived summaries")
   - Each prompt is laid out in three blocks: **static** (system rules, JSON schema, worked examples; identical for everyone) → **per user, slow-changing** (taste summary + `memory_block`, which change at most daily) → **per request** (the prompt, the brief). Only prompts of 1,024 tokens or more are cached, so the static block is written to exceed that
   - Never edit the prefix per request. OpenAI's guide: "Summarization, compaction, or context truncation can change the prefix and reset cache reuse." Compaction happens in consolidation, not in the prompt
   - Models before GPT-5.6: `prompt_cache_key = "moodify:{step}:{spotify_id}"`, plus `prompt_cache_retention="24h"` if available. GPT-5.6 and later route automatically; set `prompt_cache_options.ttl="12h"`. Check which applies to `OPENAI_MODEL` when building Phase 11
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
- [ ] `live`: your account returns recent plays and Liked Songs, and the summary mentions artists from both

Gate: `pytest tests/test_p10_spotify_data.py` green, and after re-login a live playlist has ~30% songs from your library.

Code and offline tests done 2026-10-23. Picks from the pool are checked against title and artist before use, so a wrong number from the LLM falls back to search. Moodify's own playlists are left out of the profile. Live checks wait for a re-login (`.venv/bin/python -m pytest -m live tests/test_p10_spotify_data.py -s`).

---

## Phase 11: Understand any prompt

Goal: every prompt, however it's written, becomes a clear brief that the user can see. Nothing is refused or brushed aside.

Build
- [ ] `src/brief.py`: first LLM call, `read_prompt(prompt, taste, memory) -> Brief` (structured output). `Brief`: `sections[]` (`label`, `feeling`, `energy` 1–10, `minutes | None`, `activity`, `transition_in: cut | blend`), `total_minutes | None`, `hard_rules` (`no_vocals`, `no_explicit`, `no_sudden_loud_changes`, `steady_tempo`, `languages`, `avoid[]`, `max_track_minutes`, `min_track_minutes`), `familiarity`, `tags[]` (activities and feelings, used for memory recall in Phase 16), `support_note: bool`. `OPENAI_BRIEF_MODEL` setting, defaulting to `OPENAI_MODEL`
- [ ] `src/prompts.py`: every LLM call is assembled in the three-block layout from "Caching" (static → per user → per request). The static block, with rules, schema and worked examples, is over 1,024 tokens. Cache settings are chosen for the configured model
- [ ] Mood cap 300 → 2,000 chars. Detailed prompts are often long, and cutting them drops rules
- [ ] Rules in the system prompt, each covered by a fixture prompt in the tests:
  - **Literal and precise prompts** (common among autistic users, useful for everyone): every stated rule is applied exactly as written. "No songs with screaming", "same tempo the whole time", "only songs I know", "nothing with sudden drops" become `hard_rules`, never softened or reinterpreted
  - **Sensory needs**: volume jumps, harsh sounds, busy production and unpredictability can be named as things to avoid, and they are hard rules
  - **Lists, bullet points, info-dumps, mixed languages, slang, typos, emoji** ("lock in", "hella", "🌧️☕") are all read without asking the user to rephrase
  - **Very short or vague** ("meh", "idk", one emoji): one section that fits taste and time of day; never an error
  - **Saying how you feel** ("I feel numb", "kinda empty today"): the playlist meets the feeling where it is. It never pushes darker and doesn't force cheerfulness unless asked. Onboarding "avoid" still applies (e.g. "no sad songs") unless the prompt explicitly overrides it
  - **Wanting to change mood** ("anxious, want to calm down", "need to get hyped"): at least 2 sections following the iso principle. Start close to the current feeling, then move step by step to the target
  - **Conflicts**: something the prompt states outright beats onboarding and memory; something it only implies does not (the 2026-10-23 case: "lock in" doesn't override "no instrumental")
  - **Distress**: if the prompt suggests crisis or self-harm, the playlist is still made, and the result page adds one calm line with a support link. Never a refusal, never a lecture
- [ ] Result page: a "How Moodify read this" box showing sections, length and rules, plus an "Adjust and redo" link that pre-fills the prompt
- [ ] Suggestion call takes the `Brief` instead of the raw mood

Tests (`tests/test_p11_prompts.py`)
- [ ] fixture set of ≥15 prompts covering every rule above (including the 2026-10-23 prompt and a 1,500-char detailed one). With a mocked LLM: each parses, nothing is cut below 2,000 chars, the brief reaches the suggestion call
- [ ] prompt layout: for two different users and prompts, the static block is byte-identical and comes first; per-request text only appears after the per-user block
- [ ] malformed brief from the LLM → fallback single-section brief from the raw prompt, never an error page
- [ ] `support_note` → the support line shows, and the playlist is still created
- [ ] `hard_rules.avoid` and onboarding "avoid" both reach the suggestion prompt
- [ ] `live` eval over the fixture set with the real model: the 2026-10-23 prompt gives 3 sections in order with the workout at 30 min and `transition_in: cut` on the focus section; "anxious, want to calm down" gives ≥2 sections with falling energy; every explicit rule in the literal prompts shows up in `hard_rules`

Gate: `pytest tests/test_p11_prompts.py` green and the live eval passes on ≥90% of fixtures. Failures get read and the prompt fixed.

---

## Phase 12: The right length

Goal: a playlist lasts as long as asked, and its songs fit the request, using Spotify's real track lengths.

Build
- [ ] Target length: sum of section minutes, else `total_minutes`, else 60 min. Hard cap 3 h / 100 tracks (quota)
- [ ] Over-ask: request songs per section for about 1.5× its minutes at ~3.5 min/song, so there is choice after matching
- [ ] `src/fill.py`: pure function `fill(section_minutes, candidates) -> tracks`. Add matched tracks in suggested order until the section is within ±10% or ±3 min, whichever is larger; never cut a song mid-way. Drop tracks breaking `max_track_minutes` / `min_track_minutes`
- [ ] Short section after matching → one extra LLM call for that section only (replaces the global "under 8 → 15 more")
- [ ] `search_cache` (caching layer 1): normalized title + artist → candidates, 7 days
- [ ] Result page and playlist description show total length and each section's length
- [ ] Count searches per build (cache hits excluded) into `job_events`; refuse to start a build that would exceed 150 searches

Tests (`tests/test_p12_length.py`)
- [ ] `fill` table tests: exact fit; overshoot by one long song stays within tolerance or drops it; no candidates; all too long for `max_track_minutes`
- [ ] "workout for half an hour" section → 27–33 min
- [ ] no length in prompt → 54–66 min
- [ ] a short section triggers exactly one extra call, for that section only
- [ ] the same song searched twice within 7 days → one Spotify call; after 7 days → two
- [ ] `live`: "45 minute commute, chill" → 40.5–49.5 min

Gate: `pytest tests/test_p12_length.py` green, and the live run is in range.

---

## Phase 13: Mood switches, exactly orchestrated

Goal: "first X, then Y for 30 minutes, then instantly Z" plays as X → Y → Z, at those times, switching the way the user said.

Build
- [ ] Suggestions come back per section, each song with `energy` 1–10 and a `role` (`opener`, `build`, `peak`, `bridge`, `closer`)
- [ ] Order: sections exactly as in the brief. Inside a section, songs climb or fall toward the next section's energy
- [ ] `transition_in: cut` (prompt says "instantly", "then straight into", "switch"): the new section starts at full target energy with no bridge. `blend` ("slowly", "ease into", default for mood-change arcs): 1–2 bridge songs between the two energies
- [ ] Spotify playlist order = Moodify order (`add_items` keeps batch order; test it)
- [ ] Playlist description (≤300 chars): the timeline, e.g. "0–14 summer morning → 14–44 workout → 44–90 lock in"
- [ ] Result page: tracks under section headings with start times ("Workout · starts at 14:20")

Tests (`tests/test_p13_arcs.py`)
- [ ] 3-section brief → tracks grouped in that exact order; section start times equal cumulative lengths
- [ ] `cut` → no bridge songs at that boundary; `blend` → 1–2 bridge songs
- [ ] energies inside a section move monotonically toward the next section (given mocked energies)
- [ ] `add_items` with 150 ordered URIs → Spotify receives them in the same order across batches
- [ ] description fits 300 chars and contains every section label
- [ ] `live`: the 2026-10-23 prompt gives 3 sections in order, workout 27–33 min, and starts the focus section with no bridge. Listen to it once and note in this file whether the switches felt right

Gate: `pytest tests/test_p13_arcs.py` green, and the live playlist sounds like the prompt.

---

## Phase 14: Likes and ratings

Goal: quick signals on what landed, per track and per playlist.

Build
- [ ] `track_likes(spotify_id, uri, playlist_id, liked_at)`. A ♥ button per track row on the playlist page: a plain POST form that redirects back, with a small JS enhancement so the page doesn't reload
- [ ] Like also saves to Spotify Liked Songs via `PUT /me/library` (`user-library-modify`). Unlike removes from Moodify, and from Liked Songs only if Moodify added it. Setting: "Also add likes to my Spotify Liked Songs" (default on)
- [ ] `playlists.rating` (1–10): a row of 1–10 buttons on the playlist page, changeable any time. The written review is Phase 15
- [ ] History shows rating and number of likes, and can sort by rating
- [ ] All endpoints owner-only: `POST /playlists/{id}/rating`, `POST /playlists/{id}/tracks/{position}/like` and `/unlike`

Tests (`tests/test_p14_feedback.py`)
- [ ] like → row stored, `PUT /me/library` called with that URI; with the setting off → no Spotify call
- [ ] unlike of a song Moodify added → `DELETE /me/library`; of a song already in Liked Songs before → no Spotify call
- [ ] rating 0, 11, "abc" → 422; 1 and 10 accepted; re-rating overwrites
- [ ] another user's playlist or track → 404, nothing stored
- [ ] Spotify 403 on save (scope missing) → the like is still stored, and the page says Liked Songs wasn't updated
- [ ] pages work with JS disabled (form POST + redirect)

Gate: `pytest tests/test_p14_feedback.py` green, and a live like shows up in your Spotify Liked Songs.

---

## Phase 15: Reviews: tell Moodify how it went, in your own words

Goal: after listening, the user writes what they thought, as freely as they wrote the prompt. The review is kept in SQL, fixes this playlist on request, and makes later playlists better. This phase covers steps 3–5 of the loop in "How the agent is structured".

Build
- [ ] Schema: `playlists.review TEXT` (the current review, ≤2,000 chars), `playlists.reviewed_at`, `playlists.review_verdict` (`worked | partly | missed`, filled by reflection). `review_history(playlist_id, text, created_at)` keeps earlier versions when a review is edited
- [ ] `lessons(id, spotify_id, source_playlist_id, title, description, content, kind, tags_json, evidence, status, superseded_by, created_at, last_used_at, use_count)`. `kind` is `preference | pitfall | strategy`; `status` is `active | superseded | expired`. The shape follows ReasoningBank's memory items (title, description, content)
- [ ] Playlist page: a "How was it?" box under the rating: a textarea with examples ("the workout part dragged", "loved the first 10 minutes", "too much Drake", "the switch to focus was perfect"). Reviews get the same tolerance as prompts (Phase 11 rules): slang, lists, emotion, one word or a long paragraph, all fine
- [ ] Saving a review answers immediately and queues reflection in the background. The ADK and Memory Bank designs generate memory asynchronously too, so the user never waits
- [ ] **Reflect** (`src/reflect.py`, one LLM call): inputs are the review, rating, likes, the brief, the track list by section with start times, and the job's `job_events` (what was asked vs what matched). Output: `review_verdict` plus 0–3 lessons. Each lesson must quote its `evidence` from the review and name the section or tracks it's about; lessons without evidence are dropped (grounded reflection, as in Meta's Early Experience). The output separates lessons about this playlist only ("summer-morning section too slow") from lasting preferences ("doesn't like long intros in workout music")
- [ ] **Consolidate** (`src/memory.py`, one LLM call, only when new lessons exist): the new lessons plus existing active lessons with overlapping tags. The answer is one operation per lesson: `ADD`, `UPDATE(id)`, `SUPERSEDE(id)` or `NOOP`. Newer feedback wins when it contradicts older feedback, since preferences drift (PAHF); superseded lessons stay in the table with `superseded_by` as an audit trail. At most 40 active lessons per user: past that, consolidation must merge. Every query filters by `spotify_id`
- [ ] **Fix it with this review**: a button next to a saved review. Builds a new version of the playlist with the review as a correction on top of the original brief. `playlists.parent_id` links v2 to v1; v1 stays in Spotify
- [ ] Until Phase 16 adds selective recall, the next build gets every active lesson (≤1,500 chars) in the per-user prompt block
- [ ] Login page and "What Moodify remembers" (Phase 16): reviews and lessons are sent to OpenAI and kept until you delete them or disconnect

Tests (`tests/test_p15_reviews.py`)
- [ ] review saved → `playlists.review` and `reviewed_at` set, response under 1 s with the mocked LLM delayed 5 s; editing keeps the old text in `review_history`
- [ ] review over 2,000 chars → 422; an empty review clears it; another user's playlist → 404
- [ ] reflection with fixture review "the workout part was way too slow, rest was great" → verdict `partly`, a lesson tagged `workout` whose evidence is quoted from the review
- [ ] a lesson whose `evidence` isn't in the review text is dropped
- [ ] consolidation: "loves long intros" then later "skip long intros, they bore me" → the old lesson `superseded`, the new one `active`, `superseded_by` set
- [ ] consolidation with 41 candidate lessons → at most 40 active afterwards
- [ ] "Fix it with this review" → new playlist with `parent_id`; the review text reaches the brief call as a correction
- [ ] the next build's per-user prompt block contains the active lessons, not the superseded ones
- [ ] Disconnect deletes reviews, history and lessons (cascade)
- [ ] `live` eval (on demand, results written into this file): 5 fixture playlists with reviews. For each, build with and without the lessons from the others, and have an LLM judge (temperature 0, separate call) say which result better respects the review. With-lessons should win at least 4 of 5

Gate: `pytest tests/test_p15_reviews.py` green, the live eval passes, and one real review → "Fix it" gives a v2 that you rate higher than v1.

---

## Phase 16: Memory in every build

Goal: every build recalls just the relevant parts of everything Moodify knows about the user, cheaply. Liked and high-rated things come back; reviewed problems don't repeat.

Build
- [ ] `memory_for(user, brief) -> str` (≤1,500 chars), recall step 6 of the loop. Proactive recall as in ADK ("a pre-processor to run a similarity search based on the latest user input"), scaled down: lessons whose tags overlap the brief's `tags`, plus every `preference` lesson, ranked by overlap then recency, top 8. Tag overlap instead of embeddings, because a user has at most 40 active lessons (plan.md: no vector DB; see Later)
- [ ] Also in memory, from Phase 14: liked tracks and the artists liked most ("lean toward"); playlists rated ≥8 with their prompts ("this worked for these moods")
- [ ] No-repeat: tracks from the last 3 playlists are not used again unless liked, at most 2 per playlist. Enforced in code, not only in the prompt
- [ ] `users.memory_block` + `users.memory_version` (caching layer 2): the user-wide part (preferences, likes, ratings) is rendered once and rebuilt only when it changes, so the per-user prompt block stays byte-identical between builds and hits OpenAI's prompt cache (layer 3). The brief-specific recalled lessons go in the per-request block
- [ ] Lesson upkeep: `last_used_at` and `use_count` updated on recall. `pitfall` and `strategy` lessons unused for 180 days become `expired` (Memory Bank-style TTL); `preference` lessons never expire but can be superseded
- [ ] Optional, after PAHF's pre-action clarification: when the brief's sections conflict with an active lesson, the job page asks one question with 2–3 choices ("Last time you said workout music was too slow; keep it high-energy?"). It auto-continues with the lesson after 20 s, so a build never stalls
- [ ] "More like this" on any past playlist: builds a new one seeded with that playlist's brief, its liked tracks and its review
- [ ] "What Moodify remembers" page: the memory block as the LLM sees it, active lessons with the review each came from, delete per lesson or per playlist
- [ ] Disconnect still deletes everything (`ON DELETE CASCADE` on every new table)

Tests (`tests/test_p16_memory.py`)
- [ ] recall: with fixture lessons tagged `workout`, `focus`, `sleep`, a brief tagged `workout` gets the workout lessons and all preferences, not `sleep`; never more than 8; ≤1,500 chars with 200 playlists in the DB
- [ ] a track from the last 3 playlists that isn't liked never appears again (enforced even if the mocked LLM suggests it)
- [ ] `memory_block` unchanged between two builds with no new feedback → per-user prompt block byte-identical; a new like changes it and bumps `memory_version`
- [ ] cached tokens from mocked `usage` are recorded in `job_events`
- [ ] expiry: a `pitfall` unused for 181 days → `expired`; a `preference` unused for 400 days → still `active`
- [ ] clarification (if built): no answer in 20 s → build continues with the lesson applied
- [ ] deleting a playlist from memory removes its likes, rating, review and lessons; Disconnect leaves zero rows in every table for that user
- [ ] `live`: two builds by the same user within an hour → the second reports cached input tokens > 0 (note the share here)
- [ ] `live`: rate a playlist 2/10 with the review "too slow", make a similar prompt, and its brief has higher energy (note the result here)

Gate: `pytest tests/test_p16_memory.py` green, and both live checks move the right way.

---

## Phase 17: Open straight in the Spotify app on phones

Goal: on a phone with Spotify installed and logged in, the finished playlist opens in the app without hunting for a link.

Build
- [ ] `src/links.py`: `open_link(kind, id, user_agent)`:
  - Android: `intent://{kind}/{id}#Intent;scheme=spotify;package=com.spotify.music;S.browser_fallback_url={web url};end` (opens the app directly; web page if not installed)
  - iPhone/iPad: `https://open.spotify.com/{kind}/{id}`, which Spotify documents as launching the app when installed. It works on a tap; for automatic opening, `spotify:{kind}:{id}` raises the system "Open in Spotify?" prompt
  - desktop: the web link (it hands over to the desktop app if installed)
- [ ] Job page (Phase 9): when the build finishes on a phone, the page tries to open the app automatically, and a big "OPEN SPOTIFY" button stays as the fallback. Browsers may block an app switch without a tap (Chrome on Android needs a user gesture for `intent://`), so the button is the guaranteed path. Setting: "Open Spotify when my playlist is ready" (default on for phones)
- [ ] Every track and playlist link on phones uses `open_link`, so a tap goes into the app
- [ ] App not installed: "GET SPOTIFY FREE" link to the store, per Spotify's guidelines
- [ ] Optional: "Play on this phone": `GET /me/player/devices`, then `PUT /me/player/play` with `context_uri` on the phone (Premium only; explain otherwise)
- [ ] Needs the HTTPS deploy from plan.md Phase 8. A phone can't reach 127.0.0.1, and Spotify allows `http` redirect URIs only on loopback

Tests (`tests/test_p17_mobile.py`)
- [ ] `open_link` for iPhone Safari, Android Chrome, Samsung Internet and desktop user agents → expected form, ids URL-safe, fallback URL present on Android
- [ ] job-done page on a phone UA contains the auto-open script and the button; on desktop, only the button
- [ ] setting off → no auto-open script
- [ ] manual, on the deployed URL: iPhone with Spotify → app opens on the playlist (one system prompt at most); Android with Spotify → app opens with no prompt; a phone without Spotify → store link

Gate: `pytest tests/test_p17_mobile.py` green, and both manual phone checks pass.

---

## Later, only if needed

- [ ] Embedding search for lesson recall (ReasoningBank retrieves "using embedding-based similarity search"), if a user ever has more than ~100 active lessons and tag overlap misses relevant ones
- [ ] Streaming progress (server-sent events) instead of the 3-second refresh, if the refresh feels clunky
- [ ] Per-section LLM calls run in parallel, if builds regularly take over 2 minutes
- [ ] Per-track 1–10 ratings, if playlist ratings turn out too coarse
- [ ] Energy check from a second source, if the LLM's energy judgements turn out unreliable in Phase 13 listening tests
- [ ] Replace a playlist's songs in place for "Fix it" (check that Spotify's replace-items endpoint still works for Dev Mode first), if v1/v2 copies pile up

## Sources

Read on 2026-10-23. Quotes in this file are verbatim from these pages.

Google
- [Architecting efficient context-aware multi-agent framework for production](https://developers.googleblog.com/en/architecting-efficient-context-aware-multi-agent-framework-for-production/), Google Developers Blog (ADK): storage vs working context, stable prefixes, async compaction, reactive vs proactive recall, scope by default
- [ReasoningBank: Scaling Agent Self-Evolving with Reasoning Memory](https://arxiv.org/abs/2509.25140), Google (ICLR 2026): memory items from successes and failures, title/description/content, ≤3 per run, LLM-as-judge, append-only consolidation
- [Vertex AI Agent Engine Memory Bank overview](https://docs.cloud.google.com/agent-builder/agent-engine/memory-bank/overview), Google Cloud docs: extraction, consolidation that lets memories evolve, per-identity isolation, TTL, revisions. The page itself served a generic landing page when fetched, so these points rely on the docs text quoted in search results; re-check before relying on details

Meta
- [Learning Personalized Agents from Human Feedback (PAHF)](https://ai.meta.com/research/publications/learning-personalized-agents-from-human-feedback/) ([arXiv 2602.16173](https://arxiv.org/abs/2602.16173)), Meta Superintelligence Labs, 2026: pre-action clarification, memory-grounded action, post-action feedback updating per-user memory when preferences drift
- [Agent Learning via Early Experience](https://arxiv.org/abs/2510.08558), Meta, 2025: self-reflection grounded in observed outcomes

Model provider (the cache mechanics Moodify actually runs on)
- [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching), OpenAI: 1,024-token minimum, stable prefix first, compaction resets the cache, `prompt_cache_key` and retention options by model generation, cached-token usage fields
