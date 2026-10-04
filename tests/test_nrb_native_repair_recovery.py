"""native-3 inside recovery: routing untouched, repair on native pages only (spec §2, §3, §5)."""

from __future__ import annotations

import ast
import hashlib
import inspect
import textwrap

from pathlib import Path

from app.files import documents as file_documents
from app.nrb import extraction, fontrepair, recovery
from tests import nrb_font_pdf as W
from tests.nrb_font_pdf import NEEDS_SHAPER

LOHIT = W.LOHIT.read_bytes()

# Recorded by Task 5 Step 1, BEFORE recovery.py was edited. If either changes,
# the routing changed: bump RECOVERY_ROUTING_VERSION (the base version) and
# re-record — the engine-version design (D1) is only honest while these hold.
PINNED = {
    "plan_document": "f57cd5f420dccd5c4ccef8e63c8a079e4edc82249b276e6262d8722250870101",
    "route_page": "5cb6c6391b5af28f8221f237004e7ff2d9952a4fb39b880a5429c2425ac41e3a",
}


def _ast_sha(fn) -> str:
    return hashlib.sha256(ast.dump(ast.parse(textwrap.dedent(inspect.getsource(fn)))).encode()).hexdigest()


def test_the_routing_functions_are_unchanged():
    assert _ast_sha(recovery.plan_document) == PINNED["plan_document"]
    assert _ast_sha(recovery.route_page) == PINNED["route_page"]
    assert recovery.RECOVERY_ROUTING_VERSION == "recovery-1"


def test_page_routes_for_a_native_plan_is_native_everywhere(tmp_path):
    plan = recovery.DocumentPlan(recovery.PLAN_NATIVE, "clean", None)
    assert recovery.page_routes(tmp_path / "x.pdf", plan, ["a", "b"]) == (
        (recovery.ROUTE_NATIVE, "clean"), (recovery.ROUTE_NATIVE, "clean"))


def test_page_routes_for_no_recovery_is_empty(tmp_path):
    plan = recovery.DocumentPlan(recovery.PLAN_NONE, "parser_error", None)
    assert recovery.page_routes(tmp_path / "x.pdf", plan, ["a"]) == ()


def test_page_routes_agree_with_what_recover_routes(tmp_path):
    """One routing answer, two callers: recover() and the evidence scripts."""
    from tests.test_nrb_recovery import _write_pdf

    path = _write_pdf(tmp_path, "mixed.pdf", [
        {"font": "Preeti", "embedded": True}, {"image": True}, {"font": "Preeti", "embedded": True},
    ])
    plan = recovery.DocumentPlan(recovery.PLAN_PAGES, "legacy_font_suspected", 1.0)
    pages = ["g]kfn /fi6« a}+s " * 5, "", "g]kfn /fi6« a}+s " * 5]
    routes = recovery.page_routes(path, plan, pages)
    assert [r for r, _ in routes] == [recovery.ROUTE_LEGACY, recovery.ROUTE_OCR, recovery.ROUTE_LEGACY]


class StubRepair:
    """A repair engine that records calls and answers what it is told to."""

    def __init__(self, status=fontrepair.STATUS_REPAIRED, text="REPAIRED", raises=None):
        self.version = "native-3/stub"
        self.status, self.text, self.raises = status, text, raises
        self.calls: list[int] = []

    def repair_page(self, path, page_number, native_text):
        self.calls.append(page_number)
        if self.raises:
            raise self.raises
        return fontrepair.RepairOutcome(self.status, self.text, {"runs": 1, "runs_matched": 1})


def _classified(path: Path):
    return extraction.extract_file(path, family="pdf", extension="pdf", extractor_version="native-2")


@NEEDS_SHAPER
def test_without_a_repair_engine_recovery_is_byte_identical(tmp_path):
    path = W.word_pdf(tmp_path, LOHIT)
    doc = recovery.recover(path, _classified(path))
    assert [p.text for p in doc.pages] == list(file_documents.read_pdf_pages(path).pages)
    assert all(p.detail == {} for p in doc.pages)
    assert not any(w.startswith("tounicode_suspected") for w in doc.warnings)


@NEEDS_SHAPER
def test_a_detected_document_is_repaired_with_the_real_engine(tmp_path):
    path = W.word_pdf(tmp_path, LOHIT)
    doc = recovery.recover(path, _classified(path), repair=fontrepair.FontRepairEngine())
    page = doc.pages[0]
    assert page.route == recovery.ROUTE_NATIVE and page.ok and page.indexable
    assert page.detail["repair"] == fontrepair.STATUS_REPAIRED
    assert [l.strip() for l in page.text.splitlines() if l.strip()] == list(W.LINES) * W.REPEAT
    assert any(w.startswith("tounicode_suspected:") for w in doc.warnings)


