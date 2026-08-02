"""Tests for the streaming agent loop, driven by a fake streaming Ollama.

No network. `stream_turn` is the event generator; `run_turn` collects it into the
result dict. We assert both the collected result AND the live event sequence.
"""

import pytest

from app.agent.loop import run_turn, stream_turn
from app.config import Settings


def tool_turn(name, arguments):
    """One model turn that calls a tool (Ollama sends tool_calls whole)."""
    return [
        {"message": {"content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}},
        {"message": {"content": ""}, "done": True},
    ]


def text_turn(text):
    """One model turn that streams a plain answer in two deltas."""
    half = len(text) // 2
    return [
        {"message": {"content": text[:half]}},
        {"message": {"content": text[half:]}},
        {"message": {"content": ""}, "done": True},
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
