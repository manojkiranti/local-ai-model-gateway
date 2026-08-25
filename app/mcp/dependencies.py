"""Resolve the calling user's MCP identity — the ONE place grants are loaded.

Costs one small query per request that touches MCP. The precedent is
`resolve_department`, which folds its grant check into an existing query and
measures 0.518 ms against a multi-second turn; `get_current_user` already reads
the user row on every request, so the request is DB-bound regardless.
"""

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user
from ..db.session import get_session
from ..users.models import User
from . import repository as repo
from .grants import McpIdentity


async def get_mcp_identity(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> McpIdentity:
    keys = await repo.grant_keys_for(session, user.id)
    # NOTE: user.role is not consulted. A global admin holds no grant
    # implicitly (design §3.4) and must grant themselves explicitly.
    return McpIdentity.from_grants(email=user.email, grant_keys=keys)
