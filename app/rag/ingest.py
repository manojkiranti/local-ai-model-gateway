"""Persisting a parsed, embedded document — atomically.

Everything slow (parse, chunk, embed) happens BEFORE this module is called, so
the transaction here is short. The sequence is:

    BEGIN
      lock the document row (FOR UPDATE) and re-check it is not archived
      DELETE the document's existing chunks
      INSERT the new ones, batched ~500 rows per statement (SAME transaction)
      UPDATE the document: status/chunk_count/embed_model/embed_dim
    COMMIT

Batching bounds per-statement memory without giving up atomicity. On failure the
caller rolls back, and the consequences are the ones we want: a new document
exposes zero chunks rather than a partial index, and a re-ingest keeps serving
the previous complete version until the replacement commits.

`embedding NOT NULL` guarantees no chunk is ever unsearchable; this transaction
is the separate guarantee that no document is ever half-indexed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from .chunking import Chunk
from .documents import lock_document
from .models import STATUS_ARCHIVED, STATUS_READY, Document, DocumentChunk

# Rows per INSERT statement. Bounds statement size; atomicity comes from the
# surrounding transaction, not from doing it in one statement.
CHUNK_INSERT_BATCH = 500


class DocumentGone(Exception):
    """The document was archived or deleted while it was being ingested.

    Not a failure of the document — a failure of THIS job. The worker records
    the job as failed and leaves the document exactly as the archive left it.
    """


async def archive_chunks(session: AsyncSession, *, document_id: str) -> None:
    """Remove every chunk for a document (used by archive and by replacement)."""
    await session.execute(
        delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
    )


async def replace_chunks(
    session: AsyncSession,
    *,
    document_id: str,
    department_id: int,
    chunks: Sequence[Chunk],
    embeddings: Sequence[Sequence[float]],
    embed_model: str,
    embed_dim: int,
) -> int:
    """Swap in a document's chunks and mark it ready. Returns the row count.

    Does NOT commit — the worker owns the boundary so a failure anywhere in the
    sequence rolls the whole thing back.
    """
    if len(chunks) != len(embeddings):
        raise ValueError(f"{len(chunks)} chunks but {len(embeddings)} embeddings")
    if not chunks:
        raise ValueError("refusing to store a document with zero chunks")
    # Belt and braces: vector(1536) would reject this too, but failing here
    # gives a clear message instead of a constraint error mid-transaction.
    for i, vec in enumerate(embeddings):
        if len(vec) != embed_dim:
            raise ValueError(
                f"embedding {i} has {len(vec)} dimensions, expected {embed_dim}"
            )

    # Serialize against archive_document, and re-read the status UNDER the lock.
    # The document may have been archived while we were parsing and embedding;
    # writing chunks now would resurrect it.
    doc = await lock_document(session, document_id)
    if doc is None:
        raise DocumentGone(f"document {document_id} no longer exists")
    if doc.status == STATUS_ARCHIVED:
        raise DocumentGone(
            f"document {document_id} was archived while it was being ingested"
        )

    await archive_chunks(session, document_id=document_id)

    rows = [
        {
            "document_id": document_id,
            # Passed explicitly: the composite FK requires it to be the
            # document's own department, and Postgres enforces that.
            "department_id": department_id,
            "chunk_index": chunk.chunk_index,
            "content": chunk.content,
            "embedding": list(vec),
            "token_count": chunk.token_count,
            "page_number": chunk.page_number,
            "section": chunk.section,
            "element_type": chunk.element_type,
            # `{}` for every generic path, matching the column's server default.
            # Carries the NRB extraction route (native/legacy_conversion/ocr) and
            # its converter or OCR provenance, so a citation can state where the
            # text came from. Never consulted by retrieval or ranking.
            "metadata": chunk.meta or {},
        }
        for chunk, vec in zip(chunks, embeddings)
    ]

    for start in range(0, len(rows), CHUNK_INSERT_BATCH):
        await session.execute(
            DocumentChunk.__table__.insert(), rows[start : start + CHUNK_INSERT_BATCH]
        )

    await session.execute(
        update(Document)
        .where(Document.id == document_id)
        .values(
            status=STATUS_READY,
            chunk_count=len(rows),
            embed_model=embed_model,
            embed_dim=embed_dim,
            updated_at=datetime.now(timezone.utc),
        )
    )
    return len(rows)
