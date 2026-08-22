# Chat History Lazy Loading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound all three unbounded chat-history loads — the session list, one thread, and the model's prompt context — with keyset pagination and a token budget.

**Architecture:** A new pure module `app/history/context.py` owns token estimation, turn selection and context building (no DB, no HTTP, no model — the `app/rag/ranking.py` precedent). `app/history/repository.py` loses its one unbounded read and gains three bounded ones. The two read endpoints move to a `{items, next_cursor}` envelope with keyset cursors.

**Tech Stack:** Python 3.10, FastAPI, SQLAlchemy 2 async, Postgres, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-22-chat-history-lazy-loading-design.md`

## Global Constraints

- Use THIS project's venv: `.venv/bin/python`, `.venv/bin/pytest`. Python 3.10.
- **No new dependencies.** No tokenizer library — `transformers` (93 MB) must not enter the API image.
- **No migration.** No schema change; the single Alembic head is untouched.
- `get_session_with_messages` is **deleted**, not deprecated.
- The existing tests in `tests/test_attachment_note.py` and `tests/test_history_helpers.py` must pass with **only their import path changed**. If an assertion needs editing, the refactor broke behaviour — STOP and report.
- `CONTEXT_WINDOW_TOKENS` default `32768`, matching `OLLAMA_CONTEXT_LENGTH` on the Ollama service. The gateway cannot read that value back; the duplicate is deliberate and logged.
- The truncation note is emitted **only** when turns were actually dropped.
- Commit after every task.

---

### Task 1: Script-aware token estimator

**Files:**
- Create: `app/history/context.py`
- Test: `tests/test_history_context.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `estimate_tokens(text: str) -> int`, `estimate_message_tokens(message) -> int` where `message` is an `app.history.models.ChatMessage`. Constants `LATIN_CHARS_PER_TOKEN: float`, `DEVANAGARI_CHARS_PER_TOKEN: float`, `MESSAGE_OVERHEAD_TOKENS: int`, `SAFETY_MARGIN: float`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_history_context.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_history_context.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.history.context'`

- [ ] **Step 3: Write minimal implementation**

Create `app/history/context.py`:

```python
"""What the model sees of a conversation — pure, no DB, no HTTP, no model.

Split out of `repository.py` deliberately, following `app/rag/ranking.py` and
`app/users/policy.py`: the rule deciding which turns reach the prompt is
provable with no database and no GPU.

Why a hand-rolled estimator instead of a tokenizer: an exact count needs Qwen's
vocabulary, which means `transformers` (93 MB) in the API image, and CLAUDE.md
keeps that stack out on purpose. Why SCRIPT-AWARE rather than the usual
`len(text)/4`: that ratio is calibrated on English, while this corpus is mixed
Nepali/English and Devanagari costs far more tokens per character on a BPE
vocabulary. A flat /4 under-counts a Nepali-heavy thread and lets it overflow —
exactly the failure the budget exists to prevent.

The two ratios are DELIBERATELY pessimistic. Over-estimating wastes a little
window; under-estimating lets Ollama drop the front of the prompt, which is
where the identity and date system prompt lives. Only one of those reaches a
user. Calibrate against the server's real `usage.prompt_tokens` (the method that
produced CLAUDE.md's 3475-token tool-schema figure) and record the measurement
in the design doc.
"""

from __future__ import annotations

import math
import unicodedata
from typing import Any

# Devanagari block: U+0900..U+097F, plus the extended block U+A8E0..U+A8FF.
_DEVANAGARI_RANGES = ((0x0900, 0x097F), (0xA8E0, 0xA8FF))

# Pessimistic on purpose — see the module docstring.
LATIN_CHARS_PER_TOKEN = 3.5
DEVANAGARI_CHARS_PER_TOKEN = 1.0
# Per-message cost of the role framing the model server adds around content.
MESSAGE_OVERHEAD_TOKENS = 4
SAFETY_MARGIN = 1.10


def _is_devanagari(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _DEVANAGARI_RANGES)


def estimate_tokens(text: str) -> int:
    """A pessimistic token count for `text`. Never returns less than the true
    count for either script by more than the margin allows."""
    if not text:
        return 0
    devanagari = sum(1 for ch in text if _is_devanagari(ch))
    other = len(text) - devanagari
    raw = devanagari / DEVANAGARI_CHARS_PER_TOKEN + other / LATIN_CHARS_PER_TOKEN
    return math.ceil(raw * SAFETY_MARGIN)


def estimate_message_tokens(message: Any) -> int:
    """Cost of one ChatMessage as the model will see it: its content, its role
    framing, and its attachment note if it carries one."""
    total = estimate_tokens(message.content or "") + MESSAGE_OVERHEAD_TOKENS
    if getattr(message, "attachments", None):
        # The note is re-emitted as its own message, so it is its own cost.
        total += estimate_tokens(format_attachment_note(message.attachments))
        total += MESSAGE_OVERHEAD_TOKENS
    return total
```

Note `format_attachment_note` is referenced here and moves into this module in Task 4. Until then, add this import at the top of `app/history/context.py`:

```python
from .repository import format_attachment_note  # moves into this module in Task 4
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_history_context.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add app/history/context.py tests/test_history_context.py
git commit -m "feat(history): script-aware token estimator for the context budget"
```

---

### Task 2: Turn selection under a budget

**Files:**
- Modify: `app/history/context.py`
- Test: `tests/test_history_context.py`

**Interfaces:**
- Consumes: `estimate_message_tokens` from Task 1.
- Produces: `Selection` dataclass with fields `messages: list`, `truncated: bool`, `pinned_attachments: list[dict] | None`; and `select_turns(messages: list, budget: int) -> Selection`.

`pinned_attachments` is set **only** when the message carrying the newest attachment set fell outside the budget. It is how the file ids survive without dragging the whole turn back in.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_history_context.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_history_context.py -v`
Expected: FAIL — `ImportError: cannot import name 'Selection' from 'app.history.context'`

- [ ] **Step 3: Write minimal implementation**

Append to `app/history/context.py`:

```python
from dataclasses import dataclass


