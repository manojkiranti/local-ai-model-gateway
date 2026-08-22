"""Read DTOs for chat history (what the frontend renders).

Request schemas for the turn endpoints live with their routers (chat/, agent/);
these are the session/message shapes returned by GET /v1/sessions[/{id}].
"""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    seq: int
    role: str
    content: str
    # Agent turns carry the per-iteration execution trace; chat turns are null.
    trace: Optional[list[Any]] = None
    # Department documents an assistant answer cited, with `download_url` filled
    # in by the router. Unlike `trace` this is never suppressed by EXPOSE_TRACE.
    sources: Optional[list[Any]] = None
    model: Optional[str] = None
    created_at: datetime


class SessionSummary(BaseModel):
    """One row in GET /v1/sessions (no messages, but a count for the sidebar)."""

    id: str
    title: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    message_count: int


class SessionPage(BaseModel):
    """GET /v1/sessions — one page of the sidebar.

    An envelope rather than a bare array so pagination is visible in the
    OpenAPI schema. A cursor hidden in a header would let a client that ignores
    it read page one and believe it had everything.
    """

    items: list[SessionSummary]
    next_cursor: Optional[str] = None


class SessionDetail(BaseModel):
    """GET /v1/sessions/{id} — the full thread."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    messages: list[MessageOut]
    # Walks OLDER messages. Null when the thread's first message is included.
    next_cursor: Optional[str] = None
