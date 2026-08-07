# OpenAI-Compatible Endpoint Port — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the gateway's LLM transport from Ollama's native `/api/chat` to the OpenAI-compatible `/v1/chat/completions` surface, so `OLLAMA_BASE_URL` becomes a real swap point for vLLM / llama.cpp / LiteLLM / TGI.

**Architecture:** The protocol boundary lives entirely in `app/ollama/client.py`. `stream_chat` stops yielding raw wire chunks and instead yields a small **normalized event** vocabulary (`content` / `tool_calls` / `finish`). `app/agent/loop.py` consumes those normalized events and never sees SSE, `choices[0].delta`, or fragment accumulation. Consequence: a future vLLM/LiteLLM swap touches one file, and the fragment accumulator becomes a pure function testable without a server.

**Tech Stack:** Python 3.10, httpx (streaming), FastAPI, pytest + anyio. **No `openai` SDK** — it does not solve fragment accumulation for us (only the beta `beta.chat.completions.stream()` helper accumulates), so it would add a dependency and displace our `OllamaError` → HTTP-status mapping while leaving the hard part unsolved.

## Global Constraints

- Use **this project's venv only**: `.venv/bin/python`, `.venv/bin/pytest`. Never a sibling's.
- **Never** add the `ollama` SDK. **Do not** add the `openai` SDK either (see Tech Stack).
- Iterate the HTTP body with `resp.aiter_lines()`. **Never** `aiter_bytes()`/`aiter_raw()` with manual `\n\n` splitting — SSE events span HTTP chunk boundaries and manual splitting yields intermittent truncated JSON under load, which presents as a flaky model rather than a parser bug.
- Preserve the existing error contract exactly: unreachable → `OllamaError(502)`, timeout → `OllamaError(504)`, upstream ≥400 → `OllamaError(<status>)` with the body's message extracted.
- Preserve the public `stop_reason` values (`completed` / `max_iterations` / `error`) and the `trace` entry shape consumed by `/v1/chat` and persisted to `chat_messages.trace`.
- The agent loop must remain **hand-rolled and readable** — comments explaining *why*, per CLAUDE.md.
- `fetch_url`'s SSRF guards are untouched by this work. Do not modify `app/tools/local/fetch_url.py`.

## Verified Facts (already established — do not re-litigate)

Measured against local Ollama **0.32.5**, model `qwen2.5:latest`, on 2026-08-07:

1. **`tool_call_id` is emitted and non-empty.** Example: `{"id":"call_zra5k6q3","index":0,"type":"function","function":{"name":"calculator","arguments":"{\"expression\":\"17 * 4\"}"}}`. We still synthesise a fallback id (Task 1) because other servers may omit it.
2. **The round trip works.** Sending `{"role":"tool","tool_call_id":"call_zra5k6q3","content":"68"}` back yields a correct natural-language answer. The verified tool message carries **no `name` field** — do not add one; match what was proven.
3. **Ollama's shim does NOT fragment tool-call arguments** — the whole JSON string arrives in one delta. **vLLM does fragment.** Therefore fragmentation fixtures must be **hand-authored**, not recorded. A recorded Ollama stream exercises none of the accumulator logic.
4. `arguments` arrives as a **JSON string** (native `/api/chat` gave a dict). `_coerce_arguments` (`app/agent/loop.py:44`) already accepts both, so it needs no change.
5. Content deltas include **empty strings** (`"content":""`), including on the tool-call chunk and the `finish_reason` chunk. Empty deltas must not be emitted as tokens.
6. Terminator is a literal `data: [DONE]` line.
7. `finish_reason` is `"tool_calls"` or `"stop"` on a final content-less chunk.
8. **`stream_chat`'s wire parsing has zero test coverage today.** `tests/test_agent_loop.py` fakes at the `stream_chat` level (`FakeStreamOllama`), so it tests the loop, not the protocol. Task 1 creates that missing coverage.
9. Local Ollama has `qwen2.5:latest`; `.env` sets `AGENT_MODEL=qwen2.5:latest`. The **server** runs `qwen3.5:35b-a3b` — a different box. Task 3's Modelfile must be applied on **whichever host serves the agent**, with the correct `FROM`.

---

### Task 1: SSE decoding + tool-call accumulation as pure functions

Pure functions first, no HTTP, no wiring. This is where the vLLM-fragmentation risk is actually retired.

**Files:**
- Modify: `app/ollama/client.py` (add module-level helpers above `class OllamaClient`)
- Test: `tests/test_openai_stream_parsing.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `SSE_DONE` — module-level sentinel object.
  - `parse_sse_line(line: str) -> dict[str, Any] | object | None` — returns a decoded JSON dict, the `SSE_DONE` sentinel, or `None` for lines to skip.
  - `merge_tool_call_deltas(acc: dict[int, dict[str, Any]], deltas: list[dict[str, Any]]) -> None` — folds deltas into `acc` in place, keyed by `index`.
  - `finalize_tool_calls(acc: dict[int, dict[str, Any]]) -> list[dict[str, Any]]` — returns `[{"id": str, "name": str, "arguments": str}, ...]` ordered by index, with synthesised ids where blank.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_openai_stream_parsing.py`:

