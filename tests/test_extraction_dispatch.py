"""The extraction dispatcher: which engine runs, and what it reports.

No DB, no HTTP, and — deliberately — no OCR stack. The image branch takes an
injectable `ocr` callable so the DISPATCH decision is testable everywhere,
including an environment built without INSTALL_OCR. Whether the real engine
produces good text is `tests/test_image_ocr_eval.py`'s job, not this file's.
"""

import json

import pytest

from app.publicapi import extraction


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_a_txt_file_is_native_and_authoritative(tmp_path):
    path = _write(tmp_path, "a.txt", "first line\nsecond line\n")
    out = extraction.read_any(path)
    assert out.route == extraction.NATIVE_ROUTE
    assert out.authoritative is True
    assert list(out.lines) == ["first line", "second line"]
    assert out.sheets == ()


def test_a_json_file_is_native(tmp_path):
    path = _write(tmp_path, "a.json", json.dumps({"k": "v"}))
    out = extraction.read_any(path)
    assert out.route == extraction.NATIVE_ROUTE
    assert any("k" in line for line in out.lines)


def test_a_csv_returns_sheets_not_lines(tmp_path):
    path = _write(tmp_path, "a.csv", "name,amount\nalice,10\nbob,20\n")
    out = extraction.read_any(path)
    assert out.route == extraction.NATIVE_ROUTE
    assert out.lines == ()
    assert len(out.sheets) == 1
    sheet = out.sheets[0]
    assert list(sheet.headers) == ["name", "amount"]
    assert [list(r) for r in sheet.rows] == [["alice", "10"], ["bob", "20"]]
    assert sheet.total_rows == 2


def test_a_native_pdf_reports_its_pages(tmp_path):
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", size=12)
    pdf.cell(40, 10, "Gross Pay 87500")
    path = tmp_path / "a.pdf"
    pdf.output(str(path))

    out = extraction.read_any(path)
    assert out.route == extraction.NATIVE_ROUTE
    assert out.pages == 1
    assert out.text_pages == 1
    assert out.is_scanned_pdf is False
    assert any("87500" in line for line in out.lines)


def test_a_pdf_with_no_text_layer_is_reported_as_a_FACT_not_an_exception():
    # The dispatcher reports; the ROUTER decides it is a 422. Same seam as
    # documents.py vs the read_document tool.
    out = extraction.ExtractedText(
        kind="PDF", route=extraction.NATIVE_ROUTE, pages=3, text_pages=0
    )
    assert out.is_scanned_pdf is True


def test_an_image_routes_to_ocr_and_is_never_authoritative(tmp_path):
    from PIL import Image

    path = tmp_path / "a.png"
    Image.new("RGB", (30, 12), "white").save(path)

    class _FakeResult:
        lines = ("Account No 1234",)
        scores = (0.87,)

    calls = []

    def _fake_ocr(p, *, lang):
        calls.append((p, lang))
        return _FakeResult()

    out = extraction.read_any(path, ocr=_fake_ocr)
    assert calls and calls[0][1] == "devanagari"
    assert out.route == extraction.OCR_ROUTE
    assert out.authoritative is False
    assert list(out.lines) == ["Account No 1234"]
    assert list(out.line_confidences) == [0.87]


def test_an_unsupported_extension_raises_ReadError(tmp_path):
    from app.files.readers import ReadError

    path = _write(tmp_path, "a.exe", "nope")
    with pytest.raises(ReadError):
        extraction.read_any(path)


def test_the_accepted_extension_set_is_the_union_of_the_three_families():
    from app.files import ingest

    assert extraction.EXTRACT_EXTS == frozenset(
        ingest.SPREADSHEET_EXTS | ingest.DOCUMENT_EXTS | ingest.IMAGE_EXTS
    )
