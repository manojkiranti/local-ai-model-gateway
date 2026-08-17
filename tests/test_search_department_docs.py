"""search_department_docs contract. Pure — retrieval and embedding are faked.

The three things that must hold no matter what the model sends: the department
cannot be chosen, hostile arguments are clamped, and the serialized result stays
under the agent loop's tool-result cap without losing a citation header.
"""

import asyncio

import pytest

from app.agent.loop import MAX_TOOL_RESULT_CHARS
from app.config import get_settings
from app.rag.context import DepartmentContext, rag_context
from app.rag.retrieval import RetrievedChunk
from app.tools.local import search_department_docs as tool

HR = DepartmentContext(id=1, code="hr")


def _chunk(i, body="Annual leave accrues monthly.", page=3, title="HR Leave Policy"):
    return RetrievedChunk(
        chunk_id=i, document_id=f"doc{i}", title=title, content=body,
        page_number=page, section="Leave Policy > Annual", element_type="text",
        rrf_score=1.0 / (60 + i + 1), dense_distance=0.2, lexical_score=0.5,
        dense_rank=i, lexical_rank=i,
    )


@pytest.fixture()
def faked(monkeypatch):
    """Fake embedding + retrieval; record what retrieval was asked for."""
    seen = {}

    async def fake_embed(client, texts, **kw):
        seen["embedded"] = texts[0]
        seen["mode"] = kw.get("mode")
        return [[0.1] * 1536]

    async def fake_search(**kw):
        seen.update(kw)
        return seen.get("results", [])

    monkeypatch.setattr(tool, "embed_texts", fake_embed)
    monkeypatch.setattr(tool, "search_chunks", fake_search)
    return seen


def _run(args):
    return asyncio.run(tool._search_department_docs(args))


# --------------------------------------------------------------------------- #
# The department is not the model's to choose
# --------------------------------------------------------------------------- #
def test_schema_has_no_department_parameter():
    """A prompt injection needs somewhere to put a department. There isn't one."""
    props = tool.SPEC.parameters["properties"]
    assert set(props) == {"query", "top_k"}
    assert "department" not in str(tool.SPEC.parameters).lower()


def test_refuses_explicitly_when_no_department_is_active(faked):
    out = _run({"query": "annual leave"})
    assert "ERROR" in out and "general chat" in out
    assert "department" not in faked  # retrieval never ran


def test_uses_the_department_from_the_contextvar(faked):
    faked["results"] = [_chunk(1)]
    with rag_context(HR):
        _run({"query": "annual leave"})
    assert faked["department_id"] == HR.id


def test_queries_are_embedded_in_query_mode(faked):
    """Qwen3-Embedding is asymmetric — a document-mode query silently degrades."""
    faked["results"] = [_chunk(1)]
    with rag_context(HR):
        _run({"query": "annual leave"})
    assert faked["mode"] == "query"


# --------------------------------------------------------------------------- #
# Hostile arguments
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("hostile", [100000, -5, 0, "5", "abc", None, 3.7])
def test_top_k_is_clamped_regardless_of_what_the_model_sends(faked, hostile):
    """JSON Schema bounds are advisory; the clamp is in Python."""
    faked["results"] = [_chunk(1)]
    with rag_context(HR):
        _run({"query": "leave", "top_k": hostile})
    assert 1 <= faked["limit"] <= get_settings().rag_top_k


def test_query_length_is_clamped_before_embedding(faked):
    faked["results"] = [_chunk(1)]
    with rag_context(HR):
        _run({"query": "x" * 50_000})
    assert len(faked["embedded"]) <= get_settings().rag_max_query_chars


def test_an_empty_query_is_rejected(faked):
    with rag_context(HR):
        assert "ERROR" in _run({"query": "   "})


# --------------------------------------------------------------------------- #
# Results, citations, and the size budget
# --------------------------------------------------------------------------- #
def test_no_results_is_explicit_not_an_empty_list(faked):
    """An empty result reads to the model as unremarkable and invites an answer
    from its own parameters."""
    faked["results"] = []
    with rag_context(HR):
        out = _run({"query": "annual leave"})
    assert "No matching passages" in out
    assert "Do NOT answer from general knowledge" in out


def test_citations_are_preserved(faked):
    faked["results"] = [_chunk(1)]
    with rag_context(HR):
        out = _run({"query": "annual leave"})
    assert "HR Leave Policy" in out
    assert "page 3" in out
    assert "doc=doc1" in out
    assert "Leave Policy > Annual" in out


