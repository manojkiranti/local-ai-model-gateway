"""Offline tests for read_image.

No DB: uses the in-memory fallback file store as the file source (resolve_file
falls back to file_store.get when no PostgresFileSource is installed).

The OCR engine is real where installed and stubbed where the assertion is about
the tool's own behaviour (routing, paging, caveats) rather than about recognition.
"""

from __future__ import annotations

import asyncio
import io

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.files import image_ocr
from app.files.store import PNG_MEDIA_TYPE, XLSX_MEDIA_TYPE, file_store
from app.tools.local import read_image

HAVE_OCR = image_ocr.available()
needs_ocr = pytest.mark.skipif(not HAVE_OCR, reason="rapidocr not installed")

DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


@pytest.fixture(autouse=True)
def _configure_store(tmp_path):
    file_store.configure(str(tmp_path))
    yield


def _save(raw: bytes, filename: str, media_type: str = PNG_MEDIA_TYPE) -> str:
    rec = asyncio.run(file_store.save(raw, filename=filename, media_type=media_type))
    return rec.id


def _read(args) -> str:
    return asyncio.run(read_image.SPEC.func(args))


def _png_bytes(lines=("Total Amount: 45,320.75",), size=34, width=900):
    img = Image.new("RGB", (width, 60 + size * 2 * len(lines)), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(DEJAVU, size)
    y = 25
    for line in lines:
        draw.text((30, y), line, fill=(0, 0, 0), font=font)
        y += size * 2
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _blank_png(size=(300, 200)):
    buf = io.BytesIO()
    Image.new("RGB", size, (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


class _StubResult:
    """Stands in for image_ocr.ocr_image so tool behaviour is testable without
    depending on what the recogniser happens to return."""

    def __init__(self, lines, scores=None):
        self.lines = lines
        self.scores = scores or [1.0] * len(lines)
        self.engine = "rapidocr"
        self.model = "PP-OCRv5"
        self.backend = "onnxruntime"
        self.lang = "devanagari"
        self.version = "rapidocr 3.9.2, onnxruntime 1.23.2"
        self.authoritative = False
        self.detail = {}

    @property
    def mean_score(self):
        return sum(self.scores) / len(self.scores) if self.scores else 0.0

    @property
    def min_score(self):
        return min(self.scores) if self.scores else 0.0


def _stub(monkeypatch, lines, scores=None):
    monkeypatch.setattr(
        read_image.image_ocr, "ocr_image",
        lambda path, lang=None: _StubResult(lines, scores),
    )


# --------------------------------------------------------------------------- #
# Guard rails
# --------------------------------------------------------------------------- #
def test_missing_file_id_errors():
    assert _read({}).startswith("ERROR: 'file_id' is required")


def test_unknown_id_errors_without_distinguishing_foreign_from_missing():
    assert _read({"file_id": "nope"}) == (
        "ERROR: no such file (unknown id, or you don't own it)."
    )


def test_a_spreadsheet_points_at_the_excel_tools():
    fid = _save(b"a,b\n1,2\n", "t.csv", "text/csv; charset=utf-8")
    out = _read({"file_id": fid})
    assert "inspect_excel" in out and out.startswith("ERROR:")


def test_a_document_points_at_read_document():
    """Symmetric to read_document's spreadsheet guard: a PDF handed to the image
    tool must be routed, not fed to an OCR engine that would treat page 1 as a
    picture and silently ignore a perfectly good text layer."""
    fid = _save(b"%PDF-1.4\n", "a.pdf", "application/pdf")
    out = _read({"file_id": fid})
    assert out.startswith("ERROR:") and "read_document" in out


def test_an_unsupported_language_is_refused():
    fid = _save(_png_bytes(), "a.png")
    out = _read({"file_id": fid, "lang": "klingon"})
    assert out.startswith("ERROR:") and "klingon" in out


def test_ocr_unavailable_says_so_plainly(monkeypatch):
    """The optional-build-flag path. Fail CLOSED and SAY it — §18's whole lesson
    is that every way this breaks otherwise looks like a clean success."""
    def _boom(path, lang=None):
        raise image_ocr.OcrUnavailable("rapidocr is not installed.")

    monkeypatch.setattr(read_image.image_ocr, "ocr_image", _boom)
    fid = _save(_png_bytes(), "a.png")
    out = _read({"file_id": fid})
    assert out == "ERROR: image OCR is not enabled on this deployment."


def test_no_text_detected_is_a_distinct_message_not_an_empty_body(monkeypatch):
    """An empty body reads to the model as 'the image is blank' — the same
    reasoning as read_document's scanned-PDF string."""
    _stub(monkeypatch, [])
    fid = _save(_blank_png(), "blank.png")
    out = _read({"file_id": fid})
    assert out == "ERROR: no text was detected in this image."


def test_a_corrupt_image_is_reported_not_crashed():
    fid = _save(b"not an image", "broken.png")
    out = _read({"file_id": fid})
    assert out.startswith("ERROR:")


# --------------------------------------------------------------------------- #
# Output shape — metadata leads, and the caveat is not optional
# --------------------------------------------------------------------------- #
def test_metadata_leads_and_names_the_engine_and_dimensions(monkeypatch):
    _stub(monkeypatch, ["Total Amount: 45,320.75"])
    fid = _save(_png_bytes(), "a.png")
    out = _read({"file_id": fid})
    head = out.splitlines()[0]
    assert "PNG image" in head
    assert "900×128" in head
    assert "1 line" in head


def test_the_engine_and_model_are_stated(monkeypatch):
    _stub(monkeypatch, ["x"])
    fid = _save(_png_bytes(), "a.png")
    out = _read({"file_id": fid})
    assert "PP-OCRv5" in out and "devanagari" in out


def test_every_result_carries_the_machine_read_caveat(monkeypatch):
    """§16.6: OCR output is retrieval text, not a transcription — unreliable on
    latin runs, and it renders २०६९।१।३१ as २०६९।९।३१. It must never be
    presented as authoritative for a figure, a date or an account number, and
    ODIN's own rules forbid quoting unverified figures as fact. The caveat rides
    with the TEXT, not in documentation, because the text is what reaches the
    model."""
    _stub(monkeypatch, ["Account 0123456789"])
    fid = _save(_png_bytes(), "a.png")
    out = _read({"file_id": fid})
    assert "VERIFY" in out
    assert "machine-read" in out.lower()


def test_confidence_is_reported(monkeypatch):
    _stub(monkeypatch, ["good", "iffy"], [0.99, 0.42])
    fid = _save(_png_bytes(), "a.png")
    out = _read({"file_id": fid})
    assert "0.42" in out


def test_the_caveat_survives_the_loops_end_of_result_truncation(monkeypatch):
    """agent/loop.py cuts from the END. A caveat placed after the body would be
    the first thing lost on exactly the long results that most need it."""
    _stub(monkeypatch, [f"line {i}" for i in range(500)])
    fid = _save(_png_bytes(), "a.png")
    out = _read({"file_id": fid})
    head = "\n".join(out.splitlines()[:6])
    assert "VERIFY" in head


# --------------------------------------------------------------------------- #
# Paging
# --------------------------------------------------------------------------- #
def test_line_window_is_honoured(monkeypatch):
    _stub(monkeypatch, [f"line {i}" for i in range(1, 21)])
    fid = _save(_png_bytes(), "a.png")
    out = _read({"file_id": fid, "start_line": 5, "max_lines": 3})
    assert "line 5" in out and "line 7" in out
    assert "line 8" not in out


def test_truncation_announces_the_exact_resume_point(monkeypatch):
    _stub(monkeypatch, [f"line {i}" for i in range(1, 1001)])
    fid = _save(_png_bytes(), "a.png")
    out = _read({"file_id": fid})
    assert f"start_line={read_image.READ_IMAGE_MAX_LINES + 1}" in out


def test_start_line_past_the_end_errors(monkeypatch):
    _stub(monkeypatch, ["only one"])
    fid = _save(_png_bytes(), "a.png")
    out = _read({"file_id": fid, "start_line": 99})
    assert out.startswith("ERROR: start_line=99 is past the end")


def test_the_result_never_exceeds_the_loops_cap(monkeypatch):
    _stub(monkeypatch, ["x" * 200 for _ in range(500)])
    fid = _save(_png_bytes(), "a.png")
    out = _read({"file_id": fid})
    assert len(out) <= read_image.MODEL_RESULT_CAP


def test_the_char_budget_agrees_with_the_agent_loop():
    from app.agent.loop import MAX_TOOL_RESULT_CHARS

    assert read_image.MODEL_RESULT_CAP == MAX_TOOL_RESULT_CHARS
    assert read_image.IMAGE_MAX_CHARS < MAX_TOOL_RESULT_CHARS


# --------------------------------------------------------------------------- #
# Routing — descriptions are the routing prompt
# --------------------------------------------------------------------------- #
def test_read_document_routes_images_to_this_tool():
    fid = _save(_png_bytes(), "a.png")
    from app.tools.local import read_document

    out = asyncio.run(read_document.SPEC.func({"file_id": fid}))
    assert out.startswith("ERROR:") and "read_image" in out


def test_read_documents_description_names_read_image():
    from app.tools.local import read_document

    assert "read_image" in read_document.SPEC.description


def test_this_description_routes_documents_and_spreadsheets_away():
    description = read_image.SPEC.description
    assert "read_document" in description
    assert "read_excel" in description or "inspect_excel" in description


def test_the_tool_is_registered():
    from app.tools.local import LOCAL_TOOLS

    assert read_image.SPEC in LOCAL_TOOLS
    assert len({s.name for s in LOCAL_TOOLS}) == len(LOCAL_TOOLS)


def test_the_attachment_note_tells_the_model_which_tool_reads_an_image():
    """history/repository.format_attachment_note hardcodes the routing hint. An
    image whose note names no tool leaves the model guessing."""
    from app.history.repository import format_attachment_note

    note = format_attachment_note(
        [{"id": "abc", "filename": "scan.png", "summary": "PNG image, 900×128"}]
    )
    assert "read_image" in note


# --------------------------------------------------------------------------- #
# The real engine, end to end through the tool
# --------------------------------------------------------------------------- #
@needs_ocr
def test_english_figures_survive_the_whole_tool_path():
    fid = _save(_png_bytes(("Total Amount: 45,320.75", "Account: 0123456789")), "a.png")
    out = _read({"file_id": fid})
    assert "45,320.75" in out
    assert "0123456789" in out


def test_a_multi_frame_image_says_the_other_frames_were_not_read(monkeypatch):
    """Measured: only frame 1 of a 2-page TIFF is OCR'd. Silently returning it
    as though it were the whole document is the exact failure mode
    read_document's PARTIAL line exists to prevent."""
    _stub(monkeypatch, ["frame one text"])
    from PIL import Image

    buf = io.BytesIO()
    first = Image.new("RGB", (120, 80), (255, 255, 255))
    first.save(buf, format="TIFF", save_all=True,
               append_images=[Image.new("RGB", (120, 80), (10, 10, 10))])
    fid = _save(buf.getvalue(), "scan.tif", "image/tiff")
    out = _read({"file_id": fid})
    assert "PARTIAL" in out
    assert "2 frames" in out
    head = "\n".join(out.splitlines()[:6])
    assert "PARTIAL" in head
