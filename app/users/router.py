"""User endpoints: /users/me (any authed user) and /users (admin-only)."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user, require_admin
from ..db.session import get_session
from .models import User
from .schemas import UserListResponse, UserOut

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut, summary="The current authenticated user")
async def read_me(current: User = Depends(get_current_user)) -> User:
    return current


@router.get(
    "",
    response_model=UserListResponse,
    summary="List users (admin only, paginated)",
)
async def list_users(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> UserListResponse:
    total = (await session.execute(select(func.count()).select_from(User))).scalar_one()
    rows = (
        await session.execute(
            select(User).order_by(User.id).limit(limit).offset(offset)
        )
    ).scalars().all()
    return UserListResponse(total=total, limit=limit, offset=offset, items=list(rows))
