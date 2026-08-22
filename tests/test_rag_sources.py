"""Citation resolution: retrieved passages + a final answer -> visible sources.

Pure unit tests. `app.rag.sources` deliberately avoids importing the database so
this file needs no Postgres.
"""

from __future__ import annotations

import pytest

from app.rag.sources import (
    SearchRecord,
    SourceChunk,
    SourceCollector,
    download_url_for,
    record_search,
    resolve_sources,
    source_scope,
    with_download_urls,
)


def chunk(doc_id: str, *, title: str = "Doc", page: int | None = None) -> SourceChunk:
    return SourceChunk(
        document_id=doc_id,
        title=title,
        file_name=f"{doc_id}.pdf",
        file_type="pdf",
        page_number=page,
    )


def record(*chunks: SourceChunk, code: str = "hr") -> SearchRecord:
    return SearchRecord(department_code=code, chunks=list(chunks))


# --------------------------------------------------------------------------- #
# No search at all
# --------------------------------------------------------------------------- #
def test_no_search_yields_none_not_empty_list():
    """None means 'this turn never touched the corpus'. An empty list would
    claim a search happened and found nothing — a different statement."""
    assert resolve_sources([], "Some general answer.") is None


# --------------------------------------------------------------------------- #
# Single search: [N] citations
# --------------------------------------------------------------------------- #
def test_single_search_maps_citations_to_documents():
    rec = record(
        chunk("docA", title="Leave Policy", page=2),
        chunk("docB", title="Pay Policy", page=7),
        chunk("docC", title="IT Policy", page=1),
    )
    sources = resolve_sources([rec], "Per [1] and [3], you get 20 days.")

    assert [s["document_id"] for s in sources] == ["docA", "docC"]
    assert all(s["cited"] is True for s in sources)
    assert sources[0]["title"] == "Leave Policy"
    assert sources[0]["pages"] == [2]


def test_pages_aggregate_and_sort_within_one_document():
    rec = record(
        chunk("docA", page=9),
        chunk("docA", page=2),
        chunk("docA", page=9),  # duplicate page, same doc
    )
    sources = resolve_sources([rec], "See [1], [2] and [3].")

    assert len(sources) == 1, "one entry per document, not per passage"
    assert sources[0]["pages"] == [2, 9]


def test_citation_order_is_first_seen_not_marker_order():
    """Retrieval returns best-first, so the passage order is the ranking. A
    document cited later in the prose should not jump the list."""
    rec = record(chunk("docA"), chunk("docB"))
    sources = resolve_sources([rec], "First [2], then [1].")
    assert [s["document_id"] for s in sources] == ["docB", "docA"]


def test_out_of_range_citations_are_ignored():
    rec = record(chunk("docA"), chunk("docB"))
    sources = resolve_sources([rec], "As shown in [1] and [7].")
    assert [s["document_id"] for s in sources] == ["docA"]


def test_all_citations_out_of_range_falls_back_to_all():
    rec = record(chunk("docA"), chunk("docB"))
    sources = resolve_sources([rec], "See [9].")
    assert [s["document_id"] for s in sources] == ["docA", "docB"]
    assert all(s["cited"] is False for s in sources)


def test_long_bracketed_numbers_are_not_citations():
    """A year in quoted document text must not resolve to a passage."""
    rec = record(chunk("docA"), chunk("docB"))
    sources = resolve_sources([rec], "The [2024] circular says so.")
    # Nothing parseable -> fallback, not a citation of passage 2024.
    assert all(s["cited"] is False for s in sources)


# --------------------------------------------------------------------------- #
# Single search: fallback when the model cites nothing
# --------------------------------------------------------------------------- #
def test_uncited_answer_falls_back_to_every_presented_document():
    rec = record(chunk("docA"), chunk("docB"))
    sources = resolve_sources([rec], "You get 20 days of annual leave.")

    assert [s["document_id"] for s in sources] == ["docA", "docB"]
    assert all(s["cited"] is False for s in sources)


