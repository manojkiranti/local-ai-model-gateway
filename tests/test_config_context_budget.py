"""The context window must be able to afford its own reserve + tool-schema floor.

`budget_for` (app/history/context.py) subtracts `context_reserve_tokens` and
`context_tool_schema_tokens` from `context_window_tokens` and floors the
result at `MIN_HISTORY_BUDGET`. A window too small for the two subtractions
has no visible symptom — every turn just silently gets the floor's worth of
history — so, like `_check_rerank_pool`, this fails at import instead.
"""

import pytest

from app.config import Settings

BASE_ENV = {
    "database_url": "postgresql+asyncpg://u:p@127.0.0.1:5432/db",
    "jwt_secret": "test-secret",
}


def settings_with(**overrides) -> Settings:
    return Settings(**BASE_ENV, **overrides)


def test_a_window_too_small_for_reserve_plus_schema_is_refused_at_import():
    with pytest.raises(ValueError) as exc:
        settings_with(
            context_window_tokens=8000,
            context_reserve_tokens=6000,
            context_tool_schema_tokens=4000,
        )
    message = str(exc.value)
    assert "CONTEXT_WINDOW_TOKENS" in message and "CONTEXT_RESERVE_TOKENS" in message


def test_a_window_exactly_at_the_boundary_is_refused():
    with pytest.raises(ValueError):
        settings_with(
            context_window_tokens=10000,
            context_reserve_tokens=6000,
            context_tool_schema_tokens=4000,
        )


def test_the_shipped_defaults_satisfy_the_invariant():
    settings = settings_with()
    assert (
        settings.context_window_tokens
        > settings.context_reserve_tokens + settings.context_tool_schema_tokens
    )