@dataclass
class Selection:
    """The tail of a conversation that fits the budget.

    `truncated` says something was DROPPED, not merely that the thread is long —
    it gates the note that tells the model there is history it cannot see.
    `pinned_attachments` is the newest attachment set when the message carrying
    it fell outside the budget: the file ids have to survive even though the
    turn did not.
    """

    messages: list
    truncated: bool
    pinned_attachments: list[dict] | None


def _group_turns(messages: list) -> list[list]:
    """Split an ascending message list into whole turns.

    A turn starts at a user message and runs to just before the next one, so
    user+assistant stay together and selection can never sever them. A leading
    assistant message (possible only in malformed history) forms its own group
    rather than being silently dropped.
    """
    groups: list[list] = []
    for m in messages:
        if m.role == ROLE_USER or not groups:
            groups.append([m])
        else:
            groups[-1].append(m)
    return groups


def select_turns(messages: list, budget: int) -> Selection:
    """Newest whole turns that fit `budget`, oldest dropped first.

    The last turn is ALWAYS kept even if it alone exceeds the budget: an empty
    context is strictly worse than an over-long one, because the model then has
    no question to answer. That case reports `truncated=False` — nothing was
    dropped, it simply does not fit, and claiming otherwise would emit a note
    about history that does not exist.
    """
    if not messages:
        return Selection(messages=[], truncated=False, pinned_attachments=None)

    groups = _group_turns(messages)
    kept: list[list] = []
    spent = 0
    for group in reversed(groups):
        cost = sum(estimate_message_tokens(m) for m in group)
        if kept and spent + cost > budget:
            break
        kept.insert(0, group)
        spent += cost

    selected = [m for group in kept for m in group]
    dropped = len(selected) < len(messages)

    # The newest attachment set is what `build_context_messages` treats as
    # active. If its message did not survive, carry the record forward.
    pinned = None
    if dropped:
        with_files = [m for m in messages if m.role == ROLE_USER and m.attachments]
        if with_files and with_files[-1] not in selected:
            pinned = with_files[-1].attachments

    return Selection(messages=selected, truncated=dropped, pinned_attachments=pinned)
```

Add `ROLE_USER` to the module's imports:

```python
from .models import ROLE_USER
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_history_context.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add app/history/context.py tests/test_history_context.py
git commit -m "feat(history): select newest whole turns under a token budget"
```

---

### Task 3: The budget from settings

**Files:**
- Modify: `app/config.py:90` (beside `expose_trace`, in the agent block)
- Modify: `app/history/context.py`
- Modify: `.env.example`
- Test: `tests/test_history_context.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `budget_for(settings) -> int`. Settings fields `context_window_tokens: int = 32768`, `context_reserve_tokens: int = 6000`, `context_tool_schema_tokens: int = 4000`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_history_context.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_history_context.py -k budget -v`
Expected: FAIL — `ImportError: cannot import name 'budget_for'`

- [ ] **Step 3: Write minimal implementation**

Append to `app/history/context.py`:

```python
# A conversation may never be budgeted to nothing: the current question has to
# fit or the turn is meaningless.
MIN_HISTORY_BUDGET = 512


def budget_for(settings: Any) -> int:
    """Tokens available for CHAT HISTORY in one turn.

    The window is shared: identity + date system prompt, the tool schemas, RAG
    passages, tool results, and room for the answer all come out of it first.
    `context_reserve_tokens` covers that last group.

    `context_window_tokens` is a SECOND COPY of a number this process cannot
    read. `OLLAMA_CONTEXT_LENGTH` is set on the Ollama service; `/v1` has no
    `num_ctx` request field and the completions response does not report the
    loaded window back (measured — see CLAUDE.md). Nothing reconciles the two
    copies, so if Ollama is raised later, or a deploy omits the variable and
    Ollama falls back to its 4096 default, this budget is wrong in silence.
    That is why the turn path logs the estimate against the server's reported
    `usage.prompt_tokens`.
    """
    available = (
        settings.context_window_tokens
        - settings.context_reserve_tokens
        - settings.context_tool_schema_tokens
    )
    return max(MIN_HISTORY_BUDGET, available)
```

In `app/config.py`, in the agent block after `agent_max_iterations: int = 8`:

```python
    # --- Context window (how much CHAT HISTORY reaches the model) ---
    # context_window_tokens MUST match OLLAMA_CONTEXT_LENGTH on the Ollama
    # service. It is duplicated here because the /v1 surface has no num_ctx
    # field and does not report the loaded window back, so this process cannot
    # read the real value. A mismatch is silent: we budget confidently into an
    # overflow, Ollama drops the FRONT of the prompt (where the identity and
    # date system prompt lives), and the turn still returns a normal answer.
    # app/history/context.py logs the estimate so drift is at least visible.
    context_window_tokens: int = 32768
    # Room kept for RAG passages, tool results and the answer itself.
    context_reserve_tokens: int = 6000
    # Measured floor for the local tool schemas. CLAUDE.md's 3475 figure was
    # taken at 15 tools and LOCAL_TOOLS is now 17 — re-measure when it grows.
    context_tool_schema_tokens: int = 4000
    # DB-side bound on the context read, applied BEFORE the token budget so a
    # 500-turn thread never materializes. Deliberately far more messages than
    # any budget can hold, so it never decides selection; if it is ever the
    # binding constraint the turn log will show it.
    context_max_messages: int = 200
```

Append to `.env.example`:

```
# Must match OLLAMA_CONTEXT_LENGTH on the Ollama service (see app/config.py).
CONTEXT_WINDOW_TOKENS=32768
CONTEXT_RESERVE_TOKENS=6000
CONTEXT_TOOL_SCHEMA_TOKENS=4000
CONTEXT_MAX_MESSAGES=200
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_history_context.py -v && .venv/bin/pytest tests/test_config.py -v 2>/dev/null || true`
Expected: all history-context tests pass

- [ ] **Step 5: Commit**

```bash
git add app/config.py app/history/context.py .env.example tests/test_history_context.py
git commit -m "feat(history): derive the history budget from the configured window"
```

---

### Task 4: Move the context builder into the pure module

**Files:**
- Modify: `app/history/context.py`
- Modify: `app/history/repository.py:35-115` (remove `format_attachment_note` and `build_context_messages`)
- Modify: `tests/test_history_helpers.py:4`, `tests/test_attachment_note.py:5` (import path only)
- Test: `tests/test_history_context.py`

**Interfaces:**
- Consumes: `Selection` from Task 2.
- Produces: `format_attachment_note(attachments, *, active=True) -> str` and `build_context_messages(messages, *, pending_attachments=False, truncated=False, pinned_attachments=None) -> list[dict[str, str]]`, both now in `app.history.context`. `TRUNCATION_NOTE: str`.

**The existing behaviour of both functions must not change.** The signature only grows keyword arguments that default to the old behaviour.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_history_context.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_history_context.py -v`
Expected: FAIL — `ImportError: cannot import name 'TRUNCATION_NOTE'`