@NEEDS_SHAPER
def test_an_undetected_document_never_calls_the_engine(tmp_path):
    """Too little Devanagari to judge (N=500): the engine is not consulted."""
    path = W.word_pdf(tmp_path, LOHIT, repeat=1)
    stub = StubRepair()
    doc = recovery.recover(path, _classified(path), repair=stub)
    assert stub.calls == []
    assert doc.pages[0].text == file_documents.read_pdf_pages(path).pages[0]


@NEEDS_SHAPER
def test_an_unrepaired_page_keeps_its_native_text_and_stays_indexable(tmp_path):
    """Rule 6 — the deliberate exception to rule 5 (spec §5.1)."""
    path = W.word_pdf(tmp_path, LOHIT)
    stub = StubRepair(status=fontrepair.unrepaired(fontrepair.COVERAGE), text="MUST NOT APPEAR")
    page = recovery.recover(path, _classified(path), repair=stub).pages[0]
    assert page.text == file_documents.read_pdf_pages(path).pages[0]
    assert page.indexable
    assert page.detail["repair"] == "unrepaired:coverage"
    assert page.detail["repair_engine"] == "native-3/stub"


def test_a_failed_legacy_conversion_still_withholds():
    """Rule 6's other half: rule 5 is unchanged for the converter."""
    page = recovery.convert_unit(1, "g]kfn /fi6« a}+s", reason="embedded_font",
                                 converter=None, lexicon=None, document_legacy_ratio=1.0)
    assert page.ok is False and page.text == ""


@NEEDS_SHAPER
def test_a_repair_engine_that_raises_keeps_the_native_text(tmp_path):
    """Review Focus 3."""
    path = W.word_pdf(tmp_path, LOHIT)
    stub = StubRepair(raises=RuntimeError("boom"))
    page = recovery.recover(path, _classified(path), repair=stub).pages[0]
    assert page.text == file_documents.read_pdf_pages(path).pages[0]
    assert page.detail["repair"] == fontrepair.unrepaired(fontrepair.ENGINE_ERROR)


def test_native_unit_without_suspicion_is_plain_passthrough(tmp_path):
    stub = StubRepair()
    page = recovery.native_unit(3, "पाठ", reason="clean", path=tmp_path / "x.pdf",
                                suspect=False, repair=stub)
    assert (page.route, page.reason, page.text, page.detail) == (recovery.ROUTE_NATIVE, "clean", "पाठ", {})
    assert stub.calls == []


# --------------------------------------------------------------------------- #
# Flag off executes nothing new (final review minor): no detector, no warning.
# --------------------------------------------------------------------------- #
def _detector_must_not_run(*_args, **_kwargs):
    raise AssertionError("fontrepair.detect ran with no repair engine")


def test_flag_off_never_runs_the_detector_on_a_native_pdf(tmp_path, monkeypatch):
    from app.nrb import quality
    from tests.test_nrb_recovery import UNICODE_NEPALI, _result, _write_pdf

    path = _write_pdf(tmp_path, "native.pdf", [{"font": "Arial", "embedded": True}])
    monkeypatch.setattr(fontrepair, "detect", _detector_must_not_run)
    doc = recovery.recover(
        path, _result(status=quality.STATUS_EXTRACTED, reason="clean", text=UNICODE_NEPALI, ratio=0.0),
        pages=[UNICODE_NEPALI],
    )
    assert doc.plan == recovery.PLAN_NATIVE
    assert [(p.text, p.detail) for p in doc.pages] == [(UNICODE_NEPALI, {})]
    assert doc.warnings == ()


def test_flag_off_never_runs_the_detector_on_a_per_page_pdf(tmp_path, monkeypatch):
    from tests.test_nrb_recovery import PREETI, UNICODE_NEPALI, _result, _write_pdf

    path = _write_pdf(tmp_path, "pages.pdf", [
        {"font": "ABCDEE+Preeti", "embedded": True}, {"font": "Arial"},  # p2: no_font_provenance
    ])
    monkeypatch.setattr(fontrepair, "detect", _detector_must_not_run)
    doc = recovery.recover(path, _result(text=PREETI), pages=[PREETI, UNICODE_NEPALI])
    assert doc.plan == recovery.PLAN_PAGES
    assert doc.pages[1].route == recovery.ROUTE_NATIVE
    assert (doc.pages[1].text, doc.pages[1].detail) == (UNICODE_NEPALI, {})
    assert not any(w.startswith("tounicode_suspected") for w in doc.warnings)
