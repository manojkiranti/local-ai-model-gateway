"""native-3's repair of one page, and its gate (spec §4).

Every PDF here is built by tests/nrb_font_pdf.py the way Word builds one: glyph
ids shaped by HarfBuzz, labels assigned by Word's positional rule. The ground
truth is the text that was shaped, so "repaired" is an exact comparison.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("uharfbuzz")
pytest.importorskip("fontTools")

from app.files import documents as file_documents  # noqa: E402
from app.nrb import fontrepair  # noqa: E402
from tests import nrb_font_pdf as W  # noqa: E402

LOHIT = W.LOHIT.read_bytes()


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _native(path: Path, page: int = 1) -> str:
    return file_documents.read_pdf_pages(path).pages[page - 1]


def _repair(path: Path, page: int = 1):
    return fontrepair.FontRepairEngine().repair_page(path, page, _native(path, page))


# --------------------------------------------------------------------------- #
# The detector (density, spec §4.1)
# --------------------------------------------------------------------------- #
def test_the_detector_constants_are_the_preregistered_values():
    assert fontrepair.DETECT_DENSITY == 0.001
    assert fontrepair.DETECT_MIN_DEVANAGARI == 500


def test_the_detector_fires_on_garbled_text_and_not_on_clean():
    assert fontrepair.detect(["कार्ाालर् " * 100]).suspect
    assert not fontrepair.detect(["कार्यालय " * 100]).suspect


def test_too_little_devanagari_never_fires():
    assert not fontrepair.detect(["कार्ाालर्"]).suspect


# --------------------------------------------------------------------------- #
# ToUnicode
# --------------------------------------------------------------------------- #
def test_tounicode_parses_bfchar_and_both_bfrange_forms():
    data = (b"2 beginbfchar\n<0001> <0915>\n<0002> <0930094D>\nendbfchar\n"
            b"1 beginbfrange\n<0010> <0012> <0905>\nendbfrange\n"
            b"1 beginbfrange\n<0020> <0021> [<0924> <093F>]\nendbfrange\n")
    labels = fontrepair.parse_tounicode(data)
    assert labels[1] == "क" and labels[2] == "र्"
    assert labels[0x11] == "आ"
    assert labels[0x21] == "ि"


def test_the_corrected_cmap_round_trips_through_the_parser():
    labels = {1: "क", 2: "", 300: "क्ष"}
    assert fontrepair.parse_tounicode(fontrepair.corrected_cmap(labels)) == labels


# --------------------------------------------------------------------------- #
# A Word page
# --------------------------------------------------------------------------- #
def test_a_word_page_is_garbled_natively(tmp_path):
    """The fixture's premise: Word's positional labels really do garble it."""
    native = _native(W.word_pdf(tmp_path, LOHIT))
    assert fontrepair.detect([native]).suspect, native[:300]


def test_a_word_page_is_repaired_to_exactly_the_shaped_text(tmp_path):
    out = _repair(W.word_pdf(tmp_path, LOHIT))
    assert out.status == fontrepair.STATUS_REPAIRED, out.detail
    assert _lines(out.text) == list(W.LINES) * W.REPEAT
    assert out.detail["runs"] == out.detail["runs_matched"] == len(W.LINES) * W.REPEAT
    assert out.detail["illegal_after"] == 0 < out.detail["illegal_before"]
    assert out.detail["font_identities"] == ["Lohit Devanagari/711"]


@pytest.mark.skipif(not W.SYSTEM_KALIMATI.exists(), reason="fonts-deva-extra Kalimati not installed")
def test_an_old_spec_kalimati_page_is_repaired_too(tmp_path):
    kalimati = W.SYSTEM_KALIMATI.read_bytes()
    out = _repair(W.word_pdf(tmp_path, kalimati))
    assert out.status == fontrepair.STATUS_REPAIRED, out.detail
    assert _lines(out.text) == list(W.LINES) * W.REPEAT


def test_a_correct_tounicode_is_left_alone(tmp_path):
    body = list(W.LINES) * W.REPEAT
    path = W.word_pdf(tmp_path, LOHIT, labels=W.honest_labels(LOHIT, body))
    out = _repair(path)
    assert out.status == fontrepair.unrepaired(fontrepair.NO_SUSPECT_FONT)
    assert out.text == _native(path)


def test_a_font_without_gsub_fails_coverage_and_keeps_the_native_text(tmp_path):
    """Word 365: shaped with the full font, embedded without GSUB (spec §7)."""
    path = W.word_pdf(tmp_path, LOHIT, embed=W.strip_gsub(LOHIT))
    out = _repair(path)
    assert out.status == fontrepair.unrepaired(fontrepair.COVERAGE)
    assert out.text == _native(path)
    assert out.detail["unresolved_uses"] > 0


def test_a_font_with_no_unicode_cmap_is_never_suspect(tmp_path):
    """Review Focus 1."""
    path = W.word_pdf(tmp_path, LOHIT, embed=W.symbol_cmap_only(LOHIT))
    out = _repair(path)
    assert out.status == fontrepair.unrepaired(fontrepair.NO_SUSPECT_FONT)
    assert out.text == _native(path)


def test_text_inside_a_form_xobject_is_walked(tmp_path):
    """Review Focus 2."""
    out = _repair(W.word_pdf(tmp_path, LOHIT, in_form=True))
    assert out.status == fontrepair.STATUS_REPAIRED, out.detail
    assert out.detail["runs"] == len(W.LINES) * W.REPEAT


def test_save_restore_around_the_text_does_not_lose_the_font(tmp_path):
    """Review Focus 2."""
    out = _repair(W.word_pdf(tmp_path, LOHIT, save_restore=True))
    assert out.status == fontrepair.STATUS_REPAIRED, out.detail
    assert out.detail["runs"] == len(W.LINES) * W.REPEAT


# A Word subset has no glyph for ZWJ, so the shaper draws the hidden joiner
# as the space glyph: राख्‍नु is drawn [र ा ख्(half) space न ु]. Read as a
# space it is "राख् नु", which shapes to [र ा ख ् space न ु] and fails the
# round trip on every page that has one (CHECKPOINT A, 2026-10-02).
JOINED = ("राख्\u200dनु सञ्\u200dचालन क्\u200dया पश्चात् भएको",)
NO_JOINERS = W.without_codepoints(LOHIT, (0x200C, 0x200D))


def test_a_hidden_joiner_drawn_as_the_space_glyph_is_repaired(tmp_path):
    lines = list(W.LINES) + list(JOINED)
    path = W.word_pdf(tmp_path, NO_JOINERS, lines=lines)
    out = _repair(path)
    assert out.status == fontrepair.STATUS_REPAIRED, out.detail
    assert _lines(out.text) == lines * W.REPEAT
    assert out.detail["hidden_joiners"] == 3 * W.REPEAT


def test_a_real_space_after_an_explicit_halant_stays_a_space(tmp_path):
    """पश्चात् ends in a visible halant, not a half form: its space is a space."""
    lines = list(W.LINES) + ["पश्चात् भएको अर्थात् यस्तो"]
    out = _repair(W.word_pdf(tmp_path, NO_JOINERS, lines=lines))
    assert out.status == fontrepair.STATUS_REPAIRED, out.detail
    assert _lines(out.text) == lines * W.REPEAT
    assert out.detail["hidden_joiners"] == 0


def test_a_joiner_that_cannot_be_placed_fails_layout(tmp_path, monkeypatch):
    lines = list(W.LINES) + list(JOINED)
    path = W.word_pdf(tmp_path, NO_JOINERS, lines=lines)
    monkeypatch.setattr(fontrepair, "rejoin", lambda raw, runs: None)
    out = _repair(path)
    assert out.status == fontrepair.unrepaired(fontrepair.LAYOUT)
    assert out.detail["joiners_unplaced"] == 3 * W.REPEAT
    assert out.text == _native(path)


def test_relabelling_nothing_keeps_pypdfs_spacing(tmp_path):
    """The ToUnicode still labels the space glyph " ": pypdf finds a font's
    space width through that label, and without it wrote double spaces on every
    repaired page (measured on production PDFs, 2026-10-02)."""
    lines = list(W.LINES) + list(JOINED)
    out = _repair(W.word_pdf(tmp_path, NO_JOINERS, lines=lines))
    assert "  " not in out.text


def _reph_gids(program: bytes) -> frozenset[int]:
    from app.nrb import glyphtable
    from app.nrb.reorder import REPH

    table = glyphtable.build_glyph_table(program)
    return frozenset(g for g, t in table.tokens.items() if REPH in t and g not in table.ambiguous)


def test_a_separately_positioned_reph_is_rejoined_to_its_cluster(tmp_path):
    """Word positions the reph glyph on its own inside a TJ, and pypdf writes a
    space on each side of it: मार्फत served as "माफ <R> त" orphaned the reph
    and split the word (CHECKPOINT A, 2026-10-02: 82 occurrences in 11
    documents). The run itself drew no space there, so neither is kept."""
    path = W.word_pdf(tmp_path, LOHIT, isolate=_reph_gids(LOHIT))
    out = _repair(path)
    assert out.status == fontrepair.STATUS_REPAIRED, out.detail
    assert _lines(out.text) == list(W.LINES) * W.REPEAT
    assert out.detail["gaps_closed"] > 0


R = "\ue001"  # reorder.REPH


def test_rejoin_closes_pypdfs_gaps_on_both_sides_of_a_reph():
    assert fontrepair.rejoin(f"x मक {R} री y", [(f"मक{R}री", frozenset())]) == (f"x मक{R}री y", 2)


def test_rejoin_closes_a_gap_before_a_dependent_sign():
    assert fontrepair.rejoin(f"पाक े{R}", [(f"पाके{R}", frozenset())]) == (f"पाके{R}", 1)


P = "\ue000"  # reorder.PREBASE


def test_rejoin_closes_a_gap_after_a_dependent_sign():
    """फैसला served as "फ ै सला": Word positions ै on its own, and pypdf
    writes a space on both sides of it (219 + 1,675 occurrences measured)."""
    assert fontrepair.rejoin("भएको फ ै सला", [("भएको फैसला", frozenset())]) == ("भएको फैसला", 2)


def test_rejoin_closes_a_gap_beside_the_prebase_sign():
    assert fontrepair.rejoin(f"प्रकृ {P}त", [(f"प्रकृ{P}त", frozenset())]) == (f"प्रकृ{P}त", 1)


def test_rejoin_never_touches_a_gap_between_two_letters():
    """Measured: pypdf never wrote whitespace between two letters of a run that
    the run did not draw. If it ever does, it is not this rule's to judge."""
    assert fontrepair.rejoin("कम ल", [("कमल", frozenset())]) == ("कम ल", 0)


