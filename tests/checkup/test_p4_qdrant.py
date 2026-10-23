"""Phase 4 — Qdrant: on-disk storage, local server, cloud cluster, client wiring."""
import numpy as np
import pytest

from .conftest import http_json, local_collections

SAMPLE = 300


# --- on-disk storage (.qdrant_storage, mounted by compose) -----------------

def test_local_storage_has_collections():
    assert local_collections(), ".qdrant_storage has no collections"


def test_configured_collection_exists_in_local_storage(settings):
    names = sorted(local_collections())
    assert settings.COLLECTION_NAME in names, f"COLLECTION_NAME={settings.COLLECTION_NAME!r} but storage has {names}"


def test_local_storage_vector_size_matches_settings(settings):
    sizes = {n: c["params"]["vectors"]["size"] for n, c in local_collections().items()}
    wrong = {n: s for n, s in sizes.items() if s != settings.VECTOR_SIZE}
    assert not wrong, f"VECTOR_SIZE={settings.VECTOR_SIZE} but stored collections are {wrong}"


# --- local server -----------------------------------------------------------

@pytest.fixture(scope="module")
def local_client(local_qdrant):
    from qdrant_client import QdrantClient

    client = QdrantClient(url=local_qdrant, timeout=10)
    yield client
    client.close()


@pytest.fixture(scope="module")
def sample_points(local_client):
    name = next(iter(local_collections()))
    points, _ = local_client.scroll(name, limit=SAMPLE, with_payload=True, with_vectors=True)
    if not points:
        pytest.fail(f"collection {name!r} is empty: nothing to validate")
    return name, points


@pytest.mark.docker
def test_local_server_serves_storage(local_client):
    served = {c.name for c in local_client.get_collections().collections}
    assert set(local_collections()) <= served


@pytest.mark.docker
def test_local_collection_has_points(local_client):
    counts = {n: local_client.count(n, exact=True).count for n in local_collections()}
    assert all(counts.values()), f"empty collections: {counts}"


@pytest.mark.docker
def test_stored_payloads_match_song_schema(sample_points):
    from src.schemas.songs import SongVibe

    _, points = sample_points
    bad = []
    for p in points:
        try:
            SongVibe.model_validate(p.payload)
        except Exception as e:
            bad.append((p.id, str(e).splitlines()[0]))
    assert not bad, f"{len(bad)}/{len(points)} payloads fail SongVibe, e.g. {bad[:3]}"


@pytest.mark.docker
def test_stored_vectors_are_real_embeddings(sample_points):
    """Uniform np.random.rand vectors are all-positive; real CLAP embeddings are roughly zero-centred."""
    _, points = sample_points
    vecs = np.array([p.vector for p in points], dtype=np.float32)
    negative_share = float((vecs < 0).mean())
    assert negative_share > 0.2, (
        f"only {negative_share:.1%} of components are negative: these are np.random.rand mock vectors, "
        "so every search result is noise"
    )


# --- cloud cluster -------------------------------------------------------------

@pytest.mark.live
def test_cloud_cluster_reachable(settings):
    assert settings.QDRANT_URL, "QDRANT_URL is empty: cannot reach the cloud cluster the API key belongs to"
    status, _ = http_json(f"{settings.QDRANT_URL.rstrip('/')}/", timeout=15)
    assert status == 200


@pytest.mark.live
def test_cloud_api_key_authorizes(settings):
    assert settings.QDRANT_URL, "QDRANT_URL is empty"
    status, body = http_json(
        f"{settings.QDRANT_URL.rstrip('/')}/collections",
        headers={"api-key": settings.QDRANT_API_KEY.get_secret_value()},
        timeout=15,
    )
    assert status == 200, body


# --- client wiring -------------------------------------------------------------

def _rest_uri(async_client) -> str:
    return async_client._client.rest_uri


def test_searcher_honours_qdrant_url_and_port(settings, monkeypatch):
    from src.services import searcher as mod

    monkeypatch.setattr(settings, "QDRANT_URL", "https://example-cluster.cloud.qdrant.io:6333")
    monkeypatch.setattr(settings, "QDRANT_PORT", 7777)
    uri = _rest_uri(mod.VibeSearch().client)
    assert "example-cluster" in uri or ":7777" in uri, f"searcher ignores QDRANT_URL/QDRANT_PORT, connects to {uri}"
