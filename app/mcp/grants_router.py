"""Admin routes for per-user MCP grants.

Deliberately NOT folded into `PATCH /users/{id}`, which already refuses `role`
with `extra="forbid"` because promotion is an escalation surface wanting its own
guards. Granting somebody a SQL console over the expenses database is the same
kind of surface, so it gets the same treatment: its own route, its own
validation, and `granted_by` written on every insert.
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import require_admin
from ..db.session import get_session
from ..users import repository as users_repo
from ..users.models import User
from . import repository as repo
from .schemas import GrantCreate, GrantListResponse, GrantOut

router = APIRouter(prefix="/v1/users", tags=["mcp-grants"])


async def _known_user(session: AsyncSession, user_id: int) -> User:
    user = await users_repo.get_by_id(session, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown user"
        )
    return user


@router.get(
    "/{user_id}/mcp-grants",
    response_model=GrantListResponse,
    summary="List a user's MCP tool grants (admin only)",
    responses={
        401: {"description": "Missing/invalid JWT."},
        403: {"description": "Caller is not an admin."},
        404: {"description": "Unknown user."},
    },
)
async def list_user_grants(
    user_id: int,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> GrantListResponse:
    await _known_user(session, user_id)
    rows = await repo.list_grants(session, user_id)
    return GrantListResponse(
        user_id=user_id, items=[GrantOut.model_validate(row) for row in rows]
    )


@router.post(
    "/{user_id}/mcp-grants",
    response_model=GrantListResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Grant an MCP role or permission (admin only)",
    responses={
        401: {"description": "Missing/invalid JWT."},
        403: {"description": "Caller is not an admin."},
        404: {"description": "Unknown user."},
        422: {"description": "Unknown grant key, or an unexpected field."},
    },
)
async def add_user_grant(
    user_id: int,
    body: GrantCreate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> GrantListResponse:
    await _known_user(session, user_id)
    # Idempotent: re-granting is a 201 with the same list and an untouched
    # granted_at, not a 409. The caller's intent is already satisfied.
    await repo.grant(
        session, user_id=user_id, grant_key=body.grant_key, granted_by=admin.id
    )
    await session.commit()
    rows = await repo.list_grants(session, user_id)
    return GrantListResponse(
        user_id=user_id, items=[GrantOut.model_validate(row) for row in rows]
    )


@router.delete(
    "/{user_id}/mcp-grants/{grant_key}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an MCP role or permission (admin only)",
    responses={
        401: {"description": "Missing/invalid JWT."},
        403: {"description": "Caller is not an admin."},
        404: {"description": "Unknown user."},
    },
)
async def remove_user_grant(
    user_id: int,
    grant_key: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await _known_user(session, user_id)
    # 204 whether or not a row existed: revocation is idempotent, and a 404
    # here would leak which grants a user holds to a caller who may list them
    # anyway — noise without a boundary.
    await repo.revoke(session, user_id=user_id, grant_key=grant_key)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
