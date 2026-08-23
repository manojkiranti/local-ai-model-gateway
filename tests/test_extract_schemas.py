"""The extract response envelope.

The rule under test is the one that differs from /v1/ocr: a NATIVE source has
an exact text layer, so it carries no caveat at all — the key is absent, not
null. Over-warning trains a reader to ignore the warning, which costs you the
warning on the page that needed it (docs/nrb-integration.md §29.2).
"""

from app.files import image_ocr
from app.publicapi import extraction
from app.publicapi.extract_schemas import build_extract_response


def _native():
    return extraction.ExtractedText(
        kind="DOCX", route=extraction.NATIVE_ROUTE, lines=("hello", "world")
    )


def _ocrd():
    return extraction.ExtractedText(
        kind="PNG image",
        route=extraction.OCR_ROUTE,
        lines=("Account No 1234",),
        line_confidences=(0.87,),
    )


def test_a_native_source_omits_the_caveat_KEY_entirely():
    dumped = build_extract_response(_native(), "req1").model_dump()
    assert dumped["source"]["authoritative"] is True
    assert "caveat" not in dumped["source"]


def test_an_ocr_source_carries_the_caveat_and_is_not_authoritative():
    dumped = build_extract_response(_ocrd(), "req2").model_dump()
    assert dumped["source"]["authoritative"] is False
    assert dumped["source"]["caveat"] == image_ocr.OCR_CAVEAT


def test_text_is_the_lines_joined_and_lines_carry_confidence():
    resp = build_extract_response(_ocrd(), "req3")
    assert resp.text == "Account No 1234"
    assert resp.lines[0].confidence == 0.87


def test_a_native_line_has_no_confidence_because_there_is_nothing_uncertain():
    resp = build_extract_response(_native(), "req4")
    assert resp.text == "hello\nworld"
    assert all(line.confidence is None for line in resp.lines)


def test_a_spreadsheet_serialises_sheets_and_an_empty_text():
    extracted = extraction.ExtractedText(
        kind="CSV",
        route=extraction.NATIVE_ROUTE,
        sheets=(
            extraction.Sheet(
                name="Sheet1",
                headers=("name", "amount"),
                rows=(("alice", "10"),),
                total_rows=1,
                truncated=False,
            ),
        ),
    )
    resp = build_extract_response(extracted, "req5")
    assert resp.text == ""
    assert resp.lines == []
    assert resp.sheets[0].headers == ["name", "amount"]
    assert resp.sheets[0].rows == [["alice", "10"]]


def test_page_facts_survive_and_null_pages_are_not_dropped():
    extracted = extraction.ExtractedText(
        kind="PDF",
        route=extraction.NATIVE_ROUTE,
        lines=("x",),
        pages=12,
        text_pages=11,
        pages_skipped=0,
    )
    dumped = build_extract_response(extracted, "req6").model_dump()
    assert dumped["source"]["pages"] == 12
    assert dumped["source"]["text_pages"] == 11
    # Only `caveat` is ever dropped. A genuinely-null page count stays null,
    # because "not a paged format" is a fact worth transmitting.
    csv_dump = build_extract_response(
        extraction.ExtractedText(kind="CSV", route=extraction.NATIVE_ROUTE), "r"
    ).model_dump()
    assert "pages" in csv_dump["source"] and csv_dump["source"]["pages"] is None
