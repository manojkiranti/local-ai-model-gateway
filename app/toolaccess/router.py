"""Admin routes for per-user local tool access.

Its own routes, not a field on `PATCH /users/{id}`, for the same reason the
MCP grants have theirs: widening what a user can make the gateway do is an
escalation surface that wants its own validation and an audit stamp.
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_admin
from ..db.session import get_session
from ..tools.local import LOCAL_TOOLS
from ..users import repository as users_repo
from ..users.models import User
from . import policy
from . import repository as repo
from .schemas import ToolAccessGroup, ToolAccessItem, ToolAccessResponse, ToolAccessUpdate

router = APIRouter(prefix="/v1/users", tags=["tool-access"])

_ERRORS = {
    401: {"description": "Missing/invalid JWT."},
    403: {"description": "Caller is not an admin."},
}


async def _known_user(session: AsyncSession, user_id: int) -> User:
    user = await users_repo.get_by_id(session, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown user")
    return user


def _known_tool(tool_name: str) -> None:
    if tool_name not in {spec.name for spec in LOCAL_TOOLS}:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown tool: {tool_name}"
        )


async def _state(session: AsyncSession, user_id: int) -> ToolAccessResponse:
    rows = {row.tool_name: row for row in await repo.list_overrides(session, user_id)}
    items = []
    for spec in LOCAL_TOOLS:
        row = rows.get(spec.name)
        override = row.allowed if row else None
        default = policy.default_allowed(spec.name)
        items.append(
            ToolAccessItem(
                name=spec.name,
                summary=policy.summary(spec.description),
                group=policy.group_of(spec.name),
                default_allowed=default,
                override=override,
                allowed=default if override is None else override,
                updated_at=row.updated_at if row else None,
                updated_by=row.updated_by if row else None,
            )
        )
    items.sort(key=lambda i: (policy.GROUP_ORDER.index(i.group), i.name))
    return ToolAccessResponse(user_id=user_id, groups=_groups(items), items=items)


def _same(values: list[bool]) -> bool | None:
    return values[0] if values and all(v == values[0] for v in values) else None


def _groups(items: list[ToolAccessItem]) -> list[ToolAccessGroup]:
    out = []
    for group_id in policy.GROUP_ORDER:
        members = [i for i in items if i.group == group_id]
        if not members:
            continue
        overrides = [i.override for i in members]
        if all(o is None for o in overrides):
            state = "default"
        elif all(o is True for o in overrides):
            state = "granted"
        elif all(o is False for o in overrides):
            state = "blocked"
        else:
            state = "mixed"
        latest = max(
            (i for i in members if i.updated_at is not None),
            key=lambda i: i.updated_at,
            default=None,
        )
        out.append(
            ToolAccessGroup(
                id=group_id,
                label=policy.GROUP_LABELS[group_id],
                tools=[i.name for i in members],
                allowed=_same([i.allowed for i in members]),
                default_allowed=_same([i.default_allowed for i in members]),
                state=state,
                updated_at=latest.updated_at if latest else None,
                updated_by=latest.updated_by if latest else None,
            )
        )
    return out


def _known_group(group_id: str) -> list[str]:
    tools = policy.tools_in_group(group_id, (spec.name for spec in LOCAL_TOOLS))
    if group_id not in policy.GROUP_LABELS or not tools:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown tool group: {group_id}"
        )
    return tools


@router.get(
    "/{user_id}/tool-access",
    response_model=ToolAccessResponse,
    summary="List a user's access to each local tool (admin only)",
    responses={**_ERRORS, 404: {"description": "Unknown user."}},
)
async def get_tool_access(
    user_id: int,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ToolAccessResponse:
    await _known_user(session, user_id)
    return await _state(session, user_id)


@router.put(
    "/{user_id}/tool-access/{tool_name}",
    response_model=ToolAccessResponse,
    summary="Allow or block one local tool for a user (admin only)",
    responses={
        **_ERRORS,
        404: {"description": "Unknown user or unknown tool."},
        422: {"description": "Missing `allowed`, or an unexpected field."},
    },
)
async def set_tool_access(
    user_id: int,
    tool_name: str,
    body: ToolAccessUpdate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ToolAccessResponse:
    await _known_user(session, user_id)
    _known_tool(tool_name)
    # Stored even when it equals the default: an explicit decision stays put if
    # the starter set changes later. DELETE is how an admin returns to default.
    await repo.set_override(
        session, user_id=user_id, tool_name=tool_name, allowed=body.allowed, updated_by=admin.id
    )
    await session.commit()
    return await _state(session, user_id)


@router.delete(
    "/{user_id}/tool-access/{tool_name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Return one local tool to its default for a user (admin only)",
    responses={**_ERRORS, 404: {"description": "Unknown user."}},
)
async def reset_tool_access(
    user_id: int,
    tool_name: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await _known_user(session, user_id)
    # 204 whether or not an override existed, like an MCP revoke: the caller's
    # intent (no override) is satisfied either way.
    await repo.clear_override(session, user_id=user_id, tool_name=tool_name)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put(
    "/{user_id}/tool-access/groups/{group_id}",
    response_model=ToolAccessResponse,
    summary="Allow or block a whole tool group for a user (admin only)",
    responses={
        **_ERRORS,
        404: {"description": "Unknown user or unknown group."},
        422: {"description": "Missing `allowed`, or an unexpected field."},
    },
)
async def set_group_access(
    user_id: int,
    group_id: str,
    body: ToolAccessUpdate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ToolAccessResponse:
    await _known_user(session, user_id)
    tools = _known_group(group_id)
    # Every tool in the group, one transaction: never left half-switched.
    await repo.set_group(
        session, user_id=user_id, tools=tools, allowed=body.allowed, updated_by=admin.id
    )
    await session.commit()
    return await _state(session, user_id)


@router.delete(
    "/{user_id}/tool-access/groups/{group_id}",
    response_model=ToolAccessResponse,
    summary="Return a whole tool group to its default for a user (admin only)",
    responses={**_ERRORS, 404: {"description": "Unknown user or unknown group."}},
)
async def reset_group_access(
    user_id: int,
    group_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ToolAccessResponse:
    await _known_user(session, user_id)
    tools = _known_group(group_id)
    await repo.clear_group(session, user_id=user_id, tools=tools)
    await session.commit()
    # 200 with the list (unlike the per-tool 204), so the screen needs no re-read.
    return await _state(session, user_id)
