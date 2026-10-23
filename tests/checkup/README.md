# Moodify checkup

A phased health check for the whole pipeline. Each phase assumes the previous one passes;
fix in order, and don't move on until a phase is green.

```
FMA zips ──unzip──▶ data/fma_metadata/tracks.csv ──▶ IngestionService ──▶ Qdrant collection
                    data/fma_small/*.mp3 ──▶ CLAP audio encoder ──┘          ▲
user query ──▶ OpenAI (gpt-5.4-mini) ──▶ QueryIntent.clap_prompt ──▶ CLAP text encoder ──▶ VibeSearch ──▶ FastAPI
```

## Running

```bash
.venv/bin/python -m pytest tests/checkup                      # everything
.venv/bin/python -m pytest tests/checkup/test_p0_hygiene.py   # one phase
.venv/bin/python -m pytest tests/checkup -m "not slow and not docker and not live"   # offline only
.venv/bin/python -m pytest tests/checkup/test_p9_stress.py -s # print load-test numbers
```

Markers: `live` (OpenAI, Qdrant Cloud, PyPI), `docker` (starts the compose `qdrant` service / builds images),
`slow` (loads the 1.8 GB CLAP checkpoint). The `docker` tests bring up `moodify-qdrant` themselves.

## Phases

| # | Phase | Gate (what must be true) |
|---|-------|--------------------------|
| 0 | Hygiene | no secrets in source; `.gitignore`/`.dockerignore` cover `.env`, storage, archives, venv; build context < 100 MB |
| 1 | Config | `.env` is valid dotenv; `Settings` validates strictly; Qdrant URL + keys present; bad config raises, never `sys.exit` |
| 2 | Dependencies | every import declared and pinned; torch resolves CPU-only; every module in `src/`, `scripts/`, `notebooks/` imports |
| 3 | Docker | CMD points at a real module; compose doesn't mount over `/app`; compose passes settings; ports free; image builds CPU-only |
| 4 | Qdrant | configured collection exists, has points, vector size matches, payloads match `SongVibe`, vectors aren't random; cloud reachable; client honours URL/port |
| 5 | Ingestion | `RawTrack` matches the rows; small subset only; bounded batches; no `"nan"` strings; deterministic vectors; never drops the live collection; data unzipped |
| 6 | Embeddings | CLAP loads from an explicit checkpoint; audio dim == text dim == `VECTOR_SIZE`; text space ranks sensibly |
| 7 | Query + search | OpenAI intent parsing is model-configurable, input-capped, robust to edge/hostile input; searcher returns `Err` not exceptions; finds hits; end-to-end text query returns songs |
| 8 | API | `src.main:app` exists; `/health`; `/api/v1/search` validates and returns songs |
| 9 | Stress | 1000 Qdrant searches @ 50 concurrency with p95 < 250 ms and 0 errors; project searcher burst 200 @ 50 with 0 errors; 8 parallel LLM parses with p95 < 20 s; 20k-row ingestion < 60 s |

## Fix list (from the 2026-10-22 run)

**P0 hygiene**
- Rotate the Qdrant key hardcoded in `_trash/session_backup.py` (lines 46, 68, 77), then delete the file.
- `.dockerignore`: patterns are anchored at the context root, so use `**/__pycache__`, `**/*.pyc`; add `.venv/`, `.qdrant_storage/` (currently misspelled `qdrant_starage/`), `_archive/`, `_trash/`, `logs/`, `data/`, `notebooks/`, `tests/`. Context is ~9 GB today (`_archive/graveyard_backup/fma_small.zip` alone is 7.7 GB).
- `git rm --cached app/main.py` (staged, but deleted on disk).

**P1 config**
- Delete the two Python lines at the bottom of `.env` (`DATA_DIR: Path = Field(...)`).
- Set `QDRANT_URL` to the cluster the new API key belongs to (the old cluster `7c477188-….gcp.cloud.qdrant.io` resets TLS: suspended or deleted).
- `ENV` pattern needs `$`; `min_lengths` → `min_length`; raise instead of `sys.exit(1)` in `config.py`.

