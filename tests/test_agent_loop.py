"""Tests for the streaming agent loop, driven by a fake streaming Ollama.

No network. `stream_turn` is the event generator; `run_turn` collects it into the
result dict. We assert both the collected result AND the live event sequence.
"""

import pytest

from app.agent.loop import run_turn, stream_turn
from app.config import Settings


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


class FakeStreamOllama:
    def __init__(self, turns):
        self._turns = list(turns)

    async def stream_chat(self, payload):
        turn = self._turns.pop(0) if len(self._turns) > 1 else self._turns[0]
        for chunk in turn:
            yield chunk


class FakeMCP:
    configured = False


def _settings(max_iter=10):
    return Settings(agent_max_iterations=max_iter)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_run_turn_handles_failure_modes_then_completes():
    turns = [
        tool_turn("does_not_exist", {}),         # unknown_tool
        tool_turn("get_current_time", "{bad"),   # bad_arguments (unparseable)
        tool_turn("get_current_time", {}),       # ok
        tool_turn("get_current_time", {}),       # repeat
        text_turn("All done."),                  # final
    ]
    result = await run_turn(
        messages=[{"role": "user", "content": "hi"}],
        ollama=FakeStreamOllama(turns),
        mcp=FakeMCP(),
        settings=_settings(),
    )
    assert result["stop_reason"] == "completed"
    assert result["final_answer"] == "All done."
    statuses = [tc["status"] for e in result["trace"] for tc in e["tool_calls"]]
    assert statuses == ["unknown_tool", "bad_arguments", "ok", "repeat"]


@pytest.mark.anyio
async def test_run_turn_caps_at_max_iterations():
    forever = [tool_turn("get_current_time", {})]
    result = await run_turn(
        messages=[{"role": "user", "content": "loop"}],
        ollama=FakeStreamOllama(forever),
        mcp=FakeMCP(),
        settings=_settings(max_iter=4),
    )
    assert result["stop_reason"] == "max_iterations"
    assert result["iteration_count"] == 4


@pytest.mark.anyio
async def test_stream_turn_emits_glass_box_event_sequence():
    turns = [tool_turn("get_current_time", {}), text_turn("Now you know.")]
    events = []
    async for ev in stream_turn(
        messages=[{"role": "user", "content": "what time is it?"}],
        ollama=FakeStreamOllama(turns),
        mcp=FakeMCP(),
        settings=_settings(),
    ):
        events.append(ev)

    types = [e["type"] for e in events]
    # tool activity is surfaced live, then the answer streams, then done.
    assert types[0] == "tool_call"
    assert types[1] == "tool_result"
    assert "token" in types
    assert types[-1] == "done"

    assert events[0]["name"] == "get_current_time"
    assert events[1]["status"] == "ok"
    # tokens reassemble to the final answer
    answer = "".join(e["content"] for e in events if e["type"] == "token")
    assert answer == "Now you know."

    done = events[-1]
    assert done["stop_reason"] == "completed"
    assert done["final_answer"] == "Now you know."
    assert len(done["trace"]) == 2  # tool iteration + final iteration


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


@pytest.mark.anyio
async def test_payload_uses_openai_params_and_no_ollama_options():
    """`options`/`num_ctx` are native-Ollama only. Context is set server-wide
    now (OLLAMA_CONTEXT_LENGTH / vLLM --max-model-len), not per request."""
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
