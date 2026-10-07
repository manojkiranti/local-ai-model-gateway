"""Admin routes for per-user MCP tool access (`mcp_policy`).

The local-tool routes' twin (`router.py`), for the remote MCP server's tools:
a none/read/full LEVEL per system, and per-tool overrides that beat it. The
tool list is the MCP server's live one — read with no user identity, because
this screen describes what exists, not what the admin may call.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_admin
from ..db.session import get_session
from ..mcp.client import MCPClient, MCPUnavailableError
from ..users import repository as users_repo
from ..users.models import User
from . import mcp_policy
from . import repository as repo
from .policy import summary
from .schemas import (
    McpAccessResponse,
    McpLevelUpdate,
    McpSystemAccess,
    McpToolAccessItem,
    ToolAccessUpdate,
)

router = APIRouter(prefix="/v1/users", tags=["tool-access"])

_ERRORS = {
    401: {"description": "Missing/invalid JWT."},
    403: {"description": "Caller is not an admin."},
}


async def _known_user(session: AsyncSession, user_id: int) -> User:
    if await users_repo.get_by_id(session, user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown user")


def _known_system(system: str) -> None:
    if system not in mcp_policy.SYSTEM_LABELS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown MCP system: {system}"
        )


async def _live_tools(mcp: MCPClient):
    """(exposed, excluded) from the MCP server, or None when it is unreachable."""
    if not mcp.configured:
        return [], []
    try:
        toolset = await mcp.describe(identity=None)
    except MCPUnavailableError:
        return None
    return toolset.exposed, toolset.excluded


async def _state(request: Request, session: AsyncSession, user_id: int) -> McpAccessResponse:
    mcp: MCPClient = request.app.state.mcp
    level_rows = {r.system: r for r in await repo.mcp_level_rows(session, user_id)}
    tool_rows = {r.tool_name: r for r in await repo.mcp_tool_rows(session, user_id)}
    access = mcp_policy.McpAccess(
        levels={s: r.level for s, r in level_rows.items()},
        overrides={t: r.allowed for t, r in tool_rows.items()},
        read_prefixes=mcp.read_prefixes,
        write_keywords=mcp.write_keywords,
    )

    systems = [
        McpSystemAccess(
            id=system,
            label=label,
            level=mcp_policy.level_for(system, access.levels),
            default_level=mcp_policy.DEFAULT_LEVEL,
            overridden=system in level_rows,
            updated_at=level_rows[system].updated_at if system in level_rows else None,
            updated_by=level_rows[system].updated_by if system in level_rows else None,
        )
        for system, label in mcp_policy.SYSTEM_LABELS.items()
    ]

    live = await _live_tools(mcp)
    tools: list[McpToolAccessItem] = []
    if live is not None:
        exposed, excluded = live
        listed = [(t.name, t.description, True) for t in exposed] + [
            (t.name, "", False) for t in excluded
        ]
        for name, description, server_enabled in listed:
            row = tool_rows.get(name)
            tools.append(
                McpToolAccessItem(
                    name=name,
                    summary=summary(description) if description else "",
                    system=mcp_policy.system_of(name),
                    kind="write" if access.is_write(name) else "read",
                    server_enabled=server_enabled,
                    override=row.allowed if row else None,
                    # The server-wide filter is the ceiling: nothing past it.
                    allowed=server_enabled and access.allows(name),
                    updated_at=row.updated_at if row else None,
                    updated_by=row.updated_by if row else None,
                )
            )
        tools.sort(key=lambda t: (mcp_policy.SYSTEM_ORDER.index(t.system), t.name))

    return McpAccessResponse(
        user_id=user_id,
        tool_mode=mcp.tool_mode,
        mcp_reachable=live is not None,
        systems=systems,
        tools=tools,
    )


@router.get(
    "/{user_id}/mcp-access",
    response_model=McpAccessResponse,
    summary="A user's access level per MCP system and per MCP tool (admin only)",
    responses={**_ERRORS, 404: {"description": "Unknown user."}},
)
async def get_mcp_access(
    request: Request,
    user_id: int,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> McpAccessResponse:
    await _known_user(session, user_id)
    return await _state(request, session, user_id)


@router.put(
    "/{user_id}/mcp-access/systems/{system}",
    response_model=McpAccessResponse,
    summary="Set a user's access level (none/read/full) for one MCP system (admin only)",
    responses={
        **_ERRORS,
        404: {"description": "Unknown user or unknown system."},
        422: {"description": "Missing or invalid `level`, or an unexpected field."},
    },
)
async def set_mcp_level(
    request: Request,
    user_id: int,
    system: str,
    body: McpLevelUpdate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> McpAccessResponse:
    await _known_user(session, user_id)
    _known_system(system)
    # Stored even when it equals the default, like a local-tool override: an
    # explicit decision stays put if the default changes. DELETE resets.
    await repo.set_mcp_level(
        session, user_id=user_id, system=system, level=body.level, updated_by=admin.id
    )
    await session.commit()
    return await _state(request, session, user_id)


@router.delete(
    "/{user_id}/mcp-access/systems/{system}",
    response_model=McpAccessResponse,
    summary="Return one MCP system to the default level for a user (admin only)",
    responses={**_ERRORS, 404: {"description": "Unknown user or unknown system."}},
)
async def reset_mcp_level(
    request: Request,
    user_id: int,
    system: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> McpAccessResponse:
    await _known_user(session, user_id)
    _known_system(system)
    await repo.clear_mcp_level(session, user_id=user_id, system=system)
    await session.commit()
    return await _state(request, session, user_id)


@router.put(
    "/{user_id}/mcp-access/tools/{tool_name}",
    response_model=McpAccessResponse,
    summary="Allow or block one MCP tool for a user, overriding the level (admin only)",
    responses={
        **_ERRORS,
        404: {"description": "Unknown user, or a tool the MCP server does not list."},
        422: {"description": "Missing `allowed`, or an unexpected field."},
        503: {"description": "The MCP server is unreachable, so the tool name cannot be checked."},
    },
)
async def set_mcp_tool(
    request: Request,
    user_id: int,
    tool_name: str,
    body: ToolAccessUpdate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> McpAccessResponse:
    await _known_user(session, user_id)
    live = await _live_tools(request.app.state.mcp)
    if live is None:
        # Refuse rather than store an unchecked name: a typo would sit in the
        # table looking like a decision while governing nothing.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MCP server unreachable; cannot verify the tool name. Try again shortly.",
        )
    exposed, excluded = live
    if tool_name not in {t.name for t in exposed} | {t.name for t in excluded}:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown MCP tool: {tool_name}"
        )
    await repo.set_mcp_tool(
        session, user_id=user_id, tool_name=tool_name, allowed=body.allowed, updated_by=admin.id
    )
    await session.commit()
    return await _state(request, session, user_id)


@router.delete(
    "/{user_id}/mcp-access/tools/{tool_name}",
    response_model=McpAccessResponse,
    summary="Remove one MCP tool's override for a user, so the level decides (admin only)",
    responses={**_ERRORS, 404: {"description": "Unknown user."}},
)
async def reset_mcp_tool(
    request: Request,
    user_id: int,
    tool_name: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> McpAccessResponse:
    await _known_user(session, user_id)
    # No live check: clearing an override for a tool that has since vanished
    # must still be possible.
    await repo.clear_mcp_tool(session, user_id=user_id, tool_name=tool_name)
    await session.commit()
    return await _state(request, session, user_id)
