"""Integration tests for chat-history pagination (real Postgres).

A throwaway NullPool engine per call: the app's module-level engine pools
connections bound to the first event loop, and each asyncio.run makes a new one,
so a second test would die with "Event loop is closed".
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import app.main  # noqa: F401  (registers every ORM model's mapping)
from app.config import get_settings
from app.history import repository as repo
from app.users.models import User


def _run(coro_fn):
    async def _go():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                return await coro_fn(session)
        finally:
            await engine.dispose()

    try:
        return asyncio.run(_go())
    except (OperationalError, InterfaceError, OSError) as exc:
        # ONLY a genuine connection failure skips — a blanket except would let
        # a real bug in the code under test present as "Postgres unreachable".
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}: {exc}")


async def _seed_user(session) -> int:
    user = User(
        email=f"page-{uuid.uuid4().hex[:8]}@example.com",
        auth_provider="local",
        # ck_users_credential: a local user MUST have a hash.
        password_hash="$2b$12$" + "x" * 53,
        role="member",
        is_active=True,
    )
    session.add(user)
    await session.flush()
    return user.id


def test_paging_returns_every_session_exactly_once():
    async def go(session):
        user_id = await _seed_user(session)
        for i in range(25):
            s = await repo.create_session(session, user_id=user_id, title=f"t{i}")
            await repo.add_user_message(session, session_id=s.id, content="hi")
        await session.commit()

        seen, cursor = [], None
        for _ in range(10):
            rows, cursor = await repo.list_sessions_page(
                session, user_id=user_id, limit=10, cursor=cursor
            )
            seen.extend(s.id for s, _ in rows)
            if cursor is None:
                break
        assert len(seen) == 25
        assert len(set(seen)) == 25

    _run(go)


def test_paging_is_stable_when_a_session_is_touched_mid_scroll():
    """The case offset paging silently gets wrong. Bumping a session's
    updated_at moves it to the front; with OFFSET the rows behind it shift and
    one session is shown twice while another is never shown at all."""

    async def go(session):
        user_id = await _seed_user(session)
        made = []
        for i in range(20):
            s = await repo.create_session(session, user_id=user_id, title=f"t{i}")
            await repo.add_user_message(session, session_id=s.id, content="hi")
            made.append(s.id)
        await session.commit()

        first, cursor = await repo.list_sessions_page(
            session, user_id=user_id, limit=5, cursor=None
        )
        # Touch the OLDEST session, jumping it to the front of the ordering.
        await repo.add_user_message(session, session_id=made[0], content="again")
        await session.commit()

        rest, seen = [], [s.id for s, _ in first]
        for _ in range(10):
            rows, cursor = await repo.list_sessions_page(
                session, user_id=user_id, limit=5, cursor=cursor
            )
            rest.extend(s.id for s, _ in rows)
            if cursor is None:
                break
        # No duplicates across the scroll.
        assert len(set(seen + rest)) == len(seen + rest)

    _run(go)


def test_message_count_matches_a_direct_count():
    async def go(session):
        user_id = await _seed_user(session)
        s = await repo.create_session(session, user_id=user_id, title="counted")
        for _ in range(7):
            await repo.add_user_message(session, session_id=s.id, content="hi")
        await session.commit()

        rows, _ = await repo.list_sessions_page(session, user_id=user_id, limit=10)
        counts = {row[0].id: row[1] for row in rows}
        assert counts[s.id] == 7

    _run(go)


def test_another_users_sessions_are_never_returned():
    async def go(session):
        mine = await _seed_user(session)
        theirs = await _seed_user(session)
        s = await repo.create_session(session, user_id=theirs, title="not yours")
        await repo.add_user_message(session, session_id=s.id, content="hi")
        await session.commit()

        rows, _ = await repo.list_sessions_page(session, user_id=mine, limit=100)
        assert all(row[0].user_id == mine for row in rows)

    _run(go)
