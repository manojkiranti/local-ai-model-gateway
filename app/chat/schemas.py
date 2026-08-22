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
    # Where the document came from: "nrb" for a catalog document, else the
    # document's own source ("upload"/"manual").
    origin: Optional[str] = None
    # NRB-only, and null for anything else. `routes` is the union of the extraction
    # routes behind the pages the model was shown (an NRB PDF is routed per page,
    # §16); `machine_recovered` is true when any of them was OCR'd or converted
    # from a legacy font — text that is retrieval-grade but NOT authoritative for a
    # figure, date or name. `verify_note` carries the exact wording the model was
    # shown, so a UI badge cannot contradict the answer. A client that renders a
    # source with `machine_recovered` MUST show it.
    source_url: Optional[str] = None
    published_at: Optional[str] = None
    routes: Optional[list[str]] = None
    machine_recovered: Optional[bool] = None
    verify_note: Optional[str] = None


class ChatTurnRequest(BaseModel):
    session_id: Optional[str] = None  # omit to start a new conversation
    # 8000 chars — same convention as `MAX_TOOL_RESULT_CHARS`/
    # `rag_tool_result_max_chars` elsewhere in this codebase, and picked over
    # a tighter cap on purpose: users legitimately paste stack traces, tables
    # and long questions, and 4000 chars (~700-800 English words) rejects
    # ordinary pastes with a 422 — a worse regression than the overflow this
    # exists to prevent. The frontend's composer (../react/local-ai-model-
    # frontend, Composer.tsx) has no client-side length cap today, so this is
    # the only bound in the whole path.
    #
    # Worst-case cost at this cap: 8000 Devanagari chars price at ~10,353
    # tokens through `context.estimate_tokens` (8000/0.85 * 1.10). That is
    # MORE than the ~2941 tokens `context_reserve_tokens` (12000) set aside
    # for "this message + the answer" alongside one realistic RAG result
    # (~9059 tokens) — so an all-Devanagari 8000-char paste landing in the
    # SAME turn as a big RAG result can still exceed the reserve. This is the
    # same non-guarantee CLAUDE.md's "the budget bounds HISTORY, not the
    # PROMPT" finding already documents for stacked tool results: the reserve
    # covers a realistic turn, not every worst case stacked together. A
    # rejected message is a clean FastAPI 422, not something the turn path
    # has to special-case.
    message: str = Field(..., min_length=1, max_length=8000)
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