- [ ] **Step 3: Write minimal implementation**

Move `format_attachment_note` and `build_context_messages` **verbatim** from `app/history/repository.py` into `app/history/context.py`, then delete them from `repository.py` along with the now-unused `Any` import if nothing else needs it. Remove the temporary `from .repository import format_attachment_note` line added in Task 1. Add `ROLE_ASSISTANT` to the `.models` import if the moved code needs it.

Then modify the moved `build_context_messages` to accept the two new keywords:

```python
# Emitted ONLY when turns were actually dropped. Same rule as _for_model's
# [TRUNCATED …] note in agent/loop.py: a bare cut reads to the model as a
# COMPLETE result, and a bare cut of history reads as a complete conversation —
# so the model answers "you never told me that" about something it was told.
TRUNCATION_NOTE = (
    "[earlier turns in this conversation are not shown — they no longer fit "
    "the context window. If the user refers to something from earlier that you "
    "cannot see, say so and ask them to repeat it. Do not assume it was never "
    "said.]"
)


def build_context_messages(
    messages: list,
    *,
    pending_attachments: bool = False,
    truncated: bool = False,
    pinned_attachments: list[dict] | None = None,
) -> list[dict[str, str]]:
    """Clean visible turns -> the [{role, content}] the model sees.

    (…keep the existing docstring body verbatim…)

    `truncated` prepends TRUNCATION_NOTE — see that constant.
    `pinned_attachments` is `Selection.pinned_attachments`: the newest
    attachment set whose own message fell outside the budget. It is replayed as
    the ACTIVE set, and every surviving set is demoted, because two active sets
    leave the model guessing which ids the user means.
    """
    attached_at = [
        i for i, m in enumerate(messages) if m.role == ROLE_USER and m.attachments
    ]
    # A pinned set is the active one, so nothing replayed inline may claim to be.
    suppress_inline_active = pending_attachments or pinned_attachments is not None
    active_idx = None if suppress_inline_active else (attached_at[-1] if attached_at else None)

    out: list[dict[str, str]] = []
    if truncated:
        out.append({"role": "user", "content": TRUNCATION_NOTE})
    if pinned_attachments and not pending_attachments:
        out.append({
            "role": "user",
            "content": format_attachment_note(pinned_attachments, active=True),
        })
    for i, m in enumerate(messages):
        if m.role == ROLE_USER and m.attachments:
            out.append({
                "role": "user",
                "content": format_attachment_note(m.attachments, active=(i == active_idx)),
            })
        out.append({"role": m.role, "content": m.content})
    return out
```

Update the two existing test files' imports only:

- `tests/test_history_helpers.py:4` → `from app.history.context import build_context_messages`, and keep `from app.history.repository import make_title` (`make_title` stays in `repository.py` — it is not context logic).
- `tests/test_attachment_note.py:5` → `from app.history import context as repo` (the local alias `repo` is used throughout that file; aliasing keeps the diff to one line and every assertion untouched).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_history_context.py tests/test_history_helpers.py tests/test_attachment_note.py -v`
Expected: all pass. **If any assertion in the two pre-existing files had to change, STOP** — the move altered behaviour.

- [ ] **Step 5: Commit**

```bash
git add app/history/context.py app/history/repository.py tests/
git commit -m "refactor(history): move the context builder beside the budget, add the truncation note"
```

---

### Task 5: Bounded context read + wire the turn path

**Files:**
- Modify: `app/history/repository.py` (add `get_context_tail`)
- Modify: `app/history/service.py:71-86`
- Test: `tests/test_history_integration.py`

**Interfaces:**
- Consumes: `budget_for`, `select_turns`, `build_context_messages` from Tasks 2-4; the existing `repo.get_owned_session`.
- Produces: `get_context_tail(session, *, session_id, user_id, max_messages) -> list[ChatMessage]` returning ascending `seq`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_history_integration.py`:

```python
def test_a_long_thread_does_not_grow_the_prompt_without_bound(monkeypatch):
    """The turn path must budget history. Before this, a long conversation
    overflowed the window and Ollama silently dropped the FRONT of the prompt —
    the identity and date system prompt — while still returning a normal answer.
    """
    from app.config import get_settings
    from app.history import repository as repo

    # This module's own pattern: a TestClient context + _auth_headers.
    with TestClient(app) as client:
        headers = _auth_headers(client)

    # A small window makes the budget bite within a handful of turns.
    settings = get_settings()
    monkeypatch.setattr(settings, "context_window_tokens", 8000, raising=False)

    seen_prompt_sizes = []

    class Recording(FakeOllama):
        async def stream_chat(self, payload):
            seen_prompt_sizes.append(
                sum(len(m["content"]) for m in payload["messages"])
            )
            async for chunk in super().stream_chat(payload):
                yield chunk

    app.state.ollama = Recording()

    session_id = None
    for i in range(12):
        body = {"message": "x" * 3000, "stream": False}
        if session_id:
            body["session_id"] = session_id
        resp = client.post("/v1/chat", json=body, headers=headers)
        assert resp.status_code == 200
        session_id = resp.json()["session_id"]

    # The prompt stops growing rather than climbing with every turn.
    assert seen_prompt_sizes[-1] < seen_prompt_sizes[5] * 2
    assert max(seen_prompt_sizes[6:]) <= max(seen_prompt_sizes[:6]) * 2


def test_the_model_is_told_when_earlier_turns_were_dropped(monkeypatch):
    from app.config import get_settings
    from app.history.context import TRUNCATION_NOTE

    with TestClient(app) as client:
        headers = _auth_headers(client)
        monkeypatch.setattr(get_settings(), "context_window_tokens", 8000, raising=False)

    prompts = []

    class Recording(FakeOllama):
        async def stream_chat(self, payload):
            prompts.append(payload["messages"])
            async for chunk in super().stream_chat(payload):
                yield chunk

    app.state.ollama = Recording()

    session_id = None
    for _ in range(12):
        body = {"message": "y" * 3000, "stream": False}
        if session_id:
            body["session_id"] = session_id
        session_id = client.post("/v1/chat", json=body, headers=headers).json()["session_id"]

    assert any(
        TRUNCATION_NOTE in m["content"] for m in prompts[-1]
    ), "a truncated context must announce itself to the model"
```

