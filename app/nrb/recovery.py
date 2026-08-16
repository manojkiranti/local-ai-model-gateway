"""Production extraction routing: turn a classified blob into usable text.

Native-2 says whether a document's text can be trusted. This decides what to DO
about the ones that cannot, per page, and reconstructs the document in page order
with the route recorded beside every page.

    native/native-2
        └─ trustworthy native Unicode ................... keep

    PDF, high-confidence legacy candidate (unit_legacy_ratio >= 0.80)
        └─ per page:
             page carries a font ....................... guarded npttf2utf
             no font, pixels on the page ............... PP-OCRv5 OCR

    PDF, needs_ocr
        └─ per page: no usable text layer ............... PP-OCRv5 OCR

    XLSX, high-confidence legacy candidate .............. guarded npttf2utf, per CELL
    DOCX/TXT, high-confidence legacy candidate .......... guarded npttf2utf, per line

FOUR THINGS THIS MODULE IS CAREFUL ABOUT
----------------------------------------
**1. The gate is the validated one, and it is not re-derived.** Eligibility is
`status == suspicious/legacy_font_suspected` **and** `unit_legacy_ratio >= 0.80`
— native-2's OWN unit metric, never native-1's `legacy_line_ratio`. They are
different quantities: the three research workbooks the holdout caught sit at unit
ratio 0.969-0.993 while their line ratio is 0.15-0.19. Substituting the line
metric here would route a different population and silently undo §13.4. No
threshold is introduced, lowered or raised (§14.7, §15.9).

**2. Font provenance narrows the conversion route; it never widens it.** A page
inside an eligible document goes to the converter when it carries a font, and to
OCR when it carries pixels instead. A page in an INELIGIBLE document is never
converted because it happens to embed Preeti — that would widen npttf2utf
eligibility on font presence alone, which the validated queue semantics forbid.

**3. Absence of a recognised font NAME is not a scan.** `7820b1f49fc1`'s producer
stripped its font names to `/CIDFont+F1 … /CIDFont+F6` and its conversion is
good, so eligibility reads embedded font OBJECTS. `provenance.is_legacy_font_name`
is supporting evidence, and it also catches the opposite case — a page that
NAMES Preeti without embedding it still holds glyph-mapped bytes.

**4. A page that goes to OCR is never handed to npttf2utf.** Those bytes are a
scanner's latin-alphabet guess, not a glyph mapping; running a font converter
over them produces fluent nonsense that passes every validation rule the
converter has. The two routes are exclusive per page, and when OCR is
unavailable or fails the page fails CLOSED — empty text, `ok=False`, the reason
recorded — rather than falling back to the junk text layer or to the converter.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not classify (that is `routing.py`), does not persist (there is no
recovery table — Phase 7 owns storage), does not chunk or embed, and does not
decide semantic correctness. **Conversion correctness is still `awaiting_nepali_
review` (§15), and OCR text is retrieval material, not a transcription** — see
`ocr.py` on why there is no confidence number here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..files import documents as file_documents
from ..files import readers
from . import extraction, legacy_convert, provenance, quality
from .legacy_font import ConverterUnavailable, LegacyFontConverter
from .lexicon import Lexicon
from .ocr import OcrUnavailable, PageOcrEngine

logger = logging.getLogger("app.nrb.recovery")

__all__ = [
    "CONVERSION_GATE",
    "DocumentPlan",
    "PLANS",
    "PLAN_CONVERT",
    "PLAN_NATIVE",
    "PLAN_NONE",
    "PLAN_PAGES",
    "PageText",
    "ROUTES",
    "ROUTE_LEGACY",
    "ROUTE_NATIVE",
    "ROUTE_OCR",
    "RecoveredDocument",
    "plan_document",
    "recover",
    "route_page",
]

# --- the per-page routes ---------------------------------------------------- #
# A closed vocabulary, like `quality.REASONS`: these are counted, reported and
# will be persisted as citation provenance, and a typo'd route would vanish from
# every total while still looking like a processed page.
ROUTE_NATIVE = "native"                  # the extracted text, unchanged
ROUTE_LEGACY = "legacy_conversion"       # guarded npttf2utf, per line or per cell
ROUTE_OCR = "ocr"                        # PP-OCRv5, from pixels
ROUTES = (ROUTE_NATIVE, ROUTE_LEGACY, ROUTE_OCR)

# --- the document-level plans ----------------------------------------------- #
PLAN_NATIVE = "keep_native"      # nothing to recover; the text is what it is
PLAN_CONVERT = "convert_units"   # one text stream / one workbook, converted whole
PLAN_PAGES = "route_pages"       # a PDF, decided page by page
PLAN_NONE = "no_recovery"        # nothing this pipeline can do (yet)
PLANS = (PLAN_NATIVE, PLAN_CONVERT, PLAN_PAGES, PLAN_NONE)

# The validated high-confidence conversion gate, on native-2's `unit_legacy_ratio`
# (§14.2, §15). Frozen evidence: 56 documents routed at this threshold on an
# independent holdout, 0 English false positives, 52 recovering usable Unicode.
#
# It coincides numerically with `legacy_convert.UNJUDGED_MIN_LEGACY_RATIO` because
# both come from the same place — Phase 6A's own top severity band, where §11 says
# the text is "unusable throughout, not merely doubtful". They are kept as two
# constants because they decide two different things (which DOCUMENTS are
# eligible; which UNITS inside one may be converted unjudged), and tying them
# would make a future change to either silently change the other.
CONVERSION_GATE = 0.80

# A page in a `needs_ocr` document that holds at least this much text is left
# alone. `quality.MIN_CHARS_PER_PAGE` is the same floor native-1 and native-2 use
# to decide a PDF has a text layer at all — reused rather than restated so the
# router cannot disagree with the classifier that sent the document here.
MIN_PAGE_TEXT_CHARS = quality.MIN_CHARS_PER_PAGE


@dataclass(frozen=True)
class DocumentPlan:
    """What to do with one classified document, and the evidence for it."""

    plan: str
    reason: str
    # The `unit_legacy_ratio` actually read. Recorded even when it did not decide
    # anything, because "the gate was not met" and "there was no metric" are
    # different findings and a bare plan cannot distinguish them.
    gate_ratio: float | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PageText:
    """One page (or one sheet), its route, and what came out.

    `page_number` is 1-indexed and always the SOURCE page — `provenance`,
    `documents.read_pdf_pages` and `ocr.ocr_page` all use the same numbering, so
    a future citation can name a page without a translation table.
    """

    page_number: int
    route: str
    reason: str
    text: str
    ok: bool = True
    label: str | None = None          # sheet name, for spreadsheets
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "label": self.label,
            "route": self.route,
            "reason": self.reason,
            "ok": self.ok,
            "chars": len(self.text),
            "error": self.error,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RecoveredDocument:
    """One document's recovered text, page by page, in source order."""

    family: str
    plan: str
    plan_reason: str
    gate_ratio: float | None
    pages: tuple[PageText, ...]
    warnings: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        """The reconstructed document.

        Pages joined with a newline, in page order — the same shape
        `extraction.result_from_pages` produces, so the recovered text can be
        measured by `quality.measure_text` against the native text it replaces.
        Page IDENTITY is never merged away: it stays on `pages`, which is what a
        citation will read.
        """
        return "\n".join(page.text for page in self.pages)

    @property
    def ok(self) -> bool:
        return all(page.ok for page in self.pages)

    @property
    def route_counts(self) -> dict[str, int]:
        counts = {route: 0 for route in ROUTES}
        for page in self.pages:
            counts[page.route] = counts.get(page.route, 0) + 1
        return counts

    def as_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "plan": self.plan,
            "plan_reason": self.plan_reason,
            "gate_ratio": self.gate_ratio,
            "ok": self.ok,
            "route_counts": self.route_counts,
            "warnings": list(self.warnings),
            "pages": [page.as_dict() for page in self.pages],
        }


