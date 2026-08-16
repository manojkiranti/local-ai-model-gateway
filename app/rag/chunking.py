"""Turning parsed content into retrievable chunks.

Two shapes, because prose and grids fail differently:

- **Prose** splits on a character budget with overlap, preferring paragraph then
  sentence then word boundaries, so a sentence straddling a boundary is still
  retrievable from either side.
- **Tables** repeat the header row in EVERY chunk. A spreadsheet row retrieved
  on its own is a list of bare values with no idea what its columns mean; the
  header is what makes a chunk self-describing.

Nothing here does IO or calls a model — it is all pure and unit-tested.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence


@dataclass(frozen=True)
class Chunk:
    content: str
    chunk_index: int
    page_number: int | None = None      # PDF only
    section: str | None = None          # heading path
    element_type: str | None = None     # text|heading|table|list
    token_count: int | None = None
    # Free-form provenance for `document_chunks.metadata`. None for every
    # generic path — a plain upload has nothing to say here and stores `{}`,
    # which is the column default. It exists because NRB text can reach a chunk
    # by three different routes (native, legacy_conversion, ocr) and a citation
    # has to be able to say which. Never read by chunking, retrieval or ranking.
    meta: dict | None = None


@dataclass(frozen=True)
class Block:
    """One parsed element BEFORE it becomes a Chunk.

    Docling emits a Block per layout element — a heading, a list bullet, a
    stray page number. `merge_blocks` is what turns a run of those back into a
    passage.
    """

    text: str
    section: str | None = None
    page_number: int | None = None
    element_type: str = "text"


def _split_point(text: str, limit: int) -> int:
    """Best boundary at or before `limit`: paragraph, then sentence, then word."""
    window = text[:limit]
    for sep in ("\n\n", ". ", "\n", " "):
        idx = window.rfind(sep)
        if idx > limit // 2:            # don't take a uselessly early break
            return idx + len(sep)
    return limit


def chunk_text(
    text: str,
    *,
    max_chars: int,
    overlap_chars: int,
    section: str | None = None,
    page_number: int | None = None,
    element_type: str = "text",
) -> list[Chunk]:
    """Split prose into overlapping chunks of at most `max_chars`."""
    body = text.strip()
    if not body:
        return []

    # A misconfigured overlap >= max_chars would never advance. Clamp so it
    # degrades to a smaller overlap instead of hanging.
    overlap = max(0, min(overlap_chars, max_chars // 2))
    step_floor = max(1, max_chars - overlap)

    chunks: list[Chunk] = []
    pos = 0
    while pos < len(body):
        remaining = body[pos:]
        if len(remaining) <= max_chars:
            piece, advance = remaining, len(remaining)
        else:
            cut = _split_point(remaining, max_chars)
            piece, advance = remaining[:cut], max(cut - overlap, step_floor)
        piece = piece.strip()
        if piece:
            chunks.append(
                Chunk(
                    content=piece,
                    chunk_index=len(chunks),
                    section=section,
                    page_number=page_number,
                    element_type=element_type,
                )
            )
        pos += advance
    return chunks


def chunk_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    sheet_name: str,
    max_chars: int,
) -> list[Chunk]:
    """Group rows into chunks, repeating the header in each so a chunk read in
    isolation still says what its columns are."""
    if not rows:
        return []

    header_line = " | ".join(str(h) for h in headers)
    preamble = f"Sheet: {sheet_name}\n{header_line}\n"

    chunks: list[Chunk] = []
    buffer: list[str] = []
    size = len(preamble)

    def flush() -> None:
        if buffer:
            chunks.append(
                Chunk(
                    content=preamble + "\n".join(buffer),
                    chunk_index=len(chunks),
                    section=sheet_name,
                    element_type="table",
                )
            )

    for row in rows:
        line = " | ".join(str(c) for c in row)
        # A single row wider than the cap still gets emitted: dropping data
        # silently is worse than one oversized chunk.
        if buffer and size + len(line) + 1 > max_chars:
            flush()
            buffer, size = [], len(preamble)
        buffer.append(line)
        size += len(line) + 1
    flush()
    return chunks


def renumber(chunks: Sequence[Chunk]) -> list[Chunk]:
    """Make `chunk_index` contiguous from 0 across concatenated groups —
    `uq_document_chunks_doc_index` requires uniqueness per document."""
    return [replace(c, chunk_index=i) for i, c in enumerate(chunks)]


def merge_blocks(blocks: Sequence[Block], *, max_chars: int) -> list[Block]:
    """Join consecutive blocks that belong to the same passage.

    Without this, chunking one Docling element at a time produced 559 chunks
    averaging 181 characters for a single policy document — half of them under
    60 — so real prose was buried under fragments and `max_chars` never meant
    anything. Measured 2026-08-12; see the design spec.

    Flushes when the passage's identity changes (`section` or `page_number`),
    when a table is involved, or when the budget would be exceeded.
    """
    merged: list[Block] = []
    buffer: list[Block] = []

    def flush() -> None:
        nonlocal buffer
        if buffer:
            merged.append(
                replace(buffer[0], text="\n".join(b.text for b in buffer))
            )
            buffer = []

    for block in blocks:
        if not block.text.strip():
            continue
        # A table stands alone: a markdown grid spliced into surrounding
        # sentences is unreadable to the model AND to the lexical channel.
        if block.element_type == "table":
            flush()
            merged.append(block)
            continue
        if buffer:
            head = buffer[0]
            size = sum(len(b.text) + 1 for b in buffer)
            if (
                block.section != head.section
                or block.page_number != head.page_number
                or size + len(block.text) + 1 > max_chars
            ):
                flush()
        buffer.append(block)

    flush()
    return merged


def drop_small_blocks(
    blocks: Sequence[Block], *, min_body_chars: int
) -> list[Block]:
    """Remove orphaned fragments. MUST run AFTER `merge_blocks`.

    The order is load-bearing. Run this first and it deletes real content: the
    45-character body "means Assets Liability Committee of the Bank." is a
    glossary definition that is short only because the term it defines is a
    separate Docling element. After merging, anything still this small is
    layout debris — a page number, a stray "th" from "279th".

    Tables are exempt at any size: a small table is real content, and its
    information density is not proportional to its character count.
    """
    return [
        b
        for b in blocks
        if b.element_type == "table" or len(b.text.strip()) >= min_body_chars
    ]