```python
"""Wire-level tests for OpenAI-compatible SSE parsing and tool-call accumulation.

These are PURE function tests — no HTTP, no server. They exist because the two
servers we care about stream tool calls differently and we can only run one:

  * Ollama's /v1 shim sends each tool call WHOLE in a single delta.
  * vLLM FRAGMENTS arguments across many deltas, keyed by `index`.

A recorded Ollama stream therefore proves nothing about the accumulator, so the
fragmented fixtures below are hand-authored from the OpenAI streaming spec.
"""

from app.ollama.client import (
    SSE_DONE,
    finalize_tool_calls,
    merge_tool_call_deltas,
    parse_sse_line,
)


# ---- parse_sse_line ----

def test_parse_sse_line_decodes_data_payload():
    line = 'data: {"choices":[{"delta":{"content":"hi"}}]}'
    assert parse_sse_line(line) == {"choices": [{"delta": {"content": "hi"}}]}


def test_parse_sse_line_returns_sentinel_on_done():
    assert parse_sse_line("data: [DONE]") is SSE_DONE


def test_parse_sse_line_skips_blank_and_keepalive_comments():
    # Per the SSE spec a line starting with ':' is a comment (keepalive).
    assert parse_sse_line("") is None
    assert parse_sse_line("   ") is None
    assert parse_sse_line(": ping") is None
    assert parse_sse_line(":") is None


def test_parse_sse_line_skips_non_data_fields_and_bad_json():
    assert parse_sse_line("event: message") is None
    assert parse_sse_line("id: 42") is None
    assert parse_sse_line("data: {not json") is None


def test_parse_sse_line_tolerates_missing_space_after_colon():
    assert parse_sse_line('data:{"a":1}') == {"a": 1}


# ---- merge_tool_call_deltas / finalize_tool_calls ----

def test_whole_tool_call_in_one_delta_ollama_shape():
    """Ollama 0.32.5 shape — id, name and complete arguments in one delta."""
    acc = {}
    merge_tool_call_deltas(acc, [{
        "id": "call_zra5k6q3", "index": 0, "type": "function",
        "function": {"name": "calculator", "arguments": '{"expression":"17 * 4"}'},
    }])
    assert finalize_tool_calls(acc) == [
        {"id": "call_zra5k6q3", "name": "calculator",
         "arguments": '{"expression":"17 * 4"}'}
    ]


def test_fragmented_tool_call_across_deltas_vllm_shape():
    """vLLM shape — id+name arrive first, then arguments in pieces."""
    acc = {}
    for delta in [
        {"index": 0, "id": "call_abc", "type": "function",
         "function": {"name": "calculator", "arguments": ""}},
        {"index": 0, "function": {"arguments": '{"expr'}},
        {"index": 0, "function": {"arguments": 'ession":'}},
        {"index": 0, "function": {"arguments": '"17 * 4"}'}},
    ]:
        merge_tool_call_deltas(acc, [delta])
    assert finalize_tool_calls(acc) == [
        {"id": "call_abc", "name": "calculator",
         "arguments": '{"expression":"17 * 4"}'}
    ]


def test_two_parallel_fragmented_calls_keyed_by_index():
    acc = {}
    for delta in [
        {"index": 0, "id": "a", "function": {"name": "one", "arguments": '{"x":'}},
        {"index": 1, "id": "b", "function": {"name": "two", "arguments": '{"y":'}},
        {"index": 1, "function": {"arguments": "2}"}},
        {"index": 0, "function": {"arguments": "1}"}},
    ]:
        merge_tool_call_deltas(acc, [delta])
    assert finalize_tool_calls(acc) == [
        {"id": "a", "name": "one", "arguments": '{"x":1}'},
        {"id": "b", "name": "two", "arguments": '{"y":2}'},
    ]


def test_missing_index_defaults_to_zero():
    acc = {}
    merge_tool_call_deltas(acc, [
        {"id": "c1", "function": {"name": "t", "arguments": "{}"}},
    ])
    assert finalize_tool_calls(acc) == [
        {"id": "c1", "name": "t", "arguments": "{}"}
    ]


def test_blank_id_is_synthesised_so_tool_results_can_be_correlated():
    """A server that omits ids must not break `role:tool` correlation."""
    acc = {}
    merge_tool_call_deltas(acc, [
        {"index": 0, "function": {"name": "one", "arguments": "{}"}},
        {"index": 1, "id": "", "function": {"name": "two", "arguments": "{}"}},
    ])
    assert finalize_tool_calls(acc) == [
        {"id": "call_0", "name": "one", "arguments": "{}"},
        {"id": "call_1", "name": "two", "arguments": "{}"},
    ]


def test_later_blank_fields_never_clobber_established_values():
    acc = {}
    merge_tool_call_deltas(acc, [
        {"index": 0, "id": "keep", "function": {"name": "real", "arguments": "{}"}},
    ])
    merge_tool_call_deltas(acc, [
        {"index": 0, "id": "", "function": {"name": "", "arguments": ""}},
    ])
    assert finalize_tool_calls(acc) == [
        {"id": "keep", "name": "real", "arguments": "{}"}
    ]


def test_empty_accumulator_finalizes_to_empty_list():
    assert finalize_tool_calls({}) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_openai_stream_parsing.py -v`
