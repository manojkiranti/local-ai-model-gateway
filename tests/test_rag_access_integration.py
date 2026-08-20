"""resolve_department — the department permission boundary.

Every test here is a security assertion. Skips if Postgres is unreachable.
"""

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.history.models import ChatSession
from app.rag import repository as repo
from app.rag.access import effective_department_level, resolve_department
from app.users.models import ROLE_ADMIN, ROLE_MEMBER, User

# Not a real bcrypt digest, and it does not need to be: nothing here logs in.
# It only has to be NOT NULL to satisfy `ck_users_credential`, which forbids a
# 'local' user without a password (and an 'ad' user with one).
PLACEHOLDER_HASH = "x" * 60


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


@pytest.fixture()
def env():
    """Three departments (one inactive), a member with a grant to the first,
    and an admin with no grants at all."""
    _skip_if_no_db()
    tag = uuid.uuid4().hex[:8]
    state = {"hr_code": f"hr{tag}", "fin_code": f"fin{tag}", "off_code": f"off{tag}"}

    async def setup(s):
        hr = await repo.create_department(s, code=state["hr_code"], name="HR")
        fin = await repo.create_department(s, code=state["fin_code"], name="Fin")
        off = await repo.create_department(s, code=state["off_code"], name="Off")
        off.is_active = False
        await s.flush()
        # `password_hash` is required for a 'local' user by
        # `ck_users_credential` — see the note beside PLACEHOLDER_HASH.
        member = User(email=f"m{tag}@example.com", auth_provider="local",
                      password_hash=PLACEHOLDER_HASH,
                      role=ROLE_MEMBER, is_active=True)
        admin = User(email=f"a{tag}@example.com", auth_provider="local",
                     password_hash=PLACEHOLDER_HASH,
                     role=ROLE_ADMIN, is_active=True)
        s.add_all([member, admin])
        await s.flush()
        await repo.grant_department(
            s, user_id=member.id, department_id=hr.id, granted_by=None)
        await s.commit()
        return {"hr": hr.id, "fin": fin.id, "off": off.id,
                "member": member, "admin": admin}

    state.update(_run(setup))
    yield state

    async def teardown(conn):
        await conn.execute(text("DELETE FROM users WHERE id IN (:m,:a)"),
                           {"m": state["member"].id, "a": state["admin"].id})
        await conn.execute(text("DELETE FROM departments WHERE id IN (:h,:f,:o)"),
                           {"h": state["hr"], "f": state["fin"], "o": state["off"]})
    _sql(teardown)


def _resolve(user, code, chat_session=None):
    return _run(lambda s: resolve_department(s, user, code, chat_session))


def test_no_code_and_no_session_department_is_general_chat(env):
    assert _resolve(env["member"], None, None) is None


def test_granted_department_resolves(env):
    ctx = _resolve(env["member"], env["hr_code"], None)
    assert ctx is not None
    assert ctx.id == env["hr"] and ctx.code == env["hr_code"]


def test_ungranted_department_is_403(env):
    with pytest.raises(HTTPException) as exc:
        _resolve(env["member"], env["fin_code"], None)
    assert exc.value.status_code == 403


def test_unknown_department_is_404(env):
    with pytest.raises(HTTPException) as exc:
        _resolve(env["member"], "no-such-department", None)
    assert exc.value.status_code == 404


def test_inactive_department_is_404_even_for_admin(env):
    """Soft-disabled means gone from the product, for everyone."""
    with pytest.raises(HTTPException) as exc:
        _resolve(env["admin"], env["off_code"], None)
    assert exc.value.status_code == 404


def test_admin_bypasses_the_grant_check(env):
    """The admin holds no grant to Finance and still resolves it."""
    ctx = _resolve(env["admin"], env["fin_code"], None)
    assert ctx is not None and ctx.id == env["fin"]


def test_session_bound_to_another_department_is_409(env):
    """An HR session must not be continued as Finance on a later turn."""
    bound = ChatSession(id=uuid.uuid4().hex, user_id=env["member"].id,
                        department_id=env["fin"])
    with pytest.raises(HTTPException) as exc:
        _resolve(env["member"], env["hr_code"], bound)
    assert exc.value.status_code == 409


def test_matching_session_department_resolves(env):
    bound = ChatSession(id=uuid.uuid4().hex, user_id=env["member"].id,
                        department_id=env["hr"])
    ctx = _resolve(env["member"], env["hr_code"], bound)
    assert ctx is not None and ctx.id == env["hr"]


