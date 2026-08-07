"""Async HTTP client wrapping the Ollama REST API.

Ported from the local-ai-model project. We talk to Ollama directly with httpx
(no ollama SDK). A single shared ``httpx.AsyncClient`` is owned by this class and
managed by the FastAPI lifespan (see ``app.main``). Ollama only runs the model;
this gateway is always just an HTTP client of it.
"""

from __future__ import annotations

import json
from typing import Any

import httpx


class OllamaError(Exception):
    """Raised for any failure talking to Ollama.

    ``status_code`` is the HTTP status the gateway returns to its own caller
    (502 when Ollama is unreachable, 404 when a model isn't pulled, etc.).
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
        """True if Ollama answers /api/tags within a short timeout."""
        try:
            resp = await self._client.get("/api/tags", timeout=5.0)
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def list_models(self) -> dict[str, Any]:
        return await self._get_json("/api/tags")

    # ---- generation (non-streaming) ----
    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post_json("/api/chat", payload)

    async def generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post_json("/api/generate", payload)

    async def embeddings(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post_json("/api/embeddings", payload)

    # ---- streaming chat ----
    async def open_chat_stream(self, payload: dict[str, Any]) -> httpx.Response:
        """Open a streaming POST to /api/chat.

        Connection/HTTP errors are raised here (before any bytes stream) so the
        router can map them to a clean status. On success returns an open
        streaming Response the caller must iterate then ``aclose()``.
        """
        request = self._client.build_request("POST", "/api/chat", json=payload)
        try:
            resp = await self._client.send(request, stream=True)
        except httpx.ConnectError as exc:
            raise OllamaError(502, self._unreachable_msg(exc)) from exc
        except httpx.TimeoutException as exc:
            raise OllamaError(504, f"Ollama request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise OllamaError(502, f"Error talking to Ollama: {exc}") from exc

        if resp.status_code >= 400:
            body = await resp.aread()
            await resp.aclose()
            raise OllamaError(
                resp.status_code,
                _error_from_body(body, f"Ollama returned HTTP {resp.status_code}"),
            )
        return resp

    async def stream_chat(self, payload: dict[str, Any]):
        """Stream /api/chat as parsed NDJSON chunks (dicts).

        Wraps open_chat_stream: connection/HTTP errors raise OllamaError before
        the first chunk; each yielded value is one decoded JSON line. Blank or
        malformed lines are skipped. Caller should pass ``stream: True``.
        """
        resp = await self.open_chat_stream(payload)
        try:
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
        finally:
            await resp.aclose()

    # ---- internals ----
    def _unreachable_msg(self, exc: Exception) -> str:
        return (
            f"Cannot reach Ollama at {self.base_url}. "
            f"Is `ollama serve` running? ({exc})"
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
