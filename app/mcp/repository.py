"""Data access for `user_mcp_grants`."""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import UserMcpGrant


async def list_grants(session: AsyncSession, user_id: int) -> list[UserMcpGrant]:
    result = await session.execute(
        select(UserMcpGrant)
        .where(UserMcpGrant.user_id == user_id)
        .order_by(UserMcpGrant.grant_key)
    )
    return list(result.scalars())


async def grant_keys_for(session: AsyncSession, user_id: int) -> set[str]:
    """Just the keys — what `McpIdentity.from_grants` consumes."""
    result = await session.execute(
        select(UserMcpGrant.grant_key).where(UserMcpGrant.user_id == user_id)
    )
    return set(result.scalars())


async def grant(
    session: AsyncSession, *, user_id: int, grant_key: str, granted_by: int | None
) -> None:
    """Idempotent grant.

    ON CONFLICT DO NOTHING, deliberately not DO UPDATE: re-granting must leave
    `granted_at` and `granted_by` alone. An upsert that overwrites them reports
    success while destroying the record of when access was actually given —
    the same defect `test_omitting_role_on_a_RE_grant_does_not_demote` guards
    against on the department member route.
    """
    await session.execute(
        insert(UserMcpGrant)
        .values(user_id=user_id, grant_key=grant_key, granted_by=granted_by)
        .on_conflict_do_nothing(index_elements=["user_id", "grant_key"])
    )


async def revoke(session: AsyncSession, *, user_id: int, grant_key: str) -> bool:
    """Remove a grant. Returns whether a row was actually removed."""
    result = await session.execute(
        delete(UserMcpGrant).where(
            UserMcpGrant.user_id == user_id, UserMcpGrant.grant_key == grant_key
        )
    )
    return bool(result.rowcount)
