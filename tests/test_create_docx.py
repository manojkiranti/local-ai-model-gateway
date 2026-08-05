"""Offline tests for the create_docx local tool (python-docx -> .docx).

No network: calls the tool fn directly against a temp-configured fallback file
store and asserts (a) a valid .docx (zip w/ word/document.xml) is produced and
stored, (b) title/heading/body/table text lands in the document, (c) full
Unicode survives (no latin-1 clamping like the PDF tool), and (d) validation
returns friendly ERROR strings.
"""

import asyncio
import zipfile

import pytest

from app.files.store import DOCX_MEDIA_TYPE, file_store
from app.tools.local import docx as docx_tool


@pytest.fixture(autouse=True)
def _configure_store(tmp_path):
    file_store.configure(str(tmp_path))
    yield


def _run(args):
    return asyncio.run(docx_tool.SPEC.func(args))


def _link_id(result: str) -> str:
    assert "Download it at: GET /v1/files/" in result, result
    return result.split("/v1/files/")[1].strip().split()[0]


def _document_xml(result: str) -> str:
    record = file_store.get(_link_id(result))
    assert record is not None
    assert record.media_type == DOCX_MEDIA_TYPE
    with zipfile.ZipFile(record.path) as zf:
        assert "word/document.xml" in zf.namelist()  # it's a real .docx
        return zf.read("word/document.xml").decode("utf-8")


def test_title_body_and_heading_present():
    result = _run(
        {
            "title": "Quarterly Report",
            "sections": [
                {"heading": "Overview", "body": "Revenue grew this quarter."},
                {"body": "A second section with no heading."},
            ],
        }
    )
    xml = _document_xml(result)
    assert "Quarterly Report" in xml
    assert "Overview" in xml
    assert "Revenue grew this quarter." in xml


def test_section_with_table_present():
    result = _run(
        {
            "sections": [
                {
                    "heading": "Numbers",
                    "table": {"headers": ["Month", "Sales"], "rows": [["Jan", 10], ["Feb", 20]]},
                }
            ]
        }
    )
    xml = _document_xml(result)
    assert "Month" in xml and "Sales" in xml and "Jan" in xml and "20" in xml


def test_full_unicode_preserved():
    # Unlike the PDF core font, python-docx keeps emoji/accented text as-is.
    result = _run({"title": "Café 🚀 “quotes”", "sections": [{"body": "smile 😀 café"}]})
    xml = _document_xml(result)
    assert "Café" in xml and "🚀" in xml


def test_default_filename_is_docx():
    result = _run({"sections": [{"body": "hi"}]})
    assert file_store.get(_link_id(result)).filename == "document.docx"


def test_filename_forced_to_docx_extension():
    result = _run({"filename": "report", "sections": [{"body": "hi"}]})
    assert file_store.get(_link_id(result)).filename == "report.docx"


# ---- validation: friendly ERROR strings ----

def test_missing_sections():
    assert _run({"title": "x"}).startswith("ERROR")


def test_empty_sections():
    assert _run({"sections": []}).startswith("ERROR")


def test_sections_not_a_list():
    assert _run({"sections": "nope"}).startswith("ERROR")


def test_section_not_an_object():
    assert _run({"sections": ["just a string"]}).startswith("ERROR")


def test_empty_section_object_rejected():
    assert _run({"sections": [{}]}).startswith("ERROR")


def test_table_rows_not_a_list():
    assert _run({"sections": [{"table": {"headers": ["A"], "rows": "nope"}}]}).startswith("ERROR")


def test_table_row_not_a_list():
    assert _run({"sections": [{"table": {"rows": ["not-a-row"]}}]}).startswith("ERROR")


def test_registered_in_local_tools():
    from app.tools.local import LOCAL_TOOLS

    assert any(spec.name == "create_docx" for spec in LOCAL_TOOLS)