def test_rejoin_keeps_a_space_the_run_drew():
    assert fontrepair.rejoin(f"पने{R} मना", [(f"पने{R} मना", frozenset())]) == (f"पने{R} मना", 0)


def test_rejoin_never_removes_a_line_break():
    assert fontrepair.rejoin(f"मक{R}\nरी", [(f"मक{R}री", frozenset())]) == (f"मक{R}\nरी", 0)


def test_rejoin_normalises_pypdfs_spaces_beside_a_reph_to_the_runs_own():
    runs = [(f"पने{R} मना", frozenset())]
    assert fontrepair.rejoin(f"पने{R}   मना", runs) == (f"पने{R} मना", 1)
    assert fontrepair.rejoin(f"पने{R}\nमना", runs) == (f"पने{R}\nमना", 0)


def test_rejoin_takes_all_of_pypdfs_whitespace_at_a_hidden_joiner():
    runs = [("राख् नु", frozenset({4}))]
    assert fontrepair.rejoin("x राख् \nनु y", runs) == ("x राख्\u200dनु y", 0)


def test_rejoin_follows_content_order():
    """A genuinely spaced "राख् नु" earlier on the page keeps its space."""
    runs = [("राख् नु", frozenset()), ("राख् नु", frozenset({4}))]
    assert fontrepair.rejoin("राख् नु\nराख् नु", runs) == ("राख् नु\nराख्\u200dनु", 0)


