"""Phase 6 — embeddings: the CLAP model loads and its vector space matches the Qdrant collection."""
import ast

import numpy as np
import pytest

from .conftest import ROOT


def _cos(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


@pytest.fixture(scope="session")
def clap():
    try:
        from src.services.ai_engine import get_clap_model

        return get_clap_model()
    except BaseException as e:  # laion_clap raises RuntimeError on state-dict mismatch, ImportError on bad deps
        pytest.fail(f"get_clap_model() failed: {type(e).__name__}: {str(e)[:600]}")


def test_checkpoint_path_is_explicit():
    """load_ckpt() with no path downloads ~1.8 GB into site-packages at runtime: not writable by the non-root container user."""
    tree = ast.parse((ROOT / "src/services/ai_engine.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "load_ckpt"]
    assert calls and all(c.args or c.keywords for c in calls), "pass an explicit, pre-downloaded ckpt path to load_ckpt()"


@pytest.mark.slow
def test_clap_model_loads(clap):
    model, device = clap
    assert model is not None and device in {"cpu", "cuda"}


@pytest.mark.slow
def test_audio_embedding_dim_matches_settings(clap, settings):
    from src.services.ai_engine import get_vector_size

    assert get_vector_size(*clap) == settings.VECTOR_SIZE


@pytest.mark.slow
def test_text_and_audio_share_a_space(clap):
    from src.services.ai_engine import get_vector_size

    model, device = clap
    text = model.get_text_embedding(["calm piano"], use_tensor=False)
    assert text.shape[-1] == get_vector_size(model, device)


@pytest.mark.slow
def test_text_embeddings_rank_semantically(clap):
    model, _ = clap
    sad, sad2, metal = model.get_text_embedding(
        ["sad slow piano ballad", "melancholic soft piano", "aggressive heavy metal with screaming"], use_tensor=False
    )
    assert _cos(sad, sad2) > _cos(sad, metal)


@pytest.mark.slow
def test_text_embeddings_are_deterministic(clap):
    model, _ = clap
    a, b = model.get_text_embedding(["rainy day jazz", "rainy day jazz"], use_tensor=False)
    assert np.allclose(a, b, atol=1e-5)
