"""An NRB citation must carry its extraction route, and say so when the text was
machine-recovered.

The route is the only thing that tells a reader whether a figure came from a
trustworthy text layer, from PP-OCRv5 (explicitly `authoritative: false`, §16.6)
or from a legacy-font conversion that no Nepali reader has checked (§15). Native
NRB text gets the route WITHOUT a caveat — over-warning on trustworthy text
trains a reader to ignore the warning (§29.2).

The same facts already reach the MODEL through the tool's citation header. These
tests are about the structured payload a UI renders, and the point of the shared
constant is that the two can never describe one passage differently.
"""

from app.rag.sources import (
    RECOVERED_ROUTES,
    VERIFY_NOTE,
    SearchRecord,
    SourceChunk,
    resolve_sources,
)


def nrb_chunk(route, *, page=1, authoritative=None, document_id="nrb1"):
    return SourceChunk(
        document_id=document_id,
        title="Unified Directive 2081",
        file_name="directive.pdf",
        file_type="pdf",
        page_number=page,
        origin="nrb",
        route=route,
        authoritative=authoritative,
        source_url="https://www.nrb.org.np/circular/directive-2081/",
        published_at="2024-05-02",
    )


def upload_chunk(document_id="u1", page=3):
    return SourceChunk(
        document_id=document_id,
        title="Leave Policy",
        file_name="leave.pdf",
        file_type="pdf",
        page_number=page,
        origin="upload",
    )


def one(chunks, answer="see [1]"):
    sources = resolve_sources(
        [SearchRecord(department_code="nrb", chunks=chunks)], answer
    )
    assert sources is not None
    return sources


def test_ocr_page_is_flagged_machine_recovered_with_the_verify_note():
    source = one([nrb_chunk("ocr", authoritative=False)])[0]
    assert source["origin"] == "nrb"
    assert source["routes"] == ["ocr"]
    assert source["machine_recovered"] is True
    assert source["verify_note"] == VERIFY_NOTE
    assert source["source_url"] == "https://www.nrb.org.np/circular/directive-2081/"
    assert source["published_at"] == "2024-05-02"


def test_legacy_conversion_is_also_machine_recovered():
    source = one([nrb_chunk("legacy_conversion")])[0]
    assert source["machine_recovered"] is True
    assert source["verify_note"] == VERIFY_NOTE


def test_native_nrb_text_carries_the_route_without_the_caveat():
    source = one([nrb_chunk("native")])[0]
    assert source["routes"] == ["native"]
    assert source["machine_recovered"] is False
    assert source["verify_note"] is None


def test_routes_are_the_union_over_the_documents_presented_pages():
    """One NRB PDF is routed per PAGE (§16), so a single document really can mix
    native text with a converted or OCR'd page. Reporting only the first would
    hide the recovered one, which is the page a reader must verify."""
    source = one(
        [nrb_chunk("native", page=1), nrb_chunk("ocr", page=2, authoritative=False)],
        answer="see [1] and [2]",
    )[0]
    assert source["pages"] == [1, 2]
    assert source["routes"] == ["native", "ocr"]
    assert source["machine_recovered"] is True


def test_a_generic_upload_carries_no_nrb_keys():
    """Absent, not null: a client can tell "not an NRB document" from "NRB, route
    unknown"."""
    source = one([upload_chunk()])[0]
    assert source["origin"] == "upload"
    for absent in ("source_url", "published_at", "routes", "machine_recovered",
                   "verify_note"):
        assert absent not in source


def test_a_mixed_turn_produces_both_shapes():
    sources = one(
        [nrb_chunk("ocr", authoritative=False), upload_chunk()],
        answer="[1] and [2]",
    )
    by_id = {s["document_id"]: s for s in sources}
    assert by_id["nrb1"]["machine_recovered"] is True
    assert "machine_recovered" not in by_id["u1"]


def test_authoritative_false_alone_is_enough_to_flag_it():
    """The trust flag is not derived from the route name alone — a route we have
    not enumerated must still be caveated if the chunk says it is not
    authoritative."""
    source = one([nrb_chunk("some_future_route", authoritative=False)])[0]
    assert source["machine_recovered"] is True
    assert source["verify_note"] == VERIFY_NOTE


def test_recovered_routes_is_the_set_the_tool_also_uses():
    assert RECOVERED_ROUTES == frozenset({"ocr", "legacy_conversion"})
