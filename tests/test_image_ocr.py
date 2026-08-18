"""app/files/image_ocr.py — the ONLY module that knows rapidocr exists.

Same discipline as app/nrb/ocr.py: module constants for the configuration, a
lazy import, and OcrUnavailable raised rather than an empty string returned (a
silent empty result is indistinguishable from a blank image).

The pure parts (params, line grouping) are tested WITHOUT running OCR, so they
stay covered in an environment where rapidocr is not installed.
"""

from __future__ import annotations

import sys

import numpy as np
import pytest

from app.files import image_ocr

HAVE_OCR = image_ocr.available()
needs_ocr = pytest.mark.skipif(not HAVE_OCR, reason="rapidocr not installed")

DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
NOTO_DEVA = "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf"


def _render(path, lines, font_path, size=34, width=900):
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (width, 60 + size * 2 * len(lines)), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, size)
    y = 25
    for line in lines:
        draw.text((30, y), line, fill=(0, 0, 0), font=font)
        y += size * 2
    img.save(path)
    return path


# --------------------------------------------------------------------------- #
# Configuration — the most dangerous default in the whole feature
# --------------------------------------------------------------------------- #
def test_the_measured_engine_and_model_are_recorded_as_constants():
    assert image_ocr.OCR_ENGINE == "rapidocr"
    assert image_ocr.OCR_MODEL == "PP-OCRv5"
    assert image_ocr.OCR_BACKEND == "onnxruntime"


def test_config_never_leaves_rapidocrs_chinese_defaults_in_place():
    """rapidocr/config.yaml ships Det.lang_type='ch', Rec.lang_type='ch' and
    ocr_version='PP-OCRv6'. Omitting any of them silently loads a CHINESE
    recogniser — exactly what app/nrb/ocr.py:35-41 warns about for the RAG
    converter. Every key must be set explicitly, and PP-OCRv6 must never
    appear: v5 is the version §16.6 measured.

    Asserted on the pure declarative table, so this runs even where rapidocr
    is not installed — the environment where a silent default is likeliest."""
    cfg = image_ocr.ocr_config("devanagari")
    assert cfg == {
        "Det.engine_type": "onnxruntime",
        "Det.lang_type": "ch",
        "Det.model_type": "mobile",
        "Det.ocr_version": "PP-OCRv5",
        "Rec.engine_type": "onnxruntime",
        "Rec.lang_type": "devanagari",
        "Rec.model_type": "mobile",
        "Rec.ocr_version": "PP-OCRv5",
    }


def test_the_recogniser_language_is_the_only_thing_lang_changes():
    """Detection is script-agnostic and its config is held identical to the
    measured NRB one (§16.6), so that evidence carries over. Only Rec moves."""
    deva = image_ocr.ocr_config("devanagari")
    en = image_ocr.ocr_config("en")
    assert deva["Rec.lang_type"] == "devanagari"
    assert en["Rec.lang_type"] == "en"
    for key in ("Det.engine_type", "Det.lang_type", "Det.model_type", "Det.ocr_version"):
        assert deva[key] == en[key], key


def test_an_unsupported_language_is_refused():
    with pytest.raises(ValueError):
        image_ocr.ocr_config("klingon")


@needs_ocr
def test_every_configured_key_survives_conversion_to_rapidocrs_enums():
    """rapidocr REFUSES plain strings ("The value of Det.engine_type must be
    Enum Type"), so the declarative table above is only honoured if every key
    converts. A key that silently failed to convert would fall back to the
    Chinese default."""
    params = image_ocr.ocr_params("devanagari")
    cfg = image_ocr.ocr_config("devanagari")
    for key, expected in cfg.items():
        assert key in params, key
        assert params[key].value == expected, key


def test_default_language_reads_both_scripts():
    """A latin-only recogniser returns NOTHING for Nepali; the devanagari model's
    dictionary includes ASCII, so it is the only single defensible default."""
    assert image_ocr.DEFAULT_LANG == "devanagari"
    assert image_ocr.SUPPORTED_LANGS == {"devanagari", "en"}


def test_missing_rapidocr_raises_ocr_unavailable_not_import_error(monkeypatch):
    image_ocr.reset_engines()
    monkeypatch.setitem(sys.modules, "rapidocr", None)
    with pytest.raises(image_ocr.OcrUnavailable) as exc:
        image_ocr.ocr_image("whatever.png")
    assert "not installed" in str(exc.value)
    image_ocr.reset_engines()


