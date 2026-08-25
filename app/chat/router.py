"""POST /v1/chat — the single authenticated, persisted, tool-capable chat turn.

The gateway owns conversation state (Pattern A): the client sends one new
`message` + optional `session_id`; the server rebuilds context from history and
runs the agent loop (tools always available — the model calls one only when
useful). Both streaming and non-streaming go through the SAME loop engine.

- stream=false -> JSON {session_id, message, model, stop_reason, trace?}
- stream=true  -> NDJSON typed events (token / tool_call / tool_result / done),
                  with the new session id in the `X-Session-Id` response header.

Lifecycle (docs/superpowers/specs/2026-08-02-*): the user row is committed
immediately; the assistant row on `done`. The persisted turn is the clean final
answer + trace, even though the live stream also shows tool activity/narration.
"""

import json
from contextlib import contextmanager
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..agent.loop import run_turn, stream_turn
from ..auth.dependencies import get_current_user
from ..config import Settings
from ..db.session import SessionLocal, get_session
from ..files.source import turn_files
from ..history import repository as repo
from ..history.service import open_turn
from ..mcp.client import MCPClient, MCPUnavailableError
from ..mcp.dependencies import get_mcp_identity
from ..mcp.grants import McpIdentity
from ..rag.context import rag_context
from ..rag.sources import (
    SourceCollector,
    resolve_sources,
    source_scope,
    with_download_urls,
)
from ..ollama.client import OllamaClient
from ..users.models import User
from .schemas import ChatTurnRequest, ChatTurnResponse, TurnMessage

router = APIRouter(prefix="/v1", tags=["chat"])


def _trace_if_tools(trace: Optional[list[dict[str, Any]]]) -> Optional[list[dict[str, Any]]]:
    """Persist/return a trace only when tools were actually used; a plain chat
    turn (no tool calls) keeps trace=null so the visible thread stays clean."""
    if trace and any(entry.get("tool_calls") for entry in trace):
        return trace
    return None


def _client_trace(
    trace: Optional[list[dict[str, Any]]], settings: Settings
) -> Optional[list[dict[str, Any]]]:
    """What the CLIENT is allowed to see. The trace is always persisted (audit);
    EXPOSE_TRACE=false stops it leaving the gateway so a production UI has
    nothing to draw a "how it worked" panel from."""
    return trace if settings.expose_trace else None


@contextmanager
def _department_scope(department):
    """Install the department contextvar for the turn, or nothing at all.

    A general chat has no department; entering `rag_context(None)` would make
    `current_department()` return a bogus context, so the no-department case is
    an explicit no-op rather than a null object.
    """
    if department is None:
        yield
    else:
        with rag_context(department):
            yield


def _final_content(result: dict[str, Any]) -> str:
    return (
        result.get("final_answer")
        or result.get("error_message")
        or f"(no answer: {result.get('stop_reason')})"
    )