# --------------------------------------------------------------------------- #
# The decisions. Pure, so every rule below is testable without a file, a
# converter, an OCR model or a database.
# --------------------------------------------------------------------------- #
def plan_document(
    *,
    family: str,
    status: str,
    reason: str,
    metrics: dict[str, Any] | None,
) -> DocumentPlan:
    """The document-level decision. Ordered, first match wins.

    Reads native-2's verdict as given. It does not re-classify, does not consult
    the text, and cannot promote a document the classifier called clean — the
    only inputs are the status, the reason and the unit metric the gate is
    defined on.
    """
    warnings: list[str] = []
    ratio: float | None = None
    if metrics is not None and "unit_legacy_ratio" in metrics:
        try:
            ratio = float(metrics["unit_legacy_ratio"] or 0.0)
        except (TypeError, ValueError):
            ratio = None

    # 1. Nothing was extracted, or nothing can be.
    if status in (quality.STATUS_FAILED, quality.STATUS_UNSUPPORTED):
        return DocumentPlan(PLAN_NONE, reason, ratio)

    # 2. An image file. Docling can OCR one, but no image was in the measured
    #    cohort and routing a population this task never evaluated would be
    #    exactly the untested widening §15.9 warns against. Recorded, not done.
    if family == "image":
        return DocumentPlan(PLAN_NONE, "image_ocr_not_enabled", ratio)

    # 3. The text layer is absent or too thin to use. Page-routed for a PDF,
    #    because only some pages are usually affected.
    if status == quality.STATUS_NEEDS_OCR:
        if family == "pdf":
            return DocumentPlan(PLAN_PAGES, reason, ratio)
        return DocumentPlan(PLAN_NONE, f"{reason}_unsupported_family", ratio)

    # 4. The legacy-font candidate. The ONLY branch the conversion gate governs.
    if status == quality.STATUS_SUSPICIOUS and reason == "legacy_font_suspected":
        if ratio is None:
            # A native-1 row has no unit metric, and native-1's line ratio is not
            # a substitute for it (see the module docstring). Keep the text and
            # say why, rather than routing on the wrong quantity.
            warnings.append("no_unit_metrics")
            return DocumentPlan(
                PLAN_NATIVE, "no_unit_metrics", None, tuple(warnings)
            )
        if ratio < CONVERSION_GATE:
            return DocumentPlan(PLAN_NATIVE, "below_conversion_gate", ratio)
        if family == "pdf":
            return DocumentPlan(PLAN_PAGES, reason, ratio)
        if family in ("spreadsheet", "document", "text"):
            return DocumentPlan(PLAN_CONVERT, reason, ratio)
        return DocumentPlan(PLAN_NATIVE, f"{reason}_unsupported_family", ratio)

    # 5. Suspicious for some other reason (replacement characters, control
    #    characters, partial coverage, an empty workbook). Not this pipeline's
    #    problem: neither conversion nor OCR addresses any of them.
    return DocumentPlan(PLAN_NATIVE, reason, ratio, tuple(warnings))


