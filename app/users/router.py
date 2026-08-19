"""User endpoints: the caller's own record, plus the admin levers.

`GET /users` gained a `q` filter and `PATCH /users/{id}` exists because an admin
could previously authenticate people but not administer them: finding somebody
meant paging an unfiltered list, and deactivating them was only possible in SQL.

`is_active` is the offboarding switch and the only one with immediate effect —
`get_current_user` re-reads this row on every request, so clearing it invalidates
an already-issued 24h JWT on the holder's next call. Disabling an account in
Active Directory does NOT do that, because AD is consulted at the login boundary
alone. The guards live in `app/users/policy.py`.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user, require_admin
from ..db.session import get_session
from . import repository as repo
from .models import User
from .policy import deactivation_refusal
from .schemas import UserListResponse, UserOut, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut, summary="The authenticated caller")
async def read_me(current: User = Depends(get_current_user)) -> User:
    return current


@router.get(
    "",
    response_model=UserListResponse,
    summary="List or search users (admin only, paginated)",
)
async def list_users(
    q: str | None = Query(
        None,
        max_length=320,
        description=(
            "Case-insensitive substring of the email. LIKE wildcards are treated "
            "as literal characters."
        ),
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> UserListResponse:
    total, rows = await repo.search(session, q=q, limit=limit, offset=offset)
    return UserListResponse(total=total, limit=limit, offset=offset, items=rows)


@router.patch(
    "/{user_id}",
    response_model=UserOut,
    summary="Activate or deactivate a user (admin only)",
)
async def update_user(
    user_id: int,
    body: UserUpdate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> User:
    user = await repo.get_by_id(session, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown user"
        )

    if not body.is_active:
        # Reactivation is always safe; only switching OFF can lock people out.
        refusal = deactivation_refusal(
            target_id=user.id,
            target_role=user.role,
            target_is_active=user.is_active,
            caller_id=admin.id,
            active_admin_count=await repo.count_active_admins(session),
        )
        if refusal is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=refusal)

    user.is_active = body.is_active
    await session.commit()
    await session.refresh(user)
    return user