def test_rejoin_refuses_when_a_run_is_not_in_the_text():
    runs = [("राख् नु", frozenset({4})), ("सञ् च", frozenset({3}))]
    assert fontrepair.rejoin("राख् नु", runs) is None


def test_a_roundtrip_mismatch_fails_the_page(tmp_path, monkeypatch):
    path = W.word_pdf(tmp_path, LOHIT)
    monkeypatch.setattr(fontrepair.shaping, "shape", lambda program, text: (0,))
    out = _repair(path)
    assert out.status == fontrepair.unrepaired(fontrepair.ROUNDTRIP)
    assert out.text == _native(path)
    assert out.detail["runs_matched"] == 0
    assert out.detail["examples"][0]["check"] == fontrepair.ROUNDTRIP


def test_an_orphan_marker_fails_the_page(tmp_path):
    lone = [W.shaped_lines(LOHIT, ["क"])[0][0], W.prebase_gid(LOHIT)]
    path = W.word_pdf(tmp_path, LOHIT, extra_line=lone)
    out = _repair(path)
    assert out.status == fontrepair.unrepaired(fontrepair.ORPHANS)


def test_a_page_without_devanagari_is_not_applicable(tmp_path):
    out = fontrepair.FontRepairEngine().repair_page(tmp_path / "absent.pdf", 1, "Annual report 2024")
    assert out.status == fontrepair.NOT_APPLICABLE
    assert out.text == "Annual report 2024"


def test_a_missing_shaper_is_recorded_not_raised(tmp_path, monkeypatch):
    path = W.word_pdf(tmp_path, LOHIT)
    monkeypatch.setattr(fontrepair, "engine_available", lambda: False)
    out = _repair(path)
    assert out.status == fontrepair.unrepaired(fontrepair.SHAPER_UNAVAILABLE)
    assert fontrepair.engine_version() == fontrepair.UNAVAILABLE


