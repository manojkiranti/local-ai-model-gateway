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


def test_build_context_injects_note_before_attached_user_msg():
    msgs = [
        _msg(ROLE_USER, "summarize this", attachments=[{"id": "f1", "filename": "a.csv", "summary": "CSV, 3 rows"}]),
        _msg(ROLE_ASSISTANT, "here is the summary"),
    ]
    ctx = repo.build_context_messages(msgs)
    assert ctx[0]["role"] == "user"
    assert "f1" in ctx[0]["content"]
    assert ctx[1] == {"role": "user", "content": "summarize this"}
    assert ctx[2] == {"role": "assistant", "content": "here is the summary"}


def test_format_note_marks_superseded_attachments_as_historical():
    note = repo.format_attachment_note(
        [{"id": "old1", "filename": "employees.xlsx", "summary": "Sheet1 — 4 rows"}],
        active=False,
    )
    assert "old1" in note
    assert "employees.xlsx" in note
    # A superseded file must not read like the file to work on, and its summary
    # is dropped so it can't out-weigh the active one.
    assert "Sheet1 — 4 rows" not in note
    assert "earlier" in note.lower()


def test_new_upload_demotes_every_earlier_attachment():
    """The reported bug: a second upload in the same session was ignored because
    both notes read identically and the older one was the more salient."""
    msgs = [
        _msg(ROLE_USER, "summarize", attachments=[{"id": "old1", "filename": "a.xlsx", "summary": "S"}]),
        _msg(ROLE_ASSISTANT, "here it is"),
    ]
    ctx = repo.build_context_messages(msgs, pending_attachments=True)
    note = ctx[0]
    assert note["role"] == "user"
    assert "old1" in note["content"]
    assert "earlier" in note["content"].lower()


def test_last_attachment_stays_active_when_this_turn_has_no_file():
    """'now total column B' with no new upload must still act on the last file."""
    msgs = [
        _msg(ROLE_USER, "read this", attachments=[{"id": "old1", "filename": "a.xlsx", "summary": "S"}]),
        _msg(ROLE_ASSISTANT, "done"),
        _msg(ROLE_USER, "and this", attachments=[{"id": "new2", "filename": "b.xlsx", "summary": "T"}]),
        _msg(ROLE_ASSISTANT, "done again"),
    ]
    ctx = repo.build_context_messages(msgs)
    # Notes are user-role now, so identify them by content, not by role.
    notes = [m for m in ctx if "id=" in m["content"]]
    assert len(notes) == 2
    assert "earlier" in notes[0]["content"].lower()  # old1 superseded
    assert "old1" in notes[0]["content"]
    assert "earlier" not in notes[1]["content"].lower()  # new2 still active
    assert "new2" in notes[1]["content"]


def test_build_context_no_note_when_no_attachments():
    msgs = [_msg(ROLE_USER, "plain"), _msg(ROLE_ASSISTANT, "reply")]
    ctx = repo.build_context_messages(msgs)
    assert all("id=" not in m["content"] for m in ctx)
    assert ctx == [
        {"role": "user", "content": "plain"},
        {"role": "assistant", "content": "reply"},
    ]


def test_attachment_note_is_a_user_message_not_a_system_one():
    """Regression lock, and NOT a style preference.

    Measured against qwen3.5:35b-a3b with the 16 tool schemas loaded, an
    identical note produced a tool call 3/12 times as `system` versus 12/12 as
    `user`. The model reads a system note (asked directly, it returns the id
    every time) but does not act on it once tools are in play — it asks the user
    for a file id it was already given. Stronger imperative wording moved it
    2/6; only the role fixed it. Do not "tidy" this back to system.
    """
    msgs = [_msg(ROLE_USER, "summarize this", attachments=[{"id": "f1", "filename": "a.pdf", "summary": "PDF, 2 pages"}])]
    ctx = repo.build_context_messages(msgs)
    note = ctx[0]
    assert note["role"] == "user"
    assert "f1" in note["content"]
