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

from sqlalchemy import delete, func, literal, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from .cursors import (
    decode_seq_cursor,
    decode_session_cursor,
    encode_seq_cursor,
    encode_session_cursor,
)
from .models import ROLE_ASSISTANT, ROLE_USER, ChatMessage, ChatSession

TITLE_MAX = 80
DEFAULT_PAGE_LIMIT = 30
MAX_PAGE_LIMIT = 100


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


async def get_context_tail(
    session: AsyncSession, *, session_id: str, user_id: int, max_messages: int
) -> list[ChatMessage]:
    """The newest `max_messages` of a thread, ascending by seq, for the PROMPT.

    Two things make this distinct from a thread page:

    `trace` and `sources` are NOT selected. Neither is ever in a prompt and they
    are the fat JSONB columns — loading them only to discard them was most of
    the old cost.

    This is a DB-side bound applied BEFORE the token budget, so a 500-turn
    thread never materializes. `max_messages` is far more than any budget can
    hold, so it never decides what the model sees; the budget does.

    Ownership is in the same WHERE as the page — never fetch-then-check.
    """
    newest = (
        select(
            ChatMessage.seq,
            ChatMessage.role,
            ChatMessage.content,
            ChatMessage.attachments,
        )
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .where(ChatSession.id == session_id, ChatSession.user_id == user_id)
        .order_by(ChatMessage.seq.desc())
        .limit(max_messages)
        .subquery()
    )
    result = await session.execute(select(newest).order_by(newest.c.seq.asc()))
    # Detached, partially-populated ChatMessage objects: the pure context module
    # reads role/content/attachments and nothing else.
    out = []
    for row in result.all():
        m = ChatMessage(
            session_id=session_id, seq=row.seq, role=row.role, content=row.content
        )
        m.attachments = row.attachments
        out.append(m)
    return out


async def get_thread_page(
    session: AsyncSession,
    *,
    session_id: str,
    user_id: int,
    limit: int = DEFAULT_PAGE_LIMIT,
    before_seq: str | None = None,
) -> tuple[Optional[ChatSession], list[ChatMessage], Optional[str]]:
    """One page of a thread: (session_row, messages_ascending, next_cursor).

    Returns (None, [], None) when the session is unknown or not this user's —
    the router turns that into 404, and we never confirm it exists.

    The page SELECTED is the newest `limit` messages (a chat opens at the
    bottom); the page RETURNED is ascending by `seq` so the frontend renders
    top-to-bottom unchanged. The cursor walks older.

    Anchored on `seq`, not `created_at`: seq is already UNIQUE(session_id, seq)
    and already the relationship's order_by, so it is a total order with no
    tiebreaker needed.
    """
    limit = max(1, min(limit, MAX_PAGE_LIMIT))
    row = await get_owned_session(session, session_id=session_id, user_id=user_id)
    if row is None:
        return None, [], None

    stmt = (
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.seq.desc())
        .limit(limit + 1)
    )
    if before_seq is not None:
        stmt = stmt.where(ChatMessage.seq < decode_seq_cursor(before_seq))

    newest_first = list((await session.execute(stmt)).scalars().all())
    has_more = len(newest_first) > limit
    newest_first = newest_first[:limit]
    next_cursor = (
        encode_seq_cursor(newest_first[-1].seq) if has_more and newest_first else None
    )
    return row, list(reversed(newest_first)), next_cursor


async def list_sessions_page(
    session: AsyncSession,
    *,
    user_id: int,
    limit: int = DEFAULT_PAGE_LIMIT,
    cursor: str | None = None,
) -> tuple[list[tuple[ChatSession, int]], str | None]:
    """One page of (session, message_count), newest-updated first.

    KEYSET, not offset: every turn bumps `updated_at`, so rows move between
    pages while the user scrolls and offset paging would duplicate and skip.
    `id` is the tiebreaker because `updated_at` is not unique.

    The count is a CORRELATED SUBQUERY, so it is computed for the ~30 rows
    actually returned. The old outer join + GROUP BY aggregated every message
    the user had ever sent, to populate a field for rows below the fold.
    """
    limit = max(1, min(limit, MAX_PAGE_LIMIT))
    count_sq = (
        select(func.count(ChatMessage.id))
        .where(ChatMessage.session_id == ChatSession.id)
        .correlate(ChatSession)
        .scalar_subquery()
    )
    stmt = (
        select(ChatSession, count_sq)
        .where(ChatSession.user_id == user_id)
        .order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
        .limit(limit + 1)  # one extra row tells us whether more exist
    )
    if cursor is not None:
        after_updated, after_id = decode_session_cursor(cursor)
        stmt = stmt.where(
            tuple_(ChatSession.updated_at, ChatSession.id)
            < tuple_(literal(after_updated), literal(after_id))
        )

    rows = [(row[0], row[1]) for row in (await session.execute(stmt)).all()]
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        encode_session_cursor(rows[-1][0].updated_at, rows[-1][0].id)
        if has_more and rows
        else None
    )
    return rows, next_cursor


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
