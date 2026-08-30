"""Local tool: read_department_doc — ONE corpus document, in full, paged.

`search_department_docs` returns the best PASSAGES, under a character budget.
That answers "what does the corpus say about X" and cannot answer "summarise
this circular" or "what else is in that policy" — the model's only recourse was
another search with different words, or `read_document` with an invented
file_id, which asks the user to upload a document the corpus already holds.

Scope comes from `rag_context`, never from the model: there is deliberately NO
`department` parameter, so a prompt injection has nothing to target. The
`document_id` comes from a search result's `doc=` header.

**It reads the stored CHUNKS, not the document's bytes** — see
`app/rag/reassemble.py` for why that is the only correct source for the NRB
corpus, and what it guarantees for every other document.
"""

from __future__ import annotations

from typing import Any

from ...rag.context import current_department
from ...rag.reassemble import ChunkText, to_lines
from ...rag.sources import RECOVERED_ROUTES, VERIFY_NOTE
from ._paging import HEADER_BUDGET, MODEL_RESULT_CAP, window
from .base import LocalToolSpec

READ_DOC_MAX_LINES = 400
DOC_MAX_CHARS = MODEL_RESULT_CAP - HEADER_BUDGET

NO_DEPARTMENT = (
    "ERROR: no department is active for this conversation, so there is no "
    "document corpus to read from. Start a chat from a department tab."
)
# ONE message for unknown / another department's / not-yet-ready. At document
# granularity existence is the secret — the rule the download route follows with
# its blanket 404 — so these must stay indistinguishable.
NOT_FOUND = (
    "ERROR: no such document in this department's corpus. Use "
    "search_department_docs first and take the id from a result's 'doc=' field."
)


def _header(doc, lines: list[str], start: int, last: int, truncated: bool,
            hard_cut, recovered: bool) -> list[str]:
    total = len(lines)
    word = "line" if total == 1 else "lines"
    head = f'"{doc.title}"'
    if doc.pages:
        head += f", {doc.pages} page(s)"
    head += f", {total} {word} — showing {start}–{last} of {total}."
    out = [head]

    if recovered:
        # Same constant the citation renders, for the same reason: a reader who
        # sees one wording in the answer and another on the source cannot tell
        # which to believe.
        out.append(f"CAUTION: parts of this document are {VERIFY_NOTE}.")
    if hard_cut is not None:
        line_no, length = hard_cut
        out.append(
            f"NOTE: line {line_no} is {length} characters, longer than the "
            f"{DOC_MAX_CHARS}-character budget — it was hard-cut and the rest is "
            "not retrievable by paging."
        )
    if truncated:
        out.append(
            f"TRUNCATED: call read_department_doc again with start_line={last + 1} "
            "to continue."
        )
    return out


async def _fetch_document(document_id: str, department_id: int):
    """(document, chunks, routes) for a READY document in this department.

    Its own session, like `retrieval.search_chunks`: a tool is called from the
    agent loop and has no request-scoped session to borrow. Returns None when
    the document is unknown, belongs elsewhere, or is not `ready` — the caller
    turns all three into ONE message.
    """
    from sqlalchemy import select

    from ...db.session import SessionLocal
    from ...rag.models import Document, DocumentChunk

    async with SessionLocal() as session:
        doc = (
            await session.execute(
                select(Document).where(
                    Document.id == document_id,
                    Document.department_id == department_id,
                    Document.status == "ready",
                )
            )
        ).scalar_one_or_none()
        if doc is None:
            return None

        rows = list(
            (
                await session.execute(
                    select(DocumentChunk)
                    .where(
                        DocumentChunk.document_id == document_id,
                        # Redundant with the composite FK, and kept: the
                        # department predicate is the invariant this corpus is
                        # built on and every read states it.
                        DocumentChunk.department_id == department_id,
                    )
                    .order_by(DocumentChunk.chunk_index)
                )
            ).scalars()
        )

    chunks = [
        ChunkText(
            chunk_index=r.chunk_index,
            content=r.content,
            page_number=r.page_number,
            section=r.section,
        )
        for r in rows
    ]
    routes = [str((r.meta or {}).get("route") or "") for r in rows]
    meta = doc.meta or {}
    info = _DocInfo(
        title=doc.title,
        pages=meta.get("pages"),
        origin=meta.get("origin"),
    )
    return info, chunks, routes


class _DocInfo:
    """The document facts the header needs, decoupled from the ORM row so the
    rendering above is testable with no database."""

    __slots__ = ("title", "pages", "origin")

    def __init__(self, title, pages=None, origin=None):
        self.title = title
        self.pages = pages
        self.origin = origin


async def _read_department_doc(args: dict[str, Any]) -> str:
    department = current_department()
    if department is None:
        return NO_DEPARTMENT

    document_id = args.get("document_id")
    if not isinstance(document_id, str) or not document_id.strip():
        return (
            "ERROR: 'document_id' is required — take it from a "
            "search_department_docs result's 'doc=' field."
        )

    try:
        start_line = int(args.get("start_line", 1) or 1)
    except (TypeError, ValueError):
        return "ERROR: 'start_line' must be an integer (1-based)."
    max_lines = args.get("max_lines")
    try:
        max_lines = int(max_lines) if max_lines is not None else None
    except (TypeError, ValueError):
        return "ERROR: 'max_lines' must be an integer."

    found = await _fetch_document(document_id.strip(), department.id)
    if found is None:
        return NOT_FOUND
    doc, chunks, routes = found

    if not chunks:
        return (
            f'"{doc.title}" has no indexed text. It may be an image-only or '
            "empty file; open it with its download link instead."
        )

    from ...config import get_settings

    lines = to_lines(chunks, overlap=get_settings().rag_chunk_overlap_chars)
    shown, last, truncated, hard_cut = window(
        lines, start_line, max_lines,
        line_cap=READ_DOC_MAX_LINES, char_budget=DOC_MAX_CHARS,
    )
    if not shown:
        return (
            f"ERROR: start_line={start_line} is past the end — this document has "
            f"{len(lines)} line(s)."
        )

    recovered = any(r in RECOVERED_ROUTES for r in routes)
    header = _header(doc, lines, max(1, start_line), last, truncated, hard_cut, recovered)
    return "\n".join(header + [""] + shown)


SPEC = LocalToolSpec(
    name="read_department_doc",
    description=(
        "Read ONE of the current department's documents in full, by its "
        "'document_id' — use this when the user asks to summarise a document, or "
        "asks what else it says beyond the passage a search returned. Get the id "
        "from a search_department_docs result's 'doc=' field. Page with "
        "'start_line' (1-based) and 'max_lines'; the header gives the total and "
        "the exact start_line to resume from. This reads the department's own "
        "corpus — for a file the USER attached to this chat use read_document."
    ),
    parameters={
        "type": "object",
        "properties": {
            "document_id": {
                "type": "string",
                "description": "Id from a search result's 'doc=' field.",
            },
            "start_line": {
                "type": "integer",
                "description": "1-based first line to return (default 1).",
            },
            "max_lines": {
                "type": "integer",
                "description": "Max lines this call (capped ~400).",
            },
        },
        "required": ["document_id"],
    },
    func=_read_department_doc,
)
