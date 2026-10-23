"""Phase 5 — ingestion: FMA metadata -> RawTrack -> PointStruct -> Qdrant upsert."""
import asyncio
import importlib
import math
from types import SimpleNamespace

import pandas as pd
import pytest

N_SMALL, N_MEDIUM, BATCH_LIMIT = 250, 20, 100


class FakeQdrant:
    """Records calls instead of talking to Qdrant (IngestionService takes the client by injection)."""

    def __init__(self, existing=()):
        self.collections = set(existing)
        self.calls, self.points = [], {}

    async def get_collections(self):
        return SimpleNamespace(collections=[SimpleNamespace(name=n) for n in self.collections])

    async def collection_exists(self, collection_name):
        return collection_name in self.collections

    async def create_collection(self, collection_name, **_):
        self.calls.append(("create", collection_name))
        self.collections.add(collection_name)

    async def recreate_collection(self, collection_name, **_):
        self.calls.append(("recreate", collection_name))
        self.collections.add(collection_name)

    async def delete_collection(self, collection_name, **_):
        self.calls.append(("delete", collection_name))
        self.collections.discard(collection_name)

    async def upsert(self, collection_name, points, **_):
        self.calls.append(("upsert", len(points)))
        self.points.update({p.id: p for p in points})

    @property
    def upsert_sizes(self):
        return [n for op, n in self.calls if op == "upsert"]


def _write_fma_csv(path):
    cols = pd.MultiIndex.from_tuples(
        [("set", "subset"), ("track", "title"), ("artist", "name"), ("track", "genre_top"), ("track", "date_created")]
    )
    rows, idx = [], []
    for i in range(N_SMALL + N_MEDIUM):
        idx.append(i + 2)
        rows.append(["small" if i < N_SMALL else "medium", f"Song {i}", f"Artist {i % 7}", "Rock", "2008-11-26 01:44:45"])
    rows[3][3] = None  # missing genre_top is common in FMA
    rows[4][4] = None  # missing date
    pd.DataFrame(rows, index=pd.Index(idx, name="track_id"), columns=cols).to_csv(path)


@pytest.fixture
def ingestion(settings, tmp_path, monkeypatch):
    try:
        mod = importlib.import_module("src.services.ingestion")
    except Exception as e:
        pytest.fail(f"src.services.ingestion does not import: {type(e).__name__}: {e}")
    (tmp_path / "raw").mkdir()
    for p in (tmp_path / "raw" / "tracks.csv", tmp_path / "tracks.csv"):
        _write_fma_csv(p)
    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)
    return mod


def _run(mod, client):
    asyncio.run(mod.IngestionService(db_client=client).run())
    return client


def test_rawtrack_accepts_ingestion_fields():
    try:
        from src.schemas.ingestion import RawTrack
    except Exception as e:
        pytest.fail(f"RawTrack schema is invalid: {type(e).__name__}: {str(e).splitlines()[0]}")
    t = RawTrack(track_id=2, title="Food", artist="AWOL", genre="Hip-Hop", year=2008)
    assert t.model_dump()["title"] == "Food"


def test_ingests_only_small_subset(ingestion):
    client = _run(ingestion, FakeQdrant())
    assert len(client.points) == N_SMALL


def test_upserts_in_bounded_batches(ingestion):
    sizes = _run(ingestion, FakeQdrant()).upsert_sizes
    assert sum(sizes) == N_SMALL and max(sizes) <= BATCH_LIMIT
    assert len(sizes) == math.ceil(N_SMALL / BATCH_LIMIT)


def test_payloads_have_no_nan_strings(ingestion):
    client = _run(ingestion, FakeQdrant())
    bad = [(pid, k) for pid, p in client.points.items() for k, v in p.payload.items() if str(v).lower() == "nan"]
    assert not bad, f"missing values stored as the string 'nan': {bad[:5]}"


def test_vectors_have_vector_size_and_are_deterministic(ingestion, settings):
    a, b = _run(ingestion, FakeQdrant()), _run(ingestion, FakeQdrant())
    some_id = next(iter(a.points))
    assert len(a.points[some_id].vector) == settings.VECTOR_SIZE
    assert a.points[some_id].vector == b.points[some_id].vector, "same track embedded twice gave different vectors (mock np.random embeddings)"


def test_does_not_drop_live_collection(ingestion, settings):
    """Re-ingesting must not DELETE the serving collection (see _trash/session_backup.py: use aliases)."""
    client = _run(ingestion, FakeQdrant(existing={settings.COLLECTION_NAME}))
    destructive = [c for c in client.calls if c[0] in {"recreate", "delete"} and c[1] == settings.COLLECTION_NAME]
    assert not destructive, f"ingestion wiped the live collection: {destructive}"


def test_metadata_csv_present(settings):
    candidates = [settings.DATA_DIR / "raw" / "tracks.csv", settings.DATA_DIR / "tracks.csv"]
    assert any(p.exists() for p in candidates), f"no tracks.csv under {settings.DATA_DIR} (fma_metadata.zip only lives in _archive/)"


def test_audio_dir_has_tracks(settings):
    n = sum(1 for _ in settings.AUDIO_DIR.rglob("*.mp3"))
    assert n > 0, f"no mp3s under {settings.AUDIO_DIR} (fma_small.zip only lives in _archive/)"
