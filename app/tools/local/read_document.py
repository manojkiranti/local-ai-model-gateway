"""Local tool: read_document — the text of ONE uploaded document.

Owner-scoped by file_id (see files/source.py). Handles .pdf/.docx/.txt/.md/
.json; a PDF's page boundaries appear as '[page N]' marker lines inside the
line stream, so there is only ever ONE paging unit.

Two deliberate differences from read_excel, both about truncation honesty:

  * METADATA LEADS. agent/loop.py cuts any tool result over
    MAX_TOOL_RESULT_CHARS from the END, which is exactly where read_excel puts
    its "call again with start_row=N" note. Leading metadata survives the cut.
  * WE TRUNCATE FIRST, on whole lines. If the loop cut the body instead, the
    header would promise "continue at line 401" while the model only ever saw
    line 90 — a silent hole that looks like a complete read.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from ...files import documents, ingest, readers
from ...files.store import resolve_file
from .base import LocalToolSpec

READ_DOC_MAX_LINES = 400

# Must equal agent.loop.MAX_TOOL_RESULT_CHARS. NOT imported from there: the
# agent imports the tool registry, so a tools -> agent import is circular.
# tests/test_read_document_tool.py asserts the two agree.
MODEL_RESULT_CAP = 8000
HEADER_BUDGET = 400                              # room for the metadata block
DOC_MAX_CHARS = MODEL_RESULT_CAP - HEADER_BUDGET  # 7600


def _window(
    lines: list[str], start_line: int, max_lines: Optional[int]
) -> tuple[list[str], int, bool]:
    """Return (window, last_line_number, truncated).

    Truncation is on WHOLE lines: a line that would cross the budget is dropped
    entirely, so `last_line_number` is exactly what the model received and
    `last_line_number + 1` is exactly where it should resume.
    """
    start = max(1, start_line)
    index = start - 1
    cap = READ_DOC_MAX_LINES
    if max_lines is not None:
        cap = max(1, min(int(max_lines), READ_DOC_MAX_LINES))
    selected = lines[index : index + cap]

    out: list[str] = []
    used = 0
    for line in selected:
        cost = len(line) + 1  # + the newline that joins it
        if not out and cost > DOC_MAX_CHARS:
            # A single line longer than the entire budget. Emit it alone and
            # hard-cut, or the reader could never make progress past it.
            out.append(line[:DOC_MAX_CHARS] + " …[long line truncated]")
            break
        if used + cost > DOC_MAX_CHARS:
            break
        out.append(line)
        used += cost

    last = index + len(out)
    return out, last, last < len(lines)


def _header(doc: documents.DocumentText, start: int, last: int, truncated: bool) -> list[str]:
    total = len(doc.lines)
    if doc.pages is not None:
        page_word = "page" if doc.pages == 1 else "pages"
        head = (
            f"{doc.kind}, {doc.pages} {page_word}, {total} lines — "
            f"showing lines {start}–{last} of {total}."
        )
    else:
        head = f"{doc.kind}, {total} lines — showing lines {start}–{last} of {total}."
    out = [head]

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
        return "ERROR: no such file (unknown id, or you don't own it)."

    if Path(record.path).suffix.lower() in ingest.SPREADSHEET_EXTS:
        return "ERROR: this is a spreadsheet — use inspect_excel / read_excel instead."

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

    window, last, truncated = _window(doc.lines, start_line, max_lines)
    header = _header(doc, max(1, start_line), last, truncated)
    return "\n".join(header + [""] + window)


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
        "and for any total or breakdown use aggregate_excel. For questions about "
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
