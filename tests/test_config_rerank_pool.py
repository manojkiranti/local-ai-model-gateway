"""The pool must never be smaller than what may be presented.

`rag_rerank_pool` is the candidate list retrieval fetches; `rag_top_k` is how many
passages may reach the model. `RAG_RERANK_POOL=5` with `RAG_TOP_K=12` is a
misconfiguration with no visible symptom — the answer just cites fewer passages
than the deployment asked for — so it fails at import instead, exactly like the
AD-auth coherence check next to it.
"""

import pytest

from app.config import Settings

BASE_ENV = {
    "database_url": "postgresql+asyncpg://u:p@127.0.0.1:5432/db",
    "jwt_secret": "test-secret",
}


def settings_with(**overrides) -> Settings:
    return Settings(**BASE_ENV, **overrides)


def test_a_pool_smaller_than_top_k_is_refused_at_import():
    with pytest.raises(ValueError) as exc:
        settings_with(rag_rerank_pool=5, rag_top_k=12)
    message = str(exc.value)
    assert "RAG_RERANK_POOL" in message and "RAG_TOP_K" in message


def test_a_pool_equal_to_top_k_is_allowed():
    assert settings_with(rag_rerank_pool=12, rag_top_k=12).rag_rerank_pool == 12


def test_the_shipped_defaults_satisfy_the_invariant():
    settings = settings_with()
    assert settings.rag_rerank_pool >= settings.rag_top_k
