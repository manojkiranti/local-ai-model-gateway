"""Hand-rolled agentic tool-calling loop — no framework, glass-box by design.

The loop is an async EVENT GENERATOR (`stream_turn`): it streams Ollama and yields
typed events as it runs, so callers can surface live glass-box activity —
  {"type":"token","content":…}        assistant content delta
  {"type":"tool_call","name":…,…}     a tool is about to run
  {"type":"tool_result","name":…,…}   that tool's outcome (status)
  {"type":"done", stop_reason, iteration_count, final_answer, error_message, trace}
`run_turn` collects the generator into the result dict for non-streaming callers,
so both paths share ONE engine.

Flow per iteration:
    (a) stream the model via ollama.stream_chat (normalized content/tool_calls/
        finish events — the client owns the wire format); emit token deltas
    (b) append the assistant message to `messages` (OpenAI tool_calls shape)
    (c) no tool_calls  -> that's the final answer, emit done, stop
    (d) tool_calls     -> emit tool_call/tool_result, append role:"tool" results
                          keyed by tool_call_id, loop
Capped at AGENT_MAX_ITERATIONS.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from ..config import Settings
from ..mcp.client import MCPClient, MCPUnavailableError
from ..ollama.client import OllamaClient, OllamaError
from ..tools.registry import ToolRegistry, UnknownToolError

logger = logging.getLogger("app.agent")

SYSTEM_PROMPT = (
    "You are a helpful assistant with access to tools. Use the provided tools "
    "when they help answer the user's request. Only call tools that are listed. "
    "When you have enough information, stop calling tools and reply with a final "
    "answer in plain text."
)

MAX_TOOL_RESULT_CHARS = 8000  # fed back to the model (protect num_ctx)
TRACE_RESULT_CHARS = 600  # kept in the trace / repeat cache


def _coerce_arguments(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Normalize model-provided tool arguments to a dict. Returns (args, error)."""
    if raw is None:
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    if isinstance(raw, str):
        if raw.strip() == "":
            return {}, None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return None, f"arguments were not valid JSON: {exc}"
        if isinstance(parsed, dict):
            return parsed, None
        return None, "arguments JSON was not an object"
    return None, f"unexpected arguments type: {type(raw).__name__}"


def _tool_message(call_id: str, content: str) -> dict[str, Any]:
    """A tool result, correlated to the assistant's call by id.

    The OpenAI surface correlates on `tool_call_id` (native Ollama used a
    `tool_name` field). Ids come from the server, with a synthesised fallback in
    `finalize_tool_calls`, so this is always populated — which is what lets a
    single turn run several tools without the model confusing their results.
    """
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _repeat_key(name: str, args: dict[str, Any]) -> str:
    try:
        return f"{name}::{json.dumps(args, sort_keys=True, default=str)}"
    except (TypeError, ValueError):
        return f"{name}::{args!r}"


def _done_event(
    *, stop_reason: str, iteration_count: int, final_answer: str | None,
    error_message: str | None, trace: list[dict[str, Any]],
) -> dict[str, Any]:
    # No session_id here — the router injects it before serializing to the wire.
    return {
        "type": "done",
        "stop_reason": stop_reason,
        "iteration_count": iteration_count,
        "final_answer": final_answer,
        "error_message": error_message,
        "trace": trace,
    }