# --------------------------------------------------------------------------- #
# Several searches: [N] is ambiguous
# --------------------------------------------------------------------------- #
def test_multiple_searches_return_all_documents_uncited():
    """Each call restarts numbering at [1], so a marker cannot be attributed.
    Returning everything uncited is honest; guessing would mislabel a link."""
    first = record(chunk("docA"), chunk("docB"))
    second = record(chunk("docC"))
    sources = resolve_sources([first, second], "Per [1], twenty days.")

    assert [s["document_id"] for s in sources] == ["docA", "docB", "docC"]
    assert all(s["cited"] is False for s in sources)


def test_multiple_searches_deduplicate_across_calls():
    first = record(chunk("docA", page=1))
    second = record(chunk("docA", page=4), chunk("docB"))
    sources = resolve_sources([first, second], "no citations here")
    assert [s["document_id"] for s in sources] == ["docA", "docB"]


# --------------------------------------------------------------------------- #
# Collector + contextvar
# --------------------------------------------------------------------------- #
def test_record_search_is_a_noop_without_a_collector():
    """Direct tool use outside a chat turn must not explode."""
    record_search("hr", [chunk("docA")])  # no scope installed


def test_collector_captures_within_scope_and_survives_it():
    collector = SourceCollector()
    with source_scope(collector):
        record_search("hr", [chunk("docA")])
    # Reading AFTER the scope exits is the streaming `finally` case.
    record_search("hr", [chunk("docB")])  # outside: must not be captured

    assert len(collector.records) == 1
    assert collector.records[0].department_code == "hr"
    assert [c.document_id for c in collector.records[0].chunks] == ["docA"]


def test_collector_records_a_search_that_found_nothing():
    # "searched, nothing relevant" and "never searched" are different facts and
    # resolve_sources renders them differently ([] vs null). Dropping the empty
    # record collapsed them, which made abstention indistinguishable from a
    # general chat that never touched the corpus.
    collector = SourceCollector()
    with source_scope(collector):
        record_search("hr", [])
    assert len(collector.records) == 1
    assert collector.records[0].department_code == "hr"
    assert collector.records[0].chunks == []


def test_a_search_that_found_nothing_resolves_to_empty_not_null():
    collector = SourceCollector()
    with source_scope(collector):
        record_search("hr", [])
    assert resolve_sources(collector.records, "I could not find that.") == []


def test_no_search_at_all_still_resolves_to_null():
    assert resolve_sources([], "A general answer.") is None


def test_nested_scopes_restore_the_outer_collector():
    outer, inner = SourceCollector(), SourceCollector()
    with source_scope(outer):
        with source_scope(inner):
            record_search("hr", [chunk("inner")])
        record_search("hr", [chunk("outer")])

    assert [c.document_id for r in inner.records for c in r.chunks] == ["inner"]
    assert [c.document_id for r in outer.records for c in r.chunks] == ["outer"]


# --------------------------------------------------------------------------- #
# download_url is derived, never stored
# --------------------------------------------------------------------------- #
def test_download_url_is_derived_from_code_and_id():
    assert download_url_for("hr", "abc123") == (
        "/v1/departments/hr/documents/abc123/download"
    )


def test_with_download_urls_adds_the_field_without_mutating_input():
    stored = [{"document_id": "abc", "department_code": "hr", "title": "T"}]
    out = with_download_urls(stored)

    assert out[0]["download_url"] == "/v1/departments/hr/documents/abc/download"
    assert "download_url" not in stored[0], "input must not be mutated"


def test_with_download_urls_passes_none_through():
    assert with_download_urls(None) is None


def test_resolved_sources_never_carry_a_download_url():
    """The persisted shape must stay free of the derived field, or a route
    change would leave stale URLs in the database."""
    sources = resolve_sources([record(chunk("docA"))], "[1]")
    assert "download_url" not in sources[0]


@pytest.mark.parametrize("field", ["document_id", "title", "department_code",
                                   "file_name", "file_type", "pages", "cited"])
def test_source_shape_contains_every_published_field(field):
    sources = resolve_sources([record(chunk("docA", title="T", page=3))], "[1]")
    assert field in sources[0]
