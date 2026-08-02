"""Trace schemas for the agent loop.

The loop is exposed through the unified /v1/chat endpoint (there is no separate
/v1/agent). These types describe the per-iteration execution `trace` that the
endpoint streams (in the `done` event) and persists as JSONB on the assistant row.
"""

from typing import Optional

from pydantic import BaseModel


class ToolCallTrace(BaseModel):
    name: str
    arguments: object = None
    result: Optional[str] = None
    status: str = "ok"  # ok | unknown_tool | bad_arguments | repeat | tool_error


class TraceEntry(BaseModel):
    iteration: int
    assistant_content: Optional[str] = None
    tool_calls: list[ToolCallTrace]
