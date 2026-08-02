"""Tests for the MCP tool-exposure filter — the safety gate deciding which
tools ever reach the model. No network required."""

from dataclasses import dataclass

from app.mcp.client import MCPClient, tokenize

READ_PREFIXES = ["get", "list", "search", "read", "fetch", "find", "view"]
WRITE_KEYWORDS = [
    "create", "update", "delete", "send", "remove",
    "archive", "associate", "advance", "propose", "apply", "register",
]


@dataclass
class FakeTool:
    name: str
    description: str = "desc"
    input_schema: dict | None = None


def _client(mode="read_only", allowlist=None):
    return MCPClient(
        server_url="http://mcp.example",
        auth_token="",
        tool_mode=mode,
        allowlist=allowlist or [],
        read_prefixes=READ_PREFIXES,
        write_keywords=WRITE_KEYWORDS,
    )


def test_tokenize_handles_camel_and_separators():
    assert tokenize("getUserById") == ["get", "user", "by", "id"]
    assert tokenize("hubspot-create-deal") == ["hubspot", "create", "deal"]


def test_read_only_exposes_reads_and_excludes_writes():
    tools = [
        FakeTool("get_contact"), FakeTool("list_deals"), FakeTool("searchRecords"),
        FakeTool("create_contact"), FakeTool("delete_deal"), FakeTool("send_email"),
    ]
    ts = _client("read_only").filter_tools(tools)
    assert ts.exposed_names == {"get_contact", "list_deals", "searchRecords"}


def test_read_only_excludes_ambiguous():
    ts = _client("read_only").filter_tools([FakeTool("ping"), FakeTool("get_and_delete")])
    assert ts.exposed_names == set()


def test_allowlist_mode():
    tools = [FakeTool("get_contact"), FakeTool("delete_deal"), FakeTool("run_report")]
    ts = _client("allowlist", allowlist=["delete_deal", "run_report"]).filter_tools(tools)
    assert ts.exposed_names == {"delete_deal", "run_report"}


def test_all_mode_exposes_everything():
    ts = _client("all").filter_tools([FakeTool("get_contact"), FakeTool("delete_deal")])
    assert ts.exposed_names == {"get_contact", "delete_deal"}
