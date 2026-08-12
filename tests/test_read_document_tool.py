"""Offline tests for read_document.

No DB: uses the in-memory fallback file store as the file source (resolve_file
falls back to file_store.get when no PostgresFileSource is installed). Writes a
real file to disk through the store, then drives the tool fn directly.
"""

from __future__ import annotations

import asyncio

import pytest

from app.files.store import PDF_MEDIA_TYPE, XLSX_MEDIA_TYPE, file_store
from app.tools.local import read_document


@pytest.fixture(autouse=True)
def _configure_store(tmp_path):
    file_store.configure(str(tmp_path))
    yield


def _save(raw: bytes, filename: str, media_type: str = "text/plain; charset=utf-8") -> str:
    rec = asyncio.run(file_store.save(raw, filename=filename, media_type=media_type))
    return rec.id


def _read(args) -> str:
    return asyncio.run(read_document.SPEC.func(args))


def _text_pdf_bytes(pages: list[str]) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    for body in pages:
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf.multi_cell(0, 10, body)
    return bytes(pdf.output())


def _image_only_pdf_bytes(tmp_path, n_pages: int) -> bytes:
    from fpdf import FPDF
    from PIL import Image

    img = tmp_path / "block.png"
    Image.new("RGB", (64, 64), (180, 180, 180)).save(img)
    pdf = FPDF()
    for _ in range(n_pages):
        pdf.add_page()
        pdf.image(str(img), x=10, y=10, w=50)
    return bytes(pdf.output())


# --------------------------------------------------------------------------- #
# Guard rails
# --------------------------------------------------------------------------- #
def test_missing_file_id_errors():
    assert _read({}).startswith("ERROR: 'file_id' is required")


def test_unknown_id_errors_without_distinguishing_foreign_from_missing():
    assert _read({"file_id": "nope"}) == (
        "ERROR: no such file (unknown id, or you don't own it)."
    )


def test_spreadsheet_id_points_at_the_excel_tools():
    fid = _save(b"a,b\n1,2\n", "book.xlsx", XLSX_MEDIA_TYPE)
    assert _read({"file_id": fid}) == (
        "ERROR: this is a spreadsheet — use inspect_excel / read_excel instead."
    )


def test_fully_scanned_pdf_returns_the_ocr_error(tmp_path):
    fid = _save(_image_only_pdf_bytes(tmp_path, 3), "scan.pdf", PDF_MEDIA_TYPE)
    assert _read({"file_id": fid}) == (
        "ERROR: this PDF appears to contain scanned images with no text layer "
        "— OCR is not available yet."
    )


def test_password_protected_pdf_has_its_own_error():
    from io import BytesIO

    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(BytesIO(_text_pdf_bytes(["secret"])))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt("hunter2")
    buf = BytesIO()
    writer.write(buf)
    fid = _save(buf.getvalue(), "locked.pdf", PDF_MEDIA_TYPE)
    assert _read({"file_id": fid}) == (
        "ERROR: this PDF is password-protected — it cannot be read."
    )


def test_corrupt_pdf_reports_a_read_error():
    fid = _save(b"%PDF-1.4\ngarbage", "broken.pdf", PDF_MEDIA_TYPE)
    assert _read({"file_id": fid}).startswith("ERROR: could not read the document")


# --------------------------------------------------------------------------- #
# Output shape
# --------------------------------------------------------------------------- #
def test_metadata_is_the_first_line():
    fid = _save("alpha\nbeta\ngamma".encode(), "a.txt")
    out = _read({"file_id": fid})
    assert out.splitlines()[0] == "Text file, 3 lines — showing lines 1–3 of 3."


def test_pdf_header_names_pages_and_body_carries_markers():
    fid = _save(_text_pdf_bytes(["Alpha", "Beta"]), "a.pdf", PDF_MEDIA_TYPE)
    out = _read({"file_id": fid})
    assert out.splitlines()[0].startswith("PDF, 2 pages, ")
    assert "[page 1]" in out
    assert "[page 2]" in out


def test_partially_scanned_pdf_counts_the_empty_pages_in_the_header(tmp_path):
    from fpdf import FPDF
    from PIL import Image

    img = tmp_path / "b.png"
    Image.new("RGB", (64, 64), (180, 180, 180)).save(img)
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(0, 10, "Readable")
    pdf.add_page()
    pdf.image(str(img), x=10, y=10, w=50)
    fid = _save(bytes(pdf.output()), "mixed.pdf", PDF_MEDIA_TYPE)

    out = _read({"file_id": fid})
    assert "1 of 2 pages have no extractable text (likely scanned images)." in out
    assert "[page 2] (no extractable text — likely a scanned image)" in out


def test_start_line_past_the_end_says_how_long_the_document_is():
    fid = _save(b"one\ntwo", "a.txt")
    assert _read({"file_id": fid, "start_line": 99}) == (
        "ERROR: start_line=99 is past the end — this Text file has 2 lines."
    )


# --------------------------------------------------------------------------- #
# Paging + truncation (the correctness core)
# --------------------------------------------------------------------------- #
def test_line_window_is_honoured():
    body = "\n".join(f"line {i}" for i in range(1, 21))
    fid = _save(body.encode(), "a.txt")
    out = _read({"file_id": fid, "start_line": 5, "max_lines": 3})
    assert out.splitlines()[0] == "Text file, 20 lines — showing lines 5–7 of 20."
    assert "line 5" in out and "line 7" in out and "line 8" not in out


def test_truncation_note_names_the_exact_next_start_line():
    body = "\n".join(f"line {i}" for i in range(1, 21))
    fid = _save(body.encode(), "a.txt")
    out = _read({"file_id": fid, "max_lines": 4})
    assert "TRUNCATED: call read_document again with start_line=5 to continue." in out


def test_char_budget_truncates_on_whole_lines_and_reports_truthfully():
    """The header's start_line must equal the first line NOT delivered."""
    body = "\n".join("x" * 200 for _ in range(200))  # 40k chars, way over budget
    fid = _save(body.encode(), "big.txt")
    out = _read({"file_id": fid})

    assert len(out) <= read_document.MODEL_RESULT_CAP
    lines = out.splitlines()
    header, body_lines = lines[0], [ln for ln in lines[2:] if ln]
    assert "TRUNCATED" in lines[1]
    # every delivered line is COMPLETE, never a fragment
    assert all(ln == "x" * 200 for ln in body_lines)
    # and the promised continuation point is exactly one past what we delivered
    assert f"start_line={len(body_lines) + 1} to continue." in lines[1]
    assert f"showing lines 1–{len(body_lines)} of 200." in header


def test_paging_from_the_reported_start_line_loses_nothing():
    body = "\n".join(f"line {i}" for i in range(1, 31))
    fid = _save(body.encode(), "a.txt")
    first = _read({"file_id": fid, "max_lines": 10})
    second = _read({"file_id": fid, "start_line": 11, "max_lines": 10})
    assert "line 10" in first and "line 11" not in first
    assert "line 11" in second


def test_our_budget_stays_under_the_agent_loops_cap():
    """A regression lock: if agent.loop lowers its cap, this test fails loudly
    rather than the tool silently over-promising in its header."""
    from app.agent.loop import MAX_TOOL_RESULT_CHARS

    assert read_document.MODEL_RESULT_CAP == MAX_TOOL_RESULT_CHARS
    assert read_document.DOC_MAX_CHARS < MAX_TOOL_RESULT_CHARS
