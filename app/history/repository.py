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


def format_attachment_note(attachments: list[dict[str, Any]], *, active: bool = True) -> str:
    """A short system note naming files attached to a user message, so the model
    knows their ids and can call inspect_excel/read_excel. Pure/formatting only.

    `active=False` marks a SUPERSEDED set — files attached earlier in the
    conversation that a newer upload has replaced. Those are deliberately weaker:
    different wording, and no summary. An identically-worded note for every
    upload is what made a second file get ignored in favour of the first, which
    had a fat summary and a whole assistant answer behind it.
    """
    if not active:
        lines = ["Files attached earlier in this conversation (superseded — use "
                 "one of these ONLY if the user names that file):"]
        for a in attachments:
            lines.append(f'- id={a.get("id", "")} "{a.get("filename", "")}"')
        return "\n".join(lines)

    lines = ["Active files for the current request (read documents with "
             "read_document; for spreadsheets use inspect_excel / read_excel, "
             "and total them with aggregate_excel):"]
    for a in attachments:
        fid = a.get("id", "")
        name = a.get("filename", "")
        summary = a.get("summary", "")
        detail = f" ({summary})" if summary else ""
        lines.append(f'- id={fid} "{name}"{detail}')
    return "\n".join(lines)


def build_context_messages(
    messages: list[ChatMessage], *, pending_attachments: bool = False
) -> list[dict[str, str]]:
    """Clean visible turns -> the [{role, content}] the model sees.

    Only role/content is replayed; agent turns contribute their final answer
    (their `trace` is history, not context). A user message that carried file
    attachments re-emits its attachment note (a system message) just before it,
    so 'now total column B' on a later turn still knows the file ids without the
    frontend resending them. Ordering is the caller's (seq).

    Exactly ONE attachment set is ever active: the newest. Older sets are
    replayed as superseded (see `format_attachment_note`) so the model doesn't
    have to guess which of several ids the user means. `pending_attachments`
    says the turn being opened carries its own upload — then EVERY replayed set
    is superseded, because the caller appends the active note itself. With no
    new upload, the most recent replayed set stays active.
    """
    attached_at = [
        i for i, m in enumerate(messages) if m.role == ROLE_USER and m.attachments
    ]
    active_idx = None if pending_attachments else (attached_at[-1] if attached_at else None)

    out: list[dict[str, str]] = []
    for i, m in enumerate(messages):
        if m.role == ROLE_USER and m.attachments:
            out.append({
                "role": "system",
                "content": format_attachment_note(m.attachments, active=(i == active_idx)),
            })
        out.append({"role": m.role, "content": m.content})
    return out


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
) -> ChatMessage:
    return await add_message(
        session,
        session_id=session_id,
        role=ROLE_ASSISTANT,
        content=content,
        trace=trace,
        model=model,
    )
