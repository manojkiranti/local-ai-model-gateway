"""Turn orchestration for /v1/chat.

`open_turn` implements the common front half of a turn: resolve or create the
session (verifying ownership), persist the user message IMMEDIATELY (its own
commit — so a later model failure never loses what the user typed), and return
the rebuilt context (prior clean turns + the new user message) for the model.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from . import repository as repo
from .models import ChatSession


async def open_turn(
    session: AsyncSession, *, user_id: int, session_id: str | None, message: str
) -> tuple[ChatSession, list[dict[str, str]]]:
    """Returns (chat_session, context_messages) with the user row committed.

    context_messages = prior clean turns + the new user message, ready to hand
    to the model. Raises 404 if a given session_id isn't owned by this user.
    """
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

    await repo.add_user_message(session, session_id=chat_session.id, content=message)
    await session.commit()  # user message persisted immediately

    context.append({"role": "user", "content": message})
    return chat_session, context
