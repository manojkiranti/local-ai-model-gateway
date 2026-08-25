"""GET /v1/mcp/status — MCP connection status for the frontend's badge (authed).

Unlike GET /v1/tools (which 502s when MCP is configured-but-down), this endpoint
ALWAYS returns 200 and encodes health as data: `reachable`, `tools`, and an
`error` string. That lets the UI render a status dot (🟢/🔴/⚪) from a single
field instead of branching on an HTTP error. It reuses the same on-demand probe
+ describe the tools list uses, so "connected" means "reachable and authenticated
right now" (MCP is stateless streamable HTTP — there's no persistent socket).
"""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ..auth.dependencies import get_current_user
from ..users.models import User
from .client import MCPClient, MCPUnavailableError
from .dependencies import get_mcp_identity
from .grants import McpIdentity

router = APIRouter(prefix="/v1", tags=["mcp"])


class McpStatusResponse(BaseModel):
    configured: bool  # is MCP_SERVER_URL set at all?
    reachable: bool  # did the gateway just reach + auth to it?
    server_url: str | None
    tool_mode: str | None
    tools: list[str]  # exposed (post-filter) tool names, for a tooltip
    error: str | None  # human-readable reason when unreachable


@router.get(
    "/mcp/status",
    response_model=McpStatusResponse,
    summary="MCP connection status (always 200; health is in the body)",
    responses={401: {"description": "Missing/invalid JWT."}},
)
async def mcp_status(
    request: Request,
    user: User = Depends(get_current_user),
    identity: McpIdentity = Depends(get_mcp_identity),
) -> McpStatusResponse:
    mcp: MCPClient = request.app.state.mcp

    if not mcp.configured:
        return McpStatusResponse(
            configured=False,
            reachable=False,
            server_url=None,
            tool_mode=None,
            tools=[],
            error=None,
        )

    try:
        toolset = await mcp.describe(identity=identity)
    except MCPUnavailableError as exc:
        return McpStatusResponse(
            configured=True,
            reachable=False,
            server_url=mcp.server_url,
            tool_mode=mcp.tool_mode,
            tools=[],
            error=exc.message,
        )

    return McpStatusResponse(
        configured=True,
        reachable=True,
        server_url=mcp.server_url,
        tool_mode=mcp.tool_mode,
        tools=[t.name for t in toolset.exposed],
        error=None,
    )