Both tests body-indent under the `with TestClient(app) as client:` block, matching every other test in this module (see `test_history_integration.py:125`). `_auth_headers` and `FakeOllama` already exist there — do not add second copies. Assign `app.state.ollama = Recording()` inside the `with` block, and call `_cleanup(client, headers)` at the end so the seeded sessions do not leak into other tests (that leak is what CLAUDE.md records as eventually breaking a different test).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_history_integration.py -k "without_bound or dropped" -v`
Expected: FAIL — the prompt grows every turn and no note appears. (SKIP if Postgres is unreachable; start it and re-run — a skip is not a pass.)

- [ ] **Step 3: Write minimal implementation**

In `app/history/repository.py`, add:

```python
async def get_context_tail(
    session: AsyncSession, *, session_id: str, user_id: int, max_messages: int
) -> list[ChatMessage]:
    """The newest `max_messages` of a thread, ascending by seq, for the PROMPT.

    Two things make this distinct from a thread page:

    `trace` and `sources` are NOT selected. Neither is ever in a prompt and they
    are the fat JSONB columns — loading them only to discard them was most of
    the old cost.

    This is a DB-side bound applied BEFORE the token budget, so a 500-turn
    thread never materializes. `max_messages` is far more than any budget can
    hold, so it never decides what the model sees; the budget does.

    Ownership is in the same WHERE as the page — never fetch-then-check.
    """
    newest = (
        select(
            ChatMessage.seq,
            ChatMessage.role,
            ChatMessage.content,
            ChatMessage.attachments,
        )
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .where(ChatSession.id == session_id, ChatSession.user_id == user_id)
        .order_by(ChatMessage.seq.desc())
        .limit(max_messages)
        .subquery()
    )
    result = await session.execute(select(newest).order_by(newest.c.seq.asc()))
    # Detached, partially-populated ChatMessage objects: the pure context module
    # reads role/content/attachments and nothing else.
    out = []
    for row in result.all():
        m = ChatMessage(
            session_id=session_id, seq=row.seq, role=row.role, content=row.content
        )
        m.attachments = row.attachments
        out.append(m)
    return out
```

In `app/history/service.py`, replace the `if session_id:` branch body (lines 71-86):

```python
    if session_id:
        # The session ROW only — its messages are read separately and bounded.
        chat_session = await repo.get_owned_session(
            session, session_id=session_id, user_id=user_id
        )
        if chat_session is None:
            raise HTTPException(status_code=404, detail="session not found")
        # Re-checked on EVERY turn, which is what makes a revoked grant take
        # effect on the next turn rather than at token expiry.
        dept_ctx = await resolve_department(session, user, department, chat_session)
        settings = get_settings()
        tail = await repo.get_context_tail(
            session,
            session_id=session_id,
            user_id=user_id,
            max_messages=settings.context_max_messages,
        )
        budget = ctx.budget_for(settings)
        selection = ctx.select_turns(tail, budget)
        # A new upload supersedes every earlier one, so the replayed notes are
        # demoted and only the note appended below stays active.
        context = ctx.build_context_messages(
            selection.messages,
            pending_attachments=bool(attachments),
            truncated=selection.truncated,
            pinned_attachments=selection.pinned_attachments,
        )
        _log_budget(session_id, tail, selection, budget)
```

Add imports and the log helper to `app/history/service.py`:

```python
import logging

from ..config import get_settings
from . import context as ctx

logger = logging.getLogger(__name__)


def _log_budget(session_id, tail, selection, budget) -> None:
    """Why this is logged, not merely computed: `context_window_tokens` is a
    duplicate of a value set on the Ollama service that this process cannot read
    back. If the two disagree, every symptom looks like a healthy turn. This
    line is the only place the disagreement becomes visible, and it is the
    dataset the estimator's constants get calibrated against — compare it with
    the server's reported usage.prompt_tokens.
    """
    spent = sum(ctx.estimate_message_tokens(m) for m in selection.messages)
    logger.info(
        "context session=%s read=%d selected=%d est_tokens=%d budget=%d truncated=%s",
        session_id, len(tail), len(selection.messages), spent, budget,
        selection.truncated,
    )
```

Replace `repo.format_attachment_note` at `service.py:112` with `ctx.format_attachment_note` (it moved in Task 4).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_history_integration.py -v`
Expected: all pass, **including the pre-existing multi-turn context tests** in that file.

- [ ] **Step 5: Commit**

```bash
git add app/history/repository.py app/history/service.py tests/test_history_integration.py
git commit -m "feat(history): bound the prompt context by tokens instead of replaying every turn"
```

---

### Task 6: Cursor codec

**Files:**
- Create: `app/history/cursors.py`
- Test: `tests/test_history_cursors.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `encode_session_cursor(updated_at: datetime, id: str) -> str`, `decode_session_cursor(raw: str) -> tuple[datetime, str]`, `encode_seq_cursor(seq: int) -> str`, `decode_seq_cursor(raw: str) -> int`, and `class BadCursor(Exception)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_history_cursors.py`:

```python
"""Pure tests for the pagination cursor codec (no DB)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.history.cursors import (
    BadCursor,
    decode_seq_cursor,
    decode_session_cursor,
    encode_seq_cursor,
    encode_session_cursor,
)


def test_session_cursor_round_trips():
    when = datetime(2026, 8, 22, 13, 45, 6, 123456, tzinfo=timezone.utc)
    raw = encode_session_cursor(when, "abc123")
    assert decode_session_cursor(raw) == (when, "abc123")


def test_session_cursor_is_opaque():
    # Opaque so the keyset tiebreaker can change later without a client caring.
    raw = encode_session_cursor(datetime.now(timezone.utc), "abc123")
    assert "abc123" not in raw


def test_seq_cursor_round_trips():
    assert decode_seq_cursor(encode_seq_cursor(42)) == 42


@pytest.mark.parametrize("bad", ["", "!!!!", "Zm9v", "not-base64", "eyJhIjoxfQ=="])
def test_a_bad_cursor_raises_rather_than_silently_meaning_page_one(bad):
    # A client stuck re-reading page one looks like "history is broken" and is
    # invisible server-side. The router turns this into a 400.
    with pytest.raises(BadCursor):
        decode_session_cursor(bad)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_history_cursors.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.history.cursors'`

- [ ] **Step 3: Write minimal implementation**

Create `app/history/cursors.py`:

```python
"""Opaque keyset cursors for chat-history pagination.

