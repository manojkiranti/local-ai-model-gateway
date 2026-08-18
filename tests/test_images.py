"""app/files/images.py — the pure image normalizer.

Same contract as documents.py: reports FACTS, raises only ReadError, and its
messages never embed the on-disk path or the numeric user id (documents.py's
_decode carries the long comment about that leak).

The load-bearing rule here is that summarize_image NEVER OCRs:
history/service._resolve_attachments re-summarizes every attached file on every
turn, so an OCR pass in the summary would be paid per turn.
"""

from __future__ import annotations

import pytest
from PIL import Image

from app.files import images
from app.files.readers import ReadError


def _png(path, size=(120, 80), colour=(200, 200, 200)):
    Image.new("RGB", size, colour).save(path)
    return path


def test_image_exts_cover_the_formats_the_upload_route_accepts():
    assert images.IMAGE_EXTS == {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}


def test_summarize_reports_format_and_dimensions(tmp_path):
    summary = images.summarize_image(_png(tmp_path / "a.png", (1240, 800)))
    assert summary.kind == "PNG image"
    assert summary.width == 1240
    assert summary.height == 800


def test_summary_text_is_the_one_line_attachment_note_detail(tmp_path):
    summary = images.summarize_image(_png(tmp_path / "a.png", (1240, 800)))
    assert summary.text() == "PNG image, 1240×800"


def test_summary_as_dict_carries_the_upload_response_fields(tmp_path):
    d = images.summarize_image(_png(tmp_path / "a.png", (120, 80))).as_dict()
    assert d == {"kind": "PNG image", "width": 120, "height": 80, "frames": 1}


def test_jpeg_is_reported_as_jpeg(tmp_path):
    p = tmp_path / "a.jpg"
    Image.new("RGB", (10, 10), (1, 2, 3)).save(p)
    assert images.summarize_image(p).kind == "JPEG image"


def test_a_file_that_is_not_an_image_raises_read_error(tmp_path):
    p = tmp_path / "fake.png"
    p.write_bytes(b"this is not a PNG")
    with pytest.raises(ReadError):
        images.summarize_image(p)


def test_read_error_never_leaks_the_path_or_the_user_id(tmp_path):
    """documents.py:46-63 records a real leak: a reader's message exposed
    /…/files/3/{uuid}.txt — the storage path AND the numeric user id — into
    model context. Every reader in this package repeats that guard."""
    user_dir = tmp_path / "generated_files" / "3"
    user_dir.mkdir(parents=True)
    p = user_dir / "deadbeef.png"
    p.write_bytes(b"not an image at all")
    with pytest.raises(ReadError) as exc:
        images.summarize_image(p)
    message = str(exc.value)
    assert "deadbeef" not in message
    assert str(p) not in message
    assert "/3/" not in message


def test_a_missing_file_raises_read_error_not_oserror(tmp_path):
    with pytest.raises(ReadError):
        images.summarize_image(tmp_path / "nope.png")


def test_a_decompression_bomb_is_refused(tmp_path, monkeypatch):
    """router.py's zip-bomb guard covers only the OOXML paths, so a small PNG
    that DECODES to an enormous bitmap reaches the summariser untouched. Pillow
    raises DecompressionBombError above MAX_IMAGE_PIXELS; that must surface as
    a ReadError so the upload route turns it into a 400, not a 500."""
    monkeypatch.setattr(images, "MAX_IMAGE_PIXELS", 100)
    p = _png(tmp_path / "big.png", (200, 200))  # 40_000 pixels > 100
    with pytest.raises(ReadError) as exc:
        images.summarize_image(p)
    assert "too large" in str(exc.value)


def test_the_pixel_cap_is_enforced_even_when_pillow_would_only_warn(tmp_path, monkeypatch):
    """Pillow only RAISES above 2x its limit; between 1x and 2x it emits a
    warning and decodes anyway. A guard that relied on the exception alone
    would let a 1.5x bomb through."""
    monkeypatch.setattr(images, "MAX_IMAGE_PIXELS", 30_000)
    p = _png(tmp_path / "mid.png", (200, 200))  # 40_000 = 1.33x the cap
    with pytest.raises(ReadError):
        images.summarize_image(p)


