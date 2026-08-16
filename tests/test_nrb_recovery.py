"""Phase 6B production extraction routing: provenance, page routes, fail-closed.

Offline and deterministic. No database, no network, no corpus blob and no model:
the PDFs are assembled byte by byte in this file so a page's font and image
provenance is *stated* rather than inherited from a fixture nobody can read, and
the converter and the OCR engine arrive as stubs — which is the point of both
being Protocols.

WHAT THESE TESTS ARE FOR
    The routing rules are cheap to state and expensive to get wrong, and three of
    them are counter-intuitive enough that the evidence had to be measured
    first:

      * a page whose font names were stripped by the producer is NOT a scan
        (`7820b1f49fc1` converts correctly),
      * a scan's hidden text layer must never reach npttf2utf (it is a latin
        alphabet OCR guess, and the converter would turn it into fluent
        nonsense that passes every validation rule),
      * one document can need both routes (`e08988860534`: page 1 a 300 dpi
        scan, pages 2-50 embedded Preeti).

    Each has a test below named after the case that produced it.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
from typing import Sequence

import pytest

from app.nrb import extraction, lexicon as LX, provenance, quality, recovery
from app.nrb.ocr import OcrUnavailable

REPO = pathlib.Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------- #
# Fixtures: PDFs whose provenance is written out in full.
# --------------------------------------------------------------------------- #
def _pdf_bytes(pages: Sequence[dict]) -> bytes:
    """A minimal, valid PDF whose pages declare exactly the resources asked for.

    Hand-assembled rather than produced by a library because the thing under test
    IS the resource dictionary: `{"font": "Preeti", "embedded": True}` has to mean
    a `/FontDescriptor` with a `/FontFile2`, visibly, in the test that relies on
    it. Nothing here draws anything — page TEXT is supplied to `recover()`
    separately, so text and provenance stay independent variables.
    """
    objects: list[str] = ["", ""]  # 1 = catalog, 2 = page tree; filled at the end

    def add(body: str) -> int:
        objects.append(body)
        return len(objects)

    kids: list[int] = []
    for spec in pages:
        resources: list[str] = []
        if spec.get("font"):
            descriptor = ""
            if spec.get("embedded"):
                stub = add("<< /Length 4 >>\nstream\nSTUB\nendstream")
                number = add(
                    f"<< /Type /FontDescriptor /FontName /{spec['font']} "
                    f"/FontFile2 {stub} 0 R >>"
                )
                descriptor = f" /FontDescriptor {number} 0 R"
            font = add(
                f"<< /Type /Font /Subtype /TrueType /BaseFont /{spec['font']}"
                f"{descriptor} >>"
            )
            resources.append(f"/Font << /F1 {font} 0 R >>")
        if spec.get("image"):
            image = add(
                "<< /Type /XObject /Subtype /Image /Width 2480 /Height 3507 "
                "/ColorSpace /DeviceGray /BitsPerComponent 8 /Length 4 >>\n"
                "stream\n0000\nendstream"
            )
            resources.append(f"/XObject << /Im0 {image} 0 R >>")
        contents = add("<< /Length 0 >>\nstream\n\nendstream")
        kids.append(
            add(
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << {' '.join(resources)} >> /Contents {contents} 0 R >>"
            )
        )

    objects[0] = "<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = (
        f"<< /Type /Pages /Count {len(kids)} "
        f"/Kids [{' '.join(f'{k} 0 R' for k in kids)}] >>"
    )

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{start}\n%%EOF\n"
    ).encode()
    return bytes(out)


def _write_pdf(tmp_path: pathlib.Path, name: str, pages: Sequence[dict]) -> pathlib.Path:
    path = tmp_path / name
    path.write_bytes(_pdf_bytes(pages))
    return path


# --- text fixtures, copied from the real cohort ----------------------------- #
# `041902065a1d` — genuine Preeti.
PREETI = "g]kfn /fi6« a}+ssf] k|of]hgsf] nflu dfq"
# `3d2eca8b9f95` page 1 — a 300 dpi scan's HIDDEN text layer. Not Preeti: a
# scanner's latin-alphabet guess at Devanagari. It is what `native-2` correctly
# calls suspicious and what the converter must never be handed.
SCAN_JUNK = "e al viht Hjale hle e? Uest lohe Mh Ery lnre Mih hpIk pePere beolnjie"
# `075bf12eb087` — real Unicode Devanagari.
UNICODE_NEPALI = (
    "सम्पत्ति शुद्धीकरण निवारण ऐन, २०६४ र सो अन्तर्गत बनेका नियमावलीहरुको "
    "प्रभावकारी कार्यान्वयन गर्न"
)
ENGLISH = (
    "The Bank shall issue a circular to all licensed institutions and every "
    "bank shall report its exposure within thirty days of the quarter end."
)


@pytest.fixture(scope="module")
def lexicon() -> LX.Lexicon:
    english = frozenset(
        """the bank shall issue circular all licensed institutions and every
        report its exposure within thirty days quarter end""".split()
    )
    nepali = frozenset("नेपाल राष्ट्र बैंक बैंकको प्रयोजनको लागि मात्र".split())
    return LX.Lexicon(
        version=LX.LEXICON_VERSION,
        english=english,
        nepali=nepali,
        fingerprint=LX.lexicon_fingerprint(LX.LEXICON_VERSION, english, nepali),
        provenance={"source": "test fixture"},
    )


class StubConverter:
    """A `LegacyFontConverter` that MARKS instead of converting.

    Deliberately not npttf2utf: these tests assert which units reached a
    converter and which did not, and a stub makes that visible without pulling a
    GPL-3 dependency into the routing suite. `legacy_convert`'s own correctness
    is covered by `test_nrb_legacy_conversion.py` against real Preeti.
    """

    name = "stub"
    mapping = "Preeti"
    version = "0.0-test"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def convert(self, text: str) -> str:
        self.calls.append(text)
        # Devanagari, one character per input character. Proportionate on
        # purpose: `validate_conversion` rejects a conversion that shrank or
        # exploded, so a fixed-length stub would make short cells fail
        # validation and the tests would measure the stub instead of the route.
        return "".join(ch if ch.isspace() else "क" for ch in text)


class StubOcr:
    name = "stub-ocr"
    model = "PP-OCRv5"
    version = "test"

    def __init__(self, *, fail: bool = False) -> None:
        self.pages: list[int] = []
        self.fail = fail

    def ocr_page(self, path, page_number: int) -> str:
        self.pages.append(page_number)
        if self.fail:
            raise OcrUnavailable("model unavailable")
        return f"विदेशी विनिमय व्यवस्थापन विभाग (page {page_number})"


def _result(
    *,
    family: str = "pdf",
    status: str = quality.STATUS_SUSPICIOUS,
    reason: str = "legacy_font_suspected",
    text: str = "",
    ratio: float | None = 1.0,
) -> extraction.ExtractionResult:
    """An `ExtractionResult` with only the fields routing reads set meaningfully."""
    metrics: dict = {} if ratio is None else {"unit_legacy_ratio": ratio}
    return extraction.ExtractionResult(
        parser="pypdf", family=family, status=status, reason=reason, warnings=(),
        text=text, page_count=None, pages_with_text=None, char_count=len(text),
        devanagari_ratio=None, text_page_coverage=None, metrics=metrics,
        preview=text[:300], error=None, duration_ms=0,
    )


# --------------------------------------------------------------------------- #
# 1. Provenance, read from the PDF itself.
# --------------------------------------------------------------------------- #
def test_provenance_separates_an_embedded_font_from_a_scan(tmp_path):
    path = _write_pdf(
        tmp_path, "mixed.pdf",
        [{"font": "Helvetica", "embedded": False, "image": True},
         {"font": "ABCDEE+Preeti", "embedded": True}],
    )
    prov = provenance.read_pdf_provenance(path)

    assert prov.error is None and prov.page_count == 2
    scan, text_page = prov.pages
    assert not scan.has_embedded_font and scan.has_image and scan.scan_backed
    assert text_page.has_embedded_font and not text_page.scan_backed
    assert text_page.legacy_font_names == ("/ABCDEE+Preeti",)


def test_a_logo_on_a_font_embedded_page_is_not_a_scan(tmp_path):
    """`268bcfe86d03`: an embedded Preeti circular with the bank's logo on it.

    `scan_backed` is "no font of its own AND pixels", never "has an image" — the
    weaker rule would send a perfectly convertible circular to OCR.
    """
    path = _write_pdf(tmp_path, "logo.pdf", [{"font": "FNNOBH+Preeti", "embedded": True, "image": True}])
    page = provenance.read_pdf_provenance(path).pages[0]
    assert page.has_image and page.has_embedded_font and not page.scan_backed


def test_a_stripped_font_name_is_still_an_embedded_font(tmp_path):
    """`7820b1f49fc1` — the producer emitted `/CIDFont+F1 … /F6` and the
    deterministic conversion of that document is good. Name recognition must not
    be a precondition."""
    path = _write_pdf(tmp_path, "stripped.pdf", [{"font": "CIDFont+F1", "embedded": True}])
    page = provenance.read_pdf_provenance(path).pages[0]
    assert page.has_embedded_font
    assert page.legacy_font_names == ()          # nothing recognisable
    assert not provenance.is_legacy_font_name("/CIDFont+F1")


def test_unreadable_pdf_yields_no_provenance_rather_than_raising(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(b"not a pdf at all")
    prov = provenance.read_pdf_provenance(path)
    assert prov.pages == () and prov.error is not None
    assert prov.page(1) is None


# --------------------------------------------------------------------------- #
# 2. The document-level plan — the validated gate, unchanged.
# --------------------------------------------------------------------------- #
def test_the_conversion_gate_is_the_validated_value():
    """0.80 on native-2's `unit_legacy_ratio` (§14.2, §15). Locked because the
    whole holdout is evidence for THIS number."""
    assert recovery.CONVERSION_GATE == 0.80


def test_a_clean_document_is_never_routed():
    plan = recovery.plan_document(
        family="pdf", status=quality.STATUS_EXTRACTED, reason="clean",
        metrics={"unit_legacy_ratio": 0.0},
    )
    assert plan.plan == recovery.PLAN_NATIVE


def test_a_flagged_document_below_the_gate_keeps_its_text():
    """The §14.3 English accounting templates sit at 0.48-0.54. They are flagged
    and they are NOT eligible — that is the queue semantic the holdout measured,
    and provenance does not widen it."""
    plan = recovery.plan_document(
        family="pdf", status=quality.STATUS_SUSPICIOUS,
        reason="legacy_font_suspected", metrics={"unit_legacy_ratio": 0.54},
    )
    assert plan.plan == recovery.PLAN_NATIVE
    assert plan.reason == "below_conversion_gate"


def test_a_native1_result_is_not_routed_on_the_line_metric():
    """native-1 rows carry no `unit_legacy_ratio`, and `legacy_line_ratio` is a
    different quantity (0.15-0.19 on the three workbooks native-2 routes at
    0.969-0.993). Absent metric means keep, loudly."""
    plan = recovery.plan_document(
        family="pdf", status=quality.STATUS_SUSPICIOUS,
        reason="legacy_font_suspected", metrics={"legacy_line_ratio": 0.95},
    )
    assert plan.plan == recovery.PLAN_NATIVE
    assert plan.reason == "no_unit_metrics" and "no_unit_metrics" in plan.warnings


def test_needs_ocr_pdf_is_page_routed():
    plan = recovery.plan_document(
        family="pdf", status=quality.STATUS_NEEDS_OCR, reason="no_text_layer",
        metrics={"unit_legacy_ratio": 0.0},
    )
    assert plan.plan == recovery.PLAN_PAGES and plan.reason == "no_text_layer"


def test_unsupported_and_failed_documents_get_no_recovery():
    for status in (quality.STATUS_UNSUPPORTED, quality.STATUS_FAILED):
        plan = recovery.plan_document(
            family="office_legacy", status=status, reason="no_native_parser",
            metrics={},
        )
        assert plan.plan == recovery.PLAN_NONE


# --------------------------------------------------------------------------- #
# 3. The per-page route.
# --------------------------------------------------------------------------- #
def _page(**kw) -> provenance.PageProvenance:
    base = dict(
        page_number=1, fonts=0, embedded_fonts=0, legacy_font_names=(),
        font_names=(), images=0, largest_image_pixels=0,
    )
    base.update(kw)
    return provenance.PageProvenance(**base)


def test_a_font_bearing_page_goes_to_the_converter():
    route, why = recovery.route_page(
        _page(fonts=1, embedded_fonts=1), plan_reason="legacy_font_suspected",
        text_chars=900,
    )
    assert (route, why) == (recovery.ROUTE_LEGACY, "embedded_font")


def test_a_referenced_but_unembedded_legacy_font_still_converts():
    """The bytes are glyph-mapped whether or not the producer shipped the font."""
    route, _ = recovery.route_page(
        _page(fonts=1, legacy_font_names=("/Preeti",), images=1),
        plan_reason="legacy_font_suspected", text_chars=900,
    )
    assert route == recovery.ROUTE_LEGACY


def test_a_scan_page_goes_to_ocr_not_the_converter():
    route, why = recovery.route_page(
        _page(fonts=1, font_names=("/Helvetica",), images=1),
        plan_reason="legacy_font_suspected", text_chars=2083,
    )
    assert (route, why) == (recovery.ROUTE_OCR, "no_font_scan_backed")


def test_missing_provenance_fails_closed_to_native():
    route, why = recovery.route_page(
        None, plan_reason="legacy_font_suspected", text_chars=900
    )
    assert (route, why) == (recovery.ROUTE_NATIVE, "provenance_unavailable")


def test_a_needs_ocr_page_with_a_real_text_layer_is_left_alone():
    route, _ = recovery.route_page(
        _page(fonts=1, embedded_fonts=1), plan_reason="no_text_layer", text_chars=900
    )
    assert route == recovery.ROUTE_NATIVE


def test_a_needs_ocr_page_of_pixels_goes_to_ocr():
    route, why = recovery.route_page(
        _page(images=1), plan_reason="no_text_layer", text_chars=0
    )
    assert (route, why) == (recovery.ROUTE_OCR, "no_text_layer")


# --------------------------------------------------------------------------- #
# 4. End to end, one document at a time.
# --------------------------------------------------------------------------- #
def test_clean_native_pdf_comes_back_unchanged(tmp_path, lexicon):
    path = _write_pdf(tmp_path, "clean.pdf", [{"font": "Arial", "embedded": True}])
    converter = StubConverter()
    recovered = recovery.recover(
        path,
        _result(status=quality.STATUS_EXTRACTED, reason="clean", text=ENGLISH, ratio=0.0),
        converter=converter, lexicon=lexicon, ocr=StubOcr(), pages=[ENGLISH],
    )
    assert recovered.plan == recovery.PLAN_NATIVE
    assert recovered.route_counts[recovery.ROUTE_NATIVE] == 1
    assert recovered.text == ENGLISH
    assert converter.calls == []          # nothing was handed to a converter


def test_embedded_legacy_font_pdf_routes_to_the_converter(tmp_path, lexicon):
    path = _write_pdf(tmp_path, "preeti.pdf", [{"font": "ABCDEE+Preeti", "embedded": True}])
    converter = StubConverter()
    ocr = StubOcr()
    recovered = recovery.recover(
        path, _result(text=PREETI), converter=converter, lexicon=lexicon, ocr=ocr,
        pages=[PREETI],
    )
    assert recovered.pages[0].route == recovery.ROUTE_LEGACY
    assert PREETI in converter.calls      # the raw line reached the converter
    assert ocr.pages == []                # and OCR was never asked


def test_a_scan_page_is_ocred_and_its_junk_layer_never_reaches_the_converter(
    tmp_path, lexicon
):
    """The rule the OCR spike bought. `3d2eca8b9f95`'s hidden layer is a latin
    alphabet OCR guess; npttf2utf would turn it into fluent Devanagari nonsense
    that passes every validation rule the converter has."""
    path = _write_pdf(tmp_path, "scan.pdf", [{"font": "Helvetica", "image": True}])
    converter = StubConverter()
    ocr = StubOcr()
    recovered = recovery.recover(
        path, _result(text=SCAN_JUNK), converter=converter, lexicon=lexicon, ocr=ocr,
        pages=[SCAN_JUNK],
    )
    page = recovered.pages[0]
    assert page.route == recovery.ROUTE_OCR and page.ok
    assert ocr.pages == [1]
    assert converter.calls == []
    assert SCAN_JUNK not in recovered.text
    assert page.detail["authoritative"] is False


def test_needs_ocr_document_ocrs_only_its_empty_pages(tmp_path, lexicon):
    path = _write_pdf(
        tmp_path, "partial-scan.pdf",
        [{"font": "ABCDEE+Preeti", "embedded": True}, {"image": True}],
    )
    ocr = StubOcr()
    recovered = recovery.recover(
        path,
        _result(status=quality.STATUS_NEEDS_OCR, reason="no_text_layer",
                text=ENGLISH, ratio=0.0),
        converter=StubConverter(), lexicon=lexicon, ocr=ocr, pages=[ENGLISH, ""],
    )
    assert [p.route for p in recovered.pages] == [
        recovery.ROUTE_NATIVE, recovery.ROUTE_OCR
    ]
    assert ocr.pages == [2]


def test_a_mixed_pdf_routes_page_by_page_and_keeps_page_order(tmp_path, lexicon):
    """`e08988860534`, the canonical case: page 1 is a 300 dpi scan inside a
    document whose later pages embed real Preeti."""
    path = _write_pdf(
        tmp_path, "e08988860534.pdf",
        [{"font": "Helvetica", "image": True},
         {"font": "ABCDEE+Preeti", "embedded": True},
         {"font": "ABCDEE+Preeti", "embedded": True}],
    )
    converter, ocr = StubConverter(), StubOcr()
    recovered = recovery.recover(
        path, _result(text="\n".join([SCAN_JUNK, PREETI, PREETI])),
        converter=converter, lexicon=lexicon, ocr=ocr,
        pages=[SCAN_JUNK, PREETI, PREETI],
    )

    assert [p.page_number for p in recovered.pages] == [1, 2, 3]
    assert [p.route for p in recovered.pages] == [
        recovery.ROUTE_OCR, recovery.ROUTE_LEGACY, recovery.ROUTE_LEGACY
    ]
    assert ocr.pages == [1]
    assert converter.calls == [PREETI, PREETI]
    # Page identity survives reconstruction: the OCR page is first, and its text
    # is the OCR engine's, not the junk layer's.
    assert recovered.text.splitlines()[0].startswith("विदेशी")
    assert recovered.route_counts == {
        recovery.ROUTE_NATIVE: 0, recovery.ROUTE_LEGACY: 2, recovery.ROUTE_OCR: 1
    }


def test_stripped_font_names_stay_eligible_for_conversion(tmp_path, lexicon):
    """`7820b1f49fc1` end to end: no recognisable family name anywhere, embedded
    fonts on every page, and it must still convert rather than fall to OCR."""
    path = _write_pdf(
        tmp_path, "7820b1f49fc1.pdf",
        [{"font": "CIDFont+F1", "embedded": True, "image": True}],
    )
    converter, ocr = StubConverter(), StubOcr()
    recovered = recovery.recover(
        path, _result(text=PREETI), converter=converter, lexicon=lexicon, ocr=ocr,
        pages=[PREETI],
    )
    assert recovered.pages[0].route == recovery.ROUTE_LEGACY
    assert ocr.pages == []


def test_unicode_devanagari_is_never_passed_through_the_converter(tmp_path, lexicon):
    """Guard 1 of `legacy_convert`, asserted at the ROUTING level.

    The converter is not a no-op on correct Devanagari — it turns
    `(मनी लाउन्डररङ)` into `९मनी लाउन्डररङ०` while raising the Devanagari ratio,
    so nothing downstream would catch it. A Unicode line inside a routed document
    must come back byte-identical.
    """
    path = _write_pdf(tmp_path, "unicode.pdf", [{"font": "ABCDEE+Preeti", "embedded": True}])
    converter = StubConverter()
    recovered = recovery.recover(
        path, _result(text=UNICODE_NEPALI), converter=converter, lexicon=lexicon,
        ocr=StubOcr(), pages=[UNICODE_NEPALI],
    )
    assert recovered.pages[0].route == recovery.ROUTE_LEGACY   # the page WAS routed
    assert converter.calls == []                               # the line was not
    assert recovered.text == UNICODE_NEPALI


def test_ocr_failure_fails_closed(tmp_path, lexicon):
    """No text, an explicit error, and the junk layer is not substituted back in.

    The alternative failure modes are both silent: emitting the hidden layer
    would put untrusted text back under a route that claims pixels, and handing
    the page to npttf2utf would produce confident nonsense.
    """
    path = _write_pdf(tmp_path, "scan.pdf", [{"font": "Helvetica", "image": True}])
    converter = StubConverter()
    recovered = recovery.recover(
        path, _result(text=SCAN_JUNK), converter=converter, lexicon=lexicon,
        ocr=StubOcr(fail=True), pages=[SCAN_JUNK],
    )
    page = recovered.pages[0]
    assert page.route == recovery.ROUTE_OCR
    assert page.ok is False and page.text == ""
    assert "model unavailable" in (page.error or "")
    assert converter.calls == []
    assert recovered.ok is False


def test_a_missing_ocr_engine_is_a_recorded_failure_not_a_crash(tmp_path, lexicon):
    path = _write_pdf(tmp_path, "scan.pdf", [{"font": "Helvetica", "image": True}])
    recovered = recovery.recover(
        path, _result(text=SCAN_JUNK), converter=StubConverter(), lexicon=lexicon,
        ocr=None, pages=[SCAN_JUNK],
    )
    assert recovered.pages[0].ok is False
    assert "ocr engine unavailable" in (recovered.pages[0].error or "")


def test_a_missing_converter_keeps_the_original_text(tmp_path, lexicon):
    """npttf2utf is GPL-3 and may legitimately be absent. The page keeps its
    original bytes and says so, rather than emptying itself."""
    path = _write_pdf(tmp_path, "preeti.pdf", [{"font": "ABCDEE+Preeti", "embedded": True}])
    recovered = recovery.recover(
        path, _result(text=PREETI), converter=None, lexicon=None, ocr=StubOcr(),
        pages=[PREETI],
    )
    page = recovered.pages[0]
    assert page.route == recovery.ROUTE_LEGACY and page.ok is False
    assert page.text == PREETI


# --------------------------------------------------------------------------- #
# 5. Spreadsheets: the existing per-CELL conversion, preserved.
# --------------------------------------------------------------------------- #
def test_spreadsheet_legacy_cells_convert_per_cell_never_per_rendered_row(
    tmp_path, lexicon
):
    """`|` is a Preeti codepoint mapping to `्र`, and `extraction.py` renders a row
    as `" | ".join(cells)`. The grid must come from the workbook, so a cell that
    itself contains `" | "` cannot be split into two units (§13.4)."""
    openpyxl = pytest.importorskip("openpyxl")
    path = tmp_path / "legacy.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "circular"
    sheet.append(["g]kfn", "/fi6«"])
    sheet.append(["a}+s | zfvf", 1234.5])
    book.save(path)

    converter = StubConverter()
    recovered = recovery.recover(
        path, _result(family="spreadsheet", text=""), converter=converter,
        lexicon=lexicon, ocr=StubOcr(),
    )

    assert recovered.plan == recovery.PLAN_CONVERT
    assert recovered.pages[0].label == "circular"
    assert recovered.pages[0].route == recovery.ROUTE_LEGACY
    # Every unit the converter saw is ONE cell. The `|`-bearing cell arrived
    # whole rather than as two units, and no call is a rendered row.
    assert "a}+s | zfvf" in converter.calls
    assert "g]kfn" in converter.calls and "/fi6«" in converter.calls
    assert "g]kfn | /fi6«" not in converter.calls
    # A numeric cell is not a conversion candidate: Preeti maps ASCII digits to
    # Devanagari digits, so `1234.5` would convert to something that passes every
    # validation rule while destroying a number.
    assert "1234.5" not in converter.calls
    assert recovered.pages[0].detail["converted_units"] == 3


# --------------------------------------------------------------------------- #
# 6. The dependency boundary.
# --------------------------------------------------------------------------- #
def test_the_ocr_stack_is_worker_only():
    """The API image installs `requirements.txt` and nothing else, so keeping the
    OCR packages out of it is structural rather than a convention — the same
    guarantee `requirements-nrb.txt` gives npttf2utf."""
    api = (REPO / "requirements.txt").read_text().lower()
    worker = (REPO / "requirements-worker.txt").read_text().lower()
    dockerfile = (REPO / "Dockerfile").read_text()

    for package in ("onnxruntime", "rapidocr", "docling"):
        assert package not in api, f"{package} must not be in the API image"
        assert package in worker

    assert "requirements-worker.txt" not in dockerfile
    assert "requirements-nrb.txt" not in dockerfile
    assert "COPY requirements.txt ." in dockerfile


def test_importing_the_router_does_not_import_docling_or_onnxruntime():
    """A SUBPROCESS check, because `sys.modules` is process-global and another
    test may legitimately have imported docling already. Same pattern as
    `test_docling_is_not_imported_at_module_scope`."""
    code = (
        "import sys; import app.nrb.recovery, app.nrb.ocr, app.nrb.provenance; "
        "leaked=[m for m in ('docling','torch','onnxruntime','rapidocr','npttf2utf') "
        "if m in sys.modules]; "
        "print(leaked); sys.exit(1 if leaked else 0)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True
    )
    assert proc.returncode == 0, f"leaked into the import path: {proc.stdout.strip()}"
