"""Integration tests for user_mcp_grants and the admin route.

Builds a throwaway NullPool engine per call rather than using the app's
module-level `engine`: that one pools connections bound to the first event loop,
and each `asyncio.run` creates a new one, so the second test in the file would
die with "Event loop is closed". Same rule as the RAG and api-keys integration
tests.
"""

import asyncio
import os
import re

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.mcp import grants
from app.mcp.models import UserMcpGrant

DB_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="DATABASE_URL not set")


def _run(coro_fn):
    async def main():
        engine = create_async_engine(DB_URL, poolclass=NullPool)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with maker() as session:
                return await coro_fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(main())


async def _an_admin_id(session):
    from app.users.models import User

    row = await session.scalar(select(User.id).where(User.role == "admin").limit(1))
    assert row is not None, "seed an admin first (admin@example.com)"
    return row


def test_the_check_constraint_and_the_frozenset_are_the_same_vocabulary():
    """The two copies are deliberate; drifting apart is not.

    Reads the LIVE constraint rather than a hand-written literal: a literal here
    would be a third copy, which is exactly the drift it is meant to detect.
    """

    async def go(session):
        definition = await session.scalar(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_user_mcp_grants_key'"
            )
        )
        assert definition, "ck_user_mcp_grants_key does not exist"
        in_check = set(re.findall(r"'([^']+)'", definition))
        assert in_check == set(grants.ALL_GRANTS), (
            f"CHECK has {sorted(in_check)}, grants.py has {sorted(grants.ALL_GRANTS)}"
        )

    _run(go)


def test_an_unknown_grant_key_cannot_be_stored():
    """ck_user_mcp_grants_key: a typo'd grant must not reach the table.

    A stored 'mcp-hmrs' would be silently powerless — a privilege bug that
    reads as a typo, which is why `ck_users_role` exists too.
    """

    async def go(session):
        admin_id = await _an_admin_id(session)
        session.add(
            UserMcpGrant(user_id=admin_id, grant_key="mcp-hmrs", granted_by=admin_id)
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    _run(go)


def test_every_vocabulary_key_is_storable():
    """The mirror of the test above: the CHECK must not be tighter than the code."""

    async def go(session):
        admin_id = await _an_admin_id(session)
        for key in sorted(grants.ALL_GRANTS):
            session.add(
                UserMcpGrant(user_id=admin_id, grant_key=key, granted_by=admin_id)
            )
            await session.flush()
        await session.rollback()

    _run(go)


def test_the_same_grant_cannot_be_stored_twice():
    async def go(session):
        admin_id = await _an_admin_id(session)
        session.add(
            UserMcpGrant(user_id=admin_id, grant_key=grants.ROLE_HRMS, granted_by=admin_id)
        )
        await session.flush()
        session.add(
            UserMcpGrant(user_id=admin_id, grant_key=grants.ROLE_HRMS, granted_by=admin_id)
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    _run(go)


def test_the_audit_columns_survive_the_granter_being_deleted():
    """granted_by is ON DELETE SET NULL, not CASCADE: the fact that access was
    granted at a time outlives the admin who granted it."""

    async def go(session):
        definition = await session.scalar(
            text(
                # Cast to text: asyncpg decodes the raw pg "char" type
                # (confdeltype) as bytes, not str, which would make the
                # comparison below depend on the driver's own type mapping
                # rather than on the schema.
                "SELECT confdeltype::text FROM pg_constraint "
                "WHERE conrelid = 'user_mcp_grants'::regclass "
                "AND confrelid = 'users'::regclass "
                "AND 'granted_by' = ANY("
                "  SELECT attname FROM pg_attribute "
                "  WHERE attrelid = conrelid AND attnum = ANY(conkey)"
                ")"
            )
        )
        assert definition == "n", "granted_by must be ON DELETE SET NULL"

    _run(go)


def test_re_granting_does_not_rewrite_the_audit_timestamp():
    """The `POST .../members` lesson: an upsert that overwrites audit columns
    reports success while destroying the record of when access was given."""

    async def go(session):
        from app.mcp import repository as repo

        admin_id = await _an_admin_id(session)
        await repo.grant(
            session, user_id=admin_id, grant_key=grants.ROLE_IZONE, granted_by=admin_id
        )
        await session.flush()
        first = await session.scalar(
            select(UserMcpGrant.granted_at).where(
                UserMcpGrant.user_id == admin_id,
                UserMcpGrant.grant_key == grants.ROLE_IZONE,
            )
        )
        await repo.grant(
            session, user_id=admin_id, grant_key=grants.ROLE_IZONE, granted_by=admin_id
        )
        await session.flush()
        second = await session.scalar(
            select(UserMcpGrant.granted_at).where(
                UserMcpGrant.user_id == admin_id,
                UserMcpGrant.grant_key == grants.ROLE_IZONE,
            )
        )
        assert first == second, "a re-grant rewrote granted_at"
        await session.rollback()

    _run(go)


def test_revoking_is_idempotent_and_reports_whether_anything_changed():
    async def go(session):
        from app.mcp import repository as repo

        admin_id = await _an_admin_id(session)
        await repo.grant(
            session, user_id=admin_id, grant_key=grants.ROLE_EMS, granted_by=admin_id
        )
        await session.flush()
        assert await repo.revoke(session, user_id=admin_id, grant_key=grants.ROLE_EMS) is True
        assert await repo.revoke(session, user_id=admin_id, grant_key=grants.ROLE_EMS) is False
        await session.rollback()

    _run(go)


def test_grant_keys_for_returns_a_flat_set():
    async def go(session):
        from app.mcp import repository as repo

        admin_id = await _an_admin_id(session)
        await repo.grant(
            session, user_id=admin_id, grant_key=grants.ROLE_EMS, granted_by=admin_id
        )
        await repo.grant(
            session, user_id=admin_id, grant_key=grants.PERM_EMS_QUERY, granted_by=admin_id
        )
        await session.flush()
        keys = await repo.grant_keys_for(session, admin_id)
        assert grants.ROLE_EMS in keys and grants.PERM_EMS_QUERY in keys
        await session.rollback()

    _run(go)


def test_an_admin_role_alone_confers_no_grant():
    """Design §3.4. This is the departure from effective_level that a later
    refactor is most likely to "fix" back — hence a test that names it."""

    async def go(session):
        from app.mcp import repository as repo

        admin_id = await _an_admin_id(session)
        await session.execute(
            text("DELETE FROM user_mcp_grants WHERE user_id = :uid"), {"uid": admin_id}
        )
        keys = await repo.grant_keys_for(session, admin_id)
        assert keys == set(), "a global admin was handed grants implicitly"
        await session.rollback()

    _run(go)
