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


def test_overlong_single_line_is_hard_cut_and_continuation_is_exact():
    """A single line longer than the whole char budget must still let the
    reader make progress: emitted alone, hard-cut, and the reported next
    start_line must be exactly the line after it — no skip, no repeat.

    The header must ALSO name the hard-cut inline (Finding 3): a bare
    "…[long line truncated]" suffix at the very end of the body is exactly the
    position `agent/loop.py`'s own end-of-result cut can eat, and the design's
    whole premise is that metadata leading the result is the only trustworthy
    signal of truncation.
    """
    long_line = "y" * (read_document.DOC_MAX_CHARS + 500)
    body = f"{long_line}\nnext line"
    fid = _save(body.encode(), "long.txt")

    out = _read({"file_id": fid})
    assert len(out) <= read_document.MODEL_RESULT_CAP

    lines = out.splitlines()
    header, hard_cut_note, note = lines[0], lines[1], lines[2]
    body_lines = [ln for ln in lines[3:] if ln]
    assert header == "Text file, 2 lines — showing lines 1–1 of 2."
    assert hard_cut_note == (
        f"NOTE: line 1 is {len(long_line)} characters, longer than the "
        f"{read_document.DOC_MAX_CHARS}-character read budget — it was "
        f"hard-cut, and the rest of that line is NOT retrievable by paging."
    )
    assert note == "TRUNCATED: call read_document again with start_line=2 to continue."
    # the over-long line is emitted alone, hard-cut with the truncation suffix
    assert len(body_lines) == 1
    assert body_lines[0] == long_line[: read_document.DOC_MAX_CHARS] + " …[long line truncated]"

    # continuation from the reported start_line neither skips nor repeats:
    # "next line" is the first (and only) thing in the second window.
    marker = "start_line="
    next_start = int(note[note.index(marker) + len(marker) :].split(" ", 1)[0])
    assert next_start == 2
    second = _read({"file_id": fid, "start_line": next_start})
    assert second == "Text file, 2 lines — showing lines 2–2 of 2.\n\nnext line"


def test_single_overlong_line_announces_hard_cut_even_when_not_truncated():
    """The exact repro from the finding: a document that is ONE line, longer
    than the whole char budget. `truncated` is False (there is no next line to
    resume at — last(1) < total(1) is False), so before this fix the header
    read as a complete 1-line document while ~42k characters were silently
    dropped, with the only signal being the trailing inline suffix at the very
    END of the body — the position the design says is untrustworthy. Also
    exercises the singular "1 line" fix (Finding 4)."""
    long_line = "x" * 50_000
    fid = _save(long_line.encode(), "huge.txt")

    out = _read({"file_id": fid})
    lines = out.splitlines()
    assert lines[0] == "Text file, 1 line — showing lines 1–1 of 1."
    assert "TRUNCATED" not in lines[1]  # nothing to resume — this was the only line
    assert lines[1] == (
        f"NOTE: line 1 is {len(long_line)} characters, longer than the "
        f"{read_document.DOC_MAX_CHARS}-character read budget — it was "
        f"hard-cut, and the rest of that line is NOT retrievable by paging."
    )


def _verbose_then_scanned_pdf_over_page_cap(tmp_path) -> bytes:
    """4-page PDF built to force all four possible _header lines at once:
    page 1 alone is verbose enough to blow the char budget (-> TRUNCATED),
    page 2 is image-only with no text (-> the scanned-pages count), and
    pages 3-4 exist purely to be skipped once MAX_PDF_PAGES is monkeypatched
    down to 2 (-> PARTIAL).
    """
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos
    from PIL import Image

    img = tmp_path / "cap.png"
    Image.new("RGB", (64, 64), (180, 180, 180)).save(img)

    pdf = FPDF()
    # A custom, very tall page for page 1 so ~250 lines of filler fit on ONE
    # page without fpdf2 auto-paginating (which would break the page count
    # this test relies on).
    pdf.add_page(format=(210, 3000))
    pdf.set_font("Helvetica", size=12)
    for i in range(250):
        pdf.multi_cell(
            0, 5, f"line of filler text number {i:04d} " * 3,
            new_x=XPos.LMARGIN, new_y=YPos.NEXT,
        )

    pdf.add_page()  # page 2: image only, no extractable text
    pdf.image(str(img), x=10, y=10, w=50)

    pdf.add_page()  # page 3: beyond the page cap, never read
    pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(0, 10, "page three")
    pdf.add_page()  # page 4: beyond the page cap, never read
    pdf.multi_cell(0, 10, "page four")

    return bytes(pdf.output())


def test_header_carries_all_four_lines_at_once_and_stays_under_the_cap(tmp_path, monkeypatch):
    """Worst case for HEADER_BUDGET: the main head, TRUNCATED, PARTIAL (page
    cap), and the scanned-pages count all fire on the same read. By hand the
    margin holds; this proves it rather than asserting it by design."""
    from app.files import documents

    monkeypatch.setattr(documents, "MAX_PDF_PAGES", 2)
    fid = _save(
        _verbose_then_scanned_pdf_over_page_cap(tmp_path), "big.pdf", PDF_MEDIA_TYPE
    )

    out = _read({"file_id": fid})
    assert len(out) <= read_document.MODEL_RESULT_CAP

    lines = out.splitlines()
    assert lines[0] == "PDF, 4 pages, 252 lines — showing lines 1–80 of 252."
    assert lines[1] == "TRUNCATED: call read_document again with start_line=81 to continue."
    assert lines[2] == "PARTIAL: pages 3–4 were not read (limit 2 pages)."
    assert lines[3] == "1 of 2 pages have no extractable text (likely scanned images)."
