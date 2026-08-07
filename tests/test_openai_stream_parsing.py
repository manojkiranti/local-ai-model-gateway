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
