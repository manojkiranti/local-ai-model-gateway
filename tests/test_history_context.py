"""Pure (no-DB, no-GPU) tests for the history context budget.

Split out of repository.py for the same reason app/rag/ranking.py is split from
access.py: the code deciding what the model does and does not see should be
provable with no database and no model server.
"""

from __future__ import annotations

from app.history.context import estimate_tokens


def test_devanagari_costs_more_tokens_than_latin_of_equal_length():
    # THE test for this module. A `len(text)/4` estimator passes every other
    # test in this file and still under-counts a Nepali thread into an
    # overflow, which is the bug the budget exists to prevent.
    latin = "a" * 100
    devanagari = "क" * 100
    assert estimate_tokens(devanagari) > estimate_tokens(latin)


def test_empty_text_costs_nothing():
    assert estimate_tokens("") == 0


def test_estimate_grows_with_length():
    assert estimate_tokens("word " * 100) > estimate_tokens("word " * 10)


def test_mixed_script_is_between_the_two_pure_cases():
    latin = "a" * 200
    devanagari = "क" * 200
    mixed = "a" * 100 + "क" * 100
    assert estimate_tokens(latin) < estimate_tokens(mixed) < estimate_tokens(devanagari)


from app.history.context import Selection, select_turns
from app.history.models import ROLE_ASSISTANT, ROLE_USER
from app.history.models import ChatMessage


def _msg(seq, role, content, attachments=None):
    m = ChatMessage(session_id="s", seq=seq, role=role, content=content)
    m.attachments = attachments
    return m


def _thread(turns, *, content="x" * 400):
    """`turns` whole user+assistant pairs, seq 1..2*turns."""
    out = []
    for i in range(turns):
        out.append(_msg(2 * i + 1, ROLE_USER, content))
        out.append(_msg(2 * i + 2, ROLE_ASSISTANT, content))
    return out


def test_a_thread_inside_the_budget_is_untouched():
    msgs = _thread(3)
    sel = select_turns(msgs, budget=1_000_000)
    assert sel.messages == msgs
    assert sel.truncated is False
    assert sel.pinned_attachments is None


def test_oldest_turns_are_dropped_first():
    msgs = _thread(20)
    sel = select_turns(msgs, budget=1500)
    assert sel.truncated is True
    assert len(sel.messages) < len(msgs)
    # What survives is the TAIL of the conversation, contiguous to the end.
    assert sel.messages == msgs[-len(sel.messages):]


def test_selection_never_starts_with_a_dangling_assistant():
    # Dropping mid-turn leaves an assistant reply with no question before it,
    # which reads to the model as the assistant having spoken unprompted.
    for budget in range(200, 3000, 137):
        sel = select_turns(_thread(20), budget=budget)
        if sel.messages:
            assert sel.messages[0].role == ROLE_USER


def test_a_single_over_budget_message_still_yields_a_usable_context():
    msgs = [_msg(1, ROLE_USER, "क" * 50_000)]
    sel = select_turns(msgs, budget=100)
    assert sel.messages == msgs  # never an empty context
    assert sel.truncated is False  # nothing was dropped; it simply does not fit


def test_the_active_attachment_set_is_pinned_when_its_turn_is_dropped():
    # The file ids MUST survive. Losing them makes the model ask the user for an
    # id it was already handed — the failure
    # test_attachment_note_is_a_user_message_not_a_system_one guards against.
    files = [{"id": "f1", "filename": "a.csv", "summary": "CSV, 3 rows"}]
    msgs = [_msg(1, ROLE_USER, "x" * 400, attachments=files)]
    msgs += _thread(20)[2:]
    sel = select_turns(msgs, budget=1500)
    assert sel.truncated is True
    assert msgs[0] not in sel.messages
    assert sel.pinned_attachments == files


def test_nothing_is_pinned_when_the_attachment_turn_survives():
    files = [{"id": "f1", "filename": "a.csv", "summary": "CSV, 3 rows"}]
    msgs = _thread(2) + [
        _msg(5, ROLE_USER, "look at this", attachments=files),
        _msg(6, ROLE_ASSISTANT, "done"),
    ]
    sel = select_turns(msgs, budget=1_000_000)
    assert sel.pinned_attachments is None


def test_only_the_NEWEST_attachment_set_is_pinned():
    old = [{"id": "old", "filename": "old.csv", "summary": ""}]
    new = [{"id": "new", "filename": "new.csv", "summary": ""}]
    msgs = [_msg(1, ROLE_USER, "x" * 400, attachments=old)]
    msgs += [_msg(2, ROLE_USER, "x" * 400, attachments=new)]
    msgs += _thread(20)[2:]
    sel = select_turns(msgs, budget=1500)
    assert sel.pinned_attachments == new


from app.history.context import budget_for


class _Settings:
    context_window_tokens = 32768
    context_reserve_tokens = 6000
    context_tool_schema_tokens = 4000


def test_budget_is_the_window_less_everything_else_in_the_prompt():
    assert budget_for(_Settings()) == 32768 - 6000 - 4000


def test_a_tiny_window_yields_a_floor_not_a_negative_budget():
    # A misconfigured window must not produce a negative budget, which would
    # drop every turn including the current question.
    class Tiny(_Settings):
        context_window_tokens = 1000
    assert budget_for(Tiny()) > 0


from app.history.context import TRUNCATION_NOTE, build_context_messages


def test_the_truncation_note_is_absent_when_nothing_was_dropped():
    # An always-present note trains the model to ignore it — the same
    # over-warning reasoning as the citation caveats (docs §29.2).
    ctx = build_context_messages(_thread(2), truncated=False)
    assert all(TRUNCATION_NOTE not in m["content"] for m in ctx)


def test_the_truncation_note_leads_the_context_when_turns_were_dropped():
    # It must be FIRST. Without it the model sees a conversation that simply
    # starts mid-way and will deny being told something it was told.
    ctx = build_context_messages(_thread(2), truncated=True)
    assert TRUNCATION_NOTE in ctx[0]["content"]


def test_a_pinned_attachment_set_is_replayed_as_ACTIVE():
    files = [{"id": "f1", "filename": "a.csv", "summary": "CSV, 3 rows"}]
    ctx = build_context_messages(
        _thread(2), truncated=True, pinned_attachments=files
    )
    joined = "\n".join(m["content"] for m in ctx)
    assert "id=f1" in joined
    assert "superseded" not in joined


def test_a_pinned_set_supersedes_every_surviving_set():
    # Two active sets would leave the model guessing which ids the user means.
    old = [{"id": "old", "filename": "old.csv", "summary": ""}]
    pinned = [{"id": "new", "filename": "new.csv", "summary": ""}]
    msgs = [_msg(1, ROLE_USER, "hi", attachments=old), _msg(2, ROLE_ASSISTANT, "ok")]
    ctx = build_context_messages(msgs, truncated=True, pinned_attachments=pinned)
    notes = [m["content"] for m in ctx if "id=" in m["content"]]
    assert sum(1 for n in notes if "superseded" not in n) == 1