def route_page(
    page: provenance.PageProvenance | None,
    *,
    plan_reason: str,
    text_chars: int,
) -> tuple[str, str]:
    """`(route, why)` for ONE page of a page-routed PDF.

    `page` is None when provenance could not be read. That fails CLOSED — the
    native text is kept — because an unopenable resource dictionary is not
    evidence of a scan, and both alternatives (convert blindly, OCR blindly) act
    on a guess.
    """
    if page is None:
        return ROUTE_NATIVE, "provenance_unavailable"

    if plan_reason == "legacy_font_suspected":
        # Rule 3 of the module docstring: font OBJECTS decide, names support.
        if page.has_embedded_font:
            return ROUTE_LEGACY, "embedded_font"
        if page.has_legacy_font_name:
            # Referenced but not embedded — the bytes are still glyph-mapped.
            return ROUTE_LEGACY, "legacy_font_referenced"
        if page.has_image:
            return ROUTE_OCR, "no_font_scan_backed"
        if text_chars == 0:
            return ROUTE_NATIVE, "empty_page"
        # Text, no font of its own, no pixels: nothing to invert and nothing to
        # read. Keeping it is the conservative answer.
        return ROUTE_NATIVE, "no_font_provenance"

    # A `needs_ocr` document: `no_text_layer` or `sparse_text_layer`.
    if text_chars >= MIN_PAGE_TEXT_CHARS and page.has_embedded_font:
        return ROUTE_NATIVE, "page_has_text_layer"
    if page.has_image:
        return ROUTE_OCR, plan_reason
    if text_chars >= MIN_PAGE_TEXT_CHARS:
        return ROUTE_NATIVE, "page_has_text_layer"
    return ROUTE_NATIVE, "empty_page"


