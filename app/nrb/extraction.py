"""Native extraction of a fetched NRB blob. Local files only — no DB, no network.

Phase 5 put bytes on disk and deliberately parsed none of them. This is the
parse, and it stops at a classified result: no chunk, no embedding, no
`documents` row, no OCR.

WHICH PARSER, AND WHY NOT DOCLING
    `pypdf` for PDFs, measured at ~41 pages/s on the fetched corpus against
    Docling's ~1-2 on CPU. Both read the same embedded text layer to answer the
    same question — "is there trustworthy text here" — and Docling's real value
    (layout analysis, table structure, `prov[0].page_no`) is what Phase 7 needs
    for CHUNKING, not what Phase 6A needs for screening. `docling_extract` (added
    in Task 11) is the bounded calibration that keeps that claim honest rather
    than asserted.

    Everything else is reused rather than reimplemented: `.docx` through
    `app/files/documents.py`, spreadsheets through `app/files/readers.py` — the
    same normalizers `read_document`, `read_excel` and `app/rag/parsing.py`
    already use. A second document stack would drift from the tools that ship.

WHAT IS DELIBERATELY NOT PARSED
    `.xls` and `.doc` (324 files, 1.8% of the corpus): openpyxl cannot read OLE2
    and nothing here reads legacy Word. They are `unsupported`, counted and sized,
    so Phase 6B can price xlrd/antiword against a real number. Images are
    `needs_ocr` — a valid file whose text is pixels, never a failure.

UNTRUSTED INPUT
    Every blob came off the public internet. No formula is evaluated
    (`data_only=True`, inherited from `readers.py`), no macro runs, nothing is
    shelled out to, and no error message carries a filesystem path into the
    database.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..files import documents as file_documents
from ..files import readers
from ..files.readers import ReadError
from . import quality

logger = logging.getLogger("app.nrb.extraction")

__all__ = [
    "DoclingEngine",
    "EXTRACTOR_VERSION",
    "ExtractionResult",
    "MAX_EXTRACT_BYTES",
    "MAX_SHEET_ROWS",
    "PREVIEW_CHARS",
    "docling_extract",
    "docling_pipeline_is_native",
    "extract_file",
    "result_from_pages",
]

# Bumped BY HAND when a parser or a classification rule changes. It is half of
# `nrb_extractions`'s unique key, so bumping it makes every stored result stale
# and re-extractable without deleting anything. Deliberately not derived from a
# library version: a pypdf patch release does not invalidate a corpus, and a
# threshold change in `quality.py` does.
EXTRACTOR_VERSION = "native-1"

# native-2 is a CLASSIFIER change, not a parser change. Every extractor below is
# untouched: the same pypdf/python-docx/openpyxl call, the same text, the same
# `quality` metrics. What differs is which classifier reads the evidence, and that
# native-2 is additionally handed the document's judgment UNITS — lines for text,
# CELLS for a spreadsheet. See `app/nrb/routing.py`.
#
# The dispatch is by version rather than by a flag so a row can never be ambiguous
# about which rules produced it: identity is (content_sha256, extractor_version),
# and both versions' rows sit side by side for comparison.
SUPPORTED_EXTRACTOR_VERSIONS = (EXTRACTOR_VERSION, "native-2")

# A sanity-check window for a human reading the report, not a cached artefact.
# NO extracted text is persisted beyond this: Phase 7 re-parses with Docling for
# chunking, and a stored text blob is something a later phase would eventually
# embed by accident.
PREVIEW_CHARS = 300

# Above this, a file is recorded rather than loaded. The largest blob measured
# live is 46 MB; this bounds one process's memory regardless.
MAX_EXTRACT_BYTES = 96 * 1024 * 1024

# Bounds spreadsheet scanning, matching `aggregate.MAX_SCAN_ROWS`' intent: report
# a partial measurement rather than refusing a large workbook.
MAX_SHEET_ROWS = 20_000

# Extensions with no native parser in this dependency set, whatever the sniffer
# said. `sniff` degrades to `unknown` for a body it cannot type from its head, and
# routing a legacy `.xls` into openpyxl on that basis would raise rather than
# report an honest `unsupported`.
LEGACY_OFFICE_EXTENSIONS = frozenset({"xls", "doc", "ppt"})

# An unsniffable body whose extension is unambiguous. `sniff` answering `unknown`
# is a real answer, not a failure, so the extension is allowed to break the tie —
# but only toward formats we can actually parse.
_EXTENSION_FAMILIES = {
    "pdf": "pdf",
    "docx": "document",
    "xlsx": "spreadsheet",
    "csv": "spreadsheet",
}


@dataclass(frozen=True)
class ExtractionResult:
    """One blob's extraction. Maps 1:1 onto an `nrb_extractions` row.

    Every field is a function of the BYTES alone — no title, no URL, no document
    type. See `quality.py`'s module docstring for why that is load-bearing.
    """

    parser: str                       # pypdf | python-docx | openpyxl | text | none
    family: str                       # sniff.FAMILIES
    status: str                       # quality.STATUSES
    reason: str                       # quality.REASONS
    warnings: tuple[str, ...]
    text: str                         # in memory only; never persisted
    page_count: int | None
    pages_with_text: int | None
    char_count: int
    devanagari_ratio: float | None
    text_page_coverage: float | None
    metrics: dict[str, Any]
    preview: str
    error: str | None
    duration_ms: int


def _preview(text: str) -> str:
    """A bounded, single-line window. Newlines collapse so a report line stays a
    report line, and Devanagari survives (no ASCII coercion)."""
    return " ".join(text.split())[:PREVIEW_CHARS]


def _result(
    *,
    parser: str,
    family: str,
    evidence: quality.Evidence,
    text: str,
    extra_metrics: dict[str, Any] | None = None,
    started: float,
    extractor_version: str = EXTRACTOR_VERSION,
    units: Sequence[str] | None = None,
) -> ExtractionResult:
    """Classify the evidence and flatten every measurement into one row.

    The `metrics` dict is the full record, not a summary: both page medians
    (they separate a partial scan from a sparse text layer) and both halves of the
    legacy-line ratio (a ratio alone cannot be audited) are carried, because
    Phase 6B has to be able to re-slice this without re-parsing 8.6 GB.

    `extractor_version` picks the classifier and nothing else. native-1 takes the
    identical path it always did — `quality.classify(evidence)`, same evidence,
    same metrics — so its rows stay reproducible byte for byte. native-2 adds the
    unit assessment on top and never removes a native-1 metric.

    `units` are the judgment units. `None` means "derive them from the text",
    which is right for prose and PDFs; a spreadsheet passes its CELLS explicitly,
    because the `" | "`-joined row it stores as text is not safe to score (`|` is
    a Preeti codepoint).
    """
    metrics: dict[str, Any] = {}
    if extractor_version == EXTRACTOR_VERSION:
        verdict = quality.classify(evidence)
    else:
        from . import routing, units as units_mod

        unit_texts = (
            list(units) if units is not None else list(units_mod.units_from_text(text))
        )
        routed = routing.RoutingEvidence.build(evidence, unit_texts)
        verdict = routing.classify_v2(routed)
        metrics.update(routing.unit_metrics(routed.profile))
    if evidence.text_metrics is not None:
        metrics.update(evidence.text_metrics.as_dict())
    if evidence.pages is not None:
        metrics.update(
            {
                "page_count": evidence.pages.page_count,
                "pages_with_text": evidence.pages.pages_with_text,
                "text_page_coverage": evidence.pages.text_page_coverage,
                "median_chars_per_page": evidence.pages.median_chars_per_page,
                "median_chars_per_text_page": evidence.pages.median_chars_per_text_page,
            }
        )
    if evidence.sheets is not None:
        metrics.update(
            {
                "sheet_count": evidence.sheets.sheet_count,
                "row_count": evidence.sheets.row_count,
                "non_empty_cells": evidence.sheets.non_empty_cells,
                "populated_ratio": evidence.sheets.populated_ratio,
            }
        )
    metrics.update(extra_metrics or {})
    return ExtractionResult(
        parser=parser,
        family=family,
        status=verdict.status,
        reason=verdict.reason,
        warnings=verdict.warnings,
        text=text,
        page_count=evidence.pages.page_count if evidence.pages else None,
        pages_with_text=evidence.pages.pages_with_text if evidence.pages else None,
        char_count=evidence.text_metrics.char_count if evidence.text_metrics else 0,
        devanagari_ratio=(
            evidence.text_metrics.devanagari_ratio if evidence.text_metrics else None
        ),
        text_page_coverage=(
            evidence.pages.text_page_coverage if evidence.pages else None
        ),
        metrics=metrics,
        preview=_preview(text),
        error=evidence.error,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def _failed(
    family: str, message: str, started: float,
    extractor_version: str = EXTRACTOR_VERSION,
) -> ExtractionResult:
    """A recorded failure. One bad file must never abort a batch."""
    return _result(
        parser="none",
        family=family,
        evidence=quality.Evidence(family, False, message, None, None, None),
        text="",
        started=started,
        extractor_version=extractor_version,
    )


def _no_parser(
    family: str, started: float, extractor_version: str = EXTRACTOR_VERSION,
) -> ExtractionResult:
    """A valid file we decline to open: an image, or a format with no parser."""
    return _result(
        parser="none",
        family=family,
        evidence=quality.Evidence(family, False, None, None, None, None),
        text="",
        started=started,
        extractor_version=extractor_version,
    )


def result_from_pages(
    pages: Sequence[str],
    *,
    parser: str,
    family: str = "pdf",
    started: float | None = None,
    extra_metrics: dict[str, Any] | None = None,
    extractor_version: str = EXTRACTOR_VERSION,
) -> ExtractionResult:
    """Score a PDF's per-page text. THE shared path — pypdf and Docling both use it.

    This exists so the calibration compares what the two engines *read*, never how
    their output was judged. Both sides run the same `measure_text`,
    `measure_pages` and `classify` at the same thresholds; `parser` is recorded as
    a fact and is never branched on. If this ever grew an engine-specific rule the
    agreement rate would stop meaning anything.
    """
    text = "\n".join(pages)
    return _result(
        parser=parser,
        family=family,
        evidence=quality.Evidence(
            family=family,
            parsed=True,
            error=None,
            text_metrics=quality.measure_text(text),
            pages=quality.measure_pages(pages) if pages else None,
            sheets=None,
        ),
        text=text,
        extra_metrics=extra_metrics,
        started=time.monotonic() if started is None else started,
        extractor_version=extractor_version,
    )


def _extract_pdf(
    path: Path, family: str, started: float,
    extractor_version: str = EXTRACTOR_VERSION,
) -> ExtractionResult:
    read = file_documents.read_pdf_pages(path)
    return result_from_pages(
        read.pages,
        parser="pypdf",
        family=family,
        started=started,
        extra_metrics={"pages_skipped": read.skipped},
        extractor_version=extractor_version,
    )


def _extract_document(
    path: Path, family: str, started: float,
    extractor_version: str = EXTRACTOR_VERSION,
) -> ExtractionResult:
    doc = file_documents.read_lines(path)
    text = "\n".join(doc.lines)
    return _result(
        parser="python-docx",
        family=family,
        evidence=quality.Evidence(
            family=family,
            parsed=True,
            error=None,
            text_metrics=quality.measure_text(text),
            pages=None,
            sheets=None,
        ),
        text=text,
        started=started,
        extractor_version=extractor_version,
    )


def _extract_spreadsheet(
    path: Path, family: str, started: float,
    extractor_version: str = EXTRACTOR_VERSION,
) -> ExtractionResult:
    """Every sheet, bounded, as text plus structure.

    Judged STRUCTURALLY by `classify` (are there cells?), not linguistically: a
    statistical table has no sentences, so every prose rule would misfire on one.
    Formulas are never evaluated — `readers.open_sheet_rows` opens xlsx with
    `data_only=True`, so a formula cell yields its cached value or nothing.
    """
    sheets = readers.inspect_workbook(path)
    names: list[str | None] = [s.sheet_name for s in sheets] or [None]
    parts: list[str] = []
    rows_seen = 0
    cells_total = 0
    cells_filled = 0
    # The judgment units, kept as INDIVIDUAL cells. `parts` is the rendered text
    # (`" | "`-joined, for storage and for native-1's metrics); `cells` is what
    # native-2 actually scores. They must not be the same list: `|` is a Preeti
    # codepoint that maps to `्र`, so a rendered row is unsafe to judge and
    # unsafe to convert. Cell identity is preserved so a later converter can work
    # per cell too. See `units.cells_from_rows`.
    cells: list[str] = []
    for name in names:
        with readers.open_sheet_rows(path, sheet=name) as stream:
            if stream.headers:
                parts.append(" | ".join(stream.headers))
                cells_total += len(stream.headers)
                cells_filled += sum(1 for h in stream.headers if str(h).strip())
                cells.extend(str(h) for h in stream.headers if str(h).strip())
            for row in stream.rows:
                if rows_seen >= MAX_SHEET_ROWS:
                    break
                rows_seen += 1
                cells_total += len(row)
                cells_filled += sum(1 for c in row if str(c).strip())
                if any(str(c).strip() for c in row):
                    parts.append(" | ".join(str(c) for c in row))
                    cells.extend(str(c) for c in row if str(c).strip())
    text = "\n".join(parts)
    return _result(
        parser="openpyxl",
        family=family,
        evidence=quality.Evidence(
            family=family,
            parsed=True,
            error=None,
            text_metrics=quality.measure_text(text),
            pages=None,
            sheets=quality.SheetStats(
                sheet_count=len(names),
                row_count=rows_seen,
                non_empty_cells=cells_filled,
                populated_ratio=(
                    round(cells_filled / cells_total, 4) if cells_total else 0.0
                ),
            ),
        ),
        text=text,
        extra_metrics={
            "rows_truncated": int(rows_seen >= MAX_SHEET_ROWS),
            "spreadsheet_text_cells": len(cells),
        },
        started=started,
        extractor_version=extractor_version,
        units=cells,
    )


def _extract_text(
    path: Path, family: str, started: float,
    extractor_version: str = EXTRACTOR_VERSION,
) -> ExtractionResult:
    body = path.read_bytes().decode("utf-8-sig", errors="replace")
    return _result(
        parser="text",
        family=family,
        evidence=quality.Evidence(
            family, True, None, quality.measure_text(body), None, None
        ),
        text=body,
        started=started,
        extractor_version=extractor_version,
    )


def extract_file(
    path: Path,
    *,
    family: str,
    extension: str | None,
    extractor_version: str = EXTRACTOR_VERSION,
) -> ExtractionResult:
    """Extract and classify one blob. NEVER raises.

    A pass over hundreds of files must not die on one bad document, and *how* a
    file failed is itself the finding — the same contract as `fetch.fetch_one` and
    `wp_api`'s `FetchError`.

    `family` is our own magic-byte determination (`nrb_files.sniffed_mime` through
    `sniff.family_for`), never NRB's `reported_mime_type` — that is the claim
    Phase 5 exists to check. `extension` breaks a tie only when the sniffer
    honestly answered `unknown`.
    """
    started = time.monotonic()
    path = Path(path)
    ext = (extension or path.suffix.lstrip(".")).strip().lower()

    # Checked before the family, because a sniffer that answered `unknown` must
    # not let an extension route a legacy Excel file into openpyxl.
    if ext in LEGACY_OFFICE_EXTENSIONS:
        return _no_parser("office_legacy", started, extractor_version=extractor_version)

    if family == "unknown" and ext in _EXTENSION_FAMILIES:
        family = _EXTENSION_FAMILIES[ext]

    # No parser: decided without opening the file at all.
    if family in quality.UNSUPPORTED_FAMILIES or family == "image":
        return _no_parser(family, started, extractor_version=extractor_version)

    try:
        size = path.stat().st_size
    except OSError as exc:
        return _failed(family, f"OSError: {exc.strerror or 'unreadable'}", started, extractor_version=extractor_version)
    if size > MAX_EXTRACT_BYTES:
        return _failed(family, f"file exceeds {MAX_EXTRACT_BYTES} bytes", started, extractor_version=extractor_version)
    if size == 0:
        return _failed(family, "empty file", started, extractor_version=extractor_version)

    try:
        if family == "pdf":
            return _extract_pdf(path, family, started, extractor_version=extractor_version)
        if family == "document":
            return _extract_document(path, family, started, extractor_version=extractor_version)
        if family == "spreadsheet":
            return _extract_spreadsheet(path, family, started, extractor_version=extractor_version)
        if family == "text":
            return _extract_text(path, family, started, extractor_version=extractor_version)
    except ReadError as exc:
        # Already sanitised by `documents.py`/`readers.py` — no path, no user id.
        return _failed(family, str(exc), started, extractor_version=extractor_version)
    except Exception as exc:  # noqa: BLE001 - a batch must survive any parser bug
        logger.warning("NRB extract: %s failed (%s)", family, type(exc).__name__)
        return _failed(family, type(exc).__name__, started, extractor_version=extractor_version)

    # A family with no branch above. Honest rather than silent.
    return _no_parser(family, started, extractor_version=extractor_version)


# --------------------------------------------------------------------------- #
# Calibration: what the OTHER engine makes of the same bytes.
#
# Not part of the profiling path, and never written to `nrb_extractions`. This
# exists so "pypdf is a fair proxy for the native-extraction question" is a
# measured claim rather than an assertion — and so the phase question "is native
# Docling sufficient?" has an answer.
#
# Docling and torch are imported INSIDE these functions, never at module scope:
# the slim API image must not gain ~90 packages because something imported this
# module. `test_docling_is_not_imported_when_the_nrb_extraction_module_loads`
# holds that in a subprocess, since `sys.modules` is process-global.
# --------------------------------------------------------------------------- #
def docling_pipeline_is_native() -> tuple[bool, str]:
    """Is the shared Docling pipeline still CPU-pinned with OCR off?

    Two things depend on this. The calibration is only meaningful if both engines
    read the same embedded text layer — Docling with OCR on would be answering a
    different question. And Phase 6A forbids running OCR at all, so reusing
    someone else's converter means checking what it is configured to do rather
    than assuming.

    Returns `(ok, evidence)` so a caller can print WHY it refused.
    """
    from ..rag.parsing import _pdf_pipeline_options

    options = _pdf_pipeline_options()
    device = getattr(getattr(options, "accelerator_options", None), "device", None)
    device_name = str(getattr(device, "value", device)).lower()
    ocr = bool(getattr(options, "do_ocr", False))
    return (not ocr and device_name == "cpu"), f"do_ocr={ocr}, device={device_name}"


def _docling_pages(document) -> list[str]:
    """Docling's item stream, grouped into per-page text. NO RAG filtering.

    Deliberately does NOT go through `parsing.parse_to_chunks`: that applies
    `merge_blocks`, `drop_small_blocks`, front-matter skipping and chunking on top
    of Docling, so a disagreement with pypdf could come from RAG's filter rather
    than from what Docling read off the page. Every item's text is kept, in
    document order, placed on the page `item.prov[0].page_no` reports — which is
    what makes `quality.measure_pages` applicable to both engines and the
    scanned-PDF rules comparable.
    """
    pages: dict[int, list[str]] = {}
    ordered: list[str] = []
    for item, _level in document.iterate_items():
        label = getattr(getattr(item, "label", None), "value", "") or ""
        if label == "table":
            try:
                text = item.export_to_markdown(document).strip()
            except Exception:  # noqa: BLE001 - a malformed table is not fatal
                text = ""
        else:
            text = (getattr(item, "text", "") or "").strip()
        if not text:
            continue
        prov = getattr(item, "prov", None) or []
        page = prov[0].page_no if prov else 0
        pages.setdefault(page, []).append(text)
        ordered.append(text)

    try:
        total = int(document.num_pages())
    except Exception:  # noqa: BLE001 - not every docling version exposes it
        total = max(pages) if pages else 0
    if not total:
        # No page metadata at all: one synthetic page, so the text metrics still
        # apply and only the page rules are skipped.
        return ["\n".join(ordered)] if ordered else []
    # An empty entry for a page Docling found nothing on — that IS the scanned
    # page signal, and dropping it would make coverage read as 100%.
    return ["\n".join(pages.get(number, [])) for number in range(1, total + 1)]


class DoclingEngine:
    """One Docling converter, reused across a calibration run.

    Constructing a `DocumentConverter` builds the layout pipeline and loads its
    models; doing that per file would make the measured per-document duration
    mostly startup, and the "how much slower is Docling" number would be wrong in
    the direction that flatters pypdf. So the converter is built once, its
    construction is timed SEPARATELY (`init_seconds`), and every file after that
    is a like-for-like parse.

    Not a module-level singleton and not cached: that would be global mutable
    state in an import path the API also loads. The engine is created by the
    calibration pass, used, and dropped.
    """

    def __init__(self) -> None:
        self._converter: Any = None
        self.init_seconds: float = 0.0
        self.error: str | None = None

    def open(self) -> tuple[bool, str]:
        """Build the converter. Returns `(ok, evidence)`; never raises."""
        ok, evidence = docling_pipeline_is_native()
        if not ok:
            self.error = f"docling pipeline is not native ({evidence})"
            return False, self.error
        started = time.monotonic()
        try:
            from ..rag.parsing import _docling_converter

            self._converter = _docling_converter()
        except ImportError:
            self.error = "docling is not installed (worker deps only)"
            return False, self.error
        except Exception as exc:  # noqa: BLE001 - report, do not abort the pass
            self.error = f"{type(exc).__name__} building the docling converter"
            return False, self.error
        self.init_seconds = time.monotonic() - started
        return True, f"{evidence}, init {self.init_seconds:.1f}s"

    def extract(self, path: Path) -> ExtractionResult:
        return docling_extract(path, converter=self._converter)

    def close(self) -> None:
        """Drop the converter so its models can be collected."""
        self._converter = None

    def __enter__(self) -> "DoclingEngine":
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def docling_extract(path: Path, *, converter: Any = None) -> ExtractionResult:
    """Docling's native extraction, scored by the SAME rules as pypdf's.

    Reuses `parsing._docling_converter()` — a private helper, and depended on
    deliberately. Copying its three configuration lines here would create a second
    pipeline configuration that could drift, and the way it would drift is by
    silently enabling OCR. Reusing it means the calibration is pinned to whatever
    department RAG actually runs, and `docling_pipeline_is_native` fails loudly if
    that stops being CPU/no-OCR.

    `app/rag/parsing.py` itself is NOT modified: its behaviour is load-bearing for
    department RAG, and Phase 6A must not change department semantics to make NRB
    convenient.

    Never raises — the same contract as `extract_file`. A calibration that died on
    file 12 of 40 would have measured nothing.
    """
    started = time.monotonic()
    ok, evidence = docling_pipeline_is_native()
    if not ok:
        return _failed("pdf", f"docling pipeline is not native ({evidence})", started)
    try:
        if converter is None:
            from ..rag.parsing import _docling_converter

            converter = _docling_converter()
        document = converter.convert(str(path)).document
        pages = _docling_pages(document)
    except ImportError:
        return _failed("pdf", "docling is not installed (worker deps only)", started)
    except Exception as exc:  # noqa: BLE001 - a calibration must not kill a batch
        logger.warning("NRB calibrate: docling failed (%s)", type(exc).__name__)
        return _failed("pdf", type(exc).__name__, started)

    return result_from_pages(pages, parser="docling", family="pdf", started=started)
