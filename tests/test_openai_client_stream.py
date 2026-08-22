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
async def test_sse_split_mid_json_across_http_chunks_still_reassembles():
    """Guards against regressing `aiter_lines()` to manual `aiter_bytes()` +
    `b"\\n\\n"` splitting.

    Every other test in this file hands `httpx.Response(200, content=body)` a
    single ``bytes`` blob, which httpx delivers as ONE chunk regardless of how
    `stream_chat` reads it — so those tests would keep passing even if
    `stream_chat` split raw bytes on `b"\\n\\n"` by hand instead of using
    `aiter_lines()`. This test delivers the SSE body as several *genuinely
    separate* HTTP chunks (via an async generator passed as `content=`, which
    httpx wraps in `AsyncIteratorByteStream`), with the chunk boundaries
    falling **inside** a `data:` line's JSON rather than between lines. Only
    `aiter_lines()`'s cross-chunk line reassembly makes this parseable; a
    manual `aiter_bytes()` + `b"\\n\\n"` split would hand the JSON decoder a
    truncated fragment and silently drop or corrupt content — the exact
    failure mode that shows up as intermittent truncated JSON under load and
    reads as a flaky model rather than a parser bug.
    """
    async def body_chunks():
        for chunk in (
            b'data: {"choices":[{"delta":{"content":"Hel',
            b'lo"}}]}\n\ndata: {"choices":[{"delta":{"con',
            b'tent":" world"},"finish_reason":"stop"}]}\n\n',
            b'data: [DONE]\n\n',
        ):
            yield chunk

    client = _client(lambda req: httpx.Response(200, content=body_chunks()))
    events = await _drain(client)

    assert events == [
        {"type": "content", "text": "Hello"},
        {"type": "content", "text": " world"},
        {"type": "finish", "reason": "stop"},
    ]
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


def test_normalize_usage_extracts_the_three_fields():
    from app.ollama.client import normalize_usage

    raw = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "extra": "x"}
    assert normalize_usage(raw) == {
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
    }


def test_normalize_usage_fails_closed_on_a_partial_or_malformed_object():
    from app.ollama.client import normalize_usage

    assert normalize_usage(None) is None
    assert normalize_usage({}) is None
    assert normalize_usage({"prompt_tokens": 1}) is None
    assert normalize_usage({"prompt_tokens": "not-a-number",
                             "completion_tokens": 1, "total_tokens": 1}) is None
