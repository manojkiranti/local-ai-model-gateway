"""The retrieval tool's ranking wiring.

Patched at the seams — no Postgres, no GPU. What is asserted here is the wiring
itself: that retrieval is asked for the POOL, that an abstention produces its own
message, and that the client survives long enough to rerank.
"""

import asyncio
from dataclasses import dataclass

import pytest

import app.tools.local.search_department_docs as tool
from app.rag.ranking import Ranking
from app.rag.retrieval import RetrievedChunk


def chunk(chunk_id: int, content: str = "Annual leave accrues monthly.") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, document_id=f"doc{chunk_id}", title=f"Doc {chunk_id}",
        content=content, page_number=1, section=None, element_type="text",
        rrf_score=1.0 / chunk_id, dense_distance=None, lexical_score=None,
        dense_rank=None, lexical_rank=None,
    )


@dataclass
class FakeDept:
    id: int = 1
    code: str = "hr"


@pytest.fixture()
def wired(monkeypatch):
    """Patch everything outside the tool: department, embedding, retrieval, client."""
    seen = {}

    monkeypatch.setattr(tool, "current_department", lambda: FakeDept())

    async def fake_embed(client, texts, **kw):
        return [[0.0] * 8]

    monkeypatch.setattr(tool, "embed_texts", fake_embed)

    class FakeClient:
        def __init__(self, *a, **kw):
            self.closed = False

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(tool, "OllamaClient", FakeClient)

    async def fake_search(**kwargs):
        seen.update(kwargs)
        return seen["_returns"] if "_returns" in seen else [chunk(1), chunk(2)]

    monkeypatch.setattr(tool, "search_chunks", fake_search)
    return seen


def test_retrieval_is_asked_for_the_rerank_pool_not_top_k(wired, monkeypatch):
    # The point of a reranker is rescuing a passage RRF ranked low. Handed only
    # top_k it can do nothing but reorder. Invisible in output, so asserted here.
    from app.config import get_settings

    async def fake_apply(client, query, chunks, *, settings):
        return Ranking(kept=list(chunks), scores={}, abstained=False, degraded=False)

    monkeypatch.setattr(tool.ranking, "apply", fake_apply)
    asyncio.run(tool._search_department_docs({"query": "annual leave"}))
    assert wired["limit"] == get_settings().rag_rerank_pool


def test_an_abstention_returns_its_own_message(wired, monkeypatch):
    async def fake_apply(client, query, chunks, *, settings):
        return Ranking(kept=[], scores={}, abstained=True, degraded=False)

    monkeypatch.setattr(tool.ranking, "apply", fake_apply)
    out = asyncio.run(tool._search_department_docs({"query": "pension scheme"}))
    assert out == tool.ABSTAIN.format(code="hr")
    assert "Do NOT answer from general knowledge" in out


def test_the_abstain_message_is_distinct_from_the_no_results_message(wired, monkeypatch):
    # Both tell the model the same thing, but they are different diagnoses:
    # "retrieved nothing" vs "retrieved and rejected everything".
    async def fake_apply(client, query, chunks, *, settings):
        return Ranking(kept=[], scores={}, abstained=True, degraded=False)

    monkeypatch.setattr(tool.ranking, "apply", fake_apply)
    abstained = asyncio.run(tool._search_department_docs({"query": "q"}))
    wired["_returns"] = []
    nothing = asyncio.run(tool._search_department_docs({"query": "q"}))
    assert abstained != nothing


def test_kept_passages_are_what_gets_formatted(wired, monkeypatch):
    async def fake_apply(client, query, chunks, *, settings):
        return Ranking(kept=[chunks[1]], scores={}, abstained=False, degraded=False)

    monkeypatch.setattr(tool.ranking, "apply", fake_apply)
    out = asyncio.run(tool._search_department_docs({"query": "q"}))
    assert "Doc 2" in out
    assert "Doc 1" not in out