def test_an_unreadable_file_is_read_failed(tmp_path):
    """Review Focus 3."""
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf at all")
    out = fontrepair.FontRepairEngine().repair_page(bad, 1, "कार्ाालर्")
    assert out.status == fontrepair.unrepaired(fontrepair.READ_FAILED)
    assert out.text == "कार्ाालर्"


def test_a_garbage_font_program_is_unsupported_font(tmp_path):
    """Review Focus 3."""
    path = W.word_pdf(tmp_path, LOHIT, embed=b"\x00" * 64)
    out = _repair(path)
    assert out.status == fontrepair.unrepaired(fontrepair.UNSUPPORTED_FONT)


def test_the_engine_version_reads_its_terms_live(monkeypatch):
    base = fontrepair.engine_version()
    assert base.startswith("native-3/repair-1/D=1/N=500/lang=ne/hb-")
    monkeypatch.setattr(fontrepair, "DETECT_DENSITY", 0.002)
    assert fontrepair.engine_version() != base


def test_the_layout_check_requires_every_run_intact_and_in_order():
    assert fontrepair.missing_in_order(["क ख", "ग"], "कख\nग") == 0
    assert fontrepair.missing_in_order(["ग", "क"], "क ग") == 1


def test_one_document_is_opened_once_for_many_pages(tmp_path, monkeypatch):
    path = W.word_pdf(tmp_path, LOHIT, pages=3)
    opened = []
    original = fontrepair._Document

    class Counting(original):
        def __init__(self, p):
            opened.append(p)
            super().__init__(p)

    monkeypatch.setattr(fontrepair, "_Document", Counting)
    engine = fontrepair.FontRepairEngine()
    for page in (1, 2, 3):
        assert engine.repair_page(path, page, _native(path, page)).status == fontrepair.STATUS_REPAIRED
    assert len(opened) == 1


def test_fonts_report_names_the_contradicting_font(tmp_path):
    report = fontrepair.fonts_report(W.word_pdf(tmp_path, LOHIT))
    assert report["contradicts"] is True
    assert report["fonts"][0]["identity"] == "Lohit Devanagari/711"


def test_a_form_nested_past_the_depth_limit_fails_closed(tmp_path):
    """A form the walker declines is text the gate never checked."""
    path = W.word_pdf(tmp_path, LOHIT, nest=4)
    out = _repair(path)
    assert out.status == fontrepair.unrepaired(fontrepair.LAYOUT)
    assert out.text == _native(path)
    assert out.detail["declined_forms"] == 1


def test_a_form_within_the_limit_is_still_repaired(tmp_path):
    out = _repair(W.word_pdf(tmp_path, LOHIT, nest=3))
    assert out.status == fontrepair.STATUS_REPAIRED, out.detail


def test_a_form_drawn_twice_is_walked_twice(tmp_path):
    out = _repair(W.word_pdf(tmp_path, LOHIT, twice=True))
    assert out.status == fontrepair.STATUS_REPAIRED, out.detail
    assert out.detail["runs"] == 2 * len(W.LINES) * W.REPEAT


def test_a_shaper_that_raises_is_engine_error_not_an_exception(tmp_path, monkeypatch):
    path = W.word_pdf(tmp_path, LOHIT)

    def boom(program, text):
        raise RuntimeError("hb")

    monkeypatch.setattr(fontrepair.shaping, "shape", boom)
    out = _repair(path)
    assert out.status == fontrepair.unrepaired(fontrepair.ENGINE_ERROR)
    assert out.text == _native(path)
    assert out.detail["error"] == "RuntimeError"


def test_an_extract_text_that_raises_is_engine_error(tmp_path, monkeypatch):
    from pypdf import PageObject

    path = W.word_pdf(tmp_path, LOHIT)
    native = _native(path)

    def boom(self, *a, **k):
        raise ValueError("x")

    monkeypatch.setattr(PageObject, "extract_text", boom)
    out = fontrepair.FontRepairEngine().repair_page(path, 1, native)
    assert out.status == fontrepair.unrepaired(fontrepair.ENGINE_ERROR)
    assert out.text == native


@pytest.mark.parametrize("subtype", ["PS", "Weird"])
def test_a_non_form_non_image_xobject_fails_closed(tmp_path, subtype):
    """pypdf extracts any XObject that is not an Image; the walker must not skip one unseen."""
    path = W.word_pdf(tmp_path, LOHIT, nest=1, form_subtype=subtype)
    out = _repair(path)
    assert out.status == fontrepair.unrepaired(fontrepair.LAYOUT)
    assert out.detail["declined_forms"] == 1
