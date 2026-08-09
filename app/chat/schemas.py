"""Request/response schemas for the (now stateful) chat endpoint.

The client sends a single new `message` plus an optional `session_id`; the server
owns conversation state (loads prior turns, calls the model, persists both rows).
Omit `session_id` to start a new conversation.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from ..agent.schemas import TraceEntry


class TurnMessage(BaseModel):
    """A single visible message (role + content)."""

    role: str
    content: str


class SourceOut(BaseModel):
    """One department document an answer was grounded in.

    Document-level, not passage-level: a reader wants one link per document with
    the relevant pages listed, not one entry per retrieved chunk.
    """

    document_id: str
    title: str
    department_code: str
    file_name: Optional[str] = None
    file_type: Optional[str] = None
    # Pages the cited passages came from, ascending. Empty for formats with no
    # pagination (CSV/XLSX/typed text) — not an error.
    pages: list[int] = []
    # True when the model's [N] markers named this document; False when it is
    # shown because the answer was grounded in it without an explicit citation.
    cited: bool = False
    # Derived at serialization from department_code + document_id, never stored.
    # Fetch it WITH the bearer header and make a blob URL — an <a href> cannot
    # send the token.
    download_url: Optional[str] = None


class ChatTurnRequest(BaseModel):
    session_id: Optional[str] = None  # omit to start a new conversation
    message: str = Field(..., min_length=1)
    model: Optional[str] = None  # per-request override; else DEFAULT_CHAT_MODEL
    stream: bool = False
    options: Optional[dict] = None  # passthrough Ollama options (temperature, …)
    # Ids of previously uploaded files (POST /v1/files) to attach to this turn;
    # the gateway verifies ownership and tells the model it can read them.
    file_ids: Optional[list[str]] = None
    # Department tab code (e.g. "hr"). REQUIRED only to OPEN a new department
    # chat — it binds the new session. On an existing bound session it is an
    # optional consistency check (409 on mismatch); the server reads the
    # department from chat_sessions.department_id, never from this field.
    department: Optional[str] = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {"message": "Say hello in one line.", "stream": False}
        }
    )


class ChatTurnResponse(BaseModel):
    session_id: str
    message: TurnMessage
    model: str
    stop_reason: str  # completed | max_iterations | error
    # Execution trace when tools were used this turn; null for a tool-free turn.
    trace: Optional[list[TraceEntry]] = None
    # Department documents behind this answer; null when no corpus was searched.
    # NOT gated by EXPOSE_TRACE — sources are a product feature, not diagnostics.
    sources: Optional[list[SourceOut]] = None
