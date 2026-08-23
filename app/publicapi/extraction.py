"""Uploaded bytes -> one `ExtractedText`, for `POST /v1/extract`.

Every engine reached from here already exists and is already tested — this
module chooses between them and normalises what they return. It holds no
policy of its own beyond one rule:

  **`route` decides `authoritative`, and nothing else does.** Text read from a
  document's own text layer is exact; text read by an OCR engine is not. The
  serialiser (`extract_schemas.py`) omits the caveat entirely for a native
  route, because over-warning trains a reader to ignore the warning — the
  §29.2 rule from docs/nrb-integration.md, applied to a second surface.

It reports FACTS and raises nothing but `ReadError`. A PDF with no text layer
is not an error here: it comes back with `text_pages == 0` and the ROUTER
turns that into a 422. That is the same seam `app/files/documents.py` already
has with the `read_document` tool, and it is why the scanned-vs-empty
distinction survives.

The `ocr` parameter is injectable so the DISPATCH decision stays testable in a
build without the OCR stack. Nothing else in this module needs one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..files import documents, image_ocr, images, ingest, readers

__all__ = [
    "OCR_ROUTE",
    "NATIVE_ROUTE",
    "EXTRACT_EXTS",
    "Sheet",
    "ExtractedText",
    "read_any",
]

OCR_ROUTE = "ocr"
NATIVE_ROUTE = "native"

EXTRACT_EXTS = frozenset(
    ingest.SPREADSHEET_EXTS | ingest.DOCUMENT_EXTS | ingest.IMAGE_EXTS
)


@dataclass(frozen=True)
class Sheet:
    """One worksheet. Spreadsheets are the one input that is not a line stream;
    flattening them would discard the structure a caller most wants."""

    name: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    total_rows: int
    truncated: bool


@dataclass(frozen=True)
class ExtractedText:
    kind: str
    route: str
    lines: tuple[str, ...] = ()
    line_confidences: Optional[tuple[float, ...]] = None
    sheets: tuple[Sheet, ...] = ()
    pages: Optional[int] = None
    text_pages: Optional[int] = None
    pages_skipped: Optional[int] = None
    partial: bool = False

    @property
    def authoritative(self) -> bool:
        """True only for text read from the document's own text layer."""
        return self.route == NATIVE_ROUTE

    @property
    def is_scanned_pdf(self) -> bool:
        """Pages exist and none of them yielded text. A FACT, not an error."""
        return bool(self.pages) and self.text_pages == 0


def read_any(
    path: Path,
    *,
    lang: Optional[str] = None,
    ocr: Optional[Callable[..., object]] = None,
) -> ExtractedText:
    """Dispatch on extension. Raises `readers.ReadError` for anything else."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in ingest.SPREADSHEET_EXTS:
        return _read_spreadsheet(path)
    if ext in ingest.DOCUMENT_EXTS:
        return _read_document(path)
    if ext in ingest.IMAGE_EXTS:
        return _read_image(path, lang=lang, ocr=ocr or image_ocr.ocr_image)
    raise readers.ReadError(f"unsupported file type '{ext}'")


def _read_document(path: Path) -> ExtractedText:
    doc = documents.read_lines(path)
    return ExtractedText(
        kind=doc.kind,
        route=NATIVE_ROUTE,
        lines=tuple(doc.lines),
        pages=doc.pages,
        text_pages=doc.text_pages,
        pages_skipped=doc.pages_skipped,
        # A PDF over MAX_PDF_PAGES lost pages. `read_document` reports the same
        # fact for the same reason: a silent cut reads as a complete document.
        partial=bool(doc.pages_skipped),
    )


def _read_spreadsheet(path: Path) -> ExtractedText:
    sheets: list[Sheet] = []
    truncated_any = False
    for info in readers.inspect_workbook(path):
        table = readers.load_table(path, sheet=info.sheet_name)
        truncated_any = truncated_any or table.truncated
        sheets.append(
            Sheet(
                name=table.sheet_name,
                headers=tuple(table.headers),
                rows=tuple(tuple(row) for row in table.rows),
                total_rows=table.total_rows,
                truncated=table.truncated,
            )
        )
    kind = "Excel" if path.suffix.lower() == ".xlsx" else "CSV"
    return ExtractedText(
        kind=kind, route=NATIVE_ROUTE, sheets=tuple(sheets), partial=truncated_any
    )


def _read_image(
    path: Path, *, lang: Optional[str], ocr: Callable[..., object]
) -> ExtractedText:
    # summarize_image owns the decoded-PIXEL cap and the decoder allowlist on
    # the SNIFFED format, and both run before any full decode. Never skip it.
    summary = images.summarize_image(path)
    chosen = (lang or image_ocr.DEFAULT_LANG).strip()
    result = ocr(path, lang=chosen)
    return ExtractedText(
        kind=summary.kind,
        route=OCR_ROUTE,
        lines=tuple(result.lines),
        line_confidences=tuple(result.scores),
        # Frame 1 only — a multi-frame .tif is a scanner's normal output and
        # page 2's text silently vanishes otherwise (measured).
        partial=summary.frames > 1,
    )
