"""Live MCP integration check against the configured MCP server.

This talks to a REAL server (MCP_SERVER_URL from .env), so it SKIPS cleanly when
MCP isn't configured or isn't reachable — it must never fail the offline suite.
The pure filtering logic is covered without a network in test_tool_filter.py.

Run the server (../../node/local-llm-mcp: `npm start`) to exercise this.
"""

import pytest

from app.config import get_settings
from app.mcp.client import MCPClient, MCPUnavailableError
from app.tools.registry import ToolRegistry


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _client() -> MCPClient:
    s = get_settings()
    if not s.mcp_server_url:
        pytest.skip("MCP not configured (MCP_SERVER_URL blank)")
    return MCPClient(
        server_url=s.mcp_server_url,
        auth_token=s.mcp_auth_token,
        tool_mode=s.mcp_tool_mode,
        allowlist=s.tool_allowlist,
        read_prefixes=s.read_prefixes,
        write_keywords=s.write_keywords,
    )


@pytest.mark.anyio
async def test_describe_exposes_read_only_tools():
    mcp = _client()
    try:
        toolset = await mcp.describe()
    except MCPUnavailableError as exc:
        pytest.skip(f"MCP server unreachable: {exc.message}")

    # The demo server ships get_server_time / get_echo / list_examples — all
    # read-like, so read_only mode exposes them and excludes nothing.
    assert "get_server_time" in toolset.exposed_names
    assert "get_echo" in toolset.exposed_names


@pytest.mark.anyio
async def test_registry_dispatches_mcp_tool_roundtrip():
    """The exact path the agent loop uses: load into the registry, then dispatch."""
    mcp = _client()
    registry = ToolRegistry()
    registry.register_local_tools()
    try:
        async with mcp.session() as session:
            await registry.load_mcp_tools(mcp, session)
            assert registry.backend_of("get_echo") == "mcp"
            result = await registry.dispatch("get_echo", {"message": "roundtrip"})
    except MCPUnavailableError as exc:
        pytest.skip(f"MCP server unreachable: {exc.message}")

    assert result == "roundtrip"
