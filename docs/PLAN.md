# Moodify v2: build plan

Status: proposal, 2026-10-22. Facts about Spotify, LangChain and OpenAI were checked against primary sources on that date (see [Sources](#9-sources)).

## 1. What we're building

A mobile-friendly web app. A user signs in with Spotify, answers a short questionnaire, and gets a **taste profile** they can see and edit. They type how they feel ("rainy Sunday, a bit nostalgic, nothing sleepy"). An agent picks tracks that fit both the mood and the profile and shows a preview. After the user confirms, it saves a playlist to their Spotify account, optionally queues it, and keeps a copy in Moodify that can be exported.

Stack (as requested): Supabase (Postgres, Auth, pgvector), LangChain + LangGraph agents with Deep Agents for multi-step requests, and OpenAI's mid-tier model.

## 2. Constraints that shape everything

### 2.1 Spotify (verified from developer.spotify.com)

| Rule | Since | What it forces |
|---|---|---|
| Development Mode only: **max 5 allowlisted users per app**, one Dev-Mode Client ID per developer, the owner needs Premium. Extended Quota is for **organizations with ≥250k MAU** only | Feb 11, 2026 / May 15, 2025 | The Spotify-connected product is a **5-person private beta**. Splitting one product over several Client IDs is forbidden (Terms VI.1.b) |
| Recommendations, Audio Features (valence/energy), Audio Analysis, Related Artists, browse, and 30-second previews are gone for new apps | Nov 27, 2024 | Spotify gives us **no mood signal**. It must come from elsewhere (§3.4) |
| Multi-get (`GET /tracks?ids=`), artist top-tracks and `popularity` are removed; Search returns ≤10 results per request; artist `genres` is effectively absent | Feb 2026 | Turning a candidate song into a Spotify URI costs one call. Resolve as few tracks as possible, and cache the results |
| Create a playlist with `POST /me/playlists`, add tracks with `POST /playlists/{id}/items` (≤100 per call); responses use `items` / `item`, not `tracks` / `track` | Feb 2026 | Old tutorials and SDK versions are wrong; write a small client of our own |
| Reading a playlist's tracks works only for playlists the user owns or collaborates on | Feb 2026 | "Import my followed playlists" is impossible |
| Queue and play require the **listener** to have Premium plus an active device. `/me` no longer returns `product`, so Premium can't be checked in advance. Using playback makes the app a "Streaming SDA", which **can never be monetized** | n/a | Queuing is an optional "Play now" action with fallbacks, and a decision for you (§8) |
| Refresh tokens **expire 6 months** after consent (`invalid_grant`); implicit grant and `localhost` redirects are banned | Jun–Jul 2026 / Nov 2025 | Need a re-consent flow; use Auth Code + PKCE (Supabase does this) with HTTPS or `127.0.0.1` redirects |
| Two kinds of 429: a normal rolling-30-second limit (`Retry-After`) and a Dev-Mode quota (`QUOTA_EXCEEDED`, unpublished size, multi-hour cooldowns reported) | Jul 2026 | Back off on the first; for the second, trip a circuit breaker and tell the user. Budget Spotify calls per user |
| Self-serve disconnect, and deletion of the user's Spotify data within 5 days; no storing Spotify Content indefinitely | May 2025 terms | `DELETE /v1/me` from day one; a retention job for cached metadata |
| Text prompts only (no voice SDAs); Spotify logo and links on every track and playlist; artwork must not be altered; app name must not start with "Spot" | May 2025 policy | UI rules for Phase 6; "Moodify" is fine |
| Policy III also bans games and trivia, mixing or segueing tracks, syncing to video, and products targeted at children. III.9 **allows** sending "the metadata of the user's playlists to another service" | May 2025 policy | No "guess the song" or crossfade features; an age confirmation at signup; CSV/JSON/M3U export is explicitly allowed |
| `/me` no longer returns `email` in Dev Mode. Supabase's Spotify provider fails sign-up when no email comes back, unless `external_spotify_email_optional` is enabled | Feb 2026 | First thing to test in Phase 1 |

### 2.2 The policy problem with the original idea

Building the profile by sending a user's Spotify library to an LLM conflicts with Spotify's terms on a plain reading:

> "using the Spotify Platform or any Spotify Content to train a machine learning or AI model **or otherwise ingesting Spotify Content into a machine learning or AI model**" (Developer Terms v10, IV.2.1)
>
> "'Spotify Content' means any content, data, information or material made available through the Spotify Platform … metadata, playlists, and user data including Spotify Personal Data." (Terms II.8)
>
> "Do not analyze the Spotify Content … for any purpose, including … **building profiles of users**" (Developer Policy III.13)

The terms make no distinction between training and inference, and Spotify has published no clarification. Separately, Spotify now ships prompt-to-playlist itself (Prompted Playlist in the US, CA, UK, IE, AU and SE since early 2026; Taste Profile beta; Studio), so Policy III.11 ("do not … replicate … a core user experience") is a second risk.

**Recommended design principle: the AI never touches Spotify Content.**
- The LLM, the embeddings and the profile are built only from data Spotify didn't supply: questionnaire answers, artists and songs the user types (autocompleted from MusicBrainz, not Spotify), Moodify's own likes and skips, and other music sources (§3.4).
- Spotify is a **deterministic edge adapter**: login, resolving a chosen song to a URI by ISRC or exact match, writing playlists, queuing. No Spotify response is ever put in a prompt, embedded, or used to profile anyone.
- "Library-aware" features (e.g. "mostly songs I already saved") stay **behind a feature flag that defaults off**. They would only be switched on after written confirmation from Spotify. The code path is designed now, so flipping it later isn't a rewrite.

This also makes Spotify replaceable: the music provider is an interface, and a second provider is the only route to more than 5 users (Phase 8).

**This design lowers the risk; it doesn't remove it.** Policy III.5 says "Do not create any product or service which is integrated with streams or content from another service". Read literally, that could cover enriching tracks with MusicBrainz or Last.fm metadata, so any mood app with outside data has the same exposure. The likelier target is mixing other services' *streams* into the Spotify experience, which this design never does, but that is a reading, not a ruling.

**Before onboarding even 5 testers, email Spotify developer support** and ask about III.5, III.13 and III.14 for exactly this design. Keep the answer in `docs/decisions/`.

### 2.3 OpenAI

The "mid-tier" model in the current lineup is **Sol**: `gpt-6.1-sol` / `gpt-6-sol`, about $2 in / $10 out per 1M tokens. Luna (about $0.10 / $0.50) is below it and Astra above. Pricing is from third-party trackers; confirm on OpenAI's pricing page. All three were tested live with your key on 2026-10-22 through the existing structured-output parser. The model name is a setting (`OPENAI_MODEL`), never hardcoded.

### 2.4 Getting the user's data: how Apple Music does it, and what Moodify can copy

**How Apple Music's import works** (support.apple.com/118249, March 2026; available in India):
- **Who builds it:** SongShift. Apple's article says "Transferring music to Apple Music is provided by a third-party", and the flow is served from `apple.songshift.com` with the label "Powered by SongShift".
- **How it reads Spotify:** the user signs into Spotify with normal OAuth. SongShift's *own* Spotify developer app reads their playlists, liked songs and saved albums through the Web API. It does not use the data export. SongShift has had API access since at least 2019, so it must be grandfathered into Extended Quota.
- **How songs are matched:** each item is matched to Apple's catalog, followed by a "Needs review" screen. **Only the library comes across, not listening history.** After that, Apple personalises from plays *inside Apple Music*, its own first-party data.
- **Why Spotify allows it:** Policy III.9 makes an exception for "enabling a user to transfer their personal data, or the metadata of the user's playlists to another service". That exception covers moving data, not modelling it. The profiling and AI bans in III.13 and III.14 still apply.
- **Why Moodify can't copy it literally:** that path needs Extended Quota (an organization with ≥250k MAU), and Moodify wants to *model* the data, which the transfer exception doesn't cover.

**What Moodify can copy is the pattern:** a user-initiated, one-time import as a seed, then first-party signals from then on. These are the data sources a user can bring:

| Source | Depth | How it arrives | Rules |
|---|---|---|---|
| Spotify "Download your data": **Extended streaming history** | **Lifetime.** Every play with end time, ms played, `reason_start`/`reason_end`, `skipped`, `shuffle`, platform | The user requests it on spotify.com (login plus an email confirmation) and gets a ZIP of JSON (up to ~30 days; unverified, often faster), then uploads it to Moodify | GDPR Art. 20 portability (the EDPB-endorsed guidance uses music streaming as its example). Spotify's own privacy policy offers it "for use in another party's service". **Spotify's Developer Terms bind only apps that use the Spotify Platform** (APIs, SDKs, widgets, a client ID), so Moodify must use no Spotify API at all. Residual risk: Spotify's *user* guidelines have an AI clause. Whether it covers a user's own export is ambiguous, untested, and sits with the user |
| Spotify "Download your data": **Account data** | Past year: end time, artist, track, ms played; library and own playlists | Same, faster (~5 days, unverified) | Same |
| Apple Music API (MusicKit) | About 50 recent plays without timestamps, heavy rotation, the latest year's Replay top lists, library (no play counts), favorites | Live connection. $99/yr developer fee; **every user needs an Apple Music subscription** | The developer agreement has **no AI-ingestion ban**. User metadata may be used only for a "clearly disclosed … directly relevant" function. Apple's Media Services Terms ban training or fine-tuning and automated "analysis" (a grey area). The Apple Music connection must not be paywalled |
| Last.fm | Full scrobble history with timestamps | The user enters their username | Non-commercial; attribution |
| Moodify feedback | Ongoing | First-party | Ours |

**This forces a choice between two product shapes.** It's either/or: if Moodify touches *any* Spotify API, it becomes a Spotify developer app. Then every piece of Spotify-sourced data falls under the AI ban, uploaded exports included.

| | **Shape A: bring your own data** (fits the goal you described) | **Shape B: Spotify-connected** (the §2.2 design) |
|---|---|---|
| Profile built from | Lifetime Spotify export + Last.fm + Apple Music + feedback | Questionnaire, typed seeds, Last.fm, feedback |
| AI over the user's history | **Yes**, with consent | No |
| Login | Email, Google or Apple via Supabase. **Never "Sign in with Spotify"** | Spotify via Supabase |
| User cap | None from Spotify | **5** |
| Playlist output | Apple Music library (MusicKit), plus a CSV/M3U export. Spotify users import the file with a transfer tool (Spotify's in-app "Import your music" is powered by TuneMyMusic). **Spike this flow first** | Written straight into Spotify |
| Queue / "play now" | In-page player for Apple Music subscribers (MusicKit JS; foreground only on iOS) | Spotify queue (Premium) |
| Main costs | Spotify users get their playlist via an extra import step; the export takes days to arrive | 5 users; no history modelling |

**Shape A specifics**
- **Parse the ZIP in the browser** (Web Worker), so raw files never reach our server.
  - Accept only `StreamingHistory_music_*`, `Streaming_History_Audio_*`, `YourLibrary.json` and `Playlist*.json`.
  - Before upload, strip `ip_addr*`, `conn_country`, `username`, `user_agent*` and `offline_timestamp`. Reduce `platform` to mobile, desktop or TV. Drop rows with `incognito_mode=true`, and drop playlist `collaborators` and `description`.
  - Refuse every other file: Userdata, Payments, Messages, Search, Voice, Location, Inferences, Wrapped and Technical log.
- **Storage:** normalised events go to per-user encrypted Parquet in Supabase Storage. A lifetime history can exceed 100k plays, too many for Postgres rows on the free tier. Derived features and the profile live in Postgres.
  - Keep `spotify_track_uri` only to build plain `open.spotify.com` links (no API). It is never sent to the LLM.
- **The LLM sees aggregates** (top artists with counts, context summaries), never raw rows. Set the OpenAI organization to no training and minimal retention.
- **Identity resolution is the bottleneck** (MusicBrainz allows 1 request per second).
  - Resolve tracks in order of engagement; the top ~2,000 cover most listening time.
  - Batch lookups through ListenBrainz's metadata lookup, and cache results globally so popular tracks are resolved once.
- **Onboarding timeline:**
  1. Day 0: questionnaire plus Last.fm or Apple Music.
  2. Account-data upload (the past year).
  3. Extended-history upload when it arrives.

  Each step produces a new profile version.
- **Compliance** (India DPDP Act 2023 plus Rules 2025; GDPR for EU users):
  - **Consent notice:** itemised, separate opt-in, in English and Indian languages.
  - **18+ only.** DPDP s.9(3) bars "behavioural monitoring of children", and a taste profile is exactly that.
  - **Deletion:** one click, cascading to processors (OpenAI, LangSmith). Delete raw uploads after processing.
  - **Grievances:** publish a contact; answer within 90 days.
  - **Timing:** DPDP's substantive duties commence around May 2027 (re-check the Gazette), but build them now. CERT-In's 6-hour incident reporting applies today.
  - **EU users** need a GDPR Art. 27 representative and a DPIA.
  - **Labels:** never health or mental-state labels ("depressed", "anxious"); keep to musical mood and energy, because those labels can turn listening data into GDPR Art. 9 special-category data.

## 3. Architecture

```
 phone / desktop browser
 ┌──────────────────────────────────────┐
 │ Next.js PWA (Vercel)                 │  Supabase Auth (Spotify provider, PKCE)
 │ onboarding · mood box · live preview │──────────────┐
 │ history · export · profile editor    │              │
 └───────────────┬──────────────────────┘              │
                 │ HTTPS + SSE, Supabase JWT           │
 ┌───────────────▼──────────────────────────────────┐  │
 │ FastAPI + LangGraph (Railway, Docker, py3.12)    │  │
 │  agents/  mood→playlist graph, profile graph,    │  │
 │           deep agent for multi-step requests     │  │
 │  knowledge/  catalog, enrichment, embeddings ────┼──┼──▶ MusicBrainz · Last.fm · … (§3.4)
 │  providers/spotify/  resolve · playlist · queue ─┼──┼──▶ Spotify Web API (Dev Mode)
 │  llm/  OpenAI Sol (reasoning), embeddings ───────┼──┼──▶ OpenAI
 └───────────────┬──────────────────────────────────┘  │
                 │ psycopg pool                        │
 ┌───────────────▼─────────────────────────────────────▼┐
 │ Supabase Postgres: app tables + RLS, pgvector (HNSW), │
 │ LangGraph checkpointer + store (private schema), Vault│
 └───────────────────────────────────────────────────────┘
```

### 3.1 Why these choices
- **The agent runs on our own FastAPI server, not LangGraph's hosted Agent Server.** Self-hosting Agent Server needs an Enterprise license and its cloud needs a paid plan. A FastAPI server streaming over SSE is all open-source. A run takes 10–90 s and streams, which is a poor fit for Vercel serverless functions, so Python runs on Railway; you already have the CLI.
- **Python 3.12:** `deepagents` needs ≥3.11, and `langchain-openai` needs `openai>=2.45` and `pydantic>=2.7.4`. Current pins (as of 2026-10-22): `langchain 1.4.x`, `langgraph 1.2.x`, `langchain-openai 1.7.x`, `deepagents 0.7.x` (pre-1.0: pin exactly), `langgraph-checkpoint-postgres 3.2.x`.
- **Use v1 APIs only:** `langchain.agents.create_agent` (not `create_react_agent`), middleware (not hooks), and `ToolRuntime` for context (not `InjectedState`).
- **Supabase pgvector replaces Qdrant.** The old local collection has 0 points, so nothing needs migrating.
  - Supabase ships pgvector 0.8.x.
  - Store vectors as `halfvec` with an HNSW cosine index.
  - Start at 512 dimensions (`text-embedding-3-small` with `dimensions=512`). That is about 1 KB per vector, which matters on the free tier's 500 MB. Phase 3 measures whether full 1536 dimensions retrieve noticeably better.
- **Remove torch and CLAP.** CLAP embeds audio, and Spotify no longer gives us any audio. Dropping it also drops 2.7 GB of CUDA wheels from the image.

### 3.2 Agent design (LangGraph)

**Mood → playlist graph**, one thread per request, checkpointed in Postgres:

```
START → guard → parse_intent → route ──simple──▶ retrieve → curate ─┐
                                    └──deep────▶ deep_curator ──────┤
                                                                    ▼
                         validate ──violations (≤2 retries)──▶ curate
                            │ ok
                            ▼
                     confirm (interrupt) ──reject──▶ END
                            │ approve / edit
                            ▼
                    write_to_spotify → persist → END
```

- **guard:** length cap, empty or abuse check, per-user budget check.
- **parse_intent:** the salvaged `QueryEngine` (structured output, injection-hardened instructions, injectable client). Its fixed FMA genre list becomes an open vocabulary.
- **route:** a structured classification. *Deep* means several constraints, a time-based arc, or research ("3-hour wedding set: dinner → first dance → party", "songs from films set in Tokyo, calm to hype").
- **retrieve:**
  - a pgvector search over the knowledge catalog, filtered by the profile (dislikes, explicit content, eras, languages),
  - plus Sol proposing candidates from its own knowledge,
  - merged and deduplicated by ISRC / MusicBrainz ID.
- **curate (Sol):** choose and order N tracks: energy arc, a cap per artist, and the familiar/new ratio from the questionnaire.
- **deep_curator:** `create_deep_agent` with two subagents (`catalog-researcher`, `sequencer`) and the same tools. It is capped with `ModelCallLimitMiddleware` / `ToolCallLimitMiddleware` (subagents don't inherit the parent's limits; set them on each).
- **validate:** plain code, no LLM:
  - track count and duration,
  - no duplicates, artist cap, exclusions,
  - **every track resolved to a Spotify URI** (Phase 2 resolver).

  Violations go back to `curate`.
- **confirm:** `interrupt()` with the preview. The web app lets the user approve, remove or swap tracks, or rename, then resumes with `Command(resume=…)`. Every side effect happens *after* this point, in its own node.
- **write_to_spotify:** idempotent. The playlist id is saved before tracks are added, so a resumed run never creates a second playlist. Queue is optional and falls back to an "Open in Spotify" link on 403 / no device.

**Profile graph:** collect first-party inputs → aggregate deterministically → Sol produces a `TasteProfile` (a pydantic model, a readable summary, and an embedding) → the user reviews and edits it → a new version is stored. Likes, skips and edits go into the LangGraph `PostgresStore` under `("taste", user_id)`, with semantic search, and feed the next version.

**Secrets never reach the model or the checkpoints.** `user_id` and a Spotify token provider travel in runtime context (`context_schema`, `ToolRuntime`), never in state or prompts, so they are never written into checkpoints. LangSmith tracing uses the secret anonymizer.

### 3.3 Data model (Supabase, all tables RLS-enabled)

| Table | Key columns | Access |
|---|---|---|
| `profiles` | `id` = `auth.users.id`, spotify `account_id`, display name | own row |
| `private.provider_credentials` | user_id (FK to `auth.users`, on-delete cascade), provider, refresh_token (Vault or app-level AES-GCM), access_token, expires_at, consented_at, scopes | in a schema the Data API doesn't expose; **service role only** |
| `questionnaire_responses` | user_id, version, answers jsonb | own rows |
| `taste_profiles` | user_id, version, profile jsonb, summary, embedding, inputs_digest | own rows |
| `recordings` | id (MusicBrainz recording id), isrc, title, artist_credit, year, tags jsonb, vibe_card, embedding, embedding_model, embedding_version | read: authenticated |
| `spotify_resolutions` | isrc / recording_id → spotify_uri, resolved_at (TTL, purged) | service role |
| `mood_requests` | id = LangGraph thread_id, user_id, prompt, intent, route, status, cost_usd, latency_ms | own rows |
| `playlists` / `playlist_items` | user_id, request_id, spotify_playlist_id, name; position, recording_id, spotify_uri, reason | own rows |
| `feedback` | user_id, recording_id, playlist_id, signal (like / skip / remove) | own rows |
| `langgraph.*` | checkpointer and store tables, created by their `setup()` | not exposed by the API |

### 3.4 Where the mood signal comes from

Spotify gives no mood data, so every recording gets a mood estimate from non-Spotify sources. Identity comes from the ISRC, which Spotify still returns in `external_ids`, mapped to a MusicBrainz recording ID.

| Layer | Source | Gives us | Terms / limits | Use |
|---|---|---|---|---|
| Identity | MusicBrainz `/ws/2/isrc/{isrc}` | recording ID, artist credit, year, genre tags | core data CC0; **tags CC BY-NC-SA**; 1 req/s; a real User-Agent with a project contact address | **yes** |
| Tags | Last.fm `track.getTopTags`, falling back to `artist.getTopTags` | crowd mood and genre tags | **non-commercial only**; "powered by AudioScrobbler" attribution; ≤100 MB stored; no fixed rate published (stay ≤1 req/s) | **yes** |
| Candidates | ListenBrainz Labs similar-recordings / similar-artists | the replacement for Spotify's Recommendations / Related Artists | free; data licence unverified | **yes** |
| Tags → numbers | Word-norm lexicon over the tags, calibrated on MuSe | valence / arousal estimates | MuSe is CC BY 4.0 | **yes** |
| Mood scores | AcousticBrainz high-level (final 2022 dumps) | mood_happy / sad / relaxed / party / aggressive | CC0, pre-2022 recordings only, weak quality | **yes, low weight** |
| Fallback | LLM vibe card from the MusicBrainz and Last.fm fields plus the model's own knowledge | moods, energy, instrumentation | OpenAI | **yes**, for the long tail |
| Audio features | ReccoBeats (`/v1/audio-features`, 40 IDs per call, free) | Spotify-style valence / energy / tempo | its own terms say the data is "aggregated from Spotify data" | **no**: Spotify-derived, which breaks the clean-room rule |
| Audio | Deezer 30 s previews + CLAP / Essentia (the old CLAP idea) | real audio embeddings | Deezer: non-commercial, family-private use, previews blank in India; Spotify III.5; Essentia models NC | **no** |
| Paid | Cyanite, Soundcharts | professional mood tags | enterprise pricing | only if it goes commercial |

**Free labelled data for testing:** MuSe has about 90k songs with valence/arousal/dominance, CC BY 4.0. The Deezer mood dataset (ISMIR 2018) has 18k tracks with valence/arousal. Both become the Phase 3 benchmark, so nobody has to hand-label songs.

**A recording's "vibe card"** is short text built only from these fields. It is embedded and also scored to numeric valence/energy. Retrieval combines vector similarity with numeric filters: "low energy" becomes `energy < 0.4`.

**Non-commercial licences.** Last.fm, MusicBrainz tags and Deezer are all non-commercial. That fits a 5-user Dev-Mode beta, which Spotify also restricts to non-commercial use. A commercial launch would need a Last.fm agreement and paid tagging.

### 3.5 Listener model: from listening history to a queryable profile

This is source-agnostic. It works the same on a Spotify data export, Last.fm scrobbles, Apple Music history, or Moodify's own feedback.

**Input:** listening events `(ts, recording, ms_played, skipped, reason_end, shuffle, platform)`. Fields a source lacks are left null.

1. **Resolve identity.** Map (artist, title, ISRC or URI) to a MusicBrainz recording ID. Each recording gets its mood vector from §3.4: valence, energy, tags and embedding.
2. **Engagement per (user, recording):**
   - plays and total listening time;
   - completion = `ms_played / duration`;
   - skip rate;
   - an affinity score with recency decay (90-day half-life);
   - first-heard date (how much the user explores);
   - the hours and weekdays it gets played.
3. **Sessions.** Split events wherever there's a gap of more than 30 minutes. Each session gets a time context (hour, weekday, device) and a mood centroid: the affinity-weighted mean embedding, valence and energy of the tracks the user *completed* in it.
4. **Mood contexts, the heart of the profile.** Cluster sessions into 4–8 contexts per user (HDBSCAN on centroid plus time features), e.g. "late-night mellow, weekdays after 23:00, 18% of listening".
   - Each context stores its centroid, top recordings and artists, typical hours and share of listening.
   - The LLM only *names and describes* the contexts, from the aggregates.
5. **Profile document.** A versioned `TasteProfile`:
   - deterministic stats: genre, era and language mix, how mainstream (ListenBrainz popularity), how exploratory, skip tolerance;
   - the contexts;
   - an overall taste vector;
   - an LLM-written summary.

   The user can see and edit it, and edits outrank inferences.

**Querying it for a mood:**

```
prompt → parse_intent → mood target (valence/energy range + vibe embedding)
       → match target to the user's contexts (cosine on centroids) → 1–2 contexts
       → familiar pool: the user's own high-affinity recordings near the target, minus recent over-plays
         discovery pool: catalog recordings near target ∧ context centroid
                         + ListenBrainz similar-recordings of the context's top tracks
                         + LLM suggestions conditioned on the context description
       → score = a·sim(target) + b·sim(context) + c·affinity + d·novelty_fit − e·skip_rate − f·fatigue
       → mix pools by the user's familiar/new slider → curate & sequence (LLM) → validate → confirm
```

Likes, skips and removals in Moodify update affinity, so the model keeps learning from first-party data.

**How we know it works: a time-split backtest on each user's own history.**
- Build the profile from everything except the last 4 weeks.
- Turn each held-out session's mood centroid into a pseudo-prompt.
- Measure whether the tracks the user actually *completed* in that session rank highly.
- The gate is Recall@50 and nDCG@20, which must beat two baselines: "user's top tracks" and "global popularity". It's the standard recommender evaluation, and needs no hand labels.

## 4. What to keep from the old repo

| Old piece | Verdict | Becomes |
|---|---|---|
| `Settings` (pydantic-settings, prod validator, cached `get_settings`) | **Keep the idea, rewrite.** Raise instead of `sys.exit`, no `mkdir` side effects, anchored `ENV` pattern | `core/config.py` |
| `QueryEngine` / `QueryIntent` (structured output, injection-hardened prompt, input cap, injectable client) | **Keep**, the strongest piece | `parse_intent` node |
| Exception hierarchy | **Keep** | `core/errors.py` |
| Dependency injection in `IngestionService` (pass the client in; fake it in tests) | **Keep the pattern** | every adapter |
| Batched upserts / generator streaming | **Keep the pattern** | enrichment jobs |
| Versioned collection + alias swap notes (`_trash/session_backup.py`) | **Keep the idea** | `embedding_version` + reindex job |
| `Result` / `Ok` / `Err` at service boundaries | **Keep for adapters** (Spotify, enrichment), where failure is expected and non-fatal | `providers/` |
| Multi-stage Dockerfile, non-root user | **Keep the pattern, fix the bugs** (CPU only, `COPY src/`, real CMD) | `apps/api/Dockerfile` |
| Phased checkup suite (markers `live` / `docker` / `slow`, stress harness) | **Keep the approach** | per-phase exit gates below |
| `MoodifyLogger` (rotating file handler) | **Replace** with stdout JSON logging; containers don't keep files | `core/logging.py` |
| CLAP / laion-clap / torch | **Drop.** No audio source, never loaded, 2.7 GB of CUDA | n/a |
| FMA dataset (7.5 GB of zips) | **Drop.** FMA tracks are mostly not on Spotify | n/a |
| Qdrant, Redis, FastAPI stub | **Drop** (pgvector replaces Qdrant; Redis was never used) | n/a |

## 5. Phases and exit gates

Each phase ends with tests that must pass before the next one starts, same as the checkup suite. `live` tests hit real services with a tester account; everything else runs offline in CI.

### Phase 0: Reset (½–1 day)
- **Rotate** the OpenAI and Qdrant keys pasted into chat, and the old Qdrant key hardcoded in `_trash/session_backup.py`.
- The repo has **no commits**:
  1. Commit the old tree (without secrets or zips) to a `legacy` branch.
  2. Start `main` with the new layout: `apps/api` (uv, py3.12, ruff, pytest), `apps/web` (Next.js, pnpm), `supabase/` (CLI, local ports moved off 55321–55327, which your other project uses), GitHub Actions CI, pre-commit with gitleaks.
- Delete `_archive/` and `_trash/` once you've reviewed them (§8).

**Exit gate**
- `gitleaks detect` is clean over the whole history.
- The ported `test_p0_hygiene` passes (no secrets, ignore files correct, Docker context <100 MB).
- CI is green.

### Phase 1: Accounts and auth
- Spotify app in Dev Mode, with redirect URIs for the Supabase callback and `127.0.0.1`, and up to 5 testers added by email.
- **Do this first:** sign in a tester through Supabase's Spotify provider. Dev Mode returns no email, so if sign-up fails, enable `external_spotify_email_optional`.
- Scopes: Supabase always adds `user-read-email`. Add `playlist-modify-private`, `playlist-modify-public`, `user-modify-playback-state` and `user-read-playback-state` through `options.scopes`.
- **Capture the provider tokens in a server callback route.** Supabase never stores or refreshes them, and they only exist on the session right after `exchangeCodeForSession`. POST them once to the API, which encrypts and stores them in `private.provider_credentials`.
- The API's `SpotifyTokenProvider` refreshes tokens with the client secret:
  - lock the row while refreshing so two requests can't refresh at once,
  - save the rotated refresh token,
  - turn `invalid_grant` (the 6-month expiry) into a "reconnect Spotify" prompt.
- An age confirmation at signup (Policy III.8).
- FastAPI skeleton: `/health`, Supabase JWT verification (JWKS), `GET /v1/me`, `DELETE /v1/me` (removes everything immediately and revokes stored tokens).

**Exit gate**
- Config tests: strict validation, raises instead of exiting, prod requires secrets.
- `supabase db reset` applies every migration.
- RLS tests: user A can't read B's rows; `anon` and `authenticated` can't see `private.*` at all.
- Token tests with respx: refresh near expiry, one retry on 401, `invalid_grant` leads to the re-consent state, the rotated refresh token is saved, and 20 concurrent requests cause only 1 refresh.
- Deletion test: zero rows left for the user.
- **live:** a tester logs in and `/v1/me` returns their `account_id`.

### Phase 2: Spotify adapter (deterministic, no AI)
- An async httpx client covering only the Dev-Mode endpoints we use: search (≤10), single track, `POST /me/playlists`, `POST /playlists/{id}/items`, devices, queue, play.
- **Resolver:** (ISRC, or artist + title) → Spotify URI. Try an `isrc:` search first, then a normalized exact match with a threshold. No LLM anywhere in this path. Results are cached with a TTL.
- **Rate limits:** `Retry-After` backoff with jitter; a circuit breaker on `QUOTA_EXCEEDED`; a per-user call budget.
- **Idempotent playlist writer; queue with fallbacks.**

**Exit gate**
- Contract tests from recorded fixtures for every endpoint, using the new `items` / `item` shapes.
- **Stress:** 500 resolves against a fake Spotify that injects both kinds of 429. `Retry-After` is honored, there are no retry storms, and the breaker trips on the first `QUOTA_EXCEEDED`.
- Resolver accuracy on 200 hand-checked songs: ≥95% correct and **0 wrong-artist matches**.
- Crash in the middle of a write, then retry: no duplicate playlist.
- **live:** create a private playlist, add 3 tracks, queue 1 (Premium tester), then delete the playlist.

### Phase 3: Music knowledge layer (the mood signal)
- A `recordings` catalog keyed by MusicBrainz ID and ISRC, enriched from the sources in §3.4.
- Enrichment runs as a queue with a rate limiter per source (MusicBrainz 1 req/s, Last.fm ≤1 req/s), retries, and a cache. Each recording is enriched **once globally**, not per user.
- An LLM vibe card per recording, embedded with `text-embedding-3-small` into a pgvector `halfvec` HNSW index. `embedding_version` makes reindexing a swap, not downtime.
- A valence/energy scorer: the tag lexicon combined with the LLM estimate and AcousticBrainz.
- Seed the catalog from the questionnaire's seed artists plus ListenBrainz similar-artists, and grow it as requests come in.

**Exit gate**
- **Mood accuracy:** on a held-out sample of MuSe and the Deezer mood dataset, Spearman correlation between predicted and labelled valence and arousal beats the tags-only baseline. The thresholds get fixed after the first run and then must never regress.
- **Retrieval:** 30 mood queries; nDCG@20 against MuSe quadrant labels beats random and tags-only, for both 512 and 1536 dimensions (pick the cheaper one if it's within 2%).
- **Enrichment:**
  - idempotent;
  - rate limits never exceeded under a 5k-recording backfill;
  - cost per 1k recordings logged;
  - Last.fm storage stays under 100 MB.
- **Stress:** pgvector search p95 < 50 ms at 100k recordings, reusing the old P9 harness.

### Phase 4: Onboarding and taste profile
- A questionnaire of about 8 questions:
  - eras and languages,
  - familiar ↔ new slider, energy range, explicit OK,
  - dislikes and activities,
  - 3–10 seed artists or songs, typed with MusicBrainz autocomplete.
- The profile graph from §3.2. A "How Moodify hears you" page where the user can edit the profile.
- Optional import: a Last.fm username. Many listeners scrobble Spotify to Last.fm, so their history and top artists arrive through Last.fm's API, outside Spotify's terms (decision 8).

**Exit gate**
- Unit tests with `GenericFakeChatModel`; schema validation.
- **Grounding check:** across 20 synthetic personas, 0 artists or genres in a profile that don't appear in its inputs.
- LangSmith eval: LLM-judge faithfulness averages ≥4/5.
- Editing round-trips: an edit is reflected in the next version.

### Phase 5: Mood → playlist agent
Build the graph from §3.2 with `AsyncPostgresSaver` and `AsyncPostgresStore`.

**Connection:** use Supabase's **session-mode pooler** (`*.pooler.supabase.com:5432`). It is IPv4 and supports prepared statements; the direct connection is IPv6-only. Avoid the transaction pooler on 6543. If you ever must use it, build the pool yourself with `prepare_threshold=None`, otherwise you hit `DuplicatePreparedStatement` (langgraph #2030). Run `setup()` once from a migration step, and move the tables out of `public`.

**Exit gate**
- Node unit tests with fake models.
- Interrupt tests: approve, edit and reject all resume correctly.
- Kill the run during `write_to_spotify`, then resume: no duplicate playlist.
- Validator fuzzing: 100 malformed curations, 100% caught.
- Adversarial prompts (ported from P7: injection, emoji-only, non-English, contradictory, 5k characters): always a valid intent and no instruction leakage.
- LangSmith dataset of 40 prompts × 5 personas: mood-fit judge ≥4/5, deterministic constraints 100%.
- **Budgets:** simple route p95 < 20 s and < $0.05 per playlist; deep route p95 < 90 s and < $0.30.

### Phase 6: API and mobile web app
- **API:**
  - `POST /v1/moods` (SSE events: progress, tokens, interrupt) and `POST /v1/runs/{id}/resume`,
  - `GET /v1/playlists`, `GET /v1/playlists/{id}/export?format=csv|json|m3u`,
  - `GET`/`PATCH /v1/profile`, `POST /v1/onboarding`.
- **Web:** Next.js App Router, TypeScript, Tailwind, shadcn/ui, `@supabase/ssr`, and a PWA manifest. Screens: login → onboarding → mood box with quick chips → live progress → preview and confirm → saved; plus history, profile and settings (disconnect / delete). Spotify branding rules apply (logo, "Open Spotify" links, unaltered artwork).

**Exit gate**
- Every route rejects a missing or foreign JWT. User A can't resume or read user B's run.
- SSE event order is correct, and the stream recovers after a reconnect.
- Playwright end-to-end tests on iPhone 13 and Pixel 7 viewports.
- axe: 0 serious violations. Lighthouse mobile: performance ≥85, accessibility ≥95.
- Exported files parse correctly.

### Phase 7: Deploy and operate
- **API image:** `python:3.12-slim`, uv, no torch, non-root, `uvicorn` CMD, deployed to Railway.
- **Web:** Vercel. **Database:** Supabase cloud.
- **Operations:** LangSmith tracing with redaction, Sentry, a per-user daily budget for OpenAI spend and Spotify calls.

**Exit gate**
- Image < 500 MB and built in CI.
- Healthcheck passes; a staging smoke test with a tester account covers the full flow.
- **Load:** 50 concurrent users against fake LLM and fake Spotify. API overhead p95 < 300 ms, 0 errors.
- **Real-LLM burst:** 10 concurrent playlists stay within budget.
- **Backups:** the free tier has none, so run a nightly `pg_dump` to object storage. Drill: restore it into staging and pass the smoke test.
- **Pausing:** free projects pause after 1 week idle. Either move to Pro or accept it during the beta.

### Phase 8: Compliance and reach
- Privacy policy; a retention job that purges `spotify_resolutions` past their TTL; a design-guidelines review.
- If you want library-aware features: ask Spotify in writing first.
- For more than 5 users: evaluate a second provider behind the same interface (YouTube Music, Apple Music, Tidal; not yet researched).

**Exit gate**
- Retention job test passes; the compliance checklist is done; 5 testers are onboarded.

### If we go with Shape A (§2.4)

| Phase | Change | Exit gate |
|---|---|---|
| 0 Reset | **Request your own Spotify data now** (both packages); it becomes the main test fixture and takes days | Export received; a synthetic copy with fake values committed as the fixture |
| 1 Accounts | Supabase email, Google or Apple login; no Spotify developer app at all; consent records; 18+ gate | Same RLS and deletion tests; consent stored with notice version; no Spotify client ID anywhere in config or code (CI grep) |
| 2 Data intake (replaces the Spotify adapter) | Browser ZIP parser, Last.fm connector, Apple Music connector (MusicKit JS + backend) | Parser handles every known key-name variant (2021–2026 exports); **a test asserts the upload payload contains no `ip_addr`, `conn_country`, `username`, or incognito rows**; a 150k-play export parses in under 10 s in a Web Worker; raw uploads deleted after processing |
| 3 Knowledge | Plus the identity-resolution queue | ≥90% of test exports' listening time resolved to MusicBrainz IDs; rate limits never exceeded |
| 4 Listener model | §3.5 | Time-split backtest beats "user's top tracks" and "global popularity" on Recall@50 and nDCG@20 |
| 5 Agent | The familiar pool comes from the user's own history | Same gates, plus the familiar/new ratio honored within ±10% |
| 6 App | Output to the Apple Music library, CSV and M3U; in-page MusicKit player | Apple Music write tested end-to-end with a subscriber tester; the export → TuneMyMusic → Spotify flow tested by hand and documented |
| 8 Compliance | DPIA, consent notice review, EU-representative decision | Deletion cascades to OpenAI and LangSmith; notice reviewed |

## 6. Cost and latency budget (Sol at about $2 / $10 per 1M tokens)

| Step | Tokens (in / out) | Cost |
|---|---|---|
| parse_intent | ~600 / 150 | ~$0.003 |
| curate (≈120 candidate cards) | ~6k / 1.5k | ~$0.027 |
| **simple playlist** | | **≈ $0.03–0.05**, p95 < 20 s |
| **deep playlist** (10–20 calls) | | **≈ $0.15–0.30**, p95 < 90 s |
| vibe card per recording, Sol | ~250 / 120 | ~$0.0017 (10k recordings ≈ $17) |
| vibe card per recording, Luna | ~250 / 120 | ~$0.0001 (10k recordings ≈ $1) |

Measured so far: `gpt-6-sol` parsed a mood prompt in about 3 s; under load (8 parallel), the older `gpt-5.4-mini` had p50 1.6 s.

## 7. Repo layout

```
apps/api/src/moodify/{core,llm,knowledge,providers/spotify,agents,api}/
apps/api/tests/{unit,integration,evals,stress}/
apps/web/            Next.js PWA
supabase/            config.toml, migrations/, seed.sql, tests/ (RLS)
docs/                PLAN.md (this file), decisions/ (ADRs)
```

## 8. Decisions needed from you

1. **Product shape (§2.4).** **Shape A** gives full-history modelling from user-uploaded exports, no user cap, and output via Apple Music or file import. **Shape B** writes directly to Spotify but with no AI over Spotify data and 5 users. Shape A fits "get their data, model it, curate by mood"; Shape B fits "write into Spotify and queue it". (Within Shape B: clean-room, or ask Spotify for written permission to model library data?)
2. **Reach.** Accept a 5-tester private beta on Spotify and add a second provider later for public launch, or stay Spotify-only?
3. **Queue / "Play now".** It makes the app a Streaming SDA, which can never be monetized. Keep it, or ship playlist-only (which may charge)?
4. **Bulk enrichment model.** Sol for everything as briefed, or Luna for the one-off per-recording vibe cards (about 17× cheaper)?
5. **Cleanup.** Delete the `_archive/` zips (7.5 GB) and `_trash/`, after a `legacy` branch preserves the code?
6. **Supabase plan.** Free (500 MB, pauses after 1 week idle, no backups) for the beta, or Pro ($25/month: 8 GB, daily backups)?
7. **MusicBrainz contact.** Which project email goes in the MusicBrainz User-Agent? It shouldn't be your personal address.
8. **Taste imports.** Allow a Last.fm username import (recommended in both shapes)? The Spotify "Download your data" upload is the core of Shape A. In Shape B it is *not* allowed, because a Spotify developer app is bound by a definition of Spotify Content that includes material "made available … by Spotify".
9. **Legal review.** Before launch, have a lawyer look at Spotify's user-guideline AI clause (Shape A), Apple's "automated … analysis" wording (any Apple Music use), and the DPDP and GDPR set-up.

## 9. Sources

- Spotify Developer Terms v10: https://developer.spotify.com/terms
- Spotify Developer Policy: https://developer.spotify.com/policy
- Feb 2026 developer access update: https://developer.spotify.com/blog/2026-02-06-update-on-developer-access-and-platform-security
- Feb 2026 Web API changes: https://developer.spotify.com/documentation/web-api/references/changes/february-2026
- Nov 2024 deprecations: https://developer.spotify.com/blog/2024-11-27-changes-to-the-web-api
- Quota modes: https://developer.spotify.com/documentation/web-api/concepts/quota-modes
- Jul 2026 quota updates: https://developer.spotify.com/blog/2026-07-23-web-api-quota-updates
- Refresh-token expiry: https://developer.spotify.com/blog/2026-06-18-refresh-token-expiration
- Add to queue: https://developer.spotify.com/documentation/web-api/reference/add-to-queue
- Prompted Playlists expansion: https://newsroom.spotify.com/2026-01-22/prompted-playlists-expansion/
- LangChain v1 agents: https://docs.langchain.com/oss/python/langchain/agents
- Human in the loop: https://docs.langchain.com/oss/python/langchain/human-in-the-loop
- LangGraph interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts
- LangGraph persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- Deep Agents: https://docs.langchain.com/oss/python/deepagents/overview
- Deep Agents subagents: https://docs.langchain.com/oss/python/deepagents/subagents
- Runtime context: https://docs.langchain.com/oss/python/langchain/runtime
- LangSmith deployment: https://docs.langchain.com/langsmith/deployment
- Agent testing: https://docs.langchain.com/oss/python/langchain/test/unit-testing
- OpenAI pricing (third-party): https://www.morphllm.com/openai-api-pricing
- Supabase social login (provider tokens): https://supabase.com/docs/guides/auth/social-login
- Supabase connecting to Postgres: https://supabase.com/docs/guides/database/connecting-to-postgres
- Supabase prepared statements: https://supabase.com/docs/guides/troubleshooting/disabling-prepared-statements-qL8lEL
- Supabase HNSW indexes: https://supabase.com/docs/guides/ai/vector-indexes/hnsw-indexes
- Supabase Vault: https://supabase.com/docs/guides/database/vault
- Supabase pricing: https://supabase.com/pricing
- langgraph #2030 (prepared statements): https://github.com/langchain-ai/langgraph/issues/2030
- MusicBrainz rate limiting: https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting
- MusicBrainz database licences: https://musicbrainz.org/doc/MusicBrainz_Database
- Last.fm API terms: https://www.last.fm/api/tos
- ListenBrainz Labs: https://labs.api.listenbrainz.org/
- AcousticBrainz downloads: https://acousticbrainz.org/download
- MuSe dataset: https://zenodo.org/record/4281165
- Deezer mood dataset: https://github.com/deezer/deezer_mood_detection_dataset
- ReccoBeats terms: https://reccobeats.com/docs/documentation/terms-of-service
- Deezer API terms: https://developers.deezer.com/termsofuse
- Apple Music transfer: https://support.apple.com/118249
- SongShift FAQ: https://www.songshift.com/faq
- Spotify import (TuneMyMusic): https://newsroom.spotify.com/2025-11-20/spotify-playlists-guide-transfer/
- DTI music transfer: https://dtinit.org/blog/2024/08/27/DTI-members-new-music-tool
- Spotify "Understanding my data": https://support.spotify.com/us/article/understanding-my-data/
- Spotify data rights: https://support.spotify.com/in-en/article/data-rights-and-privacy-settings/
- Spotify privacy policy (India): https://www.spotify.com/in-en/legal/privacy-policy/
- Spotify user guidelines: https://www.spotify.com/us/legal/user-guidelines/
- WP29 data-portability guidelines: https://www.edpb.europa.eu/documents/296_en
- DPDP Act 2023: https://www.indiacode.nic.in/handle/123456789/22037
- DPDP Rules 2025: https://www.meity.gov.in/static/uploads/2025/11/53450e6e5dc0bfa85ebd78686cadad39.pdf
- Apple Developer Program License Agreement: https://developer.apple.com/support/terms/apple-developer-program-license-agreement/
- Apple Media Services Terms: https://www.apple.com/legal/internet-services/itunes/us/terms.html
- Apple Music API: https://developer.apple.com/documentation/applemusicapi
- MusicKit on the Web: https://js-cdn.music.apple.com/musickit/v3/docs/
- Apple Music identity guidelines: https://marketing.services.apple/apple-music-identity-guidelines
- YouTube developer policies: https://developers.google.com/youtube/terms/developer-policies