Keyset, not offset, and that is not a style choice: sessions sort by
`updated_at DESC` and every new turn bumps `updated_at`, so rows MOVE BETWEEN
PAGES while the user scrolls. Offset paging would show some sessions twice and
skip others entirely.

The payload is base64 so it is opaque — the session keyset needs `id` as a
tiebreaker today because `updated_at` is not unique, and a client that never
parsed the cursor cannot break when that changes.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime


class BadCursor(Exception):
    """An undecodable cursor. The router answers 400 — never a silent page one."""


def _encode(payload: str) -> str:
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode(raw: str) -> str:
    if not raw:
        raise BadCursor("empty cursor")
    try:
        return base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise BadCursor(str(exc)) from exc


def encode_session_cursor(updated_at: datetime, id: str) -> str:
    return _encode(f"s|{updated_at.isoformat()}|{id}")


def decode_session_cursor(raw: str) -> tuple[datetime, str]:
    parts = _decode(raw).split("|")
    if len(parts) != 3 or parts[0] != "s":
        raise BadCursor("not a session cursor")
    try:
        return datetime.fromisoformat(parts[1]), parts[2]
    except ValueError as exc:
        raise BadCursor(str(exc)) from exc


def encode_seq_cursor(seq: int) -> str:
    return _encode(f"m|{seq}")


def decode_seq_cursor(raw: str) -> int:
    parts = _decode(raw).split("|")
    if len(parts) != 2 or parts[0] != "m":
        raise BadCursor("not a message cursor")
    try:
        return int(parts[1])
    except ValueError as exc:
        raise BadCursor(str(exc)) from exc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_history_cursors.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add app/history/cursors.py tests/test_history_cursors.py
git commit -m "feat(history): opaque keyset cursors for history pagination"
```

---

### Task 7: Paginate the session list

**Files:**
- Modify: `app/history/repository.py` (replace `list_sessions` with `list_sessions_page`)
- Modify: `app/history/schemas.py` (add `SessionPage`)
- Modify: `app/history/router.py:21-40`
- Test: `tests/test_history_pagination.py`

**Interfaces:**
- Consumes: `encode_session_cursor`/`decode_session_cursor`/`BadCursor` from Task 6.
- Produces: `list_sessions_page(session, *, user_id, limit, cursor=None) -> tuple[list[tuple[ChatSession, int]], str | None]`; `SessionPage(items: list[SessionSummary], next_cursor: str | None)`; `DEFAULT_PAGE_LIMIT = 30`, `MAX_PAGE_LIMIT = 100`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_history_pagination.py`:

```python
"""Integration tests for chat-history pagination (real Postgres).

A throwaway NullPool engine per call: the app's module-level engine pools
connections bound to the first event loop, and each asyncio.run makes a new one,
so a second test would die with "Event loop is closed".
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.history import repository as repo
from app.users.models import User


def _run(coro_fn):
    async def _go():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                return await coro_fn(session)
        finally:
            await engine.dispose()

    try:
        return asyncio.run(_go())
    except (OperationalError, InterfaceError, OSError) as exc:
        # ONLY a genuine connection failure skips — a blanket except would let
        # a real bug in the code under test present as "Postgres unreachable".
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}: {exc}")


async def _seed_user(session) -> int:
    user = User(
        email=f"page-{uuid.uuid4().hex[:8]}@example.com",
        auth_provider="local",
        # ck_users_credential: a local user MUST have a hash.
        password_hash="$2b$12$" + "x" * 53,
        role="member",
        is_active=True,
    )
    session.add(user)
    await session.flush()
    return user.id


def test_paging_returns_every_session_exactly_once():
    async def go(session):
        user_id = await _seed_user(session)
        for i in range(25):
            s = await repo.create_session(session, user_id=user_id, title=f"t{i}")
            await repo.add_user_message(session, session_id=s.id, content="hi")
        await session.commit()

        seen, cursor = [], None
        for _ in range(10):
            rows, cursor = await repo.list_sessions_page(
                session, user_id=user_id, limit=10, cursor=cursor
            )
            seen.extend(s.id for s, _ in rows)
            if cursor is None:
                break
        assert len(seen) == 25
        assert len(set(seen)) == 25

    _run(go)


def test_paging_is_stable_when_a_session_is_touched_mid_scroll():
    """The case offset paging silently gets wrong. Bumping a session's
    updated_at moves it to the front; with OFFSET the rows behind it shift and
    one session is shown twice while another is never shown at all."""

    async def go(session):
        user_id = await _seed_user(session)
        made = []
        for i in range(20):
            s = await repo.create_session(session, user_id=user_id, title=f"t{i}")
            await repo.add_user_message(session, session_id=s.id, content="hi")
            made.append(s.id)
        await session.commit()

        first, cursor = await repo.list_sessions_page(
            session, user_id=user_id, limit=5, cursor=None
        )
        # Touch the OLDEST session, jumping it to the front of the ordering.
        await repo.add_user_message(session, session_id=made[0], content="again")
        await session.commit()

        rest, seen = [], [s.id for s, _ in first]
        for _ in range(10):
            rows, cursor = await repo.list_sessions_page(
                session, user_id=user_id, limit=5, cursor=cursor
            )
            rest.extend(s.id for s, _ in rows)
            if cursor is None:
                break
        # No duplicates across the scroll.
        assert len(set(seen + rest)) == len(seen + rest)

    _run(go)


def test_message_count_matches_a_direct_count():
    async def go(session):
        user_id = await _seed_user(session)
        s = await repo.create_session(session, user_id=user_id, title="counted")
        for _ in range(7):
            await repo.add_user_message(session, session_id=s.id, content="hi")
        await session.commit()

        rows, _ = await repo.list_sessions_page(session, user_id=user_id, limit=10)
        counts = {row[0].id: row[1] for row in rows}
        assert counts[s.id] == 7

    _run(go)


def test_another_users_sessions_are_never_returned():
    async def go(session):
        mine = await _seed_user(session)
        theirs = await _seed_user(session)
        s = await repo.create_session(session, user_id=theirs, title="not yours")
        await repo.add_user_message(session, session_id=s.id, content="hi")
        await session.commit()

        rows, _ = await repo.list_sessions_page(session, user_id=mine, limit=100)
        assert all(row[0].user_id == mine for row in rows)

    _run(go)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_history_pagination.py -v`
