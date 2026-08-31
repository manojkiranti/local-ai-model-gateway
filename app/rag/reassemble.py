"""Put a corpus document back together from the chunks that were indexed.

Pure module — no DB, no HTTP, no model — for the reason `permissions.py` and
`ranking.py` are pure: it decides what text a reader is shown, and proving that
should need no database.

**Why the chunks and not the original file.** `read_department_doc` could open
the document's bytes and re-parse them, the way `read_document` does for an
attached upload. For the NRB corpus that would be actively wrong: a legacy-font
PDF's own text layer is Preeti glyph soup and a scanned page's is empty, which
is the entire reason `app/nrb/recovery.py` exists. Reading bytes would hand the
model exactly the junk the pipeline works to withhold (§16's `_withhold` rule),
while the RECOVERED text — the text search actually matched — sits in
`document_chunks`. Reading the chunks also makes one guarantee no re-parse can:
what the reader sees is what the corpus searched.

Two things the reassembly has to undo, because both are artifacts of indexing
rather than of the document:

  * **the heading prefix** — `parsing._attach_headings` prepends the heading
    path to every chunk's CONTENT (the `tsv` column is generated from `content`
    alone, so a heading kept only in metadata would be unsearchable). Joined
    naively that reprints the heading at every chunk boundary.
  * **the overlap** — `chunking` advances by `max_chars - overlap`, so
    consecutive chunks genuinely share their boundary text.

De-overlapping errs toward KEEPING text: the search is bounded by the
configured overlap, and a boundary it cannot prove is left alone. Printing a
sentence twice is untidy; deleting one the reader needed is not.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkText:
    """One indexed chunk, as much of it as reassembly needs."""

    chunk_index: int
    content: str
    page_number: int | None = None
    section: str | None = None


def _strip_section(content: str, section: str | None) -> str:
    """Remove the heading path `parsing` prepended, if this chunk carries it."""
    if not section:
        return content
    prefix = f"{section}\n\n"
    return content[len(prefix):] if content.startswith(prefix) else content


def _dedupe_overlap(previous: str, current: str, overlap: int) -> tuple[str, bool]:
    """Drop the leading part of `current` that repeats the tail of `previous`.

    Returns the remainder AND whether anything was actually removed — the
    caller needs that second value: a chunk whose overlap was stripped is a
    mid-line CONTINUATION (the split point was a character budget, not a line
    break), while a chunk that shares nothing starts fresh.

    Only the configured overlap is searched, longest match first. Anything
    longer is real repetition in the document and is left alone.
    """
    if overlap <= 0 or not previous or not current:
        return current, False
    limit = min(overlap, len(previous), len(current))
    for size in range(limit, 0, -1):
        if previous.endswith(current[:size]):
            return current[size:], True
    return current, False


def to_lines(chunks: list[ChunkText], *, overlap: int) -> list[str]:
    """The document as flat lines, in chunk order, ready for `_paging.window`.

    Page changes appear as `[page N]` marker lines — the same convention
    `app/files/documents.py` uses for an attached PDF, so there is only ever ONE
    paging unit for the model to reason about.
    """
    if not chunks:
        return []

    lines: list[str] = []
    previous_page: int | None = None
    previous_section: str | None = None
    tail = ""

    for chunk in sorted(chunks, key=lambda c: c.chunk_index):
        if chunk.page_number is not None and chunk.page_number != previous_page:
            lines.append(f"[page {chunk.page_number}]")
            previous_page = chunk.page_number
            # A new page breaks textual continuity, so nothing can overlap it.
            tail = ""

        body = _strip_section(chunk.content, chunk.section)
        if chunk.section and chunk.section != previous_section:
            # Emit the heading once, where it changes — the shape the document
            # actually had before indexing repeated it.
            lines.extend([chunk.section, ""])
            previous_section = chunk.section

        body, continued = _dedupe_overlap(tail, body, overlap)
        if not body:
            continue

        head, _, rest = body.partition("\n")
        if continued and lines:
            # Rejoin the split word/sentence rather than starting a new line.
            separator = "" if head.startswith(" ") or lines[-1].endswith(" ") else " "
            lines[-1] = f"{lines[-1]}{separator}{head}"
            if rest:
                lines.extend(rest.split("\n"))
        else:
            lines.extend(body.split("\n"))
        tail = body

    return lines
