"""The assistant's self-identity is deployment-branded, not the model's own.

Without this the model answers "I am Qwen" to any bank user who asks. Branding
the product is fine; inventing a training history is not, so the prompt is
explicit about what NOT to claim.
"""

from __future__ import annotations

import pytest

from app.agent.loop import build_system_prompt, run_turn
from app.config import Settings
from tests.test_agent_loop import FakeMCP, RecordingOllama, text_turn


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _settings(**kw):
    return Settings(**kw)


def test_prompt_carries_name_and_org():
    p = build_system_prompt(_settings(assistant_name="NIC AI", assistant_org="NIC Bank"))
    assert "NIC AI" in p
    assert "NIC Bank" in p


def test_prompt_forbids_naming_the_model_and_faking_a_training_story():
    p = build_system_prompt(_settings(assistant_name="NIC AI", assistant_org="NIC Bank"))
    low = p.lower()
    assert "underlying model" in low  # don't disclose the base model
    assert "trained" in low  # don't claim the org trained it
    assert "human" in low  # still an AI, always


def test_blank_org_drops_the_org_clauses():
    p = build_system_prompt(_settings(assistant_name="Helper", assistant_org=""))
    assert "Helper" in p
    # No dangling "an AI assistant for ." / "for  built" fragments.
    assert " for ." not in p
    assert "  " not in p


def test_tool_instructions_survive_the_identity_block():
    """Identity is PREPENDED to the working prompt — it must not replace it."""
    p = build_system_prompt(_settings(assistant_name="NIC AI"))
    assert "aggregate_excel" in p or "tools" in p.lower()
    assert "final answer" in p.lower()


@pytest.mark.anyio
async def test_loop_sends_the_settings_derived_prompt():
    """The wire that breaks silently: the loop must build from THIS turn's
    settings, not a module constant frozen at import."""
    settings = _settings(assistant_name="NIC AI", assistant_org="NIC Bank")
    ollama = RecordingOllama([text_turn("hi")])
    await run_turn(
        messages=[{"role": "user", "content": "who are you"}],
        ollama=ollama,
        mcp=FakeMCP(),
        settings=settings,
    )
    sent = ollama.payloads[0]["messages"]
    assert sent[0]["role"] == "system"
    assert "NIC AI" in sent[0]["content"]
    assert "NIC Bank" in sent[0]["content"]
