"""Offline tests for the create_pdf local tool (fpdf2 -> application/pdf).

No network: calls the tool fn directly against a temp-configured file store and
asserts (a) real PDF bytes are produced and stored, (b) validation returns
friendly ERROR strings (never raises), and (c) the result links to a retrievable
PDF file in the store.
"""

import asyncio

import pytest

from app.files.store import PDF_MEDIA_TYPE, file_store
from app.tools.local import pdf


@pytest.fixture(autouse=True)
def _configure_store(tmp_path):
    file_store.configure(str(tmp_path))
    yield


def _run(args):
    # asyncio.run gives each call its own loop — robust when other tests in the
    # suite have already created/closed the default loop.
    return asyncio.run(pdf.SPEC.func(args))


def _link_id(result: str) -> str:
    assert "Download it at: GET /v1/files/" in result, result
    return result.split("/v1/files/")[1].strip().split()[0]


def _stored_bytes(result: str) -> bytes:
    record = file_store.get(_link_id(result))
    assert record is not None
    assert record.media_type == PDF_MEDIA_TYPE
    return open(record.path, "rb").read()


# ---- happy paths: real PDF bytes land in the store ----

def test_title_and_body_renders_pdf():
    result = _run(
        {
            "title": "Quarterly Report",
            "sections": [
                {"heading": "Overview", "body": "Revenue grew this quarter."},
                {"body": "A second section with no heading."},
            ],
        }
    )
    data = _stored_bytes(result)
    assert data.startswith(b"%PDF")  # valid PDF signature
    assert b"%%EOF" in data
    assert file_store.get(_link_id(result)).size > 0


def test_section_with_table_renders_pdf():
    result = _run(
        {
            "title": "With Table",
            "sections": [
                {
                    "heading": "Numbers",
                    "table": {
                        "headers": ["Month", "Sales"],
                        "rows": [["Jan", 10], ["Feb", 20]],
                    },
                }
            ],
        }
    )
    assert _stored_bytes(result).startswith(b"%PDF")


def test_default_filename_is_pdf():
    result = _run({"sections": [{"body": "hi"}]})
    assert file_store.get(_link_id(result)).filename.lower().endswith(".pdf")


def test_filename_forced_to_pdf_extension():
    result = _run({"filename": "report", "sections": [{"body": "hi"}]})
    assert file_store.get(_link_id(result)).filename == "report.pdf"


def test_non_latin1_text_does_not_crash():
    # Core fonts are latin-1 only; unsupported chars are sanitized, not fatal.
    result = _run({"title": "emoji 🚀 and café", "sections": [{"body": "smile 😀 quote “x”"}]})
    assert _stored_bytes(result).startswith(b"%PDF")


# ---- validation: friendly ERROR strings, never exceptions ----

def test_missing_sections():
    assert _run({"title": "x"}).startswith("ERROR")


def test_empty_sections():
    assert _run({"sections": []}).startswith("ERROR")


def test_sections_not_a_list():
    assert _run({"sections": "nope"}).startswith("ERROR")


def test_section_not_an_object():
    assert _run({"sections": ["just a string"]}).startswith("ERROR")


def test_empty_section_object_rejected():
    # A section with no heading, no body, and no table has nothing to render.
    assert _run({"sections": [{}]}).startswith("ERROR")


def test_table_rows_not_a_list():
    r = _run({"sections": [{"table": {"headers": ["A"], "rows": "nope"}}]})
    assert r.startswith("ERROR")


def test_table_row_not_a_list():
    r = _run({"sections": [{"table": {"rows": ["not-a-row"]}}]})
    assert r.startswith("ERROR")


def test_registered_in_local_tools():
    from app.tools.local import LOCAL_TOOLS

    assert any(spec.name == "create_pdf" for spec in LOCAL_TOOLS)