Expected: FAIL — `AttributeError: module 'app.history.repository' has no attribute 'list_sessions_page'`. (If it SKIPS, Postgres is down — start it. A skip is not a pass.)

- [ ] **Step 3: Write minimal implementation**

In `app/history/repository.py`, replace `list_sessions` entirely:

```python
DEFAULT_PAGE_LIMIT = 30
MAX_PAGE_LIMIT = 100


async def list_sessions_page(
    session: AsyncSession,
    *,
    user_id: int,
    limit: int = DEFAULT_PAGE_LIMIT,
    cursor: str | None = None,
) -> tuple[list[tuple[ChatSession, int]], str | None]:
    """One page of (session, message_count), newest-updated first.

    KEYSET, not offset: every turn bumps `updated_at`, so rows move between
    pages while the user scrolls and offset paging would duplicate and skip.
    `id` is the tiebreaker because `updated_at` is not unique.

    The count is a CORRELATED SUBQUERY, so it is computed for the ~30 rows
    actually returned. The old outer join + GROUP BY aggregated every message
    the user had ever sent, to populate a field for rows below the fold.
    """
    limit = max(1, min(limit, MAX_PAGE_LIMIT))
    count_sq = (
        select(func.count(ChatMessage.id))
        .where(ChatMessage.session_id == ChatSession.id)
        .correlate(ChatSession)
        .scalar_subquery()
    )
    stmt = (
        select(ChatSession, count_sq)
        .where(ChatSession.user_id == user_id)
        .order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
        .limit(limit + 1)  # one extra row tells us whether more exist
    )
    if cursor is not None:
        after_updated, after_id = decode_session_cursor(cursor)
        stmt = stmt.where(
            tuple_(ChatSession.updated_at, ChatSession.id)
            < tuple_(literal(after_updated), literal(after_id))
        )

    rows = [(row[0], row[1]) for row in (await session.execute(stmt)).all()]
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = (
        encode_session_cursor(rows[-1][0].updated_at, rows[-1][0].id)
        if has_more and rows
        else None
    )
    return rows, next_cursor
```

Add to that file's imports:

```python
from sqlalchemy import delete, func, literal, select, tuple_, update

from .cursors import decode_session_cursor, encode_session_cursor
```

In `app/history/schemas.py`:

```python
class SessionPage(BaseModel):
    """GET /v1/sessions — one page of the sidebar.

    An envelope rather than a bare array so pagination is visible in the
    OpenAPI schema. A cursor hidden in a header would let a client that ignores
    it read page one and believe it had everything.
    """

    items: list[SessionSummary]
    next_cursor: Optional[str] = None
```

In `app/history/router.py`, replace `list_my_sessions`:

```python
@router.get(
    "",
    response_model=SessionPage,
    summary="List my chat sessions, newest-updated first (paginated)",
    responses={400: {"description": "Malformed cursor."}},
)
async def list_my_sessions(
    limit: int = Query(repo.DEFAULT_PAGE_LIMIT, ge=1, le=repo.MAX_PAGE_LIMIT),
    cursor: str | None = Query(None, description="Opaque; from a prior next_cursor."),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SessionPage:
    try:
        rows, next_cursor = await repo.list_sessions_page(
            session, user_id=user.id, limit=limit, cursor=cursor
        )
    except BadCursor as exc:
        # 400, never a silent page one: a client stuck re-reading the first
        # page looks like "history is broken" and is invisible server-side.
        raise HTTPException(status_code=400, detail=f"invalid cursor: {exc}") from exc
    return SessionPage(
        items=[
            SessionSummary(
                id=s.id,
                title=s.title,
                created_at=s.created_at,
                updated_at=s.updated_at,
                message_count=count,
            )
            for s, count in rows
        ],
        next_cursor=next_cursor,
    )
```

Add to the router's imports: `Query` from fastapi, `BadCursor` from `.cursors`, `SessionPage` from `.schemas`.

- [ ] **Step 4: Update the two existing consumers of the bare array**

The envelope is a breaking change INSIDE the test suite too. Two places read
`GET /v1/sessions` as a list and will raise `TypeError` / silently iterate the
envelope's keys:

- `tests/test_history_integration.py:120` (`_cleanup`):
```python
    for s in client.get("/v1/sessions", headers=headers).json()["items"]:
        client.delete(f"/v1/sessions/{s['id']}", headers=headers)
```
  Note `_cleanup` now only deletes the FIRST page. Pass `?limit=100` so a test
  that created more than 30 sessions still cleans up after itself:
```python
    resp = client.get("/v1/sessions?limit=100", headers=headers).json()
```
- `tests/test_history_integration.py:156` (`listed = …`): read `["items"]` and
  keep the surrounding assertions unchanged.

