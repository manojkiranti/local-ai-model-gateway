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
    model: Optional[str] = None
    created_at: datetime


class SessionSummary(BaseModel):
    """One row in GET /v1/sessions (no messages, but a count for the sidebar)."""

    id: str
    title: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    message_count: int


class SessionDetail(BaseModel):
    """GET /v1/sessions/{id} — the full thread."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    messages: list[MessageOut]
