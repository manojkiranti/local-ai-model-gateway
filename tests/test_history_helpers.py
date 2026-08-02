"""Unit tests for the DB-free history helpers (no Postgres needed)."""

from app.history.models import ChatMessage
from app.history.repository import build_context_messages, make_title


def test_make_title_collapses_whitespace_and_keeps_short():
    assert make_title("  Hello   there\nworld ") == "Hello there world"


def test_make_title_truncates_long_with_ellipsis():
    title = make_title("x" * 200)
    assert len(title) <= 80
    assert title.endswith("…")


def test_build_context_messages_maps_role_and_content_only():
    msgs = [
        ChatMessage(session_id="s", seq=1, role="user", content="hi"),
        ChatMessage(
            session_id="s", seq=2, role="assistant", content="hello",
            trace=[{"iteration": 1}], model="qwen2.5:latest",
        ),
    ]
    ctx = build_context_messages(msgs)
    # Only role/content is replayed — trace/model are history, not context.
    assert ctx == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
