"""Citation resolution eval — 10 labelled turns.

Success metric: every RAG-grounded answer returns at least one source, and NO
source names a document the model was not shown. The second half is the one that
matters — fabricated provenance is worse than absent provenance, because a link
makes an answer look checked.

Deliberately pure: no Postgres, no model server, no network. It runs anywhere and
it is the standing regression net for the resolution rules in `app/rag/sources.py`.
"""

import pytest

from app.rag.sources import SearchRecord, SourceChunk, resolve_sources


def chunk(doc_id, page=1, **kw):
    return SourceChunk(
        document_id=doc_id,
        title=f"Doc {doc_id}",
        file_name=f"{doc_id}.pdf",
        file_type="pdf",
        page_number=page,
        origin=kw.pop("origin", "upload"),
        **kw,
    )


NRB = dict(
    origin="nrb",
    source_url="https://www.nrb.org.np/x/",
    published_at="2024-01-01",
)

# (name, records, answer, expected document ids in order, expected `cited`)
CASES = [
    (
        "single search, one marker",
        [SearchRecord("hr", [chunk("a"), chunk("b")])],
        "per [1]",
        ["a"],
        True,
    ),
    (
        "single search, two markers",
        [SearchRecord("hr", [chunk("a"), chunk("b")])],
        "[1] and [2]",
        ["a", "b"],
        True,
    ),
    (
        "single search, no markers — grounded but unmarked",
        [SearchRecord("hr", [chunk("a"), chunk("b")])],
        "no citation at all",
        ["a", "b"],
        False,
    ),
    (
        "out-of-range marker is dropped, not an error",
        [SearchRecord("hr", [chunk("a")])],
        "see [9]",
        ["a"],
        False,
    ),
    (
        "a year in the prose is not a citation",
        [SearchRecord("hr", [chunk("a")])],
        "in [2024] the policy changed",
        ["a"],
        False,
    ),
    (
        "two searches: numbering restarts, so nothing is claimed as cited",
        [SearchRecord("hr", [chunk("a")]), SearchRecord("hr", [chunk("b")])],
        "[1]",
        ["a", "b"],
        False,
    ),
    (
        "one document on two pages collapses to one source",
        [SearchRecord("hr", [chunk("a", page=2), chunk("a", page=5)])],
        "[1] and [2]",
        ["a"],
        True,
    ),
    (
        "nrb ocr page",
        [SearchRecord("nrb", [chunk("n1", route="ocr", authoritative=False, **NRB)])],
        "[1]",
        ["n1"],
        True,
    ),
    (
        "nrb legacy conversion",
        [SearchRecord("nrb", [chunk("n2", route="legacy_conversion", **NRB)])],
        "[1]",
        ["n2"],
        True,
    ),
    (
        "nrb native text",
        [SearchRecord("nrb", [chunk("n3", route="native", **NRB)])],
        "[1]",
        ["n3"],
        True,
    ),
]


@pytest.mark.parametrize(
    "name,records,answer,expected_ids,expected_cited",
    CASES,
    ids=[c[0] for c in CASES],
)
def test_case(name, records, answer, expected_ids, expected_cited):
    sources = resolve_sources(records, answer)
    assert sources is not None, "a grounded turn must return at least one source"
    assert [s["document_id"] for s in sources] == expected_ids
    assert all(s["cited"] is expected_cited for s in sources)


def test_a_turn_with_no_search_has_no_sources():
    """None, not []. "Searched nothing" and "searched and found nothing" are
    different facts, and only one of them should render an empty Sources panel."""
    assert resolve_sources([], "hello") is None


def test_no_source_is_ever_invented():
    """The anti-fabrication invariant, swept over the whole eval set."""
    for _name, records, answer, _ids, _cited in CASES:
        presented = {c.document_id for r in records for c in r.chunks}
        for source in resolve_sources(records, answer) or []:
            assert source["document_id"] in presented


def test_pages_are_ascending_and_deduplicated():
    """All three passages are cited, so all three pages contribute — and the same
    page cited twice is one entry. (Citing only [1] would correctly yield [5]
    alone: resolution maps markers to PASSAGES, not to documents.)"""
    records = [
        SearchRecord("hr", [chunk("a", page=5), chunk("a", page=2), chunk("a", page=5)])
    ]
    sources = resolve_sources(records, "[1], [2] and [3]") or []
    assert sources[0]["pages"] == [2, 5]


@pytest.mark.parametrize(
    "route,recovered", [("ocr", True), ("legacy_conversion", True), ("native", False)]
)
def test_machine_recovered_matches_the_route(route, recovered):
    kw = dict(NRB)
    records = [SearchRecord("nrb", [chunk("n", route=route, **kw)])]
    source = (resolve_sources(records, "[1]") or [])[0]
    assert source["machine_recovered"] is recovered
    assert (source["verify_note"] is not None) is recovered
