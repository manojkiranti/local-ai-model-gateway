"""Normalize an uploaded document into a flat list of text LINES.

Pure module — no DB, no HTTP. The upload route (parse check + summary) and the
`read_document` tool both go through here, so every supported format behaves
identically downstream.

Design rule: this module reports FACTS and raises only when a file genuinely
cannot be parsed. It makes no policy decisions — notably a scanned PDF returns
normally with `text_pages == 0`, and the tool decides that means "no OCR".
Caps on how much reaches the model live in the tool, not here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .readers import ReadError

logger = logging.getLogger("app.files")

DOCUMENT_EXTS = {".pdf", ".docx", ".txt", ".md", ".json"}

# A hard bound on extraction work for one PDF. Beyond this we stop and SAY so
# (see DocumentText.pages_skipped) rather than refusing the file.
MAX_PDF_PAGES = 500


class EncryptedDocument(ReadError):
    """The file needs a password we don't have (empty password already tried)."""


@dataclass
class DocumentText:
    kind: str
    lines: list[str]
    # PDF-only; None for every other format.
    pages: Optional[int] = None
    text_pages: Optional[int] = None       # pages READ that produced text
    pages_skipped: Optional[int] = None    # pages beyond MAX_PDF_PAGES


def _decode(path: Path) -> str:
    """Bytes -> str, never raising. utf-8-sig strips a BOM when present and is
    plain utf-8 otherwise; errors='replace' means a binary file renamed .txt
    degrades to mojibake instead of crashing the reader."""
    return path.read_bytes().decode("utf-8-sig", errors="replace")


def _read_text(path: Path, ext: str) -> DocumentText:
    kind = "Markdown" if ext == ".md" else "Text file"
    return DocumentText(kind=kind, lines=_decode(path).splitlines())


def _read_json(path: Path) -> DocumentText:
    text = _decode(path)
    try:
        parsed = json.loads(text)
    except ValueError:
        # Deliberately NOT an error: near-valid JSON is still readable, and the
        # kind tells the model it is looking at raw text.
        return DocumentText(kind="JSON (unparsed)", lines=text.splitlines())
    pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
    return DocumentText(kind="JSON", lines=pretty.splitlines())


def _iter_docx_blocks(doc):
    """Yield Paragraph and Table objects in DOCUMENT ORDER.

    python-docx exposes doc.paragraphs and doc.tables as separate flat lists,
    which loses their relative position — a table would drift to the end of the
    output. Walking the body XML is the only way to keep the reading order the
    author intended.
    """
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in doc.element.body.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, doc)
        elif isinstance(child, CT_Tbl):
            yield Table(child, doc)


def _read_docx(path: Path) -> DocumentText:
    from docx import Document
    from docx.table import Table

    try:
        doc = Document(str(path))
    except Exception as exc:  # noqa: BLE001 - any docx/zip failure is a ReadError
        raise ReadError(f"could not read the Word document: {exc}") from exc

    lines: list[str] = []
    for block in _iter_docx_blocks(doc):
        if isinstance(block, Table):
            lines.append("")
            for row in block.rows:
                lines.append(" | ".join(cell.text.strip() for cell in row.cells))
            lines.append("")
            continue
        text = block.text.strip()
        style = getattr(getattr(block, "style", None), "name", "") or ""
        lines.append(f"# {text}" if style.startswith("Heading") and text else text)
    return DocumentText(kind="Word document", lines=lines)


def _read_pdf(path: Path) -> DocumentText:
    """PDF -> lines, one '[page N]' marker per page.

    An empty page is NOT skipped: it emits an explicit marker, because a silent
    gap reads to the model as "there was nothing there" rather than "this page
    could not be extracted".
    """
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            # Many real-world PDFs are encrypted with an EMPTY owner password
            # and open fine; only a genuine user password is a hard failure.
            try:
                opened = reader.decrypt("")
            except Exception:  # noqa: BLE001 - a failed decrypt is just "locked"
                opened = 0
            if not opened:
                raise EncryptedDocument("this PDF is password-protected")
        total = len(reader.pages)
    except EncryptedDocument:
        raise
    except Exception as exc:  # noqa: BLE001 - no pypdf exception escapes this module
        raise ReadError(f"could not read the PDF: {exc}") from exc

    limit = min(total, MAX_PDF_PAGES)
    lines: list[str] = []
    text_pages = 0
    for index in range(limit):
        try:
            raw = reader.pages[index].extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - exception is now logged, per-page failure doesn't kill the document
            logger.warning(f"PDF page {index + 1} extraction failed: {exc}")
            raw = ""
        page_lines = [ln.rstrip() for ln in raw.splitlines() if ln.strip()]
        if page_lines:
            text_pages += 1
            lines.append(f"[page {index + 1}]")
            lines.extend(page_lines)
        else:
            lines.append(
                f"[page {index + 1}] (no extractable text — likely a scanned image)"
            )
    return DocumentText(
        kind="PDF",
        lines=lines,
        pages=total,
        text_pages=text_pages,
        pages_skipped=total - limit,
    )


def read_lines(path: Path) -> DocumentText:
    """Any supported document -> its text as lines. Raises ReadError for an
    unsupported extension or an unparseable file."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".json":
        return _read_json(path)
    if ext in (".txt", ".md"):
        return _read_text(path, ext)
    if ext == ".docx":
        return _read_docx(path)
    if ext == ".pdf":
        return _read_pdf(path)
    raise ReadError(f"unsupported document type '{ext}'")
