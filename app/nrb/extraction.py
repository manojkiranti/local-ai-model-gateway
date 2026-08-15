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
from typing import Any

from ..files import documents as file_documents
from ..files import readers
from ..files.readers import ReadError
from . import quality

logger = logging.getLogger("app.nrb.extraction")

__all__ = [
    "EXTRACTOR_VERSION",
    "ExtractionResult",
    "MAX_EXTRACT_BYTES",
    "MAX_SHEET_ROWS",
    "PREVIEW_CHARS",
    "extract_file",
]

# Bumped BY HAND when a parser or a classification rule changes. It is half of
# `nrb_extractions`'s unique key, so bumping it makes every stored result stale
# and re-extractable without deleting anything. Deliberately not derived from a
# library version: a pypdf patch release does not invalidate a corpus, and a
# threshold change in `quality.py` does.
EXTRACTOR_VERSION = "native-1"

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
) -> ExtractionResult:
    """Classify the evidence and flatten every measurement into one row.

    The `metrics` dict is the full record, not a summary: both page medians
    (they separate a partial scan from a sparse text layer) and both halves of the
    legacy-line ratio (a ratio alone cannot be audited) are carried, because
    Phase 6B has to be able to re-slice this without re-parsing 8.6 GB.
    """
    verdict = quality.classify(evidence)
    metrics: dict[str, Any] = {}
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


def _failed(family: str, message: str, started: float) -> ExtractionResult:
    """A recorded failure. One bad file must never abort a batch."""
    return _result(
        parser="none",
        family=family,
        evidence=quality.Evidence(family, False, message, None, None, None),
        text="",
        started=started,
    )


def _no_parser(family: str, started: float) -> ExtractionResult:
    """A valid file we decline to open: an image, or a format with no parser."""
    return _result(
        parser="none",
        family=family,
        evidence=quality.Evidence(family, False, None, None, None, None),
        text="",
        started=started,
    )


def _extract_pdf(path: Path, family: str, started: float) -> ExtractionResult:
    read = file_documents.read_pdf_pages(path)
    text = "\n".join(read.pages)
    return _result(
        parser="pypdf",
        family=family,
        evidence=quality.Evidence(
            family=family,
            parsed=True,
            error=None,
            text_metrics=quality.measure_text(text),
            pages=quality.measure_pages(read.pages),
            sheets=None,
        ),
        text=text,
        extra_metrics={"pages_skipped": read.skipped},
        started=started,
    )


def _extract_document(path: Path, family: str, started: float) -> ExtractionResult:
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
    )


def _extract_spreadsheet(path: Path, family: str, started: float) -> ExtractionResult:
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
    for name in names:
        with readers.open_sheet_rows(path, sheet=name) as stream:
            if stream.headers:
                parts.append(" | ".join(stream.headers))
                cells_total += len(stream.headers)
                cells_filled += sum(1 for h in stream.headers if str(h).strip())
            for row in stream.rows:
                if rows_seen >= MAX_SHEET_ROWS:
                    break
                rows_seen += 1
                cells_total += len(row)
                cells_filled += sum(1 for c in row if str(c).strip())
                if any(str(c).strip() for c in row):
                    parts.append(" | ".join(str(c) for c in row))
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
        extra_metrics={"rows_truncated": int(rows_seen >= MAX_SHEET_ROWS)},
        started=started,
    )


def _extract_text(path: Path, family: str, started: float) -> ExtractionResult:
    body = path.read_bytes().decode("utf-8-sig", errors="replace")
    return _result(
        parser="text",
        family=family,
        evidence=quality.Evidence(
            family, True, None, quality.measure_text(body), None, None
        ),
        text=body,
        started=started,
    )


def extract_file(
    path: Path, *, family: str, extension: str | None
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
        return _no_parser("office_legacy", started)

    if family == "unknown" and ext in _EXTENSION_FAMILIES:
        family = _EXTENSION_FAMILIES[ext]

    # No parser: decided without opening the file at all.
    if family in quality.UNSUPPORTED_FAMILIES or family == "image":
        return _no_parser(family, started)

    try:
        size = path.stat().st_size
    except OSError as exc:
        return _failed(family, f"OSError: {exc.strerror or 'unreadable'}", started)
    if size > MAX_EXTRACT_BYTES:
        return _failed(family, f"file exceeds {MAX_EXTRACT_BYTES} bytes", started)
    if size == 0:
        return _failed(family, "empty file", started)

    try:
        if family == "pdf":
            return _extract_pdf(path, family, started)
        if family == "document":
            return _extract_document(path, family, started)
        if family == "spreadsheet":
            return _extract_spreadsheet(path, family, started)
        if family == "text":
            return _extract_text(path, family, started)
    except ReadError as exc:
        # Already sanitised by `documents.py`/`readers.py` — no path, no user id.
        return _failed(family, str(exc), started)
    except Exception as exc:  # noqa: BLE001 - a batch must survive any parser bug
        logger.warning("NRB extract: %s failed (%s)", family, type(exc).__name__)
        return _failed(family, type(exc).__name__, started)

    # A family with no branch above. Honest rather than silent.
    return _no_parser(family, started)
