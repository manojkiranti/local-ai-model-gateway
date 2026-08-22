"""The 8-case eval for the context budget (see the design doc's Evaluation
section). The pure cases run everywhere. The estimate-vs-actual case needs the
live model and SKIPS without it — a skip here is an unmeasured metric, not a
pass, so read the skip count."""

from __future__ import annotations

import pytest

from app.history.context import budget_for, estimate_tokens, select_turns
from app.history.models import ROLE_ASSISTANT, ROLE_USER

# name, builder -> messages
CASES = [
    "short_latin", "short_devanagari", "long_latin", "long_devanagari",
    "mixed", "active_upload_beyond_budget", "single_over_budget_message",
    "exactly_at_the_boundary",
]

LATIN_SENTENCE = (
    "The quarterly compliance review covers loan origination, KYC checks, "
    "and anti-money-laundering controls across all branches. "
)
DEVANAGARI_SENTENCE = (
    "नेपाल राष्ट्र बैंकले मौद्रिक नीतिको कार्यान्वयन तथा वित्तीय स्थायित्वका लागि "
    "विभिन्न निर्देशिका जारी गरेको छ। "
)


class _Msg:
    """A minimal stand-in for ChatMessage — only what context.py reads."""

    def __init__(self, role: str, content: str, attachments: list | None = None):
        self.role = role
        self.content = content
        self.attachments = attachments


def _turn(user_text: str, assistant_text: str = "Understood.", attachments=None) -> list[_Msg]:
    return [
        _Msg(ROLE_USER, user_text, attachments=attachments),
        _Msg(ROLE_ASSISTANT, assistant_text),
    ]


def _build(case: str) -> list[_Msg]:
    if case == "short_latin":
        msgs: list[_Msg] = []
        msgs += _turn("What is our KYC policy for new accounts?")
        msgs += _turn("Please summarize the last audit finding.")
        return msgs

    if case == "short_devanagari":
        msgs = []
        msgs += _turn("मुद्रा दर के हो?")
        msgs += _turn("परिपत्र नम्बर के हो?")
        return msgs

    if case == "long_latin":
        msgs = []
        for i in range(30):
            msgs += _turn(
                f"Turn {i}: " + LATIN_SENTENCE * 8,
                assistant_text="Acknowledged. " + LATIN_SENTENCE * 4,
            )
        return msgs

    if case == "long_devanagari":
        msgs = []
        for i in range(30):
            msgs += _turn(
                f"वार्ता {i}: " + DEVANAGARI_SENTENCE * 8,
                assistant_text="बुझें। " + DEVANAGARI_SENTENCE * 4,
            )
        return msgs

    if case == "mixed":
        msgs = []
        for i in range(20):
            if i % 2 == 0:
                msgs += _turn(f"Turn {i}: " + LATIN_SENTENCE * 5)
            else:
                msgs += _turn(f"वार्ता {i}: " + DEVANAGARI_SENTENCE * 5)
        return msgs

    if case == "active_upload_beyond_budget":
        msgs = []
        for i in range(30):
            msgs += _turn(f"Old turn {i}: " + LATIN_SENTENCE * 8)
        # The newest turn carries an upload but is itself huge enough that,
        # combined with everything ahead of it, it will not survive selection
        # under a small budget — pinning must carry the attachment forward.
        msgs += _turn(
            "Now total column B in the attached file: " + LATIN_SENTENCE * 30,
            attachments=[{"id": "f1", "filename": "ledger.xlsx"}],
        )
        return msgs

    if case == "single_over_budget_message":
        # One turn, alone, larger than any reasonable budget — must still be
        # kept (an empty context is worse than an over-long one).
        return _turn("Huge question: " + LATIN_SENTENCE * 400)

    if case == "exactly_at_the_boundary":
        # A handful of turns sized so selection sits right at the edge of the
        # 2000-token budget used by the test below.
        msgs = []
        for i in range(6):
            msgs += _turn(f"Boundary turn {i}: " + LATIN_SENTENCE * 6)
        return msgs

    raise ValueError(f"unknown case: {case}")


@pytest.mark.parametrize("case", CASES)
def test_every_case_yields_a_non_empty_selection_and_pairs_turns(case):
    msgs = _build(case)          # local builder, one branch per case name
    sel = select_turns(msgs, budget=2000)
    assert sel.messages, f"{case}: an empty context is never acceptable"
    assert sel.messages[0].role == "user", f"{case}: dangling assistant"


@pytest.mark.parametrize("case", CASES)
def test_the_estimate_never_falls_below_the_servers_own_count(case):
    pytest.skip("needs the live model: compare est_tokens with usage.prompt_tokens")
