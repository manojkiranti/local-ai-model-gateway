"""native-3 in the recovery cache, the dependency resolver and chunk metadata (spec §2, §3.2)."""

from __future__ import annotations

import pytest

from app.nrb import fontrepair, recovery, recovery_cache
from app.nrb import rag as nrb_rag
from app.nrb.recovery import PageText, ROUTE_NATIVE, ROUTE_OCR, RecoveredDocument
from app.nrb.recovery_cache import CachedRecovery, CachedUnit
from tests.test_nrb_native_repair_recovery import StubRepair
from tests.test_nrb_recovery_cache import CountingOcr, _engines
from tests import nrb_font_pdf as W
from tests.nrb_font_pdf import NEEDS_SHAPER

LOHIT = W.LOHIT.read_bytes()


def test_flag_off_keeps_todays_native_engine_string():
    assert _engines().native == "passthrough/native-2"
    assert recovery_cache.engine_versions(converter=None, lexicon=None, ocr=None,
                                          repair=None).native == "passthrough/native-2"


def test_a_repair_engine_names_the_native_engine():
    versions = recovery_cache.engine_versions(converter=None, lexicon=None, ocr=None,
                                              repair=StubRepair())
    assert versions.native == "native-3/stub"


def test_the_base_version_is_untouched():
    assert recovery_cache.base_version() == "native-2|recovery-1|prov-1|gate=0.8|unjudged=0.8"


def _units(engine_native: str, ocr_engine: str):
    return (
        CachedUnit(1, None, ROUTE_NATIVE, "clean", engine_native, True, "old", None, {}),
        CachedUnit(2, None, ROUTE_NATIVE, "clean", engine_native, True, "old", None, {}),
        CachedUnit(3, None, ROUTE_OCR, "no_font_scan_backed", ocr_engine, True, "ओसीआर", None,
                   {"authoritative": False}),
    )


def _cached(units):
    return CachedRecovery("a" * 64, recovery_cache.base_version(), "pdf", recovery.PLAN_PAGES,
                          "legacy_font_suspected", 1.0, (), units)


@NEEDS_SHAPER
def test_turning_the_repair_on_re_runs_native_units_only(tmp_path):
    path = W.word_pdf(tmp_path, LOHIT, pages=3)
    ocr = CountingOcr()
    ocr_engine = _engines(ocr=ocr).ocr
    stub = StubRepair()
    doc, report = recovery_cache.resolve(path, cached=_cached(_units("passthrough/native-2", ocr_engine)),
                                         ocr=ocr, repair=stub, cold=None)
    assert report.outcome == "partial"
    assert (report.ocr_units, report.converter_units) == (0, 0)
    assert ocr.calls == 0
    assert stub.calls == [1, 2]
    assert doc.pages[2].text == "ओसीआर"
    assert report.repaired_units == 2


@NEEDS_SHAPER
def test_cold_and_refresh_agree_on_whether_the_document_is_suspect(tmp_path):
    path = W.word_pdf(tmp_path, LOHIT, pages=2)
    result = recovery.extraction.extract_file(path, family="pdf", extension="pdf",
                                              extractor_version="native-2")
    cold = StubRepair()
    recovery.recover(path, result, repair=cold)
    warm = StubRepair()
    units = (CachedUnit(1, None, ROUTE_NATIVE, "clean", "passthrough/native-2", True, "x", None, {}),
             CachedUnit(2, None, ROUTE_NATIVE, "clean", "passthrough/native-2", True, "x", None, {}))
    cached = CachedRecovery("b" * 64, recovery_cache.base_version(), "pdf", recovery.PLAN_NATIVE,
                            "clean", None, (), units)
    recovery_cache.resolve(path, cached=cached, repair=warm, cold=None)
    assert cold.calls == warm.calls == [1, 2]


def test_a_non_pdf_native_document_is_never_repaired(tmp_path):
    """Review Focus 5: a .docx unit goes cold once, and the engine is not called."""
    stub = StubRepair()
    unit = CachedUnit(1, None, ROUTE_NATIVE, "clean", "passthrough/native-2", True, "पाठ", None, {})
    cached = CachedRecovery("c" * 64, recovery_cache.base_version(), "document",
                            recovery.PLAN_NATIVE, "clean", None, (), (unit,))
    fresh = RecoveredDocument("document", recovery.PLAN_NATIVE, "clean", None,
                              (PageText(1, ROUTE_NATIVE, "clean", "पाठ"),))
    doc, report = recovery_cache.resolve(tmp_path / "x.docx", cached=cached, repair=stub,
                                         cold=lambda: fresh)
    assert report.outcome == "cold" and report.reason == "non_pdf_engine_changed"
    assert stub.calls == [] and doc.pages[0].text == "पाठ"


@NEEDS_SHAPER
def test_flag_on_without_the_shaper_is_a_named_unavailable_engine(tmp_path, monkeypatch):
    """Review Focus 4."""
    monkeypatch.setattr(fontrepair, "engine_available", lambda: False)
    engine = fontrepair.FontRepairEngine()
    assert recovery_cache.engine_versions(converter=None, lexicon=None, ocr=None,
                                          repair=engine).native == fontrepair.UNAVAILABLE
    path = W.word_pdf(tmp_path, LOHIT)
    result = recovery.extraction.extract_file(path, family="pdf", extension="pdf",
                                              extractor_version="native-2")
    page = recovery.recover(path, result, repair=engine).pages[0]
    assert page.detail["repair"] == fontrepair.unrepaired(fontrepair.SHAPER_UNAVAILABLE)