async def _loop_events(
    registry: ToolRegistry,
    base_messages: list[dict[str, Any]],
    ollama: OllamaClient,
    settings: Settings,
) -> AsyncIterator[dict[str, Any]]:
    """The loop as an event generator. Yields token/tool_call/tool_result events
    live, then exactly one terminal `done` event carrying the collected trace."""
    messages: list[dict[str, Any]] = []
    if not base_messages or base_messages[0].get("role") != "system":
        messages.append({"role": "system", "content": SYSTEM_PROMPT})
    messages.extend(base_messages)

    trace: list[dict[str, Any]] = []
    call_cache: dict[str, str] = {}  # (name+args) -> previous result (repeat detection)

    stop_reason = "max_iterations"
    final_answer: str | None = None
    error_message: str | None = None
    iteration_count = 0
    aborted = False

    offered = [t["function"]["name"] for t in registry.list_ollama_tools()]

    for i in range(1, settings.agent_max_iterations + 1):
        iteration_count = i
        logger.info("──── iteration %d ──── tools offered: %s", i, offered)

        # (a) stream the model, giving it the merged tool list every time.
        payload = {
            "model": settings.agent_model,
            "messages": messages,
            "tools": registry.list_ollama_tools(),
            "stream": True,
            "options": {
                "temperature": settings.agent_temperature,
                "num_ctx": settings.agent_num_ctx,
            },
        }
        content_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        try:
            async for event in ollama.stream_chat(payload):
                kind = event.get("type")
                if kind == "content":
                    content_parts.append(event["text"])
                    yield {"type": "token", "content": event["text"]}
                elif kind == "tool_calls":
                    # The client buffers until complete, so this arrives once,
                    # after the stream — fragmenting servers (vLLM) and whole-
                    # call servers (Ollama) look identical from here.
                    tool_calls = event["calls"]
        except OllamaError as exc:
            logger.error("iteration %d: model stream failed: %s", i, exc.message)
            stop_reason, error_message = "error", exc.message
            trace.append({"iteration": i, "assistant_content": None, "tool_calls": []})
            break

        # (b) record the assistant message we reconstructed from the stream.
        assistant_content = "".join(content_parts)
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": assistant_content}
        if tool_calls:
            # Replay in OpenAI shape; `arguments` goes back exactly as received
            # so we never re-serialise the model's own JSON.
            assistant_msg["tool_calls"] = [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in tool_calls
            ]
        messages.append(assistant_msg)

        # (c) no tool calls -> final answer, done.
        if not tool_calls:
            logger.info("iteration %d: FINAL answer (%d chars)", i, len(assistant_content))
            final_answer = assistant_content
            stop_reason = "completed"
            trace.append({"iteration": i, "assistant_content": assistant_content, "tool_calls": []})
            break

        # (d) run each requested tool, emitting live tool_call/tool_result events.
        requested = [c["name"] for c in tool_calls]
        logger.info("iteration %d: model requested %d tool call(s): %s", i, len(tool_calls), requested)

        entry_calls: list[dict[str, Any]] = []
        for call in tool_calls:
            call_id = call["id"]
            name = call["name"] or ""
            raw_args = call["arguments"]

            # Coerce up front so the trace stores a dict on every transport —
            # native Ollama gave us dicts, the /v1 surface gives JSON strings,
            # and traces are persisted (chat_messages.trace) and read later.
            args, parse_error = _coerce_arguments(raw_args)
            record: dict[str, Any] = {
                "name": name,
                "arguments": args if parse_error is None else raw_args,
                "result": None,
                "status": "ok",
            }
            yield {"type": "tool_call", "name": name,
                   "arguments": record["arguments"], "iteration": i}

            # ROBUSTNESS 1: unknown tool -> tell the model, list valid names.
            if not registry.has_tool(name):
                result = f"ERROR: tool '{name}' does not exist. Valid tools are: {registry.tool_names()}"
                record["status"], record["result"] = "unknown_tool", result
                messages.append(_tool_message(call_id, result))
                logger.info("   ! unknown tool '%s' -> nudged model to retry", name)
                entry_calls.append(record)
                yield {"type": "tool_result", "name": name, "status": "unknown_tool",
                       "result": result[:TRACE_RESULT_CHARS], "iteration": i}
                continue

            # ROBUSTNESS 2: malformed args -> feed the parse error back.
            if parse_error is not None:
                result = f"ERROR: could not parse arguments for '{name}': {parse_error}"
                record["status"], record["result"] = "bad_arguments", result
                messages.append(_tool_message(call_id, result))
                logger.info("   ! bad arguments for '%s': %s", name, parse_error)
                entry_calls.append(record)
                yield {"type": "tool_result", "name": name, "status": "bad_arguments",
                       "result": result[:TRACE_RESULT_CHARS], "iteration": i}
                continue

            # ROBUSTNESS 3: identical repeat call -> nudge instead of re-running.
            key = _repeat_key(name, args)
            if key in call_cache:
                result = (
                    f"NOTE: you already called '{name}' with these arguments and got: "
                    f"{call_cache[key]}. Use that result, or make a different call."
                )
                record["status"], record["result"] = "repeat", result
                messages.append(_tool_message(call_id, result))
                logger.info("   ~ repeat call '%s' -> nudged model", name)
                entry_calls.append(record)
                yield {"type": "tool_result", "name": name, "status": "repeat",
                       "result": result[:TRACE_RESULT_CHARS], "iteration": i}
                continue

            # Dispatch for real (routes to MCP or local backend).
            try:
                result = await registry.dispatch(name, args)
            except MCPUnavailableError as exc:
                result = f"ERROR: MCP server became unreachable: {exc.message}"
                record["status"], record["result"] = "tool_error", result[:TRACE_RESULT_CHARS]
                messages.append(_tool_message(call_id, result))
                entry_calls.append(record)
                logger.error("   ! MCP unreachable mid-run: %s", exc.message)
                yield {"type": "tool_result", "name": name, "status": "tool_error",
                       "result": result[:TRACE_RESULT_CHARS], "iteration": i}
                stop_reason, error_message, aborted = "error", exc.message, True
                break
            except UnknownToolError:
                result = f"ERROR: tool '{name}' does not exist. Valid tools are: {registry.tool_names()}"
                record["status"] = "unknown_tool"
            except Exception as exc:  # noqa: BLE001 - ROBUSTNESS 4: surface, keep looping
                result = f"ERROR: tool '{name}' raised: {exc}"
                record["status"] = "tool_error"
                logger.info("   ! tool '%s' raised: %s", name, exc)

            result = result if result is not None else ""
            call_cache[key] = result[:TRACE_RESULT_CHARS]
            record["result"] = result[:TRACE_RESULT_CHARS]
            messages.append(_tool_message(call_id, result[:MAX_TOOL_RESULT_CHARS]))
            logger.info("   -> '%s' [%s] returned %d chars", name, record["status"], len(result))
            entry_calls.append(record)
            yield {"type": "tool_result", "name": name, "status": record["status"],
                   "result": result[:TRACE_RESULT_CHARS], "iteration": i}

        trace.append({"iteration": i, "assistant_content": assistant_content, "tool_calls": entry_calls})
        if aborted:
            break
    else:
        logger.info("stopped: max_iterations (%d) reached", settings.agent_max_iterations)

    yield _done_event(
        stop_reason=stop_reason,
        iteration_count=iteration_count,
        final_answer=final_answer,
        error_message=error_message,
        trace=trace,
    )


