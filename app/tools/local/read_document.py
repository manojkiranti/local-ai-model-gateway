"""Local tool: read_document — the text of ONE uploaded document.

Owner-scoped by file_id (see files/source.py). Handles .pdf/.docx/.txt/.md/
.json; a PDF's page boundaries appear as '[page N]' marker lines inside the
line stream, so there is only ever ONE paging unit.

Paging is `_paging.window` — shared with read_image, and the two rules it
exists for (metadata leads, whole-line truncation first) are documented there.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from ...files import documents, images, ingest, readers
from ...files.store import resolve_file
from ._paging import HEADER_BUDGET, MODEL_RESULT_CAP, window
from .base import LocalToolSpec

READ_DOC_MAX_LINES = 400
DOC_MAX_CHARS = MODEL_RESULT_CAP - HEADER_BUDGET  # 7600


def _header(
    doc: documents.DocumentText,
    start: int,
    last: int,
    truncated: bool,
    hard_cut: Optional[tuple[int, int]],
) -> list[str]:
    total = len(doc.lines)
    line_word = "line" if total == 1 else "lines"
    if doc.pages is not None:
        page_word = "page" if doc.pages == 1 else "pages"
        head = (
            f"{doc.kind}, {doc.pages} {page_word}, {total} {line_word} — "
            f"showing lines {start}–{last} of {total}."
        )
    else:
        head = f"{doc.kind}, {total} {line_word} — showing lines {start}–{last} of {total}."
    out = [head]

    if hard_cut is not None:
        # This can fire even when `truncated` is False (the cut line was the
        # LAST line in the document, so there is nothing to resume at) — that
        # is exactly the case a trailing "…[long line truncated]" suffix at
        # the very end of the body cannot announce: metadata leads so a
        # truthful signal reaches the model even if the body itself gets cut
        # by agent/loop.py's own end-of-result truncation.
        line_no, length = hard_cut
        out.append(
            f"NOTE: line {line_no} is {length} characters, longer than the "
            f"{DOC_MAX_CHARS}-character read budget — it was hard-cut, and the "
            f"rest of that line is NOT retrievable by paging."
        )
    if truncated:
        out.append(
            f"TRUNCATED: call read_document again with start_line={last + 1} to continue."
        )
    if doc.pages_skipped:
        first_skipped = doc.pages - doc.pages_skipped + 1
        out.append(
            f"PARTIAL: pages {first_skipped}–{doc.pages} were not read "
            f"(limit {documents.MAX_PDF_PAGES} pages)."
        )
    if doc.pages is not None and doc.text_pages is not None:
        read_count = doc.pages - (doc.pages_skipped or 0)
        empty = read_count - doc.text_pages
        if empty > 0:
            out.append(
                f"{empty} of {read_count} pages have no extractable text "
                f"(likely scanned images)."
            )
    return out


async def _read_document(args: dict[str, Any]) -> str:
    file_id = args.get("file_id")
    if not isinstance(file_id, str) or not file_id.strip():
        return "ERROR: 'file_id' is required (the id of an uploaded document)."
    record = await resolve_file(file_id.strip())
    if record is None:
        return (
            "ERROR: no such file (unknown id, or you don't own it). If the user "
            "did not attach this document to the chat, do NOT guess a file_id — "
            "search the department's official corpus with search_department_docs "
            "instead."
        )

    ext = Path(record.path).suffix.lower()
    if ext in ingest.SPREADSHEET_EXTS:
        return "ERROR: this is a spreadsheet — use inspect_excel / read_excel instead."
    if ext in ingest.IMAGE_EXTS:
        return "ERROR: this is an image — use read_image instead."

    try:
        start_line = int(args.get("start_line", 1) or 1)
    except (TypeError, ValueError):
        return "ERROR: 'start_line' must be an integer (1-based)."
    max_lines = args.get("max_lines")
    try:
        max_lines = int(max_lines) if max_lines is not None else None
    except (TypeError, ValueError):
        return "ERROR: 'max_lines' must be an integer."

    # Extraction is sync and CPU-bound (a big PDF is seconds) — off the loop.
    try:
        doc = await asyncio.to_thread(documents.read_lines, Path(record.path))
    except documents.EncryptedDocument:
        return "ERROR: this PDF is password-protected — it cannot be read."
    except readers.ReadError as exc:
        return f"ERROR: could not read the document ({exc})."

    # Policy: a PDF with pages but no text anywhere is a scan. Said explicitly,
    # because an empty body would read to the model as "the document is blank".
    if doc.pages and not doc.text_pages:
        return (
            "ERROR: this PDF appears to contain scanned images with no text layer "
            "— OCR is not available yet."
        )
    if not doc.lines:
        return f"{doc.kind}: this document is empty (0 lines)."
    if start_line > len(doc.lines):
        return (
            f"ERROR: start_line={start_line} is past the end — this {doc.kind} "
            f"has {len(doc.lines)} lines."
        )

    body, last, truncated, hard_cut = window(
        doc.lines, start_line, max_lines,
        line_cap=READ_DOC_MAX_LINES, char_budget=DOC_MAX_CHARS,
    )
    header = _header(doc, max(1, start_line), last, truncated, hard_cut)
    return "\n".join(header + [""] + body)


SPEC = LocalToolSpec(
    name="read_document",
    description=(
        "Read the text of a document the USER attached to THIS chat (.pdf, .docx, "
        ".txt, .md, .json) by its file_id. Page through it with 'start_line' "
        "(1-based) and 'max_lines'; the FIRST line of the result gives the total "
        "line count, and if the output was truncated the second line gives the "
        "exact start_line to continue from. In a PDF, page boundaries appear as "
        "'[page N]' marker lines, so you can cite the page a passage came from. "
        "For a spreadsheet (.xlsx/.csv) use inspect_excel / read_excel instead, "
        "and for any total or breakdown use aggregate_excel. For an IMAGE "
        "(.png/.jpg/.webp/.tif/.bmp) use read_image, which OCRs it. For questions about "
        "company policy, circulars, entitlements or internal rules that the user "
        "did NOT attach to this chat, use search_department_docs — that searches "
        "the department's official corpus, while this reads one attached file."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file_id": {
                "type": "string",
                "description": "Id of an uploaded/attached document.",
            },
            "start_line": {
                "type": "integer",
                "description": "1-based first line to return (default 1).",
            },
            "max_lines": {
                "type": "integer",
                "description": "Max lines to return this call (capped at 400).",
            },
        },
        "required": ["file_id"],
    },
    func=_read_document,
)
