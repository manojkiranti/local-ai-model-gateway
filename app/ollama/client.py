"""Async HTTP client for an OpenAI-compatible chat-completions server.

We talk to the `/v1/*` surface with httpx (no `ollama` SDK, and no `openai` SDK
either — it would not accumulate streamed tool-call fragments for us). Ollama is
the current backend; because this file is the only place that knows the wire
format, pointing `OLLAMA_BASE_URL` at vLLM / llama.cpp / LiteLLM needs no
changes outside it. A single shared ``httpx.AsyncClient`` is owned here and
managed by the FastAPI lifespan (see ``app.main``).
"""

from __future__ import annotations

import json
from typing import Any

import httpx


class OllamaError(Exception):
    """Raised for any failure talking to the model server.

    ``status_code`` is the HTTP status the gateway returns to its own caller
    (502 when the model server is unreachable, 404 when a model isn't pulled,
    etc.). The class name is kept for now — Ollama is the current backend and
    renaming would ripple through imports across the app.
    """

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


def _error_from_body(body: bytes | str, fallback: str) -> str:
    """Extract Ollama's ``{"error": "..."}`` message from a response body."""
    try:
        data = json.loads(body)
        if isinstance(data, dict) and data.get("error"):
            return str(data["error"])
    except (ValueError, TypeError):
        pass
    return fallback


# ---- OpenAI-compatible SSE / tool-call plumbing --------------------------
# This block is the ONLY place that knows the wire format. `stream_chat`
# converts it into normalized events, so the agent loop stays transport-blind
# and swapping Ollama for vLLM/LiteLLM means editing this file alone.

SSE_DONE = object()  # sentinel: the server sent `data: [DONE]`


def parse_sse_line(line: str) -> Any:
    """Decode one SSE line.

    Returns a decoded dict, the ``SSE_DONE`` sentinel, or ``None`` for anything
    that carries no payload (blank separators, ``:`` keepalive comments,
    non-``data`` fields, or malformed JSON).
    """
    line = line.strip()
    if not line or line.startswith(":"):  # blank separator or keepalive comment
        return None
    if not line.startswith("data:"):  # `event:`/`id:`/`retry:` carry no payload
        return None
    payload = line[len("data:") :].strip()
    if payload == "[DONE]":
        return SSE_DONE
    try:
        return json.loads(payload)
    except ValueError:
        return None  # a truncated/garbled chunk must not kill the whole turn


def merge_tool_call_deltas(
    acc: dict[int, dict[str, Any]], deltas: list[dict[str, Any]]
) -> None:
    """Fold streamed ``tool_calls`` deltas into ``acc`` in place, keyed by index.

    Handles BOTH shapes we may face: Ollama's shim sends a call whole in one
    delta, while vLLM streams `arguments` in fragments that must be
    concatenated. Blank fields never overwrite values already established —
    only the first delta of a fragmented call carries `id`/`name`.
    """
    for delta in deltas:
        index = delta.get("index") or 0
        slot = acc.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if delta.get("id"):
            slot["id"] = delta["id"]
        function = delta.get("function") or {}
        if function.get("name"):
            slot["name"] = function["name"]
        if function.get("arguments"):
            slot["arguments"] += function["arguments"]


