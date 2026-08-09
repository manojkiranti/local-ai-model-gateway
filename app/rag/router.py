"""Department administration.

Creating departments and granting access are admin-only. The one route open to
every authenticated caller is `GET /v1/departments`, which returns *that
caller's* departments — granted and active — because it is what the frontend
renders as tabs. A member must not be able to enumerate departments they cannot
use.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_user, require_admin
from ..db.session import get_session
from ..users.models import ROLE_ADMIN, User
from . import repository as repo
from .schemas import (
    DepartmentCreate,
    DepartmentOut,
    DepartmentUpdate,
    GrantCreate,
    MemberOut,
)

router = APIRouter(prefix="/v1/departments", tags=["departments"])


async def _require_department(session: AsyncSession, code: str):
    dept = await repo.get_department_by_code(session, code)
    if dept is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown department"
        )
    return dept


@router.post("", response_model=DepartmentOut, status_code=status.HTTP_201_CREATED)
async def create_department(
    body: DepartmentCreate,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> DepartmentOut:
    try:
        dept = await repo.create_department(session, code=body.code, name=body.name)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Department '{body.code}' already exists",
        )
    return DepartmentOut.model_validate(dept)


@router.get("", response_model=list[DepartmentOut])
async def list_departments(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[DepartmentOut]:
    """Admins see every department; everyone else sees only their own tabs."""
    if user.role == ROLE_ADMIN:
        rows = await repo.list_departments(session)
    else:
        rows = await repo.list_departments_for_user(session, user.id)
    return [DepartmentOut.model_validate(d) for d in rows]


@router.patch("/{code}", response_model=DepartmentOut)
async def update_department(
    code: str,
    body: DepartmentUpdate,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> DepartmentOut:
    dept = await _require_department(session, code)
    if body.name is not None:
        dept.name = body.name
    if body.is_active is not None:
        # Soft-disable is the only retirement path: documents and chat_sessions
        # reference departments with ON DELETE RESTRICT.
        dept.is_active = body.is_active
    await session.commit()
    await session.refresh(dept)
    return DepartmentOut.model_validate(dept)


@router.get("/{code}/members", response_model=list[MemberOut])
async def list_members(
    code: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[MemberOut]:
    dept = await _require_department(session, code)
    rows = await repo.list_department_members(session, dept.id)
    return [MemberOut.model_validate(m) for m in rows]


@router.post("/{code}/members", status_code=status.HTTP_204_NO_CONTENT)
async def grant_member(
    code: str,
    body: GrantCreate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    dept = await _require_department(session, code)
    try:
        await repo.grant_department(
            session, user_id=body.user_id, department_id=dept.id,
            granted_by=admin.id,
        )
        await session.commit()
    except IntegrityError:
        # Unknown user_id -> FK violation.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Unknown user"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{code}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_member(
    code: str,
    user_id: int,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    dept = await _require_department(session, code)
    removed = await repo.revoke_department(
        session, user_id=user_id, department_id=dept.id
    )
    await session.commit()
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such grant"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
