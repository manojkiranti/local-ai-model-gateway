"""The three admin levers needed to actually run the gateway day to day.

Before these, an admin could authenticate people but not administer them: finding
a user meant paging `GET /users` with no filter, granting a department needed a
numeric id the admin had no way to look up, and deactivating someone was only
possible in SQL.

  GET   /users?q=              find a user by email
  PATCH /users/{id}            the offboarding switch (immediate)
  POST  /v1/departments/{code}/members  now accepts {"email": ...}

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
from app.users.policy import SELF_DEACTIVATION

PASSWORD = "supersecret123"
SEEDED_ADMIN_EMAIL = "admin@example.com"
PLACEHOLDER_HASH = "x" * 60


def _sql(q, **p):
    async def run():
        engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                result = await conn.execute(text(q), p)
                return result.mappings().all() if result.returns_rows else result.rowcount
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _skip_if_no_db():
    try:
        _sql("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable: {type(exc).__name__}")


def _fresh(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


def _insert_local(email):
    _sql(
        "INSERT INTO users (email, auth_provider, password_hash, role, is_active)"
        " VALUES (:e, 'local', :h, 'member', true)",
        e=email,
        h=PLACEHOLDER_HASH,
    )


@pytest.fixture()
def cleanup():
    _skip_if_no_db()
    emails: set[str] = set()
    codes: set[str] = set()
    yield emails, codes
    if emails:
        _sql(
            "DELETE FROM user_departments WHERE user_id IN"
            " (SELECT id FROM users WHERE email = ANY(:e))",
            e=list(emails),
        )
        _sql("DELETE FROM users WHERE email = ANY(:e)", e=list(emails))
    if codes:
        _sql("DELETE FROM departments WHERE code = ANY(:c)", c=list(codes))


def _token(client, email, password=PASSWORD):
    resp = client.post("/auth/login", json={"email": email, "password": password})
    if resp.status_code != 200:
        pytest.skip(f"login for {email} failed ({resp.status_code})")
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _admin(client):
    headers = _token(client, SEEDED_ADMIN_EMAIL)
    if client.get("/users/me", headers=headers).json().get("role") != "admin":
        pytest.skip(f"{SEEDED_ADMIN_EMAIL} is not an admin in this database")
    return headers


def _make_member(client, admin, email):
    resp = client.post(
        "/auth/register", json={"email": email, "password": PASSWORD}, headers=admin
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# --------------------------------------------------------------------------
# GET /users?q=
# --------------------------------------------------------------------------

def test_a_user_can_be_found_by_their_full_email(cleanup):
    emails, _ = cleanup
    email = _fresh("find-me")
    with TestClient(app) as client:
        admin = _admin(client)
        uid = _make_member(client, admin, email)
        emails.add(email)

        body = client.get(f"/users?q={email}", headers=admin).json()

    assert body["total"] == 1
    assert [u["id"] for u in body["items"]] == [uid]


def test_the_search_is_a_case_insensitive_substring(cleanup):
    emails, _ = cleanup
    tag = uuid.uuid4().hex[:8]
    email = f"MiXeD-{tag}@example.com"
    with TestClient(app) as client:
        admin = _admin(client)
        _make_member(client, admin, email)
        emails.add(email.lower())

        by_fragment = client.get(f"/users?q={tag}", headers=admin).json()
        by_upper = client.get(f"/users?q=MIXED-{tag}", headers=admin).json()

    assert by_fragment["total"] == 1
    assert by_upper["total"] == 1


def test_the_total_reflects_the_filter_not_the_whole_table(cleanup):
    emails, _ = cleanup
    email = _fresh("counted")
    with TestClient(app) as client:
        admin = _admin(client)
        _make_member(client, admin, email)
        emails.add(email)

        filtered = client.get(f"/users?q={email}", headers=admin).json()
        unfiltered = client.get("/users?limit=1", headers=admin).json()

    assert filtered["total"] == 1
    assert unfiltered["total"] > 1, "the unfiltered total should still be everyone"


def test_like_wildcards_in_the_query_are_literal(cleanup):
    """Unescaped, `_` matches any character and would return the wrong user."""
    emails, _ = cleanup
    tag = uuid.uuid4().hex[:6]
    literal = f"zzq-{tag}-a_b@example.com"
    decoy = f"zzq-{tag}-axb@example.com"
    _insert_local(literal)
    _insert_local(decoy)
    emails.update({literal, decoy})

    with TestClient(app) as client:
        admin = _admin(client)
        found = client.get(f"/users?q=zzq-{tag}-a_b", headers=admin).json()
        both = client.get(f"/users?q=zzq-{tag}-", headers=admin).json()

    assert [u["email"] for u in found["items"]] == [literal]
    assert both["total"] == 2, "the shared prefix should still find both"


def test_a_percent_query_does_not_dump_the_table(cleanup):
    _skip_if_no_db()
    with TestClient(app) as client:
        admin = _admin(client)
        body = client.get("/users?q=%25", headers=admin).json()
    assert body["total"] == 0


def test_an_unmatched_query_is_an_empty_page_not_a_404(cleanup):
    _skip_if_no_db()
    with TestClient(app) as client:
        admin = _admin(client)
        resp = client.get("/users?q=definitely-nobody-here-xyz", headers=admin)
    assert resp.status_code == 200
    assert resp.json() == {"total": 0, "limit": 50, "offset": 0, "items": []}


def test_searching_users_is_admin_only(cleanup):
    emails, _ = cleanup
    email = _fresh("nosy")
    with TestClient(app) as client:
        admin = _admin(client)
        _make_member(client, admin, email)
        emails.add(email)
        resp = client.get("/users?q=admin", headers=_token(client, email))
    assert resp.status_code == 403


# --------------------------------------------------------------------------
# PATCH /users/{id}
# --------------------------------------------------------------------------

def test_deactivating_a_user_invalidates_their_existing_token(cleanup):
    """The point of the endpoint: it does NOT wait for the 24h JWT to expire."""
    emails, _ = cleanup
    email = _fresh("offboard")
    with TestClient(app) as client:
        admin = _admin(client)
        uid = _make_member(client, admin, email)
        emails.add(email)

        theirs = _token(client, email)
        assert client.get("/users/me", headers=theirs).status_code == 200

        patched = client.patch(
            f"/users/{uid}", json={"is_active": False}, headers=admin
        )
        after = client.get("/users/me", headers=theirs)
        relogin = client.post(
            "/auth/login", json={"email": email, "password": PASSWORD}
        )

    assert patched.status_code == 200
    assert patched.json()["is_active"] is False
    assert after.status_code == 401, "the already-issued token must stop working"
    assert relogin.status_code == 403, "and they must not be able to log in again"


def test_reactivating_restores_access(cleanup):
    emails, _ = cleanup
    email = _fresh("reactivate")
    with TestClient(app) as client:
        admin = _admin(client)
        uid = _make_member(client, admin, email)
        emails.add(email)

        client.patch(f"/users/{uid}", json={"is_active": False}, headers=admin)
        resp = client.patch(f"/users/{uid}", json={"is_active": True}, headers=admin)
        again = client.post(
            "/auth/login", json={"email": email, "password": PASSWORD}
        )

    assert resp.status_code == 200
    assert resp.json()["is_active"] is True
    assert again.status_code == 200


def test_an_admin_cannot_deactivate_themselves(cleanup):
    _skip_if_no_db()
    with TestClient(app) as client:
        admin = _admin(client)
        me = client.get("/users/me", headers=admin).json()
        resp = client.patch(
            f"/users/{me['id']}", json={"is_active": False}, headers=admin
        )
        still_ok = client.get("/users/me", headers=admin)

    assert resp.status_code == 409
    assert resp.json()["detail"] == SELF_DEACTIVATION
    assert still_ok.status_code == 200, "the refusal must not have changed anything"


def test_deactivating_a_member_is_allowed_while_admins_remain(cleanup):
    """The last-admin guard must not over-fire on ordinary users."""
    emails, _ = cleanup
    email = _fresh("ordinary")
    with TestClient(app) as client:
        admin = _admin(client)
        uid = _make_member(client, admin, email)
        emails.add(email)
        resp = client.patch(f"/users/{uid}", json={"is_active": False}, headers=admin)
    assert resp.status_code == 200


def test_patching_an_unknown_user_is_404(cleanup):
    _skip_if_no_db()
    with TestClient(app) as client:
        admin = _admin(client)
        resp = client.patch(
            "/users/999999999", json={"is_active": False}, headers=admin
        )
    assert resp.status_code == 404


def test_patching_a_user_is_admin_only(cleanup):
    emails, _ = cleanup
    actor, target = _fresh("actor"), _fresh("target")
    with TestClient(app) as client:
        admin = _admin(client)
        _make_member(client, admin, actor)
        target_id = _make_member(client, admin, target)
        emails.update({actor, target})

        resp = client.patch(
            f"/users/{target_id}",
            json={"is_active": False},
            headers=_token(client, actor),
        )
    assert resp.status_code == 403


def test_the_patch_body_rejects_unknown_fields(cleanup):
    """`role` is deliberately NOT patchable here — see the endpoint docstring."""
    emails, _ = cleanup
    email = _fresh("noescalate")
    with TestClient(app) as client:
        admin = _admin(client)
        uid = _make_member(client, admin, email)
        emails.add(email)
        resp = client.patch(
            f"/users/{uid}", json={"is_active": True, "role": "admin"}, headers=admin
        )
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Granting a department by email
# --------------------------------------------------------------------------

def test_a_department_can_be_granted_by_email(cleanup):
    emails, codes = cleanup
    email = _fresh("grantee")
    code = f"g{uuid.uuid4().hex[:6]}"
    with TestClient(app) as client:
        admin = _admin(client)
        _make_member(client, admin, email)
        emails.add(email)
        client.post("/v1/departments", json={"code": code, "name": "G"}, headers=admin)
        codes.add(code)

        granted = client.post(
            f"/v1/departments/{code}/members", json={"email": email}, headers=admin
        )
        theirs = _token(client, email)
        visible = client.get("/v1/departments", headers=theirs).json()

    assert granted.status_code == 204
    assert [d["code"] for d in visible] == [code]


def test_granting_by_user_id_still_works(cleanup):
    """Regression: the existing frontend and tests send user_id."""
    emails, codes = cleanup
    email = _fresh("byid")
    code = f"g{uuid.uuid4().hex[:6]}"
    with TestClient(app) as client:
        admin = _admin(client)
        uid = _make_member(client, admin, email)
        emails.add(email)
        client.post("/v1/departments", json={"code": code, "name": "G"}, headers=admin)
        codes.add(code)

        resp = client.post(
            f"/v1/departments/{code}/members", json={"user_id": uid}, headers=admin
        )
    assert resp.status_code == 204


def test_granting_to_an_unknown_email_is_404(cleanup):
    emails, codes = cleanup
    code = f"g{uuid.uuid4().hex[:6]}"
    with TestClient(app) as client:
        admin = _admin(client)
        client.post("/v1/departments", json={"code": code, "name": "G"}, headers=admin)
        codes.add(code)
        resp = client.post(
            f"/v1/departments/{code}/members",
            json={"email": "nobody-at-all-xyz@example.com"},
            headers=admin,
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Unknown user"


def test_supplying_both_user_id_and_email_is_422(cleanup):
    emails, codes = cleanup
    code = f"g{uuid.uuid4().hex[:6]}"
    with TestClient(app) as client:
        admin = _admin(client)
        client.post("/v1/departments", json={"code": code, "name": "G"}, headers=admin)
        codes.add(code)
        resp = client.post(
            f"/v1/departments/{code}/members",
            json={"user_id": 1, "email": "someone@example.com"},
            headers=admin,
        )
    assert resp.status_code == 422


def test_supplying_neither_user_id_nor_email_is_422(cleanup):
    emails, codes = cleanup
    code = f"g{uuid.uuid4().hex[:6]}"
    with TestClient(app) as client:
        admin = _admin(client)
        client.post("/v1/departments", json={"code": code, "name": "G"}, headers=admin)
        codes.add(code)
        resp = client.post(
            f"/v1/departments/{code}/members", json={}, headers=admin
        )
    assert resp.status_code == 422