def _nrb_chunk(i, *, route, authoritative=None, page_url="https://www.nrb.org.np/x",
               published="2024-05-12", title="AML Directive 2081", body="अनुसूची."):
    chunk_meta = {"origin": "nrb", "route": route}
    if authoritative is not None:
        chunk_meta["authoritative"] = authoritative
    doc_meta = {"origin": "nrb"}
    if page_url:
        doc_meta["page_url"] = page_url
    if published:
        doc_meta["published_at"] = published
    return RetrievedChunk(
        chunk_id=i, document_id=f"nrb{i}", title=title, content=body,
        page_number=4, section=None, element_type="text",
        rrf_score=1.0 / (60 + i + 1), dense_distance=0.2, lexical_score=0.5,
        dense_rank=i, lexical_rank=i,
        chunk_metadata=chunk_meta, doc_metadata=doc_meta,
    )


def test_an_nrb_ocr_passage_shows_its_route_and_a_trust_caveat(faked):
    """OCR/legacy text is retrieval material, never authoritative (§16.6). The
    citation must say so, or the model quotes a machine-recovered figure as fact."""
    faked["results"] = [_nrb_chunk(1, route="ocr", authoritative=False)]
    with rag_context(HR):
        out = _run({"query": "money laundering rules"})
    assert "AML Directive 2081" in out
    assert "ocr" in out.lower()
    assert "verify" in out.lower()           # the trust caveat
    assert "nrb.org.np" in out               # the source
    assert "2024-05-12" in out               # published date


def test_an_nrb_legacy_conversion_passage_is_also_caveated(faked):
    faked["results"] = [_nrb_chunk(1, route="legacy_conversion")]
    with rag_context(HR):
        out = _run({"query": "directive"})
    assert "legacy_conversion" in out
    assert "verify" in out.lower()


def test_an_nrb_native_passage_shows_route_without_the_verify_caveat(faked):
    """Native text was not machine-recovered, so it does not carry the caveat —
    over-warning on trustworthy text would train the model to ignore the warning."""
    faked["results"] = [_nrb_chunk(1, route="native")]
    with rag_context(HR):
        out = _run({"query": "policy"})
    assert "native" in out.lower()
    assert "verify" not in out.lower()


def test_a_generic_department_chunk_shows_no_route_line(faked):
    """The NRB provenance is additive: an ordinary upload's citation is unchanged."""
    faked["results"] = [_chunk(1)]
    with rag_context(HR):
        out = _run({"query": "annual leave"})
    assert "route:" not in out.lower()
    assert "verify" not in out.lower()


def test_a_chunk_without_a_page_still_cites_its_document(faked):
    faked["results"] = [_chunk(1, page=None)]
    with rag_context(HR):
        out = _run({"query": "annual leave"})
    assert "HR Leave Policy" in out and "page" not in out.split("doc=")[0].split("[1]")[1]


def test_result_stays_under_the_agent_loop_cap(faked):
    """12 chunks at 2000 chars is ~24k — a bare cut in the loop would sever a
    citation header and leave the model quoting the wrong document's page."""
    faked["results"] = [_chunk(i, body="word " * 800) for i in range(1, 13)]
    with rag_context(HR):
        out = _run({"query": "annual leave", "top_k": 12})
    assert len(out) <= MAX_TOOL_RESULT_CHARS
    assert len(out) <= get_settings().rag_tool_result_max_chars


def test_every_surviving_passage_keeps_its_header_when_trimmed(faked):
    faked["results"] = [_chunk(i, body="word " * 800) for i in range(1, 13)]
    with rag_context(HR):
        out = _run({"query": "annual leave", "top_k": 12})
    # However many survived, each is numbered and attributed.
    kept = [n for n in range(1, 13) if f"[{n}]" in out]
    assert kept, "no passage survived the budget"
    for n in kept:
        assert f"doc=doc{n}" in out


def test_trimming_announces_itself(faked):
    faked["results"] = [_chunk(i, body="word " * 800) for i in range(1, 13)]
    with rag_context(HR):
        out = _run({"query": "annual leave", "top_k": 12})
    assert "trimmed" in out.lower()


def test_a_small_result_is_not_trimmed(faked):
    faked["results"] = [_chunk(1, body="Annual leave accrues monthly.")]
    with rag_context(HR):
        out = _run({"query": "annual leave"})
    assert "Annual leave accrues monthly." in out
    assert "passage trimmed" not in out


def test_output_tells_the_model_to_answer_only_from_these_passages(faked):
    faked["results"] = [_chunk(1)]
    with rag_context(HR):
        out = _run({"query": "annual leave"})
    assert "ONLY from these passages" in out
    assert "cite" in out.lower()
