"""Phase 9 — stress: concurrency and throughput of each stage. Run with -s to see the numbers."""
import asyncio
import statistics
import time

import numpy as np
import pytest

from .test_p5_ingestion import FakeQdrant, _run, ingestion  # noqa: F401  (fixture re-export)

QDRANT_QUERIES, QDRANT_CONCURRENCY, QDRANT_P95_MS = 1000, 50, 250
STRESS_COLLECTION, STRESS_POINTS = "moodify_checkup_stress", 8000  # fma_small has 8000 tracks
LLM_BURST, LLM_P95_S = 8, 20


def _pct(samples, q):
    return float(np.percentile(samples, q))


async def _bounded(n, concurrency, make_call):
    sem = asyncio.Semaphore(concurrency)
    lat, errors = [], []

    async def one(i):
        async with sem:
            t0 = time.perf_counter()
            try:
                await make_call(i)
                lat.append(time.perf_counter() - t0)
            except Exception as e:
                errors.append(f"{type(e).__name__}: {e}")

    t0 = time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(n)))
    return lat, errors, time.perf_counter() - t0


@pytest.fixture(scope="module")
def stress_collection(local_qdrant, settings):
    """Throwaway collection sized like fma_small, so the load test hits a real HNSW index (dropped afterwards)."""
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    client = QdrantClient(url=local_qdrant, timeout=60)
    client.recreate_collection(STRESS_COLLECTION, vectors_config=VectorParams(size=settings.VECTOR_SIZE, distance=Distance.COSINE))
    data = np.random.default_rng(3).standard_normal((STRESS_POINTS, settings.VECTOR_SIZE)).astype(np.float32)
    for start in range(0, STRESS_POINTS, 1000):
        batch = [PointStruct(id=i, vector=data[i].tolist(), payload={"title": f"t{i}"}) for i in range(start, min(start + 1000, STRESS_POINTS))]
        client.upsert(STRESS_COLLECTION, points=batch, wait=True)
    yield STRESS_COLLECTION
    client.delete_collection(STRESS_COLLECTION)
    client.close()


@pytest.mark.docker
def test_qdrant_concurrent_search_load(local_qdrant, stress_collection, settings):
    """Infra ceiling: raw AsyncQdrantClient, 1000 searches at concurrency 50 over a populated collection."""
    from qdrant_client import AsyncQdrantClient

    name = stress_collection
    vecs = np.random.default_rng(1).standard_normal((QDRANT_QUERIES, settings.VECTOR_SIZE)).astype(np.float32)

    async def go():
        client = AsyncQdrantClient(url=local_qdrant, timeout=30)
        try:
            return await _bounded(
                QDRANT_QUERIES, QDRANT_CONCURRENCY,
                lambda i: client.search(collection_name=name, query_vector=vecs[i].tolist(), limit=10),
            )
        finally:
            await client.close()

    lat, errors, wall = asyncio.run(go())
    p50, p95, p99 = (_pct(lat, q) * 1000 for q in (50, 95, 99)) if lat else (0, 0, 0)
    print(f"\nqdrant {name}: {len(lat)} ok / {len(errors)} err, {len(lat) / wall:.0f} qps, "
          f"p50 {p50:.1f}ms p95 {p95:.1f}ms p99 {p99:.1f}ms")
    assert not errors, errors[:3]
    assert p95 < QDRANT_P95_MS


@pytest.mark.docker
def test_project_searcher_burst(local_qdrant, settings):
    """Same burst through the project's VibeSearch (configured collection, Result pattern)."""
    from src.services.searcher import get_searcher

    vecs = np.random.default_rng(2).standard_normal((200, settings.VECTOR_SIZE)).astype(np.float32)

    async def go():
        s = get_searcher()
        results = []

        async def call(i):
            r = await s.perform_vector_search(vecs[i])
            results.append(r)
            if r.is_err():
                raise RuntimeError(r.unwrap_err())

        try:
            return await _bounded(len(vecs), QDRANT_CONCURRENCY, call)
        finally:
            await s.close()

    lat, errors, wall = asyncio.run(go())
    print(f"\nVibeSearch burst: {len(lat)} ok / {len(errors)} err in {wall:.2f}s")
    assert not errors, errors[:3]


@pytest.mark.live
def test_query_engine_burst(settings):
    from src.services.query_engine import QueryEngine

    engine = QueryEngine()
    prompts = ["sad", "hype gym", "jazz cafe", "focus coding", "summer beach", "dark techno", "folk campfire", "k-pop"]

    lat, errors, wall = asyncio.run(_bounded(LLM_BURST, LLM_BURST, lambda i: engine.parse(prompts[i % len(prompts)])))
    print(f"\n{settings.OPENAI_MODEL} burst x{LLM_BURST}: {len(lat)} ok / {len(errors)} err, "
          f"p50 {statistics.median(lat) if lat else 0:.2f}s max {max(lat, default=0):.2f}s wall {wall:.2f}s")
    assert not errors, errors[:3]
    assert _pct(lat, 95) < LLM_P95_S


def test_ingestion_throughput(ingestion, settings, tmp_path, monkeypatch):  # noqa: F811
    from .test_p5_ingestion import _write_fma_csv
    from . import test_p5_ingestion as p5

    monkeypatch.setattr(p5, "N_SMALL", 20_000)
    monkeypatch.setattr(p5, "N_MEDIUM", 0)
    big = tmp_path / "big"
    (big / "raw").mkdir(parents=True)
    for p in (big / "raw" / "tracks.csv", big / "tracks.csv"):
        _write_fma_csv(p)
    monkeypatch.setattr(settings, "DATA_DIR", big)

    t0 = time.perf_counter()
    client = _run(ingestion, FakeQdrant())
    wall = time.perf_counter() - t0
    print(f"\ningestion: {len(client.points)} tracks in {wall:.2f}s ({len(client.points) / wall:.0f} tracks/s)")
    assert len(client.points) == 20_000 and wall < 60
