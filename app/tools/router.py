"""GET /v1/tools — the merged, filtered tool list the model can see (authed)."""

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth.dependencies import get_current_user
from ..mcp.client import MCPClient, MCPUnavailableError
from ..mcp.dependencies import get_mcp_identity
from ..mcp.grants import McpIdentity
from ..toolaccess.dependencies import get_allowed_local_tools, get_mcp_access
from ..toolaccess.mcp_policy import McpAccess
from ..users.models import User
from .registry import LOCAL_TOOLS
from .schemas import ExposedToolInfo, FilteredToolInfo, ToolsResponse

router = APIRouter(prefix="/v1", tags=["tools"])


@router.get(
    "/tools",
    response_model=ToolsResponse,
    summary="List the merged, filtered tools exposed to the model",
    responses={
        401: {"description": "Missing/invalid JWT."},
        502: {"description": "MCP server configured but unreachable."},
    },
)
async def list_tools(
    request: Request,
    user: User = Depends(get_current_user),
    identity: McpIdentity = Depends(get_mcp_identity),
    local_tools: frozenset[str] = Depends(get_allowed_local_tools),
    mcp_access: McpAccess = Depends(get_mcp_access),
) -> ToolsResponse:
    mcp: MCPClient = request.app.state.mcp

    # Local tools: the caller's allowed set (per-user access, app/toolaccess).
    exposed = [
        ExposedToolInfo(name=spec.name, description=spec.description, backend="local")
        for spec in LOCAL_TOOLS
        if spec.name in local_tools
    ]
    filtered_out: list[FilteredToolInfo] = [
        FilteredToolInfo(name=spec.name, reason="not permitted for this user")
        for spec in LOCAL_TOOLS
        if spec.name not in local_tools
    ]

    if mcp.configured:
        try:
            toolset = await mcp.describe(identity=identity)
        except MCPUnavailableError as exc:
            raise HTTPException(status_code=502, detail=exc.message) from exc
        # The caller's per-user MCP access (app/toolaccess/mcp_policy.py),
        # applied on top of the server-wide filter — the same rule the chat
        # turn's registry applies.
        exposed.extend(
            ExposedToolInfo(name=t.name, description=t.description, backend="mcp")
            for t in toolset.exposed
            if mcp_access.allows(t.name)
        )
        filtered_out.extend(
            FilteredToolInfo(name=t.name, reason="not permitted for this user")
            for t in toolset.exposed
            if not mcp_access.allows(t.name)
        )
        filtered_out.extend(
            FilteredToolInfo(name=t.name, reason=t.reason) for t in toolset.excluded
        )

    return ToolsResponse(
        mode=mcp.tool_mode,
        server_url=mcp.server_url,
        exposed=exposed,
        filtered_out=filtered_out,
    )
