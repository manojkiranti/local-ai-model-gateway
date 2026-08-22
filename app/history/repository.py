"""Data-access for chat history.

Functions take an AsyncSession and DO NOT commit — the router owns transaction
boundaries so it can honour the lifecycle: user row committed immediately, then
the model call, then the assistant row. `add_message` allocates `seq` under a
row lock (SELECT … FOR UPDATE on the session) so concurrent turns can't collide;
the UNIQUE(session_id, seq) constraint is the final safety net.

The two pure helpers (`build_context_messages`, `make_title`) have no DB
dependency and are unit-tested without Postgres.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .models import ROLE_ASSISTANT, ROLE_USER, ChatMessage, ChatSession

TITLE_MAX = 80


# --------------------------------------------------------------------------- #
# Pure helpers (no DB)
# --------------------------------------------------------------------------- #
def make_title(first_message: str) -> str:
    """A session title from the first user message: single line, truncated."""
    flat = " ".join(first_message.split()).strip()
    if len(flat) <= TITLE_MAX:
        return flat
    return flat[: TITLE_MAX - 1].rstrip() + "…"  # ellipsis


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
async def create_session(
    session: AsyncSession,
    *,
    user_id: int,
    title: Optional[str],
    department_id: Optional[int] = None,
) -> ChatSession:
    """`department_id` binds the conversation to a department FOR ITS LIFETIME;
    None is a general chat. Only a NEW session may be given one — an existing
    general chat cannot be adopted into a department (see rag.access)."""
    chat_session = ChatSession(user_id=user_id, title=title, department_id=department_id)
    session.add(chat_session)
    await session.flush()  # populate id/defaults without committing
    return chat_session


async def get_owned_session(
    session: AsyncSession, *, session_id: str, user_id: int
) -> Optional[ChatSession]:
    """Fetch a session only if it belongs to this user (else None -> 404)."""
    result = await session.execute(
        select(ChatSession).where(
            ChatSession.id == session_id, ChatSession.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def get_session_with_messages(
    session: AsyncSession, *, session_id: str, user_id: int
) -> Optional[ChatSession]:
    result = await session.execute(
        select(ChatSession)
        .where(ChatSession.id == session_id, ChatSession.user_id == user_id)
        .options(selectinload(ChatSession.messages))
    )
    return result.scalar_one_or_none()


async def list_sessions(
    session: AsyncSession, *, user_id: int
) -> list[tuple[ChatSession, int]]:
    """(session, message_count) for a user, newest-updated first."""
    count_col = func.count(ChatMessage.id)
    result = await session.execute(
        select(ChatSession, count_col)
        .outerjoin(ChatMessage, ChatMessage.session_id == ChatSession.id)
        .where(ChatSession.user_id == user_id)
        .group_by(ChatSession.id)
        .order_by(ChatSession.updated_at.desc())
    )
    return [(row[0], row[1]) for row in result.all()]


async def delete_session(
    session: AsyncSession, *, session_id: str, user_id: int
) -> bool:
    """Delete an owned session (messages cascade). True if a row was removed."""
    result = await session.execute(
        delete(ChatSession).where(
            ChatSession.id == session_id, ChatSession.user_id == user_id
        )
    )
    return result.rowcount > 0


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #
async def add_message(
    session: AsyncSession,
    *,
    session_id: str,
    role: str,
    content: str,
    trace: Optional[list[Any]] = None,
    model: Optional[str] = None,
    attachments: Optional[list[Any]] = None,
    sources: Optional[list[Any]] = None,
) -> ChatMessage:
    """Append a message with the next per-session seq, and bump the session's
    updated_at. Locks the session row so concurrent turns serialize."""
    # Lock the session row for the duration of this transaction.
    await session.execute(
        select(ChatSession.id).where(ChatSession.id == session_id).with_for_update()
    )
    next_seq = (
        await session.execute(
            select(func.coalesce(func.max(ChatMessage.seq), 0) + 1).where(
                ChatMessage.session_id == session_id
            )
        )
    ).scalar_one()

    message = ChatMessage(
        session_id=session_id,
        seq=next_seq,
        role=role,
        content=content,
        trace=trace,
        model=model,
        attachments=attachments,
        sources=sources,
    )
    session.add(message)
    # Touch the session so threads re-sort by recency (server clock).
    await session.execute(
        update(ChatSession)
        .where(ChatSession.id == session_id)
        .values(updated_at=func.now())
    )
    await session.flush()
    return message


# Convenience wrappers for readable call sites.
async def add_user_message(
    session: AsyncSession,
    *,
    session_id: str,
    content: str,
    attachments: Optional[list[Any]] = None,
) -> ChatMessage:
    return await add_message(
        session, session_id=session_id, role=ROLE_USER, content=content,
        attachments=attachments,
    )


async def add_assistant_message(
    session: AsyncSession,
    *,
    session_id: str,
    content: str,
    trace: Optional[list[Any]] = None,
    model: Optional[str] = None,
    sources: Optional[list[Any]] = None,
) -> ChatMessage:
    return await add_message(
        session,
        session_id=session_id,
        role=ROLE_ASSISTANT,
        content=content,
        trace=trace,
        model=model,
        sources=sources,
    )
