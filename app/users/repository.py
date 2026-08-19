"""Data access for `users`.

The first users-layer repository. Before AD there was exactly one way a user row
came into existence (`POST /auth/register`) and every lookup was an inline
`select(User)`; a directory login adds a second creation path, and the two must
agree about how a row is found and minted.

The one subtle rule lives here: **a directory login must not inherit the
"first registrant becomes admin" bootstrap.** `_resolve_role` in
`app/auth/router.py` grants admin when the `users` table is empty, which is the
right answer for a deliberate registration and a serious hole for a login that
provisions on demand — on a fresh deployment whoever signed in first would own
the system. `resolve_directory_role` therefore consults the ADMIN_EMAILS
allowlist and nothing else.
"""

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models import PROVIDER_AD, ROLE_ADMIN, ROLE_MEMBER, User


async def get_by_email(session: AsyncSession, email: str) -> User | None:
    """Look up a user by their (already lower-cased) email."""
    result = await session.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


def resolve_directory_role(email: str, admin_emails: set[str]) -> str:
    """The role a directory-provisioned user starts with.

    Deliberately NOT `app/auth/router.py::_resolve_role`: that one promotes the
    first user when the table is empty, which for an auto-provisioning login path
    would hand admin to whoever happens to sign in first.
    """
    return ROLE_ADMIN if email in admin_emails else ROLE_MEMBER


async def create_directory_user(
    session: AsyncSession, *, email: str, role: str
) -> User:
    """Create a directory-backed user, tolerating a concurrent first login.

    Two simultaneous first logins for the same person both find no row and both
    reach the insert; the unique index on `email` means one of them loses. That
    is a race, not an error — the loser adopts the row the winner created.

    `password_hash` stays NULL, and `ck_users_credential` guarantees it can never
    become anything else for this provider.
    """
    user = User(
        email=email,
        auth_provider=PROVIDER_AD,
        password_hash=None,
        role=role,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await get_by_email(session, email)
        if existing is None:
            raise  # a different constraint failed; do not swallow it
        return existing

    await session.refresh(user)
    return user


async def get_by_id(session: AsyncSession, user_id: int) -> User | None:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


def _like_escape(value: str) -> str:
    """Make a user-supplied fragment literal inside a LIKE pattern.

    Without this, `_` matches any single character and `%` matches anything, so a
    search for `a_b` would return `axb` — the wrong user, silently. Backslash is
    escaped first or it would corrupt the escapes added after it.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search(
    session: AsyncSession, *, q: str | None, limit: int, offset: int
) -> tuple[int, list[User]]:
    """A page of users, optionally filtered by an email fragment.

    `total` is the count of MATCHING users, not of the table: a filtered page
    whose total was the whole table would make pagination nonsense.
    """
    where = []
    if q:
        where.append(User.email.ilike(f"%{_like_escape(q)}%", escape="\\"))

    total_stmt = select(func.count()).select_from(User)
    rows_stmt = select(User).order_by(User.id).limit(limit).offset(offset)
    for clause in where:
        total_stmt = total_stmt.where(clause)
        rows_stmt = rows_stmt.where(clause)

    total = (await session.execute(total_stmt)).scalar_one()
    rows = (await session.execute(rows_stmt)).scalars().all()
    return total, list(rows)


async def count_active_admins(session: AsyncSession) -> int:
    """How many active admins exist, locking them for the caller's transaction.

    `FOR UPDATE` is what stops two admins deactivating each other concurrently:
    both would otherwise read a count of 2, both would pass the last-admin guard,
    and the deployment would end with zero active admins and no way back in
    except SQL. Locking the overlapping row set serialises them, and the set is
    a handful of rows.
    """
    stmt = (
        select(User.id)
        .where(User.role == ROLE_ADMIN, User.is_active.is_(True))
        .with_for_update()
    )
    return len((await session.execute(stmt)).scalars().all())
