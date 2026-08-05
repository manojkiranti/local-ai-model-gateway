"""Offline tests for GET /v1/mcp/status — the UI's MCP connection badge.

No DB / no network: auth is overridden and app.state.mcp is a fake. The
endpoint must ALWAYS return 200, encoding MCP health as data (reachable/error/
tools) so the frontend can render a status dot without having to handle a 502.
"""

from starlette.testclient import TestClient

from app.auth.dependencies import get_current_user
from app.main import app
from app.mcp.client import ExposedTool, MCPUnavailableError, ToolSet


class _User:
    email = "tester@example.com"


class FakeMCP:
    """Stand-in for MCPClient with just what the status endpoint touches."""

    def __init__(self, *, configured=True, reachable=True, tools=None, error="down"):
        self.configured = configured
        self.server_url = "http://localhost:3333/mcp" if configured else ""
        self.tool_mode = "read_only"
        self._reachable = reachable
        self._tools = tools or []
        self._error = error

    async def describe(self, *, user_email=None):
        if not self._reachable:
            raise MCPUnavailableError(self._error)
        return ToolSet(
            exposed=[
                ExposedTool(name=n, description="", ollama_schema={}) for n in self._tools
            ],
            excluded=[],
        )


def _client(mcp: FakeMCP) -> TestClient:
    app.dependency_overrides[get_current_user] = lambda: _User()
    app.state.mcp = mcp
    return TestClient(app)


def teardown_function():
    app.dependency_overrides.clear()


def test_status_requires_auth():
    with TestClient(app) as client:
        assert client.get("/v1/mcp/status").status_code in (401, 403)


def test_status_connected_lists_tools():
    client = _client(FakeMCP(reachable=True, tools=["get_server_time", "list_hrms_employees"]))
    r = client.get("/v1/mcp/status")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["reachable"] is True
    assert body["server_url"] == "http://localhost:3333/mcp"
    assert body["tool_mode"] == "read_only"
    assert body["tools"] == ["get_server_time", "list_hrms_employees"]
    assert body["error"] is None


def test_status_unreachable_is_200_not_502():
    client = _client(FakeMCP(reachable=False, error="Cannot reach MCP server at http://localhost:3333/mcp"))
    r = client.get("/v1/mcp/status")
    assert r.status_code == 200  # health is DATA here, never an error status
    body = r.json()
    assert body["configured"] is True
    assert body["reachable"] is False
    assert body["tools"] == []
    assert "Cannot reach" in body["error"]


def test_status_not_configured():
    client = _client(FakeMCP(configured=False))
    r = client.get("/v1/mcp/status")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert body["reachable"] is False
    assert body["server_url"] is None
    assert body["tools"] == []
    assert body["error"] is None
