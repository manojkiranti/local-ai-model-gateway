"""Live embedding-backend check. Skips unless Ollama is up AND the configured
model is pulled — so the offline suite stays green, but a real run proves the
contract the schema depends on.
"""

import asyncio

import httpx
import pytest

from app.config import get_settings
from app.ollama.client import OllamaClient
from app.rag.embedding import embed_texts


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def settings():
    return get_settings()


@pytest.fixture(scope="module")
def model_available(settings):
    try:
        resp = httpx.get(f"{settings.ollama_base_url}/api/tags", timeout=5)
        names = {m["name"] for m in resp.json().get("models", [])}
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Ollama unreachable: {type(exc).__name__}")
    if settings.rag_embed_model not in names:
        pytest.skip(
            f"{settings.rag_embed_model} not pulled "
            f"(run: ollama pull {settings.rag_embed_model})"
        )
    return settings.rag_embed_model


def test_document_embedding_is_exactly_1536_and_unit_length(settings, model_available):
    async def go():
        client = OllamaClient(settings.ollama_base_url, settings.ollama_timeout)
        try:
            return await embed_texts(
                client, ["Annual leave accrues monthly."], mode="document",
                model=model_available, dim=settings.rag_embed_dim,
                batch_size=settings.rag_embed_batch,
            )
        finally:
            await client.aclose()

    vecs = _run(go())
    assert len(vecs) == 1
    assert len(vecs[0]) == settings.rag_embed_dim == 1536
    norm = sum(x * x for x in vecs[0]) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def _cosine(u, v):
    """Both vectors are already unit length, so the dot product IS cosine."""
    return sum(a * b for a, b in zip(u, v))


def test_batch_order_is_preserved_against_the_real_backend(settings, model_available):
    """Identical inputs must come back at positions 0 and 2, not shuffled.

    Compared by COSINE, not element-wise equality. Measured against
    qwen3-embedding:4b-q8_0: the model is bit-deterministic for a given input
    embedded alone (cos = 1.0000000000), but an input's position within a batch
    perturbs it by ~0.002 per component — padding, attention masking and
    reduction order all differ. Element-wise `approx(abs=1e-6)` therefore fails
    on a perfectly correct result.

    Cosine also makes this a STRONGER shuffle detector than exact equality was:
    a genuine misordering scores ~0.78 here, nowhere near the threshold.
    """
    async def go():
        client = OllamaClient(settings.ollama_base_url, settings.ollama_timeout)
        try:
            return await embed_texts(
                client, ["alpha alpha alpha", "beta beta beta", "alpha alpha alpha"],
                mode="document", model=model_available,
                dim=settings.rag_embed_dim, batch_size=8,
            )
        finally:
            await client.aclose()

    a, b, a2 = _run(go())
    assert _cosine(a, a2) > 0.999, "identical inputs did not map to the same position"
    assert _cosine(a, b) < 0.95, "different inputs were not distinguishable"


def test_query_and_document_modes_produce_different_vectors(settings, model_available):
    """Proves the instruction prefix is actually reaching the model — if these
    matched, the asymmetry would be silently absent."""
    async def go():
        client = OllamaClient(settings.ollama_base_url, settings.ollama_timeout)
        try:
            text = "annual leave entitlement"
            q = await embed_texts(client, [text], mode="query",
                                  model=model_available,
                                  dim=settings.rag_embed_dim, batch_size=4)
            d = await embed_texts(client, [text], mode="document",
                                  model=model_available,
                                  dim=settings.rag_embed_dim, batch_size=4)
            return q[0], d[0]
        finally:
            await client.aclose()

    q, d = _run(go())
    assert q != pytest.approx(d, abs=1e-6)
