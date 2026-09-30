"""Render a corpus PDF's pages as images — the view-only path for non-admins.

A citation's original bytes are admin-only (`download_department_document`).
Everyone else who may read a document sees it through this module instead: one
PNG per page, rendered here, so neither the file nor its text layer ever
reaches their browser — there is nothing to save, select, copy or print from.
A screenshot is still possible; no server-side control can prevent that.

pypdfium2 is imported lazily, like docling in `parsing`: it is only needed when
someone actually opens a page. It ships its own PDFium build, so it needs no
system libraries in the slim image. Both functions are sync and CPU-bound —
the route runs them in a thread.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

# Width of a rendered page in pixels. Enough to read small print on a laptop
# screen; not a print-quality scan.
PAGE_WIDTH_PX = 1400


class PageError(Exception):
    """The file could not be opened as a PDF."""


def _open(path: Path):
    import pypdfium2 as pdfium

    try:
        return pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        raise PageError(f"not a readable PDF: {exc}") from exc


def page_count(path: Path) -> int:
    pdf = _open(path)
    try:
        return len(pdf)
    finally:
        pdf.close()


def render_page_png(path: Path, page_number: int) -> bytes:
    """PNG of 1-based `page_number`. IndexError if it is out of range."""
    pdf = _open(path)
    try:
        if not 1 <= page_number <= len(pdf):
            raise IndexError(page_number)
        page = pdf[page_number - 1]
        try:
            width_pt = page.get_width() or 1
            bitmap = page.render(scale=PAGE_WIDTH_PX / width_pt)
            buffer = BytesIO()
            bitmap.to_pil().save(buffer, format="PNG", optimize=True)
            return buffer.getvalue()
        finally:
            page.close()
    finally:
        pdf.close()
