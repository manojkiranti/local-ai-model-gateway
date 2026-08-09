"""Data-access for departments and department grants.

Same convention as `history/repository.py` and `files/repository.py`: every
function takes an AsyncSession and DOES NOT commit — the router owns the
transaction boundary.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Department, UserDepartment


async def create_department(
    session: AsyncSession, *, code: str, name: str
) -> Department:
    """Insert a department. `code` is unique; a duplicate raises IntegrityError
    and the router turns that into 409."""
    dept = Department(code=code, name=name)
    session.add(dept)
    await session.flush()
    return dept


async def get_department_by_code(
    session: AsyncSession, code: str
) -> Department | None:
    return (
        await session.execute(select(Department).where(Department.code == code))
    ).scalar_one_or_none()


async def get_department_by_id(
    session: AsyncSession, department_id: int
) -> Department | None:
    """Used when a bound session supplies no code: the department is read from
    `chat_sessions.department_id`, the server-side source of truth."""
    return (
        await session.execute(select(Department).where(Department.id == department_id))
    ).scalar_one_or_none()


async def list_departments(session: AsyncSession) -> list[Department]:
    """Every department, active or not — the admin view."""
    result = await session.execute(select(Department).order_by(Department.code))
    return list(result.scalars())


async def list_departments_for_user(
    session: AsyncSession, user_id: int
) -> list[Department]:
    """The departments this user may query: granted AND active.

    This is what the frontend renders as tabs. Inactive departments disappear
    from the UI without any grant being revoked, which is the point of
    soft-disable — departments can never be deleted (ON DELETE RESTRICT).
    """
    result = await session.execute(
        select(Department)
        .join(UserDepartment, UserDepartment.department_id == Department.id)
        .where(UserDepartment.user_id == user_id, Department.is_active.is_(True))
        .order_by(Department.code)
    )
    return list(result.scalars())


async def set_department_active(
    session: AsyncSession, *, code: str, is_active: bool
) -> Department | None:
    """Soft-enable/disable. Returns None if the code is unknown."""
    dept = await get_department_by_code(session, code)
    if dept is None:
        return None
    dept.is_active = is_active
    await session.flush()
    return dept


async def grant_department(
    session: AsyncSession,
    *,
    user_id: int,
    department_id: int,
    granted_by: int | None,
) -> None:
    """Grant access. Idempotent: re-granting is a no-op rather than a PK error,
    so an admin clicking twice does not produce a 500."""
    stmt = (
        pg_insert(UserDepartment)
        .values(user_id=user_id, department_id=department_id, granted_by=granted_by)
        .on_conflict_do_nothing(index_elements=["user_id", "department_id"])
    )
    await session.execute(stmt)


async def revoke_department(
    session: AsyncSession, *, user_id: int, department_id: int
) -> bool:
    """Remove access. Returns True if a grant was actually removed."""
    result = await session.execute(
        delete(UserDepartment).where(
            UserDepartment.user_id == user_id,
            UserDepartment.department_id == department_id,
        )
    )
    return result.rowcount > 0


async def has_department_access(
    session: AsyncSession, *, user_id: int, department_id: int
) -> bool:
    """The permission check for a non-admin. Absence of a row = no access."""
    found = (
        await session.execute(
            select(UserDepartment.user_id).where(
                UserDepartment.user_id == user_id,
                UserDepartment.department_id == department_id,
            )
        )
    ).first()
    return found is not None


async def list_department_members(
    session: AsyncSession, department_id: int
) -> list[UserDepartment]:
    result = await session.execute(
        select(UserDepartment)
        .where(UserDepartment.department_id == department_id)
        .order_by(UserDepartment.user_id)
    )
    return list(result.scalars())
