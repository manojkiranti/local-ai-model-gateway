"""Integration tests for chat-history pagination (real Postgres).

A throwaway NullPool engine per call: the app's module-level engine pools
connections bound to the first event loop, and each asyncio.run makes a new one,
so a second test would die with "Event loop is closed".
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

# Model modules only, not `app.main` — importing the whole FastAPI app (its
# routers, ollama/mcp clients, etc.) into a DB-only test module is unnecessary.
# These three alone populate Base.metadata with `departments`, which is what
# `chat_sessions.department_id`'s FK needs resolvable before this file's
# throwaway engine touches metadata (see tests/test_rag_reingest_integration.py
# for the same models-import pattern).
import app.rag.models  # noqa: F401  (departments, for chat_sessions.department_id's FK)
import app.history.models  # noqa: F401
import app.users.models  # noqa: F401
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


# Every user `_seed_user` creates, so `_cleanup` can remove it (and its
# sessions/messages, which cascade off `users.id`) afterwards. Without this the
# file leaks a user + sessions + messages per test, per run — CLAUDE.md records
# exactly this class of leak starving a DIFFERENT test file's drain loop once
# it accumulated (tests/test_rag_reingest_integration.py's own history). A test
# that leaves rows behind eventually breaks a different test.
_SEEDED_USER_IDS: list[int] = []


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
    _SEEDED_USER_IDS.append(user.id)
    return user.id


@pytest.fixture(autouse=True)
def _cleanup():
    """Delete this test's seeded users afterwards (chat_sessions.user_id and
    chat_messages.session_id both cascade, so this alone removes everything).
    Runs in `finally`-equivalent position via the fixture's teardown half, so
    it happens whether the test passed or the assertion raised — and any
    teardown failure here must not swallow the original test failure, so we
    let it raise on its own rather than wrapping the yield in try/except."""
    _SEEDED_USER_IDS.clear()
    yield
    ids = list(_SEEDED_USER_IDS)
    _SEEDED_USER_IDS.clear()
    if not ids:
        return

    async def _go():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": ids}
                )
        finally:
            await engine.dispose()

    try:
        asyncio.run(_go())
    except (OperationalError, InterfaceError, OSError):
        # The test already skipped for the same reason; nothing to clean.
        pass


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


def test_a_thread_page_is_the_NEWEST_messages_in_ascending_order():
    """A chat opens at the bottom, so the first page is the newest messages —
    but returned ascending, because the frontend renders top-to-bottom."""

    async def go(session):
        user_id = await _seed_user(session)
        s = await repo.create_session(session, user_id=user_id, title="t")
        for i in range(30):
            await repo.add_user_message(session, session_id=s.id, content=f"m{i}")
        await session.commit()

        row, msgs, cursor = await repo.get_thread_page(
            session, session_id=s.id, user_id=user_id, limit=10
        )
        assert row is not None
        assert [m.seq for m in msgs] == sorted(m.seq for m in msgs)
        assert msgs[-1].content == "m29"
        assert cursor is not None

    _run(go)


def test_walking_a_thread_backwards_covers_every_message_once():
    async def go(session):
        user_id = await _seed_user(session)
        s = await repo.create_session(session, user_id=user_id, title="t")
        for i in range(25):
            await repo.add_user_message(session, session_id=s.id, content=f"m{i}")
        await session.commit()

        seen, cursor = [], None
        for _ in range(10):
            _, msgs, cursor = await repo.get_thread_page(
                session, session_id=s.id, user_id=user_id, limit=10,
                before_seq=cursor,
            )
            seen.extend(m.seq for m in msgs)
            if cursor is None:
                break
        assert sorted(seen) == list(range(1, 26))

    _run(go)


def test_a_foreign_thread_is_not_readable():
    async def go(session):
        mine = await _seed_user(session)
        theirs = await _seed_user(session)
        s = await repo.create_session(session, user_id=theirs, title="t")
        await repo.add_user_message(session, session_id=s.id, content="secret")
        await session.commit()

        row, msgs, _ = await repo.get_thread_page(
            session, session_id=s.id, user_id=mine, limit=10
        )
        assert row is None
        assert msgs == []

    _run(go)


def test_the_unbounded_thread_read_is_gone():
    # Left in place, the next person reintroduces the bug this plan removed.
    assert not hasattr(repo, "get_session_with_messages")
    assert not hasattr(repo, "list_sessions")
