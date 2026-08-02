"""Offline tests for the MCP client's connection handling.

These need NO live server: they point at a dead port and assert the client
surfaces unreachability as a clean MCPUnavailableError (which the router turns
into 502 and the agent loop catches), rather than leaking a CancelledError from
the streamable-HTTP transport's internal cancel scope.
"""

import pytest

from app.mcp.client import MCPClient, MCPUnavailableError

# A port nothing should be listening on -> connection refused.
DEAD_URL = "http://127.0.0.1:59999/mcp"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _client(url: str) -> MCPClient:
    return MCPClient(
        server_url=url,
        auth_token="",
        tool_mode="read_only",
        allowlist=[],
        read_prefixes=["get"],
        write_keywords=[],
    )


def test_session_headers_forward_auth_and_user_email():
    mcp = MCPClient(
        server_url="http://mcp.example/mcp",
        auth_token="tok",
        tool_mode="read_only",
        allowlist=[],
        read_prefixes=["get"],
        write_keywords=[],
    )
    with_email = mcp._session_headers("alice@example.com")
    assert with_email["Authorization"] == "Bearer tok"
    assert with_email["x-user-email"] == "alice@example.com"
    # No email -> no header (server treats userEmail as null).
    assert "x-user-email" not in mcp._session_headers(None)


@pytest.mark.anyio
async def test_unreachable_server_raises_mcp_unavailable():
    mcp = _client(DEAD_URL)
    with pytest.raises(MCPUnavailableError):
        async with mcp.session() as session:
            await session.list_tools()


@pytest.mark.anyio
async def test_describe_on_unreachable_server_raises_mcp_unavailable():
    mcp = _client(DEAD_URL)
    with pytest.raises(MCPUnavailableError):
        await mcp.describe()
