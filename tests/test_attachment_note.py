"""Pure (no-DB) tests for the attachment-note formatting + context injection."""

from __future__ import annotations

from app.history import repository as repo
from app.history.models import ChatMessage, ROLE_ASSISTANT, ROLE_USER


def _msg(role, content, attachments=None):
    m = ChatMessage(session_id="s", seq=1, role=role, content=content)
    m.attachments = attachments
    return m


def test_format_note_lists_ids_and_summaries():
    note = repo.format_attachment_note(
        [{"id": "abc123", "filename": "sales.xlsx", "summary": "Excel, 2 sheets, 1240 rows"}]
    )
    assert "id=abc123" in note
    assert "sales.xlsx" in note
    assert "Excel, 2 sheets, 1240 rows" in note
    assert "inspect_excel" in note or "read_excel" in note


def test_build_context_injects_system_note_before_attached_user_msg():
    msgs = [
        _msg(ROLE_USER, "summarize this", attachments=[{"id": "f1", "filename": "a.csv", "summary": "CSV, 3 rows"}]),
        _msg(ROLE_ASSISTANT, "here is the summary"),
    ]
    ctx = repo.build_context_messages(msgs)
    assert ctx[0]["role"] == "system"
    assert "f1" in ctx[0]["content"]
    assert ctx[1] == {"role": "user", "content": "summarize this"}
    assert ctx[2] == {"role": "assistant", "content": "here is the summary"}


def test_build_context_no_note_when_no_attachments():
    msgs = [_msg(ROLE_USER, "plain"), _msg(ROLE_ASSISTANT, "reply")]
    ctx = repo.build_context_messages(msgs)
    assert all(m["role"] != "system" for m in ctx)
    assert ctx == [
        {"role": "user", "content": "plain"},
        {"role": "assistant", "content": "reply"},
    ]
