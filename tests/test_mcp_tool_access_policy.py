"""Pure tests for per-user MCP tool access (app/toolaccess/mcp_policy.py) and
its enforcement point, ToolRegistry.load_mcp_tools. No database, no MCP server."""

import asyncio
from types import SimpleNamespace

import pytest

from app.mcp.client import ExposedTool, ToolSet
from app.toolaccess import mcp_policy as p
from app.tools.registry import ToolRegistry

READ = ("get", "list", "search", "read", "fetch", "find", "view")
WRITE = ("create", "update", "delete", "send", "remove", "approve")


def _access(levels=None, overrides=None):
    return p.McpAccess(
        levels=levels or {}, overrides=overrides or {}, read_prefixes=READ, write_keywords=WRITE
    )


@pytest.mark.parametrize(
    "tool,system",
    [
        ("get_hrms_employee_remaining_leave", "hrms"),
        ("list_izone_list_items", "izone"),  # "items" contains "ems" — tokens, not substrings
        ("search_ems_records", "ems"),
        ("get_server_time", "other"),
        ("getHrmsEmployee", "hrms"),  # camelCase tokenizes too
    ],
)
def test_system_of_uses_name_tokens(tool, system):
    assert p.system_of(tool) == system


def test_is_write_matches_the_server_wide_classifier_inverted():
    assert not p.is_write("list_hrms_employees", read_prefixes=READ, write_keywords=WRITE)
    assert p.is_write("create_ems_expense", read_prefixes=READ, write_keywords=WRITE)
    # A read word does not launder a write word...
    assert p.is_write("get_and_delete_record", read_prefixes=READ, write_keywords=WRITE)
    # ...and a tool with no read word at all needs `full`.
    assert p.is_write("hrms_sync", read_prefixes=READ, write_keywords=WRITE)


def test_default_level_is_read_so_existing_users_lose_nothing():
    a = _access()
    assert p.DEFAULT_LEVEL == "read"
    assert a.allows("list_hrms_employees")
    assert not a.allows("create_ems_expense")


@pytest.mark.parametrize(
    "level,read_ok,write_ok", [("none", False, False), ("read", True, False), ("full", True, True)]
)
def test_levels(level, read_ok, write_ok):
    a = _access(levels={"ems": level})
    assert a.allows("search_ems_records") is read_ok
    assert a.allows("create_ems_expense") is write_ok
    # Another system keeps the default.
    assert a.allows("list_hrms_employees")


def test_a_per_tool_override_beats_the_level_both_ways():
    a = _access(
        levels={"hrms": "none", "izone": "full"},
        overrides={"get_hrms_employee_remaining_leave": True, "list_izone_documents": False},
    )
    assert a.allows("get_hrms_employee_remaining_leave")  # granted despite `none`
    assert not a.allows("list_hrms_employees")  # the rest of HRMS stays hidden
    assert not a.allows("list_izone_documents")  # blocked despite `full`
    assert a.allows("list_izone_lists")


def test_an_unknown_stored_level_fails_closed():
    assert p.level_for("hrms", {"hrms": "admin"}) == "none"


# --- enforcement: the registry --------------------------------------------------------


class _FakeMcp:
    def __init__(self, names):
        self._names = names

    async def load_toolset(self, session):
        return ToolSet(
            exposed=[
                ExposedTool(name=n, description="", ollama_schema={"type": "function", "function": {"name": n}})
                for n in self._names
            ],
            excluded=[],
        )


def _load(access):
    names = ["list_hrms_employees", "get_hrms_employee_remaining_leave", "search_ems_records", "get_echo"]
    registry = ToolRegistry()
    asyncio.run(registry.load_mcp_tools(_FakeMcp(names), session=object(), access=access))
    return registry


def test_registry_without_access_keeps_every_exposed_tool():
    assert len(_load(None).tool_names()) == 4


def test_registry_drops_tools_the_user_may_not_use():
    registry = _load(_access(levels={"hrms": "none"}, overrides={"search_ems_records": False}))
    assert registry.tool_names() == ["get_echo"]
    # Not offered to the model...
    offered = [t["function"]["name"] for t in registry.list_ollama_tools()]
    assert "list_hrms_employees" not in offered
    # ...and not dispatchable either.
    assert not registry.has_tool("list_hrms_employees")
