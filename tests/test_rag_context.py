"""The department contextvar. Pure — no DB, no app.

This is the mechanism that keeps `department` out of the tool schema: the tool
reads it from the context, so a prompt injection has nothing to target.
"""

import asyncio
import dataclasses

import pytest

from app.rag.context import DepartmentContext, current_department, rag_context

HR = DepartmentContext(id=1, code="hr")
FIN = DepartmentContext(id=2, code="finance")


def test_no_context_by_default():
    assert current_department() is None


def test_context_is_visible_inside_the_block():
    with rag_context(HR):
        assert current_department() == HR


def test_context_is_cleared_on_exit():
    with rag_context(HR):
        pass
    assert current_department() is None


def test_context_is_cleared_even_when_the_block_raises():
    try:
        with rag_context(HR):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert current_department() is None


def test_nested_contexts_restore_the_outer_value():
    with rag_context(HR):
        with rag_context(FIN):
            assert current_department() == FIN
        assert current_department() == HR


def test_context_does_not_leak_between_concurrent_tasks():
    """Two turns in one process must not see each other's department."""
    seen = {}

    async def turn(ctx, key):
        with rag_context(ctx):
            await asyncio.sleep(0)  # force interleaving
            seen[key] = current_department()

    async def main():
        await asyncio.gather(turn(HR, "a"), turn(FIN, "b"))

    asyncio.run(main())
    assert seen == {"a": HR, "b": FIN}


def test_department_context_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        HR.id = 99