Expected: collection error — `ImportError: cannot import name 'SSE_DONE' from 'app.ollama.client'`

- [ ] **Step 3: Write the implementation**

In `app/ollama/client.py`, insert after `_error_from_body` and before `class OllamaClient`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_openai_stream_parsing.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add app/ollama/client.py tests/test_openai_stream_parsing.py
git commit -m "feat(ollama): add OpenAI SSE parsing + tool-call delta accumulation

Pure helpers with hand-authored fixtures for both wire shapes: Ollama's
shim sends tool calls whole, vLLM fragments arguments across deltas. The
fragmented cases cannot be recorded from our only server, so they are
written from the OpenAI streaming spec."
```

---

### Task 2: Swap the transport to `/v1` and adapt the loop to normalized events

Client and loop move together in **one commit**. They are not separable: the
client alone leaves the gateway non-functional, so no reviewer could approve it
while rejecting the loop half. Committing them separately would also put a
knowingly-failing `tests/test_agent_loop.py` into history. The suite is green at
this task's single commit.

**Files:**
- Modify: `app/ollama/client.py` (`is_healthy`, `list_models`, `chat`, `embeddings`, `open_chat_stream`, `stream_chat`; delete `generate`)
- Modify: `app/agent/loop.py` (docstring, `_tool_message`, the streaming block in `_loop_events`, the assistant-message rebuild, the trace record)
- Test: `tests/test_openai_client_stream.py` (create)
- Test: `tests/test_agent_loop.py` (re-shape the `tool_turn`/`text_turn` fakes, add two tests)

**Interfaces:**
- Consumes: `parse_sse_line`, `merge_tool_call_deltas`, `finalize_tool_calls`, `SSE_DONE` from Task 1.
- Produces:
  - `_tool_message(call_id: str, content: str) -> dict` → `{"role": "tool", "tool_call_id": call_id, "content": content}`.
  - Trace entries keep the shape `{"iteration": int, "assistant_content": str|None, "tool_calls": [{"name","arguments","result","status"}]}` — but `arguments` is now the **coerced dict**, not the raw string (see Step 10).
  - `OllamaClient.stream_chat(payload) -> AsyncIterator[dict]` yielding **normalized events**, the contract the loop consumes:
  - `{"type": "content", "text": str}` — a non-empty content delta.
  - `{"type": "tool_calls", "calls": [{"id","name","arguments"}]}` — emitted **once**, after the stream ends, only if any call was accumulated.
  - `{"type": "finish", "reason": str | None}` — emitted last, always.

  Also: `OllamaClient.chat(payload)` posts to `/v1/chat/completions`; `list_models()`/`is_healthy()` hit `/v1/models`; `embeddings(payload)` posts to `/v1/embeddings`. `generate()` is removed (no `/v1` equivalent; grep confirms no callers).

- [ ] **Step 1: Confirm `generate()` has no callers before deleting it**

Run: `grep -rn "\.generate(" --include=*.py app/ tests/`
Expected: no output. If there IS output, keep `generate()` on `/api/generate` and note the exception in the commit message.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_openai_client_stream.py`:

```python
"""Tests for OllamaClient.stream_chat over a faked /v1 SSE transport.

httpx's MockTransport gives us the real client code path — including
`aiter_lines()`, which is what makes SSE events immune to HTTP chunk
boundaries — without a server.
"""

import httpx
import pytest

from app.ollama.client import OllamaClient, OllamaError


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _client(handler) -> OllamaClient:
    client = OllamaClient(base_url="http://fake:11434", timeout=5.0)
    client._client = httpx.AsyncClient(
        base_url="http://fake:11434", transport=httpx.MockTransport(handler)
    )
    return client


def _sse(*payloads: str) -> bytes:
    return "".join(f"data: {p}\n\n" for p in payloads).encode()


async def _drain(client, payload=None):
    return [ev async for ev in client.stream_chat(payload or {"model": "m"})]


@pytest.mark.anyio
async def test_content_deltas_become_content_events_and_empties_are_dropped():
    body = _sse(
        '{"choices":[{"delta":{"role":"assistant","content":""},"finish_reason":null}]}',
        '{"choices":[{"delta":{"content":"Hel"},"finish_reason":null}]}',
        '{"choices":[{"delta":{"content":"lo"},"finish_reason":null}]}',
        '{"choices":[{"delta":{"content":""},"finish_reason":"stop"}]}',
        "[DONE]",
    )
    client = _client(lambda req: httpx.Response(200, content=body))
    events = await _drain(client)

    assert events == [
        {"type": "content", "text": "Hel"},
        {"type": "content", "text": "lo"},
        {"type": "finish", "reason": "stop"},
    ]
    await client.aclose()


@pytest.mark.anyio
async def test_tool_calls_are_emitted_once_after_the_stream_ends():
    body = _sse(
        '{"choices":[{"delta":{"content":"","tool_calls":[{"id":"call_1","index":0,'
        '"type":"function","function":{"name":"calculator",'
        '"arguments":"{\\"expression\\":\\"17 * 4\\"}"}}]},"finish_reason":null}]}',
        '{"choices":[{"delta":{"content":""},"finish_reason":"tool_calls"}]}',
        "[DONE]",
    )
    client = _client(lambda req: httpx.Response(200, content=body))
    events = await _drain(client)

    assert events == [
        {"type": "tool_calls", "calls": [
            {"id": "call_1", "name": "calculator",
             "arguments": '{"expression":"17 * 4"}'},
        ]},
        {"type": "finish", "reason": "tool_calls"},
    ]
    await client.aclose()


@pytest.mark.anyio
async def test_fragmented_arguments_are_reassembled_before_emission():
    body = _sse(
        '{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_f",'
        '"function":{"name":"calculator","arguments":""}}]}}]}',
        '{"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"{\\"expr"}}]}}]}',
        '{"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"ession\\":\\"1+1\\"}"}}]}}]}',
        '{"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "[DONE]",
    )
    client = _client(lambda req: httpx.Response(200, content=body))
    events = await _drain(client)

    assert events[0] == {"type": "tool_calls", "calls": [
        {"id": "call_f", "name": "calculator",
         "arguments": '{"expression":"1+1"}'},
    ]}
    await client.aclose()


@pytest.mark.anyio
async def test_keepalives_and_garbage_lines_do_not_break_the_turn():
    body = (
        b": ping\n\n"
        b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
        b"data: {truncated\n\n"
        b"event: message\n\n"
        b'data: {"choices":[{"delta":{"content":"b"},"finish_reason":"stop"}]}\n\n'
        b"data: [DONE]\n\n"
    )
    client = _client(lambda req: httpx.Response(200, content=body))
    events = await _drain(client)

    assert [e for e in events if e["type"] == "content"] == [
        {"type": "content", "text": "a"},
        {"type": "content", "text": "b"},
    ]
    await client.aclose()


@pytest.mark.anyio
async def test_stream_posts_to_v1_chat_completions():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, content=_sse("[DONE]"))

    client = _client(handler)
    await _drain(client)
    assert seen["url"] == "http://fake:11434/v1/chat/completions"
    await client.aclose()


@pytest.mark.anyio
async def test_finish_event_is_always_emitted_even_with_no_finish_reason():
    client = _client(lambda req: httpx.Response(200, content=_sse("[DONE]")))
    assert await _drain(client) == [{"type": "finish", "reason": None}]
    await client.aclose()


@pytest.mark.anyio
async def test_upstream_error_status_raises_ollamaerror_with_body_message():
    client = _client(lambda req: httpx.Response(
        404, json={"error": "model 'nope' not found"}))
    with pytest.raises(OllamaError) as exc:
        await _drain(client)
    assert exc.value.status_code == 404
    assert "not found" in exc.value.message
    await client.aclose()


@pytest.mark.anyio
async def test_connect_error_maps_to_502():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler)
    with pytest.raises(OllamaError) as exc:
        await _drain(client)
    assert exc.value.status_code == 502
    await client.aclose()


@pytest.mark.anyio
async def test_timeout_maps_to_504():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    client = _client(handler)
    with pytest.raises(OllamaError) as exc:
        await _drain(client)
    assert exc.value.status_code == 504
    await client.aclose()


@pytest.mark.anyio
async def test_is_healthy_probes_v1_models():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": []})

    client = _client(handler)
    assert await client.is_healthy() is True
    assert seen["url"] == "http://fake:11434/v1/models"
    await client.aclose()
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_openai_client_stream.py -v`
Expected: FAIL — `stream_chat` still yields raw NDJSON dicts, so the equality assertions fail and the URL assertion reads `/api/chat`.

- [ ] **Step 4: Rewrite the client's endpoints and streaming**

In `app/ollama/client.py`, update the module docstring's first paragraph to:

