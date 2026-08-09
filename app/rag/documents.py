"""Data-access for corpus documents.

Convention as elsewhere: takes an AsyncSession, does not commit. The one place
that owns a transaction deliberately is `ingest.replace_chunks`.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import STATUS_ARCHIVED, STATUS_READY, Document


class DocumentConflict(Exception):
    """A non-archived document with this content already exists here."""


def content_hash_of(data: bytes) -> str:
    """sha256 of the bytes (or of typed text encoded utf-8). Drives
    `ux_documents_active_content`, which makes re-upload idempotent."""
    return hashlib.sha256(data).hexdigest()


async def create_document(
    session: AsyncSession,
    *,
    department_id: int,
    title: str,
    source: str,
    file_type: str,
    content_hash: str,
    storage_key: str | None = None,
    file_name: str | None = None,
    uploaded_by: int | None = None,
) -> Document:
    """Insert a `pending` document. Raises DocumentConflict when a non-archived
    document with the same content already exists in this department."""
    doc = Document(
        department_id=department_id,
        title=title,
        source=source,
        file_type=file_type,
        content_hash=content_hash,
        storage_key=storage_key,
        file_name=file_name,
        uploaded_by=uploaded_by,
    )
    session.add(doc)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise DocumentConflict(
            "a document with identical content already exists in this department"
        ) from exc
    return doc


async def get_document(session: AsyncSession, document_id: str) -> Document | None:
    """Read a document, always fresh from the database.

    `populate_existing` for the same reason as `jobs.get_job`: the worker and
    the archive path both mutate these rows, sessions run with
    `expire_on_commit=False`, and a cached read would report a stale status.
    """
    return (
        await session.execute(
            select(Document)
            .where(Document.id == document_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def lock_document(session: AsyncSession, document_id: str) -> Document | None:
    """Fetch a document with `SELECT ... FOR UPDATE`.

    Archiving and the ingest replacement both mutate the same document and its
    chunks. Without serializing on this row they interleave and the document is
    **resurrected**: the worker parses and embeds, an admin archives (chunks
    deleted, status='archived'), then the worker's replacement commits, putting
    the chunks back and flipping status to 'ready'. Whoever takes the lock first
    wins, and the loser sees the committed outcome and acts on it.
    """
    return (
        await session.execute(
            select(Document)
            .where(Document.id == document_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def list_documents(
    session: AsyncSession,
    department_id: int,
    *,
    include_archived: bool = False,
    ready_only: bool = False,
) -> list[Document]:
    """`ready_only` is the member view: a pending or failed document is not part
    of the corpus their answers can cite. Admins get everything non-archived."""
    stmt = select(Document).where(Document.department_id == department_id)
    if ready_only:
        stmt = stmt.where(Document.status == STATUS_READY)
    elif not include_archived:
        stmt = stmt.where(Document.status != STATUS_ARCHIVED)
    result = await session.execute(stmt.order_by(Document.created_at.desc()))
    return list(result.scalars())


async def archive_document(session: AsyncSession, document_id: str) -> bool:
    """Retire a document: delete every chunk, keep the row for audit.

    Chunks carry no status and HNSW filters before a join would be reachable, so
    an archived document whose chunks survived would keep being retrieved and
    cited. `chunk_count` is deliberately NOT reset — it is the audit record of
    what the document held.

    Takes `FOR UPDATE` on the document row so it cannot interleave with an
    in-flight ingest replacement (see `lock_document`). If archive commits
    first, the worker's replacement aborts; if the replacement commits first,
    archive then removes the new chunks and wins.
    """
    from .ingest import archive_chunks  # local import: ingest imports this module

    doc = await lock_document(session, document_id)
    if doc is None:
        return False
    await archive_chunks(session, document_id=document_id)
    doc.status = STATUS_ARCHIVED
    await session.flush()
    return True