# --------------------------------------------------------------------------- #
# Execution.
# --------------------------------------------------------------------------- #
def _converted_page(
    number: int,
    text: str,
    *,
    reason: str,
    converter: LegacyFontConverter | None,
    lexicon: Lexicon | None,
    document_legacy_ratio: float,
) -> PageText:
    """Run the guarded converter over one page's LINES.

    `document_legacy_ratio` is the DOCUMENT's `unit_legacy_ratio`, never a
    per-page recomputation. That is the validated semantic (§14, and
    `scripts/nrb_holdout_validate._doc_ratio`): the document's severity is what
    decides whether an unjudged unit — a heading, a date, a table cell — may be
    converted, and re-deriving it per page would gate page 1 of a 1.0-ratio
    document on its own three headings.
    """
    if converter is None or lexicon is None:
        return PageText(
            number, ROUTE_LEGACY, reason, text, ok=False,
            error="legacy font converter unavailable",
            detail={"converted_units": 0},
        )
    try:
        conversion = legacy_convert.convert_document(
            text, converter, lexicon, document_legacy_ratio=document_legacy_ratio
        )
    except ConverterUnavailable as exc:
        # The original survives. A converter that failed must never be
        # indistinguishable from one that no-oped.
        return PageText(
            number, ROUTE_LEGACY, reason, text, ok=False, error=str(exc),
            detail={"converted_units": 0},
        )
    return PageText(
        number,
        ROUTE_LEGACY,
        reason,
        conversion.text,
        detail={
            "converted_units": conversion.converted_lines,
            "mapping": conversion.mapping,
            "converter": f"{conversion.converter_name} {conversion.converter_version}",
            "counts": conversion.counts,
        },
    )


