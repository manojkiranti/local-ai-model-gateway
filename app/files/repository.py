"""Data-access for generated files.

Same convention as `history/repository.py`: request-scoped functions take an
AsyncSession and DO NOT commit — the caller owns the transaction boundary. The
one exception is the Postgres file sink (see `sink.py`), which uses its own
short-lived session so a created file is durable regardless of the turn's fate.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import GeneratedFile


async def record_file(
    session: AsyncSession,
    *,
    id: str,
    user_id: int,
    filename: str,
    media_type: str,
    size: int,
    path: str,
    session_id: Optional[str] = None,
) -> GeneratedFile:
    """Insert one generated-file row (does not commit)."""
    row = GeneratedFile(
        id=id,
        user_id=user_id,
        session_id=session_id,
        filename=filename,
        media_type=media_type,
        size=size,
        path=path,
    )
    session.add(row)
    await session.flush()
    return row


async def get_owned_file(
    session: AsyncSession, *, file_id: str, user_id: int
) -> Optional[GeneratedFile]:
    """Fetch a file row only if it belongs to this user (else None -> 404)."""
    result = await session.execute(
        select(GeneratedFile).where(
            GeneratedFile.id == file_id, GeneratedFile.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def delete_owned_file(
    session: AsyncSession, *, file_id: str, user_id: int
) -> Optional[str]:
    """Delete a file row the caller owns (does not commit). Returns its on-disk
    path so the router can unlink the file, or None if not owned/unknown."""
    row = await get_owned_file(session, file_id=file_id, user_id=user_id)
    if row is None:
        return None
    path = row.path
    await session.delete(row)
    return path


async def list_files(session: AsyncSession, *, user_id: int) -> list[GeneratedFile]:
    """All of a user's files, newest first (for the 'my files' UI gallery)."""
    result = await session.execute(
        select(GeneratedFile)
        .where(GeneratedFile.user_id == user_id)
        .order_by(GeneratedFile.created_at.desc(), GeneratedFile.id.desc())
    )
    return list(result.scalars().all())
