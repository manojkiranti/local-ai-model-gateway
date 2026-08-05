"""Turn orchestration for /v1/chat.

`open_turn` implements the common front half of a turn: resolve or create the
session (verifying ownership), persist the user message IMMEDIATELY (its own
commit — so a later model failure never loses what the user typed), and return
the rebuilt context (prior clean turns + the new user message) for the model.
"""

from __future__ import annotations

import asyncio

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..files import readers
from ..files import repository as files_repo
from . import repository as repo
from .models import ChatSession


async def _resolve_attachments(
    session: AsyncSession, *, user_id: int, file_ids: list[str] | None
) -> list[dict] | None:
    """Verify the caller owns each attached file and summarize it. Returns the
    persisted attachment records [{id, filename, summary}] or None. Raises 404
    for an unknown/foreign id (never leak another user's file)."""
    if not file_ids:
        return None
    records: list[dict] = []
    for fid in file_ids:
        row = await files_repo.get_owned_file(session, file_id=fid, user_id=user_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"attached file not found: {fid}")
        try:
            summary = await asyncio.to_thread(readers.summarize, row.path)
            summary_text = summary.text()
        except readers.ReadError:
            summary_text = ""  # still attach it; the read tool will report the error
        records.append({"id": row.id, "filename": row.filename, "summary": summary_text})
    return records


async def open_turn(
    session: AsyncSession,
    *,
    user_id: int,
    session_id: str | None,
    message: str,
    file_ids: list[str] | None = None,
) -> tuple[ChatSession, list[dict[str, str]]]:
    """Returns (chat_session, context_messages) with the user row committed.

    context_messages = prior clean turns + (optional attachment note) + the new
    user message, ready to hand to the model. Raises 404 if a given session_id
    isn't owned by this user, or if an attached file_id isn't owned by them.
    """
    # Verify + summarize attachments BEFORE persisting anything (so a bad id is a
    # clean 404 and doesn't leave a half-written turn).
    attachments = await _resolve_attachments(session, user_id=user_id, file_ids=file_ids)

    if session_id:
        chat_session = await repo.get_session_with_messages(
            session, session_id=session_id, user_id=user_id
        )
        if chat_session is None:
            raise HTTPException(status_code=404, detail="session not found")
        context = repo.build_context_messages(chat_session.messages)
    else:
        chat_session = await repo.create_session(
            session, user_id=user_id, title=repo.make_title(message)
        )
        context = []

    await repo.add_user_message(
        session, session_id=chat_session.id, content=message, attachments=attachments
    )
    await session.commit()  # user message persisted immediately

    if attachments:
        context.append(
            {"role": "system", "content": repo.format_attachment_note(attachments)}
        )
    context.append({"role": "user", "content": message})
    return chat_session, context