`tests/test_protected_endpoints.py:13` only asserts the status code and needs no
change.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_history_pagination.py tests/test_history_integration.py tests/test_protected_endpoints.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add app/history/ tests/test_history_pagination.py tests/test_history_integration.py
git commit -m "feat(history): keyset-paginate GET /v1/sessions behind an envelope"
```

---

### Task 8: Paginate one thread, delete the unbounded read

**Files:**
- Modify: `app/history/repository.py` (add `get_thread_page`, delete `get_session_with_messages`)
- Modify: `app/history/schemas.py` (`SessionDetail` gains `next_cursor`)
- Modify: `app/history/router.py:42-79`
- Test: `tests/test_history_pagination.py`

**Interfaces:**
- Consumes: `encode_seq_cursor`/`decode_seq_cursor`/`BadCursor` from Task 6.
- Produces: `get_thread_page(session, *, session_id, user_id, limit, before_seq=None) -> tuple[ChatSession | None, list[ChatMessage], str | None]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_history_pagination.py`:

```python
def test_a_thread_page_is_the_NEWEST_messages_in_ascending_order():
    """A chat opens at the bottom, so the first page is the newest messages —
    but returned ascending, because the frontend renders top-to-bottom."""

    async def go(session):
        user_id = await _seed_user(session)
        s = await repo.create_session(session, user_id=user_id, title="t")
        for i in range(30):
            await repo.add_user_message(session, session_id=s.id, content=f"m{i}")
        await session.commit()

        row, msgs, cursor = await repo.get_thread_page(
            session, session_id=s.id, user_id=user_id, limit=10
        )
        assert row is not None
        assert [m.seq for m in msgs] == sorted(m.seq for m in msgs)
        assert msgs[-1].content == "m29"
        assert cursor is not None

    _run(go)


def test_walking_a_thread_backwards_covers_every_message_once():
    async def go(session):
        user_id = await _seed_user(session)
        s = await repo.create_session(session, user_id=user_id, title="t")
        for i in range(25):
            await repo.add_user_message(session, session_id=s.id, content=f"m{i}")
        await session.commit()

        seen, cursor = [], None
        for _ in range(10):
            _, msgs, cursor = await repo.get_thread_page(
                session, session_id=s.id, user_id=user_id, limit=10,
                before_seq=cursor,
            )
            seen.extend(m.seq for m in msgs)
            if cursor is None:
                break
        assert sorted(seen) == list(range(1, 26))

    _run(go)


def test_a_foreign_thread_is_not_readable():
    async def go(session):
        mine = await _seed_user(session)
        theirs = await _seed_user(session)
        s = await repo.create_session(session, user_id=theirs, title="t")
        await repo.add_user_message(session, session_id=s.id, content="secret")
        await session.commit()

        row, msgs, _ = await repo.get_thread_page(
            session, session_id=s.id, user_id=mine, limit=10
        )
        assert row is None
        assert msgs == []

    _run(go)


def test_the_unbounded_thread_read_is_gone():
    # Left in place, the next person reintroduces the bug this plan removed.
    assert not hasattr(repo, "get_session_with_messages")
    assert not hasattr(repo, "list_sessions")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_history_pagination.py -k "thread or unbounded" -v`
Expected: FAIL — no `get_thread_page`, and `get_session_with_messages` still exists.

- [ ] **Step 3: Write minimal implementation**

In `app/history/repository.py`, add and delete `get_session_with_messages`:

```python
async def get_thread_page(
    session: AsyncSession,
    *,
    session_id: str,
    user_id: int,
    limit: int = DEFAULT_PAGE_LIMIT,
    before_seq: str | None = None,
) -> tuple[ChatSession | None, list[ChatMessage], str | None]:
    """One page of a thread: (session_row, messages_ascending, next_cursor).

    Returns (None, [], None) when the session is unknown or not this user's —
    the router turns that into 404, and we never confirm it exists.

    The page SELECTED is the newest `limit` messages (a chat opens at the
    bottom); the page RETURNED is ascending by `seq` so the frontend renders
    top-to-bottom unchanged. The cursor walks older.

    Anchored on `seq`, not `created_at`: seq is already UNIQUE(session_id, seq)
    and already the relationship's order_by, so it is a total order with no
    tiebreaker needed.
    """
    limit = max(1, min(limit, MAX_PAGE_LIMIT))
    row = await get_owned_session(session, session_id=session_id, user_id=user_id)
    if row is None:
        return None, [], None

    stmt = (
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.seq.desc())
        .limit(limit + 1)
    )
    if before_seq is not None:
        stmt = stmt.where(ChatMessage.seq < decode_seq_cursor(before_seq))

    newest_first = list((await session.execute(stmt)).scalars().all())
    has_more = len(newest_first) > limit
    newest_first = newest_first[:limit]
    next_cursor = (
        encode_seq_cursor(newest_first[-1].seq) if has_more and newest_first else None
    )
    return row, list(reversed(newest_first)), next_cursor
```

Add `decode_seq_cursor, encode_seq_cursor` to the `.cursors` import.

In `app/history/schemas.py`, add to `SessionDetail`:

```python
    # Walks OLDER messages. Null when the thread's first message is included.
    next_cursor: Optional[str] = None
