"""`search_department_docs` records the passages it actually SHOWED.

The tool trims its result to a character budget, dropping whole passages from
the end when they will not fit. Sources must be resolved against the surviving
list: a passage the budget removed was never in the model's context, so listing
its document as a source would invent provenance the answer does not have.
"""

from __future__ import annotations

from app.rag.retrieval import RetrievedChunk
from app.rag.sources import resolve_sources
from app.tools.local import search_department_docs as tool


def retrieved(doc_id: str, *, body_chars: int = 100, page: int = 1) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=abs(hash(doc_id)) % 10_000,
        document_id=doc_id,
        title=f"Title {doc_id}",
        content="x" * body_chars,
        page_number=page,
        section=None,
        element_type="text",
        rrf_score=0.5,
        dense_distance=0.1,
        lexical_score=0.2,
        # Per-channel ranks landed on `main` after this test was written (§29);
        # they are diagnostics and a citation never reads them.
        dense_rank=1,
        lexical_rank=1,
        file_name=f"{doc_id}.pdf",
        file_type="pdf",
    )


def test_format_returns_every_passage_when_the_budget_is_generous():
    chunks = [retrieved(f"doc{i}") for i in range(5)]
    text, presented = tool._format(chunks, department_code="hr", budget=8000)

    assert len(presented) == 5
    for i in range(1, 6):
        assert f"[{i}]" in text


def test_format_drops_trailing_passages_under_a_tight_budget():
    """The dropped ones must not be reported as presented."""
    chunks = [retrieved(f"doc{i}", body_chars=400) for i in range(8)]
    text, presented = tool._format(chunks, department_code="hr", budget=1200)

    assert 0 < len(presented) < 8, "budget should have forced some passages out"
    # Every presented passage still has its numbered header in the text...
    for i in range(1, len(presented) + 1):
        assert f"[{i}]" in text
    # ...and nothing beyond them does.
    assert f"[{len(presented) + 1}]" not in text


def test_presented_prefix_keeps_citation_numbering_aligned():
    """Passages are dropped from the END, so [1..k] still indexes the survivors."""
    chunks = [retrieved(f"doc{i}", body_chars=400) for i in range(8)]
    _, presented = tool._format(chunks, department_code="hr", budget=1200)

    assert [c.document_id for c in presented] == [
        f"doc{i}" for i in range(len(presented))
    ]


def test_sources_never_name_a_document_that_was_trimmed_away():
    """The end-to-end point of this module: an uncited answer falls back to the
    presented set, which must exclude budget-dropped documents."""
    chunks = [retrieved(f"doc{i}", body_chars=400) for i in range(8)]
    _, presented = tool._format(chunks, department_code="hr", budget=1200)

    record = tool.SourceChunk  # imported by the tool; sanity that it is wired
    assert record is not None

    from app.rag.sources import SearchRecord, SourceChunk

    rec = SearchRecord(
        department_code="hr",
        chunks=[
            SourceChunk(
                document_id=c.document_id,
                title=c.title,
                file_name=c.file_name,
                file_type=c.file_type,
                page_number=c.page_number,
            )
            for c in presented
        ],
    )
    sources = resolve_sources([rec], "An answer with no bracket markers.")
    ids = {s["document_id"] for s in sources}

    dropped = {f"doc{i}" for i in range(len(presented), 8)}
    assert ids.isdisjoint(dropped), "trimmed documents must not surface as sources"


def test_format_still_carries_file_metadata_for_citations():
    chunks = [retrieved("docA")]
    _, presented = tool._format(chunks, department_code="hr", budget=8000)
    assert presented[0].file_name == "docA.pdf"
    assert presented[0].file_type == "pdf"