# --------------------------------------------------------------------------- #
# Line grouping — pure, and required: rapidocr returns one box per WORD
# --------------------------------------------------------------------------- #
def _box(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def test_boxes_on_the_same_visual_row_join_into_one_line():
    """Measured: 'Total Amount: 45,320.75' comes back as THREE boxes on one
    row. Emitting one word per line would destroy every table."""
    boxes = np.array([_box(30, 29, 119, 60), _box(111, 27, 269, 62), _box(272, 26, 450, 64)])
    lines, _ = image_ocr.group_lines(boxes, ("Total", "Amount:", "45,320.75"), (1.0, 1.0, 1.0))
    assert lines == ["Total Amount: 45,320.75"]


def test_rows_are_ordered_top_to_bottom_and_words_left_to_right():
    boxes = np.array([_box(188, 95, 406, 129), _box(272, 26, 450, 64),
                      _box(27, 95, 181, 129), _box(30, 29, 119, 60)])
    lines, _ = image_ocr.group_lines(
        boxes, ("0123456789", "45,320.75", "Account:", "Total"), (1.0, 1.0, 1.0, 1.0)
    )
    assert lines == ["Total 45,320.75", "Account: 0123456789"]


def test_detection_order_is_never_trusted():
    """Reading order comes from the geometry, not from the order rapidocr
    happened to emit."""
    boxes = np.array([_box(0, 200, 50, 230), _box(0, 10, 50, 40)])
    lines, _ = image_ocr.group_lines(boxes, ("second", "first"), (1.0, 1.0))
    assert lines == ["first", "second"]


def test_each_line_keeps_the_lowest_confidence_of_its_boxes():
    """The honest aggregate: a line is only as trustworthy as its worst word."""
    boxes = np.array([_box(0, 10, 50, 40), _box(60, 10, 120, 40)])
    lines, scores = image_ocr.group_lines(boxes, ("good", "iffy"), (0.99, 0.42))
    assert lines == ["good iffy"]
    assert scores == [pytest.approx(0.42)]


def test_no_detections_yields_no_lines():
    """rapidocr returns txts=None (not an empty tuple) for a blank image."""
    lines, scores = image_ocr.group_lines(None, None, None)
    assert lines == [] and scores == []


# --------------------------------------------------------------------------- #
# The real engine
# --------------------------------------------------------------------------- #
@needs_ocr
def test_english_text_is_recovered_through_the_devanagari_recogniser(tmp_path):
    p = _render(tmp_path / "en.png", ["Total Amount: 45,320.75", "Account: 0123456789"], DEJAVU)
    result = image_ocr.ocr_image(p)
    body = "\n".join(result.lines)
    assert "45,320.75" in body
    assert "0123456789" in body


@needs_ocr
def test_devanagari_script_is_recovered(tmp_path):
    """Asserts the SCRIPT is recovered — deliberately NOT any particular word.

    Measured twice on this exact fixture, v5 returned 'नेपाल राषट्र बैंक' once
    (losing the ष्ट conjunct) and 'h राष्ट्र नंक' another time (keeping the
    conjunct, losing the other two words). That instability IS §16.6's finding:
    OCR output is retrieval text, not a transcription, and Devanagari
    correctness is still the open §15 Nepali review. A test that pinned a word
    would be asserting a transcription this engine does not promise — and would
    fail on an upgrade that improved the average."""
    p = _render(tmp_path / "np.png", ["नेपाल राष्ट्र बैंक"], NOTO_DEVA)
    result = image_ocr.ocr_image(p)
    body = "\n".join(result.lines)
    devanagari = [ch for ch in body if "\u0900" <= ch <= "\u097f"]
    assert len(devanagari) >= 5, body


@needs_ocr
def test_a_result_is_never_authoritative(tmp_path):
    """§16.6: OCR output is retrieval text, not a transcription. Nothing
    downstream may present a figure from it as confirmed."""
    p = _render(tmp_path / "en.png", ["Total 100"], DEJAVU)
    assert image_ocr.ocr_image(p).authoritative is False


@needs_ocr
def test_confidence_is_reported_but_never_used_as_a_gate(tmp_path):
    """Scores are information. §16.6 refuses to invent a pass/fail threshold
    from an orthography measurement, so nothing in this module may compare a
    score to a constant."""
    import ast
    import pathlib

    p = _render(tmp_path / "en.png", ["Total 100"], DEJAVU)
    result = image_ocr.ocr_image(p)
    assert 0.0 <= result.mean_score <= 1.0
    assert result.min_score <= result.mean_score

    # No score may be compared against a numeric THRESHOLD anywhere in the
    # module. Checked by AST rather than by reading, because the temptation to
    # add `if score < 0.6: withhold` is exactly what §16.6 forbids.
    source = pathlib.Path(image_ocr.__file__).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        src = (ast.get_source_segment(source, node) or "").lower()
        if "score" not in src:
            continue
        operands = [node.left, *node.comparators]
        numeric = [
            o for o in operands
            if isinstance(o, ast.Constant) and isinstance(o.value, (int, float))
        ]
        assert not numeric, f"score compared to a threshold: {src}"


@needs_ocr
def test_a_blank_image_yields_no_lines_rather_than_an_error(tmp_path):
    from PIL import Image

    p = tmp_path / "blank.png"
    Image.new("RGB", (300, 200), (255, 255, 255)).save(p)
    result = image_ocr.ocr_image(p)
    assert result.lines == []


@needs_ocr
def test_the_engine_is_loaded_once_per_language(tmp_path):
    """Model load is seconds; reloading per request would dominate a chat turn."""
    p = _render(tmp_path / "en.png", ["Total 100"], DEJAVU)
    image_ocr.reset_engines()
    image_ocr.ocr_image(p)
    info = image_ocr.engine_cache_info()
    image_ocr.ocr_image(p)
    assert image_ocr.engine_cache_info().hits > info.hits


@needs_ocr
def test_engine_version_names_the_installed_packages():
    version = image_ocr.engine_version()
    assert "rapidocr" in version and "onnxruntime" in version