```python
"""Async HTTP client for an OpenAI-compatible chat-completions server.

We talk to the `/v1/*` surface with httpx (no `ollama` SDK, and no `openai` SDK
either — it would not accumulate streamed tool-call fragments for us). Ollama is
the current backend; because this file is the only place that knows the wire
format, pointing `OLLAMA_BASE_URL` at vLLM / llama.cpp / LiteLLM needs no
changes outside it. A single shared ``httpx.AsyncClient`` is owned here and
managed by the FastAPI lifespan (see ``app.main``).
"""
```

Replace the health/discovery and generation methods:

```python
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
```

Delete the `generate()` method entirely (Step 1 confirmed no callers).

Replace `open_chat_stream` and `stream_chat`:

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_openai_client_stream.py tests/test_openai_stream_parsing.py -v`
Expected: all passed

Do **not** run the full suite yet — `tests/test_agent_loop.py` is expected to be red at this point and Steps 6-11 fix it. Do not commit here either.

- [ ] **Step 6: Re-shape the test fakes to emit normalized events**

In `tests/test_agent_loop.py` replace `tool_turn` and `text_turn`:

```python
def tool_turn(name, arguments, call_id="call_test"):
    """One model turn that calls a tool, as normalized client events.

    `arguments` is passed through verbatim so tests can still exercise the
    unparseable-JSON path.
    """
    return [
        {"type": "tool_calls", "calls": [
            {"id": call_id, "name": name, "arguments": arguments},
        ]},
        {"type": "finish", "reason": "tool_calls"},
    ]


def text_turn(text):
    """One model turn that streams a plain answer in two content events."""
    half = len(text) // 2
    return [
        {"type": "content", "text": text[:half]},
        {"type": "content", "text": text[half:]},
        {"type": "finish", "reason": "stop"},
    ]
```

- [ ] **Step 7: Add a test pinning `tool_call_id` correlation and trace-argument normalization**

Append to `tests/test_agent_loop.py`:

```python
class RecordingOllama(FakeStreamOllama):
    """Captures every payload sent, so we can assert the messages we build."""

    def __init__(self, turns):
        super().__init__(turns)
        self.payloads = []

    async def stream_chat(self, payload):
        self.payloads.append(payload)
        async for event in super().stream_chat(payload):
            yield event


@pytest.mark.anyio
async def test_tool_results_are_correlated_by_tool_call_id():
    """The `role:tool` reply must carry the id the assistant's call announced,
    and the assistant turn must be replayed in OpenAI tool_calls shape."""
    ollama = RecordingOllama([
        tool_turn("get_current_time", {}, call_id="call_xyz"),
        text_turn("Done."),
    ])
    result = await run_turn(
        messages=[{"role": "user", "content": "time?"}],
        ollama=ollama, mcp=FakeMCP(), settings=_settings(),
    )
    assert result["stop_reason"] == "completed"

    # The second request replays iteration 1's assistant turn + tool result.
    sent = ollama.payloads[1]["messages"]
    assistant = next(m for m in sent if m.get("tool_calls"))
    assert assistant["tool_calls"] == [{
        "id": "call_xyz", "type": "function",
        "function": {"name": "get_current_time", "arguments": {}},
    }]

    tool_msg = next(m for m in sent if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "call_xyz"
    assert "tool_name" not in tool_msg  # native-Ollama field, must be gone


@pytest.mark.anyio
async def test_trace_stores_coerced_arguments_not_the_raw_json_string():
    """Traces are persisted to chat_messages.trace; keeping the shape stable
    across transports means storing the dict, never the wire string."""
    ollama = FakeStreamOllama([
        tool_turn("get_current_time", '{"tz":"UTC"}'),
        text_turn("Done."),
    ])
    result = await run_turn(
        messages=[{"role": "user", "content": "time?"}],
        ollama=ollama, mcp=FakeMCP(), settings=_settings(),
    )
    recorded = result["trace"][0]["tool_calls"][0]
    assert recorded["arguments"] == {"tz": "UTC"}
```

- [ ] **Step 8: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_agent_loop.py -v`
Expected: FAIL — the loop still reads `chunk["message"]`, so it sees no content and no tool calls.

- [ ] **Step 9: Update the loop's stream consumption**

In `app/agent/loop.py`, update the flow comment in the module docstring:

```python
Flow per iteration:
    (a) stream the model via ollama.stream_chat (normalized content/tool_calls/
        finish events — the client owns the wire format); emit token deltas
    (b) append the assistant message to `messages` (OpenAI tool_calls shape)
    (c) no tool_calls  -> that's the final answer, emit done, stop
    (d) tool_calls     -> emit tool_call/tool_result, append role:"tool" results
                          keyed by tool_call_id, loop
Capped at AGENT_MAX_ITERATIONS.
```

Replace `_tool_message`:

```python
def _tool_message(call_id: str, content: str) -> dict[str, Any]:
    """A tool result, correlated to the assistant's call by id.

    The OpenAI surface correlates on `tool_call_id` (native Ollama used a
    `tool_name` field). Ids come from the server, with a synthesised fallback in
    `finalize_tool_calls`, so this is always populated — which is what lets a
    single turn run several tools without the model confusing their results.
    """
    return {"role": "tool", "tool_call_id": call_id, "content": content}
```

Replace the block from `content_parts: list[str] = []` through `messages.append(assistant_msg)` (currently `app/agent/loop.py:128-151`):

```python
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
```

- [ ] **Step 10: Update the per-call dispatch block to use ids and coerced arguments**

Still in `_loop_events`, replace the `requested = …` line and the whole `for call in tool_calls:` preamble (currently `app/agent/loop.py:162-172`) with:

```python
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
```

Then, in the four branches below it, replace every `_tool_message(name, result)` call with `_tool_message(call_id, result)`, and **delete** the now-duplicated re-coercion block:

```python
            # DELETE these four lines — args/parse_error are computed above now.
            args, parse_error = _coerce_arguments(raw_args)
            if parse_error is not None:
```

becomes:

```python
            if parse_error is not None:
```

Leave `_repeat_key`, the repeat cache, the `MCPUnavailableError` handling, and the final `messages.append(_tool_message(call_id, result[:MAX_TOOL_RESULT_CHARS]))` semantics otherwise unchanged.

- [ ] **Step 11: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_agent_loop.py tests/test_openai_client_stream.py tests/test_openai_stream_parsing.py -v`
Expected: all passed (5 in `test_agent_loop.py`)

- [ ] **Step 12: Run the full suite — it must be GREEN before committing**

Run: `.venv/bin/pytest -q`
Expected: all passed. `test_chat_auth.py` / `test_history_integration.py` touch `/v1/chat`, so a failure there means a leaked chunk-shape assumption. Do not commit until this is green — this task's whole point is that no red commit enters history.

- [ ] **Step 13: Commit (ONE commit for client + loop)**

```bash
git add app/ollama/client.py app/agent/loop.py \
  tests/test_openai_client_stream.py tests/test_agent_loop.py
git commit -m "feat: move LLM transport to OpenAI-compatible /v1/chat/completions

Client: stream_chat hides the wire format behind content/tool_calls/finish
events, so a vLLM swap touches only app/ollama/client.py. /api/tags ->
/v1/models, /api/chat -> /v1/chat/completions. Drops unused generate() (no
/v1 equivalent). Error->status mapping intact.

Loop: reads normalized events instead of wire chunks and replays assistant
turns in OpenAI tool_calls shape. Tool results carry tool_call_id rather
than Ollama's tool_name, so multi-tool turns correlate correctly. Traces
store coerced dict arguments so persisted history keeps one shape across
transports.

Client and loop ship together because neither half works alone."
```

---

### Task 3: Move `num_ctx` from a request option to a derived model

`num_ctx` has no `/v1` equivalent. Rather than relying on a passthrough, bake it into a derived Ollama model — which is exactly vLLM's semantics (context is a server-launch property), so this rehearses the target state instead of regressing.

**Files:**
- Create: `deploy/Modelfile.agent`
- Modify: `app/config.py:41-43`, `.env.example`, `.env`
- Modify: `app/agent/loop.py` (drop `options.num_ctx`, hoist `temperature`)
- Test: `tests/test_agent_loop.py` (assert the payload shape)

**Interfaces:**
- Consumes: the payload construction in `_loop_events`, and the `RecordingOllama`/`text_turn` test helpers, both from Task 2.
- Produces: `Settings.agent_num_ctx` is **removed**; `Settings.agent_model` now names a context-baked model. Request payload becomes `{"model", "messages", "tools", "stream", "temperature"}` — no `options` key.

- [ ] **Step 1: Write the failing payload test**

Append to `tests/test_agent_loop.py`:

```python
@pytest.mark.anyio
async def test_payload_uses_openai_params_and_no_ollama_options():
    """`options`/`num_ctx` are native-Ollama only. Context is a property of the
    served model now (see deploy/Modelfile.agent), matching vLLM semantics."""
    ollama = RecordingOllama([text_turn("Hi.")])
    await run_turn(
        messages=[{"role": "user", "content": "hi"}],
        ollama=ollama, mcp=FakeMCP(), settings=_settings(),
    )
    payload = ollama.payloads[0]
    assert "options" not in payload
    assert payload["temperature"] == 0.1
    assert payload["stream"] is True
    assert set(payload) == {"model", "messages", "tools", "stream", "temperature"}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_agent_loop.py::test_payload_uses_openai_params_and_no_ollama_options -v`
Expected: FAIL — `assert 'options' not in payload`

- [ ] **Step 3: Update the payload and config**

In `app/agent/loop.py`, replace the payload dict:

```python
        # (a) stream the model, giving it the merged tool list every time.
        # Context length is NOT sent: it has no OpenAI-surface equivalent and is
        # baked into the served model instead (deploy/Modelfile.agent), which is
        # how vLLM works too — one less thing to change at migration.
        payload = {
            "model": settings.agent_model,
            "messages": messages,
            "tools": registry.list_ollama_tools(),
            "stream": True,
            "temperature": settings.agent_temperature,
        }
```

In `app/config.py`, delete the `agent_num_ctx` line and update the model default:

```python
    agent_model: str = "odin-agent"
    agent_temperature: float = 0.1
    agent_max_iterations: int = 8
```

- [ ] **Step 4: Create the Modelfile**

Create `deploy/Modelfile.agent`:

```
# Context length for the agent loop. This is a DELIBERATE increase from the old
# AGENT_NUM_CTX=16384, not an accident of the port: tool results are capped at
# 8000 chars (MAX_TOOL_RESULT_CHARS ~= 2000 tokens) and one turn can chain up to
# AGENT_MAX_ITERATIONS=8 of them, so tool output alone can approach 16k tokens
# before chat history and the tool schemas are counted. Too small a context
# truncates mid-loop silently — which looks like the model "forgetting", not an
# error.
#
# The name carries no size so this number can change without a rename.
#
# The /v1 surface has no num_ctx parameter, so context is a property of the
# served model. Same as vLLM's --max-model-len launch flag.
#
# Build on whichever host serves the agent, with FROM set to that host's model:
#   local  (localhost:11434):  FROM qwen2.5:latest
#   server (GPU box):          FROM qwen3.5:35b-a3b
#
#   ollama create odin-agent -f deploy/Modelfile.agent
#   ollama show odin-agent --parameters     # verify num_ctx took
#
# Deriving a model reuses the base weights' blobs — no re-download, no second
# copy on disk. Set OLLAMA_CONTEXT_LENGTH server-wide as a floor so nothing
# silently falls back to the 4096 default.
FROM qwen2.5:latest
PARAMETER num_ctx 32768
```

- [ ] **Step 5: Build and verify the derived model**

Run:
```bash
ollama create odin-agent -f deploy/Modelfile.agent
ollama show odin-agent --parameters
```
Expected: output contains `num_ctx  32768`. If `ollama show --parameters` is unsupported on 0.32.5, use `ollama show odin-agent` and read the Parameters block.

- [ ] **Step 6: Update `.env` and `.env.example`**

In both files, delete the `AGENT_NUM_CTX=16384` line and set:

```
AGENT_MODEL=odin-agent
```

In `.env.example`, add above it:

```
# Context length is baked into the model, not sent per request.
# Build it first: ollama create odin-agent -f deploy/Modelfile.agent
```

- [ ] **Step 7: Verify no `agent_num_ctx` / `AGENT_NUM_CTX` references survive**

Run: `grep -rn "agent_num_ctx\|AGENT_NUM_CTX\|num_ctx" --include=*.py --include=*.md --include=*.example app/ tests/ docs/ .env.example`
Expected: matches only in `deploy/Modelfile.agent` and the explanatory comments added above. Any hit in `app/` is a miss — fix it.

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all passed

- [ ] **Step 9: Commit**

```bash
git add deploy/Modelfile.agent app/config.py app/agent/loop.py .env.example tests/test_agent_loop.py
git commit -m "feat(agent): bake num_ctx into a derived model instead of request options

The /v1 surface has no num_ctx, and passthrough is version-dependent. A
derived model (deploy/Modelfile.agent, num_ctx 32768) makes context a
server-side property — the same semantics as vLLM's --max-model-len, so
this is a rehearsal for the target state rather than a regression.
Payload is now pure OpenAI params: no options key."
```

---

### Task 4: Live end-to-end verification and documentation

Unit tests prove the parsing; only a real server proves the model actually tool-calls over this surface.

**Files:**
- Modify: `CLAUDE.md` (Conventions/gotchas + the "Never use the ollama SDK" line)
- Modify: `app/ollama/__init__.py:2` (stale `/api/chat, /api/embeddings, /api/tags` docstring)
- Modify: `app/chat/__init__.py:1` (stale "proxy to Ollama /api/chat" docstring)
- Create: `docs/superpowers/plans/2026-08-07-openai-port-verification.md` (record the run)

**Interfaces:**
- Consumes: everything from Tasks 1–3.
- Produces: no code interfaces — documentation and a recorded verification run.

- [ ] **Step 1: Start the gateway**

Run: `.venv/bin/uvicorn app.main:app --reload --port 8000`
Expected: starts clean, no import errors. Leave it running in a second shell.

- [ ] **Step 2: Verify a tool-calling turn end to end**

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"supersecret123"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -s -X POST http://localhost:8000/v1/chat \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"message":"What is 4891 * 227? Use the calculator tool.","stream":false}' \
  | python3 -m json.tool
```
Expected: `stop_reason: "completed"`; the answer contains `1110257`; `trace[0].tool_calls[0].name == "calculator"` with `status: "ok"` and `arguments` rendered as a **JSON object, not a string**.

- [ ] **Step 3: Verify a MULTI-tool turn — this is the id-correlation check**

```bash
curl -s -X POST http://localhost:8000/v1/chat \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"message":"Compute 17*4 with the calculator, and separately tell me the current time. Use tools for both.","stream":false}' \
  | python3 -m json.tool
```
Expected: both tools appear in the trace with `status: "ok"`, and the final answer contains **both** results (68 and a timestamp). A wrong-result-for-the-wrong-tool answer means `tool_call_id` correlation is broken — go back to Task 2 Step 10.

- [ ] **Step 4: Verify streaming still emits typed NDJSON events**

```bash
curl -sN -X POST http://localhost:8000/v1/chat \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"message":"Use the calculator to find 99*99, then explain the result.","stream":true}'
```
Expected: `tool_call` and `tool_result` lines, then a run of `token` lines, then one `done`. Token content must reassemble into readable prose — garbled or missing text points at SSE handling in Task 2.

- [ ] **Step 5: Verify the MCP status badge and a file-producing tool still work**

```bash
curl -s http://localhost:8000/v1/mcp/status -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
curl -s -X POST http://localhost:8000/v1/chat \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"message":"Make me a CSV with columns name,qty and two example rows.","stream":true}' | tail -5
```
Expected: status returns 200 with a `reachable` field. The CSV turn's `done` event references a created file — confirming the `file_sink` contextvar path is unaffected.

- [ ] **Step 6: Record the verification results**

Create `docs/superpowers/plans/2026-08-07-openai-port-verification.md` with: Ollama version, model name, and for each of Steps 2–5 the command plus its actual observed output (paste it — do not write "passed"). Note explicitly that fragmented-tool-call handling is covered by **unit fixtures only**, because Ollama's shim never fragments, and must be re-verified against the real server on the day vLLM is introduced.

- [ ] **Step 7: Update the stale docstrings**

`app/ollama/__init__.py:2` — replace the endpoint list with `(/v1/chat/completions, /v1/embeddings, /v1/models)`.
`app/chat/__init__.py:1` — replace "authenticated proxy to Ollama /api/chat" with "authenticated turn endpoint over an OpenAI-compatible model server".

- [ ] **Step 8: Update CLAUDE.md**

In the **Conventions / gotchas** section, replace the `ollama` SDK bullet with:

```markdown
- **Never** use the `ollama` SDK, and don't add the `openai` SDK either — we call
  the model server's OpenAI-compatible REST surface (`/v1/chat/completions`,
  `/v1/models`, `/v1/embeddings`) with httpx. The `openai` SDK would not solve
  streamed tool-call fragment accumulation for us (only its *beta* stream helper
  accumulates) while displacing our `OllamaError` → HTTP-status mapping.
- **The wire format lives in ONE file:** `app/ollama/client.py`. `stream_chat`
  yields normalized events (`{"type":"content","text"}` /
  `{"type":"tool_calls","calls"}` / `{"type":"finish","reason"}`); the agent loop
  never sees SSE or `choices[0].delta`. Pointing `OLLAMA_BASE_URL` at vLLM /
  llama.cpp / LiteLLM should need no edits outside that file.
- **Tool-call streaming differs per backend:** Ollama's `/v1` shim sends each
  tool call whole in one delta; **vLLM fragments `arguments` across deltas**.
  `merge_tool_call_deltas` handles both. The fragmented path is covered by
  hand-authored fixtures in `tests/test_openai_stream_parsing.py` because our
  Ollama can't produce it — re-verify live when vLLM lands.
- **Tool results correlate on `tool_call_id`**, not Ollama's `tool_name`. Ids
  come from the server (`finalize_tool_calls` synthesises a fallback). Getting
  this wrong silently mismatches results in multi-tool turns.
- **`num_ctx` is baked into the model, not the request** — `deploy/Modelfile.agent`
  (`ollama create odin-agent -f deploy/Modelfile.agent`). The `/v1` surface has
  no `num_ctx`; this matches vLLM's `--max-model-len`. Keep `OLLAMA_CONTEXT_LENGTH`
  set server-wide as a floor so nothing falls back to 4096.
- Use `resp.aiter_lines()` for SSE — never `aiter_bytes()` with manual `\n\n`
  splitting, which truncates JSON across HTTP chunk boundaries under load and
  presents as a flaky model rather than a parser bug.
```

Also update the architecture diagram's `Ollama LLM (:11434)` line to `Ollama LLM (:11434, OpenAI-compatible /v1)`.

- [ ] **Step 9: Final full-suite run**

Run: `.venv/bin/pytest -q`
Expected: all passed. Record the actual count in the commit message.

- [ ] **Step 10: Commit**

```bash
git add CLAUDE.md app/ollama/__init__.py app/chat/__init__.py \
  docs/superpowers/plans/2026-08-07-openai-port-verification.md
git commit -m "docs: record the OpenAI-compatible port and its verification

Live-verified single-tool, multi-tool (id correlation), streaming, MCP
status and file-producing turns against Ollama 0.32.5. Documents the one
gap: fragmented tool-call streaming is fixture-covered only, since
Ollama's shim never fragments — re-verify when vLLM lands."
```

---

## Open Item for the Server Box (not blocking this plan)

Everything above was verified against **local** Ollama 0.32.5 / `qwen2.5:latest`. Your agent server runs `qwen3.5:35b-a3b`. Before pointing the gateway at it, re-run the Task 4 Step 2–3 checks there, because shim tool-calling behaviour is Ollama-version-dependent — specifically confirm ids are still non-empty and that a 35b-a3b MoE emits tool calls over `/v1` as reliably as over `/api/chat`. Also budget VRAM: a 32k KV cache is a real allocation, so measure before assuming 32768 fits alongside the weights.

## Self-Review

**Spec coverage.** All four items of the agreed diff are covered: SSE parsing → Task 1/2; fragment accumulation → Task 1 (fixtures) + Task 2 (wiring); `tool_call_id` threading → Task 2; `/api/tags` → `/v1/models` → Task 2. Web-Claude's three additions: `aiter_lines()` → Global Constraints + Task 2 Step 4 comment; `:` keepalive skipping → Task 1 test + `parse_sse_line`; Modelfile `num_ctx` → Task 3; id-stability check → resolved by live probe, recorded in Verified Facts, with a synthesis fallback in Task 1. Golden-fixture-before-touching-`stream_chat` → Task 1 precedes Task 2, with the correction that fragmentation fixtures must be authored rather than recorded.

**Task structure.** Four tasks. The client rewrite and the loop adaptation are ONE task (Task 2) and one commit: neither half is separately approvable, and splitting them would put a knowingly-red `test_agent_loop.py` into history. Every task's final commit has a green suite.

**Placeholder scan.** No TBDs. Every code step carries real code; every run step names a command and an expected result; Task 4 requires pasted output rather than a "passed" claim.

**Type consistency.** `{"id","name","arguments"}` is the call shape from `finalize_tool_calls` (Task 1) through `stream_chat`'s `tool_calls` event (Task 2) into the loop (Task 2). `_tool_message(call_id, content)` is two-arg everywhere after Task 2 Step 9 — Step 10 explicitly updates all four call sites. `agent_num_ctx` is deleted in Task 3 Step 3 and Step 7 greps for survivors.