def _ocr_page(
    number: int, path: Path, *, reason: str, engine: PageOcrEngine | None
) -> PageText:
    """OCR one page, or fail closed.

    Failure yields EMPTY text, never the page's own hidden text layer. That layer
    is precisely why the page was routed here — a scanner's latin-alphabet guess
    — and emitting it would put the untrusted string back into the pipeline under
    a route that claims it was read from pixels.
    """
    if engine is None:
        return PageText(
            number, ROUTE_OCR, reason, "", ok=False,
            error="ocr engine unavailable",
        )
    try:
        text = engine.ocr_page(path, number)
    except OcrUnavailable as exc:
        return PageText(number, ROUTE_OCR, reason, "", ok=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - a pass must survive one bad page
        logger.warning("NRB recovery: OCR page %d failed (%s)", number, type(exc).__name__)
        return PageText(
            number, ROUTE_OCR, reason, "", ok=False, error=type(exc).__name__
        )
    return PageText(
        number,
        ROUTE_OCR,
        reason,
        text,
        detail={
            "engine": getattr(engine, "name", "unknown"),
            "model": getattr(engine, "model", "unknown"),
            "version": getattr(engine, "version", ""),
            # Stated on every OCR page, not only in the docs: this text is
            # retrieval material and is not authoritative for figures, dates or
            # contact details on a degraded scan.
            "authoritative": False,
        },
    )


def _recover_pdf(
    path: Path,
    plan: DocumentPlan,
    pages: Sequence[str],
    *,
    converter: LegacyFontConverter | None,
    lexicon: Lexicon | None,
    ocr: PageOcrEngine | None,
) -> tuple[tuple[PageText, ...], tuple[str, ...]]:
    """Route a PDF page by page and keep the pages in source order."""
    prov = provenance.read_pdf_provenance(path)
    warnings = list(plan.warnings)
    if prov.error:
        warnings.append(f"provenance_unavailable:{prov.error}")

    out: list[PageText] = []
    for index, text in enumerate(pages, start=1):
        route, why = route_page(
            prov.page(index), plan_reason=plan.reason, text_chars=len(text.strip())
        )
        if route == ROUTE_LEGACY:
            out.append(
                _converted_page(
                    index, text, reason=why, converter=converter, lexicon=lexicon,
                    document_legacy_ratio=plan.gate_ratio or 0.0,
                )
            )
        elif route == ROUTE_OCR:
            out.append(_ocr_page(index, path, reason=why, engine=ocr))
        else:
            out.append(PageText(index, ROUTE_NATIVE, why, text))
    return tuple(out), tuple(warnings)


def _recover_spreadsheet(
    path: Path,
    plan: DocumentPlan,
    *,
    converter: LegacyFontConverter | None,
    lexicon: Lexicon | None,
) -> tuple[tuple[PageText, ...], tuple[str, ...]]:
    """Convert a workbook PER CELL, one `PageText` per sheet.

    The grid is re-read from the workbook rather than recovered by splitting the
    stored `" | "`-joined text. That inverse only holds while no cell contains
    the sequence, and `|` is itself a Preeti codepoint mapping to `्र` — so the
    rendered row is unsafe both to score and to convert (§13.4). Cell boundaries
    come from openpyxl, and the row is re-rendered AFTER conversion.
    """
    if converter is None or lexicon is None:
        return (
            (
                PageText(
                    1, ROUTE_LEGACY, plan.reason, "", ok=False,
                    error="legacy font converter unavailable",
                ),
            ),
            plan.warnings,
        )

    sheets = readers.inspect_workbook(path)
    names: list[str | None] = [s.sheet_name for s in sheets] or [None]
    out: list[PageText] = []
    for number, name in enumerate(names, start=1):
        rows: list[tuple[str, ...]] = []
        seen = 0
        with readers.open_sheet_rows(path, sheet=name) as stream:
            if stream.headers:
                rows.append(tuple(str(h) for h in stream.headers))
            for row in stream.rows:
                if seen >= extraction.MAX_SHEET_ROWS:
                    break
                seen += 1
                if any(str(c).strip() for c in row):
                    rows.append(tuple(str(c) for c in row))
        try:
            conversion, grid = legacy_convert.convert_cells(
                rows, converter, lexicon,
                document_legacy_ratio=plan.gate_ratio or 0.0,
            )
        except ConverterUnavailable as exc:
            out.append(
                PageText(
                    number, ROUTE_LEGACY, plan.reason,
                    "\n".join(" | ".join(r) for r in rows),
                    ok=False, label=name, error=str(exc),
                )
            )
            continue
        out.append(
            PageText(
                number,
                ROUTE_LEGACY,
                plan.reason,
                "\n".join(" | ".join(r) for r in grid),
                label=name,
                detail={
                    "converted_units": conversion.converted_lines,
                    "mapping": conversion.mapping,
                    "cells": sum(len(r) for r in rows),
                    "counts": conversion.counts,
                },
            )
        )
    return tuple(out), plan.warnings


def recover(
    path: Path,
    result: extraction.ExtractionResult,
    *,
    converter: LegacyFontConverter | None = None,
    lexicon: Lexicon | None = None,
    ocr: PageOcrEngine | None = None,
    pages: Sequence[str] | None = None,
) -> RecoveredDocument:
    """Route one classified blob and return its recovered text. NEVER raises.

    `result` must come from an extractor version that produces unit metrics
    (`native-2`); a native-1 result is kept as-is with a `no_unit_metrics`
    warning rather than routed on native-1's different metric.

    `pages` lets a caller that already has the per-page text supply it. When it
    is absent the PDF is re-read, deliberately: `result.text` is
    `"\\n".join(pages)` and `str.splitlines()` is NOT its inverse — it also
    breaks on form feeds and lone `\\r`, which nine holdout PDFs contain, so
    recovering pages by splitting would silently mis-attribute lines to pages
    (the same trap `scripts/nrb_holdout_evidence.py` documents).

    Every dependency is injected. The converter is GPL-3 and the OCR engine pulls
    a model stack; passing `None` for either is a supported state that degrades
    to a recorded failure on the affected pages, never to a wrong answer.
    """
    path = Path(path)
    plan = plan_document(
        family=result.family,
        status=result.status,
        reason=result.reason,
        metrics=result.metrics,
    )

    if plan.plan == PLAN_NONE:
        return RecoveredDocument(
            result.family, plan.plan, plan.reason, plan.gate_ratio, (), plan.warnings
        )

    if plan.plan == PLAN_PAGES:
        try:
            page_texts = list(pages) if pages is not None else list(
                file_documents.read_pdf_pages(path).pages
            )
        except Exception as exc:  # noqa: BLE001 - the blob was parsed once already
            return RecoveredDocument(
                result.family, PLAN_NATIVE, "reread_failed", plan.gate_ratio,
                (PageText(1, ROUTE_NATIVE, "reread_failed", result.text, ok=False,
                          error=type(exc).__name__),),
                plan.warnings,
            )
        routed, warnings = _recover_pdf(
            path, plan, page_texts, converter=converter, lexicon=lexicon, ocr=ocr
        )
        return RecoveredDocument(
            result.family, plan.plan, plan.reason, plan.gate_ratio, routed, warnings
        )

    if plan.plan == PLAN_CONVERT:
        if result.family == "spreadsheet":
            try:
                routed, warnings = _recover_spreadsheet(
                    path, plan, converter=converter, lexicon=lexicon
                )
            except Exception as exc:  # noqa: BLE001 - one bad workbook, recorded
                logger.warning(
                    "NRB recovery: workbook re-read failed (%s)", type(exc).__name__
                )
                routed = (
                    PageText(1, ROUTE_LEGACY, plan.reason, result.text, ok=False,
                             error=type(exc).__name__),
                )
                warnings = plan.warnings
            return RecoveredDocument(
                result.family, plan.plan, plan.reason, plan.gate_ratio, routed, warnings
            )
        page = _converted_page(
            1, result.text, reason=plan.reason, converter=converter, lexicon=lexicon,
            document_legacy_ratio=plan.gate_ratio or 0.0,
        )
        return RecoveredDocument(
            result.family, plan.plan, plan.reason, plan.gate_ratio, (page,),
            plan.warnings,
        )

    # PLAN_NATIVE. A PDF still reports per page, so page identity survives for a
    # citation even when nothing was recovered.
    if result.family == "pdf":
        page_texts = list(pages) if pages is not None else None
        if page_texts is None:
            try:
                page_texts = list(file_documents.read_pdf_pages(path).pages)
            except Exception:  # noqa: BLE001 - fall back to the flat text
                page_texts = None
        if page_texts is not None:
            return RecoveredDocument(
                result.family, plan.plan, plan.reason, plan.gate_ratio,
                tuple(
                    PageText(i, ROUTE_NATIVE, plan.reason, text)
                    for i, text in enumerate(page_texts, start=1)
                ),
                plan.warnings,
            )
    return RecoveredDocument(
        result.family, plan.plan, plan.reason, plan.gate_ratio,
        (PageText(1, ROUTE_NATIVE, plan.reason, result.text),),
        plan.warnings,
    )
