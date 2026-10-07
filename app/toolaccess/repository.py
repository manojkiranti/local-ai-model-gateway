"""Data access for `user_tool_access`."""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from .models import UserMcpLevel, UserMcpToolAccess, UserToolAccess


async def list_overrides(session: AsyncSession, user_id: int) -> list[UserToolAccess]:
    result = await session.execute(
        select(UserToolAccess)
        .where(UserToolAccess.user_id == user_id)
        .order_by(UserToolAccess.tool_name)
    )
    return list(result.scalars())


async def override_map(session: AsyncSession, user_id: int) -> dict[str, bool]:
    """Just {tool: allowed} — what `policy.allowed_tools` consumes."""
    result = await session.execute(
        select(UserToolAccess.tool_name, UserToolAccess.allowed).where(
            UserToolAccess.user_id == user_id
        )
    )
    return {name: allowed for name, allowed in result.all()}


async def set_override(
    session: AsyncSession, *, user_id: int, tool_name: str, allowed: bool, updated_by: int | None
) -> None:
    """Record an admin's decision. Unlike an MCP grant (insert-only), this is an
    upsert that DOES restamp `updated_at`/`updated_by` — but only when the
    decision actually changes, so re-saving the same value keeps the record of
    when it was really made."""
    stmt = insert(UserToolAccess).values(
        user_id=user_id, tool_name=tool_name, allowed=allowed, updated_by=updated_by
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["user_id", "tool_name"],
            set_={"allowed": allowed, "updated_at": func.now(), "updated_by": updated_by},
            where=UserToolAccess.allowed.is_distinct_from(stmt.excluded.allowed),
        )
    )


async def set_group(
    session: AsyncSession, *, user_id: int, tools: list[str], allowed: bool, updated_by: int | None
) -> None:
    """One admin decision for every tool in a group, in the caller's
    transaction — so a group is never left half-switched."""
    for tool_name in tools:
        await set_override(
            session, user_id=user_id, tool_name=tool_name, allowed=allowed, updated_by=updated_by
        )


async def clear_group(session: AsyncSession, *, user_id: int, tools: list[str]) -> None:
    """Return every tool in a group to its default."""
    if tools:
        await session.execute(
            delete(UserToolAccess).where(
                UserToolAccess.user_id == user_id, UserToolAccess.tool_name.in_(tools)
            )
        )


async def clear_override(session: AsyncSession, *, user_id: int, tool_name: str) -> bool:
    """Return the tool to its default. Returns whether a row was removed."""
    result = await session.execute(
        delete(UserToolAccess).where(
            UserToolAccess.user_id == user_id, UserToolAccess.tool_name == tool_name
        )
    )
    return bool(result.rowcount)


# --- MCP access (user_mcp_levels / user_mcp_tool_access) -------------------------


async def mcp_level_rows(session: AsyncSession, user_id: int) -> list[UserMcpLevel]:
    result = await session.execute(select(UserMcpLevel).where(UserMcpLevel.user_id == user_id))
    return list(result.scalars())


async def mcp_tool_rows(session: AsyncSession, user_id: int) -> list[UserMcpToolAccess]:
    result = await session.execute(
        select(UserMcpToolAccess)
        .where(UserMcpToolAccess.user_id == user_id)
        .order_by(UserMcpToolAccess.tool_name)
    )
    return list(result.scalars())


async def mcp_access_maps(session: AsyncSession, user_id: int) -> tuple[dict[str, str], dict[str, bool]]:
    """({system: level}, {tool: allowed}) — what `mcp_policy.is_allowed` consumes."""
    levels = await session.execute(
        select(UserMcpLevel.system, UserMcpLevel.level).where(UserMcpLevel.user_id == user_id)
    )
    tools = await session.execute(
        select(UserMcpToolAccess.tool_name, UserMcpToolAccess.allowed).where(
            UserMcpToolAccess.user_id == user_id
        )
    )
    return dict(levels.all()), dict(tools.all())


async def set_mcp_level(
    session: AsyncSession, *, user_id: int, system: str, level: str, updated_by: int | None
) -> None:
    """Upsert a system level; restamps only when the level actually changes
    (`set_override`'s rule)."""
    stmt = insert(UserMcpLevel).values(
        user_id=user_id, system=system, level=level, updated_by=updated_by
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["user_id", "system"],
            set_={"level": level, "updated_at": func.now(), "updated_by": updated_by},
            where=UserMcpLevel.level.is_distinct_from(stmt.excluded.level),
        )
    )


async def clear_mcp_level(session: AsyncSession, *, user_id: int, system: str) -> None:
    await session.execute(
        delete(UserMcpLevel).where(UserMcpLevel.user_id == user_id, UserMcpLevel.system == system)
    )


async def set_mcp_tool(
    session: AsyncSession, *, user_id: int, tool_name: str, allowed: bool, updated_by: int | None
) -> None:
    stmt = insert(UserMcpToolAccess).values(
        user_id=user_id, tool_name=tool_name, allowed=allowed, updated_by=updated_by
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["user_id", "tool_name"],
            set_={"allowed": allowed, "updated_at": func.now(), "updated_by": updated_by},
            where=UserMcpToolAccess.allowed.is_distinct_from(stmt.excluded.allowed),
        )
    )


async def clear_mcp_tool(session: AsyncSession, *, user_id: int, tool_name: str) -> None:
    await session.execute(
        delete(UserMcpToolAccess).where(
            UserMcpToolAccess.user_id == user_id, UserMcpToolAccess.tool_name == tool_name
        )
    )
