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
from dataclasses import dataclass
from typing import Any

# Devanagari block: U+0900..U+097F, plus the extended block U+A8E0..U+A8FF.
_DEVANAGARI_RANGES = ((0x0900, 0x097F), (0xA8E0, 0xA8FF))

# Pessimistic on purpose — see the module docstring.
LATIN_CHARS_PER_TOKEN = 3.5
# MEASURED, not assumed: against qwen2.5:latest (local dev Ollama; the
# production qwen3.5:35b-a3b is unreachable from this environment) a
# Devanagari-heavy thread's RAW estimate (before SAFETY_MARGIN) came out at
# ~0.92x the server's actual usage.prompt_tokens at 1.0 chars/token — i.e.
# already BELOW actual, with only the margin holding the line. 0.85 was
# picked from that same measurement: it puts the raw estimate at ~1.07x
# actual (comfortably above 1.0 on its own) and the margined estimate at
# ~1.18x. See the design doc's Evaluation section for the full table and the
# re-measurement across multiple samples that confirmed it.
DEVANAGARI_CHARS_PER_TOKEN = 0.85
# Per-message cost of the role framing the model server adds around content.
MESSAGE_OVERHEAD_TOKENS = 4
SAFETY_MARGIN = 1.10

from .models import ROLE_ASSISTANT, ROLE_USER


def format_attachment_note(attachments: list[dict[str, Any]], *, active: bool = True) -> str:
    """A short note naming files attached to a user message, so the model knows
    their ids and can call read_document / read_image / inspect_excel /
    read_excel on them.
    Pure/formatting only — the caller decides the role it is emitted under (see
    `build_context_messages` and `service.open_turn`; both use `user`).

    `active=False` marks a SUPERSEDED set — files attached earlier in the
    conversation that a newer upload has replaced. Those are deliberately weaker:
    different wording, and no summary. An identically-worded note for every
    upload is what made a second file get ignored in favour of the first, which
    had a fat summary and a whole assistant answer behind it.
    """
    if not active:
        lines = ["Files attached earlier in this conversation (superseded — use "
                 "one of these ONLY if the user names that file):"]
        for a in attachments:
            lines.append(f'- id={a.get("id", "")} "{a.get("filename", "")}"')
        return "\n".join(lines)

    lines = ["Active files for the current request (read documents with "
             "read_document; images with read_image; for spreadsheets use "
             "inspect_excel / read_excel, and total them with aggregate_excel):"]
    for a in attachments:
        fid = a.get("id", "")
        name = a.get("filename", "")
        summary = a.get("summary", "")
        detail = f" ({summary})" if summary else ""
        lines.append(f'- id={fid} "{name}"{detail}')
    return "\n".join(lines)


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

    Only role/content is replayed; agent turns contribute their final answer
    (their `trace` is history, not context). A user message that carried file
    attachments re-emits its attachment note (a system message) just before it,
    so 'now total column B' on a later turn still knows the file ids without the
    frontend resending them. Ordering is the caller's (seq).

    Exactly ONE attachment set is ever active: the newest. Older sets are
    replayed as superseded (see `format_attachment_note`) so the model doesn't
    have to guess which of several ids the user means. `pending_attachments`
    says the turn being opened carries its own upload — then EVERY replayed set
    is superseded, because the caller appends the active note itself. With no
    new upload, the most recent replayed set stays active.

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
            # `user`, not `system` — see the measurement in service.open_turn. A
            # system-role note is read but not acted on once tool schemas are in
            # play, so the model asks for a file id it was already given.
            out.append({
                "role": "user",
                "content": format_attachment_note(m.attachments, active=(i == active_idx)),
            })
        out.append({"role": m.role, "content": m.content})
    return out