def test_the_repair_dependency_follows_the_flag():
    class Off:
        nrb_native_repair = False

    class On:
        nrb_native_repair = True

    assert nrb_rag._repair_dependency(Off()) is None
    assert isinstance(nrb_rag._repair_dependency(On()), fontrepair.FontRepairEngine)


def test_the_flag_defaults_off():
    from app.config import Settings

    assert Settings.model_fields["nrb_native_repair"].default is False


@pytest.mark.parametrize("status, expected", [
    (fontrepair.STATUS_REPAIRED, "font_tables"),
    ("unrepaired:coverage", "unrepaired"),
])
def test_a_detected_page_is_non_authoritative_in_its_chunks(status, expected):
    page = PageText(1, ROUTE_NATIVE, "clean", "पाठ",
                    detail={"repair": status, "repair_engine": "native-3/x"})
    meta = nrb_rag._chunk_meta(page, "native-2")
    assert meta["text_repair"] == expected
    assert meta["repair_engine"] == "native-3/x"
    assert meta["authoritative"] is False


def test_a_not_applicable_or_plain_native_page_carries_no_caveat():
    for detail in ({"repair": fontrepair.NOT_APPLICABLE}, {}):
        meta = nrb_rag._chunk_meta(PageText(1, ROUTE_NATIVE, "clean", "Annex", detail=detail), "native-2")
        assert "text_repair" not in meta and "authoritative" not in meta


# --------------------------------------------------------------------------- #
# The partial refresh: flag off runs no detector; flag on warns like the cold path
# --------------------------------------------------------------------------- #
def _two_page_pdf(tmp_path):
    from tests.test_nrb_recovery import _write_pdf

    return _write_pdf(tmp_path, "two.pdf", [{"font": "Arial", "embedded": True}] * 2)


def _stale_native(warnings=()):
    units = (CachedUnit(1, None, ROUTE_NATIVE, "clean", "native-3/old", True, "x", None, {}),
             CachedUnit(2, None, ROUTE_NATIVE, "clean", "native-3/old", True, "y", None, {}))
    return CachedRecovery("d" * 64, recovery_cache.base_version(), "pdf", recovery.PLAN_NATIVE,
                          "clean", None, tuple(warnings), units)


def test_a_flag_off_partial_refresh_never_runs_the_detector(tmp_path, monkeypatch):
    def must_not_run(*_a, **_k):
        raise AssertionError("fontrepair.detect ran with no repair engine")

    monkeypatch.setattr(fontrepair, "detect", must_not_run)
    doc, report = recovery_cache.resolve(_two_page_pdf(tmp_path), cached=_stale_native(),
                                         repair=None, cold=None)
    assert report.outcome == "partial"
    assert all(p.detail == {} for p in doc.pages)
    assert doc.warnings == ()


SUSPECT = fontrepair.Detection(suspect=True, density=0.0425, devanagari_chars=2000, illegal_clusters=85)


def test_a_flag_on_partial_refresh_warns_exactly_as_the_cold_path(tmp_path, monkeypatch):
    monkeypatch.setattr(fontrepair, "detect", lambda _texts: SUSPECT)
    path = _two_page_pdf(tmp_path)
    doc, report = recovery_cache.resolve(path, cached=_stale_native(("w0",)),
                                         repair=StubRepair(), cold=None)
    assert report.outcome == "partial"
    assert doc.warnings == ("w0", "tounicode_suspected:0.0425")

    result = recovery.extraction.extract_file(path, family="pdf", extension="pdf",
                                              extractor_version="native-2")
    cold = recovery.recover(path, result, repair=StubRepair())
    assert [w for w in cold.warnings if w.startswith("tounicode")] == ["tounicode_suspected:0.0425"]


def test_a_refresh_does_not_repeat_a_warning_the_cache_already_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(fontrepair, "detect", lambda _texts: SUSPECT)
    doc, _ = recovery_cache.resolve(_two_page_pdf(tmp_path),
                                    cached=_stale_native(("tounicode_suspected:0.0425",)),
                                    repair=StubRepair(), cold=None)
    assert doc.warnings == ("tounicode_suspected:0.0425",)


def test_the_real_dependency_path_has_no_repair_engine_by_default(monkeypatch):
    """No injection: nrb_dependencies() → get_settings() → _repair_dependency."""
    from app.config import get_settings
    from app.nrb import ocr

    class NoOcr:
        def open(self):
            return False, "stubbed out by the test"

    monkeypatch.delenv("NRB_NATIVE_REPAIR", raising=False)
    monkeypatch.setattr(ocr, "DoclingRapidOcrEngine", NoOcr)
    get_settings.cache_clear()
    nrb_rag.reset_dependencies()
    try:
        assert get_settings().nrb_native_repair is False
        assert nrb_rag.nrb_dependencies()[3] is None
    finally:
        nrb_rag.reset_dependencies()
        get_settings.cache_clear()
