"""Data-access for departments and department grants.

Same convention as `history/repository.py` and `files/repository.py`: every
function takes an AsyncSession and DOES NOT commit — the router owns the
transaction boundary.
"""

from __future__ import annotations

from sqlalchemy import Row, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..users.models import User
from .models import Department, UserDepartment
from .permissions import LEVEL_VIEWER


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
) -> list[Row]:
    """The departments this user may query: granted AND active.

    This is what the frontend renders as tabs. Inactive departments disappear
    from the UI without any grant being revoked, which is the point of
    soft-disable — departments can never be deleted (ON DELETE RESTRICT).

    Each row is `(Department, role)`: the level rides along so the tab list can
    say what the user may DO in each department without a second query per tab.
    """
    result = await session.execute(
        select(Department, UserDepartment.role)
        .join(UserDepartment, UserDepartment.department_id == Department.id)
        .where(UserDepartment.user_id == user_id, Department.is_active.is_(True))
        .order_by(Department.code)
    )
    return list(result.all())


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
    role: str = LEVEL_VIEWER,
) -> None:
    """Grant access at `role`, or change an existing grant's level.

    `on_conflict_do_UPDATE`, not `do_nothing`: this is also the promote/demote
    path, and a no-op would report success while leaving the old level in place —
    the worst possible outcome for a permission change. Still idempotent, so an
    admin clicking twice does not produce a 500.

    `granted_by`/`granted_at` are refreshed along with the level, so the row
    answers "who put them at THIS level, and when" rather than "who first let them
    in". That is the fact an audit of a privilege change actually wants.
    """
    stmt = pg_insert(UserDepartment).values(
        user_id=user_id,
        department_id=department_id,
        granted_by=granted_by,
        role=role,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "department_id"],
        set_={
            "role": stmt.excluded.role,
            "granted_by": stmt.excluded.granted_by,
            "granted_at": func.now(),
        },
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


async def get_department_level(
    session: AsyncSession, *, user_id: int, department_id: int
) -> str | None:
    """This user's level in this department, or None if they hold no grant.

    Replaces `has_department_access`: a non-None answer IS the access check, so the
    boundary and the level cost the same single primary-key lookup that slice 3
    measured at 0.518 ms. `access.resolve_department` calls this on EVERY chat
    turn — never widen it into a join.
    """
    found = (
        await session.execute(
            select(UserDepartment.role).where(
                UserDepartment.user_id == user_id,
                UserDepartment.department_id == department_id,
            )
        )
    ).first()
    return None if found is None else found[0]


async def list_department_members(
    session: AsyncSession, department_id: int
) -> list[Row]:
    """Members with their level and email.

    The email is here because `GET /users` is global-admin-only: a department owner
    managing members would otherwise see bare integers with no way to resolve them.
    Rows carry attribute access, so the router validates them straight into
    `MemberOut`.
    """
    result = await session.execute(
        select(
            UserDepartment.user_id,
            UserDepartment.department_id,
            UserDepartment.role,
            UserDepartment.granted_by,
            UserDepartment.granted_at,
            User.email,
        )
        .join(User, User.id == UserDepartment.user_id)
        .where(UserDepartment.department_id == department_id)
        .order_by(UserDepartment.user_id)
    )
    return list(result.all())
