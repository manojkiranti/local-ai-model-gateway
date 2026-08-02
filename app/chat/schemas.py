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


class ChatTurnRequest(BaseModel):
    session_id: Optional[str] = None  # omit to start a new conversation
    message: str = Field(..., min_length=1)
    model: Optional[str] = None  # per-request override; else DEFAULT_CHAT_MODEL
    stream: bool = False
    options: Optional[dict] = None  # passthrough Ollama options (temperature, …)

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
