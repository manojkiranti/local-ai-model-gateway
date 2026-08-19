"""Repository tests against real Postgres. Skips if the DB is unreachable.

Uses a throwaway NullPool engine per call rather than the app's module-level
engine: that one pools connections bound to the first event loop, and every
`asyncio.run` here creates a new one.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.rag import repository as repo


def _run(fn):
    """Run `fn(session)` against a fresh engine, disposed in the same loop."""
    async def main():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await fn(s)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _sql(fn):
    async def main():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                return await fn(conn)
        finally:
            await engine.dispose()

    return asyncio.run(main())


def _skip_if_no_db():
    try:
        _sql(lambda c: c.execute(text("SELECT 1")))
    except Exception as exc:  # noqa: BLE001 - DB down -> skip
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")


# Not a real bcrypt digest, and it does not need to be: `verify_password`
# returns False for a malformed hash rather than raising, and nothing here
# logs in. It only has to be NOT NULL to satisfy `ck_users_credential`.
PLACEHOLDER_HASH = "x" * 60


@pytest.fixture()
def user_id():
    """A throwaway user row; removed afterwards."""
    _skip_if_no_db()
    email = f"rag-repo-{uuid.uuid4().hex[:8]}@example.com"

    async def make(conn):
        # `password_hash` is required for a 'local' user by
        # `ck_users_credential`: a local account with no password could never log
        # in, and the constraint exists so an 'ad' account can never acquire one.
        # This row never authenticates over HTTP, so the value only has to exist.
        return (await conn.execute(text(
            "INSERT INTO users (email, auth_provider, password_hash, role, is_active)"
            " VALUES (:e, 'local', :h, 'member', true) RETURNING id"),
            {"e": email, "h": PLACEHOLDER_HASH})).scalar_one()

    uid = _sql(make)
    yield uid
    _sql(lambda c: c.execute(text("DELETE FROM users WHERE id = :i"), {"i": uid}))


@pytest.fixture()
def codes():
    """Unique department codes, with cleanup."""
    _skip_if_no_db()
    made = [f"d{uuid.uuid4().hex[:8]}", f"d{uuid.uuid4().hex[:8]}"]
    yield made
    _sql(lambda c: c.execute(
        text("DELETE FROM departments WHERE code = ANY(:c)"), {"c": made}))


def test_create_and_fetch_by_code(codes):
    async def go(s):
        await repo.create_department(s, code=codes[0], name="HR")
        await s.commit()
        return await repo.get_department_by_code(s, codes[0])

    dept = _run(go)
    assert dept is not None and dept.name == "HR" and dept.is_active is True


def test_unknown_code_is_none(codes):
    assert _run(lambda s: repo.get_department_by_code(s, "no-such-code-xyz")) is None


def test_access_is_denied_until_granted(codes, user_id):
    async def go(s):
        d = await repo.create_department(s, code=codes[0], name="HR")
        await s.commit()
        before = await repo.has_department_access(
            s, user_id=user_id, department_id=d.id)
        await repo.grant_department(
            s, user_id=user_id, department_id=d.id, granted_by=None)
        await s.commit()
        after = await repo.has_department_access(
            s, user_id=user_id, department_id=d.id)
        return before, after

    before, after = _run(go)
    assert before is False
    assert after is True


def test_grant_is_idempotent(codes, user_id):
    """Re-granting must not raise on the composite PK."""
    async def go(s):
        d = await repo.create_department(s, code=codes[0], name="HR")
        await s.commit()
        await repo.grant_department(
            s, user_id=user_id, department_id=d.id, granted_by=None)
        await s.commit()
        await repo.grant_department(
            s, user_id=user_id, department_id=d.id, granted_by=None)
        await s.commit()
        return len(await repo.list_department_members(s, d.id))

    assert _run(go) == 1


def test_revoke_removes_access_and_reports_whether_it_did(codes, user_id):
    async def go(s):
        d = await repo.create_department(s, code=codes[0], name="HR")
        await s.commit()
        await repo.grant_department(
            s, user_id=user_id, department_id=d.id, granted_by=None)
        await s.commit()
        first = await repo.revoke_department(s, user_id=user_id, department_id=d.id)
        await s.commit()
        second = await repo.revoke_department(s, user_id=user_id, department_id=d.id)
        await s.commit()
        access = await repo.has_department_access(
            s, user_id=user_id, department_id=d.id)
        return first, second, access

    first, second, access = _run(go)
    assert first is True
    assert second is False   # nothing left to revoke
    assert access is False


def test_list_for_user_returns_only_granted_and_active(codes, user_id):
    async def go(s):
        granted = await repo.create_department(s, code=codes[0], name="HR")
        await repo.create_department(s, code=codes[1], name="Finance")
        await s.commit()
        await repo.grant_department(
            s, user_id=user_id, department_id=granted.id, granted_by=None)
        await s.commit()
        names = [d.code for d in await repo.list_departments_for_user(s, user_id)]
        # Now soft-disable the granted one: it must disappear.
        await repo.set_department_active(s, code=codes[0], is_active=False)
        await s.commit()
        after = [d.code for d in await repo.list_departments_for_user(s, user_id)]
        return names, after

    names, after = _run(go)
    assert names == [codes[0]]          # granted only, not codes[1]
    assert after == []                  # inactive departments are hidden


def test_set_active_on_unknown_code_returns_none(codes):
    assert _run(lambda s: repo.set_department_active(
        s, code="no-such-code-xyz", is_active=False)) is None
