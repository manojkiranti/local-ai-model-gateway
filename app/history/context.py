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
DEVANAGARI_CHARS_PER_TOKEN = 1.0
# Per-message cost of the role framing the model server adds around content.
MESSAGE_OVERHEAD_TOKENS = 4
SAFETY_MARGIN = 1.10

from .models import ROLE_USER
from .repository import format_attachment_note  # moves into this module in Task 4


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