```

In `app/history/router.py`, replace the body of `get_my_session`:

```python
async def get_my_session(
    session_id: str,
    request: Request,
    limit: int = Query(repo.DEFAULT_PAGE_LIMIT, ge=1, le=repo.MAX_PAGE_LIMIT),
    cursor: str | None = Query(None, description="Opaque; walks older messages."),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SessionDetail:
    try:
        chat_session, rows, next_cursor = await repo.get_thread_page(
            session, session_id=session_id, user_id=user.id,
            limit=limit, before_seq=cursor,
        )
    except BadCursor as exc:
        raise HTTPException(status_code=400, detail=f"invalid cursor: {exc}") from exc
    if chat_session is None:
        raise HTTPException(status_code=404, detail="session not found")
    expose_trace = request.app.state.settings.expose_trace
    messages = []
    for m in rows:
        out = MessageOut.model_validate(m)
        if not expose_trace:
            out.trace = None
        out.sources = with_download_urls(out.sources)
        messages.append(out)
    return SessionDetail(
        id=chat_session.id,
        title=chat_session.title,
        created_at=chat_session.created_at,
        updated_at=chat_session.updated_at,
        messages=messages,
        next_cursor=next_cursor,
    )
```

Keep the existing comment above `expose_trace` verbatim — it explains why the trace stays in the database.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/ -k "history or chat or session" -v`
Expected: all pass. Then the whole suite:
Run: `.venv/bin/pytest -q`
Expected: no NEW failures. `tests/test_rag_reingest_integration.py::test_department_filter_restricts_the_set` is a known pre-existing failure on a developer database with real data (CLAUDE.md). **Compare the SKIP count against a pre-change run** — CLAUDE.md warns that broken auth helpers turn 86 tests into silent skips a green run hides.

- [ ] **Step 5: Commit**

```bash
git add app/history/ tests/test_history_pagination.py
git commit -m "feat(history): paginate one thread and delete the unbounded reads"
```

---

### Task 9: Document the invariants

**Files:**
- Modify: `CLAUDE.md` (the Conventions/gotchas section, and the Endpoints section)
- Modify: `docs/superpowers/specs/2026-08-22-chat-history-lazy-loading-design.md` (record the measurement)

**Interfaces:**
- Consumes: everything above.
- Produces: nothing code-facing.

- [ ] **Step 1: Update the Endpoints section**

In `CLAUDE.md`, change the `GET /v1/sessions` and `GET /v1/sessions/{id}` entries to note the envelope, `?limit=`/`?cursor=`, and the 400 on a malformed cursor.

- [ ] **Step 2: Add the gotcha**

Add to the Conventions / gotchas section:

```markdown
- **Chat history is paginated by KEYSET, and the model's context is budgeted
  separately from either read.** Three loads used to be unbounded; two of them
  shared one repository function, so a `LIMIT` added for the UI would have
  silently truncated the model's prompt. They are now three bounded reads:
  `list_sessions_page` (keyset on `(updated_at DESC, id DESC)` — offset paging
  duplicates and skips, because every turn bumps `updated_at` and rows move
  between pages mid-scroll), `get_thread_page` (keyset on `seq`, newest page
  selected, ascending order returned), and `get_context_tail` (newest N,
  selecting only `seq/role/content/attachments` — `trace` and `sources` are
  never in a prompt and were most of the cost). `get_session_with_messages` and
  `list_sessions` are DELETED, and a test asserts they stay gone.
  `app/history/context.py` owns the budget and is pure — no DB, no model — for
  the `app/rag/ranking.py` reason: the rule deciding what the model sees should
  be provable without a database or a GPU. Five things a rewrite must not lose:
  (1) **the estimator is SCRIPT-AWARE** — `len/4` is an English ratio, and on
  this mixed Nepali/English corpus it under-counts Devanagari into the very
  overflow the budget prevents; the constants are pessimistic on purpose,
  because over-estimating wastes window while under-estimating lets Ollama drop
  the FRONT of the prompt, where the identity and date system prompt lives;
  (2) **selection keeps whole turns**, so no dangling assistant reply can head
  the context as if it had spoken unprompted; (3) **the active attachment set is
  PINNED** when its own turn falls outside the budget — losing the file ids
  makes the model ask for an id it was handed, the failure
  `test_attachment_note_is_a_user_message_not_a_system_one` exists for — and a
  pinned set demotes every surviving one, because two active sets leave the
  model guessing; (4) **dropped turns announce themselves** via
  `TRUNCATION_NOTE`, for the same reason `_for_model` appends `[TRUNCATED …]`:
  a bare cut of history reads as a COMPLETE conversation, so the model denies
  being told what it was told — and the note is absent when nothing was
  dropped, because an always-present warning trains the model to ignore it;
  (5) **`CONTEXT_WINDOW_TOKENS` is a second copy of `OLLAMA_CONTEXT_LENGTH`**
  that this process cannot read back (no `num_ctx` field, no reported window),
  so a mismatch is silent and every symptom looks like a healthy turn —
  `service._log_budget` logging the estimate is the only place it becomes
  visible, and it is the dataset the estimator is calibrated against.
```

- [ ] **Step 3: Add the eval case set**

The spec's Evaluation section names an 8-thread labelled set. Create
`tests/test_history_context_eval.py` — the pure half runs offline and gates the
five invariants; the estimate-vs-actual half is skipped without a live model,
and SAYS so rather than passing vacuously:

```python
"""The 8-case eval for the context budget (see the design doc's Evaluation
section). The pure cases run everywhere. The estimate-vs-actual case needs the
live model and SKIPS without it — a skip here is an unmeasured metric, not a
pass, so read the skip count."""

from __future__ import annotations

import pytest

from app.history.context import budget_for, estimate_tokens, select_turns

# name, builder -> messages
CASES = [
    "short_latin", "short_devanagari", "long_latin", "long_devanagari",
    "mixed", "active_upload_beyond_budget", "single_over_budget_message",
    "exactly_at_the_boundary",
]


@pytest.mark.parametrize("case", CASES)
def test_every_case_yields_a_non_empty_selection_and_pairs_turns(case):
    msgs = _build(case)          # local builder, one branch per case name
    sel = select_turns(msgs, budget=2000)
    assert sel.messages, f"{case}: an empty context is never acceptable"
    assert sel.messages[0].role == "user", f"{case}: dangling assistant"


@pytest.mark.parametrize("case", CASES)
def test_the_estimate_never_falls_below_the_servers_own_count(case):
    pytest.skip("needs the live model: compare est_tokens with usage.prompt_tokens")
```

Write `_build` with one explicit branch per case name — no shared fixture that
makes the Devanagari and latin cases secretly identical.

- [ ] **Step 4: Record the calibration measurement**

Run a real turn against the live model with a long thread, read `usage.prompt_tokens` from the response, and compare it with the `est_tokens=` figure in the log line. Record both numbers, and the resulting decision on `LATIN_CHARS_PER_TOKEN`/`DEVANAGARI_CHARS_PER_TOKEN`, in the design doc's Evaluation section. **If the estimate came out BELOW the actual, raise the constants and re-measure** — that direction is the one that reaches a user.

If the live model is unavailable, write that down explicitly as unmeasured rather than leaving the section implying it was checked.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md tests/test_history_context_eval.py docs/superpowers/specs/2026-08-22-chat-history-lazy-loading-design.md
git commit -m "docs: record the history pagination and context-budget invariants"
```

---

## Out of scope

- **The frontend.** `../react/local-ai-model-frontend` needs a paired branch for the envelope (`api.ts:457`, `api.ts:463`) plus infinite-scroll in `hooks/useSessions.ts`. `message_count` is retained, so `Sidebar.tsx:251` does not change. Ships together with this branch, like `feat/role`/`feat/roles`.
- **A rolling summary** of dropped turns. Rejected in the spec: an extra model call per turn, and a summary that can silently misstate earlier facts is the wrong risk in a bank. The budget leaves a clean seam if long threads ever prove to need it.
- **Session title rename** and **file pagination** — separate open items in CLAUDE.md.
