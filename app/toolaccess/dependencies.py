"""Resolve the calling user's allowed local tools — the ONE place it is loaded.

One small query per request that runs tools, like `get_mcp_identity`.
"""

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user
from ..db.session import get_session
from ..tools.local import LOCAL_TOOLS
from ..users.models import User
from . import repository as repo
from .mcp_policy import McpAccess
from .policy import allowed_tools


async def get_allowed_local_tools(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> frozenset[str]:
    # user.role is not consulted: an admin follows the same rule as anyone,
    # and grants themselves a default-off tool like any other user.
    overrides = await repo.override_map(session, user.id)
    return allowed_tools((spec.name for spec in LOCAL_TOOLS), overrides)


async def get_mcp_access(
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> McpAccess:
    """The caller's MCP access (levels + per-tool overrides). Like the local
    tools, `user.role` is not consulted: an admin follows the same rule and
    sets their own level like anyone else's."""
    levels, overrides = await repo.mcp_access_maps(session, user.id)
    mcp = request.app.state.mcp
    return McpAccess(
        levels=levels,
        overrides=overrides,
        read_prefixes=mcp.read_prefixes,
        write_keywords=mcp.write_keywords,
    )