**P2 dependencies**
- Remove the duplicate `qdrant-client`; replace the per-line `--index-url` with a top-level `--extra-index-url https://download.pytorch.org/whl/cpu` and `torch==2.1.2+cpu`.
- Declare what the code imports: `numpy`, `pandas`, `uvloop`, `result`, plus `torchvision==0.16.2` (laion_clap imports it but doesn't declare it).
- Pin `transformers==4.30.2` (5.x refuses torch 2.1.2; ≥4.31 breaks CLAP's `position_ids` state-dict keys).
- `src/schemas/ingestion.py`: `hex` is a builtin function, not a type; the model can't even be created. Fields should be `track_id/title/artist/genre/year`.
- `scripts/run_search.py`, `notebooks/astroworld_ingest.py` import `src.core.new_config` / `src.db.client` (moved to `_trash/`); `scripts/run_benchmarks.py` imports `utils.physics_lab` (moved to `_archive/`).

**P3 docker**
- `CMD ["python", "-m", "run_search"]`: no such module. Point it at the API (`uvicorn src.main:app --host 0.0.0.0 --port 8000`).
- Runtime stage: `COPY src/ src/` instead of `COPY . .`.
- Compose: drop `./app:/app` (it hides `src/` inside the container); add `env_file: .env`; drop `version:`.
- Host ports 8000 and 6379 are already taken on this machine (another uvicorn, a local redis): remap, or don't publish redis at all (nothing uses it).
- `load_ckpt()` downloads 1.8 GB into site-packages at runtime, which the non-root user can't write: bake the checkpoint into the image or mount it.

**P4 qdrant**
- Local `songs` collection: 768-d, **0 points**. `COLLECTION_NAME="Moodify"` doesn't exist anywhere.
- qdrant-client switches to https when `api_key` is set, so with the cloud key in `.env` the searcher can't talk to plain-http local Qdrant (`SSL: WRONG_VERSION_NUMBER`). Build every client from one `QDRANT_URL` (`AsyncQdrantClient(url=..., api_key=...)`) instead of hardcoded `host=…, port=6333` in `searcher.py`, `init_db.py`, `seed_db.py`.

**P5 ingestion**
- `ingestion.py` uses `np` without importing numpy, stores missing genres as the string `"nan"`, uses random vectors, and `recreate_collection` wipes the live collection on every run (use a versioned collection + alias swap, as your own notes in `_trash/session_backup.py` say).
- Unzip `fma_metadata.zip` / `fma_small.zip` into `data/`; ingestion reads `DATA_DIR/raw/tracks.csv` but the notebook reads `DATA_DIR/tracks.csv`: pick one.

**P6 embeddings**
- `CLAP_Module(amodel='HTSAT-base')` + default `load_ckpt()` = HTSAT-tiny weights into a base model → state-dict mismatch. Use the default `HTSAT-tiny` with `630k-audioset-best.pt`, or HTSAT-base with `music_audioset_epoch_15_esc_90.14.pt`.
- CLAP embeddings are **512-d** (verified for both audio and text). Set `VECTOR_SIZE=512` and recreate the collection.

**P7–P8**
- Wire `QueryEngine` → CLAP text embedding → `VibeSearch`; write `src/main.py` with `/health` and `POST /api/v1/search`.

**P9**
- Local Qdrant 1.7, 8k × 768-d: 265 qps, p50 168 ms, p95 247 ms at 50 concurrency (first run p95 438 ms): right on the SLO edge because 8k points sit below `indexing_threshold=20000`, so every query is a full scan. Lower the threshold or enable HNSW explicitly.
- `gpt-5.4-mini`: 8 parallel parses, p50 1.6 s, max 2.1 s, 0 errors.
