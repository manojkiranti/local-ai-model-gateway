"""`POST /auth/register` is an admin action.

Why it changed: with a public register, anyone could pre-register a colleague's
address as a LOCAL account and permanently shadow their Active Directory
identity — the login route would then check the squatter's bcrypt hash and never
consult the directory. On an empty database the "first registrant becomes admin"
rule would hand that squatter admin as well.

COVERAGE GAP, stated deliberately: the empty-table bootstrap (unauthenticated
registration allowed when `users` is empty, so a fresh deployment can create its
first admin) is NOT exercised here. The harness has no empty-database fixture —
tests commit real rows into a development database with thousands of users — and
building one for a single branch would be a larger change than the branch. It is
in the manual verification list instead.

Real Postgres + TestClient; skips if the DB is down.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

from app.config import get_settings
from app.main import app

PASSWORD = "supersecret123"
SEEDED_ADMIN_EMAIL = "admin@example.com"


def _sql(fn):
    async def run():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                return await fn(conn)
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _skip_if_no_db():
    try:
        _sql(lambda c: c.execute(text("SELECT 1")))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")


def _delete_users(emails):
    if emails:
        _sql(
            lambda c: c.execute(
                text("DELETE FROM users WHERE email = ANY(:e)"), {"e": list(emails)}
            )
        )


def _fresh(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


def _login(client, email, password=PASSWORD):
    return client.post("/auth/login", json={"email": email, "password": password})


def _headers(client, email):
    resp = _login(client, email)
    if resp.status_code != 200:
        pytest.skip(f"login for {email} failed ({resp.status_code})")
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _admin_headers(client):
    headers = _headers(client, SEEDED_ADMIN_EMAIL)
    if client.get("/users/me", headers=headers).json().get("role") != "admin":
        pytest.skip(f"{SEEDED_ADMIN_EMAIL} is not an admin in this database")
    return headers


@pytest.fixture()
def created():
    _skip_if_no_db()
    emails: set[str] = set()
    yield emails
    _delete_users(emails)


def _register(client, email, headers=None):
    return client.post(
        "/auth/register",
        json={"email": email, "password": PASSWORD},
        headers=headers or {},
    )


def test_an_anonymous_caller_cannot_register(created):
    """The shadowing hole. The users table is non-empty, so this is not the
    bootstrap path."""
    email = _fresh("anon")
    with TestClient(app) as client:
        resp = _register(client, email)
    created.add(email)

    assert resp.status_code == 401
    assert resp.headers.get("www-authenticate") == "Bearer"


def test_a_member_cannot_register_another_user(created):
    member = _fresh("member")
    target = _fresh("target")
    with TestClient(app) as client:
        assert _register(client, member, _admin_headers(client)).status_code == 201
        created.add(member)

        resp = _register(client, target, _headers(client, member))
    created.add(target)

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Admin privileges required"


def test_an_admin_can_register_a_local_account(created):
    """The remaining legitimate use: service and break-glass accounts."""
    email = _fresh("svc")
    with TestClient(app) as client:
        resp = _register(client, email, _admin_headers(client))
    created.add(email)

    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == email
    assert body["auth_provider"] == "local"
    assert body["role"] == "member"


def test_a_registered_local_account_can_log_in(created):
    """Break-glass access must actually work — this is the AD-outage path."""
    email = _fresh("breakglass")
    with TestClient(app) as client:
        assert _register(client, email, _admin_headers(client)).status_code == 201
        created.add(email)
        resp = _login(client, email)

    assert resp.status_code == 200
    assert resp.json()["token_type"] == "bearer"


def test_a_duplicate_registration_is_409(created):
    email = _fresh("dup")
    with TestClient(app) as client:
        admin = _admin_headers(client)
        assert _register(client, email, admin).status_code == 201
        created.add(email)
        resp = _register(client, email, admin)

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Email already registered"


def test_an_invalid_token_is_rejected_rather_than_treated_as_anonymous(created):
    """An expired admin token must not fall through to the bootstrap check."""
    email = _fresh("badtoken")
    with TestClient(app) as client:
        resp = _register(client, email, {"Authorization": "Bearer not-a-jwt"})
    created.add(email)

    assert resp.status_code == 401