def test_a_bound_session_continues_in_its_own_department_without_a_code(env):
    """Slice-3 contract (replaces slice 1's 400): `department` is required only
    to OPEN a department chat. On a bound session the server reads
    chat_sessions.department_id — the source of truth — so omitting the field
    continues in HR rather than erroring or downgrading to general."""
    bound = ChatSession(id=uuid.uuid4().hex, user_id=env["member"].id,
                        department_id=env["hr"])
    ctx = _resolve(env["member"], None, bound)
    assert ctx is not None
    assert ctx.id == env["hr"] and ctx.code == env["hr_code"]


def test_a_bound_session_still_rechecks_the_grant_when_no_code_is_given(env):
    """The no-code path must not become a way around revocation."""
    bound = ChatSession(id=uuid.uuid4().hex, user_id=env["member"].id,
                        department_id=env["fin"])          # member has no Finance grant
    with pytest.raises(HTTPException) as exc:
        _resolve(env["member"], None, bound)
    assert exc.value.status_code == 403


def test_a_bound_session_on_an_inactive_department_is_404(env):
    bound = ChatSession(id=uuid.uuid4().hex, user_id=env["admin"].id,
                        department_id=env["off"])
    with pytest.raises(HTTPException) as exc:
        _resolve(env["admin"], None, bound)
    assert exc.value.status_code == 404


def test_general_session_stays_general(env):
    bound = ChatSession(id=uuid.uuid4().hex, user_id=env["member"].id,
                        department_id=None)
    assert _resolve(env["member"], None, bound) is None


def test_existing_general_session_cannot_be_adopted_into_a_department(env):
    """The hole this closes: every prior turn in a general chat was answered
    without departmental grounding, so relabelling the thread HR would
    misrepresent all of them. A new chat is the only way in."""
    general = ChatSession(id=uuid.uuid4().hex, user_id=env["member"].id,
                          department_id=None)
    with pytest.raises(HTTPException) as exc:
        _resolve(env["member"], env["hr_code"], general)
    assert exc.value.status_code == 409


def test_a_brand_new_session_may_be_given_a_department(env):
    """The counterpart: chat_session=None is a NEW session, not an existing
    general one, and must still be allowed to open in a department."""
    ctx = _resolve(env["member"], env["hr_code"], None)
    assert ctx is not None and ctx.id == env["hr"]


def test_session_belonging_to_another_user_is_404(env):
    """Ownership is re-checked here rather than assumed of the caller. 404, not
    403 — a foreign session id must not be confirmed to exist."""
    foreign = ChatSession(id=uuid.uuid4().hex, user_id=env["admin"].id,
                          department_id=env["hr"])
    with pytest.raises(HTTPException) as exc:
        _resolve(env["member"], env["hr_code"], foreign)
    assert exc.value.status_code == 404


def test_ownership_is_checked_before_the_department_is_even_looked_up(env):
    """A foreign session with a nonsense code still 404s on ownership — the
    check must not be reachable-around by varying the code."""
    foreign = ChatSession(id=uuid.uuid4().hex, user_id=env["admin"].id,
                          department_id=None)
    with pytest.raises(HTTPException) as exc:
        _resolve(env["member"], "no-such-department", foreign)
    assert exc.value.status_code == 404
    assert "Session" in exc.value.detail


@pytest.mark.parametrize("level", ["viewer", "editor", "owner"])
def test_every_level_can_chat_in_its_department(env, level):
    """The chat boundary admits ANY level. Curation is what levels gate; holding
    the grant at all is what "may ask a question here" means."""
    async def relevel(s):
        await repo.grant_department(
            s, user_id=env["member"].id, department_id=env["hr"],
            granted_by=None, role=level)
        await s.commit()

    _run(relevel)
    ctx = _resolve(env["member"], env["hr_code"], None)
    assert ctx is not None
    assert ctx.id == env["hr"] and ctx.code == env["hr_code"]


def test_effective_level_reports_the_members_grant(env):
    async def go(s):
        dept = await repo.get_department_by_id(s, env["hr"])
        return await effective_department_level(s, env["member"], dept)

    assert _run(go) == "viewer"


def test_effective_level_is_owner_for_a_global_admin_without_a_grant(env):
    """A global admin is owner-equivalent for capabilities, and reaches that
    answer WITHOUT a user_departments lookup — the admin bypass is unchanged."""
    async def go(s):
        dept = await repo.get_department_by_id(s, env["hr"])
        return await effective_department_level(s, env["admin"], dept)

    assert _run(go) == "owner"


def test_effective_level_is_none_without_a_grant(env):
    async def go(s):
        dept = await repo.get_department_by_id(s, env["fin"])
        return await effective_department_level(s, env["member"], dept)

    assert _run(go) is None