@router.post(
    "/chat",
    summary="Chat turn (authenticated, persisted, tool-capable; streaming or not)",
    responses={
        401: {"description": "Missing/invalid JWT."},
        404: {"description": "Unknown session, or model not pulled on Ollama."},
        502: {"description": "Ollama or MCP unreachable."},
    },
)
async def chat(
    req: ChatTurnRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    identity: McpIdentity = Depends(get_mcp_identity),
):
    ollama: OllamaClient = request.app.state.ollama
    mcp: MCPClient = request.app.state.mcp
    settings: Settings = request.app.state.settings
    # Per-request model override without mutating shared settings.
    run_settings = (
        settings.model_copy(update={"agent_model": req.model}) if req.model else settings
    )
    model = run_settings.agent_model

    # Resolve/create session + persist the user message (committed here). Any
    # attached file_ids are verified owned here (404 on a foreign id).
    chat_session, context, department = await open_turn(
        session, user=user, session_id=req.session_id, message=req.message,
        file_ids=req.file_ids, department=req.department,
    )
    sid = chat_session.id

    if req.stream:
        # Pre-flight MCP so "server down" is a clean 502 before the stream starts
        # (we can't 502 mid-stream once the body is flowing).
        try:
            await mcp.ensure_reachable()
        except MCPUnavailableError as exc:
            raise HTTPException(status_code=502, detail=exc.message) from exc

        async def event_stream():
            final_answer = error_message = stop_reason = None
            trace = None
            sources = None
            # Constructed OUTSIDE the `with` on purpose. `source_scope` resets the
            # contextvar on exit, but the `finally` below — which writes the
            # assistant row — runs after that. Holding the object here keeps the
            # turn's provenance reachable either way.
            collector = SourceCollector()
            try:
                # Files any tool creates this turn are owned by this user +
                # session, and the read tools resolve ids owner-scoped. Must be
                # set INSIDE the generator Starlette iterates (contextvar).
                # `rag_context` alongside `turn_files`, and for the same reason:
                # both are contextvars the tools read, and a contextvar set in
                # the router before returning StreamingResponse is NOT visible
                # inside the generator Starlette later iterates.
                with turn_files(user_id=user.id, session_id=sid), \
                        _department_scope(department), \
                        source_scope(collector):
                    async for event in stream_turn(
                        messages=context, ollama=ollama, mcp=mcp,
                        settings=run_settings, identity=identity,
                    ):
                        if event.get("type") == "done":
                            stop_reason = event.get("stop_reason")
                            final_answer = event.get("final_answer")
                            error_message = event.get("error_message")
                            trace = event.get("trace")
                            # Citations are resolved against the FINAL answer, so
                            # this can only happen once the loop is done.
                            sources = resolve_sources(
                                collector.records, final_answer or ""
                            )
                            # Send the client the SAME trace the non-streaming
                            # path returns: null unless tools actually ran, and
                            # null outright when EXPOSE_TRACE is off. The loop's
                            # raw trace has one entry per iteration even for a
                            # tool-free turn, which is what made the UI render
                            # "1 iteration · 0 tool calls".
                            event = {
                                **event,
                                "session_id": sid,
                                "trace": _client_trace(_trace_if_tools(trace), run_settings),
                                # Deliberately NOT run through _client_trace:
                                # sources are the answer's citations, and a
                                # deployment that hides diagnostics still wants
                                # them.
                                "sources": with_download_urls(sources),
                            }
                        yield (json.dumps(event) + "\n").encode()
            except MCPUnavailableError as exc:  # rare: handshake failed post pre-flight
                stop_reason, error_message = "error", exc.message
                done = {"type": "done", "session_id": sid, "stop_reason": "error",
                        "iteration_count": 0, "final_answer": None,
                        "error_message": exc.message, "trace": [], "sources": None}
                yield (json.dumps(done) + "\n").encode()
            finally:
                content = final_answer or error_message or f"(no answer: {stop_reason})"
                # Fresh session: the request-scoped one isn't safe mid-stream.
                async with SessionLocal() as s2:
                    await repo.add_assistant_message(
                        s2, session_id=sid, content=content,
                        trace=_trace_if_tools(trace), model=model,
                        # Stored WITHOUT download_url — derived on read.
                        sources=sources,
                    )
                    await s2.commit()

        return StreamingResponse(
            event_stream(),
            media_type="application/x-ndjson",
            headers={"X-Session-Id": sid},
        )

    # --- non-streaming: collect the same loop into a result dict ---
    collector = SourceCollector()
    try:
        # Files created this turn are owned by this user + session; read tools
        # resolve attached file ids owner-scoped.
        with turn_files(user_id=user.id, session_id=sid), \
                _department_scope(department), \
                source_scope(collector):
            result = await run_turn(
                messages=context, ollama=ollama, mcp=mcp,
                settings=run_settings, identity=identity,
            )
    except MCPUnavailableError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc

    content = _final_content(result)
    trace = _trace_if_tools(result["trace"])
    # Citations are resolved against the final answer text, so this follows the
    # loop rather than living inside it.
    sources = resolve_sources(collector.records, result.get("final_answer") or "")
    await repo.add_assistant_message(
        session, session_id=sid, content=content, trace=trace, model=model,
        sources=sources,  # stored without download_url — derived on read
    )
    await session.commit()
    return ChatTurnResponse(
        session_id=sid,
        message=TurnMessage(role="assistant", content=content),
        model=model,
        stop_reason=result["stop_reason"],
        trace=_client_trace(trace, run_settings),
        sources=with_download_urls(sources),
    )
