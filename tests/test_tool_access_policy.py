"""Pure tests for per-user local tool access — no database, no HTTP."""

from __future__ import annotations

from app.tools.local import LOCAL_TOOLS
from app.tools.registry import ToolRegistry
from app.toolaccess import policy

NAMES = [spec.name for spec in LOCAL_TOOLS]


def test_the_starter_set_is_everything_but_the_external_tools():
    assert policy.allowed_tools(NAMES, {}) == frozenset(NAMES) - {"fetch_url", "get_nrb_forex"}


def test_department_search_is_in_the_starter_set():
    """Off by default would silence every department chat until an admin acted."""
    assert policy.default_allowed("search_department_docs")
    assert policy.default_allowed("read_department_doc")


def test_an_override_wins_in_either_direction():
    overrides = {"create_pptx": False, "fetch_url": True}
    allowed = policy.allowed_tools(NAMES, overrides)
    assert "create_pptx" not in allowed
    assert "fetch_url" in allowed
    assert "get_nrb_forex" not in allowed  # untouched default-off stays off


def test_every_local_tool_has_a_group():
    """A new tool must be placed on the admin screen deliberately."""
    missing = [name for name in NAMES if name not in policy.GROUPS]
    assert missing == []


def test_summary_is_the_first_sentence():
    assert policy.summary("Create a deck. Use it when asked.") == "Create a deck."
    assert policy.summary("No full stop") == "No full stop"


def test_the_registry_only_holds_the_allowed_local_tools():
    registry = ToolRegistry()
    registry.register_local_tools(allowed=frozenset({"calculator"}))
    assert registry.tool_names() == ["calculator"]
    assert not registry.has_tool("create_pptx")


def test_the_registry_holds_every_local_tool_without_a_limit():
    registry = ToolRegistry()
    registry.register_local_tools()
    assert sorted(registry.tool_names()) == sorted(NAMES)


def test_every_group_has_a_label_and_the_external_group_is_the_default_off_set():
    assert set(policy.GROUPS.values()) <= set(policy.GROUP_LABELS)
    assert set(policy.tools_in_group(policy.GROUP_EXTERNAL, NAMES)) == policy.DEFAULT_OFF