def test_summarize_image_does_not_ocr(tmp_path):
    """The per-turn cost rule. If images.py ever imports the OCR engine, this
    fails — checked by AST so it cannot be defeated by an import inside a
    function (which is exactly how ocr.py hides rapidocr)."""
    import ast
    import pathlib

    source = pathlib.Path(images.__file__).read_text()
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
            names += [f"{node.module or ''}.{a.name}" for a in node.names]
    assert not [n for n in names if "image_ocr" in n or "rapidocr" in n], names


# --------------------------------------------------------------------------- #
# ingest.py — the extension -> family dispatch, the single plug-in point
# --------------------------------------------------------------------------- #
def test_ingest_routes_image_extensions_to_the_image_family(tmp_path):
    from app.files import ingest

    summary = ingest.summarize(_png(tmp_path / "a.png", (30, 20)))
    assert summary.text() == "PNG image, 30×20"


def test_every_image_ext_is_in_the_upload_allowlist():
    from app.files import ingest

    for ext in images.IMAGE_EXTS:
        assert ext in ingest.UPLOAD_TYPES, ext
        assert ingest.UPLOAD_TYPES[ext].startswith("image/"), ext


def test_the_three_families_do_not_overlap():
    from app.files import ingest

    assert not ingest.IMAGE_EXTS & ingest.SPREADSHEET_EXTS
    assert not ingest.IMAGE_EXTS & ingest.DOCUMENT_EXTS


def test_svg_is_not_an_accepted_upload():
    """store.SVG_MEDIA_TYPE exists for create_chart OUTPUT. Accepting SVG as an
    upload would take active markup (script, external refs) on the read path."""
    from app.files import ingest

    assert ".svg" not in ingest.UPLOAD_TYPES
    assert ".svg" not in images.IMAGE_EXTS


def test_a_format_outside_the_decoder_allowlist_is_refused(tmp_path):
    """Pillow opens far more formats than this route accepts. Anything not in
    _KINDS is refused on its SNIFFED format, so a GIF renamed .png does not
    reach the GIF decoder."""
    p = tmp_path / "a.png"
    Image.new("RGB", (10, 10), (1, 2, 3)).save(p, format="GIF")
    with pytest.raises(ReadError) as exc:
        images.summarize_image(p)
    assert "unsupported image format" in str(exc.value)
    assert "GIF" in str(exc.value)


# --------------------------------------------------------------------------- #
# Multi-frame images — a scanned .tif is routinely multi-page
# --------------------------------------------------------------------------- #
def _multiframe_tiff(path, frames=2):
    first = Image.new("RGB", (120, 80), (255, 255, 255))
    rest = [Image.new("RGB", (120, 80), (200, 200, 200)) for _ in range(frames - 1)]
    first.save(path, save_all=True, append_images=rest)
    return path


def test_a_single_frame_image_reports_one_frame(tmp_path):
    assert images.summarize_image(_png(tmp_path / "a.png")).frames == 1


def test_a_multi_frame_tiff_reports_its_frame_count(tmp_path):
    """Measured: rapidocr reads ONLY the first frame of a 2-page TIFF — the
    second page's text vanished with no warning. A multi-page TIFF is the normal
    output of a document scanner, so the count has to be a reported FACT."""
    summary = images.summarize_image(_multiframe_tiff(tmp_path / "scan.tif", 3))
    assert summary.frames == 3


def test_the_summary_text_announces_that_only_the_first_frame_is_read(tmp_path):
    summary = images.summarize_image(_multiframe_tiff(tmp_path / "scan.tif", 3))
    assert summary.text() == "TIFF image, 120×80, 3 frames (only the first is read)"


def test_a_single_frame_summary_says_nothing_about_frames(tmp_path):
    assert "frame" not in images.summarize_image(_png(tmp_path / "a.png")).text()
