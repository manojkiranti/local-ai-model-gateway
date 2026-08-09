"""ORM models for chat history.

A conversation is a `ChatSession` owning an ordered list of `ChatMessage` rows.
Only CLEAN turns are stored as rows: one user row + one assistant row per turn.
Agent-loop internals (tool calls, repeats) are NOT rows — they live in the
assistant row's `trace` JSONB, so the visible thread is always
`user, assistant, user, assistant, …`.

IDs are UUID-hex (like the file store): unguessable and stable render keys for
the frontend. `seq` gives deterministic per-session ordering because UUID PKs
don't sort chronologically.
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import DateTime

from ..db.base import Base

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"


def _uuid_hex() -> str:
    return uuid4().hex


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_hex)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Truncated first user message; rename is a later slice.
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Which department tab this conversation was opened in. NULL = general chat
    # (no RAG). RESTRICT rather than SET NULL: deleting a department must not
    # silently rewrite an old HR session into a general one — soft-disable
    # departments instead (departments.is_active).
    department_id: Mapped[int | None] = mapped_column(
        ForeignKey("departments.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Bumped on every new message so threads sort by recency.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.seq",
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (UniqueConstraint("session_id", "seq", name="uq_chat_messages_session_seq"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid_hex)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)  # per-session 1,2,3…
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Agent turns: the TraceEntry[] from the loop. Chat turns: NULL.
    trace: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Files the user attached to THIS user message (list of {id, filename,
    # summary}); NULL when nothing was attached. Persisted so the attachment
    # note is re-injected on later turns without the frontend resending ids.
    attachments: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Model that produced an assistant row (NULL for user rows).
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    session: Mapped["ChatSession"] = relationship(back_populates="messages")