async def stream_turn(
    *,
    messages: list[dict[str, Any]],
    ollama: OllamaClient,
    mcp: MCPClient,
    settings: Settings,
    user_email: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Build the tool registry (local + MCP) and stream the loop's events.

    A single MCP session spans the whole run. `user_email` is forwarded to the
    MCP server (x-user-email) so its tools can scope to the authenticated caller.
    A connect-time failure raises MCPUnavailableError (streaming callers pre-flight
    reachability); a mid-run failure surfaces as a `done` event, stop_reason=error.
    """
    registry = ToolRegistry()
    registry.register_local_tools()

    if mcp.configured:
        async with mcp.session(user_email=user_email) as session:
            await registry.load_mcp_tools(mcp, session)
            logger.info("agent run: %d tool(s) available %s", len(registry.tool_names()), registry.tool_names())
            async for event in _loop_events(registry, messages, ollama, settings):
                yield event
        return

    logger.warning("MCP not configured — running with local tools only.")
    logger.info("agent run: %d tool(s) available %s", len(registry.tool_names()), registry.tool_names())
    async for event in _loop_events(registry, messages, ollama, settings):
        yield event


async def run_turn(
    *,
    messages: list[dict[str, Any]],
    ollama: OllamaClient,
    mcp: MCPClient,
    settings: Settings,
    user_email: str | None = None,
) -> dict[str, Any]:
    """Collect stream_turn's events into the result dict (non-streaming callers).

    Same engine as streaming — just drains the token/tool events and returns the
    terminal `done` payload as {final_answer, stop_reason, iteration_count,
    error_message, trace}.
    """
    done: dict[str, Any] | None = None
    async for event in stream_turn(
        messages=messages, ollama=ollama, mcp=mcp, settings=settings, user_email=user_email
    ):
        if event.get("type") == "done":
            done = event
    assert done is not None, "loop must always emit a terminal done event"
    return {
        "final_answer": done["final_answer"],
        "stop_reason": done["stop_reason"],
        "iteration_count": done["iteration_count"],
        "error_message": done["error_message"],
        "trace": done["trace"],
    }
