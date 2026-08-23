"""Integration tests for the api_keys tables and admin routes.

Builds a throwaway NullPool engine per call rather than using the app's
module-level `engine`: that one pools connections bound to the first event loop,
and each `asyncio.run` creates a new one, so the second test in the file would
die with "Event loop is closed". Same rule as the RAG integration tests.
"""

import asyncio
import os
import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.apikeys import keygen
from app.apikeys.models import ApiKey

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
    from sqlalchemy import select

    from app.users.models import User

    row = await session.scalar(select(User.id).where(User.role == "admin").limit(1))
    assert row is not None, "seed an admin first (admin@example.com)"
    return row


def test_a_revoked_key_must_record_when_it_was_revoked():
    """ck_api_keys_revoked: is_active=false with no revoked_at is illegal.

    The half-revoked state is unrepresentable on purpose — 'inactive since
    when?' has no answer, and 'revoked but still active' would still serve.
    """

    async def body(session):
        admin_id = await _an_admin_id(session)
        minted = keygen.mint()
        session.add(
            ApiKey(
                id=uuid.uuid4().hex,
                name="ck-test",
                key_prefix=minted.prefix,
                key_hash=minted.key_hash,
                scopes=["ocr:read"],
                is_active=False,
                revoked_at=None,          # <- the illegal combination
                created_by_user_id=admin_id,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    _run(body)


def test_an_unknown_scope_cannot_be_stored():
    """ck_api_keys_scopes closes the vocabulary, like ck_documents_status."""

    async def body(session):
        admin_id = await _an_admin_id(session)
        minted = keygen.mint()
        session.add(
            ApiKey(
                id=uuid.uuid4().hex,
                name="scope-test",
                key_prefix=minted.prefix,
                key_hash=minted.key_hash,
                scopes=["ocr:reed"],      # typo
                created_by_user_id=admin_id,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    _run(body)


def test_two_keys_cannot_share_a_prefix():
    """The prefix is the lookup key, so a collision makes verification
    ambiguous. UNIQUE is functional here, not tidiness."""

    async def body(session):
        admin_id = await _an_admin_id(session)
        minted = keygen.mint()
        for _ in range(2):
            session.add(
                ApiKey(
                    id=uuid.uuid4().hex,
                    name="dup-prefix",
                    key_prefix=minted.prefix,
                    key_hash=minted.key_hash,
                    scopes=["ocr:read"],
                    created_by_user_id=admin_id,
                )
            )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    _run(body)