def finalize_tool_calls(acc: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten the accumulator into index-ordered complete calls.

    A blank id gets a synthesised ``call_<index>``: ids correlate our
    ``role:"tool"`` results back to the assistant's calls, so an id-less server
    would otherwise break multi-tool turns. Ollama 0.32.5 does supply ids; this
    keeps other backends safe.
    """
    calls: list[dict[str, Any]] = []
    for index, slot in sorted(acc.items()):
        calls.append({
            "id": slot["id"] or f"call_{index}",
            "name": slot["name"],
            "arguments": slot["arguments"],
        })
    return calls


class OllamaClient:
    def __init__(self, base_url: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- health / discovery ----
    async def is_healthy(self) -> bool:
        """True if the model server answers /v1/models within a short timeout."""
        try:
            resp = await self._client.get("/v1/models", timeout=5.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def list_models(self) -> dict[str, Any]:
        return await self._get_json("/v1/models")

    # ---- generation (non-streaming) ----
    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post_json("/v1/chat/completions", payload)

    async def embeddings(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post_json("/v1/embeddings", payload)

    # ---- streaming chat ----
    async def open_chat_stream(self, payload: dict[str, Any]) -> httpx.Response:
        """Open a streaming POST to /v1/chat/completions.

        Connection/HTTP errors are raised here (before any bytes stream) so the
        router can map them to a clean status. On success returns an open
        streaming Response the caller must iterate then ``aclose()``.
        """
        request = self._client.build_request(
            "POST", "/v1/chat/completions", json={**payload, "stream": True}
        )
        try:
            resp = await self._client.send(request, stream=True)
        except httpx.ConnectError as exc:
            raise OllamaError(502, self._unreachable_msg(exc)) from exc
        except httpx.TimeoutException as exc:
            raise OllamaError(504, f"Model server request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise OllamaError(502, f"Error talking to model server: {exc}") from exc

        if resp.status_code >= 400:
            body = await resp.aread()
            await resp.aclose()
            raise OllamaError(
                resp.status_code,
                _error_from_body(body, f"Model server returned HTTP {resp.status_code}"),
            )
        return resp

    async def stream_chat(self, payload: dict[str, Any]):
        """Stream a turn as NORMALIZED events, hiding the wire format.

        Yields, in order:
          {"type":"content","text": str}    a non-empty assistant content delta
          {"type":"tool_calls","calls":[…]} once, after the stream ends, if any
          {"type":"finish","reason": str|None}  always last

        Tool calls are buffered until the stream ends because a fragmenting
        server (vLLM) only has complete `arguments` at that point; Ollama's shim
        sends them whole, and both collapse to the same event here. The agent
        loop only needs tool calls after the stream completes, so buffering
        costs it nothing.

        We iterate ``aiter_lines()`` deliberately: httpx reassembles lines
        across HTTP chunk boundaries. Splitting ``aiter_bytes()`` on "\\n\\n" by
        hand would truncate JSON intermittently under load.
        """
        resp = await self.open_chat_stream(payload)
        tool_acc: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        try:
            async for line in resp.aiter_lines():
                chunk = parse_sse_line(line)
                if chunk is None:
                    continue
                if chunk is SSE_DONE:
                    break
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0] or {}
                delta = choice.get("delta") or {}

                text = delta.get("content")
                if text:  # servers pad with "" on role/tool-call/finish chunks
                    yield {"type": "content", "text": text}

                deltas = delta.get("tool_calls")
                if deltas:
                    merge_tool_call_deltas(tool_acc, deltas)

                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
        finally:
            await resp.aclose()

        calls = finalize_tool_calls(tool_acc)
        if calls:
            yield {"type": "tool_calls", "calls": calls}
        yield {"type": "finish", "reason": finish_reason}

    # ---- internals ----
    def _unreachable_msg(self, exc: Exception) -> str:
        return (
            f"Cannot reach the model server at {self.base_url}. "
            f"Is it running? ({exc})"
        )

    async def _get_json(self, path: str) -> dict[str, Any]:
        try:
            resp = await self._client.get(path)
        except httpx.ConnectError as exc:
            raise OllamaError(502, self._unreachable_msg(exc)) from exc
        except httpx.TimeoutException as exc:
            raise OllamaError(504, f"Ollama request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise OllamaError(502, f"Error talking to Ollama: {exc}") from exc
        return self._parse(resp)

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._client.post(path, json=payload)
        except httpx.ConnectError as exc:
            raise OllamaError(502, self._unreachable_msg(exc)) from exc
        except httpx.TimeoutException as exc:
            raise OllamaError(504, f"Ollama request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise OllamaError(502, f"Error talking to Ollama: {exc}") from exc
        return self._parse(resp)

    @staticmethod
    def _parse(resp: httpx.Response) -> dict[str, Any]:
        if resp.status_code >= 400:
            # Ollama uses 404 for "model not found, try pulling it first".
            raise OllamaError(
                resp.status_code,
                _error_from_body(resp.content, f"Ollama returned HTTP {resp.status_code}"),
            )
        return resp.json()
